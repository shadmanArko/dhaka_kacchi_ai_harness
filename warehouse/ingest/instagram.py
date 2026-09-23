"""Ingest organic Instagram post data into the warehouse, via the Meta
Graph API. See ARCHITECTURE.md section 4.7.

======================================================================
VERIFIED against the real Dhaka Kacchi Instagram Business Account via Graph
API Explorer (2026-09-22, API version v26.0):
  - GET /{business_account_id}/media with MEDIA_FIELDS below returns real
    posts correctly, including pagination via paging.next.
  - GET /{media_id}/insights: `impressions` is REJECTED for every media
    type tested (REELS and FEED) - Meta deprecated it entirely as of Graph
    API v22.0 ("the impressions metric is no longer supported"). Do not
    re-add it. `reach,likes,comments,shares,saved` all confirmed working,
    identically, for both REELS and FEED/IMAGE posts.
  - `media_product_type` values seen in practice: "REELS" and "FEED" -
    confirms _map_content_type()'s REELS->"reel" and (FEED, media_type=
    IMAGE)->"image" branches. "STORY" and CAROUSEL_ALBUM are still
    unverified (no such post existed to test against).
Not yet verified: a real scheduled/automated run (only tested via manual
Graph API Explorer calls so far), and the long-lived Page Access Token flow
- see step 4 below.
======================================================================

SETUP (do this before the first real run):
  1. Create a Meta Developer App at https://developers.facebook.com/apps
     (Business type).
  2. The Instagram account must be a Professional (Business/Creator)
     account, linked to a Facebook Page.
  3. Add the Instagram Graph API product to the app. Exactly four
     permissions are needed - no more: instagram_basic,
     instagram_manage_insights, pages_show_list, pages_read_engagement.
     Each is usable immediately by the app's own developers/testers at
     "Ready for testing" status; Meta App Review is only required to use
     them against *other* businesses' accounts, not your own.
  4. Generate a long-lived access token for that Page (a Page Access Token
     derived from a long-lived User Access Token does not expire) - a
     short-lived token from Graph API Explorer's default "Generate Access
     Token" button expires in ~1-2 hours and is not usable for a scheduled
     job.
  5. Set INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_BUSINESS_ACCOUNT_ID (the
     Instagram Business Account id, NOT the Facebook Page id and NOT the
     @username - find it via GET /me?fields=id,name,instagram_business_account
     using a Page-scoped token) - see .env.example.

Same "land raw, then transform" shape as direct.py/events.py:
raw_social_posts_instagram first, social_post + social_metrics_snapshot
second. Run with `make ingest-instagram`; preview with
`make ingest-instagram-dry-run`.
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
    InstagramSourceSettings,
    Settings,
    load_instagram_source_settings,
    load_settings,
)
from warehouse.ingest.upsert import upsert_returning

# Confirmed working 2026-09-22 (see module docstring). Bump when Meta
# deprecates this version - https://developers.facebook.com/docs/graph-api/changelog
GRAPH_API_VERSION = "v26.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Verified against real posts - see module docstring.
MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,timestamp"

# Verified against real REELS and FEED posts - see module docstring.
# `impressions` deliberately absent: Meta rejects it outright as of v22.0
# ("no longer supported"), confirmed for both media types tested. "saved"
# (not "saves") is Meta's own metric name; the mismatch with our `saves`
# column is intentional and handled in transform_and_load, not a typo.
INSIGHTS_METRICS = "reach,likes,comments,shares,saved"

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


def _fetch_media(source: InstagramSourceSettings) -> list[dict]:
    """Paginates via the "paging.next" link Meta returns in each response,
    rather than reconstructing query params by hand - that cursor token is
    opaque and not meant to be built by the caller."""
    params = urllib.parse.urlencode(
        {"fields": MEDIA_FIELDS, "access_token": source.access_token, "limit": "50"}
    )
    url: str | None = f"{GRAPH_API_BASE}/{source.business_account_id}/media?{params}"
    media: list[dict] = []
    while url:
        data = _graph_get(url)
        media.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
    return media


def _fetch_insights(source: InstagramSourceSettings, media_id: str) -> dict[str, object]:
    params = urllib.parse.urlencode(
        {"metric": INSIGHTS_METRICS, "access_token": source.access_token}
    )
    url = f"{GRAPH_API_BASE}/{media_id}/insights?{params}"
    try:
        data = _graph_get(url)
    except ExtractionError:
        # A metric that doesn't apply to this media's type/product_type
        # 400s the whole insights call (see module docstring) - treated as
        # "no insights available for this post" rather than aborting the
        # entire run over one post.
        return {}
    return {item["name"]: item["values"][0]["value"] for item in data.get("data", [])}


def extract(source: InstagramSourceSettings) -> list[dict]:
    """Full extract every run, same reasoning as direct.py/events.py: no
    incremental cursor, correctness comes from the upsert layer. One
    insights call per post (N+1) - acceptable at organic-content volume;
    revisit if Graph API rate limits ever make that a real problem."""
    media = _fetch_media(source)
    for item in media:
        item["insights"] = _fetch_insights(source, item["id"])
    return media


# ---------------------------------------------------------------------------
# Raw landing
# ---------------------------------------------------------------------------

raw_social_posts_instagram = sa.table(
    "raw_social_posts_instagram",
    sa.column("id"),
    sa.column("external_id"),
    sa.column("payload"),
    sa.column("updated_at"),
)


def land_raw(conn: sa.Connection, media: list[dict]) -> int:
    rows = [
        {
            "external_id": item["id"],
            "payload": json.dumps(item),
            "updated_at": sa.func.now(),
        }
        for item in media
    ]
    result = upsert_returning(
        conn,
        raw_social_posts_instagram,
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
    """Maps Meta's media_type/media_product_type to this warehouse's closed
    social_post.content_type vocabulary (image/video/carousel/reel/story).
    REELS->"reel" and (FEED, media_type=IMAGE)->"image" verified against
    real posts 2026-09-22 (see module docstring). CAROUSEL_ALBUM and STORY
    branches are still unverified - no such post existed on the account to
    test against."""
    product_type = payload.get("media_product_type")
    media_type = payload.get("media_type")
    if product_type == "REELS":
        return "reel"
    if product_type == "STORY":
        return "story"
    if media_type == "CAROUSEL_ALBUM":
        return "carousel"
    if media_type == "VIDEO":
        return "video"
    if media_type == "IMAGE":
        return "image"
    return None


def transform_and_load(conn: sa.Connection, *, captured_at: datetime) -> tuple[int, int]:
    """captured_at is shared across every post transformed in this run, so
    it lines up with social_metrics_snapshot's UNIQUE (social_post_id,
    captured_at) arbiter - re-running the SAME logical run converges
    instead of duplicating, same idempotency shape as every other ingest
    job here."""
    raw_rows = conn.execute(
        sa.text("SELECT external_id, payload FROM raw_social_posts_instagram")
    ).all()

    posts_upserted = 0
    snapshots_upserted = 0
    for external_id, payload in raw_rows:
        (post_row,) = upsert_returning(
            conn,
            social_post_t,
            [
                {
                    "platform": "instagram",
                    "external_id": external_id,
                    "posted_at": datetime.fromisoformat(payload["timestamp"]),
                    "permalink": payload.get("permalink"),
                    "content_type": _map_content_type(payload),
                    "caption": payload.get("caption"),
                    "updated_at": sa.func.now(),
                }
            ],
            conflict_on=["platform", "external_id"],
            # A caption/permalink can be edited after posting; posted_at
            # never changes once set - not listed here, so a re-run can't
            # silently restate it.
            update=["permalink", "content_type", "caption", "updated_at"],
            returning=["id"],
        )
        posts_upserted += 1

        insights = payload.get("insights") or {}
        inserted = upsert_returning(
            conn,
            social_metrics_snapshot_t,
            [
                {
                    "social_post_id": post_row.id,
                    "captured_at": captured_at,
                    "impressions": insights.get("impressions", 0),
                    "reach": insights.get("reach", 0),
                    "likes": insights.get("likes", 0),
                    "comments": insights.get("comments", 0),
                    "shares": insights.get("shares", 0),
                    "saves": insights.get("saved", 0),  # Meta's metric name is "saved"
                    "clicks": insights.get("clicks", 0),
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


def run(settings: Settings, source: InstagramSourceSettings, *, dry_run: bool = False) -> RunResult:
    media = extract(source)

    if dry_run:
        print(f"{len(media)} post(s) at the source:")
        for item in media:
            print(f"  {item['id']}  {item.get('media_type')}  {item.get('timestamp')}")
        return RunResult(raw_landed=0, posts_upserted=0, snapshots_upserted=0)

    captured_at = datetime.now(UTC)
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        # Two phases, two transactions - "land raw first, transform second"
        # per ARCHITECTURE.md section 4.1, same as direct.py/events.py.
        with engine.begin() as conn:
            raw_landed = land_raw(conn, media)
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
        source_settings = load_instagram_source_settings()
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
