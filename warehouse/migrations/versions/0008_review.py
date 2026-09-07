"""review

Revision ID: 0008
Revises: 0007
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

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "review"

# Subset of the shared channel vocabulary plus 'google', which is a review
# source but never an order channel.
SOURCES = "'google','lieferando','wolt','uber_eats','instagram','direct'"


def upgrade() -> None:
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        sa.Column("source", TEXT, nullable=False),
        sa.Column("external_id", TEXT, nullable=False),  # the platform's review id
        # usually NULL; set only when identity resolution matched
        sa.Column("customer_id", UUID, nullable=True),
        # NULLABLE: star-only reviews are the common case
        sa.Column("text", TEXT, nullable=True),
        # NULLABLE: Instagram comments carry no rating. Normalised to 1.0-5.0 at
        # ingest (Uber Eats thumbs map to 5/1); numeric(2,1) because aggregate
        # and half-star values exist.
        sa.Column("rating", RATING, nullable=True),
        sa.Column("posted_at", TIMESTAMPTZ, nullable=False),
        # {"cold_food": -0.8, "portion": 0.4} from the live review pipeline
        sa.Column("aspects", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("replied_at", TIMESTAMPTZ, nullable=True),
        sa.Column("reply_text", TEXT, nullable=True),
        # Section 11 open decision: retention window for review text tied to an
        # identified customer. text/reply_text may themselves contain PII, so a
        # GDPR erasure must scrub them - this records that it happened, distinct
        # from the review simply never having been replied to.
        sa.Column("redacted_at", TIMESTAMPTZ, nullable=True),
        created_at_column(),
        updated_at_column(),  # earns its place: aspects are re-scored on model change
        sa.UniqueConstraint("source", "external_id", name=uq(TABLE, "source_external")),
        sa.CheckConstraint(f"source IN ({SOURCES})", name=ck(TABLE, "source")),
        sa.CheckConstraint(
            "rating IS NULL OR (rating >= 1 AND rating <= 5)", name=ck(TABLE, "rating")
        ),
        sa.CheckConstraint("jsonb_typeof(aspects) = 'object'", name=ck(TABLE, "aspects_obj")),
        sa.CheckConstraint(
            "(replied_at IS NULL AND reply_text IS NULL) "
            "OR (replied_at IS NOT NULL AND reply_text IS NOT NULL)",
            name=ck(TABLE, "reply"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customer.id"], name=fk(TABLE, "customer_id"), ondelete="SET NULL"
        ),
    )
    create_index_if_absent(TABLE, ["customer_id"])
    # DELIBERATELY OMITTED: GIN on aspects. A cloud kitchen produces a few
    # hundred reviews a year; a seq scan is microseconds. Add at ~50k rows as
    #   CREATE INDEX ... USING gin (aspects jsonb_path_ops)
    # - jsonb_path_ops, not the default, being ~3x smaller and supporting @>,
    # which is the only operator aspect lookups need.


def downgrade() -> None:
    drop_table_if_present(TABLE)
