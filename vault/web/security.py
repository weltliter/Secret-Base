"""HTTP boundaries and serialized access checks for the local console."""
import logging
import secrets
from contextlib import contextmanager

from fastapi import Request
from fastapi.responses import JSONResponse

from ..channels.wechat import WeChatError


MAX_REQUEST_BYTES = 262144
RESPONSE_HEADERS = {
    "Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


def management_guard(service, admins):
    @contextmanager
    def management(request):
        # Serialize revocation with actual reads/writes, not just the HTTP precheck.
        with service.mutex:
            admins.require(request)
            service.require_key()
            yield

    return management


def install_security(app, admins, csrf_token):
    def failure(detail, status_code):
        return JSONResponse({"detail": detail}, status_code=status_code, headers=RESPONSE_HEADERS)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = request.url.hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            return failure("请使用本地 SSH 隧道地址访问", 403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return failure("请求来源不匹配", 403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not secrets.compare_digest(request.headers.get("x-vault-csrf", ""), csrf_token):
                return failure("请刷新管理页面后重试", 403)
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return failure("请求长度无效", 400)
        if content_length < 0:
            return failure("请求长度无效", 400)
        if content_length > MAX_REQUEST_BYTES:
            return failure("请求过大", 413)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            # Enforce the same bound on chunked bodies before JSON parsing. Starlette's
            # cached request replays this body to the downstream FastAPI handler.
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                    return failure("请求过大", 413)
                body.extend(chunk)
            request._body = bytes(body)
        public_api = {"/api/status", "/api/csrf", "/api/setup", "/api/unlock", "/api/lock"}
        if request.url.path.startswith("/api/") and request.url.path not in public_api and not admins.valid(request):
            response = failure("管理端已锁定，请使用主密码重新登录。", 423)
            admins.clear_cookie(response)
            return response
        try:
            response = await call_next(request)
        except Exception as exc:
            # Do not send or log traceback/exception repr: they may carry request data.
            logging.getLogger("vault.api").error("request failed (%s)", type(exc).__name__)
            return failure("请求暂时无法完成，请检查服务状态后重试", 500)
        response.headers.update(RESPONSE_HEADERS)
        if response.status_code == 423 and not admins.valid(request):
            admins.clear_cookie(response)
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        detail = str(exc)
        return JSONResponse({"detail": detail}, status_code=423 if detail.startswith("密码库已锁定") else 400)

    @app.exception_handler(WeChatError)
    async def wechat_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    # Validation defaults echo rejected inputs, which could contain passwords.
    from fastapi.exceptions import RequestValidationError
    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "输入格式或字段长度不符合要求"}, status_code=422)

