#!/bin/sh
set -eu
umask 077
VAULT_PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
. "$VAULT_PROJECT_ROOT/ops/deploy/common.sh"
[ "$(id -u)" -eq 0 ] || { printf '%s\n' 'Run as root.' >&2; exit 1; }
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
print("Query runtime key is ready; no secret value was printed.")
PY
