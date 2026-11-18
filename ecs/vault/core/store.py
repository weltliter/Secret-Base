"""PostgreSQL persistence. Business values arrive here only as ciphertext."""
import os
import uuid
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from .crypto import json_decrypt, unlock_envelope, validate_envelope

SCHEMA_VERSION = 2
CRYPTO_FORMAT = 1
APP_VERSION = "1.1"
BUSINESS_TABLES = ("entries", "settings", "processed_messages", "audit", "query_logs")


def check_metadata(value):
    if value is not None:
        if value["schema_version"] != SCHEMA_VERSION or value["crypto_format"] != CRYPTO_FORMAT:
            raise ValueError("不支持的数据库或加密格式版本，请使用兼容的应用版本")
        uuid.UUID(str(value["vault_id"]))
        validate_envelope(bytes(value["envelope"]))
    return value


class Store:
    def __init__(self, dsn=None):
        self.dsn = dsn or os.environ.get("DATABASE_URL", "dbname=vault user=vault_app host=/var/run/postgresql")

    @contextmanager
    def connection(self):
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5) as conn:
            yield conn

    @staticmethod
    def _metadata(conn):
        if not conn.execute("SELECT to_regclass('vault_metadata') AS name").fetchone()["name"]:
            return None
        rows = conn.execute("SELECT * FROM vault_metadata").fetchall()
        if len(rows) > 1 or (rows and rows[0]["id"] != 1):
            raise ValueError("数据库元数据无效")
        return check_metadata(rows[0] if rows else None)

    @staticmethod
    def _check_schema(conn):
        if conn.execute("SELECT to_regclass('vault_schema') AS name").fetchone()["name"]:
            versions = conn.execute("SELECT id, version FROM vault_schema").fetchall()
            if len(versions) != 1 or versions[0]["id"] != 1 or versions[0]["version"] != SCHEMA_VERSION:
                raise ValueError("不支持的数据库结构版本，请使用兼容的应用版本")

    @staticmethod
    def _has_data(conn):
        # Names are a fixed application allowlist, never request input.
        for name in BUSINESS_TABLES:
            if conn.execute("SELECT to_regclass(%s) AS name", (name,)).fetchone()["name"]:
                if conn.execute(f"SELECT EXISTS(SELECT 1 FROM {name}) AS present").fetchone()["present"]:
                    return True
        return False

    def initialize(self, *, allow_legacy=False):
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(1927711011)")
            self._check_schema(conn)
            metadata = self._metadata(conn)
            if metadata is None and self._has_data(conn) and not allow_legacy:
                raise ValueError("数据库已有数据但缺少密钥信封，请先备份并执行旧版迁移")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vault_schema (
                    id integer PRIMARY KEY CHECK(id=1), version integer NOT NULL
                );
                INSERT INTO vault_schema(id,version) VALUES(1,2) ON CONFLICT(id) DO NOTHING;
                CREATE TABLE IF NOT EXISTS vault_metadata (
                    id integer PRIMARY KEY CHECK(id=1), schema_version integer NOT NULL,
                    vault_id uuid NOT NULL UNIQUE, crypto_format integer NOT NULL,
                    app_version text NOT NULL, envelope bytea NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entries (
                    id uuid PRIMARY KEY, payload bytea NOT NULL,
                    version integer NOT NULL DEFAULT 1,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    deleted_at timestamptz
                );
                CREATE TABLE IF NOT EXISTS settings (id integer PRIMARY KEY CHECK(id=1), payload bytea NOT NULL);
                CREATE TABLE IF NOT EXISTS processed_messages (id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now());
                CREATE TABLE IF NOT EXISTS audit (id bigserial PRIMARY KEY, action text NOT NULL, entry_id uuid, created_at timestamptz NOT NULL DEFAULT now());
                CREATE TABLE IF NOT EXISTS query_logs (id uuid PRIMARY KEY, payload bytea NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
                CREATE INDEX IF NOT EXISTS query_logs_created_idx ON query_logs(created_at DESC);
            """)

    def read_metadata(self):
        with self.connection() as conn:
            self._check_schema(conn)
            value = self._metadata(conn)
            if value is not None:
                value["envelope"] = bytes(value["envelope"])
                value["vault_id"] = str(value["vault_id"])
            return value

    def read_envelope(self):
        metadata = self.read_metadata()
        return metadata["envelope"] if metadata else None

    @staticmethod
    def _insert_envelope(conn, raw):
        conn.execute("""INSERT INTO vault_metadata
            (id,schema_version,vault_id,crypto_format,app_version,envelope)
            VALUES(1,%s,%s,%s,%s,%s)""",
            (SCHEMA_VERSION, uuid.uuid4(), CRYPTO_FORMAT, APP_VERSION, raw))

    def initialize_vault(self, raw):
        validate_envelope(raw)
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(1927711011)")
            self._check_schema(conn)
            conn.execute("LOCK TABLE vault_metadata, entries, settings, processed_messages, audit, query_logs IN SHARE ROW EXCLUSIVE MODE")
            if self._metadata(conn) is not None:
                raise ValueError("密码库已经初始化")
            if self._has_data(conn):
                raise ValueError("数据库已有数据，请恢复配套的密钥信封，不要重新初始化")
            self._insert_envelope(conn, raw)

    @staticmethod
    def _verify_all(conn, key):
        for table, prefix in (("entries", "entry:"), ("settings", "settings:v1"), ("query_logs", "query-log:")):
            with conn.cursor(name="verify_" + table) as cursor:
                cursor.itersize = 100
                cursor.execute(f"SELECT id,payload FROM {table}")
                for row in cursor:
                    context = prefix if table == "settings" else prefix + str(row["id"])
                    json_decrypt(key, bytes(row["payload"]), context)

    def import_legacy_envelope(self, raw, password):
        key = unlock_envelope(raw, password)
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(1927711011)")
            self._check_schema(conn)
            conn.execute("LOCK TABLE vault_metadata, entries, settings, processed_messages, audit, query_logs IN SHARE ROW EXCLUSIVE MODE")
            existing = self._metadata(conn)
            if existing is not None and bytes(existing["envelope"]) != raw:
                raise ValueError("数据库已有不同密钥信封，迁移未执行")
            self._verify_all(conn, key)
            if existing is not None:
                return False
            self._insert_envelope(conn, raw)
            return True

    def list(self, trash=False):
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM entries WHERE deleted_at IS " + ("NOT NULL" if trash else "NULL") + " ORDER BY updated_at DESC, id"
            ).fetchall()

    def get(self, entry_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM entries WHERE id=%s", (entry_id,)).fetchone()
        if row is None:
            raise ValueError("记录不存在")
        return row

    def put(self, entry_id, payload, version=None):
        with self.connection() as conn:
            if version is None:
                conn.execute("INSERT INTO entries(id,payload) VALUES (%s,%s)", (entry_id, payload))
                action = "create"
            else:
                result = conn.execute("UPDATE entries SET payload=%s, version=version+1, updated_at=now() WHERE id=%s AND version=%s AND deleted_at IS NULL", (payload, entry_id, version))
                if result.rowcount != 1:
                    raise ValueError("记录已经修改或删除，请刷新后重试")
                action = "update"
            conn.execute("INSERT INTO audit(action,entry_id) VALUES(%s,%s)", (action, entry_id))

    def trash(self, expected: dict):
        with self.connection() as conn:
            for entry_id, version in expected.items():
                result = conn.execute("UPDATE entries SET deleted_at=now(), updated_at=now(), version=version+1 WHERE id=%s AND version=%s AND deleted_at IS NULL", (entry_id, version))
                if result.rowcount != 1:
                    raise ValueError("记录已经改变，删除未执行，请重新查询")
                conn.execute("INSERT INTO audit(action,entry_id) VALUES('trash',%s)", (entry_id,))

    def restore(self, entry_id):
        with self.connection() as conn:
            result = conn.execute("UPDATE entries SET deleted_at=NULL, updated_at=now(), version=version+1 WHERE id=%s AND deleted_at > now()-interval '30 days'", (entry_id,))
            if result.rowcount != 1:
                raise ValueError("记录不在可恢复的回收站内")
            conn.execute("INSERT INTO audit(action,entry_id) VALUES('restore',%s)", (entry_id,))

    def read_settings(self):
        with self.connection() as conn:
            row = conn.execute("SELECT payload FROM settings WHERE id=1").fetchone()
            return bytes(row["payload"]) if row else None

    def write_settings(self, payload):
        with self.connection() as conn:
            conn.execute("INSERT INTO settings(id,payload) VALUES(1,%s) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (payload,))

    def mark_message(self, digest):
        with self.connection() as conn:
            return conn.execute("INSERT INTO processed_messages(id) VALUES(%s) ON CONFLICT DO NOTHING", (digest,)).rowcount == 1

    def add_query_log(self, log_id, payload):
        with self.connection() as conn:
            conn.execute("INSERT INTO query_logs(id,payload) VALUES(%s,%s)", (log_id, payload))
            conn.execute("DELETE FROM query_logs WHERE id IN (SELECT id FROM query_logs ORDER BY created_at DESC OFFSET 2000)")

    def query_logs(self, limit=50):
        with self.connection() as conn:
            return conn.execute("SELECT id,payload,created_at FROM query_logs ORDER BY created_at DESC LIMIT %s", (min(max(limit,1),100),)).fetchall()

    def maintenance(self):
        with self.connection() as conn:
            conn.execute("DELETE FROM entries WHERE deleted_at < now()-interval '30 days'")
            conn.execute("DELETE FROM processed_messages WHERE created_at < now()-interval '30 days'")
            conn.execute("DELETE FROM audit WHERE created_at < now()-interval '90 days'")
            conn.execute("DELETE FROM query_logs WHERE created_at < now()-interval '30 days'")
