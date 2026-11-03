"""Authenticated encryption; plaintext keys are only kept in process memory."""
import base64
import json
import os
import secrets
from pathlib import Path

from nacl import bindings, pwhash
from nacl.exceptions import CryptoError


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def unb64(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def encrypt(key: bytes, value: bytes, context: str) -> bytes:
    nonce = secrets.token_bytes(bindings.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES)
    return nonce + bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
        value, context.encode(), nonce, key
    )


def decrypt(key: bytes, blob: bytes, context: str) -> bytes:
    n = bindings.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES
    try:
        return bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
            blob[n:], context.encode(), blob[:n], key
        )
    except (CryptoError, ValueError, TypeError) as exc:
        raise ValueError("密码错误或加密数据校验失败") from exc


def json_encrypt(key: bytes, value, context: str) -> bytes:
    return encrypt(key, json.dumps(value, ensure_ascii=False).encode(), context)


def json_decrypt(key: bytes, value: bytes, context: str):
    return json.loads(decrypt(key, value, context))


class KeyEnvelope:
    def __init__(self, path: Path):
        self.path = path

    @property
    def exists(self):
        return self.path.exists()

    def create(self, password: str) -> tuple[bytes, str]:
        if self.exists:
            raise ValueError("密码库已经初始化")
        if len(password) < 12:
            raise ValueError("主密码至少需要 12 个字符")
        key = secrets.token_bytes(32)
        recovery = secrets.token_bytes(32)
        salt = secrets.token_bytes(pwhash.argon2id.SALTBYTES)
        ops = pwhash.argon2id.OPSLIMIT_MODERATE
        mem = pwhash.argon2id.MEMLIMIT_MODERATE
        kek = pwhash.argon2id.kdf(32, password.encode(), salt, opslimit=ops, memlimit=mem)
        envelope = {
            "version": 1, "salt": b64(salt), "ops": ops, "mem": mem,
            "wrapped": b64(encrypt(kek, key, "vault:key:v1")),
            "recovery": b64(encrypt(recovery, key, "vault:recovery:v1")),
        }
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Exclusive creation prevents concurrent setup from replacing a live vault.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(envelope, stream)
            stream.flush()
            os.fsync(stream.fileno())
        return key, "recovery:" + b64(recovery)

    def unlock(self, password: str) -> bytes:
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if value.get("version") != 1:
            raise ValueError("不支持的密码库版本")
        if password.startswith("recovery:"):
            try:
                recovery = unb64(password.removeprefix("recovery:"))
                return decrypt(recovery, unb64(value["recovery"]), "vault:recovery:v1")
            except (ValueError, KeyError) as exc:
                raise ValueError("恢复密钥无效") from exc
        kek = pwhash.argon2id.kdf(
            32, password.encode(), unb64(value["salt"]),
            opslimit=value["ops"], memlimit=value["mem"],
        )
        return decrypt(kek, unb64(value["wrapped"]), "vault:key:v1")
