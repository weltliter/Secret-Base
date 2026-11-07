"""Console status, initialization, login and browser-scoped logout."""
from fastapi import Request
from fastapi.responses import Response

from ..schemas import PasswordInput


def register(app, service, bot, admins, csrf_token, qr_sessions):
    @app.get("/api/csrf")
    def csrf():
        return {"token": csrf_token}

    @app.get("/api/status")
    def status(request: Request):
        with service.mutex:
            settings = service.settings() if service.unlocked else {}
            authenticated = admins.valid(request) and service.unlocked
            return {"initialized": service.envelope.exists, "unlocked": authenticated,
                    "query_ready": bool(service.unlocked and settings.get("wechat_owner") and settings.get("wechat_token")),
                    "auto_restore_ready": service.query_unlock.ready,
                    "model_name": bot.engine.model, "thinking_enabled": False,
                    "entry_count": len(service.store.list()) if authenticated else 0,
                    "trash_count": len(service.store.list(True)) if authenticated else 0,
                    "model_configured": bool(settings.get("deepseek_api_key")) if authenticated else None,
                    "wechat": {"bound": bool(settings.get("wechat_owner")), "owner_id": settings.get("wechat_owner") if authenticated else None,
                               "connected": bot.connected, "error": bot.error if authenticated else None}}

    @app.post("/api/setup")
    def setup(value: PasswordInput, request: Request, response: Response):
        with service.mutex:
            recovery = service.setup(value.password)
            admins.issue(request, response)
            return {"recovery_key": recovery}

    @app.post("/api/unlock")
    def unlock(value: PasswordInput, request: Request, response: Response):
        with service.mutex:
            service.unlock(value.password)
            admins.issue(request, response)
            return {"ok": True}

    @app.post("/api/lock")
    def lock(request: Request, response: Response):
        with service.mutex:
            identity = admins.identity(request)
            admins.revoke(request, response)
            for qr in list(qr_sessions):
                if qr_sessions[qr]["admin"] == identity:
                    qr_sessions.pop(qr)
            return {"ok": True}

