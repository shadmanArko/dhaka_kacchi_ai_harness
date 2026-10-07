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
        ingest-youtube ingest-youtube-dry-run verify-ingest-youtube \
        youtube-auth youtube-auth-check verify-youtube-auth \
        ingest-youtube-analytics ingest-youtube-analytics-dry-run \
        verify-ingest-youtube-analytics \
        ingest-posthog ingest-posthog-dry-run build-web-aggregates verify-ingest-posthog \
        ingest-search-console ingest-search-console-dry-run verify-ingest-search-console \
        ingest-social-followers ingest-social-followers-dry-run verify-ingest-social-followers \
        ingest-links ingest-links-dry-run verify-ingest-links \
        run-detectors refresh-social-share \
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

ingest-youtube:  ## pull YouTube video data via the Data API v3 (needs YOUTUBE_API_KEY + YOUTUBE_CHANNEL_ID - see warehouse/ingest/youtube.py)
	$(PY) -m warehouse.ingest.youtube

ingest-youtube-dry-run:  ## show what ingest-youtube would write, without writing
	$(PY) -m warehouse.ingest.youtube --dry-run

verify-ingest-youtube:  ## prove ingest-youtube against a local fake YouTube API and a throwaway database (needs no YouTube account; never touches your dev data)
	$(PY) -m warehouse.ingest.youtube_verify

youtube-auth:  ## one-time browser consent: mint a READ-ONLY YouTube Analytics token and save it to .env (see warehouse/ingest/youtube_auth.py)
	$(PY) -m warehouse.ingest.youtube_auth

youtube-auth-check:  ## test the saved YouTube Analytics token against the API, without minting a new one
	$(PY) -m warehouse.ingest.youtube_auth --check

verify-youtube-auth:  ## prove the OAuth helper against a local fake Google (needs no account and no database)
	$(PY) -m warehouse.ingest.youtube_auth_verify

ingest-youtube-analytics:  ## pull YouTube watch time, subscribers, daily totals and traffic sources (needs the OAuth token - run `make youtube-auth` once)
	$(PY) -m warehouse.ingest.youtube_analytics

ingest-youtube-analytics-dry-run:  ## show what ingest-youtube-analytics would write, without writing
	$(PY) -m warehouse.ingest.youtube_analytics --dry-run

verify-ingest-youtube-analytics:  ## prove ingest-youtube-analytics against a fake Analytics API and a throwaway database
	$(PY) -m warehouse.ingest.youtube_analytics_verify

ingest-social-followers:  ## record today's follower/subscriber counts for Instagram, Facebook, Threads and YouTube (uses the existing credentials; history cannot be backfilled)
	$(PY) -m warehouse.ingest.social_followers

ingest-social-followers-dry-run:  ## read the four follower counts and print them, write nothing
	$(PY) -m warehouse.ingest.social_followers --dry-run

verify-ingest-social-followers:  ## prove ingest-social-followers against a local fake of all four APIs and a throwaway database
	$(PY) -m warehouse.ingest.social_followers_verify

ingest-links:  ## turn the website's tagged links (Link builder) into channels/campaigns/variants and match them to posts; label earlier visits (needs the website's tracked_links table)
	$(PY) -m warehouse.ingest.links

ingest-links-dry-run:  ## list the tagged links at the source, write nothing
	$(PY) -m warehouse.ingest.links --dry-run

verify-ingest-links:  ## prove ingest-links against two throwaway databases (a warehouse and a stand-in website database), with mutation tests
	$(PY) -m warehouse.ingest.links_verify

ingest-posthog:  ## pull the website's PostHog events (scrubbed: hashed ids, no tokens/locations) and rebuild the daily web summaries (needs POSTHOG_* in .env)
	$(PY) -m warehouse.ingest.posthog_web

ingest-posthog-dry-run:  ## pull and scrub PostHog events, print what would be kept and dropped, write nothing
	$(PY) -m warehouse.ingest.posthog_web --dry-run

build-web-aggregates:  ## rebuild the daily web summaries from raw events already stored (needs no PostHog access)
	$(PY) -m warehouse.ingest.posthog_web --build-only

verify-ingest-posthog:  ## prove the PostHog ingester and its privacy scrubbing against a fake PostHog and a throwaway database
	$(PY) -m warehouse.ingest.posthog_verify

ingest-search-console:  ## pull Google Search Console (search performance) via a read-only service account (needs SEARCH_CONSOLE_* in .env)
	$(PY) -m warehouse.ingest.search_console

ingest-search-console-dry-run:  ## show what ingest-search-console would replace, without writing
	$(PY) -m warehouse.ingest.search_console --dry-run

verify-ingest-search-console:  ## prove the Search Console ingester against a fake Google (checks the sign-in signature too) and a throwaway database
	$(PY) -m warehouse.ingest.search_console_verify

run-detectors:  ## run every ops detector, opening/resolving cockpit_alert rows (see ops/run_detectors.py)
	$(PY) -m ops.run_detectors

refresh-social-share:  ## mirror social post performance data into the social_share database (needs SOCIAL_SHARE_DATABASE_URL - see ops/refresh_social_share.py)
	$(PY) -m ops.refresh_social_share

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
