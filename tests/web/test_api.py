"""Exercise the HTTP boundary with real encryption and in-memory persistence."""
import logging
import threading
from types import SimpleNamespace

import pytest

import vault.web.routes.settings as settings_routes
from tests.support.records import entry
from tests.support.http import MASTER, initialize


SENTINEL = "api-secret-SENTINEL-781f"


def test_tags_are_optional_and_catalog_uses_active_records_only(api):
    client, service, store = api
    assert client.get("/api/tags").status_code == 423
    initialize(client)
    first = client.post("/api/entries", json=entry(tags=["工作", " 工作 ", "测试"]))
    second = client.post("/api/entries", json=entry(title="另一个系统", tags=["只在回收站"])).json()["id"]
    no_tags = entry(title="未设置标签")
    no_tags.pop("tags")
    assert client.post("/api/entries", json=no_tags).status_code == 200
    assert service.entry(first.json()["id"])["tags"] == ["工作", "测试"]
    assert client.post(f"/api/entries/{second}/trash", json={"version": 1}).status_code == 200
    response = client.get("/api/tags")
    assert set(response.json()["tags"]) == {"工作", "测试"}
    assert response.headers["cache-control"] == "no-store"
    assert all("工作".encode() not in row["payload"] for row in store.rows.values())
    assert client.post(f"/api/entries/{second}/restore", json={}).status_code == 200
    assert "只在回收站" in client.get("/api/tags").json()["tags"]
    client.post("/api/lock", json={})
    assert client.get("/api/tags").status_code == 423


def test_password_reuse_is_not_reported_as_a_duplicate_by_default(api):
    client, _, _ = api
    initialize(client)
    client.post("/api/entries", json=entry(title="系统甲", service="a.example.com"))
    client.post("/api/entries", json=entry(title="系统乙", service="b.example.com"))
    assert client.get("/api/duplicates").json()["groups"] == []
    group, = client.get("/api/duplicates?include_reuse=true").json()["groups"]
    assert group["category"] == "password_reuse"


def assert_private_response(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"


def test_setup_lock_unlock_and_recovery(api):
    client, service, _ = api
    assert client.get("/api/status").json()["initialized"] is False
    recovery = initialize(client)
    assert recovery.startswith("recovery:")
    assert client.get("/api/status").json()["unlocked"] is True
    assert client.post("/api/setup", json={"password": MASTER}).status_code == 400
    assert client.post("/api/lock", json={}).status_code == 200
    locked = client.get("/api/entries")
    assert locked.status_code == 423
    assert_private_response(locked)
    assert client.post("/api/unlock", json={"password": MASTER}).status_code == 200
    client.post("/api/lock", json={})
    assert client.post("/api/unlock", json={"password": recovery}).status_code == 200
    assert service.unlocked


def test_host_origin_and_csrf_block_writes_and_all_have_no_store(api):
    client, service, _ = api
    invalid_host = client.get("/api/status", headers={"host": "attacker.example"})
    invalid_origin = client.post("/api/setup", json={"password": MASTER}, headers={"origin": "https://attacker.example"})
    invalid_token = client.post("/api/setup", json={"password": MASTER}, headers={"X-Vault-CSRF": "wrong"})
    opaque_origin = client.post("/api/setup", json={"password": MASTER}, headers={"origin": "null"})
    for response in (invalid_host, invalid_origin, invalid_token, opaque_origin):
        assert response.status_code == 403
        assert_private_response(response)
    assert not service.envelope.exists
    ok = client.post("/api/setup", json={"password": MASTER}, headers={"origin": "http://127.0.0.1:18080"})
    assert ok.status_code == 200


def test_input_errors_never_echo_secrets(api):
    client, _, store = api
    initialize(client)
    response = client.post("/api/entries", json={**entry(), "password": SENTINEL, "tags": [SENTINEL * 4]})
    assert response.status_code == 422
    assert SENTINEL not in response.text
    assert_private_response(response)
    response = client.post("/api/entries", content='{"password":"' + SENTINEL, headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert SENTINEL not in response.text
    assert not store.rows


@pytest.mark.parametrize("changes", [
    {"tags": ["x" * 65]}, {"tags": [""]}, {"fields": {"x" * 129: "value"}},
    {"fields": {"": "value"}}, {"fields": {"key": "x" * 65537}}, {"version": 0},
])
def test_per_field_bounds_are_enforced(api, changes):
    client, _, _ = api
    initialize(client)
    response = client.post("/api/entries", json={**entry(), **changes})
    assert response.status_code == 422


@pytest.mark.parametrize("length,expected", [("wrong", 400), ("-1", 400), ("262145", 413)])
def test_invalid_or_excessive_declared_content_length_is_safe(api, length, expected):
    client, _, _ = api
    response = client.post("/api/setup", content=b"{}", headers={"content-length": length, "content-type": "application/json"})
    assert response.status_code == expected
    assert_private_response(response)


def test_chunked_body_is_bounded_before_json_parsing(api):
    client, _, _ = api
    response = client.post("/api/setup", content=iter([b"x" * 150000, b"y" * 150000]), headers={"content-type": "application/json"})
    assert response.status_code == 413
    assert_private_response(response)


def test_crud_keeps_password_exact_and_checks_versions(api):
    client, _, store = api
    initialize(client)
    original = entry(password=" \n" + SENTINEL + " \t")
    response = client.post("/api/entries", json=original)
    assert response.status_code == 200
    record_id = response.json()["id"]
    listed = client.get("/api/entries").json()["entries"][0]
    assert "password" not in listed
    assert listed["created_at"] == store.rows[record_id]["created_at"].isoformat()
    shown = client.get(f"/api/entries/{record_id}/reveal")
    assert shown.json() == {"password": original["password"]}
    assert_private_response(shown)
    updated = {**original, "title": "changed", "version": 1}
    updated.pop("password")
    assert client.put(f"/api/entries/{record_id}", json=updated).status_code == 200
    assert client.get("/api/entries").json()["entries"][0]["created_at"] == listed["created_at"]
    assert client.get(f"/api/entries/{record_id}/reveal").json()["password"] == original["password"]
    assert client.put(f"/api/entries/{record_id}", json=updated).status_code == 400
    assert client.post(f"/api/entries/{record_id}/trash", json={"version": 1}).status_code == 400
    assert client.post(f"/api/entries/{record_id}/trash", json={"version": 2}).status_code == 200
    assert not client.get("/api/entries").json()["entries"]
    assert client.get(f"/api/entries/{record_id}/reveal").status_code == 400
    assert client.get("/api/entries?trash=true").json()["entries"][0]["id"] == record_id
    assert client.post(f"/api/entries/{record_id}/restore", json={}).status_code == 200
    assert client.get("/api/entries").json()["entries"][0]["version"] == 4
    assert SENTINEL.encode() not in store.rows[record_id]["payload"]


def test_model_key_is_stored_encrypted_and_never_returned(api):
    client, service, store = api
    initialize(client)
    response = client.post("/api/settings/deepseek", json={"api_key": SENTINEL})
    assert response.status_code == 200
    assert SENTINEL not in response.text
    assert SENTINEL not in client.get("/api/status").text
    assert SENTINEL.encode() not in store.settings_blob
    assert service.settings()["deepseek_api_key"] == SENTINEL
    assert client.post("/api/settings/deepseek", json={"api_key": "   "}).status_code == 422
    assert client.post("/api/settings/deepseek", json={"api_key": "valid\nsecret"}).status_code == 422


def confirmed_qr(**changes):
    result = {"status": "confirmed", "bot_token": SENTINEL, "ilink_user_id": "owner-one", "ilink_bot_id": "bot-one", "baseurl": "https://ilinkai.weixin.qq.com"}
    result.update(changes)
    return result


def mock_create_qr(monkeypatch, name="qr-one"):
    monkeypatch.setattr(settings_routes.WeChatClient, "create_qr", lambda: {"qrcode": name, "qrcode_img_content": "https://weixin.qq.com/scan/" + name})


def test_qr_binding_returns_only_identity_not_token(api, monkeypatch):
    client, service, store = api
    initialize(client)
    mock_create_qr(monkeypatch)
    monkeypatch.setattr(settings_routes.WeChatClient, "poll_qr", lambda *args, **kwargs: confirmed_qr())
    qr = client.post("/api/wechat/qr", json={}).json()["qrcode"]
    response = client.post("/api/wechat/poll", json={"qrcode": qr})
    assert response.json() == {"status": "confirmed", "owner_id": "owner-one"}
    assert SENTINEL not in response.text
    assert SENTINEL not in client.get("/api/status").text
    assert SENTINEL.encode() not in store.settings_blob
    assert service.settings()["wechat_token"] == SENTINEL
    assert client.post("/api/wechat/poll", json={"qrcode": qr}).status_code == 400


def test_qr_creation_in_flight_is_rejected_after_relock(api, monkeypatch):
    client, service, _ = api
    initialize(client)
    def relock_during_request():
        service.lock()
        service.unlock(MASTER)
        return {"qrcode": "qr-one", "qrcode_img_content": "https://weixin.qq.com/scan/qr-one"}
    monkeypatch.setattr(settings_routes.WeChatClient, "create_qr", relock_during_request)
    assert client.post("/api/wechat/qr", json={}).status_code == 400
    assert not service.settings().get("wechat_owner")


def test_qr_poll_in_flight_cannot_bind_after_relock(api, monkeypatch):
    client, service, _ = api
    initialize(client)
    mock_create_qr(monkeypatch)
    qr = client.post("/api/wechat/qr", json={}).json()["qrcode"]
    def relock_during_poll(*args, **kwargs):
        service.lock()
        service.unlock(MASTER)
        return confirmed_qr()
    monkeypatch.setattr(settings_routes.WeChatClient, "poll_qr", relock_during_poll)
    assert client.post("/api/wechat/poll", json={"qrcode": qr}).status_code == 400
    assert not service.settings().get("wechat_owner")


def test_qr_expiration_prevents_provider_poll(api, monkeypatch):
    client, service, _ = api
    initialize(client)
    mock_create_qr(monkeypatch)
    clock = [1000]
    monkeypatch.setattr(settings_routes, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    qr = client.post("/api/wechat/qr", json={}).json()["qrcode"]
    monkeypatch.setattr(settings_routes.WeChatClient, "poll_qr", lambda *args, **kwargs: pytest.fail("expired QR was polled"))
    clock[0] += 301
    assert client.post("/api/wechat/poll", json={"qrcode": qr}).status_code == 400
    assert not service.settings().get("wechat_owner")


def test_qr_image_is_local_and_handles_capacity_limits(api):
    client, _, _ = api
    assert client.get("/api/wechat/qr-image", params={"qrcode": "test"}).status_code == 423
    initialize(client)
    response = client.get("/api/wechat/qr-image", params={"qrcode": "https://weixin.qq.com/scan/test"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert_private_response(response)
    excessive = client.get("/api/wechat/qr-image", params={"qrcode": "密" * 2000})
    assert excessive.status_code == 400
    assert_private_response(excessive)


def test_replacing_qr_invalidates_an_already_in_flight_poll(api, monkeypatch):
    client, service, _ = api
    initialize(client)
    mock_create_qr(monkeypatch)
    qr = client.post("/api/wechat/qr", json={}).json()["qrcode"]
    started, release = threading.Event(), threading.Event()
    responses = []
    def paused_poll(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return confirmed_qr()
    monkeypatch.setattr(settings_routes.WeChatClient, "poll_qr", paused_poll)
    thread = threading.Thread(target=lambda: responses.append(client.post("/api/wechat/poll", json={"qrcode": qr})))
    thread.start()
    try:
        assert started.wait(timeout=5)
        mock_create_qr(monkeypatch, "qr-two")
        assert client.post("/api/wechat/qr", json={}).status_code == 200
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert responses[0].status_code == 400
    assert not service.settings().get("wechat_owner")


@pytest.mark.parametrize("result", [
    {"status": "scaned_but_redirect", "redirect_host": "https://attacker.example"},
    confirmed_qr(baseurl="https://weixin.qq.com.attacker.example"),
])
def test_official_domain_is_rechecked_before_qr_redirect_or_binding(api, monkeypatch, result):
    client, service, _ = api
    initialize(client)
    mock_create_qr(monkeypatch)
    qr = client.post("/api/wechat/qr", json={}).json()["qrcode"]
    monkeypatch.setattr(settings_routes.WeChatClient, "poll_qr", lambda *args, **kwargs: result)
    assert client.post("/api/wechat/poll", json={"qrcode": qr}).status_code == 400
    assert not service.settings().get("wechat_owner")


def test_unexpected_exception_is_not_echoed_or_logged_with_secret(api, monkeypatch, caplog):
    client, service, _ = api
    initialize(client)
    def broken_entries(*args, **kwargs):
        raise RuntimeError(SENTINEL)
    monkeypatch.setattr(service, "entries", broken_entries)
    response = client.get("/api/entries")
    assert response.status_code == 500
    assert SENTINEL not in response.text
    assert SENTINEL not in caplog.text
    assert "RuntimeError" in caplog.text
    assert_private_response(response)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
