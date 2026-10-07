"""Refreshes `social_share` - a separate, isolated Postgres database holding
a decoupled copy of organic social media performance data, shared with an
outside collaborator over a tunnel-only SSH login + the social_share_reader
role (SELECT-only). See deploy/CLAUDE.md's "Social share database" section
for the full design and one-time setup.

Full-refresh design (TRUNCATE then bulk insert), not incremental upsert:
`social_share.social_post_metrics` is a derived data PRODUCT, not a source
of truth, so mirroring the current state exactly is simpler and safer than
merge logic that would need to handle rows that no longer exist upstream.

Run via `make refresh-social-share`, scheduled daily on cron after the
ingest jobs land fresh data (deploy/CLAUDE.md) - a run before they finish
would ship yesterday's numbers, not today's.
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, text

from warehouse.config import ConfigError, load_settings, load_social_share_target_settings

# One row per post, its platform-native fields, and its latest metrics
# snapshot - a flat, self-explanatory shape for an outside reader who has
# never seen this repo's internal social_post/social_metrics_snapshot split.
# LATERAL + ORDER BY captured_at DESC LIMIT 1 picks the latest snapshot per
# post even though today there is only ever one (see ml/01-data/report.md's
# identical reasoning) - correct now and if periodic re-snapshots are ever
# added later.
# WHICH PLATFORMS LEAVE THE BUILDING is an explicit allowlist, not "whatever is
# in social_post". This query used to have no platform filter at all, so the
# first YouTube ingest followed by the next daily refresh would have handed an
# outside collaborator YouTube data nobody had decided to share. Adding a
# platform to this tuple is a deliberate act - sharing it is a decision, not a
# side effect of ingesting it.
SHARED_PLATFORMS = ("instagram", "facebook", "threads")

# COALESCE(..., 0) is a compatibility shim, not a statement of fact: the shared
# table's metric columns are NOT NULL (deploy/postgres-init/01-init-databases.sh,
# a one-time init script this repo cannot alter from a migration), and the
# warehouse columns became nullable in 0030. For the three shared platforms
# every value is already a real integer, so this changes nothing today; it
# exists so a future NULL cannot make TRUNCATE-then-INSERT fail halfway.
EXTRACT_SQL = """
    SELECT
        sp.id,
        sp.platform,
        sp.content_type,
        sp.posted_at,
        sp.caption,
        sp.permalink,
        COALESCE(m.impressions, 0) AS impressions,
        COALESCE(m.reach, 0)       AS reach,
        COALESCE(m.likes, 0)       AS likes,
        COALESCE(m.comments, 0)    AS comments,
        COALESCE(m.shares, 0)      AS shares,
        COALESCE(m.saves, 0)       AS saves,
        COALESCE(m.clicks, 0)      AS clicks
    FROM social_post sp
    JOIN LATERAL (
        SELECT impressions, reach, likes, comments, shares, saves, clicks
        FROM social_metrics_snapshot s
        WHERE s.social_post_id = sp.id
        ORDER BY s.captured_at DESC
        LIMIT 1
    ) m ON true
    WHERE sp.platform = ANY(:platforms)
    ORDER BY sp.posted_at
"""

INSERT_SQL = """
    INSERT INTO social_post_metrics
        (id, platform, content_type, posted_at, caption, permalink,
         impressions, reach, likes, comments, shares, saves, clicks, refreshed_at)
    VALUES
        (:id, :platform, :content_type, :posted_at, :caption, :permalink,
         :impressions, :reach, :likes, :comments, :shares, :saves, :clicks, now())
"""


def run() -> int:
    source_settings = load_settings()
    target_settings = load_social_share_target_settings()

    source_engine = create_engine(source_settings.sqlalchemy_url)
    target_engine = create_engine(target_settings.sqlalchemy_url)

    with source_engine.connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                text(EXTRACT_SQL), {"platforms": list(SHARED_PLATFORMS)}
            ).mappings()
        ]

    # TRUNCATE + insert in one transaction: the reader role never sees a
    # half-refreshed (or briefly empty) table, only the old snapshot or the
    # new one.
    with target_engine.begin() as conn:
        conn.execute(text("TRUNCATE social_post_metrics"))
        if rows:
            conn.execute(text(INSERT_SQL), rows)

    print(f"refreshed social_share.social_post_metrics: {len(rows)} row(s)")
    return 0


def main() -> int:
    try:
        return run()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
