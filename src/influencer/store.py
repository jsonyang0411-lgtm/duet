"""达人模块的数据存储（第 6 项，B5）：达人名单（档位、主播类型、平台 UID）、待处理事项、操作记录。

寄样审批单（influencer_approvals）2026-10-27 起不再写入：寄样单改走 PRM 的平台动作 jst.sample_order。
老库里的审批单和它的操作记录按 90 天删掉（run_maintenance），里面的收件信息跟着删。

和机器人共用一个 SQLite 文件（默认 data/messages.db）。机器人和控制台都只通过这里
读写，不共享内存对象。表名都以 influencer_ 开头，建表不经过原项目的 src/db/schema.py。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .settings import DRAFT_TTL_SECONDS, PERSONAL_INFO_RETENTION_SECONDS, influencer_chat_ids_from_env

logger = logging.getLogger(__name__)

# 操作记录里的"来源"
SOURCE_CONSOLE = "控制台"
SOURCE_ENV = "环境变量"

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

-- 以前规则对话里还没补齐的索样申请，现在不再写入（寄样信息在 PRM 合作单上）。老库里的到期删除
CREATE TABLE IF NOT EXISTS influencer_drafts (
    chat_id    TEXT PRIMARY KEY,
    draft_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

-- 寄样审批单：2026-10-27 起不再写入（寄样单走 PRM 的 jst.sample_order）。老库里的 90 天后删除
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

-- 和达人的对话：以前交给 Claude 直接回复时用，现在不再写入（达人的私聊走 PRM）。
-- 老库里的可能有收件信息，照样 90 天后删除（D10）
CREATE TABLE IF NOT EXISTS influencer_dialog (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    TEXT    NOT NULL,
    incoming   TEXT    NOT NULL,
    reply      TEXT    NOT NULL DEFAULT '',
    created_at REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_influencer_dialog_chat
    ON influencer_dialog(chat_id, created_at DESC);

-- 以前主动发给达人的消息的发送队列，现在不再写入（通知走 PRM 待审批）。老库里的 90 天后删除
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

# 待处理事项的种类。老库里还有 message / out_of_stock / commission（以前的审批那条路写的）
INBOX_DRAFT = "draft"  # Mac 端（WECHAT_BACKEND=edge）：机器人想回的话，没发出，等运营审核（D19）


# 建表以后加的列：老库启动时补上（ALTER TABLE ... ADD COLUMN），新库也靠这里加，SCHEMA_SQL 不再改
_ADDED_COLUMNS = {
    "influencer_contacts": (
        ("platform", "TEXT NOT NULL DEFAULT ''"),        # DY / XHS
        ("platform_no", "TEXT NOT NULL DEFAULT ''"),     # 抖音号 / 小红书号（运营在名单里填）
        ("tier", "TEXT NOT NULL DEFAULT ''"),            # 下面几项是按号码从聚水潭查来的（tiers.py）
        ("channel", "TEXT NOT NULL DEFAULT ''"),
        ("sales_label", "TEXT NOT NULL DEFAULT ''"),
        ("level", "INTEGER"),
        ("profile_note", "TEXT NOT NULL DEFAULT ''"),    # 查不到时的原因
        ("profile_checked_at", "REAL"),
        ("platform_uid", "TEXT NOT NULL DEFAULT ''"),    # 平台的达人 UID（聚水潭查来的；开抖音专属佣金要用）
    ),
    "influencer_approvals": (
        ("platform", "TEXT NOT NULL DEFAULT ''"),
        ("platform_no", "TEXT NOT NULL DEFAULT ''"),
        ("tier", "TEXT NOT NULL DEFAULT ''"),
        ("channel", "TEXT NOT NULL DEFAULT ''"),
        ("sales_label", "TEXT NOT NULL DEFAULT ''"),
        ("items_json", "TEXT NOT NULL DEFAULT ''"),
        ("live_session", "TEXT NOT NULL DEFAULT ''"),
    ),
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _placeholders(count: int) -> str:
    return ", ".join("?" * count)


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
            _add_missing_columns(conn)

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
        platform: Optional[str] = None,
        platform_no: Optional[str] = None,
    ) -> Dict[str, Any]:
        """新增或修改一位达人；传 None 的字段保持原样。返回改完后的记录。"""
        now = time.time()
        changes = {
            name: value for name, value in (
                ("display_name", display_name),
                ("xhs_account", xhs_account),
                ("platform", platform),
                ("platform_no", platform_no),
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
            if "platform" in changes or "platform_no" in changes:
                # 号码变了，原来查到的档位、主播类型作废，下一轮同步马上按新号码重查（prm_bridge）
                conn.execute(
                    "UPDATE influencer_contacts SET tier = '', channel = '', sales_label = '', level = NULL,"
                    " profile_note = '', profile_checked_at = NULL, platform_uid = '' WHERE chat_id = ?", (chat_id,),
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

    def record_daren_profile(
        self, chat_id: str, *, tier: str, channel: str, sales_label: str, level: Optional[int], note: str,
        uid: str = "", now: Optional[float] = None,
    ) -> None:
        """记下按号码从聚水潭查来的档位、主播类型、销售额、平台 UID（tiers.py）。查不到时 tier 为空，note 写原因。"""
        now = time.time() if now is None else now
        with self._connect() as conn:
            conn.execute(
                "UPDATE influencer_contacts SET tier = ?, channel = ?, sales_label = ?, level = ?,"
                " profile_note = ?, profile_checked_at = ?, platform_uid = ? WHERE chat_id = ?",
                (tier, channel, sales_label, level, note, now, uid, chat_id),
            )

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

    # ---- 操作记录 ----

    @staticmethod
    def _log(conn: sqlite3.Connection, action: str, *, source: str, chat_id: str = "", detail: str = "") -> None:
        conn.execute(
            "INSERT INTO influencer_events (chat_id, action, detail, source, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, action, detail, source, time.time()),
        )

    # ---- 待运营处理的事项 ----

    def add_inbox_item(self, chat_id: str, kind: str, content: str, *, auto_replied: bool = False) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO influencer_inbox (chat_id, kind, content, auto_replied, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (chat_id, kind, content, int(auto_replied), time.time()),
            )
        return cur.lastrowid

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

    # ---- 维护 ----

    def run_maintenance(self, now: Optional[float] = None) -> None:
        """清除到期的个人信息（Q3、D10）：老的寄样审批单（含收件人、电话、地址）和它们的操作记录、
        作废的草稿、处理完 90 天的待处理事项、90 天前的对话记录和发出 90 天的消息；
        原项目的表里达人私聊的消息和由它们整理出来的记忆，也按 90 天清除（B27）。

        机器人启动时、控制台每次读名单状态时都会调用，所以不需要单独的定时任务。
        """
        now = time.time() if now is None else now
        cutoff = now - PERSONAL_INFO_RETENTION_SECONDS
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM influencer_events WHERE packet_id IN"
                " (SELECT packet_id FROM influencer_approvals WHERE created_at < ?)", (cutoff,),
            )
            conn.execute("DELETE FROM influencer_approvals WHERE created_at < ?", (cutoff,))
            conn.execute("DELETE FROM influencer_drafts WHERE updated_at < ?", (now - DRAFT_TTL_SECONDS,))
            conn.execute("DELETE FROM influencer_inbox WHERE handled_at IS NOT NULL AND handled_at < ?", (cutoff,))
            conn.execute("DELETE FROM influencer_dialog WHERE created_at < ?", (cutoff,))
            conn.execute("DELETE FROM influencer_outbox WHERE created_at < ?", (cutoff,))
            self._purge_influencer_chats(conn, cutoff)


def open_store(db_path: str) -> InfluencerStore:
    """机器人启动时调用：打开数据库，把环境变量 INFLUENCER_CHAT_IDS 并进名单（只增不删，Q4），
    再做一次维护（清除到期的个人信息）。"""
    store = InfluencerStore(db_path)
    added = store.add_contacts(
        sorted(influencer_chat_ids_from_env()), source=SOURCE_ENV, note="来自环境变量 INFLUENCER_CHAT_IDS",
    )
    if added:
        logger.info("[达人模块] 从环境变量把 %d 位达人加进了名单", len(added))
    store.run_maintenance()
    return store
