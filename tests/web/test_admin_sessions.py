import secrets

import pytest
from fastapi.testclient import TestClient

from tests.support.records import entry
from tests.support.store import MemoryStore
from vault.core.service import VaultService
from vault.web.app import create_app
from vault.web.sessions import COOKIE


MASTER = "admin and query separation test"


@pytest.fixture
def running(tmp_path):
    machine = tmp_path / "machine.key"
    machine.write_bytes(secrets.token_bytes(32))
    store = MemoryStore()
    service = VaultService(store, tmp_path / "vault.json", machine)
    app = create_app(service, start_bot=False)
    with TestClient(app, base_url="http://127.0.0.1:18080") as client:
        client.headers["X-Vault-CSRF"] = client.get("/api/csrf").json()["token"]
        response = client.post("/api/setup", json={"password": MASTER})
        assert response.status_code == 200
        service.update_settings({"wechat_owner": "owner", "wechat_token": "fixture-token"})
        record = service.save(entry())
        yield client, app, service, store, machine, record


def test_lock_only_revokes_browser_and_queries_continue(running):
    client, app, service, _, _, record = running
    epoch = service.epoch
    cookie = client.cookies.get(COOKIE)
    assert client.post("/api/lock", json={}).status_code == 200
    assert service.unlocked and service.epoch == epoch
    status = client.get("/api/status").json()
    assert not status["unlocked"] and status["query_ready"] and status["auto_restore_ready"]
    assert status["wechat"]["owner_id"] is None
    assert client.get("/api/entries").status_code == 423
    assert client.get(f"/api/entries/{record}/reveal").status_code == 423
    assert client.post(f"/api/entries/{record}/trash", json={"version": 1}).status_code == 423
    assert client.post("/api/settings/deepseek", json={"api_key": "attempted-change"}).status_code == 423
    assert entry()["password"] in app.state.bot.handle("owner", "找客户演示云平台")
    assert app.state.bot.handle("stranger", "找客户演示云平台") is None
    client.cookies.set(COOKIE, cookie)
    assert client.get("/api/tags").status_code == 423
    assert client.post("/api/unlock", json={"password": "incorrect password"}).status_code == 400
    assert service.unlocked and service.epoch == epoch


def test_another_browser_is_not_authenticated_by_query_runtime(running):
    client, app, service, _, _, _ = running
    token = client.headers["X-Vault-CSRF"]
    with TestClient(app, base_url="http://127.0.0.1:18080") as other:
        other.headers["X-Vault-CSRF"] = token
        assert other.get("/api/entries").status_code == 423
        assert other.post("/api/entries", json=entry()).status_code == 423
        response = other.post("/api/unlock", json={"password": MASTER})
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        client.post("/api/lock", json={})
        assert other.get("/api/entries").status_code == 200
        assert client.get("/api/entries").status_code == 423


def test_expired_admin_session_does_not_stop_wechat(running):
    client, app, service, _, _, _ = running
    for key in app.state.admins.sessions:
        app.state.admins.sessions[key] = 0
    assert client.get("/api/entries").status_code == 423
    assert entry()["password"] in app.state.bot.handle("owner", "找客户演示云平台")


def test_restart_auto_restores_queries_but_never_admin_login(running):
    client, app, service, store, machine, _ = running
    data_key = service.key
    persisted = service.query_unlock.path.read_bytes()
    assert data_key not in persisted and MASTER.encode() not in persisted
    assert entry()["password"].encode() not in persisted
    old_cookie = client.cookies.get(COOKIE)
    service.lock()
    restarted = VaultService(store, service.envelope.path, machine)
    new_app = create_app(restarted, start_bot=False)
    with TestClient(new_app, base_url="http://127.0.0.1:18080") as fresh:
        fresh.cookies.set(COOKIE, old_cookie)
        assert fresh.get("/api/status").json()["query_ready"]
        assert fresh.get("/api/entries").status_code == 423
        assert restarted.key == data_key
        assert entry()["password"] in new_app.state.bot.handle("owner", "找客户演示云平台")


@pytest.mark.parametrize("damage", ["missing_machine", "wrong_machine", "changed_envelope", "corrupted_wrapping"])
def test_auto_restore_fails_closed_without_opening_admin(running, damage):
    _, _, service, store, machine, _ = running
    if damage == "missing_machine":
        machine.unlink()
    elif damage == "wrong_machine":
        machine.write_bytes(secrets.token_bytes(32))
    elif damage == "changed_envelope":
        store.metadata["envelope"] += b"\n"
    else:
        service.query_unlock.path.write_text('{"version":1,"wrapped":"broken"}')
    restarted = VaultService(store, service.envelope.path, machine)
    with TestClient(create_app(restarted, start_bot=False), base_url="http://127.0.0.1:18080") as client:
        assert not restarted.unlocked
        assert not client.get("/api/status").json()["query_ready"]
        assert client.get("/api/entries").status_code == 423


def test_admin_lock_cancels_qr_binding_that_is_in_flight(running, monkeypatch):
    client, app, service, _, _, _ = running
    from vault.web.routes import settings as module
    def pending_qr():
        assert client.post("/api/lock", json={}).status_code == 200
        return {"qrcode": "pending", "qrcode_img_content": "https://weixin.qq.com/scan/pending"}
    monkeypatch.setattr(module.WeChatClient, "create_qr", pending_qr)
    assert client.post("/api/wechat/qr", json={}).status_code == 423
    assert service.settings()["wechat_owner"] == "owner"
