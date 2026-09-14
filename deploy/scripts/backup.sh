#!/usr/bin/env bash
# Nightly Postgres backup: pg_dump both databases (gzipped), keep 30 days
# locally, sync off-box via rclone to Backblaze B2 (free tier - these dumps
# are a few KB-MB each, nowhere near the 10GB free allowance) - a genuinely
# separate failure domain from the VPS's own disk, and from Contabo entirely
# (Contabo Object Storage was considered and rejected - paid, and B2's free
# tier covers this workload forever).
#
# Runs on the VPS HOST (not inside a container) via cron, since it needs
# `docker compose exec` and the host's own rclone config. Output must be
# redirected somewhere the cron user can actually write - NOT /var/log/,
# which is root-owned and silently swallows the whole command's output
# (worse: the failed redirect itself means the command underneath never
# even runs) on a non-root deploy user's crontab:
#   0 3 * * * /opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy/scripts/backup.sh >> /opt/dhaka-kacchi/logs/backup.log 2>&1
#
# One-time setup before this can run:
#   1. Create a free Backblaze B2 account + a private bucket, then an
#      Application Key scoped to just that bucket (Account > App Keys).
#   2. `rclone config create backblaze-b2 b2 account=<keyID> key=<applicationKey>`
#      on the VPS host.
#   3. Run this script manually once, and do one test restore, before
#      trusting it for real. Day one of a real incident shouldn't be the
#      first time a restore is attempted.
set -euo pipefail

BACKUP_DIR="/opt/dhaka-kacchi/backups"
RETENTION_DAYS=30
RCLONE_REMOTE="backblaze-b2:dhaka-kacchi-backups-a1b2c3"
COMPOSE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/docker-compose.yml"
TIMESTAMP="$(date +%Y-%m-%d_%H%M%S)"

mkdir -p "$BACKUP_DIR"

for db in ordering warehouse; do
    dest="$BACKUP_DIR/${db}_${TIMESTAMP}.sql.gz"
    echo "Backing up $db -> $dest"
    docker compose -f "$COMPOSE_FILE" exec -T postgres \
        pg_dump -U postgres "$db" | gzip > "$dest"
done

echo "Pruning local backups older than $RETENTION_DAYS days"
find "$BACKUP_DIR" -name '*.sql.gz' -mtime "+$RETENTION_DAYS" -delete

echo "Syncing $BACKUP_DIR -> $RCLONE_REMOTE"
rclone copy "$BACKUP_DIR" "$RCLONE_REMOTE" --include '*.sql.gz'

echo "Backup complete: $TIMESTAMP"
