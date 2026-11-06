import json

import httpx
import pytest

from vault.channels.wechat import DEFAULT_BASE_URL, WeChatClient, WeChatError, validate_base_url


@pytest.mark.parametrize("url", [
    "http://ilinkai.weixin.qq.com", "https://weixin.qq.com.evil.test", "https://evilweixin.qq.com",
    "https://127.0.0.1", "https://weixin.qq.com:444", "https://weixin.qq.com/private",
    "https://user:pass@weixin.qq.com", "https://weixin.qq.com?token=secret", "https://weixin.qq.com#secret",
])
def test_rejects_nonofficial_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        validate_base_url(url)


def test_normalizes_official_redirect_host():
    assert validate_base_url("ilinkai.weixin.qq.com") == DEFAULT_BASE_URL
    assert validate_base_url("https://ILINKAI.weixin.qq.com:443/") == DEFAULT_BASE_URL


def test_send_preserves_password_whitespace_and_protocol_fields():
    captured = []
    def handle(request):
        captured.append(request)
        return httpx.Response(200, json={"ret": 0})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        WeChatClient("TOKEN", client=client).send_text("owner", "密码：  Ab C\n尾部空格  ", "context", "client-1")
    request = captured[0]
    payload = json.loads(request.content)
    assert payload["msg"] == {
        "from_user_id": "", "to_user_id": "owner", "client_id": "client-1",
        "message_type": 2, "message_state": 2, "context_token": "context",
        "item_list": [{"type": 1, "text_item": {"text": "密码：  Ab C\n尾部空格  "}}],
    }
    assert payload["base_info"]["bot_agent"] == "WeChatVault/1.0"
    assert request.headers["authorization"] == "Bearer TOKEN"
    assert request.headers["authorizationtype"] == "ilink_bot_token"


def test_get_updates_uses_context_cursor():
    def handle(request):
        assert json.loads(request.content)["get_updates_buf"] == "old-cursor"
        return httpx.Response(200, json={"ret": 0, "msgs": [], "get_updates_buf": "next-cursor"})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert WeChatClient("TOKEN", client=client).get_updates("old-cursor")["get_updates_buf"] == "next-cursor"


def test_long_poll_timeout_preserves_cursor():
    def handle(request):
        raise httpx.ReadTimeout("private details", request=request)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert WeChatClient("TOKEN", client=client).get_updates("cursor") == {"msgs": [], "get_updates_buf": "cursor"}


def test_expired_login_is_sanitized_and_identifiable(capsys):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"ret": -14, "errmsg": "SECRET_CANARY"}))) as client:
        with pytest.raises(WeChatError) as error:
            WeChatClient("TOKEN", client=client).get_updates()
    assert error.value.code == -14
    assert error.value.retry_after == 3600
    assert "SECRET_CANARY" not in str(error.value)
    assert "SECRET_CANARY" not in str(capsys.readouterr())


def test_http_redirect_is_not_followed_even_with_client_redirects_enabled():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.test/steal"})
    with httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True) as client:
        with pytest.raises(WeChatError):
            WeChatClient("TOKEN", client=client).get_updates()
    assert len(calls) == 1
    assert calls[0].url.host == "ilinkai.weixin.qq.com"


def test_create_qr_has_no_existing_token():
    def handle(request):
        assert request.method == "POST"
        assert request.url.path == "/ilink/bot/get_bot_qrcode"
        assert request.url.params["bot_type"] == "3"
        assert "authorization" not in request.headers
        assert json.loads(request.content) == {"local_token_list": []}
        return httpx.Response(200, json={"qrcode": "qr", "qrcode_img_content": "qr-content"})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert WeChatClient.create_qr(client=client)["qrcode"] == "qr"


def test_poll_qr_only_sends_application_headers():
    def handle(request):
        assert request.method == "GET"
        assert "authorization" not in request.headers
        assert "authorizationtype" not in request.headers
        assert "x-wechat-uin" not in request.headers
        assert request.url.params["qrcode"] == "qr/?private"
        assert request.url.params["verify_code"] == "1234"
        return httpx.Response(200, json={"status": "confirmed", "bot_token": "secret", "ilink_user_id": "owner", "ilink_bot_id": "bot", "baseurl": DEFAULT_BASE_URL})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = WeChatClient.poll_qr("qr/?private", verify_code="1234", client=client)
    assert result["ilink_user_id"] == "owner"


def test_poll_qr_rejects_external_login_redirect():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "scaned_but_redirect", "redirect_host": "evil.test"}))) as client:
        with pytest.raises(ValueError):
            WeChatClient.poll_qr("qr", client=client)


def test_confirmed_login_requires_verified_user_identity():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "confirmed", "bot_token": "secret", "ilink_bot_id": "bot"}))) as client:
        with pytest.raises(WeChatError):
            WeChatClient.poll_qr("qr", client=client)


def test_send_requires_context_token_before_network_request():
    def unexpected(request):
        pytest.fail("network should not be called")
    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        with pytest.raises(ValueError):
            WeChatClient("TOKEN", client=client).send_text("owner", "password", "", "id")
