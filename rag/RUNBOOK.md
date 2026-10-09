# RAG runbook — set up, add a store, test, fix

| | |
|---|---|
| **Audience** | Whoever is operating this subsystem: Ahmad, a future session of Claude, or anyone taking over |
| **Covers** | Setting it up from nothing · adding/removing a store · testing it · what breaks and why |
| **Design** | `rag/MULTI_STORE_DESIGN.md` (why it is shaped this way) · `RAG_progress.md` (every decision, in order) |
| **Assumes** | Windows (PowerShell), Linux or macOS · `uv` installed · Docker Desktop available |
| **Shells** | Every command is given for both POSIX shells and PowerShell — see §0 |

Everything below is a command you can paste. Where a command produces output
worth recognising, the expected output is shown.

**Contents**

| | |
|---|---|
| [§0](#0-running-these-commands-linux--macos--windows) | Running these commands — which shell each block is for |
| [§1](#1-what-this-subsystem-is-in-one-page) | What the subsystem is, in one page |
| [§2](#2-set-up-from-nothing) | Set up from nothing (six steps) |
| [§3](#3-adding-a-store-the-main-how-to) | **Adding a store** — the main how-to |
| [§4](#4-removing-a-store) | Removing a store |
| [§5](#5-testing) | Testing — four levels, plus the opt-in scale test |
| [§6](#6-what-breaks-and-what-it-means) | What breaks, and what it means |
| [§7](#7-opening-the-database-in-a-gui-dbeaver-pgadmin-tableplus-datagrip) | **Opening the database in a GUI** (DBeaver, pgAdmin, …) |
| [§8](#8-known-gaps-deliberate-at-prototype-stage) | Known gaps |
| [§9](#9-cheat-sheet) | Cheat sheet |

---

## 0. Running these commands (Linux · macOS · Windows)

**Most of this runbook is shell-neutral.** `docker …`, `uv run python -m rag.…`,
`uv run uvicorn …` and `psql` take exactly the same arguments in PowerShell as
they do in bash or zsh, so those blocks are shown ONCE, marked
`# Linux · macOS · Windows`. Only the handful of commands that genuinely differ
are shown twice, labelled. Nothing here needs WSL, Git Bash or a POSIX shell on
Windows.

The five real differences, all of which have bitten someone:

| | Linux · macOS (bash/zsh) | Windows (PowerShell) |
|---|---|---|
| **Line continuation** | `\` at end of line | **`` ` `` (backtick)** — or just keep it on one line, which is what this runbook does |
| **Env var for one command** | `VAR=value command` | `$env:VAR="value"; command` |
| **Running an `.exe` by full quoted path** | `"/path/to/x" arg` | `& "C:\path\to\x.exe" arg` — without `&`, PowerShell parses the quoted string as text (`Unexpected token 'start'`) |
| **`curl`** | `curl …` | `curl.exe …` — plain `curl` is a PowerShell alias for `Invoke-WebRequest`, which does not accept `-s`, `-o` or `-w` |
| **`mkdir -p a/b/c`** | as written | `New-Item -ItemType Directory -Force a/b/c` — `-p` is not a PowerShell flag |

Two more things worth knowing when you edit the Python one-liners in §3, §5 and
§6:

* **PowerShell double-quoted strings interpolate `$`.** None of the snippets
  here contain one, so they paste unchanged — but if you add a `$` to a
  `python -c "…"` snippet, escape it as `` `$ `` or switch the outer quotes to
  single quotes.
* A `python -c "…"` string may span several lines in both shells. If you prefer
  files, save the snippet as `snippet.py` and run `uv run python snippet.py` —
  identical everywhere.

> **`make` targets** (`make rag-verify`, …) assume `make` is installed. It is
> not part of Git Bash on Windows by default; the `uv run …` command each target
> wraps is written out in full everywhere in this runbook, so nothing here
> depends on `make` existing.

---

## 1. What this subsystem is, in one page

**Two stores today:**

| Store (logical name) | Table | Tier | Content | Folder |
|---|---|---|---|---|
| `brand_book` | `chunks_brand_book` | **public** | The customer-facing brand knowledge — story, menu, ordering, FAQ | `Knowledge_Base/public/` |
| `voice_and_rules` | `chunks_voice_and_rules` | **internal** | Voice, audience and legal rules for the business's own content agents | `Knowledge_Base/internal/` |

**Three roles, one database (`dhaka_kacchi_rag`):**

| Role | May read | Used by |
|---|---|---|
| `rag_writer` | nothing; may **write** every store table | the ingestion job |
| `rag_public_reader` | public stores only | anything facing customers |
| `rag_internal_reader` | every store (member of the public role) | the owner's own agents, the local web UI |

**The rule everything else follows from:**

> The caller chooses *where to look*. The credential decides *what it is allowed
> to find*. **Postgres** — not Python — makes that decision.

A public chatbot that has been tricked into asking for internal data does not
get a polite refusal from application code; it gets a permission error from the
database, because its role was never granted anything on that table. Delete
every `if` in the Python and it still cannot read it. That property is tested
(`rag/stress_test.py`, section 3c) rather than assumed.

**What lives where:**

```
rag/
  stores.toml          <- the registry: which stores exist. THE file you edit to add one.
  config.py            <- reads stores.toml + the three credentials. Validates both.
  bootstrap_db.py      <- one-time: database, vector extension, three roles
  schema.py            <- creates one table per store, grants per visibility tier
  ingest.py            <- chunk -> embed -> upsert (one source at a time)
  load_files.py        <- the generic loader: reads a store's folder
  load_social_share.py <- the other loader (currently unregistered, kept for restore)
  reindex.py           <- runs load+prune for every store
  retrieval.py         <- retrieve(query, store=...) / list_stores()
  verify_stores.py     <- checks stores.toml against the real grants
  stress_test.py       <- the test suite this runbook keeps referring to
  webui.py             <- local search page (http://127.0.0.1:8010)
  Knowledge_Base/
    public/            <- files that become the PUBLIC store
    internal/          <- files that become the INTERNAL store
    facts.yaml         <- NOT embedded (live data; see §6.4)
    documents/         <- unregistered archive (was the "knowledge_base" store)
```

---

## 2. Set up from nothing

Six steps, in this order. Each is idempotent — running one twice is safe.

### 2.1 Start the database

**If the container already exists** (it does on this machine) this is the whole
step. It normally comes back by itself: its restart policy is
`unless-stopped`, so Docker starts it as soon as Docker Desktop is running.
Check the policy, and the container's state, with:

```bash
# Linux · macOS · Windows (identical)
docker inspect dhaka-kacchi-rag --format '{{.HostConfig.RestartPolicy.Name}}'   # -> unless-stopped
docker start dhaka-kacchi-rag
docker exec dhaka-kacchi-rag pg_isready -U postgres
```

```
/var/run/postgresql:5432 - accepting connections
```

`docker start` on an already-running container is a harmless no-op, which is why
it is here unconditionally — the case it covers is a container that was stopped
by hand, or Docker itself not running when the machine booted. If the policy
ever reads `no` (a container created by hand without `--restart`), set it:

```bash
# Linux · macOS · Windows (identical)
docker update --restart unless-stopped dhaka-kacchi-rag
```

**Only on a machine that has never had it**, create the container:

```bash
# Linux · macOS · Windows (identical - docker.exe takes the same arguments)
docker run -d --name dhaka-kacchi-rag -e POSTGRES_PASSWORD=localdevpassword -p 5434:5432 pgvector/pgvector:pg16
```

> **One line, never a `\`-continued block.** These pages get read in PowerShell
> as often as in bash, and `\` is a bash-ism: PowerShell ends the command there
> and then reads each following line as a command of its own
> (`-e : The term '-e' is not recognized as the name of a cmdlet…`). That is
> exactly the failure this line is written to avoid — see §0.

> The container is `pgvector/pgvector:pg16` on host port **5434**. The image
> ships the `vector` extension; a plain `postgres:16` image would not, and
> `CREATE EXTENSION vector` would fail in step 2.

### 2.2 Fill in `.env`

Copy `.env.example` to `.env` at the repo root (one `.env`, shared with the
warehouse) and set:

```bash
# file contents, not commands - same on every OS
RAG_ADMIN_DATABASE_URL=postgresql://postgres:localdevpassword@127.0.0.1:5434/postgres
RAG_WRITER_DATABASE_URL=postgresql://rag_writer:...@127.0.0.1:5434/dhaka_kacchi_rag
RAG_PUBLIC_READER_DATABASE_URL=postgresql://rag_public_reader:...@127.0.0.1:5434/dhaka_kacchi_rag
RAG_INTERNAL_READER_DATABASE_URL=postgresql://rag_internal_reader:...@127.0.0.1:5434/dhaka_kacchi_rag
```

Only the **admin** URL is knowable up front. The other three passwords are
generated by the next step and printed exactly once.

### 2.3 Create the database, extension and roles

```bash
# Linux · macOS · Windows (identical)
uv run python -m rag.bootstrap_db
```

```
=== SAVE THESE NOW - shown only this once ===
  rag_writer password: ...
  rag_public_reader password: ...
  rag_internal_reader password: ...
```

Paste those into `.env`. **Postgres stores only a hash** — if the printout is
lost, the password cannot be recovered, only reset:

```bash
# Linux · macOS · Windows (identical - one line, so no continuation to get wrong)
docker exec -it dhaka-kacchi-rag psql -U postgres -c "ALTER ROLE rag_writer PASSWORD 'new-one';"
```

Re-running this step never resets an existing role's password (it prints
"already exists, password unchanged").

### 2.4 Create the store tables and their grants

```bash
# Linux · macOS · Windows (identical)
uv run python -m rag.schema
```

```
ensured store 'brand_book' -> table 'chunks_brand_book' exists
granted 'chunks_brand_book' (public) -> SELECT to 'rag_public_reader', write to 'rag_writer'
ensured store 'voice_and_rules' -> table 'chunks_voice_and_rules' exists
granted 'chunks_voice_and_rules' (internal) -> SELECT to 'rag_internal_reader', write to 'rag_writer'
```

That second line is the security model: the public role is granted the public
table **and simply never mentioned** in the internal one.

### 2.5 Ingest the content

```bash
# Linux · macOS · Windows (identical)
uv run python -m rag.reindex
```

```
brand_book: loaded from Knowledge_Base/public/
  files ingested: 1
  files skipped: 0
  total chunk rows written: N
  orphaned chunks deleted: 0
voice_and_rules: loaded from Knowledge_Base/internal/
  ...
```

The first run downloads and loads `BAAI/bge-m3` (~4.3 GB cached, ~2 GB RAM while
embedding, 85 ms per text on a 6-core laptop). Later runs reuse the cache.

### 2.6 Check it, then use it

```bash
# Linux · macOS · Windows (identical)
uv run python -m rag.verify_stores        # config vs real grants -> "OK: 2 store(s) verified"
uv run python -m rag.stress_test --quick  # content, retrieval, isolation, robustness
uv run uvicorn rag.webui:app --port 8010 --host 127.0.0.1
```

Open <http://127.0.0.1:8010>, pick a store, search. That is the whole setup.
(The page takes 30–60 s to answer while the embedding model loads — §6.8.)

And if you would rather look at the raw tables than at the search page, connect
a database GUI to the container — host `localhost`, port **5434**, database
`dhaka_kacchi_rag`: §7 has the credentials to use and the DBeaver steps.

---

## 3. Adding a store (the main how-to)

The worked example: suppose `brain/` grows a `menu-and-allergens.md` that the
customer chatbot should be able to answer from. It is public content, and it is
a *different subject* from the brand book, so it gets its own store — a store
per subject is what lets an agent choose where to look.

### 3.0 When to add a store (and when not to)

Add one when **a different audience must or must not read it**, or when its
content is a genuinely different subject an agent should be able to pick.

Do **not** add one for: a new file on the same subject (drop it in the existing
folder — that is the whole point of folders), or data that lives in a database
(query it — see §6.4), or values that change (prices, deadlines — §6.4).

### 3.1 Put the content in a folder

**Linux · macOS**

```bash
mkdir -p rag/Knowledge_Base/public          # or internal/
# ...and put menu-and-allergens.md in it (any editor, or: cp source.md rag/Knowledge_Base/public/)
```

**Windows (PowerShell)** — `-p` is bash; PowerShell spells it `-Force`:

```powershell
New-Item -ItemType Directory -Force rag/Knowledge_Base/public   # or internal/
# ...and put menu-and-allergens.md in it (any editor, or: Copy-Item source.md rag/Knowledge_Base/public/)
```

Nothing else decides visibility. A file in `public/` is readable by a customer
chatbot; the same file in `internal/` is not. You can audit the boundary with a
directory listing:

```
Knowledge_Base/public/    -> whatever is listed here is public
Knowledge_Base/internal/  -> whatever is listed here is not
```

```bash
# Linux · macOS
ls rag/Knowledge_Base/public rag/Knowledge_Base/internal
```

```powershell
# Windows (PowerShell) - 'ls' is an alias for Get-ChildItem and works too
Get-ChildItem rag/Knowledge_Base/public, rag/Knowledge_Base/internal
```

If the new store's content cannot be expressed as "a folder of files", you will
also need a loader (see §3.5). A folder of `.md`/`.txt`/`.pdf`/`.htm` needs no
code at all.

### 3.2 Describe it in `rag/stores.toml`

```toml
[stores.menu_and_allergens]
table              = "chunks_menu_and_allergens"
visibility         = "public"                 # or "internal"
source_dir         = "public"                 # folder under Knowledge_Base/
chunk_size_tokens  = 300
overlap_tokens     = 60
description        = "The current menu and the official allergen list, item by item. Use this when a customer asks what is in a dish or whether it contains an allergen."
```

Rules the loader will enforce for you (a bad value stops the process at startup,
by name):

* `table` must be a plain lowercase identifier, and **no two stores may claim
  the same table** (two names on one table would give the same rows two
  different visibility tiers).
* `visibility` must be exactly `public` or `internal`.
* `source_dir` must be a relative path with no `..` and no leading `/`.
* `overlap_tokens` must be smaller than `chunk_size_tokens`.
* `description` is **prompt text**, not documentation: an agent reads it while
  choosing where to search. Write it as "what questions does this store answer".

> **Two stores may share a `source_dir`** (e.g. a public and an internal mirror
> of the same notes). Both then embed the same files, each into its own table —
> legitimate, but it doubles the embedding work, so do it deliberately.

#### Choosing the chunk sizes

Not a guess: count the real tokens first, with the model that will embed them.
This snippet is the same in both shells (it contains no `$`, which is the only
character PowerShell would rewrite inside the double quotes — §0):

```bash
# Linux · macOS · Windows (identical as written)
uv run python -c "
from transformers import AutoTokenizer
from pathlib import Path
tok = AutoTokenizer.from_pretrained('BAAI/bge-m3')
t = Path('rag/Knowledge_Base/public/menu-and-allergens.md').read_text(encoding='utf-8')
print(len(tok(t, add_special_tokens=False)['input_ids']), 'tokens')"
```

<details>
<summary>Prefer a file to a one-liner? (identical on every OS)</summary>

Save this as `snippet.py` in the repo root, then `uv run python snippet.py`:

```python
from pathlib import Path
from transformers import AutoTokenizer

tok = AutoTokenizer.from_pretrained("BAAI/bge-m3")
text = Path("rag/Knowledge_Base/public/menu-and-allergens.md").read_text(encoding="utf-8")
print(len(tok(text, add_special_tokens=False)["input_ids"]), "tokens")
```

</details>

Markdown is ingested **one chunk-unit per `##` section** (`load_files.py`), so
compare the section sizes, not the file size. If most sections are under your
`chunk_size_tokens`, most sections become exactly one chunk — which is the
outcome you want. The two existing stores use 300/60 (a ~190-token median per
section).

### 3.3 Create the table and the grants

```bash
# Linux · macOS · Windows (identical)
uv run python -m rag.schema
```

```
ensured store 'menu_and_allergens' -> table 'chunks_menu_and_allergens' exists
granted 'chunks_menu_and_allergens' (public) -> SELECT to 'rag_public_reader', write to 'rag_writer'
```

That is all the enforcement there is — and all it needs to be.

### 3.4 Ingest it, and confirm the boundary

```bash
# Linux · macOS · Windows (identical)
uv run python -m rag.load_files menu_and_allergens     # just this store
uv run python -m rag.verify_stores                     # config vs grants
uv run python -m rag.reindex                           # or all stores at once
```

### 3.5 Only if the content is NOT a folder of files

`load_files.py` reads markdown, text, PDF and HTML. For anything else — a
database export, an API, a spreadsheet — write a loader module that exposes the
same two functions every loader exposes:

```python
def load_store(store: Store, *, registry: StoreRegistry | None = None) -> dict: ...
def prune_orphaned(store: Store) -> int: ...
```

then register it in `rag/reindex.py`:

```python
_LOADER_MODULES = {
    ...,
    "menu_and_allergens": my_new_loader,
}
```

`load_store()` must return a dict containing `total_chunks_written`, and both
functions must use `ingest.ingest_source(store=store.name, registry=registry, ...)`
for writes — that is what keeps the upsert key, the metadata shape and the
prune's keep-list consistent with everything else.

### 3.6 Nothing else changes

Adding a store does **not** require: a new role (roles follow audiences, not
tables), a new credential, a new endpoint, or a UI change. The web UI's store
picker, `list_stores()`, and the file-serving endpoint all read the registry and
the grants.

If you want the new store visible to agents on their menu, and the agent
interface enumerates stores in a tool description (design §8), **restart the
agent process** — menus are built at startup.

---

## 4. Removing a store

Four steps, in this order. The example is what was actually done to
`social_share` on 2026-10-09.

1. **Unregister it** — delete its `[stores.x]` block from `rag/stores.toml`.
   From here on nothing can search it (retrieval only resolves tables through
   the registry), but its table still exists and `verify_stores.py` will now
   list it under NOTES.
2. **Unwire its loader** — remove its line from `_LOADER_MODULES` in
   `rag/reindex.py` if you want the module gone too. Leaving the line is
   harmless: reindex only walks registered stores.
3. **Drop the table, if the data is not wanted** (this is irreversible):

   ```bash
   # Linux · macOS · Windows (identical - one line on purpose, see §0)
   docker exec -it dhaka-kacchi-rag psql -U postgres -d dhaka_kacchi_rag -c "DROP TABLE chunks_social_share;"
   ```

   **Or keep it as an archive** — `chunks_knowledge_base` (2,721 rows of the
   old test-documents corpus) was kept exactly this way: unregistered, unused,
   and reported by `verify_stores.py` on every run so it cannot be forgotten.
   Nothing can reach it, and re-registering is one TOML block.
4. **Confirm**: `uv run python -m rag.verify_stores` — the store is gone from
   the verified list, and the table (if kept) appears under NOTES.

---

## 5. Testing

There are four levels. Run them in this order when something feels wrong.

### 5.1 Does the config match the database?

```bash
# Linux · macOS · Windows (identical)
uv run python -m rag.verify_stores        # exit 0 = agree, 1 = drift
```

```
OK: 2 store(s) verified against the real grants
  - brand_book (public) -> chunks_brand_book
  - voice_and_rules (internal) -> chunks_voice_and_rules
```

It fails loudly on the things that matter: a store declared public that the
public role *cannot* read, a store declared internal that it *can*
(`INTERNAL DATA IS EXPOSED`), a reader that cannot reach a store it should, a
writer missing INSERT/UPDATE/DELETE. Run it after any change to `stores.toml`,
`schema.py`, or a grant made by hand.

### 5.2 Does the whole system work?

```bash
# Linux · macOS · Windows (identical)
uv run python -m rag.stress_test            # ~1-2 min; --quick skips write+load
```

Six sections, each answering a question someone will actually ask:

| § | Checks | What a failure means |
|---|---|---|
| 1 Content | rows exist; one embedding width; every `source_path` under the store's own folder; no empty chunks | the ingestion never ran, or ran into the wrong table |
| 2 Retrieval | 14 golden questions must return a specific phrase from the right store; public results must never contain internal-only text; neighbour context stays inside its store | chunking/settings problem, or cross-store contamination |
| 3 **Isolation** | the public menu omits internal stores; `retrieve()` refuses them; **with every app check bypassed, raw SQL as the public role is still refused by Postgres**; a forbidden store and a nonexistent one give the same error *shape* (only the caller's own string differs) and the refusal names neither the table nor "permission"; the file endpoint's store check | the security model is broken — treat as urgent, not as a test failure |
| 4 Robustness | hostile store names (a table name, `'; DROP TABLE`, `""`, wrong case, `../`) are rejected *and the tables survive*; empty/whitespace/stopword-only/very long/Bengali/SQL-shaped/emoji queries do not crash | an input path is unguarded |
| 5 Maintenance | re-ingesting the same source writes the same rows; a planted synthetic orphan is deleted by the prune and **nothing else is** | a prune whose keep-list has drifted deletes real data — the worst failure this subsystem can have |
| 6 Load | 40 searches across 4 threads, timed, with p50/p95 reported | a connection or lock problem; the numbers are for comparison, not a gate |

A seventh section is opt-in — `--scale 500` (or any number) writes that many
synthetic chunks into the public store, times retrieval at that size, prints the
query plan the database chose, and removes every row it wrote before returning:

```bash
# Linux · macOS · Windows (identical)
uv run python -m rag.stress_test --quick --scale 200   # ~4 min, mostly embedding
```

Its purpose is to keep the "no vector index needed yet" decision honest with a
measurement instead of an assumption — at 156 chunks the plan is
`Seq Scan ... cost=0.00..37.70`, i.e. the whole store costs nothing to scan,
and the query's cost is the embedding, not the database.

Exit code 0 = everything passed. Section 3c is the one to run after *any*
change to `schema.py`, `bootstrap_db.py`, or the grants — it is the design's
acceptance test, not a smoke test.

### 5.3 By hand, in SQL (what "structural" actually means)

Run as the **public** role against the **internal** table — the thing a public
chatbot would do if it were talked into it:

```bash
# Linux · macOS · Windows (identical - one line, no continuation)
docker exec -it dhaka-kacchi-rag psql -U postgres -d dhaka_kacchi_rag -c "SET ROLE rag_public_reader; SELECT count(*) FROM chunks_voice_and_rules;"
```

```
ERROR:  permission denied for table chunks_voice_and_rules
```

And the same role against the public table:

```bash
# Linux · macOS · Windows (identical)
docker exec -it dhaka-kacchi-rag psql -U postgres -d dhaka_kacchi_rag -c "SET ROLE rag_public_reader; SELECT count(*) FROM chunks_brand_book;"
```

To see exactly what a role holds (identical in both shells — the SQL contains no
`$`, so PowerShell leaves it alone):

```bash
# Linux · macOS · Windows (identical as written)
docker exec -it dhaka-kacchi-rag psql -U postgres -d dhaka_kacchi_rag -c "
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_name LIKE 'chunks%' AND grantee LIKE 'rag%'
ORDER BY table_name, grantee;"
```

An interactive shell instead of one-shot queries:

```bash
# Linux · macOS · Windows (identical)
docker exec -it dhaka-kacchi-rag psql -U postgres -d dhaka_kacchi_rag
# then, at the psql prompt:  \dt   ,   \du   ,   \q to leave
```

The same checks by clicking instead of typing — including the one that shows the
isolation in the table tree itself — are in **§7** (DBeaver and friends).

### 5.4 By hand, from Python

```bash
# Linux · macOS · Windows (identical as written - no $ inside the quotes)
uv run python -c "
from rag.config import load_store_registry, load_rag_public_reader_settings, load_rag_internal_reader_settings
from rag.retrieval import list_stores, retrieve
reg = load_store_registry()
pub, internal = load_rag_public_reader_settings(), load_rag_internal_reader_settings()
print('public sees  :', [s['store'] for s in list_stores(registry=reg, settings=pub)])
print('internal sees:', [s['store'] for s in list_stores(registry=reg, settings=internal)])
for r in retrieve('what is kacchi biryani?', store='brand_book', top_k=3, registry=reg, settings=pub):
    print(' -', r['source_path'], '|', r['metadata'].get('heading'), '|', r['chunk_text'][:60].replace(chr(10),' '))
"
```

```
public sees  : ['brand_book']
internal sees: ['brand_book', 'voice_and_rules']
 - public/brand-book.md::frequently-asked-questions | Frequently asked questions | **What is kacchi biryani?** ...
```

### 5.5 In the browser

```bash
# Linux · macOS · Windows (identical)
uv run uvicorn rag.webui:app --port 8010 --host 127.0.0.1
```

Stop it with **Ctrl-C** in that terminal — the same keystroke in both shells.
(If you lost the terminal: §6.7 shows how to find and kill just that process on
each OS.)

Search both stores, expand a result to see its heading badge, its neighbour
context, and "open document" in the side panel. The **re-index** button runs
`rag.reindex` in the background and polls for the result — with two small
markdown stores that is a matter of seconds; it used to be ~40 minutes when the
test corpora were registered.

---

## 6. What breaks, and what it means

### 6.1 `connection refused` on port 5434

Docker Desktop is not running (or the container is stopped). Neither is a
wrong-port problem, however much it looks like one.

```bash
# Linux · macOS · Windows (identical)
docker start dhaka-kacchi-rag                                # no-op if it is already up
docker inspect dhaka-kacchi-rag --format '{{.HostConfig.RestartPolicy.Name}}'
docker update --restart unless-stopped dhaka-kacchi-rag     # only if that printed 'no'
```

On Windows, "Docker Desktop is not running" usually means the desktop app itself
was never started after a login — `docker start` will then fail with a pipe
error (`open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file
specified`). Start Docker Desktop from the Start menu, wait for the whale icon
to stop animating, then re-run the two commands above.

A first search after this may hang for a few seconds on a dead socket rather
than failing fast — there is no connect timeout configured (known gap, see
§8).

### 6.2 The container dies during a long ingestion

Observed twice, during the big test corpora: the container exits silently under
memory pressure from a concurrent heavy job on the same machine. The loaders are
upsert-based and commit per source, so a crash loses at most partial progress:

```bash
# Linux · macOS · Windows (identical)
docker start dhaka-kacchi-rag
uv run python -m rag.reindex          # idempotent: picks up where it stopped
```

### 6.3 `store ... has no source_dir in rag/stores.toml`

You added a store that `load_files.py` is expected to fill, but did not say
which folder it reads. Add `source_dir = "public"` (etc.) to its block.

### 6.4 Why `facts.yaml` is not searchable (and must not be)

`Knowledge_Base/facts.yaml` holds prices, deadlines, delivery fees and the
pickup point. It sits **outside every store folder**, so no store can pick it
up, and `load_files.py` raises by name if a `.yaml`/`.yml`/`.json` file ever
appears inside one.

The reason is the general rule this subsystem follows: **structured values that
change do not belong in a vector store.** An embedding cannot be updated when a
price changes — it can only be re-embedded — so a retrieved price is a
confidently-quoted stale price. That is worse than no answer. Agents read those
values through a live lookup instead.

The same reasoning removed the `social_share` store: that corpus *is* a
database, and SQL answers "how many likes did the Eid posts get" exactly.

### 6.5 A store went empty after a re-index

The prune deleted rows whose `source_path` the loader did not recognise. This is
the failure mode `load_files.py` is built to make impossible (`_units_for_file`
is the single place that decides what a file becomes, used by both the loader
and the prune), and section 5 of the stress test exists to catch it.

Recovery: `uv run python -m rag.reindex` re-embeds and rewrites everything.
Diagnosis: compare what the loader would write with what is stored —

```bash
# Linux · macOS · Windows (identical as written - no $ inside the quotes)
uv run python -c "
from rag.config import load_store_registry
from rag import load_files
store = load_store_registry().get('brand_book')
print(sorted(load_files._expected_source_paths(store)))"
```

A mismatch here (paths with backslashes, a changed heading slug) is the cause.

### 6.6 `uv run` hangs and prints nothing

`uv run` re-syncs the environment before running, and that sync needs the
network. On a machine that is offline (or in a sandbox with no network), use:

```bash
# Linux · macOS · Windows (identical)
uv run --no-sync python -m rag.reindex
```

If a sync was interrupted half-way, packages can be left partially installed —
the symptom is `ImportError: cannot import name 'AutoTokenizer' from
'transformers' (unknown location)`. Repair it offline from the local cache:

```bash
# Linux · macOS · Windows (identical)
uv sync --offline
```

### 6.7 Segfault (exit code 139) on the first search or embed

**Symptom:** a script that embeds — `rag.stress_test`, `rag.load_files`,
`rag.reindex`, `uvicorn rag.webui:app` — dies with `Segmentation fault`
(exit 139) on its first inference, right after printing
`Loading weights: 100%`. No traceback, because the process is killed by the
signal rather than raising.

**Cause, as far as it has been pinned down (2026-10-09):** another process on
the same machine has already loaded `BAAI/bge-m3` **and run at least one
embedding**. A second process may load the model happily; it crashes on its own
first forward pass. Measured directly:

| other process state | second process's first embed |
|---|---|
| nothing else running | OK |
| model loaded, never inferred | OK |
| model loaded **and inferred** (web UI, or any holder) | **SIGSEGV** |

Seven attempts with a competing inference-done process all crashed (the web UI
idle, the web UI after serving searches, a plain sleeping model-holder, and an
ingestion run); every attempt with no competing model process succeeded,
including the full suite runs. Not memory pressure (14 GB free at the time), not
thread count (`OMP_NUM_THREADS=1` still crashes), and not specific to the web
server. The underlying mechanism is **not established** — the honest description
is "two processes cannot both run bge-m3 inference on this machine", not a
theory about why.

**What to do:** stop the web UI (or any other RAG process) before running a
terminal job. **Ctrl-C** in the terminal running uvicorn is enough in both
shells. If you no longer have that terminal, find and kill *just that* process —
never a blanket "kill all python", which would take your unrelated jobs with it:

**Linux · macOS**

```bash
lsof -nP -iTCP:8010 -sTCP:LISTEN      # -> the PID listening on 8010
kill <PID>
```

**Windows (PowerShell)**

```powershell
Get-NetTCPConnection -LocalPort 8010 -State Listen |
  Select-Object -ExpandProperty OwningProcess      # -> the PID listening on 8010
Stop-Process -Id <PID>
```

Then the job runs normally:

```bash
# Linux · macOS · Windows (identical)
uv run python -m rag.reindex
```

**What it means in practice:**

- The **re-index button inside the web UI is safe** — the job runs in the web
  server's own process, which already has the model.
- Running `rag.reindex` from a terminal while the UI is up is **not** safe.
- `rag.stress_test` should be run with no other RAG process running.
- The real fix, if this ever matters beyond a laptop, is a single embedding
  service that every caller talks to, instead of every process loading its own
  4.3 GB copy of the model. That is the production shape anyway (see §8).

### 6.8 "Hmmm… can't reach this page" at 127.0.0.1:8010

The web server is not running. Nothing keeps it alive across a reboot, a closed
terminal, or a session that stopped it — it is a foreground `uvicorn` process
you start when you want it:

```bash
# Linux · macOS · Windows (identical)
uv run uvicorn rag.webui:app --port 8010 --host 127.0.0.1
```

Give it **30–60 seconds** before the page loads: it loads `BAAI/bge-m3` into
the process at import, and the port does not answer until that finishes. If the
browser is still failing after a minute, check the two things that silently
stop it. The database check is identical everywhere:

```bash
# Linux · macOS · Windows (identical)
docker ps --filter name=dhaka-kacchi-rag          # the database must be Up
```

The port check differs, because PowerShell's `curl` is an alias for
`Invoke-WebRequest` and takes different flags:

**Linux · macOS**

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8010/    # 200 = fine
```

**Windows (PowerShell)** — either call the real curl by its full name (`curl.exe`
ships with Windows 10+), or use the native cmdlet:

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8010/      # 200 = fine
# or, without curl at all:
(Invoke-WebRequest -Uri http://127.0.0.1:8010/ -UseBasicParsing).StatusCode
```

This is a different failure from §6.1: there, the **page** works and the
**search** hangs because the database is down. Here the page itself never
loads.

### 6.9 `INTERNAL DATA IS EXPOSED` from `verify_stores`

Someone granted the public role a grant it should not have — often by hand while
debugging, or by marking a store `public` in `stores.toml` without thinking
about who reads it. Fix the grant (or the config, if the config is what is
wrong):

```bash
# Linux · macOS · Windows (identical - one line, no continuation)
docker exec -it dhaka-kacchi-rag psql -U postgres -d dhaka_kacchi_rag -c "REVOKE SELECT ON chunks_voice_and_rules FROM rag_public_reader;"
```

Then confirm with `uv run python -m rag.verify_stores` (identical on every OS).

Remember the rule from §1: the config declares intent, the grants enforce. When
they disagree, **the grants win** — and this verifier is what makes the
disagreement visible.

---

## 7. Opening the database in a GUI (DBeaver, pgAdmin, TablePlus, DataGrip)

Any client that speaks the PostgreSQL wire protocol works — the tool does not
matter, these values do. They were tested from the Windows host (not from
inside the container), which is exactly the path a GUI takes.

| field | value |
|---|---|
| **Host** | `localhost` (or `127.0.0.1`) |
| **Port** | **`5434`** |
| **Database** | `dhaka_kacchi_rag` (or `postgres`, the maintenance DB) |
| **Username / Password** | a role and its password, from `.env` — see the table below |
| **SSL** | leave on the default (*prefer* or *disable*) |

**Port 5434, not 5432.** The container publishes its internal 5432 as **5434**
on the host:

```bash
# Linux · macOS · Windows (identical)
docker port dhaka-kacchi-rag
```

```
5432/tcp -> 0.0.0.0:5434
```

Port 5432 on this machine is very likely a *different* Postgres (the
warehouse's, or another project's) — connecting there shows you the wrong
database, or nothing at all.

### 7.1 Which credential to connect with

All four passwords are in `.env` (the admin one is the same value the container
was created with in §2.1). Which you pick decides what the GUI can even see:

| Connect as | Tables visible | Rights |
|---|---|---|
| **`rag_internal_reader`** | all three `chunks*` tables | **read-only — the right default for browsing** |
| `rag_public_reader` | **only `chunks_brand_book`** | read-only |
| `postgres` (admin) | everything, every database | anything, including `DROP` |
| `rag_writer` | all three | read **and write** — it is the ingestion credential; don't browse with it |

> **The isolation is visible in the sidebar, and this is the cheapest way to
> see it.** Connected as `rag_public_reader`, `chunks_voice_and_rules` does not
> appear in the table tree at all (Postgres hides tables a role has no
> privilege on), and typing `SELECT * FROM chunks_voice_and_rules;` is refused
> with `permission denied for table chunks_voice_and_rules`. Same server, same
> database, same tables — a different credential. That is §1's rule, without
> running any of our code.

### 7.2 Step by step in DBeaver

1. **Database → New Database Connection → PostgreSQL → Next.**
2. Fill in the five fields above. The first time, DBeaver offers to download
   the PostgreSQL **JDBC driver** — accept it.
3. Leave the **SSL** tab alone (default *prefer*). This container runs with
   `ssl = off`; choosing *require* makes the connection fail.
4. **Test Connection** → *Connected* → **Finish**.
5. Expand: `dhaka_kacchi_rag` → *Schemas* → `public` → *Tables* →
   `chunks_brand_book` → right-click → **View Data**.

pgAdmin, TablePlus, DataGrip, HeidiSQL: identical values, same five fields.

### 7.3 Reading the tables once you are in

```sql
-- The most useful view of a store, in one query.
SELECT source_path, chunk_index, metadata->>'heading' AS heading, left(chunk_text, 80) AS preview
FROM chunks_brand_book
ORDER BY source_path, chunk_index;

-- How wide are the vectors, and how many rows per store?
SELECT vector_dims(embedding) AS dimensions, count(*) FROM chunks_brand_book GROUP BY 1;

-- Who can read what (the same question verify_stores.py asks).
SELECT grantee, table_name, privilege_type
FROM information_schema.role_table_grants
WHERE table_name LIKE 'chunks%' AND grantee LIKE 'rag%'
ORDER BY table_name, grantee;
```

Two things that look like problems but are not:

* **`embedding` is a `vector(1024)`.** Most GUIs have never heard of pgvector's
  type and render the column as a long list of floats (`[0.013,-0.021,…]`) or
  as an opaque value. That is the vector itself. Select around it (as above) to
  keep the grid readable, or cast it: `left(embedding::text, 40)`.
* **`metadata` is `jsonb`** — in DBeaver it opens as a tree you can expand,
  which is the pleasant way to read a chunk's heading/page number.

### 7.4 If the connection fails

| Symptom | Cause |
|---|---|
| connection refused / timeout | Docker Desktop is not running, or the container is stopped → §6.1 |
| `FATAL: password authentication failed` | wrong password for that role — copy it from `.env`. A reader password that has been lost can only be **reset**, never recovered (§2.3) |
| `FATAL: database "…" does not exist` | typo; the two that exist are `dhaka_kacchi_rag` and `postgres` |
| connects, but you see none of the vector tables | you are on the wrong port (5432) or in the wrong database — check both |
| SSL / `server does not support SSL connections` | SSL is off in this container; set the connection's SSL mode to *disable* or *prefer* |

### 7.5 Two things about this container worth knowing

* **It is published on every network interface, not just localhost**
  (`0.0.0.0:5434` above), and its admin password is the placeholder from §2.1.
  Anyone on the same network who knows it can read the whole database. Fine on
  a trusted home network; do not leave it running on public wifi, and do not
  reuse that password anywhere real.
* **Its data lives in an anonymous Docker volume**
  (`/var/lib/postgresql/data`), so it survives `docker rm` — but *not*
  `docker volume prune`. Re-ingesting the brand stores takes seconds, but
  `chunks_knowledge_base` (the 2,721-row archive) was never re-ingestable from
  this repo unless `Knowledge_Base/documents/` is still on disk. Don't prune
  volumes while this container exists.

### 7.6 If you actually wanted the *other* database

The remote `social_share` data product is **not** in this container: it is
reached over an SSH tunnel on port **5433** with a read-only credential, and it
is documented in `rag/DataBase_Access.txt`. Same idea in DBeaver — different
host, port and user.

---

## 8. Known gaps (deliberate, at prototype stage)

* **One model-using process at a time** — see §6.7. Two processes that both run
  bge-m3 inference crash the second one. The fix is an embedding service; until
  then, stop the web UI before running a terminal job.
* **No connect timeout** on the reader/writer engines: a dead database hangs a
  request instead of failing in a few seconds.
* **No vector index** (IVFFlat/HNSW): a sequential scan over a few dozen rows is
  faster than an index. Revisit at ~100k chunks.
* **Re-index progress is coarse**: "started HH:MM", because the loaders report
  nothing per file.
* **Re-index state lives in server memory**: restarting the web server mid-run
  kills the job (re-run it — everything is idempotent).
* **`load_files.py` cannot see a file that got shorter** without its heading
  changing (the prune's known limitation, decision #29).
* **No web-UI authentication**: it is a local single-user tool, and the
  credential it runs with (internal) is its only access control.

---

## 9. Cheat sheet

**Every line below is identical on Linux, macOS and Windows (PowerShell)** —
copy any of them as they are. The only commands in this runbook that differ by
OS are listed in §0, and they appear with both spellings where they are used
(`mkdir`, `curl`, process-killing, and anything that would have been written
with a `\` continuation).

```bash
# --- setup (once) -------------------------------------------------------
docker run -d --name dhaka-kacchi-rag -e POSTGRES_PASSWORD=... -p 5434:5432 pgvector/pgvector:pg16
uv run python -m rag.bootstrap_db        # database + extension + 3 roles
uv run python -m rag.schema              # tables + grants, from stores.toml

# --- everyday -----------------------------------------------------------
docker start dhaka-kacchi-rag            # harmless if already up (see §2.1)
uv run python -m rag.reindex             # re-embed every store from disk
uv run python -m rag.reindex brand_book  # ...or just one store
uv run python -m rag.load_files brand_book
uv run uvicorn rag.webui:app --port 8010 --host 127.0.0.1

# --- checking -----------------------------------------------------------
uv run python -m rag.verify_stores       # config vs grants (exit 1 = drift)
uv run python -m rag.stress_test         # full suite
uv run python -m rag.stress_test --quick # ...without the write + load sections

# --- inspecting ---------------------------------------------------------
docker exec -it dhaka-kacchi-rag psql -U postgres -d dhaka_kacchi_rag
  \dt                                     -- tables
  SELECT source_path, chunk_index, metadata->>'heading' FROM chunks_brand_book ORDER BY source_path, chunk_index;
  SELECT grantee, table_name, privilege_type FROM information_schema.role_table_grants WHERE grantee LIKE 'rag%';
  \q                                      -- leave
```

The same commands as `make` targets, for anyone who prefers them (the repo
root's `Makefile`; `make help` lists everything). `make` is not installed with
Git Bash on Windows by default — the `uv run …` form above always works:

```bash
make rag-verify        # = uv run python -m rag.verify_stores
make rag-test          # = uv run python -m rag.stress_test --quick
make rag-test-full     # = uv run python -m rag.stress_test
make rag-schema        # = uv run python -m rag.schema
make rag-reindex       # = uv run python -m rag.reindex
make rag-ui            # = uv run uvicorn rag.webui:app --port 8010 --host 127.0.0.1
```

**The one habit worth having:** after any change to `stores.toml`, `schema.py`
or a grant, run `verify_stores` then `stress_test`. Together they cover the two
things that go wrong quietly — config drifting from grants, and a prune
disagreeing with its loader.
