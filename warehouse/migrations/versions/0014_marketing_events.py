"""marketing_events

Revision ID: 0014
Revises: 0013

First slice of the ARCHITECTURE.md section 4.7 marketing-attribution layer:
event_taxonomy (the closed, versioned event vocabulary) and event (the
behavioral event stream itself). channel/campaign/campaign_variant do not
exist yet, so event.channel_id/campaign_id/campaign_variant_id land here as
plain nullable uuid columns with no FK - a later migration adds those three
FKs via replace_constraint() once the referenced tables exist, per this
repo's own rule that a bare follow-up ALTER has no IF NOT EXISTS guard.

event_taxonomy uses a surrogate uuid id plus a UNIQUE event_name, not
event_name as the PK directly: warehouse/verify.py's check_uuid_primary_keys
enforces a single uuid primary key on every table, globally, the same shape
already used by menu_item.slug / ingredient.slug.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (
    JSONB,
    TEXT,
    TIMESTAMPTZ,
    UUID,
    ck,
    create_index_if_absent,
    create_table_if_absent,
    drop_table_if_present,
    fk,
    pk_column,
    pk_constraint,
    upsert,
    uq,
)

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TAXONOMY_TABLE = "event_taxonomy"
EVENT_TABLE = "event"

CATEGORIES = "'traffic','commerce','engagement','lifecycle'"

# Fixed ids so downgrade() can delete exactly what this migration inserted,
# same reasoning as 0012_seed.py's SUPPLIER_IDS/INGREDIENT_IDS.
EVENT_TAXONOMY_IDS = {
    slug: uuid.UUID(f"d0000000-0000-4000-8000-{i:012d}")
    for i, slug in enumerate(
        [
            "page_view",
            "menu_view",
            "product_view",
            "add_to_cart",
            "begin_checkout",
            "purchase",
            "coupon_used",
            "social_click",
            "contact",
            "newsletter_signup",
        ],
        start=1,
    )
}

# slug, category, description
EVENT_TAXONOMY_ROWS = [
    ("page_view", "traffic", "Any page loaded on the website"),
    ("menu_view", "traffic", "Menu page or menu section viewed"),
    ("product_view", "traffic", "A specific menu item viewed"),
    ("add_to_cart", "commerce", "A menu item added to the cart"),
    ("begin_checkout", "commerce", "Checkout flow started"),
    ("purchase", "commerce", "Order completed"),
    ("coupon_used", "commerce", "A promotion code applied to an order"),
    ("social_click", "engagement", "A tracked link click originating from a social post"),
    ("contact", "engagement", "A contact/lead form or channel initiated"),
    ("newsletter_signup", "lifecycle", "Marketing consent captured"),
]

event_taxonomy = sa.table(
    "event_taxonomy",
    sa.column("id", UUID),
    sa.column("event_name", TEXT),
    sa.column("category", TEXT),
    sa.column("description", TEXT),
    sa.column("added_at", TIMESTAMPTZ),
)


def upgrade() -> None:
    create_table_if_absent(
        TAXONOMY_TABLE,
        pk_column(),
        pk_constraint(TAXONOMY_TABLE),
        sa.Column("event_name", TEXT, nullable=False),
        sa.Column("category", TEXT, nullable=False),
        sa.Column("description", TEXT, nullable=True),
        # required_properties documents which event.properties keys a producer
        # is expected to send for this event_name - advisory, not enforced by a
        # constraint (properties is jsonb; Postgres can't CHECK its own shape
        # against another row's data).
        sa.Column("required_properties", JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("added_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        # A taxonomy entry is deprecated, never deleted - matches the
        # "corrections must be deliberate" philosophy already in the
        # recipe/order_line design (section 5's point-in-time-costs property).
        sa.Column("deprecated_at", TIMESTAMPTZ, nullable=True),
        sa.UniqueConstraint("event_name", name=uq(TAXONOMY_TABLE, "event_name")),
        sa.CheckConstraint(f"category IN ({CATEGORIES})", name=ck(TAXONOMY_TABLE, "category")),
    )

    create_table_if_absent(
        EVENT_TABLE,
        pk_column(),
        pk_constraint(EVENT_TABLE),
        sa.Column("event_name", TEXT, nullable=False),
        sa.Column("occurred_at", TIMESTAMPTZ, nullable=False),
        # Where the event was captured: 'website', 'manual', and whatever else
        # gets added later. Capture itself happens outside this repo (the
        # dhaka-kacchi-connect ordering backend for 'website'); this warehouse
        # only reads it, the same read-only way orders/order_items already are.
        sa.Column("source", TEXT, nullable=False),
        # Distinct from both session_id (one visit) and customer_id (resolved
        # identity): a persistent per-browser id that exists before either of
        # those does.
        sa.Column("anonymous_id", TEXT, nullable=True),
        sa.Column("session_id", TEXT, nullable=True),
        # customer_id is NULL-able: most events happen before identity
        # resolution has a customer to attach to.
        sa.Column("customer_id", UUID, nullable=True),
        sa.Column("order_id", UUID, nullable=True),
        # No FK yet: channel/campaign/campaign_variant don't exist. A later
        # migration adds these three FKs via replace_constraint() once those
        # tables land - see the module docstring.
        sa.Column("channel_id", UUID, nullable=True),
        sa.Column("campaign_id", UUID, nullable=True),
        sa.Column("campaign_variant_id", UUID, nullable=True),
        sa.Column("properties", JSONB, nullable=False, server_default=sa.text("'{}'")),
        sa.ForeignKeyConstraint(
            ["event_name"],
            ["event_taxonomy.event_name"],
            name=fk(EVENT_TABLE, "event_name"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customer.id"],
            name=fk(EVENT_TABLE, "customer_id"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=fk(EVENT_TABLE, "order_id"), ondelete="SET NULL"
        ),
    )

    # Every FK's referencing column needs its own supporting index - Postgres
    # indexes the referenced side automatically and the referencing side never
    # (warehouse/verify.py's check_fk_child_indexes). customer_id/order_id are
    # mostly NULL, so partial indexes keep them small.
    create_index_if_absent(EVENT_TABLE, ["event_name"])
    create_index_if_absent(
        EVENT_TABLE, ["customer_id"], where="customer_id IS NOT NULL", suffix="notnull"
    )
    create_index_if_absent(
        EVENT_TABLE, ["order_id"], where="order_id IS NOT NULL", suffix="notnull"
    )
    # Not FK-driven, but the real query shape: time-range scans.
    create_index_if_absent(EVENT_TABLE, ["occurred_at"])

    now = sa.func.now()
    upsert(
        event_taxonomy,
        [
            {
                "id": EVENT_TAXONOMY_IDS[slug],
                "event_name": slug,
                "category": category,
                "description": description,
                "added_at": now,
            }
            for slug, category, description in EVENT_TAXONOMY_ROWS
        ],
        conflict_on=["event_name"],
        # deprecated_at is deliberately excluded: it's operator-owned, and a
        # redeploy of this seed must not un-deprecate an entry someone retired.
        update=["category", "description"],
    )


def downgrade() -> None:
    # Child before parent; dropping event_taxonomy also removes the seed rows,
    # so no separate delete_by() is needed (both are created in this same
    # migration, unlike 0012_seed.py which seeds into pre-existing tables).
    drop_table_if_present(EVENT_TABLE)
    drop_table_if_present(TAXONOMY_TABLE)
