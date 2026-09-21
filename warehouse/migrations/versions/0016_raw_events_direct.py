"""raw_events_direct

Revision ID: 0016
Revises: 0015
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

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "raw_events_direct"


def upgrade() -> None:
    # RAW LANDING per ARCHITECTURE.md section 4.1, same shape as
    # raw_orders_direct (0013): one table per source, land verbatim first,
    # transform second. No `source` column, by the same reasoning
    # raw_orders_direct has no `channel` column - this table only ever holds
    # rows read from dhaka-kacchi-connect's own `events` table.
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        # dhaka-kacchi-connect's events.id ("evt_<uuid>"). Same value
        # warehouse.event.external_id gets after transform, so a raw row and
        # its transformed row are traceable by eye - same pattern as
        # raw_orders_direct.
        sa.Column("external_id", TEXT, nullable=False),
        # Verbatim event row - event_name, occurred_at, source, anonymous_id,
        # session_id, customer_id, order_id, properties, all untransformed.
        sa.Column("payload", JSONB, nullable=False),
        created_at_column(),  # first extraction run that landed this row
        updated_at_column(),  # most recent extraction run that re-confirmed it
        sa.UniqueConstraint("external_id", name=uq(TABLE, "external_id")),
    )


def downgrade() -> None:
    drop_table_if_present(TABLE)
