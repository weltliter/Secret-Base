# Shared by the deploy, backup and restore commands in manage.sh.
load_state_dir() {
    VAULT_STATE_DIR=$(python3 "$VAULT_PROJECT_ROOT/ops/config.py" state-dir)
}

vault_compose() {
    python3 "$VAULT_PROJECT_ROOT/ops/config.py" compose "$@"
}

require_root() {
    if [ "$(id -u)" -ne 0 ]; then
        printf '%s\n' 'Run this command as root.' >&2
        exit 1
    fi
}

prepare_maintenance_lock() {
    python3 - "$1/.backup.lock" <<'PY'
import os
import stat
import sys

# Keep the same inode for the host commands and the web backup manager.
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
try:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise SystemExit('Maintenance lock must be a regular file.')
    os.fchown(fd, 10001, 70)
    os.fchmod(fd, 0o600)
finally:
    os.close(fd)
PY
}

lock_maintenance() {
    BACKUP_ROOT="$VAULT_STATE_DIR/backups"
    [ ! -L "$BACKUP_ROOT" ] || exit 1
    install -d -o 10001 -g 70 -m 700 "$BACKUP_ROOT"
    prepare_maintenance_lock "$BACKUP_ROOT"
    exec 9<>"$BACKUP_ROOT/.backup.lock"
    flock -n 9 || { printf '%s\n' 'Another backup, restore or migration is running.' >&2; exit 1; }
}

prepare_query_key() {
    [ ! -L "$VAULT_STATE_DIR/secrets" ] || exit 1
    install -d -o root -g root -m 700 "$VAULT_STATE_DIR/secrets"
    python3 - "$VAULT_STATE_DIR/secrets/query-unlock.key" <<'PY'
import os
import secrets
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.is_symlink():
    raise SystemExit("Refusing a linked query key.")
if path.exists():
    if not stat.S_ISREG(path.stat().st_mode) or len(path.read_bytes()) != 32:
        raise SystemExit("Existing query key is invalid; it was not overwritten.")
else:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(secrets.token_bytes(32))
        stream.flush()
        os.fsync(stream.fileno())
os.chown(path, 10001, 70)
os.chmod(path, 0o400)
print("Query runtime key is ready.")
PY
}
