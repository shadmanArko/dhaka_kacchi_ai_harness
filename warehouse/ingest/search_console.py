"""Ingest Google Search Console into the warehouse: how the site performs in
Google Search. See ARCHITECTURE.md section 4.7 and migration 0034.

Signs in as a Google SERVICE ACCOUNT (google_service_account.py): a robot identity
with a key file, added as a Restricted - i.e. read-only - user on the property in
Search Console. No browser, no human, no token that expires in a week.

======================================================================
VERIFIED against the real property sc-domain:dhakakacchi.com (2026-10-07):
  - The service account signs in and sees exactly one property, as
    "siteRestrictedUser".
  - POST /webmasters/v3/sites/{site}/searchAnalytics/query works for
    dimensions [date], [date,page,country,device] and
    [date,query,page,country,device], for type "web" and type "image".
  - dataState "final" ends about two days back; "all" runs up to yesterday but
    its newest days are provisional. This job stores FINAL only.
  - The [date] report is aggregated byProperty and the page reports byPage, and
    they are NOT interchangeable: the page and query tables held only 151 of the
    471 web impressions (32%) and 3 of 18 clicks, because Google omits rows too
    small or anonymized to publish. Only search_site_daily has true totals (see
    migration 0034).
  - Countries come back as lowercase ISO alpha-3 ('deu'); devices as DESKTOP /
    MOBILE / TABLET.
  - The property's history starts 2026-09-14, the day the site first appeared.
Not verified: behaviour at volume (paging past 25,000 rows is exercised only
against a fake), Search Console's rate limits, and types other than web/image.
======================================================================

HOW IT WORKS
  1. Sign in, then for each of {web, image} x {site, page, query} reports ask for
     the window, paging 25,000 rows at a time.
  2. Only when EVERY request has succeeded, replace that window in the three
     tables: delete the window, insert what came back. Replace, not append, so a
     row Google later drops stops existing here too; and all-or-nothing, so a
     half-fetched window can never pass for a complete one.
  3. The first run backfills about 16 months (the most the API keeps). Later runs
     re-pull the last 10 days, since figures are settled but occasionally
     adjusted.

TWO GUARDS AGAINST A BAD DAY. Replacing a window means a bad response could erase
good data, so:
  - If Google returns NO site-level rows for a window that already has some, the
    run stops without deleting anything. A property that had traffic last week did
    not have none this week; an empty answer is the failure, not the truth.
  - Rows whose query + page text is huge are skipped and counted, rather than
    crashing the run on Postgres's index-entry size limit.

Run with `make ingest-search-console`; preview with
`make ingest-search-console-dry-run`.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from warehouse.config import (
    ConfigError,
    SearchConsoleSourceSettings,
    Settings,
    load_search_console_source_settings,
    load_settings,
)
from warehouse.ingest.google_service_account import (
    ServiceAccountError,
    ServiceAccountKey,
    access_token,
    parse_key,
)
from warehouse.ingest.upsert import upsert_returning

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
REQUEST_TIMEOUT_S = 60

SEARCH_TYPES = ("web", "image")
ROW_LIMIT = 25000  # the API's own maximum per request
ROW_GUARD = 2_000_000  # a run this big is a bug or a very different site

TRAILING_DAYS = 10
BACKFILL_DAYS = 480  # about 16 months, the most the API retains

# Postgres cannot index an entry over ~2.7KB. Search queries are short and URLs
# rarely long, but one pathological row must not take the whole run down.
MAX_KEY_CHARS = 2000

REPORTS: dict[str, list[str]] = {
    "site_daily": ["date"],
    "page_daily": ["date", "page", "country", "device"],
    "query_daily": ["date", "query", "page", "country", "device"],
}

_INSERT_BATCH = 1500


class ExtractionError(RuntimeError):
    """The source read failed in a way the operator must act on. Never caught."""


class WindowError(ValueError):
    """The requested date window is impossible (a usage error)."""


@dataclass(frozen=True, slots=True)
class Window:
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class RunResult:
    raw_landed: int
    site_rows: int
    page_rows: int
    query_rows: int
    skipped_oversize: int


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def make_window(*, days: int, since: date | None, today: date, has_data: bool) -> Window:
    """Ends YESTERDAY: asking for more than exists is harmless (the API returns what
    it has), whereas guessing the lag would silently drop days if Google got faster."""
    end = today - timedelta(days=1)
    if since is not None:
        start = since
    elif not has_data:
        start = today - timedelta(days=BACKFILL_DAYS)  # first run: everything available
    else:
        start = end - timedelta(days=days - 1)
    if start > end:
        raise WindowError(f"window start {start} is after its end {end}")
    return Window(start=start, end=end)


def _explain(status: int, body: bytes, key: ServiceAccountKey, site_url: str) -> str:
    try:
        message = str(json.loads(body).get("error", {}).get("message", ""))
    except (ValueError, AttributeError):
        message = ""
    low = message.lower()
    if status == 403 and ("has not been used" in low or "is disabled" in low):
        return (
            "the Search Console API is not enabled in the Google Cloud project that owns the "
            "service account -> APIs & Services -> Library -> Google Search Console API -> Enable"
        )
    if status == 403:
        return (
            f"{key.client_email} is not a user of {site_url}. In Search Console: Settings -> "
            "Users and permissions -> Add user, with that email (Restricted is enough)."
        )
    if status == 404:
        return (
            f"{site_url} was not found. SEARCH_CONSOLE_SITE_URL must match the property exactly "
            "(sc-domain:yourdomain.com for a Domain property, https://yourdomain.com/ for a "
            "URL-prefix one)."
        )
    if status == 401:
        return "Google rejected the access token; check the service-account key is current."
    if status == 429:
        return (
            "Google is rate-limiting this account. Nothing was written; the next run re-pulls "
            "the same window, so just let it retry."
        )
    return message[:240] or f"HTTP {status}"


def _post(
    settings: SearchConsoleSourceSettings,
    key: ServiceAccountKey,
    token: str,
    body: dict,
) -> dict:
    site = urllib.parse.quote(settings.site_url, safe="")
    request = urllib.request.Request(
        f"{settings.api_base}/sites/{site}/searchAnalytics/query",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ExtractionError(
            f"Search Console error {exc.code}: "
            f"{_explain(exc.code, exc.read(), key, settings.site_url)}"
        ) from None
    except urllib.error.URLError as exc:
        raise ExtractionError(f"Search Console unreachable: {exc.reason}") from None


def extract(
    settings: SearchConsoleSourceSettings,
    key: ServiceAccountKey,
    window: Window,
    *,
    row_limit: int = ROW_LIMIT,
) -> dict[tuple[str, str], dict]:
    """{(report, search_type): {"dimensions": [...], "rows": [...]}}, paging each
    request until a short page says it is done."""
    try:
        token = access_token(key, SCOPE)
    except ServiceAccountError as exc:
        raise ExtractionError(str(exc)) from None

    out: dict[tuple[str, str], dict] = {}
    for search_type in SEARCH_TYPES:
        for report, dimensions in REPORTS.items():
            rows: list[dict] = []
            start_row = 0
            while True:
                page = (
                    _post(
                        settings,
                        key,
                        token,
                        {
                            "startDate": window.start.isoformat(),
                            "endDate": window.end.isoformat(),
                            "dimensions": dimensions,
                            "type": search_type,
                            # FINAL only: a provisional day looks like a bad day until it is
                            # restated, which is a trap for whoever reads the table later.
                            "dataState": "final",
                            "rowLimit": row_limit,
                            "startRow": start_row,
                        },
                    ).get("rows")
                    or []
                )
                rows.extend(page)
                if len(page) < row_limit:
                    break
                start_row += row_limit
                if len(rows) >= ROW_GUARD:
                    raise ExtractionError(
                        f"{report}/{search_type} returned over {ROW_GUARD} rows: stopping rather "
                        "than load something this unexpected. Narrow the window."
                    )
            out[(report, search_type)] = {"dimensions": dimensions, "rows": rows}
    return out


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------

raw_search_console = sa.table(
    "raw_search_console",
    sa.column("id"),
    sa.column("report"),
    sa.column("search_type"),
    sa.column("start_date"),
    sa.column("end_date"),
    sa.column("payload"),
    sa.column("updated_at"),
)


def land_raw(conn: sa.Connection, pulled: dict[tuple[str, str], dict], window: Window) -> int:
    rows = [
        {
            "report": report,
            "search_type": search_type,
            "start_date": window.start,
            "end_date": window.end,
            "payload": json.dumps(payload),
            "updated_at": sa.func.now(),
        }
        for (report, search_type), payload in pulled.items()
    ]
    result = upsert_returning(
        conn,
        raw_search_console,
        rows,
        conflict_on=["report", "search_type", "start_date", "end_date"],
        update=["payload", "updated_at"],
        returning=["id"],
    )
    return len(result)


# ---------------------------------------------------------------------------
# Transform + load
# ---------------------------------------------------------------------------

_METRICS = ["clicks", "impressions", "position"]

_TABLES = {
    "site_daily": ("search_site_daily", ["day", "search_type"]),
    "page_daily": ("search_page_daily", ["day", "search_type", "page", "country", "device"]),
    "query_daily": (
        "search_query_daily",
        ["day", "search_type", "query", "page", "country", "device"],
    ),
}

_table_defs = {
    report: sa.table(
        name,
        *[sa.column(c) for c in [*keys, *_METRICS, "fetched_at", "updated_at"]],
    )
    for report, (name, keys) in _TABLES.items()
}


def _row(
    report: str, search_type: str, dimensions: list[str], raw: dict, fetched_at: datetime
) -> dict | None:
    """One typed row, or None for a row too large to index. Keyed by the DIMENSION
    NAMES the payload carries, never by position."""
    named = dict(zip(dimensions, raw["keys"], strict=True))
    if len(named.get("query", "")) + len(named.get("page", "")) > MAX_KEY_CHARS:
        return None
    row: dict = {
        "day": date.fromisoformat(named["date"]),
        "search_type": search_type,
        "clicks": int(round(raw["clicks"])),
        "impressions": int(round(raw["impressions"])),
        # An average ranking, so numeric with a fixed scale, never a float.
        "position": Decimal(str(round(raw["position"], 3))),
        "fetched_at": fetched_at,
        "updated_at": sa.func.now(),
    }
    for name in ("page", "query", "country", "device"):
        if name in named:
            row[name] = named[name]
    return row


def _insert(conn: sa.Connection, report: str, rows: list[dict]) -> int:
    _, keys = _TABLES[report]
    total = 0
    for start in range(0, len(rows), _INSERT_BATCH):
        total += len(
            upsert_returning(
                conn,
                _table_defs[report],
                rows[start : start + _INSERT_BATCH],
                conflict_on=keys,
                update=[*_METRICS, "fetched_at", "updated_at"],
                returning=["day"],
            )
        )
    return total


def transform_and_load(
    conn: sa.Connection, window: Window, *, fetched_at: datetime
) -> tuple[int, int, int, int]:
    """Replaces THIS window in the three tables, from the raw rows just landed for it."""
    payloads = {
        (report, search_type): payload
        for report, search_type, payload in conn.execute(
            sa.text(
                "SELECT report, search_type, payload FROM raw_search_console "
                "WHERE start_date = :s AND end_date = :e"
            ),
            {"s": window.start, "e": window.end},
        ).all()
    }

    # The guard: an empty answer for a window that already has data is a failure,
    # not a quiet week, and replacing the window would erase the good rows.
    for search_type in SEARCH_TYPES:
        returned = len(payloads.get(("site_daily", search_type), {}).get("rows", []))
        existing = conn.execute(
            sa.text(
                "SELECT count(*) FROM search_site_daily "
                "WHERE search_type = :t AND day BETWEEN :s AND :e"
            ),
            {"t": search_type, "s": window.start, "e": window.end},
        ).scalar()
        if returned == 0 and existing > 0:
            raise ExtractionError(
                f"Search Console returned no {search_type} rows for {window.start}..{window.end}, "
                f"but {existing} day(s) are already stored for that window. Refusing to replace "
                "them with nothing; if the property truly lost its data, delete the rows by hand."
            )

    counts = {"site_daily": 0, "page_daily": 0, "query_daily": 0}
    skipped = 0
    for search_type in SEARCH_TYPES:
        for report in REPORTS:
            table, _ = _TABLES[report]
            conn.execute(
                sa.text(f"DELETE FROM {table} WHERE search_type = :t AND day BETWEEN :s AND :e"),
                {"t": search_type, "s": window.start, "e": window.end},
            )
            payload = payloads.get((report, search_type))
            if not payload:
                continue
            rows = []
            for raw in payload["rows"]:
                typed = _row(report, search_type, payload["dimensions"], raw, fetched_at)
                if typed is None:
                    skipped += 1
                else:
                    rows.append(typed)
            counts[report] += _insert(conn, report, rows)
    return counts["site_daily"], counts["page_daily"], counts["query_daily"], skipped


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(
    settings: Settings,
    source: SearchConsoleSourceSettings,
    *,
    dry_run: bool = False,
    days: int = TRAILING_DAYS,
    since: date | None = None,
    today: date | None = None,
    row_limit: int = ROW_LIMIT,
    out=print,
) -> RunResult:
    try:
        key = parse_key(source.key_json, source=source.key_source)
    except ServiceAccountError as exc:
        raise ConfigError(str(exc)) from None

    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            has_data = bool(
                conn.execute(sa.text("SELECT count(*) FROM search_site_daily")).scalar()
            )
        window = make_window(
            days=days, since=since, today=today or datetime.now(UTC).date(), has_data=has_data
        )
        pulled = extract(source, key, window, row_limit=row_limit)

        if dry_run:
            mode = (
                "first run: full backfill" if not has_data and since is None else "trailing window"
            )
            out(f"window {window.start} .. {window.end}  ({mode})")
            for (report, search_type), payload in pulled.items():
                out(f"  {report:<12} {search_type:<6} {len(payload['rows']):>6} row(s)")
            site = pulled[("site_daily", "web")]["rows"]
            if site:
                days_returned = sorted(r["keys"][0] for r in site)
                out(
                    f"web search: data from {days_returned[0]} through {days_returned[-1]}, "
                    f"{sum(r['clicks'] for r in site):.0f} click(s), "
                    f"{sum(r['impressions'] for r in site):.0f} impression(s)"
                )
            return RunResult(0, 0, 0, 0, 0)

        fetched_at = datetime.now(UTC)
        with engine.begin() as conn:
            landed = land_raw(conn, pulled, window)
            site_n, page_n, query_n, skipped = transform_and_load(
                conn, window, fetched_at=fetched_at
            )
    finally:
        engine.dispose()
    if skipped:
        out(f"note: skipped {skipped} row(s) whose query/page text was too long to index")
    return RunResult(landed, site_n, page_n, query_n, skipped)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="fetch and summarise; write nothing")
    parser.add_argument(
        "--days", type=int, default=TRAILING_DAYS, help=f"trailing window (default {TRAILING_DAYS})"
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
        source = load_search_console_source_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        result = run(settings, source, dry_run=args.dry_run, days=args.days, since=args.since)
    except (ConfigError, WindowError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except ExtractionError as exc:
        print(f"extraction error: {exc}", file=sys.stderr)
        return 3

    if not args.dry_run:
        print(
            f"landed {result.raw_landed} raw report(s); replaced the window with "
            f"{result.site_rows} site day(s), {result.page_rows} page row(s), "
            f"{result.query_rows} query row(s)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
