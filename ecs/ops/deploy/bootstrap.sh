# First deployment and preparation for disaster recovery.
deploy_vault() {
    case "$#:${1-}" in
        0:|1:--prepare-only) ;;
        *) printf '%s\n' 'Usage: sh manage.sh deploy [--prepare-only]' >&2; exit 1 ;;
    esac
    require_root
    umask 077
    load_state_dir
    vault_compose config --quiet

    # Reject linked descendants before privileged directory/file operations.
    python3 - "$VAULT_STATE_DIR" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
for relative in ('data', 'data/postgres', 'data/pgsocket', 'data/vault', 'backups', 'backups/web', 'secrets', 'models', 'models/bge-small-zh-v1.5'):
    if (root / relative).is_symlink():
        raise SystemExit('Linked state directory refused: ' + relative)
PY
    install -d -m 700 "$VAULT_STATE_DIR" "$VAULT_STATE_DIR/data"
    install -d -o 10001 -g 70 -m 700 "$VAULT_STATE_DIR/data/vault" "$VAULT_STATE_DIR/backups" "$VAULT_STATE_DIR/backups/web"
    install -d -o 70 -g 70 -m 770 "$VAULT_STATE_DIR/data/pgsocket"
    install -d -o 70 -g 70 -m 700 "$VAULT_STATE_DIR/data/postgres"
    install -d -m 755 "$VAULT_STATE_DIR/models" "$VAULT_STATE_DIR/models/bge-small-zh-v1.5"
    prepare_maintenance_lock "$VAULT_STATE_DIR/backups"
    prepare_query_key
    python3 "$VAULT_PROJECT_ROOT/ops/models/fetch-embedding.py"

    if [ "$#" -eq 0 ]; then
        vault_compose up -d --build
    fi
    printf '%s\n' 'Environment ready. Existing vaults require restoration or explicit migration before application login.'
}
