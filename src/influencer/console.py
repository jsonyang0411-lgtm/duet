"""控制台「达人名单」页的接口（/api/influencer/*，第 6 项 B4；待处理事项 B20）。

src/web/server.py 只负责把请求转到 handle()，这里读写数据库，返回 JSON。
控制台只监听本机，沿用 server.py 的 Origin 校验和"只收 POST"的路径白名单（D2）。
寄样审批单 2026-10-27 起不在这里：寄样单、专属佣金都在「待审批」里用 PRM 的平台动作办。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from .store import SOURCE_CONSOLE, InfluencerStore


def handle(
    path: str, method: str, query: Dict[str, str], body: Dict[str, Any], store: InfluencerStore,
) -> Optional[Dict[str, Any]]:
    """处理一个 /api/influencer/* 请求；路径不认识时返回 None。"""
    if path == "/api/influencer/contacts":
        store.run_maintenance()
        return {"ok": True, "items": store.list_contacts()}
    if path == "/api/influencer/contacts/action" and method == "POST":
        return save_contact(store, body)
    if path == "/api/influencer/recent-chats":
        return {"ok": True, "items": store.recent_private_chats()}
    if path == "/api/influencer/inbox":
        handled = query.get("handled", "") in ("1", "true")
        return {"ok": True, "items": store.list_inbox(handled=handled)}
    if path == "/api/influencer/inbox/action" and method == "POST":
        return mark_inbox_handled(store, body)
    return None


def attention_counts(db_path: str) -> Dict[str, Any]:
    """左栏角标用的数量：待处理。每 30 秒问一次，所以只读打开数据库、不建表。
    数据库或表还不存在时（机器人没跑过）算 0。"""
    zeros = {"ok": True, "inbox": 0}
    path = Path(db_path)
    if db_path == ":memory:" or not path.exists():
        return zeros
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            inbox = conn.execute("SELECT COUNT(*) FROM influencer_inbox WHERE handled_at IS NULL").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return zeros
    return {"ok": True, "inbox": inbox}


def mark_inbox_handled(store: InfluencerStore, body: Dict[str, Any]) -> Dict[str, Any]:
    """运营处理完以后，把这条待处理事项标为已处理。"""
    try:
        item_id = int(body.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "id 要是数字"}
    if not store.mark_inbox_handled(item_id, source=SOURCE_CONSOLE):
        return {"ok": False, "error": "这条事项不存在，或者已经处理过了"}
    return {"ok": True}


def save_contact(store: InfluencerStore, body: Dict[str, Any]) -> Dict[str, Any]:
    """添加、修改、启用或停用一位达人；没传的字段保持原样。"""
    chat_id = str(body.get("chat_id") or "").strip()
    if not chat_id:
        return {"ok": False, "error": "chat_id 不能为空"}
    if chat_id.endswith("@chatroom"):
        return {"ok": False, "error": "这是群聊，不能设为达人"}

    def text(name: str) -> Optional[str]:
        value = body.get(name)
        return None if value is None else str(value).strip()

    enabled = body.get("enabled")
    platform = text("platform")
    if platform is not None:
        platform = platform.upper()
        if platform not in ("", "DY", "XHS"):
            return {"ok": False, "error": "平台只能是 DY（抖音）或 XHS（小红书）"}
    platform_no = text("platform_no")
    if platform_no and platform is None and not (store.get_contact(chat_id) or {}).get("platform"):
        return {"ok": False, "error": "填了号码要同时选平台（抖音或小红书）"}
    contact = store.save_contact(
        chat_id,
        source=SOURCE_CONSOLE,
        display_name=text("display_name"),
        xhs_account=text("xhs_account"),
        note=text("note"),
        enabled=None if enabled is None else bool(enabled),
        platform=platform,
        platform_no=platform_no,
    )
    return {"ok": True, "contact": contact}
