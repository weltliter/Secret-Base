#!/bin/sh
# Source after assigning VAULT_PROJECT_ROOT from the calling script's location.
set -eu
: "${VAULT_PROJECT_ROOT:?Set the source checkout root before sourcing common.sh}"
VAULT_PROJECT_ROOT=$(CDPATH= cd -- "$VAULT_PROJECT_ROOT" && pwd -P)
if [ ! -f "$VAULT_PROJECT_ROOT/compose.yaml" ]; then
    printf '%s\n' 'Missing compose.yaml in source checkout.' >&2
    exit 1
fi
VAULT_STATE_DIR=$(python3 - "$VAULT_PROJECT_ROOT/.env" <<'PY'
import os
import sys
from pathlib import Path
value = os.environ.get('VAULT_STATE_DIR')
config = Path(sys.argv[1])
if not value and config.is_file():
    for line in config.read_text(encoding='utf-8-sig').splitlines():
        name, sep, setting = line.partition('=')
        if sep and name.strip() == 'VAULT_STATE_DIR':
            value = setting.strip().strip('\"\'')
value = value or '/var/lib/wechat-vault'
path = Path(value)
if not path.is_absolute() or '..' in path.parts or path in (Path('/'), Path('/var'), Path('/var/lib'), Path('/opt'), Path('/home')):
    raise SystemExit('State directory must be an explicit absolute project-specific path.')
if any(p.is_symlink() for p in (path, *path.parents)):
    raise SystemExit('State directory must not contain symlinks.')
print(path.resolve())
PY
)
export VAULT_PROJECT_ROOT VAULT_STATE_DIR
vault_compose() {
    docker compose --project-directory "$VAULT_PROJECT_ROOT" -f "$VAULT_PROJECT_ROOT/compose.yaml" "$@"
}
