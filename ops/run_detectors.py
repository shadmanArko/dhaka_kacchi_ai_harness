"""Runs every ops detector once, opening/converging or auto-resolving
warehouse.cockpit_alert rows accordingly. See ARCHITECTURE.md section 4.3.

These are deliberately NOT the 24 autonomous LLM agents ARCHITECTURE.md
section 3/7 describes - that Layer 2 (orchestrator, per-agent tools,
approval tiers) doesn't exist yet, and `agent_action` (the "NEEDS YOUR
DECISION" side of the cockpit) has nothing real to populate until it does.
These are plain, deterministic threshold checks over data this warehouse
already has - the "*-detector" suffix on every agent name is deliberate,
not a style choice: cockpit_alert.agent has no FK/CHECK (a real agent
roster is a catalogue, not a contract - see migration 0010's own comment),
so nothing stops a real future agent from writing under a plain
"finance"/"operations"/"brand-marketing" name instead once Layer 2 exists.

Run with `make run-detectors`.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

import sqlalchemy as sa

from ops.cockpit import DetectorResult, resolve_alert, upsert_alert
from ops.detectors import no_orders, social_engagement_drop, zero_cogs
from warehouse.config import ConfigError, Settings, load_settings

DETECTORS: list[Callable[[sa.Connection], list[DetectorResult]]] = [
    zero_cogs.check,
    no_orders.check,
    social_engagement_drop.check,
]


def run(settings: Settings) -> tuple[int, int]:
    """Returns (alerts opened/updated, alerts auto-resolved)."""
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    triggered = 0
    resolved = 0
    try:
        with engine.begin() as conn:
            for detector in DETECTORS:
                for result in detector(conn):
                    if result.triggered:
                        upsert_alert(
                            conn,
                            agent=result.agent,
                            alert_key=result.alert_key,
                            severity=result.severity,
                            title=result.title,
                            detail=result.detail,
                        )
                        triggered += 1
                        print(f"  ALERT  [{result.severity}] {result.agent}/{result.alert_key}")
                        print(f"         {result.title}")
                    else:
                        resolved += resolve_alert(
                            conn, agent=result.agent, alert_key=result.alert_key
                        )
    finally:
        engine.dispose()
    return triggered, resolved


def main(argv: list[str] | None = None) -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    triggered, resolved = run(settings)
    print(f"{triggered} alert(s) open/updated, {resolved} auto-resolved")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
