"""Prove warehouse.ingest.events is idempotent.

Same ok/FAIL script idiom as warehouse.ingest.direct_verify - see that
module's own docstring for why this isn't a pytest suite.

Run with `make verify-ingest-events`.
"""

from __future__ import annotations

import sys

import sqlalchemy as sa

from warehouse.config import ConfigError, Settings, load_ordering_source_settings, load_settings
from warehouse.ingest.events import run

_failures: list[str] = []
_checks_run = 0

_COUNTS_SQL = sa.text(
    """
    SELECT
        (SELECT count(*) FROM raw_events_direct) AS raw,
        (SELECT count(*) FROM event WHERE external_id IS NOT NULL) AS events
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
            [f"raw {before.raw}->{after.raw}", f"event {before.events}->{after.events}"]
            if tuple(before) != tuple(after)
            else [],
        )

        with engine.connect() as conn:
            raw_distinct, ingested = conn.execute(
                sa.text(
                    "SELECT (SELECT count(DISTINCT external_id) FROM raw_events_direct),"
                    "       (SELECT count(*) FROM event WHERE external_id IS NOT NULL)"
                )
            ).one()
        _check(
            "event(external_id IS NOT NULL) count matches distinct raw_events_direct external_ids",
            [] if raw_distinct == ingested else [f"{ingested} vs {raw_distinct}"],
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
