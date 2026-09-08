# VPS infrastructure — architecture

A five-minute tour of how the VPS is put together and why. For "how do I run
it," see [CLAUDE.md](./CLAUDE.md) instead.

## Shape

```
                          Contabo VPS (Docker Compose, one bridge network)

  Internet ──HTTPS──▶ ┌────────┐         ┌────────────────┐
                       │ Caddy  │────────▶│ ordering-backend│
                       │(:80/443)│        │ (Node/Hono)     │
                       └────────┘         └────────┬────────┘
                                                     │
                                                     ▼
                                          ┌────────────────────┐
                                          │ postgres            │
                                          │ (two databases:     │
                                          │  ordering, warehouse)│
                                          └──────────┬──────────┘
                                                     ▲
                                          ┌──────────┴──────────┐
                                          │ warehouse (Python)   │
                                          │ profiles:[cron] —    │
                                          │ never `up`, only     │
                                          │ `run --rm`, triggered│
                                          │ by cron              │
                                          └──────────────────────┘
```

## Why it's shaped this way

**One Postgres instance, two databases, not two instances.** Two full
Postgres instances would double fixed memory/CPU overhead on a small VPS for
no benefit at this data volume. Two databases (not two schemas in one
database) gives each backend a real, Postgres-enforced isolation boundary —
a schema boundary can leak via `search_path` or a wildcard grant; a database
boundary can't.

**Three Postgres roles, not one shared login.**
`ordering_app`/`warehouse_app` each own exactly one database.
`ordering_reader` can only ever `SELECT` from `ordering` — the warehouse's
ingestion job connects as this role, so it structurally cannot write into
the live ordering database, regardless of what the ingestion code does or
doesn't check. See `postgres-init/01-init-databases.sh`.

**The warehouse is a CLI tool, not a service.** It has no HTTP server and
nothing calls it interactively — Compose's `profiles: ["cron"]` keeps
`docker compose up` from starting it at all. It only runs when invoked
directly (by cron, or by hand), does its work, and exits.

**Caddy, not nginx + certbot.** Caddy's built-in ACME client requests and
renews the Let's Encrypt certificate as part of the same process — no
separate renewal cron job that can silently break and leave an expired
certificate unnoticed, which is the actual failure mode nginx+certbot setups
hit for a solo operator with no on-call rotation.

**Postgres bound to `127.0.0.1` only.** Never reachable from outside the
VPS — not even with a password. Debugging happens over an SSH tunnel.
Container-to-container traffic (the app connecting to Postgres) never
leaves the Docker bridge's virtual interface, so `sslmode=disable` between
containers doesn't expose anything real — the official `postgres:16-alpine`
image has no TLS certificate configured out of the box anyway, so
`sslmode=require` would simply fail to connect. Public-facing traffic
already gets real TLS from Caddy.

**Backups are a separate failure domain from the VPS's own disk.** A
nightly `pg_dump` synced to Contabo Object Storage (a different service,
same billing account) survives a VPS-level disaster that a same-disk backup
wouldn't.

## Where each repo's own Docker concerns live

This `deploy/` directory only holds cross-cutting infrastructure (Compose,
Caddy, Postgres roles, backups). Each application owns its own `Dockerfile`
and `.dockerignore`:
- `dhaka_kacchi_ai_harness/Dockerfile` (the warehouse image)
- `dhaka-kacchi-connect/worker/Dockerfile` (the ordering-backend image)

`docker-compose.yml` builds both from their own repos via relative
`context:` paths, assuming both repos are checked out as siblings under
`/opt/dhaka-kacchi/` (see CLAUDE.md's setup steps).

## What this deliberately does NOT have

No Kubernetes (one VPS, four containers — no case for an orchestrator this
small). No message queue between services. No admin UI for any of this —
it's `docker compose` commands over SSH, which is the right amount of
tooling for a solo operator running one VPS.
