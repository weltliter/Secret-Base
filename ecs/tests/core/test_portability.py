"""Independent portability checks: only database state crosses the machine boundary."""
import copy
import os
import uuid
from pathlib import Path

import pytest

from tests.support.records import entry
from tests.support.store import MemoryStore
from vault.core.crypto import create_envelope, json_encrypt
from vault.core.migrate import migrate
from vault.core.service import VaultService
from vault.core.store import Store


MASTER = "synthetic portability master 2026"


def populated(tmp_path):
    store = MemoryStore()
    machine = tmp_path / "original-machine.key"
    machine.write_bytes(os.urandom(32))
    service = VaultService(store, tmp_path / "old-host" / "vault.json", machine)
    recovery = service.setup(MASTER)
    active = service.save(entry())
    deleted = service.save(entry(title="Recoverable trash"))
    service.trash({deleted: 1})
    service.update_settings({"deepseek_api_key": "synthetic-api-secret", "wechat_token": "synthetic-token"})
    service.record_query("synthetic private query", [], {"ids": []})
    return store, service, machine, recovery, active, deleted


@pytest.mark.parametrize("use_recovery", [False, True])
def test_database_is_sufficient_on_a_new_machine(tmp_path, monkeypatch, use_recovery):
    store, old, machine, recovery, active, deleted = populated(tmp_path)
    copied_database = copy.deepcopy(store)
    assert not old.envelope.path.exists()
    machine.unlink()
    old.query_unlock.path.unlink()
    old.lock()
    # SSH settings are intentionally neither arguments nor inputs to the vault.
    monkeypatch.setenv("ECS_PRIVATE_KEY_FILE", str(tmp_path / "does-not-exist.pem"))
    monkeypatch.setenv("ECS_HOST", "replacement.example.invalid")
    new_machine = tmp_path / "replacement-machine.key"
    new_machine.write_bytes(os.urandom(32))
    replacement = VaultService(copied_database, tmp_path / "replacement" / "vault.json", new_machine)
    assert not replacement.resume_queries()
    replacement.unlock(recovery if use_recovery else MASTER)
    assert replacement.entry(active)["password"] == entry()["password"]
    assert replacement.entries(True)[0]["id"] == deleted
    assert replacement.settings()["deepseek_api_key"] == "synthetic-api-secret"
    assert replacement.query_logs()[0]["query"] == "synthetic private query"
    assert not replacement.envelope.path.exists()
    restarted = VaultService(copied_database, replacement.envelope.path, new_machine)
    assert restarted.resume_queries()
    assert restarted.entry(active)["password"] == entry()["password"]


def test_wrong_credential_does_not_initialize_a_replacement_vault(tmp_path):
    store, old, _, _, _, _ = populated(tmp_path)
    before = copy.deepcopy(store.metadata)
    replacement = VaultService(store, tmp_path / "fresh" / "vault.json")
    with pytest.raises(ValueError):
        replacement.unlock("incorrect synthetic password")
    assert not replacement.unlocked
    assert store.metadata == before
    assert not replacement.envelope.path.exists()
    assert old.unlocked


@pytest.mark.parametrize("field", ["schema_version", "crypto_format"])
def test_unknown_versions_fail_closed(tmp_path, field):
    store, _, _, _, _, _ = populated(tmp_path)
    store.metadata[field] = 999
    before = copy.deepcopy(store.metadata)
    with pytest.raises(ValueError):
        store.initialize()
    with pytest.raises(ValueError):
        VaultService(store, tmp_path / "fresh" / "vault.json").unlock(MASTER)
    assert store.metadata == before


def legacy_fixture(tmp_path):
    store, service, _, recovery, _, _ = populated(tmp_path)
    raw = store.metadata["envelope"]
    store.metadata = None
    envelope = tmp_path / "legacy-vault.json"
    envelope.write_bytes(raw)
    # The migration API checks this prerequisite; ops wrappers validate/create
    # the real paired archive. A byte marker does not claim archive validation.
    backup = tmp_path / "pre-migration.backup"
    backup.write_bytes(b"synthetic pre-backup marker")
    return store, service, envelope, backup, recovery


def test_legacy_migration_preserves_exact_bytes_and_is_idempotent(tmp_path):
    store, _, envelope, backup, recovery = legacy_fixture(tmp_path)
    original_rows = copy.deepcopy(store.rows)
    raw = envelope.read_bytes()
    assert migrate(store, envelope, recovery, backup_path=backup, lock_path=tmp_path / "maintenance.lock")
    assert store.metadata["envelope"] == raw
    assert store.rows == original_rows
    assert not migrate(store, envelope, MASTER, backup_path=backup, lock_path=tmp_path / "maintenance.lock")


@pytest.mark.parametrize("damage", ["wrong_password", "record", "settings", "old_log"])
def test_failed_legacy_migration_never_publishes_an_envelope(tmp_path, damage):
    store, service, envelope, backup, _ = legacy_fixture(tmp_path)
    if damage == "record":
        next(iter(store.rows.values()))["payload"] = b"corrupted"
    elif damage == "settings":
        store.settings_blob = b"corrupted"
    elif damage == "old_log":
        # Ensure migration checks beyond the UI's 50-row query-log page.
        for index in range(60):
            log_id = str(uuid.uuid4())
            store.add_query_log(log_id, json_encrypt(service.key, {"query": str(index)}, "query-log:" + log_id))
        store.query_history[-1]["payload"] = b"corrupted"
    with pytest.raises(ValueError):
        migrate(store, envelope, "incorrect master" if damage == "wrong_password" else MASTER,
                backup_path=backup, lock_path=tmp_path / "maintenance.lock")
    assert store.metadata is None


def test_migration_conflict_cannot_replace_existing_envelope(tmp_path):
    store, _, envelope, backup, _ = legacy_fixture(tmp_path)
    assert migrate(store, envelope, MASTER, backup_path=backup, lock_path=tmp_path / "maintenance.lock")
    original = copy.deepcopy(store.metadata)
    other, _, _ = create_envelope(MASTER)
    envelope.write_bytes(other)
    with pytest.raises(ValueError):
        migrate(store, envelope, MASTER, backup_path=backup, lock_path=tmp_path / "maintenance.lock")
    assert store.metadata == original


@pytest.fixture
def postgres_store():
    dsn = os.environ.get("VAULT_TEST_DSN")
    if not dsn:
        pytest.skip("Set VAULT_TEST_DSN to a synthetic PostgreSQL database for transaction checks")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    schema = "vault_portability_" + uuid.uuid4().hex
    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield Store(make_conninfo(dsn, options="-c search_path=" + schema))
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_migration_checks_all_ciphertext_transactionally(postgres_store, tmp_path):
    store = postgres_store
    store.initialize()
    service = VaultService(store, tmp_path / "runtime" / "vault.json")
    service.setup(MASTER)
    service.save(entry())
    for number in range(65):
        service.record_query(f"old query {number}", [], {"ids": []})
    raw = store.read_envelope()
    with store.connection() as conn:
        conn.execute("DELETE FROM vault_metadata")
        conn.execute("UPDATE query_logs SET payload=%s WHERE id=(SELECT id FROM query_logs ORDER BY created_at LIMIT 1)", (b"corrupt",))
    with pytest.raises(ValueError):
        store.import_legacy_envelope(raw, MASTER)
    assert store.read_envelope() is None
    with store.connection() as conn:
        assert conn.execute("SELECT count(*) AS count FROM entries").fetchone()["count"] == 1


def test_postgres_unknown_schema_is_rejected_before_ddl(postgres_store):
    store = postgres_store
    with store.connection() as conn:
        conn.execute("CREATE TABLE vault_schema(id integer PRIMARY KEY, version integer NOT NULL)")
        conn.execute("INSERT INTO vault_schema VALUES(1,999)")
    with pytest.raises(ValueError):
        store.initialize()
    with store.connection() as conn:
        assert conn.execute("SELECT to_regclass('entries') AS name").fetchone()["name"] is None
        assert conn.execute("SELECT version FROM vault_schema").fetchone()["version"] == 999
