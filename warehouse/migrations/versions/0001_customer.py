"""customer

Revision ID: 0001
Revises:
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

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "customer"

# Shared customer-facing channel vocabulary. orders.channel, review.source and
# customer.channel_first_seen each CHECK a SUBSET of one spelling set, so review
# -> order -> customer join on channel with no mapping table. Not one shared
# constraint: 'google' is a review source but never an order channel.
CHANNELS = "'direct','lieferando','wolt','uber_eats','instagram','whatsapp','catering'"


def upgrade() -> None:
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        # jsonb, NOT the text[] sketched in ARCHITECTURE.md section 5: an array
        # loses WHICH hash is which, and identity resolution must match
        # email-to-email and phone-to-phone, never across types.
        # {"email_sha256": ..., "phone_sha256": ..., "addr_fuzzy": ...}
        sa.Column("contact_hashes", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        # cached min(orders.placed_at); NULL until the first order lands
        sa.Column("first_order_at", TIMESTAMPTZ, nullable=True),
        # acquisition channel, for CAC by channel (section 6)
        sa.Column("channel_first_seen", TEXT, nullable=True),
        # {"marketing_email": bool, "profiling": bool, "basis": "..."} - section 10
        sa.Column("consent_flags", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        # soft delete / GDPR erasure marker
        sa.Column("deleted_at", TIMESTAMPTZ, nullable=True),
        created_at_column(),
        # earns its place: identity resolution merges rows over time
        updated_at_column(),
        sa.CheckConstraint("jsonb_typeof(contact_hashes) = 'object'", name=ck(TABLE, "hashes_obj")),
        sa.CheckConstraint("jsonb_typeof(consent_flags) = 'object'", name=ck(TABLE, "consent_obj")),
        sa.CheckConstraint(
            f"channel_first_seen IS NULL OR channel_first_seen IN ({CHANNELS})",
            name=ck(TABLE, "first_channel"),
        ),
        # Enforces the erasure discipline in the schema itself: a soft-deleted
        # customer MUST carry no contact hashes. See section 10.
        sa.CheckConstraint(
            "deleted_at IS NULL OR contact_hashes = '{}'::jsonb", name=ck(TABLE, "erasure")
        ),
    )


def downgrade() -> None:
    drop_table_if_present(TABLE)
