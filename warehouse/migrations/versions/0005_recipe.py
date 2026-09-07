"""recipe

Revision ID: 0005
Revises: 0004
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

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "recipe"

# Defends historical margin. A partial unique on WHERE active_to IS NULL would
# only defend the PRESENT; ARCHITECTURE.md section 5 requires that last
# quarter's P&L cannot silently change, which is a statement about the PAST.
# Overlaps get created during backfill and correction, where BOTH rows already
# have active_to set - exactly the case a partial unique waves through.
#
# '[)' half-open bounds: closing at T and opening at T is seamless - no gap, no
# overlap. DEFERRABLE so a version swap may insert-then-close inside one txn
# via SET CONSTRAINTS ... DEFERRED.
EXCLUDE_DEF = (
    "EXCLUDE USING gist ("
    "menu_item_id WITH =, "
    "ingredient_id WITH =, "
    "tstzrange(active_from, active_to, '[)') WITH &&"
    ") DEFERRABLE INITIALLY IMMEDIATE"
)


def upgrade() -> None:
    # Required for the EXCLUDE below: GiST needs btree_gist to handle uuid '='.
    create_extension_if_absent("btree_gist")
    # qty          = NET quantity in the finished portion, in ingredient.unit
    # yield_factor = usable fraction after trim/cook loss, (0,1]
    # gross to buy = qty / yield_factor
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        sa.Column("menu_item_id", UUID, nullable=False),
        sa.Column("ingredient_id", UUID, nullable=False),
        sa.Column("qty", QTY, nullable=False),
        sa.Column("yield_factor", YIELD, nullable=False, server_default=sa.text("1.0000")),
        # one bump rewrites all of that dish's lines
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("active_from", TIMESTAMPTZ, nullable=False, server_default=NOW),  # inclusive
        sa.Column("active_to", TIMESTAMPTZ, nullable=True),  # EXCLUSIVE; NULL = current version
        created_at_column(),
        updated_at_column(),  # earns its place: closing active_to IS an update
        # ON CONFLICT arbiter for idempotent recipe seeding.
        sa.UniqueConstraint(
            "menu_item_id", "ingredient_id", "version", name=uq(TABLE, "item_ing_version")
        ),
        sa.CheckConstraint("qty > 0", name=ck(TABLE, "qty")),
        sa.CheckConstraint("yield_factor > 0 AND yield_factor <= 1", name=ck(TABLE, "yield")),
        sa.CheckConstraint("version >= 1", name=ck(TABLE, "version")),
        sa.CheckConstraint(
            "active_to IS NULL OR active_to > active_from", name=ck(TABLE, "window")
        ),
        sa.ForeignKeyConstraint(
            ["menu_item_id"], ["menu_item.id"], name=fk(TABLE, "menu_item_id"), ondelete="CASCADE"
        ),
        # RESTRICT: an ingredient inside any recipe version is historical
        # evidence for a COGS number.
        sa.ForeignKeyConstraint(
            ["ingredient_id"],
            ["ingredient.id"],
            name=fk(TABLE, "ingredient_id"),
            ondelete="RESTRICT",
        ),
    )
    # EXCLUDE cannot be declared inline through op.create_table, so it goes
    # through replace_constraint, which is idempotent by drop-then-add AND
    # converges a same-named constraint carrying a wrong definition.
    replace_constraint(TABLE, excl(TABLE, "no_overlap"), EXCLUDE_DEF)
    # menu_item_id is already covered as the leading column of uq_recipe_*.
    create_index_if_absent(TABLE, ["ingredient_id"])


def downgrade() -> None:
    drop_table_if_present(TABLE)
    # btree_gist is deliberately NOT dropped: other schemas in this database may
    # rely on it, and dropping a shared extension on downgrade is not our call.
