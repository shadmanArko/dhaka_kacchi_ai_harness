"""Query -> embed -> hybrid retrieval (dense + keyword) -> top-k chunks.

Every query gets TWO rankings, which retrieve() merges: a DENSE one (pgvector
similarity between the query's embedding and each chunk's stored embedding)
and a KEYWORD one (Postgres full-text search over the chunk text), fused by
reciprocal rank. The keyword half was added 2026-10-02 (RAG_progress.md
decision #32) after a real failure: the query "what is transformers ?" ranked
a SEC contract chunk containing the company name "Transformair" ABOVE the
Transformer paper's abstract - the query's dense scores sat in one flat
0.45-0.49 band where relevant and irrelevant chunks were barely
distinguishable, while the keyword half cleanly excluded the contract and
kept the papers.

Multi-store (rag/MULTI_STORE_DESIGN.md): a caller names WHICH store to search
using its LOGICAL name, and this module resolves that name to a physical
table through the registry. Naming a store is routing, not authority - the
credential carried in `settings` decides, through Postgres grants, whether
the table can be read at all. Nothing here trusts the caller.

Connects as whichever reader role its `settings` carry - read-only in every
case, per RAG_progress.md decisions #4/#7. Ran for real against Postgres many
times since 2026-10-01 (an earlier "untested" note here had gone stale).
"""

from __future__ import annotations

import sqlalchemy as sa

# Same per-connection adapter registration ingest.py needs - a query vector
# is also a plain Python list[float] on our side, and psycopg needs the same
# teaching to compare it against the `vector` column.
from pgvector.psycopg import register_vector

# The shared identifier-quoting helper. The physical table name is the one
# value that has to be spliced into SQL text rather than bound as a
# parameter, so it is escaped with exactly the same function the setup
# scripts use.
from rag.bootstrap_db import quote_identifier

# The registry types, plus the single exception that BOTH "no such store" and
# "not allowed to read that store" are reported as.
from rag.config import RagReaderSettings, Store, StoreRegistry, UnknownStoreError
from rag.embedding import embed_texts

# How many candidates each half of the hybrid contributes to the fusion
# below. Deeper than any caller's top_k on purpose: a chunk the dense side
# ranked 30th may be the one the keyword side ranks 1st, and the fusion needs
# to SEE both opinions before it can rank anything.
_DENSE_CANDIDATES = 50
_KEYWORD_CANDIDATES = 50

# The RRF "k" smoothing constant (from the original Reciprocal Rank Fusion
# paper - k=60 is the widely used default). Each list contributes
# 1/(k + position) to a chunk's fused score; the constant keeps a single
# list's #1 from steamrolling a chunk both lists agree on (without it,
# position 1 vs 2 would differ by 2x instead of a gentle 1/61 vs 1/62).
_RRF_K = 60


# ---------------------------------------------------------------------------
# The three queries
# ---------------------------------------------------------------------------
#
# Each is now BUILT per call from the resolved physical table name, instead of
# being a module-level constant pointed at one hardcoded table. Two things
# about that are worth being explicit about:
#
#   1. The table name comes from stores.toml via the registry - never from the
#      caller. A caller's `store` argument is only ever used as a dictionary
#      key (see config.py's StoreRegistry.get), so it cannot reach this string
#      in any form.
#   2. It is still quoted through quote_identifier() before being spliced in,
#      because a table name CANNOT be a bind parameter - Postgres has no
#      syntax for "a parameter standing in for a name" - and defence in depth
#      costs nothing here.
#
# Building the string per call costs microseconds against a query that scans
# thousands of vectors, so caching the TextClause would add state for no
# measurable gain.


def _similarity_search_sql(table: str) -> sa.TextClause:
    """The DENSE half: pgvector similarity search against `table`.

    Written as one hand-checked string (not built through sa.table() /
    pg_insert() like ingest.py's writes) since a read-only ORDER BY ... LIMIT
    query is simple enough that the extra query-builder machinery wouldn't buy
    anything here - same "plain SQL where plain SQL is clearest" instinct as
    the rest of this codebase's hand-written queries (e.g.
    warehouse/queries/*.sql).

      - "embedding <#> CAST(:query_vector AS vector)" is pgvector's
        negative-inner-product operator - see RAG_progress.md decision #16 for
        why this one, not <=> or <->, given our vectors are pre-normalized.
        The explicit CAST is required: unlike an INSERT (where Postgres
        already knows the target column's type and can coerce a plain array to
        it), this query gives Postgres nothing to infer the parameter's type
        from other than the <#> operator itself - without it, psycopg sends
        the Python list as a generic "double precision[]" array, and <#> has
        no matching overload for that pairing.
        `CAST(... AS vector)` rather than the terser `::vector` shorthand
        specifically because SQLAlchemy's text() bind-parameter parser
        misreads ":query_vector::vector" - the second ":" confuses its own
        ":name" bind-parameter syntax, leaving the parameter unsubstituted
        entirely. Found by actually running this query twice, two different
        failures.
      - Aliased "AS distance" so the caller can see how confident/close each
        match actually was, not just get back an unordered list of text - also
        exactly what the web UI displays per result.
      - LIMIT :candidate_k (not the caller's top_k): this is only ONE half of
        the hybrid - retrieve() fetches a wide pool from each half, fuses
        them, and only then trims to the caller's top_k.
    """
    return sa.text(
        f"""
        SELECT
            id,
            source_type,
            source_path,
            chunk_index,
            chunk_text,
            metadata,
            embedding <#> CAST(:query_vector AS vector) AS distance
        FROM {quote_identifier(table)}
        ORDER BY embedding <#> CAST(:query_vector AS vector) ASC
        LIMIT :candidate_k
        """
    )


def _keyword_search_sql(table: str) -> sa.TextClause:
    """The KEYWORD half: Postgres full-text search over `table`.

    For the cases dense similarity is mushy on (short/generic queries; names
    like "Transformair" that sit near "transformers" in embedding space for no
    useful reason) or plainly blind to (exact codes such as an agreement
    number, which mean nothing semantically).

      - 'english' config: English stemming means a query for "transformers"
        also matches the literal word "Transformer" inside the papers (both
        stem to "transform"), and the English stopword list drops filler words
        ("what", "is") so a question-shaped query reduces to its real terms.
        Bengali text passes through as whole-word tokens, which still matches
        exact Bengali words in the social corpus.
      - plainto_tsquery ANDs the surviving terms; a query whose terms appear
        nowhere (or which reduces to nothing at all) simply returns ZERO rows
        - retrieve() then degrades gracefully to dense-only.
      - The same distance expression is selected here too, so every row that
        reaches the fused result already carries its cosine score for the UI,
        no matter which half it came from.
    """
    return sa.text(
        f"""
        SELECT
            id,
            source_type,
            source_path,
            chunk_index,
            chunk_text,
            metadata,
            embedding <#> CAST(:query_vector AS vector) AS distance,
            ts_rank_cd(
                to_tsvector('english', chunk_text),
                plainto_tsquery('english', :query)
            ) AS keyword_rank
        FROM {quote_identifier(table)}
        WHERE to_tsvector('english', chunk_text) @@ plainto_tsquery('english', :query)
        ORDER BY keyword_rank DESC
        LIMIT :candidate_k
        """
    )


def _neighbor_sql(table: str) -> sa.TextClause:
    """Look up one chunk's immediate neighbours, inside one store.

    "Neighbours" means same source_path, one chunk_index lower and one higher.
    This is what lets the web UI show a few words before/after a matched chunk
    WITHOUT storing any extra text at ingestion time: the neighbouring text is
    already sitting in the table as its own row, since chunk_text()'s overlap
    makes adjacent chunks share content at their boundary.

    Scoped to the same table - and therefore the same store - on purpose: a
    neighbour lookup must never reach across into another store, which is what
    stops neighbour context from becoming a side door around the isolation.
    """
    return sa.text(
        f"""
        SELECT chunk_index, chunk_text
        FROM {quote_identifier(table)}
        WHERE source_path = :source_path
          AND chunk_index IN (:before_index, :after_index)
        """
    )


def _reader_engine(settings: RagReaderSettings) -> sa.Engine:
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)

    # Same reasoning as ingest.py's _writer_engine: register pgvector's
    # adapter on every new low-level connection this engine opens, so a plain
    # Python list can be bound as :query_vector below.
    @sa.event.listens_for(engine, "connect")
    def _register_vector_type(dbapi_connection, connection_record):
        del connection_record
        register_vector(dbapi_connection)

    return engine


def _role_can_read(conn: sa.Connection, table: str) -> bool:
    """Ask Postgres whether the CONNECTED role may SELECT from `table`.

    `has_table_privilege` with no role argument answers for `current_user` -
    the identity this very connection authenticated as. That is exactly the
    question we want answered, and it means no role name has to be passed
    around or guessed. It also accounts for privileges INHERITED through role
    membership, which is how rag_internal_reader can read public stores it was
    never granted directly.

    The table name is a bind PARAMETER here, not spliced text: this is a
    function call taking a string, not an identifier position, so there is
    nothing to quote.

    This is deliberately NOT the security boundary - it is UX and information
    hiding. If it were deleted entirely, the search query would still fail
    inside Postgres with a permission error, because the grant simply is not
    there (design doc sections 7.1 and 13).
    """
    return bool(
        conn.execute(
            sa.text("SELECT has_table_privilege(:table, 'SELECT')"),
            {"table": table},
        ).scalar()
    )


def list_stores(*, registry: StoreRegistry, settings: RagReaderSettings) -> list[dict]:
    """Every store this credential can actually read, each with its
    description.

    This is the "menu" an agent is shown, and what the tool description
    enumerates (design doc section 8). The filtering is the important part:
    the list is derived by ASKING THE DATABASE which tables the connected role
    may read, never by reading the `visibility` field in stores.toml. That is
    what keeps the menu honest - a config that had drifted from the real
    grants could otherwise hand a public caller a menu entry naming an
    internal store, which is an information leak even though the subsequent
    read would fail.

    Returns plain dicts (not Store objects) so callers get a shape that is
    safe to serialise straight into JSON or a prompt, without accidentally
    exposing registry internals.
    """
    with _reader_engine(settings).connect() as conn:
        return [
            {
                # The LOGICAL name - what a caller passes back to retrieve().
                "store": store.name,
                # The registry's description, which is prompt text (§8.3).
                "description": store.description,
                # For display only. Never used for any access decision - the
                # filtering above already happened, in the database.
                "visibility": store.visibility,
            }
            for store in registry
            if _role_can_read(conn, store.table)
        ]


def _resolve_readable_store(
    conn: sa.Connection, registry: StoreRegistry, store: str
) -> Store:
    """Return the Store `store` names, or raise UnknownStoreError.

    Two situations are deliberately collapsed into one error (design doc
    section 7.2):

      1. the logical name is not in the registry at all, and
      2. it is, but this connection's role has no SELECT grant on its table.

    If those could be told apart, a public caller asking for "hr_policies"
    would learn from the error message that an internal store exists. One
    exception type, one message, no distinction - so that knowledge is simply
    not available to anyone who should not have it.
    """
    # Step 1: resolve the logical name. registry.get() raises
    # UnknownStoreError itself if the name is unknown, and this lookup is the
    # ONLY thing that ever happens with a caller-supplied string - a
    # dictionary key, never SQL.
    resolved = registry.get(store)

    # Step 2: ask the database whether this connection may read its table.
    if not _role_can_read(conn, resolved.table):
        # Note the message is built from the CALLER's string, not from
        # resolved.table: naming the physical table here would leak the
        # schema layout, and it would differ from the unknown-name message,
        # which is exactly the distinction we are erasing.
        raise UnknownStoreError(f"unknown store {store!r}")

    # Both checks passed - hand back the resolved store so callers do not have
    # to look it up a second time.
    return resolved


def can_read_store(
    *,
    store: str,
    registry: StoreRegistry,
    settings: RagReaderSettings,
) -> bool:
    """Whether this credential may read `store` - the same question
    retrieve() asks, answered as a plain True/False instead of by raising.

    Exists for callers that need to CHECK rather than search: webui.py's
    file-serving endpoint (which must not hand out a document belonging to a
    store this credential cannot read) and the stress test. Returning False
    for both "unknown store" and "not allowed" preserves the uniform-error
    rule - a caller cannot use this to learn which stores exist.
    """
    with _reader_engine(settings).connect() as conn:
        try:
            _resolve_readable_store(conn, registry, store)
        except UnknownStoreError:
            return False

    return True


def store_for_source_path(
    source_path: str, *, registry: StoreRegistry
) -> Store | None:
    """Which store a stored `source_path` belongs to, or None if none claims
    it.

    A source_path is always "<source_dir>/<something>" for a file-backed store
    (load_files.py builds it that way), so the owning store is decided by
    folder prefix - the same rule that decided it at ingestion time, and the
    same rule a human reading the folder tree would apply.

    Deliberately registry-only and credential-free: this says which store a
    path BELONGS to, not whether anyone may read it. Callers must still ask
    can_read_store() before acting on the answer - which is exactly the
    two-step webui.py's file endpoint performs.
    """
    # The LONGEST matching prefix wins, not the first one found: with folders
    # "public" and "public/extra" declared, "public/extra/x.md" belongs to the
    # second. Comparing every candidate and keeping the longest is what makes
    # nested store folders behave the way a reader would expect.
    best: Store | None = None
    for store in registry:
        # A store with no source_dir is fed from somewhere other than a folder
        # (a CSV export, say), so no file path can belong to it.
        if not store.source_dir or not source_path.startswith(f"{store.source_dir}/"):
            continue
        if best is None or len(store.source_dir) > len(best.source_dir):
            best = store

    return best


def get_chunk_neighbors(
    *,
    store: str,
    source_path: str,
    chunk_index: int,
    registry: StoreRegistry,
    settings: RagReaderSettings,
) -> dict:
    """Return the text of the chunk immediately before and immediately after
    the given (source_path, chunk_index), inside the named store.

    Returns {"before": str | None, "after": str | None} - None for either side
    that doesn't exist (chunk_index=0 has no "before", and the last chunk of a
    source has no "after").
    """
    with _reader_engine(settings).connect() as conn:
        # Same resolution and permission check as a search: neighbour context
        # must not become a side door into a store the caller cannot read.
        resolved = _resolve_readable_store(conn, registry, store)

        rows = (
            conn.execute(
                _neighbor_sql(resolved.table),
                {
                    "source_path": source_path,
                    "before_index": chunk_index - 1,
                    "after_index": chunk_index + 1,
                },
            )
            .mappings()
            .all()
        )

    # Build a lookup from chunk_index -> chunk_text out of whichever neighbor
    # rows actually came back (there may be zero, one, or two), then pull
    # "before"/"after" out of it by the exact index we asked for - simpler
    # than trying to figure out from row order alone which result was the
    # "before" one and which was the "after" one.
    by_index = {row["chunk_index"]: row["chunk_text"] for row in rows}
    return {
        "before": by_index.get(chunk_index - 1),
        "after": by_index.get(chunk_index + 1),
    }


def _fuse_by_reciprocal_rank(
    dense_rows: list[dict], keyword_rows: list[dict], *, limit: int
) -> list[dict]:
    """Merge the dense and keyword rankings into one list.

    Reciprocal rank fusion: a chunk's score is the sum of 1/(k + its position)
    over every list it appears in, so a chunk that BOTH halves rank well beats
    a chunk only one half loves. Concretely, for decision #32's failure: the
    "Transformair" contract chunk leads the dense list but is absent from the
    keyword list (nothing in it matches "transform"), so it collects only its
    dense contribution - and falls below chunks the dense side ranked slightly
    lower but that the keyword side also picked up.

    Ties are broken by whichever chunk reached the score dict first -
    deterministic, if arbitrary; this is a ranking device, not a calibrated
    confidence measure.
    """
    fused_scores: dict = {}
    rows_by_id: dict = {}

    # Walk each list in rank order (both SQL statements already ordered
    # themselves), adding each chunk's reciprocal-rank contribution. A chunk
    # appearing in both lists simply accumulates two contributions.
    for rank, row in enumerate(dense_rows, start=1):
        fused_scores[row["id"]] = fused_scores.get(row["id"], 0.0) + 1.0 / (_RRF_K + rank)
        rows_by_id[row["id"]] = row
    for rank, row in enumerate(keyword_rows, start=1):
        fused_scores[row["id"]] = fused_scores.get(row["id"], 0.0) + 1.0 / (_RRF_K + rank)
        # setdefault: when a row is in both lists, keep whichever copy
        # arrived first (the row content is identical either way).
        rows_by_id.setdefault(row["id"], row)

    # Descending fused score, trimmed to what the caller actually asked for.
    # When the keyword half found nothing, this reduces exactly to the dense
    # order (1/(k + rank) is strictly decreasing in rank).
    ranked_ids = sorted(fused_scores, key=fused_scores.__getitem__, reverse=True)
    return [rows_by_id[i] for i in ranked_ids[:limit]]


def retrieve(
    query: str,
    *,
    store: str,
    top_k: int,
    registry: StoreRegistry,
    settings: RagReaderSettings,
) -> list[dict]:
    """Embed `query`, search ONE named store for it, and return the best
    `top_k` chunks.

    `store` is the LOGICAL name from rag/stores.toml (e.g. "social_share"),
    not a table name. It selects where to look; it grants nothing. Whether the
    search actually runs is decided by the credential inside `settings`,
    through the grants Postgres holds - so an unreadable store fails with the
    same UnknownStoreError an unrecognised one does.

    One store per query, deliberately (design doc sections 3.3 and 15): that
    is what keeps this a single query and leaves the fused ranking below
    exactly as it was before multi-store existed. Searching several stores at
    once would raise the open question of what "best answer" means across
    corpora - a real design question, not a formatting one, and not one this
    subsystem needs answered yet.

    Each returned dict has: id, source_type, source_path, chunk_index,
    chunk_text, metadata, distance (negative inner product of the two unit
    vectors - effectively minus the cosine similarity - so lower is more
    similar; note the fused ORDER can deviate slightly from pure distance
    order, on purpose - that deviation is exactly what the keyword
    contribution buys).
    """
    # embed_texts() takes a batch (see RAG_progress.md decision #13) -
    # wrapping the single query string in a one-element list reuses the exact
    # same embedding function ingestion uses for chunk text, then [0] pulls
    # the one vector back out of the one-element result.
    query_vector = embed_texts([query])[0]

    with _reader_engine(settings).connect() as conn:
        # Resolve the logical name and confirm this credential may read the
        # table BEFORE running anything against it. Step 1 is the only use of
        # the caller's string; step 2 asks the database. Neither is the real
        # enforcement - the grant is - but together they turn what would
        # otherwise be a raw permission error into a clean, uniform
        # "unknown store".
        resolved = _resolve_readable_store(conn, registry, store)

        # The caller's name has done its job; from here on only the CONFIG's
        # table name is used.
        table = resolved.table

        dense_rows = (
            conn.execute(
                _similarity_search_sql(table),
                {"query_vector": query_vector, "candidate_k": _DENSE_CANDIDATES},
            )
            .mappings()
            .all()
        )
        keyword_rows = (
            conn.execute(
                _keyword_search_sql(table),
                {
                    "query": query,
                    "query_vector": query_vector,
                    "candidate_k": _KEYWORD_CANDIDATES,
                },
            )
            .mappings()
            .all()
        )

    # `.mappings()` returns dict-likes keyed by column name; dict(row)
    # converts them to plain dicts (a simpler, more predictable type for
    # whatever calls retrieve() next, e.g. the future agent interface), then
    # the fusion merges the two lists into the final ranking.
    return _fuse_by_reciprocal_rank(
        [dict(row) for row in dense_rows],
        [dict(row) for row in keyword_rows],
        limit=top_k,
    )
