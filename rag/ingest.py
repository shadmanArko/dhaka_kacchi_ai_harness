"""The ingestion pipeline: raw text in, upserted rows in `chunks` out.

Ties together chunking.chunk_text(), embedding.embed_chunks(), and
upsert.upsert_returning() into one idempotent step. Connects as
rag_writer (RagWriterSettings) - never the admin role, per
RAG_progress.md decisions #7/#8. Still UNTESTED against a real Postgres
instance as of 2026-09-25 - this machine has no Postgres yet (see the
TODO in RAG_progress.md). The vector/jsonb type-adaptation plumbing below
is believed correct but has not been confirmed against a live database.
"""

from __future__ import annotations

# `sa` for the lightweight sa.table()/sa.column() pattern this whole
# codebase uses for raw-SQL-shaped table references, same as
# warehouse/ingest/direct.py's orders_t/order_line_t.
import sqlalchemy as sa

# psycopg's own wrapper that tells it "serialize this Python dict as a
# jsonb value specifically" - without it, psycopg either doesn't know how
# to adapt a plain dict at all, or defaults to the plain `json` type
# instead of `jsonb`, which wouldn't match the column type we defined in
# schema.py.
from psycopg.types.json import Jsonb

# pgvector's psycopg integration - register_vector() teaches a psycopg
# connection how to convert a Python list[float] into Postgres's `vector`
# type (and back), on both INSERT and SELECT. Without this, a plain
# Python list handed to a `vector` column would fail to adapt.
from pgvector.psycopg import register_vector

from rag.chunking import chunk_text
from rag.config import RagWriterSettings, StoreRegistry
from rag.embedding import embed_chunks
from rag.upsert import upsert_returning

# The lightweight table reference this codebase uses for raw upserts
# (mirrors warehouse/ingest/direct.py's orders_t/order_line_t shape).
# `id` IS declared here even though this pipeline never supplies a value
# for it (it has a database-side default, gen_random_uuid()) - sa.table()
# objects only know about columns explicitly listed, and
# upsert_returning()'s default `returning=("id",)` needs chunks_t to be
# able to look up an "id" column to build `RETURNING id`, or it fails with
# a plain KeyError. Caught by actually running this against the real
# database (see RAG_progress.md's ingestion test log) - worth remembering
# as a general shape: a lightweight table reference needs every column
# ANY caller might reference, not just the ones being written to.
# `created_at` is still left out - nothing in this pipeline ever reads it
# back, unlike `id`.
#
# Multi-store: built PER CALL now, from the physical table name the registry
# resolved, instead of being one module-level constant aimed at a single
# hardcoded table. Every store table has an identical shape (see schema.py's
# _create_store_table), so this one column list is correct for all of them -
# only the name differs. `table_name` always originates in stores.toml, never
# from a caller.
def _chunks_table(table_name: str) -> sa.TableClause:
    """The lightweight table reference for one store's chunks table."""
    return sa.table(
        table_name,
        sa.column("id"),
        sa.column("source_type"),
        sa.column("source_path"),
        sa.column("chunk_index"),
        sa.column("chunk_text"),
        sa.column("embedding"),
        sa.column("metadata"),
        sa.column("updated_at"),
    )

# Which columns get overwritten when a chunk already exists at the same
# (source_path, chunk_index) - i.e. a real re-ingest of changed content.
# source_type/source_path/chunk_index are the conflict key itself (they're
# how we found the existing row, so there's nothing to "update" about
# them); created_at is deliberately excluded so the original insert time
# is preserved, matching decision #12/#10's created_at-vs-updated_at
# split.
_UPDATE_ON_CONFLICT = ("chunk_text", "embedding", "metadata", "updated_at")


def _writer_engine(settings: RagWriterSettings) -> sa.Engine:
    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)

    # SQLAlchemy fires this event every time it opens a brand new
    # low-level connection to Postgres (not on every query - connections
    # get reused from the pool). We use it to register pgvector's list<->
    # vector adapter on that specific connection, since the adapter has to
    # be registered per-connection, not globally for the whole process.
    @sa.event.listens_for(engine, "connect")
    def _register_vector_type(dbapi_connection, connection_record):
        # `del` tells linters this argument is deliberately unused - the
        # event always passes both, but this callback only needs the
        # first one.
        del connection_record
        register_vector(dbapi_connection)

    return engine


def ingest_source(
    *,
    store: str,
    registry: StoreRegistry,
    text: str,
    source_path: str,
    source_type: str,
    chunk_size_tokens: int,
    overlap_tokens: int,
    settings: RagWriterSettings,
    metadata: dict | None = None,
) -> int:
    """Chunk `text`, embed every chunk, and upsert all of them into the
    chunks table belonging to `store`. Returns how many rows were actually
    written (inserted or updated - see upsert_returning's own docstring for
    why a row that hit an unchanged conflict wouldn't count, if `update`
    weren't always passed here, which it is).

    `store` is the LOGICAL name from rag/stores.toml (e.g. "social_share"),
    and `registry` is the loaded registry that resolves it to a physical
    table. The caller is a loader, which already knows which store it feeds -
    so it says so, rather than this function trying to infer it.

    Note that chunk_size_tokens/overlap_tokens are still passed IN rather
    than read from the registry here, even though the registry holds them.
    That is deliberate (design doc section 5.3): those two are the LOADER's
    business, and keeping them as plain arguments leaves this function
    generic - equally usable by anything that is not registry-driven at all,
    such as a future importer feeding a table that has no registry entry yet.

    `metadata` (RAG_progress.md decision #20, "Option A"): a snapshot of
    whatever non-chunked information describes the SOURCE as a whole
    (e.g. a social_post_metrics row's platform/posted_at/permalink/
    engagement counts) - applied IDENTICALLY to every chunk produced from
    this one call, since it describes the source, not an individual
    chunk's position within it. Defaults to an empty dict for sources that
    genuinely have nothing extra worth keeping (e.g. a plain .txt file).

    Safe to call repeatedly on the same source: unchanged chunks upsert to
    the same values (a harmless no-op write), changed ones get their
    chunk_text/embedding/metadata/updated_at refreshed, and the
    UNIQUE (source_path, chunk_index) constraint means nothing ever
    duplicates.
    """
    # None becomes an empty dict here, once, rather than every caller
    # needing to remember to pass {} explicitly for the common "no extra
    # metadata" case.
    metadata = metadata if metadata is not None else {}
    # Stage 1: split the raw text into chunk dicts (chunk_text,
    # source_path, chunk_index, source_type) - pure Python, no database
    # involved yet.
    chunks = chunk_text(
        text,
        source_path=source_path,
        source_type=source_type,
        chunk_size_tokens=chunk_size_tokens,
        overlap_tokens=overlap_tokens,
    )

    # An empty/whitespace-only source produces zero chunks - nothing to
    # embed or write, so stop here rather than doing pointless work below.
    if not chunks:
        return 0

    # Stage 2: embed every chunk's text in one batched call, adding an
    # "embedding" key to each chunk dict - still no database involved.
    embedded_chunks = embed_chunks(chunks)

    # Stage 3: shape each chunk dict into exactly the columns chunks_t
    # expects, in the form psycopg/pgvector need to adapt correctly:
    #   - embedding: pgvector's register_vector() (wired up in
    #     _writer_engine above) handles a plain Python list[float]
    #     automatically, so no extra wrapping needed here.
    #   - metadata: wrapped in Jsonb(...) so psycopg serializes it as a
    #     jsonb value, matching the column's real type. The SAME metadata
    #     dict (the caller-supplied source-level snapshot) is attached to
    #     every chunk from this call - see decision #20's "Option A".
    rows = [
        {
            "source_type": chunk["source_type"],
            "source_path": chunk["source_path"],
            "chunk_index": chunk["chunk_index"],
            "chunk_text": chunk["chunk_text"],
            "embedding": chunk["embedding"],
            "metadata": Jsonb(metadata),
            "updated_at": sa.func.now(),
        }
        for chunk in embedded_chunks
    ]

    # Resolve the logical store name to its physical table BEFORE opening the
    # transaction. registry.get() raises UnknownStoreError for a name that is
    # not configured - a typo in a loader should stop the run immediately,
    # not half-way through a batch.
    table_name = registry.get(store).table

    # Stage 4: one transaction, one batched upsert for the whole source -
    # `.begin()` commits automatically if this block completes without an
    # exception, and rolls back the whole thing if anything fails midway
    # (never a half-written source).
    with _writer_engine(settings).begin() as conn:
        written = upsert_returning(
            conn,
            _chunks_table(table_name),
            rows,
            conflict_on=("source_path", "chunk_index"),
            update=_UPDATE_ON_CONFLICT,
            returning=("id",),
        )

    return len(written)
