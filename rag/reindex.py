"""Re-index orchestration: one call that refreshes the whole vector store
from LOCAL state - both corpora, load + prune each (RAG_progress.md
decisions #25 and #30).

"Re-index" here deliberately means: reprocess what is already on disk
(`rag/data/social_post_metrics.csv` and `rag/Knowledge_Base/` as they
currently sit) - never reach out to the live `social_share` database or
the SSH tunnel. Pulling a fresh CSV export, or dropping new files into
Knowledge_Base/, stays a separate, manual step (decision #25).

This is what webui.py's re-index button will call. It is also runnable
directly from the terminal via `uv run python -m rag.reindex`.

Everything it calls is idempotent (upsert-based loads, keep-list prunes),
so re-running after an interruption - or just re-running - always
converges on the same correct state.
"""

from __future__ import annotations

import sys

# Both corpus modules imported whole (not their individual functions) so
# the call sites below read "load_social_share.load_all()" - and so the
# orchestration here can be exercised with stubs, without activating the
# real (slow) loaders.
from rag import load_knowledge_base, load_social_share
from rag.config import ConfigError


def run_reindex() -> dict:
    """Refresh both corpora from local state: load + prune for
    social_share, then load + prune for Knowledge_Base.

    Load-before-prune per corpus - exactly the order each loader's own
    main() uses. That order matters for the prune's safety: the load has
    just (re)written every row that legitimately belongs to a source on
    disk, so anything the prune then deletes is a genuine leftover.

    Returns one combined stats dict for the caller (the web UI, or the
    terminal via main() below) to report:
        {
            "social_share":   {posts_ingested, posts_skipped_empty,
                               chunks_written, orphaned_chunks_deleted},
            "knowledge_base": {files_ingested, files_skipped,
                               chunks_written, orphaned_chunks_deleted},
            "total_chunks_written": int,
            "total_orphaned_chunks_deleted": int,
        }

    Deliberately catches NOTHING (decision #30): if any of the four
    operations raises - e.g. load_knowledge_base's NotImplementedError
    for a .docx dropped into the folder - the error goes straight to the
    caller to surface. A partially finished re-index is always safe to
    simply re-run, so failing loudly beats inventing partial-failure
    bookkeeping at this stage.
    """
    # social_share first: it's the small, fast corpus (~428 posts, a few
    # minutes), so a broken setup shows up in seconds instead of after
    # Knowledge_Base has been grinding for many minutes.
    social_stats = load_social_share.load_all()
    social_deleted = load_social_share.prune_orphaned()

    # Knowledge_Base second: the big, slow one (2,721 chunks, ~40 minutes
    # on this machine at the last measured full run).
    kb_stats = load_knowledge_base.load_all()
    kb_deleted = load_knowledge_base.prune_orphaned()

    # One flat dict of numbers, grouped per corpus plus overall totals -
    # shaped as data (not printed text) so the web UI can render it as it
    # likes and the terminal main() below can print it.
    return {
        "social_share": {
            "posts_ingested": social_stats["posts_ingested"],
            "posts_skipped_empty": social_stats["posts_skipped_empty"],
            "chunks_written": social_stats["total_chunks_written"],
            "orphaned_chunks_deleted": social_deleted,
        },
        "knowledge_base": {
            "files_ingested": kb_stats["files_ingested"],
            "files_skipped": kb_stats["files_skipped"],
            "chunks_written": kb_stats["total_chunks_written"],
            "orphaned_chunks_deleted": kb_deleted,
        },
        "total_chunks_written": (
            social_stats["total_chunks_written"] + kb_stats["total_chunks_written"]
        ),
        "total_orphaned_chunks_deleted": social_deleted + kb_deleted,
    }


def main() -> int:
    """Terminal entry point (`uv run python -m rag.reindex`): runs the
    same re-index and prints the numbers. Shaped like the two loaders'
    own main()s, including the ConfigError handling.
    """
    try:
        stats = run_reindex()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    social = stats["social_share"]
    kb = stats["knowledge_base"]

    print("social_share:")
    print(f"  posts ingested: {social['posts_ingested']}")
    print(f"  posts skipped (empty/NULL caption): {social['posts_skipped_empty']}")
    print(f"  chunk rows written: {social['chunks_written']}")
    print(f"  orphaned chunks deleted: {social['orphaned_chunks_deleted']}")
    print("Knowledge_Base:")
    print(f"  files ingested: {kb['files_ingested']}")
    print(f"  files skipped: {kb['files_skipped']}")
    print(f"  chunk rows written: {kb['chunks_written']}")
    print(f"  orphaned chunks deleted: {kb['orphaned_chunks_deleted']}")
    print(f"total chunk rows written: {stats['total_chunks_written']}")
    print(f"total orphaned chunks deleted: {stats['total_orphaned_chunks_deleted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
