"""达人模块的数据存储（第 6 项，B5）：达人名单、没补齐的申请、审批单、操作记录。

和机器人共用一个 SQLite 文件（默认 data/messages.db）。机器人和控制台都只通过这里
读写，不共享内存对象。表名都以 influencer_ 开头，建表不经过原项目的 src/db/schema.py。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, Iterator, List, Optional, Tuple

from .settings import DRAFT_TTL_SECONDS, PERSONAL_INFO_RETENTION_SECONDS
from .types import ApprovalActionPacket, GateStatus, SampleRequestDraft

if TYPE_CHECKING:
    from .send_policy import SendPolicy

# 操作记录里的"来源"
SOURCE_BOT = "机器人"
SOURCE_CONSOLE = "控制台"
SOURCE_ENV = "环境变量"
SOURCE_AUTO = "自动"

# 处理完的状态：从这一刻起开始算个人信息的保留期（Q3）
FINISHED_STATUSES = (GateStatus.REJECTED, GateStatus.FULFILLED, GateStatus.FAILED, GateStatus.EXPIRED)

# 不是联系人的系统会话，不出现在"最近私聊"里
_SYSTEM_CHATS = ("filehelper", "weixin", "newsapp", "fmessage", "medianote", "floatbottle")

SCHEMA_SQL = """
-- 达人名单（D3）和达人档案。停用的达人不删，环境变量也不会把他们加回来（Q4）
CREATE TABLE IF NOT EXISTS influencer_contacts (
    chat_id           TEXT    PRIMARY KEY,
    display_name      TEXT    NOT NULL DEFAULT '',
    xhs_account       TEXT    NOT NULL DEFAULT '',
    follower_count    INTEGER,
    recent_like_count INTEGER,
    stats_updated_at  REAL,
    enabled           INTEGER NOT NULL DEFAULT 1,
    note              TEXT    NOT NULL DEFAULT '',
    created_at        REAL    NOT NULL,
    updated_at        REAL    NOT NULL
);

-- 还没补齐的索样申请，一位达人一份（SampleRequestDraft 的 JSON）
CREATE TABLE IF NOT EXISTS influencer_drafts (
    chat_id    TEXT PRIMARY KEY,
    draft_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- 审批单：列名和 ApprovalActionPacket 的字段名一一对应
CREATE TABLE IF NOT EXISTS influencer_approvals (
    packet_id                TEXT    PRIMARY KEY,
    influencer_id            TEXT    NOT NULL,
    influencer_name          TEXT    NOT NULL DEFAULT '',
    follower_count           INTEGER,
    requested_product        TEXT    NOT NULL,
    sku_id                   TEXT    NOT NULL,
    stock_available          INTEGER NOT NULL DEFAULT 0,
    sample_cost              REAL,
    proposed_commission_rate REAL    NOT NULL,
    shipping_address         TEXT    NOT NULL DEFAULT '',
    what_cannot_be_undone    TEXT    NOT NULL DEFAULT '',
    qualification            TEXT    NOT NULL DEFAULT '',
    recipient_name           TEXT    NOT NULL DEFAULT '',
    recipient_phone          TEXT    NOT NULL DEFAULT '',
    xhs_account              TEXT    NOT NULL DEFAULT '',
    evidence                 TEXT    NOT NULL DEFAULT '',
    status                   TEXT    NOT NULL,
    created_at               REAL    NOT NULL,
    expires_at               REAL    NOT NULL,
    failure_reason           TEXT    NOT NULL DEFAULT '',
    order_sn                 TEXT    NOT NULL DEFAULT '',
    tracking_number          TEXT    NOT NULL DEFAULT '',
    commission_plan          TEXT    NOT NULL DEFAULT '',
    handled_at               REAL,
    personal_info_cleared_at REAL
);

CREATE INDEX IF NOT EXISTS idx_influencer_approvals_status
    ON influencer_approvals(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_influencer_approvals_chat
    ON influencer_approvals(influencer_id, created_at DESC);

-- 操作记录：什么时候、从哪里、对哪张审批单或哪位达人做了什么。
-- 驳回原因留着每周复盘用（R7）。这里不写收件人信息
CREATE TABLE IF NOT EXISTS influencer_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_id  TEXT NOT NULL DEFAULT '',
    chat_id    TEXT NOT NULL DEFAULT '',
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_influencer_events_packet
    ON influencer_events(packet_id, created_at);

-- 待运营处理的事项：达人发来的非索样消息（B20）、达人申请时缺货（B21）。
-- 提醒只做在控制台里（D14）。处理完 90 天后删除，和收件信息的保留期一样（D10）
CREATE TABLE IF NOT EXISTS influencer_inbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    content      TEXT    NOT NULL DEFAULT '',
    auto_replied INTEGER NOT NULL DEFAULT 0,
    created_at   REAL    NOT NULL,
    handled_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_influencer_inbox_open
    ON influencer_inbox(handled_at, created_at DESC);

-- 和达人的对话：交给 Claude 回复时带上最近几轮，它才记得前面聊过什么。
-- 里面可能有收件信息，90 天后删除（D10）
CREATE TABLE IF NOT EXISTS influencer_dialog (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT    NOT NULL,
    incoming   TEXT    NOT NULL,
    reply      TEXT    NOT NULL DEFAULT '',
    created_at REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_influencer_dialog_chat
    ON influencer_dialog(chat_id, created_at DESC);

-- 主动发给达人的消息（批准后的确认消息等，B15）的发送队列。什么时候能发由 send_policy 决定
-- （间隔、每小时上限、夜间不发）。发出后 90 天删除
CREATE TABLE IF NOT EXISTS influencer_outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT    NOT NULL,
    text       TEXT    NOT NULL,
    kind       TEXT    NOT NULL DEFAULT '',
    packet_id  TEXT    NOT NULL DEFAULT '',
    created_at REAL    NOT NULL,
    sent_at    REAL
);

CREATE INDEX IF NOT EXISTS idx_influencer_outbox_open
    ON influencer_outbox(sent_at, created_at);
"""

# 审批单从待审批改成执行中时记的一条操作记录，用来判断执行是不是被中断了（B23）
EVENT_EXECUTION_STARTED = "开始执行"

# 待处理事项的种类
INBOX_MESSAGE = "message"
INBOX_OUT_OF_STOCK = "out_of_stock"
INBOX_COMMISSION = "commission"  # 达人要求提高佣金，机器人回了固定话术

_PACKET_COLUMNS = tuple(f.name for f in fields(ApprovalActionPacket))
_DRAFT_FIELDS = frozenset(f.name for f in fields(SampleRequestDraft))


def _placeholders(count: int) -> str:
    return ", ".join("?" * count)


def _packet_from_row(row: sqlite3.Row) -> ApprovalActionPacket:
    values = {name: row[name] for name in _PACKET_COLUMNS}
    values["status"] = GateStatus(values["status"])
    return ApprovalActionPacket(**values)


def _contact_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    contact = dict(row)
    contact["enabled"] = bool(contact["enabled"])
    return contact


class InfluencerStore:
    def __init__(self, db_path: str):
        self._db_path = db_path
        # ":memory:" 只给测试用：内存库每开一个连接就是一个新库，所以一直用同一个连接
        self._shared: Optional[sqlite3.Connection] = None
        if db_path == ":memory:":
            self._shared = sqlite3.connect(":memory:", check_same_thread=False)
            self._shared.row_factory = sqlite3.Row
        else:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._shared_lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """一次操作开一个连接：正常结束就提交，出错就回滚。"""
        if self._shared is not None:
            with self._shared_lock, self._shared:
                yield self._shared
            return
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---- 达人名单（D3） ----

    def is_influencer(self, chat_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM influencer_contacts WHERE chat_id = ? AND enabled = 1", (chat_id,),
            ).fetchone()
        return row is not None

    def add_contacts(self, chat_ids: Iterable[str], *, source: str, note: str = "") -> List[str]:
        """把名单里还没有的 chat_id 加进来；已有的（包括停用的）一概不动（Q4）。返回新加的。"""
        now = time.time()
        added = []
        with self._connect() as conn:
            for chat_id in chat_ids:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO influencer_contacts (chat_id, note, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?)",
                    (chat_id, note, now, now),
                )
                if cur.rowcount:
                    added.append(chat_id)
                    self._log(conn, "加入名单", chat_id=chat_id, detail=note, source=source)
        return added

    def save_contact(
        self,
        chat_id: str,
        *,
        source: str,
        display_name: Optional[str] = None,
        xhs_account: Optional[str] = None,
        note: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """新增或修改一位达人；传 None 的字段保持原样。返回改完后的记录。"""
        now = time.time()
        changes = {
            name: value for name, value in (
                ("display_name", display_name),
                ("xhs_account", xhs_account),
                ("note", note),
                ("enabled", None if enabled is None else int(enabled)),
            ) if value is not None
        }
        with self._connect() as conn:
            before = conn.execute(
                "SELECT enabled FROM influencer_contacts WHERE chat_id = ?", (chat_id,),
            ).fetchone()
            if before is None:
                conn.execute(
                    "INSERT INTO influencer_contacts (chat_id, created_at, updated_at) VALUES (?, ?, ?)",
                    (chat_id, now, now),
                )
            if changes:
                assignments = ", ".join(f"{name} = ?" for name in changes)
                conn.execute(
                    f"UPDATE influencer_contacts SET {assignments}, updated_at = ? WHERE chat_id = ?",
                    (*changes.values(), now, chat_id),
                )
            if before is None:
                action = "加入名单"
            elif enabled is not None and bool(before["enabled"]) != enabled:
                action = "启用" if enabled else "停用"
            else:
                action = "修改名单"
            self._log(conn, action, chat_id=chat_id, source=source)
            row = conn.execute("SELECT * FROM influencer_contacts WHERE chat_id = ?", (chat_id,)).fetchone()
        return _contact_from_row(row)

    def get_contact(self, chat_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM influencer_contacts WHERE chat_id = ?", (chat_id,)).fetchone()
        return None if row is None else _contact_from_row(row)

    def list_contacts(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM influencer_contacts ORDER BY enabled DESC, updated_at DESC",
            ).fetchall()
        return [_contact_from_row(row) for row in rows]

    def record_profile(
        self,
        chat_id: str,
        *,
        nickname: str,
        xhs_account: str,
        follower_count: Optional[int],
        recent_like_count: Optional[int],
    ) -> None:
        """记下机器人看到的达人资料。显示名和小红书账号只在名单里还空着时才填，运营填的优先；
        粉丝和点赞只记在名单上那个小红书账号名下，拿不到数据时保留上次的。"""
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT display_name, xhs_account FROM influencer_contacts WHERE chat_id = ?", (chat_id,),
            ).fetchone()
            if row is None:
                return
            values: Dict[str, Any] = {}
            if nickname and not row["display_name"]:
                values["display_name"] = nickname
            if xhs_account and not row["xhs_account"]:
                values["xhs_account"] = xhs_account
            account = row["xhs_account"] or xhs_account
            if xhs_account and xhs_account == account and follower_count is not None:
                values.update(
                    follower_count=follower_count, recent_like_count=recent_like_count, stats_updated_at=now,
                )
            if not values:
                return
            assignments = ", ".join(f"{name} = ?" for name in values)
            conn.execute(
                f"UPDATE influencer_contacts SET {assignments}, updated_at = ? WHERE chat_id = ?",
                (*values.values(), now, chat_id),
            )

    def recent_private_chats(self, *, limit: int = 30) -> List[Dict[str, Any]]:
        """最近私聊过的联系人，取自原项目的 messages 表，供在控制台里挑选达人。

        群聊（chat_id 以 @chatroom 结尾）、公众号（gh_ 开头）和文件传输助手这类系统会话
        都去掉。机器人还没跑过、没有 messages 表时返回空列表。
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT m.chat_id, MAX(m.timestamp) AS last_ts, COUNT(*) AS message_count,"
                    " (SELECT n.sender_name FROM messages AS n WHERE n.chat_id = m.chat_id"
                    "  ORDER BY n.timestamp DESC LIMIT 1) AS name,"
                    " c.enabled AS contact_enabled"
                    " FROM messages AS m LEFT JOIN influencer_contacts AS c ON c.chat_id = m.chat_id"
                    " WHERE m.chat_id NOT LIKE '%@chatroom' AND substr(m.chat_id, 1, 3) != 'gh_'"
                    f" AND m.chat_id NOT IN ({_placeholders(len(_SYSTEM_CHATS))})"
                    " GROUP BY m.chat_id ORDER BY last_ts DESC LIMIT ?",
                    (*_SYSTEM_CHATS, limit),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
        return [
            {
                "chat_id": row["chat_id"],
                "name": row["name"] or "",
                "last_ts": row["last_ts"],
                "message_count": row["message_count"],
                "in_list": row["contact_enabled"] is not None,
                "enabled": bool(row["contact_enabled"]),
            }
            for row in rows
        ]

    # ---- 没补齐的申请 ----

    def get_draft(self, chat_id: str) -> Optional[SampleRequestDraft]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT draft_json FROM influencer_drafts WHERE chat_id = ?", (chat_id,),
            ).fetchone()
        if row is None:
            return None
        data = json.loads(row["draft_json"])
        draft = SampleRequestDraft(**{name: value for name, value in data.items() if name in _DRAFT_FIELDS})
        draft.asked_for = tuple(draft.asked_for)
        return draft

    def save_draft(self, draft: SampleRequestDraft) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO influencer_drafts (chat_id, draft_json, updated_at) VALUES (?, ?, ?)",
                (draft.chat_id, json.dumps(asdict(draft), ensure_ascii=False), draft.updated_at),
            )

    def delete_draft(self, chat_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM influencer_drafts WHERE chat_id = ?", (chat_id,))

    # ---- 审批单 ----

    def add_approval(self, packet: ApprovalActionPacket, *, source: str) -> None:
        values = asdict(packet)
        values["status"] = packet.status.value
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO influencer_approvals ({', '.join(_PACKET_COLUMNS)})"
                f" VALUES ({_placeholders(len(_PACKET_COLUMNS))})",
                [values[name] for name in _PACKET_COLUMNS],
            )
            self._log(
                conn, "提交审批", packet_id=packet.packet_id, chat_id=packet.influencer_id,
                detail=packet.requested_product, source=source,
            )

    def get_approval(self, packet_id: str) -> Optional[ApprovalActionPacket]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM influencer_approvals WHERE packet_id = ?", (packet_id,),
            ).fetchone()
        return None if row is None else _packet_from_row(row)

    def list_approvals(
        self, statuses: Optional[Iterable[GateStatus]] = None, *, limit: int = 200,
    ) -> List[ApprovalActionPacket]:
        """按创建时间从新到旧列出审批单；statuses 为 None 时列出全部。"""
        sql, params = "SELECT * FROM influencer_approvals", []
        if statuses is not None:
            params = [status.value for status in statuses]
            if not params:
                return []
            sql += f" WHERE status IN ({_placeholders(len(params))})"
        with self._connect() as conn:
            rows = conn.execute(sql + " ORDER BY created_at DESC LIMIT ?", (*params, limit)).fetchall()
        return [_packet_from_row(row) for row in rows]

    def latest_pending_without_xhs_account(self, chat_id: str) -> Optional[ApprovalActionPacket]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM influencer_approvals WHERE influencer_id = ? AND status = ?"
                " AND xhs_account = '' ORDER BY created_at DESC LIMIT 1",
                (chat_id, GateStatus.PENDING.value),
            ).fetchone()
        return None if row is None else _packet_from_row(row)

    def update_pending(self, packet_id: str, **values: Any) -> bool:
        """改一张还在待审批的单。单已经不是待审批了就什么都不改，返回 False。"""
        return self._update(packet_id, GateStatus.PENDING, values)

    def transition(
        self, packet_id: str, from_status: GateStatus, to_status: GateStatus, **values: Any,
    ) -> bool:
        """当前状态是 from_status 时才改成 to_status，否则什么都不改，返回 False。

        "每张审批单只能执行一次"靠的就是这个条件更新：两个请求同时想把同一张单从
        待审批改成执行中，只有一个能改到。跨线程、跨进程、重启之后都成立。
        """
        values["status"] = to_status.value
        if to_status in FINISHED_STATUSES:
            values.setdefault("handled_at", time.time())
        return self._update(packet_id, from_status, values)

    def _update(self, packet_id: str, expected: GateStatus, values: Dict[str, Any]) -> bool:
        unknown = set(values) - set(_PACKET_COLUMNS)
        if unknown:
            raise ValueError(f"审批单没有这些字段：{sorted(unknown)}")
        if not values:
            return False
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE influencer_approvals SET {assignments} WHERE packet_id = ? AND status = ?",
                (*values.values(), packet_id, expected.value),
            )
        return cur.rowcount == 1

    def count_samples(self, *, since: float, chat_id: str = "") -> int:
        """寄出去的样品单数（D5）：since 以后执行成功的，加上正在执行的。chat_id 为空时算所有达人。"""
        sql = (
            "SELECT COUNT(*) FROM influencer_approvals"
            " WHERE ((status = ? AND handled_at >= ?) OR status = ?)"
        )
        params: List[Any] = [GateStatus.FULFILLED.value, since, GateStatus.EXECUTING.value]
        if chat_id:
            sql += " AND influencer_id = ?"
            params.append(chat_id)
        with self._connect() as conn:
            return conn.execute(sql, params).fetchone()[0]

    def count_by_status(self) -> Dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM influencer_approvals GROUP BY status",
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    # ---- 操作记录 ----

    def add_event(
        self, action: str, *, source: str, packet_id: str = "", chat_id: str = "", detail: str = "",
    ) -> None:
        with self._connect() as conn:
            self._log(conn, action, packet_id=packet_id, chat_id=chat_id, detail=detail, source=source)

    @staticmethod
    def _log(
        conn: sqlite3.Connection, action: str, *, source: str,
        packet_id: str = "", chat_id: str = "", detail: str = "",
    ) -> None:
        conn.execute(
            "INSERT INTO influencer_events (packet_id, chat_id, action, detail, source, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (packet_id, chat_id, action, detail, source, time.time()),
        )

    def execution_started_at(self, packet_id: str) -> Optional[float]:
        """这张单最近一次"开始执行"的时间。老版本没记这条，查不到时返回 None。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(created_at) AS at FROM influencer_events WHERE packet_id = ? AND action = ?",
                (packet_id, EVENT_EXECUTION_STARTED),
            ).fetchone()
        return row["at"]

    def events_for(self, packet_ids: Iterable[str]) -> Dict[str, List[Dict[str, Any]]]:
        """每张审批单的操作记录，按时间先后排列。"""
        ids = list(packet_ids)
        if not ids:
            return {}
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT packet_id, action, detail, source, created_at FROM influencer_events"
                f" WHERE packet_id IN ({_placeholders(len(ids))}) ORDER BY created_at, id",
                ids,
            ).fetchall()
        events: Dict[str, List[Dict[str, Any]]] = {packet_id: [] for packet_id in ids}
        for row in rows:
            event = dict(row)
            events[event.pop("packet_id")].append(event)
        return events

    # ---- 待运营处理的事项 ----

    def add_inbox_item(self, chat_id: str, kind: str, content: str, *, auto_replied: bool = False) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO influencer_inbox (chat_id, kind, content, auto_replied, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (chat_id, kind, content, int(auto_replied), time.time()),
            )
        return cur.lastrowid

    def last_auto_reply_at(self, chat_id: str, kind: str = INBOX_MESSAGE) -> Optional[float]:
        """这位达人最近一次因为某类事项收到机器人自动回复的时间；没回过返回 None。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(created_at) AS t FROM influencer_inbox"
                " WHERE chat_id = ? AND kind = ? AND auto_replied = 1",
                (chat_id, kind),
            ).fetchone()
        return row["t"]

    def list_inbox(self, *, handled: bool = False, limit: int = 200) -> List[Dict[str, Any]]:
        """待处理（或已处理）的事项，从新到旧，带上名单里的显示名。"""
        condition = "i.handled_at IS NOT NULL" if handled else "i.handled_at IS NULL"
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT i.*, COALESCE(c.display_name, '') AS display_name FROM influencer_inbox AS i"
                " LEFT JOIN influencer_contacts AS c ON c.chat_id = i.chat_id"
                f" WHERE {condition} ORDER BY i.created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            item["auto_replied"] = bool(item["auto_replied"])
        return items

    def count_open_inbox(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM influencer_inbox WHERE handled_at IS NULL").fetchone()
        return row["n"]

    def mark_inbox_handled(self, item_id: int, *, source: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE influencer_inbox SET handled_at = ? WHERE id = ? AND handled_at IS NULL",
                (time.time(), item_id),
            )
            if cur.rowcount:
                row = conn.execute("SELECT chat_id FROM influencer_inbox WHERE id = ?", (item_id,)).fetchone()
                self._log(conn, "已人工处理", chat_id=row["chat_id"], detail=f"待处理事项 #{item_id}", source=source)
        return cur.rowcount == 1

    # ---- 对话 ----

    def add_dialog(self, chat_id: str, incoming: str, reply: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO influencer_dialog (chat_id, incoming, reply, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, incoming, reply, time.time()),
            )

    def recent_dialog(self, chat_id: str, *, limit: int = 20, since: float = 0) -> List[Dict[str, Any]]:
        """这位达人最近的几轮对话，从旧到新。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT incoming, reply, created_at FROM influencer_dialog"
                " WHERE chat_id = ? AND created_at >= ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (chat_id, since, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    # ---- 发送队列 ----

    def enqueue_outgoing(self, chat_id: str, text: str, *, kind: str = "", packet_id: str = "") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO influencer_outbox (chat_id, text, kind, packet_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, text, kind, packet_id, time.time()),
            )
        return cur.lastrowid

    def next_outgoing(self, policy: "SendPolicy", now: Optional[float] = None) -> Tuple[Optional[Dict[str, Any]], float, str]:
        """队列里最早的一条，以及现在能不能发：(这一条, 还要等几秒, 原因)。队列空时第一项是 None。"""
        now = time.time() if now is None else now
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM influencer_outbox WHERE sent_at IS NULL ORDER BY created_at, id LIMIT 1",
            ).fetchone()
            if row is None:
                return None, 0.0, ""
            last = conn.execute("SELECT MAX(sent_at) AS t FROM influencer_outbox").fetchone()["t"]
            hour = conn.execute(
                "SELECT COUNT(*) AS n, MIN(sent_at) AS oldest FROM influencer_outbox WHERE sent_at >= ?",
                (now - 3600,),
            ).fetchone()
        wait, reason = policy.wait_before_next_send(now, last, hour["n"], hour["oldest"])
        return dict(row), wait, reason

    def mark_outgoing_sent(self, item_id: int, now: Optional[float] = None) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE influencer_outbox SET sent_at = ? WHERE id = ? AND sent_at IS NULL",
                (time.time() if now is None else now, item_id),
            )
        return cur.rowcount == 1

    def count_unsent(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM influencer_outbox WHERE sent_at IS NULL").fetchone()[0]

    # ---- 原项目表里的达人私聊（B27） ----

    @staticmethod
    def _purge_influencer_chats(conn: sqlite3.Connection, cutoff: float) -> None:
        """名单里的达人（包括停用的）的私聊，在原项目各表里 cutoff 以前的内容都删掉：
        消息原文、图片语音索引、导入的聊天记录、机器人发出的话，以及由这些消息整理出的记忆。

        这些表是原项目建的，达人模块单独用一个库（测试）时可能不存在，不存在就跳过。
        消息表上的全文索引（fts_messages、fts_history）是外部内容索引，要先用 'delete'
        命令把已经编进索引的行去掉，否则索引里会留下残影。
        """
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        chats = "SELECT chat_id FROM influencer_contacts"

        def state(key: str) -> int:
            if "memory_state" not in tables:
                return 0
            row = conn.execute("SELECT value FROM memory_state WHERE key = ?", (key,)).fetchone()
            return int(row[0]) if row and str(row[0]).isdigit() else 0

        for table, index in (("messages", "fts_messages"), ("history_messages", "fts_history")):
            if table not in tables:
                continue
            where = f"chat_id IN ({chats}) AND timestamp < ?"
            if index in tables:
                conn.execute(
                    f"INSERT INTO {index}({index}, rowid, content)"
                    f" SELECT 'delete', id, content FROM {table} WHERE {where} AND id <= ?",
                    (cutoff, state(f"{index}.cursor")),
                )
            conn.execute(f"DELETE FROM {table} WHERE {where}", (cutoff,))
        for table, column in (("message_media", "timestamp"), ("bot_replies", "timestamp"),
                              ("group_facts", "updated_at"), ("group_memory", "updated_at")):
            if table in tables:
                conn.execute(f"DELETE FROM {table} WHERE chat_id IN ({chats}) AND {column} < ?", (cutoff,))

        # 记忆 v2：片段按结束时间、事实按最后一次出现的时间；连同引用的原话和向量一起删
        owners = []
        if "memory_episodes" in tables:
            owners.append(("episode", "memory_episodes", "end_ts"))
        if "memory_facts" in tables:
            owners.append(("fact", "memory_facts", "last_seen_at"))
        for kind, table, column in owners:
            old = f"SELECT id FROM {table} WHERE chat_id IN ({chats}) AND {column} < ?"
            for extra in ("memory_evidence", "memory_vectors"):
                if extra in tables:
                    conn.execute(
                        f"DELETE FROM {extra} WHERE owner_kind = ? AND owner_id IN ({old})", (kind, cutoff),
                    )
            conn.execute(f"DELETE FROM {table} WHERE chat_id IN ({chats}) AND {column} < ?", (cutoff,))
        if "memory_evidence" in tables:
            # 还在用的记忆里，引用的旧原话也删掉
            for kind, table, _ in owners:
                conn.execute(
                    f"DELETE FROM memory_evidence WHERE owner_kind = ? AND ts < ?"
                    f" AND owner_id IN (SELECT id FROM {table} WHERE chat_id IN ({chats}))",
                    (kind, cutoff),
                )

    # ---- 概览 ----

    def overview(self, now: Optional[float] = None) -> Dict[str, int]:
        """控制台「今日概览」和晨报用的数字，全部来自数据库（B13：不再写死假数据）。
        "近 24 小时"按提交、处理的时间算。"""
        now = time.time() if now is None else now
        day_ago = now - 24 * 3600
        pending = GateStatus.PENDING.value
        with self._connect() as conn:
            def count(sql: str, *params: Any) -> int:
                return conn.execute(sql, params).fetchone()[0]

            def handled_since(status: GateStatus) -> int:
                return count(
                    "SELECT COUNT(*) FROM influencer_approvals WHERE status = ? AND handled_at >= ?",
                    status.value, day_ago,
                )

            return {
                "pending": count("SELECT COUNT(*) FROM influencer_approvals WHERE status = ?", pending),
                "expiring_soon": count(
                    "SELECT COUNT(*) FROM influencer_approvals WHERE status = ? AND expires_at < ?",
                    pending, now + 12 * 3600,
                ),
                "executing": count(
                    "SELECT COUNT(*) FROM influencer_approvals WHERE status = ?", GateStatus.EXECUTING.value,
                ),
                "inbox": count("SELECT COUNT(*) FROM influencer_inbox WHERE handled_at IS NULL"),
                "submitted_24h": count("SELECT COUNT(*) FROM influencer_approvals WHERE created_at >= ?", day_ago),
                "fulfilled_24h": handled_since(GateStatus.FULFILLED),
                "rejected_24h": handled_since(GateStatus.REJECTED),
                "failed_24h": handled_since(GateStatus.FAILED),
                "out_of_stock_24h": count(
                    "SELECT COUNT(*) FROM influencer_inbox WHERE kind = ? AND created_at >= ?",
                    INBOX_OUT_OF_STOCK, day_ago,
                ),
                "contacts": count("SELECT COUNT(*) FROM influencer_contacts WHERE enabled = 1"),
            }

    # ---- 维护 ----

    def run_maintenance(self, now: Optional[float] = None) -> None:
        """把超过有效期的待审批单标成过期；清除到期的个人信息（Q3）；删掉作废的草稿，
        以及处理完 90 天的待处理事项、90 天前的对话记录和发出 90 天的消息；
        原项目的表里达人私聊的消息和由它们整理出来的记忆，也按 90 天清除（B27）。

        机器人启动时、控制台每次列审批单时都会调用，所以不需要单独的定时任务。
        """
        now = time.time() if now is None else now
        finished = [status.value for status in FINISHED_STATUSES]
        with self._connect() as conn:
            overdue = conn.execute(
                "SELECT packet_id, influencer_id FROM influencer_approvals WHERE status = ? AND expires_at < ?",
                (GateStatus.PENDING.value, now),
            ).fetchall()
            for row in overdue:
                cur = conn.execute(
                    "UPDATE influencer_approvals SET status = ?, handled_at = expires_at"
                    " WHERE packet_id = ? AND status = ?",
                    (GateStatus.EXPIRED.value, row["packet_id"], GateStatus.PENDING.value),
                )
                if cur.rowcount:
                    self._log(
                        conn, "过期", packet_id=row["packet_id"], chat_id=row["influencer_id"],
                        detail="超过有效期没有审批", source=SOURCE_AUTO,
                    )
            conn.execute(
                "UPDATE influencer_approvals SET recipient_name = '', recipient_phone = '',"
                " shipping_address = '', evidence = '', personal_info_cleared_at = ?"
                f" WHERE personal_info_cleared_at IS NULL AND status IN ({_placeholders(len(finished))})"
                " AND handled_at < ?",
                (now, *finished, now - PERSONAL_INFO_RETENTION_SECONDS),
            )
            conn.execute("DELETE FROM influencer_drafts WHERE updated_at < ?", (now - DRAFT_TTL_SECONDS,))
            conn.execute(
                "DELETE FROM influencer_inbox WHERE handled_at IS NOT NULL AND handled_at < ?",
                (now - PERSONAL_INFO_RETENTION_SECONDS,),
            )
            conn.execute(
                "DELETE FROM influencer_dialog WHERE created_at < ?", (now - PERSONAL_INFO_RETENTION_SECONDS,),
            )
            conn.execute(
                "DELETE FROM influencer_outbox WHERE sent_at IS NOT NULL AND sent_at < ?",
                (now - PERSONAL_INFO_RETENTION_SECONDS,),
            )
            self._purge_influencer_chats(conn, now - PERSONAL_INFO_RETENTION_SECONDS)
