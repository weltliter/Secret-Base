"""Every artifact request authenticates; an admitted stream has a bounded grant."""
import json

import anyio
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from ...backup.manager import BackupError


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
