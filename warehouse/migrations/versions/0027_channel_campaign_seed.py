"""channel_campaign_seed

Revision ID: 0027
Revises: 0026

Seeds the first REAL rows into channel/campaign/campaign_variant (0017-0019),
which have existed as schema-only since they were built - see
DATA_CONSTRAINTS.md's prior note that "nothing yet writes real channel/
campaign/variant rows in production." One channel + one open-ended "ongoing"
campaign + one catch-all variant per organic social platform this warehouse
actually ingests (instagram.py/facebook.py/threads.py) - NOT a real planned-
content campaign structure, since none exists yet. This is what
warehouse/ingest/events.py resolves a website visit's captured utm_source/
utm_content against (see that module's own docstring for the resolution
logic) - the write side is dhaka-kacchi-connect's src/lib/utmCapture.ts,
which stamps utm_source=<platform>&utm_content=bio_link on every event in a
session that landed via a link carrying those params.

campaign.starts_at is each platform's REAL earliest social_post.posted_at
(pulled from production 2026-09-23), not this migration's run date - see
CLAUDE.md's own recipe.active_from war story on exactly this mistake: a
baseline dated "now" instead of "always true" silently zeroes out
point-in-time correctness for everything before it. An event captured before
these rows existed is not retroactively attributed (event upsert is DO
NOTHING - see events.py), but the campaign's own starts_at is still the
honest answer to "when did this channel's organic posting actually begin."

These are pre-existing tables (unlike 0014's event_taxonomy, created in the
same migration as its seed) - downgrade() deletes exactly these rows by slug,
child-before-parent, rather than dropping the tables.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from warehouse.migrations.helpers import (
    TEXT,
    TIMESTAMPTZ,
    UUID,
    delete_by,
    upsert,
)

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamptz(value: str) -> sa.sql.ColumnElement:
    """Offline-safe TIMESTAMPTZ literal - see 0012_seed.py's identical helper.
    A bare Python str bound to a TIMESTAMPTZ column has no literal renderer
    under `make sql`'s literal_binds=True."""
    return sa.cast(sa.literal(value, TEXT), TIMESTAMPTZ)


channel_t = sa.table(
    "channel",
    sa.column("id", UUID),
    sa.column("slug", TEXT),
    sa.column("name", TEXT),
    sa.column("kind", TEXT),
    sa.column("platform", TEXT),
)
campaign_t = sa.table(
    "campaign",
    sa.column("id", UUID),
    sa.column("slug", TEXT),
    sa.column("channel_id", UUID),
    sa.column("name", TEXT),
    sa.column("starts_at", TIMESTAMPTZ),
    sa.column("status", TEXT),
)
campaign_variant_t = sa.table(
    "campaign_variant",
    sa.column("id", UUID),
    sa.column("slug", TEXT),
    sa.column("campaign_id", UUID),
    sa.column("utm_content", TEXT),
)

# Fixed ids so downgrade() and a future re-seed both know exactly what this
# migration owns - same reasoning as 0012_seed.py/0014's own fixed-id
# pattern. Namespaced per table (c1/c2/c3) purely for readability; uuid
# uniqueness only needs to hold within each table.
CHANNEL_IDS = {
    "instagram-organic": uuid.UUID("c1000000-0000-4000-8000-000000000001"),
    "facebook-organic": uuid.UUID("c1000000-0000-4000-8000-000000000002"),
    "threads-organic": uuid.UUID("c1000000-0000-4000-8000-000000000003"),
}
CAMPAIGN_IDS = {
    "instagram-organic-ongoing": uuid.UUID("c2000000-0000-4000-8000-000000000001"),
    "facebook-organic-ongoing": uuid.UUID("c2000000-0000-4000-8000-000000000002"),
    "threads-organic-ongoing": uuid.UUID("c2000000-0000-4000-8000-000000000003"),
}
CAMPAIGN_VARIANT_IDS = {
    "instagram-organic-bio-link": uuid.UUID("c3000000-0000-4000-8000-000000000001"),
    "facebook-organic-bio-link": uuid.UUID("c3000000-0000-4000-8000-000000000002"),
    "threads-organic-bio-link": uuid.UUID("c3000000-0000-4000-8000-000000000003"),
}

# slug, name, platform
CHANNEL_ROWS = [
    ("instagram-organic", "Instagram (organic)", "instagram"),
    ("facebook-organic", "Facebook (organic)", "facebook"),
    ("threads-organic", "Threads (organic)", "threads"),
]

# slug, channel_slug, name, starts_at (real earliest social_post.posted_at
# per platform, pulled from production 2026-09-23 - see module docstring)
CAMPAIGN_ROWS = [
    (
        "instagram-organic-ongoing",
        "instagram-organic",
        "Instagram organic posting",
        "2026-04-28T22:22:07+00:00",
    ),
    (
        "facebook-organic-ongoing",
        "facebook-organic",
        "Facebook organic posting",
        "2026-04-26T09:29:28+00:00",
    ),
    (
        "threads-organic-ongoing",
        "threads-organic",
        "Threads organic posting",
        "2026-06-02T15:19:32+00:00",
    ),
]

# slug, campaign_slug, utm_content. "bio_link" matches the ONE link each
# platform's profile actually carries today - there is no per-post link, so
# one catch-all variant per platform is the honest granularity, not an
# oversight. Add a real per-post/per-link variant later if a link-in-bio
# tool with per-post links is ever adopted.
CAMPAIGN_VARIANT_ROWS = [
    ("instagram-organic-bio-link", "instagram-organic-ongoing", "bio_link"),
    ("facebook-organic-bio-link", "facebook-organic-ongoing", "bio_link"),
    ("threads-organic-bio-link", "threads-organic-ongoing", "bio_link"),
]


def upgrade() -> None:
    upsert(
        channel_t,
        [
            {
                "id": CHANNEL_IDS[slug],
                "slug": slug,
                "name": name,
                "kind": "organic_social",
                "platform": platform,
            }
            for slug, name, platform in CHANNEL_ROWS
        ],
        conflict_on=["slug"],
        update=["name", "platform"],
    )
    upsert(
        campaign_t,
        [
            {
                "id": CAMPAIGN_IDS[slug],
                "slug": slug,
                "channel_id": CHANNEL_IDS[channel_slug],
                "name": name,
                "starts_at": _timestamptz(starts_at),
                "status": "active",
            }
            for slug, channel_slug, name, starts_at in CAMPAIGN_ROWS
        ],
        conflict_on=["slug"],
        # starts_at excluded: it's a historical fact fixed at seed time, not
        # something a re-run should ever move. status/name stay operator-
        # adjustable via a future admin surface without a redeploy undoing it
        # - but there is no such surface yet, so converging both here is
        # still correct for now.
        update=["name", "status"],
    )
    upsert(
        campaign_variant_t,
        [
            {
                "id": CAMPAIGN_VARIANT_IDS[slug],
                "slug": slug,
                "campaign_id": CAMPAIGN_IDS[campaign_slug],
                "utm_content": utm_content,
            }
            for slug, campaign_slug, utm_content in CAMPAIGN_VARIANT_ROWS
        ],
        conflict_on=["slug"],
        update=["utm_content"],
    )


def downgrade() -> None:
    # Child before parent, matching each FK's RESTRICT direction.
    delete_by(campaign_variant_t, "slug", [slug for slug, _, _ in CAMPAIGN_VARIANT_ROWS])
    delete_by(campaign_t, "slug", [slug for slug, _, _, _ in CAMPAIGN_ROWS])
    delete_by(channel_t, "slug", [slug for slug, _, _ in CHANNEL_ROWS])
