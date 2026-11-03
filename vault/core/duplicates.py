"""Context-aware comparisons performed locally; fingerprints never leave memory."""
import hashlib
import hmac
import json
import secrets
import unicodedata
from collections import defaultdict
from urllib.parse import urlsplit


def normalize_name(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").split()).casefold()


def normalize_address(value):
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value if "://" in value else "https://" + value)
        host = parsed.hostname or ""
        if parsed.scheme not in ("http", "https") or not host or any(c.isspace() for c in host):
            return value
        if "." not in host and ":" not in host and host != "localhost":
            return normalize_name(value)  # Older entries may contain a platform label.
        port = parsed.port
        authority = host.casefold()
        if port and port != (443 if parsed.scheme == "https" else 80):
            authority += ":" + str(port)
        # Preserve paths, tenant queries and hash routes: they can identify different systems.
        address = authority + parsed.path.rstrip("/")
        if parsed.query:
            address += "?" + parsed.query
        if parsed.fragment:
            address += "#" + parsed.fragment
        if parsed.username is not None:
            return value  # Do not discard meaningful embedded identity information.
        return address
    except ValueError:
        return value


def compare_entries(entries, *, include_reuse=False):
    key = secrets.token_bytes(32)

    def fingerprint(parts):
        return hmac.new(key, json.dumps(parts, ensure_ascii=False).encode(), hashlib.sha256).digest()

    exact, by_address, by_name, accounts, passwords = (defaultdict(list) for _ in range(5))
    context = {}
    for entry in entries:
        name = normalize_name(entry.get("title"))
        address = normalize_address(entry.get("service"))
        account, password = entry.get("username", ""), entry.get("password", "")
        context[entry["id"]] = (name, address, account)
        if password:
            passwords[fingerprint([password])].append(entry)
        # Empty fields are missing evidence, not proof that two credentials match.
        if not account or not password:
            continue
        exact[fingerprint([name, address, account, password])].append(entry)
        if address:
            by_address[fingerprint([address, account, password])].append(entry)
        if name:
            by_name[fingerprint([name, account, password])].append(entry)
        if name or address:
            accounts[fingerprint([name, address, account])].append(entry)

    groups, emitted = [], set()

    def add(category, kind, reason, values):
        identity = (category, frozenset(v["id"] for v in values))
        if len(values) < 2 or identity in emitted:
            return
        emitted.add(identity)
        differences = []
        for field, label in (("description", "检索描述"), ("tags", "标签"), ("notes", "备注"), ("fields", "自定义字段")):
            contents = [sorted(v.get(field, [])) if field == "tags" else v.get(field) for v in values]
            if len({json.dumps(value, sort_keys=True, ensure_ascii=False) for value in contents}) > 1:
                differences.append(label)
        if differences and category in ("duplicate", "possible_duplicate"):
            kind += "，补充资料不同"
            reason += "；" + "、".join(differences) + "存在差异，请核对后处理"
        groups.append({"category": category, "kind": kind, "description": reason,
                       "differences": differences, "ids": [v["id"] for v in values],
                       "titles": [v["title"] for v in values]})

    for values in exact.values():
        name, address, _ = context[values[0]["id"]]
        if name and address:
            add("duplicate", "凭据重复", "系统名称、网址、账号和密码均一致", values)
        elif name or address:
            add("possible_duplicate", "疑似重复（信息不完整）", "已有系统信息与账号、密码一致，但缺少系统名称或网址", values)
    for values in by_address.values():
        if len({context[v["id"]][0] for v in values}) > 1:
            add("possible_duplicate", "疑似重复（系统名称不同）", "网址、账号和密码一致，系统名称不同；可能是别名或同站点不同系统", values)
    for values in by_name.values():
        if len({context[v["id"]][1] for v in values}) > 1:
            add("possible_duplicate", "疑似重复（网址不同）", "系统名称、账号和密码一致，网址不同；可能是不同入口或环境", values)
    for values in accounts.values():
        if len({fingerprint([v["password"]]) for v in values}) > 1:
            add("version", "同系统账号存在不同密码", "系统名称、网址和账号一致，密码不同；可能是历史版本，不自动判为重复", values)
    if include_reuse:
        for values in passwords.values():
            if len({fingerprint(context[v["id"]]) for v in values}) > 1:
                add("password_reuse", "密码复用提醒（非重复记录）", "不同系统、网址或账号使用了相同密码；仅作提醒，不作为删除依据", values)
    return groups
