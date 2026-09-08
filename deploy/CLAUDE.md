# VPS infrastructure — maintenance guide

## What this is

The Docker Compose stack that runs the ordering backend, the warehouse's
on-demand jobs, and Postgres, all on one Contabo VPS, behind Caddy for
HTTPS. This is infrastructure config, not application code — the
applications themselves live in their own repos
(`dhaka-kacchi-connect/worker`, this repo's `warehouse/`).

See [ARCHITECTURE.md](./ARCHITECTURE.md) for how the pieces fit together.
This file is "how do I run it / change it."

## First-time setup on a new VPS

1. Order a Contabo VPS (a few vCPUs, 8GB RAM, NVMe, Ubuntu 24.04 LTS), SSH
   key uploaded at creation.
2. SSH in, create a non-root sudo user, set timezone: `sudo timedatectl
   set-timezone Europe/Berlin`.
3. Install Docker Engine + Compose plugin (Docker's official apt repo).
4. `sudo ufw allow OpenSSH && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable`
   — nothing else needs a public port.
5. At Hostinger's DNS, add an `A` record: name `api`, value = this VPS's
   public IPv4. Do this early so it has time to propagate.
6. Clone both repos as siblings:
   ```bash
   sudo mkdir -p /opt/dhaka-kacchi && sudo chown $USER /opt/dhaka-kacchi
   cd /opt/dhaka-kacchi
   git clone <dhaka_kacchi_ai_harness repo url>
   git clone <dhaka-kacchi-connect repo url>
   ```
7. `cd dhaka_kacchi_ai_harness/deploy && cp .env.example .env && chmod 600 .env`
   — fill in every password (`openssl rand -base64 24` per line) and the
   SMTP/Twilio values. Back up its contents to a password manager now.
8. Start Postgres only, first, so the one-time init script can run against
   an empty volume:
   ```bash
   docker compose up -d postgres
   ```
9. Apply both schemas:
   ```bash
   docker compose run --rm warehouse uv run alembic upgrade head
   docker compose run --rm ordering-backend npm run db:migrate
   ```
10. Start everything else:
    ```bash
    docker compose up -d
    ```
    (`warehouse` stays down — see "The warehouse never runs as a service" below.)
11. Confirm: `curl -i https://api.dhakakacchi.de/health` returns `200` with
    a valid certificate — proves Caddy obtained TLS and is proxying
    correctly.
12. Install cron on the VPS host (not inside a container):
    ```
    0 3 * * * /opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy/scripts/backup.sh >> /var/log/dhaka-kacchi-backup.log 2>&1
    0 * * * * cd /opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy && docker compose run --rm warehouse make ingest-direct >> /var/log/dhaka-kacchi-ingest.log 2>&1
    ```
13. Run `scripts/backup.sh` manually once and do one test restore before
    trusting it — day one of a real incident shouldn't be the first time a
    restore is attempted.

## Day-to-day commands

Run these from `deploy/` on the VPS.

| Task | Command |
|---|---|
| See what's running | `docker compose ps` |
| View logs | `docker compose logs -f ordering-backend` |
| Restart the ordering backend after a code change | `docker compose up -d --build ordering-backend` |
| Run a warehouse command | `docker compose run --rm warehouse <command>`, e.g. `make gate` |
| Apply a new warehouse migration | `docker compose run --rm warehouse uv run alembic upgrade head` |
| Apply a new ordering-backend schema change | `docker compose run --rm ordering-backend npm run db:migrate` (see the DROP TABLE warning in that repo's `worker/CLAUDE.md` first) |
| Tail Postgres | `docker compose exec postgres psql -U postgres` |
| Restart everything | `docker compose restart` |

## The warehouse never runs as a service

`docker compose up` never starts it — it's declared with `profiles:
["cron"]` specifically so it's skipped. It only ever runs via `docker
compose run --rm warehouse <command>`, invoked by cron (see step 12 above)
or by hand. This isn't an oversight: the warehouse has no HTTP server, no
long-running process — it's a CLI tool that reads/writes Postgres and exits.

## Adding a future second backend

Additive only — nothing above needs to change:
1. Add a new service block to `docker-compose.yml` (own build context, own
   database or a read-only reader role on an existing one — copy the
   `ordering_reader` pattern in `postgres-init/01-init-databases.sh`).
2. Add a new DNS `A` record for its subdomain.
3. Add a matching block to `Caddyfile`.
4. `docker compose up -d`.

## Restoring from a backup

```bash
gunzip -c /opt/dhaka-kacchi/backups/ordering_2026-01-01_030000.sql.gz \
  | docker compose exec -T postgres psql -U postgres ordering
```
(Swap `ordering` for `warehouse` as needed. This assumes the target
database already exists and is empty — for a full disaster recovery, run
the "First-time setup" steps above first, up through the `alembic
upgrade`/`db:migrate` step, then restore on top.)

## Secrets

Every password lives in `deploy/.env` (`chmod 600`, gitignored). Never in
code, never in `docker-compose.yml` itself. If `.env` is ever lost or
leaked, rotate every password in it and update the file — nothing else
needs to change.
