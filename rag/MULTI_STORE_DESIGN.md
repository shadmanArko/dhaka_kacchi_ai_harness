# Multi-Store RAG — Design Document

| | |
|---|---|
| **Status** | Design agreed — **not implemented** |
| **Date** | 2026-10-06 |
| **Scope** | Extend the RAG subsystem from one vector table to N tables with per-table visibility (public / internal), enforced structurally |
| **Decision log** | Decisions #35–#47 in `RAG_progress.md` |
| **Supersedes** | Nothing. The existing single-table design stays valid until Phase 1 ships |

**How to read this.** §1–§4 are the *what and why* — read these to understand the
design. §5–§9 are the *how* — the contracts, the security boundaries, the exact
changes. §10–§15 are the *plan* — migration, phases, acceptance tests, risks. Every
diagram is Mermaid and renders on GitHub, in VS Code and in Obsidian.

---

## Contents

1. [Summary](#1-summary)
2. [Goals and non-goals](#2-goals-and-non-goals)
3. [The model](#3-the-model)
4. [Target architecture](#4-target-architecture)
5. [The store registry](#5-the-store-registry)
6. [Roles and grants](#6-roles-and-grants)
7. [The interface](#7-the-interface)
8. [Discovery: the agent's menu](#8-discovery-the-agents-menu)
9. [Security boundaries](#9-security-boundaries)
10. [Code changes, file by file](#10-code-changes-file-by-file)
11. [Migrating the existing data](#11-migrating-the-existing-data)
12. [Development phases](#12-development-phases)
13. [Verification and acceptance criteria](#13-verification-and-acceptance-criteria)
14. [Risks](#14-risks)
15. [Deferred / explicitly not doing](#15-deferred--explicitly-not-doing)
16. [Appendix — the decisions behind this document](#16-appendix--the-decisions-behind-this-document)

---

## 1. Summary

Today the RAG subsystem has exactly one vector table, `chunks`, holding both
corpora, and exactly one reader identity, `rag_reader`, which can read all of it.

This design replaces that with:

- **N store tables**, one per logical corpus, all sharing an identical schema shape.
- **A visibility tier per store** — `public` or `internal`.
- **Two reader roles** — `rag_public_reader` (public stores only) and
  `rag_internal_reader` (everything), composed by Postgres role membership.
- **A config-file registry** mapping the *logical* store name a caller uses
  (`hr_docs`) to the *physical* table (`chunks_hr_docs`).
- **A caller-supplied store name** on every query, which selects where to search
  but grants no authority of its own.

The governing rule, which everything else follows from:

> **The caller chooses where to look. The credential decides what it is allowed
> to find. Postgres — not Python — makes the decision.**

---

## 2. Goals and non-goals

### Goals

| # | Goal | How |
|---|---|---|
| 1 | A public-facing caller **cannot read internal data even if the application has a bug** | The database refuses it; the app check is redundancy |
| 2 | Adding a new store is a **config edit plus a grant**, not a code change | `stores.toml` + `grant` per table |
| 3 | Renaming a physical table **does not break callers** | Callers use logical names; config maps to physical |
| 4 | The store menu an agent sees is **honest** | Generated from `has_table_privilege`, not from the config |
| 5 | The retrieval logic stays **unchanged in shape** | One store per query keeps the dense + keyword + RRF pipeline intact |

### Non-goals

- Cross-store search, or merging results from multiple stores — §15.
- Per-row visibility inside one table (Row-Level Security) — §15.
- Automatic (semantic) routing to pick a store — §15.
- Authenticating the *human* in the web UI — still a local single-user prototype.

---

## 3. The model

### 3.1 Principal vs resource

Access control is always a pair — *(who is asking, what is being asked for) →
allow or deny* — and getting that pair right is the whole design.

The **rejected** design attached a password to each *table*. It fails because a
per-table secret identifies the **resource**, not the **principal**: it cannot
express revocation, per-caller audit, rotation, or "this caller may read 3 of 5
internal stores". It also re-implements authentication in Python — without
hashing or rotation — while the database already provides it, hardened.

The **accepted** design attaches the credential to the **audience**, and lets
Postgres grants decide what that audience may read.

### 3.2 Tiers

Two tiers exist today because two audiences exist:

| Tier | Audience | Example caller |
|---|---|---|
| `public` | Anyone — including people we do not trust | Customer chatbot |
| `internal` | The business's own operators and agents | Owner's ops agent |

A third tier is added **only when a third audience appears** (say, a contractor
who may read project docs but never financials) — never merely because a new
table was created.

### 3.3 Routing vs authority

Two independent inputs to every query, intersected:

```mermaid
flowchart LR
    A["① what the caller ASKS FOR<br/>store = 'hr_policies'<br/><i>routing intent —<br/>carries no authority</i>"]
    B["② what the credential PERMITS<br/>rag_public_reader's GRANTs<br/><i>= public tables only</i>"]
    A --> I{"intersection"}
    B --> I
    I -->|"hr_policies is public"| OK["search runs"]
    I -->|"hr_policies is internal"| NO["DENIED"]
    style OK fill:#1f6f43,color:#fff
    style NO fill:#8b2e2e,color:#fff
```

The caller may ask for anything. It can only receive what its role was granted.

---

## 4. Target architecture

### 4.1 Who holds which credential

The credential belongs to the **process**, never to the prompt. This single
diagram is the security model:

```mermaid
flowchart TB
    subgraph clients["Callers"]
        PUB["Public chatbot<br/><i>untrusted users</i>"]
        OPS["Owner / ops agent<br/><i>trusted</i>"]
    end

    PUB --> CA["rag_public_reader"]
    OPS --> CB["rag_internal_reader"]

    CA ==>|"SELECT"| T1
    CB ==>|"SELECT"| T1
    CB ==>|"SELECT"| T2
    CB ==>|"SELECT"| T3

    CA -.->|"NO GRANT"| T2
    CA -.->|"NO GRANT"| T3

    T1[("chunks_social_share<br/>PUBLIC")]
    T2[("chunks_knowledge_base<br/>INTERNAL")]
    T3[("chunks_hr_policies<br/>INTERNAL")]

    style T1 fill:#1f6f43,color:#fff
    style T2 fill:#8b2e2e,color:#fff
    style T3 fill:#8b2e2e,color:#fff
```

The **dotted red lines are the entire security model.** There is no code
anywhere that enforces "public callers may not read internal stores" — there is
simply no grant, so Postgres refuses.

### 4.2 Component map

```mermaid
flowchart TB
    subgraph callers[" "]
        CB["Public chatbot process"]
        AG["Ops agent process"]
    end

    subgraph rag["rag/ (Python, run with uv)"]
        CFG["config.py<br/>· loads .env (credentials)<br/>· loads stores.toml (registry)"]
        RET["retrieval.py<br/>retrieve(query, store=…)<br/>list_stores()"]
        LOAD["loaders + reindex.py"]
        UI["webui.py"]
    end

    subgraph db["dhaka_kacchi_rag (Postgres + pgvector)"]
        STORES[("store tables<br/>chunks_*")]
    end

    CB --> RET
    AG --> RET
    UI --> RET
    CFG --> RET
    CFG --> LOAD
    LOAD -->|"rag_writer"| STORES
    RET -->|"reader credential"| STORES
```

---

## 5. The store registry

### 5.1 Location and format

`rag/stores.toml`, read by `rag/config.py` — which stays the single module that
owns configuration.

TOML specifically because Python 3.12's standard library reads it (`tomllib`).
**No new dependency**, which matches this project's house instinct.

### 5.2 Shape

```toml
# rag/stores.toml
#
# One entry per vector store. Adding a store = add an entry here, create its
# table, and grant the right roles. No Python changes required.

[stores.social_share]
table              = "chunks_social_share"
visibility         = "public"
chunk_size_tokens  = 100
overlap_tokens     = 20
description        = "Marketing posts from social platforms: menu highlights, promotions, and the story behind the kacchi box."

[stores.knowledge_base]
table              = "chunks_knowledge_base"
visibility         = "internal"
chunk_size_tokens  = 250
overlap_tokens     = 50
description        = "Technical papers and legal filings: machine-learning research papers and SEC contract exhibits."

[stores.hr_policies]
table              = "chunks_hr_policies"
visibility         = "internal"
chunk_size_tokens  = 250
overlap_tokens     = 50
description        = "Internal HR policy: staff handbook, shift rules, leave and disciplinary procedures."
```

### 5.3 Field by field

| Field | Consumer | Purpose |
|---|---|---|
| **logical name** (the TOML key) | Callers, `retrieve()` | What a caller passes. Stable across renames. |
| `table` | `retrieve()`, loaders, `schema.py` | The physical table. **Never caller-controlled.** |
| `visibility` | The verifier, the UI | A **declaration of intent** — see §5.4 |
| `chunk_size_tokens`, `overlap_tokens` | **Loaders only** | Ingestion settings. `retrieve()` never reads them. |
| `description` | Discovery (§8) | What an agent reads when choosing a store |

Two consumers taking two different slices — and the fact that both fit cleanly
is a sign the abstraction is at the right level.

### 5.4 `visibility` is an assertion, not a permission

The config file must **never** be the thing that decides access. If an entry says
`visibility = "public"` while the grants say otherwise, **the grants win**.

So `visibility` has exactly two legitimate jobs:

1. **Documentation** — a human reading the file learns the intent.
2. **Verification** — a check (§13) asserts every declared public store is
   readable by `rag_public_reader`, and every declared internal store is *not*.
   Drift fails loudly, exactly like the warehouse's `make verify`.

```mermaid
flowchart LR
    C["stores.toml<br/><b>declares intent</b><br/>visibility = 'public'"]
    D["Postgres GRANTs<br/><b>hold truth</b>"]
    V{"verify_stores.py<br/>do they agree?"}
    C --> V
    D --> V
    V -->|agree| OK["✓ pass"]
    V -->|drift| FAIL["✗ fail loudly"]
    style OK fill:#1f6f43,color:#fff
    style FAIL fill:#8b2e2e,color:#fff
```

### 5.5 Validation at load time

`config.py` fails fast on:

- a duplicate `table` across two stores — two logical names pointing at one table
  would give the same data two different visibility tiers
- a missing required field
- `overlap_tokens >= chunk_size_tokens` (the chunker's own guard, surfaced earlier)
- a `visibility` value other than `public` / `internal`
- a `table` name that is not a safe identifier (§9.1)

---

## 6. Roles and grants

### 6.1 The roles

| Role | Holds | Used by |
|---|---|---|
| `postgres` (admin) | Superuser | One-time setup only |
| `rag_writer` | Write on **every** store table | The ingestion job |
| `rag_public_reader` | `SELECT` on **public** store tables only | Public chatbot, anything facing untrusted users |
| `rag_internal_reader` | `SELECT` on **all** store tables | Owner / ops agents |

```mermaid
graph BT
    W["rag_writer<br/><i>write — all stores</i>"]
    PR["rag_public_reader<br/><i>SELECT — public stores</i>"]
    IR["rag_internal_reader<br/><i>SELECT — all stores</i>"]
    IR -->|"member of<br/>(inherits its grants)"| PR
    style IR fill:#1f6f43,color:#fff
    style PR fill:#1f6f43,color:#fff
```

Membership means public-store grants are written **once**. Adding a public store
later is one `GRANT`, not two.

### 6.2 Grants per store

For a **public** store:

```sql
GRANT SELECT ON chunks_social_share TO rag_public_reader;   -- internal inherits this
GRANT SELECT, INSERT, UPDATE, DELETE ON chunks_social_share TO rag_writer;
```

For an **internal** store:

```sql
GRANT SELECT ON chunks_hr_policies TO rag_internal_reader;  -- NOT granted to public
GRANT SELECT, INSERT, UPDATE, DELETE ON chunks_hr_policies TO rag_writer;
```

**The whole security model is the absence of that one line.**

### 6.3 Credentials

`.env` gains one URL per reader identity:

```bash
RAG_ADMIN_DATABASE_URL=postgresql://postgres:...@localhost:5434/postgres
RAG_WRITER_DATABASE_URL=postgresql://rag_writer:...@localhost:5434/dhaka_kacchi_rag
RAG_PUBLIC_READER_DATABASE_URL=postgresql://rag_public_reader:...@localhost:5434/dhaka_kacchi_rag
RAG_INTERNAL_READER_DATABASE_URL=postgresql://rag_internal_reader:...@localhost:5434/dhaka_kacchi_rag
```

`RAG_READER_DATABASE_URL` is retired. Only two call sites consume a reader
credential, so this is a clean break rather than a compatibility shim.

`config.py` gains `load_rag_public_reader_settings()` and
`load_rag_internal_reader_settings()` — same shape as the existing loaders, each
validating its own URL.

### 6.4 Which process gets which

| Process | Credential in its config |
|---|---|
| Public chatbot service | `RAG_PUBLIC_READER_DATABASE_URL` |
| Internal ops tool / owner agent | `RAG_INTERNAL_READER_DATABASE_URL` |

The credential belongs to the **deployment**. An LLM never sees, holds or
transmits one — so it cannot be persuaded to use a different one.

---

## 7. The interface

### 7.1 `retrieve()`

```python
def retrieve(
    query: str,
    *,
    store: str,                    # NEW: logical name, e.g. "hr_policies"
    top_k: int,
    registry: StoreRegistry,       # NEW: loaded from rag/stores.toml
    settings: RagReaderSettings,   # UNCHANGED: carries the credential
) -> list[dict]:
```

```mermaid
flowchart TD
    A["retrieve(query, store='hr_policies', …)"] --> B{"store in<br/>registry?"}
    B -->|no| E["raise: unknown store"]
    B -->|yes| C{"does this role have SELECT<br/>on the physical table?<br/>(has_table_privilege)"}
    C -->|no| E2["raise: unknown store<br/><i>same error — no existence leak</i>"]
    C -->|yes| D["dense + keyword search<br/>on the physical table"]
    D --> F["RRF fusion → top_k"]
    style E fill:#8b2e2e,color:#fff
    style E2 fill:#8b2e2e,color:#fff
    style F fill:#1f6f43,color:#fff
```

**Steps 1–2 are UX and information-hiding, not security.** Delete them and step 3
still fails inside Postgres with a permission error — because the role has no
grant. That redundancy is deliberate: the application check produces a clean
error; the grant is the actual enforcement (§13 tests exactly this).

### 7.2 Uniform error for "not permitted" and "not found"

An unauthorized store returns **the same error** as a nonexistent one.

The menu (§8) only ever lists readable stores, so anything not on the menu is
indistinguishable from nonexistent. Returning "permission denied" would confirm
to a public caller that an internal store *exists* — a small but real
information leak. Uniform errors close it.

### 7.3 Full request lifecycle

```mermaid
sequenceDiagram
    participant U as User / agent LLM
    participant P as RAG process · one credential
    participant R as stores.toml registry
    participant DB as Postgres

    U->>P: question, store = "hr_policies"
    Note over U,P: no password, no credential, ever —<br/>routing only
    P->>R: logical name → physical table
    R-->>P: chunks_hr_policies
    P->>DB: has_table_privilege(role, table, SELECT)?

    alt public chatbot — rag_public_reader
        DB-->>P: false
        P-->>U: "unknown store" (uniform error)
        Note over P,U: even if this check were deleted,<br/>the search SQL itself would fail —<br/>the role has no grant
    else internal agent — rag_internal_reader
        DB-->>P: true
        P->>DB: dense + keyword search
        DB-->>P: ranked chunks
        P-->>U: results
    end
```

### 7.4 `list_stores()`

```python
def list_stores(*, registry: StoreRegistry, settings: RagReaderSettings) -> list[dict]:
    """Every store this credential can actually read, with descriptions."""
```

Returns `[{"store", "description", "visibility"}, ...]`, filtered by asking the
database — not by reading the config's `visibility` field (§8).

### 7.5 Unchanged

`get_chunk_neighbors()` gains the same `store` parameter and nothing else. The
fused ranking, the RRF constant, the dense/keyword SQL, the result dict shape —
**all unchanged.** One store per query is what buys that.

---

## 8. Discovery: the agent's menu

An agent that must *name* a store needs to know which names exist.

**Chosen mechanism: enumerate the stores in the tool description**, generated
per-credential. The RAG is exposed to agents as a tool (function first, MCP
later, per Step 8), and that tool's `store` parameter documents the list of
stores this agent may read — each with its one-line description.

### 8.1 The scaling ladder

| Set size | Mechanism | Status |
|---|---|---|
| **A handful** | Enumerate in the tool description | ✅ **Chosen** |
| Dozens | A `list_stores()` call the agent makes first | Deferred — §15 |
| Hundreds | Semantic routing (relevance, never security) | Deferred — §15 |

### 8.2 The invariant

**The menu is filtered by the same credential as retrieval, and derived from the
database — not from the config file.**

```mermaid
flowchart LR
    ALL["stores.toml<br/>every store"] --> Q{"has_table_privilege<br/>(this role, table, 'SELECT')"}
    Q -->|true| M["appears on<br/>this agent's menu"]
    Q -->|false| X["hidden"]
    style M fill:#1f6f43,color:#fff
    style X fill:#8b2e2e,color:#fff
```

If the menu came from the config's `visibility`, a config that had drifted from
the grants would hand a public chatbot a menu entry naming an internal store. The
read would still fail — but the chatbot would have learned the store exists.
Deriving from the grants makes the menu honest by construction.

Consequence: one source of truth (the grants) is consumed at **three** points —
enforcement, discovery, verification — instead of three lists that drift apart.

### 8.3 What the choice commits to

1. **The menu is generated at process start, not per call.** Tool schemas are
   registered when the agent's process starts, so adding a store to
   `stores.toml` requires a restart before agents see it. Acceptable — stores are
   configuration, not runtime data.
2. **`description` in `stores.toml` is prompt text, not documentation.** It is
   read by a model deciding where to search, so it must be written for *that*
   reader: what kinds of questions does this store answer? Store-selection
   quality becomes tunable by editing a TOML file — no code change, no deploy.
3. **Every description ships on every model call**, not just retrieval calls,
   because tool schemas travel with the request. Trivial at three stores; the
   trigger to move to a discovery call is when they stop fitting comfortably.
4. **There are two menus, not one** — one per credential. A public agent's tool
   schema never names an internal store.
5. **This is also Step 8's design.** "Interface for agents to call this" and "how
   an agent picks a store" are the same problem. Build them together.

---

## 9. Security boundaries

### 9.1 Table names cannot be bind parameters — the injection boundary

The physical table name must be interpolated into SQL text; Postgres has no
syntax for "a parameter standing in for a table name". Today that is safe
because the name is a literal in the source. After this change it comes from
config — so the rule becomes explicit:

> **The caller supplies a logical name. The config supplies the physical name.
> The physical name is never caller-controlled — and it is validated and quoted
> anyway.**

```mermaid
flowchart LR
    U["caller supplies:<br/>'hr_policies'"] --> K["dict key lookup<br/><b>never reaches SQL</b>"]
    K --> T["config supplies:<br/>'chunks_hr_policies'"]
    T --> V["validated:<br/>identifier pattern"]
    V --> Q["quoted with _q()"]
    Q --> S["SQL text"]
    style K fill:#1f6f43,color:#fff
```

Three defences, in order:

1. The caller's `store` value is only ever a **dict key** into the registry. It
   never reaches SQL in any form. An unknown key errors out.
2. `table` from the config is validated at load against a strict identifier
   pattern (letters, digits, underscore).
3. When spliced into SQL, the identifier is double-quoted with the existing
   `_q()` helper, which doubles any embedded quote.

Defence 3 is redundant if 1 and 2 hold. It stays because the cost is one function
call and the cost of a mistake is the whole database.

### 9.2 Secrets never enter a prompt

No password, token or credential is ever passed as part of a question, placed in
an LLM context, or returned by any function here. Prompt injection therefore has
no credential to steal: a public chatbot tricked into asking for internal data is
still holding a role that cannot read it.

### 9.3 The config is not an enforcement layer

Restated because it is the most likely thing to be eroded later: `stores.toml`
*describes* stores. It does not *permit access to* them. Any future feature that
reads `visibility` to make a security-relevant decision is a regression — and
the verifier exists to catch exactly that drift.

---

## 10. Code changes, file by file

| File | Change | Size |
|---|---|---|
| `rag/stores.toml` | **New.** The registry. | S |
| `rag/config.py` | Add `Store` + `StoreRegistry` + TOML loading + validation. Add two reader settings loaders. | M |
| `rag/schema.py` | Create N tables from the registry instead of one `chunks`; grant per table by `visibility`; keep drop-then-add CHECK convergence per table. | M |
| `rag/bootstrap_db.py` | Create `rag_public_reader` / `rag_internal_reader` (+ membership) instead of `rag_reader`. Print all generated passwords. | S |
| `rag/ingest.py` | `chunks_t` becomes a per-store table reference from the registry. `ingest_source()` gains a `store` parameter. | S–M |
| `rag/retrieval.py` | The three SQL strings become templates on the resolved table name. `retrieve()` / `get_chunk_neighbors()` gain `store`. Add `list_stores()`. | M |
| `rag/load_social_share.py` | Read chunk settings from the registry instead of module constants. Pass its store name. | S |
| `rag/load_knowledge_base.py` | Same. Its prune's `LIKE 'documents/%'` scoping stays, now implicit per table. | S |
| `rag/reindex.py` | Iterate the registry instead of hardcoding two corpora. | S–M |
| `rag/webui.py` | Store selector in the UI, populated from `list_stores()` for the server's credential. | M |
| `rag/verify_stores.py` | **New.** The config-vs-grants verifier. | S–M |
| `.env.example` | Document the reader URLs (currently missing even the existing RAG vars). | S |

Sizing is relative (S ≈ a short session, M ≈ a working session). No item here is
large; the heavy cost is wall-clock (§11), not development.

---

## 11. Migrating the existing data

The live `chunks` table holds 3,304 rows from **two** corpora, so it cannot
simply be renamed — it has to become two tables.

```mermaid
flowchart LR
    subgraph before["BEFORE"]
        B1[("chunks<br/>3,304 rows<br/>both corpora")]
    end
    subgraph after["AFTER"]
        A1[("chunks_social_share<br/>583 rows · PUBLIC")]
        A2[("chunks_knowledge_base<br/>2,721 rows · INTERNAL")]
    end
    B1 ==>|"re-ingest<br/>~45 min"| A1
    B1 ==>|"re-ingest<br/>~45 min"| A2
    style A1 fill:#1f6f43,color:#fff
    style A2 fill:#8b2e2e,color:#fff
```

| Option | Cost | Verdict |
|---|---|---|
| **A. Copy rows** — `INSERT INTO … SELECT … WHERE source_type='csv'`, likewise for documents | Minutes. No re-embedding needed — every vector came from the same `bge-m3` model, so all remain valid. | More migration SQL to write and verify; leaves the chunk-boundary bug in place |
| **B. Re-ingest** — create the new tables, run both loaders | ~45 minutes wall clock | ✅ **Recommended** |

**Why B despite being slower.** Ingestion is already idempotent, already verified,
already resumable — the migration is "run the two loaders you already have", with
no new migration code to get wrong. And it is not purely a cost: decision #34
records a **pending chunk-boundary fix** (the mid-word-snap guard was dead under
`bge-m3`) whose own note says it "applies to stored chunks only on the next
re-index". Option B applies it for free; option A carries the bug forward.

After a verified re-ingest, the old `chunks` table is dropped (or kept one
session as a fallback, then dropped).

---

## 12. Development phases

Each phase ends at a **working state** — the system is never half-migrated across
a phase boundary. Phases 1–3 change no behaviour a caller can see, deliberately:
the risky work (roles, grants, isolation) lands and is proven *before* anything
depends on it.

```mermaid
flowchart LR
    P1["1 · Registry<br/><i>stores.toml + validation</i>"] --> P2["2 · Schema<br/>parameterization"]
    P2 --> P3["3 · Roles<br/>& grants"]
    P2 --> P4["4 · Interface<br/>store=…"]
    P3 --> P4
    P1 --> P5["5 · Ingestion<br/>registry-driven"]
    P2 --> P5
    P3 --> P6["6 · Data<br/>migration"]
    P4 --> P6
    P5 --> P6
    P4 --> P7["7 · Web UI<br/>+ verifier"]
    P6 --> P7
    style P1 fill:#1f6f43,color:#fff
```

| # | Phase | Ends when | Depends on | Size |
|---|---|---|---|---|
| 1 | **Registry** — `stores.toml`, `Store`, `StoreRegistry`, validation | Config loads and validates; both existing corpora are described in it. **No behaviour change.** | — | S |
| 2 | **Schema parameterization** — `schema.py` creates tables from the registry; the grant helper takes a visibility tier | New empty tables exist with correct grants; the old `chunks` table is untouched and the system still works | 1 | M |
| 3 | **Roles** — `rag_public_reader` / `rag_internal_reader`, membership, `.env`, two settings loaders | Both reader credentials connect; the public one **cannot** select from an internal table (verified by hand) | 2 | S–M |
| 4 | **Interface** — `store` through `retrieve()` and `get_chunk_neighbors()`, `list_stores()`, uniform errors | A caller can search a named store with a given credential; unknown and unauthorized give identical errors | 2, 3 | M |
| 5 | **Ingestion** — loaders + `reindex.py` read the registry | Both corpora can be ingested into their new tables, driven by config | 1, 2 | M |
| 6 | **Data migration** — re-ingest both corpora; verify counts; drop `chunks` | Store holds the same content, correctly split, with fixed chunk boundaries | 3, 4, 5 | wall-clock heavy |
| 7 | **Web UI + verifier** — store selector; `verify_stores.py` | UI lists only readable stores; the verifier passes and fails correctly on a planted mismatch | 4, 6 | M |

---

## 13. Verification and acceptance criteria

### 13.1 The test that matters most

> **Delete the application-level allowlist check. Confirm the query still fails
> inside Postgres.**

If removing the Python check opens the data, the design was never structural and
the whole exercise failed. Run it deliberately, once, as an acceptance test.

```mermaid
flowchart TD
    A["attacker / bug / prompt injection<br/>asks for an internal store"] --> B["application check<br/>deliberately removed"]
    B --> C["SQL reaches Postgres<br/>as rag_public_reader"]
    C --> D{"does this role have SELECT<br/>on chunks_hr_policies?"}
    D -->|no| E["<b>permission denied by the database</b><br/>the design holds"]
    D -->|yes| F["data returned<br/>the design failed"]
    style E fill:#1f6f43,color:#fff
    style F fill:#8b2e2e,color:#fff
```

### 13.2 Checklist

| # | Check | Proves |
|---|---|---|
| 1 | `rag_public_reader` selects from a public table | The public path works at all |
| 2 | `rag_public_reader` selecting from an internal table **fails** | The isolation is real |
| 3 | With the app check bypassed, #2 still fails | Enforcement is in Postgres |
| 4 | `list_stores()` with the public credential omits every internal store | Discovery is credential-filtered |
| 5 | `list_stores()` with the internal credential lists everything | Inheritance works |
| 6 | Editing `table` in `stores.toml` and restarting redirects queries, with **no code change** | The indirection delivers |
| 7 | `verify_stores.py` passes on the real config; flip one `visibility` and confirm it **fails** | The verifier isn't decorative |
| 8 | Per-store chunk counts match after migration; no mid-word cuts at chunk edges | Migration preserved content and applied #34 |

Check 7 is the one most likely to be skipped, and the one that keeps the config
honest over time.

---

## 14. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Table-name interpolation becomes an injection vector** if a caller value ever reaches SQL | High | §9.1 — logical names are dict keys only; pattern validation; identifier quoting |
| **Config/grant drift** — a store marked public that isn't, or vice versa | High | `verify_stores.py`; the menu is derived from the DB, not the config |
| **Someone erodes the boundary later** by reading `visibility` for an access decision | High | §9.3 states it explicitly; the verifier fails on drift |
| **A process is deployed with the wrong credential** (public chatbot given the internal URL) | **Critical** — and the design cannot prevent it | The highest-risk failure mode is a *deployment* mistake, not a code one. Mitigations: unambiguous variable names, documented service→credential mapping (§6.4), and a startup log line printing which role the process connected as |
| Re-ingest takes ~45 min and the local container crashes under load (known — see the guide's Appendix B) | Medium | Loaders are idempotent and resumable; re-run on crash |
| Re-index now iterates N stores, so wall clock grows with store count | Low | Per-store re-index becomes possible and desirable — `reindex(store="social_share")` |
| Two reader credentials to rotate instead of one | Low | Rotation is 2 `ALTER ROLE` calls; membership means public grants are still written once |

---

## 15. Deferred / explicitly not doing

| Deferred | Why |
|---|---|
| **Cross-store search** (one question, several stores, merged results) | Chosen against: one store per query keeps the existing ranking untouched. If ever needed, RRF already exists in `retrieval.py` for fusing dense + keyword, and fusing N stores is the same shape — but "what does best-answer mean across corpora" is a real question, not a formatting one. |
| **Row-Level Security** (public and internal rows in one table) | The legitimate version of "one table with a visibility flag" — but heavier to reason about, easy to misconfigure, and unnecessary while separate tables + grants are available. |
| **Role per table** | Rejected: it recreates the per-table-password mistake one layer down. Roles model audiences, not resources. |
| **A `list_stores()` discovery call** (ladder level 2) | Deferred with the enumeration choice. Becomes right when store descriptions stop fitting comfortably in the tool description — it makes new stores visible without a restart, at the cost of a round trip. |
| **Semantic routing** to auto-pick a store (ladder level 3) | Real, but a relevance decision — and over-engineering for a handful of stores. Never permitted to broaden what a credential can read. |
| **Per-store `source_type` CHECK lists** | Every store currently accepts the same source types. Narrowing per store is a refinement, not a need. |
| **Web-UI authentication** | Still a local single-user prototype; the credential the server runs with is the only access control there today. |

---

## 16. Appendix — the decisions behind this document

Recorded with the reasoning, because the reasoning is the part that transfers.
These are also mirrored as decisions #35–#47 in `RAG_progress.md`.

1. **Separate tables, one database.** Postgres grants privileges per *table* but
   not per row (without RLS). The table shape is what makes enforcement possible
   at all.
2. **Rejected: a password per table, supplied by the caller.** A password
   identifies a *principal*, not a *resource*. A per-table secret cannot express
   who is asking, cannot be revoked per caller, cannot be audited, and must be
   handed to whichever component uses it — including an LLM, where it can be
   extracted or prompt-injected out.
3. **Credentials belong to the process, never to a prompt.** The LLM expresses
   intent; the deployment's credential enforces identity. A public chatbot cannot
   be tricked into reading internal data because it holds no credential that can.
4. **Callers authenticate as a role; they never claim one.** "Identify my role" is
   self-declaration and forgeable. The connection *is* the role, proven by secret
   at connect time.
5. **Role count follows audiences, not tables.** Two audiences today → two reader
   roles. Postgres role membership composes them so grants aren't duplicated.
6. **One store per query.** Chosen over "search everything the role can read": it
   keeps `retrieve()` a single query and the ranking pipeline untouched.
7. **Registry in a config file** (TOML, stdlib-readable). Adding a store is an
   edit, not a deploy.
8. **`visibility` is a checkable assertion, never a permission.** The grants are
   the truth; the config declares intent and something verifies they agree.
9. **Discovery is credential-filtered and database-derived**
   (`has_table_privilege`). One source of truth, consumed at three points —
   enforcement, discovery, verification — instead of three lists that drift.
10. **Logical names in the interface**, physical tables in the config. Renaming a
    table becomes a config edit that no caller notices. **Security consequence:**
    this creates a new SQL-injection surface — table names cannot be bind
    parameters — closed by three defences (§9.1).
11. **Uniform error for unauthorized and nonexistent stores.** The menu already
    hides what you can't read; saying "permission denied" would confirm the store
    exists.
12. **The app-level permission check is redundancy, not security.** Delete it and
    Postgres still refuses. That property is the acceptance test (§13).
13. **Discovery by enumeration in the tool description**, over a `list_stores()`
    call. For a handful of stores the enumeration costs nothing, needs no extra
    round trip, and puts the menu exactly where the model already looks. Two
    consequences accepted with it: a new store needs a process restart to appear
    in an agent's menu, and every store's description occupies prompt space on
    every call. Both are fine at this size and both are why the discovery-call
    option stays on the ladder (§15).
