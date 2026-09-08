"""Ingest orders from the direct ordering backend (Cloudflare D1) into the
warehouse. See ARCHITECTURE.md section 4.1 (Ingestion) and Appendix A.

Reads D1 via `npx wrangler d1 execute ... --json`, run from the ordering
backend's worker/ directory. --local vs --remote is the ENTIRE production
switch (DHAKA_KACCHI_D1_TARGET) - nothing else in this pipeline changes when
the ordering backend eventually gets deployed for real.

Idempotent: re-running with nothing new at the source leaves every row count
unchanged. Run with `make ingest-direct`; preview with `make ingest-direct-dry-run`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from warehouse.config import (
    ConfigError,
    DirectSourceSettings,
    Settings,
    load_direct_source_settings,
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

WRANGLER_D1_QUERY = (
    "SELECT o.id, o.created_at, o.delivery_date, o.status, "
    "o.subtotal_cents, "
    "oi.sku, oi.unit_price_cents, oi.quantity "
    "FROM orders o LEFT JOIN order_items oi ON oi.order_id = o.id "
    "ORDER BY o.created_at, o.id, oi.id"
)


class ExtractionError(RuntimeError):
    """The D1 read failed in a way the operator must act on. Never caught."""


@dataclass(frozen=True, slots=True)
class RunResult:
    raw_landed: int
    orders_upserted: int
    order_lines_upserted: int


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _run_wrangler(cfg: DirectSourceSettings) -> list[dict]:
    cmd = [
        "npx",
        "--yes",
        "wrangler",
        "d1",
        "execute",
        cfg.d1_database_name,
        f"--{cfg.d1_target}",
        "--json",
        "--command",
        WRANGLER_D1_QUERY,
    ]
    try:
        proc = subprocess.run(cmd, cwd=cfg.worker_dir, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise ExtractionError(
            "`npx` was not found on PATH. Install Node.js (bundles npm/npx), "
            "confirm `npx --version` works, then retry."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(
            f"wrangler d1 execute did not finish within {exc.timeout}s. "
            "Check that the ordering-backend's local D1 state isn't wedged."
        ) from exc
    return _parse_wrangler_json(proc)


def _parse_wrangler_json(proc: subprocess.CompletedProcess[str]) -> list[dict]:
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ExtractionError(
            "wrangler did not return valid JSON on stdout.\n"
            f"  exit={proc.returncode} stderr={proc.stderr[:500]!r}\n"
            f"  stdout[:500]={proc.stdout[:500]!r}"
        ) from exc

    if isinstance(parsed, dict) and "error" in parsed:
        text = str(parsed["error"].get("text", parsed["error"]))
        if "no such table" in text.lower():
            raise ExtractionError(
                f"D1 has no orders/order_items table ({text}).\n"
                "  Local D1 state was never initialised. Run, inside worker/:\n"
                "  npm run db:migrate:local   (or db:migrate:remote for --remote)"
            )
        if "couldn't find a d1 db" in text.lower():
            raise ExtractionError(
                f"wrangler could not resolve the D1 binding ({text}).\n"
                "  Check DHAKA_KACCHI_CONNECT_PATH / worker/wrangler.toml."
            )
        raise ExtractionError(f"D1 query failed: {text}")

    if not isinstance(parsed, list) or not parsed or "results" not in parsed[0]:
        raise ExtractionError(f"unexpected wrangler --json shape: {str(parsed)[:500]!r}")
    if not parsed[0].get("success", False):
        raise ExtractionError(f"D1 query reported success=false: {parsed[0]}")
    return parsed[0]["results"]


def _group_into_orders(rows: list[dict]) -> list[dict]:
    """Flat orders-LEFT-JOIN-order_items rows -> nested per-order dicts.

    line_no comes from POSITION in this already-`ORDER BY oi.id`-sorted list,
    not from D1's own order_items.id (a source-internal autoincrement, not
    something to expose as identity in the warehouse).
    """
    orders: dict[str, dict] = {}
    for row in rows:
        order = orders.setdefault(
            row["id"],
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "delivery_date": row["delivery_date"],
                "status": row["status"],
                "subtotal_cents": row["subtotal_cents"],
                "items": [],
            },
        )
        if row["sku"] is not None:  # LEFT JOIN guard: an item-less order stays visible
            order["items"].append(
                {
                    "sku": row["sku"],
                    "unit_price_cents": row["unit_price_cents"],
                    "quantity": row["quantity"],
                }
            )
    return list(orders.values())


def extract(cfg: DirectSourceSettings) -> list[dict]:
    """Full extract every run - the source has no `updated_at`/cursor to filter
    on (confirmed: no code path ever mutates a D1 order after insert). At this
    order volume that's fine; correctness comes from the upsert layer below,
    not from incremental extraction.
    """
    return _group_into_orders(_run_wrangler(cfg))


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


def land_raw(conn: sa.Connection, orders: list[dict]) -> int:
    rows = [
        {"external_id": o["id"], "payload": json.dumps(o), "updated_at": sa.func.now()}
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


def run(settings: Settings, source: DirectSourceSettings, *, dry_run: bool = False) -> RunResult:
    orders = extract(source)

    if dry_run:
        print(f"resolved worker_dir={source.worker_dir} d1_target={source.d1_target}")
        if source.d1_target == "local":
            print(
                "  note: --local will ingest EVERYTHING currently in local dev D1, "
                "including any manual/Playwright test orders."
            )
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
        source_settings = load_direct_source_settings()
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
