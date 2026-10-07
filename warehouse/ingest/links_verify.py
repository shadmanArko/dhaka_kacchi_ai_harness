"""Prove warehouse.ingest.links behaves correctly, WITHOUT the real website database.

Same idiom as youtube_verify.py / social_followers_verify.py: plain ok/FAIL output and an
exit code, and TWO throwaway databases of its own - a warehouse (fully migrated) and a
stand-in for the website's database holding just a `tracked_links` table - so your dev
data is never touched.

It proves the things that would silently corrupt attribution: each link gets exactly one
variant under the RIGHT channel (an existing platform's channel is reused, not duplicated),
a re-run changes nothing, a post is matched however its URL is dressed up and re-matched
once it is ingested later or cleared when removed, a clash with an existing variant is
reported instead of creating an ambiguous second one, events that arrived before their
link get labelled but an event that already has a label is never changed, and a missing
source table gives an instruction rather than a stack trace.

Then it MUTATES the job (drop the clash guard, date a campaign "now", skip URL
normalising, drop the only-fill-empty guard) and demands the matching check fails.

Run with `make verify-ingest-links`.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa

from warehouse import bootstrap_db
from warehouse.config import ConfigError, OrderingSourceSettings, load_settings
from warehouse.ingest import links as real_module
from warehouse.ingest.youtube_verify import _migrate

SOURCE_DDL = """
CREATE TABLE tracked_links (
  id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), created_by TEXT,
  label TEXT NOT NULL, source TEXT NOT NULL, medium TEXT NOT NULL, campaign TEXT NOT NULL,
  content TEXT NOT NULL, destination_path TEXT NOT NULL DEFAULT '/', url TEXT NOT NULL,
  post_url TEXT, CONSTRAINT uq_tracked_links_source_content UNIQUE (source, content)
)
"""

INSERT_LINK_SQL = (
    "INSERT INTO tracked_links (id, created_at, label, source, medium, campaign, content, "
    "destination_path, url, post_url) VALUES (:id, :created_at, :label, :source, :medium, "
    ":campaign, :content, :destination_path, :url, :post_url)"
)

T1 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
T3 = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)


def _link(
    n: int,
    source: str,
    medium: str,
    content: str,
    *,
    at=T1,
    post_url=None,
    campaign="batch-2026-10-10",
):
    return {
        "id": f"lnk_{n}",
        "created_at": at,
        "label": f"Link {n}",
        "source": source,
        "medium": medium,
        "campaign": campaign,
        "content": content,
        "destination_path": "/",
        "url": f"https://dhakakacchi.com/?utm_source={source}&utm_content={content}",
        "post_url": post_url,
    }


BASE_LINKS = [
    _link(1, "instagram", "story", "story-last-call", at=T1),
    _link(
        2,
        "instagram",
        "organic_social",
        "reel-kacchi-pot",
        at=T2,
        post_url="https://www.instagram.com/p/AAA111/?igsh=zz",
    ),
    _link(3, "whatsapp", "message", "broadcast-friday", at=T2),
    _link(4, "creator-ayesha", "creator", "ayesha-reel-1", at=T3),
    _link(5, "qr-menu-card", "print", "menu-card-a", at=T3),
]


def _quiet(fn):
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        return fn()


# ---------------------------------------------------------------------------
# Helpers over the two databases
# ---------------------------------------------------------------------------


class Env:
    def __init__(self, wh_engine, src_engine, settings, source):
        self.wh, self.src, self.settings, self.source = wh_engine, src_engine, settings, source

    def reset(self, links):
        with self.src.begin() as c:
            c.execute(sa.text("DROP TABLE IF EXISTS tracked_links"))
            c.execute(sa.text(SOURCE_DDL))
            for link in links:
                c.execute(
                    sa.text(INSERT_LINK_SQL),
                    link,
                )
        with self.wh.begin() as c:
            for t in ("event", "tracked_link", "raw_tracked_links"):
                c.execute(sa.text(f"DELETE FROM {t}"))
            c.execute(sa.text("UPDATE social_post SET campaign_variant_id = NULL"))
            c.execute(sa.text("DELETE FROM social_post"))
            c.execute(sa.text("DELETE FROM campaign_variant WHERE slug NOT LIKE '%-bio-link'"))
            c.execute(sa.text("DELETE FROM campaign WHERE slug NOT LIKE '%-organic-ongoing'"))
            c.execute(sa.text("DELETE FROM channel WHERE slug NOT LIKE '%-organic'"))

    def add_link(self, link):
        with self.src.begin() as c:
            c.execute(
                sa.text(INSERT_LINK_SQL),
                link,
            )

    def set_post_url(self, link_id, post_url):
        with self.src.begin() as c:
            c.execute(
                sa.text("UPDATE tracked_links SET post_url=:p WHERE id=:i"),
                {"p": post_url, "i": link_id},
            )

    def add_post(self, external_id, permalink):
        with self.wh.begin() as c:
            c.execute(
                sa.text(
                    "INSERT INTO social_post (platform, external_id, posted_at, permalink) "
                    "VALUES ('instagram', :e, now(), :p)"
                ),
                {"e": external_id, "p": permalink},
            )

    def add_event(self, props, **fk):
        with self.wh.begin() as c:
            return c.execute(
                sa.text(
                    "INSERT INTO event (event_name, occurred_at, source, external_id, properties, "
                    "channel_id, campaign_id, campaign_variant_id) VALUES ('page_view', now(), "
                    "'website', :x, CAST(:props AS jsonb), :ch, :cam, :var) RETURNING id"
                ),
                {
                    "x": f"e_{os.urandom(4).hex()}",
                    "props": __import__("json").dumps(props),
                    "ch": fk.get("channel_id"),
                    "cam": fk.get("campaign_id"),
                    "var": fk.get("variant_id"),
                },
            ).scalar_one()

    def q(self, sql, **p):
        with self.wh.connect() as c:
            return c.execute(sa.text(sql), p).all()

    def one(self, sql, **p):
        rows = self.q(sql, **p)
        return rows[0][0] if rows else None


def _mutant(replacements):
    source = Path(real_module.__file__).read_text()
    for old, new in replacements:
        if old not in source:
            raise AssertionError(f"mutation target not found in links.py: {old!r}")
        source = source.replace(old, new, 1)
    module = types.ModuleType("links_mutant")
    module.__file__ = real_module.__file__
    sys.modules[module.__name__] = module
    exec(compile(source, "links_mutant", "exec"), module.__dict__)  # noqa: S102
    return module


# ---------------------------------------------------------------------------
# Scenarios: each returns a list of problems (empty = pass)
# ---------------------------------------------------------------------------


def scenarios(mod, env: Env) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {}

    def go(name, fn, links=None):
        env.reset(BASE_LINKS if links is None else links)
        try:
            results[name] = fn()
        except Exception as exc:  # a crash is a failure with a reason
            results[name] = [f"crashed: {type(exc).__name__}: {exc}"]

    def run():
        return _quiet(lambda: mod.run(env.settings, env.source))

    def counts():
        return tuple(
            env.one(f"SELECT count(*) FROM {t}")
            for t in (
                "raw_tracked_links",
                "tracked_link",
                "channel",
                "campaign",
                "campaign_variant",
            )
        )

    def builds_backbone():
        r = run()
        problems = []
        if (r.links, r.variants_created, r.clashes) != (5, 5, 0):
            problems.append(
                f"links/variants/clashes = {(r.links, r.variants_created, r.clashes)}, "
                "want (5, 5, 0)"
            )
        # Instagram's links reuse the seeded channel; they do not create a second "instagram".
        n_ig = env.one("SELECT count(*) FROM channel WHERE platform = 'instagram'")
        if n_ig != 1:
            problems.append(f"{n_ig} instagram channels, want the one seeded channel reused")
        # Each link: its own variant, utm_content = its content tag, under its source's channel.
        rows = env.q(
            "SELECT t.content, v.utm_content, ch.platform FROM tracked_link t "
            "JOIN campaign_variant v ON v.id = t.campaign_variant_id "
            "JOIN campaign c ON c.id = v.campaign_id JOIN channel ch ON ch.id = c.channel_id "
            "ORDER BY t.content"
        )
        want = {
            "story-last-call": "instagram",
            "reel-kacchi-pot": "instagram",
            "broadcast-friday": "whatsapp",
            "ayesha-reel-1": "creator-ayesha",
            "menu-card-a": "qr-menu-card",
        }
        got = {r[0]: r[2] for r in rows if r[0] == r[1]}
        if got != want:
            problems.append(f"link -> channel platform is {got}, want {want}")
        kinds = dict(
            env.q(
                "SELECT platform, kind FROM channel "
                "WHERE platform IN ('whatsapp','creator-ayesha','qr-menu-card')"
            )
        )
        if kinds != {
            "whatsapp": "owned",
            "creator-ayesha": "organic_social",
            "qr-menu-card": "offline",
        }:
            problems.append(f"new channel kinds {kinds}")
        return problems

    def campaign_dated_at_link():
        run()
        row = env.q(
            "SELECT c.starts_at FROM campaign c WHERE c.slug = 'instagram-organic-batch-2026-10-10'"
        )
        if not row:
            return ["campaign 'instagram-organic-batch-2026-10-10' not created"]
        # Earliest instagram link is T1; a campaign dated "now" would silently zero out history.
        return (
            [] if row[0][0] == T1 else [f"starts_at {row[0][0]}, want the first link's time {T1}"]
        )

    def idempotent():
        run()
        before = counts()
        r = run()
        after = counts()
        problems = [] if before == after else [f"counts changed on re-run: {before} -> {after}"]
        if r.variants_created != 0:
            problems.append(f"re-run created {r.variants_created} variants")
        return problems

    def matches_posts():
        env.add_post("AAA111", "https://www.instagram.com/p/AAA111/")
        run()
        link = env.one(
            "SELECT social_post_id IS NOT NULL FROM tracked_link WHERE external_id = 'lnk_2'"
        )
        other = env.one("SELECT count(*) FROM tracked_link WHERE social_post_id IS NOT NULL")
        problems = []
        if not link:
            problems.append(
                "a post attached with a dressed-up URL was not matched to its social_post"
            )
        if other != 1:
            problems.append(f"{other} links matched a post, want exactly 1")
        return problems

    def post_ingested_later():
        run()  # the post is not in social_post yet
        if (
            env.one("SELECT social_post_id FROM tracked_link WHERE external_id = 'lnk_2'")
            is not None
        ):
            return ["matched a post that does not exist"]
        env.add_post("AAA111", "https://instagram.com/p/AAA111")
        run()
        ok = env.one(
            "SELECT social_post_id IS NOT NULL FROM tracked_link WHERE external_id = 'lnk_2'"
        )
        return [] if ok else ["not matched after the post was ingested"]

    def post_cleared():
        env.add_post("AAA111", "https://www.instagram.com/p/AAA111/")
        run()
        env.set_post_url("lnk_2", None)
        run()
        got = env.q(
            "SELECT post_url, social_post_id FROM tracked_link WHERE external_id = 'lnk_2'"
        )[0]
        return (
            []
            if tuple(got) == (None, None)
            else [f"after clearing: {tuple(got)}, want (None, None)"]
        )

    def clash_reported():
        env.add_link(_link(6, "instagram", "bio", "bio_link", at=T3))
        r = run()
        problems = []
        if r.clashes != 1:
            problems.append(f"{r.clashes} clashes, want 1")
        n = env.one(
            "SELECT count(*) FROM campaign_variant v JOIN campaign c ON c.id=v.campaign_id "
            "JOIN channel ch ON ch.id=c.channel_id "
            "WHERE ch.platform='instagram' AND v.utm_content='bio_link'"
        )
        if n != 1:
            problems.append(f"{n} instagram variants answer to bio_link, want only the seeded one")
        if (
            env.one("SELECT campaign_variant_id FROM tracked_link WHERE external_id='lnk_6'")
            is not None
        ):
            problems.append("the clashing link was given a variant")
        return problems

    def late_events_labelled():
        env.reset(BASE_LINKS)
        seeded_var = env.one(
            "SELECT id FROM campaign_variant WHERE slug = 'instagram-organic-bio-link'"
        )
        seeded_ch = env.one("SELECT id FROM channel WHERE slug = 'instagram-organic'")
        seeded_cam = env.one("SELECT id FROM campaign WHERE slug = 'instagram-organic-ongoing'")
        late = env.add_event({"utm_source": "instagram", "utm_content": "story-last-call"})
        labelled = env.add_event(
            {"utm_source": "instagram", "utm_content": "story-last-call"},
            channel_id=seeded_ch,
            campaign_id=seeded_cam,
            variant_id=seeded_var,
        )
        unrelated = env.add_event({"utm_source": "instagram", "utm_content": "never-made"})
        direct = env.add_event({})
        r = run()

        def get(i):
            return env.q("SELECT campaign_variant_id, channel_id FROM event WHERE id=:i", i=i)[0]

        want_var = env.one("SELECT campaign_variant_id FROM tracked_link WHERE external_id='lnk_1'")
        problems = []
        if get(late)[0] != want_var:
            problems.append("an event that arrived before its link was not attributed to it")
        if get(labelled)[0] != seeded_var:
            problems.append("an event that ALREADY had attribution was changed")
        if get(unrelated)[0] is not None or get(direct)[0] is not None:
            problems.append("an unrelated or direct event was attributed")
        if r.events_attributed != 1:
            problems.append(f"events_attributed {r.events_attributed}, want 1")
        if run().events_attributed != 0:
            problems.append("a second run attributed events again")
        return problems

    def missing_source_table():
        with env.src.begin() as c:
            c.execute(sa.text("DROP TABLE tracked_links"))
        try:
            _quiet(lambda: mod.run(env.settings, env.source))
        except mod.ExtractionError as exc:
            return [] if "0004_tracked_links.sql" in str(exc) else [f"unhelpful message: {exc}"]
        return ["no error for a missing source table"]

    def dry_run_writes_nothing():
        before = counts()
        r = _quiet(lambda: mod.run(env.settings, env.source, dry_run=True))
        problems = [] if counts() == before else ["a dry run wrote rows"]
        if r.links != 5:
            problems.append(f"dry run saw {r.links} links, want 5")
        return problems

    def normalizes_urls():
        cases = [
            ("https://www.instagram.com/p/DAbc123/", "https://instagram.com/p/DAbc123"),
            (
                "https://www.instagram.com/p/DAbc123/?utm_source=ig_web_copy_link&igsh=x",
                "https://instagram.com/p/DAbc123",
            ),
            ("http://instagram.com/reel/XyZ/", "https://instagram.com/reel/XyZ"),
            (
                "https://www.facebook.com/dhakakacchi/posts/123456#comments",
                "https://facebook.com/dhakakacchi/posts/123456",
            ),
            ("https://m.facebook.com/story.php", "https://facebook.com/story.php"),
            (
                "https://www.threads.net/@dhakakacchi/post/ABC/",
                "https://threads.net/@dhakakacchi/post/ABC",
            ),
            ("https://youtu.be/abc123", "https://youtu.be/abc123"),
            ("https://www.youtube.com/shorts/abc123", "https://youtube.com/shorts/abc123"),
            ("https://instagram.com/", None),
            ("https://example.com/p/1", None),
            ("https://evil-instagram.com/p/1", None),
            ("not a url", None),
            ("javascript:alert(1)", None),
        ]
        return [
            f"{raw!r} -> {mod.normalize_post_url(raw)!r}, want {want!r}"
            for raw, want in cases
            if mod.normalize_post_url(raw) != want
        ]

    for name, fn in [
        (
            "each link gets its own variant under the right channel; existing channels are reused",
            builds_backbone,
        ),
        ("a campaign is dated at its first link, not at ingest time", campaign_dated_at_link),
        ("a re-run changes nothing", idempotent),
        ("a post attached with a dressed-up URL is matched to its social_post", matches_posts),
        ("a post ingested after its link is matched on the next run", post_ingested_later),
        ("removing a post address clears the match", post_cleared),
        ("a clash with an existing variant is reported and creates no second one", clash_reported),
        (
            "late events are labelled; an already-labelled event is never changed",
            late_events_labelled,
        ),
        ("a missing source table gives an instruction", missing_source_table),
        ("a dry run writes nothing", dry_run_writes_nothing),
        ("post URLs normalise identically to the website (shared cases)", normalizes_urls),
    ]:
        go(name, fn)
    return results


MUTATIONS = [
    (
        "dropping the clash guard",
        [
            (
                "WHERE ch.platform = :p AND v.utm_content = :content AND v.slug <> :slug LIMIT 1",
                "WHERE false LIMIT 1",
            )
        ],
        "a clash with an existing variant is reported and creates no second one",
    ),
    (
        "dating a campaign at ingest time",
        [('"starts_at": link["created_at"],', '"starts_at": sa.func.now(),')],
        "a campaign is dated at its first link, not at ingest time",
    ),
    (
        "matching posts on the raw, un-normalised permalink",
        [("normalized = normalize_post_url(permalink)", "normalized = permalink")],
        "a post attached with a dressed-up URL is matched to its social_post",
    ),
    (
        "labelling events that already have attribution",
        [
            (
                "WHERE e.campaign_variant_id IS NULL\n       AND e.channel_id IS NULL\n"
                "       AND e.campaign_id IS NULL\n       AND ",
                "WHERE ",
            )
        ],
        "late events are labelled; an already-labelled event is never changed",
    ),
    (
        "creating a new channel instead of reusing the platform's",
        [
            (
                '"SELECT id, slug, name FROM channel WHERE platform = :p '
                'ORDER BY created_at, slug"',
                '"SELECT id, slug, name FROM channel WHERE false"',
            )
        ],
        "each link gets its own variant under the right channel; existing channels are reused",
    ),
]


def main() -> int:
    try:
        real = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    pid = os.getpid()
    wh_name, src_name = f"dk_verify_links_wh_{pid}", f"dk_verify_links_src_{pid}"
    wh_url = real.database_url.set(database=wh_name).render_as_string(hide_password=False)
    src_url_obj = real.database_url.set(database=src_name)
    wh = load_settings({"DATABASE_URL": wh_url})
    src = load_settings({"DATABASE_URL": src_url_obj.render_as_string(hide_password=False)})
    source = OrderingSourceSettings(sqlalchemy_url=wh.sqlalchemy_url.set(database=src_name))

    failures: list[str] = []
    total = 0
    print(f"building throwaway databases {wh_name} and {src_name} ...")
    bootstrap_db.create(wh)
    bootstrap_db.create(src)
    wh_engine = sa.create_engine(wh.sqlalchemy_url, poolclass=sa.pool.NullPool)
    src_engine = sa.create_engine(src.sqlalchemy_url, poolclass=sa.pool.NullPool)
    try:
        _migrate(wh_url)
        env = Env(wh_engine, src_engine, wh, source)

        for name, problems in scenarios(real_module, env).items():
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
            caught = bool(scenarios(_mutant(edits), env)[expect])
            if caught:
                print(f"  ok    caught: {label}")
            else:
                failures.append(f"mutation survived: {label}")
                print(f"  FAIL  NOT caught: {label}")
    finally:
        wh_engine.dispose()
        src_engine.dispose()
        bootstrap_db.drop(wh, yes=True)
        bootstrap_db.drop(src, yes=True)

    print()
    if failures:
        print(f"FAILED {len(failures)}/{total} checks: {'; '.join(failures)}")
        return 1
    print(f"all {total} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
