"""Bounded disk-backed PostgreSQL snapshots. No business key material leaves DB."""
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..core.maintenance import maintenance_lock

LOG = logging.getLogger("vault.backup")
ID = re.compile(r"[0-9a-f]{32}\Z")
ERRORS = {
    "busy": "另一个备份、恢复或迁移正在进行，请稍后重试。",
    "space": "备份目录配额或磁盘剩余空间不足。",
    "timeout": "生成备份超时，请检查服务器资源后重试。",
    "interrupted": "生成任务被中断，请重新生成备份。",
    "tools": "需要 PostgreSQL 17 导出和恢复工具。",
    "export": "数据库导出或完整性检查失败，请检查服务配置。",
}


class BackupError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(ERRORS.get(code, code))


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def regular(path):
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


class Download:
    def __init__(self, manager, task, stream):
        self.manager, self.task, self.stream = manager, task, stream
        self.started = manager.monotonic()
        self.deadline = self.started + manager.limits["download_timeout_seconds"]
        self.sent = 0
        self.closed = False

    def chunks(self):
        try:
            while self.manager.monotonic() < self.deadline:
                block = self.stream.read(64 * 1024)
                if not block:
                    return
                self.sent += len(block)
                yield block
        finally:
            self.close()

    def close(self):
        with self.manager.mutex:
            if self.closed:
                return
            self.closed = True
            self.stream.close()
            identity = self.task["id"]
            self.manager.pins[identity] -= 1
            event = {"at": stamp(self.manager.clock()), "size_bytes": self.sent,
                     "seconds": round(self.manager.monotonic() - self.started, 3),
                     "status": "sent" if self.sent == self.task["size_bytes"] else "interrupted"}
            current = self.manager.tasks.get(identity)
            if current:
                current["downloads"] = (current.get("downloads", []) + [event])[-20:]
                self.manager._persist(current)
            LOG.info("download %s %s bytes=%s", identity, event["status"], self.sent)
            self.manager.cleanup()


class BackupManager:
    def __init__(self, service, *, directory=None, lock_path=None, enabled=None,
                 exporter=None, clock=time.time, monotonic=time.monotonic, **limits):
        self.service = service
        self.directory = Path(directory or os.environ.get("VAULT_BACKUP_DIR", "/backups/web"))
        self.lock_path = lock_path or os.environ.get("VAULT_MAINTENANCE_LOCK", "/backups/.backup.lock")
        self.enabled = (bool(getattr(service.store, "dsn", None)) or exporter is not None) if enabled is None else enabled
        defaults = {"timeout_seconds": ("VAULT_BACKUP_TIMEOUT_SECONDS", 1800),
                    "retention_days": ("VAULT_BACKUP_RETENTION_DAYS", 7),
                    "quota_bytes": ("VAULT_BACKUP_QUOTA_BYTES", 5 * 1024**3),
                    "reserve_bytes": ("VAULT_BACKUP_RESERVE_BYTES", 1024**3),
                    "download_timeout_seconds": ("VAULT_DOWNLOAD_TIMEOUT_SECONDS", 1800),
                    "download_concurrency": ("VAULT_DOWNLOAD_CONCURRENCY", 2)}
        if set(limits) - set(defaults):
            raise ValueError("Unknown backup limit")
        self.limits = {key: int(limits.get(key, os.environ.get(env, value))) for key, (env, value) in defaults.items()}
        if any(value < (0 if key == "reserve_bytes" else 1) for key, value in self.limits.items()):
            raise ValueError("Invalid backup limits")
        self.exporter = exporter or self._export
        self.clock, self.monotonic = clock, monotonic
        self.mutex = threading.RLock()
        self.stop = threading.Event()
        self.tasks, self.pins = {}, {}
        self.worker = self.cleaner = None
        self.started = False

    def start(self):
        with self.mutex:
            if self.started or not self.enabled:
                return
            if self.directory.is_symlink():
                raise ValueError("Backup directory cannot be a symlink")
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            for path in self.directory.glob("*.json"):
                if not ID.fullmatch(path.stem) or not regular(path) or path.stat().st_size > 65536:
                    continue
                try:
                    task = json.loads(path.read_bytes())
                    if task["id"] != path.stem or task["status"] not in ("generating", "ready", "failed", "expired"):
                        continue
                    float(task["expires_timestamp"])
                    self.tasks[task["id"]] = task
                    if task["status"] == "generating":
                        self._fail(task, "interrupted")
                    elif task["status"] == "ready" and not regular(self._path(task["id"], ".dump")):
                        self._fail(task, "export")
                except (ValueError, KeyError, TypeError, OSError):
                    continue
            # Only remove files in our strict naming namespace; never other backups.
            for path in self.directory.iterdir():
                if path.suffix in (".part", ".tmp") and ID.fullmatch(path.stem) and regular(path):
                    path.unlink()
            self.started = True
            self.cleanup()
            self.cleaner = threading.Thread(target=self._maintain, daemon=True, name="backup-cleanup")
            self.cleaner.start()

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=5)
        if self.cleaner:
            self.cleaner.join(timeout=2)

    def _maintain(self):
        while not self.stop.wait(3600):
            try:
                self.cleanup()
            except OSError:
                LOG.warning("backup cleanup failed")

    def _path(self, identity, suffix):
        if not ID.fullmatch(identity):
            raise BackupError("备份任务不存在。", 404)
        return self.directory / (identity + suffix)

    def _persist(self, task):
        path = self._path(task["id"], ".json")
        temporary = self._path(task["id"], ".tmp")
        if temporary.exists() or temporary.is_symlink():
            raise ValueError("Unexpected backup metadata temporary file")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(task, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _public(self, task):
        return {key: value for key, value in task.items() if key not in ("expires_timestamp",)}

    def _available(self):
        if not self.enabled:
            raise BackupError("当前环境未配置数据库备份。", 503)
        if not self.started or self.stop.is_set():
            raise BackupError("备份服务尚未就绪。", 503)

    def list(self):
        self._available()
        with self.mutex:
            self.cleanup()
            return {"tasks": [self._public(task) for task in sorted(self.tasks.values(), key=lambda t: t["created_at"], reverse=True)],
                    "limits": dict(self.limits)}

    def get(self, identity):
        self._available()
        self._path(identity, ".json")
        with self.mutex:
            self.cleanup()
            if identity not in self.tasks:
                raise BackupError("备份任务不存在。", 404)
            return self._public(self.tasks[identity])

    def create(self):
        self._available()
        with self.mutex:
            for task in self.tasks.values():
                if task["status"] == "generating":
                    return self._public(task)
            self.cleanup()
            self._space()
            metadata = self.service.store.read_metadata()
            if not metadata:
                raise BackupError("密码库尚未初始化，无法生成可恢复备份。", 409)
            now = self.clock()
            identity = uuid.uuid4().hex
            expiry = now + self.limits["retention_days"] * 86400
            task = {"id": identity, "status": "generating", "phase": "export",
                    "created_at": stamp(now), "completed_at": None, "expires_at": stamp(expiry),
                    "expires_timestamp": expiry, "filename": f"vault-backup-{datetime.fromtimestamp(now, timezone.utc):%Y%m%dT%H%M%SZ}-{identity[:8]}.dump",
                    "size_bytes": None, "sha256": None, "error_code": None, "error": None,
                    "app_version": metadata["app_version"], "schema_version": metadata["schema_version"],
                    "crypto_format": metadata["crypto_format"], "vault_id": str(metadata["vault_id"]),
                    "backup_format": 2, "postgres_major": 17, "downloads": []}
            self.tasks[identity] = task
            self._persist(task)
            self.worker = threading.Thread(target=self._generate, args=(identity,), daemon=True, name="vault-backup")
            self.worker.start()
            return self._public(task)

    def _space(self):
        with self.mutex:
            used = sum(path.stat().st_size for path in self.directory.iterdir()
                       if regular(path) and ID.fullmatch(path.stem) and path.suffix in (".dump", ".part", ".json", ".tmp", ".sha256"))
            if used >= self.limits["quota_bytes"] or shutil.disk_usage(self.directory).free < self.limits["reserve_bytes"]:
                raise BackupError("space", 507)

    def _check_run(self, deadline):
        if self.stop.is_set():
            raise BackupError("interrupted")
        if self.monotonic() >= deadline:
            raise BackupError("timeout")
        self._space()

    def _tool(self, program):
        try:
            result = subprocess.run([program, "--version"], capture_output=True, timeout=5, check=True)
            if not re.search(rb"\(PostgreSQL\) 17(?:\.|\s)", result.stdout):
                raise BackupError("tools")
        except (OSError, subprocess.SubprocessError):
            raise BackupError("tools") from None

    def _process(self, args, deadline, output=None):
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=output or subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, shell=False)
        try:
            while process.poll() is None:
                self._check_run(deadline)
                self.stop.wait(0.1)
            self._check_run(deadline)
            if process.returncode:
                raise BackupError("export")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()

    def _export(self, path, deadline):
        self._tool("pg_dump")
        self._tool("pg_restore")
        with path.open("xb") as output:
            os.chmod(path, 0o600)
            self._process(["pg_dump", "--format=custom", "--no-owner", "--dbname", self.service.store.dsn], deadline, output)
            output.flush()
            os.fsync(output.fileno())
        # pg_restore parses the archive table of contents; full restore is a separate acceptance gate.
        self._process(["pg_restore", "--list", str(path)], deadline)

    def _generate(self, identity):
        task = self.tasks[identity]
        staging = self._path(identity, ".part")
        deadline = self.monotonic() + self.limits["timeout_seconds"]
        try:
            try:
                with maintenance_lock(self.lock_path):
                    self.exporter(staging, deadline)
                    if not regular(staging) or staging.stat().st_size < 5:
                        raise BackupError("export")
                    with staging.open("rb") as stream:
                        if stream.read(5) != b"PGDMP":
                            raise BackupError("export")
                        stream.seek(0)
                        digest = hashlib.sha256()
                        with self.mutex:
                            task["phase"] = "checksum"
                            self._persist(task)
                        while block := stream.read(1024 * 1024):
                            self._check_run(deadline)
                            digest.update(block)
                    with self.mutex:
                        self._check_run(deadline)
                        size = staging.stat().st_size
                        os.replace(staging, self._path(identity, ".dump"))
                        now = self.clock()
                        expiry = now + self.limits["retention_days"] * 86400
                        task.update(status="ready", phase="complete", completed_at=stamp(now),
                                    expires_timestamp=expiry, expires_at=stamp(expiry), size_bytes=size, sha256=digest.hexdigest())
                        self._persist(task)
                        LOG.info("backup %s ready bytes=%s", identity, size)
            except ValueError as exc:
                # Maintenance contention uses a fixed message, never expose exception content.
                raise BackupError("busy") from exc
        except Exception as exc:
            with self.mutex:
                self._fail(task, exc.code if isinstance(exc, BackupError) else "export")
        finally:
            if regular(staging):
                staging.unlink()

    def _fail(self, task, code):
        task.update(status="failed", phase="failed", completed_at=stamp(self.clock()),
                    error_code=code, error=ERRORS.get(code, ERRORS["export"]))
        self._persist(task)
        LOG.warning("backup %s failed code=%s", task["id"], code)

    def cleanup(self):
        if not self.enabled or not self.directory.exists():
            return
        with self.mutex:
            now = self.clock()
            for identity, task in list(self.tasks.items()):
                if task["status"] == "generating":
                    continue
                expired = task["expires_timestamp"] <= now
                if expired and task["status"] != "expired":
                    task.update(status="expired", phase="expired")
                    self._persist(task)
                if (expired or task["status"] == "failed") and not self.pins.get(identity):
                    for suffix in (".dump", ".part", ".sha256"):
                        path = self._path(identity, suffix)
                        if regular(path):
                            path.unlink()
                    if now > task["expires_timestamp"] + 86400:
                        self._path(identity, ".json").unlink(missing_ok=True)
                        del self.tasks[identity]
                        self.pins.pop(identity, None)

    def ready(self, identity):
        task = self.get(identity)
        if task["status"] == "expired":
            raise BackupError("备份已过期，请重新生成。", 410)
        if task["status"] != "ready":
            raise BackupError("备份尚未准备完成。", 409)
        return task

    def download(self, identity):
        with self.mutex:
            task = self.ready(identity)
            if sum(self.pins.values()) >= self.limits["download_concurrency"]:
                raise BackupError("下载并发已达上限，请稍后重试。", 429)
            path = self._path(identity, ".dump")
            if not regular(path):
                raise BackupError("备份文件不可用，请重新生成。", 410)
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            stream = os.fdopen(fd, "rb")
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != task["size_bytes"]:
                stream.close()
                raise BackupError("备份文件完整性检查失败。", 410)
            self.pins[identity] = self.pins.get(identity, 0) + 1
            return Download(self, task, stream)
