"""menu_item

Revision ID: 0002
Revises: 0001
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

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "menu_item"

# Invented: no category vocabulary exists upstream. The live menu uses only
# 'main' and 'drink'; the rest are the minimum so the first side or bundle does
# not need a migration.
CATEGORIES = "'main','side','drink','dessert','bundle'"


def upgrade() -> None:
    # ONE ROW PER DISH, not per version. A global UNIQUE on slug and versioned
    # active_from/active_to are mutually exclusive, and order_line already
    # snapshots unit_price + unit_cogs_at_time, so versioning menu_item would be
    # a second, redundant mechanism for the same invariant - and redundant
    # mechanisms are the ones that disagree. active_from/active_to here mean
    # "the window this dish was sellable"; current_price is a mirror of the live
    # backend, NOT history.
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        # = the live backend's sku (worker/src/data.ts), e.g. 'kacchi_taster'
        slug_column(),
        # name_de / name_bn are NULLABLE and seeded NULL: no German or Bengali
        # text exists in any repo, and translations are brand-voice decisions
        # owned by brain/brand/voice.md and the seo-content agent.
        sa.Column("name_de", TEXT, nullable=True),
        sa.Column("name_en", TEXT, nullable=False),
        sa.Column("name_bn", TEXT, nullable=True),
        sa.Column("category", TEXT, nullable=False),
        sa.Column("current_price", MONEY, nullable=False),
        sa.Column("active_from", TIMESTAMPTZ, nullable=False, server_default=NOW),  # inclusive
        sa.Column("active_to", TIMESTAMPTZ, nullable=True),  # EXCLUSIVE; NULL = still on the menu
        created_at_column(),
        updated_at_column(),
        # The ON CONFLICT arbiter for the seed migration.
        sa.UniqueConstraint("slug", name=uq(TABLE, "slug")),
        sa.CheckConstraint("slug ~ '^[a-z0-9_]+$'", name=ck(TABLE, "slug_fmt")),
        sa.CheckConstraint(f"category IN ({CATEGORIES})", name=ck(TABLE, "category")),
        sa.CheckConstraint("current_price >= 0", name=ck(TABLE, "price")),
        sa.CheckConstraint(
            "active_to IS NULL OR active_to > active_from", name=ck(TABLE, "window")
        ),
    )


def downgrade() -> None:
    drop_table_if_present(TABLE)
