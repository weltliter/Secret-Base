"""Synthetic export tests; real PostgreSQL restore is the integration gate."""
import hashlib
import json
import threading
from types import SimpleNamespace

import pytest

from vault.backup.manager import BackupError, BackupManager
from vault.core.maintenance import maintenance_lock
from fastapi.testclient import TestClient
from vault.core.service import VaultService
from vault.web.app import create_app
from tests.support.store import MemoryStore

PAYLOAD = b"PGDMP" + b"synthetic-encrypted-database" * 9000


def fake_export(path, deadline):
    path.write_bytes(PAYLOAD)


def manager_for(service, tmp_path, **kwargs):
    return BackupManager(service, directory=tmp_path / "web", lock_path=tmp_path / ".backup.lock",
                         exporter=kwargs.pop("exporter", fake_export), reserve_bytes=0, **kwargs)


def await_task(manager, task):
    manager.worker.join(5)
    assert not manager.worker.is_alive()
    return manager.get(task["id"])


@pytest.fixture
def configured(tmp_path):
    service = VaultService(MemoryStore(), tmp_path / "runtime" / "vault.json")
    manager = manager_for(service, tmp_path)
    with TestClient(create_app(service, start_bot=False, backup_manager=manager), base_url="http://127.0.0.1:18080") as client:
        client.headers["X-Vault-CSRF"] = client.get("/api/csrf").json()["token"]
        assert client.post("/api/setup", json={"password": "synthetic backup master"}).status_code == 200
        yield client, manager, service


def test_native_download_checksums_metadata_and_auth(configured):
    client, manager, service = configured
    task = client.post("/api/backups", json={}).json()
    task = await_task(manager, task)
    assert task["status"] == "ready"
    response = client.get(f'/api/backups/{task["id"]}/download')
    assert response.status_code == 200 and response.content == PAYLOAD
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert task["sha256"] == hashlib.sha256(response.content).hexdigest()
    assert response.headers["content-length"] == str(len(PAYLOAD))
    assert client.get(f'/api/backups/{task["id"]}/checksum').text.startswith(task["sha256"])
    manifest = client.get(f'/api/backups/{task["id"]}/manifest').json()
    assert manifest["schema_version"] == 2 and manifest["backup_format"] == 2
    assert "envelope" not in json.dumps(manifest)
    assert client.get(f'/api/backups/{task["id"]}/download', headers={"Range": "bytes=1-"}).status_code == 416
    assert client.get('/api/backups/not-a-valid-id').status_code == 404
    epoch = service.epoch
    lease = manager.download(task["id"])
    assert client.post("/api/lock", json={}).status_code == 200
    assert service.unlocked and service.epoch == epoch
    assert client.get(f'/api/backups/{task["id"]}/download').status_code == 423
    assert client.post("/api/backups", json={}).status_code == 423
    assert b"".join(lease.chunks()) == PAYLOAD
    assert client.post("/api/unlock", json={"password": "synthetic backup master"}).status_code == 200
    assert client.get("/api/backups").json()["tasks"][0]["id"] == task["id"]


def test_creation_requires_csrf_and_origin(configured):
    client, manager, _ = configured
    client.headers.pop("X-Vault-CSRF")
    assert client.post("/api/backups", json={}).status_code == 403
    assert client.post("/api/backups", json={}, headers={"Origin": "https://example.invalid"}).status_code == 403
    assert not manager.tasks


def test_double_click_is_one_export_and_failed_partial_not_published(api, tmp_path):
    _, service, _ = api
    service.setup("synthetic test master")
    entered, release = threading.Event(), threading.Event()
    calls = []
    def exporter(path, deadline):
        calls.append(path)
        entered.set()
        assert release.wait(5)
        path.write_bytes(PAYLOAD)
        raise RuntimeError("secret-never-in-task")
    manager = manager_for(service, tmp_path, exporter=exporter)
    manager.start()
    try:
        one = manager.create()
        assert entered.wait(2)
        assert manager.create()["id"] == one["id"]
        release.set()
        failed = await_task(manager, one)
        assert failed["status"] == "failed" and len(calls) == 1
        assert "secret-never-in-task" not in json.dumps(failed)
        assert not list(manager.directory.glob("*.part"))
        assert not list(manager.directory.glob("*.dump"))
        with pytest.raises(BackupError) as error:
            manager.download(one["id"])
        assert error.value.status == 409
    finally:
        release.set()
        manager.close()


def test_retention_pins_and_download_concurrency(api, tmp_path):
    _, service, _ = api
    service.setup("synthetic test master")
    clock = [1000]
    manager = manager_for(service, tmp_path, clock=lambda: clock[0], retention_days=1)
    manager.start()
    try:
        task = await_task(manager, manager.create())
        first, second = manager.download(task["id"]), manager.download(task["id"])
        with pytest.raises(BackupError) as error:
            manager.download(task["id"])
        assert error.value.status == 429
        clock[0] += 86401
        assert manager.get(task["id"])["status"] == "expired"
        path = manager.directory / (task["id"] + ".dump")
        assert path.exists()
        second.close()
        assert path.exists()
        assert b"".join(first.chunks()) == PAYLOAD
        assert not path.exists()
        with pytest.raises(BackupError) as error:
            manager.download(task["id"])
        assert error.value.status == 410
    finally:
        manager.close()


def test_deadline_limits_existing_transfer(api, tmp_path):
    _, service, _ = api
    service.setup("synthetic test master")
    clock = [10]
    manager = manager_for(service, tmp_path, monotonic=lambda: clock[0], download_timeout_seconds=1)
    manager.start()
    try:
        task = await_task(manager, manager.create())
        stream = manager.download(task["id"])
        chunks = stream.chunks()
        assert len(next(chunks)) <= 65536
        clock[0] = 12
        assert list(chunks) == []
        assert stream.closed and sum(manager.pins.values()) == 0
    finally:
        manager.close()


def test_quota_space_and_external_maintenance_fail_closed(api, tmp_path, monkeypatch):
    _, service, _ = api
    service.setup("synthetic test master")
    manager = manager_for(service, tmp_path)
    manager.start()
    try:
        with maintenance_lock(manager.lock_path):
            task = await_task(manager, manager.create())
        assert task["status"] == "failed" and task["error_code"] == "busy"
        manager.limits["quota_bytes"] = 1
        with pytest.raises(BackupError) as error:
            manager.create()
        assert error.value.status == 507
        manager.limits["quota_bytes"] = 5 * 1024**3
        manager.limits["reserve_bytes"] = 1
        monkeypatch.setattr("vault.backup.manager.shutil.disk_usage", lambda path: SimpleNamespace(free=0))
        with pytest.raises(BackupError) as error:
            manager.create()
        assert error.value.status == 507
    finally:
        manager.close()


def test_restarts_keep_ready_tasks_and_fail_interrupted(api, tmp_path):
    _, service, _ = api
    service.setup("synthetic test master")
    manager = manager_for(service, tmp_path)
    manager.start()
    task = await_task(manager, manager.create())
    manager.close()
    metadata_path = manager.directory / (task["id"] + ".json")
    stored = json.loads(metadata_path.read_bytes())
    stored.update(status="generating", phase="export")
    metadata_path.write_text(json.dumps(stored), encoding="utf-8")
    other_backup = manager.directory / "my-important-backup.dump"
    other_backup.write_bytes(b"never delete user backups")
    restarted = manager_for(service, tmp_path)
    restarted.start()
    try:
        assert restarted.get(task["id"])["error_code"] == "interrupted"
        assert not (manager.directory / (task["id"] + ".dump")).exists()
        assert other_backup.read_bytes() == b"never delete user backups"
    finally:
        restarted.close()


def test_generation_timeout_never_publishes_partial(api, tmp_path):
    _, service, _ = api
    service.setup("synthetic test master")
    clock = [0]
    def too_slow(path, deadline):
        path.write_bytes(PAYLOAD)
        clock[0] = 2
    manager = manager_for(service, tmp_path, exporter=too_slow, timeout_seconds=1, monotonic=lambda: clock[0])
    manager.start()
    try:
        task = await_task(manager, manager.create())
        assert task["status"] == "failed" and task["error_code"] == "timeout"
        assert not list(manager.directory.glob("*.dump"))
        assert not list(manager.directory.glob("*.part"))
    finally:
        manager.close()
