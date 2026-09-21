"""campaign_variant

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (
    TEXT,
    UUID,
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

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "campaign_variant"


def upgrade() -> None:
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        sa.Column("campaign_id", UUID, nullable=False),
        # Globally UNIQUE, not scoped to (campaign_id, slug): matches every
        # other slug column in this schema (menu_item/ingredient/supplier)
        # and keeps warehouse/verify.py's check_slug_uniqueness meaningful
        # without a special case for this one table. Namespace the value if
        # collision risk exists across campaigns (e.g. "oct_promo_reel_03",
        # not bare "reel_03").
        slug_column(),
        sa.Column("creative_ref", TEXT, nullable=True),
        sa.Column("audience", TEXT, nullable=True),
        sa.Column("utm_content", TEXT, nullable=True),
        created_at_column(),
        updated_at_column(),
        sa.UniqueConstraint("slug", name=uq(TABLE, "slug")),
        # RESTRICT, same reasoning as campaign -> channel: a variant with
        # event/attribution history is historical evidence, not deletable.
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaign.id"], name=fk(TABLE, "campaign_id"), ondelete="RESTRICT"
        ),
    )
    create_index_if_absent(TABLE, ["campaign_id"])


def downgrade() -> None:
    drop_table_if_present(TABLE)
