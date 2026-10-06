# Dhaka Kacchi RAG Subsystem — Engineering Guide

**What this document is.** A complete, self-contained explanation of a working
Retrieval-Augmented Generation (RAG) system: what RAG is, how this particular
one is designed, what every phase does and why, and how to build the same thing
from scratch on your own machine.

**Who it's for.** A competent engineer who has never built a RAG system. No
prior knowledge of embeddings, vector databases, or pgvector is assumed — every
term is explained the first time it appears, and there is a glossary at the end.

**How to read it.** Part 1 is background and applies to every RAG system ever
built. Part 2 describes this specific system and the reasoning behind its
choices. Part 3 walks phase by phase through the actual implementation. Part 4
is a hands-on setup guide you can follow top to bottom to get the same system
running locally. If you only want to run it, jump to Part 4 and use Part 5 as a
reference.

**Accuracy note.** Everything here describes code that exists and has been run
against a real database. The exact things that are *not* built yet (there are
three) are listed explicitly in §2.6 — nothing in this document claims a feature
that isn't there.

---

## Contents

- [Part 1 — RAG from first principles](#part-1--rag-from-first-principles)
- [Part 2 — The Dhaka Kacchi RAG system](#part-2--the-dhaka-kacchi-rag-system)
- [Part 3 — Phase by phase](#part-3--phase-by-phase)
- [Part 4 — Manual setup from scratch](#part-4--manual-setup-from-scratch)
- [Part 5 — Glossary](#part-5--glossary)
- [Appendix A — File-by-file map](#appendix-a--file-by-file-map)
- [Appendix B — Known limitations and risks](#appendix-b--known-limitations-and-risks)

---

# Part 1 — RAG from first principles

## 1.1 The problem RAG solves

A Large Language Model (LLM) is a very capable text engine with two hard
limitations that matter for any business use case.

**It doesn't know your private data.** A model is trained on a large snapshot of
public text, and then its weights are frozen. It has never seen your internal
documents, your database, your contracts, or your customer reviews. If you ask
it about them, it will either say it doesn't know, or — much worse — invent a
plausible-sounding answer. That invention is called **hallucination**.

**Its memory has a fixed size.** A model doesn't remember previous
conversations, and even within one conversation it can only attend to a limited
window of text (the **context window**). You cannot paste an entire document
library into the prompt and hope it fits.

There are two classic ways to work around this:

| Approach | What it means | Why it's often the wrong first choice |
|---|---|---|
| **Fine-tuning** | Continue training the model on your data so the knowledge is baked into the weights. | Expensive, needs a training pipeline, produces a new model artifact per update, cannot cite sources, and the knowledge is stale the moment a document changes. |
| **RAG** | Leave the model alone. At question time, *find* the relevant passages and paste them into the prompt. | Needs a retrieval system (this document), but it's cheap, updates instantly, and can show its sources. |

RAG is the second option. It is usually summarized as **giving the model an
open-book exam**: instead of testing what it memorized, you hand it the right
pages and ask it to answer from those pages.

## 1.2 The core idea

```
User question
      │
      ▼
┌─────────────────────┐
│  RETRIEVAL          │  find the passages in YOUR data that are
│  (search)           │  most relevant to this question
└─────────────────────┘
      │  a handful of relevant text passages
      ▼
┌─────────────────────┐
│  AUGMENTATION       │  build a prompt that contains:
│  (prompt building)  │  the question + the retrieved passages
└─────────────────────┘
      │
      ▼
┌─────────────────────┐
│  GENERATION         │  the LLM answers USING only the passages
│  (the LLM call)     │  it was handed
└─────────────────────┘
      │
      ▼
Answer, ideally with citations back to the source documents
```

The word "Augmented" in RAG refers to the middle box: the prompt is *augmented*
with retrieved evidence before it reaches the model.

Two consequences follow immediately, and they're the reason RAG is worth the
complexity:

1. **Freshness is free.** Update a document, re-run ingestion, and the next
   question sees the new content. No retraining.
2. **Answers are traceable.** Because the evidence was retrieved from a known
   source, the system can show *which* document and *which* passage the answer
   came from — and can refuse to answer when nothing relevant was found.

## 1.3 The five moving parts

Every RAG system, from a weekend prototype to a production service, has these
five parts:

| Part | Job |
|---|---|
| **Corpus** | The body of content you want to be able to answer questions about. Files, database rows, web pages, tickets — anything textual. |
| **Chunker** | Cuts long documents into smaller pieces ("chunks") that are small enough to retrieve precisely and short enough to fit in a prompt. |
| **Embedding model** | Turns a piece of text into a **vector** — a fixed-length list of numbers that represents its *meaning*. |
| **Vector store** | A database that stores those vectors and can quickly answer "which stored vectors are most similar to this one?" |
| **Retriever** | Takes a user's question, turns it into a vector the same way, asks the vector store for the closest matches, and returns the corresponding text. |

Optionally there's a sixth: the **generator** (the LLM call). This system
deliberately stops before that stage — see §2.6.

### Why embeddings are the trick

A **vector** here is a list of numbers — for this system, 1024 numbers per
piece of text. The embedding model is trained so that texts with similar
*meaning* land close together in this 1024-dimensional space, regardless of the
exact words used.

That's the whole magic:

- "How do I reset my password?" and "I forgot my login credentials" contain
  almost no words in common, but their vectors will be close together.
- "What is the story behind the kacchi box?" and a Bengali-language post that
  tells that story will also land close together — *if* the embedding model
  understands Bengali. (Choosing a model that does was a real decision in this
  project; see §3.3.)

"Closeness" is measured with a **similarity metric**. The usual one is
**cosine similarity**, which measures the angle between two vectors: 1.0 means
pointing the same direction (identical meaning), 0 means unrelated, −1 means
opposite. Because this system normalizes every vector to length 1 (see §3.3),
cosine similarity reduces to a plain dot product, which is cheaper to compute —
a detail that shows up again in the SQL in §3.5.

## 1.4 The two halves of the system: ingestion and retrieval

A RAG system is really two pipelines joined by a database.

```
  ── INGESTION (offline, run whenever content changes) ──────────────────
  Source documents → extract text → chunk → embed → store (text + vector)
                                                          │
                                                          ▼
                                                    [ vector store ]
                                                          │
  ── RETRIEVAL (online, run per question) ─────────────────┼─────────────
  User question → embed → search for nearest vectors ─────┘
                → return the text of the best-matching chunks
```

The split matters because the two halves have completely different
performance profiles. Ingestion is slow, heavy, and run rarely (in this project:
a full corpus pass takes about 41 minutes). Retrieval is light, fast, and runs
on every single user question. A good design keeps the expensive work on the
left side of that diagram.

## 1.5 Phase 1 — Ingestion, step by step

### (a) Extraction

Get plain text out of whatever the source format is — PDF, HTML, DOCX, a
database column, a CSV. This is format-specific and boring, and it is
deliberately kept away from everything downstream: by the time text reaches the
chunker, nobody cares that it used to be a PDF.

### (b) Chunking

Split each document into pieces. This is more consequential than it looks, and
§1.6 is entirely about it.

### (c) Embedding

Run every chunk through the embedding model to get its vector. Models process
**batches** far more efficiently than one string at a time, because the matrix
math underneath parallelizes across the batch — CPU or GPU alike.

### (d) Storing

Write each chunk into the vector store as a row: the original text, its vector,
its position within the source document, and whatever metadata is useful. The
text is stored alongside the vector on purpose — you need the text back at
retrieval time to show it (or feed it to an LLM).

## 1.6 Why chunking is a real design decision

You cannot embed a whole 50-page document as one vector. Three reasons:

1. **The model has a token limit.** Every embedding model accepts at most a
   certain number of **tokens** (word-pieces — roughly ¾ of a word in English).
   Beyond that, it truncates or errors. This project's model accepts 8192.
2. **One vector per document averages away the meaning.** A 50-page contract
   compressed into a single vector represents "contract-ness", not the one
   clause you asked about. Retrieval precision collapses.
3. **The prompt has a budget.** Retrieved text eventually goes into an LLM
   prompt. Retrieving 50 pages when you needed one paragraph wastes the entire
   context window.

So: cut documents into chunks of a few hundred tokens, embed each separately,
and retrieve at chunk granularity. Two refinements make this work well:

- **Overlap.** If chunks are cut back-to-back with no overlap, an idea that
  straddles a boundary gets split in half and is retrievable from neither side.
  The standard fix is a sliding window: each chunk starts `chunk_size −
  overlap` tokens after the previous one, so consecutive chunks share their
  boundary text. This project uses 20% overlap (100/20 for social posts,
  250/50 for documents).
- **Measure in tokens, not characters.** A character count doesn't tell you
  anything precise about a model's token limit — the ratio varies wildly by
  language (about 4 characters per token in English, about 1.4 for Bengali text
  in an English-trained tokenizer). Measuring in the model's own tokens is the
  only way chunk sizes mean something.

## 1.7 Phase 2 — Retrieval, step by step

1. **Embed the question** using the *same* model that embedded the chunks. This
   is non-negotiable: two different models produce vectors in unrelated spaces,
   and comparing them is meaningless.
2. **Search** the vector store for the nearest stored vectors — an "approximate
   nearest neighbour" query. The store returns candidates ordered by similarity.
3. **Take the top *k*** (typically 5–20) and return their stored text.

### Dense vs keyword search

Vector search is called **dense** retrieval: it compares meaning, and it's
excellent at paraphrases and synonyms. But it has two well-known blind spots:

- **Exact identifiers.** Searching for part number `A7636` or an error code
  `ERR_4021` — these have no meaningful semantics for a model to key on.
- **Mushy short queries.** A vague query can produce similarity scores where
  the relevant and irrelevant results are separated by almost nothing.

The complement is **keyword** (lexical) search — the classic term-matching
search that Postgres, Lucene, and every search engine have offered for decades.
It fails at paraphrase ("forgot my login" won't match "password reset") but is
unbeatable at exact tokens.

Modern systems run **both** and merge the results. That merged approach is
called **hybrid search**, and this system implements it (§3.5). The standard
way to merge two ranked lists is **Reciprocal Rank Fusion (RRF)**: each list
contributes `1 / (k + rank)` to each document it contains, and documents that
*both* lists ranked well win. The constant `k` (60 in the original paper and
here) softens the difference between position 1 and 2 so that one list's
absolute top pick can't steamroll a document both lists agree on.

## 1.8 Phase 3 — Generation

The retrieved chunks and the user's question are combined into a prompt that
instructs the model to answer *from the provided context*, and to say it doesn't
know when the context doesn't contain the answer. The output is then often
returned with citations — which chunk of which source document each claim came
from.

This is the "G" in RAG. **In this particular system it is not built yet** —
see §2.6. Everything upstream of it is.

---

# Part 2 — The Dhaka Kacchi RAG system

## 2.1 Where this fits in the wider project

The parent project is an AI harness for a small Bangladeshi restaurant (Dhaka
Kacchi). It has a data warehouse (§ `warehouse/`), a git-backed "company brain"
of standard operating procedures (§ `brain/`), and agent tooling.

Notably, the parent project's architecture document **deliberately rejected** a
vector database for the company brain, with this reasoning: *"at this scale the
relevant slice fits in context... diffs plus PR review are worth more than
semantic retrieval."* That decision stands. This RAG subsystem does **not**
replace it. It is an *additional* retrieval capability that agents can query —
built because the capstone requires demonstrating RAG specifically.

Knowing this matters for reading the design: several choices below (small
corpus, local-only embeddings, no vector index) are correct *because* the
corpus is small and the brain's core knowledge still lives in git. They would be
different choices at ten million documents.

## 2.2 The two corpora

The system is designed around two very different sources, which is useful
because it exercises two entirely different content shapes.

| | **Corpus A: social_share** | **Corpus B: Knowledge_Base** |
|---|---|---|
| Source | A CSV export of social post captions + engagement metrics from a sibling ordering/social backend | A local folder of real documents |
| Content | 428 posts, mostly short marketing captions, mixed English and Bengali | 47 files: 5 arXiv papers (PDF), 41 SEC EDGAR contract exhibits (HTML), 1 plain text file |
| Rows in the store | 583 chunks | 2,721 chunks (2,063 html + 657 pdf + 1 txt) |
| Chunk size / overlap | 100 / 20 tokens | 250 / 50 tokens |
| Identity (`source_path`) | `social_post_metrics:<row-uuid>` | `documents/<relative-path>` — and for PDFs, `documents/<file>.pdf::page<N>` |
| Extra metadata | Full row snapshot: platform, posted_at, permalink, likes, reach, saves, clicks… | `source_url` (from a manifest), `page_number` for PDFs |

**Total: 3,304 chunks** in the live store.

Two details worth noticing in that table, because they're the kind of thing that
looks trivial and isn't:

- **PDFs are chunked per page, not per file.** The pipeline's rule is that one
  call to the ingestion function attaches one metadata dict to every chunk it
  produces. A 40-page PDF's page number genuinely varies per chunk, so the
  loader calls ingestion *once per page*, with `::page<N>` appended to the
  source path. The accepted trade-off: a sentence split across a page boundary
  becomes two unrelated mini-documents.
- **`urls.txt` in the corpus is a manifest, not content.** It's a list of the 41
  source URLs, positionally matching `doc_001.htm` … `doc_041.htm`. The loader
  reads it to attach a real `source_url` to each HTML chunk, and deliberately
  skips ingesting the file itself.

## 2.3 The design decisions that matter

These were made deliberately, most of them with alternatives considered and
rejected. They're the most transferable part of this document.

### Decision 1 — pgvector, not a dedicated vector database

The obvious first instinct is a purpose-built vector DB (Pinecone, Weaviate,
Qdrant, Chroma). This project chose **pgvector**, a Postgres extension that adds
a `vector` column type and similarity operators to ordinary Postgres.

*Why:* the project already runs Postgres, and its own architecture states the
principle that *"the business does not generate enough data to justify the
operational surface, and a solo operator cannot maintain it."* A second
specialized service means a second thing to back up, secure, monitor, and
upgrade. At 3,304 chunks, Postgres with a sequential scan is imperceptibly
slower than a dedicated vector DB and vastly simpler to run.

A counter-argument was raised and rejected: *"we'll have PDFs and documents in
the corpus, so we need a document database."* Source format is handled entirely
by the extraction step, which is upstream of storage — by the time content
reaches the store, it's `(vector, text, metadata)` no matter where it came from.
**Vector storage choice and source-format handling are decoupled.**

### Decision 2 — a separate *database*, not a schema in the existing one

The RAG store is its own Postgres database (`dhaka_kacchi_rag`) on the same
server as the business warehouse — not a schema inside it.

*Why:* agents query the RAG store, and agents must never be able to touch
warehouse tables even by accident. In Postgres you cannot `JOIN` across two
databases, so this isolation is **structural** rather than merely
permission-based: there is no query an agent can write that reaches warehouse
data through this connection. The cost is real and accepted — RAG chunks cannot
be joined against warehouse tables like `menu_item` in a single query. Agents
never needed that.

### Decision 3 — three database roles, least privilege by construction

The system does not have one database credential. It has three, each with
exactly the rights its job needs:

| Role | Used by | Privileges on `chunks` |
|---|---|---|
| `postgres` (admin, via `RAG_ADMIN_DATABASE_URL`) | One-time setup only | Superuser. Never used at runtime by ingestion or agent code. |
| `rag_writer` | The recurring ingestion job | `SELECT, INSERT, UPDATE, DELETE` on `chunks`. No DDL at all, no other table. |
| `rag_reader` | Agent/query code | `SELECT` only. Structurally incapable of modifying a row. |

Two reasons for the split, both load-bearing:

1. **Blast radius.** A compromised process only ever holds the one credential
   it was loaded for.
2. **Misuse by convenience.** If one settings object held all three
   credentials, the admin credential would sit in memory inside agent-facing
   code paths that should never touch it — one typo away from being used.

This is the same pattern the parent project uses for its ordering-database
reader role, so it's established practice rather than a RAG-specific invention.

### Decision 4 — local embeddings, not an API

Every chunk and every query must be embedded. The two options are a hosted API
(OpenAI, Cohere) or a model running locally.

*Why local:* API embeddings make every chunk and every query a network call, and
send data off-infrastructure. The project's architecture already commits to
pseudonymizing anything that leaves the infra. A local model sidesteps that
question permanently — and if the corpus ever contains something
PII-adjacent (review text tied to a customer), there is nothing to reconsider.

The trade-off is honest: slightly lower raw quality than the best hosted
models, plus you have to manage a model file (here: 4.3 GB of disk, ~1.9 GB of
RAM while embedding — see Appendix B).

### Decision 5 — the embedding model is `BAAI/bge-m3` (1024 dimensions)

This was **changed mid-project, and the reason is instructive.**

The first choice was `all-MiniLM-L6-v2` — the standard "known good default" for
local RAG: 384 dimensions, fast on CPU. Then the real corpus arrived, containing
Bengali-language captions. Running the longest real caption (1,446 characters,
mostly Bengali) through it produced **1,013 tokens** — about 1.4 characters per
token, against roughly 4 for English, because the model's tokenizer was trained
almost entirely on English and shattered Bengali words into fragments. It also
blew past the model's own 512-token limit.

`BAAI/bge-m3` fixed all of it: the same caption became **470 tokens** (about 3.1
characters per token), producing 12 chunks instead of 26. It has a 1024-dimension
output and an 8192-token limit, and it requires no special query prefix (unlike
the `multilingual-e5` alternative that was considered).

**The generalizable lesson:** the "best default" embedding model is only the
best default *for English*. The moment a corpus contains another language, token
counts, chunk sizing, and quality all change, and you find out by measuring real
data, not by reading about it.

Changing the model changed the vector width, which changed the database column
from `vector(384)` to `vector(1024)`. Because no real data existed yet, the
table was dropped and recreated rather than migrated.

### Decision 6 — chunk sizes tuned against measured data, not rules of thumb

Both corpora's chunk settings were set by **tokenizing the actual corpus** and
looking at the distribution.

- *Social posts:* the initial 50-token estimate was based on a characters-per-token
  rule of thumb. Real measurement gave median 38, average 70.7, p75 103, p90 173,
  max 470. At 50, **43% of captions would have been split unnecessarily**,
  fragmenting ideas that fit comfortably in one piece. Chosen: **100** (keeps the
  median and p75 whole) with 20-token overlap.
- *Knowledge_Base:* median 1079 tokens per page/document — academic papers and
  legal exhibits run far denser and longer. Chosen: **250 / 50**, four times
  bigger than the social setting, and still splitting a typical document into
  several focused chunks.

### Decision 7 — hybrid retrieval with reciprocal rank fusion

Added after a real, reproducible failure. The query *"what is transformers?"*
returned, as its top hit, a chunk of an SEC contract — the document was a
license agreement with a company called **"Transformair"**. Worse, the whole top
band sat inside a ~0.01 similarity range (0.4800 vs 0.4890): the relevant
Transformer-paper abstract and the irrelevant contract chunk were barely
distinguishable by dense similarity alone.

Before changing anything, two hypotheses were ruled out with evidence: storage
corruption (a stored vector vs. a freshly computed one: cosine 1.000000, both
unit norm) and missing query prefixes (BGE's documented instruction prefix made
the control query *worse*: 0.6576 → 0.5983).

The actual fix was to stop relying on dense retrieval alone — see §3.5. After
the fix, the same query returns 8 of 8 relevant paper chunks, the contract
chunk is gone from the top-8 entirely, known-good control queries are unchanged,
and — as a bonus — an exact-code query like `A7636` now finds the exact contract
containing it, which pure embeddings could never do.

### Decision 8 — metadata is snapshotted into each chunk, not joined later

Every chunk carries a `metadata` JSONB column holding a snapshot of the source's
other columns. For social posts: platform, posted_at, permalink, impressions,
reach, likes, comments, shares, saves, clicks.

*Why:* one retrieval call then returns everything an agent needs about a matched
chunk — no second lookup, no join. This mirrors an existing rule in the
warehouse ("point-in-time costs": a line item stores the cost *as of the order*,
and never joins live to the current ingredient price). The accepted consequence
is that engagement numbers drift stale between ingestion runs; a re-index
refreshes them.

### Decision 9 — a plain SQL script for schema, not a migration framework

The warehouse uses Alembic migrations extensively. This subsystem has exactly
one table and no schema history to manage, so it uses a plain idempotent Python
script. Revisit if the schema grows enough tables to need real migration
history.

### Decision 10 — no vector index yet (a deliberate deferral)

pgvector supports approximate-nearest-neighbour indexes (IVFFlat, HNSW). This
system does not create one — every similarity query is a sequential scan.

*Why:* at 3,304 rows a sequential scan completes in milliseconds, and an
approximate index trades **exact** results for speed you don't need yet. The
time to add an HNSW index is when query latency becomes visible — a real,
deliberate deferral, not an oversight.

## 2.4 Component map

```
┌──────────────────────────── YOUR MACHINE ──────────────────────────────┐
│                                                                        │
│   ┌── rag/ (Python, run with `uv`) ─────────────────────────────────┐  │
│   │                                                                │  │
│   │  config.py ──────────────► the ONE place that reads .env       │  │
│   │     │                         (3 settings loaders, one per role)│  │
│   │     │                                                          │  │
│   │  bootstrap_db.py   ── one-time: DB + extension + roles         │  │
│   │  schema.py         ── one-time: chunks table + GRANTs          │  │
│   │                                                                │  │
│   │  ── INGESTION ──                                               │  │
│   │  load_social_share.py    ─┐                                    │  │
│   │  load_knowledge_base.py  ─┼─► chunking.py ─► embedding.py      │  │
│   │                           │        │              │            │  │
│   │                           │        └── token-level│            │  │
│   │                           │           sliding     │            │  │
│   │                           │           window      ▼            │  │
│   │                           │                  ingest.py         │  │
│   │                           │                (chunk+embed+upsert)│  │
│   │                           │                       │            │  │
│   │  reindex.py ──────────────┘                       │            │  │
│   │   (orchestrates load+prune for both corpora)      │            │  │
│   │                                                   │            │  │
│   │  ── RETRIEVAL ──                                  │            │  │
│   │  retrieval.py  ◄──────────────────────────────────┼────────────┤  │
│   │   (embed query → dense + keyword → RRF fusion)    │            │  │
│   │        ▲                                          │            │  │
│   │  webui.py (FastAPI, port 8010)                    │            │  │
│   │   /  /api/search  /api/file  /api/reindex         │            │  │
│   └───────────────────────────────────────────────────┼────────────┘  │
│                                                       │               │
│   ┌── Docker container: dhaka-kacchi-rag ─────────────┼────────────┐  │
│   │   image: pgvector/pgvector:pg16                   ▼            │  │
│   │   host port 5434 ──► container port 5432          │            │  │
│   │                                                   │            │  │
│   │   database dhaka_kacchi_rag                       │            │  │
│   │     └── table chunks (id, source_type, source_path,             │  │
│   │         chunk_index, chunk_text, embedding vector(1024),        │  │
│   │         metadata jsonb, created_at, updated_at)                 │  │
│   │                                                                 │  │
│   │   roles: postgres (admin) · rag_writer · rag_reader             │  │
│   └─────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

## 2.5 The data model

One table holds everything.

```sql
CREATE TABLE chunks (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type  text NOT NULL,        -- pdf | markdown | docx | txt | csv | xlsx | html
    source_path  text NOT NULL,        -- where the original lives (see below)
    chunk_index  integer NOT NULL,     -- this chunk's position within that source (0,1,2…)
    chunk_text   text NOT NULL,        -- the retrievable text itself
    embedding    vector(1024) NOT NULL,-- bge-m3's 1024-dim representation of chunk_text
    metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_chunks_source_path_chunk_index UNIQUE (source_path, chunk_index)
);
```

Column-by-column reasoning:

- **`id`** — a UUID primary key, because chunks are parallel-inserted and a UUID
  needs no coordination to generate. `gen_random_uuid()` is native to Postgres 13+.
- **`source_path` + `chunk_index` together are the identity of a chunk.** This
  pair is the `UNIQUE` constraint, and it is *the* mechanism that makes the whole
  pipeline idempotent: re-ingesting a source produces the same
  `(source_path, chunk_index)` keys, which hit `ON CONFLICT` and become updates
  rather than duplicates. The same shape the warehouse uses for orders
  (`UNIQUE (channel, external_id)`).
- **`chunk_text` is stored, not just the vector.** Retrieval returns this text;
  without it you'd have a matching vector and nothing to show for it.
- **`metadata` is `jsonb`** — flexible shape, because a PDF chunk has a page
  number and a social post chunk has a permalink. Same pattern the warehouse
  uses for ingredient price history.
- **`created_at` vs `updated_at`** — `created_at` is written once and never
  touched again; `updated_at` is overwritten on every re-ingest. The upsert's
  update list deliberately excludes `created_at`, so the original insertion time
  survives.
- **`source_type` is `text` + a `CHECK` constraint, never a Postgres `ENUM`.**
  This is a house rule in the parent project and the reason is concrete: an
  ENUM's values cannot be dropped once used, and the world's source formats are
  not fixed. It was vindicated in practice — `'html'` was added to the allowed
  list after real HTML documents arrived, via a drop-then-add `ALTER TABLE`
  that left the 583 existing rows untouched. An ENUM would have needed a
  migration.

### `source_path` conventions

| Source | `source_path` looks like | Why |
|---|---|---|
| Social post | `social_post_metrics:550e8400-e29b-41d4-a716-446655440000` | The post's own UUID — unique, and traceable back to the exact source row |
| HTML/TXT/MD file | `documents/doc_007.htm` | Path relative to `Knowledge_Base/`, always with forward slashes |
| PDF page | `documents/arxiv_1706.03762.pdf::page3` | One path per *page*, since page number varies per chunk |

The forward-slash rule is not cosmetic. Building these paths with `str()` on
Windows yields backslashes (`documents\file.pdf`), and when such a string is
embedded in the web UI's JavaScript (`onclick="openDoc('...')"`), the browser's
JS engine treats `\f` as an escape sequence and silently drops the backslash —
corrupting the path before it ever reaches the server. The fix is `.as_posix()`.
This caused a real bug and a full re-ingestion of 2,721 chunks (see §3.1).

## 2.6 What is deliberately not built yet

Three things, each a planned step rather than an oversight:

1. **Generation.** No LLM call exists anywhere in the system. The web UI is
   explicitly a *pure-retrieval* search interface — you get ranked chunks, not
   written answers. This was a requirement, not a shortcut: it let the retrieval
   quality be judged directly, without an LLM's fluent prose hiding whether the
   right chunks were actually found.
2. **An agent-facing interface.** `retrieve()` is designed as the one shared,
   reusable function. Whether agents call it through the HTTP endpoint or
   through a dedicated MCP server is a decision deferred to when that's built.
3. **Evaluation.** No golden set, no measured retrieval quality, no grounding
   metric. Ranking is verified by inspection on real queries (and by the
   before/after evidence in §2.3, Decision 7), not scored.

---

# Part 3 — Phase by phase

Each phase below gives: what it does, how it's implemented, the diagram, and the
real lessons learned building it.

## Phase 0 — Bootstrap: database, extension, roles

**Purpose:** create everything the pipeline needs, exactly once, safely
re-runnable.

**Implemented in:** `bootstrap_db.py`, then `schema.py`.

```mermaid
flowchart LR
    A["RAG_ADMIN_DATABASE_URL<br/>(connects to the 'postgres'<br/>maintenance database)"] --> B["CREATE DATABASE<br/>dhaka_kacchi_rag"]
    B --> C["CREATE EXTENSION vector<br/>(inside the new database)"]
    C --> D["CREATE ROLE rag_writer<br/>+ random password, printed once"]
    D --> E["CREATE ROLE rag_reader<br/>+ random password, printed once"]
    E --> F["CREATE TABLE chunks<br/>+ UNIQUE + CHECK constraints"]
    F --> G["GRANT CONNECT +<br/>SELECT/INSERT/UPDATE/DELETE to rag_writer<br/>SELECT only to rag_reader"]
```

**Why it connects to the `postgres` maintenance database first:** on the very
first run, `dhaka_kacchi_rag` does not exist yet, so there is nothing else to
connect to. `CREATE DATABASE` cannot run inside a transaction, so this step uses
autocommit — as does `CREATE EXTENSION` — while `CREATE TABLE` and `GRANT` run
in normal transactions.

**The two-role problem, and its real bug.** The roles are created with
`LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE`. An early version created them with
*no password at all*, which makes a `LOGIN` role impossible to actually log in
as — caught before the first real run. The fix generates a random password
(`secrets.token_urlsafe(24)`), prints it **once**, and never resets it on
re-runs. Postgres stores only the hash of a password, so if that printout is
lost, the only recovery is `ALTER ROLE ... PASSWORD`, not retrieval.

**The second real bug, caught on the first live run:**
`CREATE ROLE ... PASSWORD :pwd` — a bind parameter — failed with
`syntax error at or near "$1"`. Postgres's grammar for that one clause accepts
only a literal string, even though bind parameters work for essentially every
other value in the same file. Fixed by quoting the literal directly, with proper
escaping, instead of binding it. Because every step is idempotent, the re-run
skipped straight past the two steps that had already succeeded.

**Why `CREATE TABLE IF NOT EXISTS` alone is not enough.** It is
*existence*-idempotent, not *definition*-convergent: if the table already exists
with a **wrong shape**, it is silently skipped. So the `CHECK` constraint on
`source_type` is applied as a separate, always-run drop-then-add step — which is
exactly how `'html'` was added later without disturbing existing rows.

## Phase 1 — Extraction: source text in, plain text out

**Purpose:** get plain text out of each source format, along with the identity
and metadata that will travel with every chunk from that source.

```mermaid
flowchart TD
    subgraph A["Corpus A — social_share"]
        A1["rag/data/social_post_metrics.csv<br/>428 rows"] --> A2["skip rows with<br/>[NULL]/empty caption"]
        A2 --> A3["row → text = the caption<br/>metadata = platform, posted_at,<br/>permalink, likes, reach, ...<br/>source_path = 'social_post_metrics:{id}'"]
    end
    subgraph B["Corpus B — Knowledge_Base/documents/"]
        B1[".pdf"] --> B2["pypdf extracts text<br/>PER PAGE<br/>source_path = '...pdf::pageN'<br/>metadata.page_number = N"]
        B3[".htm"] --> B4["BeautifulSoup strips tags<br/>→ visible text<br/>metadata.source_url from urls.txt"]
        B5[".txt / .md"] --> B6["read as-is"]
        B7[".docx / .xlsx"] --> B8["raises NotImplementedError<br/>(loudly, not silently)"]
    end
```

**Implemented in:** `load_social_share.py` and `load_knowledge_base.py`.

Both loaders are written to the same shape: walk the source, extract text,
build the metadata snapshot, and call `ingest_source()` once per
"document" — where a PDF *page* counts as a document.

Three details worth carrying to any similar system:

- **Unsupported formats fail loudly.** A `.docx` dropped into the folder raises
  `NotImplementedError` naming the file, rather than being silently skipped. The
  principle in this codebase is to never let untested things appear to work.
- **A manifest file is not content.** `urls.txt` is detected and skipped, and
  used only to enrich the HTML chunks with real source URLs.
- **Enumeration is flat, not recursive.** `Knowledge_Base/documents/` is read
  with a single `iterdir()` — files must sit directly in that folder.

## Phase 2 — Chunking: text in, chunk dicts out

**Purpose:** split text into overlapping, token-measured windows that never cut
a word in half.

```mermaid
flowchart TD
    A["raw text"] --> B["tokenize ONCE with the model's<br/>own tokenizer,<br/>keeping character offsets<br/>for every token"]
    B --> C["slide a window of<br/>chunk_size_tokens<br/>forward by chunk_size − overlap"]
    C --> D["snap each window edge OUTWARD<br/>to a real word boundary"]
    D --> E["slice the ORIGINAL text by<br/>character offsets<br/>(never reassemble from tokens)"]
    E --> F["{chunk_text, source_path,<br/>chunk_index, source_type}"]
    F --> G{"window reached<br/>end of text?"}
    G -->|no| C
    G -->|yes| H["list of chunk dicts"]
```

**Implemented in:** `chunking.py`. Signature:

```python
chunk_text(text, *, source_path, source_type,
           chunk_size_tokens, overlap_tokens) -> list[dict]
```

The output dicts are shaped to match the `chunks` table columns directly.

**Why `source_path`/`source_type` are parameters, not inferred here.** An early
design had the chunker decide the source type by inspecting a file extension.
That breaks the moment the source isn't a file — a database row has no
extension. The *caller* (the format-specific loader) already knows what it's
parsing, so it supplies this, and the chunker stays generic.

**The word-boundary snap, and a subtle real bug.** Raw token windows cut words
in half (a chunk ending in `'basmat'`), because tokenizers split unfamiliar
words into sub-word pieces. The fix widens each window outward so it never lands
mid-word. But *how* you detect "mid-word" is tokenizer-specific, and there are
two opposite conventions in the wild:

- **WordPiece** (BERT-style, incl. the retired `all-MiniLM-L6-v2`): continuations
  carry a `##` prefix — `['international', '##ization']`.
- **SentencePiece/XLM-R** (`bge-m3`, the current model): the *start* of a word
  carries a `▁` (U+2581) marker, and continuations carry nothing —
  `['▁international', 'ization']`.

The original guard checked only for `##`. So when the model was switched to
`bge-m3`, the guard silently stopped matching anything and windows could cut
words again — the exact bug class it existed to prevent. The fix probes the
tokenizer once at import time ("does it mark word *starts* or continuations?")
and handles both conventions. **Lesson: anything that encodes assumptions about
a specific model's tokenizer breaks quietly when the model changes.**

## Phase 3 — Embedding: text in, 1024 floats out

**Purpose:** turn text into vectors, once for chunks (ingestion) and once per
query (retrieval), using the same model both times.

**Implemented in:** `embedding.py`. Two functions:

```python
embed_texts(texts: list[str]) -> list[list[float]]   # the core, generic one
embed_chunks(chunks: list[dict]) -> list[dict]        # ingestion-time wrapper
```

```mermaid
flowchart LR
    A["list of plain strings<br/>(chunk texts, OR one query)"] --> B["bge-m3<br/>SentenceTransformer.encode()"]
    B --> C["NumPy array,<br/>shape (n, 1024)"]
    C --> D["normalize_embeddings=True<br/>→ every vector has length 1"]
    D --> E[".tolist() →<br/>list[list[float]]"]
```

Design points:

- **`embed_texts` takes a batch, not one string.** Batching is a real
  performance win (matrix operations parallelize across the batch), not a style
  preference.
- **The core function is generic on purpose.** The same function embeds chunk
  text at ingestion and the raw query string at retrieval — there is no second
  "embed a string" implementation to drift out of sync.
- **`normalize_embeddings=True` is what makes the retrieval SQL cheap.** Scaling
  every vector to unit length means `cosine_similarity(a, b) = a · b` — the
  division in the cosine formula disappears. The retrieval layer exploits this
  directly by using an inner-product operator instead of a cosine operator
  (§3.5). The two decisions are coupled: change one, revisit the other.
- **The model is loaded once at import time**, not per call — loading weights
  from disk is slow, and doing it once per process is what makes batching pay off.

## Phase 4 — Storage: chunk dicts in, upserted rows out

**Purpose:** write chunks (text + vector + metadata) into `chunks`, idempotently.

**Implemented in:** `ingest.py` (`ingest_source()`) and `upsert.py`
(`upsert_returning()`).

```mermaid
flowchart TD
    A["ingest_source(text, source_path, source_type,<br/>chunk_size, overlap, metadata, settings)"] --> B["chunk_text() → list of chunk dicts"]
    B --> C["embed_chunks() → each dict gains<br/>an 'embedding' key, one batched call"]
    C --> D["shape rows for the table<br/>+ wrap metadata in Jsonb(...)"]
    D --> E["ONE transaction:<br/>INSERT ... ON CONFLICT<br/>(source_path, chunk_index)<br/>DO UPDATE SET<br/>chunk_text, embedding, metadata, updated_at"]
    E --> F["returns the number of rows written"]
```

**The idempotency contract.** Re-ingesting an unchanged source is a harmless
no-op write; changed content refreshes exactly four columns
(`chunk_text`, `embedding`, `metadata`, `updated_at`); `created_at`,
`source_type`, `source_path` and `chunk_index` are never touched by an update.
Nothing ever duplicates, because the `UNIQUE (source_path, chunk_index)`
constraint makes the second write an update.

This is what makes the whole system **resumable**: a crash mid-ingestion loses
at most the current source, and the recovery procedure is simply "run it again."
That was verified for real when a 2,721-chunk run died at 1,995 and an unmodified
re-run picked up cleanly.

**Two plumbing details that are easy to get wrong:**

1. **`register_vector` must run per connection.** psycopg has no built-in idea
   what a Postgres `vector` type is, so a plain Python `list[float]` fails to
   adapt. `pgvector` provides `register_vector(connection)`, which is wired to
   SQLAlchemy's `"connect"` event so every new low-level connection gets taught
   the type. It can't be done globally, once.
2. **`Jsonb(...)` wrapping.** Without it, psycopg may serialize a Python dict as
   plain `json` rather than `jsonb`, which won't match the column.

**A bug worth remembering** (it generalizes beyond RAG): the upsert helper's
default `returning=("id",)` needs the lightweight table reference to have an
`id` column *declared* — even though nothing ever writes to it. A
`sa.table()` object only knows the columns explicitly listed, so it raised a
plain `KeyError`. **A lightweight table reference needs every column any caller
might reference, not just the ones being written.**

## Phase 5 — Retrieval: query in, ranked chunks out

**Purpose:** the heart of the system. Turn a question into the best `k` chunks.

**Implemented in:** `retrieval.py` (`retrieve()`), plus
`get_chunk_neighbors()`.

```mermaid
flowchart TD
    Q["user query"] --> E1["embed_texts([query])[0]<br/>→ one 1024-dim vector"]
    E1 --> D["DENSE search<br/>SELECT ... ORDER BY<br/>embedding &lt;#&gt; CAST(:qv AS vector)<br/>LIMIT 50"]
    Q --> K["KEYWORD search<br/>ts_rank_cd(to_tsvector('english', chunk_text),<br/>plainto_tsquery('english', query))<br/>LIMIT 50"]
    D --> R["Reciprocal Rank Fusion<br/>score = Σ 1/(60 + rank)<br/>over both lists"]
    K --> R
    R --> T["trim to top_k<br/>→ list of dicts with<br/>text, source, metadata, distance"]
```

### The dense half

```sql
SELECT id, source_type, source_path, chunk_index, chunk_text, metadata,
       embedding <#> CAST(:query_vector AS vector) AS distance
FROM chunks
ORDER BY embedding <#> CAST(:query_vector AS vector) ASC
LIMIT 50
```

Three things in that SQL are load-bearing:

- **`<#>` is pgvector's negative-inner-product operator**, chosen over `<=>`
  (cosine distance) and `<->` (L2). Because every vector is already unit length
  (§3.3), inner product *is* cosine similarity — `<=>` would waste effort
  recomputing a normalization that is already true by construction. `<#>`
  returns the *negative* inner product, so `ORDER BY ... ASC` puts the most
  similar chunk first.
- **The explicit `CAST(... AS vector)` is required.** Unlike an `INSERT`, where
  Postgres knows the target column's type and can coerce an array, a bare
  `ORDER BY embedding <#> :param` gives Postgres nothing to infer the
  parameter's type from — psycopg sends a plain Python list as
  `double precision[]`, and `<#>` has no overload for that pairing. The original
  version failed with exactly that error.
- **It says `CAST(...)`, not `::vector`,** for a specific reason: SQLAlchemy's
  `text()` treats `:name` as a bind parameter, and the second colon in
  `:query_vector::vector` confuses its parser — leaving the parameter
  unsubstituted and producing a syntax error. `CAST(x AS vector)` avoids the
  ambiguity entirely. (Fixing the first bug introduced the second; both were
  caught by actually running the query.)

Note that the dense search fetches **50 candidates** even when the caller asked
for 5. Each half of the hybrid needs a deeper pool than the final answer,
because the whole point of fusion is to see both halves' opinions before
deciding.

### The keyword half

```sql
SELECT ..., ts_rank_cd(to_tsvector('english', chunk_text),
                       plainto_tsquery('english', :query)) AS keyword_rank
FROM chunks
WHERE to_tsvector('english', chunk_text) @@ plainto_tsquery('english', :query)
ORDER BY keyword_rank DESC
LIMIT 50
```

- **Postgres's own full-text search** — no extra service, no Elasticsearch.
- **The `'english'` configuration stems and drops stopwords**: a query for
  "transformers" also matches the literal word "Transformer" in the papers (both
  stem to `transform`), and filler words like "what" and "is" are dropped, so a
  question-shaped query reduces to its actual terms.
- **Bengali passes through as whole-word tokens**, which still matches exact
  Bengali words in the social corpus.
- **Degrades gracefully**: a query whose terms appear nowhere (or which reduces
  to nothing but stopwords) simply returns zero rows, and the fusion then
  returns the dense ranking unchanged.

### The fusion

Reciprocal Rank Fusion, with `k = 60`:

```
for each list (dense, keyword):
    for rank, row in enumerate(list, start=1):
        score[row.id] += 1 / (60 + rank)

return rows sorted by score, descending, truncated to top_k
```

A chunk both halves rank well beats a chunk only one half loves. In the
transformers example, the "Transformair" contract chunk leads the dense list but
is **absent** from the keyword list (nothing in it matches `transform`), so it
collects only one contribution and falls below chunks the dense side ranked
slightly lower but that the keyword side also found.

The `k=60` constant matters: without it, ranks 1 and 2 would differ by a factor
of 2, letting one list's top pick dominate a chunk both lists agree on. With it,
`1/61` vs `1/62` is a gentle difference. This is a ranking device, not a
calibrated confidence score.

**Two honest nuances:**

- The fused order can deviate from a result's displayed similarity score. That
  deviation is exactly what the keyword contribution buys; the UI hides scores
  by default for this reason.
- Ordering *within* the relevant set is still imperfect — a results-table chunk
  can outrank the abstract for "what is transformers?". A reranker or RRF weight
  tuning is the principled next step, deferred until an evaluation set exists.

### Neighbour context

`get_chunk_neighbors(source_path, chunk_index)` returns the text of
`chunk_index − 1` and `chunk_index + 1` from the same source. This gives the UI
"a few words before/after" a match **with no extra ingestion-time storage** —
the neighbouring text is already sitting in the table as its own row, and
chunking's overlap already made adjacent chunks share boundary content.

### Read-only by construction

`retrieve()` connects with `rag_reader` and issues only `SELECT`s. It is not
*policy* that stops it writing — the role has no write grants at all.

## Phase 6 — Presentation: the web UI

**Implemented in:** `webui.py` — FastAPI + uvicorn, pure retrieval, **no LLM
call anywhere in the file.**

| Route | Purpose |
|---|---|
| `GET /` | The single-page search UI (inline HTML + JS) |
| `GET /api/search?q=...&top_k=...` | Wraps `retrieve()`; `top_k` validated server-side (5–20) as well as in the dropdown |
| `GET /api/file?path=...` | Serves a source document for the side-panel viewer |
| `POST /api/reindex` | Starts a full re-index in a background thread |
| `GET /api/reindex/status` | Returns the current re-index state |

Design points:

- **`_build_reference()` builds a human-traceable reference per result.** A
  social post gets a clickable permalink + platform + date; a file chunk gets
  its relative path; a PDF chunk gets an inline "open document" that jumps to
  the right page. It's written generically so a future source type degrades to
  its bare path instead of crashing.
- **Security-critical path traversal guard.** `/api/file` resolves the requested
  path and checks `is_relative_to(KNOWLEDGE_BASE_DIR)` *before* touching the
  filesystem. A `../../` escape resolves outside the allowed directory and is
  rejected with a 400 — it is never opened.
- **The similarity score is displayed as `-distance`.** Since `distance` is a
  *negative* inner product, every real result shows a negative number, which
  looks like a bug. This is a display-only flip; the SQL and ranking are
  untouched.
- **Results are compact by default** — the matched chunk text, clamped to about
  four lines — with a per-result "show more" that reveals neighbour context, the
  reference, and the score. Search results are for scanning; detail is opt-in.

## Phase 7 — Re-index orchestration

**Purpose:** one action that brings the whole vector store into agreement with
what's on disk.

**Implemented in:** `reindex.py`.

```mermaid
flowchart TD
    A["run_reindex()"] --> B["load_social_share.load_all()<br/>(upsert every post)"]
    B --> C["load_social_share.prune_orphaned()<br/>(delete rows whose source is gone)"]
    C --> D["load_knowledge_base.load_all()"]
    D --> E["load_knowledge_base.prune_orphaned()"]
    E --> F["one stats dict:<br/>per corpus — ingested, skipped,<br/>chunks written, orphans deleted"]
```

**Load before prune, per corpus, and the order is the safety property.** The
load has just rewritten every row that legitimately belongs to a source on disk;
therefore anything the prune deletes afterwards is a genuine leftover.

**The prune is a keep-list comparison.** It builds the set of every
`source_path` the loader *would* write for the folder **as it sits right now**,
then deletes any chunk whose `source_path` is not in that set:

```sql
DELETE FROM chunks
WHERE source_path LIKE 'documents/%'
  AND source_path != ALL(CAST(:expected AS text[]))
```

For PDFs this correctly handles two cases with one test — *the file is gone*
and *the file exists but no longer has page 3* — because both fall out of "would
the loader produce this exact string today?"

Two honest limitations:

- **Sources that shrank are not caught.** An `.htm` edited to half its length
  keeps its source path, so its stale tail chunks survive. Closing this needs
  the loader to report per-source chunk counts to the prune.
- **The keep-list must use the identical path expression the loader writes
  with.** A backslash-mismatched list would match nothing — and the prune would
  then delete *every* chunk of that corpus. (This is the §3.1 bug's second
  appearance.)

**Why the re-index runs in a background thread with polling** rather than one
long HTTP request: a full re-index takes roughly 40 minutes, and with a
long-held request a dropped connection makes a perfectly healthy job look
failed, while a page refresh loses the report entirely. The endpoint starts the
job and returns immediately; the page polls a status endpoint every 5 seconds
and asks once on load, so a refresh mid-run loses nothing. A module-level lock
makes check-then-start indivisible, so a double-click can never start two runs.

**What re-index deliberately does *not* do:** it never reaches out to the live
source database or an SSH tunnel. It reprocesses only what is already on disk.
Automating a live data pull from a locally-run web server would mean embedding a
live credential into that server — a meaningfully bigger security surface for no
real gain.

## Cross-cutting theme: idempotency everywhere

Every write path in this system is safe to run twice. It's the property that
makes the whole thing operable by one person:

| Layer | Mechanism |
|---|---|
| Bootstrap | `CREATE ... IF NOT EXISTS`, duplicate-error codes (`42P04`, `42710`) treated as success |
| Table creation | `CREATE TABLE IF NOT EXISTS`, plus drop-then-add for the CHECK constraint to converge definitions |
| Ingestion | `UNIQUE (source_path, chunk_index)` + `ON CONFLICT DO UPDATE` |
| Prune | Keep-list comparison against current disk state |
| Re-index | Composition of the above — "a partially finished re-index is always safe to simply re-run" |

That last row is why the orchestrator deliberately catches nothing: a loud
failure beats invented partial-failure bookkeeping when recovery is "run it
again."

---

# Part 4 — Manual setup from scratch

This section gets the same system running on a fresh machine. Commands are given
for all three platforms where they differ.

> **Before you start:** the corpora in this repository (social post exports, SEC
> filings, arXiv papers) are the project owner's data and aren't reproduced by
> this guide. Everything else is reproducible, and §4.3 shows how to point the
> pipeline at **your own** documents — the pipeline is corpus-agnostic.

## 4.1 Prerequisites

| Requirement | Why | Check with |
|---|---|---|
| **Python 3.12+** | The code uses modern typing syntax (`list[dict]`, `str | None`) | `python --version` |
| **uv** (Astral) | This project manages dependencies and runs modules with it | `uv --version` |
| **Docker** (Docker Desktop on Windows/macOS) | Runs Postgres + pgvector | `docker info` |
| **~6 GB free disk** | Mostly the 4.3 GB `bge-m3` model download on first use | — |
| **~4 GB free RAM** | Postgres container + the embedding model while ingesting | — |

## 4.2 Step-by-step

### Step 1 — Get the code and install dependencies

```bash
git clone <your-fork-or-copy-of-this-repo>
cd Capstone_Project          # repo root, the folder containing pyproject.toml

uv sync                      # creates .venv and installs everything in pyproject.toml
```

Dependencies that matter for RAG (all in `pyproject.toml`):

```
sqlalchemy, psycopg[binary], pgvector     # database access
sentence-transformers                     # the embedding model (pulls in torch)
python-dotenv                             # reads .env
fastapi, uvicorn[standard]                # the web UI
pypdf                                     # PDF text extraction
beautifulsoup4                            # HTML text extraction
fonttools                                 # cleaner PDF font handling (silences a warning)
```

### Step 2 — Run Postgres with pgvector in Docker

```bash
docker run -d --name dhaka-kacchi-rag \
  -e POSTGRES_PASSWORD=localdevpassword \
  -p 5434:5432 \
  pgvector/pgvector:pg16
```

What each flag does:

- `-d` — detached (runs in the background).
- `--name dhaka-kacchi-rag` — a stable name to refer to the container later.
- `-e POSTGRES_PASSWORD=...` — the superuser password. **Choose your own**; it
  becomes your admin credential in Step 3.
- `-p 5434:5432` — publish host port **5434** to the container's **5432**.
  (5434 rather than 5432 so it can coexist with another Postgres on the same
  machine.)
- `pgvector/pgvector:pg16` — the official image: Postgres 16 with the `vector`
  extension already built and available.

Verify it's up:

```bash
docker exec dhaka-kacchi-rag pg_isready -U postgres
# expect: /var/run/postgresql:5432 - accepting connections
```

Optional but recommended, so the container comes back automatically after a
reboot instead of failing with "connection refused":

```bash
docker update --restart unless-stopped dhaka-kacchi-rag
```

### Step 3 — Create `.env`

Create a file named **`.env`** in the repository root (next to
`pyproject.toml`). It is git-ignored and holds all three connection strings:

```bash
RAG_ADMIN_DATABASE_URL=postgresql://postgres:localdevpassword@localhost:5434/postgres
RAG_WRITER_DATABASE_URL=postgresql://rag_writer:CHANGE_ME@localhost:5434/dhaka_kacchi_rag
RAG_READER_DATABASE_URL=postgresql://rag_reader:CHANGE_ME@localhost:5434/dhaka_kacchi_rag
```

Three things to understand about these:

1. **The admin URL points at the `postgres` *maintenance* database**, not at
   `dhaka_kacchi_rag` — on the first run, the RAG database doesn't exist yet, so
   there is nothing else to connect to. The config loader explicitly allows the
   maintenance database for this one variable and **refuses** it for the other
   two, so a copy-paste mistake fails immediately with a clear message.
2. **The `CHANGE_ME` placeholders get filled in by Step 4's output.** The setup
   script generates a random password for each role and prints it exactly once.
3. **Plain `postgresql://` is fine** — the code sets the `postgresql+psycopg`
   driver itself. The loader also validates the URL's shape at load time (must be
   Postgres, must name a database, must set `sslmode` if the host isn't local)
   and aborts with a readable error rather than failing later inside SQLAlchemy.

### Step 4 — Create the database, extension, roles, table, and grants

```bash
uv run python -m rag.bootstrap_db
uv run python -m rag.schema
```

The first creates the database, enables the `vector` extension inside it, and
creates `rag_writer` / `rag_reader` with random passwords:

```
role 'rag_writer' created with password: <copy this now>
role 'rag_reader' created with password: <copy this now>
```

**Copy both passwords into `.env` immediately.** Postgres stores only a hash —
if you lose the printout, the only recovery is `ALTER ROLE ... PASSWORD`. Re-running
the script on existing roles deliberately does *not* reset them.

The second creates the `chunks` table and applies the grants. Both scripts are
idempotent; run them as often as you like.

### Step 5 — Provide a corpus

Put files in `rag/Knowledge_Base/documents/` — **directly in that folder, not in
subfolders** (enumeration is flat). Supported today: `.pdf` (extracted per
page), `.htm`/`.html`, `.txt`, `.md`. `.docx`/`.xlsx` are declared in the schema
but the loaders raise a clear "not implemented" error for them.

Or, for the database-row path, place a CSV at `rag/data/social_post_metrics.csv`.
See §4.3 for the expected columns.

### Step 6 — Ingest

Either run the two loaders:

```bash
uv run python -m rag.load_social_share      # the CSV corpus
uv run python -m rag.load_knowledge_base    # the documents folder
```

Or run the full orchestrator (both corpora, load + prune, which is what the UI
button calls):

```bash
uv run python -m rag.reindex
```

**First run downloads the embedding model (~4.3 GB) and is slow.** Expect
roughly 40 minutes for a corpus of ~3,000 chunks on a laptop, with the machine's
fans audible. That's the model running locally, by design.

Both prints a summary — files/posts ingested, chunks written, orphans deleted.

### Step 7 — Search

```bash
uv run uvicorn rag.webui:app --port 8010 --host 127.0.0.1
```

Open <http://127.0.0.1:8010> and ask a question. You get ranked chunks with
their sources — **no generated answer**, because generation isn't built
(§2.6).

## 4.3 Using your own corpus

The pipeline is not tied to this project's data. Two paths:

**A) Documents (no code changes needed).** Drop your files into
`rag/Knowledge_Base/documents/` and run
`uv run python -m rag.load_knowledge_base`. Adjust the two constants at the top
of `load_knowledge_base.py` if your documents are shaped differently from
academic papers and legal exhibits:

```python
CHUNK_SIZE_TOKENS = 250
OVERLAP_TOKENS    = 50
```

**How to choose these:** tokenize a sample of your real corpus and look at the
distribution (median, p75, p90, max). Set chunk size so your *typical* document
stays whole in one chunk and only genuinely long ones split. Keep overlap at
about 20% of the chunk size. This is exactly how both settings in this project
were chosen — see §2.3, Decision 6.

**B) Database rows (small code change).** `load_social_share.py` is the template.
It expects a CSV with an `id`, a `caption`, and engagement columns, and it does
three things you'd need to adapt: skip rows with no real caption, build a
metadata snapshot from the row's other columns, and call `ingest_source()` per
row. The core call is:

```python
ingest_source(
    text=caption,
    source_path=f"social_post_metrics:{row['id']}",   # must be unique per source
    source_type="csv",
    chunk_size_tokens=100,
    overlap_tokens=20,
    settings=load_rag_writer_settings(),
    metadata=snapshot_dict,
)
```

**The one rule that matters:** `source_path` must be stable and unique per
source. It is the identity that makes re-ingestion idempotent — an unstable
value (a timestamp, a random id) turns every re-index into a pile of duplicates.

## 4.4 Verification checklist

Run through this after setup, in order. Each step proves a different layer works.

| # | Check | Command | Expected |
|---|---|---|---|
| 1 | Container alive | `docker exec dhaka-kacchi-rag pg_isready -U postgres` | `accepting connections` |
| 2 | Host can reach it | `docker ps` | port mapping shows `0.0.0.0:5434->5432/tcp` |
| 3 | Table exists & has rows | `docker exec dhaka-kacchi-rag psql -U postgres -d dhaka_kacchi_rag -tAc "select count(*) from chunks;"` | your chunk count (3,304 in this project) |
| 4 | Read-only role really is read-only | connect as `rag_reader` and try `INSERT` | permission denied — **this is the expected result** |
| 5 | Retrieval returns something sensible | open the web UI and search a phrase you know is in your corpus | the chunk containing it, near the top |
| 6 | Idempotency | run `uv run python -m rag.reindex` twice | same chunk count both times, second run reports 0 orphans deleted |

Check 4 is the one people skip and shouldn't: the entire agent-safety story
rests on `rag_reader` being unable to write, so prove it.

## 4.5 Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Connection to localhost:5434 refused` | The Docker engine isn't running, or the container is stopped | Start Docker Desktop, then `docker start dhaka-kacchi-rag`. **The port is almost never the real problem.** |
| `docker: cannot connect ... daemon is running` | Docker Desktop/engine is down | Start Docker Desktop and wait for the engine |
| Container exits silently mid-ingestion (`Exited (255)`) | Host memory pressure killing the container while the embedding model is also resident | Just re-run the loader — everything is idempotent and resumes. See Appendix B. |
| `operator does not exist: vector <#> double precision[]` | A query binds a vector without an explicit cast | Use `CAST(:param AS vector)`, not a bare `:param` |
| `syntax error at or near "$1"` in role creation | Bind parameter used in `CREATE ROLE ... PASSWORD` | That clause accepts only a literal (already handled in this codebase) |
| `KeyError: 'id'` from the upsert helper | Lightweight table reference is missing a column the helper needs | Declare it in `sa.table(...)` (see §3.4) |
| Web UI shows "download document" instead of "open document" for PDFs | `Path(...).suffix` applied to a `...pdf::page2`-style source path | Split on `::page` before computing the suffix (see §2.5) |
| File path arrives at the server corrupted (missing separators) | Backslashes from Windows paths survived into a JavaScript string | Build paths with `.as_posix()` |
| Chunks cut mid-word | Tokenizer word-boundary detection doesn't match the current model's convention | Probe the tokenizer's marker style (§3.2) |
| Dim-mismatch error on insert | Vectors were produced by a different model than the column was created for | The column is `vector(1024)` for `bge-m3`; re-create the table and re-ingest if you change models |

## 4.6 Resource expectations (measured on a 6-core / 12-thread laptop)

| Operation | Cost |
|---|---|
| First model download | ~4.3 GB to the Hugging Face cache |
| Model in memory | +692 MB idle, ~1.9 GB while embedding |
| Embedding speed | ~85 ms per short text on 6 cores |
| Social corpus (583 chunks) | a few minutes |
| Document corpus (2,721 chunks) | 41 min 17 s wall clock, ~2.4 GB RAM, ~11,500 CPU-seconds |

If you move this to a small VPS, budget for this honestly: the embedding model
is the dominant cost, and a 1–4 vCPU box will be proportionally slower. See
Appendix B for the deployment discussion.

---

# Part 5 — Glossary

| Term | Meaning |
|---|---|
| **Chunk** | A piece of a source document, small enough to embed and retrieve individually. |
| **Corpus** | The whole body of content the system can answer questions about. |
| **Cosine similarity** | The angle between two vectors: 1.0 = identical direction, 0 = unrelated. With unit vectors it equals the dot product. |
| **Dense retrieval** | Searching by vector similarity (meaning). |
| **Embedding** | A vector representing a piece of text's meaning, produced by an embedding model. |
| **Embedding model** | The neural network that turns text into vectors. Here: `BAAI/bge-m3`, 1024 dimensions. |
| **Hallucination** | An LLM confidently producing false information. RAG's main motivation. |
| **Hybrid search** | Running dense and keyword retrieval and merging the results. |
| **Idempotent** | Safe to run more than once with the same end state. The property that makes this system recoverable. |
| **Ingestion** | The offline pipeline: extract → chunk → embed → store. |
| **Keyword / lexical retrieval** | Searching by exact term matching (Postgres full-text search here). |
| **k (top-k)** | How many results a retrieval call returns. |
| **OLAP / OLTP** | (Not used here, but a common confusion) Analytical vs. transactional database workloads. |
| **pgvector** | A Postgres extension adding a `vector` type and similarity operators. |
| **Reciprocal Rank Fusion (RRF)** | Merging ranked lists by summing `1/(k + rank)` per list. `k=60` here. |
| **source_path** | This system's stable identifier for a source document (or PDF page). Half of a chunk's unique key. |
| **Token** | A word-piece — the unit tokenizers and models actually count. ~4 characters in English, far fewer in other scripts. |
| **Vector** | An ordered list of numbers. Here, 1024 of them per text. |
| **Vector store** | The database holding vectors and answering nearest-neighbour queries. Here: Postgres + pgvector. |

---

# Appendix A — File-by-file map

| File | Role |
|---|---|
| `rag/config.py` | The only module that reads environment variables. Three validated, fail-fast settings loaders — one per database role. |
| `rag/bootstrap_db.py` | One-time: create database, `vector` extension, and the two roles. Prints generated passwords once. |
| `rag/schema.py` | One-time: create the `chunks` table and apply grants. Idempotent; converges the `source_type` CHECK. |
| `rag/chunking.py` | Token-measured, overlapping, word-boundary-safe splitting. Format-agnostic. |
| `rag/embedding.py` | `embed_texts` (generic, batched) and `embed_chunks` (ingestion wrapper). |
| `rag/upsert.py` | Generic `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` helper. |
| `rag/ingest.py` | `ingest_source()`: chunk → embed → upsert, one transaction. |
| `rag/load_social_share.py` | CSV corpus loader + orphan prune. |
| `rag/load_knowledge_base.py` | Document folder loader (PDF per page, HTML, TXT/MD) + orphan prune. |
| `rag/reindex.py` | Orchestrates load + prune for both corpora; returns a stats dict. |
| `rag/retrieval.py` | `retrieve()` (hybrid dense + keyword, RRF) and `get_chunk_neighbors()`. |
| `rag/webui.py` | FastAPI app: search UI, file serving, re-index button + status polling. |
| `rag/RAG_progress.md` | The authoritative decision log — 34 numbered decisions with the reasoning at the time, including superseded ones. |
| `rag/data/social_post_metrics.csv` | Corpus A source. |
| `rag/Knowledge_Base/documents/` | Corpus B source (flat folder). |

---

# Appendix B — Known limitations and risks

Recorded honestly, because a document that only lists strengths is not an
engineering document.

**1. The local Postgres container crashes under heavy ingestion load.**
Observed twice: the container exits silently (`Exited (255)`, no fatal error in
its own logs) while a heavy `bge-m3` ingestion runs concurrently — strongly
suggesting host memory pressure killing it inside Docker's WSL2 VM (not
confirmed with an explicit OOM message). **Mitigation in place:** the pipeline
commits per source and is upsert-based, so a crash loses at most partial
progress; recovery is simply re-running the loader, which was proven for real
when a run died at 1,995 of 2,721 chunks. **Not yet fixed:** no monitoring, no
auto-restart, no raised WSL2 memory allocation, no reduced ingestion batch size.

**2. `bge-m3`'s deployment footprint.** 4.3 GB on disk, ~1.9 GB RAM while
embedding, ~85 ms per short text on a 6-core machine. Fine for a background
ingestion job; a real constraint if retrieval-and-generation is ever expected
inline on a small VPS. This is in genuine tension with the parent project's
stated principle of not adding operational surface — recorded as a trade-off to
weigh before this moves to permanent infrastructure, not as a settled question.

**3. No vector index.** Every similarity query is a sequential scan. Correct and
fast at 3,304 rows; the thing to fix when latency becomes visible.

**4. No connection timeouts.** If the database dies, an in-flight HTTP request
can hang indefinitely rather than failing fast. A real production-readiness gap,
accepted at prototype stage.

**5. Prune misses shortened sources.** An `.htm` edited to half its length keeps
its source path, so its stale tail chunks survive until the source is deleted or
renamed. The fix would be per-source chunk-count reporting from the loader.

**6. Ordering within the relevant set is imperfect.** RRF fixed gross errors
(irrelevant documents in the top results) but not fine ranking. A cross-encoder
reranker or tuned RRF weights is the principled next step — deferred until an
evaluation set exists to measure it against.

**7. No authentication, no queueing, in-memory job state.** The web UI is a
single-user local prototype. A server restart mid-re-index kills the job and
clears the display (recoverable by re-running — everything is idempotent).

---

*This document describes the system as of 2026-10-06. For the authoritative,
continuously-updated decision record — including decisions later superseded —
see `RAG_progress.md` in the same folder.*
