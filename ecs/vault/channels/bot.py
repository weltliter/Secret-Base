"""Owner-only command routing; secrets are rendered outside the model boundary."""
import hashlib
import logging
import re
import threading
import time

from ..search.engine import RetrievalEngine
from ..core.service import QueryVault
from .wechat import WeChatClient, WeChatError


READ_ONLY = "微信仅提供查询和查重；新增、修改、删除及恢复请通过当前电脑的 SSH 隧道管理页操作。"
HELP = "可直接描述要找的账号，例如：去年项目用的阿里云账号。默认最多返回3条，可说返回5条。\n查重：综合系统名称、网址和凭据核对重复候选。\n" + READ_ONLY


class BotController:
    def __init__(self, service, engine=None):
        self.service = QueryVault(service)
        self.engine = engine or RetrievalEngine()
        self.stop_event = threading.Event()
        self.connected = False
        self.error = None
        self.clarifying = None
        self.observed_epoch = service.epoch

    def clear(self):
        self.clarifying = None
        self.connected = False
        self.error = None
        if hasattr(self.engine, "clear"):
            self.engine.clear()

    def _check_epoch(self):
        if self.observed_epoch != self.service.epoch:
            self.clear()
            self.observed_epoch = self.service.epoch

    def _render(self, ids, weak_ids=()):
        records = [self.service.entry(i) for i in list(dict.fromkeys(ids))[:5]]
        if not records:
            return "没有找到匹配的记录，请补充平台、用途或项目描述。"
        parts = []
        weak = set(weak_ids)
        weak_heading = False
        for index, entry in enumerate(records, 1):
            if entry["id"] in weak and not weak_heading:
                parts.append("以下为相近候选（弱匹配，可能不相关），请核对系统名称：")
                weak_heading = True
            text = f"{index}. 系统名称：{entry['title']}\n网址：{entry.get('service') or '未填写'}\n账号：{entry.get('username', '')}"
            text += f"\n密码：{entry.get('password', '')}"
            parts.append(text)
        return "\n\n".join(parts)

    def _command(self, text):
        if text in ("帮助", "help", "/help", "菜单"):
            return HELP
        if text in ("取消", "取消查询"):
            self.clarifying = None
            return "已清除查询上下文。"
        mutating = re.search(r"删除|删掉|清空|修改|新增|添加|更新|替换|恢复|保存|导入|绑定|解绑|解锁|锁定|回收站|\b(?:delete|update|insert|restore|drop)\b", text, re.I)
        explicit_query = re.match(r"^(?:请)?(?:查询|查找|搜索|找|查看)", text)
        if mutating and not explicit_query:
            return READ_ONLY
        return None

    def handle(self, sender, text):
        with self.service.mutex:
            self._check_epoch()
            if not self.service.unlocked:
                return None
            settings = self.service.settings()
            owner = settings.get("wechat_owner")
            if not owner or sender != owner:
                return None
            epoch = self.service.epoch
            if not isinstance(text, str) or not text.strip():
                return "请发送文字描述。"
            text = text.strip()[:4000]
            command = self._command(text)
            if command is not None:
                return command
        limit = 5 if re.search(r"(?:5|五)\s*(?:条|个)", text) else 3
        with self.service.mutex:
            if epoch != self.service.epoch or not self.service.unlocked:
                return None
            if (re.fullmatch(r"(?:查重|查看重复|重复账号|重复密码|查找重复)(?:[，, ]*(?:返回|显示|给我)?\s*前?[35三五]\s*(?:条|个))?", text)
                    or re.search(r"(?:查一下|检查|查找|查看).*(?:有没有|是否|有无)?重复(?:记录|账号|账户|密码)?", text)):
                self.clarifying = None
                groups = self.service.duplicate_groups()
                ids = list(dict.fromkeys(i for g in groups for i in g["ids"]))[:limit]
                if not ids:
                    return "没有发现综合信息匹配的重复候选或同系统账号的不同密码版本。"
                visible = set(ids)
                reasons = [g["kind"] + "：" + g["description"] for g in groups if len(visible.intersection(g["ids"])) > 1]
                return "\n".join(dict.fromkeys(reasons)) + "\n\n" + self._render(ids)
            if self.clarifying and time.monotonic() < self.clarifying["expires"] and re.match(r"^(?:补充|是指|就是|刚才|上一个)", text):
                text = self.clarifying["query"] + "；补充：" + text
            self.clarifying = None
            entries = self.service.entries()
            descriptors = self.service.descriptors()
            key = settings.get("deepseek_api_key")
            # A pasted known secret should not be forwarded as a model query.
            known_secrets = {v for e in entries for v in
                             [e.get("username", ""), e.get("password", ""), e.get("notes", ""), *e.get("fields", {}).values()]
                             if isinstance(v, str) and v}
            known_secrets.update(v for v in (key, settings.get("wechat_token")) if isinstance(v, str) and v)
            secret_in_query = any(v in text for v in known_secrets)
            if secret_in_query:
                key = None
                for value in sorted(known_secrets, key=len, reverse=True):
                    text = text.replace(value, "[机密已隐藏]")
        result = self.engine.search(text, descriptors, key, limit)
        with self.service.mutex:
            if epoch != self.service.epoch or not self.service.unlocked:
                return "密码库状态已变化，请解锁后重新查询。"
            try:
                self.service.record_query(text, descriptors, result)
            except Exception:
                # A log-store failure must not suppress an otherwise valid query.
                logging.getLogger("vault.audit").warning("query_log_write_failed")
            if result.get("ids"):
                allowed = {d["id"] for d in descriptors}
                ids = list(dict.fromkeys(i for i in result["ids"] if isinstance(i, str) and i in allowed))[:limit]
                return self._render(ids, result.get("weak_ids", []))
            self.clarifying = {"query": text, "expires": time.monotonic() + 300}
            return result.get("clarification") or "没有找到匹配记录，请补充平台、用途或项目描述。"

    def _send_reply(self, client, owner, answer, context, digest, epoch):
        for start in range(0, len(answer), 3000):
            # Serialize the authorization check and each HTTP send with locking
            # or rebinding. Once a lock has completed, no remaining chunk starts.
            with self.service.mutex:
                if epoch != self.service.epoch or not self.service.unlocked or self.stop_event.is_set():
                    return
                if self.service.settings().get("wechat_owner") != owner:
                    return
                client.send_text(owner, answer[start:start + 3000], context, "vault-" + digest[:24] + "-" + str(start))

    def _wait_retry(self, seconds, epoch):
        deadline = time.monotonic() + seconds
        # A revoked token backs off for the full vendor interval, while a new
        # binding/unlock can resume immediately instead of waiting an hour.
        while not self.stop_event.is_set() and self.service.epoch == epoch and self.service.unlocked:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.stop_event.wait(min(1.0, remaining))

    def run(self):
        while not self.stop_event.is_set():
            client = None
            epoch = self.service.epoch
            try:
                self._check_epoch()
                if not self.service.unlocked:
                    self.stop_event.wait(2)
                    continue
                settings = self.service.settings()
                if not settings.get("wechat_token") or not settings.get("wechat_owner"):
                    self.stop_event.wait(3)
                    continue
                epoch = self.service.epoch
                client = WeChatClient(settings["wechat_token"], settings.get("wechat_base_url", "https://ilinkai.weixin.qq.com"))
                result = client.get_updates(settings.get("wechat_cursor", ""))
                if epoch != self.service.epoch or not self.service.unlocked:
                    continue
                self.connected, self.error = True, None
                for message in result.get("msgs", []):
                    if epoch != self.service.epoch or not self.service.unlocked:
                        break
                    if not isinstance(message, dict) or message.get("from_user_id") != settings["wechat_owner"]:
                        continue
                    if message.get("message_type") not in (1, None):
                        continue
                    context = message.get("context_token")
                    message_id = message.get("message_id")
                    if message_id is None:
                        message_id = message.get("client_id")
                    if not context or message_id is None:
                        continue
                    digest = hashlib.sha256((settings.get("wechat_bot_id", "") + ":" + settings["wechat_owner"] + ":" + str(message_id)).encode()).hexdigest()
                    with self.service.mutex:
                        if epoch != self.service.epoch or not self.service.unlocked:
                            break
                        if not self.service.mark_message(digest):
                            continue
                    parts = [item["text_item"]["text"] for item in message.get("item_list", [])
                             if isinstance(item, dict) and item.get("type") == 1
                             and isinstance(item.get("text_item"), dict)
                             and isinstance(item["text_item"].get("text"), str)]
                    if not parts:
                        continue
                    try:
                        answer = self.handle(settings["wechat_owner"], "\n".join(parts))
                    except ValueError as exc:
                        answer = str(exc)
                    if answer and epoch == self.service.epoch and self.service.unlocked:
                        self._send_reply(client, settings["wechat_owner"], answer, context, digest, epoch)
                with self.service.mutex:
                    if epoch == self.service.epoch and self.service.unlocked and result.get("get_updates_buf"):
                        self.service.save_cursor(result["get_updates_buf"])
            except WeChatError as exc:
                self.connected, self.error = False, str(exc)
                self._wait_retry(exc.retry_after, epoch)
            except Exception:
                # Neither exception repr nor tracebacks may expose model/chat/DB payloads.
                self.connected, self.error = False, "连接暂不可用，请检查配置或重新解锁。"
                self.stop_event.wait(5)
            finally:
                if client:
                    client.close()
