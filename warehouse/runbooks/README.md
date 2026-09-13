# Operator runbooks

Hand-run SQL that **writes** to the warehouse database (`dhaka_kacchi`) — ad
hoc corrections, manual data entry, one-off fixes. Not run by any
application code, not part of the migration chain.

This is the counterpart to [`../queries/`](../queries/), which is
deliberately read-only — see that folder's own README for why the split
exists.

## Files

| File | Purpose |
|---|---|
| [`data_entry.sql`](data_entry.sql) | Copy-paste `INSERT`/`UPDATE` examples for every table you'd realistically add data to by hand (suppliers, ingredients, menu items, recipes, ad spend, reviews) |

## Running them

Same as the query library — open in DataGrip with the `dhaka_kacchi` data
source selected, place the cursor in one statement, run just that one
(`Cmd+Enter` or your run-statement shortcut). Don't run a whole file at
once — each block is meant to be copied out, edited with your own values,
and run on its own.
