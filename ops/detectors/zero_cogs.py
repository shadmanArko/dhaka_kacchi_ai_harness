"""Detects delivered orders whose order_line.unit_cogs_at_time resolved to
zero in the last 24h.

This is the exact known gap warehouse/ingest/direct.py's own `_cogs_for_sku`
warning already flags at ingest time (see that module's docstring and
CLAUDE.md's "A real bug this surfaced" note) - surfaced here so it's
visible from the CEO cockpit without reading ingest logs. A zero COGS is
either a genuinely missing recipe (the known salad/chutney gap) or the
recipe.active_from bug class CLAUDE.md documents - never a value to treat
as real margin, and never silenced as noise.

Deliberately NOT an LLM agent - see ops/run_detectors.py's own docstring:
a plain, deterministic threshold check.
"""

from __future__ import annotations

import sqlalchemy as sa

from ops.cockpit import DetectorResult

AGENT = "finance-detector"
ALERT_KEY = "zero_cogs_delivered_orders"

_SQL = sa.text(
    """
    SELECT DISTINCT o.external_id
    FROM order_line ol
    JOIN orders o ON o.id = ol.order_id
    WHERE o.status = 'delivered'
      AND o.delivered_at >= now() - interval '1 day'
      AND ol.unit_cogs_at_time = 0
    ORDER BY o.external_id
    LIMIT 20
    """
)


def check(conn: sa.Connection) -> list[DetectorResult]:
    external_ids = [row.external_id for row in conn.execute(_SQL).all()]
    if not external_ids:
        return [DetectorResult(AGENT, ALERT_KEY, triggered=False)]

    shown = external_ids[:10]
    detail = "orders: " + ", ".join(shown) + (" …" if len(external_ids) > len(shown) else "")
    return [
        DetectorResult(
            AGENT,
            ALERT_KEY,
            triggered=True,
            severity="warn",
            title=f"{len(external_ids)} delivered order(s) with €0 COGS in the last 24h",
            detail=detail,
        )
    ]
