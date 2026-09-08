#!/usr/bin/env bash
# Nightly Postgres backup: pg_dump both databases (gzipped), keep 30 days
# locally, sync off-box via rclone to Contabo Object Storage - a genuinely
# separate failure domain from the VPS's own disk.
#
# Runs on the VPS HOST (not inside a container) via cron, since it needs
# `docker compose exec` and the host's own rclone config:
#   0 3 * * * /opt/dhaka-kacchi/dhaka_kacchi_ai_harness/deploy/scripts/backup.sh >> /var/log/dhaka-kacchi-backup.log 2>&1
#
# One-time setup before this can run:
#   1. `rclone config` on the VPS host - create a remote named
#      `contabo-object-storage` pointing at the Object Storage bucket.
#   2. Run this script manually once, and do one test restore, before
#      trusting it for real. Day one of a real incident shouldn't be the
#      first time a restore is attempted.
set -euo pipefail

BACKUP_DIR="/opt/dhaka-kacchi/backups"
RETENTION_DAYS=30
RCLONE_REMOTE="contabo-object-storage:dhaka-kacchi-backups"
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
