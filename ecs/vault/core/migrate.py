"""Explicit 1.0 envelope import; never reads files during normal app startup."""
import argparse
import getpass
from pathlib import Path
import stat

from .maintenance import maintenance_lock
from .store import Store


def regular_file(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        raise ValueError("迁移输入必须是非空普通文件，不能是链接")
    return path


def migrate(store, envelope_path, password, *, backup_path, lock_path=None):
    """The caller must create a matching database+envelope backup first.

    The backup parameter is checked as a nonempty regular file. Pair matching
    and backup creation are the responsibility of the maintenance wrapper;
    this function does not claim arbitrary files are validated backups.
    """
    with maintenance_lock(lock_path):
        regular_file(backup_path)
        source = regular_file(envelope_path)
        if source.stat().st_size > 16384:
            raise ValueError("密钥信封过大")
        raw = source.read_bytes()
        # Reject bad credentials before even creating additive schema objects.
        from .crypto import unlock_envelope
        unlock_envelope(raw, password)
        store.initialize(allow_legacy=True)
        return store.import_legacy_envelope(raw, password)


def main():
    parser = argparse.ArgumentParser(description="Import a backed-up 1.0 envelope into the database; stop the app first.")
    parser.add_argument("--envelope", required=True, type=Path)
    parser.add_argument("--backup", required=True, type=Path,
                        help="Existing matching pre-migration database+envelope backup (must be created first)")
    args = parser.parse_args()
    try:
        credential = getpass.getpass("Master password or recovery key: ")
        changed = migrate(Store(), args.envelope, credential, backup_path=args.backup)
    except (ValueError, OSError):
        raise SystemExit("Migration refused. Check the backup, credential, envelope and database; no existing envelope was replaced.") from None
    print("Migration complete." if changed else "Already migrated; encrypted data verified.")


if __name__ == "__main__":
    main()
