import json
from copy import deepcopy

import httpx
import pytest

from tests.support.records import entry
from tests.support.http import initialize
from tests.support.records import RECORDS, answer
from vault.search.engine import RetrievalEngine
from vault.search.vectors import VectorIndex


class FakeVectors:
    configured = True
    def __init__(self):
        self.seen = []
    def rank(self, query, descriptors, limit=12):
        self.seen.append(deepcopy(descriptors))
        return [{"id": row["id"], "score": 0.7 - index * 0.1} for index, row in enumerate(descriptors[:limit])]
    def clear(self):
        self.seen.clear()


def test_semantic_candidates_are_available_without_keyword_overlap():
    vectors = FakeVectors()
    def handler(request):
        candidates = json.loads(json.loads(request.content)["messages"][1]["content"])["initial_candidates"]
        assert len(candidates) == 3
        return answer(["github"])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = RetrievalEngine(client=client, vectors=vectors).search("协作维护源文件", RECORDS, "token")
    assert result["ids"] == ["github", "mail", "cloud"]
    assert result["weak_ids"] == ["mail", "cloud"]
    assert result["diagnostics"]["mode"] == "hybrid_ai"


def test_unrelated_query_returns_nearest_candidates_explicitly_marked_weak():
    with httpx.Client(transport=httpx.MockTransport(lambda r: answer([]))) as client:
        result = RetrievalEngine(client=client, vectors=FakeVectors()).search("苹果", RECORDS, "token")
    assert len(result["ids"]) == 3 and result["weak_ids"] == result["ids"]
    assert result["diagnostics"]["weak_count"] == 3


def test_explicit_exclusions_are_never_reintroduced_by_vectors():
    with httpx.Client(transport=httpx.MockTransport(lambda r: answer(["github"], excluded_ids=["mail"]))) as client:
        result = RetrievalEngine(client=client, vectors=FakeVectors()).search("找工作记录，不要邮箱", RECORDS, "token", 5)
    assert result["ids"] == ["github", "cloud"]
    assert result["weak_ids"] == ["cloud"]


def test_keyword_noise_is_not_silently_promoted_to_a_confirmed_match():
    rows = [dict(RECORDS[0], title="办公 OA", description="审批"), dict(RECORDS[1], title="办公 VPN", description="连接内网")]
    with httpx.Client(transport=httpx.MockTransport(lambda r: answer(["github"]))) as client:
        result = RetrievalEngine(client=client).search("办公 OA", rows, "token")
    assert result["ids"] == ["github"]


def test_embeddings_receive_no_secret_fields_and_runtime_logs_contain_no_query(capsys):
    vectors = FakeVectors()
    rows = [dict(RECORDS[0], password="PW_CANARY", username="ACCOUNT_CANARY", notes="NOTE_CANARY")]
    RetrievalEngine(vectors=vectors).search("QUERY_CANARY", rows, None)
    assert set(vectors.seen[0][0]) == {"id", "title", "service", "description", "tags"}
    output = str(capsys.readouterr())
    assert not any(x in output for x in ("PW_CANARY", "ACCOUNT_CANARY", "NOTE_CANARY", "QUERY_CANARY"))


def test_provider_failure_is_diagnosable_without_logging_provider_body():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"error":"PRIVATE_CANARY"}))) as client:
        result = RetrievalEngine(client=client, vectors=FakeVectors()).search("GitHub", RECORDS, "token")
    assert result["diagnostics"]["fallback_reason"] == "http_401"
    assert result["weak_ids"] == result["ids"]
    assert "PRIVATE_CANARY" not in json.dumps(result)


def test_vector_cache_updates_modified_records_and_removes_deleted_records():
    batches = []
    def embed(texts, query=False):
        if not query: batches.append(texts)
        return [[2, 0] if "VPN" in text else [0, 2] for text in texts]
    index = VectorIndex(embedder=embed)
    rows = [dict(RECORDS[0], title="VPN"), RECORDS[1]]
    assert index.rank("VPN", rows)[0]["id"] == "github"
    assert len(batches) == 2
    index.rank("VPN", rows)
    assert len(batches) == 2
    rows[0] = dict(rows[0], description="changed")
    index.rank("VPN", rows[:1])
    assert len(batches) == 3 and set(index.cache) == {"github"}
    index.clear()
    assert not index.cache
    index.rank("VPN", rows)
    index.rank("VPN", [])
    assert not index.cache


def test_long_descriptors_are_chunked_and_tail_can_match():
    def embed(texts, query=False):
        return [[1, 0] if "末尾线索" in text else [0, 1] for text in texts]
    index = VectorIndex(embedder=embed)
    rows = [dict(RECORDS[0], description="填充" * 800 + "末尾线索"), RECORDS[1]]
    assert index.rank("末尾线索", rows)[0]["score"] == 1
    assert index.cache["github"][1].shape[0] > 1


def test_log_history_is_encrypted_redacted_and_admin_only(api):
    client, service, store = api
    assert client.get("/api/query-logs").status_code == 423
    initialize(client)
    record = service.save(entry())
    result = {"ids":[record],"weak_ids":[record],"vector_scores":{record:0.42},"diagnostics":{"trace_id":"fixture","mode":"hybrid_ai"}}
    service.record_query("QUERY_CANARY " + entry()["username"] + entry()["password"], service.descriptors(), result)
    payload = store.query_history[0]["payload"]
    for value in ("QUERY_CANARY",entry()["username"],entry()["password"],entry()["title"]):
        assert value.encode() not in payload
    response = client.get("/api/query-logs")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    logged = response.json()["logs"][0]
    assert "[机密已隐藏]" in logged["query"] and entry()["password"] not in json.dumps(logged)
    assert logged["results"][0]["weak"]
    assert not {"password", "username", "notes"}.intersection(logged["results"][0])
    client.post("/api/lock",json={})
    assert client.get("/api/query-logs").status_code == 423


def test_log_ciphertext_is_bound_to_its_identity(api):
    client, service, store = api
    initialize(client)
    service.record_query("test", [], {"ids":[]})
    store.query_history[0]["id"] = "swapped"
    with pytest.raises(ValueError): service.query_logs()
