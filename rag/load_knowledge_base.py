"""Load documents from rag/Knowledge_Base/documents/ into dhaka_kacchi_rag.

Three source formats handled for real (RAG_progress.md decision #26):
  .pdf          - pypdf extracts text PER PAGE; one ingest_source() call
                  per page (not per file), so each chunk's metadata can
                  carry that page's own page_number.
  .htm / .html  - BeautifulSoup strips markup down to visible text; one
                  ingest_source() call per file. If urls.txt lists this
                  file's original URL, it's attached as metadata.source_url.
  .txt / .md    - read directly as plain text, one call per file.

.docx/.xlsx are allowed source_type values (decision #10) but have no
real files to build/test against yet - calling this loader on one raises
NotImplementedError rather than silently mishandling it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from bs4 import BeautifulSoup
from pypdf import PdfReader

# Needed for prune_orphaned()'s DELETE statement - the only place in this
# file that writes SQL directly rather than going through ingest_source()
# (same pattern and same reason as load_social_share.py's import of it).
import sqlalchemy as sa

from rag.config import ConfigError, load_rag_writer_settings
from rag.ingest import ingest_source

# Resolved once, absolute - the same root both this loader and webui.py's
# file-serving endpoint treat as authoritative, so a chunk's source_path
# (always relative to this directory) means the same thing everywhere.
KNOWLEDGE_BASE_DIR = (Path(__file__).resolve().parent / "Knowledge_Base").resolve()
DOCUMENTS_DIR = KNOWLEDGE_BASE_DIR / "documents"

# Chunk settings for this corpus specifically - measured real bge-m3
# token counts across all 174 real documents/pages (RAG_progress.md
# decision #27): min 31, median 1079, p25 764, p75 1434, max 38545 (one
# very long .htm contract) - academic papers and legal exhibits run far
# denser/longer than social media captions (decision #18's 100/20), so
# these are deliberately bigger. Given the median is ~4x this chunk size,
# a typical page/document still splits into several focused chunks,
# which is the right granularity for dense technical/legal text.
CHUNK_SIZE_TOKENS = 250
OVERLAP_TOKENS = 50

# urls.txt is a manifest (original source URL per .htm file), never
# ingested as searchable content itself - see decision #26.
_SKIP_FILENAMES = {"urls.txt"}

# Formats with no real file to build/test against yet in this corpus -
# calling the loader on one of these raises rather than guessing at an
# untested code path.
_NOT_YET_IMPLEMENTED_SUFFIXES = {".docx", ".xlsx"}


def _load_url_manifest() -> dict[str, str]:
    """Map a .htm filename (e.g. "doc_001.htm") to its real original URL,
    using urls.txt's positional correspondence: line N <-> doc_{N:03d}.htm
    in sorted filename order - confirmed by direct inspection (see
    decision #26): 41 lines, 41 .htm files, line 1's URL filename matches
    doc_001.htm's own embedded <FILENAME> tag.
    """
    manifest_path = DOCUMENTS_DIR / "urls.txt"
    if not manifest_path.exists():
        return {}

    with manifest_path.open(encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]

    htm_files = sorted(p.name for p in DOCUMENTS_DIR.glob("doc_*.htm"))
    # zip() stops at the shorter of the two lists, so a mismatched count
    # (if the folder's contents ever drift from the manifest) degrades to
    # "map as many as line up" rather than crashing - a few .htm files
    # just wouldn't get a source_url, which is a cosmetic loss, not an
    # ingestion failure.
    return dict(zip(htm_files, urls, strict=False))


def _extract_pdf_pages(path: Path) -> list[str]:
    """Return one string per page, in page order. A page's text can be
    empty (e.g. a figure-only page with no extractable text) - callers
    skip empty pages rather than embedding nothing.
    """
    reader = PdfReader(str(path))
    return [page.extract_text() or "" for page in reader.pages]


def _extract_html_text(path: Path) -> str:
    """Strip HTML markup down to plain visible text. These are real SEC
    filing exhibits (deeply nested inline-styled <p>/<font> tags) -
    BeautifulSoup's get_text() walks the whole tree and concatenates text
    nodes, which handles arbitrarily messy/nested markup far more
    robustly than a hand-written regex would.
    """
    with path.open(encoding="utf-8", errors="replace") as f:
        # html.parser is the standard library's own parser - no extra
        # dependency beyond beautifulsoup4 itself (an alternative,
        # lxml, is faster but is a second library to install for a
        # prototype-scale corpus where parsing speed isn't the bottleneck).
        soup = BeautifulSoup(f.read(), "html.parser")
    # separator="\n" keeps paragraph-like breaks between what were
    # separate tags, rather than running all visible text together into
    # one unbroken wall of text.
    return soup.get_text(separator="\n")


def load_all() -> dict:
    """Ingest every supported file in Knowledge_Base/documents/. Returns
    a stats dict (files_ingested, files_skipped, total_chunks_written).

    Safe to re-run: ingest_source() is upsert-based per source_path
    (decisions #14/#15), so an unchanged file just re-writes identical
    rows.
    """
    settings = load_rag_writer_settings()
    url_manifest = _load_url_manifest()

    files_ingested = 0
    files_skipped = 0
    total_chunks_written = 0

    for path in sorted(DOCUMENTS_DIR.iterdir()):
        if not path.is_file() or path.name in _SKIP_FILENAMES:
            files_skipped += 1
            continue

        suffix = path.suffix.lower()
        # .as_posix() forces forward slashes regardless of OS - NOT
        # str(), which gives backslashes on Windows. Found the hard way:
        # a backslash embedded in the frontend's JS template literal
        # (onclick="openDoc('${ref.url}', ...)") gets silently dropped by
        # the browser's JS engine (an unrecognized escape sequence like
        # "\a" just becomes "a"), corrupting the path before it ever
        # reaches /api/file. Forward slashes also happen to be the
        # correct separator for anything URL-shaped anyway.
        rel_path = path.relative_to(KNOWLEDGE_BASE_DIR).as_posix()

        if suffix in _NOT_YET_IMPLEMENTED_SUFFIXES:
            raise NotImplementedError(
                f"{rel_path}: {suffix} support is not implemented yet - "
                "see RAG_progress.md decision #26."
            )

        if suffix == ".pdf":
            for page_num, page_text in enumerate(_extract_pdf_pages(path), start=1):
                if not page_text.strip():
                    continue
                total_chunks_written += ingest_source(
                    text=page_text,
                    source_path=f"{rel_path}::page{page_num}",
                    source_type="pdf",
                    chunk_size_tokens=CHUNK_SIZE_TOKENS,
                    overlap_tokens=OVERLAP_TOKENS,
                    settings=settings,
                    metadata={"page_number": page_num},
                )
            files_ingested += 1

        elif suffix in (".htm", ".html"):
            text = _extract_html_text(path)
            if not text.strip():
                files_skipped += 1
                continue
            metadata = {}
            if path.name in url_manifest:
                metadata["source_url"] = url_manifest[path.name]
            total_chunks_written += ingest_source(
                text=text,
                source_path=rel_path,
                source_type="html",
                chunk_size_tokens=CHUNK_SIZE_TOKENS,
                overlap_tokens=OVERLAP_TOKENS,
                settings=settings,
                metadata=metadata,
            )
            files_ingested += 1

        elif suffix in (".txt", ".md"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if not text.strip():
                files_skipped += 1
                continue
            total_chunks_written += ingest_source(
                text=text,
                source_path=rel_path,
                source_type="markdown" if suffix == ".md" else "txt",
                chunk_size_tokens=CHUNK_SIZE_TOKENS,
                overlap_tokens=OVERLAP_TOKENS,
                settings=settings,
                metadata={},
            )
            files_ingested += 1

        else:
            # An extension this loader has no parser for at all (not even
            # a "not implemented yet" placeholder) - skip rather than
            # guess at how to extract text from an unknown format.
            files_skipped += 1

    return {
        "files_ingested": files_ingested,
        "files_skipped": files_skipped,
        "total_chunks_written": total_chunks_written,
    }


def _expected_source_paths() -> set[str]:
    """Build the "keep-list" that prune_orphaned() compares against: the
    set of every source_path the loader WOULD write if it ran right now,
    based on the folder as it currently sits on disk (RAG_progress.md
    decision #29 - the same "keep-list" idea as
    load_social_share.prune_orphaned()).

    Two shapes, because they become source_paths differently (decision
    #29):
      - .htm / .html / .txt / .md -> ONE path per file (the file's own
        relative path, e.g. "documents/doc_001.htm").
      - .pdf -> ONE path per PAGE ("documents/<name>.pdf::page3"), because
        load_all() ingests one page at a time. WHICH page numbers exist
        depends on the file's current page count.
    """
    expected: set[str] = set()

    # Walk the folder exactly the way load_all() does (same sorted order,
    # same skip rules) so the two functions can never disagree about what
    # counts as an ingestible file.
    for path in sorted(DOCUMENTS_DIR.iterdir()):
        # Same skips as load_all(): non-files and the urls.txt manifest
        # are never ingested, so they contribute no source_paths.
        if not path.is_file() or path.name in _SKIP_FILENAMES:
            continue

        suffix = path.suffix.lower()

        # Same .as_posix() rule as load_all() - forward slashes, never
        # backslashes (decision #28's bug). This matters MORE here than
        # anywhere else in the subsystem: these strings get compared
        # character-for-character against what's stored in the database,
        # so a backslash version would match zero rows - making the prune
        # treat every real chunk as orphaned and delete the whole corpus.
        rel_path = path.relative_to(KNOWLEDGE_BASE_DIR).as_posix()

        if suffix in _NOT_YET_IMPLEMENTED_SUFFIXES:
            # Mirrors load_all(): a .docx/.xlsx in the folder fails loudly
            # rather than being silently mishandled - no support for those
            # formats exists yet (decision #26).
            raise NotImplementedError(
                f"{rel_path}: {suffix} support is not implemented yet - "
                "see RAG_progress.md decision #26."
            )

        if suffix == ".pdf":
            # Count the pages the file has RIGHT NOW - structure only, no
            # text extraction, so this stays cheap (unlike the loader's
            # real text-extraction step). Page numbers run 1..N to match
            # load_all()'s enumerate(..., start=1) exactly. Pages that
            # happen to be empty have no chunks of their own, but listing
            # them anyway is harmless: the keep-list only ever answers
            # "is this row's source_path on it?".
            num_pages = len(PdfReader(str(path)).pages)
            expected.update(
                f"{rel_path}::page{n}" for n in range(1, num_pages + 1)
            )

        elif suffix in (".htm", ".html", ".txt", ".md"):
            # One source_path per file - exactly the string load_all()
            # writes for these formats.
            expected.add(rel_path)

        # Any other extension: load_all() skips it (no parser), writes no
        # source_paths - nothing to add here either.

    return expected


def prune_orphaned() -> int:
    """Delete any chunk whose source no longer exists in
    Knowledge_Base/documents/ - a file that was deleted or renamed, or a
    PDF page that no longer exists because the file was replaced with a
    shorter version (RAG_progress.md decision #29).

    Same shape as load_social_share.prune_orphaned(): build the keep-list
    of source_paths that SHOULD exist right now, then delete this loader's
    own rows that aren't on it. Returns how many chunk rows were deleted.
    Connects as rag_writer (RAG_progress.md decision #25 - DELETE was
    added to this role specifically for this function and its
    social_share twin).

    Known, accepted limitation (same class of gap as the social_share
    prune's): this catches sources that DISAPPEARED (whole file, or a
    page), not sources that are still present but got SHORTER - an .htm
    edited down keeps its expected source_path, so its now-stale tail
    chunks survive. Decision #29 explains why closing that gap was
    deferred.
    """
    settings = load_rag_writer_settings()

    # Every source_path that should exist right now, based on the folder
    # on disk - anything in `chunks` under documents/ that isn't in this
    # set is, by definition, orphaned.
    expected_source_paths = _expected_source_paths()

    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)
    with engine.begin() as conn:
        # Scoped to source_path starting with "documents/" so this can
        # never touch another corpus's rows (e.g. the social_share
        # "social_post_metrics:%" chunks) - only this loader's own rows
        # are ever candidates for deletion here.
        # CAST(:expected AS text[]) explicitly - copied from the
        # social_share prune (same lesson as retrieval.py's vector cast):
        # without the explicit cast, Postgres can't infer what type a
        # bound Python list should become for this comparison and refuses
        # the query outright rather than guessing.
        result = conn.execute(
            sa.text(
                """
                DELETE FROM chunks
                WHERE source_path LIKE 'documents/%'
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

    print(f"files ingested: {stats['files_ingested']}")
    print(f"files skipped: {stats['files_skipped']}")
    print(f"total chunk rows written: {stats['total_chunks_written']}")

    # Same finishing step as load_social_share.py's main(): load, then
    # prune, so re-running the loader can never leave rows behind for
    # files that have since been removed from the folder.
    deleted = prune_orphaned()
    print(f"orphaned chunks deleted: {deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
