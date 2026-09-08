"""Prove warehouse.ingest.direct is idempotent and computes correct COGS.

Matches the warehouse.verify / warehouse.gate idiom: a script with plain
ok/FAIL output and an exit code, run against a live database, not a pytest
suite. This repo has zero pytest usage today despite it being a listed
dependency; introducing pytest here first would be new machinery, not
consistency.

Run with `make verify-ingest-direct`.
"""

from __future__ import annotations

import sys
from decimal import Decimal

import sqlalchemy as sa

from warehouse.config import ConfigError, Settings, load_ordering_source_settings, load_settings
from warehouse.ingest.direct import run

_failures: list[str] = []
_checks_run = 0

_COUNTS_SQL = sa.text(
    """
    SELECT
        (SELECT count(*) FROM raw_orders_direct) AS raw,
        (SELECT count(*) FROM orders WHERE channel = 'direct') AS orders,
        (SELECT count(*) FROM order_line ol JOIN orders o ON o.id = ol.order_id
          WHERE o.channel = 'direct') AS lines
    """
)


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


def _counts(conn: sa.Connection):
    return conn.execute(_COUNTS_SQL).one()


def run_checks(settings: Settings) -> int:
    source = load_ordering_source_settings()
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)

    try:
        print("run 1/2 ...")
        run(settings, source)
        with engine.connect() as conn:
            before = _counts(conn)

        print("run 2/2 (nothing changed at the source) ...")
        run(settings, source)
        with engine.connect() as conn:
            after = _counts(conn)

        _check(
            "re-run with nothing new leaves row counts unchanged",
            [
                f"raw {before.raw}->{after.raw}",
                f"orders {before.orders}->{after.orders}",
                f"order_line {before.lines}->{after.lines}",
            ]
            if tuple(before) != tuple(after)
            else [],
        )

        with engine.connect() as conn:
            raw_distinct, direct_orders = conn.execute(
                sa.text(
                    "SELECT (SELECT count(DISTINCT external_id) FROM raw_orders_direct),"
                    "       (SELECT count(*) FROM orders WHERE channel = 'direct')"
                )
            ).one()
        _check(
            "orders(direct) count matches distinct raw_orders_direct external_ids",
            [] if raw_distinct == direct_orders else [f"{direct_orders} vs {raw_distinct}"],
        )

        with engine.connect() as conn:
            bad = conn.execute(
                sa.text(
                    "SELECT o.external_id, ol.unit_cogs_at_time "
                    "FROM order_line ol "
                    "JOIN orders o ON o.id = ol.order_id "
                    "JOIN menu_item m ON m.id = ol.menu_item_id "
                    "WHERE o.channel = 'direct' AND m.slug = 'kacchi_taster' "
                    "  AND ol.unit_cogs_at_time != :expected"
                ),
                {"expected": Decimal("2.5434")},
            ).all()
        _check(
            "kacchi_taster order lines match the hand-computed COGS (2.5434)",
            [f"{r.external_id}: {r.unit_cogs_at_time}" for r in bad],
        )
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
    return run_checks(settings)


if __name__ == "__main__":
    raise SystemExit(main())
