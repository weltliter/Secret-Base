#!/bin/sh
set -eu
umask 077
VAULT_PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
. "$VAULT_PROJECT_ROOT/ops/deploy/common.sh"
[ "$#" -eq 2 ] && [ "$2" = --confirm-replace ] || {
    printf '%s\n' 'Usage: restore.sh /absolute/path/backup.dump --confirm-replace' 'Also accepts a legacy database.dump + vault.json tar.gz archive.' >&2
    exit 1
}
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'Run restore as root.' >&2; exit 1; }
ARCHIVE=$1
case "$ARCHIVE" in /*) ;; *) printf '%s\n' 'Use an absolute backup path.' >&2; exit 1 ;; esac
[ -f "$ARCHIVE" ] && [ ! -L "$ARCHIVE" ] || exit 1
BACKUP_ROOT="$VAULT_STATE_DIR/backups"
[ ! -L "$BACKUP_ROOT" ] || exit 1
install -d -o 10001 -g 70 -m 700 "$BACKUP_ROOT"
LOCK="$BACKUP_ROOT/.backup.lock"
[ ! -L "$LOCK" ] || exit 1
touch "$LOCK"
[ -f "$LOCK" ] || exit 1
chown 10001:70 "$LOCK"
chmod 600 "$LOCK"
exec 9<>"$LOCK"
flock -n 9 || { printf '%s\n' 'Another backup, restore or migration is running.' >&2; exit 1; }
STAGING=$(mktemp -d "$BACKUP_ROOT/.restore-XXXXXXXX")
STAGE_DB="vault_restore_$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
DB_CREATED=0
cleanup() {
    if [ "$DB_CREATED" = 1 ]; then vault_compose exec -T -u postgres db dropdb -U postgres --if-exists "$STAGE_DB" >/dev/null 2>&1 || true; fi
    case "$STAGING" in "$BACKUP_ROOT"/.restore-*) rm -rf -- "$STAGING" ;; esac
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
LEGACY=0
case "$ARCHIVE" in
    *.dump) cp -- "$ARCHIVE" "$STAGING/database.dump" ;;
    *.tar.gz)
        EXPECTED=$(printf 'database.dump\nvault.json')
        [ "$(tar -tzf "$ARCHIVE" | LC_ALL=C sort)" = "$EXPECTED" ] || { printf '%s\n' 'Invalid legacy archive members.' >&2; exit 1; }
        [ "$(tar -tvzf "$ARCHIVE" | cut -c 1 | LC_ALL=C sort -u)" = '-' ] || exit 1
        tar -xzf "$ARCHIVE" -C "$STAGING" --no-same-owner --no-same-permissions database.dump vault.json
        LEGACY=1 ;;
    *) printf '%s\n' 'Expected .dump or legacy .tar.gz.' >&2; exit 1 ;;
esac
[ -s "$STAGING/database.dump" ] && [ ! -L "$STAGING/database.dump" ] || exit 1
chown -R 10001:70 "$STAGING"
chmod 750 "$STAGING"
chmod 640 "$STAGING"/*
vault_compose up -d db
ATTEMPT=0
until vault_compose exec -T -u postgres db pg_isready -U postgres -d vault >/dev/null 2>&1; do
    ATTEMPT=$((ATTEMPT + 1))
    [ "$ATTEMPT" -lt 30 ] || exit 1
    sleep 2
done
[ "$(vault_compose exec -T -u postgres db id -u postgres)" = 70 ] || {
    printf '%s\n' 'Restore requires the configured postgres:17-alpine UID 70; incompatible database image.' >&2
    exit 1
}
vault_compose exec -T -u postgres db createdb -U postgres -O vault_app "$STAGE_DB"
DB_CREATED=1
# Dump SQL runs as the unprivileged application role, never as superuser.
vault_compose exec -T -u postgres db pg_restore --no-owner --role=vault_app --exit-on-error --single-transaction -U postgres -d "$STAGE_DB" < "$STAGING/database.dump"
STAGE_DSN="dbname=$STAGE_DB user=postgres host=/var/run/postgresql"
# UID 70 is postgres in postgres:17-alpine. Peer auth allows the isolated checker
# to use the scratch DB without widening pg_hba or reading machine runtime keys.
if [ "$LEGACY" = 1 ]; then
    vault_compose run --rm --no-deps --user 70:70 -e "DATABASE_URL=$STAGE_DSN" -v "$STAGING:/restore-input:ro" app python -m vault.backup.restore_check --legacy /restore-input/vault.json
else
    vault_compose run --rm --no-deps -T --user 70:70 -e "DATABASE_URL=$STAGE_DSN" app python -m vault.backup.restore_check
fi
vault_compose exec -T -u postgres db pg_dump --format=custom --no-owner -U postgres "$STAGE_DB" > "$STAGING/verified.dump"
# Only after preflight succeeds do we stop writers, snapshot and replace target.
vault_compose stop app
ROLLBACK="$BACKUP_ROOT/pre-restore-$(date -u +%Y%m%dT%H%M%SZ)-$$.dump"
vault_compose exec -T -u postgres db pg_dump --format=custom --no-owner -U postgres vault > "$ROLLBACK.part"
mv -- "$ROLLBACK.part" "$ROLLBACK"
if [ -f "$VAULT_STATE_DIR/data/vault/vault.json" ]; then
    cp -- "$VAULT_STATE_DIR/data/vault/vault.json" "$ROLLBACK.vault.json"
fi
vault_compose exec -T -u postgres db pg_restore --clean --if-exists --no-owner --role=vault_app --exit-on-error --single-transaction -U postgres -d vault < "$STAGING/verified.dump"
rm -f -- "$VAULT_STATE_DIR/data/vault/query-unlock.json"
sh "$VAULT_PROJECT_ROOT/ops/security/init-query-key.sh"
vault_compose up -d app
printf '%s\n' 'Restore complete. Log in with the original master password or recovery key to enable queries on this machine.'
printf 'Rollback database saved: %s\n' "$ROLLBACK"
