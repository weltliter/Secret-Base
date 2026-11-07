"""Revocable, browser-scoped admin sessions independent of the query runtime."""
import hashlib
import secrets
import threading
import time

from fastapi import HTTPException


COOKIE = "vault_admin"


class AdminSessions:
    def __init__(self, ttl=8 * 3600):
        self.ttl = ttl
        self.sessions = {}
        self.mutex = threading.RLock()

    @staticmethod
    def identity(request):
        token = request.cookies.get(COOKIE, "")
        return hashlib.sha256(token.encode()).hexdigest() if token else ""

    def valid(self, request):
        identity = self.identity(request)
        with self.mutex:
            expires = self.sessions.get(identity, 0)
            if expires <= time.monotonic():
                self.sessions.pop(identity, None)
                return False
            return True

    def require(self, request):
        if not self.valid(request):
            raise HTTPException(423, "管理端已锁定，请使用主密码重新登录。")

    def issue(self, request, response):
        token = secrets.token_urlsafe(32)
        with self.mutex:
            now = time.monotonic()
            self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
            self.sessions.pop(self.identity(request), None)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = now + self.ttl
        # HTTP exists only on the loopback end of the encrypted SSH tunnel.
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")

    def revoke(self, request, response):
        with self.mutex:
            self.sessions.pop(self.identity(request), None)
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
