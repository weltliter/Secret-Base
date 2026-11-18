"""Model configuration and owner-bound WeChat QR login endpoints."""
import io
import time

from fastapi import Request
from fastapi.responses import Response

from ...channels.wechat import WeChatClient, validate_base_url
from ..schemas import KeyInput, QRInput


QR_STATUSES = {"wait", "scaned", "confirmed", "expired", "need_verifycode", "verify_code_blocked", "scaned_but_redirect", "binded_redirect"}


def register(app, service, bot, admins, management, qr_sessions):
    @app.post("/api/settings/deepseek")
    def deepseek(request: Request, value: KeyInput):
        with management(request):
            service.update_settings({"deepseek_api_key": value.api_key})
            return {"ok": True}

    @app.post("/api/wechat/qr")
    def create_qr(request: Request):
        with management(request):
            service.require_key()
            epoch = service.epoch
        result = WeChatClient.create_qr()
        with management(request):
            service.require_key()
            if epoch != service.epoch:
                raise ValueError("密码库状态已变化，请重新生成二维码")
            qr_sessions.clear()
            qr_sessions[result["qrcode"]] = {"base_url": "https://ilinkai.weixin.qq.com", "expires": time.monotonic() + 300, "epoch": epoch, "admin": admins.identity(request)}
        return {"qrcode": result["qrcode"], "qrcode_img_content": result["qrcode_img_content"]}

    @app.get("/api/wechat/qr-image")
    def qr_image(request: Request, qrcode: str):
        with management(request):
            service.require_key()
            if len(qrcode) > 4096:
                raise ValueError("二维码数据过长")
            import qrcode as qr_library
            buffer = io.BytesIO()
            try:
                qr_library.make(qrcode).save(buffer, format="PNG")
            except qr_library.exceptions.DataOverflowError:
                raise ValueError("二维码数据过长") from None
            return Response(buffer.getvalue(), media_type="image/png")

    @app.post("/api/wechat/poll")
    def poll_qr(request: Request, value: QRInput):
        with management(request):
            service.require_key()
            session = qr_sessions.get(value.qrcode)
            if not session or session["admin"] != admins.identity(request) or time.monotonic() > session["expires"] or session["epoch"] != service.epoch:
                raise ValueError("二维码已过期，请重新生成")
        result = WeChatClient.poll_qr(value.qrcode, base_url=session["base_url"], verify_code=value.verify_code)
        with management(request):
            service.require_key()
            if session is not qr_sessions.get(value.qrcode) or session["epoch"] != service.epoch or time.monotonic() > session["expires"]:
                raise ValueError("二维码或密码库状态已变化，请重新扫码")
            status = result.get("status", "wait")
            if status not in QR_STATUSES:
                raise ValueError("微信二维码状态无效，请重新扫码")
            if result.get("redirect_host"):
                session["base_url"] = validate_base_url(result["redirect_host"])
            if status == "confirmed":
                service.update_settings({"wechat_token": result["bot_token"], "wechat_owner": result["ilink_user_id"], "wechat_bot_id": result["ilink_bot_id"], "wechat_base_url": validate_base_url(result["baseurl"]), "wechat_cursor": ""})
                service.epoch += 1
                bot.clear()
                qr_sessions.clear()
                return {"status": "confirmed", "owner_id": result["ilink_user_id"]}
            return {"status": status}

