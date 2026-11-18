"""Owner authorization and a strictly read-only chat surface, without live APIs."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

import vault.channels.bot as bot_module
from vault.channels.bot import BotController
from vault.core.service import VaultService
from vault.channels.wechat import WeChatError
from tests.support.store import ByteStore
from tests.support.records import entry


class CommandStore(ByteStore):
    def __init__(self):
        super().__init__()
        self.deleted_batches = []
        self.processed = set()

    def trash(self, expected):
        for record_id, version in expected.items():
            row = self.get(record_id)
            if row["deleted_at"] is not None or row["version"] != version:
                raise ValueError("记录已经改变，删除未执行，请重新查询")
        self.deleted_batches.append(dict(expected))
        for record_id in expected:
            row = self.rows[record_id]
            row["deleted_at"] = datetime.now(timezone.utc)
            row["version"] += 1

    def restore(self, record_id):
        row = self.get(record_id)
        if row["deleted_at"] is None:
            raise ValueError("记录不在回收站")
        row["deleted_at"] = None
        row["version"] += 1

    def mark_message(self, digest):
        if digest in self.processed:
            return False
        self.processed.add(digest)
        return True

    def maintenance(self):
        pass


class FakeEngine:
    def __init__(self):
        self.calls = []
        self.callback = None
        self.no_match = False

    def search(self, query, descriptors, api_key, limit):
        self.calls.append((query, deepcopy(descriptors), api_key, limit))
        if self.callback:
            self.callback()
        return {"ids": [] if self.no_match else [d["id"] for d in descriptors[:limit]],
                "clarification": "请补充项目名称。", "degraded": api_key is None}


@pytest.fixture
def fixture(tmp_path):
    store = CommandStore()
    service = VaultService(store, tmp_path / "vault.json")
    service.setup("correct horse battery staple")
    service.update_settings({"wechat_owner": "owner-1", "wechat_token": "token-1", "wechat_bot_id": "bot-1", "deepseek_api_key": "provider-key"})
    ids = [service.save(entry(title=f"项目 {i}", username=f"account-{i}", password=f"secret-{i}")) for i in range(5)]
    engine = FakeEngine()
    controller = BotController(service, engine)
    return controller, service, store, engine, ids




def test_three_or_five_exact_local_credentials_and_descriptor_only_model(fixture):
    bot, service, _, engine, ids = fixture
    answer = bot.handle("owner-1", "找项目")
    assert answer.count("\n密码：") == 3
    assert "账号：account-0\n密码：secret-0" in answer
    assert "secret-3" not in answer
    assert len(answer.splitlines()) == 3 * 4 + 2
    assert "系统名称：" in answer and "网址：" in answer
    assert all(value not in answer for value in ("用途：", "标记：", "SSH", "检索", "恢复码-SENTINEL", "API Key"))
    for record in service.entries():
        assert record["description"] not in answer
        assert record["notes"] not in answer
    answer = bot.handle("owner-1", "找项目，返回5条")
    assert answer.count("\n密码：") == 5
    for query, descriptors, key, limit in engine.calls:
        assert key == "provider-key"
        for descriptor in descriptors:
            assert set(descriptor) == {"id", "title", "service", "description", "tags"}
            assert "secret-" not in repr(descriptor)
            assert "account-" not in repr(descriptor)
    assert [call[3] for call in engine.calls] == [3, 5]


def test_known_pasted_secret_disables_external_model(fixture):
    bot, _, _, engine, _ = fixture
    bot.handle("owner-1", "找密码为 secret-0 的项目")
    assert engine.calls[-1][2] is None
    assert "secret-0" not in engine.calls[-1][0]
    assert "[机密已隐藏]" in engine.calls[-1][0]
















def test_clear_or_rebind_during_model_call_prevents_old_result(fixture):
    bot, service, _, engine, _ = fixture
    engine.callback = service.lock
    answer = bot.handle("owner-1", "找项目")
    assert "secret-" not in answer
    assert "状态已变化" in answer


class Sender:
    def __init__(self, on_send=None):
        self.sent = []
        self.on_send = on_send

    def send_text(self, owner, text, context, client_id):
        self.sent.append((owner, text, context, client_id))
        if self.on_send:
            self.on_send()


@pytest.mark.parametrize("change", ["lock", "rebind"])
def test_each_output_chunk_checks_current_binding(fixture, change):
    bot, service, _, _, _ = fixture
    epoch = service.epoch
    def changed():
        if change == "lock":
            service.lock()
        else:
            service.update_settings({"wechat_owner": "other-owner"})
            service.epoch += 1
    client = Sender(changed)
    bot._send_reply(client, "owner-1", "x" * 6500, "context", "digest", epoch)
    assert len(client.sent) == 1
    assert client.sent[0][1] == "x" * 3000


def test_stale_reply_is_not_sent_at_all(fixture):
    bot, service, _, _, _ = fixture
    client = Sender()
    bot._send_reply(client, "owner-1", "secret", "context", "digest", service.epoch - 1)
    assert client.sent == []


class AdvancingEvent:
    def __init__(self, clock, callback=None):
        self.clock = clock
        self.callback = callback
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, delay):
        self.waits.append(delay)
        self.clock[0] += delay
        if self.callback:
            self.callback()


def test_invalid_token_backoff_waits_full_hour_without_real_sleep(fixture, monkeypatch):
    bot, service, _, _, _ = fixture
    now = [0.0]
    monkeypatch.setattr(bot_module.time, "monotonic", lambda: now[0])
    event = AdvancingEvent(now)
    bot.stop_event = event
    bot._wait_retry(3600, service.epoch)
    assert now[0] == 3600
    assert max(event.waits) <= 1


def test_rebinding_interrupts_invalid_token_backoff(fixture, monkeypatch):
    bot, service, _, _, _ = fixture
    now = [0.0]
    monkeypatch.setattr(bot_module.time, "monotonic", lambda: now[0])
    epoch = service.epoch
    def rebind():
        service.epoch += 1
    bot.stop_event = AdvancingEvent(now, rebind)
    bot._wait_retry(3600, epoch)
    assert now[0] == 1


def test_run_uses_vendor_retry_and_filters_unknown_sender_before_model(fixture, monkeypatch):
    bot, service, store, engine, _ = fixture
    owner_message = {"from_user_id": "owner-1", "message_type": 1, "message_id": 1, "context_token": "ctx", "item_list": [{"type": 1, "text_item": {"text": "找项目"}}]}
    outsider = {**owner_message, "from_user_id": "outsider", "message_id": 2}
    client = Sender()
    calls = [0]
    def get_updates(cursor):
        calls[0] += 1
        if calls[0] == 1:
            return {"msgs": [outsider, owner_message, owner_message], "get_updates_buf": "next"}
        raise WeChatError("微信登录已失效，请重新扫码", code=-14)
    client.get_updates = get_updates
    client.close = lambda: None
    monkeypatch.setattr(bot_module, "WeChatClient", lambda *args: client)
    waits = []
    def wait_retry(seconds, epoch):
        waits.append(seconds)
        bot.stop_event.set()
    monkeypatch.setattr(bot, "_wait_retry", wait_retry)
    bot.run()
    assert waits == [3600]
    assert len(engine.calls) == 1
    assert len(client.sent) == 1 and client.sent[0][0] == "owner-1"
    assert len(store.processed) == 1


@pytest.mark.parametrize("text", ["查重，返回5条", "查一下有没有重复记录", "检查这些账号有没有重复"])
def test_explicit_duplicate_requests_use_local_comparison(fixture, text):
    bot, service, _, engine, ids = fixture
    service.save(entry(title="same", username="account-0", password="secret-0"))
    answer = bot.handle("owner-1", text)
    assert "疑似重复（系统名称不同）" in answer
    assert not engine.calls


@pytest.mark.parametrize("command", ["删除第2条", "确认删除 ABC123", "把重复的都删掉", "帮我修改第1条密码", "新增一个账号", "恢复第2条", "清空回收站", "回收站", "保存这条记录", "导入所有账号", "绑定别的微信", "解锁密码库", "DELETE FROM entries"])
def test_wechat_cannot_mutate_even_for_owner(fixture, command):
    bot, service, store, engine, _ = fixture
    before = deepcopy(store.rows)
    assert "仅提供查询" in bot.handle("owner-1", command)
    assert store.rows == before
    assert not engine.calls
    assert not store.deleted_batches


def test_outsider_cannot_query_or_request_management(fixture):
    bot, service, store, engine, _ = fixture
    assert bot.handle("outsider", "找项目") is None
    assert bot.handle("outsider", "删除第2条") is None
    assert not engine.calls and not store.deleted_batches
    service.update_settings({"wechat_owner": None})
    assert bot.handle(None, "找项目") is None


def test_chat_capability_has_no_business_write_methods(fixture):
    bot, _, _, _, _ = fixture
    for method in ("save", "trash", "restore", "setup", "unlock", "lock", "update_settings", "store"):
        assert not hasattr(bot.service, method)


def test_query_about_past_changes_still_only_reads(fixture):
    bot, _, store, engine, _ = fixture
    before = deepcopy(store.rows)
    answer = bot.handle("owner-1", "查询之前删除项目的账号")
    assert "密码：" in answer
    assert len(engine.calls) == 1 and store.rows == before


def test_lock_unlock_discards_query_context(fixture):
    bot, service, _, engine, _ = fixture
    engine.no_match = True
    bot.handle("owner-1", "找项目")
    assert bot.clarifying
    service.lock()
    assert bot.handle("owner-1", "找项目") is None
    assert bot.clarifying is None
    service.unlock("correct horse battery staple")
    bot.handle("owner-1", "另一个场景")
    assert engine.calls[-1][0] == "另一个场景"


def test_a_new_query_is_not_appended_to_a_previous_failed_query(fixture):
    bot, _, _, engine, _ = fixture
    engine.no_match = True
    bot.handle("owner-1", "苹果")
    bot.handle("owner-1", "OA")
    assert engine.calls[-1][0] == "OA"
    bot.handle("owner-1", "补充：公司的办公系统")
    assert engine.calls[-1][0] == "OA；补充：补充：公司的办公系统"


def test_weak_results_are_labelled_but_still_only_render_four_record_fields(fixture):
    bot, service, _, _, ids = fixture
    answer = bot._render(ids[:3], ids[1:3])
    assert answer.count("弱匹配") == 1
    assert answer.count("\n密码：") == 3
    assert "用途：" not in answer and "标记：" not in answer
