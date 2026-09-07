# Reference queries

Hand-written, read-only SQL for exploring and operating the warehouse. Not run by
any application code and not part of the migration chain — this is a reference
library for humans (and for agents reading the warehouse via the MCP server's
allowlisted SQL).

**Distinction from `warehouse/models/`:** `models/` holds SQL *views* — objects
created inside the database that other code depends on. This directory holds
*queries* — snippets you run ad hoc in `psql` or DataGrip. A query graduates to
`models/` only once something depends on it existing as a named object in the
database, not just as a script in this folder.

## Files

| File | Purpose |
|---|---|
| [`01_exploration.sql`](01_exploration.sql) | Orientation: what tables exist, how many rows, what's seeded |
| [`02_margin_and_cogs.sql`](02_margin_and_cogs.sql) | Recipe → COGS → margin, and the ARCHITECTURE.md §9 exit gate |
| [`03_operations.sql`](03_operations.sql) | Day-to-day: recent orders, open cockpit alerts, pending approvals |
| [`04_schema_inspection.sql`](04_schema_inspection.sql) | Introspect columns, constraints, and indexes on any table |

## Running them

**In DataGrip:** open the file with the `dhaka_kacchi` data source selected, place
the cursor in a query, `Cmd+Enter` (or the run-statement shortcut) to execute just
that statement — most files hold several independent queries under one heading.

**From the shell:**
```bash
psql -d dhaka_kacchi -f warehouse/queries/02_margin_and_cogs.sql
```

**Via the project's own accessor** (respects `DATABASE_URL`, works from any cwd):
```bash
make psql < warehouse/queries/01_exploration.sql
```

## Conventions

- Every query is preceded by a one-line comment naming what it answers.
- Table names, not aliases, in every `WHERE`/`ORDER BY` a first-time reader would
  scan for — clarity over brevity.
- `orders` is the deliberately-plural table (`order` is a reserved word); nothing
  here needs to quote it.
- Placeholders to substitute are UPPER_SNAKE, e.g. `:TABLE_NAME`.
- Nothing in this directory writes. If a query needs write access to be useful
  (an ad hoc correction, a manual upsert), it belongs in an operator runbook, not
  here.
