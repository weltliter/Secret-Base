"""Every artifact request authenticates; an admitted stream has a bounded grant."""
import json
import logging
import os
import uuid
from contextlib import contextmanager

import anyio
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from ...backup.manager import BackupError
from ...backup.restore import replace_data
from ...core.maintenance import maintenance_lock


class GrantedDownloadResponse(StreamingResponse):
    def __init__(self, download):
        self.download = download
        super().__init__(download.chunks(), media_type="application/octet-stream", headers={
            "Content-Disposition": f'attachment; filename="{download.task["filename"]}"',
            "Content-Length": str(download.task["size_bytes"]), "Cache-Control": "no-store",
            "Accept-Ranges": "none",
        }, background=BackgroundTask(download.close))

    async def __call__(self, scope, receive, send):
        try:
            with anyio.move_on_after(self.download.manager.limits["download_timeout_seconds"]):
                await super().__call__(scope, receive, send)
        finally:
            self.download.close()


def register(app, service, admins, manager):
    def invoke(request, action):
        # No service mutex around export/stream: logout and WeChat stay responsive.
        admins.require(request)
        try:
            return action()
        except BackupError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @app.post("/api/backups", status_code=202)
    def create(request: Request):
        return invoke(request, manager.create)

    @app.get("/api/backups")
    def listing(request: Request):
        return invoke(request, manager.list)

    @app.post("/api/backups/restore")
    async def restore(request: Request):
        admins.require(request)
        if request.headers.get("x-vault-confirm-replace") != "replace-all":
            raise HTTPException(400, "请确认删除全部现有数据并使用上传的备份恢复。")
        if request.headers.get("content-type", "").split(";")[0] != "application/octet-stream":
            raise HTTPException(415, "请上传 .dump 数据库备份文件。")
        identity = uuid.uuid4().hex
        upload = manager._path(identity, ".part")
        extracted = manager._path(identity, ".sql")
        deadline = manager.monotonic() + manager.limits["timeout_seconds"]

        @contextmanager
        def replacement():
            with service.mutex:
                # Logout/expiry during upload or validation cancels replacement.
                admins.require(request)
                service.query_unlock.path.unlink(missing_ok=True)
                service.query_unlock.ready = False
                yield
                # The database commit has completed while holding this mutex.
                service.lock()
                service.failed_unlocks = 0
                service.next_unlock = 0
                admins.revoke_all()
                app.state.bot.clear()

        def apply():
            manager._tool("pg_restore")
            with upload.open("rb") as stream:
                if stream.read(5) != b"PGDMP":
                    raise BackupError("invalid_restore")
            with extracted.open("xb") as output:
                os.chmod(extracted, 0o600)
                manager._process(["pg_restore", "--data-only", "--no-owner", "--no-privileges",
                                  "--schema=public", "--file=-", str(upload)], deadline, output)
            replace_data(service.store, extracted, lambda: manager._check_run(deadline), replacement)

        try:
            manager._available()
            # Admit exactly one upload/backup/restore across processes.
            with maintenance_lock(manager.lock_path):
                size = 0
                manager._check_run(deadline)
                with anyio.fail_after(manager.limits["timeout_seconds"]):
                    async with await anyio.open_file(upload, "xb") as output:
                        os.chmod(upload, 0o600)
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > manager.limits["quota_bytes"]:
                                raise BackupError("上传文件超过备份目录配额。", 413)
                            manager._check_run(deadline)
                            await output.write(chunk)
                    admins.require(request)
                    await anyio.to_thread.run_sync(apply)
            response = Response(json.dumps({"ok": True, "message": "全量恢复成功，请使用备份对应的主密码或恢复密钥重新登录。"}, ensure_ascii=False), media_type="application/json")
            admins.clear_cookie(response)
            return response
        except BackupError as exc:
            code = {"export": "invalid_restore", "timeout": "restore_timeout"}.get(exc.code, exc.code)
            raise HTTPException(exc.status, str(BackupError(code))) from None
        except TimeoutError:
            raise HTTPException(408, str(BackupError("restore_timeout"))) from None
        except HTTPException:
            raise
        except ValueError as exc:
            code = "busy" if "另一个备份" in str(exc) else "invalid_restore"
            raise HTTPException(409 if code == "busy" else 400, str(BackupError(code))) from None
        except Exception as exc:
            logging.getLogger("vault.backup").warning("restore failed (%s)", type(exc).__name__)
            raise HTTPException(400, str(BackupError("restore"))) from None
        finally:
            upload.unlink(missing_ok=True)
            extracted.unlink(missing_ok=True)

    @app.get("/api/backups/{identity}")
    def detail(identity: str, request: Request):
        return invoke(request, lambda: manager.get(identity))

    @app.get("/api/backups/{identity}/download")
    def download(identity: str, request: Request):
        admins.require(request)
        if request.headers.get("range"):
            raise HTTPException(416, "此版本不支持断点续传，请重新下载完整文件。")
        return GrantedDownloadResponse(invoke(request, lambda: manager.download(identity)))

    @app.get("/api/backups/{identity}/checksum")
    def checksum(identity: str, request: Request):
        task = invoke(request, lambda: manager.ready(identity))
        return Response(f'{task["sha256"]}  {task["filename"]}\n', media_type="text/plain", headers={
            "Content-Disposition": f'attachment; filename="{task["filename"]}.sha256"', "Cache-Control": "no-store"})

    @app.get("/api/backups/{identity}/manifest")
    def manifest(identity: str, request: Request):
        task = invoke(request, lambda: manager.ready(identity))
        value = {key: task[key] for key in ("filename", "size_bytes", "sha256", "created_at", "completed_at",
                                           "app_version", "schema_version", "crypto_format", "vault_id", "backup_format", "postgres_major")}
        value["recovery"] = "兼容源码 + 此数据库文件 + 原主密码或恢复密钥；ECS SSH 私钥只用于连接新服务器。"
        return Response(json.dumps(value, ensure_ascii=False, indent=2), media_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="{task["filename"]}.json"', "Cache-Control": "no-store"})
