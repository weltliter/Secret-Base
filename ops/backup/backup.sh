#!/bin/sh
set -eu
umask 077

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
if [ "$PROJECT_ROOT" != '/opt/wechat-vault' ]; then
    printf '%s\n' 'Run the deployed backup script from its deployed /opt/wechat-vault/ops subdirectory.' >&2
    exit 1
fi
BACKUP_ROOT="$PROJECT_ROOT/backups"
ENVELOPE="$PROJECT_ROOT/data/vault/vault.json"
COMPOSE_FILE="$PROJECT_ROOT/compose.yaml"

if [ ! -f "$ENVELOPE" ] || [ ! -f "$COMPOSE_FILE" ]; then
    printf '%s\n' 'Vault has not been initialized, or the deployment files are missing.' >&2
    exit 1
fi
if [ -L "$BACKUP_ROOT" ]; then
    printf '%s\n' 'Refusing a symlink as the backup directory.' >&2
    exit 1
fi
mkdir -p -- "$BACKUP_ROOT"
if [ "$(CDPATH= cd -- "$BACKUP_ROOT" && pwd -P)" != "$PROJECT_ROOT/backups" ]; then
    printf '%s\n' 'Backup directory is outside the project.' >&2
    exit 1
fi

exec 9>"$BACKUP_ROOT/.backup.lock"
if ! flock -n 9; then
    printf '%s\n' 'Another backup is already running.' >&2
    exit 1
fi

STAGING=$(mktemp -d "$BACKUP_ROOT/.backup-XXXXXXXX")
ARCHIVE="$BACKUP_ROOT/vault-backup-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
cleanup() {
    case "$STAGING" in
        "$BACKUP_ROOT"/.backup-*) rm -rf -- "$STAGING" ;;
    esac
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

# Business fields are encrypted before they reach PostgreSQL. The envelope
# contains only wrapped keys, salts, and KDF parameters; never copy logs or keys.
docker compose -f "$COMPOSE_FILE" exec -T -u postgres db \
    pg_dump --format=custom --no-owner -U postgres vault > "$STAGING/database.dump"
cp -- "$ENVELOPE" "$STAGING/vault.json"
tar -czf "$STAGING/archive.tar.gz" -C "$STAGING" database.dump vault.json
mv -- "$STAGING/archive.tar.gz" "$ARCHIVE"

# The exact, verified directory and filename pattern constrain retention cleanup.
find "$BACKUP_ROOT" -maxdepth 1 -type f \
    -name 'vault-backup-????????T??????Z.tar.gz' -mmin +10080 -delete
printf 'Backup created: %s\n' "$ARCHIVE"
