"""campaign

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (
    MONEY,
    TEXT,
    TIMESTAMPTZ,
    UUID,
    ck,
    create_index_if_absent,
    create_table_if_absent,
    created_at_column,
    drop_table_if_present,
    fk,
    pk_column,
    pk_constraint,
    slug_column,
    updated_at_column,
    uq,
)

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "campaign"

STATUSES = "'draft','active','paused','completed','cancelled'"


def upgrade() -> None:
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        slug_column(),
        sa.Column("channel_id", UUID, nullable=False),
        sa.Column("name", TEXT, nullable=False),
        sa.Column("objective", TEXT, nullable=True),
        sa.Column("budget_eur", MONEY, nullable=True),
        sa.Column("starts_at", TIMESTAMPTZ, nullable=False),
        # NULL = still running, same "no end yet" convention as
        # menu_item.active_to / recipe.active_to.
        sa.Column("ends_at", TIMESTAMPTZ, nullable=True),
        sa.Column("status", TEXT, nullable=False, server_default=sa.text("'draft'")),
        created_at_column(),
        updated_at_column(),
        sa.UniqueConstraint("slug", name=uq(TABLE, "slug")),
        sa.CheckConstraint(f"status IN ({STATUSES})", name=ck(TABLE, "status")),
        sa.CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name=ck(TABLE, "window")),
        sa.CheckConstraint("budget_eur IS NULL OR budget_eur >= 0", name=ck(TABLE, "budget")),
        # RESTRICT, not SET NULL/CASCADE: a channel with campaign history is
        # historical evidence, same reasoning as order_line -> menu_item.
        # Retire a channel by leaving it unused, not by deleting it.
        sa.ForeignKeyConstraint(
            ["channel_id"], ["channel.id"], name=fk(TABLE, "channel_id"), ondelete="RESTRICT"
        ),
    )
    create_index_if_absent(TABLE, ["channel_id"])


def downgrade() -> None:
    drop_table_if_present(TABLE)
