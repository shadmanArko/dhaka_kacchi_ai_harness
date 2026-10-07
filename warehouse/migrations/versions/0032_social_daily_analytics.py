"""social_daily_analytics

Revision ID: 0032
Revises: 0031

Four tables for metrics that are facts about a DAY rather than a reading taken
at a moment: raw_youtube_analytics (the landing table), social_account_daily
(per platform per day), social_post_daily (per post per day) and
social_traffic_source_daily (per platform per day per source of views).

WHY THESE ARE NOT COLUMNS ON social_metrics_snapshot. A snapshot is immutable
(0022: "a metric that moved is a NEW row, never a rewrite"). YouTube Analytics
numbers are the opposite: each day's figure is RESTATED for roughly three days
after it first appears, and the newest days are simply absent until YouTube has
computed them. Forcing those into immutable snapshots would either freeze a
provisional number forever or break the snapshot rule. So the daily tables are
the one place in this schema where re-writing a row is correct, keyed on the
(entity, day) pair and carrying fetched_at so a reader can see how fresh a figure
is. The cumulative watch_seconds/subscribers_gained/impression_ctr columns that
0029 added to the snapshot are deliberately NOT written for YouTube; they stay
available for a source that reports a cumulative figure at read time.

UNITS, deliberately not converted: YouTube reports estimatedMinutesWatched in
WHOLE MINUTES and has no seconds metric (verified against the real channel:
estimatedSecondsWatched is rejected). Multiplying by 60 would claim sixty-second
precision that does not exist, so the column is watch_minutes. Per-day figures
are rounded individually, so a video's daily minutes need not sum to its total.
avg_view_seconds is YouTube's averageViewDuration, in seconds; it is a per-day
average and is NOT additive.

EVERY METRIC IS NULLABLE with no default, same rule as 0029/0030: NULL means the
source did not report it, 0 means a real zero. A day absent from the source is an
absent ROW, never a row of zeros.

social_traffic_source_daily.source_type is open text with no CHECK, like
channel.platform in 0017 and unlike social_post.platform: YouTube adds source
types (SHORTS, YT_SEARCH, NO_LINK_OTHER, ...) on its own schedule, and a closed
vocabulary here would turn a YouTube product change into a failed ingest.

Day boundaries: YouTube documents Analytics reporting in Pacific Time. That is
taken from its documentation, not verified here, so `day` is YouTube's day and
may differ from the Berlin calendar day by up to a day at the edges.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (
    JSONB,
    TEXT,
    TIMESTAMPTZ,
    UUID,
    ck,
    create_table_if_absent,
    created_at_column,
    drop_table_if_present,
    fk,
    pk_column,
    pk_constraint,
    updated_at_column,
    uq,
)

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RAW = "raw_youtube_analytics"
ACCOUNT = "social_account_daily"
POST = "social_post_daily"
TRAFFIC = "social_traffic_source_daily"

# Same list as social_post.platform (0028). Kept in sync by hand, deliberately
# not shared with it: this table has its own reason to widen (a platform with
# account-level data but no posts), so the two should not be coupled.
PLATFORMS = "'instagram','facebook','threads','youtube'"

# The metric columns the two entity tables share.
METRICS = (
    "views",
    "watch_minutes",
    "avg_view_seconds",
    "subscribers_gained",
    "subscribers_lost",
    "likes",
    "comments",
    "shares",
)


def _metric_columns() -> list[sa.Column]:
    return [sa.Column(name, sa.BigInteger, nullable=True) for name in METRICS]


def _nonneg(table: str, columns: Sequence[str]) -> sa.CheckConstraint:
    # NULL passes a CHECK, so `x >= 0` alone already tolerates NULL; the explicit
    # IS NULL arms are for the reader of \d output, same as 0029.
    clause = " AND ".join(f"({c} IS NULL OR {c} >= 0)" for c in columns)
    return sa.CheckConstraint(clause, name=ck(table, "nonneg"))


def _fetched_at() -> sa.Column:
    return sa.Column("fetched_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()"))


def upgrade() -> None:
    # Landing table. One row per (report, window): each daily run asks for a
    # sliding window, so successive runs add rows rather than overwrite, and a
    # same-window re-run replaces its own payload. The payload is the verbatim
    # API response, from which every typed row below is recomputable.
    create_table_if_absent(
        RAW,
        pk_column(),
        pk_constraint(RAW),
        sa.Column("report", TEXT, nullable=False),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        created_at_column(),
        updated_at_column(),
        sa.UniqueConstraint("report", "start_date", "end_date", name=uq(RAW, "window")),
    )

    create_table_if_absent(
        ACCOUNT,
        pk_column(),
        pk_constraint(ACCOUNT),
        sa.Column("platform", TEXT, nullable=False),
        sa.Column("day", sa.Date, nullable=False),
        *_metric_columns(),
        _fetched_at(),
        created_at_column(),
        updated_at_column(),
        sa.UniqueConstraint("platform", "day", name=uq(ACCOUNT, "platform_day")),
        sa.CheckConstraint(f"platform IN ({PLATFORMS})", name=ck(ACCOUNT, "platform")),
        _nonneg(ACCOUNT, METRICS),
    )

    create_table_if_absent(
        POST,
        pk_column(),
        pk_constraint(POST),
        sa.Column("social_post_id", UUID, nullable=False),
        sa.Column("day", sa.Date, nullable=False),
        *_metric_columns(),
        _fetched_at(),
        created_at_column(),
        updated_at_column(),
        sa.UniqueConstraint("social_post_id", "day", name=uq(POST, "post_day")),
        _nonneg(POST, METRICS),
        # CASCADE, same reasoning as social_metrics_snapshot (0022): a day of a
        # post's analytics has no meaning without the post. social_post_id is
        # already indexed as the leading column of uq_social_post_daily_post_day.
        sa.ForeignKeyConstraint(
            ["social_post_id"],
            ["social_post.id"],
            name=fk(POST, "social_post_id"),
            ondelete="CASCADE",
        ),
    )

    create_table_if_absent(
        TRAFFIC,
        pk_column(),
        pk_constraint(TRAFFIC),
        sa.Column("platform", TEXT, nullable=False),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("source_type", TEXT, nullable=False),
        sa.Column("views", sa.BigInteger, nullable=True),
        sa.Column("watch_minutes", sa.BigInteger, nullable=True),
        _fetched_at(),
        created_at_column(),
        updated_at_column(),
        sa.UniqueConstraint(
            "platform", "day", "source_type", name=uq(TRAFFIC, "platform_day_source")
        ),
        sa.CheckConstraint(f"platform IN ({PLATFORMS})", name=ck(TRAFFIC, "platform")),
        _nonneg(TRAFFIC, ("views", "watch_minutes")),
    )


def downgrade() -> None:
    # Children before parents is not needed here (nothing references these but
    # social_post, which stays), but reverse order keeps the habit.
    drop_table_if_present(TRAFFIC)
    drop_table_if_present(POST)
    drop_table_if_present(ACCOUNT)
    drop_table_if_present(RAW)
