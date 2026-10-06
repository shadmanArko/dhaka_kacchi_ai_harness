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

from rag.config import load_rag_reader_settings
# The re-index orchestration that the /api/reindex endpoints below kick
# off in a background thread (RAG_progress.md decisions #30/#31).
from rag.reindex import run_reindex
from rag.retrieval import get_chunk_neighbors, retrieve

# The exact dropdown choices Ahmad asked for, with 5 as the default -
# written as one constant so the frontend dropdown and the backend's own
# validation can never drift apart from each other.
ALLOWED_TOP_K_VALUES = (5, 10, 15, 20)
DEFAULT_TOP_K = 5

# Where Knowledge_Base documents live - resolve() turns this into an
# absolute path once, up front, so every request's path-safety check
# below compares against a known-good absolute root rather than a
# relative one that could be interpreted differently depending on the
# server process's current working directory.
KNOWLEDGE_BASE_DIR = (Path(__file__).resolve().parent / "Knowledge_Base").resolve()

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
_reader_settings = load_rag_reader_settings()


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
    # relative path under Knowledge_Base/ (set by load_knowledge_base.py)
    # - /api/file?path=... serves it back, with the same safety check
    # applied to both places so they can never drift apart.
    #
    # PDF pages encode their page number INTO source_path itself
    # ("somefile.pdf::page2" - see load_knowledge_base.py's per-page
    # ingest_source() calls) - Path(...).suffix naively applied to that
    # whole string returns ".pdf::page2", not ".pdf", because Path only
    # looks at the LAST dot in the string and there isn't one after
    # "pdf". Splitting on "::page" first recovers the real underlying
    # file path before ever asking for its suffix or serving it back.
    base_path = row["source_path"].split("::page")[0]
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
        "label": row["source_path"],
        "url": file_url,
        "previewable_inline": suffix in _INLINE_PREVIEWABLE_SUFFIXES,
        "heading": metadata.get("heading"),
        "page_number": page_number,
    }


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
) -> dict:
    """The actual search endpoint - embeds `q` and returns the `top_k`
    most similar chunks, each with a clean reference attached. Pure
    retrieval: the text returned here is exactly what's stored in
    `chunks.chunk_text`, never anything generated by an LLM.
    """
    results = retrieve(q, top_k=top_k, settings=_reader_settings)

    enriched = []
    for row in results:
        # "A few words before/after" the matched chunk, pulled from the
        # already-stored neighboring chunk rows (same source_path, one
        # chunk_index lower/higher) - no extra ingestion-time storage
        # needed, see retrieval.get_chunk_neighbors' own docstring.
        neighbors = get_chunk_neighbors(
            source_path=row["source_path"],
            chunk_index=row["chunk_index"],
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

    return {"query": q, "top_k": top_k, "results": enriched}


@app.get("/api/file")
def get_file(path: str = Query(..., description="A file's path, relative to Knowledge_Base/")):
    """Serve one document from Knowledge_Base/ so the frontend's side
    panel can display it (inline for PDF/text, a download for anything
    a browser can't render natively, e.g. .docx/.xlsx).

    SECURITY: this is the one place in rag/ that turns a caller-supplied
    string into a filesystem path, so it's the one place a path-traversal
    attack (e.g. path="../../../../Windows/System32/some_file") could read
    something it shouldn't. Guarded by resolving the full path and
    checking it's still actually inside KNOWLEDGE_BASE_DIR before ever
    touching the filesystem - a relative path with ".." segments resolves
    to somewhere OUTSIDE that directory, and gets rejected here rather
    than ever being opened.
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
        <button id="reindex-btn" onclick="startReindex()" title="Re-embed both corpora from the local files (social_share CSV + Knowledge_Base)">re-index</button>
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
          const ss = s.stats.social_share;
          const kb = s.stats.knowledge_base;
          el.textContent = `re-index finished ${fmtTime(s.finished_at)}: ` +
            `social_share ${ss.posts_ingested} posts, ${ss.chunks_written} chunks written, ${ss.orphaned_chunks_deleted} orphans deleted; ` +
            `Knowledge_Base ${kb.files_ingested} files, ${kb.chunks_written} chunks written, ${kb.orphaned_chunks_deleted} orphans deleted`;
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
      if (!confirm('Re-index both corpora from the local files? Re-embeds everything, takes 30-45 minutes.')) {
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
      const response = await fetch(`/api/search?q=${encodeURIComponent(q)}&top_k=${topK}`);
      const data = await response.json();

      statusEl.textContent = `${data.results.length} result(s) for "${data.query}"`;

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
