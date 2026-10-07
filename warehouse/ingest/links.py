"""Ingest the tagged links made in the website's admin Link builder, and give each one
its own place in the attribution backbone so a visit can be traced to ONE post.

Reads dhaka-kacchi-connect's `tracked_links` table over the same read-only
`ordering_reader` connection the orders and events jobs use (worker/migrations-manual/
0004_tracked_links.sql). Same shape: land raw, then transform; idempotent; `--dry-run`.

WHAT IT BUILDS, per link:
  channel          one per utm_source ("instagram", "whatsapp", "creator-ayesha"). An
                   EXISTING channel with that platform is reused (the seeded
                   "instagram-organic" etc. from 0027), so a post's visits roll up with
                   the rest of that platform. Only a source with no channel yet gets one.
  campaign         one per (channel, campaign tag), e.g. "instagram-organic-batch-2026-10-10".
  campaign_variant one per LINK, with utm_content = the link's content tag. This is the
                   row events.py resolves a visit to, via channel.platform = utm_source +
                   variant.utm_content - the whole point of the job.
  tracked_link     the typed copy of the link, plus social_post_id (see below).

POSTS. A link has to exist before the post that carries it, so the person pastes the
published post's address into the Link builder afterwards (post_url). It is matched to
social_post.permalink after normalising both the same way (normalize_post_url, a mirror
of normalizePostUrl in the website worker; both are tested against the same cases). A
link with no post_url, or whose post is not ingested yet, simply has social_post_id NULL;
a creator or WhatsApp link never will have one. That is not an error.

LATE LINKS, AND THE ONE DELIBERATE EXCEPTION TO "EVENTS NEVER CHANGE". events.py
resolves attribution once, at ingest, and never rewrites an event. A link made at 14:00
and clicked at 14:10 can therefore be ingested as an event BEFORE this job has seen the
link, and would stay unattributed forever. So after syncing, this job labels events that
have utm_source+utm_content matching a link's variant AND no attribution yet. It only
ever fills an EMPTY label: an event that already has a channel/campaign/variant is never
touched. The utm values are part of the event's own immutable properties, so this derives
a label from facts already stored; it does not rewrite the fact.

CLASHES ARE REPORTED, NOT HIDDEN. A visit resolves by (platform, utm_content) and takes
the first match, so two variants answering to the same pair would make attribution
arbitrary. If a link's (source, content) already belongs to another variant (for example
someone made a link with content "bio_link", which 0027 seeded for each platform), the
link is stored with campaign_variant_id NULL and counted as a clash in the run summary.
The website's own (source, content) UNIQUE rules out two LINKS clashing; this covers a
link against a pre-existing variant.

channel.kind comes from the link's medium (CHANNEL_KIND_FOR_MEDIUM); it only matters for a
source that has no channel yet.

Run with `make ingest-links`; preview with `make ingest-links-dry-run`; prove with
`make verify-ingest-links`. It needs no new configuration (ORDERING_DATABASE_URL). If the
website's tracked_links table has not been created yet the job says so instead of failing
obscurely. Run it just before `ingest-events` (cron does).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlsplit

import sqlalchemy as sa

from warehouse.config import (
    ConfigError,
    OrderingSourceSettings,
    Settings,
    load_ordering_source_settings,
    load_settings,
)
from warehouse.ingest.upsert import upsert_returning

CHANNEL_KIND_FOR_MEDIUM = {
    "organic_social": "organic_social",
    "story": "organic_social",
    "bio": "organic_social",
    "creator": "organic_social",
    "community": "organic_social",
    "message": "owned",
    "email": "owned",
    "print": "offline",
}

POST_HOSTS = (
    "instagram.com",
    "facebook.com",
    "fb.com",
    "fb.watch",
    "threads.net",
    "threads.com",
    "youtube.com",
    "youtu.be",
)

_LINKS_SQL = sa.text(
    """
    SELECT id, created_at, label, source, medium, campaign, content,
           destination_path, url, post_url
    FROM tracked_links
    ORDER BY created_at, id
    """
)


class ExtractionError(RuntimeError):
    """The source read failed in a way the operator must act on."""


@dataclass(frozen=True, slots=True)
class RunResult:
    raw_landed: int
    links: int
    variants_created: int
    clashes: int
    posts_matched: int
    events_attributed: int


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def normalize_post_url(raw: str) -> str | None:
    """A post URL in the form social_post.permalink is compared in: https, no leading
    www./m./web./mbasic., no query or fragment, no trailing slash. None when it is not a
    link to a post on a platform we track. KEEP IN SYNC with normalizePostUrl() in
    dhaka-kacchi-connect worker/src/lib/trackedLinks.ts."""
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    for prefix in ("www.", "m.", "web.", "mbasic."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    if not any(host == h or host.endswith("." + h) for h in POST_HOSTS):
        return None
    path = parts.path.rstrip("/")
    if path == "":
        return None
    return f"https://{host}{path}"


def channel_name(source: str) -> str:
    return source.replace("-", " ").replace("_", " ").title()


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def extract(source: OrderingSourceSettings) -> list[dict]:
    engine = sa.create_engine(source.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            rows = conn.execute(_LINKS_SQL).mappings().all()
    except sa.exc.ProgrammingError as exc:
        if "tracked_links" in str(exc) and "does not exist" in str(exc):
            raise ExtractionError(
                "The website database has no tracked_links table yet. Apply "
                "dhaka-kacchi-connect worker/migrations-manual/0004_tracked_links.sql "
                "to it first (see that directory's README)."
            ) from None
        raise
    finally:
        engine.dispose()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Raw landing
# ---------------------------------------------------------------------------

raw_tracked_links = sa.table(
    "raw_tracked_links",
    sa.column("id"),
    sa.column("external_id"),
    sa.column("payload"),
    sa.column("updated_at"),
)

tracked_link_t = sa.table(
    "tracked_link",
    sa.column("external_id"),
    sa.column("label"),
    sa.column("source"),
    sa.column("medium"),
    sa.column("campaign"),
    sa.column("content"),
    sa.column("destination_path"),
    sa.column("url"),
    sa.column("post_url"),
    sa.column("social_post_id"),
    sa.column("campaign_variant_id"),
    sa.column("link_created_at"),
    sa.column("updated_at"),
)


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def land_raw(conn: sa.Connection, links: list[dict]) -> int:
    rows = [
        {
            "external_id": link["id"],
            "payload": json.dumps(link, default=_json_default),
            "updated_at": sa.func.now(),
        }
        for link in links
    ]
    result = upsert_returning(
        conn,
        raw_tracked_links,
        rows,
        conflict_on=["external_id"],
        update=["payload", "updated_at"],
        returning=["id"],
    )
    return len(result)


# ---------------------------------------------------------------------------
# Transform + load
# ---------------------------------------------------------------------------


def _ensure_channel(conn: sa.Connection, link: dict) -> sa.Row:
    found = conn.execute(
        sa.text("SELECT id, slug, name FROM channel WHERE platform = :p ORDER BY created_at, slug"),
        {"p": link["source"]},
    ).first()
    if found:
        return found
    conn.execute(
        sa.text(
            "INSERT INTO channel (slug, name, kind, platform) VALUES (:slug, :name, :kind, :p) "
            "ON CONFLICT (slug) DO NOTHING"
        ),
        {
            "slug": link["source"],
            "name": channel_name(link["source"]),
            "kind": CHANNEL_KIND_FOR_MEDIUM[link["medium"]],
            "p": link["source"],
        },
    )
    return conn.execute(
        sa.text(
            "SELECT id, slug, name FROM channel WHERE platform = :p ORDER BY created_at LIMIT 1"
        ),
        {"p": link["source"]},
    ).one()


def _ensure_campaign(conn: sa.Connection, channel: sa.Row, link: dict) -> sa.Row:
    slug = f"{channel.slug}-{link['campaign']}"
    conn.execute(
        sa.text(
            "INSERT INTO campaign (slug, channel_id, name, starts_at, status) "
            "VALUES (:slug, :channel_id, :name, :starts_at, 'active') "
            "ON CONFLICT (slug) DO NOTHING"
        ),
        {
            "slug": slug,
            "channel_id": channel.id,
            "name": f"{channel.name} - {link['campaign']}",
            # The link's real creation time, never "now": the same lesson as 0027's
            # campaign.starts_at (CLAUDE.md, the recipe.active_from war story).
            "starts_at": link["created_at"],
        },
    )
    return conn.execute(sa.text("SELECT id, slug FROM campaign WHERE slug = :s"), {"s": slug}).one()


def _ensure_variant(
    conn: sa.Connection, campaign: sa.Row, link: dict
) -> tuple[object | None, bool]:
    """(variant id or None on a clash, whether a new variant row was created)."""
    slug = f"{campaign.slug}-{link['content']}"
    clash = conn.execute(
        sa.text(
            "SELECT v.id FROM campaign_variant v "
            "JOIN campaign c ON c.id = v.campaign_id "
            "JOIN channel ch ON ch.id = c.channel_id "
            "WHERE ch.platform = :p AND v.utm_content = :content AND v.slug <> :slug LIMIT 1"
        ),
        {"p": link["source"], "content": link["content"], "slug": slug},
    ).first()
    if clash:
        return None, False
    inserted = conn.execute(
        sa.text(
            "INSERT INTO campaign_variant (slug, campaign_id, creative_ref, utm_content) "
            "VALUES (:slug, :campaign_id, :creative_ref, :content) "
            "ON CONFLICT (slug) DO NOTHING RETURNING id"
        ),
        {
            "slug": slug,
            "campaign_id": campaign.id,
            "creative_ref": link["label"],
            "content": link["content"],
        },
    ).first()
    if inserted:
        return inserted.id, True
    existing = conn.execute(
        sa.text("SELECT id FROM campaign_variant WHERE slug = :s"), {"s": slug}
    ).one()
    return existing.id, False


def _post_ids_by_permalink(conn: sa.Connection) -> dict[str, object]:
    out: dict[str, object] = {}
    for post_id, permalink in conn.execute(
        sa.text("SELECT id, permalink FROM social_post WHERE permalink IS NOT NULL")
    ):
        normalized = normalize_post_url(permalink)
        if normalized:
            out[normalized] = post_id
    return out


# Fills EMPTY attribution only (see LATE LINKS in the module docstring). The IN list
# restricts it to variants this job created, so the seeded catch-all bio_link variants
# and everything else stay exactly as events.py resolved them.
_ATTRIBUTE_LATE_EVENTS_SQL = sa.text(
    """
    UPDATE event e
       SET channel_id = ch.id, campaign_id = c.id, campaign_variant_id = v.id
      FROM tracked_link t
      JOIN campaign_variant v ON v.id = t.campaign_variant_id
      JOIN campaign c ON c.id = v.campaign_id
      JOIN channel ch ON ch.id = c.channel_id
     WHERE e.campaign_variant_id IS NULL
       AND e.channel_id IS NULL
       AND e.campaign_id IS NULL
       AND e.properties ->> 'utm_source' = ch.platform
       AND e.properties ->> 'utm_content' = v.utm_content
    """
)


def transform_and_load(conn: sa.Connection) -> tuple[int, int, int, int, int]:
    """(links, variants created, clashes, posts matched, events attributed)."""
    raw_rows = conn.execute(
        sa.text(
            "SELECT payload FROM raw_tracked_links ORDER BY (payload ->> 'created_at'), external_id"
        )
    ).all()
    posts = _post_ids_by_permalink(conn)

    variants_created = clashes = posts_matched = 0
    rows = []
    for (payload,) in raw_rows:
        link = dict(payload)
        link["created_at"] = datetime.fromisoformat(link["created_at"])
        channel = _ensure_channel(conn, link)
        campaign = _ensure_campaign(conn, channel, link)
        variant_id, created = _ensure_variant(conn, campaign, link)
        variants_created += int(created)
        clashes += int(variant_id is None)

        post_url = normalize_post_url(link["post_url"]) if link.get("post_url") else None
        post_id = posts.get(post_url) if post_url else None
        posts_matched += int(post_id is not None)

        rows.append(
            {
                "external_id": link["id"],
                "label": link["label"],
                "source": link["source"],
                "medium": link["medium"],
                "campaign": link["campaign"],
                "content": link["content"],
                "destination_path": link["destination_path"],
                "url": link["url"],
                "post_url": post_url,
                "social_post_id": post_id,
                "campaign_variant_id": variant_id,
                "link_created_at": link["created_at"],
                "updated_at": sa.func.now(),
            }
        )

    if rows:
        upsert_returning(
            conn,
            tracked_link_t,
            rows,
            conflict_on=["external_id"],
            # The link's own tags never change at the source; what can change is the
            # post it is attached to, and what we resolved it to.
            update=["post_url", "social_post_id", "campaign_variant_id", "updated_at"],
            returning=["external_id"],
        )

    attributed = conn.execute(_ATTRIBUTE_LATE_EVENTS_SQL).rowcount or 0
    return len(rows), variants_created, clashes, posts_matched, attributed


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(settings: Settings, source: OrderingSourceSettings, *, dry_run: bool = False) -> RunResult:
    links = extract(source)

    if dry_run:
        print(f"{len(links)} link(s) at the source:")
        for link in links:
            post = "post attached" if link.get("post_url") else "no post yet"
            print(f"  {link['source']:<22} {link['content']:<28} {link['campaign']:<18} {post}")
        return RunResult(0, len(links), 0, 0, 0, 0)

    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        with engine.begin() as conn:
            raw_landed = land_raw(conn, links)
        with engine.begin() as conn:
            n, created, clashes, matched, attributed = transform_and_load(conn)
    finally:
        engine.dispose()

    return RunResult(raw_landed, n, created, clashes, matched, attributed)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="read and print; write nothing")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
        source_settings = load_ordering_source_settings()
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
            f"landed {result.raw_landed} raw row(s); {result.links} link(s), "
            f"{result.variants_created} new variant(s), {result.posts_matched} matched to a post, "
            f"{result.events_attributed} earlier event(s) attributed"
        )
        if result.clashes:
            print(
                f"warning: {result.clashes} link(s) share a source and content with an existing "
                "variant and were stored WITHOUT one, so their visits cannot be attributed. "
                "Give them a different content name in the Link builder.",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
