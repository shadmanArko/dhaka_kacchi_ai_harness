"""A minimal web UI for searching the RAG knowledge base directly - pure
retrieval, no LLM call involved anywhere in this file (per Ahmad's
explicit requirement: "communicate with my RAG... get meaningful answers
without any LLM interaction").

Same shape as this repo's own CEO cockpit (ARCHITECTURE.md SS4: "a single
FastAPI endpoint + minimal mobile HTML page") - one FastAPI app, one
inline HTML page, no separate frontend build step or framework.

Run it with: uv run uvicorn rag.webui:app --reload --port 8010
Then open http://127.0.0.1:8010 in a browser.
"""

from __future__ import annotations

# Path gives us a safe, structured way to check "is this file really
# inside the folder I expect it to be in" - critical for the file-serving
# endpoint below, which must never let a request escape outside
# Knowledge_Base/ onto the rest of this machine's filesystem.
from pathlib import Path

# threading lets the re-index (below) run in a background thread, so the
# HTTP request that starts it can return immediately instead of staying
# open for the ~40 minutes the job takes.
import threading

# datetime is used only to stamp the re-index job's start/finish times as
# ISO strings, so the browser can render them in the viewer's local time.
from datetime import datetime, timezone

# FastAPI is the actual web framework - handles turning an HTTP request
# into a call to one of our Python functions below, and turning our
# return value back into an HTTP response.
from fastapi import FastAPI, HTTPException, Query

# FileResponse streams a file straight from disk as the HTTP response
# body (used for serving a Knowledge_Base document); HTMLResponse tells
# FastAPI "the string I'm returning is a raw HTML page, send it with the
# right Content-Type header" - without it, FastAPI would try to treat a
# returned string as plain text or JSON instead.
from fastapi.responses import FileResponse, HTMLResponse

# UnknownStoreError is what retrieve() raises for BOTH an unrecognised store
# name and one this credential may not read - the endpoint below turns it
# into one 404 either way, preserving that deliberate sameness.
from rag.config import (
    # The one folder documents may be served from - imported from config.py
    # rather than recomputed here, so this endpoint and the loader can never
    # disagree about where a source_path is relative to (they did have two
    # copies of this line before 2026-10-09; one is enough).
    KNOWLEDGE_BASE_DIR,
    UnknownStoreError,
    load_rag_internal_reader_settings,
    load_store_registry,
)
# The re-index orchestration that the /api/reindex endpoints below kick
# off in a background thread (RAG_progress.md decisions #30/#31).
from rag.reindex import run_reindex
from rag.retrieval import (
    can_read_store,
    get_chunk_neighbors,
    list_stores,
    retrieve,
    store_for_source_path,
)

# The exact dropdown choices Ahmad asked for, with 5 as the default -
# written as one constant so the frontend dropdown and the backend's own
# validation can never drift apart from each other.
ALLOWED_TOP_K_VALUES = (5, 10, 15, 20)
DEFAULT_TOP_K = 5

# KNOWLEDGE_BASE_DIR (where documents live - the absolute root every
# request's path-safety check below compares against) is imported from
# rag/config.py, alongside the other things this server is configured with.
# It is resolved to an absolute path there, once, so it cannot be
# reinterpreted depending on the server process's current working directory.

# File extensions a browser can render natively, inline, without any
# conversion - used to tell the frontend whether to show an inline
# preview (PDF in an <iframe>, text in a plain <pre>) or just a download
# link (e.g. .docx/.xlsx, which no browser renders on its own).
_INLINE_PREVIEWABLE_SUFFIXES = {".pdf", ".txt", ".md", ".csv"}

# FastAPI's main application object - every route (@app.get(...)) below
# attaches itself to this one object, and this is what uvicorn actually
# runs.
app = FastAPI(title="Dhaka Kacchi RAG Search")

# The reader settings (connects as rag_reader, read-only - see
# RAG_progress.md decisions #4/#7) are loaded ONCE here at import time,
# not on every single search request - reading environment variables and
# validating a connection string on every request would be wasted,
# repeated work for a value that never changes while the server runs.
# Connects as the INTERNAL reader: this page is a local, single-user operator
# tool, so it is the trusted audience that may see every store - including the
# ones marked internal. A public-facing surface would load
# load_rag_public_reader_settings() instead, and would then be unable to read
# internal stores no matter what the UI tried to show.
_reader_settings = load_rag_internal_reader_settings()

# The store registry, loaded once at startup: it maps the logical store names
# the UI and API speak in to the physical tables, and gives every store's
# description for the picker below.
_registry = load_store_registry()


def _build_reference(row: dict) -> dict:
    """Turn one retrieved chunk's raw metadata into a clean, display-ready
    reference the frontend can show under each result - "where did this
    text come from, and how can a human go look at the original."

    Two real shapes, told apart by whether "permalink" is present in the
    chunk's metadata (only social_share posts have one, per decision #20):
      - a database row (social_share post): the frontend shows the FULL
        snapshotted row (every column, not just platform/permalink/
        posted_at) - Ahmad's explicit ask, and all of it is already
        sitting in `metadata` from ingestion, nothing new to compute here.
      - a file (Knowledge_Base document, once that loader exists): no
        permalink to link to, so instead we point at this server's own
        /api/file endpoint, plus whatever heading/page metadata the
        (future) file loader attaches.
    """
    metadata = row.get("metadata") or {}

    if "permalink" in metadata:
        return {
            "kind": "database_row",
            "label": f"{metadata.get('platform', 'unknown platform')} post",
            "url": metadata["permalink"],
            # The WHOLE snapshotted row, every column - not a trimmed
            # subset - so the frontend can render a complete details
            # table, per Ahmad's explicit request.
            "full_row": metadata,
        }

    # Anything else is a file-type source. source_path for these is a
    # relative path under Knowledge_Base/ (set by load_files.py)
    # - /api/file?path=... serves it back, with the same safety check
    # applied to both places so they can never drift apart.
    #
    # A source_path can carry a "::" suffix naming WHICH PART of the file
    # this chunk came from - "somefile.pdf::page2" for a PDF page,
    # "brand-book.md::what-we-serve" for a markdown section (load_files.py
    # ingests one unit per page and per section). Path(...).suffix naively
    # applied to that whole string returns ".pdf::page2", not ".pdf", because
    # Path only looks at the LAST dot in the string and there isn't one after
    # "pdf". Splitting on "::" first recovers the real underlying file path
    # before ever asking for its suffix or serving it back.
    base_path = row["source_path"].split("::")[0]
    suffix = Path(base_path).suffix.lower()
    page_number = metadata.get("page_number")

    file_url = f"/api/file?path={base_path}"
    # Chrome/Firefox/Edge's built-in PDF viewer honours a "#page=N"
    # fragment to jump straight to that page when the PDF loads in the
    # iframe - a nice, free usability win given we already know which
    # page this chunk came from.
    if suffix == ".pdf" and page_number:
        file_url += f"#page={page_number}"

    return {
        "kind": "file",
        # The FILE's path, not the whole source_path: the "::page2" /
        # "::what-we-serve" suffix names a part of the file, and it is already
        # shown as a badge of its own (page number / heading) right next to
        # this label.
        "label": base_path,
        "url": file_url,
        "previewable_inline": suffix in _INLINE_PREVIEWABLE_SUFFIXES,
        "heading": metadata.get("heading"),
        "page_number": page_number,
    }


@app.get("/api/stores")
def stores() -> dict:
    """The stores THIS server's credential can actually read.

    This is what fills the store picker in the page. It is deliberately
    derived from the database (retrieval.list_stores asks Postgres which
    tables the connected role may SELECT) rather than from the registry's
    declared visibility - so the menu can never advertise a store this
    process would then be refused. See MULTI_STORE_DESIGN.md section 8.
    """
    return {"stores": list_stores(registry=_registry, settings=_reader_settings)}


@app.get("/api/search")
def search(
    # Query(...) with no default means "q" is REQUIRED - a search request
    # with no query text at all isn't a valid search, so FastAPI rejects
    # it automatically with a clear error rather than this code having to
    # check "is q empty" by hand.
    q: str = Query(..., description="The search query text"),
    # Query(DEFAULT_TOP_K) means "top_k defaults to 5 if the caller
    # doesn't specify one" - ge/le (greater-or-equal / less-or-equal)
    # bounds reject anything outside the dropdown's actual range before
    # this code ever runs, so an invalid value never reaches retrieve().
    top_k: int = Query(DEFAULT_TOP_K, ge=min(ALLOWED_TOP_K_VALUES), le=max(ALLOWED_TOP_K_VALUES)),
    # Which store to search. Optional so a bare /api/search?q=... still works
    # (it falls back to the first readable store below), but the page always
    # sends the picker's current value.
    store: str | None = Query(None, description="Logical store name to search"),
) -> dict:
    """The actual search endpoint - embeds `q` and returns the `top_k`
    most similar chunks FROM ONE NAMED STORE, each with a clean reference
    attached. Pure retrieval: the text returned here is exactly what's
    stored in the store's chunk_text, never anything generated by an LLM.

    Naming a store is routing, not permission: whether it can actually be
    read was decided when this server's credential was loaded, and Postgres
    enforces it. An unreadable or unknown store produces the same 404 -
    deliberately indistinguishable (MULTI_STORE_DESIGN.md section 7.2).
    """
    # No store named? Fall back to the first one this credential can read,
    # so the endpoint stays usable from a plain URL. An empty list can only
    # happen if the role has been granted nothing at all, which is a
    # misconfiguration worth naming clearly rather than searching nothing.
    if store is None:
        available = list_stores(registry=_registry, settings=_reader_settings)
        if not available:
            raise HTTPException(
                status_code=503,
                detail="no readable stores are configured for this credential",
            )
        store = available[0]["store"]

    try:
        results = retrieve(
            q, store=store, top_k=top_k, registry=_registry, settings=_reader_settings
        )
    except UnknownStoreError as exc:
        # 404 for both "never heard of it" and "not allowed to read it" -
        # the same uniform response the exception type exists to guarantee.
        raise HTTPException(status_code=404, detail=str(exc)) from None

    enriched = []
    for row in results:
        # "A few words before/after" the matched chunk, pulled from the
        # already-stored neighboring chunk rows (same source_path, one
        # chunk_index lower/higher, and the SAME store) - no extra
        # ingestion-time storage needed, see get_chunk_neighbors' docstring.
        neighbors = get_chunk_neighbors(
            store=store,
            source_path=row["source_path"],
            chunk_index=row["chunk_index"],
            registry=_registry,
            settings=_reader_settings,
        )
        enriched.append(
            {
                "chunk_text": row["chunk_text"],
                "distance": row["distance"],
                "context_before": neighbors["before"],
                "context_after": neighbors["after"],
                "reference": _build_reference(row),
            }
        )

    # `store` is echoed back so the page can label which corpus these
    # results came from.
    return {"query": q, "store": store, "top_k": top_k, "results": enriched}


@app.get("/api/file")
def get_file(path: str = Query(..., description="A file's path, relative to Knowledge_Base/")):
    """Serve one document from Knowledge_Base/ so the frontend's side
    panel can display it (inline for PDF/text, a download for anything
    a browser can't render natively, e.g. .docx/.xlsx).

    Two independent guards, because this endpoint is the ONE place in the
    whole subsystem where the public/internal boundary is not enforced by a
    Postgres grant:

    1. PATH SAFETY. This is the only place in rag/ that turns a caller-supplied
       string into a filesystem path, so it is the only place a path-traversal
       attempt (path="../../../../Windows/System32/some_file") could read
       something it shouldn't. Guarded by resolving the full path and checking
       it is still inside KNOWLEDGE_BASE_DIR before touching the filesystem.

    2. STORE VISIBILITY. Knowledge_Base/ now holds BOTH tiers side by side -
       public/brand-book.md and internal/voice-and-rules.md - so "inside the
       folder" is no longer the same question as "may this caller see it". The
       path must belong to a store (by folder prefix) AND this server's
       credential must be able to read that store, asked of Postgres
       (has_table_privilege) exactly as a search would. A public-facing
       deployment loads the public credential and therefore cannot fetch
       internal/ documents, whatever path it asks for - the same structural
       isolation the tables get, extended to the files the tables point at.
       Today this server runs with the INTERNAL credential (see
       _reader_settings above), so both tiers are servable here; the guard is
       what keeps that from becoming a hole the moment a public surface
       reuses this endpoint.
    """
    # Combine the requested relative path onto the known-safe root, then
    # resolve() collapses any ".."/"." segments into a final absolute
    # path - this is the step that would reveal an attempted escape.
    requested = (KNOWLEDGE_BASE_DIR / path).resolve()

    # is_relative_to() checks whether `requested` is actually inside
    # KNOWLEDGE_BASE_DIR once all the ".." tricks have been resolved away -
    # if it's not, refuse outright rather than ever calling open() on it.
    if not requested.is_relative_to(KNOWLEDGE_BASE_DIR):
        raise HTTPException(status_code=400, detail="invalid path")

    # Which store claims this path (by source_dir prefix), and may this
    # server's credential read it? Both answers are needed before a single
    # byte is read. A path that no store claims - facts.yaml, an archived
    # folder, anything unregistered - is refused by the same 404 as a
    # nonexistent file, so the endpoint cannot be used to discover what else
    # is lying around in the folder.
    store = store_for_source_path(path, registry=_registry)
    if store is None or not can_read_store(
        store=store.name, registry=_registry, settings=_reader_settings
    ):
        raise HTTPException(status_code=404, detail="file not found")

    if not requested.is_file():
        raise HTTPException(status_code=404, detail="file not found")

    return FileResponse(requested)


# ---------------------------------------------------------------------------
# Re-indexing: a long-running job (tens of minutes) the page can start and poll
# ---------------------------------------------------------------------------

# The current re-index job's state, kept at MODULE level (not inside a
# request-handling function) because the job deliberately outlives the
# HTTP request that starts it: POST /api/reindex returns immediately, and
# the page then polls GET /api/reindex/status until this says it's done.
# Fields:
#   running     - True while the background thread is working
#   started_at  - ISO timestamp of when the current/last run began
#   finished_at - ISO timestamp of when it ended (None while running)
#   stats       - run_reindex()'s full result dict, once it succeeds
#   error       - "Type: message" text if it raised instead
_reindex_state: dict = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "stats": None,
    "error": None,
}

# Guards the check-then-start in start_reindex() below: two nearly
# simultaneous POSTs (a double-click, two tabs) must not both see
# "running: False" and both start a run - this lock makes checking the
# flag and setting it one indivisible step.
_reindex_lock = threading.Lock()


def _reindex_worker() -> None:
    """The background thread's whole body: run the re-index, then record
    how it ended in _reindex_state.

    This is the ONE place in this file that deliberately catches every
    exception: there is no HTTP response for an error to travel out
    through (the request that started this thread finished long ago), so
    success and failure both just get parked in the state dict for the
    polling endpoint to report.
    """
    try:
        stats = run_reindex()
        # Success: keep the numbers for the page to render.
        _reindex_state["stats"] = stats
        _reindex_state["error"] = None
    except Exception as exc:
        # run_reindex() catches nothing on purpose (decision #30), so
        # whatever it raised lands here. str(exc) can be empty for some
        # exception classes, so the type name is kept too.
        _reindex_state["stats"] = None
        _reindex_state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        # Success or failure, the job is no longer running - the next
        # poll will show the outcome and re-enable the button. Set last,
        # so "not running" in a poll response always means the outcome
        # fields above are already filled in.
        _reindex_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        _reindex_state["running"] = False


@app.post("/api/reindex")
def start_reindex() -> dict:
    """Start a re-index run in a background thread and return the current
    state immediately - this request must NOT stay open for the ~40
    minutes the job takes (that's what the polling status endpoint is
    for). If a run is already in progress this is a no-op that simply
    reports the running state, so a double-click or a second tab can
    never start two jobs at once.
    """
    with _reindex_lock:
        if not _reindex_state["running"]:
            # Clear the previous run's outcome up front, so polling can
            # never show stale numbers as if they belonged to the run now
            # starting.
            _reindex_state.update(
                running=True,
                started_at=datetime.now(timezone.utc).isoformat(),
                finished_at=None,
                stats=None,
                error=None,
            )
            # daemon=True: the worker must never be the one thing keeping
            # the server process alive.
            threading.Thread(
                target=_reindex_worker, name="reindex", daemon=True
            ).start()
    # dict(...) snapshots the state, so the response can't race with the
    # worker thread writing into the live dict while it's serialized.
    return dict(_reindex_state)


@app.get("/api/reindex/status")
def reindex_status() -> dict:
    """Report the re-index state for the page's polling loop - whether a
    run is in progress, when it started/finished, and either the stats
    dict (success) or an error string (failure). Never blocks: it just
    reads whatever the background thread has written so far.
    """
    return dict(_reindex_state)


# The page itself - one plain HTML string with inline CSS/JS, no build
# step, no separate static file server. Kept deliberately simple,
# matching the cockpit's own "minimal mobile HTML page" philosophy.
_PAGE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dhaka Kacchi RAG Search</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 0; padding: 0; }
    .page { display: flex; height: 100vh; }
    .results-pane { flex: 1; min-width: 0; padding: 1.5rem; overflow-y: auto; box-sizing: border-box; }
    /* The side panel starts at width 0 and is invisible until a document
       is actually opened - toggled by adding the "open" class in JS below,
       rather than always reserving half the screen for an empty panel. */
    .doc-pane { width: 0; border-left: 1px solid #ddd; overflow: hidden; transition: width 0.15s; }
    .doc-pane.open { width: 45vw; }
    .doc-pane iframe { width: 100%; height: 100%; border: none; }
    .doc-pane-header { display: flex; justify-content: space-between; align-items: center; padding: 0.5rem 1rem; border-bottom: 1px solid #ddd; }
    h1 { font-size: 1.3rem; margin-top: 0; }
    .search-row { display: flex; gap: 0.5rem; margin-bottom: 1.5rem; }
    #q { flex: 1; padding: 0.5rem; font-size: 1rem; }
    #top_k { padding: 0.5rem; font-size: 1rem; }
    button { padding: 0.5rem 1rem; font-size: 1rem; cursor: pointer; }
    .result { border-bottom: 1px solid #ddd; padding: 1rem 0; }
    .heading-badge { display: inline-block; font-size: 0.75rem; background: #eef; color: #335; padding: 0.1rem 0.5rem; border-radius: 3px; margin-bottom: 0.4rem; }
    .chunk-text { white-space: pre-wrap; margin-bottom: 0.4rem; }
    .chunk-text .context { color: #999; }
    .chunk-text .matched { font-weight: 600; background: #fff6d6; }
    /* Results are COMPACT by default (Ahmad's request, decision #33):
       the matched chunk text alone, clamped to a few lines - "show more"
       swaps in the full view (context, reference, score). */
    .chunk-text.clamped { display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden; }
    .result-toggle { color: #0a58ca; background: none; border: none; padding: 0; margin-top: 0.35rem; font-size: 0.85rem; cursor: pointer; }
    .reference { font-size: 0.85rem; color: #555; }
    .reference a, .reference button.linklike { color: #0a58ca; background: none; border: none; padding: 0; font-size: inherit; cursor: pointer; }
    .distance { font-size: 0.8rem; color: #888; }
    #status { color: #888; font-size: 0.9rem; }
    /* The re-index row: a small, secondary action under the search bar,
       with its own one-line status text beside it. */
    .reindex-row { display: flex; gap: 0.75rem; align-items: center; margin-bottom: 1.5rem; }
    #reindex-btn { padding: 0.35rem 0.75rem; font-size: 0.9rem; }
    #reindex-btn:disabled { cursor: default; opacity: 0.6; }
    #reindex-status { color: #555; font-size: 0.85rem; }
    details.full-row { margin-top: 0.3rem; font-size: 0.85rem; }
    details.full-row table { border-collapse: collapse; margin-top: 0.3rem; }
    details.full-row td { border: 1px solid #ddd; padding: 0.2rem 0.5rem; }
    details.full-row td.key { color: #555; white-space: nowrap; }
  </style>
</head>
<body>
  <div class="page">
    <div class="results-pane">
      <h1>Dhaka Kacchi RAG Search</h1>
      <div class="search-row">
        <!-- Which store to search. Filled from GET /api/stores, which only
             ever lists stores THIS server's credential can actually read. -->
        <select id="store" title="Which store to search"></select>
        <input id="q" type="text" placeholder="Ask a question...">
        <select id="top_k">
          <option value="5" selected>5 results</option>
          <option value="10">10 results</option>
          <option value="15">15 results</option>
          <option value="20">20 results</option>
        </select>
        <button onclick="runSearch()">Search</button>
      </div>
      <div class="reindex-row">
        <button id="reindex-btn" onclick="startReindex()" title="Re-embed every store from its folder on disk (Knowledge_Base/public + Knowledge_Base/internal)">re-index</button>
        <span id="reindex-status"></span>
      </div>
      <div id="status"></div>
      <div id="results"></div>
    </div>
    <div class="doc-pane" id="doc-pane">
      <div class="doc-pane-header">
        <span id="doc-pane-title"></span>
        <button onclick="closeDoc()">close</button>
      </div>
      <iframe id="doc-pane-frame"></iframe>
    </div>
  </div>

  <script>
    // Pressing Enter in the search box triggers a search, same as
    // clicking the button - a small usability detail, not required, but
    // expected of any search box.
    // Fill the store picker once, at load, from the server. A failure here
    // is shown rather than swallowed: with no store selected a search cannot
    // run, so silently leaving the list empty would produce a confusing
    // "unknown store" on the first search instead of naming the real cause.
    async function loadStores() {
      const sel = document.getElementById('store');
      try {
        const resp = await fetch('/api/stores');
        const data = await resp.json();
        sel.innerHTML = '';
        for (const s of data.stores) {
          const opt = document.createElement('option');
          opt.value = s.store;
          // The description is prompt text meant for an agent; here it
          // serves as the tooltip a human reads when choosing.
          opt.title = s.description;
          opt.textContent = s.store + (s.visibility === 'internal' ? ' (internal)' : '');
          sel.appendChild(opt);
        }
        if (!data.stores.length) {
          document.getElementById('status').textContent =
            'No readable stores for this credential - check stores.toml and the grants.';
        }
      } catch (err) {
        document.getElementById('status').textContent = 'Could not load stores: ' + err;
      }
    }
    loadStores();

    document.getElementById('q').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') runSearch();
    });

    function escapeHtml(text) {
      // Escaping < before dropping arbitrary chunk text into innerHTML -
      // without this, a chunk that happened to contain something like
      // "<script>" would be interpreted as real HTML instead of just
      // being displayed as text.
      return text.replace(/</g, '&lt;');
    }

    function openDoc(url, label) {
      // Loading the document into the side panel's iframe and sliding
      // the panel open - used for anything the browser can render
      // natively inline (PDF, plain text/markdown/csv).
      document.getElementById('doc-pane-frame').src = url;
      document.getElementById('doc-pane-title').textContent = label;
      document.getElementById('doc-pane').classList.add('open');
    }

    function closeDoc() {
      document.getElementById('doc-pane').classList.remove('open');
      document.getElementById('doc-pane-frame').src = '';
    }

    function toggleResult(btn) {
      // One result expands/collapses on its own: hide whichever view is
      // currently visible and show the other. The default set up in
      // runSearch below is compact - just the matched chunk text.
      const result = btn.closest('.result');
      const compact = result.querySelector('.compact-view');
      const full = result.querySelector('.full-view');
      const expanding = !compact.hidden;
      compact.hidden = expanding;
      full.hidden = !expanding;
    }

    function buildReferenceHtml(ref) {
      if (ref.kind === 'database_row') {
        // Every column from the source row, rendered as a plain
        // key/value table inside a native <details> element - collapsed
        // by default so the result list stays scannable, but the full
        // row is one click away, per Ahmad's explicit request.
        const rowsHtml = Object.entries(ref.full_row)
          .map(([k, v]) => `<tr><td class="key">${escapeHtml(String(k))}</td><td>${escapeHtml(String(v))}</td></tr>`)
          .join('');
        return `
          <a href="${ref.url}" target="_blank">${escapeHtml(ref.label)}</a>
          <details class="full-row">
            <summary>show all columns for this row</summary>
            <table>${rowsHtml}</table>
          </details>
        `;
      }

      // kind === 'file': either an inline "open in side panel" action
      // (PDF/text-like formats the browser can render on its own) or a
      // plain download link (e.g. .docx/.xlsx, which no browser renders
      // natively without converting it first - out of scope for now).
      const headingHtml = ref.heading
        ? `<span class="heading-badge">${escapeHtml(ref.heading)}</span>`
        : (ref.page_number ? `<span class="heading-badge">page ${ref.page_number}</span>` : '');
      const openHtml = ref.previewable_inline
        ? `<button class="linklike" onclick="openDoc('${ref.url}', '${escapeHtml(ref.label)}')">open document &rarr;</button>`
        : `<a href="${ref.url}" download>download document</a>`;
      return `${headingHtml}<br>${escapeHtml(ref.label)} - ${openHtml}`;
    }

    // ---- re-index button: start it, then poll until it finishes ----

    // One pending timer (if any) for the polling loop below - a fresh
    // poll always cancels any older scheduled one first, so two polling
    // loops can never run at the same time.
    let reindexPollTimer = null;

    function fmtTime(iso) {
      // ISO (UTC) timestamp -> short local time for display.
      return iso ? new Date(iso).toLocaleTimeString() : '';
    }

    function renderReindexState(s) {
      // Draw the current re-index state into the button + status line.
      // textContent (not innerHTML) everywhere here - error text comes
      // from the server and should render as plain text, never as HTML.
      const btn = document.getElementById('reindex-btn');
      const el = document.getElementById('reindex-status');
      if (s.running) {
        // Disabled while running - the backend would ignore a second
        // click anyway (see start_reindex), but the UI should say so too.
        btn.disabled = true;
        el.textContent = `re-indexing... started ${fmtTime(s.started_at)} - this takes 30-45 minutes`;
      } else {
        btn.disabled = false;
        if (s.error) {
          el.textContent = `re-index FAILED (${fmtTime(s.finished_at)}): ${s.error}`;
        } else if (s.stats) {
          // The stats shape is now per-STORE and registry-driven, so it is
          // rendered by walking whatever stores the run actually reported
          // rather than by naming two hardcoded corpora. That means adding
          // a store needs no change here.
          const parts = s.stats.stores.map(function (st) {
            if (st.status === 'skipped_no_loader') {
              return `${st.store} skipped (no loader registered)`;
            }
            return `${st.store}: ${st.chunks_written} chunks written, ` +
              `${st.orphaned_chunks_deleted} orphans deleted`;
          });
          el.textContent = `re-index finished ${fmtTime(s.finished_at)} - ` + parts.join('; ');
        } else {
          el.textContent = '';
        }
      }
    }

    async function pollReindex() {
      // Ask the server what the re-index is doing right now and redraw;
      // while it's still running, schedule the next check in 5 seconds.
      // That is the whole polling loop.
      clearTimeout(reindexPollTimer);
      const resp = await fetch('/api/reindex/status');
      const s = await resp.json();
      renderReindexState(s);
      if (s.running) {
        reindexPollTimer = setTimeout(pollReindex, 5000);
      }
    }

    async function startReindex() {
      // Confirm first: this re-embeds EVERYTHING and takes tens of
      // minutes - far too heavy for an accidental click to trigger.
      if (!confirm('Re-index every store from the local files? Re-embeds everything, takes 30-45 minutes.')) {
        return;
      }
      // POST starts the background run server-side; the response is the
      // state right after starting, and the polling loop takes over.
      const resp = await fetch('/api/reindex', { method: 'POST' });
      const s = await resp.json();
      renderReindexState(s);
      if (s.running) {
        pollReindex();
      }
    }

    async function runSearch() {
      const q = document.getElementById('q').value.trim();
      const topK = document.getElementById('top_k').value;
      const statusEl = document.getElementById('status');
      const resultsEl = document.getElementById('results');

      if (!q) { return; }

      statusEl.textContent = 'Searching...';
      resultsEl.innerHTML = '';
      closeDoc();

      // encodeURIComponent escapes the query text so special characters
      // (spaces, &, Bengali/non-Latin script, etc.) survive being put
      // into a URL correctly, rather than breaking the request.
      // The store is sent explicitly rather than left to the server's
      // default, so the result label and the picker can never disagree.
      const store = document.getElementById('store').value;
      if (!store) {
        statusEl.textContent = 'No store selected.';
        return;
      }
      const response = await fetch(
        `/api/search?q=${encodeURIComponent(q)}&top_k=${topK}&store=${encodeURIComponent(store)}`
      );
      const data = await response.json();

      statusEl.textContent = `${data.results.length} result(s) for "${data.query}" in "${data.store}"`;

      for (const r of data.results) {
        const div = document.createElement('div');
        div.className = 'result';

        // Stitch "a few words before" + the actual matched chunk +
        // "a few words after" into one paragraph, with the matched part
        // visually highlighted and the surrounding context dimmed - this
        // is the "show some words before/after" requirement, built from
        // the neighboring chunk rows the backend already looked up.
        const before = r.context_before ? `<span class="context">[...] ${escapeHtml(r.context_before)}</span> ` : '';
        const after = r.context_after ? ` <span class="context">${escapeHtml(r.context_after)} [...]</span>` : '';
        const matched = `<span class="matched">${escapeHtml(r.chunk_text)}</span>`;

        // Compact by default (Ahmad's request, decision #33): just the
        // matched chunk text, clamped to a few lines. "show more" swaps
        // in the full view - context before/after, the reference (+ full
        // row / open-document controls), and the similarity score.
        div.innerHTML = `
          <div class="compact-view">
            <div class="chunk-text clamped">${escapeHtml(r.chunk_text)}</div>
            <button class="result-toggle" onclick="toggleResult(this)">show more</button>
          </div>
          <div class="full-view" hidden>
            <div class="chunk-text">${before}${matched}${after}</div>
            <div class="reference">${buildReferenceHtml(r.reference)}</div>
            <div class="distance">similarity score (higher = more similar): ${(-r.distance).toFixed(4)}</div>
            <button class="result-toggle" onclick="toggleResult(this)">show less</button>
          </div>
        `;
        resultsEl.appendChild(div);
      }
    }

    // On page load, ask about the current re-index state once up front -
    // so refreshing mid-run (or coming back after one finished) shows
    // what happened instead of nothing.
    pollReindex();
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Serve the one-page search UI. Returning the raw HTML string
    directly (no templating engine) is deliberate - this page has no
    server-rendered data in it at all, everything is filled in client-side
    by JavaScript calling /api/search, so a templating engine would add
    complexity for zero benefit here.
    """
    return _PAGE_HTML
