import json

import pytest

from vault.core.duplicates import compare_entries, normalize_address


def row(id, **changes):
    return {"id": id, "title": "客户管理系统", "service": "https://crm.example.com/login", "username": "admin", "password": "secret exact ", "description": "客户项目", "tags": [], "notes": "", "fields": {}, **changes}


def test_all_four_identity_fields_are_required_for_high_confidence_duplicate():
    group, = compare_entries([row("1"), row("2")])
    assert group["category"] == "duplicate"
    assert "系统名称、网址、账号和密码" in group["description"]


@pytest.mark.parametrize("changes", [
    {"username": "someone-else"},
    {"title": "另一个系统", "service": "https://other.example.com", "password": "another secret"},
    {"title": "另一个系统", "service": "https://other.example.com"},
])
def test_account_or_password_match_alone_is_not_a_duplicate(changes):
    assert compare_entries([row("1"), row("2", **changes)]) == []


def test_password_reuse_is_opt_in_and_clearly_separate():
    entries = [row("1"), row("2", username="someone-else")]
    assert compare_entries(entries) == []
    group, = compare_entries(entries, include_reuse=True)
    assert group["category"] == "password_reuse"
    assert "非重复记录" in group["kind"]


@pytest.mark.parametrize("changes", [
    {"title": "CRM 别名"},
    {"service": "https://crm.example.com/test"},
    {"service": "https://crm.example.com/login?tenant=b"},
    {"service": "https://crm.example.com/login#tenant=b"},
])
def test_partial_context_match_is_only_a_review_candidate(changes):
    group, = compare_entries([row("1"), row("2", **changes)])
    assert group["category"] == "possible_duplicate"


def test_missing_url_is_not_a_high_confidence_duplicate():
    group, = compare_entries([row("1", service=""), row("2", service="")])
    assert group["category"] == "possible_duplicate"


def test_versions_require_matching_system_url_and_account():
    group, = compare_entries([row("1"), row("2", password="secret exact")])
    assert group["category"] == "version"
    assert compare_entries([row("1"), row("2", title="别的系统", password="new")]) == []


@pytest.mark.parametrize("field", ["username", "password"])
def test_empty_credentials_are_not_evidence_of_duplication(field):
    assert compare_entries([row("1", **{field: ""}), row("2", **{field: ""})]) == []


def test_metadata_differences_are_named_without_revealing_values():
    group, = compare_entries([row("1"), row("2", description="DESCRIPTION_CANARY", notes="PRIVATE_CANARY", fields={"key": "SECRET_CANARY"}, tags=["TAG_CANARY"])])
    assert group["category"] == "duplicate"
    assert set(group["differences"]) == {"检索描述", "标签", "备注", "自定义字段"}
    output = json.dumps(group)
    assert all(secret not in output for secret in ("CANARY", "secret exact", "admin"))


def test_benign_url_spelling_differences_compare_equal():
    assert normalize_address("HTTPS://CRM.EXAMPLE.COM:443/login/") == normalize_address("crm.example.com/login")
    group, = compare_entries([row("1"), row("2", service="HTTPS://CRM.EXAMPLE.COM:443/login/")])
    assert group["category"] == "duplicate"


def test_account_case_and_whitespace_are_not_silently_normalized():
    assert compare_entries([row("1"), row("2", username="Admin")]) == []
    assert compare_entries([row("1"), row("2", username="admin ")]) == []
