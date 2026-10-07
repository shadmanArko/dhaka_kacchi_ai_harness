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
   SMTP/BerlinSMS/Telegram values. Back up its contents to a password
   manager now.
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
11. Confirm: `curl -i https://api.dhakakacchi.com/health` returns `200` with
    a valid certificate — proves Caddy obtained TLS and is proxying
    correctly.
12. Set up off-site backup sync (see `scripts/backup.sh`'s header comment):
    create a free Backblaze B2 account, a private bucket, and an
    Application Key scoped to just that bucket, then on the VPS host:
    ```bash
    curl https://rclone.org/install.sh | sudo bash
    rclone config create backblaze-b2 b2 account=<keyID> key=<applicationKey>
    ```
13. Install cron on the VPS host (not inside a container). **Output must
    go somewhere the (non-root) deploy user can actually write** —
    `/var/log/` is root-owned; a cron job redirecting there fails silently,
    and worse, the command itself never runs at all (bash refuses to start
    it if the output redirect can't be opened) — this bit us once already,
    see `git log` around the Sentry/Impressum work for the incident:
    ```bash
    mkdir -p /opt/dhaka-kacchi/logs
    ```
    ```
    0 3 * * * /opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy/scripts/backup.sh >> /opt/dhaka-kacchi/logs/backup.log 2>&1
    0 * * * * cd /opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy && docker compose run --rm warehouse make ingest-direct >> /opt/dhaka-kacchi/logs/ingest.log 2>&1
    5 * * * * cd /opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy && docker compose run --rm warehouse make ingest-events >> /opt/dhaka-kacchi/logs/ingest-events.log 2>&1
    30 4 * * * cd /opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy && docker compose run --rm warehouse make ingest-instagram >> /opt/dhaka-kacchi/logs/ingest-instagram.log 2>&1
    45 4 * * * cd /opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy && docker compose run --rm warehouse make ingest-facebook >> /opt/dhaka-kacchi/logs/ingest-facebook.log 2>&1
    0 5 * * * cd /opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy && docker compose run --rm warehouse make ingest-threads >> /opt/dhaka-kacchi/logs/ingest-threads.log 2>&1
    15 5 * * * cd /opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy && docker compose run --rm warehouse make ingest-youtube >> /opt/dhaka-kacchi/logs/ingest-youtube.log 2>&1
    20 5 * * * cd /opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy && docker compose run --rm warehouse make ingest-youtube-analytics >> /opt/dhaka-kacchi/logs/ingest-youtube-analytics.log 2>&1
    30 5 * * * cd /opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy && docker compose run --rm warehouse make refresh-social-share >> /opt/dhaka-kacchi/logs/refresh-social-share.log 2>&1
    15 6 * * * cd /opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy && docker compose run --rm warehouse make run-detectors >> /opt/dhaka-kacchi/logs/detectors.log 2>&1
    ```
    The events job runs at :05, not :00 - offset from ingest-direct so the two
    never run concurrently and interleave in a shared log. Separate log file
    for the same reason (and so a grep for one job's output isn't polluted by
    the other's).

    Instagram/Facebook/Threads are **daily, not hourly** - deliberately, not
    an oversight: Meta's own Page Insights docs state most metrics only
    update once every 24 hours, so an hourly cron would just make 24x the
    API calls for no new data. Scheduled at 4:30/4:45/5:00am (after the 3am
    backup, well clear of the hourly :00/:05 jobs), staggered from each
    other - all three jobs are genuinely slow (one insights API call per
    post, observed anywhere from ~2 to ~40+ minutes depending on Meta's
    response latency that day), so running them back-to-back rather than
    concurrently avoids long-running jobs contending for the same app's
    API quota at once. Threads uses a completely separate host
    (graph.threads.net) and its own quota, so it can't contend with the
    other two even if it did overlap - the stagger is mostly to keep their
    logs from interleaving.

    `ingest-youtube` runs daily at 5:15am, right behind Threads, for the same
    reason as the Meta jobs (a post's numbers are cumulative; hourly reads add
    cost, not information). Unlike them it is FAST - a handful of API calls
    for the whole channel, seconds not minutes - and it uses Google's quota,
    not Meta's, so it cannot contend with anything above. It needs
    `YOUTUBE_API_KEY` and `YOUTUBE_CHANNEL_ID` in this directory's `.env` (see
    `.env.example`); unlike the Threads token the API key never expires. YouTube
    rows are deliberately NOT mirrored into `social_share` - that job has an
    explicit platform allowlist (`SHARED_PLATFORMS` in ops/refresh_social_share.py),
    so sharing YouTube with the collaborator is a decision, not a side effect.

    `ingest-youtube-analytics` runs at 5:20am, after `ingest-youtube` has made
    sure every video exists as a `social_post` (it asks YouTube about the videos
    already in the warehouse). It is a SEPARATE job on purpose: a revoked token
    or a disabled Analytics API then fails this one loudly without taking the
    public-stats job down with it. Its tables behave differently from the
    snapshot tables above - each day's figure is RESTATED for ~3 days, and the
    newest ~3 days are simply absent - so every run re-pulls a trailing 14-day
    window and overwrites, and a day that is missing today is filled in by a
    later run rather than stored as zero. Needs the three `YOUTUBE_OAUTH_*`
    values in this directory's `.env`, with a READ-ONLY refresh token minted by
    `make youtube-auth` on your own machine (never a token that can upload or
    delete). To backfill after the first run, from `deploy/`:
    `docker compose run --rm warehouse uv run python -m warehouse.ingest.youtube_analytics --since 2026-09-01`.

    WHAT DEPLOY DOES AND DOES NOT DO FOR A NEW SOURCE: pushing to `main`
    makes deploy.sh pull the code, apply every pending warehouse migration
    (so the schema is ready with no manual step), and run `make verify` and
    `make gate`. It does NOT write secrets into `.env` and does NOT edit the
    crontab - both are hand-done on the VPS, once, and a new ingester that is
    missing either fails quietly in its own log file rather than loudly in the
    deploy. After adding a source, check its log the next morning.

    `refresh-social-share` (ops/refresh_social_share.py, see this file's own
    "Social share database" section) runs at 5:30am, after Threads lands
    but before the detectors - same reasoning as the ingest jobs above:
    reading `warehouse` before that day's posts have landed would ship a
    stale mirror to the collaborator who reads it.

    `run-detectors` (ops/run_detectors.py) runs last, at 6:15am, after every
    ingest job for the day has had a chance to land - a detector reading
    stale data would either miss a real problem or, worse, flag one that
    already cleared hours ago.
14. Run `scripts/backup.sh` manually once, confirm a new file lands in both
    `/opt/dhaka-kacchi/backups/` AND the Backblaze bucket
    (`rclone ls backblaze-b2:<bucket-name>`), and do one test restore
    before trusting any of this — day one of a real incident shouldn't be
    the first time a restore is attempted.

## Day-to-day commands

Run these from `deploy/` on the VPS.

| Task | Command |
|---|---|
| See what's running | `docker compose ps` |
| View logs | `docker compose logs -f ordering-backend` |
| Restart the ordering backend after a code change | `docker compose up -d --build ordering-backend` |
| Restart the post-engagement predictor after retraining (`ml/05-production/build_artifact.py`) or a code change | `docker compose up -d --build predictor` |
| Run a warehouse command | `docker compose run --rm warehouse <command>`, e.g. `make gate` |
| Apply a new warehouse migration | `docker compose run --rm warehouse uv run alembic upgrade head` |
| Apply a new ordering-backend schema change | `docker compose run --rm ordering-backend npm run db:migrate` (see the DROP TABLE warning in that repo's `worker/CLAUDE.md` first) |
| Register/re-register the Telegram inbound webhook (after setting `TELEGRAM_WEBHOOK_SECRET` in `.env`, or rotating it) | `docker compose run --rm ordering-backend npm run telegram:set-webhook` |
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

## Adding the `warehouse_reader` role to an existing production database

`postgres-init/01-init-databases.sh` only runs once, automatically,
against an *empty* data volume — it will never create `warehouse_reader`
on a production database that already existed before this role was added
(2026-09-23, for `dhaka-kacchi-connect`'s admin reporting page). Add it
by hand, once, with:

```bash
docker compose exec -T postgres psql -U postgres -c "
  CREATE ROLE warehouse_reader LOGIN PASSWORD '<same value as WAREHOUSE_READER_PASSWORD in .env>';
"
docker compose exec -T postgres psql -U postgres warehouse -c "
  GRANT CONNECT ON DATABASE warehouse TO warehouse_reader;
  GRANT USAGE ON SCHEMA public TO warehouse_reader;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO warehouse_reader;
  ALTER DEFAULT PRIVILEGES FOR ROLE warehouse_app IN SCHEMA public
      GRANT SELECT ON TABLES TO warehouse_reader;
"
```
Then add `WAREHOUSE_READER_PASSWORD` to `.env` (same value used above),
`docker compose up -d` (recreates `postgres` with the new env var — no
data loss, it only adds an env var to an already-running container's
next start), and `docker compose up -d --build ordering-backend` to pick
up `WAREHOUSE_DATABASE_URL`.

**Generate the password with `openssl rand -hex 24`, not `-base64`** — a
base64 password can contain `+`/`/`/`=`, which breaks when embedded
directly into a plain `postgresql://user:password@host` connection string
(confirmed the hard way: `TypeError: Invalid URL` from `pg-connection-
string` the first time this bit us). Hex is always URL-safe.

## Adding the `warehouse_cockpit_writer` role (same situation, one more role)

Same reasoning as `warehouse_reader` above, added 2026-09-23 for the admin
cockpit page's acknowledge/resolve actions (`worker/src/lib/
cockpitRepository.ts`) — a narrower role scoped to just `cockpit_alert`,
deliberately separate from `warehouse_reader` (see postgres-init/01-init-
databases.sh's header for why). Add it the same way:

```bash
docker compose exec -T postgres psql -U postgres -c "
  CREATE ROLE warehouse_cockpit_writer LOGIN PASSWORD '<same value as WAREHOUSE_COCKPIT_WRITER_PASSWORD in .env>';
"
docker compose exec -T postgres psql -U postgres warehouse -c "
  GRANT CONNECT ON DATABASE warehouse TO warehouse_cockpit_writer;
  GRANT USAGE ON SCHEMA public TO warehouse_cockpit_writer;
  GRANT SELECT, UPDATE ON cockpit_alert TO warehouse_cockpit_writer;
"
```
**`SELECT` is required alongside `UPDATE`, not optional** — an
`UPDATE ... WHERE ...` needs `SELECT` on the WHERE-clause columns to
evaluate the filter, which `UPDATE` alone does not grant. Granting only
`UPDATE` (as this doc originally said) produces `permission denied for
table cockpit_alert` the moment the acknowledge/resolve routes actually
run a query, not at grant time — confirmed the hard way on first deploy.
Then add `WAREHOUSE_COCKPIT_WRITER_PASSWORD` to `.env`, `docker compose up
-d postgres`, and `docker compose up -d --build ordering-backend` to pick
up `WAREHOUSE_COCKPIT_DATABASE_URL`. Same `openssl rand -hex 24` warning
as above applies.

## Social share database

A separate Postgres database (`social_share`, own Postgres instance, own
databases list alongside `ordering`/`warehouse`) holding one clean,
read-only table — `social_post_metrics`, one row per organic Instagram/
Facebook/Threads post plus its latest engagement metrics — decoupled from
the live warehouse specifically so it can be handed to an outside
collaborator. See `ops/refresh_social_share.py` for the refresh job and
`warehouse/config.py`'s `SocialShareTargetSettings` for how it connects.

**Why a separate database, not a narrower role on `warehouse`:** a leaked
reader password or a sloppy query in this arrangement can only ever expose
this one derived table — structurally, not by convention — never anything
in the same database as real orders or customer data. The collaborator's
Postgres role (`social_share_reader`) also cannot even *connect* to
`ordering` or `warehouse` (see the `REVOKE CONNECT ... FROM PUBLIC` lines
below — Postgres grants CONNECT to every role by default unless revoked,
confirmed the hard way that this needed an explicit fix, not just relying
on missing table grants).

### One-time production setup

1. Generate two passwords (`openssl rand -hex 24` — not `-base64`, see the
   warning above) and add both to `.env`:
   ```
   SOCIAL_SHARE_WRITER_PASSWORD=...
   SOCIAL_SHARE_READER_PASSWORD=...
   ```
2. Create the database, roles, and table:
   ```bash
   docker compose exec -T postgres psql -U postgres -c "
     CREATE ROLE social_share_writer LOGIN PASSWORD '<SOCIAL_SHARE_WRITER_PASSWORD>';
     CREATE ROLE social_share_reader LOGIN PASSWORD '<SOCIAL_SHARE_READER_PASSWORD>';
     CREATE DATABASE social_share OWNER social_share_writer;
     REVOKE CONNECT ON DATABASE ordering FROM PUBLIC;
     REVOKE CONNECT ON DATABASE warehouse FROM PUBLIC;
     REVOKE CONNECT ON DATABASE social_share FROM PUBLIC;
   "
   docker compose exec -T postgres psql -U postgres social_share -c "
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
     ALTER TABLE social_post_metrics OWNER TO social_share_writer;
     GRANT CONNECT ON DATABASE social_share TO social_share_reader;
     GRANT USAGE ON SCHEMA public TO social_share_reader;
     GRANT SELECT ON social_post_metrics TO social_share_reader;
   "
   ```
   (This is exactly what `postgres-init/01-init-databases.sh` does on a
   fresh volume — done by hand here because this only runs once,
   automatically, against an *empty* volume, same situation as
   `warehouse_reader`/`warehouse_cockpit_writer` above.)
3. `docker compose up -d postgres` to pick up the two new env vars, then
   run the refresh job once by hand to populate the table and confirm it
   works before trusting cron with it:
   ```bash
   docker compose run --rm warehouse make refresh-social-share
   ```
4. Add the cron entry — after the ingest jobs land fresh data, before
   `run-detectors`:
   ```
   30 5 * * * cd /opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy && docker compose run --rm warehouse make refresh-social-share >> /opt/dhaka-kacchi/logs/refresh-social-share.log 2>&1
   ```

### Giving the collaborator a way in

The Postgres port is loopback-only (`127.0.0.1:5432`, see docker-compose.yml)
— nothing external reaches it today, by design. Rather than opening a public
port, this adds one more restricted SSH login whose ONLY capability is
local port-forwarding — no shell, no files, no commands. Two independent
secrets are needed to reach the data (this SSH key, plus the
`social_share_reader` password) — either alone is useless.

1. On the VPS:
   ```bash
   sudo useradd -m -s /usr/sbin/nologin datashare
   sudo mkdir -p /home/datashare/.ssh
   sudo chmod 700 /home/datashare/.ssh
   sudo chown -R datashare:datashare /home/datashare/.ssh
   ```
2. Have the collaborator generate their OWN keypair on their own machine
   (`ssh-keygen -t ed25519 -C "social-share"`) and send back only the
   **public** key — the private key should never travel through anyone
   else, including you or me.
3. Add it to `/home/datashare/.ssh/authorized_keys`, with the restriction
   prefix (note: this is the *inverse* of the CI/CD deploy key above — that
   one allows a fixed command and blocks port-forwarding; this one allows
   ONLY port-forwarding and blocks everything else):
   ```
   restrict,port-forwarding ssh-ed25519 AAAA... social-share
   ```
   `sudo chmod 600 /home/datashare/.ssh/authorized_keys` afterward.
4. Give the collaborator this recipe (their side, no VPS access needed
   beyond the tunnel):
   ```bash
   ssh -N -L 5433:localhost:5432 datashare@<vps-host>
   ```
   then point any Postgres client (DBeaver, pgAdmin, psql, pandas via
   SQLAlchemy) at:
   ```
   postgresql://social_share_reader:<SOCIAL_SHARE_READER_PASSWORD>@localhost:5433/social_share
   ```
   The tunnel needs to stay running (a second terminal, or `-f` to
   background it) while they query.

### Revoking access later

Two independent things to pull, either one is sufficient on its own:
- Remove their line from `/home/datashare/.ssh/authorized_keys` (or
  `sudo userdel -r datashare` to remove the whole account).
- Rotate `SOCIAL_SHARE_READER_PASSWORD` (`ALTER ROLE social_share_reader
  WITH PASSWORD '...'`, then update `.env`, `docker compose up -d
  postgres`).

## CI/CD

Two separate, independently-triggered pipelines, added at different times -
they don't share a workflow or a secret, and touching one never touches
the other:

- **`dhaka-kacchi-connect`'s `deploy-backend.yml`** (pre-existing) - only
  on a `worker/**` change, only rebuilds/restarts `ordering-backend`.
  Deliberately never runs that repo's own `db:migrate` (its schema.sql
  migrations are a manual, backed-up, human-run step by design - see that
  repo's own `worker/CLAUDE.md`). Uses secrets `VPS_HOST`/`VPS_USER`/
  `VPS_SSH_PRIVATE_KEY` in that repo's own GitHub settings, over an
  unrestricted key (full shell access as the `deploy` user).
- **This repo's `deploy-vps.yml`** (added 2026-09-23) - on every push to
  `main`, runs the full sequence in `deploy/deploy.sh`: `alembic upgrade
  head` → `make verify` → `make gate` → rebuild/restart `predictor`
  (see §4.8 in ARCHITECTURE.md) → rebuild/restart
  `ordering-backend` → health-check `https://api.dhakakacchi.com/health`.
  `ordering-backend` `depends_on` `predictor`'s healthcheck, so a broken
  predictor build blocks the ordering-backend restart rather than leaving
  a half-deployed stack.
  **Stops before touching the running container if migrations/verify/gate
  fail** - a bad migration blocks the deploy instead of taking down a
  currently-healthy backend. Runs over a SEPARATE, purpose-built
  **forced-command SSH key**: even if `VPS_SSH_PRIVATE_KEY` (this repo's
  own secret, same name as the connect repo's but a DIFFERENT key/value)
  ever leaked, it can only execute one fixed script on the VPS, nothing
  else - no interactive shell, no port forwarding.

### How the forced-command key works

`~deploy/.ssh/authorized_keys` has a line shaped like:
```
command="/opt/dhaka-kacchi/bin/run-deploy.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA...
```
Whatever command an SSH client sends is *ignored*; the `command=` clause
always runs `run-deploy.sh` instead. `deploy-vps.yml`'s `script:` field is
therefore just a placeholder - the real logic never travels over the wire.

`/opt/dhaka-kacchi/bin/run-deploy.sh` is deliberately **NOT tracked in
git** - it's the one thing this key can run, so it must never be something
`git pull` could change out from under a live SSH session. Its only job:
`git pull origin main` in both repos, then `exec` into
`dhaka_kacchi_ai_harness/deploy/deploy.sh` (which *is* tracked in git, so
changes to the actual deploy steps are code-reviewed and history-tracked
like everything else). `exec`, not a plain call, so a mid-pull script
replacement can never leave two versions of deploy logic overlapping in
one process.

### Rotating or losing the key

Generate a fresh one (`ssh-keygen -t ed25519 -f ~/.ssh/github_actions_warehouse_deploy -N ""`
on the VPS), append the restricted line above to `authorized_keys` with
the new public key, update the `VPS_SSH_PRIVATE_KEY` secret in this
repo's GitHub settings with the new private key, and remove the old
`authorized_keys` line once the new one is confirmed working. `run-
deploy.sh` and `deploy.sh` need no changes - only the credential rotates.

### First-time setup on a new VPS (if this stack is ever rebuilt elsewhere)

1. `ssh-keygen -t ed25519 -f ~/.ssh/github_actions_warehouse_deploy -N ""`
   as the `deploy` user.
2. `mkdir -p /opt/dhaka-kacchi/bin` and create `run-deploy.sh` there with
   the content described above (not committed anywhere - copy it from
   this section or from a working VPS).
3. `chmod +x /opt/dhaka-kacchi/bin/run-deploy.sh`.
4. Append `command="/opt/dhaka-kacchi/bin/run-deploy.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty `
   (note the trailing space) immediately before the public key's own
   content in `~/.ssh/authorized_keys`.
5. Add `VPS_HOST`/`VPS_USER`/`VPS_SSH_PRIVATE_KEY` to this repo's GitHub
   secrets (Settings → Secrets and variables → Actions).
6. Trigger `workflow_dispatch` from the Actions tab once to confirm it
   works before relying on a real push.
