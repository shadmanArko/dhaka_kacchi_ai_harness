"""order_line

Revision ID: 0007
Revises: 0006
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

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "order_line"


def upgrade() -> None:
    # The point-in-time anchor of the whole warehouse.
    create_table_if_absent(
        TABLE,
        # Surrogate PK plus line_no, NOT a composite (order_id, menu_item_id):
        # the same dish can legitimately appear twice on one order at two prices
        # (one discounted, one not), and a composite key makes that
        # unrepresentable.
        pk_column(),
        pk_constraint(TABLE),
        sa.Column("order_id", UUID, nullable=False),
        sa.Column("line_no", sa.Integer, nullable=False),
        sa.Column("menu_item_id", UUID, nullable=False),
        # numeric, not integer: catering and half portions (section 7)
        sa.Column("qty", LINE_QTY, nullable=False),
        # (12,2) not (14,4): a CHARGED price is exact to the cent...
        sa.Column("unit_price", MONEY, nullable=False),
        # ...whereas a DERIVED cost is not. Written AT ORDER TIME and never
        # recomputed. Section 5: never join to ingredient.current_price for
        # historical margin - when basmati moves 20%, last quarter's P&L must
        # not silently change. This column is the only correct COGS source for
        # history.
        sa.Column("unit_cogs_at_time", UNIT_MONEY, nullable=False),
        created_at_column(),
        # NO updated_at, deliberately: an order line is an immutable historical
        # fact. A silent restatement is exactly what section 5 forbids; a
        # correction must be a visible, deliberate rewrite, not a timestamp bump.
        # The ON CONFLICT arbiter for re-ingesting an order's lines. Without it
        # every re-run appends duplicate lines and doubles COGS.
        sa.UniqueConstraint("order_id", "line_no", name=uq(TABLE, "order_line_no")),
        sa.CheckConstraint("line_no > 0", name=ck(TABLE, "line_no")),
        sa.CheckConstraint("qty > 0", name=ck(TABLE, "qty")),
        sa.CheckConstraint("unit_price >= 0", name=ck(TABLE, "price")),
        sa.CheckConstraint("unit_cogs_at_time >= 0", name=ck(TABLE, "cogs")),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=fk(TABLE, "order_id"), ondelete="CASCADE"
        ),
        # RESTRICT, never SET NULL: deleting a dish that has sales history would
        # destroy the exit-gate query's grouping. Deprecate via active_to.
        sa.ForeignKeyConstraint(
            ["menu_item_id"], ["menu_item.id"], name=fk(TABLE, "menu_item_id"), ondelete="RESTRICT"
        ),
    )
    # order_id is already covered as the leading column of uq_order_line_*.
    create_index_if_absent(TABLE, ["menu_item_id"])


def downgrade() -> None:
    drop_table_if_present(TABLE)
