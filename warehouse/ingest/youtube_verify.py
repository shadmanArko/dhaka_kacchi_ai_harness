"""Prove warehouse.ingest.youtube behaves correctly, WITHOUT a YouTube account.

Matches the warehouse.verify / direct_verify idiom: plain ok/FAIL output and an
exit code, not a pytest suite (this repo has zero pytest usage).

Unlike direct_verify.py, which runs against the live dev database, this one
builds and drops its OWN throwaway database. It has to: the fake channel it
serves would otherwise plant fake YouTube rows in whatever DATABASE_URL points
at, and a verify command that pollutes real data is worse than no verify
command. Your dev database is never touched.

What it stands in front of the code is a local fake of the YouTube Data API
implementing the documented response shapes: a paginated uploads playlist,
videos.list with its 50-id limit, a private video that videos.list will not
return, hidden likes, disabled comments, a live stream, a Short with and
without the #shorts tag, and a quota error whose body echoes the API key.

IT DOES NOT PROVE the code is right about the REAL YouTube - only that it is
right about the documented shapes. The first run against the real channel is
the verification of that, and youtube.py's STATUS block lists what to confirm.

Run with `make verify-ingest-youtube`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import sqlalchemy as sa

from warehouse import bootstrap_db
from warehouse.config import (
    REPO_ROOT,
    ConfigError,
    YouTubeSourceSettings,
    load_settings,
)
from warehouse.ingest import youtube
from warehouse.ingest.youtube import ExtractionError, run, transform_and_load

API_KEY = "FAKE-KEY-DO-NOT-LEAK-12345"
CHANNEL_ID = "UC" + "x" * 22
TOTAL_LISTED = 125  # ids in the uploads playlist (spans three 50-item pages)

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
# The fake YouTube API
# ---------------------------------------------------------------------------


def _video(vid: str, duration: str, **overrides) -> dict:
    snippet = {
        "publishedAt": "2026-09-22T17:20:56Z",
        "title": f"Title {vid}",
        "description": "",
        "liveBroadcastContent": "none",
    }
    snippet.update(overrides.pop("snippet", {}))
    stats = {"viewCount": "100", "likeCount": "10", "commentCount": "2"}
    for key in overrides.pop("drop_stats", []):
        stats.pop(key)
    stats.update(overrides.pop("stats", {}))
    return {
        "id": vid,
        "snippet": snippet,
        "contentDetails": {"duration": duration},
        "statistics": stats,
    }


def _build_catalogue() -> tuple[list[str], dict[str, dict]]:
    """Returns (ids in the uploads playlist, {id: videos.list item}). Anything
    in the first but not the second is private/deleted, which an API key cannot
    see - exactly like the real thing."""
    special = {
        "v000": _video("v000", "PT45S", stats={"viewCount": "4321"}),  # a Short
        "v001": _video("v001", "PT10M3S", drop_stats=["likeCount"]),  # likes hidden
        "v002": _video("v002", "PT2M30S", snippet={"description": "so tasty #Shorts"}),
        "v003": _video("v003", "PT2M30S"),  # same length, untagged -> video
        "v004": _video("v004", "P0D", snippet={"liveBroadcastContent": "live"}),  # live
        "v005": _video("v005", "PT5M", drop_stats=["commentCount"]),  # comments off
        "v007": _video("v007", "PT1H0M0S"),  # an hour long
        "v008": _video("v008", "garbage"),  # unparseable duration
        "v009": _video("v009", "PT1M0S"),  # exactly 60s -> still a Short
        "v010": _video("v010", "PT61S"),  # 61s, untagged -> video
    }
    ids = [f"v{i:03d}" for i in range(TOTAL_LISTED)]
    catalogue = {i: special.get(i) or _video(i, "PT5M") for i in ids}
    del catalogue["v006"]  # listed in the playlist, invisible to videos.list
    return ids, catalogue


class _State:
    ids: list[str] = []
    catalogue: dict[str, dict] = {}
    mode = "ok"  # "ok" | "quota" | "no_channel"
    max_batch_seen = 0
    video_calls = 0


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # silence the default stderr access log
        pass

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 (http.server's required name)
        parts = urlsplit(self.path)
        resource = parts.path.rsplit("/", 1)[-1]
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}

        if q.get("key") != API_KEY:
            return self._send(400, {"error": {"code": 400, "message": "API key not valid."}})

        if _State.mode == "quota":
            # Echoes the key in the body on purpose: the real API's errors can
            # include request details, and the ingester must not pass them on.
            return self._send(
                403,
                {
                    "error": {
                        "code": 403,
                        "message": f"Quota exceeded for key {API_KEY}.",
                        "errors": [{"reason": "quotaExceeded"}],
                    }
                },
            )

        if resource == "channels":
            if _State.mode == "no_channel":
                return self._send(200, {"items": []})
            return self._send(
                200,
                {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUfake"}}}]},
            )

        if resource == "playlistItems":
            size = int(q.get("maxResults", "5"))
            start = int(q.get("pageToken", "0"))
            page = _State.ids[start : start + size]
            body = {"items": [{"contentDetails": {"videoId": vid}} for vid in page]}
            if start + size < len(_State.ids):
                body["nextPageToken"] = str(start + size)
            return self._send(200, body)

        if resource == "videos":
            wanted = q["id"].split(",")
            _State.video_calls += 1
            _State.max_batch_seen = max(_State.max_batch_seen, len(wanted))
            if len(wanted) > 50:  # the real API rejects this
                return self._send(400, {"error": {"code": 400, "message": "too many ids"}})
            return self._send(
                200, {"items": [_State.catalogue[v] for v in wanted if v in _State.catalogue]}
            )

        return self._send(404, {"error": {"code": 404, "message": f"no route {resource}"}})


def _start_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/youtube/v3"


# ---------------------------------------------------------------------------
# The throwaway database
# ---------------------------------------------------------------------------


def _migrate(db_url: str) -> None:
    env = {**os.environ, "DATABASE_URL": db_url}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{proc.stderr[-1500:]}")


def _scalar(conn: sa.Connection, sql: str, **params):
    return conn.execute(sa.text(sql), params).scalar()


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def run_checks(base_api: str) -> int:
    real = load_settings()
    tmp_name = f"dk_verify_youtube_{os.getpid()}"
    tmp_url = real.database_url.set(database=tmp_name).render_as_string(hide_password=False)
    tmp = load_settings({"DATABASE_URL": tmp_url})

    source = YouTubeSourceSettings(api_key=API_KEY, channel_id=CHANNEL_ID, api_base=base_api)
    _State.ids, _State.catalogue = _build_catalogue()
    visible = len(_State.catalogue)

    print(f"building throwaway database {tmp_name} ...")
    bootstrap_db.create(tmp)
    engine = sa.create_engine(tmp.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        _migrate(tmp_url)

        # -- extraction ----------------------------------------------------
        videos, listed = youtube.extract(source)
        problems = []
        if listed != TOTAL_LISTED:
            problems.append(f"listed {listed}, expected {TOTAL_LISTED} (pagination broken?)")
        if len(videos) != visible:
            problems.append(f"returned {len(videos)}, expected {visible}")
        _check("paginates the uploads playlist and returns every visible video", problems)

        _check(
            "never sends more than 50 ids to videos.list",
            [f"saw a batch of {_State.max_batch_seen}"] if _State.max_batch_seen > 50 else [],
        )
        _check(
            "batches rather than one-call-per-video",
            [f"{_State.video_calls} videos.list calls for {listed} ids"]
            if _State.video_calls > 4
            else [],
        )

        # -- run 1 ---------------------------------------------------------
        result = run(tmp, source)
        problems = []
        if (result.raw_landed, result.posts_upserted, result.snapshots_upserted) != (
            visible,
            visible,
            visible,
        ):
            problems.append(f"got {result}, expected {visible} of each")
        _check("first run lands every visible video as raw, post and snapshot", problems)

        with engine.connect() as conn:
            types = dict(
                conn.execute(sa.text("SELECT external_id, content_type FROM social_post")).all()
            )
        expected_types = {
            "v000": "short",  # 45s
            "v009": "short",  # exactly 60s is still a Short
            "v010": "video",  # 61s untagged
            "v002": "short",  # 150s with #Shorts (case-insensitive)
            "v003": "video",  # 150s untagged
            "v004": "video",  # live stream, P0D
            "v007": "video",  # an hour
            "v008": None,  # unparseable duration -> unknown, not guessed
            "v001": "video",
        }
        _check(
            "Short/video classification follows the documented rule",
            [
                f"{k}: got {types.get(k)!r}, want {v!r}"
                for k, v in expected_types.items()
                if types.get(k) != v
            ],
        )

        # -- NULL discipline -----------------------------------------------
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT "
                    " count(*) FILTER (WHERE impressions IS NOT NULL OR reach IS NOT NULL OR "
                    "   shares IS NOT NULL OR saves IS NOT NULL OR clicks IS NOT NULL OR "
                    "   watch_seconds IS NOT NULL OR subscribers_gained IS NOT NULL OR "
                    "   impression_ctr IS NOT NULL) AS invented, "
                    " count(*) FILTER (WHERE views IS NULL) AS no_views "
                    "FROM social_metrics_snapshot"
                )
            ).one()
        problems = []
        if row.invented:
            problems.append(f"{row.invented} snapshot(s) hold a value the Data API never returned")
        if row.no_views:
            problems.append(f"{row.no_views} snapshot(s) are missing views")
        _check("metrics the Data API does not return are NULL, never an invented 0", problems)

        with engine.connect() as conn:

            def metric(external_id: str, column: str):
                return _scalar(
                    conn,
                    f"SELECT s.{column} FROM social_metrics_snapshot s "
                    "JOIN social_post p ON p.id = s.social_post_id WHERE p.external_id = :e",
                    e=external_id,
                )

            got = {
                "v000 views": (metric("v000", "views"), 4321),
                "v000 likes": (metric("v000", "likes"), 10),
                "v001 likes (hidden)": (metric("v001", "likes"), None),
                "v005 comments (disabled)": (metric("v005", "comments"), None),
                "v005 likes (present)": (metric("v005", "likes"), 10),
            }
        _check(
            "hidden likes and disabled comments are NULL; present stats are stored as numbers",
            [f"{k}: got {g!r}, want {w!r}" for k, (g, w) in got.items() if g != w],
        )

        with engine.connect() as conn:
            private = _scalar(conn, "SELECT count(*) FROM social_post WHERE external_id = 'v006'")
        _check(
            "a private video (listed but not returned) is skipped, not an error",
            [f"v006 landed {private} time(s)"] if private else [],
        )

        with engine.connect() as conn:
            permalinks = dict(
                conn.execute(
                    sa.text(
                        "SELECT external_id, permalink FROM social_post "
                        "WHERE external_id IN ('v000','v003')"
                    )
                ).all()
            )
        _check(
            "Shorts get a /shorts/ permalink, videos a /watch URL",
            [
                f"{k}: {v}"
                for k, v in {
                    "v000": "https://www.youtube.com/shorts/v000",
                    "v003": "https://www.youtube.com/watch?v=v003",
                }.items()
                if permalinks.get(k) != v
            ],
        )

        with engine.connect() as conn:
            caption_ok = _scalar(
                conn,
                "SELECT caption FROM social_post WHERE external_id = 'v002'",
            )
        _check(
            "caption is title then description",
            [] if caption_ok == "Title v002\n\nso tasty #Shorts" else [repr(caption_ok)],
        )

        # -- idempotency ---------------------------------------------------
        fixed = datetime(2026, 1, 1, tzinfo=UTC)
        ids = [v["id"] for v in videos]
        with engine.begin() as conn:
            a = transform_and_load(conn, captured_at=fixed, external_ids=ids)
        with engine.begin() as conn:
            b = transform_and_load(conn, captured_at=fixed, external_ids=ids)
        _check(
            "re-running with the SAME captured_at inserts no duplicate snapshots",
            [f"second pass inserted {b[1]} snapshot(s)"] if b[1] != 0 else [],
        )
        _check(
            "...and the first pass at a new captured_at did insert them",
            [f"inserted {a[1]}, expected {visible}"] if a[1] != visible else [],
        )

        with engine.connect() as conn:
            posts_before = _scalar(conn, "SELECT count(*) FROM social_post")
            raw_before = _scalar(conn, "SELECT count(*) FROM raw_social_posts_youtube")
        run(tmp, source)
        with engine.connect() as conn:
            posts_after = _scalar(conn, "SELECT count(*) FROM social_post")
            raw_after = _scalar(conn, "SELECT count(*) FROM raw_social_posts_youtube")
            snaps = _scalar(conn, "SELECT count(*) FROM social_metrics_snapshot")
        _check(
            "a later run adds no posts or raw rows (natural-key upsert)",
            [f"posts {posts_before}->{posts_after}, raw {raw_before}->{raw_after}"]
            if (posts_before, raw_before) != (posts_after, raw_after)
            else [],
        )
        _check(
            "...but does append one snapshot per video, building the time series",
            [f"{snaps} snapshots, expected {visible * 3}"] if snaps != visible * 3 else [],
        )

        # -- a video removed from the channel is not re-snapshotted -------------
        _State.catalogue.pop("v020")
        _State.ids.remove("v020")
        with engine.connect() as conn:
            before = _scalar(
                conn,
                "SELECT count(*) FROM social_metrics_snapshot s JOIN social_post p "
                "ON p.id = s.social_post_id WHERE p.external_id = 'v020'",
            )
        run(tmp, source)
        with engine.connect() as conn:
            after = _scalar(
                conn,
                "SELECT count(*) FROM social_metrics_snapshot s JOIN social_post p "
                "ON p.id = s.social_post_id WHERE p.external_id = 'v020'",
            )
            still_there = _scalar(
                conn, "SELECT count(*) FROM social_post WHERE external_id = 'v020'"
            )
        problems = []
        if after != before:
            problems.append(f"v020 snapshots {before}->{after}: stale data got a fresh timestamp")
        if still_there != 1:
            problems.append("the post row for a removed video should be kept, not deleted")
        _check("a removed video is kept but never re-snapshotted from its stale payload", problems)

        # -- the share job must not pick YouTube up ----------------------------
        from ops.refresh_social_share import EXTRACT_SQL, SHARED_PLATFORMS

        with engine.connect() as conn:
            shared = {
                r.platform
                for r in conn.execute(sa.text(EXTRACT_SQL), {"platforms": list(SHARED_PLATFORMS)})
            }
        _check(
            "ops/refresh_social_share.py does not pick up YouTube rows",
            ["youtube leaked into the share job"] if "youtube" in shared else [],
        )

        # -- failure paths -------------------------------------------------
        _State.mode = "quota"
        try:
            youtube.extract(source)
            _check("a quota error raises ExtractionError", ["no exception raised"])
        except ExtractionError as exc:
            message = str(exc)
            problems = []
            if "quotaExceeded" not in message:
                problems.append(f"reason missing from: {message}")
            if "midnight Pacific" not in message:
                problems.append("no plain-language hint for the owner")
            _check("a quota error raises ExtractionError with the reason and a fix", problems)
            _check(
                "the API key never appears in an error message",
                [f"key leaked: {message}"] if API_KEY in message else [],
            )

        _State.mode = "no_channel"
        try:
            youtube.extract(source)
            _check("an unknown channel id raises ExtractionError", ["no exception raised"])
        except ExtractionError as exc:
            _check(
                "an unknown channel id raises an error that names the likely cause",
                [] if "YOUTUBE_CHANNEL_ID" in str(exc) else [str(exc)],
            )
        _State.mode = "ok"

        bad = YouTubeSourceSettings(api_key="WRONG", channel_id=CHANNEL_ID, api_base=base_api)
        try:
            youtube.extract(bad)
            _check("a wrong API key raises ExtractionError", ["no exception raised"])
        except ExtractionError as exc:
            _check(
                "a wrong API key raises ExtractionError without echoing it",
                ["wrong key echoed"] if "WRONG" in str(exc) else [],
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

    server, base_api = _start_server()
    try:
        return run_checks(base_api)
    finally:
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
