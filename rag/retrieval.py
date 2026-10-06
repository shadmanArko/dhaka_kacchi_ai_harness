"""Query -> embed -> hybrid retrieval (dense + keyword) -> top-k chunks.

Every query gets TWO rankings, which retrieve() merges: a DENSE one
(pgvector similarity between the query's embedding and each chunk's
stored embedding) and a KEYWORD one (Postgres full-text search over the
chunk text), fused by reciprocal rank. The keyword half was added
2026-10-02 (RAG_progress.md decision #32) after a real failure: the
query "what is transformers ?" ranked a SEC contract chunk containing
the company name "Transformair" ABOVE the Transformer paper's abstract -
the query's dense scores sat in one flat 0.45-0.49 band where relevant
and irrelevant chunks were barely distinguishable, while the keyword
half cleanly excluded the contract and kept the papers.

Connects as rag_reader (RagReaderSettings) - structurally read-only, per
RAG_progress.md decisions #4/#7. Ran for real against Postgres many
times since 2026-10-01 (an earlier "untested" note here had gone stale).
"""

from __future__ import annotations

import sqlalchemy as sa

# Same per-connection adapter registration ingest.py needs - a query
# vector is also a plain Python list[float] on our side, and psycopg needs
# the same teaching to compare it against the `vector` column.
from pgvector.psycopg import register_vector

from rag.config import RagReaderSettings
from rag.embedding import embed_texts

# How many candidates each half of the hybrid contributes to the fusion
# below. Deeper than any caller's top_k on purpose: a chunk the dense
# side ranked 30th may be the one the keyword side ranks 1st, and the
# fusion needs to SEE both opinions before it can rank anything.
_DENSE_CANDIDATES = 50
_KEYWORD_CANDIDATES = 50

# The RRF "k" smoothing constant (from the original Reciprocal Rank
# Fusion paper - k=60 is the widely used default). Each list contributes
# 1/(k + position) to a chunk's fused score; the constant keeps a single
# list's #1 from steamrolling a chunk both lists agree on (without it,
# position 1 vs 2 would differ by 2x instead of a gentle 1/61 vs 1/62).
_RRF_K = 60


# The DENSE half: pgvector similarity search. Written as one hand-checked
# string (not built through sa.table()/pg_insert() like ingest.py's
# writes) since a read-only ORDER BY ... LIMIT query is simple enough
# that the extra query-builder machinery wouldn't buy anything here -
# same "plain SQL where plain SQL is clearest" instinct as the rest of
# this codebase's hand-written queries (e.g. warehouse/queries/*.sql).
#   - "embedding <#> CAST(:query_vector AS vector)" is pgvector's
#     negative-inner-product operator - see RAG_progress.md decision #16
#     for why this one, not <=> or <->, given our vectors are pre-
#     normalized. The explicit CAST is required: unlike an INSERT (where
#     Postgres already knows the target column's type and can coerce a
#     plain array to it), this query gives Postgres nothing to infer the
#     parameter's type from other than the <#> operator itself - without
#     it, psycopg sends the Python list as a generic "double precision[]"
#     array, and <#> has no matching overload for that pairing.
#     `CAST(... AS vector)` rather than the terser `::vector` shorthand
#     specifically because SQLAlchemy's text() bind-parameter parser
#     misreads ":query_vector::vector" - the second ":" confuses its own
#     ":name" bind-parameter syntax, leaving the parameter unsubstituted
#     entirely. Found by actually running this query twice, two different
#     failures.
#   - Aliased "AS distance" so the caller can see how confident/close each
#     match actually was, not just get back an unordered list of text -
#     also exactly what the web UI displays per result.
#   - LIMIT :candidate_k (not the caller's top_k): this is now only ONE
#     half of the hybrid - retrieve() fetches a wide pool from each half,
#     fuses them, and only then trims to the caller's top_k.
_SIMILARITY_SEARCH_SQL = sa.text(
    """
    SELECT
        id,
        source_type,
        source_path,
        chunk_index,
        chunk_text,
        metadata,
        embedding <#> CAST(:query_vector AS vector) AS distance
    FROM chunks
    ORDER BY embedding <#> CAST(:query_vector AS vector) ASC
    LIMIT :candidate_k
    """
)


# The KEYWORD half: Postgres' own full-text search over the same chunk
# text - for the cases dense similarity is mushy on (short/generic
# queries; names like "Transformair" that sit near "transformers" in
# embedding space for no useful reason) or plainly blind to (exact codes
# such as an agreement number, which mean nothing semantically).
#   - 'english' config: English stemming means a query for "transformers"
#     also matches the literal word "Transformer" inside the papers (both
#     stem to "transform"), and the English stopword list drops filler
#     words ("what", "is") so a question-shaped query reduces to its real
#     terms. Bengali text passes through as whole-word tokens, which
#     still matches exact Bengali words in the social corpus.
#   - plainto_tsquery ANDs the surviving terms; a query whose terms
#     appear nowhere (or which reduces to nothing at all) simply returns
#     ZERO rows - retrieve() then degrades gracefully to dense-only.
#   - The same distance expression is selected here too, so every row
#     that reaches the fused result already carries its cosine score for
#     the UI, no matter which half it came from.
_KEYWORD_SEARCH_SQL = sa.text(
    """
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
    FROM chunks
    WHERE to_tsvector('english', chunk_text) @@ plainto_tsquery('english', :query)
    ORDER BY keyword_rank DESC
    LIMIT :candidate_k
    """
)


def _reader_engine(settings: RagReaderSettings) -> sa.Engine:
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)

    # Same reasoning as ingest.py's _writer_engine: register pgvector's
    # adapter on every new low-level connection this engine opens, so a
    # plain Python list can be bound as :query_vector below.
    @sa.event.listens_for(engine, "connect")
    def _register_vector_type(dbapi_connection, connection_record):
        del connection_record
        register_vector(dbapi_connection)

    return engine


# Looks up the immediate neighbor chunks (one before, one after) of a
# given chunk WITHIN THE SAME SOURCE DOCUMENT - "same source_path, one
# chunk_index lower/higher." This is what lets the web UI show "a few
# words before/after" a matched chunk without storing any extra text at
# ingestion time: the neighboring text is already sitting in `chunks` as
# its own row, since chunk_text()'s overlap already makes adjacent chunks
# share some content at their boundary.
_NEIGHBOR_SQL = sa.text(
    """
    SELECT chunk_index, chunk_text
    FROM chunks
    WHERE source_path = :source_path
      AND chunk_index IN (:before_index, :after_index)
    """
)


def get_chunk_neighbors(
    *, source_path: str, chunk_index: int, settings: RagReaderSettings
) -> dict:
    """Return the text of the chunk immediately before and immediately
    after the given (source_path, chunk_index), if they exist.

    Returns {"before": str | None, "after": str | None} - None for either
    side that doesn't exist (e.g. chunk_index=0 has no "before", and the
    last chunk of a source has no "after").
    """
    with _reader_engine(settings).connect() as conn:
        rows = conn.execute(
            _NEIGHBOR_SQL,
            {
                "source_path": source_path,
                "before_index": chunk_index - 1,
                "after_index": chunk_index + 1,
            },
        ).mappings().all()

    # Build a lookup from chunk_index -> chunk_text out of whichever
    # neighbor rows actually came back (there may be zero, one, or two),
    # then pull "before"/"after" out of it by the exact index we asked
    # for - simpler than trying to figure out from row order alone which
    # result was the "before" one and which was the "after" one.
    by_index = {row["chunk_index"]: row["chunk_text"] for row in rows}
    return {
        "before": by_index.get(chunk_index - 1),
        "after": by_index.get(chunk_index + 1),
    }


def _fuse_by_reciprocal_rank(
    dense_rows: list[dict], keyword_rows: list[dict], *, limit: int
) -> list[dict]:
    """Merge the dense and keyword rankings into one list.

    Reciprocal rank fusion: a chunk's score is the sum of 1/(k + its
    position) over every list it appears in, so a chunk that BOTH halves
    rank well beats a chunk only one half loves. Concretely, for decision
    #32's failure: the "Transformair" contract chunk leads the dense list
    but is absent from the keyword list (nothing in it matches
    "transform"), so it collects only its dense contribution - and falls
    below chunks the dense side ranked slightly lower but that the
    keyword side also picked up.

    Ties are broken by whichever chunk reached the score dict first -
    deterministic, if arbitrary; this is a ranking device, not a
    calibrated confidence measure.
    """
    fused_scores: dict = {}
    rows_by_id: dict = {}

    # Walk each list in rank order (both SQL statements already ordered
    # themselves), adding each chunk's reciprocal-rank contribution. A
    # chunk appearing in both lists simply accumulates two contributions.
    for rank, row in enumerate(dense_rows, start=1):
        fused_scores[row["id"]] = fused_scores.get(row["id"], 0.0) + 1.0 / (_RRF_K + rank)
        rows_by_id[row["id"]] = row
    for rank, row in enumerate(keyword_rows, start=1):
        fused_scores[row["id"]] = fused_scores.get(row["id"], 0.0) + 1.0 / (_RRF_K + rank)
        # setdefault: when a row is in both lists, keep whichever copy
        # arrived first (the row content is identical either way).
        rows_by_id.setdefault(row["id"], row)

    # Descending fused score, trimmed to what the caller actually asked
    # for. When the keyword half found nothing, this reduces exactly to
    # the dense order (1/(k + rank) is strictly decreasing in rank).
    ranked_ids = sorted(fused_scores, key=fused_scores.__getitem__, reverse=True)
    return [rows_by_id[i] for i in ranked_ids[:limit]]


def retrieve(query: str, *, top_k: int, settings: RagReaderSettings) -> list[dict]:
    """Embed `query`, pull candidates from BOTH a dense (pgvector
    similarity) and a keyword (Postgres full-text) search, fuse the two
    rankings by reciprocal rank, and return the best `top_k`.

    Each returned dict has: id, source_type, source_path, chunk_index,
    chunk_text, metadata, distance (negative inner product of the two
    unit vectors - effectively minus the cosine similarity - so lower is
    more similar; note the fused ORDER can deviate slightly from pure
    distance order, on purpose - that deviation is exactly what the
    keyword contribution buys).

    Read-only end to end: connects as rag_reader, and both queries are
    plain SELECTs - structurally incapable of writing anything, which is
    the entire point of decision #4/#7's role split.
    """
    # embed_texts() takes a batch (see RAG_progress.md decision #13) -
    # wrapping the single query string in a one-element list reuses the
    # exact same embedding function ingestion uses for chunk text, then
    # [0] pulls the one vector back out of the one-element result.
    query_vector = embed_texts([query])[0]

    with _reader_engine(settings).connect() as conn:
        dense_rows = conn.execute(
            _SIMILARITY_SEARCH_SQL,
            {"query_vector": query_vector, "candidate_k": _DENSE_CANDIDATES},
        ).mappings().all()
        keyword_rows = conn.execute(
            _KEYWORD_SEARCH_SQL,
            {
                "query": query,
                "query_vector": query_vector,
                "candidate_k": _KEYWORD_CANDIDATES,
            },
        ).mappings().all()

    # `.mappings()` returns dict-likes keyed by column name; dict(row)
    # converts them to plain dicts (a simpler, more predictable type for
    # whatever calls retrieve() next, e.g. the future agent interface),
    # then the fusion merges the two lists into the final ranking.
    return _fuse_by_reciprocal_rank(
        [dict(row) for row in dense_rows],
        [dict(row) for row in keyword_rows],
        limit=top_k,
    )
