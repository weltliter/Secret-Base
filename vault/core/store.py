"""PostgreSQL persistence. Business values arrive here only as ciphertext."""
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


class Store:
    def __init__(self, dsn=None):
        self.dsn = dsn or os.environ.get("DATABASE_URL", "dbname=vault user=vault_app host=/var/run/postgresql")

    @contextmanager
    def connection(self):
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5) as conn:
            yield conn

    def initialize(self):
        with self.connection() as conn:
            conn.execute("""
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
