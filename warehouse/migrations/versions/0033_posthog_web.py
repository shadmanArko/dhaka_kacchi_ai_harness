"""posthog_web

Revision ID: 0033
Revises: 0032

raw_posthog_events plus four daily summary tables built from it:
web_traffic_daily, web_page_daily, web_acquisition_daily, web_event_daily.

"RAW" HERE MEANS SCRUBBED. Every other raw_* table in this schema holds the
source's response verbatim, and that is the point of landing raw first. This one
cannot: a verbatim PostHog event carries the visitor's full URL (a live
password-reset token was found in it), their customer id, a precise location and
a device fingerprint. So events are reduced to an allowlist of reviewed fields
and their identifiers are replaced by keyed hashes BEFORE they are written; the
unscrubbed event never touches this database. The cost is that a field dropped
today cannot be recovered later from this table, which is the intended trade.
See warehouse/ingest/posthog_sanitize.py for exactly what is kept and why.

source_created_at is when PostHog RECEIVED the event, and is the incremental
watermark, not occurred_at. Events can arrive late (a phone that was offline
flushes its queue hours afterwards, stamped with the time they happened), so a
watermark on occurred_at would skip them forever; ingestion time cannot go
backwards. occurred_at is still what the summaries bucket on.

THE SUMMARY TABLES ARE REBUILT, NOT APPENDED. Each run recomputes a trailing
window of days from raw_posthog_events (delete the window, re-insert it), so a
late event restates the day it belonged to. Raw events are pruned after 13
months but the summaries are not, so a rebuild only ever touches days that still
have their raw events.

Days are BERLIN calendar days (the business runs on Berlin time), unlike the
YouTube tables of 0032, which use YouTube's own Pacific-time day. The two will
disagree by up to a day at the edges; they are different clocks, not a bug.

`locale` is read from a two-letter path prefix (/de/order -> 'de'; no prefix ->
'en') and `path` has that prefix removed, so /de/order and /order are the same
page in two languages rather than two pages. The prefix rule assumes no route is
a two-letter word, which holds today.

'Visitors' counts distinct pseudonymous ids. A visitor with cookies keeps one id
across days; a cookieless visitor's id rotates daily by PostHog's design, so
summing visitors over several days overcounts. Per-day figures are right.

Empty-string conventions: acquisition fields are '' (not NULL) when absent, so
the natural key (day, source, medium, ...) can be a plain UNIQUE constraint
without NULLS NOT DISTINCT, which needs Postgres 15+ (local dev is 14).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (
    JSONB,
    TEXT,
    TIMESTAMPTZ,
    ck,
    create_index_if_absent,
    create_table_if_absent,
    created_at_column,
    drop_table_if_present,
    pk_column,
    pk_constraint,
    uq,
)

revision: str = "0033"
down_revision: str | Sequence[str] | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RAW = "raw_posthog_events"
TRAFFIC = "web_traffic_daily"
PAGE = "web_page_daily"
ACQ = "web_acquisition_daily"
EVENT = "web_event_daily"


def _count(name: str) -> sa.Column:
    return sa.Column(name, sa.BigInteger, nullable=False)


def _built_at() -> sa.Column:
    return sa.Column("built_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()"))


def _nonneg(table: str, *columns: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(" AND ".join(f"{c} >= 0" for c in columns), name=ck(table, "nonneg"))


def upgrade() -> None:
    create_table_if_absent(
        RAW,
        pk_column(),
        pk_constraint(RAW),
        # PostHog's own event uuid: the natural key, and what makes re-pulling the
        # overlap window safe.
        sa.Column("event_uuid", TEXT, nullable=False),
        sa.Column("occurred_at", TIMESTAMPTZ, nullable=False),
        sa.Column("source_created_at", TIMESTAMPTZ, nullable=False),
        # The SCRUBBED event: allowlisted properties, hashed identifiers.
        sa.Column("payload", JSONB, nullable=False),
        # No updated_at: an event is an immutable fact, same as a snapshot (0022).
        created_at_column(),
        sa.UniqueConstraint("event_uuid", name=uq(RAW, "event_uuid")),
    )
    create_index_if_absent(RAW, ["occurred_at"])
    create_index_if_absent(RAW, ["source_created_at"])

    create_table_if_absent(
        TRAFFIC,
        pk_column(),
        pk_constraint(TRAFFIC),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("locale", TEXT, nullable=False),
        _count("pageviews"),
        _count("sessions"),
        _count("visitors"),
        _built_at(),
        sa.UniqueConstraint("day", "locale", name=uq(TRAFFIC, "day_locale")),
        _nonneg(TRAFFIC, "pageviews", "sessions", "visitors"),
    )

    create_table_if_absent(
        PAGE,
        pk_column(),
        pk_constraint(PAGE),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("locale", TEXT, nullable=False),
        sa.Column("path", TEXT, nullable=False),
        _count("pageviews"),
        _count("sessions"),
        _count("visitors"),
        _built_at(),
        sa.UniqueConstraint("day", "locale", "path", name=uq(PAGE, "day_locale_path")),
        _nonneg(PAGE, "pageviews", "sessions", "visitors"),
    )

    # One row per (day, source, ...) counting SESSIONS by how they arrived, taken
    # from each session's FIRST pageview - a later in-site pageview carries the
    # site itself as its referrer and would misattribute the visit.
    create_table_if_absent(
        ACQ,
        pk_column(),
        pk_constraint(ACQ),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("utm_source", TEXT, nullable=False),
        sa.Column("utm_medium", TEXT, nullable=False),
        sa.Column("utm_campaign", TEXT, nullable=False),
        sa.Column("utm_content", TEXT, nullable=False),
        sa.Column("referring_domain", TEXT, nullable=False),
        _count("sessions"),
        _built_at(),
        sa.UniqueConstraint(
            "day",
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_content",
            "referring_domain",
            name=uq(ACQ, "day_source"),
        ),
        _nonneg(ACQ, "sessions"),
    )

    # The site's own named events (order_cart_started, order_submitted, ...), not
    # PostHog's $-prefixed ones. Called web_event_daily rather than a "funnel"
    # because a funnel is a way of READING these counts, not a thing stored here.
    create_table_if_absent(
        EVENT,
        pk_column(),
        pk_constraint(EVENT),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("event_name", TEXT, nullable=False),
        _count("events"),
        _count("sessions"),
        _count("visitors"),
        _built_at(),
        sa.UniqueConstraint("day", "event_name", name=uq(EVENT, "day_event")),
        _nonneg(EVENT, "events", "sessions", "visitors"),
    )


def downgrade() -> None:
    for table in (EVENT, ACQ, PAGE, TRAFFIC, RAW):
        drop_table_if_present(table)
