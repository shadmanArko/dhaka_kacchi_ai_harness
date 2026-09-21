"""social_metrics_snapshot

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (
    TIMESTAMPTZ,
    UUID,
    ck,
    create_table_if_absent,
    created_at_column,
    drop_table_if_present,
    fk,
    pk_column,
    pk_constraint,
    uq,
)

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "social_metrics_snapshot"


def upgrade() -> None:
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        sa.Column("social_post_id", UUID, nullable=False),
        sa.Column("captured_at", TIMESTAMPTZ, nullable=False),
        sa.Column("impressions", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("reach", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("likes", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("comments", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("shares", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("saves", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("clicks", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        created_at_column(),
        # NO updated_at, deliberately: a snapshot is an immutable point-in-time
        # fact once captured, same reasoning as order_line. A metric that
        # moved is a NEW row (a later captured_at), never a rewrite of this
        # one - that's the entire point of tracking it as a time series
        # instead of upserting a single current-state row.
        sa.UniqueConstraint("social_post_id", "captured_at", name=uq(TABLE, "post_captured_at")),
        sa.CheckConstraint(
            "impressions >= 0 AND reach >= 0 AND likes >= 0 AND comments >= 0 "
            "AND shares >= 0 AND saves >= 0 AND clicks >= 0",
            name=ck(TABLE, "nonneg"),
        ),
        # CASCADE, unlike the RESTRICT used everywhere else in this section:
        # a snapshot has no independent meaning without its post, so
        # deleting/deduping a social_post row should take its snapshots with
        # it rather than block the delete.
        sa.ForeignKeyConstraint(
            ["social_post_id"],
            ["social_post.id"],
            name=fk(TABLE, "social_post_id"),
            ondelete="CASCADE",
        ),
    )
    # social_post_id is already covered as the leading column of
    # uq_social_metrics_snapshot_post_captured_at.


def downgrade() -> None:
    drop_table_if_present(TABLE)
