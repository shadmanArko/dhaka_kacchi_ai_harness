"""Ingest YouTube video data into the warehouse, via the YouTube Data API v3.
See ARCHITECTURE.md section 4.7.

======================================================================
STATUS - READ THIS FIRST. This is STAGE 1 and it has NOT yet been run against
the real Dhaka Kacchi channel. Unlike instagram.py/facebook.py/threads.py,
which carry a "VERIFIED against the real account" block, nothing below is
verified against live YouTube yet: it was exercised only against a local fake
server (warehouse/ingest/youtube_verify.py) that implements the documented
response shapes. The first real run is the verification, and these are the
things it must confirm, in priority order:

  1. SHORT vs VIDEO classification (_map_content_type). The Data API has no
     "is this a Short" field. The rule used - duration <= 60s, or <= 180s with
     "#shorts" in the title/description - is a heuristic. Compare the result
     against YouTube Studio's Shorts tab; the raw payload is kept, so a better
     rule can be applied by re-running the transform with no new API call.
  2. Whether likeCount/commentCount are present. The API omits likeCount when
     the owner hides likes and commentCount when comments are off; both are
     stored as NULL ("not reported"), never 0.
  3. That the uploads playlist returns every public video. Private, unlisted
     and deleted videos are invisible to an API key by design.

WHAT THIS STAGE DOES NOT DO, deliberately:
  - No watch time, average view duration, subscribers gained, impressions or
    click-through rate. Those exist only in the YouTube ANALYTICS API, which
    needs OAuth as the channel owner - a separate stage. The columns for them
    (views excepted) exist since migration 0029 and are written as NULL here.
  - impressions, reach, shares, saves and clicks are NULL, not 0: the Data API
    does not return them. (Migration 0030 made that possible; before it, this
    job would have had to invent five zeros per video.)
  - "views" goes in social_metrics_snapshot.views, NOT .impressions. Threads
    maps its views into impressions; YouTube does not, because a view and an
    impression are different things on this platform and 0029 added a column
    precisely so they would not be conflated.
======================================================================

SETUP (do this before the first real run):
  1. Google Cloud Console -> create or pick a project.
  2. APIs & Services -> Library -> enable "YouTube Data API v3".
  3. APIs & Services -> Credentials -> Create credentials -> API key.
     Restrict it (Edit API key -> API restrictions -> "YouTube Data API v3"
     only), so a leaked key can do nothing else.
  4. YOUTUBE_CHANNEL_ID: the 24-character id starting "UC" - NOT the @handle.
     YouTube Studio -> Settings -> Channel -> Advanced settings.
  5. Set YOUTUBE_API_KEY and YOUTUBE_CHANNEL_ID - see .env.example.

QUOTA: the default is 10,000 units/day. A run costs 1 (channels.list) plus 1
per 50 videos for playlistItems.list plus 1 per 50 for videos.list - about 5
units for a 100-video channel. Quota is a non-issue at this scale.

Same "land raw, then transform" shape as instagram.py/facebook.py/threads.py:
raw_social_posts_youtube first, social_post + social_metrics_snapshot second.
Run with `make ingest-youtube`; preview with `make ingest-youtube-dry-run`.

TWO DELIBERATE DIFFERENCES FROM threads.py, both fixes for weaknesses that file
shares with instagram.py/facebook.py:

  - The API key is REDACTED from every error message. threads.py puts the full
    request URL, access token included, into its exception text, which lands in
    cron logs. Here the key never appears in an error.
  - transform_and_load only snapshots the videos fetched in THIS run, not every
    row ever landed in the raw table. Raw rows are never deleted, so a video
    removed from the channel would otherwise be re-snapshotted every day from
    its stale payload with a fresh captured_at - fabricating a current
    reading for something that no longer exists.
"""

from __future__ import annotations

import json
import re
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
    YouTubeSourceSettings,
    load_settings,
    load_youtube_source_settings,
)
from warehouse.ingest.upsert import upsert_returning

REQUEST_TIMEOUT_S = 30

# videos.list accepts at most 50 ids per call.
BATCH_SIZE = 50

# playlistItems.list maximum page size.
PAGE_SIZE = 50

# See _map_content_type and the STATUS block above: heuristics, unverified.
SHORT_MAX_SECONDS = 60
SHORT_TAGGED_MAX_SECONDS = 180


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

# Plain-language fixes for the failures an owner will actually hit. The API's
# own messages are accurate but written for developers; this is what to DO.
_HINTS = {
    "quotaExceeded": "The daily quota is used up; it resets at midnight Pacific time.",
    "keyInvalid": "The API key is wrong. Re-copy YOUTUBE_API_KEY from Cloud Console.",
    "accessNotConfigured": (
        "The YouTube Data API v3 is not enabled for this key's project. "
        "Cloud Console -> APIs & Services -> Library -> YouTube Data API v3 -> Enable."
    ),
    "ipRefererBlocked": (
        "The key has an IP or referrer restriction that excludes this machine. "
        "Restrict by API (YouTube Data API v3) instead of by address."
    ),
}


def _explain(body: str) -> str:
    """Turn a YouTube error body into 'reason: message. what to do'. Falls back
    to the raw body, truncated, if it is not the documented JSON shape."""
    try:
        err = json.loads(body)["error"]
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        text = f"{reason or err.get('status', 'error')}: {err.get('message', '').strip()}"
        hint = _HINTS.get(reason)
        return f"{text} -> {hint}" if hint else text
    except (ValueError, KeyError, TypeError, IndexError, AttributeError):
        return body[:300]


def _api_get(source: YouTubeSourceSettings, resource: str, params: dict[str, str]) -> dict:
    query = urllib.parse.urlencode({**params, "key": source.api_key})
    url = f"{source.api_base}/{resource}?{query}"
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        # Redacted on the way out: the key is in the URL, and a quota-exceeded
        # body can echo request details. It must never reach a cron log.
        message = f"YouTube API error {exc.code} on {resource}: {_explain(body)}"
        raise ExtractionError(message.replace(source.api_key, "***")) from None
    except urllib.error.URLError as exc:
        message = f"YouTube API unreachable ({resource}): {exc.reason}"
        raise ExtractionError(message.replace(source.api_key, "***")) from None


def _uploads_playlist_id(source: YouTubeSourceSettings) -> str:
    data = _api_get(source, "channels", {"part": "contentDetails", "id": source.channel_id})
    items = data.get("items") or []
    if not items:
        raise ExtractionError(
            f"YouTube returned no channel for id {source.channel_id}. Check "
            "YOUTUBE_CHANNEL_ID is the UC... id of the channel itself."
        )
    try:
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except KeyError as exc:
        raise ExtractionError(f"channel response had no uploads playlist: {exc}") from None


def _video_ids(source: YouTubeSourceSettings, playlist_id: str) -> list[str]:
    """Every video id in the channel's uploads playlist, oldest-listed last.

    Paginates via nextPageToken. Order-preserving de-duplication: a video can
    in principle appear twice if it is edited mid-pagination, and a duplicate id
    would make videos.list return it once but count it twice downstream."""
    ids: list[str] = []
    seen: set[str] = set()
    token: str | None = None
    while True:
        params = {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": str(PAGE_SIZE),
        }
        if token:
            params["pageToken"] = token
        data = _api_get(source, "playlistItems", params)
        for item in data.get("items", []):
            vid = item.get("contentDetails", {}).get("videoId")
            if vid and vid not in seen:
                seen.add(vid)
                ids.append(vid)
        token = data.get("nextPageToken")
        if not token:
            return ids


def _videos(source: YouTubeSourceSettings, ids: list[str]) -> list[dict]:
    videos: list[dict] = []
    for start in range(0, len(ids), BATCH_SIZE):
        batch = ids[start : start + BATCH_SIZE]
        data = _api_get(
            source,
            "videos",
            {"part": "snippet,contentDetails,statistics", "id": ",".join(batch)},
        )
        videos.extend(data.get("items", []))
    return videos


def extract(source: YouTubeSourceSettings) -> tuple[list[dict], int]:
    """Returns (videos, ids_listed). The two differ when the uploads playlist
    names videos that videos.list will not return - private or deleted ones,
    which an API key cannot see. Surfaced rather than hidden, so a count that
    looks short is explainable."""
    ids = _video_ids(source, _uploads_playlist_id(source))
    return _videos(source, ids), len(ids)


# ---------------------------------------------------------------------------
# Raw landing
# ---------------------------------------------------------------------------

raw_social_posts_youtube = sa.table(
    "raw_social_posts_youtube",
    sa.column("id"),
    sa.column("external_id"),
    sa.column("payload"),
    sa.column("updated_at"),
)


def land_raw(conn: sa.Connection, videos: list[dict]) -> int:
    rows = [
        {
            "external_id": video["id"],
            "payload": json.dumps(video),
            "updated_at": sa.func.now(),
        }
        for video in videos
    ]
    result = upsert_returning(
        conn,
        raw_social_posts_youtube,
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
    sa.column("views"),
    sa.column("watch_seconds"),
    sa.column("subscribers_gained"),
    sa.column("impression_ctr"),
)

# ISO 8601 duration as YouTube emits it: PT45S, PT1M5S, PT1H2M3S, and P0D for a
# live stream that has not produced a duration. A day component is accepted
# for completeness (24h+ streams exist).
_DURATION_RE = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def _duration_seconds(iso: str | None) -> int | None:
    if not iso:
        return None
    match = _DURATION_RE.match(iso)
    if not match:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _to_int(value: object) -> int | None:
    """statistics values arrive as strings, and are ABSENT (not "0") when the
    owner hides them or switches comments off. Absent must stay NULL: 0 would
    claim a measurement that was never made."""
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _map_content_type(payload: dict) -> str | None:
    """Maps a video to this warehouse's closed social_post.content_type
    vocabulary: 'short' or 'video'. UNVERIFIED against the real channel - see
    the STATUS block.

    The Data API has no is-a-Short field, so this is a heuristic:
      - duration <= 60s                              -> 'short'
      - duration <= 180s AND "#shorts" in the text   -> 'short'
        (Shorts have been allowed up to 3 minutes since October 2024, so the
        60s rule alone would mislabel those; the hashtag is a corroborating
        signal, not proof)
      - anything else                                -> 'video'
    Known gap: an untagged Short between 61 and 180 seconds is labelled
    'video'. That errs toward the plain label rather than inventing a format
    claim, and is recomputable from the raw payload.

    Live and upcoming streams are 'video' regardless of duration (a stream with
    no duration yet reads as P0D, i.e. 0 seconds, which is not a Short).
    Unparseable or missing duration gives NULL: unknown, not 'video'."""
    snippet = payload.get("snippet") or {}
    if snippet.get("liveBroadcastContent") in ("live", "upcoming"):
        return "video"

    seconds = _duration_seconds((payload.get("contentDetails") or {}).get("duration"))
    if seconds is None:
        return None
    if seconds <= SHORT_MAX_SECONDS:
        return "short"
    text = f"{snippet.get('title', '')} {snippet.get('description', '')}".lower()
    if seconds <= SHORT_TAGGED_MAX_SECONDS and "#shorts" in text:
        return "short"
    return "video"


def _permalink(video_id: str, content_type: str | None) -> str:
    if content_type == "short":
        return f"https://www.youtube.com/shorts/{video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


def _caption(snippet: dict) -> str | None:
    """Title, then description if there is one. Other platforms' `caption` is
    the post text; on YouTube the title is the short text people actually
    read, and the description is the long form. Both are kept (the raw payload
    holds them separately) rather than choosing one."""
    title = (snippet.get("title") or "").strip()
    description = (snippet.get("description") or "").strip()
    combined = f"{title}\n\n{description}" if description else title
    return combined or None


def _published_at(snippet: dict) -> datetime:
    # YouTube emits "2026-09-22T17:20:56Z"; normalised rather than trusting
    # every Python version's fromisoformat to accept a trailing Z.
    return datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00"))


def transform_and_load(
    conn: sa.Connection, *, captured_at: datetime, external_ids: list[str]
) -> tuple[int, int]:
    """captured_at is shared across the whole run, same idempotency shape as
    the other ingesters. Restricted to `external_ids` - see the module
    docstring for why this is not simply "every raw row"."""
    if not external_ids:
        return 0, 0

    raw_rows = conn.execute(
        sa.text(
            "SELECT external_id, payload FROM raw_social_posts_youtube "
            "WHERE external_id = ANY(:ids)"
        ),
        {"ids": external_ids},
    ).all()

    posts_upserted = 0
    snapshots_upserted = 0
    for external_id, payload in raw_rows:
        snippet = payload.get("snippet") or {}
        stats = payload.get("statistics") or {}
        content_type = _map_content_type(payload)

        (post_row,) = upsert_returning(
            conn,
            social_post_t,
            [
                {
                    "platform": "youtube",
                    "external_id": external_id,
                    "posted_at": _published_at(snippet),
                    "permalink": _permalink(external_id, content_type),
                    "content_type": content_type,
                    "caption": _caption(snippet),
                    "updated_at": sa.func.now(),
                }
            ],
            conflict_on=["platform", "external_id"],
            update=["permalink", "content_type", "caption", "updated_at"],
            returning=["id"],
        )
        posts_upserted += 1

        inserted = upsert_returning(
            conn,
            social_metrics_snapshot_t,
            [
                {
                    "social_post_id": post_row.id,
                    "captured_at": captured_at,
                    "views": _to_int(stats.get("viewCount")),
                    "likes": _to_int(stats.get("likeCount")),
                    "comments": _to_int(stats.get("commentCount")),
                    # Not returned by the Data API - NULL, never 0. impressions
                    # and clicks come from the Analytics API (a later stage);
                    # reach/shares/saves have no YouTube equivalent here at all.
                    "impressions": None,
                    "reach": None,
                    "shares": None,
                    "saves": None,
                    "clicks": None,
                    # Analytics API only - a later stage.
                    "watch_seconds": None,
                    "subscribers_gained": None,
                    "impression_ctr": None,
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


def run(settings: Settings, source: YouTubeSourceSettings, *, dry_run: bool = False) -> RunResult:
    videos, ids_listed = extract(source)

    if dry_run:
        print(
            f"{len(videos)} video(s) returned, {ids_listed} listed in the uploads playlist"
            + (
                f" ({ids_listed - len(videos)} not visible to an API key: private or deleted)"
                if ids_listed != len(videos)
                else ""
            )
        )
        for video in videos:
            stats = video.get("statistics") or {}
            print(
                f"  {video['id']}  {_map_content_type(video) or '?':<6}  "
                f"{(video.get('contentDetails') or {}).get('duration', '?'):<10}  "
                f"views={stats.get('viewCount', '-'):<7} likes={stats.get('likeCount', '-'):<5}  "
                f"{(video.get('snippet') or {}).get('title', '')[:50]}"
            )
        return RunResult(raw_landed=0, posts_upserted=0, snapshots_upserted=0)

    captured_at = datetime.now(UTC)
    external_ids = [video["id"] for video in videos]
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:
            raw_landed = land_raw(conn, videos)
        with engine.begin() as conn:
            posts_upserted, snapshots_upserted = transform_and_load(
                conn, captured_at=captured_at, external_ids=external_ids
            )
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
        source_settings = load_youtube_source_settings()
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
