"""Ingest orders from the ordering backend's own Postgres database into the
warehouse. See ARCHITECTURE.md section 4.1 (Ingestion) and Appendix A.

Reads two plain tables (orders, order_items) directly from the ordering
backend's `ordering` database, over a normal Postgres connection, as the
read-only `ordering_reader` role - see warehouse/config.py's
OrderingSourceSettings. Both backends share one Postgres instance on the VPS
(see dhaka_kacchi_ai_harness's VPS infrastructure docs), so this is an
ordinary cross-database read, not a subprocess or a different protocol.

Idempotent: re-running with nothing new at the source leaves every row count
unchanged. Run with `make ingest-direct`; preview with `make ingest-direct-dry-run`.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from warehouse.config import (
    ConfigError,
    OrderingSourceSettings,
    Settings,
    load_ordering_source_settings,
    load_settings,
)
from warehouse.ingest.upsert import upsert_returning

CHANNEL = "direct"

BERLIN_TZ = ZoneInfo("Europe/Berlin")

# PLACEHOLDER: the source stores only a delivery DATE (always a Saturday),
# never a time window - neither BACKEND.md nor worker/src/index.ts define one
# as of this writing. 14:00 local is a stand-in afternoon slot; replace once a
# real delivery window exists in the source system. Same pattern as the
# PRICE WARNING in 0012_seed.py: a named, documented placeholder, never a
# silent magic number.
PROMISED_DELIVERY_TIME_LOCAL = time(14, 0)

# PLACEHOLDER: the source has no packaging-cost concept at all (confirmed - no
# code anywhere computes one). A real number for takeaway box + bottle
# packaging in Berlin, unverified against an actual invoice. Replace once real
# packaging costs are known.
PLACEHOLDER_PACKAGING_COST_EUR = Decimal("0.50")

# Deliberately excludes customer_name/email/phone, notes, and the street/
# house-number address fields - see ARCHITECTURE.md section 10's
# pseudonymisation policy. A future identity-resolution job should read those
# columns directly (with hashing applied at landing time), as its own job -
# not smuggled into this margin-ingestion job. Widened vs. the old D1 query to
# include every column that's safe to keep, per that same table's own
# comment promising raw payloads are landed "verbatim... so the transform can
# be re-derived without re-hitting the source."
_ORDERS_SQL = sa.text(
    """
    SELECT id, created_at, delivery_date, fulfillment_type, status,
           subtotal_cents, delivery_fee_cents, distance_km,
           address_postal_code, address_city, payment_method,
           email_sent, whatsapp_sent
    FROM orders
    ORDER BY created_at, id
    """
)
_ORDER_ITEMS_SQL = sa.text(
    """
    SELECT order_id, sku, name, unit_price_cents, quantity
    FROM order_items
    ORDER BY order_id, id
    """
)


class ExtractionError(RuntimeError):
    """The source read failed in a way the operator must act on. Never caught."""


@dataclass(frozen=True, slots=True)
class RunResult:
    raw_landed: int
    orders_upserted: int
    order_lines_upserted: int


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract(source: OrderingSourceSettings) -> list[dict]:
    """Full extract every run - orders.updated_at exists (see worker/schema.sql)
    but nothing here filters on it yet; at this order volume that's fine,
    correctness comes from the upsert layer below, not from incremental
    extraction. Revisit once volume makes a full refresh too slow.

    Two plain queries, not a join: this connection is read-only
    (`ordering_reader`), and un-flattening a LEFT JOIN server-side is no
    longer needed now that there's no per-invocation subprocess cost to
    amortize by joining.
    """
    engine = sa.create_engine(source.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            order_rows = conn.execute(_ORDERS_SQL).mappings().all()
            item_rows = conn.execute(_ORDER_ITEMS_SQL).mappings().all()
    finally:
        engine.dispose()

    orders: dict[str, dict] = {row["id"]: {**dict(row), "items": []} for row in order_rows}
    for row in item_rows:
        order = orders.get(row["order_id"])
        if order is None:
            continue  # order_items row with no matching orders row - not this job's problem
        order["items"].append(
            {
                "sku": row["sku"],
                "name": row["name"],
                "unit_price_cents": row["unit_price_cents"],
                "quantity": row["quantity"],
            }
        )
    return list(orders.values())


# ---------------------------------------------------------------------------
# Raw landing
# ---------------------------------------------------------------------------

raw_orders_direct = sa.table(
    "raw_orders_direct",
    sa.column("id"),
    sa.column("external_id"),
    sa.column("payload"),
    sa.column("updated_at"),
)


def _json_default(value: object) -> str:
    """Defensive only - doesn't fire today, since orders.created_at/
    delivery_date are TEXT columns in worker/schema.sql, so psycopg always
    hands them back as plain str. Kept as cheap insurance against a future
    schema change to TIMESTAMPTZ/DATE, which would otherwise raise
    `TypeError: Object of type datetime is not JSON serializable` on the
    first real run - round-trips through the same ISO-8601 string shape a
    TEXT column already gives us, so nothing downstream would need to change.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def land_raw(conn: sa.Connection, orders: list[dict]) -> int:
    rows = [
        {
            "external_id": o["id"],
            "payload": json.dumps(o, default=_json_default),
            "updated_at": sa.func.now(),
        }
        for o in orders
    ]
    result = upsert_returning(
        conn,
        raw_orders_direct,
        rows,
        conflict_on=["external_id"],
        update=["payload", "updated_at"],
        returning=["id"],
    )
    return len(result)


# ---------------------------------------------------------------------------
# Transform + load
# ---------------------------------------------------------------------------

orders_t = sa.table(
    "orders",
    sa.column("id"),
    sa.column("channel"),
    sa.column("external_id"),
    sa.column("placed_at"),
    sa.column("promised_at"),
    sa.column("gross"),
    sa.column("discounts"),
    sa.column("channel_fee"),
    sa.column("packaging_cost"),
    sa.column("delivery_cost"),
    sa.column("status"),
    sa.column("updated_at"),
)
order_line_t = sa.table(
    "order_line",
    sa.column("order_id"),
    sa.column("line_no"),
    sa.column("menu_item_id"),
    sa.column("qty"),
    sa.column("unit_price"),
    sa.column("unit_cogs_at_time"),
)

_COGS_SQL = sa.text(
    """
    SELECT mi.id AS menu_item_id,
           coalesce(sum(r.qty / r.yield_factor * i.current_price), 0) AS unit_cogs_at_time
    FROM menu_item mi
    LEFT JOIN recipe r ON r.menu_item_id = mi.id
                      AND r.active_from <= :placed_at
                      AND (r.active_to IS NULL OR r.active_to > :placed_at)
    LEFT JOIN ingredient i ON i.id = r.ingredient_id
    WHERE mi.slug = :sku
    GROUP BY mi.id
    """
)


def _promised_at(delivery_date_str: str) -> datetime:
    d = date.fromisoformat(delivery_date_str)
    local = datetime.combine(d, PROMISED_DELIVERY_TIME_LOCAL, tzinfo=BERLIN_TZ)
    return local.astimezone(UTC)


def _cogs_for_sku(conn: sa.Connection, sku: str, placed_at: datetime, *, external_id: str):
    row = conn.execute(_COGS_SQL, {"sku": sku, "placed_at": placed_at}).first()
    if row is None:
        raise ExtractionError(
            f"sku {sku!r} from order {external_id!r} has no matching menu_item.slug - "
            "add it to the menu_item catalog before re-running."
        )
    if row.unit_cogs_at_time == 0:
        # Visibility, not a hard fail: a menu item can legitimately have no
        # recipe rows yet (the known salad/chutney gap - see CLAUDE.md). Costs
        # nothing under current seeded data, since every seeded item already
        # has recipe rows. Matches the "exception-first monitoring" principle
        # in ARCHITECTURE.md section 2.8.
        print(f"  warn  {sku!r} resolved to zero COGS as of {placed_at} (order {external_id})")
    return row.unit_cogs_at_time


def transform_and_load(conn: sa.Connection) -> tuple[int, int]:
    raw_rows = conn.execute(sa.text("SELECT external_id, payload FROM raw_orders_direct")).all()

    orders_upserted = 0
    lines_upserted = 0
    for external_id, payload in raw_rows:
        placed_at = datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00"))
        gross = Decimal(payload["subtotal_cents"]) / Decimal(100)

        (order_row,) = upsert_returning(
            conn,
            orders_t,
            [
                {
                    "channel": CHANNEL,
                    "external_id": external_id,
                    "placed_at": placed_at,
                    "promised_at": _promised_at(payload["delivery_date"]),
                    "gross": gross,
                    "discounts": Decimal("0.00"),  # real fact: no discount concept in the source
                    "channel_fee": Decimal("0.00"),  # real fact: direct channel has no commission
                    "packaging_cost": PLACEHOLDER_PACKAGING_COST_EUR,
                    "delivery_cost": Decimal("0.00"),  # real fact: source states free delivery
                    "status": payload["status"],
                    "updated_at": sa.func.now(),
                }
            ],
            conflict_on=["channel", "external_id"],
            # Only status/updated_at converge on re-run - see direct.py's module
            # docstring / the plan: a re-run must never silently restate a
            # financial fact, but SHOULD pick up a real status transition once
            # the source ever starts making them.
            update=["status", "updated_at"],
            returning=["id"],
        )
        orders_upserted += 1

        line_rows = []
        for line_no, item in enumerate(payload["items"], start=1):
            cogs = _cogs_for_sku(conn, item["sku"], placed_at, external_id=external_id)
            menu_item_id = conn.execute(
                sa.text("SELECT id FROM menu_item WHERE slug = :slug"), {"slug": item["sku"]}
            ).scalar_one()
            line_rows.append(
                {
                    "order_id": order_row.id,
                    "line_no": line_no,
                    "menu_item_id": menu_item_id,
                    "qty": Decimal(item["quantity"]),
                    "unit_price": Decimal(item["unit_price_cents"]) / Decimal(100),
                    "unit_cogs_at_time": cogs,
                }
            )
        if line_rows:
            inserted = upsert_returning(
                conn,
                order_line_t,
                line_rows,
                conflict_on=["order_id", "line_no"],
                update=None,  # DO NOTHING: an order line is an immutable historical fact
                returning=["order_id"],
            )
            lines_upserted += len(inserted)

    return orders_upserted, lines_upserted


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(settings: Settings, source: OrderingSourceSettings, *, dry_run: bool = False) -> RunResult:
    orders = extract(source)

    if dry_run:
        print(f"{len(orders)} order(s) at the source:")
        for o in orders:
            skus = ", ".join(f"{i['sku']}x{i['quantity']}" for i in o["items"])
            gross = Decimal(o["subtotal_cents"]) / Decimal(100)
            print(f"  {o['id']}  {o['status']:<10}  gross={gross}  {skus}")
        return RunResult(raw_landed=0, orders_upserted=0, order_lines_upserted=0)

    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        # Two phases, two transactions - "land raw first, transform second" per
        # ARCHITECTURE.md section 4.1. Landing must succeed even if a later
        # order's transform hits a bad sku.
        with engine.begin() as conn:
            raw_landed = land_raw(conn, orders)
        with engine.begin() as conn:
            orders_upserted, lines_upserted = transform_and_load(conn)
    finally:
        engine.dispose()

    return RunResult(
        raw_landed=raw_landed,
        orders_upserted=orders_upserted,
        order_lines_upserted=lines_upserted,
    )


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

    try:
        result = run(settings, source_settings, dry_run=args.dry_run)
    except ExtractionError as exc:
        print(f"extraction error: {exc}", file=sys.stderr)
        return 3

    if not args.dry_run:
        print(
            f"landed {result.raw_landed} raw row(s), "
            f"upserted {result.orders_upserted} order(s), "
            f"{result.order_lines_upserted} line(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
