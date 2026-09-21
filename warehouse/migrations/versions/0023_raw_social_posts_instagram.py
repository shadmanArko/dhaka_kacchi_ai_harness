"""raw_social_posts_instagram

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (
    JSONB,
    TEXT,
    create_table_if_absent,
    created_at_column,
    drop_table_if_present,
    pk_column,
    pk_constraint,
    updated_at_column,
    uq,
)

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "raw_social_posts_instagram"


def upgrade() -> None:
    # RAW LANDING per ARCHITECTURE.md section 4.1/4.7, same shape as
    # raw_orders_direct/raw_events_direct: one table per source, land
    # verbatim first, transform second. No `platform` column - this table
    # only ever holds Instagram Graph API media rows by construction; a
    # future Facebook-page ingest job gets its own raw_*_facebook table
    # rather than a shared polymorphic one, same reasoning raw_orders_direct
    # already documents.
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        # The Instagram media id (social_post.external_id after transform).
        sa.Column("external_id", TEXT, nullable=False),
        # Verbatim media object plus its insights, nested as
        # payload['insights']: {metric_name: value, ...} - see
        # warehouse/ingest/instagram.py's module docstring for why the
        # insights metric list itself is UNVERIFIED against a live account
        # as of this migration.
        sa.Column("payload", JSONB, nullable=False),
        created_at_column(),  # first extraction run that landed this row
        updated_at_column(),  # most recent extraction run that re-confirmed it
        sa.UniqueConstraint("external_id", name=uq(TABLE, "external_id")),
    )


def downgrade() -> None:
    drop_table_if_present(TABLE)
