-- =============================================================================
-- Schema inspection — introspect what's actually in the database, rather than
-- trusting a migration file or a doc. Useful after any manual change, or when
-- diagnosing a `make verify` failure (warehouse/verify.py runs equivalent
-- checks, programmatically, on every table).
-- =============================================================================


-- Columns, types, nullability and defaults for one table.
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name = 'orders'   -- <- swap table name
ORDER BY ordinal_position;


-- Every constraint on one table, with its full definition.
-- Constraint names follow the house convention: pk_ uq_ fk_ ck_ excl_.
SELECT conname, contype, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'orders'::regclass   -- <- swap table name
ORDER BY contype, conname;


-- Every index on one table, including partial indexes and their WHERE clause.
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
  AND tablename = 'orders'   -- <- swap table name
ORDER BY indexname;


-- All foreign keys in the warehouse, with their ON DELETE behaviour —
-- useful for reasoning about a GDPR erasure or a cascade before running one.
SELECT
    c.relname                                      AS child_table,
    k.conname,
    pg_get_constraintdef(k.oid)                    AS definition
FROM pg_constraint k
JOIN pg_class c ON c.oid = k.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE k.contype = 'f' AND n.nspname = 'public'
ORDER BY child_table;


-- Every CHECK constraint across the whole warehouse in one view — the
-- business rules the database itself enforces, independent of any application
-- code.
SELECT
    c.relname AS table_name,
    k.conname,
    pg_get_constraintdef(k.oid) AS rule
FROM pg_constraint k
JOIN pg_class c ON c.oid = k.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE k.contype = 'c' AND n.nspname = 'public'
ORDER BY table_name, conname;


-- Table sizes — irrelevant at current volume, but the query to reach for once
-- it stops being irrelevant.
SELECT
    relname AS table_name,
    pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC;


-- Which Alembic revision the database is currently at.
SELECT version_num FROM alembic_version;
