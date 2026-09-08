#!/usr/bin/env bash
# Runs ONCE, automatically, the first time the postgres container starts
# against an empty data volume - the official postgres image never re-runs
# anything in /docker-entrypoint-initdb.d/ against existing data. To re-run
# this by hand later, either wipe the volume (destroys all data) or apply
# the SQL below manually with psql.
#
# Creates two databases and three least-privilege roles:
#   ordering_app     - read-write, owns `ordering`   (used by the ordering
#                                                       backend, dhaka-kacchi-connect/worker)
#   warehouse_app    - read-write, owns `warehouse`  (used by this repo)
#   ordering_reader  - read-only on `ordering`, used ONLY by
#                       warehouse/ingest/direct.py - so that job can never
#                       write into the live ordering database, structurally,
#                       not just by convention.
#
# Passwords come from environment variables set in deploy/.env - never
# hardcoded here. See deploy/CLAUDE.md for the full list of required vars.
set -euo pipefail

: "${ORDERING_APP_PASSWORD:?ORDERING_APP_PASSWORD must be set}"
: "${WAREHOUSE_APP_PASSWORD:?WAREHOUSE_APP_PASSWORD must be set}"
: "${ORDERING_READER_PASSWORD:?ORDERING_READER_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE ROLE ordering_app LOGIN PASSWORD '$ORDERING_APP_PASSWORD';
    CREATE ROLE warehouse_app LOGIN PASSWORD '$WAREHOUSE_APP_PASSWORD';
    CREATE ROLE ordering_reader LOGIN PASSWORD '$ORDERING_READER_PASSWORD';

    CREATE DATABASE ordering OWNER ordering_app;
    CREATE DATABASE warehouse OWNER warehouse_app;
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
