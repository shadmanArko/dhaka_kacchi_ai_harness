"""raw_social_posts_threads

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (
    JSONB,
    TEXT,
    create_table_if_absent,
    created_at_column,
    drop_table_if_present,
    pk_column,
    pk_constraint,
    updated_at_column,
    uq,
)

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "raw_social_posts_threads"


def upgrade() -> None:
    # Same shape as raw_social_posts_instagram (0023) - one table per
    # source. Threads is a genuinely different API (graph.threads.net, its
    # own token/permission flow) from Instagram/Facebook's shared
    # graph.facebook.com, even though it lands into the same social_post
    # table downstream.
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        # The Threads post id (social_post.external_id after transform).
        sa.Column("external_id", TEXT, nullable=False),
        # Verbatim post object plus its insights, nested as
        # payload['insights']: {metric_name: value, ...}.
        sa.Column("payload", JSONB, nullable=False),
        created_at_column(),
        updated_at_column(),
        sa.UniqueConstraint("external_id", name=uq(TABLE, "external_id")),
    )


def downgrade() -> None:
    drop_table_if_present(TABLE)
