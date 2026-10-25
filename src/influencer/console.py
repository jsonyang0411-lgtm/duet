"""控制台「达人审批」页的接口（/api/influencer/*，第 6 项 B4；待处理事项 B20、B21）。

src/web/server.py 只负责把请求转到 handle()，这里读写数据库、执行审批，返回 JSON。
控制台只监听本机，沿用 server.py 的 Origin 校验和"只收 POST"的路径白名单（D2）。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from .handler import InfluencerBusinessHandler, approval_notice, rejection_notice, sample_limit_problem
from .jst_erp import JuShuiTanERPClient
from .scheduled_agent import morning_brief
from .settings import (
    DEFAULT_COMMISSION_RATE,
    MAX_COMMISSION_RATE,
    STUCK_EXECUTION_SECONDS,
    mock_enabled,
    sample_limits,
)
from .store import EVENT_EXECUTION_STARTED, SOURCE_CONSOLE, InfluencerStore
from .types import STATUS_LABELS, ApprovalActionPacket, GateStatus
from .xhs_cli import XiaohongshuCLIClient

# 审批单列表的筛选项 → 状态；None 表示全部
STATUS_FILTERS = {
    "pending": [GateStatus.PENDING],
    "executing": [GateStatus.EXECUTING],
    "fulfilled": [GateStatus.FULFILLED],
    "rejected": [GateStatus.REJECTED],
    "failed": [GateStatus.FAILED],
    "expired": [GateStatus.EXPIRED],
    "all": None,
}

TRACKING_PLACEHOLDER = "〔运单号〕"
LINK_PLACEHOLDER = "〔推广链接〕"


def handle(
    path: str, method: str, query: Dict[str, str], body: Dict[str, Any], store: InfluencerStore,
) -> Optional[Dict[str, Any]]:
    """处理一个 /api/influencer/* 请求；路径不认识时返回 None。"""
    if path == "/api/influencer/status":
        return status(store)
    if path == "/api/influencer/approvals":
        return list_approvals(store, query.get("status", "pending"))
    if path == "/api/influencer/approvals/action" and method == "POST":
        return act_on_approval(store, body)
    if path == "/api/influencer/contacts":
        return {"ok": True, "items": store.list_contacts()}
    if path == "/api/influencer/contacts/action" and method == "POST":
        return save_contact(store, body)
    if path == "/api/influencer/recent-chats":
        return {"ok": True, "items": store.recent_private_chats()}
    if path == "/api/influencer/overview":
        store.run_maintenance()
        numbers = store.overview()
        return {"ok": True, **numbers, "brief": morning_brief(numbers)}
    if path == "/api/influencer/inbox":
        handled = query.get("handled", "") in ("1", "true")
        return {"ok": True, "items": store.list_inbox(handled=handled)}
    if path == "/api/influencer/inbox/action" and method == "POST":
        return mark_inbox_handled(store, body)
    return None


def attention_counts(db_path: str) -> Dict[str, Any]:
    """左栏角标用的数量：待审批 + 待处理。每 30 秒问一次，所以只读打开数据库：
    不建表、不碰聚水潭和小红书的适配器（模拟模式下它们每次构造都会写一条警告）。
    数据库或表还不存在时（机器人没跑过）都算 0。"""
    zeros = {"ok": True, "pending": 0, "inbox": 0}
    path = Path(db_path)
    if db_path == ":memory:" or not path.exists():
        return zeros
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        try:
            pending = conn.execute(
                "SELECT COUNT(*) FROM influencer_approvals WHERE status = ? AND expires_at >= ?",
                (GateStatus.PENDING.value, time.time()),
            ).fetchone()[0]
            inbox = conn.execute("SELECT COUNT(*) FROM influencer_inbox WHERE handled_at IS NULL").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return zeros
    return {"ok": True, "pending": pending, "inbox": inbox}


def status(store: InfluencerStore) -> Dict[str, Any]:
    """页面顶部要显示的：模拟模式、聚水潭和小红书 CLI 能不能用、待审批数量。"""
    store.run_maintenance()
    counts = store.count_by_status()
    return {
        "ok": True,
        "mock": mock_enabled(),
        "erp": JuShuiTanERPClient().readiness(),
        "xhs": XiaohongshuCLIClient().readiness(),
        "pending": counts.get(GateStatus.PENDING.value, 0),
        "inbox": store.count_open_inbox(),
        "contacts": sum(1 for contact in store.list_contacts() if contact["enabled"]),
        "default_commission_rate": DEFAULT_COMMISSION_RATE,
        "max_commission_rate": MAX_COMMISSION_RATE,
        "sample_limits": sample_limits(),
    }


def list_approvals(store: InfluencerStore, status_filter: str) -> Dict[str, Any]:
    if status_filter not in STATUS_FILTERS:
        return {"ok": False, "error": f"不认识的筛选：{status_filter}"}
    store.run_maintenance()
    packets = store.list_approvals(STATUS_FILTERS[status_filter])
    events = store.events_for(packet.packet_id for packet in packets)
    by_status = store.count_by_status()
    counts = {
        name: sum(by_status.get(status.value, 0) for status in statuses)
        for name, statuses in STATUS_FILTERS.items() if statuses is not None
    }
    return {
        "ok": True,
        "items": [packet_json(packet, events[packet.packet_id], store) for packet in packets],
        "counts": counts,
    }


def packet_json(
    packet: ApprovalActionPacket, events: List[Dict[str, Any]], store: Optional[InfluencerStore] = None,
) -> Dict[str, Any]:
    """审批卡要显示的全部内容。notice 是要发给达人的消息：待审批时是批准后的预览，
    执行成功后是带运单号的确认消息，驳回后是婉拒话术（Q2，由运营复制后发送）。"""
    data = asdict(packet)
    data["status"] = packet.status.value
    data["status_label"] = STATUS_LABELS[packet.status]
    data["events"] = events
    if packet.status == GateStatus.PENDING and store is not None:
        # 寄样上限（D5）：超了就在审批卡上标出来，「通过」按钮不能点
        data["limit_problem"] = sample_limit_problem(store, packet)
    if packet.status == GateStatus.PENDING:
        data["notice"] = approval_notice(replace(packet, tracking_number=TRACKING_PLACEHOLDER, product_link=LINK_PLACEHOLDER))
    elif packet.status == GateStatus.FULFILLED:
        data["notice"] = approval_notice(packet)
    elif packet.status == GateStatus.REJECTED:
        data["notice"] = rejection_notice(packet)
    else:
        data["notice"] = ""
    if packet.status == GateStatus.EXECUTING:
        # 什么时候开始执行的、是不是已经可以人工核对了（B23）。老版本没记开始时间的单直接算中断
        started = next((e["created_at"] for e in reversed(events) if e["action"] == EVENT_EXECUTION_STARTED), None)
        data["execution_started_at"] = started
        data["interrupted"] = started is None or time.time() - started >= STUCK_EXECUTION_SECONDS
    return data


def act_on_approval(store: InfluencerStore, body: Dict[str, Any]) -> Dict[str, Any]:
    """通过或驳回一张审批单。通过时会真的调用聚水潭和小红书 CLI（模拟模式下是假的）。
    action=resolve 是给执行被中断的单记下人工核对的结果（B23），不调用聚水潭和小红书。"""
    packet_id = str(body.get("packet_id") or "").strip()
    action = body.get("action")
    if action == "resolve":
        return resolve_interrupted(store, packet_id, body)
    if action not in ("approve", "reject"):
        return {"ok": False, "error": "action 只能是 approve（通过）、reject（驳回）或 resolve（人工核对）"}
    commission_rate = body.get("commission_rate")
    if commission_rate in (None, ""):
        commission_rate = None
    else:
        try:
            commission_rate = float(commission_rate)
        except (TypeError, ValueError):
            return {"ok": False, "error": "佣金要填数字"}
    handler = InfluencerBusinessHandler(store)
    result = asyncio.run(handler.execute_operator_approval(
        packet_id,
        action == "approve",
        xhs_account=str(body.get("xhs_account") or "").strip(),
        commission_rate=commission_rate,
        reason=str(body.get("reason") or "").strip(),
        source=SOURCE_CONSOLE,
    ))
    return _with_packet(store, packet_id, result)


def resolve_interrupted(store: InfluencerStore, packet_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
    outcome = body.get("outcome")
    if outcome not in ("fulfilled", "failed"):
        return {"ok": False, "error": "outcome 只能是 fulfilled（已执行）或 failed（失败）"}
    result = InfluencerBusinessHandler(store).resolve_interrupted_execution(
        packet_id,
        outcome == "fulfilled",
        order_sn=str(body.get("order_sn") or ""),
        tracking_number=str(body.get("tracking_number") or ""),
        commission_plan=str(body.get("commission_plan") or ""),
        product_link=str(body.get("product_link") or ""),
        note=str(body.get("note") or ""),
        source=SOURCE_CONSOLE,
    )
    return _with_packet(store, packet_id, result)


def _with_packet(store: InfluencerStore, packet_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    response = {"ok": bool(result.get("success")), **result}
    packet = store.get_approval(packet_id)
    if packet is not None:
        response["packet"] = packet_json(packet, store.events_for([packet_id])[packet_id], store)
    return response


def mark_inbox_handled(store: InfluencerStore, body: Dict[str, Any]) -> Dict[str, Any]:
    """运营回复过达人、或者处理完缺货以后，把这条待处理事项标为已处理。"""
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
    contact = store.save_contact(
        chat_id,
        source=SOURCE_CONSOLE,
        display_name=text("display_name"),
        xhs_account=text("xhs_account"),
        note=text("note"),
        enabled=None if enabled is None else bool(enabled),
    )
    return {"ok": True, "contact": contact}
