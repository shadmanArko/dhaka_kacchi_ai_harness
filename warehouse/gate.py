"""The ARCHITECTURE.md section 9 Phase-1 exit gate.

  "One SQL query answers 'true margin per order last month by channel'."

Run with `make gate`. Also runs two guards, because the query returning rows is
not the same as the query being trustworthy.
"""

from __future__ import annotations

import sys

import sqlalchemy as sa

from warehouse.config import ConfigError, Settings, load_settings

# COGS comes off order_line.unit_cogs_at_time, frozen at order time. It does NOT
# join `ingredient` - section 5: "Never join to current ingredient price for
# historical margin. When basmati moves 20%, last quarter's P&L must not
# silently change."
#
# No VAT term: Kleinunternehmer (section 19 UStG), so `gross` is full revenue.
# See the note in 0006_orders.py for what changes at the threshold.
GATE_SQL = """
WITH bounds AS (
    SELECT date_trunc('month', now()) - interval '1 month' AS month_start,
           date_trunc('month', now())                      AS month_end
),
order_cogs AS (
    SELECT ol.order_id, sum(ol.qty * ol.unit_cogs_at_time) AS cogs
    FROM order_line ol
    GROUP BY ol.order_id
)
SELECT o.channel,
       count(*)                                                     AS orders,
       round(sum(o.gross), 2)                                       AS gross,
       round(sum(o.discounts), 2)                                   AS discounts,
       round(sum(o.channel_fee + o.packaging_cost + o.delivery_cost), 2) AS direct_costs,
       round(sum(oc.cogs), 2)                                       AS cogs,
       round(sum(o.gross - o.discounts - o.channel_fee
                 - o.packaging_cost - o.delivery_cost - oc.cogs), 2) AS true_margin,
       round(100 * sum(o.gross - o.discounts - o.channel_fee
                       - o.packaging_cost - o.delivery_cost - oc.cogs)
                 / nullif(sum(o.gross - o.discounts), 0), 1)        AS true_margin_pct,
       -- Staleness of the orders.net_margin cache, measured ONLY over rows
       -- that actually carry a cached value. Rows where it was never computed
       -- are reported separately as `uncached` - "not yet computed" and
       -- "computed and now wrong" are different problems and folding them
       -- together would make a fresh warehouse look permanently broken.
       count(*) FILTER (WHERE o.net_margin IS NULL)                  AS uncached,
       round(coalesce(sum(o.net_margin
                 - (o.gross - o.discounts - o.channel_fee
                    - o.packaging_cost - o.delivery_cost - oc.cogs))
             FILTER (WHERE o.net_margin IS NOT NULL), 0), 2)         AS cached_margin_drift
FROM orders o
JOIN order_cogs oc ON oc.order_id = o.id
CROSS JOIN bounds b
WHERE o.status = 'delivered'
  AND o.placed_at >= b.month_start
  AND o.placed_at <  b.month_end
GROUP BY o.channel
ORDER BY true_margin DESC
"""

# Guard 1: NOT NULL prevents nulls, but 0.0000 is the silent failure mode - a
# delivered line with no cost makes the margin look better than it is.
ZERO_COGS_SQL = """
SELECT count(*) FROM order_line ol JOIN orders o ON o.id = ol.order_id
WHERE o.status = 'delivered' AND ol.unit_cogs_at_time = 0
"""


def run(settings: Settings) -> int:
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    failures = 0
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text(GATE_SQL)).mappings().all()
            print("true margin per order, last month, by channel")
            print("-" * 78)
            if not rows:
                print("  (no delivered orders in the previous calendar month)")
            for r in rows:
                print(
                    f"  {r['channel']:<12} orders={r['orders']:>4}  gross={r['gross']:>9}"
                    f"  cogs={r['cogs']:>8}  margin={r['true_margin']:>9}"
                    f"  ({r['true_margin_pct']}%)"
                    f"  uncached={r['uncached']}  drift={r['cached_margin_drift']}"
                )

            print()
            # Guard 2: the query must not depend on current ingredient prices.
            plan = "\n".join(
                r[0] for r in conn.execute(sa.text(f"EXPLAIN (COSTS OFF) {GATE_SQL}")).fetchall()
            )
            if "ingredient" in plan:
                print("  FAIL  plan touches `ingredient` - COGS is not point-in-time")
                failures += 1
            else:
                print("  ok    plan touches only orders + order_line (point-in-time COGS)")

            zero = conn.execute(sa.text(ZERO_COGS_SQL)).scalar()
            if zero:
                print(f"  FAIL  {zero} delivered order_line row(s) have unit_cogs_at_time = 0")
                failures += 1
            else:
                print("  ok    no delivered order line has zero COGS")
    finally:
        engine.dispose()
    print()
    print("exit gate: PASS" if not failures else f"exit gate: FAIL ({failures})")
    return 1 if failures else 0


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    return run(settings)


if __name__ == "__main__":
    raise SystemExit(main())
