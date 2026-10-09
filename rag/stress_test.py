"""Stress-test and acceptance-test the RAG subsystem end to end.

    uv run python -m rag.stress_test            # everything
    uv run python -m rag.stress_test --quick    # skips the write + load tests

Every check below was written to answer a question someone will actually ask
about this system, in the order they matter:

  1. CONTENT      - is what should be in the stores actually in them?
  2. RETRIEVAL    - does a real question get the right chunk back, from the
                    right store, with the other store's content absent?
  3. ISOLATION    - can a public credential reach internal data? (It must not.)
                    Including with every application-level check removed - the
                    acceptance test from rag/MULTI_STORE_DESIGN.md section 13.
  4. ROBUSTNESS   - hostile and degenerate inputs: empty, huge, Bengali,
                    SQL-shaped, a caller naming a table instead of a store.
  5. MAINTENANCE  - re-ingest is idempotent; the prune deletes real orphans
                    and NOTHING else (the failure mode that would silently
                    wipe a corpus).
  6. LOAD         - many concurrent searches, timed. Reports p50/p95 rather
                    than asserting a threshold: this laptop is not production
                    hardware, and a number the reader can compare against
                    their own run is worth more than a pass/fail gate.

Exit code 0 = every check passed, 1 = at least one failed (each failure says
what was expected and what happened), 2 = the environment is unusable (a
missing credential, a store that was never ingested) - in which case the fix
is in rag/RUNBOOK.md, not in this file.

Nothing here is imported by the running system. It is a diagnostic tool, and
it is deliberately blunt: it writes real rows and deletes them again (section
5), so it needs the writer credential as well as the readers'.
"""

from __future__ import annotations

import concurrent.futures
import statistics
import sys
import time

import sqlalchemy as sa

# The public/internal reader credentials - the whole point of section 3 is to
# hold both at once and confirm they see different worlds.
from rag.config import (
    ConfigError,
    UnknownStoreError,
    load_rag_internal_reader_settings,
    load_rag_public_reader_settings,
    load_rag_writer_settings,
    load_store_registry,
)

# embed_texts is used by the scale section, which needs one vector to hand to
# EXPLAIN (retrieve() embeds internally and does not expose the vector).
from rag.embedding import embed_texts
from rag.ingest import ingest_source
from rag.retrieval import (
    can_read_store,
    get_chunk_neighbors,
    list_stores,
    retrieve,
    store_for_source_path,
)

# The two stores this system is built around, named here once so a future
# third store does not have to be threaded through every check by hand.
PUBLIC_STORE = "brand_book"
INTERNAL_STORE = "voice_and_rules"

# Retrieval-quality expectations: (store, question, a phrase that MUST appear
# in the top-5 chunks for that question).
#
# These are not exact-match assertions - retrieval returns chunks, and a chunk
# legitimately contains more than the sentence being looked for. Each expected
# phrase is chosen to be UNAMBIGUOUS: it appears in the document that should
# answer the question and nowhere else, so a hit cannot be luck.
GOLDEN_QUERIES: list[tuple[str, str, str]] = [
    # --- the brand book (public) -----------------------------------------
    (PUBLIC_STORE, "what is kacchi biryani?", "cooks it together with the rice"),
    (PUBLIC_STORE, "what drink is served with the kacchi?", "Borhani"),
    (PUBLIC_STORE, "do you deliver outside Berlin?", "don't deliver outside Berlin"),
    (PUBLIC_STORE, "how long does the food keep?", "3 days in refrigerator"),
    (PUBLIC_STORE, "what is the potato doing in my biryani?", "absorbs the ghee"),
    (PUBLIC_STORE, "when can I order and when is the deadline?", "deadline"),
    (PUBLIC_STORE, "is the meat halal?", "halal mutton"),
    (PUBLIC_STORE, "where did this restaurant come from?", "one dinner"),
    # --- voice and rules (internal) --------------------------------------
    (INTERNAL_STORE, "can we say we are the best kacchi in Berlin?", "unprovable superlatives"),
    (INTERNAL_STORE, "which language should a public post be in?", "German first"),
    (INTERNAL_STORE, "can we claim the Borhani is good for digestion?", "health claims"),
    (INTERNAL_STORE, "what should we post on Ashura?", "Solemn days"),
    # Note the phrase used here: the document writes the range with an en
    # dash ("1–3"), so an ASCII "1-3" would never match it. Picking a phrase
    # that needs no unusual punctuation is more robust than getting the dash
    # right by luck - and this one is unique to that bullet either way.
    (INTERNAL_STORE, "how many hashtags should a caption have?", "None, or 4 or more"),
    (INTERNAL_STORE, "what should we do about the founder using Alhamdulillah?", "Islamic phrases"),
]

# Phrases that exist ONLY in the internal document. If any of these ever
# appears in a public-store result, the public store is contaminated and the
# whole point of splitting the two is gone.
INTERNAL_ONLY_PHRASES = ["UWG", "blocked_words", "unprovable superlatives"]


class Report:
    """Collects check results and prints them as they happen.

    Deliberately simple: a check either passed, failed (with the expectation
    and what actually happened) or was skipped (with the reason). Every line
    is printed immediately, so a run that hangs or crashes still shows how far
    it got - which is the whole point of a diagnostic tool.
    """

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        """Record one pass/fail check. Returns `ok`, so callers can write
        `if not report.check(...): return` and stop a section early."""
        if ok:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            self.failures.append(f"{name}: {detail}")
            print(f"  FAIL  {name}")
            if detail:
                # Indented under its check, so a failure reads as one thought
                # rather than a line that wrapped.
                print(f"        {detail}")
        return ok

    def skip(self, name: str, reason: str) -> None:
        self.skipped += 1
        print(f"  SKIP  {name} ({reason})")

    def section(self, title: str) -> None:
        print(f"\n{title}")


# ---------------------------------------------------------------------------
# 1. Content - is the right text in the right table?
# ---------------------------------------------------------------------------


def check_content(report: Report, registry, settings) -> dict[str, int]:
    """Row counts, embedding shape, and the source_path/folder invariant.

    Returns {store name: row count} so later sections can tell "no rows" (an
    ingestion that never ran) apart from "rows but wrong" (a real bug).
    """
    report.section("1. CONTENT")

    counts: dict[str, int] = {}

    # One read-only connection, as the internal credential, for all the
    # catalogue-style questions below.
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)

    for store in registry:
        with engine.connect() as conn:
            count = conn.execute(
                sa.text(f"SELECT count(*) FROM {store.table}")
            ).scalar_one()
            counts[store.name] = count

        report.check(
            f"{store.name}: table {store.table} holds chunks",
            count > 0,
            "table is EMPTY - run `uv run python -m rag.reindex`",
        )
        if count == 0:
            continue

        with engine.connect() as conn:
            # Every embedding must be exactly the model's width (1024 for
            # bge-m3). A mixed-width table would make `<#>` fail at query
            # time - and it is what a half-finished model swap looks like.
            widths = (
                conn.execute(
                    sa.text(
                        f"SELECT DISTINCT vector_dims(embedding) FROM {store.table}"
                    )
                )
                .scalars()
                .all()
            )
            report.check(
                f"{store.name}: all embeddings are the same width",
                len(widths) == 1,
                f"found {len(widths)} different vector widths: {widths}",
            )

            # A file-backed store's rows must all point INTO that store's own
            # folder. This is the invariant webui.py's file-serving endpoint
            # relies on to decide which store a path belongs to, and the one
            # that makes "which tier is this content" answerable with `ls`.
            if store.source_dir:
                strays = conn.execute(
                    sa.text(
                        f"SELECT count(*) FROM {store.table} "
                        "WHERE source_path NOT LIKE :prefix"
                    ),
                    {"prefix": f"{store.source_dir}/%"},
                ).scalar_one()
                report.check(
                    f"{store.name}: every source_path sits under {store.source_dir}/",
                    strays == 0,
                    f"{strays} row(s) point outside the folder this store owns",
                )

            # Ingested rows must carry real text - an empty chunk is a row
            # that can never be retrieved and never be read.
            empty_text = conn.execute(
                sa.text(
                    f"SELECT count(*) FROM {store.table} "
                    "WHERE chunk_text IS NULL OR btrim(chunk_text) = ''"
                )
            ).scalar_one()
            report.check(
                f"{store.name}: no empty chunks",
                empty_text == 0,
                f"{empty_text} row(s) have empty chunk_text",
            )

    return counts


# ---------------------------------------------------------------------------
# 2. Retrieval - does a real question get the right answer?
# ---------------------------------------------------------------------------


def check_retrieval(report: Report, registry, settings) -> None:
    """Ask each golden question and confirm the expected phrase comes back.

    Reported as a hit-rate rather than all-or-nothing: a single miss in
    fourteen is worth seeing as a number ("13/14") and investigating, not as a
    wall of red that hides the twelve that worked. The section still FAILS if
    anything misses, because each query was chosen to be unambiguous.
    """
    report.section("2. RETRIEVAL (golden questions)")

    misses: list[str] = []

    for store, question, expected in GOLDEN_QUERIES:
        results = retrieve(
            question, store=store, top_k=5, registry=registry, settings=settings
        )
        haystack = " ".join(row["chunk_text"] for row in results)
        if expected.lower() not in haystack.lower():
            misses.append(f"{store}: {question!r} -> expected {expected!r} in top-5")

    total = len(GOLDEN_QUERIES)
    report.check(
        f"golden set: {total - len(misses)}/{total} questions answered from the right store",
        not misses,
        "\n        ".join(misses),
    )

    # Content isolation, the retrieval-quality half of section 3's access
    # isolation: a public-store search must never surface internal text, even
    # when the question is phrased to ask for it.
    leaked: list[str] = []
    for phrase in INTERNAL_ONLY_PHRASES:
        results = retrieve(
            phrase, store=PUBLIC_STORE, top_k=10, registry=registry, settings=settings
        )
        if any(phrase.lower() in row["chunk_text"].lower() for row in results):
            leaked.append(phrase)

    report.check(
        "public store never returns internal-only text",
        not leaked,
        f"internal phrases found in public results: {leaked}",
    )

    # Neighbour context must stay inside its own store: the chunk before a
    # brand-book section must come from the brand book, never from the
    # internal document - a side door the design explicitly closes.
    sample = retrieve(
        "what is kacchi biryani?", store=PUBLIC_STORE, top_k=1, registry=registry,
        settings=settings,
    )[0]
    neighbors = get_chunk_neighbors(
        store=PUBLIC_STORE,
        source_path=sample["source_path"],
        chunk_index=sample["chunk_index"],
        registry=registry,
        settings=settings,
    )
    neighbor_texts = [t for t in (neighbors["before"], neighbors["after"]) if t]
    report.check(
        "neighbour context comes from the same store and source",
        all(
            t in _texts_for_source(sample["source_path"], PUBLIC_STORE, registry, settings)
            for t in neighbor_texts
        ),
        "a neighbour chunk's text was not found under the same source_path",
    )


def _texts_for_source(source_path, store, registry, settings) -> set[str]:
    """Every chunk_text stored for one source_path, for the neighbour check
    above - proving a neighbour row really is a sibling of the matched chunk
    rather than something pulled from another store."""
    table = registry.get(store).table
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                f"SELECT chunk_text FROM {table} WHERE source_path = :p"
            ),
            {"p": source_path},
        ).scalars().all()
    return set(rows)


# ---------------------------------------------------------------------------
# 3. Isolation - the checks the whole design exists for
# ---------------------------------------------------------------------------


def check_isolation(report: Report, registry, public_settings, internal_settings) -> None:
    """Everything that must be true about who can read what.

    Sections 3a-3c mirror rag/MULTI_STORE_DESIGN.md section 13's checklist. The
    acceptance test is 3c: with every application-level check REMOVED, the
    database itself must still refuse.
    """
    report.section("3. ISOLATION (public vs internal)")

    # --- 3a. the menu each credential sees --------------------------------
    public_menu = [s["store"] for s in list_stores(registry=registry, settings=public_settings)]
    internal_menu = [s["store"] for s in list_stores(registry=registry, settings=internal_settings)]

    report.check(
        "public credential's menu is public stores only",
        INTERNAL_STORE not in public_menu,
        f"public menu was {public_menu}",
    )
    report.check(
        "public credential's menu is not empty",
        PUBLIC_STORE in public_menu,
        f"public menu was {public_menu} - its grant is missing?",
    )
    report.check(
        "internal credential's menu covers every store",
        set(internal_menu) == {s.name for s in registry},
        f"internal menu was {internal_menu} (registry has {[s.name for s in registry]})",
    )

    # --- 3b. the application-level refusal, per credential ----------------
    internal_table = registry.get(INTERNAL_STORE).table

    try:
        retrieve("anything", store=INTERNAL_STORE, top_k=1, registry=registry,
                 settings=public_settings)
        report.check(
            "public credential is refused an internal store by retrieve()",
            False,
            "retrieve() returned results for an internal store!",
        )
    except UnknownStoreError:
        report.check("public credential is refused an internal store by retrieve()", True)

    # --- 3c. THE ACCEPTANCE TEST ------------------------------------------
    # No application check at all: this is the raw SQL retrieve() would run,
    # sent to Postgres as the public role. If this ever succeeds, the
    # isolation was never structural - it was a Python `if`, and Python `if`s
    # are what a bug or a prompt injection walks through.
    engine = sa.create_engine(public_settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    refused = False
    detail = ""
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f"SELECT count(*) FROM {internal_table}")).scalar_one()
    except Exception as exc:
        # Which exception CLASS this arrives as depends on the driver (a
        # privilege error is SQLSTATE 42501, class 42, which psycopg reports as
        # a ProgrammingError - but pinning the test to that is pinning it to a
        # driver detail). What actually matters is that the database REFUSED,
        # so the refusal is recognised from the message and the SQLSTATE, and
        # anything else is reported as the unexpected thing it is.
        message = str(exc).lower()
        refused = "permission denied" in message or "42501" in message
        detail = f"unexpected error type: {type(exc).__name__}: {exc}"

    report.check(
        "ACCEPTANCE: raw SQL as the public role cannot read an internal table",
        refused,
        detail or "the query SUCCEEDED - internal data is exposed to the public role",
    )

    # The same acceptance test for the public table, to prove the refusal
    # above is a GRANT decision and not "this role cannot read anything".
    public_table = registry.get(PUBLIC_STORE).table
    allowed = True
    try:
        with engine.connect() as conn:
            conn.execute(sa.text(f"SELECT count(*) FROM {public_table}")).scalar_one()
    except Exception as exc:
        allowed = False
        detail = f"unexpected error: {exc}"

    report.check(
        "control: the same raw SQL CAN read a public table",
        allowed,
        detail,
    )

    # --- 3d. the uniform error --------------------------------------------
    # "Not allowed" and "does not exist" must be indistinguishable, or a
    # public caller could enumerate internal store names by asking.
    #
    # What "indistinguishable" means, precisely: the same exception type, the
    # same message SHAPE, and no extra information. The caller's own string is
    # echoed back (it has to be - "unknown store 'x'" is the message), so the
    # comparison is made with that one word masked out. What must NOT differ is
    # anything else: an extra sentence, a "permission denied", the physical
    # table name, a hint that the store exists.
    def _error_for(name: str) -> tuple[type, str]:
        try:
            retrieve(name, store=name, top_k=1, registry=registry, settings=public_settings)
        except UnknownStoreError as exc:
            # Mask the caller's own string, so the shapes can be compared.
            return (type(exc), str(exc).replace(repr(name), "<name>"))
        return (type(None), "")

    forbidden = _error_for(INTERNAL_STORE)
    nonexistent = _error_for("no_such_store_at_all")
    report.check(
        "forbidden and nonexistent stores give the same error",
        forbidden == nonexistent and forbidden[0] is UnknownStoreError,
        f"forbidden={forbidden!r} nonexistent={nonexistent!r}",
    )

    # And the message must not leak the physical table name or the word
    # "permission": either would tell a public caller that the store it asked
    # for is real, which is exactly the knowledge this check exists to deny it.
    forbidden_message = str(_error_for(INTERNAL_STORE)[1]).lower()
    internal_table_name = registry.get(INTERNAL_STORE).table
    report.check(
        "the refusal names neither the table nor a permission problem",
        internal_table_name not in forbidden_message
        and "permission" not in forbidden_message
        and "denied" not in forbidden_message,
        f"message was {forbidden_message!r}",
    )

    # --- 3e. the file-serving boundary ------------------------------------
    # /api/file decides what to serve by asking which store a path belongs to
    # and whether the credential can read it (webui.py). Both halves are
    # exercised here directly, since they are what stops an internal document
    # being handed to a public caller.
    internal_path = f"{registry.get(INTERNAL_STORE).source_dir}/voice-and-rules.md"
    owner = store_for_source_path(internal_path, registry=registry)
    report.check(
        "a file path resolves to its owning store",
        owner is not None and owner.name == INTERNAL_STORE,
        f"{internal_path!r} resolved to {owner and owner.name!r}",
    )
    report.check(
        "the public credential may NOT read that store (file serving refused)",
        not can_read_store(store=INTERNAL_STORE, registry=registry, settings=public_settings),
    )
    report.check(
        "the public credential MAY read the public store",
        can_read_store(store=PUBLIC_STORE, registry=registry, settings=public_settings),
    )
    report.check(
        "a path no store claims belongs to nothing (so it is never served)",
        store_for_source_path("facts.yaml", registry=registry) is None
        and store_for_source_path("../secrets.txt", registry=registry) is None,
    )


# ---------------------------------------------------------------------------
# 4. Robustness - inputs that are meant to break it
# ---------------------------------------------------------------------------


def check_robustness(report: Report, registry, settings) -> None:
    """Degenerate and hostile inputs. None may crash, none may corrupt.

    The caller-supplied `store` value is the interesting one: it is the only
    string from outside that reaches this subsystem, and the design's claim is
    that it can ONLY ever be a dictionary key (design doc section 9.1). These
    cases test that claim with the shapes an attacker would actually try.
    """
    report.section("4. ROBUSTNESS")

    # --- 4a. hostile store names ------------------------------------------
    # A physical table name, a SQL fragment, the empty string, a wrong-case
    # version of a real name, and a traversal attempt. All must raise the one
    # uniform error, and - the part that matters - nothing must change in the
    # database as a result.
    hostile_names = [
        registry.get(PUBLIC_STORE).table,          # the physical name, not the logical one
        f"{PUBLIC_STORE}; DROP TABLE {registry.get(PUBLIC_STORE).table}; --",
        f"{PUBLIC_STORE}' OR '1'='1",
        "",
        PUBLIC_STORE.upper(),
        "../../etc/passwd",
    ]

    bad_behaviour: list[str] = []
    for name in hostile_names:
        try:
            retrieve("x", store=name, top_k=1, registry=registry, settings=settings)
            bad_behaviour.append(f"{name!r} was ACCEPTED as a store name")
        except UnknownStoreError:
            pass  # exactly right: a rejected lookup, not a crash
        except Exception as exc:
            bad_behaviour.append(f"{name!r} raised {type(exc).__name__}: {exc}")

    report.check(
        "hostile store names are rejected, not executed",
        not bad_behaviour,
        "\n        ".join(bad_behaviour),
    )

    # And the tables are still there, with the same contents, after all that.
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    with engine.connect() as conn:
        counts = {
            store.name: conn.execute(
                sa.text(f"SELECT count(*) FROM {store.table}")
            ).scalar_one()
            for store in registry
        }
    report.check(
        "the stores survived the hostile names intact",
        all(count > 0 for count in counts.values()),
        f"row counts after the attack attempts: {counts}",
    )

    # --- 4b. degenerate queries -------------------------------------------
    # Each of these is a shape a real caller produces by accident: an empty
    # box, a pasted paragraph, a Bengali question, a query that is nothing but
    # stopwords (which reduces to an empty keyword query), and a SQL-looking
    # string (which is a bind parameter, so it is inert).
    degenerate_queries = {
        "empty": "",
        "whitespace": "   \n  ",
        "stopwords only": "what is the of and",
        "very long": "kacchi biryani " * 400,
        "bengali": "কাচ্চি বিরিয়ানি কোথায় পাওয়া যায়?",
        "sql-shaped": "'; DROP TABLE chunks_brand_book; --",
        "punctuation": "?!?!...---",
        "emoji": "🍛🔥🥛",
    }

    broken: list[str] = []
    for label, query in degenerate_queries.items():
        try:
            results = retrieve(
                query, store=PUBLIC_STORE, top_k=3, registry=registry, settings=settings
            )
            if len(results) > 3:
                broken.append(f"{label}: returned {len(results)} results for top_k=3")
        except Exception as exc:
            broken.append(f"{label}: {type(exc).__name__}: {exc}")

    report.check(
        "degenerate queries are handled without crashing",
        not broken,
        "\n        ".join(broken),
    )

    # top_k beyond the number of rows in the store: must return what exists,
    # not error.
    try:
        results = retrieve(
            "kacchi", store=PUBLIC_STORE, top_k=1000, registry=registry, settings=settings
        )
        report.check("top_k larger than the store is clamped by the data", len(results) > 0)
    except Exception as exc:
        report.check("top_k larger than the store is clamped by the data", False, str(exc))


# ---------------------------------------------------------------------------
# 5. Maintenance - re-ingest is idempotent, the prune deletes only orphans
# ---------------------------------------------------------------------------


def check_maintenance(report: Report, registry, settings, public_settings) -> None:
    """The two operations that can silently destroy a corpus if they are ever
    wrong: re-ingest (must not duplicate) and prune (must not over-delete).

    The prune test is the important one. Its failure mode - deleting rows
    whose source_path it failed to recognise - is invisible until someone
    searches and finds nothing, so it gets planted evidence: a synthetic
    orphan is written, the prune is run, and BOTH halves are checked: the
    orphan went, everything real stayed.
    """
    report.section("5. MAINTENANCE (idempotence and prune safety)")

    try:
        writer_settings = load_rag_writer_settings()
    except ConfigError as exc:
        report.skip("re-ingest is idempotent", f"no writer credential: {exc}")
        report.skip("the prune deletes orphans and nothing else", "no writer credential")
        return

    import rag.load_files as load_files  # local import: only needed here

    store = registry.get(PUBLIC_STORE)
    table = store.table

    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)

    def row_count() -> int:
        with engine.connect() as conn:
            return conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()

    # --- 5a. idempotence ---------------------------------------------------
    before = row_count()
    # One section of one file, re-ingested: cheap (a few embeddings) and
    # enough to prove the upsert path.
    text = "## Stress test section\n\nThe kacchi is cooked on dum for six hours."
    written_first = ingest_source(
        store=PUBLIC_STORE, registry=registry, text=text,
        source_path="public/__stress_test__.md::stress-test-section",
        source_type="markdown", chunk_size_tokens=store.chunk_size_tokens,
        overlap_tokens=store.overlap_tokens, settings=writer_settings,
    )
    after_first = row_count()
    try:
        written_second = ingest_source(
            store=PUBLIC_STORE, registry=registry, text=text,
            source_path="public/__stress_test__.md::stress-test-section",
            source_type="markdown", chunk_size_tokens=store.chunk_size_tokens,
            overlap_tokens=store.overlap_tokens, settings=writer_settings,
        )
        after_second = row_count()

        report.check(
            "re-ingesting the same source writes the same rows, not more",
            after_second == after_first and written_second == written_first,
            f"rows {before} -> {after_first} -> {after_second}; "
            f"writes {written_first} then {written_second}",
        )
    finally:
        # Clean up the planted rows whatever happened above, so a failure here
        # does not leave junk in a real store for the next person to find.
        deleted_by_prune = load_files.prune_orphaned(store)
        final = row_count()

    # How many rows the plant actually added - asked rather than assumed,
    # since a one-line section becomes one chunk today but would become two if
    # a store's chunk settings were ever retuned below its token count.
    planted = after_first - before

    # If a previous run died between planting and pruning, the planted path is
    # already in the table and the code above merely UPDATED it - which would
    # make the deletion check below pass while proving nothing. Named as its
    # own check so that state is visible rather than silently weakening the
    # test.
    report.check(
        "the store held no leftovers from an earlier run before planting",
        planted >= 1,
        "the planted source_path was already present - a previous run did not "
        "clean up after itself (the prune below removes it either way)",
    )

    report.check(
        "the prune removed every planted row and nothing else",
        final == before and deleted_by_prune == planted,
        f"expected {before} rows and {planted} deletion(s), got {final} rows "
        f"and {deleted_by_prune} deletions",
    )

    # --- 5b. the keep-list still matches what the loader writes -----------
    # Deleting and re-loading the whole public store would be the strongest
    # form of this check, but it costs a full re-embed of the brand book on
    # every stress run. Instead: the prune that just ran deleted exactly the
    # planted rows, which is the same proof - a keep-list that had drifted
    # from the loader's own paths would have deleted the entire store above.
    report.check(
        "the prune's keep-list matches the loader (nothing real was deleted)",
        final > 0,
        "the public store is now EMPTY - the keep-list and the loader disagree",
    )

    # --- 5c. the reader still sees the store after all that ---------------
    results = retrieve(
        "what is kacchi biryani?", store=PUBLIC_STORE, top_k=3,
        registry=registry, settings=public_settings,
    )
    report.check(
        "the public store still answers a question after the write cycle",
        len(results) == 3,
        f"got {len(results)} results",
    )


# ---------------------------------------------------------------------------
# 6. Load - many searches at once, timed
# ---------------------------------------------------------------------------


def check_load(report: Report, registry, settings, *, workers: int, per_worker: int) -> None:
    """Concurrent searches against both stores, timed.

    Every retrieve() call embeds its query on the CPU with bge-m3 before it
    touches the database, so this measures the whole path, not just Postgres.
    The numbers are reported, never asserted - see this module's docstring.
    """
    report.section(f"6. LOAD ({workers} concurrent workers x {per_worker} queries)")

    queries = [
        "what is kacchi biryani?",
        "how does delivery work?",
        "which words should we avoid?",
        "what is the deadline for ordering?",
        "can we make health claims?",
    ]
    # Spread the queries over both stores, so the load also exercises the
    # per-call table resolution rather than hammering one table.
    plan = [
        (queries[i % len(queries)], PUBLIC_STORE if i % 2 == 0 else INTERNAL_STORE)
        for i in range(per_worker)
    ]

    def one_search(pair: tuple[str, str]) -> float:
        query, store = pair
        started = time.perf_counter()
        retrieve(query, store=store, top_k=5, registry=registry, settings=settings)
        return time.perf_counter() - started

    started = time.perf_counter()
    errors: list[str] = []
    timings: list[float] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one_search, pair) for pair in plan * workers]
        for future in concurrent.futures.as_completed(futures):
            try:
                timings.append(future.result())
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

    wall = time.perf_counter() - started

    if not timings:
        report.check("concurrent searches all completed", False, "; ".join(errors))
        return

    timings.sort()
    p50 = statistics.median(timings)
    p95 = timings[min(len(timings) - 1, int(len(timings) * 0.95))]
    per_second = len(timings) / wall

    print(
        f"        {len(timings)} searches in {wall:.1f}s "
        f"({per_second:.2f} searches/s) - p50 {p50 * 1000:.0f}ms, "
        f"p95 {p95 * 1000:.0f}ms, slowest {timings[-1] * 1000:.0f}ms"
    )

    report.check(
        f"all {len(timings) + len(errors)} concurrent searches completed",
        not errors,
        "\n        ".join(errors[:5]),
    )
    # A very loose sanity bound only: if a search takes longer than 30s
    # something is wrong (a dead connection, a lock). The real number is the
    # one printed above.
    report.check(
        "no search took longer than 30s",
        timings[-1] < 30,
        f"slowest was {timings[-1]:.1f}s",
    )


# ---------------------------------------------------------------------------
# 7. Scale - what a bigger store costs (opt-in: --scale [N])
# ---------------------------------------------------------------------------


def check_scale(report: Report, registry, settings, *, chunks: int) -> None:
    """Ingest a synthetic corpus, measure retrieval at that size, then remove
    every trace of it.

    This exists to answer one question with a number instead of a guess: **how
    big does a store have to get before the missing vector index matters?**
    There is no IVFFlat/HNSW index by design (a sequential scan over a few
    dozen rows is faster than an index), and the honest way to keep that
    decision honest is to measure what a scan actually costs at, say, 2,000
    rows rather than to assume.

    It is opt-in (`--scale [N]`, N default 500) because it writes real rows and
    embeds real text - both undone before the function returns, and the store's
    row count is asserted back to where it started. The rows are written to a
    source_path under the public store's own folder
    ("public/__scale_test__.md"), so the content checks in section 1 would
    still hold if this ran before them.
    """
    report.section(f"7. SCALE (ingest {chunks} synthetic chunks, measure, remove)")

    try:
        writer_settings = load_rag_writer_settings()
    except ConfigError as exc:
        report.skip("scale test", f"no writer credential: {exc}")
        return

    store = registry.get(PUBLIC_STORE)
    table = store.table
    source_path = "public/__scale_test__.md"

    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)

    def row_count() -> int:
        with engine.connect() as conn:
            return conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()

    before = row_count()

    # A synthetic document long enough to split into `chunks` pieces. The
    # sentences vary by number so the chunks are not identical to each other
    # (identical text would embed to identical vectors, which is not what a
    # real corpus looks like and would flatter the scan).
    paragraph = (
        "Batch {n}: the mutton is marinated overnight, layered with basmati rice, "
        "sealed in the handi and cooked on dum for six hours. Borhani is served "
        "cold alongside it. "
    )
    text = "\n\n".join(paragraph.format(n=i) for i in range(chunks * 4))

    started = time.perf_counter()
    written = ingest_source(
        store=PUBLIC_STORE,
        registry=registry,
        text=text,
        source_path=source_path,
        source_type="markdown",
        chunk_size_tokens=store.chunk_size_tokens,
        overlap_tokens=store.overlap_tokens,
        settings=writer_settings,
    )
    ingest_seconds = time.perf_counter() - started

    after = row_count()
    report.check(
        f"{chunks} synthetic chunks were ingested (in {ingest_seconds:.1f}s, "
        f"{(after - before) / ingest_seconds:.1f} chunks/s including embedding)",
        after - before == written and written > 0,
        f"asked for ~{chunks} chunks, the loader wrote {written}",
    )

    try:
        # Retrieval at this size, and the plan the database actually chose.
        timings = []
        for _ in range(10):
            started = time.perf_counter()
            retrieve(
                "how is the mutton cooked?", store=PUBLIC_STORE, top_k=5,
                registry=registry, settings=settings,
            )
            timings.append(time.perf_counter() - started)
        timings.sort()

        print(
            f"        with {after} chunks in this store: p50 "
            f"{statistics.median(timings) * 1000:.0f}ms, "
            f"slowest {timings[-1] * 1000:.0f}ms"
        )

        # The plan is the point of this section: a Seq Scan with a Sort is the
        # expected shape now, and seeing it is what makes "add an index when it
        # stops being fast enough" a decision rather than a rumour.
        with engine.connect() as conn:
            plan = conn.execute(
                sa.text(
                    f"EXPLAIN SELECT id FROM {table} "
                    "ORDER BY embedding <#> CAST(:q AS vector) LIMIT 50"
                ),
                {"q": embed_texts(["how is the mutton cooked?"])[0]},
            ).scalars().all()
        scan_kind = next(
            (line.strip() for line in plan if "Scan" in line), "(no scan line found)"
        )
        print(f"        query plan: {scan_kind}")

        report.check(
            "retrieval still answers correctly with a larger store",
            len(
                retrieve(
                    "how is the mutton cooked?", store=PUBLIC_STORE, top_k=5,
                    registry=registry, settings=settings,
                )
            )
            == 5,
        )
    finally:
        # Remove every row this section wrote, however it ended. A DELETE by
        # source_path (not the prune, which would also consider the rest of
        # the folder) so the cleanup cannot touch anything else.
        with sa.create_engine(
            writer_settings.sqlalchemy_url, poolclass=sa.pool.NullPool
        ).begin() as conn:
            conn.execute(
                sa.text(f"DELETE FROM {table} WHERE source_path = :p"),
                {"p": source_path},
            )

    final = row_count()
    report.check(
        "the synthetic corpus left no trace",
        final == before,
        f"expected {before} rows after cleanup, found {final}",
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    quick = "--quick" in sys.argv

    # --scale [N]: opt in to the synthetic-corpus section, optionally naming
    # how many chunks to write. Parsed by hand (rather than with argparse)
    # because it is one optional value - the same instinct as reindex.py's
    # single positional argument.
    scale_chunks: int | None = None
    if "--scale" in sys.argv:
        index = sys.argv.index("--scale")
        following = sys.argv[index + 1] if index + 1 < len(sys.argv) else ""
        scale_chunks = int(following) if following.isdigit() else 500

    print("RAG stress test - see rag/RUNBOOK.md for what each section means")
    if quick:
        print("(--quick: skipping the write and load sections)")

    # Fail fast, and clearly, on the two ways the environment can be unusable:
    # a missing credential, and a registry that does not describe a working
    # pair of stores. Both are RUNBOOK problems, not bugs in this file.
    try:
        registry = load_store_registry()
        internal_settings = load_rag_internal_reader_settings()
        public_settings = load_rag_public_reader_settings()
    except ConfigError as exc:
        print(f"\nENVIRONMENT ERROR: {exc}", file=sys.stderr)
        print("See rag/RUNBOOK.md (Setup).", file=sys.stderr)
        return 2

    for name in (PUBLIC_STORE, INTERNAL_STORE):
        if name not in registry:
            print(
                f"\nENVIRONMENT ERROR: store {name!r} is not in rag/stores.toml "
                "- this test is written against the two brand stores.",
                file=sys.stderr,
            )
            return 2

    report = Report()

    counts = check_content(report, registry, internal_settings)
    check_retrieval(report, registry, internal_settings)
    check_isolation(report, registry, public_settings, internal_settings)
    check_robustness(report, registry, internal_settings)

    if quick:
        report.skip("maintenance checks", "--quick")
        report.skip("load test", "--quick")
    else:
        check_maintenance(report, registry, internal_settings, public_settings)
        check_load(report, registry, internal_settings, workers=4, per_worker=10)

    if scale_chunks is not None:
        check_scale(report, registry, internal_settings, chunks=scale_chunks)
    else:
        report.skip("scale test", "not requested (pass --scale [N] to run it)")

    print(
        f"\n{'=' * 70}\n"
        f"{report.passed} passed, {report.failed} failed, {report.skipped} skipped"
    )
    if report.failed:
        print("\nFAILURES:")
        for failure in report.failures:
            print(f"  - {failure}")
        print(f"\nRow counts at the end: {counts}")
        return 1

    print("Every check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
