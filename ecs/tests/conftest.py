"""Shared fixtures for synthetic vaults and in-memory HTTP checks."""
import pytest
from fastapi.testclient import TestClient
from vault.core import crypto
from vault.core.service import VaultService
import vault.web.app as app_module
from tests.support.store import MemoryStore


@pytest.fixture(autouse=True)
def fast_test_kdf(monkeypatch):
    # Keep the real algorithm, but use tiny parameters for tests only.
    monkeypatch.setattr(crypto.pwhash.argon2id, "OPSLIMIT_MODERATE", crypto.pwhash.argon2id.OPSLIMIT_MIN)
    monkeypatch.setattr(crypto.pwhash.argon2id, "MEMLIMIT_MODERATE", crypto.pwhash.argon2id.MEMLIMIT_MIN)


@pytest.fixture
def api(tmp_path):
    store = MemoryStore()
    service = VaultService(store, tmp_path / "vault.json")
    app = app_module.create_app(service, start_bot=False)
    with TestClient(app, base_url="http://127.0.0.1:18080") as client:
        token = client.get("/api/csrf").json()["token"]
        client.headers["X-Vault-CSRF"] = token
        yield client, service, store
