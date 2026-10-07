"""raw_social_posts_youtube

Revision ID: 0031
Revises: 0030
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

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "raw_social_posts_youtube"


def upgrade() -> None:
    # Same shape as raw_social_posts_threads (0026) - one table per source.
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        # The YouTube video id (social_post.external_id after transform).
        sa.Column("external_id", TEXT, nullable=False),
        # The verbatim `videos.list` item (snippet + contentDetails +
        # statistics), unmodified. Everything the transform derives - the
        # Short/video classification above all - is recomputable from this
        # without another API call, which is the point of landing raw first.
        sa.Column("payload", JSONB, nullable=False),
        created_at_column(),
        updated_at_column(),
        sa.UniqueConstraint("external_id", name=uq(TABLE, "external_id")),
    )


def downgrade() -> None:
    drop_table_if_present(TABLE)
