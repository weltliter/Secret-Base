import secrets

import pytest
from fastapi.testclient import TestClient

from tests.support.records import entry
from tests.support.store import MemoryStore
from vault.core.service import VaultService
from vault.web.app import create_app
from vault.web.sessions import COOKIE


MASTER = "admin and query separation test"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def running(tmp_path):
    machine = tmp_path / "machine.key"
    machine.write_bytes(secrets.token_bytes(32))
    store = MemoryStore()
    service = VaultService(store, tmp_path / "vault.json", machine)
    app = create_app(service, start_bot=False)
    app.state.admins.clock = Clock()
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
    app.state.admins.clock.advance(300)
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


def test_polling_and_reads_do_not_renew_idle_session(running):
    client, app, _, _, _, _ = running
    cookie = client.cookies.get(COOKIE)
    for step in range(1, 6):
        app.state.admins.clock.advance(59)
        status = client.get("/api/status").json()
        assert status["session"] == {"idle_remaining_seconds": 300 - 59 * step,
                                     "absolute_remaining_seconds": 3600 - 59 * step}
        assert client.get("/api/entries").status_code == 200
    app.state.admins.clock.advance(5)
    status = client.get("/api/status")
    assert status.json()["session"] is None
    assert not status.json()["unlocked"]
    assert "Max-Age=0" in status.headers["set-cookie"]
    client.cookies.set(COOKIE, cookie)
    assert client.post("/api/session/activity", json={}).status_code == 423


def test_expired_cookie_cannot_read_or_write_any_management_resource(running):
    client, app, _, _, _, record = running
    cookie = client.cookies.get(COOKIE)
    app.state.admins.clock.advance(300)
    requests = [
        ("GET", "/api/entries", None),
        ("GET", f"/api/entries/{record}/reveal", None),
        ("POST", "/api/entries", entry()),
        ("PUT", f"/api/entries/{record}", {**entry(), "version": 1}),
        ("POST", f"/api/entries/{record}/trash", {"version": 1}),
        ("POST", f"/api/entries/{record}/restore", {}),
        ("POST", "/api/settings/deepseek", {"api_key": "synthetic-key"}),
        ("GET", "/api/query-logs", None),
        ("POST", "/api/session/activity", {}),
    ]
    for method, path, body in requests:
        client.cookies.set(COOKIE, cookie)
        response = client.request(method, path, json=body)
        assert response.status_code == 423, path
        assert "Max-Age=0" in response.headers["set-cookie"]


def test_only_explicit_authenticated_activity_renews_idle(running):
    client, app, _, _, _, _ = running
    app.state.admins.clock.advance(250)
    # A normal mutation is not a signal of user activity: retries must not renew.
    assert client.post("/api/settings/deepseek", json={"api_key": "synthetic-key"}).status_code == 200
    assert client.get("/api/status").json()["session"]["idle_remaining_seconds"] == 50
    assert client.post("/api/session/activity", json={}).json() == {
        "idle_remaining_seconds": 300, "absolute_remaining_seconds": 3350}
    app.state.admins.clock.advance(299)
    assert client.get("/api/entries").status_code == 200
    app.state.admins.clock.advance(1)
    assert client.post("/api/session/activity", json={}).status_code == 423


def test_absolute_deadline_wins_despite_continuous_activity(running):
    client, app, _, _, _, _ = running
    cookie = client.cookies.get(COOKIE)
    for _ in range(17):
        app.state.admins.clock.advance(200)
        assert client.post("/api/session/activity", json={}).status_code == 200
    assert client.get("/api/status").json()["session"] == {
        "idle_remaining_seconds": 200, "absolute_remaining_seconds": 200}
    app.state.admins.clock.advance(199)
    assert client.post("/api/session/activity", json={}).json() == {
        "idle_remaining_seconds": 1, "absolute_remaining_seconds": 1}
    app.state.admins.clock.advance(1)
    assert client.post("/api/session/activity", json={}).status_code == 423
    client.cookies.set(COOKIE, cookie)
    assert client.get("/api/entries").status_code == 423


def test_activity_requires_csrf_and_matching_origin(running):
    client, app, _, _, _, _ = running
    app.state.admins.clock.advance(250)
    assert client.post("/api/session/activity", headers={"X-Vault-CSRF": ""}, json={}).status_code == 403
    assert client.post("/api/session/activity", headers={"Origin": "https://invalid.example"}, json={}).status_code == 403
    assert client.get("/api/status").json()["session"]["idle_remaining_seconds"] == 50
    token = client.headers["X-Vault-CSRF"]
    with TestClient(app, base_url="http://127.0.0.1:18080") as anonymous:
        anonymous.headers["X-Vault-CSRF"] = token
        assert anonymous.post("/api/session/activity", json={}).status_code == 423


def test_session_replacement_revokes_old_cookie_and_cleans_qr(running, monkeypatch):
    client, app, _, _, _, _ = running
    from vault.web.routes import settings as module
    monkeypatch.setattr(module.WeChatClient, "create_qr", lambda: {
        "qrcode": "synthetic-qr", "qrcode_img_content": "https://weixin.qq.com/scan/synthetic"})
    assert client.post("/api/wechat/qr", json={}).status_code == 200
    old_cookie = client.cookies.get(COOKIE)
    assert client.post("/api/unlock", json={"password": MASTER}).status_code == 200
    assert client.post("/api/wechat/poll", json={"qrcode": "synthetic-qr"}).status_code == 400
    client.cookies.clear()
    client.cookies.set(COOKIE, old_cookie)
    assert client.get("/api/entries").status_code == 423


def test_idle_expiry_cancels_qr_binding_in_flight(running, monkeypatch):
    client, app, service, _, _, _ = running
    from vault.web.routes import settings as module

    def late_qr():
        app.state.admins.clock.advance(300)
        return {"qrcode": "late", "qrcode_img_content": "https://weixin.qq.com/scan/late"}

    monkeypatch.setattr(module.WeChatClient, "create_qr", late_qr)
    assert client.post("/api/wechat/qr", json={}).status_code == 423
    assert service.settings()["wechat_owner"] == "owner"


def test_activity_in_another_browser_does_not_extend_first_browser(running):
    client, app, _, _, _, _ = running
    with TestClient(app, base_url="http://127.0.0.1:18080") as other:
        other.headers["X-Vault-CSRF"] = client.headers["X-Vault-CSRF"]
        assert other.post("/api/unlock", json={"password": MASTER}).status_code == 200
        app.state.admins.clock.advance(250)
        assert other.post("/api/session/activity", json={}).status_code == 200
        app.state.admins.clock.advance(50)
        assert client.get("/api/entries").status_code == 423
        assert other.get("/api/entries").status_code == 200
