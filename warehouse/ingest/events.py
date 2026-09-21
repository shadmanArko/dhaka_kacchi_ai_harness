"""Ingest marketing/behavioral events from the ordering backend's own
Postgres database into the warehouse. See ARCHITECTURE.md section 4.7 and
dhaka-kacchi-connect's worker/migrations-manual/0002_events.sql.

Reads dhaka-kacchi-connect's `events` table directly, over the same
read-only `ordering_reader` connection warehouse.ingest.direct already uses
for orders - see warehouse/config.py's OrderingSourceSettings. Same "land
raw, then transform" shape as direct.py: raw_events_direct first, event
second.

Idempotent: re-running with nothing new at the source leaves every row count
unchanged. Run with `make ingest-events`; preview with `make ingest-events-dry-run`.

IDENTITY GAP, DELIBERATE: event.customer_id/order_id are left NULL by this
job. The source's customer_id/order_id are dhaka-kacchi-connect's own ids
("cust_<uuid>"/"ord_<uuid>"), not the warehouse's UUID primary keys, and
resolving one to the other is identity resolution - "the hardest problem in
the build" per ARCHITECTURE.md section 5, explicitly out of scope here, the
same reason warehouse.ingest.direct never populates orders.customer_id
either. The source ids are preserved verbatim under
properties.source_customer_id / properties.source_order_id instead of being
silently dropped, so a future identity-resolution job has something to join
against.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime

import sqlalchemy as sa

from warehouse.config import (
    ConfigError,
    OrderingSourceSettings,
    Settings,
    load_ordering_source_settings,
    load_settings,
)
from warehouse.ingest.upsert import upsert_returning

_EVENTS_SQL = sa.text(
    """
    SELECT id, event_name, occurred_at, source, anonymous_id, session_id,
           customer_id, order_id, properties
    FROM events
    ORDER BY occurred_at, id
    """
)


@dataclass(frozen=True, slots=True)
class RunResult:
    raw_landed: int
    events_upserted: int


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract(source: OrderingSourceSettings) -> list[dict]:
    """Full extract every run, same reasoning as direct.py's extract(): no
    incremental cursor yet, correctness comes from the upsert layer below,
    not from filtering what's read. Fine at current event volume."""
    engine = sa.create_engine(source.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            rows = conn.execute(_EVENTS_SQL).mappings().all()
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Raw landing
# ---------------------------------------------------------------------------

raw_events_direct = sa.table(
    "raw_events_direct",
    sa.column("id"),
    sa.column("external_id"),
    sa.column("payload"),
    sa.column("updated_at"),
)


def _json_default(value: object) -> str:
    """occurred_at is a real TIMESTAMPTZ at the source (unlike orders.
    created_at, which is TEXT - see direct.py's own _json_default), so
    psycopg hands it back as a datetime, not a string. Needed for real, not
    defensive insurance."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def land_raw(conn: sa.Connection, events: list[dict]) -> int:
    rows = [
        {
            "external_id": e["id"],
            "payload": json.dumps(e, default=_json_default),
            "updated_at": sa.func.now(),
        }
        for e in events
    ]
    result = upsert_returning(
        conn,
        raw_events_direct,
        rows,
        conflict_on=["external_id"],
        update=["payload", "updated_at"],
        returning=["id"],
    )
    return len(result)


# ---------------------------------------------------------------------------
# Transform + load
# ---------------------------------------------------------------------------

event_t = sa.table(
    "event",
    sa.column("id"),
    sa.column("event_name"),
    sa.column("occurred_at"),
    sa.column("source"),
    sa.column("anonymous_id"),
    sa.column("session_id"),
    sa.column("customer_id"),
    sa.column("order_id"),
    sa.column("external_id"),
    sa.column("properties"),
)


def transform_and_load(conn: sa.Connection) -> int:
    raw_rows = conn.execute(sa.text("SELECT external_id, payload FROM raw_events_direct")).all()

    events_upserted = 0
    rows = []
    for external_id, payload in raw_rows:
        properties = dict(payload.get("properties") or {})
        # Preserve the source's own customer/order ids rather than silently
        # dropping them - see the module docstring's IDENTITY GAP note.
        if payload.get("customer_id"):
            properties["source_customer_id"] = payload["customer_id"]
        if payload.get("order_id"):
            properties["source_order_id"] = payload["order_id"]

        rows.append(
            {
                "event_name": payload["event_name"],
                "occurred_at": datetime.fromisoformat(payload["occurred_at"]),
                "source": payload["source"],
                "anonymous_id": payload.get("anonymous_id"),
                "session_id": payload.get("session_id"),
                "customer_id": None,
                "order_id": None,
                "external_id": external_id,
                "properties": json.dumps(properties),
            }
        )

    if rows:
        # DO NOTHING, not converge-on-reingest: an event is an immutable
        # historical fact once recorded, same reasoning as order_line -
        # nothing about a past event should change on a later run. An
        # event_name this warehouse's event_taxonomy doesn't know about
        # raises here (FK violation, RESTRICT) rather than being silently
        # dropped - see the module docstring's note on the two repos'
        # taxonomies being a manual, unenforced contract.
        inserted = upsert_returning(
            conn,
            event_t,
            rows,
            conflict_on=["source", "external_id"],
            update=None,
            returning=["id"],
        )
        events_upserted = len(inserted)

    return events_upserted


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(settings: Settings, source: OrderingSourceSettings, *, dry_run: bool = False) -> RunResult:
    events = extract(source)

    if dry_run:
        print(f"{len(events)} event(s) at the source:")
        for e in events:
            print(f"  {e['id']}  {e['event_name']:<20}  {e['occurred_at']}  source={e['source']}")
        return RunResult(raw_landed=0, events_upserted=0)

    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        # Two phases, two transactions - "land raw first, transform second"
        # per ARCHITECTURE.md section 4.1, same as direct.py.
        with engine.begin() as conn:
            raw_landed = land_raw(conn, events)
        with engine.begin() as conn:
            events_upserted = transform_and_load(conn)
    finally:
        engine.dispose()

    return RunResult(raw_landed=raw_landed, events_upserted=events_upserted)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="extract and print what would be ingested; write nothing",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        source_settings = load_ordering_source_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    result = run(settings, source_settings, dry_run=args.dry_run)

    if not args.dry_run:
        print(f"landed {result.raw_landed} raw row(s), upserted {result.events_upserted} event(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
