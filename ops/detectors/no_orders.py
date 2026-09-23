"""Detects a full week with zero new orders.

Deliberately a 7-DAY window, not "no orders in the last 24 hours": this
business runs a weekly pre-order cadence (Saturday delivery, "Pre-order
only" per its own Facebook bio), so a naive 24h check would misfire on
almost every non-Saturday day - exactly the noisy-false-positive failure
mode ARCHITECTURE.md section 4.2 warns about ("silence is the default";
more than a handful of queued items means autonomy tiers are
misconfigured, not that everything is actually broken). A genuinely empty
trailing week is a real red flag regardless of cadence.
"""

from __future__ import annotations

import sqlalchemy as sa

from ops.cockpit import DetectorResult

AGENT = "operations-detector"
ALERT_KEY = "no_orders_7d"

_SQL = sa.text("SELECT count(*) FROM orders WHERE placed_at >= now() - interval '7 days'")


def check(conn: sa.Connection) -> list[DetectorResult]:
    count = conn.execute(_SQL).scalar_one()
    if count > 0:
        return [DetectorResult(AGENT, ALERT_KEY, triggered=False)]
    return [
        DetectorResult(
            AGENT,
            ALERT_KEY,
            triggered=True,
            severity="critical",
            title="No new orders in the last 7 days",
            detail=(
                "Zero rows placed in `orders` in the trailing week - check the "
                "ordering site and social channels are actually reachable."
            ),
        )
    ]
