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


def create_envelope(password: str) -> tuple[bytes, bytes, str]:
    """Create portable wrapped keys without coupling encryption to storage."""
    if len(password) < 12:
        raise ValueError("主密码至少需要 12 个字符")
    key = secrets.token_bytes(32)
    recovery = secrets.token_bytes(32)
    salt = secrets.token_bytes(pwhash.argon2id.SALTBYTES)
    ops = pwhash.argon2id.OPSLIMIT_MODERATE
    mem = pwhash.argon2id.MEMLIMIT_MODERATE
    kek = pwhash.argon2id.kdf(32, password.encode(), salt, opslimit=ops, memlimit=mem)
    value = {
        "version": 1, "salt": b64(salt), "ops": ops, "mem": mem,
        "wrapped": b64(encrypt(kek, key, "vault:key:v1")),
        "recovery": b64(encrypt(recovery, key, "vault:recovery:v1")),
    }
    return json.dumps(value).encode("utf-8"), key, "recovery:" + b64(recovery)


def validate_envelope(raw: bytes) -> dict:
    """Bound untrusted KDF inputs before any expensive password derivation."""
    try:
        if len(raw) > 16384:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
            raise ValueError()
        if type(value.get("ops")) is not int or not 1 <= value["ops"] <= 10:
            raise ValueError()
        if type(value.get("mem")) is not int or not pwhash.argon2id.MEMLIMIT_MIN <= value["mem"] <= 1024**3:
            raise ValueError()
        for field, length in (("salt", pwhash.argon2id.SALTBYTES), ("wrapped", 72), ("recovery", 72)):
            if not isinstance(value.get(field), str) or len(base64.b64decode(value[field], validate=True)) != length:
                raise ValueError()
        return value
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("密钥信封格式或版本无效") from None


def unlock_envelope(raw: bytes, password: str) -> bytes:
    value = validate_envelope(raw)
    if password.startswith("recovery:"):
        try:
            recovery = base64.b64decode(password.removeprefix("recovery:"), validate=True)
            if len(recovery) != 32:
                raise ValueError()
            return decrypt(recovery, unb64(value["recovery"]), "vault:recovery:v1")
        except (ValueError, TypeError):
            raise ValueError("恢复密钥无效") from None
    kek = pwhash.argon2id.kdf(32, password.encode(), unb64(value["salt"]),
                             opslimit=value["ops"], memlimit=value["mem"])
    return decrypt(kek, unb64(value["wrapped"]), "vault:key:v1")


class DatabaseEnvelope:
    """The database is the sole normal source of key-envelope bytes.

    path is kept as a runtime-directory anchor for existing callers; it is
    never opened. Legacy file access belongs exclusively to migration tools.
    """
    def __init__(self, store, path):
        self.store = store
        self.path = Path(path)

    @property
    def exists(self):
        return self.store.read_envelope() is not None

    def read_bytes(self):
        value = self.store.read_envelope()
        if value is None:
            raise ValueError("数据库缺少密钥信封，请恢复或迁移配套备份")
        return value

    def create(self, password):
        raw, key, recovery = create_envelope(password)
        self.store.initialize_vault(raw)
        return key, recovery

    def unlock(self, password):
        return unlock_envelope(self.read_bytes(), password)


class KeyEnvelope:
    """Legacy file format, retained only for import and legacy test fixtures."""
    def __init__(self, path: Path):
        self.path = path

    @property
    def exists(self):
        return self.path.exists()

    def create(self, password: str) -> tuple[bytes, str]:
        if self.exists:
            raise ValueError("密码库已经初始化")
        raw, key, recovery = create_envelope(password)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Exclusive creation prevents concurrent setup from replacing a live vault.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return key, recovery

    def unlock(self, password: str) -> bytes:
        return unlock_envelope(self.path.read_bytes(), password)
