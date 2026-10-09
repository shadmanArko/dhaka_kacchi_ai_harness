"""Create the RAG database, its pgvector extension, and its two roles.

Mirrors warehouse/bootstrap_db.py's shape (idempotent CREATE DATABASE,
AUTOCOMMIT for DDL, catch-a-concurrent-create-as-success), extended with
two things the warehouse script doesn't need: enabling the `vector`
extension inside the new database, and creating the rag_writer / rag_reader
roles (see RAG_progress.md decisions #7-#9 for why those roles exist and
why their names are hardcoded here rather than read from config).

Safe to run any number of times - every step checks "does this already
exist" before creating anything.
"""

# Same "evaluate type hints as text" import as config.py - keeps this file
# consistent with the rest of the codebase even though nothing here
# actually needs a forward-referenced type.
from __future__ import annotations

# `secrets` is the standard library's module for generating
# cryptographically random values - safe to use for real passwords, unlike
# the ordinary `random` module (which is predictable enough to be guessed,
# since it's designed for things like games/simulations, not security).
import secrets

# `sys` gives us `sys.stderr` and `sys.exit`-style return codes, used below
# to report a config error the same way warehouse/bootstrap_db.py does.
import sys

# The conventional alias for SQLAlchemy - `sa.text(...)` lets us run raw
# SQL strings safely (with bind parameters), and `sa.create_engine(...)`
# opens an actual connection pool to Postgres.
import sqlalchemy as sa

# The specific exception SQLAlchemy raises when Postgres rejects a
# statement (e.g. a duplicate CREATE) - we catch this below to tell a real
# error apart from "someone else already created this at the same time".
from sqlalchemy.exc import ProgrammingError

# Pull in this subsystem's own config loader/dataclass/error type - the
# only thing this script is allowed to use to find out how to connect,
# per the "config.py is the one place that reads os.environ" house rule.
from rag.config import ConfigError, RagAdminSettings, load_rag_admin_settings

# These three names are architectural decisions, not environment-specific
# settings (see RAG_progress.md decision #9) - so unlike warehouse/'s
# database name, which comes from DATABASE_URL, these are hardcoded here.
# Flagged in RAG_progress.md as worth revisiting later, not as final.
RAG_DATABASE_NAME = "dhaka_kacchi_rag"
RAG_WRITER_ROLE = "rag_writer"

# The two reader roles introduced by the multi-store design (see
# rag/MULTI_STORE_DESIGN.md section 6). There is one role per AUDIENCE, never
# one per table: a role answers "who is asking", not "what are they asking
# for". Adding a store never creates a role; adding a genuinely different
# kind of caller does.
#
#   rag_public_reader   - may read stores whose visibility is "public". Used
#                         by anything facing people we do not trust, such as
#                         a customer-facing chatbot.
#   rag_internal_reader - may read every store. Made a MEMBER of the public
#                         role (see create_roles further down), so it
#                         inherits every public-store grant automatically and
#                         those grants only ever have to be written once.
RAG_PUBLIC_READER_ROLE = "rag_public_reader"
RAG_INTERNAL_READER_ROLE = "rag_internal_reader"

# RETIRED 2026-10-09: `rag_reader`, the single reader role from before the
# multi-store design. It could read the original `chunks` table and nothing
# else, and both went together. A database that predates that date may still
# have the role (and `RAG_READER_DATABASE_URL` in its .env) hanging around;
# rag/RUNBOOK.md has the two commands that remove it. New setups never create
# it - this script is the only thing that ever did.

# The Postgres error code ("SQLSTATE") for "you tried to create something
# that already exists" - Postgres assigns a stable 5-character code to
# every category of error, and this is the one for a duplicate database.
# Checking this exact code (rather than just "any error happened") is what
# lets us tell "harmless race with another run of this same script" apart
# from "something is actually broken".
DUPLICATE_DATABASE = "42P04"

# The equivalent SQLSTATE for "this role/user already exists" - used the
# same way, for the CREATE ROLE statements further down.
DUPLICATE_OBJECT = "42710"


def quote_identifier(identifier: str) -> str:
    """Quote a Postgres identifier. Identifiers cannot be bind parameters.

    Public (no leading underscore) because rag/schema.py needs exactly this
    same escaping for the table and role names it interpolates into GRANT
    statements - sharing the one implementation is safer than having two
    copies that could drift apart.
    """
    # Postgres identifiers (table names, database names, role names) can't
    # be passed as query parameters the way values can - SQL simply has no
    # syntax for "a parameter standing in for a name". So instead we wrap
    # the name in double quotes ourselves, and double up any literal quote
    # character inside it (the standard SQL escaping rule), which makes it
    # syntactically safe even if the name were adversarial.
    return '"' + identifier.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    """Quote a Postgres string literal for splicing directly into SQL text.

    Needed specifically for CREATE ROLE's PASSWORD clause - unlike almost
    every other value in this codebase, Postgres's grammar for that one
    clause only accepts a literal string, not a bind parameter (`$1`).
    Confirmed by actually running this against the real Docker instance:
    `CREATE ROLE ... PASSWORD :pwd` failed with "syntax error at or near
    $1" even though the exact same bind-parameter pattern works everywhere
    else in this file (e.g. the `:n` in create_database's SELECT). Doubling
    any embedded single quote is the standard SQL escaping rule - the
    randomly generated passwords here never contain one, but this is
    written to be safe regardless of how the value was produced.
    """
    return "'" + value.replace("'", "''") + "'"


def _admin_engine(settings: RagAdminSettings) -> sa.Engine:
    # CREATE DATABASE cannot run inside a transaction block, but SQLAlchemy
    # opens one implicitly by default on every connection - AUTOCOMMIT
    # turns that off, so each statement takes effect immediately on its
    # own, which is required for CREATE DATABASE/CREATE ROLE to work at
    # all. NullPool means "don't keep connections open between uses" -
    # appropriate here since this script runs once and exits, not as a
    # long-lived service.
    return sa.create_engine(
        settings.sqlalchemy_url, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool
    )


def admin_engine_on_rag_db(settings: RagAdminSettings) -> sa.Engine:
    """An admin engine pointed at dhaka_kacchi_rag ITSELF, not at the
    maintenance database.

    `_admin_engine` above connects wherever the admin URL points - which,
    for one-time setup, is the `postgres` maintenance database, because
    dhaka_kacchi_rag does not exist yet the first time this script runs.
    Every LATER admin task (creating tables, granting privileges, verifying
    grants) has to happen INSIDE the real database instead, and this is the
    shared way to get there: take the admin URL and swap just the
    database-name portion.

    Public (no leading underscore) because both rag/schema.py and
    rag/verify_stores.py need exactly this, and two copies of it would
    eventually disagree about something.
    """
    # `.set(database=...)` returns a new URL object with just that one part
    # changed - the host, port, user and password are carried over untouched.
    target_url = settings.sqlalchemy_url.set(database=RAG_DATABASE_NAME)
    return sa.create_engine(target_url, poolclass=sa.pool.NullPool)


def create_database(settings: RagAdminSettings) -> int:
    """CREATE DATABASE dhaka_kacchi_rag, unless it already exists."""
    # Open one admin connection (to the `postgres` maintenance database,
    # per RagAdminSettings' own docstring) that we'll reuse for every check
    # in this function.
    with _admin_engine(settings).connect() as conn:
        # Ask Postgres' own catalog table whether a database with this name
        # is already registered. `:n` is a bind parameter - SQLAlchemy
        # substitutes it safely, escaping anything unusual in the value,
        # which is why this check (unlike the identifier quoting above) is
        # allowed to use a plain parameter.
        exists = conn.execute(
            sa.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": RAG_DATABASE_NAME}
        ).scalar()
        if exists:
            # Nothing to do - print a status line and return a success
            # code (0), matching the Unix convention this script's caller
            # (the Makefile) expects.
            print(f"database {RAG_DATABASE_NAME!r} already exists")
            return 0
        try:
            # TEMPLATE template1 inherits the same encoding/collation the
            # rest of this Postgres cluster already uses, same reasoning as
            # warehouse/bootstrap_db.py's identical line.
            conn.execute(
                sa.text(
                    f"CREATE DATABASE {quote_identifier(RAG_DATABASE_NAME)} "
                    "TEMPLATE template1 ENCODING 'UTF8'"
                )
            )
        except ProgrammingError as exc:
            # The exists-check above and this CREATE aren't atomic as a
            # pair - it's possible for another process to create the same
            # database in the gap between them. If that's exactly what
            # happened (the error's SQLSTATE says "duplicate database"),
            # that's a race we lost harmlessly, not a real failure - the
            # database exists either way, which is all we actually wanted.
            if getattr(exc.orig, "sqlstate", None) == DUPLICATE_DATABASE:
                print(f"database {RAG_DATABASE_NAME!r} was created concurrently")
                return 0
            # Any other kind of error is a genuine problem - let it
            # propagate up instead of pretending it succeeded.
            raise
    print(f"created database {RAG_DATABASE_NAME!r}")
    return 0


def create_extension(settings: RagAdminSettings) -> int:
    """CREATE EXTENSION vector inside dhaka_kacchi_rag itself.

    This has to run as a *separate* connection from create_database(),
    pointed at dhaka_kacchi_rag rather than the postgres maintenance
    database - an extension is enabled inside a specific database, not at
    the whole-server level.
    """
    # Take the same admin URL used to create the database, but swap just
    # the database name portion so this connection lands inside
    # dhaka_kacchi_rag itself (which now exists, since create_database()
    # already ran) instead of the `postgres` maintenance database.
    target_url = settings.sqlalchemy_url.set(database=RAG_DATABASE_NAME)
    engine = sa.create_engine(target_url, isolation_level="AUTOCOMMIT", poolclass=sa.pool.NullPool)
    with engine.connect() as conn:
        # `IF NOT EXISTS` makes this line idempotent on its own - Postgres
        # itself handles "already enabled" as a silent no-op, so there's no
        # need for a manual exists-check/except dance like create_database
        # needed (CREATE EXTENSION doesn't raise on a duplicate the way
        # CREATE DATABASE does).
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    print("ensured extension 'vector' is enabled in dhaka_kacchi_rag")
    return 0


def _generate_password() -> str:
    # token_urlsafe(24) produces a random string from 24 bytes of real
    # randomness, encoded so it's safe to put straight into a connection
    # URL (no characters that would need escaping, unlike raw bytes).
    # 24 bytes is comfortably more randomness than anything short enough
    # to realistically guess or brute-force.
    return secrets.token_urlsafe(24)


def _create_role_if_absent(conn: sa.Connection, role_name: str) -> str | None:
    """Shared helper for the two CREATE ROLE calls below - same
    check-then-create-and-catch-the-race shape as create_database(), just
    scoped to a single role instead of a whole database.

    Returns the newly-generated password if this call actually created the
    role, or None if the role already existed (in which case its existing
    password is left untouched - we never overwrite a password we don't
    already know, since that could silently lock out whatever already had
    the old one).
    """
    password = _generate_password()
    try:
        # LOGIN means this role is allowed to actually authenticate and
        # open a connection (a role without LOGIN can only be used for
        # permission-grouping, not for connecting) - both rag_writer and
        # rag_reader need to log in, since real processes connect as them.
        # NOSUPERUSER/NOCREATEDB/NOCREATEROLE are explicit, not just
        # defaults, so the restriction is visible in the SQL itself rather
        # than relying on Postgres' default being safe.
        # PASSWORD has to be spliced into the SQL text as a quoted literal,
        # NOT passed as a bind parameter - Postgres's grammar for CREATE
        # ROLE's PASSWORD clause only accepts a literal string there, not a
        # parameter placeholder (`$1`). Confirmed the hard way: the bind-
        # parameter version of this line failed against the real database
        # with "syntax error at or near $1" even though that exact pattern
        # works for every other value in this file. _quote_literal() does
        # the escaping quote_identifier() does for identifiers, just for
        # string values instead of names.
        conn.execute(
            sa.text(
                f"CREATE ROLE {quote_identifier(role_name)} "
                f"WITH LOGIN PASSWORD {_quote_literal(password)} "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE"
            )
        )
        print(f"created role {role_name!r}")
        return password
    except ProgrammingError as exc:
        if getattr(exc.orig, "sqlstate", None) == DUPLICATE_OBJECT:
            # Same race-tolerance reasoning as create_database(): if the
            # role already exists (created by an earlier run, or
            # concurrently by another process), that's the desired end
            # state, not a failure - but we have no way to know its
            # existing password, so there's nothing to print for it.
            print(f"role {role_name!r} already exists (password unchanged, not shown)")
            return None
        raise


def create_roles(settings: RagAdminSettings) -> int:
    """CREATE the writer role and both reader roles, unless they already
    exist - each with a freshly generated random password, printed ONCE so it
    can be copied into .env. There is no way to retrieve a Postgres role's
    password after the fact (Postgres only ever stores a hash of it, never
    the plaintext) - if this output is lost, the only fix is resetting the
    password with ALTER ROLE, not recovering the original.

    Three roles, deliberately NOT one per table: a role answers "who is
    asking", never "what are they asking for" (see the role constants'
    comment at the top of this file, and rag/MULTI_STORE_DESIGN.md section
    6). WHICH tables each reader may actually see is decided later, by the
    grants that rag/schema.py applies.

    Table grants are therefore intentionally NOT set here - the tables don't
    exist yet at this point. There is exactly one exception, below: the role
    MEMBERSHIP, which is a relationship between two roles rather than a
    privilege on a table, and so can be established immediately.
    """
    with _admin_engine(settings).connect() as conn:
        # The ingestion identity: writes to every store, reads none of them
        # for its own purposes.
        writer_password = _create_role_if_absent(conn, RAG_WRITER_ROLE)

        # The two reader identities introduced by the multi-store design.
        public_password = _create_role_if_absent(conn, RAG_PUBLIC_READER_ROLE)
        internal_password = _create_role_if_absent(conn, RAG_INTERNAL_READER_ROLE)

        # Make the internal reader a MEMBER of the public reader. From here
        # on, every grant made to the public role is automatically inherited
        # by the internal one - so a public store's SELECT only ever has to be
        # written once, however many reader tiers sit above it.
        #
        # Note the direction, which is the opposite of how it first reads: the
        # MORE privileged identity is granted the LESSER one's rights, never
        # the other way round. Granting the public role to itself through the
        # internal one would hand public callers everything.
        #
        # Re-running this is harmless: Postgres treats a repeat membership
        # grant as a no-op notice, not an error.
        conn.execute(
            sa.text(
                f"GRANT {quote_identifier(RAG_PUBLIC_READER_ROLE)} "
                f"TO {quote_identifier(RAG_INTERNAL_READER_ROLE)}"
            )
        )

    # Print any freshly generated passwords together, clearly labelled, at
    # the very end - easier to find and copy than if they were scattered
    # between the individual "created role" lines above.
    if writer_password or public_password or internal_password:
        print()
        print("=== SAVE THESE NOW - shown only this once ===")
        if writer_password:
            print(f"  {RAG_WRITER_ROLE} password: {writer_password}")
        if public_password:
            print(f"  {RAG_PUBLIC_READER_ROLE} password: {public_password}")
        if internal_password:
            print(f"  {RAG_INTERNAL_READER_ROLE} password: {internal_password}")
        print("Copy these into .env as part of RAG_WRITER_DATABASE_URL,")
        print("RAG_PUBLIC_READER_DATABASE_URL and RAG_INTERNAL_READER_DATABASE_URL.")
        print("===============================================")

    return 0


def main() -> int:
    """Entry point: run all three setup steps in order.

    Order matters: the database has to exist before the extension can be
    enabled inside it, but role creation doesn't depend on either of the
    other two, so it's placed last mostly for readability (create the
    thing, prepare it, then create who's allowed to use it).
    """
    try:
        # Load and validate the admin connection details up front - if
        # RAG_ADMIN_DATABASE_URL is missing or malformed, fail here with a
        # clear message rather than partway through creating things.
        settings = load_rag_admin_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    # Run each step in turn; if any one of them returns a non-zero (error)
    # status, stop immediately and surface that status rather than
    # continuing on to the next step with a half-finished setup.
    for step in (create_database, create_extension, create_roles):
        status = step(settings)
        if status != 0:
            return status
    return 0


# Only run main() when this file is executed directly (e.g.
# `python -m rag.bootstrap_db`), not when it's imported by something else
# (e.g. a test importing create_database to call it directly).
if __name__ == "__main__":
    raise SystemExit(main())
