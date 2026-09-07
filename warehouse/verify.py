"""Assert the live schema matches house conventions.

WHY THIS EXISTS. The migrations are idempotent via Postgres' ``IF NOT EXISTS``
grammar, which is *existence*-idempotent but not *definition*-convergent: a
table that already exists with the wrong shape is skipped with only a NOTICE.
No guard available in Alembic or Postgres closes that gap. This module is the
compensating control - it inspects what actually landed.

Deliberately INTROSPECTION-driven, not manifest-driven. A hardcoded list of
expected columns would rot within a week and then be ignored. Every check below
is a rule about shape, so it keeps holding as the schema grows.

Exit code 0 if every check passes, 1 otherwise.
"""

from __future__ import annotations

import sys

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

from warehouse.config import REPO_ROOT, ConfigError, Settings, load_settings

EXPECTED_TABLES = frozenset(
    {
        "customer",
        "menu_item",
        "supplier",
        "ingredient",
        "recipe",
        "orders",
        "order_line",
        "review",
        "ad_spend",
        "cockpit_alert",
        "agent_action",
    }
)

# Alembic owns this table; its varchar version_num is not ours to police.
IGNORED_TABLES = frozenset({"alembic_version"})

NAME_PREFIXES = ("idx_", "uq_", "pk_", "fk_", "ck_", "excl_")

_failures: list[str] = []
_checks_run = 0


def _check(name: str, problems: list[str]) -> None:
    global _checks_run
    _checks_run += 1
    if problems:
        _failures.append(name)
        print(f"  FAIL  {name}")
        for p in problems:
            print(f"          {p}")
    else:
        print(f"  ok    {name}")


def _tables_pred(alias: str = "c") -> str:
    ignored = ", ".join(f"'{t}'" for t in sorted(IGNORED_TABLES))
    return f"{alias}.table_schema = 'public' AND {alias}.table_name NOT IN ({ignored})"


def check_single_head_at_head(conn: sa.Connection) -> None:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    heads = list(script.get_heads())
    problems: list[str] = []
    if len(heads) != 1:
        # Hand-assigned 0001-style revision ids are only safe on a linear chain;
        # two authors would both grab the next ordinal. Catch a fork same-day.
        problems.append(f"expected exactly one head, found {len(heads)}: {heads}")
    applied = [
        r[0] for r in conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchall()
    ]
    if len(applied) != 1:
        problems.append(f"alembic_version has {len(applied)} rows: {applied}")
    elif heads and applied[0] != heads[0]:
        problems.append(f"database is at {applied[0]!r}, head is {heads[0]!r} - run `make upgrade`")
    _check("alembic at a single head", problems)


def check_expected_tables(conn: sa.Connection) -> None:
    found = {
        r[0]
        for r in conn.execute(
            sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
    }
    missing = sorted(EXPECTED_TABLES - found)
    _check(
        f"all {len(EXPECTED_TABLES)} warehouse tables present",
        [f"missing table: {t}" for t in missing],
    )


def check_uuid_primary_keys(conn: sa.Connection) -> None:
    rows = conn.execute(
        sa.text(
            """
            SELECT c.relname,
                   count(*)                       AS n_cols,
                   min(format_type(a.atttypid, a.atttypmod)) AS coltype
            FROM pg_constraint k
            JOIN pg_class c ON c.oid = k.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN unnest(k.conkey) AS ck(attnum) ON true
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ck.attnum
            WHERE k.contype = 'p' AND n.nspname = 'public'
              AND c.relname <> ALL(:ignored)
            GROUP BY c.relname
            """
        ),
        {"ignored": list(IGNORED_TABLES)},
    ).fetchall()
    have_pk = {r[0] for r in rows}
    problems = [f"{t}: no PRIMARY KEY" for t in sorted(EXPECTED_TABLES - have_pk)]
    problems += [
        f"{name}: PK is {n} column(s) of type {coltype}, expected a single uuid"
        for name, n, coltype in rows
        if n != 1 or coltype != "uuid"
    ]
    _check("every table has a single uuid primary key", problems)


def check_no_varchar(conn: sa.Connection) -> None:
    rows = conn.execute(
        sa.text(
            f"""SELECT c.table_name, c.column_name FROM information_schema.columns c
                WHERE {_tables_pred()} AND c.data_type = 'character varying'"""
        )
    ).fetchall()
    _check(
        "no varchar - text unconditionally",
        [f"{t}.{col} is character varying" for t, col in rows],
    )


def check_no_naive_timestamps(conn: sa.Connection) -> None:
    rows = conn.execute(
        sa.text(
            f"""SELECT c.table_name, c.column_name FROM information_schema.columns c
                WHERE {_tables_pred()} AND c.data_type = 'timestamp without time zone'"""
        )
    ).fetchall()
    _check(
        "no naive timestamps - timestamptz everywhere",
        [f"{t}.{col} is timestamp without time zone" for t, col in rows],
    )


def check_no_native_enums(conn: sa.Connection) -> None:
    rows = conn.execute(sa.text("SELECT typname FROM pg_type WHERE typtype = 'e'")).fetchall()
    _check(
        "no native enum types - text + CHECK instead",
        [f"enum type {r[0]!r} exists; a value can never be dropped from it" for r in rows],
    )


def check_numeric_discipline(conn: sa.Connection) -> None:
    rows = conn.execute(
        sa.text(
            f"""SELECT c.table_name, c.column_name, c.data_type,
                       c.numeric_precision, c.numeric_scale
                FROM information_schema.columns c
                WHERE {_tables_pred()}
                  AND c.data_type IN ('double precision','real','money','numeric')"""
        )
    ).fetchall()
    problems = []
    for table, col, dtype, precision, scale in rows:
        if dtype in ("double precision", "real", "money"):
            problems.append(f"{table}.{col} is {dtype}; money must never be inexact or `money`")
        elif precision is None or scale is None:
            problems.append(f"{table}.{col} is bare numeric with no precision/scale")
    _check("no float/money types; every numeric has explicit precision", problems)


def check_fk_child_indexes(conn: sa.Connection) -> None:
    """Postgres indexes the REFERENCED side of an FK automatically and the
    REFERENCING side never. Missing child indexes make cascade deletes and
    GDPR SET NULL scans table-scan the child."""
    rows = conn.execute(
        sa.text(
            """
            SELECT c.relname AS child_table,
                   k.conname,
                   (SELECT a.attname FROM pg_attribute a
                     WHERE a.attrelid = c.oid AND a.attnum = k.conkey[1]) AS first_col
            FROM pg_constraint k
            JOIN pg_class c ON c.oid = k.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE k.contype = 'f' AND n.nspname = 'public'
            """
        )
    ).fetchall()
    problems = []
    for child, conname, first_col in rows:
        # An index (or a unique constraint's index) whose LEADING column is the
        # FK's first column serves the lookup. Partial indexes count.
        supported = conn.execute(
            sa.text(
                """
                SELECT 1 FROM pg_index i
                JOIN pg_class ic ON ic.oid = i.indexrelid
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
                WHERE i.indrelid = CAST(:child AS regclass) AND a.attname = :col
                LIMIT 1
                """
            ),
            {"child": child, "col": first_col},
        ).scalar()
        if not supported:
            problems.append(
                f"{child}.{first_col} ({conname}) has no index with it as leading column"
            )
    _check("every foreign key's child column has a supporting index", problems)


def check_identifier_names(conn: sa.Connection) -> None:
    rows = conn.execute(
        sa.text(
            """
            SELECT conname FROM pg_constraint k
            JOIN pg_class c ON c.oid = k.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname <> ALL(:ignored)
            UNION ALL
            SELECT indexname FROM pg_indexes
            WHERE schemaname = 'public' AND tablename <> ALL(:ignored)
            """
        ),
        {"ignored": list(IGNORED_TABLES)},
    ).fetchall()
    problems = []
    for (name,) in rows:
        if not name.startswith(NAME_PREFIXES):
            problems.append(f"{name!r} does not start with one of {NAME_PREFIXES}")
        if len(name) == 63:
            # NAMEDATALEN is 64, so Postgres truncates at 63 - which can quietly
            # collapse two distinct names into one.
            problems.append(f"{name!r} is exactly 63 chars; likely silently truncated")
    _check("constraint/index names follow the house prefixes", problems)


def check_slug_uniqueness(conn: sa.Connection) -> None:
    rows = conn.execute(
        sa.text(
            f"""SELECT c.table_name FROM information_schema.columns c
                WHERE {_tables_pred()} AND c.column_name = 'slug'"""
        )
    ).fetchall()
    problems = []
    for (table,) in rows:
        unique = conn.execute(
            sa.text(
                """
                SELECT 1 FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
                WHERE i.indrelid = CAST(:t AS regclass) AND i.indisunique
                  AND a.attname = 'slug' AND i.indnatts = 1
                LIMIT 1
                """
            ),
            {"t": table},
        ).scalar()
        if not unique:
            problems.append(f"{table}.slug is not UNIQUE; seed upserts would have no arbiter")
    _check("every slug column is UNIQUE", problems)


CHECKS = (
    check_single_head_at_head,
    check_expected_tables,
    check_uuid_primary_keys,
    check_no_varchar,
    check_no_naive_timestamps,
    check_no_native_enums,
    check_numeric_discipline,
    check_fk_child_indexes,
    check_identifier_names,
    check_slug_uniqueness,
)


def run(settings: Settings) -> int:
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    print(f"verifying {settings.database_name} ...")
    try:
        with engine.connect() as conn:
            for check in CHECKS:
                check(conn)
    finally:
        engine.dispose()
    print()
    if _failures:
        print(f"FAILED {len(_failures)}/{_checks_run} checks: {', '.join(_failures)}")
        return 1
    print(f"all {_checks_run} checks passed")
    return 0


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
