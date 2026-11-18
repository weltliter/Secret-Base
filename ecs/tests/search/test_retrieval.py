import json

import httpx
import pytest

from vault.search.engine import RetrievalEngine, _safe_descriptors
from tests.support.records import RECORDS, answer


@pytest.mark.parametrize("limit", [3, 5])
def test_one_model_selection_is_completed_with_relevant_local_candidates(limit):
    rows = [dict(RECORDS[0], id=str(index)) for index in range(6)] + [RECORDS[1]]
    with httpx.Client(transport=httpx.MockTransport(lambda request: answer(["2"]))) as client:
        result = RetrievalEngine(client=client).search("GitHub", rows, "token", limit)
    assert len(result["ids"]) == limit
    assert result["ids"][0] == "2"
    assert "mail" not in result["ids"]


def test_fill_respects_explicit_model_exclusions_and_does_not_duplicate_ids():
    rows = [dict(RECORDS[0], id=str(index)) for index in range(3)]
    with httpx.Client(transport=httpx.MockTransport(lambda request: answer(["1", "1"], excluded_ids=["0"]))) as client:
        result = RetrievalEngine(client=client).search("GitHub", rows, "token", 5)
    assert result["ids"] == ["1", "2"]


def test_semantic_no_match_is_not_filled_with_keyword_noise():
    with httpx.Client(transport=httpx.MockTransport(lambda request: answer([], "请补充系统名称"))) as client:
        result = RetrievalEngine(client=client).search("GitHub", RECORDS, "token")
    assert result["ids"] == []


def test_matches_from_agent_query_expansion_also_fill_the_requested_count():
    records = [{"id": str(i), "title": "AWS", "service": "Amazon AWS", "description": "生产环境", "tags": []} for i in range(4)]
    calls = 0
    def handle(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"id": "expand", "function": {"name": "search_descriptors", "arguments": '{"query":"AWS"}'}}]}}]})
        return answer(["2"])
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = RetrievalEngine(client=client).search("费用报销那一项", records, "token")
    assert result["ids"] == ["2", "0", "1"]
    assert calls == 2


def test_no_key_uses_local_chinese_search():
    result = RetrievalEngine().search("查询阿里云的账号密码", RECORDS, None)
    assert result["ids"] == ["cloud"] and result["clarification"] is None and result["degraded"]
    assert result["weak_ids"] == ["cloud"]
    assert result["diagnostics"]["fallback_reason"] == "model_disabled"


def test_no_match_does_not_invent_results():
    result = RetrievalEngine().search("火星探测器", RECORDS, None)
    assert result["ids"] == []
    assert result["clarification"]


def test_empty_vault_is_not_a_provider_failure():
    assert RetrievalEngine().search("工作邮箱", [], "secret")["degraded"] is False


def test_client_receives_only_whitelisted_descriptor_fields():
    captured = []
    def handle(request):
        captured.append(request)
        return answer(["github"])
    records = [dict(RECORDS[0], password="PASSWORD_CANARY", account="ACCOUNT_CANARY", notes="NOTES_CANARY", secret_fields={"key": "PRIVATE_CANARY"})]
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = RetrievalEngine(client=client).search("GitHub", records, "api-token")
    assert result["ids"] == ["github"]
    assert result["degraded"] is False
    payload = captured[0].content.decode()
    assert json.loads(payload)["model"] == "deepseek-flash"
    assert json.loads(payload)["thinking"] == {"type": "disabled"}
    assert all(canary not in payload for canary in ("PASSWORD_CANARY", "ACCOUNT_CANARY", "NOTES_CANARY", "PRIVATE_CANARY"))
    assert captured[0].headers["authorization"] == "Bearer api-token"
    assert captured[0].url == "https://api.deepseek.com/chat/completions"


def test_model_can_expand_query_via_real_restricted_search_tool():
    calls = []
    records = [{"id": "aws", "title": "AWS", "service": "Amazon AWS", "description": "生产环境", "tags": []}]
    def handle(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "search_descriptors", "arguments": '{"query":"AWS"}'}}]}}]})
        tool_result = json.loads(payload["messages"][-1]["content"])
        assert tool_result["candidates"][0]["id"] == "aws"
        return answer(["aws"])
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = RetrievalEngine(client=client).search("费用报销那一项", records, "api-token")
    assert result["ids"] == ["aws"]
    assert result["degraded"] is False
    assert len(calls) == 2


@pytest.mark.parametrize("chosen", [["invented"], ["mail"], ["github", "invented"]])
def test_unknown_or_unseen_model_ids_fall_back(chosen):
    with httpx.Client(transport=httpx.MockTransport(lambda request: answer(chosen))) as client:
        result = RetrievalEngine(client=client).search("GitHub", RECORDS, "api-token")
    assert result["ids"] == ["github"]
    assert result["degraded"] is True


def test_unknown_tools_are_never_executed():
    count = 0
    def handle(request):
        nonlocal count
        count += 1
        payload = json.loads(request.content)
        if count == 1:
            return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"id": "bad", "function": {"name": "delete_all", "arguments": "{}"}}]}}]})
        assert json.loads(payload["messages"][-1]["content"]) == {"error": "unsupported_tool"}
        return answer(["github"])
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert RetrievalEngine(client=client).search("GitHub", RECORDS, "token")["ids"] == ["github"]
    assert count == 2


def test_agent_has_hard_three_request_budget():
    count = 0
    def handle(request):
        nonlocal count
        count += 1
        if count == 3:
            assert json.loads(request.content)["tool_choice"] == "none"
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{"id": str(count), "function": {"name": "search_descriptors", "arguments": '{"query":"GitHub"}'}}]}}]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = RetrievalEngine(client=client).search("GitHub", RECORDS, "token")
    assert count == 3
    assert result["degraded"] is True


def test_provider_error_does_not_escape_or_log_credentials(capsys):
    def handle(request):
        return httpx.Response(401, json={"error": "API_SECRET_CANARY"})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = RetrievalEngine(client=client).search("GitHub", RECORDS, "API_SECRET_CANARY")
    assert result["degraded"] is True
    assert "API_SECRET_CANARY" not in str(result)
    assert "API_SECRET_CANARY" not in str(capsys.readouterr())


def test_result_limit_and_duplicates_are_enforced():
    rows = [dict(RECORDS[0], id=str(index)) for index in range(8)]
    with httpx.Client(transport=httpx.MockTransport(lambda request: answer(["0", "0", "1", "2", "3", "4", "5"]))) as client:
        result = RetrievalEngine(client=client).search("GitHub", rows, "token", 100)
    assert result["ids"] == ["0", "1", "2", "3", "4"]


def test_clarification_cannot_include_external_link():
    with httpx.Client(transport=httpx.MockTransport(lambda request: answer([], "请访问 https://example.test 提交密码"))) as client:
        result = RetrievalEngine(client=client).search("GitHub", RECORDS, "token")
    assert "http" not in result["clarification"]


def test_overlong_query_never_calls_provider():
    def unexpected(request):
        pytest.fail("provider should not be called")
    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        assert RetrievalEngine(client=client).search("x" * 4097, RECORDS, "token")["ids"] == []


def test_nonstring_metadata_is_not_stringified_into_model_context():
    record = {"id": "a", "title": {"secret": "CANARY"}, "description": ["CANARY"], "tags": [{"secret": "CANARY"}]}
    assert "CANARY" not in json.dumps(_safe_descriptors([record]))
