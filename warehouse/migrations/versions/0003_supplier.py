"""supplier

Revision ID: 0003
Revises: 0002
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

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "supplier"


def upgrade() -> None:
    # Not in the original task list, but ingredient.supplier_id has no FK target
    # without it, and ARCHITECTURE.md section 5 lists supplier as a core entity.
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        slug_column(),  # e.g. 'halal_butcher_berlin'
        sa.Column("name", TEXT, nullable=False),  # legal/trading name as invoiced
        sa.Column("lead_time_days", sa.Integer, nullable=True),  # NULL = unknown, not zero
        sa.Column("min_order_eur", MONEY, nullable=True),
        # 0.00-1.00, same scale as agent confidence. Agent-earned, never seeded.
        sa.Column("reliability_score", RATIO, nullable=True),
        created_at_column(),
        updated_at_column(),
        sa.UniqueConstraint("slug", name=uq(TABLE, "slug")),
        sa.CheckConstraint("slug ~ '^[a-z0-9_]+$'", name=ck(TABLE, "slug_fmt")),
        sa.CheckConstraint(
            "lead_time_days IS NULL OR lead_time_days >= 0", name=ck(TABLE, "lead_time")
        ),
        sa.CheckConstraint(
            "min_order_eur IS NULL OR min_order_eur >= 0", name=ck(TABLE, "min_order")
        ),
        sa.CheckConstraint(
            "reliability_score IS NULL OR (reliability_score >= 0 AND reliability_score <= 1)",
            name=ck(TABLE, "reliability"),
        ),
    )


def downgrade() -> None:
    drop_table_if_present(TABLE)
