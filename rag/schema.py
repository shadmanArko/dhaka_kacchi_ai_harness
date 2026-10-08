"""Create the RAG vector tables inside dhaka_kacchi_rag, and grant each role
exactly the privileges it needs on them.

Two generations of table live side by side while the multi-store migration is
in progress (see rag/MULTI_STORE_DESIGN.md):

  * the LEGACY single table, `chunks`, holding both corpora, and
  * one table PER STORE, named by rag/stores.toml, each with a visibility
    tier that decides which reader roles are granted SELECT on it.

Everything here is registry-driven: adding a store to rag/stores.toml and
re-running this script creates its table and grants the right roles, with no
code change.

Run this AFTER rag/bootstrap_db.py - it depends on the database, the `vector`
extension, and all four roles already existing. See RAG_progress.md decision
#10 for the column-by-column reasoning behind this table shape, and decision
#11 for why this is a plain script rather than an Alembic migration.

Safe to run any number of times - every statement is written to be a no-op
when it has already been applied.
"""

# Same "evaluate type hints lazily" import as every other file in rag/.
from __future__ import annotations

import sys

# `sa` for raw SQL execution, same as bootstrap_db.py.
import sqlalchemy as sa

# Everything this module needs from the bootstrap script: the database name,
# all four role names, and the shared identifier-quoting helper. We only need
# admin settings here too - creating a table and granting privileges both
# require elevated rights, not the restricted rights the other roles have.
from rag.bootstrap_db import (
    RAG_DATABASE_NAME,
    RAG_INTERNAL_READER_ROLE,
    RAG_PUBLIC_READER_ROLE,
    RAG_READER_ROLE,
    RAG_WRITER_ROLE,
    admin_engine_on_rag_db,
    quote_identifier,
)

# The store registry describes WHICH tables should exist and what visibility
# each one has; this module is the code that actually makes that true.
from rag.config import (
    ConfigError,
    RagAdminSettings,
    StoreRegistry,
    load_rag_admin_settings,
    load_store_registry,
)

# The fixed list of source formats every store table accepts, matching what
# was decided in RAG_progress.md decision #10. Written once here as a Python
# tuple so the CHECK constraint's SQL text and this comment can't drift apart
# from each other by accident.
# "html" added 2026-10-02 (decision #26) - real SEC EDGAR exhibit documents
# turned up in rag/Knowledge_Base/, and the original decision #10 list was all
# file formats that existed as of that decision, not an exhaustive final list -
# exactly what text+CHECK (over a native ENUM) is FOR: widening this later
# without a breaking migration.
SOURCE_TYPES = ("pdf", "markdown", "docx", "txt", "csv", "xlsx", "html")

# The LEGACY single table's name. Kept as a named constant (rather than being
# passed to _create_store_table directly) so the day this table is dropped,
# a search for this name finds every remaining reference.
LEGACY_CHUNKS_TABLE = "chunks"


def _source_type_values_sql() -> str:
    """Render SOURCE_TYPES as the SQL fragment 'pdf','markdown',... .

    Built from the Python tuple above, so the CHECK constraint's allowed
    values always match SOURCE_TYPES exactly - there is no second list to
    forget to update.
    """
    return ", ".join(f"'{value}'" for value in SOURCE_TYPES)


def _create_store_table(settings: RagAdminSettings, *, table: str) -> None:
    """CREATE TABLE <table>, shaped exactly like every other store table.

    Every store table has an IDENTICAL shape - same columns, same types, same
    constraints - and only the name differs. That is deliberate: it is what
    lets retrieval.py run the same query against any store, without knowing
    or caring which one it was pointed at.

    Constraint names are prefixed with the table name (`uq_<table>_...`,
    `ck_<table>_...`) because Postgres constraint names only have to be
    unique per table, but readable names make a failure say which table was
    involved. For the legacy table this reproduces the original names
    exactly (`uq_chunks_source_path_chunk_index`, `ck_chunks_source_type`).

    Idempotent: the table itself uses IF NOT EXISTS, and the CHECK constraint
    is drop-then-add so that it CONVERGES on every run rather than being
    skipped when the table already exists.
    """
    # Quoted here, once, and reused for every statement below - see
    # config.py's _IDENTIFIER_PATTERN for the validation that already
    # guarantees this is a plain lowercase identifier. Quoting anyway is
    # cheap insurance.
    quoted_table = quote_identifier(table)

    with admin_engine_on_rag_db(settings).begin() as conn:
        # `.begin()` (rather than `.connect()`) opens an explicit transaction
        # that auto-commits on success and auto-rolls-back on any exception -
        # appropriate here since we want the table AND its constraint to each
        # be all-or-nothing.
        conn.execute(
            sa.text(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted_table} (
                    -- id: the row's own identity. A UUID (rather than a
                    -- counter) because chunks are written in parallel by the
                    -- loader and a UUID needs no coordination; and
                    -- gen_random_uuid() is built into Postgres 13+, so this
                    -- needs no extension beyond pgvector.
                    -- Named explicitly: this repo's convention is that
                    -- Postgres' own `<table>_pkey` default is the one naming
                    -- pattern that must not be relied on.
                    -- DEFAULT gen_random_uuid() matters: the ingestion
                    -- pipeline never supplies an id itself, it lets the
                    -- database invent one and reads it back via RETURNING.
                    id uuid NOT NULL DEFAULT gen_random_uuid(),
                    -- source_type: which kind of source document this chunk
                    -- was cut from - text+CHECK, never a native Postgres
                    -- ENUM, matching this repo's own convention (an ENUM
                    -- value can never be dropped once used; a CHECK's allowed
                    -- list can be widened with a plain ALTER TABLE later).
                    source_type text NOT NULL,
                    -- source_path: where the original document lives, so a
                    -- human or agent can go look at the real source if a
                    -- chunk's content is ever wrong or needs updating.
                    source_path text NOT NULL,
                    -- chunk_index: this chunk's position within that source
                    -- document (0, 1, 2, ...) - source_path alone isn't
                    -- precise enough for a multi-page/multi-section source,
                    -- since many chunks can come from one file.
                    chunk_index integer NOT NULL,
                    -- chunk_text: the actual retrievable text content of this
                    -- chunk - what gets shown to the agent/LLM once this row
                    -- is retrieved.
                    chunk_text text NOT NULL,
                    -- embedding: the 1024-dimensional vector representation
                    -- of chunk_text, produced by the BAAI/bge-m3 model
                    -- (RAG_progress.md decision #17, switched from the
                    -- 384-dim all-MiniLM-L6-v2 after it badly mangled real
                    -- Bengali captions) - this is what similarity search
                    -- actually compares against.
                    embedding vector(1024) NOT NULL,
                    -- metadata: anything extra about this chunk that varies
                    -- by source type and doesn't deserve its own fixed column
                    -- - e.g. a PDF page number, a markdown heading, a CSV row
                    -- range. Stored as jsonb (flexible shape), matching the
                    -- same pattern already used by customer.contact_hashes /
                    -- ingredient.price_history / review.aspects elsewhere in
                    -- this codebase.
                    metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb,
                    -- created_at: when this row was first inserted - set
                    -- once, never touched again after that.
                    created_at timestamptz NOT NULL DEFAULT now(),
                    -- updated_at: when this row was last changed (initial
                    -- insert OR a later upsert) - the ingestion job is
                    -- responsible for setting this to now() on every upsert,
                    -- not just at first insert.
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    -- The primary key, named explicitly per this repo's
                    -- naming convention.
                    CONSTRAINT pk_{table} PRIMARY KEY (id),
                    -- UNIQUE (source_path, chunk_index): the constraint that
                    -- makes re-running ingestion on an unchanged source an
                    -- idempotent upsert instead of a pile of duplicate rows -
                    -- same shape as orders' own UNIQUE (channel, external_id).
                    CONSTRAINT uq_{table}_source_path_chunk_index
                        UNIQUE (source_path, chunk_index)
                )
                """
            )
        )

        # The CHECK constraint is applied as a SEPARATE step, always run,
        # rather than inline in CREATE TABLE - CREATE TABLE IF NOT EXISTS is
        # existence-idempotent, not definition-convergent (same lesson already
        # documented in this repo's own CLAUDE.md "Alembic traps" section): on
        # an ALREADY-EXISTING table, widening SOURCE_TYPES above and re-running
        # this script would otherwise silently do nothing, leaving the live
        # constraint stale. Drop-then-add converges it to match SOURCE_TYPES on
        # every run, exactly the same "replace_constraint()" idea already used
        # elsewhere in this codebase for the same reason.
        conn.execute(
            sa.text(f"ALTER TABLE {quoted_table} DROP CONSTRAINT IF EXISTS ck_{table}_source_type")
        )
        conn.execute(
            sa.text(
                f"ALTER TABLE {quoted_table} ADD CONSTRAINT ck_{table}_source_type "
                f"CHECK (source_type IN ({_source_type_values_sql()}))"
            )
        )


def create_chunks_table(settings: RagAdminSettings) -> int:
    """Create the LEGACY single table, `chunks`.

    Kept only so the existing system keeps working while the multi-store
    migration is in progress. It holds both corpora today and is dropped once
    the per-store tables have been populated and verified (design doc phase
    6) - at which point this function and `grant_privileges` below are
    deleted together.
    """
    # Same shape as every store table - literally the same code, pointed at
    # the legacy name.
    _create_store_table(settings, table=LEGACY_CHUNKS_TABLE)
    print(f"ensured legacy table {LEGACY_CHUNKS_TABLE!r} exists in {RAG_DATABASE_NAME}")
    return 0


def create_store_tables(settings: RagAdminSettings, registry: StoreRegistry) -> int:
    """Create one table per store in the registry.

    Idempotent, and safe to run before the tables are populated: this only
    ever creates EMPTY tables with the right shape, never writes data.
    """
    # Walk the registry in file order, creating each store's table.
    for store in registry:
        _create_store_table(settings, table=store.table)
        # Say which LOGICAL store mapped to which physical table, so the
        # output double-checks the config's own indirection.
        print(f"ensured store {store.name!r} -> table {store.table!r} exists")
    return 0


def grant_privileges(settings: RagAdminSettings) -> int:
    """LEGACY grants: rag_writer and rag_reader, on the single `chunks` table.

    This is the step that enforces the original "agents can only read, never
    write" rule from RAG_progress.md decision #4/#7 - role creation alone
    (bootstrap_db.py) doesn't grant any table access by itself.

    Retired together with `chunks` itself, once the migration completes; the
    multi-store equivalent is `grant_store_privileges` below.
    """
    with admin_engine_on_rag_db(settings).begin() as conn:
        # A role needs CONNECT on the database before anything else it's
        # granted can matter - stated explicitly here rather than relying on
        # Postgres' own default (new roles can usually already connect to any
        # database by default, but writing this out makes the intent visible
        # in the SQL itself instead of depending on a server-wide default that
        # could be different on another machine).
        conn.execute(
            sa.text(
                f"GRANT CONNECT ON DATABASE {quote_identifier(RAG_DATABASE_NAME)} "
                f"TO {quote_identifier(RAG_WRITER_ROLE)}"
            )
        )
        conn.execute(
            sa.text(
                f"GRANT CONNECT ON DATABASE {quote_identifier(RAG_DATABASE_NAME)} "
                f"TO {quote_identifier(RAG_READER_ROLE)}"
            )
        )
        # rag_writer: the recurring ingestion job needs to add new chunks and
        # update existing ones (the upsert-on-re-ingest behaviour) - SELECT is
        # included too, because an upsert has to be able to check "does this
        # row already exist" before deciding whether to INSERT or UPDATE it.
        # DELETE added 2026-10-02 (RAG_progress.md decision #25) specifically
        # for the re-index "prune orphaned chunks" step - a deliberate,
        # confirmed expansion of this role's privileges, not an oversight.
        # Still no DDL rights of any kind, and still scoped to this one table.
        conn.execute(
            sa.text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {quote_identifier(LEGACY_CHUNKS_TABLE)} "
                f"TO {quote_identifier(RAG_WRITER_ROLE)}"
            )
        )
        # rag_reader: agents only ever read at query time - SELECT and nothing
        # else, structurally incapable of changing any row.
        conn.execute(
            sa.text(
                f"GRANT SELECT ON {quote_identifier(LEGACY_CHUNKS_TABLE)} "
                f"TO {quote_identifier(RAG_READER_ROLE)}"
            )
        )
    print(
        f"granted {LEGACY_CHUNKS_TABLE!r} privileges to "
        f"{RAG_WRITER_ROLE!r} and {RAG_READER_ROLE!r}"
    )
    return 0


def grant_store_privileges(settings: RagAdminSettings, registry: StoreRegistry) -> int:
    """Grant each role exactly what it needs on each STORE table.

    This is where the whole multi-store design is enforced, and it comes down
    to one idea: which roles a table's SELECT is granted to is decided by its
    `visibility` in rag/stores.toml, and the PUBLIC reader role is simply
    never granted anything marked internal.

    There is no code anywhere that checks "is this caller allowed to read
    that store" - there is only the absence of a grant, which Postgres
    enforces before a single row can be returned (design doc section 1).
    """
    with admin_engine_on_rag_db(settings).begin() as conn:
        # Both reader roles need CONNECT on the database before any table
        # grant of theirs can take effect - same reasoning as the legacy
        # grants above.
        for role in (RAG_PUBLIC_READER_ROLE, RAG_INTERNAL_READER_ROLE):
            conn.execute(
                sa.text(
                    f"GRANT CONNECT ON DATABASE {quote_identifier(RAG_DATABASE_NAME)} "
                    f"TO {quote_identifier(role)}"
                )
            )

        # Walk the registry once, granting per store.
        for store in registry:
            quoted_table = quote_identifier(store.table)

            # A store marked public is readable by the public role. The
            # internal role does NOT need its own grant here: it is a MEMBER
            # of the public role (see bootstrap_db.py), so it inherits this
            # one automatically. That is why public grants are written once,
            # no matter how many reader tiers exist above them.
            if store.visibility == "public":
                conn.execute(
                    sa.text(
                        f"GRANT SELECT ON {quoted_table} "
                        f"TO {quote_identifier(RAG_PUBLIC_READER_ROLE)}"
                    )
                )

            # An internal store is granted to the internal role ONLY. For a
            # store marked internal, the absence of a public grant on the
            # line above IS the security model.
            else:
                conn.execute(
                    sa.text(
                        f"GRANT SELECT ON {quoted_table} "
                        f"TO {quote_identifier(RAG_INTERNAL_READER_ROLE)}"
                    )
                )

            # Every store is writable by the single ingestion role, whatever
            # its visibility: visibility governs who may READ, not who may
            # maintain. Same privilege set the legacy table grants, for the
            # same reasons.
            conn.execute(
                sa.text(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON {quoted_table} "
                    f"TO {quote_identifier(RAG_WRITER_ROLE)}"
                )
            )

            # Which role just received this table's SELECT grant - exactly
            # one of the two ever does, since the internal role inherits the
            # public role's grants rather than being granted separately.
            reader_role = (
                RAG_PUBLIC_READER_ROLE
                if store.visibility == "public"
                else RAG_INTERNAL_READER_ROLE
            )

            # Report each one individually, so the output is a readable
            # record of exactly which grants were applied.
            print(
                f"granted {store.table!r} ({store.visibility}) -> "
                f"SELECT to {reader_role!r}, write to {RAG_WRITER_ROLE!r}"
            )
    return 0


def main() -> int:
    try:
        # Same fail-fast config loading as bootstrap_db.py - if the admin
        # connection details are missing or invalid, stop here with a clear
        # message. The registry is loaded up front too, so a malformed
        # stores.toml fails before any DDL has been attempted.
        settings = load_rag_admin_settings()
        registry = load_store_registry()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    # Create the tables first, then grant privileges on them - grants would
    # fail if a table didn't exist yet, so the order here isn't arbitrary.
    #
    # Legacy steps run first and are removed together with the `chunks` table
    # once the migration is finished (design doc phase 6).
    for legacy_step in (create_chunks_table, grant_privileges):
        status = legacy_step(settings)
        if status != 0:
            return status

    # Registry-driven steps: these are the ones that survive the migration.
    for store_step in (create_store_tables, grant_store_privileges):
        status = store_step(settings, registry)
        if status != 0:
            return status

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
