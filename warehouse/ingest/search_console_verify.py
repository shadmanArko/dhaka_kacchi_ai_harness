# ruff: noqa: E501
# (Dense test fixtures: long literal data and assertion messages read better unwrapped.)
"""Prove warehouse.ingest.search_console and google_service_account, WITHOUT a
Google account and without touching your data.

Same idiom as the other *_verify.py scripts: plain ok/FAIL output, an exit code,
and a throwaway database that is created and dropped.

THE SIGN-IN IS CHECKED FOR REAL. The test generates a fresh RSA key pair, hands
the private half to the ingester as a service-account key, and the fake Google
VERIFIES the signed assertion with the public half: signature, issuer, the exact
read-only scope, audience, and a short lifetime. A signer that produced a
plausible-looking but wrong token would fail here, instead of passing because
nothing checked it.

The fake reproduces what the real Search Console API was observed to do on
2026-10-07: dataState "final" ends about three days back, rows are sparse, rows
page by startRow, a missing property is 404, an account that is not a user of the
property is 403, and the page/query reports are PARTIAL while the by-date report
carries the true totals.

IT DOES NOT PROVE the ingester is right about the REAL Google - `make
ingest-search-console-dry-run` does that. It proves the logic against the
observed shapes.

Run with `make verify-ingest-search-console`.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sqlalchemy as sa
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from warehouse import bootstrap_db
from warehouse.config import REPO_ROOT, ConfigError, SearchConsoleSourceSettings, load_settings
from warehouse.ingest import google_service_account as gsa
from warehouse.ingest import search_console as sc
from warehouse.ingest.search_console import ExtractionError, WindowError

SITE = "sc-domain:dhakakacchi.com"
EMAIL = "robot@fake-project.iam.gserviceaccount.com"
ACCESS = "FAKE-ACCESS-TOKEN-xyz"
TODAY = date(2026, 10, 7)
LAST_FINAL = TODAY - timedelta(days=3)  # observed: final data trails by about three days
FIRST_DAY = date(2026, 9, 14)  # the real property's first day

PAGES = [
    "https://dhakakacchi.com/",
    "https://dhakakacchi.com/about/",
    "https://dhakakacchi.com/de/order/",
]
COUNTRIES = ["deu", "bgd", "usa"]
DEVICES = ["MOBILE", "DESKTOP", "TABLET"]
QUERIES = ["dhaka kacchi", "what is kacchi biryani", "kacchi berlin", "mutton kacchi"]

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
# A real RSA key pair, and the fake Google that verifies signatures made with it
# ---------------------------------------------------------------------------

_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _PRIVATE.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
_PUBLIC = _PRIVATE.public_key()
PRIVATE_KEY_MARKER = PRIVATE_PEM.splitlines()[1][
    :30
]  # a slice of the key body: must never be shown


def key_json(token_uri: str, *, private_key: str = PRIVATE_PEM) -> str:
    return json.dumps(
        {
            "type": "service_account",
            "client_email": EMAIL,
            "private_key": private_key,
            "token_uri": token_uri,
        }
    )


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def value(*parts: object) -> int:
    return sum(map(ord, "|".join(map(str, parts))))


def rows_for(search_type: str, report: str) -> list[dict]:
    """The fake property's data for one report: deterministic and SPARSE (some
    days and some combinations have no row), and ending at the final-data lag."""
    dims = sc.REPORTS[report]
    out = []
    day = FIRST_DAY
    while day <= LAST_FINAL:
        d = day.isoformat()
        if report == "site_daily":
            combos = [()]
        elif report == "page_daily":
            combos = [(p, c, v) for p in PAGES for c in COUNTRIES for v in DEVICES]
        else:
            combos = [
                (q, p, c, v) for q in QUERIES for p in PAGES for c in COUNTRIES for v in DEVICES
            ]
        for combo in combos:
            h = value(search_type, report, d, *combo)
            if report != "site_daily" and h % 11 != 0:
                continue  # sparse: most combinations have no traffic
            impressions = (
                1 + h % 9 + (30 if report == "site_daily" else 0)
            )  # the site total is the larger, true figure
            clicks = min(impressions, h % 3)
            position = round(1 + (h % 83) / 7, 3)
            keys = [d, *combo]
            assert len(keys) == len(dims)
            out.append(
                {
                    "keys": keys,
                    "clicks": float(clicks),
                    "impressions": float(impressions),
                    "ctr": 0.0,
                    "position": position,
                }
            )
        day += timedelta(days=1)
    return out


class Fake:
    mode = "ok"
    bodies: list[dict] = []
    claims: dict = {}
    bump: dict[tuple, int] = {}  # (type, report, keys-tuple) -> extra impressions
    dropped: set[tuple] = set()  # (type, report, keys-tuple) removed from the response
    extra_rows: list[tuple[str, str, list]] = []
    calls = 0
    token_uri = ""


def served(search_type: str, report: str) -> list[dict]:
    rows = []
    for r in rows_for(search_type, report):
        key = (search_type, report, tuple(r["keys"]))
        if key in Fake.dropped:
            continue
        r = {**r, "impressions": r["impressions"] + Fake.bump.get(key, 0)}
        rows.append(r)
    for t, rep, keys in Fake.extra_rows:
        if (t, rep) == (search_type, report):
            rows.append(
                {"keys": keys, "clicks": 0.0, "impressions": 1.0, "ctr": 0.0, "position": 3.0}
            )
    return rows


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

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode()

        if self.path == "/token":
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
            head, claims, sig = form["assertion"].split(".")
            if Fake.mode == "bad_signature":
                return self._send(
                    400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."}
                )
            try:
                _PUBLIC.verify(
                    _unb64(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
                )
            except InvalidSignature:
                return self._send(
                    400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."}
                )
            Fake.claims = json.loads(_unb64(claims))
            Fake.claims["_alg"] = json.loads(_unb64(head))["alg"]
            return self._send(
                200, {"access_token": ACCESS, "expires_in": 3600, "token_type": "Bearer"}
            )

        Fake.calls += 1
        if self.headers.get("Authorization") != f"Bearer {ACCESS}":
            return self._send(401, {"error": {"code": 401, "message": "bad token"}})
        m = re.fullmatch(r"/webmasters/v3/sites/([^/]+)/searchAnalytics/query", self.path)
        if not m:
            return self._send(404, {"error": {"code": 404, "message": "no route"}})
        if urllib.parse.unquote(m.group(1)) != SITE:
            return self._send(404, {"error": {"code": 404, "message": "Site not found."}})
        if Fake.mode == "not_a_user":
            return self._send(
                403,
                {
                    "error": {
                        "code": 403,
                        "message": f"User does not have sufficient permission for site '{SITE}'.",
                    }
                },
            )
        if Fake.mode == "api_disabled":
            return self._send(
                403,
                {
                    "error": {
                        "code": 403,
                        "message": "Google Search Console API has not been used in project 1 before or it is disabled.",
                    }
                },
            )
        if Fake.mode in ("429", "500"):
            return self._send(
                int(Fake.mode), {"error": {"code": int(Fake.mode), "message": "simulated"}}
            )
        if Fake.mode == "fail_fourth" and Fake.calls == 4:
            return self._send(500, {"error": {"code": 500, "message": "backend error"}})

        body = json.loads(raw)
        Fake.bodies.append(body)
        if (
            body.get("dataState") != "final"
            or body["rowLimit"] > 25000
            or body["type"] not in ("web", "image")
        ):
            return self._send(
                400, {"error": {"code": 400, "message": "unsupported request in the fake"}}
            )
        report = next(r for r, d in sc.REPORTS.items() if d == body["dimensions"])
        start, end = date.fromisoformat(body["startDate"]), date.fromisoformat(body["endDate"])
        if Fake.mode == "empty_site" and report == "site_daily":
            return self._send(
                200, {"responseAggregationType": "byProperty"}
            )  # no "rows" key at all
        rows = [
            r
            for r in served(body["type"], report)
            if start <= date.fromisoformat(r["keys"][0]) <= end
        ]
        page = rows[body.get("startRow", 0) : body.get("startRow", 0) + body["rowLimit"]]
        self._send(
            200,
            {
                "rows": page,
                "responseAggregationType": "byProperty" if report == "site_daily" else "byPage",
            },
        )


def _reset() -> None:
    Fake.mode = "ok"
    Fake.bodies = []
    Fake.bump = {}
    Fake.dropped = set()
    Fake.extra_rows = []
    Fake.calls = 0


# ---------------------------------------------------------------------------
# Database + expectations
# ---------------------------------------------------------------------------

TABLES = {
    "site_daily": ("search_site_daily", ["day", "search_type"]),
    "page_daily": ("search_page_daily", ["day", "search_type", "page", "country", "device"]),
    "query_daily": (
        "search_query_daily",
        ["day", "search_type", "query", "page", "country", "device"],
    ),
}
SHAPE = {
    "site_daily": lambda k: (date.fromisoformat(k[0]),),
    "page_daily": lambda k: (date.fromisoformat(k[0]), k[1], k[2], k[3]),
    "query_daily": lambda k: (date.fromisoformat(k[0]), k[1], k[2], k[3], k[4]),
}


def expected(*, start: date, end: date) -> dict[str, dict]:
    out = {}
    for report, (_, _) in TABLES.items():
        table = {}
        for search_type in sc.SEARCH_TYPES:
            for r in served(search_type, report):
                day = date.fromisoformat(r["keys"][0])
                if start <= day <= end:
                    table[(search_type, *SHAPE[report](r["keys"]))] = (
                        int(r["clicks"]),
                        int(r["impressions"]),
                        Decimal(str(round(r["position"], 3))),
                    )
        out[report] = table
    return out


def actual(engine: sa.Engine, *, start: date, end: date) -> dict[str, dict]:
    out = {}
    with engine.connect() as conn:
        for report, (table, keys) in TABLES.items():
            cols = ["search_type", *[k for k in keys if k != "search_type"]]
            n = len(cols)
            rows = conn.execute(
                sa.text(
                    f"SELECT {', '.join(cols)}, clicks, impressions, position FROM {table} WHERE day BETWEEN :s AND :e"
                ),
                {"s": start, "e": end},
            ).all()
            out[report] = {tuple(r[:n]): (r[n], r[n + 1], Decimal(r[n + 2])) for r in rows}
    return out


def diff(want: dict, got: dict) -> list[str]:
    problems = []
    for report in want:
        w, g = want[report], got[report]
        if w != g:
            missing, extra = set(w) - set(g), set(g) - set(w)
            wrong = [k for k in set(w) & set(g) if w[k] != g[k]]
            problems.append(
                f"{report}: {len(missing)} missing, {len(extra)} unexpected, {len(wrong)} wrong (expected {len(w)} rows, found {len(g)})"
            )
    return problems


def counts(engine: sa.Engine) -> dict[str, int]:
    names = ["raw_search_console", *(t for t, _ in TABLES.values())]
    with engine.connect() as conn:
        return {n: conn.execute(sa.text(f"SELECT count(*) FROM {n}")).scalar() for n in names}


def _migrate(db_url: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT, env={**os.environ, "DATABASE_URL": db_url}, capture_output=True, text=True,
    )  # fmt: skip
    if proc.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{proc.stderr[-1500:]}")


def _summary() -> int:
    print()
    if _failures:
        print(f"FAILED {len(_failures)}/{_checks_run} checks: {', '.join(_failures)}")
        return 1
    print(f"all {_checks_run} checks passed")
    return 0


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def key_checks(base: str) -> None:
    uri = f"{base}/token"
    problems = []
    cases = {
        "not json": ("{nope", "not valid JSON"),
        "wrong kind of file": (
            json.dumps({"type": "authorized_user", "client_id": "x"}),
            "not a service-account key",
        ),
        "missing fields": (
            json.dumps({"type": "service_account", "client_email": "a@b"}),
            "missing",
        ),
        "token_uri over plain http to a stranger": (
            key_json("http://evil.example/token"),
            "must be https",
        ),
    }
    for label, (raw, needle) in cases.items():
        try:
            gsa.parse_key(raw, source="test")
            problems.append(f"{label}: accepted")
        except gsa.ServiceAccountError as exc:
            if needle not in str(exc):
                problems.append(f"{label}: message {exc} lacks {needle!r}")
    good = gsa.parse_key(key_json(uri), source="test")
    if PRIVATE_KEY_MARKER in repr(good) or PRIVATE_KEY_MARKER in str(good):
        problems.append("the private key shows up in repr(key)")
    _check(
        "a malformed or wrong-kind key file is refused with a plain message; repr never shows the key",
        problems,
    )

    mangled = gsa.ServiceAccountKey(
        EMAIL, "-----BEGIN PRIVATE KEY-----\nbm90IGEga2V5\n-----END PRIVATE KEY-----\n", uri
    )
    problems = []
    try:
        gsa.build_assertion(mangled, sc.SCOPE)
        problems.append("a garbage private key was accepted")
    except gsa.ServiceAccountError as exc:
        if "fresh key" not in str(exc) or "bm90IGEga2V5" in str(exc):
            problems.append(f"bad message or leaked key text: {exc}")
    _check("an unreadable private key says to download a fresh one and does not echo it", problems)

    w = sc.make_window
    problems = []
    if w(days=10, since=None, today=TODAY, has_data=False).start != TODAY - timedelta(
        days=sc.BACKFILL_DAYS
    ):
        problems.append("first run does not backfill ~16 months")
    later = w(days=10, since=None, today=TODAY, has_data=True)
    if (later.end, (later.end - later.start).days) != (TODAY - timedelta(days=1), 9):
        problems.append(f"later runs are not a 10-day window ending yesterday: {later}")
    try:
        w(days=10, since=TODAY, today=TODAY, has_data=True)
        problems.append("an impossible window was accepted")
    except WindowError:
        pass
    _check(
        "window: first run backfills everything available, later runs re-pull 10 days, nonsense is rejected",
        problems,
    )


def run_checks(base: str) -> int:
    real = load_settings()
    name = f"dk_verify_sc_{os.getpid()}"
    url = real.database_url.set(database=name).render_as_string(hide_password=False)
    tmp = load_settings({"DATABASE_URL": url})
    Fake.token_uri = f"{base}/token"
    source = SearchConsoleSourceSettings(
        SITE, key_json(Fake.token_uri), "TEST", f"{base}/webmasters/v3"
    )
    say: list[str] = []

    def go(src: SearchConsoleSourceSettings = source, **kw) -> sc.RunResult:
        say.clear()
        kw.setdefault("today", TODAY)
        return sc.run(tmp, src, out=say.append, **kw)

    key_checks(base)

    print(f"building throwaway database {name} ...")
    bootstrap_db.create(tmp)
    engine = sa.create_engine(tmp.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        _migrate(url)
        _reset()
        everything = (date(2020, 1, 1), TODAY)

        # -- the sign-in itself -----------------------------------------------------------
        try:
            go(row_limit=40)
        except ExtractionError as exc:
            # e.g. the fake rejected the signed assertion. Report it as a failed
            # check rather than a traceback, then stop: nothing later means anything.
            _check("the first run signs in and loads the data", [f"it stopped: {exc}"])
            return _summary()
        claims = Fake.claims
        problems = []
        if claims.get("_alg") != "RS256":
            problems.append(f"algorithm {claims.get('_alg')!r}")
        if claims.get("scope") != sc.SCOPE or not claims["scope"].endswith("webmasters.readonly"):
            problems.append(f"scope is {claims.get('scope')!r}: must be exactly the READ-ONLY one")
        if claims.get("iss") != EMAIL or claims.get("aud") != Fake.token_uri:
            problems.append("issuer or audience wrong")
        if not (0 < claims.get("exp", 0) - claims.get("iat", 0) <= 3600):
            problems.append("assertion lifetime is not within Google's one hour")
        if abs(claims.get("iat", 0) - datetime.now(UTC).timestamp()) > 300:
            problems.append("assertion not issued 'now'")
        _check(
            "the signed sign-in assertion VERIFIES against the public key, is read-only, and short-lived",
            problems,
        )

        # -- first run: backfill, paging, the data ----------------------------------------------
        problems = diff(
            expected(start=everything[0], end=everything[1]),
            actual(engine, start=everything[0], end=everything[1]),
        )
        if not Fake.bodies or max(b["startRow"] for b in Fake.bodies) == 0:
            problems.append("paging (startRow) was never exercised")
        _check(
            "first run loads every row of all six reports (web + image x site/page/query), through multiple pages",
            problems,
        )

        problems = []
        if any(b["dataState"] != "final" for b in Fake.bodies):
            problems.append("a request asked for provisional data")
        with engine.connect() as conn:
            newest = [
                conn.execute(sa.text(f"SELECT max(day) FROM {t}")).scalar()
                for t, _ in TABLES.values()
            ]
        if any(n != LAST_FINAL for n in newest):
            problems.append(
                f"newest stored day {newest}, want {LAST_FINAL} - never a day Google had not finalised"
            )
        if any(
            date.fromisoformat(r["keys"][0]) > LAST_FINAL
            for t in sc.SEARCH_TYPES
            for r in served(t, "site_daily")
        ):
            problems.append("fixture error")
        _check(
            "only FINAL data is requested and stored; the newest days are absent, never zero",
            problems,
        )

        problems = []
        with engine.connect() as conn:
            bad = conn.execute(
                sa.text(
                    "SELECT count(*) FROM search_site_daily WHERE position <> round(position, 3)"
                )
            ).scalar()
            one = conn.execute(sa.text("SELECT position FROM search_page_daily LIMIT 1")).scalar()
        if bad or not isinstance(one, Decimal):
            problems.append("position is not a fixed-scale decimal")
        _check("average position is stored as an exact decimal, never a float", problems)

        # -- idempotency and replacement -------------------------------------------------------------
        before = counts(engine)
        go()  # the trailing window now
        problems = diff(
            expected(start=everything[0], end=everything[1]),
            actual(engine, start=everything[0], end=everything[1]),
        )
        after = counts(engine)
        if {k: v for k, v in after.items() if k != "raw_search_console"} != {
            k: v for k, v in before.items() if k != "raw_search_console"
        }:
            problems.append(f"row counts changed on a no-op re-run: {before} -> {after}")
        _check("a re-run (trailing window) changes nothing", problems)

        recent = LAST_FINAL - timedelta(days=2)
        site_key = ("web", "site_daily", (recent.isoformat(),))
        page_rows = [
            r
            for r in rows_for("web", "page_daily")
            if r["keys"][0] >= (LAST_FINAL - timedelta(days=5)).isoformat()
        ]
        gone = ("web", "page_daily", tuple(page_rows[0]["keys"]))
        Fake.bump = {site_key: 500}
        Fake.dropped = {gone}
        go()
        problems = diff(
            expected(start=everything[0], end=everything[1]),
            actual(engine, start=everything[0], end=everything[1]),
        )
        with engine.connect() as conn:
            still = conn.execute(
                sa.text(
                    "SELECT count(*) FROM search_page_daily WHERE day = :d AND page = :p AND country = :c AND device = :v AND search_type='web'"
                ),
                {
                    "d": date.fromisoformat(gone[2][0]),
                    "p": gone[2][1],
                    "c": gone[2][2],
                    "v": gone[2][3],
                },
            ).scalar()
        if still:
            problems.append("a row Google stopped returning still exists here")
        _check(
            "a restated figure is REPLACED, and a row Google later drops stops existing here too",
            problems,
        )
        _reset()
        go()

        problems = []
        inside_day = LAST_FINAL - timedelta(days=1)
        outside_day = TODAY - timedelta(days=100)  # far outside the 10-day trailing window
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO search_site_daily(day, search_type, clicks, impressions, position) VALUES (:d, 'web', 7, 777, 1)"
                ),
                {"d": outside_day},
            )
            conn.execute(
                sa.text(
                    "UPDATE search_site_daily SET impressions = 8 WHERE search_type = 'web' AND day = :d"
                ),
                {"d": inside_day},
            )
        go()  # a trailing-window run
        with engine.connect() as conn:
            old = conn.execute(
                sa.text(
                    "SELECT impressions FROM search_site_daily WHERE search_type = 'web' AND day = :d"
                ),
                {"d": outside_day},
            ).scalar()
            inside = conn.execute(
                sa.text(
                    "SELECT impressions FROM search_site_daily WHERE search_type = 'web' AND day = :d"
                ),
                {"d": inside_day},
            ).scalar()
        if old != 777:
            problems.append(
                "a day OUTSIDE the trailing window was overwritten: older history must be left alone"
            )
        real = next(
            int(r["impressions"])
            for r in served("web", "site_daily")
            if r["keys"][0] == inside_day.isoformat()
        )
        if inside != real:
            problems.append(
                f"a day INSIDE the window kept a stale value ({inside}) instead of the real {real}"
            )
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM search_site_daily WHERE impressions = 777"))
        _check(
            "a trailing run replaces only its own window and never touches older history", problems
        )

        # -- oversize rows --------------------------------------------------------------------------------
        _reset()
        Fake.extra_rows = [
            ("web", "query_daily", [LAST_FINAL.isoformat(), "x" * 2500, PAGES[0], "deu", "MOBILE"])
        ]
        result = go()
        problems = []
        if result.skipped_oversize != 1:
            problems.append(f"skipped {result.skipped_oversize}, expected 1")
        if not any("skipped 1" in s for s in say):
            problems.append("the skip was silent")
        problems += diff(
            {
                k: v
                for k, v in expected(start=everything[0], end=everything[1]).items()
                if k != "query_daily"
            },
            {
                k: v
                for k, v in actual(engine, start=everything[0], end=everything[1]).items()
                if k != "query_daily"
            },
        )
        _check(
            "a row too long to index is skipped, counted and reported - it cannot crash the run",
            problems,
        )
        _reset()
        go()

        # -- the empty-answer guard -------------------------------------------------------------------------------
        Fake.mode = "empty_site"
        snap = counts(engine)
        problems = []
        try:
            go()
            problems.append("an empty site-level answer was accepted")
        except ExtractionError as exc:
            if "Refusing to replace" not in str(exc):
                problems.append(f"wrong message: {exc}")
        if counts(engine) != snap:
            problems.append("data was deleted despite the refusal")
        _check(
            "an EMPTY answer for a window that already has data refuses to replace it with nothing",
            problems,
        )
        _reset()

        # -- failures: loud, plain-language, nothing written, no key in the message -------------------------------------
        def fails(
            label: str, needle: str, *, mode: str = "ok", src: SearchConsoleSourceSettings = source
        ) -> None:
            _reset()
            Fake.mode = mode
            snap, problems = counts(engine), []
            try:
                go(src)
                problems.append("no ExtractionError raised")
            except ExtractionError as exc:
                text = str(exc)
                if needle not in text:
                    problems.append(f"message lacks {needle!r}: {text}")
                for secret in (PRIVATE_KEY_MARKER, "BEGIN PRIVATE KEY"):
                    if secret in text:
                        problems.append("the private key leaked into the message")
            if counts(engine) != snap:
                problems.append("rows were written despite the failure")
            _check(label, problems)

        fails(
            "an account that is not a user of the property says which email to add and where",
            "Users and permissions",
            mode="not_a_user",
        )
        fails("a disabled Search Console API says how to enable it", "Enable", mode="api_disabled")
        fails("a wrong property name (404) says it must match exactly", "SEARCH_CONSOLE_SITE_URL",
              src=SearchConsoleSourceSettings("sc-domain:wrong.example", source.key_json, "TEST", source.api_base))  # fmt: skip
        fails(
            "a rate limit says nothing was written and it will retry",
            "just let it retry",
            mode="429",
        )
        fails(
            "a server error on the 4th request writes NOTHING from requests 1-3",
            "backend error",
            mode="fail_fourth",
        )
        fails(
            "a revoked/invalid key says to make a new one",
            "revoked or deleted",
            mode="bad_signature",
        )
        fails("Google being unreachable is a clear error", "Could not reach Google",
              src=SearchConsoleSourceSettings(SITE, key_json("http://127.0.0.1:9/token"), "TEST", source.api_base))  # fmt: skip

        # -- dry run --------------------------------------------------------------------------------------------------------
        _reset()
        snap = counts(engine)
        go(dry_run=True)
        problems = []
        if counts(engine) != snap:
            problems.append("a dry run wrote rows")
        if not any(f"through {LAST_FINAL}" in s for s in say):
            problems.append("the dry run does not say how fresh the data is")
        _check("--dry-run writes nothing and reports the newest day it saw", problems)
    finally:
        engine.dispose()
        bootstrap_db.drop(tmp, yes=True)

    return _summary()


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
