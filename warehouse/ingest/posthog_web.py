"""Ingest the website's PostHog events into the warehouse, scrubbed, and build
the daily web-traffic summaries from them. See ARCHITECTURE.md section 4.7 and
migration 0033.

READ warehouse/ingest/posthog_sanitize.py FIRST. Every event passes through it
before it is written, and what it keeps and drops is the privacy design of this
whole job. In one line: an allowlist of reviewed fields, visitor and customer ids
replaced by keyed hashes, URLs stripped to utm_* parameters, location cut to
country/region, and the owner's own /admin traffic excluded.

======================================================================
VERIFIED against the real Dhaka Kacchi PostHog project (2026-10-07, EU cloud,
project 274142; 3,026 events since 2026-09-14):
  - POST {host}/api/projects/{id}/query/ with a personal API key (scope: query
    read) and a HogQLQuery body works, and `uuid, event, timestamp, created_at,
    distinct_id, properties` can be selected. `properties` arrives as JSON text.
  - OFFSET IS REFUSED for personal API keys ("use keyset pagination"), so this
    pages by (created_at, uuid) with a tuple comparison, which is accepted.
  - toDateTime64('...', 6, 'UTC') is accepted and keeps microseconds;
    parseDateTime64BestEffort and timeZone() are NOT available.
  - `created_at` (when PostHog received the event) exists and trails `timestamp`
    by at most 123 seconds in this project. The watermark uses it, not
    `timestamp`, so a late-arriving event can never be skipped.
  - coalesce(properties.$pathname, '') not like '/admin%' works in the WHERE.
Not verified: behaviour on a project with far more events (paging is exercised
only against a fake), and PostHog's rate limits on the query endpoint.
======================================================================

THE FLOW, and why it is in this order:
  1. Pull every event PostHog received since the saved watermark (minus a small
     overlap, since PostHog can insert slightly out of order), page by page.
  2. Scrub each one. If any finished payload still LOOKS sensitive (a customer
     id, a token in a URL, an email address, a PostHog key), the run STOPS and
     writes nothing: the tripwire in posthog_sanitize.leak_check fails closed.
  3. Only if every page succeeded, write them (all-or-nothing, so a half-pulled
     range can never be mistaken for a complete one) in one transaction.
  4. Delete raw events older than 13 months. The summaries are kept.
  5. Rebuild the last 14 days of summaries from the raw events (delete the window,
     re-insert it), so a late event restates the day it belonged to.

Re-running is safe at any point: events are keyed on PostHog's own uuid, so the
overlap window and a repeated run insert nothing twice.

Run with `make ingest-posthog`; preview with `make ingest-posthog-dry-run`
(pulls and scrubs, writes nothing, prints what was dropped); summaries only, with
no PostHog access needed, with `make build-web-aggregates`.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from warehouse.config import (
    ConfigError,
    PostHogSourceSettings,
    Settings,
    load_posthog_source_settings,
    load_settings,
)
from warehouse.ingest.posthog_sanitize import Sanitized, leak_check, sanitize_event
from warehouse.ingest.upsert import upsert_returning

PAGE_SIZE = 1000
REQUEST_TIMEOUT_S = 60

# Re-read this much BEFORE the saved watermark each run. PostHog inserts events
# in batches, so two events can land with created_at slightly out of order across
# a run boundary. Repeats are harmless (unique event uuid); gaps are not.
OVERLAP = timedelta(minutes=10)

# Do not read events newer than this. They may still be mid-ingestion, and the
# overlap above catches them next run anyway.
SETTLE = timedelta(seconds=90)

# Raw events older than this are deleted. The summaries built from them are kept.
RETENTION_DAYS = 396  # 13 months

DEFAULT_REBUILD_DAYS = 14

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{0,36}$")


class ExtractionError(RuntimeError):
    """The source read failed in a way the operator must act on. Never caught."""


@dataclass(frozen=True, slots=True)
class RunResult:
    fetched: int
    excluded_admin: int
    new_events: int
    pruned: int
    summary_rows: dict[str, int]


@dataclass(frozen=True, slots=True)
class Pull:
    events: list[Sanitized]
    fetched: int
    excluded_admin: int
    unreviewed: Counter
    pages: int


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _literal(moment: datetime) -> str:
    """A UTC instant as a HogQL literal that keeps microseconds. toDateTime64 with
    an explicit zone, because an unzoned literal is read in the PROJECT's
    timezone and a mismatch would shift the watermark by hours."""
    return f"toDateTime64('{moment.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S.%f')}', 6, 'UTC')"


def build_query(after: datetime, after_uuid: str, until: datetime, limit: int) -> str:
    if not _UUID_RE.match(after_uuid):
        raise ValueError(f"unexpected uuid {after_uuid!r}")  # it is interpolated into SQL
    return (
        "select toString(uuid), event, timestamp, created_at, distinct_id, properties "
        "from events "
        f"where (created_at, toString(uuid)) > ({_literal(after)}, '{after_uuid}') "
        f"and created_at <= {_literal(until)} "
        # The owner's own admin traffic is not stored. Filtered here to save the
        # transfer, and AGAIN in sanitize_event, which does not trust this line.
        "and coalesce(properties.$pathname, '') not like '/admin%' "
        "order by created_at, toString(uuid) "
        f"limit {limit}"
    )


def _explain(status: int, body: bytes, project_id: str) -> str:
    try:
        detail = str(json.loads(body).get("detail", ""))
    except (ValueError, AttributeError):
        detail = ""
    hints = {
        401: "PostHog rejected the personal API key (mistyped, revoked or expired). Create a new "
        "one under your profile -> Personal API keys and update POSTHOG_PERSONAL_API_KEY.",
        403: f"the key is not allowed to run queries on project {project_id}. It needs the 'query' "
        "READ scope, and access to that project.",
        404: f"project {project_id} was not found on this host. Check POSTHOG_PROJECT_ID and that "
        "POSTHOG_HOST is the right region (eu.posthog.com vs us.posthog.com).",
        429: "PostHog is rate-limiting this key. Nothing was written; the next run continues from "
        "the saved position, so just let it retry.",
    }
    return hints.get(status) or detail[:240] or f"HTTP {status}"


def _query(settings: PostHogSourceSettings, sql: str) -> list[list]:
    request = urllib.request.Request(
        f"{settings.host}/api/projects/{settings.project_id}/query/",
        data=json.dumps({"query": {"kind": "HogQLQuery", "query": sql}}).encode(),
        headers={
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read())["results"]
    except urllib.error.HTTPError as exc:
        message = f"PostHog error {exc.code}: {_explain(exc.code, exc.read(), settings.project_id)}"
        raise ExtractionError(message.replace(settings.api_key, "***")) from None
    except urllib.error.URLError as exc:
        raise ExtractionError(f"PostHog unreachable: {exc.reason}") from None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def extract(
    settings: PostHogSourceSettings,
    *,
    after: datetime,
    until: datetime,
    page_size: int = PAGE_SIZE,
) -> Pull:
    """Every event received in (after, until], scrubbed. Pages by a (created_at,
    uuid) keyset, never OFFSET (which PostHog refuses for personal keys), and
    never holds an unscrubbed event past the loop iteration that read it."""
    events: list[Sanitized] = []
    unreviewed: Counter = Counter()
    fetched = excluded = pages = 0
    cursor_time, cursor_uuid = after, ""

    while True:
        rows = _query(settings, build_query(cursor_time, cursor_uuid, until, page_size))
        pages += 1
        for uuid, event, timestamp, created_at, distinct_id, properties in rows:
            fetched += 1
            props = json.loads(properties) if isinstance(properties, str) else (properties or {})
            clean = sanitize_event(
                salt=settings.salt,
                uuid=uuid,
                event=event,
                timestamp=timestamp,
                created_at=created_at,
                distinct_id=distinct_id or "",
                properties=props,
            )
            if clean is None:
                excluded += 1
                continue
            problem = leak_check(clean.payload)
            if problem:
                # Names the KIND and the event, never the value. Nothing has been
                # written yet, so stopping here stores nothing.
                raise ExtractionError(
                    f"refusing to store event {uuid} ({event}): after scrubbing it still "
                    f"contains {problem}. Fix posthog_sanitize.py before re-running."
                )
            unreviewed.update(clean.unreviewed)
            events.append(clean)

        if len(rows) < page_size:
            return Pull(events, fetched, excluded, unreviewed, pages)
        last = rows[-1]
        next_time, next_uuid = _parse_time(last[3]), last[0]
        if (next_time, next_uuid) <= (cursor_time, cursor_uuid):
            raise ExtractionError("paging made no progress; stopping rather than looping forever")
        cursor_time, cursor_uuid = next_time, next_uuid


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------

raw_posthog_events = sa.table(
    "raw_posthog_events",
    sa.column("id"),
    sa.column("event_uuid"),
    sa.column("occurred_at"),
    sa.column("source_created_at"),
    sa.column("payload"),
)

_INSERT_BATCH = 1000


def land_raw(conn: sa.Connection, events: list[Sanitized]) -> int:
    """Returns how many were NEW. ON CONFLICT DO NOTHING (an event is immutable),
    so the overlap window and a repeated run insert nothing twice."""
    new = 0
    for start in range(0, len(events), _INSERT_BATCH):
        rows = [
            {
                "event_uuid": e.event_uuid,
                "occurred_at": e.occurred_at,
                "source_created_at": e.source_created_at,
                "payload": json.dumps(e.payload),
            }
            for e in events[start : start + _INSERT_BATCH]
        ]
        new += len(
            upsert_returning(
                conn, raw_posthog_events, rows, conflict_on=["event_uuid"], update=None
            )
        )
    return new


def prune(conn: sa.Connection, *, now: datetime) -> int:
    result = conn.execute(
        sa.text("DELETE FROM raw_posthog_events WHERE occurred_at < :cutoff"),
        {"cutoff": now - timedelta(days=RETENTION_DAYS)},
    )
    return result.rowcount


def watermark(conn: sa.Connection) -> tuple[datetime, str] | None:
    row = conn.execute(
        sa.text(
            "SELECT source_created_at, event_uuid FROM raw_posthog_events "
            "ORDER BY source_created_at DESC, event_uuid DESC LIMIT 1"
        )
    ).first()
    return (row[0], row[1]) if row else None


# ---------------------------------------------------------------------------
# Summaries, built from the raw events
# ---------------------------------------------------------------------------

# One CTE shared by all four builds. Days are BERLIN days. The two-letter path
# prefix is the locale ('en' if absent) and is stripped, so /de/order and /order
# are one page in two languages. The window starts one day early so a session
# that began the evening before is still seen from its true first pageview.
_BASE = """
WITH ev AS (
    SELECT occurred_at,
           (occurred_at AT TIME ZONE 'Europe/Berlin')::date AS day,
           payload->>'event' AS event,
           payload->>'distinct_id' AS visitor,
           payload->'properties'->>'$session_id' AS session,
           payload->'properties' AS props,
           coalesce(payload->'properties'->>'$pathname', '') AS path
    FROM raw_posthog_events
    WHERE occurred_at >= (((CAST(:from_day AS date) - 1)::timestamp) AT TIME ZONE 'Europe/Berlin')
), e AS (
    SELECT ev.*,
           coalesce((regexp_match(path, '^/([a-z]{2})(/|$)'))[1], 'en') AS locale,
           CASE
               WHEN regexp_replace(path, '^/[a-z]{2}(?=/|$)', '') IN ('', '/') THEN '/'
               ELSE regexp_replace(regexp_replace(path, '^/[a-z]{2}(?=/|$)', ''), '/+$', '')
           END AS path_norm
    FROM ev
)
"""

_BUILDS = {
    "web_traffic_daily": """
        INSERT INTO web_traffic_daily (day, locale, pageviews, sessions, visitors)
        SELECT day, locale, count(*), count(DISTINCT session), count(DISTINCT visitor)
        FROM e WHERE event = '$pageview' AND day >= :from_day
        GROUP BY day, locale""",
    "web_page_daily": """
        INSERT INTO web_page_daily (day, locale, path, pageviews, sessions, visitors)
        SELECT day, locale, path_norm, count(*), count(DISTINCT session), count(DISTINCT visitor)
        FROM e WHERE event = '$pageview' AND day >= :from_day
        GROUP BY day, locale, path_norm""",
    # A session is attributed by its FIRST pageview: a later in-site pageview has
    # the site itself as its referrer and would misattribute the whole visit.
    "web_acquisition_daily": """
        , first_pv AS (
            SELECT DISTINCT ON (session) *
            FROM e WHERE event = '$pageview' AND session IS NOT NULL
            ORDER BY session, occurred_at
        )
        INSERT INTO web_acquisition_daily
            (day, utm_source, utm_medium, utm_campaign, utm_content, referring_domain, sessions)
        SELECT day,
               coalesce(props->>'utm_source', ''), coalesce(props->>'utm_medium', ''),
               coalesce(props->>'utm_campaign', ''), coalesce(props->>'utm_content', ''),
               CASE WHEN coalesce(props->>'$referring_domain', '') IN ('', '$direct') THEN ''
                    ELSE props->>'$referring_domain' END,
               count(*)
        FROM first_pv WHERE day >= :from_day
        GROUP BY 1, 2, 3, 4, 5, 6""",
    "web_event_daily": """
        INSERT INTO web_event_daily (day, event_name, events, sessions, visitors)
        SELECT day, event, count(*), count(DISTINCT session), count(DISTINCT visitor)
        FROM e WHERE event NOT LIKE '$%' AND day >= :from_day
        GROUP BY day, event""",
}


def build_summaries(conn: sa.Connection, *, rebuild_days: int, now: datetime) -> dict[str, int]:
    """Recomputes the last `rebuild_days` days of every summary table from
    raw_posthog_events: delete the window, re-insert it. Returns rows written.

    It never touches a day older than the oldest raw event. Raw events are pruned
    after 13 months but summaries are not, so rebuilding a day whose raw events
    are gone would erase the only record of it. The oldest day is also skipped
    once raw has reached the pruning horizon, since that day may be partial."""
    stats = conn.execute(
        sa.text(
            "SELECT (min(occurred_at) AT TIME ZONE 'Europe/Berlin')::date, "
            "       (CAST(:now AS timestamptz) AT TIME ZONE 'Europe/Berlin')::date "
            "FROM raw_posthog_events"
        ),
        {"now": now},
    ).one()
    oldest_raw_day, berlin_today = stats
    if oldest_raw_day is None:
        return {name: 0 for name in _BUILDS}

    from_day = berlin_today - timedelta(days=rebuild_days)
    if oldest_raw_day <= berlin_today - timedelta(days=RETENTION_DAYS - 2):
        oldest_raw_day += timedelta(days=1)  # raw has been pruned: the first day may be partial
    from_day = max(from_day, oldest_raw_day)

    written: dict[str, int] = {}
    for table, statement in _BUILDS.items():
        conn.execute(sa.text(f"DELETE FROM {table} WHERE day >= :from_day"), {"from_day": from_day})
        written[table] = conn.execute(sa.text(_BASE + statement), {"from_day": from_day}).rowcount
    return written


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(
    settings: Settings,
    source: PostHogSourceSettings,
    *,
    dry_run: bool = False,
    since: datetime | None = None,
    rebuild_days: int = DEFAULT_REBUILD_DAYS,
    now: datetime | None = None,
    page_size: int = PAGE_SIZE,
    out=print,
) -> RunResult:
    now = now or datetime.now(UTC)
    until = now - SETTLE

    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            mark = watermark(conn)

        # Never ask for anything older than the retention horizon: it would be
        # pruned straight after landing, and would be re-fetched forever.
        horizon = now - timedelta(days=RETENTION_DAYS)
        start = (mark[0] - OVERLAP) if mark else (since or EPOCH)
        after = max(start, horizon)

        pull = extract(source, after=after, until=until, page_size=page_size)

        if dry_run:
            out(f"window: events PostHog received after {after:%Y-%m-%d %H:%M:%S} UTC")
            out(
                f"pulled {pull.fetched} event(s) in {pull.pages} page(s); "
                f"{pull.excluded_admin} more excluded as /admin; {len(pull.events)} would be kept"
            )
            names = Counter(e.payload["event"] for e in pull.events)
            for name, n in names.most_common(8):
                out(f"  {n:>6}  {name}")
            if pull.unreviewed:
                out("properties dropped that nobody has reviewed yet (allow or ignore each):")
                for key, n in pull.unreviewed.most_common(15):
                    out(f"  {n:>6}  {key}")
            return RunResult(pull.fetched, pull.excluded_admin, 0, 0, {})

        with engine.begin() as conn:
            new = land_raw(conn, pull.events)
            pruned = prune(conn, now=now)
            summaries = build_summaries(conn, rebuild_days=rebuild_days, now=now)
    finally:
        engine.dispose()

    if pull.unreviewed:
        out(
            "note: dropped properties nobody has reviewed yet: "
            + ", ".join(f"{k} ({n})" for k, n in pull.unreviewed.most_common(8))
        )
    return RunResult(pull.fetched, pull.excluded_admin, new, pruned, summaries)


def build_only(
    settings: Settings, *, rebuild_days: int, now: datetime | None = None
) -> dict[str, int]:
    """Rebuild the summaries from raw events already stored; needs no PostHog access."""
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:
            return build_summaries(conn, rebuild_days=rebuild_days, now=now or datetime.now(UTC))
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="pull and scrub; write nothing")
    parser.add_argument(
        "--since",
        type=lambda v: datetime.fromisoformat(v).replace(tzinfo=UTC),
        help="first run only: start from this date (default: as far back as retention allows)",
    )
    parser.add_argument(
        "--rebuild-days",
        type=int,
        default=DEFAULT_REBUILD_DAYS,
        help=f"days of summaries to recompute (default {DEFAULT_REBUILD_DAYS})",
    )
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="skip PostHog; rebuild the summaries from raw events already stored",
    )
    args = parser.parse_args(argv)
    if args.rebuild_days < 1:
        print("config error: --rebuild-days must be at least 1", file=sys.stderr)
        return 2

    try:
        settings = load_settings()
        if args.build_only:
            written = build_only(settings, rebuild_days=args.rebuild_days)
            print("rebuilt: " + ", ".join(f"{t} {n}" for t, n in written.items()))
            return 0
        source = load_posthog_source_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        result = run(
            settings, source, dry_run=args.dry_run, since=args.since, rebuild_days=args.rebuild_days
        )
    except ExtractionError as exc:
        print(f"extraction error: {exc}", file=sys.stderr)
        return 3

    if not args.dry_run:
        print(
            f"pulled {result.fetched} event(s), {result.new_events} new, "
            f"{result.excluded_admin} admin excluded, {result.pruned} pruned; summaries: "
            + ", ".join(f"{t} {n}" for t, n in result.summary_rows.items())
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
