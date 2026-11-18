"""Preflight a scratch database before allowing replacement of the target."""
import argparse
import getpass
import stat
from pathlib import Path
from ..core.crypto import unlock_envelope
from ..core.store import Store, BUSINESS_TABLES

def validate(store, legacy=None, credential=None):
    with store.connection() as conn:
        name = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        if not name.startswith("vault_restore_"):
            raise ValueError("Restore preflight requires a scratch database")
        for table in BUSINESS_TABLES:
            if not conn.execute("SELECT to_regclass(%s) AS name", (table,)).fetchone()["name"]:
                raise ValueError("Backup is missing an application table")
    if legacy is not None:
        path = Path(legacy)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 16384:
            raise ValueError("Invalid legacy envelope file")
        raw = path.read_bytes()
        unlock_envelope(raw, credential)
        store.initialize(allow_legacy=True)
        store.import_legacy_envelope(raw, credential)
    else:
        with store.connection() as conn:
            for table in ("vault_schema", "vault_metadata"):
                if not conn.execute("SELECT to_regclass(%s) AS name", (table,)).fetchone()["name"]:
                    raise ValueError("Standalone dump is missing key/version metadata")
        if store.read_metadata() is None:
            raise ValueError("Backup has no encrypted envelope")
    return True

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy", type=Path)
    args = parser.parse_args()
    try:
        password = getpass.getpass("Original master password or recovery key: ") if args.legacy else None
        validate(Store(), args.legacy, password)
    except Exception:
        raise SystemExit("Restore preflight failed: check backup format/version, envelope and original credential. Target was not replaced.") from None
    print("Scratch database preflight passed.")

if __name__ == '__main__':
    main()
