"""Shared fixtures for synthetic vaults and in-memory HTTP checks."""
import pytest
from vault.core import crypto


@pytest.fixture(autouse=True)
def fast_test_kdf(monkeypatch):
    # Keep the real algorithm, but use tiny parameters for tests only.
    monkeypatch.setattr(crypto.pwhash.argon2id, "OPSLIMIT_MODERATE", crypto.pwhash.argon2id.OPSLIMIT_MIN)
    monkeypatch.setattr(crypto.pwhash.argon2id, "MEMLIMIT_MODERATE", crypto.pwhash.argon2id.MEMLIMIT_MIN)
