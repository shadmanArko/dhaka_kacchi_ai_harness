"""social_followers

Revision ID: 0035
Revises: 0034

Daily follower / subscriber counts for Instagram, Facebook, Threads and YouTube.

TWO PIECES:

  raw_social_followers   append-only: one row per platform per CAPTURE, holding the
                         API's answer verbatim. Never rewritten, so every capture
                         survives even when two land on the same day.
  social_account_daily   gains `followers` + `followers_fetched_at` (typed, one
                         value per platform per day - the last capture of that day).

WHY A COLUMN ON social_account_daily AND NOT A NEW TABLE: it already is "one row per
platform per day" (0032), and a reader asking "how is the account doing on day X" wants
followers next to views and subscribers_gained. YouTube's analytics job writes the other
columns of the same row; it names its columns explicitly in its upsert, so neither job
can overwrite the other's figures (youtube_analytics_verify and social_followers_verify
both prove it).

WHAT `day` MEANS HERE: the BERLIN calendar day on which the count was read. A follower
count is not a per-day flow, it is a level at one instant, and none of these APIs can
answer "what was it last Tuesday". So THIS HISTORY CANNOT BE BACKFILLED: a day on which
the job did not run has no row, and stays absent. Absent is not zero. (YouTube's `day`
in the SAME table is YouTube's own reporting day, see 0032; the two can differ by a day
at the edges, and followers_fetched_at says exactly when this one was read.)

NULL means the platform did not report a count (YouTube lets a channel hide its
subscriber count); 0 is a real zero.

`fetched_at` (0032) becomes NULLABLE with no default. It means "when the ANALYTICS columns
of this row were fetched", and 0032 made it NOT NULL DEFAULT now(), so a row created by the
follower job alone would have claimed an analytics fetch that never happened - a row of NULL
views stamped as if YouTube had reported them. Readers can now tell an analytics row
(fetched_at set) from a followers-only one (fetched_at NULL). The YouTube Analytics job
names fetched_at explicitly in every write, so nothing it does changes.

subscribers_gained/subscribers_lost (0032) are YouTube's daily FLOWS; `followers` is the
LEVEL. They are different measurements and need not reconcile exactly - the flows come
from the Analytics API with ~3 days' lag and restatement, the level is read live.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from warehouse.migrations.helpers import (
    JSONB,
    TEXT,
    TIMESTAMPTZ,
    add_column_if_absent,
    ck,
    create_table_if_absent,
    created_at_column,
    drop_column_if_present,
    drop_constraint_if_present,
    drop_table_if_present,
    pk_column,
    pk_constraint,
    replace_constraint,
    uq,
)

revision: str = "0035"
down_revision: str | Sequence[str] | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RAW = "raw_social_followers"
ACCOUNT = "social_account_daily"
PLATFORMS = "'instagram','facebook','threads','youtube'"


def upgrade() -> None:
    create_table_if_absent(
        RAW,
        pk_column(),
        pk_constraint(RAW),
        sa.Column("platform", TEXT, nullable=False),
        sa.Column("captured_at", TIMESTAMPTZ, nullable=False),
        # The API's answer as returned (a single object), never edited.
        sa.Column("payload", JSONB, nullable=False),
        created_at_column(),
        sa.UniqueConstraint("platform", "captured_at", name=uq(RAW, "capture")),
        sa.CheckConstraint(f"platform IN ({PLATFORMS})", name=ck(RAW, "platform")),
    )

    op.alter_column(ACCOUNT, "fetched_at", nullable=True, server_default=None)
    add_column_if_absent(ACCOUNT, sa.Column("followers", sa.BigInteger, nullable=True))
    add_column_if_absent(ACCOUNT, sa.Column("followers_fetched_at", TIMESTAMPTZ, nullable=True))
    replace_constraint(
        ACCOUNT,
        ck(ACCOUNT, "followers"),
        # A count must say when it was read. (The reverse is legal: a hidden YouTube
        # subscriber count is read, at a time, and is NULL.)
        "CHECK ((followers IS NULL OR followers >= 0) "
        "AND (followers IS NULL OR followers_fetched_at IS NOT NULL))",
    )


def downgrade() -> None:
    drop_constraint_if_present(ck(ACCOUNT, "followers"), ACCOUNT, type_="check")
    drop_column_if_present(ACCOUNT, "followers_fetched_at")
    drop_column_if_present(ACCOUNT, "followers")
    drop_table_if_present(RAW)
    # Back to 0032's NOT NULL DEFAULT now(). Followers-only rows have no analytics
    # fetch time, so they are stamped now() - lossy by necessity, the same collapse
    # of "never fetched" into "fetched" that this migration exists to remove.
    op.execute(f"UPDATE {ACCOUNT} SET fetched_at = now() WHERE fetched_at IS NULL")
    op.alter_column(ACCOUNT, "fetched_at", nullable=False, server_default=sa.text("now()"))
