#!/bin/sh
set -eu
umask 077
VAULT_PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
. "$VAULT_PROJECT_ROOT/ops/deploy/common.sh"
if [ "$#" -eq 0 ]; then
    vault_compose run --rm --no-deps -T -e VAULT_BACKUP_DIR=/backups/manual app python -m vault.backup.cli
    printf 'Host backup directory: %s/backups/manual\n' "$VAULT_STATE_DIR"
    exit 0
fi
[ "$#" -eq 1 ] && [ "$1" = --legacy ] || { printf '%s\n' 'Usage: backup.sh [--legacy]' >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'Legacy backup requires root.' >&2; exit 1; }
BACKUP_ROOT="$VAULT_STATE_DIR/backups"
ENVELOPE="$VAULT_STATE_DIR/data/vault/vault.json"
[ -f "$ENVELOPE" ] && [ ! -L "$ENVELOPE" ] || { printf '%s\n' 'Legacy envelope is missing.' >&2; exit 1; }
[ ! -L "$BACKUP_ROOT" ] || exit 1
install -d -o 10001 -g 70 -m 700 "$BACKUP_ROOT"
LOCK="$BACKUP_ROOT/.backup.lock"
[ ! -L "$LOCK" ] || exit 1
# Preserve the inode even when another process holds it.
touch "$LOCK"
[ -f "$LOCK" ] || exit 1
chown 10001:70 "$LOCK"
chmod 600 "$LOCK"
exec 9<>"$LOCK"
flock -n 9 || { printf '%s\n' 'Another maintenance operation is running.' >&2; exit 1; }
STAGING=$(mktemp -d "$BACKUP_ROOT/.legacy-XXXXXXXX")
cleanup() { case "$STAGING" in "$BACKUP_ROOT"/.legacy-*) rm -rf -- "$STAGING" ;; esac; }
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
vault_compose exec -T -u postgres db pg_dump --format=custom --no-owner -U postgres vault > "$STAGING/database.dump"
cp -- "$ENVELOPE" "$STAGING/vault.json"
ARCHIVE="$BACKUP_ROOT/vault-legacy-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
tar -czf "$STAGING/archive.tar.gz" -C "$STAGING" database.dump vault.json
mv -- "$STAGING/archive.tar.gz" "$ARCHIVE"
sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"
printf 'Legacy rollback backup created: %s\n' "$ARCHIVE"
