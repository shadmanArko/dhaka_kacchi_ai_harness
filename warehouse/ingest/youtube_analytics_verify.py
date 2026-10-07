"""Prove warehouse.ingest.youtube_analytics behaves correctly, WITHOUT a YouTube
account.

Same idiom as the other *_verify.py scripts: plain ok/FAIL output and an exit
code. Like youtube_verify.py it builds and drops its OWN throwaway database, so
the fake channel it serves never lands in your dev data.

The fake Analytics API reproduces what the REAL one was observed to do on
2026-10-07 (see youtube_analytics.py's VERIFIED block), because a fake that is
friendlier than the real thing proves nothing:
  - rows are sparse, and the newest THREE days are absent, not zero;
  - the per-video report is refused without a `filters=video==...` list;
  - the response's column order follows the request, and a test also reverses
    it, because the ingester must map by name and never by position.

IT DOES NOT PROVE the code is right about the REAL YouTube - only about those
observed shapes. Run `make ingest-youtube-analytics-dry-run` against the real
channel to see the real thing.

Run with `make verify-ingest-youtube-analytics`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.parse
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sqlalchemy as sa

from warehouse import bootstrap_db
from warehouse.config import REPO_ROOT, ConfigError, YouTubeOAuthSettings, load_settings
from warehouse.ingest import youtube_analytics as ya
from warehouse.ingest.youtube_analytics import ExtractionError, WindowError

CLIENT_ID = "fake-client.apps.googleusercontent.com"
CLIENT_SECRET = "FAKE-CLIENT-SECRET-DO-NOT-LEAK"
REFRESH = "1//FAKE-REFRESH-TOKEN-DO-NOT-LEAK"
ACCESS = "FAKE-ACCESS-TOKEN"
OAUTH = YouTubeOAuthSettings(CLIENT_ID, CLIENT_SECRET, REFRESH)

TODAY = date(2026, 10, 7)
LAG_DAYS = 3  # YouTube's observed lag: today minus this is the newest day with data
LAST_DAY = TODAY - timedelta(days=LAG_DAYS)
CHANNEL_FIRST_DAY = date(2026, 9, 20)

_failures: list[str] = []
_checks_run = 0


def _check(name: str, problems: list[str]) -> None:
    global _checks_run
    _checks_run += 1
    if problems:
        _failures.append(name)
        print(f"  FAIL  {name}")
        for p in problems:
            print(f"          {p}")
    else:
        print(f"  ok    {name}")


# ---------------------------------------------------------------------------
# The fake Google
# ---------------------------------------------------------------------------

# What each API metric MUST land in. Written out by hand on purpose: deriving it
# from the ingester's own mapping would make the check circular - a wrong mapping
# in the code would define its own expectation and pass.
EXPECTED_COLUMNS = {
    "views": "views",
    "estimatedMinutesWatched": "watch_minutes",
    "averageViewDuration": "avg_view_seconds",
    "subscribersGained": "subscribers_gained",
    "subscribersLost": "subscribers_lost",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
}
EXPECTED_TRAFFIC_COLUMNS = {"views": "views", "estimatedMinutesWatched": "watch_minutes"}
METRIC_ORDER = list(EXPECTED_COLUMNS)  # the API names, also written independently of the code


def video_ids(n: int) -> list[str]:
    # Ids that start with '-' exist on the real channel, so the fake has them too.
    return [("-" if i % 7 == 0 else "") + f"vid{i:03d}" for i in range(n)]


def first_day_of(video: str) -> date:
    return CHANNEL_FIRST_DAY + timedelta(days=sum(map(ord, video)) % 6)


def value(key: str, day: date, metric: str) -> int:
    """Deterministic, never zero, so a missing metric cannot hide behind a 0."""
    k = METRIC_ORDER.index(metric) if metric in METRIC_ORDER else 0
    base = (day.toordinal() * 31 + k * 7 + sum(map(ord, key))) % 50 + 1
    return base + _Fake.bumps.get((key, day, metric), 0)


class _Fake:
    mode = "ok"  # see _Handler for the modes
    bumps: dict[tuple[str, date, str], int] = {}
    video_chunks: list[list[str]] = []
    calls: list[str] = []
    ghost = False  # add rows for a video that was never requested


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    # --- the token endpoint -------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
        if _Fake.mode == "invalid_grant":
            # Echoes the secret AND the refresh token, like a careless server could.
            detail = f"bad token {form.get('refresh_token')} for {form.get('client_secret')}"
            return self._send(400, {"error": "invalid_grant", "error_description": detail})
        if form.get("refresh_token") != REFRESH or form.get("client_secret") != CLIENT_SECRET:
            return self._send(401, {"error": "invalid_client"})
        self._send(200, {"access_token": ACCESS, "token_type": "Bearer"})

    # --- the reports endpoint ------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("Authorization") != f"Bearer {ACCESS}":
            return self._send(401, {"error": {"code": 401, "message": "bad token"}})
        q = {
            k: v[0]
            for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()
        }
        dims, metrics = q["dimensions"], q["metrics"].split(",")
        start, end = date.fromisoformat(q["startDate"]), date.fromisoformat(q["endDate"])
        _Fake.calls.append(dims)

        if _Fake.mode == "not_enabled":
            msg = "YouTube Analytics API has not been used in project 1 before or it is disabled."
            return self._send(403, {"error": {"code": 403, "message": msg}})
        if _Fake.mode == "video_500" and dims == "video,day":
            return self._send(500, {"error": {"code": 500, "message": "backend error"}})

        newest = min(end, LAST_DAY)  # the lag: later days are simply absent
        days = [
            start + timedelta(days=i)
            for i in range((newest - start).days + 1)
            if start + timedelta(days=i) >= CHANNEL_FIRST_DAY
        ]
        shown = list(reversed(metrics)) if _Fake.mode == "reversed_columns" else metrics

        def headers(dim_names: list[str]) -> list[dict]:
            return [{"name": n} for n in [*dim_names, *shown]]

        if dims == "day":
            if _Fake.mode == "huge":
                rows = [["2026-01-01", *([1] * len(shown))] for _ in range(ya.ROW_GUARD)]
                return self._send(200, {"columnHeaders": headers(["day"]), "rows": rows})
            rows = [[d.isoformat(), *[value("channel", d, m) for m in shown]] for d in days]
            if _Fake.mode == "ragged" and rows:
                rows[0] = rows[0][:-1]
            return self._send(200, {"columnHeaders": headers(["day"]), "rows": rows})

        if dims == "day,insightTrafficSourceType":
            rows = [
                [d.isoformat(), src, *[value(f"traffic:{src}", d, m) for m in shown]]
                for d in days
                for src in ("SHORTS", "YT_SEARCH")
            ]
            cols = headers(["day", "insightTrafficSourceType"])
            return self._send(200, {"columnHeaders": cols, "rows": rows})

        if dims == "video,day":
            filt = q.get("filters", "")
            if not filt.startswith("video=="):
                # Exactly what the real API did without a filter list.
                msg = "The query is not supported. Check the documentation."
                return self._send(400, {"error": {"code": 400, "message": msg}})
            ids = filt[len("video==") :].split(",")
            if len(ids) > 500:
                return self._send(400, {"error": {"code": 400, "message": "too many ids"}})
            _Fake.video_chunks.append(ids)
            wanted = [*ids, "ghost000"] if _Fake.ghost else ids
            rows = [
                [v, d.isoformat(), *[value(v, d, m) for m in shown]]
                for v in wanted
                for d in days
                if d >= first_day_of(v)
            ]
            return self._send(200, {"columnHeaders": headers(["video", "day"]), "rows": rows})

        self._send(400, {"error": {"code": 400, "message": f"unsupported {dims}"}})


def _reset() -> None:
    _Fake.mode = "ok"
    _Fake.bumps = {}
    _Fake.video_chunks = []
    _Fake.calls = []
    _Fake.ghost = False


# ---------------------------------------------------------------------------
# Throwaway database + helpers
# ---------------------------------------------------------------------------


def _migrate(db_url: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": db_url},
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{proc.stderr[-1500:]}")


def _scalar(conn: sa.Connection, sql: str, **params):
    return conn.execute(sa.text(sql), params).scalar()


def _counts(engine: sa.Engine) -> dict[str, int]:
    tables = [
        "raw_youtube_analytics",
        "social_account_daily",
        "social_post_daily",
        "social_traffic_source_daily",
        "social_post",
        "social_metrics_snapshot",
    ]
    with engine.connect() as conn:
        return {t: _scalar(conn, f"SELECT count(*) FROM {t}") for t in tables}


def _add_videos(engine: sa.Engine, ids: list[str]) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO social_post(platform, external_id, posted_at, content_type, caption) "
                "VALUES ('youtube', :e, now(), 'short', 't') ON CONFLICT DO NOTHING"
            ),
            [{"e": v} for v in ids],
        )


def _expected_days(start: date, end: date, first: date = CHANNEL_FIRST_DAY) -> list[date]:
    last = min(end, LAST_DAY)
    return [
        start + timedelta(days=i)
        for i in range((last - start).days + 1)
        if start + timedelta(days=i) >= first
    ]


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def run_checks(base: str) -> int:
    real = load_settings()
    tmp_name = f"dk_verify_yta_{os.getpid()}"
    tmp_url = real.database_url.set(database=tmp_name).render_as_string(hide_password=False)
    tmp = load_settings({"DATABASE_URL": tmp_url})
    urls = {"token_url": f"{base}/token", "reports_url": f"{base}/reports"}
    say: list[str] = []

    def go(**kw) -> ya.RunResult:
        say.clear()
        return ya.run(tmp, OAUTH, today=kw.pop("today", TODAY), out=say.append, **urls, **kw)

    print(f"building throwaway database {tmp_name} ...")
    bootstrap_db.create(tmp)
    engine = sa.create_engine(tmp.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        _migrate(tmp_url)
        videos = video_ids(3)
        _add_videos(engine, videos)
        window = ya.make_window(days=14, since=None, today=TODAY)

        # -- the window --
        problems = []
        if window.end != TODAY - timedelta(days=1):
            problems.append(f"ends {window.end}, want yesterday")
        if (window.end - window.start).days != 13:
            problems.append(f"spans {(window.end - window.start).days + 1} days, want 14")
        try:
            ya.make_window(days=14, since=TODAY, today=TODAY)
            problems.append("a window starting after its end was accepted")
        except WindowError:
            pass
        _check("window ends yesterday, is 14 days wide, and rejects an impossible range", problems)

        # -- run 1 --------------------------------------------------------------
        _reset()
        result = go()
        days = _expected_days(window.start, window.end)
        exp_post = sum(
            len(_expected_days(window.start, window.end, first_day_of(v))) for v in videos
        )
        problems = []
        if result.account_rows != len(days):
            problems.append(f"channel days {result.account_rows}, want {len(days)}")
        if result.traffic_rows != len(days) * 2:
            problems.append(f"traffic rows {result.traffic_rows}, want {len(days) * 2}")
        if result.post_rows != exp_post:
            problems.append(f"video-days {result.post_rows}, want {exp_post}")
        if result.raw_landed != 3:
            problems.append(f"raw reports landed {result.raw_landed}, want 3")
        _check("first run loads every day the API returned, for all three reports", problems)

        # -- the lag: absent days stay absent ---------------------------------------
        with engine.connect() as conn:
            newest = {
                t: _scalar(conn, f"SELECT max(day) FROM {t}")
                for t in (
                    "social_account_daily",
                    "social_post_daily",
                    "social_traffic_source_daily",
                )
            }
            beyond = sum(
                _scalar(conn, f"SELECT count(*) FROM {t} WHERE day > :d", d=LAST_DAY)
                for t in newest
            )
        problems = [
            f"{t} newest day {d}, want {LAST_DAY}" for t, d in newest.items() if d != LAST_DAY
        ]
        if beyond:
            problems.append(f"{beyond} row(s) for days YouTube had not reported yet")
        _check("days newer than the API's lag are ABSENT, never filled with zeros", problems)

        # -- values map by NAME ------------------------------------------------------
        def spot_check() -> list[str]:
            out = []
            with engine.connect() as conn:
                row = (
                    conn.execute(
                        sa.text("SELECT * FROM social_account_daily WHERE day = :d"),
                        {"d": LAST_DAY},
                    )
                    .mappings()
                    .one()
                )
                want = {
                    col: value("channel", LAST_DAY, api) for api, col in EXPECTED_COLUMNS.items()
                }
                out += [
                    f"channel {c}: got {row[c]}, want {w}" for c, w in want.items() if row[c] != w
                ]

                v = videos[1]
                row = (
                    conn.execute(
                        sa.text(
                            "SELECT d.* FROM social_post_daily d JOIN social_post p "
                            "ON p.id = d.social_post_id WHERE p.external_id = :v AND d.day = :d"
                        ),
                        {"v": v, "d": LAST_DAY},
                    )
                    .mappings()
                    .one()
                )
                want = {col: value(v, LAST_DAY, api) for api, col in EXPECTED_COLUMNS.items()}
                out += [
                    f"video {c}: got {row[c]}, want {w}" for c, w in want.items() if row[c] != w
                ]

                row = (
                    conn.execute(
                        sa.text(
                            "SELECT views, watch_minutes FROM social_traffic_source_daily "
                            "WHERE day = :d AND source_type = 'YT_SEARCH'"
                        ),
                        {"d": LAST_DAY},
                    )
                    .mappings()
                    .one()
                )
                want = {
                    col: value("traffic:YT_SEARCH", LAST_DAY, api)
                    for api, col in EXPECTED_TRAFFIC_COLUMNS.items()
                }
                out += [
                    f"traffic {c}: got {row[c]}, want {w}" for c, w in want.items() if row[c] != w
                ]
            return out

        _check(
            "every metric lands in the right column (minutes are minutes, not seconds)",
            spot_check(),
        )

        _Fake.mode = "reversed_columns"
        go()
        _check(
            "mapping is by column NAME: reversed column order stores the same values",
            spot_check(),
        )
        _Fake.mode = "ok"

        # -- idempotency + restatement -------------------------------------------------
        go()
        again = _counts(engine)
        problems = []
        for t in ("social_account_daily", "social_post_daily", "social_traffic_source_daily"):
            n = _counts(engine)[t]
            if (
                n
                != (result.account_rows, result.post_rows, result.traffic_rows)[
                    (
                        "social_account_daily",
                        "social_post_daily",
                        "social_traffic_source_daily",
                    ).index(t)
                ]
            ):
                problems.append(f"{t} has {n} rows after a re-run")
        if again["raw_youtube_analytics"] != 3:
            problems.append(f"raw has {again['raw_youtube_analytics']} rows, want 3 for one window")
        _check(
            "re-running the same window adds no rows (natural-key upsert, one raw row per report)",
            problems,
        )

        with engine.connect() as conn:
            stamp_before = _scalar(
                conn, "SELECT fetched_at FROM social_account_daily WHERE day = :d", d=LAST_DAY
            )
        _Fake.bumps = {
            ("channel", LAST_DAY, "views"): 1000,
            (videos[1], LAST_DAY, "views"): 500,
            ("traffic:YT_SEARCH", LAST_DAY, "views"): 250,
        }
        go()
        with engine.connect() as conn:
            got_channel = _scalar(
                conn, "SELECT views FROM social_account_daily WHERE day = :d", d=LAST_DAY
            )
            got_video = _scalar(
                conn,
                "SELECT d.views FROM social_post_daily d "
                "JOIN social_post p ON p.id = d.social_post_id "
                "WHERE p.external_id = :v AND d.day = :d",
                v=videos[1],
                d=LAST_DAY,
            )
            got_traffic = _scalar(
                conn,
                "SELECT views FROM social_traffic_source_daily "
                "WHERE day = :d AND source_type = 'YT_SEARCH'",
                d=LAST_DAY,
            )
            stamp_after = _scalar(
                conn, "SELECT fetched_at FROM social_account_daily WHERE day = :d", d=LAST_DAY
            )
            n_account = _scalar(conn, "SELECT count(*) FROM social_account_daily")
        problems = []
        for label, got, key in (
            ("channel", got_channel, "channel"),
            ("video", got_video, videos[1]),
            ("traffic", got_traffic, "traffic:YT_SEARCH"),
        ):
            want = value(key, LAST_DAY, "views")
            if got != want:
                problems.append(f"{label} views {got}, want the restated {want}")
        if stamp_after <= stamp_before:
            problems.append("fetched_at did not advance on restatement")
        if n_account != len(days):
            problems.append(f"restating changed the row count to {n_account}")
        _check(
            "a restated day REPLACES the earlier figure in all three tables (and fetched_at moves)",
            problems,
        )
        _Fake.bumps = {}

        # -- only the window just fetched is transformed --
        go()  # settle back to un-bumped values
        reports = ya.extract(OAUTH, window, videos, out=say.append, **urls)
        with engine.begin() as conn:
            ya.land_raw(conn, reports, window)
            stale = json.loads(json.dumps(reports[ya.REPORT_CHANNEL]))
            for row in stale["rows"]:
                row[1] = 999_999  # views, far from anything real
            conn.execute(
                sa.text(
                    "INSERT INTO raw_youtube_analytics(report, start_date, end_date, payload) "
                    "VALUES ('channel_daily', :s, :e, CAST(:p AS jsonb))"
                ),
                {
                    "s": window.start - timedelta(days=30),
                    "e": window.end - timedelta(days=30),
                    "p": json.dumps(stale),
                },
            )
        with engine.begin() as conn:
            ya.transform_and_load(conn, window, fetched_at=datetime.now(UTC))
        with engine.connect() as conn:
            poisoned = _scalar(
                conn, "SELECT count(*) FROM social_account_daily WHERE views = 999999"
            )
        _check(
            "an OLD raw window is never re-applied over fresher data",
            [f"{poisoned} row(s) overwritten from a stale window"] if poisoned else [],
        )

        # -- chunking + batching with a realistic number of videos -------------------------------
        many = video_ids(250)
        _add_videos(engine, many)
        _reset()
        result = go(since=CHANNEL_FIRST_DAY)
        sizes = [len(c) for c in _Fake.video_chunks]
        asked = {v for c in _Fake.video_chunks for v in c}
        with engine.connect() as conn:
            known = {
                r[0]
                for r in conn.execute(
                    sa.text("SELECT external_id FROM social_post WHERE platform='youtube'")
                )
            }
        problems = []
        if max(sizes, default=0) > ya.VIDEO_CHUNK:
            problems.append(f"a request listed {max(sizes)} videos, limit {ya.VIDEO_CHUNK}")
        if asked != known:
            problems.append(f"asked about {len(asked)} videos, warehouse has {len(known)}")
        if len(sizes) != 3:
            problems.append(f"{len(sizes)} per-video requests for 250 videos, want 3")
        exp = sum(
            len(_expected_days(CHANNEL_FIRST_DAY, TODAY - timedelta(days=1), first_day_of(v)))
            for v in known
        )
        if result.post_rows != exp:
            problems.append(f"loaded {result.post_rows} video-days, want {exp}")
        if exp <= ya.INSERT_BATCH:
            problems.append(f"only {exp} rows: the multi-batch insert path was not exercised")
        _check(
            "250 videos are split into <=100-id requests, all are covered, and large loads batch",
            problems,
        )

        # -- degenerate inputs --
        _reset()
        lines: list[str] = []
        got = ya.extract(OAUTH, window, [], out=lines.append, **urls)
        problems = []
        if ya.REPORT_VIDEO in got:
            problems.append("a per-video report was requested with no videos")
        if "video" in _Fake.calls and "video,day" in _Fake.calls:
            problems.append("a video,day query was sent")
        if not any("no YouTube videos" in s for s in lines):
            problems.append("the skip was silent")
        _check(
            "with no videos the per-video report is skipped OUT LOUD, the others still run",
            problems,
        )

        _reset()
        _Fake.ghost = True
        go()
        with engine.connect() as conn:
            ghost = _scalar(conn, "SELECT count(*) FROM social_post WHERE external_id = 'ghost000'")
        _check(
            "rows for a video the warehouse does not know are skipped without error",
            [] if ghost == 0 else ["a post was created for an unknown video"],
        )

        # -- failures write NOTHING ---------------------------------------------------------------
        def must_fail(mode: str, needle: str, forbidden: tuple[str, ...] = ()) -> list[str]:
            _reset()
            _Fake.mode = mode
            snap = _counts(engine)
            problems = []
            try:
                go()
                problems.append("no ExtractionError raised")
            except ExtractionError as exc:
                text = str(exc)
                if needle not in text:
                    problems.append(f"message lacks {needle!r}: {text}")
                problems += [f"message leaked {f!r}: {text}" for f in forbidden if f in text]
            except Exception as exc:  # noqa: BLE001 - a raw traceback is the failure being reported
                problems.append(f"raised {type(exc).__name__} instead of a clear ExtractionError")
            if _counts(engine) != snap:
                problems.append("rows were written despite the failure")
            return problems

        _check(
            "an expired token says to run `make youtube-auth`, leaking neither token nor secret",
            must_fail("invalid_grant", "make youtube-auth", (REFRESH, CLIENT_SECRET)),
        )
        _check("a disabled Analytics API says how to enable it", must_fail("not_enabled", "Enable"))
        _check(
            "if ANY report fails nothing is written (no half-fetched window)",
            must_fail("video_500", "500"),
        )
        _check(
            "a response with a different shape fails loudly", must_fail("ragged", "response shape")
        )
        _check(
            "a possibly-truncated response fails instead of loading a partial window",
            must_fail("huge", "truncated"),
        )

        # -- dry run --
        _reset()
        snap = _counts(engine)
        go(dry_run=True)
        problems = []
        if _counts(engine) != snap:
            problems.append("a dry run wrote rows")
        if not any("newest day returned" in s for s in say):
            problems.append("the summary does not say how fresh the data is")
        _check("--dry-run writes nothing and reports how fresh the data is", problems)

        # -- the other tables are never touched --
        _reset()
        snap = _counts(engine)
        go()
        after = _counts(engine)
        _check(
            "analytics never touches social_post or social_metrics_snapshot",
            [
                f"{t} changed {snap[t]}->{after[t]}"
                for t in ("social_post", "social_metrics_snapshot")
                if snap[t] != after[t]
            ],
        )
    finally:
        engine.dispose()
        bootstrap_db.drop(tmp, yes=True)

    print()
    if _failures:
        print(f"FAILED {len(_failures)}/{_checks_run} checks: {', '.join(_failures)}")
        return 1
    print(f"all {_checks_run} checks passed")
    return 0


def main() -> int:
    try:
        load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return run_checks(f"http://127.0.0.1:{server.server_address[1]}")
    finally:
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
