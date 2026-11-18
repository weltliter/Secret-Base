"""Cross-component security properties using real encryption and a byte store.

These tests exercise the service's storage/model boundary without a PostgreSQL
server. Database transactions and deployment networking need integration checks.
"""
from copy import deepcopy
import json

import pytest

from vault.core.service import VaultService
from tests.support.store import ByteStore
from tests.support.records import entry


@pytest.fixture
def opened_vault(tmp_path):
    store = ByteStore()
    service = VaultService(store, tmp_path / "vault.json")
    recovery = service.setup("correct horse battery staple")
    return service, store, recovery


def test_business_values_and_service_credentials_reach_storage_only_as_ciphertext(opened_vault):
    service, store, recovery = opened_vault
    original = entry()
    record_id = service.save(deepcopy(original))
    service.update_settings({"deepseek_key": "sk-DEEPSEEK-SENTINEL-5f21", "wechat_token": "WX-SENTINEL-3b14"})

    persisted = store.rows[record_id]["payload"] + store.settings_blob + service.envelope.read_bytes()
    for value in [*original.values(), *original["fields"].values(), *original["tags"], "sk-DEEPSEEK-SENTINEL-5f21", "WX-SENTINEL-3b14", recovery]:
        assert str(value).encode() not in persisted
    for field, value in original.items():
        assert service.entry(record_id)[field] == value


def test_model_descriptors_exclude_all_secret_fields(opened_vault):
    service, _, _ = opened_vault
    original = entry()
    service.save(deepcopy(original))
    descriptors = service.descriptors()
    assert set(descriptors[0]) == {"id", "title", "service", "description", "tags"}
    model_context = json.dumps(descriptors, ensure_ascii=False)
    for secret in (original["username"], original["password"], original["notes"], original["fields"]["API Key"]):
        assert secret not in model_context


def test_ciphertext_cannot_be_swapped_between_records_or_tampered(opened_vault):
    service, store, _ = opened_vault
    first = service.save(entry(title="first"))
    second = service.save(entry(title="second", password="another password"))
    original_blob = store.rows[first]["payload"]
    store.rows[first]["payload"] = store.rows[second]["payload"]
    with pytest.raises(ValueError):
        service.entry(first)
    corrupted = bytearray(original_blob)
    corrupted[-1] ^= 1
    store.rows[first]["payload"] = bytes(corrupted)
    with pytest.raises(ValueError):
        service.entry(first)


def test_restart_requires_unlock_and_recovery_key_restores_exact_values(opened_vault):
    service, store, recovery = opened_vault
    record_id = service.save(entry())
    restarted = VaultService(store, service.envelope.path)
    with pytest.raises(ValueError, match="锁定"):
        restarted.entries()
    restarted.unlock(recovery)
    assert restarted.entry(record_id)["password"] == entry()["password"]
    restarted.lock()
    assert not restarted.unlocked
    with pytest.raises(ValueError, match="锁定"):
        restarted.settings()


def test_incorrect_master_password_does_not_unlock(opened_vault):
    service, store, _ = opened_vault
    restarted = VaultService(store, service.envelope.path)
    with pytest.raises(ValueError):
        restarted.unlock("this password is incorrect")
    assert not restarted.unlocked


def test_duplicate_labels_preserve_service_and_exact_password_distinctions(opened_vault):
    service, _, _ = opened_vault
    first = service.save(entry(password="Exact Password ", notes="first note"))
    duplicate = service.save(entry(password="Exact Password ", notes="different note"))
    other_version = service.save(entry(password="Exact Password"))
    another_service = service.save(entry(service="another.example.com", password="Exact Password "))
    groups = service.duplicate_groups(include_reuse=True)

    duplicated = [group for group in groups if group["kind"].startswith("凭据重复")]
    assert len(duplicated) == 1
    assert set(duplicated[0]["ids"]) == {first, duplicate}
    assert "备注" in duplicated[0]["description"]
    version_group = next(group for group in groups if "不同密码" in group["kind"])
    assert set(version_group["ids"]) == {first, duplicate, other_version}
    reused = next(group for group in groups if group["category"] == "password_reuse")
    assert set(reused["ids"]) == {first, duplicate, another_service}
    assert "Exact Password" not in json.dumps(groups)


def test_same_credentials_without_matching_system_context_are_not_duplicates(opened_vault):
    service, _, _ = opened_vault
    service.save(entry(service="", title="用途不明一"))
    service.save(entry(service="", title="用途不明二"))
    groups = service.duplicate_groups()
    assert groups == []


def test_unchanged_password_is_preserved_on_edit(opened_vault):
    service, _, _ = opened_vault
    record_id = service.save(entry())
    changed = entry(title="新标题")
    changed.pop("password")
    service.save(changed, entry_id=record_id, version=1)
    assert service.entry(record_id)["password"] == entry()["password"]
    assert service.entry(record_id)["title"] == "新标题"


def test_missing_envelope_cannot_reinitialize_existing_database(opened_vault, tmp_path):
    service, store, _ = opened_vault
    service.save(entry())
    store.metadata = None
    broken = VaultService(store, tmp_path / "missing.json")
    with pytest.raises(ValueError, match="数据库已有数据"):
        broken.setup("a completely different password")
    assert not broken.envelope.exists


def test_known_secret_pasted_in_search_field_is_redacted(opened_vault):
    service, _, _ = opened_vault
    service.save(entry(description="用途里的误贴密码 PASTE-SECRET-XYZ", password="PASTE-SECRET-XYZ"))
    assert "PASTE-SECRET-XYZ" not in json.dumps(service.descriptors(), ensure_ascii=False)
