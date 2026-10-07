"""Prove warehouse.ingest.social_followers behaves correctly, WITHOUT any social account.

Same idiom as youtube_verify.py: plain ok/FAIL output and an exit code, a throwaway
database of its own (your dev data is never touched), and a local fake standing in
for the platforms. The fake serves the response shapes seen on the real accounts
2026-10-07 (see social_followers.py), so this proves the code is right about THOSE
shapes, not about any change the platforms make later.

Beyond the happy path it proves the things that would silently corrupt the history:
the Berlin-day boundary (summer AND winter), a later capture replacing an earlier one
and never the reverse, YouTube Analytics' figures in the same row surviving, a hidden
subscriber count becoming NULL not 0, one failing platform not costing the others,
tokens never reaching an error message, and invalid answers never being landed.

Then it MUTATES the job - removes the out-of-order guard, swaps the Berlin day for UTC,
makes the upsert clobber neighbouring columns, lets a boolean through as a count - and
demands that the matching check fails. A check that cannot fail proves nothing.

Run with `make verify-ingest-social-followers`.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import threading
import types
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import sqlalchemy as sa

from warehouse import bootstrap_db
from warehouse.config import ConfigError, load_settings
from warehouse.ingest import social_followers as real_module
from warehouse.ingest import youtube_analytics

IG_ID, IG_TOKEN = "17841400000000001", "FAKE-IG-TOKEN-DO-NOT-LEAK"
FB_ID, FB_TOKEN = "100000000000001", "FAKE-FB-TOKEN-DO-NOT-LEAK"
TH_ID, TH_TOKEN = "28000000000000001", "FAKE-TH-TOKEN-DO-NOT-LEAK"
YT_ID, YT_KEY = "UC" + "x" * 22, "FAKE-YT-KEY-DO-NOT-LEAK"
TOKENS = (IG_TOKEN, FB_TOKEN, TH_TOKEN, YT_KEY)

SUMMER = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)  # Berlin is UTC+2


class _State:
    values = {"instagram": 41, "facebook": 355, "threads": 12, "youtube": 7}
    fan_count = 300  # Facebook's older figure; must NOT be preferred over followers_count
    mode = {"instagram": "ok", "facebook": "ok", "threads": "ok", "youtube": "ok"}


def _body(platform: str) -> tuple[int, dict]:
    n, mode = _State.values[platform], _State.mode[platform]
    if mode == "error":
        token = {
            "instagram": IG_TOKEN,
            "facebook": FB_TOKEN,
            "threads": TH_TOKEN,
            "youtube": YT_KEY,
        }
        return 403, {"error": {"message": f"Token {token[platform]} has expired"}}
    value: object = {"bool": True, "negative": -5, "text": "abc", "ok": n}[
        mode if mode in ("bool", "negative", "text") else "ok"
    ]
    if platform == "instagram":
        return 200, (
            {"id": IG_ID} if mode == "missing" else {"id": IG_ID, "followers_count": value}
        )
    if platform == "facebook":
        if mode == "fan_only":
            return 200, {"id": FB_ID, "fan_count": _State.fan_count}
        return 200, {"id": FB_ID, "followers_count": value, "fan_count": _State.fan_count}
    if platform == "threads":
        if mode == "missing":
            return 200, {"data": []}
        return 200, {
            "data": [{"name": "followers_count", "period": "day", "total_value": {"value": value}}]
        }
    stats = {"viewCount": "858", "subscriberCount": str(value), "hiddenSubscriberCount": False}
    if mode == "hidden":
        stats = {"viewCount": "858", "hiddenSubscriberCount": True}
    if mode == "missing":
        return 200, {"items": []}
    return 200, {"items": [{"id": YT_ID, "statistics": stats}]}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = url.path.strip("/").split("/")
        if parts[0] == "graph" and len(parts) == 2:
            ok = q.get("access_token") == (IG_TOKEN if parts[1] == IG_ID else FB_TOKEN)
            platform = "instagram" if parts[1] == IG_ID else "facebook"
        elif parts[0] == "threads" and parts[-1] == "threads_insights":
            ok = q.get("access_token") == TH_TOKEN and q.get("metric") == "followers_count"
            platform = "threads"
        elif parts[0] == "yt" and parts[-1] == "channels":
            ok = q.get("key") == YT_KEY and q.get("id") == YT_ID and q.get("part") == "statistics"
            platform = "youtube"
        else:
            return self._send(404, {"error": "no route"})
        if not ok:
            return self._send(400, {"error": {"message": "invalid credentials or query"}})
        status, body = _body(platform)
        self._send(status, body)


def _start_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_env(**overrides: str) -> None:
    base = {
        "INSTAGRAM_ACCESS_TOKEN": IG_TOKEN,
        "INSTAGRAM_BUSINESS_ACCOUNT_ID": IG_ID,
        "FACEBOOK_PAGE_ACCESS_TOKEN": FB_TOKEN,
        "FACEBOOK_PAGE_ID": FB_ID,
        "THREADS_ACCESS_TOKEN": TH_TOKEN,
        "THREADS_USER_ID": TH_ID,
        "YOUTUBE_API_KEY": YT_KEY,
        "YOUTUBE_CHANNEL_ID": YT_ID,
    }
    base.update(overrides)
    # Set explicitly, never deleted: the loaders call load_dotenv(override=False), which
    # would refill a DELETED variable from the developer's real .env. An empty string is
    # "set", so it stays empty and the loader reports it as unconfigured.
    os.environ.update(base)


def _reset(engine: sa.Engine) -> None:
    _State.values = {"instagram": 41, "facebook": 355, "threads": 12, "youtube": 7}
    _State.mode = dict.fromkeys(_State.mode, "ok")
    _set_env()
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM raw_social_followers"))
        conn.execute(sa.text("DELETE FROM social_account_daily"))


def _rows(engine: sa.Engine, sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(sa.text(sql), params).all()


def _followers(engine: sa.Engine) -> dict[tuple[str, str], int | None]:
    rows = _rows(engine, "SELECT platform, day, followers FROM social_account_daily")
    return {(p, str(d)): f for p, d, f in rows}


def _mutant(replacements: list[tuple[str, str]]) -> types.ModuleType:
    """The real module's source with edits applied. Each edit MUST apply, or the
    mutation silently tests nothing."""
    source = Path(real_module.__file__).read_text()
    for old, new in replacements:
        if old not in source:
            raise AssertionError(f"mutation target not found in social_followers.py: {old!r}")
        source = source.replace(old, new, 1)
    module = types.ModuleType("social_followers_mutant")
    module.__file__ = real_module.__file__
    sys.modules[module.__name__] = module  # dataclass(slots=True) looks itself up here
    exec(compile(source, "social_followers_mutant", "exec"), module.__dict__)  # noqa: S102
    return module


# ---------------------------------------------------------------------------
# Scenarios: each returns a list of problems (empty = pass)
# ---------------------------------------------------------------------------


def scenarios(mod, engine, settings, bases) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {}

    def go(name: str, fn) -> None:
        _reset(engine)
        try:
            with contextlib.redirect_stderr(io.StringIO()):  # skip warnings are expected here
                results[name] = fn()
        except Exception as exc:  # a crash is a failure with a reason, not a pass
            results[name] = [f"crashed: {type(exc).__name__}: {exc}"]

    def run(**kw):
        return mod.run(settings, bases=bases, captured_at=kw.pop("at", SUMMER), **kw)

    def happy():
        out = run()
        problems = []
        want = {"instagram": 41, "facebook": 355, "threads": 12, "youtube": 7}
        if out.stored != want:
            problems.append(f"stored {out.stored}, want {want}")
        got = _followers(engine)
        for p, n in want.items():
            if got.get((p, "2026-10-07")) != n:
                problems.append(f"{p}: row has {got.get((p, '2026-10-07'))}, want {n}")
        raw = _rows(engine, "SELECT count(*) FROM raw_social_followers")[0][0]
        if raw != 4:
            problems.append(f"{raw} raw rows, want 4")
        stamped = _rows(
            engine, "SELECT platform FROM social_account_daily WHERE fetched_at IS NOT NULL"
        )
        if stamped:
            problems.append(
                f"followers-only rows claim an analytics fetch time: {[r[0] for r in stamped]}"
            )
        if mod.exit_code(out) != 0:
            problems.append("exit code not 0")
        return problems

    def fb_fallback():
        _State.mode["facebook"] = "fan_only"
        out = run(platforms=("facebook",))
        return [] if out.stored == {"facebook": 300} else [f"stored {out.stored}, want 300"]

    def same_capture_twice():
        run()
        _State.values["instagram"] = 99  # the API changes, but this is the SAME capture
        run()
        raw = _rows(engine, "SELECT count(*) FROM raw_social_followers")[0][0]
        got = _followers(engine).get(("instagram", "2026-10-07"))
        return ([f"{raw} raw rows after a repeated capture, want 4"] if raw != 4 else []) + (
            [f"typed value {got}, want the first capture's 41"] if got != 41 else []
        )

    def later_replaces_earlier():
        run()
        _State.values["instagram"] = 50
        run(at=datetime(2026, 10, 7, 15, 0, tzinfo=UTC))
        got = _followers(engine).get(("instagram", "2026-10-07"))
        raw = _rows(engine, "SELECT count(*) FROM raw_social_followers WHERE platform='instagram'")[
            0
        ][0]
        return ([f"day holds {got}, want the later capture's 50"] if got != 50 else []) + (
            [f"{raw} raw captures kept, want both (2)"] if raw != 2 else []
        )

    def older_never_replaces_newer():
        _State.values["instagram"] = 50
        run(platforms=("instagram",), at=datetime(2026, 10, 7, 15, 0, tzinfo=UTC))
        _State.values["instagram"] = 41
        run(platforms=("instagram",), at=datetime(2026, 10, 7, 9, 0, tzinfo=UTC))  # arrives late
        got = _followers(engine).get(("instagram", "2026-10-07"))
        return [] if got == 50 else [f"an older capture overwrote the newer: day holds {got}"]

    def berlin_day_boundaries():
        problems = []
        cases = [
            (datetime(2026, 10, 7, 21, 59, tzinfo=UTC), "2026-10-07"),  # 23:59 CEST
            (datetime(2026, 10, 7, 22, 0, tzinfo=UTC), "2026-10-08"),  # 00:00 CEST
            (datetime(2026, 1, 15, 22, 59, tzinfo=UTC), "2026-01-15"),  # 23:59 CET
            (datetime(2026, 1, 15, 23, 0, tzinfo=UTC), "2026-01-16"),  # 00:00 CET
        ]
        for at, want_day in cases:
            _reset(engine)
            run(platforms=("instagram",), at=at)
            days = [d for (_, d) in _followers(engine)]
            if days != [want_day]:
                problems.append(f"{at.isoformat()} -> {days}, want {want_day}")
        return problems

    def keeps_youtube_analytics():
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO social_account_daily (platform, day, views, subscribers_gained, "
                    "fetched_at) VALUES ('youtube', '2026-10-07', 100, 3, now())"
                )
            )
        before = _rows(
            engine,
            "SELECT fetched_at FROM social_account_daily WHERE platform='youtube'",
        )[0][0]
        run()
        row = _rows(
            engine,
            "SELECT views, subscribers_gained, followers, fetched_at = :before "
            "FROM social_account_daily WHERE platform='youtube' AND day='2026-10-07'",
            before=before,
        )[0]
        problems = (
            []
            if tuple(row) == (100, 3, 7, True)
            else [f"row is {tuple(row)}, want (100, 3, 7, fetched_at unchanged)"]
        )
        # And the other direction: YouTube Analytics must never write `followers`.
        if "followers" in set(youtube_analytics.METRIC_COLUMNS.values()):
            problems.append("youtube_analytics writes the followers column")
        return problems

    def hidden_is_null():
        _State.mode["youtube"] = "hidden"
        out = run(platforms=("youtube",))
        row = _rows(
            engine,
            "SELECT followers, followers_fetched_at FROM social_account_daily "
            "WHERE platform='youtube'",
        )
        problems = []
        if out.stored != {"youtube": None}:
            problems.append(f"stored {out.stored}, want {{'youtube': None}}")
        if not row or row[0][0] is not None or row[0][1] is None:
            problems.append(f"row {row}, want followers NULL with a read time")
        return problems

    def one_failure_isolated():
        _State.mode["threads"] = "error"
        out = run()
        problems = []
        if sorted(out.stored) != ["facebook", "instagram", "youtube"]:
            problems.append(f"stored {sorted(out.stored)}, want the other three")
        if list(out.failed) != ["threads"]:
            problems.append(f"failed {list(out.failed)}, want ['threads']")
        leaked = [t for t in TOKENS if any(t in m for m in out.failed.values())]
        if leaked:
            problems.append("a token reached the error message")
        if mod.exit_code(out) != 3:
            problems.append(f"exit code {mod.exit_code(out)}, want 3")
        return problems

    def invalid_never_landed():
        problems = []
        for platform, mode in [
            ("instagram", "bool"),
            ("facebook", "negative"),
            ("threads", "text"),
            ("instagram", "missing"),
            ("threads", "missing"),
            ("youtube", "missing"),
        ]:
            _reset(engine)
            _State.mode[platform] = mode
            out = run(platforms=(platform,))
            raw = _rows(engine, "SELECT count(*) FROM raw_social_followers")[0][0]
            rows = _rows(engine, "SELECT count(*) FROM social_account_daily")[0][0]
            if platform not in out.failed or raw or rows or out.stored:
                problems.append(
                    f"{platform}/{mode}: failed={list(out.failed)} raw={raw} rows={rows}"
                )
        return problems

    def unconfigured_skipped():
        _set_env(INSTAGRAM_ACCESS_TOKEN="")
        out = run()
        problems = []
        if out.skipped != ["instagram"] or sorted(out.stored) != ["facebook", "threads", "youtube"]:
            problems.append(f"skipped={out.skipped} stored={sorted(out.stored)}")
        if mod.exit_code(out) != 0:
            problems.append("a skipped platform alone must not fail the run")
        return problems

    def nothing_configured_is_error():
        _set_env(
            INSTAGRAM_ACCESS_TOKEN="",
            FACEBOOK_PAGE_ACCESS_TOKEN="",
            THREADS_ACCESS_TOKEN="",
            YOUTUBE_API_KEY="",
        )
        out = run()
        return [] if mod.exit_code(out) == 2 else [f"exit code {mod.exit_code(out)}, want 2"]

    def unreachable_is_redacted():
        dead = type(bases)(
            graph="http://127.0.0.1:9/graph", threads=bases.threads, youtube=bases.youtube
        )
        out = mod.run(settings, bases=dead, captured_at=SUMMER, platforms=("instagram",))
        msg = out.failed.get("instagram", "")
        return (
            [] if "instagram" in out.failed else ["unreachable API not reported as a failure"]
        ) + (["token leaked"] if IG_TOKEN in msg else [])

    def dry_run_writes_nothing():
        out = run(dry_run=True)
        n = _rows(
            engine,
            "SELECT (SELECT count(*) FROM raw_social_followers) + "
            "(SELECT count(*) FROM social_account_daily)",
        )[0][0]
        return ([f"{n} rows written by a dry run"] if n else []) + (
            [] if len(out.stored) == 4 else [f"dry run read {len(out.stored)}/4 platforms"]
        )

    def constraints_hold():
        problems = []
        for sql, what in [
            (
                "INSERT INTO social_account_daily (platform, day, followers) "
                "VALUES ('instagram', '2026-01-01', 5)",
                "a count with no read time",
            ),
            (
                "INSERT INTO social_account_daily (platform, day, followers, followers_fetched_at) "
                "VALUES ('instagram', '2026-01-02', -1, now())",
                "a negative count",
            ),
        ]:
            try:
                with engine.begin() as conn:
                    conn.execute(sa.text(sql))
                problems.append(f"accepted {what}")
            except sa.exc.IntegrityError:
                pass
        return problems

    for name, fn in [
        ("reads all four platforms and stores each on the Berlin day", happy),
        ("Facebook falls back to fan_count only when followers_count is absent", fb_fallback),
        ("the same capture twice adds no raw row and never restates", same_capture_twice),
        (
            "a later capture replaces the day's figure; both captures stay in raw",
            later_replaces_earlier,
        ),
        ("an older capture arriving late never overwrites a newer one", older_never_replaces_newer),
        ("the day follows Berlin time in summer AND winter", berlin_day_boundaries),
        ("YouTube Analytics figures in the same row survive", keeps_youtube_analytics),
        ("a hidden YouTube subscriber count is NULL with a read time, not 0", hidden_is_null),
        (
            "one failing platform costs the others nothing; token stays out of the message",
            one_failure_isolated,
        ),
        (
            "invalid answers (boolean, negative, text, missing) are rejected before landing",
            invalid_never_landed,
        ),
        ("an unconfigured platform is skipped, not fatal", unconfigured_skipped),
        ("nothing configured at all is a config error", nothing_configured_is_error),
        ("an unreachable API is a redacted failure", unreachable_is_redacted),
        ("a dry run writes nothing", dry_run_writes_nothing),
        ("the table rejects a count without a read time and a negative count", constraints_hold),
    ]:
        go(name, fn)
    return results


MUTATIONS = [
    (
        "removing the out-of-order guard",
        [
            (
                "WHERE social_account_daily.followers_fetched_at IS NULL "
                '"\n            "OR social_account_daily.followers_fetched_at '
                '<= EXCLUDED.followers_fetched_at"',
                '"',
            )
        ],
        "an older capture arriving late never overwrites a newer one",
    ),
    (
        "stamping an analytics fetch time on a followers-only row",
        [
            (
                "(platform, day, followers, followers_fetched_at) ",
                "(platform, day, followers, followers_fetched_at, fetched_at) ",
            ),
            (":f, :t) ", ":f, :t, now()) "),
        ],
        "reads all four platforms and stores each on the Berlin day",
    ),
    (
        "using the UTC day instead of Berlin's",
        [("AT TIME ZONE 'Europe/Berlin'", "AT TIME ZONE 'UTC'")],
        "the day follows Berlin time in summer AND winter",
    ),
    (
        "clobbering neighbouring columns in the upsert",
        [('"updated_at = now() "', '"views = NULL, updated_at = now() "')],
        "YouTube Analytics figures in the same row survive",
    ),
    (
        "letting a boolean through as a count",
        [
            (
                "if isinstance(value, bool) or not isinstance(value, int | str):",
                "if not isinstance(value, int | str):",
            )
        ],
        "invalid answers (boolean, negative, text, missing) are rejected before landing",
    ),
    (
        "writing 0 for a hidden count",
        [
            (
                "            return None\n        return _count(stats.get",
                "            return 0\n        return _count(stats.get",
            )
        ],
        "a hidden YouTube subscriber count is NULL with a read time, not 0",
    ),
    (
        "letting one failure abort the run",
        [
            (
                "            except ExtractionError as exc:\n"
                "                outcome.failed[platform] = str(exc)",
                "            except ExtractionError as exc:\n                raise",
            )
        ],
        "one failing platform costs the others nothing; token stays out of the message",
    ),
]


def main() -> int:
    try:
        real = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    server, origin = _start_server()
    bases = real_module.Bases(
        graph=f"{origin}/graph", threads=f"{origin}/threads", youtube=f"{origin}/yt"
    )
    tmp_name = f"dk_verify_followers_{os.getpid()}"
    tmp_url = real.database_url.set(database=tmp_name).render_as_string(hide_password=False)
    tmp = load_settings({"DATABASE_URL": tmp_url})
    saved_env = dict(os.environ)

    failures: list[str] = []
    total = 0
    print(f"building throwaway database {tmp_name} ...")
    bootstrap_db.create(tmp)
    engine = sa.create_engine(tmp.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        from warehouse.ingest.youtube_verify import _migrate

        _migrate(tmp_url)

        for name, problems in scenarios(real_module, engine, tmp, bases).items():
            total += 1
            if problems:
                failures.append(name)
                print(f"  FAIL  {name}")
                for p in problems:
                    print(f"          {p}")
            else:
                print(f"  ok    {name}")

        print("mutations (each must make its matching check fail):")
        for label, edits, expect in MUTATIONS:
            total += 1
            mutant = _mutant(edits)
            caught = bool(scenarios(mutant, engine, tmp, bases)[expect])
            if caught:
                print(f"  ok    caught: {label}")
            else:
                failures.append(f"mutation survived: {label}")
                print(f"  FAIL  NOT caught: {label}")
    finally:
        engine.dispose()
        bootstrap_db.drop(tmp, yes=True)
        server.shutdown()
        os.environ.clear()
        os.environ.update(saved_env)

    print()
    if failures:
        print(f"FAILED {len(failures)}/{total} checks: {'; '.join(failures)}")
        return 1
    print(f"all {total} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
