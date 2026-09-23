"""Ingest organic Threads post data into the warehouse, via the Threads API
(graph.threads.net - a genuinely separate API from Instagram/Facebook's
graph.facebook.com, despite all three being Meta products). See
ARCHITECTURE.md section 4.7.

======================================================================
VERIFIED against the real Dhaka Kacchi Threads account (2026-09-23, API
version v1.0, the only version this API currently has):
  - GET /{threads_user_id}/threads with MEDIA_FIELDS below returns real
    posts correctly, paginated via paging.next. Every post seen so far has
    media_type "VIDEO" - TEXT_POST/IMAGE/CAROUSEL_ALBUM/AUDIO/REPOST_FACADE
    are documented possible values but unverified against a real post.
  - GET /{media_id}/insights?metric=views,likes,replies,reposts,quotes,shares:
    ALL SIX metrics returned successfully in one call, including "views" and
    "shares", which Meta's own docs mark "in development" - unlike Facebook's
    read_insights gap, there was no missing-permission wall here. `replies`
    is returned under a differently-named field id (thread_replies) but the
    same `name: "replies"` in the JSON body - only `name` matters for parsing.
  - No reach, saves, or clicks equivalent exists in the Threads API at all -
    hardcoded to 0 below, same documented-gap pattern as facebook.py's
    reach/impressions/saves gaps. `reposts`/`quotes` are real, working
    metrics but have no column in social_metrics_snapshot - untracked, a
    real gap, not an oversight.
======================================================================

SETUP (do this before the first real run):
  1. Same Meta Developer App as instagram.py/facebook.py. The Threads
     product must be added (My Apps -> your app -> App settings -> Basic ->
     "Threads App ID"/"Threads App secret" section - if absent, add the
     Threads product first).
  2. Add the "Access the Threads API" use case (Use cases -> Add use cases).
  3. Add the Threads account as a Threads Tester (App roles -> Roles ->
     Add People -> role "Threads Tester" -> enter the Threads @username),
     then accept the invite from that Threads account itself: Threads app/
     threads.net -> Settings -> More settings -> Website permissions ->
     Invites tab -> accept.
  4. Under the "Access the Threads API" use case's Settings tab, the
     "User Token Generator" section lists every Threads Tester with a
     "Generate Access Token" button - click it to get a long-lived token
     directly, no OAuth redirect-URI dance needed for your own tester
     account. Note: unlike a Facebook Page token, this is NOT non-expiring
     - Threads long-lived tokens are documented as 60-day validity,
     refreshable via GET https://graph.threads.net/refresh_access_token.
     Not yet automated here - an operational task to revisit before the
     first token actually expires.
  5. Set THREADS_ACCESS_TOKEN and THREADS_USER_ID (the numeric Threads user
     id, found via GET /v1.0/me?fields=id,username&access_token=... - NOT
     the @username) - see .env.example.

Same "land raw, then transform" shape as instagram.py/facebook.py:
raw_social_posts_threads first, social_post + social_metrics_snapshot
second. Run with `make ingest-threads`; preview with
`make ingest-threads-dry-run`.
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
    Settings,
    ThreadsSourceSettings,
    load_settings,
    load_threads_source_settings,
)
from warehouse.ingest.upsert import upsert_returning

THREADS_API_VERSION = "v1.0"
THREADS_API_BASE = f"https://graph.threads.net/{THREADS_API_VERSION}"

# Verified against real posts - see module docstring.
MEDIA_FIELDS = "id,media_type,text,timestamp,permalink"

# Verified against a real post - see module docstring. All six work in one
# call, including the two Meta marks "in development" (views, shares).
INSIGHTS_METRICS = "views,likes,replies,reposts,quotes,shares"

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
        raise ExtractionError(f"Threads API error {exc.code} for {url}: {body}") from exc
    except urllib.error.URLError as exc:
        raise ExtractionError(f"Threads API unreachable: {exc}") from exc


def _fetch_media(source: ThreadsSourceSettings) -> list[dict]:
    """Paginates via the "paging.next" link Threads returns, same reasoning
    as instagram.py's _fetch_media: that cursor token is opaque and not
    meant to be built by the caller."""
    params = urllib.parse.urlencode(
        {"fields": MEDIA_FIELDS, "access_token": source.access_token, "limit": "25"}
    )
    url: str | None = f"{THREADS_API_BASE}/{source.user_id}/threads?{params}"
    media: list[dict] = []
    while url:
        data = _graph_get(url)
        media.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
    return media


def _fetch_insights(source: ThreadsSourceSettings, media_id: str) -> dict[str, object]:
    params = urllib.parse.urlencode(
        {"metric": INSIGHTS_METRICS, "access_token": source.access_token}
    )
    url = f"{THREADS_API_BASE}/{media_id}/insights?{params}"
    try:
        data = _graph_get(url)
    except ExtractionError:
        # Same reasoning as instagram.py/facebook.py: one post's metric-
        # availability quirk (e.g. REPOST_FACADE posts return an empty
        # array per Meta's docs) shouldn't abort the entire run.
        return {}
    return {item["name"]: item["values"][0]["value"] for item in data.get("data", [])}


def extract(source: ThreadsSourceSettings) -> list[dict]:
    """Full extract every run, same reasoning as every other ingest job
    here: no incremental cursor, correctness comes from the upsert layer."""
    media = _fetch_media(source)
    for item in media:
        item["insights"] = _fetch_insights(source, item["id"])
    return media


# ---------------------------------------------------------------------------
# Raw landing
# ---------------------------------------------------------------------------

raw_social_posts_threads = sa.table(
    "raw_social_posts_threads",
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
        raw_social_posts_threads,
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
    """Maps Threads' media_type to this warehouse's closed
    social_post.content_type vocabulary. VIDEO->"video" verified against
    real posts 2026-09-23 (see module docstring). IMAGE and CAROUSEL_ALBUM
    map by the same naming convention as instagram.py's mapping, but are
    unverified - no such Threads post existed to test against. TEXT_POST
    and AUDIO have no matching value in this warehouse's content_type CHECK
    (image/video/carousel/reel/story) - a real, accepted gap, same
    reasoning as facebook.py's reel/story gap: falls through to NULL rather
    than widening the CHECK for a media type that may never actually occur
    for this account."""
    media_type = payload.get("media_type")
    if media_type == "VIDEO":
        return "video"
    if media_type == "IMAGE":
        return "image"
    if media_type == "CAROUSEL_ALBUM":
        return "carousel"
    return None


def transform_and_load(conn: sa.Connection, *, captured_at: datetime) -> tuple[int, int]:
    """captured_at shared across the whole run, same idempotency shape as
    instagram.py's/facebook.py's transform_and_load."""
    raw_rows = conn.execute(
        sa.text("SELECT external_id, payload FROM raw_social_posts_threads")
    ).all()

    posts_upserted = 0
    snapshots_upserted = 0
    for external_id, payload in raw_rows:
        (post_row,) = upsert_returning(
            conn,
            social_post_t,
            [
                {
                    "platform": "threads",
                    "external_id": external_id,
                    "posted_at": datetime.fromisoformat(payload["timestamp"]),
                    "permalink": payload.get("permalink"),
                    "content_type": _map_content_type(payload),
                    "caption": payload.get("text"),
                    "updated_at": sa.func.now(),
                }
            ],
            conflict_on=["platform", "external_id"],
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
                    # "views" is the closest Threads analog to impressions -
                    # Meta itself labels it "in development" (see module
                    # docstring), so treat this column as lower-confidence
                    # than the same column for Instagram/Facebook.
                    "impressions": insights.get("views", 0),
                    "reach": 0,  # no Threads equivalent exists
                    "likes": insights.get("likes", 0),
                    "comments": insights.get("replies", 0),
                    "shares": insights.get("shares", 0),
                    "saves": 0,  # no Threads equivalent exists
                    "clicks": 0,  # no post-level click metric on Threads
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


def run(settings: Settings, source: ThreadsSourceSettings, *, dry_run: bool = False) -> RunResult:
    media = extract(source)

    if dry_run:
        print(f"{len(media)} post(s) at the source:")
        for item in media:
            print(f"  {item['id']}  {item.get('media_type')}  {item.get('timestamp')}")
        return RunResult(raw_landed=0, posts_upserted=0, snapshots_upserted=0)

    captured_at = datetime.now(UTC)
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
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
        source_settings = load_threads_source_settings()
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
