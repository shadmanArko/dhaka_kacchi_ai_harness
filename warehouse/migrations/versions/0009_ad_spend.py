"""ad_spend

Revision ID: 0009
Revises: 0008
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

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "ad_spend"

# Deliberately NOT the shared channel vocabulary. This names an ad NETWORK,
# whose relation to order channel is many-to-many: meta_ads drives both 'direct'
# and 'lieferando' orders. That relation is modelled by attributed_orders /
# attributed_margin, not by a shared token.
PLATFORMS = "'meta_ads','google_ads','lieferando_ads','wolt_ads','uber_eats_ads'"


def upgrade() -> None:
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        sa.Column("date", sa.Date, nullable=False),  # platform reporting is daily-bucketed
        sa.Column("platform", TEXT, nullable=False),
        # NOT NULL with an '__account__' sentinel for account-level fees. On
        # PG14 NULLs are DISTINCT in a UNIQUE index, so a NULL campaign_id would
        # silently permit duplicate rows and break idempotent re-ingestion.
        # (PG15's NULLS NOT DISTINCT is unavailable on the 14.x target.)
        sa.Column("campaign_id", TEXT, nullable=False, server_default=sa.text("'__account__'")),
        sa.Column("spend", MONEY, nullable=False, server_default=sa.text("0")),
        sa.Column("impressions", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("clicks", sa.BigInteger, nullable=False, server_default=sa.text("0")),
        sa.Column("attributed_orders", sa.Integer, nullable=False, server_default=sa.text("0")),
        # Section 7: performance-marketing reallocates budget against DELIVERED
        # MARGIN, not platform ROAS.
        sa.Column("attributed_margin", MONEY, nullable=False, server_default=sa.text("0")),
        created_at_column(),
        # earns its place strongly: attribution windows mature over days
        updated_at_column(),
        # Also serves as the read index for date-range and date+platform
        # queries, being a btree led by `date`. The dedup constraint IS the index.
        sa.UniqueConstraint(
            "date", "platform", "campaign_id", name=uq(TABLE, "date_platform_campaign")
        ),
        sa.CheckConstraint(f"platform IN ({PLATFORMS})", name=ck(TABLE, "platform")),
        sa.CheckConstraint(
            "spend >= 0 AND impressions >= 0 AND clicks >= 0 AND attributed_orders >= 0",
            name=ck(TABLE, "nonneg"),
        ),
        sa.CheckConstraint("clicks <= impressions", name=ck(TABLE, "ctr")),
    )


def downgrade() -> None:
    drop_table_if_present(TABLE)
