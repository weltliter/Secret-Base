"""CLI snapshot using the same bounded engine as the management UI."""
import os
import time
from pathlib import Path
from ..core.service import VaultService
from ..core.store import Store
from .manager import BackupManager

def main():
    service = VaultService(Store(), Path(os.environ.get("VAULT_DATA_DIR", "/data")) / "vault.json")
    service.store.initialize()
    manager = BackupManager(service)
    manager.start()
    try:
        task = manager.create()
        while task["status"] == "generating":
            time.sleep(0.2)
            task = manager.get(task["id"])
        if task["status"] != "ready":
            raise SystemExit(task.get("error") or "Backup failed")
        path = manager.directory / (task["id"] + ".dump")
        path.with_suffix(".sha256").write_text(f'{task["sha256"]}  {path.name}\n', encoding="utf-8")
        print(f'Backup created: {path}; bytes={task["size_bytes"]}; sha256={task["sha256"]}')
    finally:
        manager.close()

if __name__ == '__main__':
    main()
