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

import openai
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from rag.chatbot import Chatbot, build_chatbot

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


_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Dhaka Kacchi Chat</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,650&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #f8f2e8;
    --bg-glow: #f3e3c6;
    --surface: #fffdf9;
    --surface-2: #f4ece0;
    --ink: #2a211b;
    --ink-soft: #6e6157;
    --line: #e7dccd;
    --green: #134e3a;
    --green-ink: #ffffff;
    --saffron: #d98e1f;
    --saffron-soft: #fbecd2;
    --clay: #a8492a;
    --shadow: 0 1px 2px rgba(60, 40, 20, .06), 0 12px 32px rgba(60, 40, 20, .08);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #15120f;
      --bg-glow: #2a2116;
      --surface: #1e1a16;
      --surface-2: #29231d;
      --ink: #f2e9dd;
      --ink-soft: #b2a596;
      --line: #352d25;
      --green: #2f8a68;
      --green-ink: #ffffff;
      --saffron: #f0a83a;
      --saffron-soft: #3a2c17;
      --clay: #e07a52;
      --shadow: 0 1px 2px rgba(0, 0, 0, .3), 0 12px 32px rgba(0, 0, 0, .35);
    }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    font: 400 15.5px/1.55 Inter, system-ui, -apple-system, "Segoe UI", sans-serif;
    color: var(--ink);
    background:
      radial-gradient(900px 500px at 85% -10%, var(--bg-glow), transparent 70%),
      radial-gradient(700px 420px at -10% 110%, var(--bg-glow), transparent 70%),
      var(--bg);
    background-attachment: fixed;
  }
  .app {
    height: 100dvh;
    max-width: 780px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    padding: 18px 16px 14px;
    gap: 14px;
  }

  /* Header */
  header { display: flex; align-items: center; gap: 14px; }
  .mark {
    flex: none; width: 50px; height: 50px; border-radius: 15px;
    display: grid; place-items: center;
    background: var(--green); color: var(--saffron);
    box-shadow: var(--shadow);
  }
  .titles { min-width: 0; flex: 1; }
  h1 {
    margin: 0; font: 650 1.35rem/1.15 Fraunces, Georgia, serif; letter-spacing: -.01em;
  }
  .tagline { margin: 2px 0 0; color: var(--ink-soft); font-size: .9rem; }
  .new-chat {
    flex: none; border: 1px solid var(--line); background: var(--surface);
    color: var(--ink); border-radius: 999px; padding: 8px 14px; font: inherit;
    font-size: .85rem; font-weight: 500; cursor: pointer;
    transition: border-color .15s, background .15s;
  }
  .new-chat:hover { border-color: var(--saffron); }

  /* Chat card */
  .card {
    flex: 1; min-height: 0; display: flex; flex-direction: column;
    background: var(--surface); border: 1px solid var(--line);
    border-radius: 22px; box-shadow: var(--shadow); overflow: hidden;
  }
  .status {
    display: flex; align-items: center; gap: 8px; padding: 12px 18px;
    border-bottom: 1px solid var(--line); font-size: .82rem; color: var(--ink-soft);
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #2fa36b; box-shadow: 0 0 0 3px rgba(47, 163, 107, .18); }
  .status strong { color: var(--ink); font-weight: 600; }
  .messages {
    flex: 1; overflow-y: auto; padding: 20px 18px 8px;
    display: flex; flex-direction: column; gap: 14px; scroll-behavior: smooth;
  }
  .row { display: flex; gap: 10px; align-items: flex-end; animation: rise .25s ease-out; }
  .row.user { justify-content: flex-end; }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  .avatar {
    flex: none; width: 30px; height: 30px; border-radius: 10px;
    display: grid; place-items: center; background: var(--saffron-soft); color: var(--clay);
  }
  .bubble {
    max-width: min(80%, 560px); padding: 11px 15px; border-radius: 18px;
    overflow-wrap: anywhere; white-space: pre-wrap;
  }
  .bot .bubble { background: var(--surface-2); border-bottom-left-radius: 6px; }
  .user .bubble { background: var(--green); color: var(--green-ink); border-bottom-right-radius: 6px; }
  .bubble a { color: inherit; text-decoration-color: var(--saffron); text-underline-offset: 3px; font-weight: 500; }
  .bubble strong { font-weight: 600; }
  .sources { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0 0; padding: 0; list-style: none; }
  .sources li {
    font-size: .74rem; color: var(--ink-soft); background: var(--surface);
    border: 1px solid var(--line); border-radius: 999px; padding: 2px 9px; white-space: nowrap;
  }
  .bubble.error { background: transparent; border: 1px dashed var(--clay); color: var(--clay); }

  .typing { display: inline-flex; gap: 5px; padding: 4px 2px; }
  .typing span {
    width: 7px; height: 7px; border-radius: 50%; background: var(--ink-soft);
    animation: blink 1.2s infinite ease-in-out;
  }
  .typing span:nth-child(2) { animation-delay: .15s; }
  .typing span:nth-child(3) { animation-delay: .3s; }
  @keyframes blink { 0%, 80%, 100% { opacity: .25; transform: translateY(0); } 40% { opacity: 1; transform: translateY(-3px); } }

  /* Suggestions */
  .suggestions { display: flex; flex-wrap: wrap; gap: 8px; padding: 4px 18px 12px; }
  .chip {
    border: 1px solid var(--line); background: var(--surface); color: var(--ink);
    border-radius: 999px; padding: 7px 13px; font: inherit; font-size: .86rem; cursor: pointer;
    transition: border-color .15s, background .15s, transform .15s;
  }
  .chip:hover { border-color: var(--saffron); background: var(--saffron-soft); transform: translateY(-1px); }

  /* Composer */
  form {
    display: flex; gap: 10px; align-items: flex-end; padding: 12px;
    border-top: 1px solid var(--line); background: var(--surface);
  }
  textarea {
    flex: 1; resize: none; max-height: 140px; min-height: 46px;
    border: 1px solid var(--line); border-radius: 14px; padding: 12px 14px;
    font: inherit; color: var(--ink); background: var(--bg); outline: none;
    transition: border-color .15s, box-shadow .15s;
  }
  textarea:focus { border-color: var(--saffron); box-shadow: 0 0 0 3px var(--saffron-soft); }
  textarea::placeholder { color: var(--ink-soft); }
  .send {
    flex: none; width: 46px; height: 46px; border: 0; border-radius: 14px; cursor: pointer;
    background: var(--saffron); color: #2a1a05; display: grid; place-items: center;
    transition: transform .15s, opacity .15s;
  }
  .send:hover:not(:disabled) { transform: translateY(-1px); }
  .send:disabled { opacity: .45; cursor: not-allowed; }

  footer { text-align: center; font-size: .76rem; color: var(--ink-soft); }
  footer a { color: inherit; }

  @media (max-width: 520px) {
    .app { padding: 12px 10px 10px; gap: 10px; }
    .tagline { display: none; }
    .bubble { max-width: 86%; }
    .new-chat span { display: none; }
  }
</style>
</head>
<body>
<div class="app">
  <header>
    <div class="mark" aria-hidden="true">
      <!-- a sealed dum pot -->
      <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
        <path d="M4 10h16"/><path d="M5 10c0 5 2.5 9 7 9s7-4 7-9"/><path d="M9 7c0-1.7 1.3-3 3-3s3 1.3 3 3"/>
        <path d="M2.5 10h1.5M20 10h1.5"/><path d="M9.5 14c.8.6 1.6.9 2.5.9s1.7-.3 2.5-.9"/>
      </svg>
    </div>
    <div class="titles">
      <h1>Dhaka Kacchi Berlin</h1>
      <p class="tagline">The real Old Dhaka kacchi, cooked fresh every Saturday in Berlin.</p>
    </div>
    <button class="new-chat" id="newChat" type="button" title="Start a new chat">↺ <span>New chat</span></button>
  </header>

  <main class="card">
    <div class="status"><span class="dot"></span><strong>Kacchi assistant</strong> · menu, ordering, pickup &amp; delivery</div>
    <div class="messages" id="messages" aria-live="polite"></div>
    <div class="suggestions" id="suggestions"></div>
    <form id="composer" autocomplete="off">
      <textarea id="input" rows="1" maxlength="500" placeholder="Ask about the kacchi, ordering or delivery…" aria-label="Your message"></textarea>
      <button class="send" id="send" type="submit" aria-label="Send">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="M13 6l6 6-6 6"/></svg>
      </button>
    </form>
  </main>

  <footer>AI assistant · prices and times may change · to order, message us on
    <a href="https://wa.me/4915563583687" target="_blank" rel="noopener">WhatsApp</a></footer>
</div>

<script>
  const WELCOME = "Assalamu alaikum, and welcome! 👋\nI can tell you about our Old Dhaka kacchi, how ordering works, pickup and delivery. What would you like to know?";
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
    return (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)).replace(/[^A-Za-z0-9-]/g, "");
  }
  // A fresh chat per page load: the page keeps no transcript, so the server
  // must not either - otherwise a reload would answer with unseen history.
  let sessionId = newId();

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  // Escape first, then add the little formatting the bot uses.
  function format(text) {
    let html = escapeHtml(text);
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/(https?:\/\/[^\s<]+[^\s<.,;:!?)])/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
    html = html.replace(/\+49[\d ]{9,16}\d/g, m => '<a href="https://wa.me/' + m.replace(/\D/g, "") + '" target="_blank" rel="noopener">' + m + "</a>");
    html = html.replace(/(^|[\s(])([\w.+-]+@[\w-]+\.[\w.]+)/g, '$1<a href="mailto:$2">$2</a>');
    return html;
  }

  const POT = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 10h16"/><path d="M5 10c0 5 2.5 9 7 9s7-4 7-9"/><path d="M9 7c0-1.7 1.3-3 3-3s3 1.3 3 3"/></svg>';

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
        addMessage("bot", "You've asked a lot of questions in a short time - please wait a few minutes and try again. For anything urgent, message us on WhatsApp: +49 155 63583687", { error: true });
        return;
      }
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      addMessage("bot", data.reply, { sources: data.sources });
    } catch (e) {
      typing.remove();
      addMessage("bot", "Sorry, I couldn't answer just now. Please try again in a moment, or message us on WhatsApp: +49 155 63583687", { error: true });
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
