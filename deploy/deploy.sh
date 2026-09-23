#!/usr/bin/env bash
# The actual VPS deploy logic - versioned here so changes to it are
# code-reviewed and history-tracked like everything else in this repo.
#
# Invoked by /opt/dhaka-kacchi/bin/run-deploy.sh - a tiny, STABLE wrapper
# that lives OUTSIDE git on the VPS (hand-installed once, never touched by
# `git pull`) and execs into this file after pulling both repos fresh. See
# that file's own header for why a self-modifying "script pulls its own
# repo mid-execution" design is deliberately avoided.
#
# Triggered by .github/workflows/deploy-vps.yml in BOTH sibling repos, on
# every push to main, over a forced-command SSH key that can ONLY run
# run-deploy.sh - see deploy/CLAUDE.md's "CI/CD" section for the full
# security design and one-time setup steps.
#
# FAILS LOUDLY (set -e) AND STOPS BEFORE TOUCHING THE RUNNING CONTAINER if
# migrations, verify, or the margin gate fail - a bad migration blocks the
# deploy instead of taking down a currently-healthy production backend.
# The `docker compose up -d --build` calls for predictor then ordering-backend
# are the last writes this script makes, in that order (ordering-backend
# depends_on predictor's healthcheck, so it won't start against a broken
# predictor build) - everything before them only runs disposable, --rm
# containers.
set -euo pipefail

DEPLOY_DIR=/opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy
HEALTH_URL=https://api.dhakakacchi.com/health

log() {
  printf '[deploy %s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

cd "$DEPLOY_DIR"

log "applying warehouse migrations..."
docker compose run --rm --build warehouse uv run alembic upgrade head

log "verifying schema conventions (make verify)..."
docker compose run --rm warehouse make verify

log "checking the phase-1 margin gate (make gate)..."
docker compose run --rm warehouse make gate

log "rebuilding and restarting the post-engagement predictor..."
docker compose up -d --build predictor

log "rebuilding and restarting the ordering backend..."
docker compose up -d --build ordering-backend

log "waiting for the backend to come back healthy..."
attempt=0
until curl -sf "$HEALTH_URL" > /dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 15 ]; then
    log "health check FAILED after ${attempt} attempts - deploy did not verify cleanly"
    exit 1
  fi
  sleep 2
done

log "health check passed - deploy complete"
