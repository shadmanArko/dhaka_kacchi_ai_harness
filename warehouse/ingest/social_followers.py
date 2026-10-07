"""Daily follower / subscriber counts: Instagram, Facebook, Threads, YouTube.

One tiny account-level call per platform, run nightly. Reuses the credentials the
post ingesters already have; nothing new to create or configure.

WHERE IT GOES (migration 0035):
  raw_social_followers   append-only, the API's answer verbatim, one row per capture
  social_account_daily   `followers` + `followers_fetched_at` for the Berlin day of
                         the capture (a second capture the same day replaces the first)

THIS HISTORY CANNOT BE BACKFILLED. None of these APIs can say what a follower count
was last week; a day the job did not run has no row and never will. That is why this
was built before the reporting panel, not after.

ONE FAILED PLATFORM DOES NOT STOP THE OTHERS. Each platform is read, landed and
stored on its own; the run exits 3 if any of them failed, after storing the rest, so
an expired Threads token (they last ~60 days) shows up in the cron log without
costing three other platforms' counts. A platform whose credentials are not
configured at all is SKIPPED with a warning, so a laptop without Threads still works.

WHAT EACH PLATFORM RETURNS (VERIFIED against the real accounts 2026-10-07):
  Instagram  GET /{ig-user-id}?fields=followers_count            -> 41
  Facebook   GET /{page-id}?fields=followers_count,fan_count     -> 355 / 355
             (followers_count is preferred; fan_count is the older "likes" figure)
  Threads    GET /{user-id}/threads_insights?metric=followers_count
             -> data[0].total_value.value = 0. UNVERIFIED: whether Threads reports a
             real 0 or hides the count below a threshold (its demographics need 100
             followers). It is stored as returned; if the account is known to have
             followers while this stays 0, that is the cause.
  YouTube    GET channels?part=statistics&id=...  (API key, 1 quota unit)
             statistics.subscriberCount, a STRING; NULL when
             statistics.hiddenSubscriberCount is true (the owner can hide it). YouTube
             also rounds public subscriber counts above 1,000, so it is approximate.

Exit codes: 0 ok, 2 config error (or nothing configured), 3 a platform failed.
Run with `make ingest-social-followers`; prove with `make verify-ingest-social-followers`.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime

import sqlalchemy as sa

from warehouse.config import (
    ConfigError,
    Settings,
    load_facebook_source_settings,
    load_instagram_source_settings,
    load_settings,
    load_threads_source_settings,
    load_youtube_source_settings,
)

GRAPH_BASE = "https://graph.facebook.com/v26.0"
THREADS_BASE = "https://graph.threads.net/v1.0"
REQUEST_TIMEOUT_S = 30
PLATFORMS = ("instagram", "facebook", "threads", "youtube")
BERLIN_DAY_SQL = "((:t AT TIME ZONE 'Europe/Berlin')::date)"


class ExtractionError(RuntimeError):
    """A platform read failed in a way the operator must act on."""


@dataclass(frozen=True, slots=True)
class Bases:
    """API roots. A seam for the verifier's local fake server; the defaults are real."""

    graph: str = GRAPH_BASE
    threads: str = THREADS_BASE
    youtube: str | None = None  # None -> the base carried by the YouTube settings


@dataclass(slots=True)
class Outcome:
    stored: dict[str, int | None] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _get(url: str, secret: str, what: str) -> dict:
    """GET + JSON. Every error is redacted: the credential rides in the URL, and
    Graph API error bodies can echo request details into a cron log."""
    try:
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        message = f"{what}: HTTP {exc.code}: {body}"
    except urllib.error.URLError as exc:
        message = f"{what}: unreachable ({exc.reason})"
    except (ValueError, OSError) as exc:
        message = f"{what}: unreadable response ({type(exc).__name__})"
    raise ExtractionError(message.replace(secret, "***"))


def _count(value: object, what: str) -> int:
    """bool is an int subclass in Python; a True from a drifting API must not become 1."""
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ExtractionError(f"{what}: expected a whole number, got {value!r}")
    try:
        number = int(value)
    except ValueError:
        raise ExtractionError(f"{what}: expected a whole number, got {value!r}") from None
    if number < 0:
        raise ExtractionError(f"{what}: negative count {number}")
    return number


def parse(platform: str, payload: dict) -> int | None:
    """The follower count in a landed payload; None only for a hidden YouTube count."""
    what = f"{platform} follower count"
    if platform == "instagram":
        return _count(payload.get("followers_count"), what)
    if platform == "facebook":
        value = payload.get("followers_count")
        return _count(payload.get("fan_count") if value is None else value, what)
    if platform == "threads":
        try:
            entry = next(d for d in payload["data"] if d.get("name") == "followers_count")
            return _count(entry["total_value"]["value"], what)
        except (KeyError, StopIteration, TypeError):
            raise ExtractionError(f"{what}: followers_count missing from the response") from None
    if platform == "youtube":
        try:
            stats = payload["items"][0]["statistics"]
        except (KeyError, IndexError, TypeError):
            raise ExtractionError(f"{what}: channel statistics missing from the response") from None
        if stats.get("hiddenSubscriberCount") is True:
            return None
        return _count(stats.get("subscriberCount"), what)
    raise ExtractionError(f"unknown platform {platform!r}")


def fetch(platform: str, bases: Bases) -> dict:
    """The platform's raw answer. Raises ConfigError if it is not configured."""
    q = urllib.parse.urlencode
    if platform == "instagram":
        s = load_instagram_source_settings()
        url = f"{bases.graph}/{s.business_account_id}?" + q(
            {"fields": "followers_count", "access_token": s.access_token}
        )
        return _get(url, s.access_token, "instagram")
    if platform == "facebook":
        s = load_facebook_source_settings()
        url = f"{bases.graph}/{s.page_id}?" + q(
            {"fields": "followers_count,fan_count", "access_token": s.access_token}
        )
        return _get(url, s.access_token, "facebook")
    if platform == "threads":
        s = load_threads_source_settings()
        url = f"{bases.threads}/{s.user_id}/threads_insights?" + q(
            {"metric": "followers_count", "access_token": s.access_token}
        )
        return _get(url, s.access_token, "threads")
    if platform == "youtube":
        s = load_youtube_source_settings()
        base = bases.youtube or s.api_base
        url = f"{base}/channels?" + q({"part": "statistics", "id": s.channel_id, "key": s.api_key})
        return _get(url, s.api_key, "youtube")
    raise ExtractionError(f"unknown platform {platform!r}")


# ---------------------------------------------------------------------------
# Land + store
# ---------------------------------------------------------------------------


def land_and_store(
    conn: sa.Connection, platform: str, payload: dict, *, captured_at: datetime
) -> int | None:
    """Lands the payload, then derives the typed row FROM THE LANDED ROW (not from the
    in-memory copy), so what is stored is always what raw holds."""
    conn.execute(
        sa.text(
            "INSERT INTO raw_social_followers (platform, captured_at, payload) "
            "VALUES (:p, :t, CAST(:payload AS jsonb)) "
            "ON CONFLICT (platform, captured_at) DO NOTHING"
        ),
        {"p": platform, "t": captured_at, "payload": json.dumps(payload)},
    )
    landed = conn.execute(
        sa.text(
            "SELECT payload FROM raw_social_followers WHERE platform = :p AND captured_at = :t"
        ),
        {"p": platform, "t": captured_at},
    ).scalar_one()
    followers = parse(platform, landed)
    # Only these two columns are named, so YouTube Analytics' figures in the same row are
    # never touched. The WHERE keeps an older capture from overwriting a newer one of the
    # same Berlin day if two runs ever overlap.
    conn.execute(
        sa.text(
            f"INSERT INTO social_account_daily (platform, day, followers, followers_fetched_at) "
            f"VALUES (:p, {BERLIN_DAY_SQL}, :f, :t) "
            "ON CONFLICT (platform, day) DO UPDATE SET "
            "followers = EXCLUDED.followers, "
            "followers_fetched_at = EXCLUDED.followers_fetched_at, "
            "updated_at = now() "
            "WHERE social_account_daily.followers_fetched_at IS NULL "
            "OR social_account_daily.followers_fetched_at <= EXCLUDED.followers_fetched_at"
        ),
        {"p": platform, "t": captured_at, "f": followers},
    )
    return followers


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(
    settings: Settings,
    *,
    platforms: tuple[str, ...] = PLATFORMS,
    bases: Bases | None = None,
    dry_run: bool = False,
    captured_at: datetime | None = None,
) -> Outcome:
    bases = bases or Bases()
    captured_at = captured_at or datetime.now(UTC)
    outcome = Outcome()
    engine = (
        None if dry_run else sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    )
    try:
        for platform in platforms:
            try:
                payload = fetch(platform, bases)
                followers = parse(platform, payload)  # fail before landing anything invalid
                if engine is not None:
                    with engine.begin() as conn:
                        followers = land_and_store(conn, platform, payload, captured_at=captured_at)
                outcome.stored[platform] = followers
            except ConfigError as exc:
                outcome.skipped.append(platform)
                print(f"warning: {platform} skipped, not configured ({exc})", file=sys.stderr)
            except ExtractionError as exc:
                outcome.failed[platform] = str(exc)
    finally:
        if engine is not None:
            engine.dispose()
    return outcome


def exit_code(outcome: Outcome) -> int:
    """3 if any platform failed (after storing the rest), 2 if nothing was configured."""
    if outcome.failed:
        return 3
    return 0 if outcome.stored else 2


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="read and print; write nothing")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    outcome = run(settings, dry_run=args.dry_run)
    verb = "would store" if args.dry_run else "stored"
    for platform, followers in outcome.stored.items():
        shown = "not reported (hidden)" if followers is None else followers
        print(f"{verb} {platform}: {shown}")
    for message in outcome.failed.values():
        print(f"extraction error: {message}", file=sys.stderr)
    code = exit_code(outcome)
    if code == 2:
        print("config error: no platform is configured", file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
