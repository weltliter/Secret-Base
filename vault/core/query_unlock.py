"""Machine-bound encrypted data-key envelope for unattended query startup."""
import hashlib
import json
import os
import secrets
from pathlib import Path

from .crypto import b64, decrypt, encrypt, unb64


class QueryUnlock:
    def __init__(self, envelope_path, machine_key_path=None):
        self.envelope_path = Path(envelope_path)
        self.path = self.envelope_path.with_name("query-unlock.json")
        self.machine_key_path = Path(machine_key_path) if machine_key_path else None
        self.ready = False

    def machine_key(self):
        if self.machine_key_path is None:
            return None
        key = self.machine_key_path.read_bytes()
        if len(key) != 32:
            raise ValueError("查询服务运行密钥无效")
        return key

    def context(self):
        digest = hashlib.sha256(self.envelope_path.read_bytes()).hexdigest()
        return "vault:query-unlock:v1:" + digest

    def enable(self, data_key):
        key = self.machine_key()
        if key is None:
            return
        value = {"version": 1, "wrapped": b64(encrypt(key, data_key, self.context()))}
        temporary = self.path.with_name(".query-unlock-" + secrets.token_hex(8))
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.ready = True

    def load(self):
        self.ready = False
        if self.machine_key_path is None or not self.path.exists() or not self.envelope_path.exists():
            return None
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if value.get("version") != 1:
            raise ValueError("查询服务密钥信封版本无效")
        key = decrypt(self.machine_key(), unb64(value["wrapped"]), self.context())
        if len(key) != 32:
            raise ValueError("查询服务密钥信封无效")
        return key
