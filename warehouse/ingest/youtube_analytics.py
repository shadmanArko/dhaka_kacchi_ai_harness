"""Ingest YouTube ANALYTICS (watch time, subscribers, daily totals, traffic
sources) into the warehouse, via the YouTube Analytics API v2. See
ARCHITECTURE.md section 4.7 and migration 0032.

This is the owner-only half of YouTube. warehouse/ingest/youtube.py reads the
public numbers (views, likes, comments) with an API key; this reads what only
the channel owner can see, which needs OAuth - run `make youtube-auth` once to
mint a read-only token.

======================================================================
VERIFIED against the real Dhaka Kacchi channel (2026-10-07, Analytics API v2):
  - estimatedMinutesWatched, averageViewDuration, subscribersGained,
    subscribersLost, likes, comments, shares, views: ALL accepted together, by
    day, for the channel.
  - The same set by video AND day works ONLY with a `filters=video==a,b,c`
    list. Without it YouTube answers HTTP 400 "query not supported".
  - dimensions=day,insightTrafficSourceType works (SHORTS, YT_SEARCH,
    NO_LINK_OTHER, YT_OTHER_PAGE seen).
  - There is NO seconds metric: estimatedSecondsWatched is rejected as an
    unknown identifier. Watch time exists only in whole minutes, so the
    columns are watch_minutes and nothing here multiplies by 60.
  - DATA LAGS. A window ending yesterday returned rows only through three days
    before today; the newest days are ABSENT, not zero. This job therefore
    never writes a row for a day YouTube did not return, and a day that is
    missing today is simply filled in by a later run.
  - videoThumbnailImpressions / videoThumbnailImpressionsClickRate: HTTP 400 in
    every query shape tried, although the API recognises the names (a made-up
    metric gets a different error). Not requested here; see migration 0029's
    impression_ctr, which stays NULL.
Not verified: the Pacific-Time day boundary YouTube documents for these reports.
======================================================================

WHY DAILY TABLES AND NOT SNAPSHOTS: each day's figure is restated for about
three days after it first appears, and a snapshot is immutable. So this job
RE-PULLS A TRAILING WINDOW (default 14 days) every run and UPSERTS, replacing
the earlier figure; fetched_at records when each row was last refreshed. Only
the window this run asked for is ever transformed - older raw windows are
history, and re-applying them would overwrite a fresher figure with a stale one
(unlike youtube.py's snapshots, where re-reading old raw rows is harmless).

THE JOB FAILS LOUDLY, AND ONLY AFTER SAVING WHAT IT CAN: an expired token or a
disabled API raises ExtractionError (exit 3) with the fix in plain words. A
failure in one report does not discard the others already fetched - but nothing
is written unless ALL reports succeeded, so a half-fetched window never
masquerades as a complete one.

Run with `make ingest-youtube-analytics`; preview with
`make ingest-youtube-analytics-dry-run`; backfill with
`python -m warehouse.ingest.youtube_analytics --since 2026-09-01`.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import sqlalchemy as sa

from warehouse.config import (
    ConfigError,
    Settings,
    YouTubeOAuthSettings,
    load_settings,
    load_youtube_oauth_settings,
)
from warehouse.ingest.upsert import upsert_returning
from warehouse.ingest.youtube_auth import (
    REPORTS_URL,
    TOKEN_URL,
    AuthError,
    refresh_access_token,
    require_https_or_local,
)

PLATFORM = "youtube"
REQUEST_TIMEOUT_S = 30

DEFAULT_DAYS = 14

# videos per filters=video==... list. The API documents a higher ceiling; this
# keeps each URL short and each response small, and costs nothing at this scale.
VIDEO_CHUNK = 100

# A response this large may have been silently truncated by the API, and a
# truncated window is worse than a failed one. Fail loudly instead of guessing
# at the true limit, which is not verified here.
ROW_GUARD = 9000

# Insert in batches: Postgres caps one statement at 65,535 bound parameters, and
# a long backfill of many videos x many days x 12 columns would blow through it.
INSERT_BATCH = 2000

# API metric name -> warehouse column. Order is the order requested.
METRIC_COLUMNS = {
    "views": "views",
    "estimatedMinutesWatched": "watch_minutes",
    "averageViewDuration": "avg_view_seconds",
    "subscribersGained": "subscribers_gained",
    "subscribersLost": "subscribers_lost",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
}
TRAFFIC_COLUMNS = {"views": "views", "estimatedMinutesWatched": "watch_minutes"}

REPORT_CHANNEL = "channel_daily"
REPORT_VIDEO = "video_daily"
REPORT_TRAFFIC = "traffic_daily"


class ExtractionError(RuntimeError):
    """The source read failed in a way the operator must act on. Never caught."""


class WindowError(ValueError):
    """The requested date window is impossible. A distinct type so that
    main() reports ONLY this as a usage error, and a stray ValueError from
    parsing a response is never mislabelled as one."""


@dataclass(frozen=True, slots=True)
class Window:
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class RunResult:
    raw_landed: int
    account_rows: int
    post_rows: int
    traffic_rows: int


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def make_window(*, days: int, since: date | None, today: date) -> Window:
    """Ends YESTERDAY, not on the last day YouTube has data for: asking for
    more than exists is harmless (the API returns what it has), whereas guessing
    the lag would silently drop days if YouTube ever gets faster."""
    end = today - timedelta(days=1)
    start = since if since is not None else end - timedelta(days=days - 1)
    if start > end:
        raise WindowError(f"window start {start} is after its end {end}")
    return Window(start=start, end=end)


def _explain(status: int, body: bytes) -> str:
    try:
        message = json.loads(body).get("error", {}).get("message", "")
    except (ValueError, AttributeError):
        message = ""
    if status == 403 and ("has not been used" in message or "disabled" in message):
        return (
            "the YouTube Analytics API is not enabled in your Google Cloud project -> APIs & "
            "Services -> Library -> YouTube Analytics API -> Enable"
        )
    if status == 401:
        return "the token was rejected -> run `make youtube-auth`"
    if status == 403:
        return f"permission denied ({message[:120]}) -> run `make youtube-auth`"
    if status == 400:
        return (
            f"{message[:160]} -> this query shape was verified against the real channel on "
            "2026-10-07; if it now fails, YouTube changed it"
        )
    return message[:200] or f"HTTP {status}"


def _report(
    token: str,
    reports_url: str,
    window: Window,
    *,
    metrics: Sequence[str],
    dimensions: str,
    sort: str,
    filters: str | None = None,
) -> dict:
    params = {
        "ids": "channel==MINE",
        "startDate": window.start.isoformat(),
        "endDate": window.end.isoformat(),
        "metrics": ",".join(metrics),
        "dimensions": dimensions,
        "sort": sort,
    }
    if filters:
        params["filters"] = filters
    request = urllib.request.Request(
        f"{reports_url}?{urllib.parse.urlencode(params)}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ExtractionError(
            f"YouTube Analytics error {exc.code} ({dimensions}): {_explain(exc.code, exc.read())}"
        ) from None
    except urllib.error.URLError as exc:
        raise ExtractionError(f"YouTube Analytics unreachable: {exc.reason}") from None

    rows = body.get("rows") or []
    if len(rows) >= ROW_GUARD:
        raise ExtractionError(
            f"{len(rows)} rows returned for {dimensions}: possibly truncated. Narrow the window."
        )
    return {"columnHeaders": body.get("columnHeaders", []), "rows": rows}


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def extract(
    oauth: YouTubeOAuthSettings,
    window: Window,
    video_ids: Sequence[str],
    *,
    token_url: str = TOKEN_URL,
    reports_url: str = REPORTS_URL,
    out=print,
) -> dict[str, dict]:
    """Returns {report key: {"columnHeaders": [...], "rows": [...]}}. The video
    report is omitted when there are no videos to ask about - not an error, but
    said out loud, because a silent skip looks identical to a healthy run."""
    require_https_or_local(reports_url, "reports URL")
    try:
        token = refresh_access_token(oauth, token_url=token_url)
    except AuthError as exc:
        raise ExtractionError(str(exc)) from None

    reports: dict[str, dict] = {}
    reports[REPORT_CHANNEL] = _report(
        token,
        reports_url,
        window,
        metrics=list(METRIC_COLUMNS),
        dimensions="day",
        sort="day",
    )
    reports[REPORT_TRAFFIC] = _report(
        token,
        reports_url,
        window,
        metrics=list(TRAFFIC_COLUMNS),
        dimensions="day,insightTrafficSourceType",
        sort="day",
    )

    if not video_ids:
        out("no YouTube videos in the warehouse yet - skipping per-video analytics")
        return reports

    merged: dict = {"columnHeaders": [], "rows": []}
    for chunk in _chunks(list(video_ids), VIDEO_CHUNK):
        part = _report(
            token,
            reports_url,
            window,
            metrics=list(METRIC_COLUMNS),
            dimensions="video,day",
            sort="day",
            filters=f"video=={','.join(chunk)}",
        )
        merged["columnHeaders"] = merged["columnHeaders"] or part["columnHeaders"]
        merged["rows"].extend(part["rows"])
    reports[REPORT_VIDEO] = merged
    return reports


# ---------------------------------------------------------------------------
# Raw landing
# ---------------------------------------------------------------------------

raw_youtube_analytics = sa.table(
    "raw_youtube_analytics",
    sa.column("id"),
    sa.column("report"),
    sa.column("start_date"),
    sa.column("end_date"),
    sa.column("payload"),
    sa.column("updated_at"),
)


def land_raw(conn: sa.Connection, reports: dict[str, dict], window: Window) -> int:
    rows = [
        {
            "report": key,
            "start_date": window.start,
            "end_date": window.end,
            "payload": json.dumps(payload),
            "updated_at": sa.func.now(),
        }
        for key, payload in reports.items()
    ]
    result = upsert_returning(
        conn,
        raw_youtube_analytics,
        rows,
        conflict_on=["report", "start_date", "end_date"],
        update=["payload", "updated_at"],
        returning=["id"],
    )
    return len(result)


# ---------------------------------------------------------------------------
# Transform + load
# ---------------------------------------------------------------------------

_METRIC_TABLE_COLUMNS = list(METRIC_COLUMNS.values())

social_account_daily_t = sa.table(
    "social_account_daily",
    sa.column("platform"),
    sa.column("day"),
    *[sa.column(c) for c in _METRIC_TABLE_COLUMNS],
    sa.column("fetched_at"),
    sa.column("updated_at"),
)
social_post_daily_t = sa.table(
    "social_post_daily",
    sa.column("social_post_id"),
    sa.column("day"),
    *[sa.column(c) for c in _METRIC_TABLE_COLUMNS],
    sa.column("fetched_at"),
    sa.column("updated_at"),
)
social_traffic_source_daily_t = sa.table(
    "social_traffic_source_daily",
    sa.column("platform"),
    sa.column("day"),
    sa.column("source_type"),
    *[sa.column(c) for c in TRAFFIC_COLUMNS.values()],
    sa.column("fetched_at"),
    sa.column("updated_at"),
)


def _records(payload: dict) -> Iterator[dict]:
    """Each row as {api column name: value}. Keyed by the response's own
    columnHeaders rather than by position, so a reordered response cannot
    silently put minutes into the views column."""
    names = [h["name"] for h in payload.get("columnHeaders", [])]
    for row in payload.get("rows", []):
        if len(row) != len(names):
            raise ExtractionError(
                f"a report row has {len(row)} values for {len(names)} columns: "
                "YouTube changed the response shape"
            )
        yield dict(zip(names, row, strict=True))


def _metrics(record: dict, mapping: dict[str, str]) -> dict[str, int | None]:
    """A metric the response did not include stays None (NULL) - never 0. The
    API sends numbers for everything it was asked for, so in practice this only
    matters if YouTube drops a metric from a report."""
    out: dict[str, int | None] = {}
    for api_name, column in mapping.items():
        value = record.get(api_name)
        out[column] = int(value) if isinstance(value, (int, float)) else None
    return out


def _batched(rows: list[dict], size: int) -> Iterator[list[dict]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _load(
    conn: sa.Connection,
    table: sa.TableClause,
    rows: list[dict],
    *,
    conflict_on: list[str],
    update: list[str],
) -> int:
    total = 0
    for batch in _batched(rows, INSERT_BATCH):
        total += len(
            upsert_returning(
                conn, table, batch, conflict_on=conflict_on, update=update, returning=["day"]
            )
        )
    return total


def transform_and_load(
    conn: sa.Connection, window: Window, *, fetched_at: datetime
) -> tuple[int, int, int]:
    """Reads back THIS window's raw rows - so the typed tables are derived from
    what was landed, and only from this window (see the module docstring)."""
    payloads = {
        report: payload
        for report, payload in conn.execute(
            sa.text(
                "SELECT report, payload FROM raw_youtube_analytics "
                "WHERE start_date = :s AND end_date = :e"
            ),
            {"s": window.start, "e": window.end},
        ).all()
    }
    stamp = {"fetched_at": fetched_at, "updated_at": sa.func.now()}

    account_rows = [
        {
            "platform": PLATFORM,
            "day": date.fromisoformat(rec["day"]),
            **_metrics(rec, METRIC_COLUMNS),
            **stamp,
        }
        for rec in _records(payloads.get(REPORT_CHANNEL, {}))
    ]
    n_account = _load(
        conn,
        social_account_daily_t,
        account_rows,
        conflict_on=["platform", "day"],
        update=[*_METRIC_TABLE_COLUMNS, "fetched_at", "updated_at"],
    )

    traffic_rows = [
        {
            "platform": PLATFORM,
            "day": date.fromisoformat(rec["day"]),
            "source_type": rec["insightTrafficSourceType"],
            **_metrics(rec, TRAFFIC_COLUMNS),
            **stamp,
        }
        for rec in _records(payloads.get(REPORT_TRAFFIC, {}))
    ]
    n_traffic = _load(
        conn,
        social_traffic_source_daily_t,
        traffic_rows,
        conflict_on=["platform", "day", "source_type"],
        update=[*TRAFFIC_COLUMNS.values(), "fetched_at", "updated_at"],
    )

    video_records = list(_records(payloads.get(REPORT_VIDEO, {})))
    ids = {
        external_id: post_id
        for external_id, post_id in conn.execute(
            sa.text(
                "SELECT external_id, id FROM social_post "
                "WHERE platform = :p AND external_id = ANY(:ids)"
            ),
            {"p": PLATFORM, "ids": sorted({rec["video"] for rec in video_records})},
        ).all()
    }
    # A video in the response that is not in social_post (deleted from the
    # warehouse, or listed between two runs) is skipped, not an error: the next
    # youtube.py run creates the post and the next run of this job fills it in.
    post_rows = [
        {
            "social_post_id": ids[rec["video"]],
            "day": date.fromisoformat(rec["day"]),
            **_metrics(rec, METRIC_COLUMNS),
            **stamp,
        }
        for rec in video_records
        if rec["video"] in ids
    ]
    n_post = _load(
        conn,
        social_post_daily_t,
        post_rows,
        conflict_on=["social_post_id", "day"],
        update=[*_METRIC_TABLE_COLUMNS, "fetched_at", "updated_at"],
    )
    return n_account, n_post, n_traffic


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _video_ids(conn: sa.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            sa.text("SELECT external_id FROM social_post WHERE platform = :p ORDER BY external_id"),
            {"p": PLATFORM},
        )
    ]


def run(
    settings: Settings,
    oauth: YouTubeOAuthSettings,
    *,
    dry_run: bool = False,
    days: int = DEFAULT_DAYS,
    since: date | None = None,
    today: date | None = None,
    token_url: str = TOKEN_URL,
    reports_url: str = REPORTS_URL,
    out=print,
) -> RunResult:
    window = make_window(days=days, since=since, today=today or datetime.now(UTC).date())

    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            video_ids = _video_ids(conn)

        reports = extract(
            oauth, window, video_ids, token_url=token_url, reports_url=reports_url, out=out
        )

        if dry_run:
            out(f"window {window.start} .. {window.end}  ({len(video_ids)} video(s) known)")
            for key, payload in reports.items():
                days = [rec["day"] for rec in _records(payload)]
                newest = max(days, default=None)
                out(f"  {key:<14} {len(days):>5} row(s), newest day returned: {newest}")
            return RunResult(0, 0, 0, 0)

        fetched_at = datetime.now(UTC)
        with engine.begin() as conn:
            raw_landed = land_raw(conn, reports, window)
        with engine.begin() as conn:
            n_account, n_post, n_traffic = transform_and_load(conn, window, fetched_at=fetched_at)
    finally:
        engine.dispose()

    return RunResult(
        raw_landed=raw_landed, account_rows=n_account, post_rows=n_post, traffic_rows=n_traffic
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="fetch and summarise; write nothing")
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"trailing window in days (default {DEFAULT_DAYS}); recent days are restated",
    )
    parser.add_argument(
        "--since", type=date.fromisoformat, help="backfill from YYYY-MM-DD instead of --days"
    )
    args = parser.parse_args(argv)
    if args.days < 1:
        print("config error: --days must be at least 1", file=sys.stderr)
        return 2

    try:
        settings = load_settings()
        oauth = load_youtube_oauth_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        result = run(settings, oauth, dry_run=args.dry_run, days=args.days, since=args.since)
    except WindowError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except ExtractionError as exc:
        print(f"extraction error: {exc}", file=sys.stderr)
        return 3

    if not args.dry_run:
        print(
            f"landed {result.raw_landed} raw report(s); upserted {result.account_rows} channel "
            f"day(s), {result.post_rows} video-day(s), {result.traffic_rows} traffic-source row(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
