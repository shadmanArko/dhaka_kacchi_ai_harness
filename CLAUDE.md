# Working context — Dhaka Kacchi AI harness

`ARCHITECTURE.md` is the map. This file is the set of things that will bite you.

## Layout

`warehouse/` is Layer 0. `migrations/` is the Alembic chain, `config.py` is the ONE
place that reads `os.environ`, `verify.py` enforces schema conventions, `gate.py`
runs the §9 Phase-1 exit gate. `tools/ agents/ control/ brain/ ops/` are stubs per
Appendix A.

Run `make help`. Common: `make upgrade`, `make verify`, `make gate`, `make reset`.

## Schema conventions (enforced by `make verify`, not by convention alone)

- `text` unconditionally, never `varchar(n)`.
- `timestamptz` always, never bare `timestamp`. `ad_spend.date` is a `date`.
- `text` + `CHECK`, **never** native Postgres `ENUM` — an enum value can't be
  dropped, and channels/statuses churn constantly in year one.
- Money: `numeric(12,2)` order-level, `numeric(14,4)` per-unit. Never float, never
  integer cents. **This departs from the sibling TS services**, which use integer
  cents guarded by `money.ts`. Reason: COGS is price × qty ÷ yield_factor, e.g.
  bulk spices €18/kg × 0.008kg = €0.144 — integer cents rounds away the exact
  quantity the margin model exists to compute. Ingest converts cents → numeric.
- `uuid` PKs via `gen_random_uuid()` (native in PG13+), plus a natural-key `slug`
  UNIQUE on `menu_item` / `ingredient` / `supplier`.
- Names: `idx_` `uq_` `pk_` `fk_` `ck_` `excl_`. Primary keys are named explicitly;
  Postgres' `<table>_pkey` default is the one thing that would break the pattern.

## Traps

**`orders` is plural, and it is the only plural table.** `order` is reserved:
`CREATE TABLE order` is a syntax error. `"order"` works but needs quotes in every
query forever — including the SQL the `warehouse` MCP server hands to agents, which
would fail every time an LLM writes `FROM order`.

**Point-in-time costs.** `order_line.unit_cogs_at_time` is written at order time and
never recomputed. NEVER join to `ingredient.current_price` for historical margin —
when basmati moves 20%, last quarter's P&L must not silently change. `make gate`
asserts the query plan does not touch `ingredient`.

**VAT is deliberately absent.** Kleinunternehmer (§19 UStG), so `gross` is full
revenue. **Revisit at the threshold**: German takeaway food is 7% and drinks 19%, so
a kacchi + borhani basket is mixed-rate and needs `order_line.vat_rate` — an
order-level column cannot represent it. Until then every margin figure would be
optimistic by 7–19%.

**Seed prices and recipe quantities are placeholders.** Only menu *prices* are real
(verbatim from `dhaka-kacchi-connect/worker/src/data.ts`, the stated source of
truth). Ingredient costs carry `{"source": "placeholder"}` in `price_history`.
`salad` and `chutney` have no ingredient rows yet, so per-portion COGS is understated.

**Allergen data originates in `brain/menu/items/<slug>.md`** and flows *into* the
warehouse, never the reverse (§4.3 rule 1). `spice_mix_bulk` has empty allergen codes
*pending verification* — commercial garam masala often contains mustard and celery.

**The meat is mutton/lamb, not beef.** The `20 kg beef` line in ARCHITECTURE.md §5
is illustrative mockup text in a sample JSON payload.

## Alembic traps

- **`alembic upgrade head` twice proves nothing** — the version table short-circuits
  it. `make verify-idempotent` is the real test: it clears `alembic_version` and
  replays the whole chain over a live, populated schema.
- **It will NOT replay an already-applied seed** however idempotent the body is.
  After editing `0012_seed.py`, run `make reseed`.
- **Declare PK/FK/UNIQUE/CHECK inline in `create_table`.** Inline constraints ride
  the table's own `IF NOT EXISTS`. Postgres has no `ADD CONSTRAINT IF NOT EXISTS`,
  so a follow-up `ALTER` has no guard at all. Use `replace_constraint()` only where
  inline is impossible (e.g. the `recipe` EXCLUDE) — it drop-then-adds, which also
  *converges* a wrong-definition constraint rather than skipping it.
- **`IF NOT EXISTS` is existence-idempotent, not definition-convergent.** A
  wrong-shaped existing table is skipped silently. `make verify` is the only thing
  that catches that.
- **Offline mode is a hard constraint, not a nicety.** Helpers must never call
  `sa.inspect(op.get_bind())` — there is no connection in `--sql` mode. Seed values
  need literal renderers: pass JSONB/array values through `_jsonb()` / `_text_array()`
  and numerics as `Decimal`, or `make sql` breaks. `make verify-idempotent` step 4
  catches it.
- **`op.create_table` silently drops attached `sa.Index` and `Column(index=True)`.**
  Every index needs its own `create_index_if_absent`.
- **`op.execute(str)` wraps in `sa.text()`**, which parses `:word` as a bind param.
  Keep bare colons out of constraint bodies (`::` casts are fine).
- **`--autogenerate` and `alembic check` are unavailable** — `warehouse/models/` holds
  SQL views, not ORM classes, so `target_metadata = None`. Don't "fix" this by
  importing half-built models into `env.py`. `make verify` replaces it.
- **Hand-assigned `0001`-style revision ids only work on a linear chain.** Two authors
  would both grab the next ordinal. `make verify` asserts exactly one head. If the
  repo ever grows parallel branches, switch new revisions to Alembic's hex ids and
  use `alembic merge`; `0001`–`0012` keep working.
- **Never set `sqlalchemy.url` in `alembic.ini`** — ConfigParser `%`-interpolates
  values, so a password containing `%` breaks. The engine is built in `env.py`.
- **`transaction_per_migration=True`** — without it the entire chain is one
  transaction, so a failure at 0009 rolls back 0001–0008 and records nothing.

## Not built yet (deliberately)

`payout_line`, `staff_shift`, `creator_collab` — §5 entities not needed for the
Phase-1 margin gate. `payout_line` matters most: until it exists, `orders.channel_fee`
is a *trusted* aggregator number, which is exactly what §5 says never to trust.
