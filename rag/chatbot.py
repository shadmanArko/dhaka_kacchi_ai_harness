"""A RAG chatbot: retrieve from the vector stores, answer with Claude via OpenRouter.

One LLM call per user turn, and every design choice below is about keeping
that call small:

  * Retrieval runs in Python BEFORE the call, not as a tool the model calls.
    A tool round trip would send the whole prompt twice per turn.
  * Only TOP_K_PER_STORE chunks per readable store go in (each chunk is at
    most ~300 bge-m3 tokens - stores.toml's chunk_size_tokens).
  * Retrieved context rides on the CURRENT user message only. History keeps
    the bare question and answer, so old context is never re-sent.
  * History is capped at HISTORY_TURNS exchanges.
  * facts.yaml is sent compacted: comments, nulls and blank lines stripped.
  * Effort is "low" and the system prompt asks for short answers.

Which stores the bot can see is decided by the credential, exactly as for
every other reader (MULTI_STORE_DESIGN.md section 7): the default is the
PUBLIC reader, so a customer-facing bot cannot reach `voice_and_rules` no
matter what it is asked. `--internal` loads the internal reader instead, for
the business's own use.

Prices, deadlines and fees come from Knowledge_Base/facts.yaml, never from
the brand book text (RUNBOOK.md section 6.4) - the file is read on every
turn, so an edit to it takes effect immediately with no re-index.

The LLM is reached through OpenRouter's OpenAI-compatible API
(OPENROUTER_API_KEY, CHAT_MODEL - see rag/config.py), so this uses the
`openai` SDK pointed at OpenRouter, not the Anthropic SDK.

Run it with: uv run python -m rag.chatbot            (customer view)
             uv run python -m rag.chatbot --internal (staff view)
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field

import openai

from rag.config import (
    FACTS_FILE,
    OPENROUTER_BASE_URL,
    ChatSettings,
    RagReaderSettings,
    StoreRegistry,
    load_chat_settings,
    load_rag_internal_reader_settings,
    load_rag_public_reader_settings,
    load_store_registry,
)
from rag.retrieval import list_stores, retrieve

# Chunks pulled from each readable store per turn. 3 x ~300 tokens is enough
# for the brand book's one-section-per-chunk layout.
TOP_K_PER_STORE = 3

# Previous question/answer pairs kept for follow-ups ("and how much is it?").
HISTORY_TURNS = 3

# Covers the model's reasoning plus the answer; at effort "low" a reply rarely
# uses a fraction of it (~150 tokens measured), and it is billed only for what
# is generated. OpenRouter still refuses (402) a request whose cap exceeds the
# remaining credit, so this is kept modest.
MAX_TOKENS = 1024

# OpenRouter maps this onto Claude's own effort setting.
EFFORT = "low"

_PUBLIC_SYSTEM = """\
You are the customer chatbot for Dhaka Kacchi Berlin.
Answer only from the <facts> and <context> given. Facts beat context for any \
price, time, fee or place. Anything marked (unconfirmed) may be quoted but \
must be flagged as subject to change. If the answer is not there, say you \
are not sure and suggest WhatsApp +49 155 63583687.
Reply in the customer's language, warm and brief: at most 3 short sentences \
unless they ask for detail. No markdown headings."""

_INTERNAL_SYSTEM = """\
You assist the Dhaka Kacchi Berlin team. Answer only from the <facts> and \
<context> given, and say plainly when they do not cover the question. \
Values marked (unconfirmed) still need the owner's confirmation - say so \
when you use one. Be concise."""

_REFUSAL_REPLY = "Sorry, I can't help with that. For anything else, just ask!"

# An inline comment that flags a value the owner has not confirmed yet. Matched
# as a prefix: the file also has "[CONFIRM current]" and "[CONFIRM still ...]".
_CONFIRM_TAG = "[CONFIRM"


def compact_facts(raw: str) -> str:
    """facts.yaml with everything an LLM does not need removed.

    Kept as text rather than parsed YAML so the [CONFIRM] comments, which a
    YAML parser would discard, survive as "(unconfirmed)" markers. Dropped:
    full-line comments, blank lines and other inline comments. A null value
    becomes "unknown", so the model says it does not know instead of
    guessing - or quoting "null" at a customer.
    """
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        value, _, comment = line.partition(" #")
        value = value.rstrip()
        # Also inside flow maps: "{ certified: null, certifier: null }".
        value = re.sub(r":\s*null\b", ": unknown", value)
        if _CONFIRM_TAG in comment:
            value += " (unconfirmed)"
        lines.append(value)
    return "\n".join(lines)


def format_context(rows: list[dict]) -> str:
    """Retrieved chunks as numbered, labelled blocks - one header line each."""
    blocks = []
    for n, row in enumerate(rows, start=1):
        # "public/brand-book.md::what-we-serve" -> "brand-book.md / what-we-serve"
        label = row["source_path"].rsplit("/", 1)[-1].replace("::", " / ")
        blocks.append(f"[{n}] {label}\n{row['chunk_text'].strip()}")
    return "\n\n".join(blocks)


@dataclass
class Reply:
    text: str
    sources: list[str]
    input_tokens: int
    output_tokens: int


@dataclass
class Chatbot:
    chat_settings: ChatSettings
    reader_settings: RagReaderSettings
    registry: StoreRegistry
    internal: bool = False
    history: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._client = openai.OpenAI(
            api_key=self.chat_settings.api_key, base_url=OPENROUTER_BASE_URL
        )
        # Asked of the database once: which stores this credential can read.
        self._stores = [
            s["store"] for s in list_stores(registry=self.registry, settings=self.reader_settings)
        ]
        if not self._stores:
            raise RuntimeError("this credential can read no stores - check the grants")

    def _retrieve(self, question: str) -> list[dict]:
        # A follow-up like "how much is it?" embeds poorly on its own, so the
        # previous question is prepended. Embedding is local, so this is free.
        query = question
        if self.history:
            query = f"{self.history[-2]['content']}\n{question}"
        rows = []
        for store in self._stores:
            rows += retrieve(
                query,
                store=store,
                top_k=TOP_K_PER_STORE,
                registry=self.registry,
                settings=self.reader_settings,
            )
        return rows

    def ask(self, question: str) -> Reply:
        rows = self._retrieve(question)
        facts = compact_facts(FACTS_FILE.read_text(encoding="utf-8"))
        system = _INTERNAL_SYSTEM if self.internal else _PUBLIC_SYSTEM

        user_turn = (
            f"<facts>\n{facts}\n</facts>\n<context>\n{format_context(rows)}\n</context>\n\n"
            f"{question}"
        )
        response = self._client.chat.completions.create(
            model=self.chat_settings.model,
            max_tokens=MAX_TOKENS,
            reasoning_effort=EFFORT,
            messages=[
                {"role": "system", "content": system},
                *self.history,
                {"role": "user", "content": user_turn},
            ],
        )

        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        # A safety decline arrives as finish_reason "content_filter" (OpenRouter
        # maps Claude's "refusal" onto it), usually with no text at all.
        if choice.finish_reason == "content_filter" or not text:
            text = _REFUSAL_REPLY

        # Only the bare question and answer go into history - the facts and
        # context were for this turn and are re-retrieved fresh next turn.
        self.history += [
            {"role": "user", "content": question},
            {"role": "assistant", "content": text},
        ]
        self.history = self.history[-2 * HISTORY_TURNS :]

        return Reply(
            text=text,
            sources=[r["source_path"] for r in rows],
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
        )


def build_chatbot(*, internal: bool = False) -> Chatbot:
    reader = load_rag_internal_reader_settings() if internal else load_rag_public_reader_settings()
    return Chatbot(
        chat_settings=load_chat_settings(),
        reader_settings=reader,
        registry=load_store_registry(),
        internal=internal,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the Dhaka Kacchi RAG stores.")
    parser.add_argument(
        "--internal", action="store_true", help="staff view: read internal stores too"
    )
    parser.add_argument("--sources", action="store_true", help="print retrieved sections")
    args = parser.parse_args()

    bot = build_chatbot(internal=args.internal)
    print(f"Dhaka Kacchi chatbot ({bot.chat_settings.model}, stores: {', '.join(bot._stores)})")
    print("Ask a question. Empty line or Ctrl+C to quit.\n")
    total_in = total_out = 0
    while True:
        try:
            question = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question:
            break
        try:
            reply = bot.ask(question)
        except openai.APIError as exc:
            print(f"[LLM error: {exc}]\n")
            continue
        total_in += reply.input_tokens
        total_out += reply.output_tokens
        print(f"bot> {reply.text}")
        if args.sources:
            print("     sources: " + "; ".join(dict.fromkeys(reply.sources)))
        print(
            f"     [tokens in {reply.input_tokens} / out {reply.output_tokens}"
            f" | session in {total_in} / out {total_out}]\n"
        )


if __name__ == "__main__":
    main()
