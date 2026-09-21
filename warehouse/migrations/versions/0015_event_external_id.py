"""event_external_id

Revision ID: 0015
Revises: 0014

0014 gave `event` a `source` column but no natural-key arbiter for
re-ingestion - unlike `orders`' `UNIQUE (channel, external_id)`, there was
nothing stopping a re-run of a future ingest job from inserting every event
twice. `external_id` is the source system's own id for the event (e.g.
dhaka-kacchi-connect's `events.id`, "evt_<uuid>") - nullable, since a
manually-entered event (source='manual') has no such external system id to
key on and doesn't need dedup.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (
    TEXT,
    add_column_if_absent,
    drop_column_if_present,
    drop_constraint_if_present,
    replace_constraint,
    uq,
)

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "event"
CONSTRAINT = uq(TABLE, "source_external_id")


def upgrade() -> None:
    add_column_if_absent(TABLE, sa.Column("external_id", TEXT, nullable=True))
    # UNIQUE, not a plain add_column - event already exists (0014), so this
    # can't ride create_table's own IF NOT EXISTS. replace_constraint is the
    # documented escape hatch: Postgres has no ADD CONSTRAINT IF NOT EXISTS,
    # so drop-then-add is the only guard, and it also converges a
    # wrong-definition constraint rather than skipping it.
    replace_constraint(TABLE, CONSTRAINT, "UNIQUE (source, external_id)")


def downgrade() -> None:
    drop_constraint_if_present(CONSTRAINT, TABLE, type_="unique")
    drop_column_if_present(TABLE, "external_id")
