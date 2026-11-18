"""Record lifecycle, tag catalogue and query history endpoints."""
from uuid import UUID

from fastapi import Request

from ..schemas import EntryInput, VersionInput


def register(app, service, management):
    @app.get("/api/entries")
    def entries(request: Request, trash: bool = False):
        with management(request):
            values = service.entries(trash)
            for value in values:
                value.pop("password", None)
            return {"entries": values}

    @app.get("/api/entries/{entry_id}/reveal")
    def reveal(request: Request, entry_id: UUID):
        with management(request):
            return {"password": service.entry(str(entry_id))["password"]}

    @app.post("/api/entries")
    def add(request: Request, value: EntryInput):
        with management(request):
            data = value.model_dump(exclude={"version"})
            return {"id": service.save(data)}

    @app.put("/api/entries/{entry_id}")
    def edit(request: Request, entry_id: UUID, value: EntryInput):
        with management(request):
            if value.version is None:
                raise ValueError("缺少记录版本，请刷新后重试")
            return {"id": service.save(value.model_dump(exclude={"version"}), str(entry_id), value.version)}

    @app.post("/api/entries/{entry_id}/trash")
    def trash(request: Request, entry_id: UUID, value: VersionInput):
        with management(request):
            service.trash({str(entry_id): value.version})
            return {"ok": True}

    @app.post("/api/entries/{entry_id}/restore")
    def restore(request: Request, entry_id: UUID):
        with management(request):
            service.restore(str(entry_id))
            return {"ok": True}

    @app.get("/api/duplicates")
    def duplicates(request: Request, include_reuse: bool = False):
        with management(request):
            return {"groups": service.duplicate_groups(include_reuse=include_reuse)}

    @app.get("/api/query-logs")
    def query_logs(request: Request, limit: int = 50):
        with management(request):
            return {"logs": service.query_logs(limit)}

    @app.get("/api/tags")
    def tags(request: Request):
        with management(request):
            return {"tags": service.tags()}

