"""Real HTTP backup -> fresh PostgreSQL recovery using synthetic data only.

Run inside the app image with pg_dump/pg_restore 17 installed. Explicitly set
VAULT_TEST_ADMIN_DSN to an isolated local PostgreSQL server and pass
--confirm-isolated. Only newly created vault_recovery_* databases are dropped.
"""
import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

# A read-only /checks bind is outside Docker WORKDIR=/app. Keep the script
# usable both there and from a source checkout without copying private state.
for source_root in (Path.cwd(), *Path(__file__).resolve().parents):
    if (source_root / "vault").is_dir():
        sys.path.insert(0, str(source_root))
        break

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from vault.core.crypto import decrypt, encrypt
from vault.core.service import VaultService
from vault.core.store import Store


MASTER = "recovery-check-only master password 928417"
RECORD = {"title": "恢复测试云平台", "service": "recovery.example.invalid", "username": "fixture-account-928417",
          "password": "fixture-password-328579", "description": "隔离恢复合成记录", "tags": ["验收"],
          "notes": "fixture-private-note-8274", "fields": {"密保": "fixture-answer-7519"}}
PRIVATE_QUERY = "fixture-query-privacy-6472"
API_KEY = "fixture-model-api-key-3678"
TOKEN = "fixture-wechat-token-9816"


def emit(stage, **fields):
    print(json.dumps({"stage": stage, **fields}, ensure_ascii=False), flush=True)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def memory_peak(pid):
    status = Path(f"/proc/{pid}/status")
    if not status.exists():
        return 0
    for line in status.read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) * 1024
    return 0


class HttpApp:
    def __init__(self, dsn, root):
        self.root = root
        root.mkdir(parents=True)
        self.runtime = root / "runtime"
        self.runtime.mkdir()
        self.machine = root / "new-machine.key"
        self.machine.write_bytes(os.urandom(32))
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ, DATABASE_URL=dsn, VAULT_DATA_DIR=str(self.runtime),
                   VAULT_QUERY_KEY_FILE=str(self.machine), VAULT_BACKUP_DIR=str(root / "backups" / "web"),
                   VAULT_MAINTENANCE_LOCK=str(root / "backups" / ".backup.lock"),
                   VAULT_BACKUP_RESERVE_BYTES="1073741824", VAULT_BACKUP_QUOTA_BYTES="5368709120")
        # No original SSH credentials or machine-local startup envelope are passed.
        for name in ("SSH_AUTH_SOCK", "ECS_PRIVATE_KEY_FILE", "ECS_HOST", "VAULT_EMBEDDING_DIR"):
            env.pop(name, None)
        self.log = (root / "app.log").open("wb")
        self.process = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                                         "--serve", "--port", str(self.port)],
                                        env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.client = httpx.Client(base_url=self.url, timeout=15, trust_env=False)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Synthetic HTTP app exited before readiness; inspect its isolated log")
            try:
                if self.client.get("/health").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("Synthetic HTTP app failed readiness")
        self.client.headers["X-Vault-CSRF"] = self.client.get("/api/csrf").json()["token"]

    def login(self, credential=MASTER):
        response = self.client.post("/api/unlock", json={"password": credential})
        assert response.status_code == 200, ("login", response.status_code)

    def close(self):
        self.client.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        self.log.close()


def serve(port):
    import uvicorn
    from vault.web.app import create_app
    # The bot never calls external WeChat/AI services during synthetic checks.
    uvicorn.run(create_app(start_bot=False), host="127.0.0.1", port=port,
                access_log=False, log_level="warning")


def run(args):
    if not args.confirm_isolated:
        raise SystemExit("Pass --confirm-isolated only for a disposable test PostgreSQL server")
    admin_dsn = os.environ.get("VAULT_TEST_ADMIN_DSN")
    if not admin_dsn:
        raise SystemExit("Set VAULT_TEST_ADMIN_DSN explicitly; production DATABASE_URL is never used")
    parsed = conninfo_to_dict(admin_dsn)
    if parsed.get("host", "") not in ("127.0.0.1", "localhost", "::1") and not parsed.get("host", "").startswith("/"):
        raise SystemExit("Acceptance databases must be on an explicit loopback or local Unix socket")
    for binary in ("pg_dump", "pg_restore"):
        version = subprocess.run([binary, "--version"], check=True, capture_output=True, text=True).stdout
        assert re.search(r"\b17(?:\.|\b)", version), f"{binary} major version must be 17"
    if args.large_mib < 513 and not args.quick:
        raise SystemExit("Full acceptance requires --large-mib >= 513 (or explicitly use --quick)")
    padding_mib = 0 if args.quick else args.large_mib
    identifier = uuid.uuid4().hex
    databases = [f"vault_recovery_{identifier}_source", f"vault_recovery_{identifier}_restored"]
    created = []
    apps = []
    report = {"mode": "quick" if args.quick else "full", "padding_mib": padding_mib, "checks": []}
    with tempfile.TemporaryDirectory(prefix="vault-recovery-", dir=args.work_dir) as temporary:
        root = Path(temporary)
        try:
            with psycopg.connect(admin_dsn, autocommit=True) as admin:
                for database in databases:
                    admin.execute(sql.SQL("CREATE DATABASE {} OWNER vault_app").format(sql.Identifier(database)))
                    created.append(database)
            source_dsn, restored_dsn = [make_conninfo(admin_dsn, dbname=name, user="vault_app") for name in databases]
            source = HttpApp(source_dsn, root / "source")
            apps.append(source)
            response = source.client.post("/api/setup", json={"password": MASTER})
            assert response.status_code == 200
            recovery = response.json()["recovery_key"]
            old_cookie = source.client.cookies.get("vault_admin")
            ids = []
            for title in (RECORD["title"], "待恢复的回收站记录"):
                result = source.client.post("/api/entries", json={**RECORD, "title": title})
                assert result.status_code == 200
                ids.append(result.json()["id"])
            assert source.client.post(f"/api/entries/{ids[1]}/trash", json={"version": 1}).status_code == 200
            store = Store(source_dsn)
            service = VaultService(store, root / "seed-runtime" / "vault.json")
            service.unlock(MASTER)
            service.update_settings({"deepseek_api_key": API_KEY, "wechat_owner": "fixture-owner", "wechat_token": TOKEN})
            service.record_query(PRIVATE_QUERY, [], {"ids": []})
            assert store.mark_message("fixture-processed-message")
            metadata = store.read_metadata()
            assert not source.runtime.joinpath("vault.json").exists()

            if padding_mib:
                emit("padding", target_mib=padding_mib)
                with store.connection() as conn:
                    conn.execute("CREATE TABLE acceptance_padding(id integer PRIMARY KEY, payload bytea NOT NULL)")
                    for index in range(padding_mib):
                        # Authenticated incompressible ciphertext, bounded to one MiB per insert.
                        block = encrypt(service.key, os.urandom(1024 * 1024), "acceptance-padding")
                        conn.execute("INSERT INTO acceptance_padding VALUES(%s,%s)", (index, block))
                        if (index + 1) % 64 == 0:
                            emit("padding", completed_mib=index + 1)

            source.login()
            assert source.client.post("/api/backups", headers={"Origin": "https://wrong.example"}, json={}).status_code == 403
            response = source.client.post("/api/backups", json={})
            assert response.status_code in (200, 202), ("create backup", response.status_code)
            task = response.json()
            task_id = task["id"]
            duplicate = source.client.post("/api/backups", json={}).json()
            # A very small quick backup may finish between two clicks.
            current = source.client.get(f"/api/backups/{task_id}").json()
            if current["status"] == "generating":
                assert duplicate["id"] == task_id
            deadline = time.monotonic() + 1850
            last_progress = 0
            while task["status"] == "generating":
                assert time.monotonic() < deadline, "Backup generation exceeded its limit"
                result = source.client.get(f"/api/backups/{task_id}")
                if result.status_code == 423:
                    source.login()
                    continue
                assert result.status_code == 200
                task = result.json()
                if time.monotonic() - last_progress > 20:
                    emit("backup", status=task["status"], phase=task.get("phase"))
                    last_progress = time.monotonic()
                time.sleep(0.5)
            assert task["status"] == "ready", (task["status"], task.get("error_code"))
            if padding_mib:
                assert task["size_bytes"] > 512 * 1024**2, "Compressed fixture must exceed application memory limit"
            source.login()
            download = root / "only-portable-data.dump"
            digest = hashlib.sha256()
            size = 0
            peak = memory_peak(source.process.pid)
            logout_seconds = None
            emit("download", expected_bytes=task["size_bytes"])
            with source.client.stream("GET", f"/api/backups/{task_id}/download", timeout=1800) as response:
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
                assert "attachment" in response.headers["content-disposition"]
                with download.open("wb") as target:
                    for block in response.iter_bytes(chunk_size=65536):
                        target.write(block)
                        digest.update(block)
                        size += len(block)
                        peak = max(peak, memory_peak(source.process.pid))
                        if logout_seconds is None:
                            started = time.monotonic()
                            assert source.client.post("/api/lock", json={}).status_code == 200
                            logout_seconds = time.monotonic() - started
                            assert logout_seconds < 5, "Logout blocked by backup/download"
                            assert source.client.get(f"/api/backups/{task_id}/download").status_code == 423
                            assert source.client.get("/api/entries").status_code == 423
                            assert source.client.get("/api/status").json()["query_ready"]
            assert size == task["size_bytes"] and digest.hexdigest() == task["sha256"]
            assert peak and peak < 512 * 1024**2, "App high-water resident memory exceeded 512 MiB"
            report.update(download_bytes=size, sha256=digest.hexdigest(), app_peak_rss_bytes=peak,
                          logout_seconds=round(logout_seconds, 3))
            source.login()
            assert task_id in [item["id"] for item in source.client.get("/api/backups").json()["tasks"]]
            checksum = source.client.get(f"/api/backups/{task_id}/checksum")
            assert checksum.status_code == 200 and task["sha256"] in checksum.text
            assert source.client.get(f"/api/backups/{task_id}/manifest").status_code == 200
            source.close()
            apps.remove(source)
            source.machine.unlink()
            source.runtime.joinpath("query-unlock.json").unlink()
            assert not source.runtime.joinpath("vault.json").exists()
            report["checks"].extend(["native HTTP streaming and SHA-256", "logout during admitted download",
                                     "locked new download rejected", "query runtime survives logout", "no legacy envelope file"])

            result = subprocess.run(["pg_restore", "--no-owner", "--exit-on-error", "--single-transaction",
                                     "--dbname", restored_dsn, str(download)], capture_output=True)
            assert result.returncode == 0, "Restore into new isolated database failed"
            restored = Store(restored_dsn)
            assert restored.read_metadata() == metadata
            fresh = HttpApp(restored_dsn, root / "replacement")
            apps.append(fresh)
            assert not fresh.runtime.joinpath("vault.json").exists()
            assert not fresh.runtime.joinpath("query-unlock.json").exists()
            fresh.client.cookies.set("vault_admin", old_cookie)
            assert fresh.client.get("/api/entries").status_code == 423
            fresh.client.cookies.clear()
            assert fresh.client.post("/api/unlock", json={"password": "incorrect synthetic credential"}).status_code == 400
            time.sleep(2.1)
            fresh.login(MASTER)
            assert fresh.client.get(f"/api/entries/{ids[0]}/reveal").json()["password"] == RECORD["password"]
            verification = VaultService(restored, root / "verification" / "vault.json")
            verification.unlock(recovery)
            assert verification.entries(True)[0]["id"] == ids[1]
            assert verification.settings()["deepseek_api_key"] == API_KEY
            assert verification.query_logs()[0]["query"] == PRIVATE_QUERY
            assert not restored.mark_message("fixture-processed-message")
            assert fresh.client.post("/api/lock", json={}).status_code == 200
            fresh.login(recovery)
            assert fresh.client.get(f"/api/entries/{ids[0]}/reveal").json()["password"] == RECORD["password"]
            assert fresh.runtime.joinpath("query-unlock.json").is_file()

            forbidden = [MASTER, recovery, RECORD["username"], RECORD["password"], RECORD["notes"],
                         RECORD["fields"]["密保"], API_KEY, TOKEN, PRIVATE_QUERY]
            with restored.connection() as conn:
                for table in ("entries", "settings", "query_logs"):
                    for row in conn.execute(f"SELECT payload FROM {table}"):
                        assert all(value.encode() not in bytes(row["payload"]) for value in forbidden)
                assert conn.execute("SELECT count(*) AS n FROM audit").fetchone()["n"] >= 3
                before = conn.execute("SELECT max(id) AS n FROM audit").fetchone()["n"]
                if padding_mib:
                    assert conn.execute("SELECT count(*) AS n FROM acceptance_padding").fetchone()["n"] == padding_mib
                    block = conn.execute("SELECT payload FROM acceptance_padding WHERE id=0").fetchone()["payload"]
                    assert len(decrypt(verification.key, bytes(block), "acceptance-padding")) == 1024 * 1024
            verification.save({**RECORD, "title": "恢复后新增"})
            with restored.connection() as conn:
                assert conn.execute("SELECT max(id) AS n FROM audit").fetchone()["n"] > before
            raw_envelope = restored.read_envelope()
            assert all(value.encode() not in raw_envelope for value in forbidden)
            assert fresh.machine.read_bytes() not in raw_envelope
            fresh.close()
            apps.remove(fresh)
            for app_root in (source.root, fresh.root):
                log = (app_root / "app.log").read_bytes()
                assert all(value.encode() not in log for value in forbidden), "Secret in application runtime log"
            report["checks"].extend(["fresh database and new machine key", "master and recovery unlock",
                                     "old cookie and wrong credential rejected", "trash/settings/logs/dedupe/sequences restored",
                                     "actual restored ciphertext and runtime logs inspected"])
            emit("passed", **report)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        finally:
            for app in apps:
                app.close()
            with psycopg.connect(admin_dsn, autocommit=True) as admin:
                for database in reversed(created):
                    assert re.fullmatch(r"vault_recovery_[a-f0-9]{32}_(source|restored)", database)
                    admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-isolated", action="store_true")
    parser.add_argument("--large-mib", type=int, default=520)
    parser.add_argument("--quick", action="store_true", help="skip large transfer; not full release acceptance")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(args.port)
    else:
        run(args)


if __name__ == "__main__":
    main()
