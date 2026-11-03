"""In-memory encrypted persistence doubles shared by regression checks."""
from datetime import datetime, timedelta, timezone


class ByteStore:
    """Capture exactly what the service passes to persistent storage."""

    def __init__(self):
        self.rows = {}
        self.settings_blob = None
        self.query_history = []

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
    def initialize(self):
        pass

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


