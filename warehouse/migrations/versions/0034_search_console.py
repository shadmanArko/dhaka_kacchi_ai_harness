"""search_console

Revision ID: 0034
Revises: 0033

Google Search Console: how the site performs in Google Search. A landing table
(raw_search_console) and three daily tables at different grains:

  search_site_daily   per day: the property's TRUE totals
  search_page_daily   per day x page x country x device
  search_query_daily  per day x query x page x country x device

WHY THREE TABLES, AND WHICH ONE TO TRUST FOR "HOW MUCH". The page and query
tables are PARTIAL. Google omits any row too small or too anonymized to publish,
and on a small site that is most of them. Measured on the real property on
2026-10-07, for ordinary web search over the same 21 days:

    search_site_daily    471 impressions   18 clicks    (the true totals)
    search_page_daily    151 impressions    3 clicks    (32% / 17% of the truth)
    search_query_daily   151 impressions    3 clicks

So the site table is the ONLY place a total can be read, and the other two answer
"which pages / which searches", never "how many". (In principle the opposite error
is possible too: an impression is counted once per property but once per page, so
a search that showed two pages could double-count in the page table. On a site
this small the omission dwarfs it.) A query that appears in the page totals may be
absent from the query table entirely. Keep all three rather than pretend one can
stand in for another, and expect the gap to narrow as the site gets more traffic.

Only FINAL data is stored. The API will also hand out the newest, still-provisional
days, but a provisional day looks like a bad day (low numbers) until it is
restated, which is a trap for whoever reads the table later. Final data trails by
about two days; the newest days are simply ABSENT until then, never zero. Same
rule as the YouTube tables in 0032.

Each run re-pulls a trailing window and REPLACES it (delete, then insert), so a
row Google later drops stops existing here too. position is Google's average
ranking for that row (1 = top): an average, so it is not additive and must never
be summed. CTR is not stored: it is clicks / impressions and a stored copy could
only disagree with them.

country is Search Console's own lowercase ISO 3166-1 alpha-3 code ('deu', not
'DE'), kept as returned so it joins to Google's documentation, not to PostHog's
two-letter codes. device is DESKTOP / MOBILE / TABLET.

search_type separates ordinary web search from image search, which this site
already appears in. Closed vocabulary (CHECK) because it is Google's own fixed
list, unlike YouTube's open-ended traffic sources.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (
    JSONB,
    TEXT,
    TIMESTAMPTZ,
    ck,
    create_table_if_absent,
    created_at_column,
    drop_table_if_present,
    pk_column,
    pk_constraint,
    updated_at_column,
    uq,
)

revision: str = "0034"
down_revision: str | Sequence[str] | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RAW = "raw_search_console"
SITE = "search_site_daily"
PAGE = "search_page_daily"
QUERY = "search_query_daily"

SEARCH_TYPES = "'web','image','video','news','discover','googleNews'"
DEVICES = "'DESKTOP','MOBILE','TABLET'"


def _metrics() -> list[sa.Column]:
    return [
        sa.Column("clicks", sa.BigInteger, nullable=False),
        sa.Column("impressions", sa.BigInteger, nullable=False),
        sa.Column("position", sa.Numeric(8, 3), nullable=False),
        sa.Column("fetched_at", TIMESTAMPTZ, nullable=False, server_default=sa.text("now()")),
        created_at_column(),
        updated_at_column(),
    ]


def _checks(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(f"search_type IN ({SEARCH_TYPES})", name=ck(table, "search_type")),
        sa.CheckConstraint(
            "clicks >= 0 AND impressions >= 0 AND position >= 0",
            name=ck(table, "metrics"),
        ),
    ]


def upgrade() -> None:
    create_table_if_absent(
        RAW,
        pk_column(),
        pk_constraint(RAW),
        sa.Column("report", TEXT, nullable=False),
        sa.Column("search_type", TEXT, nullable=False),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        # The API's rows, as returned, plus the dimension names they are keyed by.
        sa.Column("payload", JSONB, nullable=False),
        created_at_column(),
        updated_at_column(),
        sa.UniqueConstraint(
            "report", "search_type", "start_date", "end_date", name=uq(RAW, "window")
        ),
    )

    create_table_if_absent(
        SITE,
        pk_column(),
        pk_constraint(SITE),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("search_type", TEXT, nullable=False),
        *_metrics(),
        sa.UniqueConstraint("day", "search_type", name=uq(SITE, "day_type")),
        *_checks(SITE),
    )

    create_table_if_absent(
        PAGE,
        pk_column(),
        pk_constraint(PAGE),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("search_type", TEXT, nullable=False),
        sa.Column("page", TEXT, nullable=False),
        sa.Column("country", TEXT, nullable=False),
        sa.Column("device", TEXT, nullable=False),
        *_metrics(),
        sa.UniqueConstraint(
            "day", "search_type", "page", "country", "device", name=uq(PAGE, "day_page")
        ),
        sa.CheckConstraint(f"device IN ({DEVICES})", name=ck(PAGE, "device")),
        *_checks(PAGE),
    )

    create_table_if_absent(
        QUERY,
        pk_column(),
        pk_constraint(QUERY),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("search_type", TEXT, nullable=False),
        sa.Column("query", TEXT, nullable=False),
        sa.Column("page", TEXT, nullable=False),
        sa.Column("country", TEXT, nullable=False),
        sa.Column("device", TEXT, nullable=False),
        *_metrics(),
        sa.UniqueConstraint(
            "day", "search_type", "query", "page", "country", "device", name=uq(QUERY, "day_query")
        ),
        sa.CheckConstraint(f"device IN ({DEVICES})", name=ck(QUERY, "device")),
        *_checks(QUERY),
    )


def downgrade() -> None:
    for table in (QUERY, PAGE, SITE, RAW):
        drop_table_if_present(table)
