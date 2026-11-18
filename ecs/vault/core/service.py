import threading
import os
import time
import uuid

from .crypto import DatabaseEnvelope, json_decrypt, json_encrypt
from .duplicates import compare_entries
from .query_unlock import QueryUnlock
from .maintenance import maintenance_lock


class VaultService:
    def __init__(self, store, envelope_path, query_key_path=None):
        self.store = store
        self.envelope = DatabaseEnvelope(store, envelope_path)
        self.query_unlock = QueryUnlock(self.envelope, query_key_path)
        self.mutex = threading.RLock()
        self.key = None
        self.epoch = 0
        self.failed_unlocks = 0
        self.next_unlock = 0

    @property
    def unlocked(self):
        return self.key is not None

    def require_key(self):
        if self.key is None:
            raise ValueError("密码库已锁定，请通过电脑管理页解锁")
        return self.key

    def setup(self, password):
        lock_path = os.environ.get("VAULT_MAINTENANCE_LOCK") or self.envelope.path.parent / ".maintenance.lock"
        with maintenance_lock(lock_path), self.mutex:
            self.query_unlock.machine_key()
            if self.store.read_settings() or self.store.list() or self.store.list(True):
                raise ValueError("数据库已有数据，请恢复配套的密钥信封，不要重新初始化")
            self.key, recovery = self.envelope.create(password)
            self.query_unlock.enable(self.key)
            self.epoch += 1
            return recovery

    def unlock(self, password):
        with self.mutex:
            if time.monotonic() < self.next_unlock:
                raise ValueError("请稍后再尝试解锁")
            try:
                key = self.envelope.unlock(password)
                # Detect an accidentally mismatched envelope and database before exposing data.
                self._verify_key(key)
            except (ValueError, FileNotFoundError):
                self.failed_unlocks += 1
                self.next_unlock = time.monotonic() + min(30, 2 ** min(self.failed_unlocks, 5))
                raise ValueError("主密码或恢复密钥错误") from None
            self.query_unlock.enable(key)
            if self.key != key:
                self.key = key
                self.epoch += 1
            self.failed_unlocks = 0
            self.next_unlock = 0

    def _verify_key(self, key):
        settings = self.store.read_settings()
        if settings:
            json_decrypt(key, settings, "settings:v1")
        else:
            rows = self.store.list() or self.store.list(True)
            if rows:
                row = rows[0]
                json_decrypt(key, bytes(row["payload"]), "entry:" + str(row["id"]))

    def resume_queries(self):
        with self.mutex:
            key = self.query_unlock.load()
            if key is None:
                return False
            self._verify_key(key)
            self.key = key
            self.epoch += 1
            self.query_unlock.ready = True
            return True

    def lock(self):
        with self.mutex:
            self.key = None
            self.epoch += 1

    def _decode(self, row):
        entry_id = str(row["id"])
        value = json_decrypt(self.require_key(), bytes(row["payload"]), "entry:" + entry_id)
        value.update(id=entry_id, version=row["version"], created_at=row["created_at"].isoformat(), updated_at=row["updated_at"].isoformat(), deleted=row["deleted_at"] is not None)
        return value

    def entries(self, trash=False):
        with self.mutex:
            self.require_key()
            return [self._decode(row) for row in self.store.list(trash)]

    def entry(self, entry_id, allow_trash=False):
        with self.mutex:
            value = self._decode(self.store.get(entry_id))
            if value["deleted"] and not allow_trash:
                raise ValueError("记录已删除")
            return value

    def save(self, value, entry_id=None, version=None):
        with self.mutex:
            key = self.require_key()
            if entry_id is not None:
                original = self.entry(entry_id)
                if value.get("password") is None:
                    value["password"] = original.get("password", "")
            else:
                entry_id = str(uuid.uuid4())
                value["password"] = value.get("password") or ""
            value["tags"] = list(dict.fromkeys(tag.strip() for tag in value.get("tags", []) if tag.strip()))
            self.store.put(entry_id, json_encrypt(key, value, "entry:" + entry_id), version)
            return entry_id

    def settings(self):
        with self.mutex:
            key = self.require_key()
            payload = self.store.read_settings()
            return json_decrypt(key, payload, "settings:v1") if payload else {}

    def update_settings(self, changes):
        with self.mutex:
            settings = self.settings()
            settings.update(changes)
            self.store.write_settings(json_encrypt(self.require_key(), settings, "settings:v1"))

    def descriptors(self):
        entries = self.entries()
        # Also redact known secrets accidentally pasted into an allowed field.
        secret_values = sorted({v for e in entries for v in
                                [e.get("username", ""), e.get("password", ""), *e.get("fields", {}).values()]
                                if isinstance(v, str) and v}, key=len, reverse=True)
        def scrub(text):
            for secret in secret_values:
                text = text.replace(secret, "[机密已隐藏]")
            return text
        return [{"id": e["id"], "title": scrub(e.get("title", "")),
                 "service": scrub(e.get("service", "")), "description": scrub(e.get("description", "")),
                 "tags": [scrub(tag) for tag in e.get("tags", [])]} for e in entries]

    def tags(self):
        return sorted({tag.strip() for entry in self.entries() for tag in entry.get("tags", []) if tag.strip()}, key=str.casefold)

    def record_query(self, query, descriptors, result):
        """Store diagnostic history as ciphertext, never a rendered credential reply."""
        with self.mutex:
            key = self.require_key()
            secrets = [value for row in self.entries() for value in
                       (row.get("username", ""), row.get("password", ""), row.get("notes", ""), *row.get("fields", {}).values())]
            settings = self.settings()
            secrets.extend(settings.get(name, "") for name in ("deepseek_api_key", "wechat_token"))
            for value in sorted({value for value in secrets if isinstance(value, str) and value}, key=len, reverse=True):
                query = query.replace(value, "[机密已隐藏]")
            by_id = {row["id"]: row for row in descriptors}
            weak = set(result.get("weak_ids", []))
            scores = result.get("vector_scores", {})
            value = {"query": query[:4096], "diagnostics": result.get("diagnostics", {}),
                     "results": [{"id": record_id, "title": by_id[record_id]["title"], "weak": record_id in weak,
                                  "similarity": scores.get(record_id)} for record_id in result["ids"] if record_id in by_id]}
            log_id = str(uuid.uuid4())
            self.store.add_query_log(log_id, json_encrypt(key, value, "query-log:" + log_id))

    def query_logs(self, limit=50):
        with self.mutex:
            key = self.require_key()
            return [{**json_decrypt(key, bytes(row["payload"]), "query-log:" + str(row["id"])),
                     "id": str(row["id"]), "created_at": row["created_at"].isoformat()} for row in self.store.query_logs(limit)]

    def duplicate_groups(self, *, include_reuse=False):
        return compare_entries(self.entries(), include_reuse=include_reuse)

    def trash(self, expected):
        with self.mutex:
            self.require_key()
            self.store.trash(expected)

    def restore(self, entry_id):
        with self.mutex:
            self.require_key()
            self.store.restore(entry_id)


class QueryVault:
    """Capability surface for WeChat: active-record reads and transport state only.

    Business writes are deliberately absent. The local admin API receives the
    full VaultService; the chat controller receives this limited view.
    """
    def __init__(self, vault):
        self._vault = vault
        self.mutex = vault.mutex

    @property
    def epoch(self):
        return self._vault.epoch

    @property
    def unlocked(self):
        return self._vault.unlocked

    def settings(self):
        return self._vault.settings()

    def entries(self):
        return self._vault.entries()

    def entry(self, entry_id):
        return self._vault.entry(entry_id)

    def descriptors(self):
        return self._vault.descriptors()

    def duplicate_groups(self):
        return self._vault.duplicate_groups()

    def mark_message(self, digest):
        return self._vault.store.mark_message(digest)

    def save_cursor(self, cursor):
        self._vault.update_settings({"wechat_cursor": cursor})

    def record_query(self, query, descriptors, result):
        self._vault.record_query(query, descriptors, result)
