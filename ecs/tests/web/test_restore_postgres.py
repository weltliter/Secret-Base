"""Real pg_dump/upload/rollback checks, only on an explicitly disposable DB."""
import os
import subprocess
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict

from tests.support.records import entry
from tests.web.test_restore import HEADERS
from vault.backup.manager import BackupError, BackupManager
from vault.backup.restore import replace_data
from vault.core.service import VaultService
from vault.core.store import Store
from vault.web.app import create_app


@pytest.fixture
def database(tmp_path):
    dsn = os.environ.get("VAULT_RESTORE_TEST_DSN")
    if not dsn:
        pytest.skip("Set VAULT_RESTORE_TEST_DSN to a disposable PostgreSQL 17 database")
    assert conninfo_to_dict(dsn)["dbname"].startswith("vault_restore_test_")
    store = Store(dsn)
    store.initialize()
    with store.connection() as conn:
        conn.execute("TRUNCATE entries, settings, processed_messages, audit, query_logs, vault_metadata RESTART IDENTITY")
    machine = tmp_path / "machine.key"
    machine.write_bytes(os.urandom(32))
    service = VaultService(store, tmp_path / "runtime" / "vault.json", machine)
    manager = BackupManager(service, directory=tmp_path / "backups", lock_path=tmp_path / ".lock", reserve_bytes=0)
    with TestClient(create_app(service, start_bot=False, backup_manager=manager), base_url="http://127.0.0.1:18080") as client:
        client.headers["X-Vault-CSRF"] = client.get("/api/csrf").json()["token"]
        yield store, service, manager, client


def test_real_upload_replaces_all_tables_and_restores_credentials(database, tmp_path):
    store, service, manager, client = database
    recovery = client.post("/api/setup", json={"password": "source backup master"}).json()["recovery_key"]
    # Incompressible large encrypted value verifies upload > the normal JSON cap.
    active = service.save(entry(notes=os.urandom(2 * 1024**2).hex()))
    trash = service.save(entry(title="source trash"))
    service.trash({trash: 1})
    service.update_settings({"deepseek_api_key": "source fixture secret"})
    service.record_query("source query", [], {"ids": []})
    store.mark_message("source message")
    metadata = store.read_metadata()
    with store.connection() as conn:
        conn.execute("ALTER SEQUENCE audit_id_seq RESTART WITH 200")
    task = client.post("/api/backups", json={}).json()
    manager.worker.join(20)
    assert manager.get(task["id"])["status"] == "ready"
    dump = client.get(f'/api/backups/{task["id"]}/download').content
    assert len(dump) > 262144
    # Simulate an unrelated initialized target using a different encryption key.
    with store.connection() as conn:
        conn.execute("TRUNCATE entries, settings, processed_messages, audit, query_logs, vault_metadata RESTART IDENTITY")
    service.lock()
    assert client.post("/api/setup", json={"password": "target current master"}).status_code == 200
    target = service.save(entry(title="must disappear"))
    service.update_settings({"target_only": "must disappear"})
    service.record_query("target only query", [], {"ids": []})
    store.mark_message("target only message")
    original = store.read_metadata()
    # Broken archive cannot touch the target or invalidate its login.
    bad = client.post("/api/backups/restore", content=dump[:len(dump)//2], headers=HEADERS)
    assert bad.status_code == 400
    assert store.read_metadata() == original and service.entry(target)["title"] == "must disappear"
    response = client.post("/api/backups/restore", content=iter(dump[i:i+65536] for i in range(0, len(dump), 65536)), headers=HEADERS)
    assert response.status_code == 200, response.text
    assert store.read_metadata() == metadata
    assert not service.unlocked and not service.query_unlock.path.exists()
    assert client.get("/api/entries").status_code == 423
    assert client.post("/api/unlock", json={"password": "source backup master"}).status_code == 200
    assert [row["id"] for row in service.entries()] == [active]
    assert [row["id"] for row in service.entries(True)] == [trash]
    assert service.settings() == {"deepseek_api_key": "source fixture secret"}
    assert [row["query"] for row in service.query_logs()] == ["source query"]
    assert not store.mark_message("source message") and store.mark_message("target only message")
    client.post("/api/lock", json={})
    assert client.post("/api/unlock", json={"password": recovery}).status_code == 200
    service.save(entry(title="after restore"))
    with store.connection() as conn:
        assert conn.execute("SELECT max(id) AS n FROM audit").fetchone()["n"] == 200
    assert not list(manager.directory.glob("*.part")) and not list(manager.directory.glob("*.sql"))


def test_rollback_after_truncate_and_incompatible_metadata(database, tmp_path):
    store, service, _, client = database
    client.post("/api/setup", json={"password": "rollback fixture master"})
    original_id = service.save(entry())
    path = tmp_path / "data.sql"
    subprocess.run(["pg_dump", "--data-only", "--no-owner", "--dbname", store.dsn, "--file", str(path)], check=True, capture_output=True)
    target_id = service.save(entry(title="target after snapshot"))
    original = path.read_bytes()
    for damage in (original.replace(b"1\t2\n", b"1\t999\n"), original.replace(b"COPY public.settings", b"COPY public.unknown_table")):
        path.write_bytes(damage)
        with pytest.raises(BackupError):
            replace_data(store, path, lambda: None)
        assert {row["id"] for row in service.entries()} == {original_id, target_id}
    path.write_bytes(original)

    class InterruptedStore:
        @contextmanager
        def connection(self):
            with store.connection() as conn:
                class Connection:
                    def __getattr__(self, name):
                        return getattr(conn, name)
                    def execute(self, statement, *args):
                        result = conn.execute(statement, *args)
                        if statement.startswith("ALTER SEQUENCE public.audit_id_seq RESTART"):
                            raise RuntimeError("synthetic interruption after deletion and insertion")
                        return result
                yield Connection()
    with pytest.raises(RuntimeError):
        replace_data(InterruptedStore(), path, lambda: None)
    assert {row["id"] for row in service.entries()} == {original_id, target_id}
    with store.connection() as conn:
        assert conn.execute("SELECT last_value FROM audit_id_seq").fetchone()["last_value"] == 2
