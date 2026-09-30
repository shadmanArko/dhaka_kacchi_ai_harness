#!/usr/bin/env bash
# Runs ONCE, automatically, the first time the postgres container starts
# against an empty data volume - the official postgres image never re-runs
# anything in /docker-entrypoint-initdb.d/ against existing data. To re-run
# this by hand later, either wipe the volume (destroys all data) or apply
# the SQL below manually with psql.
#
# Creates two databases and five least-privilege roles:
#   ordering_app            - read-write, owns `ordering`   (used by the
#                              ordering backend, dhaka-kacchi-connect/worker)
#   warehouse_app           - read-write, owns `warehouse`  (used by this repo)
#   ordering_reader         - read-only on `ordering`, used ONLY by
#                              warehouse/ingest/direct.py - so that job can
#                              never write into the live ordering database,
#                              structurally, not just by convention.
#   warehouse_reader        - read-only on `warehouse`, the mirror image of
#                              ordering_reader: used ONLY by
#                              dhaka-kacchi-connect's admin reporting page
#                              (worker/src/lib/reportingRepository.ts), so
#                              that page can never write into the warehouse,
#                              structurally.
#   warehouse_cockpit_writer - SELECT + UPDATE on `warehouse.cockpit_alert`
#                              only, used ONLY by dhaka-kacchi-connect's
#                              admin cockpit page's acknowledge/resolve
#                              actions (worker/src/lib/cockpitRepository.ts)
#                              - deliberately a THIRD, narrower role rather
#                              than reusing warehouse_reader for this, so a
#                              bug in the cockpit UI still cannot write into
#                              anything but this one table, and cannot
#                              INSERT/DELETE even there (only
#                              ops/run_detectors.py, as warehouse_app, ever
#                              creates a new alert row). SELECT is required
#                              alongside UPDATE, not optional: an
#                              `UPDATE ... WHERE ...` needs SELECT on the
#                              WHERE-clause columns to evaluate the filter,
#                              which UPDATE alone does not grant (learned the
#                              hard way - see git log around 2026-09-23).
#   social_share_writer      - INSERT/UPDATE/DELETE (via TRUNCATE) on
#                              `social_share.social_post_metrics` only, used
#                              ONLY by ops/refresh_social_share.py.
#   social_share_reader      - SELECT-only on that same table, in that same
#                              database - the ONLY credential ever handed to
#                              an outside collaborator (see deploy/CLAUDE.md's
#                              "Social share database" section). A SEPARATE
#                              database from `warehouse`, not just a
#                              narrower role inside it: a leaked reader
#                              password or a sloppy query can only ever see
#                              this one derived table, never anything in the
#                              same database as real order/customer data.
#
# This script CANNOT grant SELECT/UPDATE ON cockpit_alert here - the table
# doesn't exist yet at first-init time (this repo's own `alembic upgrade
# head`, run as warehouse_app, creates it later). See deploy/CLAUDE.md's
# "First-time setup" step that runs right after the schema migration for
# the one-off GRANT this role still needs.
#
# Passwords come from environment variables set in deploy/.env - never
# hardcoded here. See deploy/CLAUDE.md for the full list of required vars.
set -euo pipefail

: "${ORDERING_APP_PASSWORD:?ORDERING_APP_PASSWORD must be set}"
: "${WAREHOUSE_APP_PASSWORD:?WAREHOUSE_APP_PASSWORD must be set}"
: "${ORDERING_READER_PASSWORD:?ORDERING_READER_PASSWORD must be set}"
: "${WAREHOUSE_READER_PASSWORD:?WAREHOUSE_READER_PASSWORD must be set}"
: "${WAREHOUSE_COCKPIT_WRITER_PASSWORD:?WAREHOUSE_COCKPIT_WRITER_PASSWORD must be set}"
: "${SOCIAL_SHARE_WRITER_PASSWORD:?SOCIAL_SHARE_WRITER_PASSWORD must be set}"
: "${SOCIAL_SHARE_READER_PASSWORD:?SOCIAL_SHARE_READER_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE ordering_app LOGIN PASSWORD '$ORDERING_APP_PASSWORD';
    CREATE ROLE warehouse_app LOGIN PASSWORD '$WAREHOUSE_APP_PASSWORD';
    CREATE ROLE ordering_reader LOGIN PASSWORD '$ORDERING_READER_PASSWORD';
    CREATE ROLE warehouse_reader LOGIN PASSWORD '$WAREHOUSE_READER_PASSWORD';
    CREATE ROLE warehouse_cockpit_writer LOGIN PASSWORD '$WAREHOUSE_COCKPIT_WRITER_PASSWORD';
    CREATE ROLE social_share_writer LOGIN PASSWORD '$SOCIAL_SHARE_WRITER_PASSWORD';
    CREATE ROLE social_share_reader LOGIN PASSWORD '$SOCIAL_SHARE_READER_PASSWORD';

    CREATE DATABASE ordering OWNER ordering_app;
    CREATE DATABASE warehouse OWNER warehouse_app;
    CREATE DATABASE social_share OWNER social_share_writer;

    -- Postgres grants CONNECT on every database to PUBLIC by default - so
    -- without this, ANY role with valid login credentials (including
    -- social_share_reader) could open a connection to `ordering` or
    -- `warehouse` and enumerate table names via information_schema, even
    -- with no table-level SELECT grant there. Every role that legitimately
    -- needs to connect already has an EXPLICIT GRANT CONNECT below (or owns
    -- the database outright, which bypasses this entirely) - confirmed
    -- these revokes don't break anything before adding them.
    REVOKE CONNECT ON DATABASE ordering FROM PUBLIC;
    REVOKE CONNECT ON DATABASE warehouse FROM PUBLIC;
    REVOKE CONNECT ON DATABASE social_share FROM PUBLIC;
EOSQL

# ordering_reader's read access is granted inside `ordering` itself, not at
# the CREATE DATABASE step above - database ownership doesn't imply this.
# ALTER DEFAULT PRIVILEGES FOR ROLE ordering_app is what actually matters:
# the `orders`/`order_items` tables don't exist yet at this point (the
# ordering backend's own `npm run db:migrate`, run as ordering_app, creates
# them later) - this makes ordering_reader's SELECT grant apply to every
# table ordering_app creates from now on, automatically.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "ordering" <<-EOSQL
    GRANT CONNECT ON DATABASE ordering TO ordering_reader;
    GRANT USAGE ON SCHEMA public TO ordering_reader;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO ordering_reader;
    ALTER DEFAULT PRIVILEGES FOR ROLE ordering_app IN SCHEMA public
        GRANT SELECT ON TABLES TO ordering_reader;
EOSQL

# Same reasoning as ordering_reader above, mirrored for `warehouse`: this
# only runs on a brand-new volume, where `warehouse` doesn't have any tables
# yet either (this repo's own `alembic upgrade head`, run as warehouse_app,
# creates them later) - the default-privileges grant makes warehouse_reader's
# SELECT apply to every table warehouse_app creates from now on.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "warehouse" <<-EOSQL
    GRANT CONNECT ON DATABASE warehouse TO warehouse_reader;
    GRANT USAGE ON SCHEMA public TO warehouse_reader;
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO warehouse_reader;
    ALTER DEFAULT PRIVILEGES FOR ROLE warehouse_app IN SCHEMA public
        GRANT SELECT ON TABLES TO warehouse_reader;

    -- CONNECT/USAGE don't need cockpit_alert to exist, so these two are safe
    -- here - the actual "GRANT SELECT, UPDATE ON cockpit_alert" is
    -- deliberately NOT here (see this file's header comment) and must be
    -- run once, by hand, right after `alembic upgrade head` - see
    -- deploy/CLAUDE.md.
    GRANT CONNECT ON DATABASE warehouse TO warehouse_cockpit_writer;
    GRANT USAGE ON SCHEMA public TO warehouse_cockpit_writer;
EOSQL

# social_share is a genuinely separate database (not a schema in
# `warehouse`), owned by social_share_writer, so the table can be created
# right here at first-init time - unlike cockpit_alert above, nothing here
# depends on a migration this repo runs later. See ops/refresh_social_share.py
# for what writes into it and deploy/CLAUDE.md's "Social share database"
# section for the full design.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "social_share" <<-EOSQL
    CREATE TABLE social_post_metrics (
        id uuid PRIMARY KEY,
        platform text NOT NULL,
        content_type text,
        posted_at timestamptz NOT NULL,
        caption text,
        permalink text,
        impressions bigint NOT NULL DEFAULT 0,
        reach bigint NOT NULL DEFAULT 0,
        likes bigint NOT NULL DEFAULT 0,
        comments bigint NOT NULL DEFAULT 0,
        shares bigint NOT NULL DEFAULT 0,
        saves bigint NOT NULL DEFAULT 0,
        clicks bigint NOT NULL DEFAULT 0,
        refreshed_at timestamptz NOT NULL DEFAULT now()
    );

    -- Created as $POSTGRES_USER (the init script's connecting role) above,
    -- so ownership must be handed to social_share_writer explicitly - unlike
    -- ordering_app/warehouse_app, which own their tables automatically
    -- because THEY run the migration that creates them, this table is
    -- created directly by this script instead. Without this, TRUNCATE in
    -- ops/refresh_social_share.py fails with "permission denied".
    ALTER TABLE social_post_metrics OWNER TO social_share_writer;

    GRANT CONNECT ON DATABASE social_share TO social_share_reader;
    GRANT USAGE ON SCHEMA public TO social_share_reader;
    GRANT SELECT ON social_post_metrics TO social_share_reader;
EOSQL
