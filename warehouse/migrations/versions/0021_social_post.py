"""social_post

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (
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
    updated_at_column,
    uq,
)

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "social_post"

# Closed, not open text like channel.platform: this is specifically "a
# platform this warehouse has an ingest job for," same reasoning as
# ad_spend.PLATFORMS. Extend when a second platform's job actually exists.
PLATFORMS = "'instagram','facebook'"
CONTENT_TYPES = "'image','video','carousel','reel','story'"


def upgrade() -> None:
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        sa.Column("platform", TEXT, nullable=False),
        # The platform's own post/media id.
        sa.Column("external_id", TEXT, nullable=False),
        # NULL for organic content with no campaign behind it - most posts,
        # at least until a paid or planned-content workflow exists.
        sa.Column("campaign_variant_id", UUID, nullable=True),
        sa.Column("posted_at", TIMESTAMPTZ, nullable=False),
        sa.Column("permalink", TEXT, nullable=True),
        sa.Column("content_type", TEXT, nullable=True),
        sa.Column("caption", TEXT, nullable=True),
        created_at_column(),
        updated_at_column(),  # caption/permalink can be edited after posting
        sa.UniqueConstraint("platform", "external_id", name=uq(TABLE, "platform_external_id")),
        sa.CheckConstraint(f"platform IN ({PLATFORMS})", name=ck(TABLE, "platform")),
        sa.CheckConstraint(
            f"content_type IS NULL OR content_type IN ({CONTENT_TYPES})",
            name=ck(TABLE, "content_type"),
        ),
        # RESTRICT, same reasoning as every other node in the attribution
        # backbone: a variant with post history attached is evidence, not
        # deletable.
        sa.ForeignKeyConstraint(
            ["campaign_variant_id"],
            ["campaign_variant.id"],
            name=fk(TABLE, "campaign_variant_id"),
            ondelete="RESTRICT",
        ),
    )
    create_index_if_absent(
        TABLE, ["campaign_variant_id"], where="campaign_variant_id IS NOT NULL", suffix="notnull"
    )
    create_index_if_absent(TABLE, ["posted_at"])


def downgrade() -> None:
    drop_table_if_present(TABLE)
