# CLI backup and disaster recovery, called by manage.sh.
backup_vault() {
    umask 077
    if [ "$#" -eq 0 ]; then
        load_state_dir
        vault_compose run --rm --no-deps -T -e VAULT_BACKUP_DIR=/backups/manual app python -m vault.backup.cli
        printf 'Host backup directory: %s/backups/manual\n' "$VAULT_STATE_DIR"
        return
    fi
    if [ "$#" -ne 1 ] || [ "$1" != --legacy ]; then
        printf '%s\n' 'Usage: sh manage.sh backup [--legacy]' >&2
        exit 1
    fi
    require_root
    load_state_dir
    backup_legacy
}

backup_legacy() {
    ENVELOPE="$VAULT_STATE_DIR/data/vault/vault.json"
    [ -f "$ENVELOPE" ] && [ ! -L "$ENVELOPE" ] || { printf '%s\n' 'Legacy envelope is missing.' >&2; exit 1; }
    lock_maintenance
    STAGING=$(mktemp -d "$BACKUP_ROOT/.legacy-XXXXXXXX")
    trap cleanup_legacy EXIT
    trap 'exit 1' HUP INT TERM

    vault_compose exec -T -u postgres db pg_dump --format=custom --no-owner -U postgres vault > "$STAGING/database.dump"
    cp -- "$ENVELOPE" "$STAGING/vault.json"
    ARCHIVE="$BACKUP_ROOT/vault-legacy-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
    tar -czf "$STAGING/archive.tar.gz" -C "$STAGING" database.dump vault.json
    mv -- "$STAGING/archive.tar.gz" "$ARCHIVE"
    sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"
    printf 'Legacy rollback backup created: %s\n' "$ARCHIVE"
}

cleanup_legacy() {
    case "$STAGING" in "$BACKUP_ROOT"/.legacy-*) rm -rf -- "$STAGING" ;; esac
}

restore_vault() {
    [ "$#" -eq 2 ] && [ "$2" = --confirm-replace ] || {
        printf '%s\n' 'Usage: sh manage.sh restore /absolute/path/backup.dump --confirm-replace' 'Also accepts a legacy database.dump + vault.json tar.gz archive.' >&2
        exit 1
    }
    require_root
    umask 077
    ARCHIVE=$1
    case "$ARCHIVE" in /*) ;; *) printf '%s\n' 'Use an absolute backup path.' >&2; exit 1 ;; esac
    if [ ! -f "$ARCHIVE" ] || [ -L "$ARCHIVE" ]; then
        printf '%s\n' 'Restore input must be a regular backup file.' >&2
        exit 1
    fi

    load_state_dir
    lock_maintenance
    STAGING=$(mktemp -d "$BACKUP_ROOT/.restore-XXXXXXXX")
    STAGE_DB="vault_restore_$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
    DB_CREATED=0
    trap cleanup_restore EXIT
    trap 'exit 1' HUP INT TERM

    LEGACY=0
    case "$ARCHIVE" in
        *.dump) cp -- "$ARCHIVE" "$STAGING/database.dump" ;;
        *.tar.gz)
            EXPECTED=$(printf 'database.dump\nvault.json')
            [ "$(tar -tzf "$ARCHIVE" | LC_ALL=C sort)" = "$EXPECTED" ] || { printf '%s\n' 'Invalid legacy archive members.' >&2; exit 1; }
            [ "$(tar -tvzf "$ARCHIVE" | cut -c 1 | LC_ALL=C sort -u)" = '-' ] || exit 1
            tar -xzf "$ARCHIVE" -C "$STAGING" --no-same-owner --no-same-permissions database.dump vault.json
            LEGACY=1
            ;;
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
    # UID 70 can access the scratch database through peer authentication.
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
    prepare_query_key
    vault_compose up -d app
    printf '%s\n' 'Restore complete. Log in with the original master password or recovery key to enable queries on this machine.'
    printf 'Rollback database saved: %s\n' "$ROLLBACK"
}

cleanup_restore() {
    if [ "$DB_CREATED" = 1 ]; then
        vault_compose exec -T -u postgres db dropdb -U postgres --if-exists "$STAGE_DB" >/dev/null 2>&1 || true
    fi
    case "$STAGING" in "$BACKUP_ROOT"/.restore-*) rm -rf -- "$STAGING" ;; esac
}
