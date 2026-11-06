"""Minimal text-only client for Tencent's published iLink bot protocol.

Protocol reference: https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md
No message body, token, QR value, or provider error body is logged here.
"""

from __future__ import annotations

import base64
import secrets
from typing import Any
from urllib.parse import urlsplit

import httpx


DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.4.10"
CLIENT_VERSION = str((2 << 16) | (4 << 8) | 10)


class WeChatError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = 3600 if code == -14 else 5


def validate_base_url(value: str) -> str:
    """Constrain credentials and QR redirects to HTTPS Weixin-owned hosts."""
    if not isinstance(value, str) or not value:
        raise ValueError("微信接口地址为空")
    candidate = value if "://" in value else "https://" + value
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("微信接口端口无效") from None
    if (
        parsed.scheme != "https"
        or not (host == "weixin.qq.com" or host.endswith(".weixin.qq.com"))
        or parsed.username is not None or parsed.password is not None
        or port not in (None, 443)
        or parsed.path not in ("", "/")
        or parsed.query or parsed.fragment
        or any(not (character.isascii() and (character.isalnum() or character in ".-")) for character in host)
    ):
        raise ValueError("只允许微信官方 HTTPS 接口地址")
    return "https://" + host


def _headers(token: str | None = None, *, poll_qr: bool = False) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": CLIENT_VERSION,
    }
    if not poll_qr:
        headers["AuthorizationType"] = "ilink_bot_token"
        headers["X-WECHAT-UIN"] = base64.b64encode(str(secrets.randbits(32)).encode("ascii")).decode("ascii")
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def _base_info() -> dict[str, str]:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": "WeChatVault/1.0"}


def _response_json(response: httpx.Response) -> dict:
    if not 200 <= response.status_code < 300:
        raise WeChatError("微信接口 HTTP 请求失败", code=response.status_code)
    try:
        result = response.json()
    except (ValueError, TypeError):
        raise WeChatError("微信接口返回格式无效") from None
    if not isinstance(result, dict):
        raise WeChatError("微信接口返回格式无效")
    for field in ("ret", "errcode"):
        value = result.get(field)
        if value not in (None, 0):
            code = value if isinstance(value, int) and not isinstance(value, bool) else None
            raise WeChatError("微信登录已失效，请重新扫码" if code == -14 else "微信接口业务请求失败", code=code)
    return result


def _request(client: httpx.Client, method: str, url: str, **kwargs: Any) -> dict:
    try:
        return _response_json(client.request(method, url, follow_redirects=False, **kwargs))
    except httpx.HTTPError:
        raise WeChatError("无法连接微信接口") from None


class WeChatClient:
    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL, *, client: httpx.Client | None = None) -> None:
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise ValueError("微信登录令牌无效")
        self.token = token
        self.base_url = validate_base_url(base_url)
        self._owned_client = client is None
        self._client = client or httpx.Client(trust_env=False, follow_redirects=False)

    def close(self) -> None:
        if self._owned_client:
            self._client.close()

    def get_updates(self, cursor: str = "") -> dict:
        try:
            response = self._client.post(
                self.base_url + "/ilink/bot/getupdates",
                headers=_headers(self.token),
                json={"get_updates_buf": cursor, "base_info": _base_info()},
                timeout=httpx.Timeout(45.0, connect=10.0),
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            return {"msgs": [], "get_updates_buf": cursor}
        except httpx.HTTPError:
            raise WeChatError("无法连接微信接口") from None
        result = _response_json(response)
        if not isinstance(result.get("msgs", []), list):
            raise WeChatError("微信消息返回格式无效")
        return result

    def send_text(self, user_id: str, text: str, context_token: str, client_id: str) -> None:
        if not all(isinstance(value, str) and value for value in (user_id, text, context_token, client_id)):
            raise ValueError("发送微信消息需要收件人、正文、上下文和消息编号")
        _request(
            self._client, "POST", self.base_url + "/ilink/bot/sendmessage",
            headers=_headers(self.token),
            json={
                "msg": {
                    "from_user_id": "", "to_user_id": user_id, "client_id": client_id,
                    "message_type": 2, "message_state": 2, "context_token": context_token,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                },
                "base_info": _base_info(),
            },
            timeout=20.0,
        )

    @staticmethod
    def create_qr(*, client: httpx.Client | None = None) -> dict:
        owned = client is None
        requester = client or httpx.Client(trust_env=False, follow_redirects=False)
        try:
            result = _request(
                requester, "POST", DEFAULT_BASE_URL + "/ilink/bot/get_bot_qrcode",
                params={"bot_type": "3"}, headers=_headers(),
                json={"local_token_list": []}, timeout=20.0,
            )
            if not all(isinstance(result.get(field), str) and result[field] for field in ("qrcode", "qrcode_img_content")):
                raise WeChatError("微信二维码返回格式无效")
            return result
        finally:
            if owned:
                requester.close()

    @staticmethod
    def poll_qr(qrcode: str, base_url: str = DEFAULT_BASE_URL, verify_code: str | None = None, *, client: httpx.Client | None = None) -> dict:
        if not isinstance(qrcode, str) or not qrcode:
            raise ValueError("二维码标识为空")
        address = validate_base_url(base_url)
        params = {"qrcode": qrcode}
        if verify_code:
            params["verify_code"] = verify_code
        owned = client is None
        requester = client or httpx.Client(trust_env=False, follow_redirects=False)
        try:
            result = _request(
                requester, "GET", address + "/ilink/bot/get_qrcode_status",
                params=params, headers=_headers(poll_qr=True), timeout=40.0,
            )
            for field in ("baseurl", "redirect_host"):
                if result.get(field):
                    result[field] = validate_base_url(result[field])
            if result.get("status") == "confirmed":
                if not all(isinstance(result.get(field), str) and result[field] for field in ("bot_token", "ilink_user_id", "ilink_bot_id")):
                    raise WeChatError("微信绑定身份返回不完整")
                result.setdefault("baseurl", address)
            return result
        finally:
            if owned:
                requester.close()
