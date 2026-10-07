"""social_metrics_video_columns

Revision ID: 0029
Revises: 0028

Adds the video-shaped metrics social_metrics_snapshot had no home for:
views, watch_seconds, subscribers_gained, impression_ctr. YouTube is the
first source that needs them; Instagram Reels and Threads also report views
today but those still land in `impressions` and are deliberately left alone
here (re-mapping history is a separate, optional backfill, not a side effect
of adding columns).

THE POINT OF THIS MIGRATION, AND WHAT IT DOES DIFFERENTLY FROM 0022:
all four columns are NULLABLE with NO default. 0022's columns are
`NOT NULL DEFAULT 0`, which cannot tell "the platform reported zero" from
"the platform never returned this metric" - and the warehouse already holds
that exact ambiguity: every Instagram post stores impressions = 0, every
Threads post stores reach = 0, and only Facebook has clicks. A reader of
those rows cannot tell a real zero from a gap. These four never have that
problem: NULL means not reported, 0 means a real zero.

impression_ctr is a RATIO in [0, 1] (0.045 = 4.5%), numeric(7,6) because
verify.py rejects float and bare numeric. The YouTube Analytics API's own
unit for this metric (ratio vs percent) is UNVERIFIED against the real
channel at the time of writing - the CHECK below makes a wrong guess fail
loudly on the first real ingest rather than silently store 4.5 as 450%.
Stage C of the YouTube ingester must confirm the unit and normalise before
this column is ever written.

Existing rows are untouched: all four are NULL for every pre-existing
snapshot, which is the honest answer.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (
    add_column_if_absent,
    ck,
    drop_column_if_present,
    drop_constraint_if_present,
    replace_constraint,
)

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "social_metrics_snapshot"
CONSTRAINT = ck(TABLE, "video_metrics")

# NULL passes a CHECK, so `views >= 0` alone already tolerates NULL - the
# explicit IS NULL arms are not needed for correctness, they are here so the
# intent ("unreported is allowed, negative is not") is readable in \d output.
DEFINITION = (
    "CHECK ("
    "(views IS NULL OR views >= 0) "
    "AND (watch_seconds IS NULL OR watch_seconds >= 0) "
    "AND (subscribers_gained IS NULL OR subscribers_gained >= 0) "
    "AND (impression_ctr IS NULL OR (impression_ctr >= 0 AND impression_ctr <= 1))"
    ")"
)


def upgrade() -> None:
    add_column_if_absent(TABLE, sa.Column("views", sa.BigInteger, nullable=True))
    add_column_if_absent(TABLE, sa.Column("watch_seconds", sa.BigInteger, nullable=True))
    add_column_if_absent(TABLE, sa.Column("subscribers_gained", sa.BigInteger, nullable=True))
    add_column_if_absent(TABLE, sa.Column("impression_ctr", sa.Numeric(7, 6), nullable=True))
    replace_constraint(TABLE, CONSTRAINT, DEFINITION)


def downgrade() -> None:
    # Constraint first: it references the columns being dropped. Data in these
    # columns is lost - unavoidable for a column drop, and the reason these
    # are additive-only in practice.
    drop_constraint_if_present(CONSTRAINT, TABLE, type_="check")
    drop_column_if_present(TABLE, "impression_ctr")
    drop_column_if_present(TABLE, "subscribers_gained")
    drop_column_if_present(TABLE, "watch_seconds")
    drop_column_if_present(TABLE, "views")
