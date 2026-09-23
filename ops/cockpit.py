"""Shared plumbing for ops/detectors/*.py - the ARCHITECTURE.md section 4.3
`cockpit_alert` table.

The ON CONFLICT target here is a PARTIAL unique index (agent, alert_key
WHERE resolved_at IS NULL - see migration 0010_cockpit_alert.py's own
comment on why: "an agent may hold at most ONE OPEN alert per condition"),
not a plain UNIQUE constraint - warehouse/ingest/upsert.py's generic
upsert_returning() has no index_where support and can't express this, hence
this small, table-specific helper instead of reusing it.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

cockpit_alert_t = sa.table(
    "cockpit_alert",
    sa.column("agent"),
    sa.column("alert_key"),
    sa.column("severity"),
    sa.column("title"),
    sa.column("detail"),
    sa.column("resolved_at"),
    sa.column("resolution"),
)


@dataclass(frozen=True, slots=True)
class DetectorResult:
    """One condition a detector checked. `triggered=False` still carries
    `agent`/`alert_key` so the orchestrator knows what to resolve if an
    alert for this exact condition is currently open."""

    agent: str
    alert_key: str
    triggered: bool
    severity: str = "warn"
    title: str = ""
    detail: str | None = None


def upsert_alert(
    conn: sa.Connection,
    *,
    agent: str,
    alert_key: str,
    severity: str,
    title: str,
    detail: str | None,
) -> None:
    """Opens a new alert, or converges an already-open one's severity/title/
    detail to the latest detection - never touches `detected_at` on an
    existing open alert, so the cockpit can show how long a problem has
    actually persisted, not just when it was last re-checked."""
    stmt = pg_insert(cockpit_alert_t).values(
        agent=agent, alert_key=alert_key, severity=severity, title=title, detail=detail
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["agent", "alert_key"],
        index_where=sa.text("resolved_at IS NULL"),
        set_={
            "severity": stmt.excluded.severity,
            "title": stmt.excluded.title,
            "detail": stmt.excluded.detail,
        },
    )
    conn.execute(stmt)


def resolve_alert(conn: sa.Connection, *, agent: str, alert_key: str) -> int:
    """Closes an open alert for this condition, if one exists - a no-op
    (rowcount 0) when there wasn't one, which is the common case on every
    healthy run. `resolution='auto'` marks this apart from a human closing
    the card from the cockpit UI (see ARCHITECTURE.md section 4.2:
    "Resolution is automatic when the underlying metric normalises, or
    manual when you close it.")."""
    result = conn.execute(
        sa.text(
            "UPDATE cockpit_alert SET resolved_at = now(), resolution = 'auto' "
            "WHERE agent = :agent AND alert_key = :alert_key AND resolved_at IS NULL"
        ),
        {"agent": agent, "alert_key": alert_key},
    )
    return result.rowcount
