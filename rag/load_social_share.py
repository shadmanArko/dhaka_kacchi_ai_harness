"""Load the real social_share corpus (social_post_metrics.caption) into
dhaka_kacchi_rag.

Reads the CSV export at rag/data/social_post_metrics.csv (produced by a
one-off export over the SSH tunnel to social_share - see
RAG_progress.md's "RESOLVED" entry for how that export was done) and
calls ingest.ingest_source() once per post that has real caption text.

This file never connects to social_share directly - by the time it runs,
the real database pull has already happened and been written to a local
CSV. Keeping the "pull from social_share" step and the "load into
dhaka_kacchi_rag" step separate means this loader can be re-run against
the same CSV as many times as needed (e.g. after a code change) without
needing the SSH tunnel open every time.
"""

from __future__ import annotations

# csv.DictReader turns each row of the exported file into a plain dict
# keyed by column name (platform, caption, posted_at, ...) - easier to
# work with than raw comma-split text, and handles the quoting/escaping
# rules real CSV files need (e.g. a caption containing a comma).
import csv
import sys
from pathlib import Path

# Needed for prune_orphaned()'s DELETE statement - the only place in this
# file that writes SQL directly rather than going through ingest_source().
import sqlalchemy as sa

from rag.config import ConfigError, load_rag_writer_settings
from rag.ingest import ingest_source

# Where the exported CSV lives - see rag/data/'s own .gitignore entry
# (rag/data/*.csv) for why this file is never committed: real caption
# text, not synthetic test data.
CSV_PATH = Path(__file__).resolve().parent / "data" / "social_post_metrics.csv"

# Chunk settings tuned specifically for THIS corpus's real token-count
# distribution under the bge-m3 tokenizer (RAG_progress.md decision #18):
# median 38 tokens, p75 103 - size 100 keeps most captions whole in a
# single chunk, only splitting the genuinely long, multi-beat posts.
CHUNK_SIZE_TOKENS = 100
OVERLAP_TOKENS = 20

# A value the source CSV uses to represent SQL NULL when exported as text
# - psql's \copy and plain CSV writers don't agree on one universal
# convention, so this is checked explicitly rather than assumed.
_NULL_MARKERS = {"", "[NULL]"}


def _has_real_caption(caption: str | None) -> bool:
    # A caption is only worth ingesting if it's non-empty text once
    # whitespace is stripped away - a caption that's just spaces/newlines
    # would produce zero chunks anyway (chunk_text() already guards this),
    # so filtering here avoids even trying.
    return bool(caption) and caption.strip() not in _NULL_MARKERS


def _build_metadata(row: dict[str, str]) -> dict:
    """Snapshot every non-caption column into a plain dict, per
    RAG_progress.md decision #20 ("Option A") - this becomes every chunk
    from this post's `metadata`, so an agent reading a retrieved chunk can
    see platform/timing/engagement without a second lookup.

    Light type coercion from the CSV's all-strings-by-default shape: the
    engagement counters become real integers (so an agent/consumer can
    compare/sum them numerically, not just display them), everything else
    stays as the text the source already gave us.
    """
    return {
        "post_id": row["id"],
        "platform": row["platform"],
        "content_type": row["content_type"],
        "posted_at": row["posted_at"],
        "permalink": row["permalink"],
        "impressions": int(row["impressions"]),
        "reach": int(row["reach"]),
        "likes": int(row["likes"]),
        "comments": int(row["comments"]),
        "shares": int(row["shares"]),
        "saves": int(row["saves"]),
        "clicks": int(row["clicks"]),
        "refreshed_at": row["refreshed_at"],
    }


def load_all() -> dict:
    """Ingest every post with a real caption from CSV_PATH. Returns a
    stats dict (posts_ingested, posts_skipped_empty, total_chunks_written)
    - returned as data, not just printed, so rag/reindex.py can call this
    and report the numbers back through the web UI, not only the CLI.

    Safe to re-run: ingest_source() is already upsert-based per post
    (RAG_progress.md decisions #14/#15), so re-running this loader against
    an unchanged CSV just re-writes the same rows with the same values -
    no duplication, no error.
    """
    settings = load_rag_writer_settings()

    with CSV_PATH.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    total_chunks_written = 0
    posts_ingested = 0
    posts_skipped_empty = 0

    for row in rows:
        if not _has_real_caption(row["caption"]):
            posts_skipped_empty += 1
            continue

        written = ingest_source(
            text=row["caption"],
            source_path=f"social_post_metrics:{row['id']}",
            source_type="csv",
            chunk_size_tokens=CHUNK_SIZE_TOKENS,
            overlap_tokens=OVERLAP_TOKENS,
            settings=settings,
            metadata=_build_metadata(row),
        )
        total_chunks_written += written
        posts_ingested += 1

    return {
        "posts_ingested": posts_ingested,
        "posts_skipped_empty": posts_skipped_empty,
        "total_chunks_written": total_chunks_written,
    }


def prune_orphaned() -> int:
    """Delete any chunk whose post no longer exists in CSV_PATH with a
    real caption - i.e. the post was deleted from social_share (and the
    CSV was re-exported without it), or its caption was cleared.

    Returns how many chunk rows were deleted. Connects as rag_writer
    (RAG_progress.md decision #25 - DELETE was added to this role
    specifically for this function).

    RAG_progress.md decision #25 explains why this reprocesses the local
    CSV rather than pulling a fresh one: a stale local CSV means this
    prune step simply won't know about deletions that happened on the
    live social_share database since the last manual export - that's a
    known, accepted limitation of "re-index reprocesses local state
    only," not a bug in this function.
    """
    settings = load_rag_writer_settings()

    with CSV_PATH.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Every source_path that SHOULD exist right now, based on the current
    # CSV - anything in `chunks` whose source_path isn't in this set is,
    # by definition, orphaned.
    expected_source_paths = {
        f"social_post_metrics:{row['id']}" for row in rows if _has_real_caption(row["caption"])
    }

    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    with engine.begin() as conn:
        # Scoped to source_path starting with "social_post_metrics:" so
        # this can never touch a future, differently-sourced chunk that
        # happens to also have source_type='csv' - only this loader's own
        # rows are ever candidates for deletion here.
        # CAST(:expected AS text[]) explicitly, same lesson learned the
        # hard way with the vector query in retrieval.py: without an
        # explicit cast, Postgres can't always infer what type a bound
        # Python list should become, and refuses the query outright
        # rather than guessing.
        result = conn.execute(
            sa.text(
                """
                DELETE FROM chunks
                WHERE source_path LIKE 'social_post_metrics:%'
                  AND source_path != ALL(CAST(:expected AS text[]))
                """
            ),
            {"expected": list(expected_source_paths)},
        )
        deleted = result.rowcount

    return deleted


def main() -> int:
    try:
        stats = load_all()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    print(f"posts with a real caption: {stats['posts_ingested']}")
    print(f"posts skipped (empty/NULL caption): {stats['posts_skipped_empty']}")
    print(f"total chunk rows written: {stats['total_chunks_written']}")

    deleted = prune_orphaned()
    print(f"orphaned chunks deleted: {deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
