"""Detects a drop in average per-post engagement (likes+comments+shares,
each post's LATEST metrics snapshot) between the last 14 days' posts and
the preceding 14 days', per platform.

KNOWN BIAS, DELIBERATE: a post's engagement accumulates over time, so a
post published 2 days ago has had less time to earn likes than one from
3 weeks ago - this comparison is directional (a real, large drop is a real
signal), not a precise rate. Matches ARCHITECTURE.md section 4.1's own
worked example ("Instagram conversion -18%") in spirit, not literally -
"conversion" would need click-through data this warehouse doesn't capture
per-post; engagement is what's actually measurable today.

MIN_POSTS_PER_WINDOW guards against one unusually quiet or busy day
swinging the comparison on thin data - both windows need real volume
before this fires at all.
"""

from __future__ import annotations

import sqlalchemy as sa

from ops.cockpit import DetectorResult

AGENT = "brand-marketing-detector"
MIN_POSTS_PER_WINDOW = 3
DROP_THRESHOLD = 0.30

_SQL = sa.text(
    """
    WITH latest_snapshot AS (
        SELECT DISTINCT ON (social_post_id) *
        FROM social_metrics_snapshot
        ORDER BY social_post_id, captured_at DESC
    ),
    bucketed AS (
        SELECT
            sp.platform,
            CASE WHEN sp.posted_at >= now() - interval '14 days' THEN 'recent' ELSE 'prior' END
                AS bucket,
            (ls.likes + ls.comments + ls.shares) AS engagement
        FROM social_post sp
        JOIN latest_snapshot ls ON ls.social_post_id = sp.id
        WHERE sp.posted_at >= now() - interval '28 days'
    )
    SELECT platform, bucket, count(*) AS post_count, avg(engagement) AS avg_engagement
    FROM bucketed
    GROUP BY platform, bucket
    """
)


def check(conn: sa.Connection) -> list[DetectorResult]:
    by_platform: dict[str, dict[str, tuple[int, float]]] = {}
    for row in conn.execute(_SQL).all():
        by_platform.setdefault(row.platform, {})[row.bucket] = (
            row.post_count,
            float(row.avg_engagement or 0),
        )

    results: list[DetectorResult] = []
    for platform, buckets in by_platform.items():
        alert_key = f"social_engagement_drop_{platform}"
        recent = buckets.get("recent")
        prior = buckets.get("prior")

        if (
            not recent
            or not prior
            or recent[0] < MIN_POSTS_PER_WINDOW
            or prior[0] < MIN_POSTS_PER_WINDOW
            or prior[1] <= 0
        ):
            results.append(DetectorResult(AGENT, alert_key, triggered=False))
            continue

        recent_count, recent_avg = recent
        prior_count, prior_avg = prior
        drop = (prior_avg - recent_avg) / prior_avg
        if drop < DROP_THRESHOLD:
            results.append(DetectorResult(AGENT, alert_key, triggered=False))
            continue

        results.append(
            DetectorResult(
                AGENT,
                alert_key,
                triggered=True,
                severity="warn",
                title=f"{platform.capitalize()} engagement down {drop:.0%} vs. the prior 2 weeks",
                detail=(
                    f"Avg engagement/post: {recent_avg:.1f} (last 14d, {recent_count} posts) "
                    f"vs {prior_avg:.1f} (prior 14d, {prior_count} posts)."
                ),
            )
        )
    return results
