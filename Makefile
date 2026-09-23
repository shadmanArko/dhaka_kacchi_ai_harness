SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

UV      := uv
PY      := $(UV) run python
ALEMBIC := $(UV) run alembic

SCHEMA_HEAD := 0011
SEED_REV    := 0012

.PHONY: help install env db upgrade schema seed reseed downgrade reset nuke \
        verify verify-idempotent gate ingest-direct ingest-direct-dry-run \
        verify-ingest-direct ingest-events ingest-events-dry-run \
        verify-ingest-events ingest-instagram ingest-instagram-dry-run \
        ingest-facebook ingest-facebook-dry-run \
        ingest-threads ingest-threads-dry-run \
        run-detectors \
        current history sql psql revision lint fmt

help:  ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install:  ## create .venv and resolve dependencies (idempotent)
	$(UV) sync

env:  ## create .env from .env.example if absent; never overwrites
	@if [ -f .env ]; then echo ".env already exists - leaving it alone"; else \
	  sed "s/YOUR_USER/$$(whoami)/" .env.example > .env; \
	  echo "wrote .env for user $$(whoami)"; fi

db: install  ## create the dhaka_kacchi database if it does not exist
	$(PY) -m warehouse.bootstrap_db

upgrade: db  ## apply the whole chain, schema + seed
	$(ALEMBIC) upgrade head

schema: db  ## apply table migrations only, no seed data
	$(ALEMBIC) upgrade $(SCHEMA_HEAD)

seed: schema  ## apply the seed migration
	$(ALEMBIC) upgrade $(SEED_REV)

reseed:  ## re-run the seed after editing 0012 (upgrade alone will NOT replay it)
	$(ALEMBIC) downgrade $(SCHEMA_HEAD)
	$(ALEMBIC) upgrade $(SEED_REV)

downgrade:  ## step back exactly one revision
	$(ALEMBIC) downgrade -1

reset: db  ## tear the schema down to base and rebuild it
	$(ALEMBIC) downgrade base
	$(ALEMBIC) upgrade head

nuke:  ## DROP DATABASE (interactive confirmation)
	$(PY) -m warehouse.bootstrap_db --drop

verify:  ## assert alembic is at head and the schema matches house conventions
	$(PY) -m warehouse.verify

verify-idempotent: upgrade  ## prove every migration survives a replay over a live schema
	@echo "==> 1/4 baseline"
	$(PY) -m warehouse.verify
	@echo "==> 2/4 forgetting alembic_version, replaying the full chain over a live schema"
	$(PY) -m warehouse.bootstrap_db --forget-migrations
	$(ALEMBIC) upgrade head
	$(PY) -m warehouse.verify
	@echo "==> 3/4 full down/up round trip"
	$(ALEMBIC) downgrade base
	$(ALEMBIC) upgrade head
	$(PY) -m warehouse.verify
	@echo "==> 4/4 offline render must not error"
	$(ALEMBIC) upgrade base:head --sql > /dev/null
	@echo "OK - migrations are idempotent and offline-renderable"

gate:  ## run the ARCHITECTURE.md section 9 exit-gate query
	$(PY) -m warehouse.gate

ingest-direct:  ## pull orders from the ordering backend's own Postgres database into the warehouse
	$(PY) -m warehouse.ingest.direct

ingest-direct-dry-run:  ## show what ingest-direct would write, without writing
	$(PY) -m warehouse.ingest.direct --dry-run

verify-ingest-direct:  ## prove ingest-direct is idempotent and COGS-correct
	$(PY) -m warehouse.ingest.direct_verify

ingest-events:  ## pull marketing/behavioral events from the ordering backend's own Postgres database
	$(PY) -m warehouse.ingest.events

ingest-events-dry-run:  ## show what ingest-events would write, without writing
	$(PY) -m warehouse.ingest.events --dry-run

verify-ingest-events:  ## prove ingest-events is idempotent
	$(PY) -m warehouse.ingest.events_verify

ingest-instagram:  ## pull organic Instagram post data via the Meta Graph API (needs INSTAGRAM_ACCESS_TOKEN - see warehouse/ingest/instagram.py)
	$(PY) -m warehouse.ingest.instagram

ingest-instagram-dry-run:  ## show what ingest-instagram would write, without writing
	$(PY) -m warehouse.ingest.instagram --dry-run

ingest-facebook:  ## pull Facebook Page posts via the Meta Graph API (needs FACEBOOK_PAGE_ACCESS_TOKEN - see warehouse/ingest/facebook.py)
	$(PY) -m warehouse.ingest.facebook

ingest-facebook-dry-run:  ## show what ingest-facebook would write, without writing
	$(PY) -m warehouse.ingest.facebook --dry-run

ingest-threads:  ## pull organic Threads post data via the Threads API (needs THREADS_ACCESS_TOKEN - see warehouse/ingest/threads.py)
	$(PY) -m warehouse.ingest.threads

ingest-threads-dry-run:  ## show what ingest-threads would write, without writing
	$(PY) -m warehouse.ingest.threads --dry-run

run-detectors:  ## run every ops detector, opening/resolving cockpit_alert rows (see ops/run_detectors.py)
	$(PY) -m ops.run_detectors

current:  ## which revision is applied
	$(ALEMBIC) current --verbose

history:  ## the whole chain, current marked
	$(ALEMBIC) history --indicate-current

sql:  ## render the entire chain as static SQL to stdout
	$(ALEMBIC) upgrade base:head --sql

psql:  ## open psql against DATABASE_URL
	psql "$$($(PY) -m warehouse.config print-url)"

revision:  ## make revision REV=0003 NAME=supplier
	@test -n "$(REV)" || { echo "REV= is required, e.g. REV=0003"; exit 1; }
	@test -n "$(NAME)" || { echo "NAME= is required, e.g. NAME=supplier"; exit 1; }
	$(ALEMBIC) revision -m "$(NAME)" --rev-id "$(REV)"

lint:  ## ruff check + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt:  ## ruff format
	$(UV) run ruff format .
