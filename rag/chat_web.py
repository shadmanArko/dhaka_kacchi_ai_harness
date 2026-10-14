"""The customer chatbot as a web page: rag/chatbot.py behind one FastAPI app.

Same shape as rag/webui.py - one FastAPI app, one inline HTML page, no
frontend build step. The difference is what sits behind it: webui.py is pure
retrieval for the team, this is the LLM chatbot for customers.

It is ALWAYS the public view. The page never chooses the reader: every
session is built with the public credential, so nothing typed into this page
can reach the internal stores (MULTI_STORE_DESIGN.md section 7). There is
deliberately no `--internal` switch here - staff use `make rag-chat-internal`.

Each browser tab gets its own Chatbot (its own short history), keyed by a
random id the page generates. Sessions live in memory only and the oldest are
dropped past MAX_SESSIONS, so a restart simply starts every chat fresh.

Because the page may be shared publicly (e.g. through a Cloudflare tunnel),
every question is paid for and anyone with the link can ask: chat is rate
limited per visitor (PER_CLIENT_LIMIT per PER_CLIENT_WINDOW_S) and in total
(DAILY_LIMIT per day), and FastAPI's /docs pages are switched off.

Run it with: uv run uvicorn rag.chat_web:app --port 8011 --host 127.0.0.1
Then open http://127.0.0.1:8011 in a browser.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict, deque
from datetime import date
from pathlib import Path

import openai
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from rag.chatbot import Chatbot, build_chatbot

_LOGO_FILE = Path(__file__).parent / "static" / "logo.webp"

# Open chats kept in memory; the least recently used is dropped beyond this.
MAX_SESSIONS = 200

# A customer question longer than this is almost certainly not a question -
# and every character is paid for on the LLM call.
MAX_MESSAGE_CHARS = 500

# Questions one visitor may ask per window, and all visitors together per day.
# Generous for a real customer, a hard ceiling on what a stranger can spend.
PER_CLIENT_LIMIT = 15
PER_CLIENT_WINDOW_S = 10 * 60
DAILY_LIMIT = 300

app = FastAPI(title="Dhaka Kacchi Berlin - Chat", docs_url=None, redoc_url=None, openapi_url=None)

_sessions: OrderedDict[str, tuple[Chatbot, threading.Lock]] = OrderedDict()
_sessions_lock = threading.Lock()

_limits_lock = threading.Lock()
_client_hits: dict[str, deque[float]] = {}
_daily = {"day": date.today(), "count": 0}


class ChatIn(BaseModel):
    session_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class ResetIn(BaseModel):
    session_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9-]+$")


def _session(session_id: str) -> tuple[Chatbot, threading.Lock]:
    with _sessions_lock:
        if session_id in _sessions:
            _sessions.move_to_end(session_id)
            return _sessions[session_id]
    # Built outside the global lock: it asks the database which stores the
    # public reader may see, and one slow start must not block every chat.
    entry = (build_chatbot(internal=False), threading.Lock())
    with _sessions_lock:
        entry = _sessions.setdefault(session_id, entry)
        while len(_sessions) > MAX_SESSIONS:
            _sessions.popitem(last=False)
    return entry


def _client_key(request: Request) -> str:
    # Behind a Cloudflare tunnel every request arrives from 127.0.0.1, and the
    # real visitor is in CF-Connecting-IP. Local requests have no such header.
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown"
    )


def _check_limits(client: str) -> None:
    """Raise 429 if this visitor, or everyone today, has asked too much."""
    now = time.monotonic()
    with _limits_lock:
        if _daily["day"] != date.today():
            _daily.update(day=date.today(), count=0)
        if _daily["count"] >= DAILY_LIMIT:
            raise HTTPException(status_code=429, detail="daily")
        hits = _client_hits.setdefault(client, deque())
        while hits and now - hits[0] > PER_CLIENT_WINDOW_S:
            hits.popleft()
        if len(hits) >= PER_CLIENT_LIMIT:
            raise HTTPException(status_code=429, detail="client")
        hits.append(now)
        _daily["count"] += 1
        # Forget visitors who have gone quiet, so the dict cannot grow forever.
        if len(_client_hits) > 5000:
            for key in [
                k for k, v in _client_hits.items() if not v or now - v[-1] > PER_CLIENT_WINDOW_S
            ]:
                del _client_hits[key]


def _source_label(source_path: str) -> str:
    """ "public/brand-book.md::6-frequently-asked-questions" -> "Frequently asked questions"."""
    section = source_path.split("::", 1)[-1]
    words = re.sub(r"^\d+-", "", section).replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else source_path


@app.post("/api/chat")
def chat(body: ChatIn, request: Request) -> dict:
    # A plain `def`: FastAPI runs it in a worker thread, so the blocking
    # retrieval + LLM call never stalls other chats.
    _check_limits(_client_key(request))
    bot, lock = _session(body.session_id)
    with lock:  # one turn at a time per chat - history is not thread-safe
        try:
            reply = bot.ask(body.message.strip())
        except openai.APIError as exc:
            raise HTTPException(status_code=502, detail="The assistant is unavailable.") from exc
    return {
        "reply": reply.text,
        "sources": list(dict.fromkeys(_source_label(s) for s in reply.sources)),
    }


@app.post("/api/reset")
def reset(body: ResetIn) -> dict:
    with _sessions_lock:
        _sessions.pop(body.session_id, None)
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _PAGE


@app.get("/logo.webp", include_in_schema=False)
def logo() -> FileResponse:
    # The business's own logo (gold on transparent, made for a dark page),
    # copied from dhakakacchi.com so this page matches the website.
    return FileResponse(
        _LOGO_FILE, media_type="image/webp", headers={"Cache-Control": "public, max-age=86400"}
    )


# The page mirrors www.dhakakacchi.com's own theme: near-black ink, warm gold,
# cream text, gold hairlines, 2px corners, Cormorant Garamond display type
# over DM Sans, tiny wide-tracked uppercase labels. The colour values are the
# website's own CSS custom properties, copied as-is (oklch).
_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0b0d">
<title>Dhaka Kacchi Chat</title>
<link rel="icon" href="/logo.webp">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet"
  href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;1,300;1,400&family=DM+Sans:wght@300;400;500;600&display=swap">
<style>
  :root {
    /* www.dhakakacchi.com tokens */
    --black-ink: oklch(6% .006 275);
    --deep: oklch(9% .008 80);
    --surface: oklch(13% .011 80);
    --gold: oklch(74% .084 82);
    --gold-2: oklch(87% .055 82);
    --gold-3: oklch(58% .11 78);
    --cream: oklch(93% .036 85);
    --muted-warm: oklch(58% .024 78);
    --line: oklch(74% .084 82 / .18);
    --line-strong: oklch(74% .084 82 / .55);
    --radius: 2px;
    --serif: "Cormorant Garamond", Georgia, serif;
    --sans: "DM Sans", system-ui, sans-serif;
    color-scheme: dark;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    font: 300 15.5px/1.6 var(--sans);
    color: var(--cream);
    background:
      radial-gradient(60rem 28rem at 50% -8rem, oklch(74% .084 82 / .10), transparent 70%),
      var(--black-ink);
    background-attachment: fixed;
    -webkit-font-smoothing: antialiased;
  }
  ::selection { background: var(--gold); color: var(--black-ink); }

  .app {
    height: 100dvh; max-width: 820px; margin: 0 auto;
    display: flex; flex-direction: column; padding: 14px 16px 12px; gap: 14px;
  }

  /* Top bar - logo left, actions right, gold hairline below */
  .topbar {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding-bottom: 12px; border-bottom: 1px solid var(--line);
  }
  .topbar img { height: 46px; width: auto; display: block; }
  .actions { display: flex; gap: 8px; }
  .btn {
    font: 500 .66rem/1 var(--sans); text-transform: uppercase; letter-spacing: .18em;
    color: var(--gold); background: transparent; border: 1px solid var(--line-strong);
    border-radius: var(--radius); padding: 10px 14px; cursor: pointer; text-decoration: none;
    transition: background .2s, color .2s, border-color .2s; white-space: nowrap;
  }
  .btn:hover { border-color: var(--gold); }
  .btn.solid { background: var(--gold); color: var(--black-ink); border-color: var(--gold); }
  .btn.solid:hover { background: var(--gold-2); border-color: var(--gold-2); }

  /* Heading - the website's eyebrow + serif headline with a gold italic */
  .intro { text-align: center; }
  .eyebrow {
    display: block; font: 400 .66rem/1.4 var(--sans); text-transform: uppercase;
    letter-spacing: .3em; color: var(--gold); margin-bottom: 6px;
  }
  h1 {
    margin: 0; font: 300 clamp(1.7rem, 4.5vw, 2.4rem)/1.1 var(--serif);
    letter-spacing: -.01em; color: var(--cream);
  }
  h1 em { font-style: italic; color: var(--gold); }
  .ornament {
    display: flex; align-items: center; justify-content: center; gap: 10px;
    margin-top: 8px; color: var(--gold-3); font: italic 400 .9rem var(--serif); letter-spacing: .12em;
  }
  .ornament::before, .ornament::after { content: ""; width: 42px; height: 1px; background: var(--line-strong); }

  /* Chat panel */
  .card {
    position: relative; flex: 1; min-height: 0; display: flex; flex-direction: column;
    background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  }
  .card::before {  /* thin gold rule across the top, as on the site's cards */
    content: ""; position: absolute; inset: -1px -1px auto; height: 1px;
    background: linear-gradient(90deg, transparent, var(--gold), transparent);
  }
  .status {
    display: flex; align-items: center; gap: 10px; padding: 12px 18px;
    border-bottom: 1px solid var(--line);
    font: 400 .64rem/1 var(--sans); text-transform: uppercase; letter-spacing: .22em; color: var(--muted-warm);
  }
  .status strong { color: var(--gold); font-weight: 500; }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--gold); box-shadow: 0 0 0 3px oklch(74% .084 82 / .18); }

  .messages {
    flex: 1; overflow-y: auto; padding: 22px 18px 10px;
    display: flex; flex-direction: column; gap: 16px; scroll-behavior: smooth;
    scrollbar-width: thin; scrollbar-color: var(--line-strong) transparent;
  }
  .row { display: flex; gap: 10px; align-items: flex-start; animation: rise .3s ease-out; }
  .row.user { justify-content: flex-end; }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  .avatar {
    flex: none; width: 30px; height: 30px; display: grid; place-items: center;
    border: 1px solid var(--line-strong); border-radius: var(--radius); color: var(--gold); background: var(--deep);
  }
  .bubble {
    max-width: min(80%, 580px); padding: 12px 16px; border-radius: var(--radius);
    overflow-wrap: anywhere; white-space: pre-wrap;
  }
  .bot .bubble { background: var(--deep); border: 1px solid var(--line); color: var(--cream); }
  .user .bubble { background: var(--gold); color: var(--black-ink); font-weight: 400; }
  .bubble a { color: var(--gold-2); text-decoration: underline; text-decoration-color: var(--line-strong); text-underline-offset: 3px; }
  .user .bubble a { color: inherit; }
  .bubble strong { font-weight: 500; color: var(--gold-2); }
  .user .bubble strong { color: inherit; }
  .sources { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0 0; padding: 0; list-style: none; white-space: normal; }
  .sources li {
    font: 400 .58rem/1 var(--sans); text-transform: uppercase; letter-spacing: .18em;
    color: var(--gold); border: 1px solid var(--line); border-radius: var(--radius); padding: 5px 8px;
  }
  .bubble.error { background: transparent; border: 1px dashed oklch(57.7% .245 27.325 / .7); color: var(--gold-2); }

  .typing { display: inline-flex; gap: 6px; padding: 5px 2px; }
  .typing span { width: 5px; height: 5px; border-radius: 50%; background: var(--gold); animation: blink 1.2s infinite ease-in-out; }
  .typing span:nth-child(2) { animation-delay: .15s; }
  .typing span:nth-child(3) { animation-delay: .3s; }
  @keyframes blink { 0%, 80%, 100% { opacity: .2; } 40% { opacity: 1; } }

  /* Suggested questions - the website's outlined gold tags */
  .suggestions { display: flex; flex-wrap: wrap; gap: 8px; padding: 4px 18px 14px; }
  .chip {
    font: 400 .66rem/1 var(--sans); text-transform: uppercase; letter-spacing: .15em;
    color: var(--gold); background: transparent; border: 1px solid var(--line-strong);
    border-radius: var(--radius); padding: 9px 13px; cursor: pointer;
    transition: background .2s, color .2s, border-color .2s;
  }
  .chip:hover { background: var(--gold); color: var(--black-ink); border-color: var(--gold); }

  /* Composer */
  form {
    display: flex; gap: 10px; align-items: flex-end; padding: 12px;
    border-top: 1px solid var(--line); background: var(--deep);
  }
  textarea {
    flex: 1; resize: none; max-height: 140px; min-height: 46px;
    border: 1px solid var(--line-strong); border-radius: var(--radius); padding: 12px 14px;
    font: 300 15px/1.5 var(--sans); color: var(--cream); background: var(--black-ink); outline: none;
    transition: border-color .2s, box-shadow .2s;
  }
  textarea:focus { border-color: var(--gold); box-shadow: 0 0 0 3px oklch(74% .084 82 / .15); }
  textarea::placeholder { color: var(--muted-warm); }
  .send {
    flex: none; width: 46px; height: 46px; border: 0; border-radius: var(--radius); cursor: pointer;
    background: var(--gold); color: var(--black-ink); display: grid; place-items: center;
    transition: background .2s, opacity .2s;
  }
  .send:hover:not(:disabled) { background: var(--gold-2); }
  .send:disabled { opacity: .35; cursor: not-allowed; }

  footer {
    text-align: center; font: 400 .6rem/1.6 var(--sans); text-transform: uppercase;
    letter-spacing: .25em; color: var(--muted-warm);
  }
  footer a { color: var(--gold); text-decoration: none; }

  :focus-visible { outline: 1px solid var(--gold); outline-offset: 2px; }
  textarea:focus-visible { outline: none; }  /* has its own gold border + glow */

  @media (max-width: 560px) {
    .app { padding: 10px 10px 8px; gap: 10px; }
    .eyebrow { letter-spacing: .18em; font-size: .58rem; }
    .status span.sub { display: none; }
    .topbar img { height: 38px; }
    .btn { padding: 9px 10px; letter-spacing: .12em; }
      .ornament { display: none; }
    .bubble { max-width: 86%; }
    footer { letter-spacing: .15em; }
  }
  @media (max-height: 640px) { .intro { display: none; } }
</style>
</head>
<body>
<div class="app">
  <header class="topbar">
    <a href="https://www.dhakakacchi.com/" target="_blank" rel="noopener" aria-label="Dhaka Kacchi Berlin website">
      <img src="/logo.webp" alt="Dhaka Kacchi Berlin">
    </a>
    <div class="actions">
      <button class="btn" id="newChat" type="button" title="Start a new chat">↺ New chat</button>
      <a class="btn solid" href="https://www.dhakakacchi.com/" target="_blank" rel="noopener">Order now</a>
    </div>
  </header>

  <section class="intro">
    <span class="eyebrow">Berlin, Germany · Cooked fresh every Saturday</span>
    <h1>Ask us about your <em>kacchi</em></h1>
    <div class="ornament">menu · ordering · pickup · delivery</div>
  </section>

  <main class="card">
    <div class="status"><span class="dot"></span><strong>Kacchi assistant</strong><span class="sub">· replies in seconds</span></div>
    <div class="messages" id="messages" aria-live="polite"></div>
    <div class="suggestions" id="suggestions"></div>
    <form id="composer" autocomplete="off">
      <textarea id="input" rows="1" maxlength="500"
        placeholder="Ask about the kacchi, ordering or delivery…" aria-label="Your message"></textarea>
      <button class="send" id="send" type="submit" aria-label="Send">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M5 12h14"/><path d="M13 6l6 6-6 6"/></svg>
      </button>
    </form>
  </main>

  <footer>AI assistant · prices &amp; times may change · order on
    <a href="https://wa.me/4915563583687" target="_blank" rel="noopener">WhatsApp</a></footer>
</div>

<script>
  const WELCOME = "Assalamu alaikum, and welcome to Dhaka Kacchi Berlin.\n" +
    "Ask me about our Old Dhaka kacchi, how ordering works, pickup or delivery.";
  const SUGGESTIONS = [
    "What's in the kacchi?",
    "How much is delivery?",
    "When do I need to order?",
    "Where is the pickup point?",
    "What is Borhani?",
    "Do you do catering?",
  ];

  const $messages = document.getElementById("messages");
  const $suggestions = document.getElementById("suggestions");
  const $form = document.getElementById("composer");
  const $input = document.getElementById("input");
  const $send = document.getElementById("send");
  let busy = false;

  function newId() {
    const raw = crypto.randomUUID ? crypto.randomUUID()
      : String(Date.now()) + Math.random().toString(16).slice(2);
    return raw.replace(/[^A-Za-z0-9-]/g, "");
  }
  // A fresh chat per page load: the page keeps no transcript, so the server
  // must not either - otherwise a reload would answer with unseen history.
  let sessionId = newId();

  function escapeHtml(s) {
    const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
    return s.replace(/[&<>"']/g, c => map[c]);
  }
  // Escape first, then add the little formatting the bot uses.
  function format(text) {
    let html = escapeHtml(text);
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/(https?:\/\/[^\s<]+[^\s<.,;:!?)])/g,
      '<a href="$1" target="_blank" rel="noopener">$1</a>');
    html = html.replace(/\+49[\d ]{9,16}\d/g, m =>
      '<a href="https://wa.me/' + m.replace(/\D/g, "") + '" target="_blank" rel="noopener">' + m + "</a>");
    html = html.replace(/(^|[\s(])([\w.+-]+@[\w-]+\.[\w.]+)/g, '$1<a href="mailto:$2">$2</a>');
    return html;
  }

  // The steaming pot from the logo, as a small gold line icon.
  const POT = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M5 12c0 4.5 2.8 8 7 8s7-3.5 7-8"/><path d="M4 12h16"/>' +
    '<path d="M10 9c-1-1.2.8-2-.2-3.4M13.5 9c-1-1.2.8-2-.2-3.4"/></svg>';

  function addMessage(role, text, opts = {}) {
    const row = document.createElement("div");
    row.className = "row " + role;
    if (role === "bot") row.innerHTML = '<div class="avatar" aria-hidden="true">' + POT + "</div>";
    const bubble = document.createElement("div");
    bubble.className = "bubble" + (opts.error ? " error" : "");
    if (opts.typing) {
      bubble.innerHTML = '<span class="typing" aria-label="Typing"><span></span><span></span><span></span></span>';
    } else {
      bubble.innerHTML = role === "bot" ? format(text) : escapeHtml(text);
    }
    if (opts.sources && opts.sources.length) {
      const ul = document.createElement("ul");
      ul.className = "sources";
      ul.title = "Where this answer came from";
      for (const s of opts.sources) {
        const li = document.createElement("li"); li.textContent = s; ul.appendChild(li);
      }
      bubble.appendChild(ul);
    }
    row.appendChild(bubble);
    $messages.appendChild(row);
    $messages.scrollTop = $messages.scrollHeight;
    return row;
  }

  function renderSuggestions(show) {
    $suggestions.innerHTML = "";
    if (!show) return;
    for (const q of SUGGESTIONS) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "chip"; b.textContent = q;
      b.addEventListener("click", () => send(q));
      $suggestions.appendChild(b);
    }
  }

  function setBusy(v) {
    busy = v;
    $send.disabled = v || !$input.value.trim();
  }

  const WHATSAPP = "+49 155 63583687";

  async function send(text) {
    text = text.trim();
    if (!text || busy) return;
    renderSuggestions(false);
    addMessage("user", text);
    $input.value = ""; autosize();
    setBusy(true);
    const typing = addMessage("bot", "", { typing: true });
    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, message: text }),
      });
      typing.remove();
      if (res.status === 429) {
        addMessage("bot", "You've asked a lot of questions in a short time - please wait a few " +
          "minutes and try again. For anything urgent, message us on WhatsApp: " + WHATSAPP, { error: true });
        return;
      }
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      addMessage("bot", data.reply, { sources: data.sources });
    } catch (e) {
      typing.remove();
      addMessage("bot", "Sorry, I couldn't answer just now. Please try again in a moment, " +
        "or message us on WhatsApp: " + WHATSAPP, { error: true });
    } finally {
      setBusy(false);
      $input.focus();
    }
  }

  function autosize() {
    $input.style.height = "auto";
    $input.style.height = Math.min($input.scrollHeight, 140) + "px";
    $send.disabled = busy || !$input.value.trim();
  }

  function start() {
    $messages.innerHTML = "";
    addMessage("bot", WELCOME);
    renderSuggestions(true);
    $input.value = ""; autosize();
    $input.focus();
  }

  $input.addEventListener("input", autosize);
  $input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send($input.value); }
  });
  $form.addEventListener("submit", e => { e.preventDefault(); send($input.value); });
  document.getElementById("newChat").addEventListener("click", () => {
    fetch("/api/reset", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    }).catch(() => {});
    sessionId = newId();
    start();
  });

  start();
</script>
</body>
</html>
"""
