"""Synthetic records and model replies; never real user data."""
import json
import httpx


def entry(**changes):
    value = {
        "title": "客户演示云平台",
        "service": "cloud.example.com",
        "username": "account-SENTINEL-8b23@example.com",
        "password": "  Pässword-SENTINEL-7e99\n",
        "description": "去年给客户演示项目时注册的云服务",
        "tags": ["客户演示", "测试环境"],
        "notes": "恢复码-SENTINEL-d135",
        "fields": {"API Key": "sk-SENTINEL-7cc4"},
    }
    value.update(changes)
    return value


RECORDS = [
    {"id": "github", "title": "工作代码托管", "service": "GitHub", "description": "研发项目代码仓库", "tags": ["工作", "开发"]},
    {"id": "mail", "title": "个人邮箱", "service": "Outlook", "description": "生活收件", "tags": ["邮件"]},
    {"id": "cloud", "title": "测试主机", "service": "阿里云 ECS", "description": "测试项目服务器", "tags": ["测试"]},
]


def answer(ids, clarification=None, excluded_ids=None):
    return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"ids": ids, "excluded_ids": excluded_ids or [], "clarification": clarification}, ensure_ascii=False)}}]})


