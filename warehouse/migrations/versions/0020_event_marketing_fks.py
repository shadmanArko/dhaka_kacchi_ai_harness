"""event_marketing_fks

Revision ID: 0020
Revises: 0019

Fulfils the promise in 0014's own docstring/comments: event.channel_id /
campaign_id / campaign_variant_id were left as plain nullable uuid columns
with no FK because channel/campaign/campaign_variant didn't exist yet. They
do now (0017-0019) - add the three FKs via replace_constraint(), per this
repo's own rule that a bare follow-up ALTER ... ADD CONSTRAINT has no IF NOT
EXISTS guard (Postgres has none), so drop-then-add is the only safe,
convergent way to add them.

RESTRICT on all three, same reasoning as campaign -> channel and
campaign_variant -> campaign: an event already attributed to a channel/
campaign/variant is historical evidence, not something a later deletion
should silently orphan or cascade away.
"""

from __future__ import annotations

from collections.abc import Sequence

from warehouse.migrations.helpers import (
    create_index_if_absent,
    drop_constraint_if_present,
    drop_index_if_present,
    fk,
    idx,
    replace_constraint,
)

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "event"


def upgrade() -> None:
    replace_constraint(
        TABLE,
        fk(TABLE, "channel_id"),
        "FOREIGN KEY (channel_id) REFERENCES channel (id) ON DELETE RESTRICT",
    )
    replace_constraint(
        TABLE,
        fk(TABLE, "campaign_id"),
        "FOREIGN KEY (campaign_id) REFERENCES campaign (id) ON DELETE RESTRICT",
    )
    replace_constraint(
        TABLE,
        fk(TABLE, "campaign_variant_id"),
        "FOREIGN KEY (campaign_variant_id) REFERENCES campaign_variant (id) ON DELETE RESTRICT",
    )
    # Every FK's referencing column needs its own supporting index
    # (warehouse/verify.py's check_fk_child_indexes) - partial, since all
    # three are mostly NULL until the attribution layer is actually wired
    # into event production (still a future step - see ARCHITECTURE.md
    # section 4.7's build sequence).
    create_index_if_absent(TABLE, ["channel_id"], where="channel_id IS NOT NULL", suffix="notnull")
    create_index_if_absent(
        TABLE, ["campaign_id"], where="campaign_id IS NOT NULL", suffix="notnull"
    )
    create_index_if_absent(
        TABLE, ["campaign_variant_id"], where="campaign_variant_id IS NOT NULL", suffix="notnull"
    )


def downgrade() -> None:
    drop_index_if_present(idx(TABLE, "channel_id", suffix="notnull"), TABLE)
    drop_index_if_present(idx(TABLE, "campaign_id", suffix="notnull"), TABLE)
    drop_index_if_present(idx(TABLE, "campaign_variant_id", suffix="notnull"), TABLE)
    drop_constraint_if_present(fk(TABLE, "channel_id"), TABLE, type_="foreignkey")
    drop_constraint_if_present(fk(TABLE, "campaign_id"), TABLE, type_="foreignkey")
    drop_constraint_if_present(fk(TABLE, "campaign_variant_id"), TABLE, type_="foreignkey")
