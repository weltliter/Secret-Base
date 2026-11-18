"""In-memory encrypted persistence doubles shared by regression checks."""
from datetime import datetime, timedelta, timezone
import uuid

from vault.core.crypto import validate_envelope, unlock_envelope, json_decrypt
from vault.core.store import check_metadata


class ByteStore:
    """Capture exactly what the service passes to persistent storage."""

    def __init__(self):
        self.rows = {}
        self.settings_blob = None
        self.query_history = []
        self.metadata = None
        self.processed_messages = set()
        self.audit = []

    def read_metadata(self):
        return check_metadata(self.metadata)

    def read_envelope(self):
        value = self.read_metadata()
        return value["envelope"] if value else None

    def has_data(self):
        return bool(self.rows or self.settings_blob or self.query_history or self.processed_messages or self.audit)

    def initialize_vault(self, raw):
        validate_envelope(raw)
        if self.read_metadata() is not None:
            raise ValueError("密码库已经初始化")
        if self.has_data():
            raise ValueError("数据库已有数据，请恢复配套的密钥信封，不要重新初始化")
        self.metadata = {"id": 1, "schema_version": 2, "vault_id": str(uuid.uuid4()),
                         "crypto_format": 1, "app_version": "1.1", "envelope": raw}

    def import_legacy_envelope(self, raw, password):
        key = unlock_envelope(raw, password)
        existing = self.read_metadata()
        if existing and existing["envelope"] != raw:
            raise ValueError("数据库已有不同密钥信封，迁移未执行")
        for row in self.rows.values():
            json_decrypt(key, row["payload"], "entry:" + str(row["id"]))
        if self.settings_blob:
            json_decrypt(key, self.settings_blob, "settings:v1")
        for row in self.query_history:
            json_decrypt(key, row["payload"], "query-log:" + str(row["id"]))
        if existing:
            return False
        self.metadata = {"id": 1, "schema_version": 2, "vault_id": str(uuid.uuid4()),
                         "crypto_format": 1, "app_version": "1.1", "envelope": raw}
        return True

    def list(self, trash=False):
        return [row for row in self.rows.values() if (row["deleted_at"] is not None) == trash]

    def get(self, entry_id):
        if entry_id not in self.rows:
            raise ValueError("记录不存在")
        return self.rows[entry_id]

    def put(self, entry_id, payload, version=None):
        old = self.rows.get(entry_id)
        if version is not None and (old is None or old["version"] != version):
            raise ValueError("记录已经修改")
        self.rows[entry_id] = {
            "id": entry_id,
            "payload": payload,
            "version": 1 if old is None else old["version"] + 1,
            "created_at": old["created_at"] if old else datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "deleted_at": None,
        }

    def read_settings(self):
        return self.settings_blob

    def write_settings(self, payload):
        self.settings_blob = payload

    def add_query_log(self, log_id, payload):
        self.query_history.insert(0, {"id": log_id, "payload": payload, "created_at": datetime.now(timezone.utc)})

    def query_logs(self, limit=50):
        return self.query_history[:limit]


class MemoryStore(ByteStore):
    def initialize(self, *, allow_legacy=False):
        self.read_metadata()
        if not self.metadata and self.has_data() and not allow_legacy:
            raise ValueError("数据库已有数据但缺少密钥信封，请先备份并执行旧版迁移")

    def trash(self, expected):
        for record_id, version in expected.items():
            row = self.get(record_id)
            if row["version"] != version or row["deleted_at"] is not None:
                raise ValueError("记录已经改变，删除未执行，请重新查询")
        for record_id in expected:
            row = self.rows[record_id]
            row["deleted_at"] = datetime.now(timezone.utc)
            row["updated_at"] = row["deleted_at"]
            row["version"] += 1

    def restore(self, record_id):
        row = self.get(record_id)
        if row["deleted_at"] is None or row["deleted_at"] < datetime.now(timezone.utc) - timedelta(days=30):
            raise ValueError("记录不在可恢复的回收站内")
        row["deleted_at"] = None
        row["updated_at"] = datetime.now(timezone.utc)
        row["version"] += 1


