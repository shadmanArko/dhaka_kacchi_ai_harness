"""channel

Revision ID: 0017
Revises: 0016

First table of the ARCHITECTURE.md section 4.7 attribution backbone. The
marketing acquisition channel - NOT the same thing as orders.channel, which
is fulfillment platform (Lieferando/Wolt/direct). Do not conflate the two;
see the section 4.7 note on this exact point.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (
    TEXT,
    ck,
    create_table_if_absent,
    created_at_column,
    drop_table_if_present,
    pk_column,
    pk_constraint,
    slug_column,
    updated_at_column,
    uq,
)

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "channel"

KINDS = "'owned','paid_social','paid_search','marketplace','organic_social','offline'"


def upgrade() -> None:
    create_table_if_absent(
        TABLE,
        pk_column(),
        pk_constraint(TABLE),
        slug_column(),
        sa.Column("name", TEXT, nullable=False),
        sa.Column("kind", TEXT, nullable=False),
        # Deliberately unconstrained, unlike `kind`: the set of platforms
        # (instagram, facebook, google, tiktok, direct, email, ...) grows
        # over time, and a CHECK here would need constant upkeep for no
        # real safety benefit - `kind` is the small, stable vocabulary that
        # actually needs one.
        sa.Column("platform", TEXT, nullable=True),
        created_at_column(),
        updated_at_column(),
        sa.UniqueConstraint("slug", name=uq(TABLE, "slug")),
        sa.CheckConstraint(f"kind IN ({KINDS})", name=ck(TABLE, "kind")),
    )


def downgrade() -> None:
    drop_table_if_present(TABLE)
