"""Application assembly and runtime lifecycle for the encrypted vault."""
import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ..channels.bot import BotController
from ..core.service import VaultService
from ..core.store import Store
from ..backup.manager import BackupManager
from ..search.diagnostics import configure as configure_query_logging
from .routes import records, session, settings, backups
from .security import install_security, management_guard
from .sessions import AdminSessions


def create_app(service=None, start_bot=True, backup_manager=None):
    configure_query_logging()
    # HTTP client INFO messages include complete URLs, including QR login values.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    service = service or VaultService(Store(), Path(os.environ.get("VAULT_DATA_DIR", "/data")) / "vault.json",
                                      query_key_path=os.environ.get("VAULT_QUERY_KEY_FILE"))
    bot = BotController(service)
    admins = AdminSessions()
    csrf_token = secrets.token_urlsafe(32)
    qr_sessions = {}
    backup_manager = backup_manager or BackupManager(service)

    @asynccontextmanager
    async def lifespan(app):
        service.store.initialize()
        backup_manager.start()
        if not service.unlocked:
            try:
                service.resume_queries()
            except Exception as exc:
                logging.getLogger("vault.startup").warning("query startup requires admin login (%s)", type(exc).__name__)
        thread = None
        if start_bot:
            thread = threading.Thread(target=bot.run, daemon=True, name="wechat-vault")
            thread.start()
            def maintain():
                while not bot.stop_event.is_set():
                    try:
                        service.store.maintenance()
                    except Exception as exc:
                        logging.getLogger("vault.maintenance").error("maintenance failed (%s)", type(exc).__name__)
                    bot.stop_event.wait(3600)
            threading.Thread(target=maintain, daemon=True, name="vault-maintenance").start()
        try:
            yield
        finally:
            bot.stop_event.set()
            backup_manager.close()
            service.lock()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service, app.state.bot = service, bot
    app.state.admins = admins
    app.state.backups = backup_manager

    management = management_guard(service, admins)
    install_security(app, admins, csrf_token)
    session.register(app, service, bot, admins, csrf_token, qr_sessions)
    records.register(app, service, management)
    settings.register(app, service, bot, admins, management, qr_sessions)
    backups.register(app, service, admins, backup_manager)

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (Path(__file__).parent / "templates" / "console.html").read_text(encoding="utf-8")

    @app.get("/health")
    def health():
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    return app


app = create_app()
