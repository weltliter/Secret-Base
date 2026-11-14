#!/bin/sh
set -eu
umask 077
VAULT_PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
. "$VAULT_PROJECT_ROOT/ops/deploy/common.sh"
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'Run bootstrap as root.' >&2; exit 1; }
ACTION=${1:---prepare-only}
case "$ACTION" in --prepare-only|--start) ;; *) printf '%s\n' 'Usage: bootstrap.sh [--prepare-only|--start]' >&2; exit 1 ;; esac
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
python3 - "$VAULT_STATE_DIR/backups/.backup.lock" <<'PY'
import os
import stat
import sys
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
try:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise SystemExit('Maintenance lock must be a regular file.')
    os.fchown(fd, 10001, 70)
    os.fchmod(fd, 0o600)
finally:
    os.close(fd)
PY
sh "$VAULT_PROJECT_ROOT/ops/security/init-query-key.sh"
python3 "$VAULT_PROJECT_ROOT/ops/models/fetch-embedding.py" --directory "$VAULT_STATE_DIR/models/bge-small-zh-v1.5"
vault_compose config --quiet
if [ "$ACTION" = --start ]; then
    vault_compose up -d --build
fi
printf '%s\n' 'Environment ready. Existing vaults require restoration or explicit migration before application login.'
