# Direct-channel order ingestion (D1 → warehouse)

## Context

The warehouse spine (11 tables, migrated + seeded) is built and verified, but every
operational table is empty — `make gate` runs correctly but has nothing to compute
margin from. Per `ARCHITECTURE.md` §9, the next Phase-1 step is ingestion: "one
idempotent Python job per source... land raw into a `raw_*` table first, transform
second," starting with the ordering backend (§4.1, priority 1, "owned, direct DB
read").

**Research surfaced a blocker that changes the shape of this task.** The ordering
backend (`dhaka-kacchi-connect`, a separate sibling repo) has never been deployed:
`wrangler.toml`'s `database_id` is still the literal placeholder
`"REPLACE_WITH_D1_DATABASE_ID"`, the `worker/` directory isn't even committed to git,
there's no CI/CD, and the only D1 data anywhere is 4 test rows (manual + Playwright)
in local Miniflare dev emulation. There are no real orders to ingest yet.

**Decision (confirmed with the user):** build the job now against the **local** dev
D1, via `npx wrangler d1 execute <db> --local --json --command "..."`, run with `cwd`
set to `worker/`. This is deliberately future-proof — the exact same command works
against a real deployed database by swapping `--local` for `--remote`; nothing else
in the pipeline changes. This was verified empirically, not assumed: the design pass
actually ran this command against the live local D1 and confirmed both the success
JSON shape (`[{"results": [...], "success": true, "meta": {...}}]`) and two distinct
error shapes (`{"error": {"text": "no such table..."}}` and a wrong-binding error),
against 4 real rows (`kacchi_taster`/`kacchi_regular`/`borhani`, 9 line items total).

**Intended outcome:** a re-runnable, idempotent job that lands raw D1 orders, then
transforms them into `orders`/`order_line` with point-in-time COGS — proving the
whole ingestion pattern end-to-end today, so switching to production later is a
one-value config change, not a rebuild.

## Source schema (D1, read-only reference — do not modify `dhaka-kacchi-connect`)

```sql
orders(id TEXT PK 'ord_<uuid>', created_at TEXT ISO8601, delivery_date TEXT 'YYYY-MM-DD',
       station, customer_name, customer_email, customer_phone, notes,
       subtotal_cents INT, payment_method DEFAULT 'cash_on_delivery',
       status DEFAULT 'received' [received|confirmed|delivered|cancelled],
       email_sent, whatsapp_sent)
order_items(id INTEGER PK AUTOINCREMENT, order_id FK, sku, name, unit_price_cents, quantity)
```

Confirmed facts that shape every mapping decision below: `orders.id` is
server-generated (`crypto.randomUUID()`); there is **no `updated_at`** and **no code
path ever mutates `status`** after insert — every order sits at `'received'`
forever today, so only append-style full-refresh extraction is possible (not a gap
to solve now). `sku` already equals warehouse `menu_item.slug` — no mapping needed.
No `discounts`/`channel_fee`/`packaging_cost`/`delivery_cost`/`delivered_at` concept
exists in the source at all; `channel_fee=0` and `delivery_cost=0` are **real facts**
(direct channel has no commission; the source's own comment says all stations get
free delivery), not gaps.

## New migration: `0013_raw_orders_direct.py`

One JSONB blob per order (items nested), upserted by `external_id` — not an
append-only log, because the source is asserted immutable post-insert today, and an
upsert is what makes "re-run with nothing new → identical row counts" provable.
**Source-specific table, not a generalized `raw_orders(source, ...)`** — matches
ARCHITECTURE.md's own `raw_*`-per-source phrasing, and a generalized table's schema
would have to compromise across sources that don't actually look alike (D1 rows vs.
a future Lieferando webhook body). Revisit only if a 4th/5th source makes the
duplication genuinely painful.

```python
create_table_if_absent(
    "raw_orders_direct",
    pk_column(), pk_constraint("raw_orders_direct"),
    sa.Column("external_id", TEXT, nullable=False),   # D1 orders.id, 'ord_<uuid>'
    sa.Column("payload", JSONB, nullable=False),       # verbatim order + nested items[]
    created_at_column(), updated_at_column(),
    sa.UniqueConstraint("external_id", name=uq("raw_orders_direct", "external_id")),
)
```
No `channel` column: this table only ever holds `'direct'` rows by construction, so a
CHECK-constrained single value would be ceremony. Add `"raw_orders_direct"` to
`EXPECTED_TABLES` in `warehouse/verify.py`.

## Config: `warehouse/config.py` — additive, not bolted onto `Settings`

A second, separate dataclass/loader (`DirectSourceSettings` /
`load_direct_source_settings()`), because `Settings`/`load_settings()` is called by
`bootstrap_db.py`/`verify.py`/`gate.py`/`migrations/env.py` — none of which have
anything to do with the ordering backend, and forcing them to require a checked-out
sibling repo just to run `make verify` would be wrong. New env vars, added to
`.env.example`:

```
DHAKA_KACCHI_CONNECT_PATH=../dhaka-kacchi-connect   # default if unset
DHAKA_KACCHI_D1_TARGET=local                        # 'local' or 'remote' — the whole prod switch
```

Fail-fast validation mirrors `load_settings()`: resolve the path, confirm
`worker/wrangler.toml` exists, reject an unrecognized `d1_target` — same
`ConfigError` class, same actionable-message style.

## Extraction — `warehouse/ingest/direct.py`

One query (`orders LEFT JOIN order_items`, `ORDER BY created_at, o.id, oi.id`) via:
```python
["npx", "--yes", "wrangler", "d1", "execute", cfg.d1_database_name,
 f"--{cfg.d1_target}", "--json", "--command", WRANGLER_D1_QUERY]
```
run with `cwd=cfg.worker_dir` (confirmed required — `npx` resolves the *pinned*
`worker/node_modules/wrangler`, not a freshly-fetched one, only when cwd is right),
`timeout=60`. `LEFT JOIN` so an item-less order still surfaces instead of vanishing.

Four distinct, actionable failure modes (not one generic exception): `npx`/node
missing; malformed JSON; `"no such table"` (local D1 never migrated — tells the
operator to run `npm run db:migrate:local`); wrong D1 binding (bad
`DHAKA_KACCHI_CONNECT_PATH`). An empty `results` list is **not** an error — zero
orders is legitimate.

Flat rows are grouped into nested per-order dicts in Python (`line_no` from list
position, not from D1's internal `item_id` autoincrement). This is a **full extract
every run** — no cursor exists at the source to filter on; at this volume that's
fine and it's the upsert layer, not the extraction, that guarantees correctness.

## Transform/load

**Column mapping** (`channel_fee`, `delivery_cost` = `0.00` as real facts;
`customer_id` = `NULL`, identity resolution is out of scope per §5;
`delivered_at` = `NULL` always, source has no such data; `net_margin` = `NULL`,
a separate future recompute job's job):

- `packaging_cost` → a **named placeholder constant**,
  `PLACEHOLDER_PACKAGING_COST_EUR = Decimal("0.50")`, commented the same way the
  `0012` seed flags ingredient prices — a real number, unverified against an actual
  invoice.
- `promised_at` → synthesized from the date-only `delivery_date` at a fixed
  `PROMISED_DELIVERY_TIME_LOCAL = time(14, 0)` **Europe/Berlin**, converted via
  `zoneinfo` (DST-correct, no manual offset math) — also an explicitly documented
  placeholder, since the source has no real delivery-window concept yet.

**Point-in-time COGS** (the part that has to be right):
```sql
SELECT mi.id AS menu_item_id,
       coalesce(sum(r.qty / r.yield_factor * i.current_price), 0) AS unit_cogs_at_time
FROM menu_item mi
LEFT JOIN recipe r ON r.menu_item_id = mi.id
                  AND r.active_from <= :placed_at
                  AND (r.active_to IS NULL OR r.active_to > :placed_at)
LEFT JOIN ingredient i ON i.id = r.ingredient_id
WHERE mi.slug = :sku
GROUP BY mi.id
```
Keyed off each order's own `placed_at`, not "now" — the point-in-time rule this
whole schema was built around. `LEFT JOIN` so a menu item with no recipe yet returns
`0` instead of vanishing (matches the existing, already-documented salad/chutney
recipe gap); **zero rows** (unmatched `sku`, not a cost-data gap) is a hard error
naming the order and sku. Add one `print()` line whenever a resolved cost is exactly
`0` for a menu item that should have ingredients — cheap visibility, matches the
"exception-first monitoring" principle in §2.8, and costs nothing since every
seeded item already has recipe rows so it won't fire under current data.

**Known named limitation, not fixed here:** the join uses `ingredient.current_price`
(there's no point-in-time price history table, only an unstructured
`price_history` JSONB log) — correct *today* only because no ingredient price has
moved since the `0012` seed. The real fix (an `ingredient_price_history` table
mirroring `recipe`'s `active_from`/`active_to` + EXCLUDE pattern) is a Layer-1
schema change that should land before `procurement-inventory` goes live and starts
actually changing prices — not part of this task.

**Idempotent upserts:**
- `orders`: `ON CONFLICT (channel, external_id) DO UPDATE SET status=excluded.status,
  updated_at=excluded.updated_at`, **with** `RETURNING id`. This is a deliberate,
  narrow deviation from `0012`'s "`DO NOTHING` for business events" convention, for
  a mechanical reason: `DO NOTHING ... RETURNING` returns **no row** on a skipped
  conflict, and the transform needs the order's id on every run to attach
  `order_line`. Every other column (`gross`, `placed_at`, `discounts`, cost columns)
  is deliberately excluded from `SET` — a re-run must never silently restate a
  financial fact.
- `order_line`: `ON CONFLICT (order_id, line_no) DO NOTHING` — no deviation, matches
  `0007`'s "immutable historical fact" design intent exactly.

A small shared helper, `warehouse/ingest/upsert.py` (`upsert_returning()`), because
`migrations/helpers.py`'s `upsert()` calls `alembic.op.execute()` and only has
meaning inside a migration — it cannot be imported into application code, and more
`ingest/<source>.py` jobs are coming that will need the same shape.

**Two-phase run**, each its own transaction: (1) extract → land into
`raw_orders_direct`; (2) read raw back from the table (not from step 1's in-memory
objects — keeps the phases genuinely independent) → transform → upsert into
`orders`/`order_line`. Landing must succeed even if a later order's transform hits a
bad sku.

## File layout

```
warehouse/ingest/direct.py          the job — config → extract → land → transform → load
warehouse/ingest/upsert.py          shared upsert_returning() for application code
warehouse/ingest/direct_verify.py   idempotency + COGS correctness proof (see below)
warehouse/migrations/versions/0013_raw_orders_direct.py
```

No `warehouse/ingest/base.py` — one job existing is a sample size of one; the next
source (Google Business Profile, OAuth REST API) shares almost no surface with this
one beyond the upsert helper (already factored out) and the `Settings`/`ConfigError`
idiom (already global). Revisit once a 2nd/3rd source makes it concrete, matching
this repo's own "dbt at ~30 models" bar for when abstraction earns its keep.

`direct.py` takes a `--dry-run` flag: extracts and prints a per-order summary
(external_id, customer, sku×qty, computed gross), touching **no** table, not even
`raw_orders_direct`. When `d1_target == "local"` it also prints an explicit notice
that this will ingest *everything* currently in local dev D1 — including the 4
existing test rows.

**On those 4 test rows: leave them as-is, no filtering logic.** A name/email
heuristic (`LIKE '%@example.com'`) is a miniature, badly-done version of the
identity-resolution problem §5 already calls "the hardest problem in the build" and
defers — the wrong layer to improvise it in. They only exist in local Miniflare
state and will not exist in real deployed D1, so the problem disappears on its own
once this points at production. If it ever needs cleaning up before then, that's a
deliberate one-off `DELETE ... WHERE external_id IN (...)`, not permanent code.

## Makefile targets

```
ingest-direct           # python -m warehouse.ingest.direct
ingest-direct-dry-run   # ... --dry-run
verify-ingest-direct    # python -m warehouse.ingest.direct_verify
```
No `db`/`upgrade` prerequisite — matches `gate:`/`verify:`'s existing precedent
(assumes `make upgrade` already ran), not `upgrade:`'s.

## Verification

`warehouse/ingest/direct_verify.py`, matching `verify.py`/`gate.py`'s exact
script-with-exit-code idiom (this repo uses that pattern everywhere and has zero
pytest usage despite it being a listed dependency — introducing pytest here first
would be new machinery, not consistency):

1. Run the job once; capture `count(*)` on `raw_orders_direct`, `orders WHERE
   channel='direct'`, and `order_line` joined to direct orders.
2. Run it again with nothing changed at the source; assert all three counts are
   **byte-identical** — the idempotency proof.
3. Cross-check: `count(orders WHERE channel='direct')` equals
   `count(DISTINCT external_id) FROM raw_orders_direct`.
4. Hand-computed COGS spot check against the real seeded recipe: any `order_line`
   for `kacchi_taster` must have `unit_cogs_at_time == Decimal("2.5434")` (matches
   the value already verified during the schema build).
5. `make lint` clean, matching every prior migration in this repo.

**Expected, non-bug behavior worth calling out up front:** after this runs, `make
gate` will likely still print "no delivered orders in the previous calendar month" —
the 4 test orders are dated 2026-07-25 (outside the rolling "last month" window as
of today) *and* every one sits at `status='received'`, never `'delivered'`, since
nothing in the source ever transitions it. That's correct behavior on fixture data,
not a broken pipeline. To see a real gate number, either place a fresh order through
the local dev site today, or do a deliberate manual `UPDATE orders SET
status='delivered', delivered_at=... WHERE external_id='...'` as a one-off test.

## Explicitly out of scope

Identity resolution (`customer_id` stays NULL), scheduling/cron, deploying the
ordering backend to production, ingestion for any other source, the
`agent_action`/`cockpit_alert` control plane (no agent framework exists yet — this
is a plain batch job), a normalized `ingredient` price-history table, recomputing
`orders.net_margin`, a shared `ingest/base.py` framework, filtering the 4 test rows.
