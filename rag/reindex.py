"""Re-index orchestration: refresh the vector stores from LOCAL state.

One call that walks the store registry (rag/stores.toml) and, for every store
that has a loader, runs load + prune (RAG_progress.md decisions #25 and #30).

"Re-index" here deliberately means: reprocess what is already on disk
(`rag/Knowledge_Base/public/`, `rag/Knowledge_Base/internal/`, and - if it is
ever re-registered - `rag/data/social_post_metrics.csv`) as they currently
sit, never reach out to any live database. Pulling a fresh export, or dropping
new files into a store folder, stays a separate, manual step (decision #25).

This is what webui.py's re-index button calls. It is also runnable directly
from the terminal via `uv run python -m rag.reindex` (optionally naming a
single store to re-index just that one).

Registry-driven rather than hardcoded (multi-store design, section 10): the
list of stores comes from configuration, not from a fixed pair of calls here.
A store that has no loader registered is REPORTED as skipped rather than
silently ignored - a corpus that quietly never gets re-indexed is a far worse
failure than a loud gap in the report.

Everything it calls is idempotent (upsert-based loads, keep-list prunes), so
re-running after an interruption - or just re-running - always converges on
the same correct state.
"""

from __future__ import annotations

import sys

# Both loader modules imported whole (not their individual functions) so the
# call sites below read "load_files.load_store(...)" - and so the orchestration
# here can be exercised with stubs, without activating the real (slow) loaders.
#
# load_social_share is imported even though its store is currently
# unregistered: the map below is the single place a store is wired to its
# loader, and keeping the module importable is what makes re-registering it a
# one-line change (see rag/RUNBOOK.md, "Removing a store").
from rag import load_files, load_social_share

# UnknownStoreError is raised when a caller names a store that is not in the
# registry - for this operator-facing tool that is a straight error, not
# something to bend into an "unknown store" style message.
from rag.config import ConfigError, UnknownStoreError, load_store_registry

# Which module knows how to load each store, keyed by LOGICAL store name.
#
# This is the one place that maps a registry entry to the code that fills it.
# Adding a store whose content is a FOLDER means touching only stores.toml
# (load_files reads the folder named by the store's own source_dir); adding a
# store whose source is a NEW kind of thing means adding its loader module
# here as well, plus one line below.
#
# Deliberately in code rather than in the config: a config field naming an
# importable module is a much bigger thing to get wrong (it is a name that
# decides which code runs) than a dictionary here is to read.
_LOADER_MODULES = {
    # Both brand stores are folders of markdown, so both use the generic file
    # loader - they differ only in which folder they read, which is config.
    "brand_book": load_files,
    "voice_and_rules": load_files,
    # Unregistered as of 2026-10-09 (its data is queried directly instead).
    # Left wired up on purpose: re-adding the [stores.social_share] block to
    # stores.toml restores it with no code change at all.
    "social_share": load_social_share,
}


def run_reindex(*, store: str | None = None) -> dict:
    """Refresh stores from local state: load + prune each.

    With no arguments, every store in the registry that has a loader is
    processed, in the order stores.toml lists them. Passing `store` names ONE
    logical store and processes only that one - genuinely useful, because a
    full run re-embeds every chunk of every store, which is wasteful when only
    one store's source data has actually changed. (With the two brand stores
    that is a few seconds; when the test corpora were registered it was closer
    to 45 minutes for the document store alone, which is where the option came
    from.)

    Load-before-prune per store, exactly the order each loader's own main()
    uses. That order matters for the prune's safety: the load has just
    (re)written every row that legitimately belongs to a source on disk, so
    anything the prune then deletes is a genuine leftover.

    Returns a stats dict for the caller (the web UI, or the terminal via
    main() below) to report:

        {
            "stores": [
                {"store": str,
                 "status": "reindexed" | "skipped_no_loader",
                 "chunks_written": int,
                 "orphaned_chunks_deleted": int},
                ...
            ],
            "total_chunks_written": int,
            "total_orphaned_chunks_deleted": int,
        }

    Deliberately catches NOTHING from the loaders (decision #30): if a load or
    prune raises - e.g. load_files' NotImplementedError for a .docx dropped
    into the folder, or its refusal to prune a store whose folder has gone
    empty - the error goes straight to the caller to surface. A partially
    finished re-index is always safe to simply re-run, so failing loudly beats
    inventing partial-failure bookkeeping at this stage.
    """
    # The registry is the source of truth for WHICH stores exist and in what
    # order they should be processed.
    registry = load_store_registry()

    # Either one named store or every store, keeping registry order in both
    # cases. registry.get() raises UnknownStoreError for a name that is not
    # configured, which is the right behaviour for an operator tool.
    targets = [registry.get(store)] if store is not None else list(registry)

    results: list[dict] = []

    for target in targets:
        # Which module fills this store, if any.
        loader = _LOADER_MODULES.get(target.name)

        if loader is None:
            # A store can legitimately exist in the registry with no loader -
            # e.g. one populated by hand, or a store whose loader has not been
            # written yet. Record it explicitly so the gap is visible in the
            # report instead of being mistaken for "nothing to do".
            results.append(
                {
                    "store": target.name,
                    "status": "skipped_no_loader",
                    "chunks_written": 0,
                    "orphaned_chunks_deleted": 0,
                }
            )
            continue

        # Load first, then prune - see the docstring above for why this order
        # is what makes the prune safe. Both loaders implement the same two
        # functions taking the Store being processed (load_store /
        # prune_orphaned), which is what lets this loop stay ignorant of
        # whether it is driving a folder of markdown or a CSV export. They
        # return stats dicts with their own format-specific keys
        # (posts_ingested for the CSV corpus, files_ingested for a folder),
        # but they share `total_chunks_written`, which is the one number this
        # orchestrator needs.
        load_stats = loader.load_store(target, registry=registry)
        deleted = loader.prune_orphaned(target)

        results.append(
            {
                "store": target.name,
                "status": "reindexed",
                "chunks_written": load_stats["total_chunks_written"],
                "orphaned_chunks_deleted": deleted,
            }
        )

    return {
        "stores": results,
        # Totals across everything processed, for a one-line summary.
        "total_chunks_written": sum(r["chunks_written"] for r in results),
        "total_orphaned_chunks_deleted": sum(
            r["orphaned_chunks_deleted"] for r in results
        ),
    }


def main() -> int:
    """Terminal entry point (`uv run python -m rag.reindex [store]`): runs the
    same re-index and prints the numbers. Shaped like the two loaders' own
    main()s, including the ConfigError handling.

    The optional argument names a single store, e.g.:
        uv run python -m rag.reindex social_share
    """
    # Anything after the module name is treated as a store to limit the run
    # to. Kept as a bare positional argument rather than pulling in argparse
    # for one optional value.
    requested_store = sys.argv[1] if len(sys.argv) > 1 else None

    try:
        stats = run_reindex(store=requested_store)
    except (ConfigError, UnknownStoreError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # One block per store, then the totals.
    for entry in stats["stores"]:
        if entry["status"] == "skipped_no_loader":
            print(f"{entry['store']}: skipped (no loader registered for this store)")
            continue
        print(f"{entry['store']}:")
        print(f"  chunk rows written: {entry['chunks_written']}")
        print(f"  orphaned chunks deleted: {entry['orphaned_chunks_deleted']}")

    print(f"total chunk rows written: {stats['total_chunks_written']}")
    print(f"total orphaned chunks deleted: {stats['total_orphaned_chunks_deleted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
