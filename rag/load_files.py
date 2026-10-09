"""Load every FILE-BACKED store into its table.

One loader for every store whose content is a folder of documents. Which
folder belongs to which store is not decided here - it is declared per store
in rag/stores.toml (`source_dir`), so adding a store that holds files is a
config edit, not a new Python module. This module used to be
`load_knowledge_base.py`, hardcoded to `Knowledge_Base/documents/`; it is now
generic over the registry (see rag/RUNBOOK.md, "Adding a store").

The folder a file sits in IS its store, and therefore its visibility:

    rag/Knowledge_Base/
      public/     -> the store declared public   (a customer bot may read it)
      internal/   -> the store declared internal (only the business's own
                     agents may read it)
      facts.yaml  -> no store at all: live data, deliberately NOT embedded
      documents/  -> unregistered archive (was the store "knowledge_base")

Visible in a directory listing, with no code and no config involved - which is
the point: the boundary between public and internal content is something a
human can check with `ls`, not something buried in a TOML file.

Formats handled (same set as before, unchanged):

  .md            - text ingested PER SECTION (one ingest_source() call per
                   markdown heading), so each chunk's metadata can carry that
                   section's own heading and so a chunk never straddles two
                   unrelated parts of a document. The YAML frontmatter at the
                   top of a file is stripped, not embedded: it is metadata
                   about the document ("status: DRAFT", "collection: brand"),
                   not content a customer should ever be shown.
  .txt           - read directly as plain text, one call per file.
  .htm / .html   - BeautifulSoup strips markup down to visible text; one call
                   per file, with the original URL attached as metadata if
                   the folder carries a urls.txt manifest.
  .pdf           - pypdf extracts text PER PAGE; one call per page, so each
                   chunk's metadata can carry that page's own page_number.

Deliberately NOT handled:

  .docx / .xlsx  - allowed source_type values (decision #10) but no real file
                   to build or test against, so calling this loader on one
                   raises rather than silently mishandling it.
  .yaml/.yml/.json - structured data does not belong in a vector store. Its
                   values change (a price, a deadline, a fee) and an embedding
                   cannot be updated without re-embedding; a retrieved stale
                   price is worse than no answer at all, because it is quoted
                   with confidence. facts.yaml lives at the Knowledge_Base
                   root for exactly this reason - outside every store folder,
                   so no store can ever pick it up - and agents read it
                   through a live lookup instead. Dropping one inside a store
                   folder is a mistake, and this loader says so by name.

Safe to re-run: every write goes through ingest_source(), which upserts on
(source_path, chunk_index).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Needed for prune_orphaned()'s DELETE statement - the only place in this
# file that writes SQL directly rather than going through ingest_source()
# (same pattern and same reason as load_social_share.py's import of it).
import sqlalchemy as sa
from bs4 import BeautifulSoup
from pypdf import PdfReader

# The shared identifier-quoting helper: prune_orphaned() splices this store's
# table name into a DELETE, and a table name can never be a bind parameter.
from rag.bootstrap_db import quote_identifier

# load_store_registry is how this loader finds out which table to write to,
# which folder to read, and how big its chunks should be - all three now live
# in rag/stores.toml rather than as constants in this file. KNOWLEDGE_BASE_DIR
# is the one folder every store's source_dir is relative to, shared with
# webui.py's file-serving endpoint so a chunk's source_path means the same
# thing to both.
from rag.config import (
    KNOWLEDGE_BASE_DIR,
    ConfigError,
    Store,
    StoreRegistry,
    load_rag_writer_settings,
    load_store_registry,
)
from rag.ingest import ingest_source

# urls.txt is a manifest (original source URL per .htm file), never ingested
# as searchable content itself - see decision #26. Recognised by name in any
# store folder, not just the old documents one.
_SKIP_FILENAMES = {"urls.txt"}

# Formats with no real file to build/test against yet - calling the loader on
# one of these raises rather than guessing at an untested code path.
_NOT_YET_IMPLEMENTED_SUFFIXES = {".docx", ".xlsx"}

# Structured data, which must never be embedded - see this module's docstring
# for the full reasoning. Raising (rather than skipping quietly) is the point:
# someone who drops facts.yaml into public/ believes it will be searchable,
# and the useful outcome is to be told it will not be, immediately.
_STRUCTURED_SUFFIXES = {".yaml", ".yml", ".json"}

# The YAML frontmatter block at the top of a markdown file: "---", then
# anything, then a closing "---" line. Anchored at the very start (\A) because
# a "---" later in the text is a horizontal rule, not frontmatter.
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL)

# A markdown H2 heading, and separately the H1 document title. Splitting is
# done on H2 rather than on every heading level: in a document like the brand
# book, H2 is the level that separates genuinely different subjects ("What we
# serve" vs "How ordering works"), while lower levels are sub-points within
# one subject and belong in the same chunk.
_H2_RE = re.compile(r"^##[ \t]+(.*?)[ \t]*$", re.MULTILINE)
_H1_RE = re.compile(r"^#[ \t]+(.*?)[ \t]*$", re.MULTILINE)

# Anything that is not a lowercase letter or digit becomes a dash in a
# section's slug. Kept deliberately narrow (ASCII only) so the result is
# always safe to put in a URL and in a source_path, whatever language the
# heading itself was written in.
_SLUG_UNSAFE_RE = re.compile(r"[^a-z0-9]+")


def _source_dir(store: Store) -> Path:
    """The absolute folder this store's files live in.

    Fails loudly if the store has no `source_dir` at all, or the folder it
    names does not exist - both are configuration mistakes, and the alternative
    (silently ingesting nothing, or ingesting an empty folder and then letting
    the prune delete everything) would be far worse than a clear error.
    """
    # A store without a folder is a legitimate thing in general (one fed from
    # a database export, say) - but it cannot be fed by THIS loader, so say so
    # by name instead of failing later with a confusing AttributeError.
    if store.source_dir is None:
        raise ConfigError(
            f"store {store.name!r} has no source_dir in rag/stores.toml, so "
            "there is no folder for rag/load_files.py to read. Add one (e.g. "
            f'source_dir = "some-folder"), or give this store its own loader.'
        )

    # config.py has already validated the SHAPE of source_dir (relative, no
    # "..", no leading slash); resolving it against Knowledge_Base_DIR here is
    # what turns that relative name into the real folder on this machine.
    directory = (KNOWLEDGE_BASE_DIR / store.source_dir).resolve()

    # Belt and braces on top of the pattern check: after resolving, the folder
    # must still genuinely be inside Knowledge_Base/. This is the check that
    # would catch a symlink pointing out of the tree, which no string pattern
    # can see.
    if not directory.is_relative_to(KNOWLEDGE_BASE_DIR):
        raise ConfigError(
            f"store {store.name!r}: source_dir {store.source_dir!r} resolves "
            f"outside {KNOWLEDGE_BASE_DIR} - refusing to read it."
        )

    if not directory.is_dir():
        raise ConfigError(
            f"store {store.name!r}: source_dir {store.source_dir!r} does not "
            f"exist (expected the folder {directory})."
        )

    return directory


def _load_url_manifest(directory: Path) -> dict[str, str]:
    """Map a .htm filename (e.g. "doc_001.htm") to its real original URL,
    using urls.txt's positional correspondence: line N <-> doc_{N:03d}.htm
    in sorted filename order - confirmed by direct inspection (see
    RAG_progress.md decision #26): 41 lines, 41 .htm files, line 1's URL
    filename matches doc_001.htm's own embedded <FILENAME> tag.

    Absent manifest (the normal case for a folder of hand-written notes) just
    means no file gets a source_url - not an error.
    """
    manifest_path = directory / "urls.txt"
    if not manifest_path.exists():
        return {}

    with manifest_path.open(encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip()]

    htm_files = sorted(p.name for p in directory.glob("doc_*.htm"))
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


def _strip_frontmatter(text: str) -> tuple[str, str | None]:
    """Remove a leading YAML frontmatter block; return (body, title).

    The title is pulled out with one small regex rather than a YAML parser:
    pyyaml is not a dependency of this project, and the only value worth
    keeping is a plain `title: ...` line. A frontmatter block that does not
    contain one simply yields title=None - nothing here needs the rest of it,
    because every other field in there describes the DOCUMENT (its draft
    status, its intended collection), not its content.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        # No frontmatter at all - the whole file is body.
        return text, None

    frontmatter = match.group(1)
    body = text[match.end() :]

    # Grab "title: something" if present, ignoring any trailing "# comment"
    # the author left on the line.
    title = None
    for line in frontmatter.splitlines():
        if line.lower().startswith("title:"):
            title = line.split(":", 1)[1].split("#", 1)[0].strip()
            break

    return body, title


def _slug(text: str, *, fallback: str) -> str:
    """Turn a heading into a short, URL- and path-safe slug.

    Used inside a section's source_path ("public/brand-book.md::what-we-serve"),
    so the constraints are the same as any filename's: lowercase ASCII, digits
    and dashes only. A heading written entirely in a non-Latin script slugifies
    to nothing, which is what `fallback` is for (the caller passes a positional
    name like "section-3" instead).
    """
    slug = _SLUG_UNSAFE_RE.sub("-", text.lower()).strip("-")
    return slug or fallback


def _markdown_sections(body: str, *, title: str | None) -> list[tuple[str, str]]:
    """Split a markdown body into (heading, text) sections, one per H2.

    Returns the heading as a plain string (empty for the introductory part of
    a file that has no heading above its first section) and the section's full
    text INCLUDING its own "## ..." line - the heading words are useful to
    retrieval (a keyword search for "ordering" should be able to match the
    section called "How ordering works"), so they are kept in the chunk text
    rather than being stripped out into metadata alone.

    Markdown is split by heading rather than purely by token count because a
    heading is a boundary the author already decided on: a chunk that starts
    in "What we serve" and ends in "Legal rules" is worse than two smaller
    chunks, no matter how evenly sized.
    """
    # Every H2 boundary, in document order: (start_index, heading_text).
    boundaries = [(m.start(), m.group(1).strip()) for m in _H2_RE.finditer(body)]

    sections: list[tuple[str, str]] = []

    # Anything before the first H2 is the file's introduction. What usually
    # sits there in these documents is just the H1 title line, which carries
    # no content of its own and would embed into a near-meaningless chunk - so
    # it is only kept if there is real prose underneath it. The H1 line itself
    # is always dropped from the text; the section is labelled with the
    # frontmatter title when there is one.
    preamble = body[: boundaries[0][0]] if boundaries else body
    preamble_without_h1 = _H1_RE.sub("", preamble).strip()
    if len(preamble_without_h1) > 0:
        sections.append((title or "", preamble.strip()))

    # Then each H2 section, running from its own heading to the next one.
    for index, (start, heading) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(body)
        text = body[start:end].strip()
        if text:
            sections.append((heading, text))

    return sections


def _units_for_file(
    path: Path, *, rel_path: str, url_manifest: dict[str, str]
) -> list[tuple[str, str, str, dict]]:
    """Every unit of content this file contributes, as
    (source_path, text, source_type, metadata).

    ONE function answers "what would this file be ingested as?" for both the
    loader and the prune - which is the only way the two can be guaranteed to
    agree. They must: the prune deletes every row whose source_path the loader
    would NOT write, so a single character of disagreement between them (the
    backslash bug of 2026-10-02 was exactly this) deletes real data, or leaves
    orphans behind forever.

    The units differ per format for a reason:
      - .pdf   -> one unit per PAGE (a page number is real metadata, and a
                 50-page contract is not one idea)
      - .md    -> one unit per SECTION (a heading is a boundary the author
                  chose; it becomes both the chunk's metadata and part of its
                 source_path)
      - others -> one unit per FILE

    Raises for formats this loader refuses to guess at (.docx/.xlsx) and for
    structured data (.yaml/.yml/.json - see the module docstring).
    """
    suffix = path.suffix.lower()

    # Refuse rather than skip: someone who put one of these in a store folder
    # expects it to be searchable, and "it silently never was" is the worst
    # possible outcome.
    if suffix in _STRUCTURED_SUFFIXES:
        raise ConfigError(
            f"{rel_path}: {suffix} files are structured data and are NOT "
            "embedded by design - their values change (prices, deadlines, "
            "fees) and a retrieved stale value would be quoted as if it were "
            "current. Keep them OUTSIDE the store folders (facts.yaml at the "
            "Knowledge_Base root is the existing example) and read them "
            "through a live lookup instead."
        )

    if suffix in _NOT_YET_IMPLEMENTED_SUFFIXES:
        raise NotImplementedError(
            f"{rel_path}: {suffix} support is not implemented yet - "
            "see RAG_progress.md decision #26."
        )

    if suffix == ".pdf":
        units = []
        for page_num, page_text in enumerate(_extract_pdf_pages(path), start=1):
            # A page with no extractable text (a full-page figure, say)
            # contributes nothing - skipping it here keeps the prune's
            # keep-list and the loader's writes in step, since neither
            # produces a row for it.
            if not page_text.strip():
                continue
            units.append(
                (
                    # "::pageN" rather than a real path segment because this
                    # is not a file: it is page N OF this file. Everything
                    # downstream that needs the underlying file splits on "::"
                    # (webui.py's _build_reference does exactly that).
                    f"{rel_path}::page{page_num}",
                    page_text,
                    "pdf",
                    {"page_number": page_num},
                )
            )
        return units

    if suffix in (".htm", ".html"):
        text = _extract_html_text(path)
        if not text.strip():
            return []
        metadata = {}
        if path.name in url_manifest:
            metadata["source_url"] = url_manifest[path.name]
        return [(rel_path, text, "html", metadata)]

    if suffix == ".md":
        raw = path.read_text(encoding="utf-8", errors="replace")
        body, title = _strip_frontmatter(raw)
        units = []
        # Section slugs are made unique WITHIN the file by numbering repeats:
        # two sections may legitimately be called "Notes", and identical
        # source_paths would make their chunks collide on
        # UNIQUE (source_path, chunk_index) - the second section's chunks
        # would silently overwrite the first's.
        used_slugs: dict[str, int] = {}
        for index, (heading, section_text) in enumerate(
            _markdown_sections(body, title=title), start=1
        ):
            base = _slug(heading, fallback=f"section-{index}")
            used_slugs[base] = used_slugs.get(base, 0) + 1
            slug = base if used_slugs[base] == 1 else f"{base}-{used_slugs[base]}"
            units.append(
                (
                    f"{rel_path}::{slug}",
                    section_text,
                    "markdown",
                    {"heading": heading} if heading else {},
                )
            )
        return units

    if suffix == ".txt":
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return []
        return [(rel_path, text, "txt", {})]

    # An extension this loader has no parser for at all - contribute nothing.
    # The caller counts it as "skipped" rather than pretending it was read.
    return []


def _ingestible_files(directory: Path) -> list[Path]:
    """Every file in `directory` the loader would even look at, in a stable
    order.

    Shared by the loader and the prune so that "which files count" is also
    answered in exactly one place. Sorted for the same reason: two runs over
    the same folder must produce the same sequence, so a section that gets a
    numbered slug gets the SAME number on a re-run (otherwise every re-index
    would rename chunks and the prune would delete-then-recreate them).
    """
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.name not in _SKIP_FILENAMES
    )


def _rel_path(path: Path) -> str:
    """A file's path relative to Knowledge_Base/, with FORWARD slashes.

    .as_posix(), never str(): on Windows str() returns backslashes, and a
    backslash in a source_path was a real bug (2026-08-10... 2026-10-02,
    decision #28) - it is silently eaten when the path is embedded in the
    frontend's JavaScript string literals. It matters even more here than
    elsewhere: these strings are compared character-for-character against what
    is stored in the database by the prune.
    """
    return path.relative_to(KNOWLEDGE_BASE_DIR).as_posix()


def load_store(store: Store, *, registry: StoreRegistry | None = None) -> dict:
    """Ingest every file in `store`'s folder into `store`'s table.

    Returns a stats dict (files_ingested, files_skipped, total_chunks_written).

    Idempotent: ingest_source() upserts on (source_path, chunk_index), so
    re-running this over unchanged files rewrites identical rows and changes
    nothing else.
    """
    # The writer credential - never the admin one (decisions #7/#8).
    settings = load_rag_writer_settings()

    # The registry is what turns the store's logical name into a physical
    # table inside ingest_source(). Loaded here when the caller (the CLI) did
    # not already have one; the orchestrator passes its own so every store in
    # one re-index run is resolved against the same, single config load.
    if registry is None:
        registry = load_store_registry()

    directory = _source_dir(store)
    url_manifest = _load_url_manifest(directory)

    files_ingested = 0
    files_skipped = 0
    total_chunks_written = 0

    for path in _ingestible_files(directory):
        units = _units_for_file(path, rel_path=_rel_path(path), url_manifest=url_manifest)

        # Nothing came out of this file (an empty .txt, a figure-only PDF, an
        # extension with no parser) - count it as skipped so the run's numbers
        # add up to the folder's contents.
        if not units:
            files_skipped += 1
            continue

        for source_path, text, source_type, metadata in units:
            total_chunks_written += ingest_source(
                store=store.name,
                registry=registry,
                text=text,
                source_path=source_path,
                source_type=source_type,
                # Chunk settings come from the registry entry, not from
                # constants in this file - so retuning a store's chunks is a
                # config edit, and the loader and the retriever can never
                # disagree about which settings belong to which store.
                chunk_size_tokens=store.chunk_size_tokens,
                overlap_tokens=store.overlap_tokens,
                settings=settings,
                metadata=metadata,
            )

        files_ingested += 1

    return {
        "files_ingested": files_ingested,
        "files_skipped": files_skipped,
        "total_chunks_written": total_chunks_written,
    }


def _expected_source_paths(store: Store) -> set[str]:
    """Build the "keep-list" that prune_orphaned() compares against: every
    source_path the loader WOULD write if it ran right now, given the folder
    as it currently sits on disk (RAG_progress.md decision #29).

    Built by running the very same `_units_for_file` the loader uses, so the
    two cannot disagree about what a file becomes - see that function's own
    docstring for why that guarantee is the whole point.
    """
    directory = _source_dir(store)
    url_manifest = _load_url_manifest(directory)

    expected: set[str] = set()
    for path in _ingestible_files(directory):
        for source_path, _text, _source_type, _metadata in _units_for_file(
            path, rel_path=_rel_path(path), url_manifest=url_manifest
        ):
            expected.add(source_path)

    return expected


def prune_orphaned(store: Store) -> int:
    """Delete this store's chunks whose source no longer exists on disk - a
    file that was deleted or renamed, a PDF page that no longer exists because
    the file was replaced with a shorter version, or a markdown section whose
    heading was renamed (which changes its slug, and therefore its
    source_path). Returns how many rows were deleted.

    Load-before-prune is the caller's job (see reindex.py): once the load has
    just rewritten every row that legitimately belongs to a file on disk,
    anything left over is a genuine orphan.

    Connects as rag_writer - DELETE was granted to that role specifically for
    this function and its social_share twin (decision #25).

    Known, accepted limitation: this catches sources that DISAPPEARED, not
    sources that are still present but got SHORTER without their heading
    changing - a .txt or .htm edited down keeps its expected source_path, so
    its now-stale tail chunks survive. Closing that gap would need the loader
    to report per-source chunk counts to the prune (decision #29 explains why
    it was deferred). Markdown is the exception: because a section's
    source_path contains its heading, most real edits to these documents do
    produce a new source_path and are cleaned up.
    """
    settings = load_rag_writer_settings()

    # The physical table is resolved through the registry, and the delete is
    # scoped to that whole table - which is now what keeps this prune from
    # touching another store's rows. The pre-multi-store version had to filter
    # on `source_path LIKE 'documents/%'` to get that isolation, because every
    # corpus shared one table. With one table per store, this statement simply
    # cannot see anything else.
    registry = load_store_registry()
    table = registry.get(store.name).table

    expected_source_paths = _expected_source_paths(store)

    engine = sa.create_engine(settings.sqlalchemy_url, poolclass=sa.pool.NullPool)

    # A keep-list that is EMPTY would make the DELETE below match every single
    # row - "this path is not on the list" is true of everything when the list
    # is empty. That is the right answer when the folder really is empty and
    # nothing was ever ingested, and a catastrophic one when the folder merely
    # LOOKS empty to this run (a drive not mounted, a file renamed to a
    # refused extension, a mistyped source_dir). So it is checked rather than
    # trusted: an empty folder over a non-empty table stops the run and asks
    # for a deliberate decision instead of silently emptying a store.
    if not expected_source_paths:
        with engine.connect() as conn:
            existing = conn.execute(
                sa.text(f"SELECT count(*) FROM {quote_identifier(table)}")
            ).scalar_one()
        if existing:
            raise ConfigError(
                f"refusing to prune store {store.name!r}: its folder "
                f"(Knowledge_Base/{store.source_dir}/) currently yields NO "
                f"ingestible files, but {table!r} holds {existing} rows - "
                "pruning would delete every one of them. If the folder is "
                "genuinely meant to be empty, empty the table deliberately: "
                f"DELETE FROM {table};"
            )

    with engine.begin() as conn:
        # CAST(:expected AS text[]) explicitly - copied from the social_share
        # prune (same lesson as retrieval.py's vector cast): without the
        # explicit cast, Postgres can't infer what type a bound Python list
        # should become for this comparison and refuses the query outright
        # rather than guessing.
        #
        # The table name, by contrast, CANNOT be a parameter - so it is
        # quoted instead, with the same helper the setup scripts use.
        result = conn.execute(
            sa.text(
                f"""
                DELETE FROM {quote_identifier(table)}
                WHERE source_path != ALL(CAST(:expected AS text[]))
                """
            ),
            {"expected": list(expected_source_paths)},
        )
        deleted = result.rowcount

    return deleted


def main() -> int:
    """Terminal entry point: `uv run python -m rag.load_files <store>`.

    Unlike the old documents-only loader, this one cannot guess which store to
    load - that is the whole point of it being generic - so the logical store
    name is a required argument. Re-indexing every store at once is
    rag.reindex's job (`uv run python -m rag.reindex`).
    """
    if len(sys.argv) != 2:
        print(
            "usage: uv run python -m rag.load_files <store>\n"
            "  e.g. uv run python -m rag.load_files brand_book\n"
            "  (all stores at once: uv run python -m rag.reindex)",
            file=sys.stderr,
        )
        return 2

    requested = sys.argv[1]

    try:
        registry = load_store_registry()
        store = registry.get(requested)
        stats = load_store(store, registry=registry)
    except (ConfigError, LookupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except NotImplementedError as exc:
        # A file in a store folder whose format this loader has no parser for
        # (.docx/.xlsx). Caught here so an operator sees one sentence naming
        # the file and the reason, rather than a traceback ending in "not
        # implemented yet" - the message inside the exception already says
        # everything, it just needs to be read without the stack above it.
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"{store.name}: loaded from Knowledge_Base/{store.source_dir}/")
    print(f"  files ingested: {stats['files_ingested']}")
    print(f"  files skipped: {stats['files_skipped']}")
    print(f"  total chunk rows written: {stats['total_chunks_written']}")

    # Same finishing step as every other loader: load, then prune, so
    # re-running can never leave rows behind for files that have since been
    # removed from the folder.
    deleted = prune_orphaned(store)
    print(f"  orphaned chunks deleted: {deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
