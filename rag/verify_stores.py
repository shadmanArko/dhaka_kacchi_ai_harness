"""Verify that the store registry's DECLARED visibility matches the REAL
Postgres grants.

This is the check described in rag/MULTI_STORE_DESIGN.md sections 5.4 and 13.
The registry in rag/stores.toml says what each store is meant to be; Postgres
grants are what is actually enforced. Those two can drift apart - someone adds
a store and forgets its grant, or widens one by hand while debugging - and a
drift in the dangerous direction (an internal store that the public role can
read) is invisible in the config file, which still says "internal".

Nothing in the running system consults this script. It exists so that drift
FAILS LOUDLY the next time anyone runs it, the same way `make verify` keeps
the warehouse's schema conventions honest.

    uv run python -m rag.verify_stores

Exit code 0 means every store agrees with its grants; exit code 1 means at
least one does not, and every disagreement is printed with the store, the
role, and which way it is wrong.
"""

from __future__ import annotations

import sys

# `sa` for the read-only catalogue queries below, same as every other module
# in rag/.
import sqlalchemy as sa

# The admin settings (and the engine helper) are used here for the same
# reason schema.py uses them: answering "what may role X do on table Y?"
# requires the rights to ask that question about arbitrary roles, which the
# reader and writer roles themselves do not have.
from rag.bootstrap_db import (
    RAG_INTERNAL_READER_ROLE,
    RAG_PUBLIC_READER_ROLE,
    RAG_WRITER_ROLE,
    admin_engine_on_rag_db,
)

# The registry being verified, and this module's usual fail-fast config style.
from rag.config import (
    ConfigError,
    RagAdminSettings,
    Store,
    StoreRegistry,
    load_rag_admin_settings,
    load_store_registry,
)


def _table_exists(conn: sa.Connection, table: str) -> bool:
    """Whether `table` exists in this database.

    Checked FIRST, because has_table_privilege() raises an error (rather than
    returning false) when asked about a table that isn't there - and "the
    table is missing entirely" is a mismatch this script should report, not
    crash on.

    `to_regclass` resolves a name to a table and returns NULL instead of
    raising when it cannot. The name is a bind parameter here: to_regclass
    takes a string VALUE, not an identifier position, so there is nothing to
    quote.
    """
    return (
        conn.execute(
            sa.text("SELECT to_regclass(:table) IS NOT NULL"),
            {"table": table},
        ).scalar()
        is True
    )


def _role_exists(conn: sa.Connection, role: str) -> bool:
    """Whether a role with this name exists.

    Same reasoning as _table_exists: has_table_privilege() raises for an
    unknown role, and a missing role is a finding to report rather than a
    crash. pg_roles is Postgres' own catalogue of roles.
    """
    return (
        conn.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"),
            {"role": role},
        ).scalar()
        is not None
    )


def _has_privilege(conn: sa.Connection, role: str, table: str, privilege: str) -> bool:
    """Whether `role` holds `privilege` on `table`.

    Unlike the current-user form in retrieval.py, this form names the role
    explicitly - which is the whole point here: the verifier is asking about
    the reader roles' rights, not its own. It still accounts for privileges
    inherited through role membership, so rag_internal_reader correctly comes
    back true for public stores it was never granted directly.
    """
    return (
        conn.execute(
            sa.text("SELECT has_table_privilege(:role, :table, :privilege)"),
            {"role": role, "table": table, "privilege": privilege},
        ).scalar()
        is True
    )


def verify_store(conn: sa.Connection, store: Store) -> list[str]:
    """Check one store against the real grants.

    Returns a list of human-readable problems - EMPTY means this store is
    exactly as the registry declares it. Every message names the store, the
    role and the table, because the fix is a grant (or a config edit) and the
    reader needs to know which one to change.
    """
    problems: list[str] = []

    # A store whose table does not exist yet cannot have correct grants. This
    # is expected during a phased rollout (config first, tables later), which
    # is exactly why it is reported as a finding rather than treated as an
    # error to crash on.
    if not _table_exists(conn, store.table):
        problems.append(
            f"{store.name}: table {store.table!r} does not exist "
            "(run `uv run python -m rag.schema`)"
        )
        # Without the table, none of the privilege questions below can be
        # answered - has_table_privilege would raise. Report and move on.
        return problems

    # --- the public reader, which is what the whole design is about --------
    public_has_select = _has_privilege(conn, RAG_PUBLIC_READER_ROLE, store.table, "SELECT")

    if store.visibility == "public":
        # A public store MUST be readable by the public role; otherwise the
        # config promises something the database does not deliver, and every
        # public search of it fails.
        if not public_has_select:
            problems.append(
                f"{store.name}: declared visibility 'public', but "
                f"{RAG_PUBLIC_READER_ROLE} has NO SELECT on {store.table!r}"
            )
    else:
        # An internal store MUST NOT be readable by the public role. This is
        # the check that matters most - a false here means an internal corpus
        # is exposed, and nothing else in the system would notice.
        if public_has_select:
            problems.append(
                f"{store.name}: declared visibility {store.visibility!r}, but "
                f"{RAG_PUBLIC_READER_ROLE} CAN read {store.table!r} - "
                "INTERNAL DATA IS EXPOSED"
            )

    # --- the internal reader, which must reach every store ----------------
    # The internal role is meant to read everything. If it cannot read a
    # store, the business's own agents silently lose access to it, so this is
    # a real problem in either direction.
    if not _has_privilege(conn, RAG_INTERNAL_READER_ROLE, store.table, "SELECT"):
        problems.append(
            f"{store.name}: {RAG_INTERNAL_READER_ROLE} has NO SELECT on "
            f"{store.table!r} - the internal reader must reach every store"
        )

    # --- the writer, which must be able to maintain every store -----------
    # Visibility governs who may READ, never who may maintain. Checked
    # privilege by privilege so a partial grant (say, INSERT but not DELETE)
    # is reported precisely - a missing DELETE would break the prune, which
    # is a silent data-staleness bug rather than a visible failure.
    for privilege in ("INSERT", "UPDATE", "DELETE"):
        if not _has_privilege(conn, RAG_WRITER_ROLE, store.table, privilege):
            problems.append(
                f"{store.name}: {RAG_WRITER_ROLE} lacks {privilege} on "
                f"{store.table!r}"
            )

    return problems


def verify_stores(settings: RagAdminSettings, registry: StoreRegistry) -> list[str]:
    """Check every store in the registry.

    Returns the combined list of problems across all stores; an empty list
    means the config and the database agree completely.
    """
    problems: list[str] = []

    with admin_engine_on_rag_db(settings).connect() as conn:
        # If a reader role is missing entirely, every check below would
        # either raise or produce a confusing cascade of per-store failures.
        # Report the root cause once and stop.
        for role in (RAG_PUBLIC_READER_ROLE, RAG_INTERNAL_READER_ROLE, RAG_WRITER_ROLE):
            if not _role_exists(conn, role):
                problems.append(
                    f"role {role!r} does not exist "
                    "(run `uv run python -m rag.bootstrap_db`)"
                )
        if problems:
            return problems

        # Walk the registry, collecting each store's problems in order.
        for store in registry:
            problems.extend(verify_store(conn, store))

    return problems


def main() -> int:
    # Same fail-fast config loading as every other entry point: a broken
    # admin URL or a malformed stores.toml should stop here with a clear
    # message, not part-way through the report.
    try:
        settings = load_rag_admin_settings()
        registry = load_store_registry()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    problems = verify_stores(settings, registry)

    if problems:
        # Every finding, one per line, so this reads well in a terminal and
        # in CI output alike.
        print(f"FAIL: {len(problems)} problem(s) found between stores.toml and the database:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    # A short positive summary, naming each store and the tier it was
    # confirmed at - so a passing run still shows WHAT was actually checked,
    # rather than just "ok".
    print(f"OK: {len(registry)} store(s) verified against the real grants")
    for store in registry:
        print(f"  - {store.name} ({store.visibility}) -> {store.table}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
