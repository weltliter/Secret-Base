"""Upload admission and runtime invalidation; real database tests are separate."""
import io
import pytest

from tests.web.test_backups import configured, PAYLOAD
from vault.backup.manager import BackupError
from vault.backup.restore import copy_data
from vault.core.maintenance import maintenance_lock

HEADERS = {"Content-Type": "application/octet-stream", "X-Vault-Confirm-Replace": "replace-all"}


def upload(client, content=PAYLOAD, headers=None):
    return client.post("/api/backups/restore", content=content, headers=HEADERS if headers is None else headers)


def test_upload_requires_auth_csrf_origin_and_explicit_confirmation(configured):
    client, manager, _ = configured
    assert upload(client, headers={"Content-Type": "application/octet-stream"}).status_code == 400
    assert upload(client, headers={**HEADERS, "Origin": "https://wrong.invalid"}).status_code == 403
    assert upload(client, headers={**HEADERS, "X-Vault-CSRF": "wrong"}).status_code == 403
    assert upload(client, headers={**HEADERS, "Content-Type": "application/json"}).status_code == 415
    client.post("/api/lock", json={})
    assert upload(client).status_code == 423
    assert not list(manager.directory.glob("*.part"))


def test_upload_bounds_chunked_body_and_contends_with_backup(configured):
    client, manager, _ = configured
    with maintenance_lock(manager.lock_path):
        assert upload(client).status_code == 409
    manager.limits["quota_bytes"] = 1024
    assert upload(client).status_code == 413
    assert upload(client, iter([b"PGDMP", b"x" * 1024])).status_code == 413
    assert not list(manager.directory.glob("*.part"))


@pytest.mark.parametrize("payload", [b"", b"not a database", b"PGDMPbroken"])
def test_invalid_dump_preserves_runtime_and_removes_temporary_files(configured, monkeypatch, payload):
    client, manager, service = configured
    monkeypatch.setattr(manager, "_tool", lambda _: None)
    def fail(*args):
        raise BackupError("export")
    monkeypatch.setattr(manager, "_process", fail)
    original = service.store.read_metadata().copy()
    assert upload(client, payload).status_code == 400
    assert service.store.read_metadata() == original
    assert service.unlocked and client.get("/api/status").json()["unlocked"]
    assert not list(manager.directory.glob("*.part"))
    assert not list(manager.directory.glob("*.sql"))


def test_success_accepts_large_body_and_invalidates_every_session(configured, monkeypatch):
    client, manager, service = configured
    monkeypatch.setattr(manager, "_tool", lambda _: None)
    monkeypatch.setattr(manager, "_process", lambda args, deadline, output: output.write(b"synthetic"))
    old_cookie = client.cookies.get("vault_admin")
    client.post("/api/unlock", json={"password": "synthetic backup master"})
    service.query_unlock.path.parent.mkdir(parents=True, exist_ok=True)
    service.query_unlock.path.write_text("synthetic old startup envelope")
    service.query_unlock.ready = True
    epoch = service.epoch
    calls = []
    def replace(store, path, check, replacement):
        calls.append(path.read_bytes())
        check()
        with replacement():
            pass  # Database transaction is tested with real PostgreSQL.
    monkeypatch.setattr("vault.web.routes.backups.replace_data", replace)
    response = upload(client, b"PGDMP" + b"x" * 300000)
    assert response.status_code == 200 and response.json()["ok"]
    assert calls == [b"synthetic"]
    assert not service.unlocked and service.epoch > epoch
    assert not service.query_unlock.ready and not service.query_unlock.path.exists()
    assert not client.app.state.admins.sessions
    assert client.get("/api/entries").status_code == 423
    assert client.get("/api/entries", headers={"Cookie": "vault_admin=" + old_cookie}).status_code == 423
    assert not list(manager.directory.glob("*.sql"))


def test_logout_during_validation_prevents_replacement(configured, monkeypatch):
    client, manager, service = configured
    monkeypatch.setattr(manager, "_tool", lambda _: None)
    def extract(args, deadline, output):
        client.app.state.admins.revoke_all()
    monkeypatch.setattr(manager, "_process", extract)
    def replace(store, path, check, replacement):
        with replacement():
            pytest.fail("Revoked session reached replacement")
    monkeypatch.setattr("vault.web.routes.backups.replace_data", replace)
    assert upload(client).status_code == 423
    assert service.unlocked


def test_copy_reader_is_bounded_and_does_not_treat_midrow_marker_as_terminator():
    class Writer:
        blocks = []
        def write(self, block):
            assert len(block) <= 65536
            self.blocks.append(block)
    writer = Writer()
    data = b"x" * 65536 + b"\\.\n" + b"second row\n"
    copy_data(io.BytesIO(data + b"\\.\n"), writer, lambda: None)
    assert b"".join(writer.blocks) == data
    with pytest.raises(BackupError):
        copy_data(io.BytesIO(b"truncated"), writer, lambda: None)
