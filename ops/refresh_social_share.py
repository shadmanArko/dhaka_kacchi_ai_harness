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
EXTRACT_SQL = """
    SELECT
        sp.id,
        sp.platform,
        sp.content_type,
        sp.posted_at,
        sp.caption,
        sp.permalink,
        m.impressions, m.reach, m.likes, m.comments, m.shares, m.saves, m.clicks
    FROM social_post sp
    JOIN LATERAL (
        SELECT impressions, reach, likes, comments, shares, saves, clicks
        FROM social_metrics_snapshot s
        WHERE s.social_post_id = sp.id
        ORDER BY s.captured_at DESC
        LIMIT 1
    ) m ON true
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
        rows = [dict(row) for row in conn.execute(text(EXTRACT_SQL)).mappings()]

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
