"""cockpit_alert

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (  # noqa: F401
    JSONB,
    LINE_QTY,
    MONEY,
    NOW,
    QTY,
    RATING,
    RATIO,
    TEXT,
    TEXT_ARRAY,
    TIMESTAMPTZ,
    UNIT_MONEY,
    UUID,
    YIELD,
    ck,
    create_extension_if_absent,
    create_index_if_absent,
    create_table_if_absent,
    created_at_column,
    drop_index_if_present,
    drop_table_if_present,
    excl,
    fk,
    pk_column,
    pk_constraint,
    replace_constraint,
    slug_column,
    updated_at_column,
    uq,
)

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "cockpit_alert"


def upgrade() -> None:
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        # No FK and no CHECK: section 7's 24-agent roster is a catalogue, not a
        # contract. A typo'd agent name surfaces immediately in the cockpit's
        # "AGENTS WORKING" panel - cheaper than a migration per roster change.
        sa.Column("agent", TEXT, nullable=False),
        # Stable condition id, e.g. 'cold_food_mention_spike'. Section 4.2's
        # "silence is the default" and the 5-item cap are unenforceable without
        # it: title is free text ("up 300%" vs "up 320%") and cannot dedup.
        sa.Column("alert_key", TEXT, nullable=False),
        sa.Column("severity", TEXT, nullable=False),
        sa.Column("title", TEXT, nullable=False),
        sa.Column("detail", TEXT, nullable=True),
        sa.Column("detected_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
        sa.Column("acknowledged_at", TIMESTAMPTZ, nullable=True),
        sa.Column("resolved_at", TIMESTAMPTZ, nullable=True),
        # 'auto' when the underlying metric normalises, else the human's note.
        sa.Column("resolution", TEXT, nullable=True),
        created_at_column(),
        # NO updated_at: every state change already has its own named timestamp
        # (detected / acknowledged / resolved). A generic column would be a
        # second, weaker source of truth for a fact already recorded.
        sa.CheckConstraint("severity IN ('info','warn','critical')", name=ck(TABLE, "severity")),
        sa.CheckConstraint(
            "acknowledged_at IS NULL OR acknowledged_at >= detected_at", name=ck(TABLE, "ack")
        ),
        sa.CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= detected_at", name=ck(TABLE, "res")
        ),
        sa.CheckConstraint(
            "resolved_at IS NOT NULL OR resolution IS NULL", name=ck(TABLE, "res_txt")
        ),
    )
    # An agent may hold at most ONE OPEN alert per condition, so re-detection is
    # a no-op upsert rather than a 30th duplicate card. Must be a partial index:
    # Postgres cannot express partial uniqueness as a table constraint.
    create_index_if_absent(
        TABLE, ["agent", "alert_key"], unique=True, where="resolved_at IS NULL", suffix="open"
    )
    # The cockpit's PROBLEMS DETECTED panel. Earns its place at any volume: the
    # table grows unbounded while the OPEN set stays ~5 rows - the ideal ratio
    # for a partial index.
    create_index_if_absent(
        TABLE,
        ["severity", "detected_at"],
        where="resolved_at IS NULL",
        suffix="open",
        postgresql_ops={"detected_at": "DESC"},
    )


def downgrade() -> None:
    drop_table_if_present(TABLE)
