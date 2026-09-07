"""agent_action

Revision ID: 0011
Revises: 0010
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

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "agent_action"

# Section 8 defines exactly four tiers.
TIERS = "'read','draft','act','never'"
# Lifecycle, from section 4.4 and the section 9 phase-2 gate:
#   proposed -> queued -> approved -> executed -> reversed  (reversal window)
#                      -> rejected
#                      -> expired    (auto-cancel at expires_at)
#            -> executed             (tier=act: no queue step)
#                      -> failed
STATUSES = "'proposed','queued','approved','rejected','executed','reversed','expired','failed'"


def upgrade() -> None:
    # APPEND-ONLY audit log (section 4.4). Enforce that as a grant, not a
    # convention, once an application role exists:
    #   REVOKE DELETE, TRUNCATE ON agent_action FROM <app_role>;
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        sa.Column("agent", TEXT, nullable=False),
        # Key into brain/policy/autonomy-tiers.yaml. No CHECK: action types live
        # in versioned YAML, and a migration per policy edit is the wrong trade.
        sa.Column("action_type", TEXT, nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("tier", TEXT, nullable=False),
        sa.Column("status", TEXT, nullable=False, server_default=sa.text("'proposed'")),
        # --- the section 5 agent decision schema -------------------------
        sa.Column("decision", TEXT, nullable=False),  # one sentence: what it wants to do
        sa.Column("reason", TEXT, nullable=False),  # why, grounded in data it read
        sa.Column("confidence", RATIO, nullable=False),  # 0.00-1.00
        sa.Column("expected_impact", TEXT, nullable=True),
        sa.Column("risk", TEXT, nullable=True),
        # NOT derivable from tier alone: place_purchase_order is act <= 200 EUR
        # and draft above, so the verdict depends on the payload. Store the
        # policy's evaluation as of proposal time.
        sa.Column("requires_approval", sa.Boolean, nullable=False),
        # --- lifecycle + control plane -----------------------------------
        sa.Column("proposed_at", TIMESTAMPTZ, nullable=False, server_default=NOW),
        # Section 4.4 "unapproved actions auto-cancel at expiry"; fed by
        # expires_h in autonomy-tiers.yaml. Unimplementable without this column.
        sa.Column("expires_at", TIMESTAMPTZ, nullable=True),
        sa.Column("approved_by", TEXT, nullable=True),
        # Section 4.4: rejections require a one-tap reason code, which feeds the
        # agent's eval set.
        sa.Column("rejection_reason_code", TEXT, nullable=True),
        # Section 4.4: "edits are captured as a diff, not just a final value";
        # section 4.5 pulls edit distance from it as a live signal.
        sa.Column("edit_diff", JSONB, nullable=True),
        sa.Column("executed_at", TIMESTAMPTZ, nullable=True),
        # Section 4.2: every write tool accepts an idempotency_key.
        sa.Column("idempotency_key", TEXT, nullable=True),
        # Section 4.2: a write tool returns a reversal_token or declares
        # reversible: false. The control plane reads this to decide which
        # autonomy tier an action is even ELIGIBLE for.
        sa.Column("reversible", sa.Boolean, nullable=True),
        sa.Column("reversal_token", TEXT, nullable=True),
        sa.Column("reversed_at", TIMESTAMPTZ, nullable=True),
        sa.Column("outcome", JSONB, nullable=True),
        created_at_column(),
        # NO updated_at: append-only, and each transition has its own timestamp.
        # PG14 treats NULLs as distinct, which is exactly what is wanted here:
        # many rows may carry no idempotency key.
        sa.UniqueConstraint("idempotency_key", name=uq(TABLE, "idempotency_key")),
        sa.CheckConstraint(f"tier IN ({TIERS})", name=ck(TABLE, "tier")),
        sa.CheckConstraint(f"status IN ({STATUSES})", name=ck(TABLE, "status")),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name=ck(TABLE, "confidence")),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name=ck(TABLE, "payload_obj")),
        # Section 8: a read-tier action "cannot mutate anything", so it may
        # never reach an executing state.
        sa.CheckConstraint(
            "tier <> 'read' OR status IN ('proposed','rejected','expired')",
            name=ck(TABLE, "read_tier"),
        ),
        sa.CheckConstraint(
            "reversed_at IS NULL OR reversal_token IS NOT NULL", name=ck(TABLE, "reversal")
        ),
        sa.CheckConstraint(
            "reversed_at IS NULL OR (executed_at IS NOT NULL AND reversed_at >= executed_at)",
            name=ck(TABLE, "reversed_after"),
        ),
        sa.CheckConstraint(
            "status <> 'executed' OR executed_at IS NOT NULL", name=ck(TABLE, "executed")
        ),
        sa.CheckConstraint(
            "status <> 'approved' OR approved_by IS NOT NULL", name=ck(TABLE, "approved")
        ),
        sa.CheckConstraint(
            "status <> 'rejected' OR rejection_reason_code IS NOT NULL", name=ck(TABLE, "rejected")
        ),
        # Anything needing approval must carry a deadline, or it sits stale
        # forever instead of auto-cancelling.
        sa.CheckConstraint(
            "requires_approval = false OR expires_at IS NOT NULL", name=ck(TABLE, "expiry")
        ),
    )
    # The cockpit's NEEDS YOUR DECISION panel. Partial: the table grows
    # unbounded while the pending set stays under the section 4.2 cap of 5.
    create_index_if_absent(
        TABLE,
        ["proposed_at"],
        where="status IN ('proposed','queued')",
        suffix="pending",
    )
    # The section 4.4 auto-cancel sweeper.
    create_index_if_absent(
        TABLE,
        ["expires_at"],
        where="status IN ('proposed','queued')",
        suffix="pending",
    )


def downgrade() -> None:
    drop_table_if_present(TABLE)
