"""ingredient

Revision ID: 0004
Revises: 0003
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

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "ingredient"

# Purchase units, not base units. Deliberate: basmati at 2.8000 EUR/kg keeps four
# significant figures, whereas 0.0028 EUR/g keeps two.
UNITS = "'kg','l','piece','pack'"


def upgrade() -> None:
    # UNIT DISCIPLINE - the single biggest COGS bug risk.
    # `unit` is the STOCK-KEEPING unit. current_price is EUR *per that unit*, and
    # recipe.qty is expressed *in that same unit*. There is exactly one unit per
    # ingredient and NO conversion anywhere in the cost model.
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        slug_column(),  # e.g. 'basmati_rice'
        sa.Column("name", TEXT, nullable=False),
        # nullable: suppliers churn, ingredients do not
        sa.Column("supplier_id", UUID, nullable=True),
        sa.Column("unit", TEXT, nullable=False),
        sa.Column("current_price", UNIT_MONEY, nullable=False),  # EUR per `unit`
        # EU FIC 1169/2011 Annex II, German gastronomy letter scheme.
        sa.Column(
            "allergen_codes", TEXT_ARRAY, nullable=False, server_default=sa.text("'{}'::text[]")
        ),
        # German ZZulV numeric codes ('1'..'9')
        sa.Column(
            "additive_codes", TEXT_ARRAY, nullable=False, server_default=sa.text("'{}'::text[]")
        ),
        # [{price, valid_from, source}]. A supplier-negotiation / trend artefact,
        # NOT a margin input: historical COGS is protected by
        # order_line.unit_cogs_at_time, never by replaying this.
        sa.Column("price_history", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        created_at_column(),
        updated_at_column(),  # earns its place: current_price moves weekly
        sa.UniqueConstraint("slug", name=uq(TABLE, "slug")),
        sa.CheckConstraint("slug ~ '^[a-z0-9_]+$'", name=ck(TABLE, "slug_fmt")),
        sa.CheckConstraint(f"unit IN ({UNITS})", name=ck(TABLE, "unit")),
        sa.CheckConstraint("current_price >= 0", name=ck(TABLE, "price")),
        sa.CheckConstraint("jsonb_typeof(price_history) = 'array'", name=ck(TABLE, "hist_shape")),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["supplier.id"], name=fk(TABLE, "supplier_id"), ondelete="SET NULL"
        ),
    )
    # Postgres does NOT index the child side of a foreign key.
    create_index_if_absent(TABLE, ["supplier_id"])


def downgrade() -> None:
    drop_table_if_present(TABLE)
