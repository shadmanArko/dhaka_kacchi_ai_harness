# ruff: noqa: E501
# (Dense test fixtures: long literal data and assertion messages read better unwrapped.)
"""Prove warehouse.ingest.posthog_web and posthog_sanitize, WITHOUT a PostHog
account and without touching your data.

Same idiom as the other *_verify.py scripts: plain ok/FAIL output and an exit
code, a throwaway database that is created and dropped.

THE CENTRAL IDEA: the fake PostHog serves events deliberately stuffed with
SECRETS - a password-reset token, an order id, ad click ids, a customer id, a
device id, a city, a postal code, coordinates, an email, a user agent, button
text, credentials inside a URL. After a run, every one of those strings is
searched for in everything the ingester stored. Finding one is a failure. So the
privacy claim is tested by looking for the actual secrets, not by asserting that
a function was called.

The fake reproduces what the real PostHog was observed to do on 2026-10-07: it
REFUSES `OFFSET` for personal keys, supports the tuple-keyset query the ingester
sends, answers 401/403/404/429 with the real shapes, and returns `properties` as
JSON text.

IT DOES NOT PROVE the ingester is right about the REAL PostHog - run
`make ingest-posthog-dry-run` to see that. It does prove the logic is right
about the observed shapes, and that the scrubbing holds against hostile input.

Run with `make verify-ingest-posthog`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sqlalchemy as sa

from warehouse import bootstrap_db
from warehouse.config import REPO_ROOT, ConfigError, PostHogSourceSettings, load_settings
from warehouse.ingest import posthog_sanitize as ps
from warehouse.ingest import posthog_web as pw
from warehouse.ingest.posthog_web import ExtractionError

API_KEY = "phx_FAKEKEYDONOTLEAK0123456789abcdef"
SALT = "0123456789abcdef0123456789abcdef0123456789abcdef"
PROJECT = "424242"

# --- the secrets: every one of these must be ABSENT from everything stored -----
TOKEN = "RESETTOKEN-7f3a9c"
ORDER = "ord_PLANTED_8841"
FBCLID = "FBCLID-PLANTED-5521"
CUST = "cust_5ecretf00dbabe-aaaa-bbbb-cccc-000000000001"
DEVICE = "DEVICE-PLANTED-0042"
WINDOW = "WINDOW-PLANTED-0043"
CITY = "Plantedcityname"
POSTAL = "PLZ-PLANTED-90210"
LAT, LON = 52.123456, 13.654321
EMAIL = "planted.person@example.com"
USER_AGENT = "Mozilla/5.0 PLANTED-USER-AGENT-STRING"
EL_TEXT = "PLANTED-BUTTON-TEXT-Qx9"
CREDS = "hunter2planted"
WA_TEXT = "PLANTED-WHATSAPP-PREFILL"
SECRETS = [
    TOKEN, ORDER, FBCLID, CUST, DEVICE, WINDOW, CITY, POSTAL, str(LAT), str(LON), EMAIL,
    USER_AGENT, EL_TEXT, CREDS, WA_TEXT,
]  # fmt: skip

NOW0 = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
FIRST_DAY = datetime(2026, 9, 22, 8, 0, 0, tzinfo=UTC)
TIE_TIME = datetime(2026, 10, 3, 10, 0, 0, tzinfo=UTC)
TIE_SIZE = 250
PAGE = 100  # small, so a few hundred events force many pages

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
# The dataset
# ---------------------------------------------------------------------------

PEOPLE = [
    *[f"1111111{i}-aaaa-4bbb-8ccc-00000000000{i}" for i in range(5)],  # anonymous visitors
    "cookieless_PLANTEDCOOKIELESS01",
    CUST,
]
PAGES = ["/", "/order/", "/de/", "/de/order/", "/about", "/orders", "/de/about/", "/history"]


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def berlin_day(moment: datetime) -> date:
    """Berlin local day. The fixture stays inside summer time (UTC+2) on purpose,
    so the expectation needs no timezone database and is independent of the code
    under test."""
    return (moment + timedelta(hours=2)).date()


def is_admin(event: dict) -> bool:
    return str(event["properties"].get("$pathname", "")).startswith("/admin")


class Data:
    events: list[dict] = []
    counter = 0

    @classmethod
    def add(cls, event: str, when: datetime, person: str, props: dict, *, lag: int = 5,
            created: datetime | None = None) -> dict:  # fmt: skip
        cls.counter += 1
        n = cls.counter
        merged = {
            "$device_id": DEVICE,
            "$window_id": WINDOW,
            "$raw_user_agent": USER_AGENT,
            "$geoip_city_name": CITY,
            "$geoip_postal_code": POSTAL,
            "$geoip_latitude": LAT,
            "$geoip_longitude": LON,
            "$geoip_country_code": "DE",
            "$geoip_subdivision_1_name": "Berlin",
            "$el_text": EL_TEXT,
            "fbclid": FBCLID,
            "$fbc": f"fb.1.1.{FBCLID}",
            "$set": {"$current_url": f"https://dhakakacchi.com/reset-password?token={TOKEN}", "email": EMAIL},
            "$set_once": {"$initial_referrer": f"https://x.example/?token={TOKEN}"},
            "email": EMAIL,
            "customer_blob": {"id": CUST},
            "$browser": "Chrome",
            "$os": "Mac OS X",
            "$device_type": "Desktop",
            "$web_vitals_LCP_value": 1234.5,
            **props,
        }  # fmt: skip
        if person == CUST:
            merged["$user_id"] = CUST
            merged["$anon_distinct_id"] = PEOPLE[0]
        record = {
            "uuid": f"{n:08x}-0000-4000-8000-{n:012x}",
            "event": event,
            "timestamp": when,
            "created_at": created or (when + timedelta(seconds=lag)),
            "distinct_id": person,
            "properties": merged,
        }
        cls.events.append(record)
        return record


def pageview(when: datetime, person: str, path: str, *, landing: bool, utm: bool, session: str):
    base = f"https://dhakakacchi.com{path}"
    if landing:
        url = f"{base}?token={TOKEN}&order={ORDER}&fbclid={FBCLID}"
        url += "&utm_source=instagram&utm_content=bio_link" if utm else ""
        url += f"#frag-{TOKEN}"
        props = {
            "$current_url": url,
            "$referrer": f"https://l.instagram.com/?u=x&token={TOKEN}" if utm else "$direct",
            "$referring_domain": "l.instagram.com" if utm else "$direct",
        }
        if utm:
            props.update(
                {"utm_source": "instagram", "utm_content": "bio_link", "utm_medium": "social"}
            )
    else:
        props = {
            "$current_url": base,
            "$referrer": "https://dhakakacchi.com/",
            "$referring_domain": "dhakakacchi.com",
        }
    props.update({"$pathname": path, "$session_id": session, "title": "Dhaka Kacchi Berlin"})
    return Data.add("$pageview", when, person, props)


def build_dataset() -> None:
    Data.events, Data.counter = [], 0
    for day in range(15):  # 22 Sep .. 6 Oct
        for pi, person in enumerate(PEOPLE):
            session = f"sess-{pi}-{day}"
            start = FIRST_DAY + timedelta(days=day, minutes=pi * 7)
            pageview(
                start,
                person,
                PAGES[(day + pi) % len(PAGES)],
                landing=True,
                utm=pi % 2 == 0,
                session=session,
            )
            pageview(start + timedelta(minutes=2), person, PAGES[(day + pi + 3) % len(PAGES)],
                     landing=False, utm=False, session=session)  # fmt: skip
            if pi >= 5 or day % 3 == 0:
                Data.add("order_cart_started", start + timedelta(minutes=3), person,
                         {"$pathname": "/order/", "$session_id": session, "fulfillmentType": "delivery"})  # fmt: skip
            if pi == 6 and day % 2 == 0:
                Data.add("order_submitted", start + timedelta(minutes=5), person,
                         {"$pathname": "/order/", "$session_id": session, "totalCents": 3000,
                          "itemCount": 2, "fulfillmentType": "pickup"})  # fmt: skip
            if pi == 6:
                Data.add("order_delivery_fee_quoted", start + timedelta(minutes=4), person,
                         {"$pathname": "/order/", "$session_id": session, "feeCents": 350, "distanceKm": 4.2})  # fmt: skip

    # The owner's own admin traffic: must never be stored.
    for day in range(5):
        Data.add("$pageview", FIRST_DAY + timedelta(days=day, hours=3), PEOPLE[1],
                 {"$pathname": "/admin/reporting", "$current_url": "https://dhakakacchi.com/admin/reporting",
                  "$session_id": f"admin-{day}"})  # fmt: skip

    # Same received-at instant for a whole crowd: forces paging to split a tie.
    for i in range(TIE_SIZE):
        Data.add("$autocapture", TIE_TIME - timedelta(seconds=i + 1), PEOPLE[i % 6],
                 {"$pathname": "/order/", "$session_id": f"sess-tie-{i % 6}"}, created=TIE_TIME)  # fmt: skip

    # A Berlin-midnight boundary: 22:30 UTC is 00:30 the NEXT day in Berlin.
    pageview(
        datetime(2026, 10, 4, 22, 30, tzinfo=UTC),
        PEOPLE[0],
        "/",
        landing=True,
        utm=False,
        session="sess-edge-a",
    )
    pageview(
        datetime(2026, 10, 4, 21, 59, 59, tzinfo=UTC),
        PEOPLE[1],
        "/",
        landing=True,
        utm=False,
        session="sess-edge-b",
    )

    # Credentials inside a URL, and a WhatsApp link with prefilled text.
    Data.add("$autocapture", FIRST_DAY + timedelta(days=2, hours=4), PEOPLE[2], {
        "$pathname": "/about", "$session_id": "sess-wa",
        "$external_click_url": f"https://user:{CREDS}@wa.me/4915500000000?text={WA_TEXT}%20{ORDER}",
    })  # fmt: skip

    # Too recent: PostHog may still be ingesting these, so a run must leave them.
    for i in range(3):
        pageview(
            NOW0 - timedelta(seconds=40 + i),
            PEOPLE[3],
            "/",
            landing=False,
            utm=False,
            session="sess-recent",
        )
        Data.events[-1]["created_at"] = NOW0 - timedelta(seconds=30)


def landed(now: datetime, *, upto: datetime | None = None) -> list[dict]:
    cutoff = upto or (now - pw.SETTLE)
    return [e for e in Data.events if e["created_at"] <= cutoff and not is_admin(e)]


# ---------------------------------------------------------------------------
# What the summaries SHOULD say, computed independently of the ingester
# ---------------------------------------------------------------------------


def split(path: str) -> tuple[str, str]:
    match = re.match(r"^/([a-z]{2})(/|$)", path)
    locale = match.group(1) if match else "en"
    stripped = re.sub(r"^/[a-z]{2}(?=/|$)", "", path)
    return locale, ("/" if stripped in ("", "/") else stripped.rstrip("/"))


def expected(events: list[dict]) -> dict[str, dict]:
    pv = [e for e in events if e["event"] == "$pageview"]
    traffic, pages, acq, ev = {}, {}, {}, {}

    def bucket(table: dict, key, e) -> None:
        cell = table.setdefault(key, {"n": 0, "s": set(), "v": set()})
        cell["n"] += 1
        cell["s"].add(e["properties"].get("$session_id"))
        cell["v"].add(e["distinct_id"])

    for e in pv:
        day = berlin_day(e["timestamp"])
        locale, norm = split(e["properties"].get("$pathname", ""))
        bucket(traffic, (day, locale), e)
        bucket(pages, (day, locale, norm), e)
    first: dict[str, dict] = {}
    for e in sorted(pv, key=lambda x: x["timestamp"]):
        first.setdefault(e["properties"]["$session_id"], e)
    for e in first.values():
        p = e["properties"]
        ref = p.get("$referring_domain") or ""
        key = (berlin_day(e["timestamp"]), p.get("utm_source") or "", p.get("utm_medium") or "",
               p.get("utm_campaign") or "", p.get("utm_content") or "", "" if ref in ("", "$direct") else ref)  # fmt: skip
        acq[key] = acq.get(key, 0) + 1
    for e in events:
        if not e["event"].startswith("$"):
            bucket(ev, (berlin_day(e["timestamp"]), e["event"]), e)

    flat = lambda t: {k: (c["n"], len(c["s"] - {None}), len(c["v"])) for k, c in t.items()}  # noqa: E731
    return {
        "web_traffic_daily": flat(traffic),
        "web_page_daily": flat(pages),
        "web_acquisition_daily": acq,
        "web_event_daily": flat(ev),
    }


SELECTS = {
    "web_traffic_daily": ("SELECT day, locale, pageviews, sessions, visitors FROM web_traffic_daily", 2),
    "web_page_daily": ("SELECT day, locale, path, pageviews, sessions, visitors FROM web_page_daily", 3),
    "web_acquisition_daily": (
        "SELECT day, utm_source, utm_medium, utm_campaign, utm_content, referring_domain, sessions "
        "FROM web_acquisition_daily", 6),
    "web_event_daily": ("SELECT day, event_name, events, sessions, visitors FROM web_event_daily", 2),
}  # fmt: skip


def actual(engine: sa.Engine) -> dict[str, dict]:
    out = {}
    with engine.connect() as conn:
        for table, (sql, width) in SELECTS.items():
            rows = conn.execute(sa.text(sql)).all()
            out[table] = {
                tuple(r[:width]): (tuple(r[width:]) if len(r) - width > 1 else r[width])
                for r in rows
            }
    return out


def diff(want: dict[str, dict], got: dict[str, dict]) -> list[str]:
    problems = []
    for table in want:
        if want[table] != got[table]:
            missing = set(want[table]) - set(got[table])
            extra = set(got[table]) - set(want[table])
            wrong = [
                k for k in set(want[table]) & set(got[table]) if want[table][k] != got[table][k]
            ]
            problems.append(
                f"{table}: {len(missing)} missing, {len(extra)} unexpected, {len(wrong)} wrong "
                f"(e.g. {(sorted(wrong) or sorted(missing) or sorted(extra, key=str))[:1]})"
            )
    return problems


# ---------------------------------------------------------------------------
# The fake PostHog
# ---------------------------------------------------------------------------


class Fake:
    mode = "ok"  # ok | 403 | 429 | 500 | stuck | fail_third | ignore_admin_filter
    queries: list[str] = []
    calls = 0


def _parse_literal(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)


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
        sql = json.loads(self.rfile.read(length))["query"]["query"]
        Fake.calls += 1

        if self.headers.get("Authorization") != f"Bearer {API_KEY}":
            return self._send(
                401, {"type": "authentication_error", "detail": "Invalid personal API key."}
            )
        if self.path != f"/api/projects/{PROJECT}/query/":
            return self._send(404, {"detail": "Not found."})
        if Fake.mode in ("403", "429", "500"):
            return self._send(int(Fake.mode), {"detail": f"simulated {Fake.mode} for {API_KEY}"})
        if Fake.mode == "fail_third" and Fake.calls == 3:
            return self._send(500, {"detail": "backend error"})
        Fake.queries.append(sql)
        if "offset" in sql.lower():
            return self._send(
                400, {"detail": "OFFSET is not supported on queries made with a personal API key."}
            )
        if "parseDateTime64BestEffort" in sql:
            return self._send(
                400, {"detail": "Unsupported function call 'parseDateTime64BestEffort(...)'."}
            )

        m = re.search(
            r"\(created_at, toString\(uuid\)\) > \(toDateTime64\('([^']+)', 6, 'UTC'\), '([^']*)'\)",
            sql,
        )
        u = re.search(r"created_at <= toDateTime64\('([^']+)', 6, 'UTC'\)", sql)
        limit = int(re.search(r"limit (\d+)", sql).group(1))
        if not (m and u):
            return self._send(400, {"detail": "the fake does not understand this query shape"})
        after = (_parse_literal(m.group(1)), m.group(2))
        until = _parse_literal(u.group(1))

        rows = sorted(Data.events, key=lambda e: (e["created_at"], e["uuid"]))
        if Fake.mode != "stuck":
            rows = [e for e in rows if (e["created_at"], e["uuid"]) > after]
        rows = [e for e in rows if e["created_at"] <= until]
        if "'/admin%'" in sql and Fake.mode != "ignore_admin_filter":
            rows = [e for e in rows if not is_admin(e)]
        rows = rows[:limit]
        self._send(200, {"columns": [], "results": [
            [e["uuid"], e["event"], iso(e["timestamp"]), iso(e["created_at"]), e["distinct_id"],
             json.dumps(e["properties"])] for e in rows
        ]})  # fmt: skip


def _reset() -> None:
    Fake.mode = "ok"
    Fake.queries = []
    Fake.calls = 0


# ---------------------------------------------------------------------------
# Throwaway database
# ---------------------------------------------------------------------------


def _migrate(db_url: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT, env={**os.environ, "DATABASE_URL": db_url}, capture_output=True, text=True,
    )  # fmt: skip
    if proc.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed:\n{proc.stderr[-1500:]}")


def counts(engine: sa.Engine) -> dict[str, int]:
    tables = ["raw_posthog_events", *SELECTS]
    with engine.connect() as conn:
        return {t: conn.execute(sa.text(f"SELECT count(*) FROM {t}")).scalar() for t in tables}


def stored_text(engine: sa.Engine) -> str:
    with engine.connect() as conn:
        return "\n".join(
            r[0] for r in conn.execute(sa.text("SELECT payload::text FROM raw_posthog_events"))
        )


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def sanitizer_checks() -> None:
    u = ps.scrub_url
    problems = []
    cases = {
        f"https://x.com/a?token={TOKEN}": "https://x.com/a",
        "https://x.com/a?order=1&utm_source=ig&fbclid=2": "https://x.com/a?utm_source=ig",
        f"https://x.com/a#token={TOKEN}": "https://x.com/a",
        f"https://u:{CREDS}@x.com:8080/a?utm_term=k": "https://x.com:8080/a?utm_term=k",
        "https://x.com/de/order/": "https://x.com/de/order/",
        "$direct": "$direct",
        "/relative?token=1#f": "/relative",
        "": "",
    }
    for raw, want in cases.items():
        got = u(raw)
        if got != want:
            problems.append(f"{raw!r} -> {got!r}, want {want!r}")
    _check(
        "URLs keep only utm_* parameters; fragments and credentials go; clean URLs are untouched",
        problems,
    )

    a, b = ps.pseudonym(SALT, CUST), ps.pseudonym(SALT, "someone-else")
    problems = []
    if not re.fullmatch(r"ph_[0-9a-f]{32}", a):
        problems.append(f"unexpected pseudonym shape {a!r}")
    if a != ps.pseudonym(SALT, CUST):
        problems.append("not deterministic")
    if a == b:
        problems.append("two different ids collided")
    if a == ps.pseudonym(SALT + "x", CUST):
        problems.append("the salt does not change the result")
    if (
        CUST in a
        or a == "ph_" + __import__("hashlib").sha256((SALT + CUST).encode()).hexdigest()[:32]
    ):
        problems.append("looks like a bare hash or leaks the id")
    _check(
        "pseudonyms are keyed (HMAC), deterministic, salt-dependent and collision-free", problems
    )

    s = ps.sanitize_event(salt=SALT, uuid="u", event="e", timestamp="2026-10-07T10:00:00.000000Z",
                          created_at="2026-10-07T10:00:01.000000Z", distinct_id="d",
                          properties={"$pathname": "/administrator-fan", "kind": {"x": 1}, "title": "t" * 900,
                                      "never_seen": 1, "$sdk_debug_x": 1, "$fbc": "fb.1.1.z"})  # fmt: skip
    problems = []
    if s is None:
        problems.append("a non-admin path was excluded")
    else:
        if "never_seen" in s.payload["properties"]:
            problems.append("an unknown property survived the allowlist")
        if "kind" in s.payload["properties"]:
            problems.append("a nested value survived under an allowed name")
        if len(s.payload["properties"]["title"]) != ps.MAX_STRING_LENGTH:
            problems.append("an oversized string was not capped")
        if set(s.unreviewed) != {"never_seen", "kind (not a plain value)"}:
            problems.append(
                f"unreviewed report is {dict(s.unreviewed)} - noise and known-sensitive keys must stay out of it"
            )
    _check(
        "allowlist: unknown/nested values dropped and reported; SDK noise is not reported; strings capped",
        problems,
    )

    admin = ps.sanitize_event(salt=SALT, uuid="u", event="e", timestamp="2026-10-07T10:00:00.000000Z",
                              created_at="2026-10-07T10:00:00.000000Z", distinct_id="d",
                              properties={"$pathname": "/admin/orders"})  # fmt: skip
    via_url = ps.sanitize_event(salt=SALT, uuid="u", event="e", timestamp="2026-10-07T10:00:00.000000Z",
                                created_at="2026-10-07T10:00:00.000000Z", distinct_id="d",
                                properties={"$current_url": "https://dhakakacchi.com/admin/x?a=1"})  # fmt: skip
    _check(
        "/admin events are excluded, even when only the URL (not $pathname) reveals it",
        [] if admin is None and via_url is None else ["an admin event was kept"],
    )


def _summary() -> int:
    print()
    if _failures:
        print(f"FAILED {len(_failures)}/{_checks_run} checks: {', '.join(_failures)}")
        return 1
    print(f"all {_checks_run} checks passed")
    return 0


def run_checks(base: str) -> int:
    real = load_settings()
    name = f"dk_verify_ph_{os.getpid()}"
    url = real.database_url.set(database=name).render_as_string(hide_password=False)
    tmp = load_settings({"DATABASE_URL": url})
    src = PostHogSourceSettings(host=base, project_id=PROJECT, api_key=API_KEY, salt=SALT)
    say: list[str] = []

    def go(now: datetime = NOW0, source: PostHogSourceSettings = src, **kw) -> pw.RunResult:
        say.clear()
        kw.setdefault("page_size", PAGE)
        return pw.run(tmp, source, now=now, out=say.append, **kw)

    sanitizer_checks()

    print(f"building throwaway database {name} ...")
    bootstrap_db.create(tmp)
    engine = sa.create_engine(tmp.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        _migrate(url)
        build_dataset()
        _reset()
        want_events = landed(NOW0)

        # -- run 1 ------------------------------------------------------------------
        try:
            result = go(rebuild_days=90)
        except ExtractionError as exc:
            # e.g. the fail-closed tripwire refused to store something. Report it
            # as a failed check rather than a traceback, then stop: nothing after
            # this point means anything without the first run.
            _check("the first run completes and stores the events", [f"it stopped: {exc}"])
            return _summary()
        problems = []
        if result.new_events != len(want_events):
            problems.append(f"stored {result.new_events} events, expected {len(want_events)}")
        if result.excluded_admin != 0:
            problems.append(
                f"{result.excluded_admin} admin events reached the ingester; the query should filter them"
            )
        if len(Fake.queries) < 5:
            problems.append(f"only {len(Fake.queries)} request(s): paging not exercised")
        _check(
            f"pages through {len(want_events)} events, including {TIE_SIZE} sharing one received-at instant",
            problems,
        )

        problems = []
        if any("offset" in q.lower() for q in Fake.queries):
            problems.append("the ingester sent OFFSET, which PostHog refuses for personal keys")
        if not all("'/admin%'" in q for q in Fake.queries):
            problems.append("a query did not exclude /admin at the source")
        _check("every query is keyset-paged and filters /admin at the source", problems)

        problems = []
        with engine.connect() as conn:
            uuids = {
                r[0] for r in conn.execute(sa.text("SELECT event_uuid FROM raw_posthog_events"))
            }
        missing = {e["uuid"] for e in want_events} - uuids
        recent = {e["uuid"] for e in Data.events if e["created_at"] > NOW0 - pw.SETTLE}
        if missing:
            problems.append(f"{len(missing)} events lost")
        if uuids & recent:
            problems.append(
                "an event newer than the settle window was read before PostHog finished ingesting it"
            )
        if any(is_admin(e) and e["uuid"] in uuids for e in Data.events):
            problems.append("an /admin event was stored")
        _check("nothing lost, nothing too fresh read, no /admin event stored", problems)

        # -- the privacy claim --------------------------------------------------------
        text = stored_text(engine)
        problems = [f"STORED a planted secret: {s!r}" for s in SECRETS if s in text]
        _check(
            f"none of the {len(SECRETS)} planted secrets appears anywhere in what was stored",
            problems,
        )

        problems = []
        with engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT payload FROM raw_posthog_events")).scalars().all()
        ids = {p["distinct_id"] for p in rows}
        if not all(re.fullmatch(r"ph_[0-9a-f]{32}", i) for i in ids):
            problems.append("a stored visitor id is not a pseudonym")
        originals = {e["distinct_id"] for e in want_events}
        if len(ids) != len(originals):
            problems.append(
                f"{len(originals)} real visitors became {len(ids)} pseudonyms: hashing merged or split people"
            )
        cust_rows = [p for p in rows if p["properties"].get("$user_id")]
        if not cust_rows or any(
            p["distinct_id"] != ps.pseudonym(SALT, CUST)
            or p["properties"]["$user_id"] != ps.pseudonym(SALT, CUST)
            for p in cust_rows
        ):
            problems.append("the same customer got different pseudonyms in different fields")
        _check(
            "visitors become stable pseudonyms: nobody merged or split, a customer is one pseudonym everywhere",
            problems,
        )

        problems = []
        landing = [p for p in rows if p["properties"].get("utm_source") == "instagram"][0][
            "properties"
        ]
        for key in (
            "utm_source",
            "utm_content",
            "$geoip_country_code",
            "$browser",
            "$web_vitals_LCP_value",
        ):
            if key not in landing:
                problems.append(f"useful field {key} was dropped")
        if "$geoip_city_name" in landing or "$geoip_latitude" in landing or "$device_id" in landing:
            problems.append("a location or device field survived")
        for p in rows:
            for key, value in p["properties"].items():
                if (
                    isinstance(value, str)
                    and re.search(r"(url|referrer|href)$", key, re.I)
                    and "?" in value
                ):
                    stray = [
                        k
                        for k, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(value).query)
                        if not k.startswith("utm_")
                    ]
                    if stray:
                        problems.append(f"{key} still carries query parameter(s) {stray}")
                        break
        wa = [p["properties"] for p in rows if "$external_click_url" in p["properties"]]
        if not wa or wa[0]["$external_click_url"] != "https://wa.me/4915500000000":
            problems.append(f"credentials/prefill not stripped from the external link: {wa[:1]}")
        _check(
            "useful fields survive (utm, country, browser, speed); credentials and prefilled text do not",
            problems,
        )

        problems = []
        if not any("email" in line for line in say):
            problems.append(
                "an unreviewed property (email) was dropped silently instead of reported"
            )
        if not any("customer_blob" in line for line in say):
            problems.append("a nested unknown property was not reported")
        _check(
            "unreviewed dropped properties are REPORTED so a new field is noticed, not lost",
            problems,
        )

        # -- the summaries ------------------------------------------------------------
        problems = diff(expected(want_events), actual(engine))
        _check(
            "all four summary tables match an independent recount (Berlin days, locale, first-touch)",
            problems,
        )

        # -- idempotency and incremental behaviour -------------------------------------
        before = counts(engine)
        again = go()
        problems = []
        if again.new_events != 0:
            problems.append(f"a repeat run stored {again.new_events} duplicate(s)")
        if counts(engine) != before:
            problems.append("row counts changed on a no-op re-run")
        if again.fetched >= len(want_events) / 2:
            problems.append(
                f"re-run fetched {again.fetched} of {len(want_events)}: not incremental"
            )
        if again.fetched == 0:
            problems.append(
                "no overlap re-read at all: events received out of order could be missed"
            )
        _check(
            "a re-run stores nothing twice, is incremental, and still re-reads a small overlap",
            problems,
        )

        with engine.connect() as conn:
            mark = conn.execute(
                sa.text("SELECT max(source_created_at) FROM raw_posthog_events")
            ).scalar()
        Data.add("order_cart_started", mark - timedelta(minutes=8), PEOPLE[2],
                 {"$pathname": "/order/", "$session_id": "sess-ooo"}, created=mark - timedelta(minutes=3))  # fmt: skip
        result = go()
        _check(
            "an event received a few minutes BEFORE the saved position (out-of-order insert) is still caught",
            []
            if result.new_events == 1
            else [f"stored {result.new_events}, expected the 1 out-of-order event"],
        )

        # -- new + late events -----------------------------------------------------------
        now1 = NOW0 + timedelta(hours=1)
        late_ts = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)  # happened days ago...
        pageview(late_ts, PEOPLE[1], "/de/order/", landing=True, utm=False, session="sess-late")
        Data.events[-1]["created_at"] = now1 - timedelta(minutes=5)  # ...but only just arrived
        pageview(
            now1 - timedelta(minutes=20),
            PEOPLE[2],
            "/about",
            landing=True,
            utm=True,
            session="sess-new",
        )
        result = go(now=now1)
        problems = []
        if (
            result.new_events != 2 + 3
        ):  # the late one, the new one, and the 3 left behind by the settle window
            problems.append(
                f"stored {result.new_events} new events, expected 5 (late + new + 3 previously too fresh)"
            )
        problems += diff(expected(landed(now1)), actual(engine))
        _check(
            "a LATE-arriving event (old timestamp, new arrival) is caught and restates the day it belongs to",
            problems,
        )

        # -- rebuild safety, retention -------------------------------------------------------
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO web_traffic_daily(day, locale, pageviews, sessions, visitors) "
                                 "VALUES ('2026-10-01', 'zz', 9, 9, 9), ('2026-08-15', 'zz', 7, 7, 7)"))  # fmt: skip
            old = now1 - timedelta(days=pw.RETENTION_DAYS + 5)
            conn.execute(sa.text(
                "INSERT INTO raw_posthog_events(event_uuid, occurred_at, source_created_at, payload) "
                "VALUES ('ffffffff-old', :t, :t, '{}')"), {"t": old})  # fmt: skip
        # rebuild_days=90 reaches back BEFORE the oldest raw event (22 Sep), which is
        # exactly the case where a careless rebuild would erase history it cannot recompute.
        result = go(now=now1, rebuild_days=90)
        with engine.connect() as conn:
            stale = conn.execute(
                sa.text(
                    "SELECT count(*) FROM web_traffic_daily WHERE locale = 'zz' AND day = '2026-10-01'"
                )
            ).scalar()
            ancient = conn.execute(
                sa.text(
                    "SELECT count(*) FROM web_traffic_daily WHERE locale = 'zz' AND day = '2026-08-15'"
                )
            ).scalar()
            gone = conn.execute(
                sa.text("SELECT count(*) FROM raw_posthog_events WHERE event_uuid = 'ffffffff-old'")
            ).scalar()
        problems = []
        if stale:
            problems.append("a bogus summary row inside the rebuild window survived the rebuild")
        if not ancient:
            problems.append(
                "a summary for a day older than the raw events was DELETED by a rebuild (history lost)"
            )
        if gone or result.pruned != 1:
            problems.append(f"raw event past retention not pruned (pruned={result.pruned})")
        _check(
            "rebuild fixes its own window but never touches days whose raw events are gone; old raw is pruned",
            problems,
        )

        before = counts(engine)
        built = pw.build_only(tmp, rebuild_days=90, now=now1)
        _check(
            "--build-only rebuilds the summaries from stored events with no PostHog access",
            []
            if counts(engine) == before and sum(built.values()) > 0
            else [f"counts changed or nothing built: {built}"],
        )

        _reset()
        Fake.mode = "ignore_admin_filter"
        Data.add("$pageview", now1 + timedelta(minutes=10), PEOPLE[1],
                 {"$pathname": "/admin/reporting", "$session_id": "sess-adm"}, created=now1 + timedelta(minutes=11))  # fmt: skip
        raw_before = counts(engine)["raw_posthog_events"]
        result = go(now=now1 + timedelta(hours=1))
        with engine.connect() as conn:
            admin_rows = conn.execute(
                sa.text(
                    "SELECT count(*) FROM raw_posthog_events WHERE payload->'properties'->>'$pathname' LIKE '/admin%'"
                )
            ).scalar()
        Data.events.pop()
        problems = []
        if result.excluded_admin < 1:
            problems.append("the admin event was not even recognised")
        if admin_rows or counts(engine)["raw_posthog_events"] != raw_before:
            problems.append("an /admin event was stored when the source ignored the filter")
        _check(
            "the /admin guard also holds in the ingester itself, not just in the query", problems
        )

        # -- failure modes: loud, plain-language, nothing written, no key in the message -----------
        def fails(label: str, needle: str, *, mode: str = "ok", source: PostHogSourceSettings = src,
                  page_size: int = PAGE) -> None:  # fmt: skip
            _reset()
            Fake.mode = mode
            pageview(
                now1 + timedelta(minutes=1),
                PEOPLE[0],
                "/",
                landing=False,
                utm=False,
                session="sess-f",
            )
            Data.events[-1]["created_at"] = now1 + timedelta(minutes=2)  # something new to pull
            snap, problems = counts(engine), []
            try:
                go(now=now1 + timedelta(hours=1), source=source, page_size=page_size)
                problems.append("no ExtractionError raised")
            except ExtractionError as exc:
                text = str(exc)
                if needle not in text:
                    problems.append(f"message lacks {needle!r}: {text}")
                if API_KEY in text or SALT in text:
                    problems.append(f"message leaked a secret: {text}")
            if counts(engine) != snap:
                problems.append("rows were written despite the failure")
            _check(label, problems)
            Data.events.pop()

        bad_key = PostHogSourceSettings(
            host=base, project_id=PROJECT, api_key="phx_WRONGWRONGWRONG0123456789", salt=SALT
        )
        wrong_project = PostHogSourceSettings(host=base, project_id="1", api_key=API_KEY, salt=SALT)
        dead = PostHogSourceSettings(
            host="http://127.0.0.1:9", project_id=PROJECT, api_key=API_KEY, salt=SALT
        )
        fails(
            "a wrong/revoked key (401) says how to make a new one",
            "Personal API keys",
            source=bad_key,
        )
        fails(
            "a key without the query scope (403) says which scope", "'query' READ scope", mode="403"
        )
        fails(
            "a wrong project id (404) says to check the id and region",
            "POSTHOG_PROJECT_ID",
            source=wrong_project,
        )
        fails(
            "a rate limit (429) says nothing was lost and it will resume",
            "next run continues",
            mode="429",
        )
        fails(
            "a failure on page 3 writes NOTHING from pages 1-2",
            "backend error",
            mode="fail_third",
            page_size=1,
        )
        fails(
            "a server error that echoes the API key back is redacted before it is shown",
            "simulated 500",
            mode="500",
        )
        fails("PostHog being unreachable is a clear error", "unreachable", source=dead)

        _reset()
        Fake.mode = "stuck"
        problems = []
        try:
            go(now=now1 + timedelta(hours=1))
            problems.append("a server that ignores the cursor was not detected")
        except ExtractionError as exc:
            if "no progress" not in str(exc):
                problems.append(f"wrong message: {exc}")
        _check(
            "a server that keeps returning the same page cannot make the job loop forever", problems
        )

        # -- the fail-closed tripwire -----------------------------------------------------------------
        for label, field, bad in (
            ("an email address in an allowed field", "title", "write to oops.person@example.org"),
            ("a customer id in an allowed field", "kind", "cust_leaked123"),
            (
                "a token inside a free-text field the URL scrubber never looks at",
                "title",
                "see page?token=abc123",
            ),
        ):
            _reset()
            e = pageview(
                now1 + timedelta(minutes=3),
                PEOPLE[0],
                "/",
                landing=False,
                utm=False,
                session="sess-t",
            )
            e["properties"][field] = bad
            e["created_at"] = now1 + timedelta(minutes=4)
            snap, problems = counts(engine), []
            try:
                go(now=now1 + timedelta(hours=1))
                problems.append("a payload that still looked sensitive was stored")
            except ExtractionError as exc:
                if "refusing to store" not in str(exc):
                    problems.append(f"wrong message: {exc}")
                if bad.split()[-1] in str(exc):
                    problems.append("the tripwire message repeated the sensitive value")
            if counts(engine) != snap:
                problems.append("rows were written")
            Data.events.pop()
            _check(f"tripwire (fail closed): {label} stops the run and stores nothing", problems)

        # -- dry run ---------------------------------------------------------------------------------------
        _reset()
        snap = counts(engine)
        go(now=now1 + timedelta(hours=1), dry_run=True)
        problems = []
        if counts(engine) != snap:
            problems.append("a dry run wrote rows")
        if not any("would be kept" in s for s in say):
            problems.append("the dry run does not say what it would keep")
        _check(
            "--dry-run writes nothing and reports what it would keep and what it dropped", problems
        )
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
