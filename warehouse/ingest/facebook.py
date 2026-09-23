"""Ingest Facebook Page post data into the warehouse, via the Meta Graph
API. See ARCHITECTURE.md section 4.7.

======================================================================
VERIFIED against the real Dhaka Kacchi Facebook Page via Graph API Explorer
(2026-09-22, API version v26.0):
  - GET /{page_id}/posts with POST_FIELDS below returns real posts, id
    format "{page_id}_{post_id}" (different from Instagram's plain numeric
    id). attachments.media_type values are lowercase: "video", "photo",
    "album" - no "reel" distinction at this level even for reel-permalink
    posts, unlike Instagram's REELS media_product_type.
  - GET /{post_id}?fields=comments.summary(true),shares: comments/shares are
    ordinary FIELDS on the post object, not Insights metrics - there is no
    post_comments/post_shares Insights metric at all. `shares` is entirely
    absent from the response when a post has zero shares (not an error, not
    a zero value - just an omitted key).
  - GET /{post_id}/insights: almost every metric name we tried was rejected,
    including several NOT marked deprecated in Meta's own docs
    (post_reactions_by_type_total, post_engaged_users) - root cause was a
    MISSING PERMISSION (read_insights), not wrong metric names. Once
    read_insights was granted: post_reactions_by_type_total confirmed
    working, but returns a NESTED breakdown ({"like": 4}, keyed by reaction
    type), not a plain scalar like Instagram's insights. post_clicks
    confirmed working as a plain scalar.
  - RE-VERIFIED 2026-09-23 (after a token rotation - see below): a working
    reach/impressions equivalent WAS found, but it required checking Meta's
    OWN current docs, not assuming the blog post's phrasing - post_impressions
    itself is still fully removed ("(#100) The value must be a valid insights
    metric"), and so is post_impressions_organic (also invalid - the blog
    post implying it still works was wrong, or it was removed since).
    post_media_view ("The number of times your Page's post entered a
    person's screen" - identical wording to the old deprecated
    post_impressions) IS a working impressions replacement. post_total_media_
    view_unique IS a working reach replacement (unique viewers). Both
    confirmed live against a real post, together with post_reactions_by_type_
    total/post_clicks in one combined call.
  - GOTCHA: post_total_media_view_unique's response contains TWO entries for
    the SAME metric name - one period="lifetime" (the real cumulative total)
    and one period="day" (a short recent-days breakdown, values near 0 for
    a post that's been up a while). A naive {item["name"]: ...} dict build
    lets the "day" entry silently overwrite the "lifetime" one - _fetch_
    insights below filters to period=="lifetime" specifically to avoid this.
    No other metric used here has exhibited this multi-period quirk, but the
    filter is applied uniformly since Meta could add it to any metric later.
  - No "saved" equivalent exists for Facebook posts at all (Instagram-only
    concept) - saves is always 0 here, deliberately.
======================================================================

SETUP: same Meta Developer App as warehouse/ingest/instagram.py (see that
module's docstring for the account/App/Page prerequisites), plus:
  - The "Manage everything on your Page" use case added to the app (Meta
    dashboard: My Apps -> your app -> Use cases -> Add use cases -> Content
    management -> "Manage everything on your Page").
  - Permissions: pages_read_engagement, pages_show_list (both may already
    be added from the Instagram setup), pages_read_user_content, and
    read_insights.
  - A Page Access Token (not a User Token) for the target Page - in Graph
    API Explorer, select the Page itself (not "User Token") in the "User or
    Page" dropdown before generating.
  - Set FACEBOOK_PAGE_ACCESS_TOKEN and FACEBOOK_PAGE_ID - see .env.example.

Same "land raw, then transform" shape as instagram.py/direct.py/events.py:
raw_social_posts_facebook first, social_post + social_metrics_snapshot
second. Run with `make ingest-facebook`; preview with
`make ingest-facebook-dry-run`.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa

from warehouse.config import (
    ConfigError,
    FacebookSourceSettings,
    Settings,
    load_facebook_source_settings,
    load_settings,
)
from warehouse.ingest.upsert import upsert_returning

GRAPH_API_VERSION = "v26.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Verified against real posts - see module docstring. comments/shares are
# ordinary fields, not Insights metrics.
POST_FIELDS = (
    "id,message,created_time,permalink_url,attachments{media_type},comments.summary(true),shares"
)

# Verified against real posts - see module docstring. post_reactions_by_type_
# total is a nested breakdown, handled specially in transform_and_load, not a
# plain scalar like the other three. post_total_media_view_unique needs the
# period=="lifetime" filter in _fetch_insights below.
INSIGHTS_METRICS = (
    "post_reactions_by_type_total,post_clicks,post_media_view,post_total_media_view_unique"
)

REQUEST_TIMEOUT_S = 30


class ExtractionError(RuntimeError):
    """The source read failed in a way the operator must act on. Never caught."""


@dataclass(frozen=True, slots=True)
class RunResult:
    raw_landed: int
    posts_upserted: int
    snapshots_upserted: int


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _graph_get(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ExtractionError(f"Graph API error {exc.code} for {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise ExtractionError(f"Graph API unreachable: {exc}") from exc


def _fetch_posts(source: FacebookSourceSettings) -> list[dict]:
    """Paginates via the "paging.next" link Meta returns, same reasoning as
    instagram.py's _fetch_media: that cursor token is opaque and not meant
    to be built by the caller."""
    params = urllib.parse.urlencode(
        {"fields": POST_FIELDS, "access_token": source.access_token, "limit": "25"}
    )
    url: str | None = f"{GRAPH_API_BASE}/{source.page_id}/posts?{params}"
    posts: list[dict] = []
    while url:
        data = _graph_get(url)
        posts.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
    return posts


def _fetch_insights(source: FacebookSourceSettings, post_id: str) -> dict[str, object]:
    params = urllib.parse.urlencode(
        {"metric": INSIGHTS_METRICS, "access_token": source.access_token}
    )
    url = f"{GRAPH_API_BASE}/{post_id}/insights?{params}"
    try:
        data = _graph_get(url)
    except ExtractionError:
        # Same reasoning as instagram.py: one post's metric-availability
        # quirk shouldn't abort the entire run.
        return {}
    # period=="lifetime" only - post_total_media_view_unique also returns a
    # period=="day" entry under the SAME name, which would otherwise
    # silently overwrite the real lifetime total - see module docstring's
    # GOTCHA note.
    return {
        item["name"]: item["values"][0]["value"]
        for item in data.get("data", [])
        if item.get("period") == "lifetime"
    }


def extract(source: FacebookSourceSettings) -> list[dict]:
    """Full extract every run, same reasoning as every other ingest job
    here: no incremental cursor, correctness comes from the upsert layer."""
    posts = _fetch_posts(source)
    for post in posts:
        post["insights"] = _fetch_insights(source, post["id"])
    return posts


# ---------------------------------------------------------------------------
# Raw landing
# ---------------------------------------------------------------------------

raw_social_posts_facebook = sa.table(
    "raw_social_posts_facebook",
    sa.column("id"),
    sa.column("external_id"),
    sa.column("payload"),
    sa.column("updated_at"),
)


def land_raw(conn: sa.Connection, posts: list[dict]) -> int:
    rows = [
        {
            "external_id": post["id"],
            "payload": json.dumps(post),
            "updated_at": sa.func.now(),
        }
        for post in posts
    ]
    result = upsert_returning(
        conn,
        raw_social_posts_facebook,
        rows,
        conflict_on=["external_id"],
        update=["payload", "updated_at"],
        returning=["id"],
    )
    return len(result)


# ---------------------------------------------------------------------------
# Transform + load
# ---------------------------------------------------------------------------

social_post_t = sa.table(
    "social_post",
    sa.column("id"),
    sa.column("platform"),
    sa.column("external_id"),
    sa.column("posted_at"),
    sa.column("permalink"),
    sa.column("content_type"),
    sa.column("caption"),
    sa.column("updated_at"),
)
social_metrics_snapshot_t = sa.table(
    "social_metrics_snapshot",
    sa.column("id"),
    sa.column("social_post_id"),
    sa.column("captured_at"),
    sa.column("impressions"),
    sa.column("reach"),
    sa.column("likes"),
    sa.column("comments"),
    sa.column("shares"),
    sa.column("saves"),
    sa.column("clicks"),
)


def _map_content_type(payload: dict) -> str | None:
    """Verified against real posts - see module docstring. Facebook's
    attachments.media_type carries no reel/story distinction the way
    Instagram's media_product_type does, so a Facebook reel lands as
    "video" here, not "reel" - a real, accepted precision gap."""
    attachments = (payload.get("attachments") or {}).get("data") or []
    media_type = attachments[0].get("media_type") if attachments else None
    if media_type == "photo":
        return "image"
    if media_type == "video":
        return "video"
    if media_type == "album":
        return "carousel"
    return None


def transform_and_load(conn: sa.Connection, *, captured_at: datetime) -> tuple[int, int]:
    """captured_at shared across the whole run, same idempotency shape as
    instagram.py's transform_and_load."""
    raw_rows = conn.execute(
        sa.text("SELECT external_id, payload FROM raw_social_posts_facebook")
    ).all()

    posts_upserted = 0
    snapshots_upserted = 0
    for external_id, payload in raw_rows:
        (post_row,) = upsert_returning(
            conn,
            social_post_t,
            [
                {
                    "platform": "facebook",
                    "external_id": external_id,
                    "posted_at": datetime.fromisoformat(payload["created_time"]),
                    "permalink": payload.get("permalink_url"),
                    "content_type": _map_content_type(payload),
                    "caption": payload.get("message"),
                    "updated_at": sa.func.now(),
                }
            ],
            conflict_on=["platform", "external_id"],
            update=["permalink", "content_type", "caption", "updated_at"],
            returning=["id"],
        )
        posts_upserted += 1

        insights = payload.get("insights") or {}
        # post_reactions_by_type_total is a nested {reaction_type: count}
        # breakdown, not a scalar (see module docstring) - "likes" here
        # means specifically the "like" reaction, not total reactions of
        # every type. love/wow/haha/sorry/anger aren't tracked in this
        # warehouse's schema; a real, accepted gap, not an oversight.
        reactions = insights.get("post_reactions_by_type_total") or {}
        shares = payload.get("shares") or {}

        inserted = upsert_returning(
            conn,
            social_metrics_snapshot_t,
            [
                {
                    "social_post_id": post_row.id,
                    "captured_at": captured_at,
                    "impressions": insights.get("post_media_view", 0),
                    "reach": insights.get("post_total_media_view_unique", 0),
                    "likes": reactions.get("like", 0),
                    "comments": (payload.get("comments") or {})
                    .get("summary", {})
                    .get("total_count", 0),
                    "shares": shares.get("count", 0),
                    "saves": 0,  # no Facebook equivalent exists
                    "clicks": insights.get("post_clicks", 0),
                }
            ],
            conflict_on=["social_post_id", "captured_at"],
            update=None,  # DO NOTHING: a snapshot is immutable once captured
            returning=["id"],
        )
        snapshots_upserted += len(inserted)

    return posts_upserted, snapshots_upserted


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(settings: Settings, source: FacebookSourceSettings, *, dry_run: bool = False) -> RunResult:
    posts = extract(source)

    if dry_run:
        print(f"{len(posts)} post(s) at the source:")
        for post in posts:
            print(f"  {post['id']}  {post.get('created_time')}  {post.get('permalink_url')}")
        return RunResult(raw_landed=0, posts_upserted=0, snapshots_upserted=0)

    captured_at = datetime.now(UTC)
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:
            raw_landed = land_raw(conn, posts)
        with engine.begin() as conn:
            posts_upserted, snapshots_upserted = transform_and_load(conn, captured_at=captured_at)
    finally:
        engine.dispose()

    return RunResult(
        raw_landed=raw_landed,
        posts_upserted=posts_upserted,
        snapshots_upserted=snapshots_upserted,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="extract and print what would be ingested; write nothing",
    )
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        source_settings = load_facebook_source_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        result = run(settings, source_settings, dry_run=args.dry_run)
    except ExtractionError as exc:
        print(f"extraction error: {exc}", file=sys.stderr)
        return 3

    if not args.dry_run:
        print(
            f"landed {result.raw_landed} raw row(s), "
            f"upserted {result.posts_upserted} post(s), "
            f"{result.snapshots_upserted} snapshot(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
