"""Revocable, browser-scoped admin sessions independent of the query runtime."""
import hashlib
import math
import secrets
import threading
import time
from dataclasses import dataclass, replace

from fastapi import HTTPException


COOKIE = "vault_admin"
LOCKED_MESSAGE = "管理端已锁定，请使用主密码重新登录。"


@dataclass(frozen=True)
class AdminSession:
    idle_expires_at: float
    absolute_expires_at: float


class AdminSessions:
    def __init__(self, ttl=3600, idle_ttl=300, *, clock=None):
        if not all(math.isfinite(value) and value > 0 for value in (ttl, idle_ttl)):
            raise ValueError("Session limits must be positive and finite.")
        self.ttl = ttl
        self.idle_ttl = idle_ttl
        self.clock = clock or time.monotonic
        self.sessions = {}
        self.mutex = threading.RLock()
        self._revoked_callback = None

    def on_revoke(self, callback):
        """Install local artifact cleanup; callbacks run outside the session mutex."""
        self._revoked_callback = callback

    def _notify_revoked(self, identities):
        if self._revoked_callback:
            for identity in identities:
                if identity:
                    self._revoked_callback(identity)

    def _prune_locked(self, now):
        expired = [identity for identity, session in self.sessions.items()
                   if min(session.idle_expires_at, session.absolute_expires_at) <= now]
        for identity in expired:
            del self.sessions[identity]
        return expired

    @staticmethod
    def identity(request):
        token = request.cookies.get(COOKIE, "")
        return hashlib.sha256(token.encode()).hexdigest() if token else ""

    def valid(self, request):
        return self.remaining(request) is not None

    def _remaining(self, request, *, activity=False):
        identity = self.identity(request)
        with self.mutex:
            now = self.clock()
            expired = self._prune_locked(now)
            session = self.sessions.get(identity)
            if session is not None and activity:
                session = replace(session, idle_expires_at=min(now + self.idle_ttl,
                                                               session.absolute_expires_at))
                self.sessions[identity] = session
            result = None if session is None else {
                "idle_remaining_seconds": session.idle_expires_at - now,
                "absolute_remaining_seconds": session.absolute_expires_at - now,
            }
        # Management routes also hold the service mutex. Taking it from a callback
        # while holding self.mutex would reverse that lock order in middleware.
        self._notify_revoked(expired)
        return result

    def remaining(self, request):
        """Inspect both deadlines without renewing either one."""
        return self._remaining(request)

    def activity(self, request):
        """Only the explicit activity endpoint may renew an unexpired idle timer."""
        result = self._remaining(request, activity=True)
        if result is None:
            raise HTTPException(423, LOCKED_MESSAGE)
        return result

    def require(self, request):
        if not self.valid(request):
            raise HTTPException(423, LOCKED_MESSAGE)

    def issue(self, request, response):
        token = secrets.token_urlsafe(32)
        with self.mutex:
            now = self.clock()
            retired = self._prune_locked(now)
            identity = self.identity(request)
            if self.sessions.pop(identity, None) is not None:
                retired.append(identity)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = AdminSession(
                min(now + self.idle_ttl, now + self.ttl), now + self.ttl)
        self._notify_revoked(retired)
        # HTTP exists only on the loopback end of the encrypted SSH tunnel.
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")

    def revoke(self, request, response):
        identity = self.identity(request)
        with self.mutex:
            self.sessions.pop(identity, None)
        self._notify_revoked([identity])
        self.clear_cookie(response)

    def revoke_all(self):
        with self.mutex:
            identities = list(self.sessions)
            self.sessions.clear()
        self._notify_revoked(identities)

    @staticmethod
    def clear_cookie(response):
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
