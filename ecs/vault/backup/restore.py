"""Restore application data atomically; never execute SQL from an uploaded dump."""
import re
from contextlib import nullcontext

from ..core.store import SCHEMA_VERSION, check_metadata
from .manager import BackupError


COLUMNS = {
    "vault_schema": "id, version",
    "vault_metadata": "id, schema_version, vault_id, crypto_format, app_version, envelope",
    "entries": "id, payload, version, created_at, updated_at, deleted_at",
    "settings": "id, payload",
    "processed_messages": "id, created_at",
    "audit": "id, action, entry_id, created_at",
    "query_logs": "id, payload, created_at",
}
COPY = re.compile(rb"COPY public\.([a-z_]+) \(([^\r\n]+)\) FROM stdin;\n\Z")
SEQUENCE = re.compile(rb"SELECT pg_catalog\.setval\('public.audit_id_seq', ([0-9]+), (true|false)\);\n\Z")


def copy_data(stream, writer, check):
    """Bound memory even for a single very large COPY row."""
    line_start = True
    while True:
        check()
        block = stream.readline(65536)
        if not block:
            raise BackupError("invalid_restore")
        if line_start and block == b"\\.\n":
            return
        writer.write(block)
        line_start = block.endswith(b"\n")


def replace_data(store, path, check, replacement=nullcontext):
    """Stage typed rows, validate metadata, then replace all seven tables in one tx.

    Names/columns and COPY commands are application constants. Archive SQL,
    functions, triggers and schema definitions are never executed. The deployed
    vault_app role needs no new privileges (in particular no CREATEDB).
    """
    with store.connection() as conn:
        conn.execute("SET LOCAL statement_timeout = '30min'")
        conn.execute("SET LOCAL lock_timeout = '5s'")
        conn.execute("SET LOCAL search_path = public, pg_catalog")
        for table in COLUMNS:
            conn.execute(f"CREATE TEMP TABLE restore_{table} (LIKE public.{table} INCLUDING ALL) ON COMMIT DROP")
        seen, sequence = set(), None
        with path.open("rb") as stream:
            while line := stream.readline(8192):
                check()
                if len(line) >= 8192:
                    raise BackupError("invalid_restore")
                match = COPY.fullmatch(line)
                if match:
                    table, columns = (part.decode("ascii") for part in match.groups())
                    if table not in COLUMNS or table in seen or columns != COLUMNS[table]:
                        raise BackupError("invalid_restore")
                    seen.add(table)
                    with conn.cursor().copy(f"COPY restore_{table} ({COLUMNS[table]}) FROM STDIN") as writer:
                        copy_data(stream, writer, check)
                elif match := SEQUENCE.fullmatch(line):
                    if sequence is not None:
                        raise BackupError("invalid_restore")
                    sequence = int(match[1]) + (match[2] == b"true")
                elif line.startswith(b"COPY "):
                    raise BackupError("invalid_restore")
                # All other archive text is ignored, never passed to execute().
        if seen != set(COLUMNS) or sequence is None or not 1 <= sequence < 2**63:
            raise BackupError("invalid_restore")
        versions = conn.execute("SELECT * FROM restore_vault_schema").fetchall()
        metadata = conn.execute("SELECT * FROM restore_vault_metadata").fetchall()
        if versions != [{"id": 1, "version": SCHEMA_VERSION}] or len(metadata) != 1 or metadata[0]["id"] != 1:
            raise BackupError("invalid_restore")
        check_metadata(metadata[0])
        maximum = conn.execute("SELECT coalesce(max(id), 0) AS n FROM restore_audit").fetchone()["n"]
        if sequence <= maximum:
            raise BackupError("invalid_restore")
        check()
        # TRUNCATE + INSERT + ALTER SEQUENCE are transactional, including rollback
        # of the sequence (setval would escape rollback).
        with replacement():
            conn.execute("TRUNCATE " + ", ".join("public." + table for table in COLUMNS))
            for table, columns in COLUMNS.items():
                check()
                conn.execute(f"INSERT INTO public.{table} ({columns}) SELECT {columns} FROM restore_{table}")
            conn.execute(f"ALTER SEQUENCE public.audit_id_seq RESTART WITH {sequence}")
            check()
            conn.commit()
