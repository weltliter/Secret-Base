"""Exercise the deployed app against an isolated PostgreSQL schema.

Run inside the app container. Never initializes or edits the user's vault.
"""
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from fastapi.testclient import TestClient

from vault.web.app import create_app
from vault.channels.bot import BotController
from vault.core.service import VaultService
from vault.core.store import Store


base_dsn = os.environ["DATABASE_URL"]
schema = "vault_check_" + uuid.uuid4().hex
assert schema.startswith("vault_check_") and schema.replace("_", "").isalnum()
with psycopg.connect(base_dsn) as conn:
    conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

try:
    with tempfile.TemporaryDirectory(prefix="vault-check-") as temp:
        store = Store(make_conninfo(base_dsn, options="-c search_path=" + schema))
        machine_key = Path(temp) / "machine.key"
        machine_key.write_bytes(os.urandom(32))
        service = VaultService(store, Path(temp) / "vault.json", machine_key)
        app = create_app(service, start_bot=False)
        with TestClient(app, base_url="http://127.0.0.1:18080") as client:
            token = client.get("/api/csrf").json()["token"]
            headers = {"X-Vault-CSRF": token, "Origin": "http://127.0.0.1:18080"}
            started = time.monotonic()
            response = client.post("/api/setup", headers=headers, json={"password": "integration-only master password"})
            assert response.status_code == 200, response.status_code
            recovery = response.json()["recovery_key"]
            kdf_seconds = round(time.monotonic() - started, 2)
            original = {"title": "测试云平台", "service": "integration.example.invalid", "username": "fixture-account-9483",
                        "password": "fixture-secret-3682", "description": "用于客户演示的云平台", "tags": ["客户演示"],
                        "notes": "fixture-private-note", "fields": {"密保": "fixture-other-secret"}}
            ids = []
            for note in ("第一条", "备注不同"):
                result = client.post("/api/entries", headers=headers, json={**original, "notes": note})
                assert result.status_code == 200
                ids.append(result.json()["id"])
            assert len(client.get("/api/duplicates").json()["groups"]) == 1
            assert client.get("/api/duplicates").json()["groups"][0]["category"] == "duplicate"
            assert client.get("/api/tags").json()["tags"] == ["客户演示"]
            assert client.get(f"/api/entries/{ids[0]}/reveal").json()["password"] == original["password"]
            assert "password" not in client.get("/api/entries").json()["entries"][0]
            for row in store.list():
                for secret in (original["password"], original["username"], original["description"]):
                    assert secret.encode() not in bytes(row["payload"])
            service.update_settings({"wechat_owner": "fixture-owner", "wechat_token": "fixture-token"})
            bot = BotController(service)
            assert bot.handle("someone-else", "查重") is None
            assert "仅提供查询" in bot.handle("fixture-owner", "删除第1条")
            assert "凭据重复" in bot.handle("fixture-owner", "查重")
            assert len(service.entries()) == 2
            response = client.put(f"/api/entries/{ids[0]}", headers=headers, json={**original, "title": "已更新", "version": 1})
            assert response.status_code == 200
            # A batch deletion must roll back every row if any version is stale.
            try:
                store.trash({ids[1]: 1, ids[0]: 1})
                raise AssertionError("stale batch unexpectedly succeeded")
            except ValueError:
                pass
            assert not store.list(True)
            assert client.post(f"/api/entries/{ids[0]}/trash", headers=headers, json={"version": 2}).status_code == 200
            assert len(store.list(True)) == 1
            assert client.post(f"/api/entries/{ids[0]}/restore", headers=headers, json={}).status_code == 200
            query_epoch = service.epoch
            assert client.post("/api/lock", headers=headers, json={}).status_code == 200
            assert client.get("/api/entries").status_code == 423
            assert service.unlocked and service.epoch == query_epoch
            assert client.get("/api/status").json()["query_ready"]
            assert original["password"] in bot.handle("fixture-owner", "测试云平台")
            assert client.get("/api/query-logs").status_code == 423
            assert client.post("/api/unlock", headers=headers, json={"password": recovery}).status_code == 200
            assert service.epoch == query_epoch
            assert service.entry(ids[0])["password"] == original["password"]
            history=client.get("/api/query-logs").json()["logs"]
            assert history and history[0]["query"] == "测试云平台"
            assert original["password"] not in json.dumps(history,ensure_ascii=False)
            for row in store.query_logs():
                assert "测试云平台".encode() not in bytes(row["payload"])
            assert client.get("/api/status").headers["cache-control"] == "no-store"
            assert client.post("/api/entries", json=original).status_code == 403
            old_cookie = client.cookies.get("vault_admin")
        restarted_service = VaultService(store, Path(temp) / "vault.json", machine_key)
        restarted = create_app(restarted_service, start_bot=False)
        with TestClient(restarted, base_url="http://127.0.0.1:18080") as client:
            client.cookies.set("vault_admin", old_cookie)
            status = client.get("/api/status").json()
            assert status["query_ready"] and status["auto_restore_ready"] and not status["unlocked"]
            assert client.get("/api/entries").status_code == 423
            assert original["password"] in restarted.state.bot.handle("fixture-owner", "测试云平台")
            assert restarted.state.bot.handle("someone-else", "测试云平台") is None
            persisted = (Path(temp) / "query-unlock.json").read_bytes()
            assert restarted_service.key not in persisted and original["password"].encode() not in persisted
        print(json.dumps({"integration": "passed", "database": "real PostgreSQL isolated schema", "production_kdf_seconds": kdf_seconds,
                          "checks": ["encrypted persistence", "CRUD", "duplicates", "wechat read-only", "atomic version conflict", "trash and restore", "admin logout preserves queries", "restart resumes queries without admin login", "CSRF"]}))
finally:
    with psycopg.connect(base_dsn) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
