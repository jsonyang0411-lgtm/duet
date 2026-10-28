"""PRM copilot (``PRM_COPILOT=on``): a registered creator's private messages get drafted
replies on the Mac, and an operator decides in the console what is sent.

The flow, all of it silent towards the creator::

    creator writes ──► route step ``prm`` ──► ``handle``: phone/ID numbers become
    placeholders, the text (and any picture or file) goes into a ``prm`` job of the
    Claude relay (src/relay; a burst of messages is one job) ──► the Mac worker drafts
    in its locked-down workspace ──► ``on_result``: the JSON is checked
    (result.py) and becomes a packet ──► the console: send / edit and send / drop, or
    the kind's gate is ``auto`` (8.1, gates.py) ──► the sender thread puts the numbers
    back and sends through the WeChat backend (text first, then the material files).

A person takes over a chat (``mode`` = human for ``PRM_TAKEOVER_MIN``) when the
operators reply from this WeChat account themselves (the backend hands those lines to
``on_own_message``; the bot's own lines are recognised and confirm their packet
instead), or from the console; paused needs the console to end it.  Meanwhile the
creator's messages are only recorded; the next draft job is told what both sides
said (``history``).  A draft left waiting ``PRM_HOLDING_MIN`` minutes gets one
holding reply per round of the creator's messages (none when she was answered in that round
already: an operator's own line, or a reply sent after her message).  ``PRM_RETENTION_DAYS`` after a
deal is done (delivered, or quiet that long) its receiving details are cleared everywhere (8.2, retention.py),
the Mac's case file through a ``purge`` job.  The deal actions (stage, mode,
shipment) are in deals.py.

Registering is always an operator's decision.  When the creator registered is a new friend
(src/wechat/new_friends.py found the line WeChat writes when two people become friends), her
messages since she was added, friend-request greeting included, become one draft job that waits
for a person (``take_new_friend``).  WeChat's friend notices (「我通过了你的朋友验证请求…」 in her
name, 「你已添加了…」) are never drafted.

A draft with a platform action (platforms.py: 小红书 / 抖音 / 淘宝 CLIs on the Mac) comes
with the Mac's preview of it and always waits for a person, whatever its kind's gate; so does
every draft of a next step the bot asked for (a 专属佣金 step, ``meta.system_note``).
批准并执行 moves it to ``executing`` and queues a ``platform_execute`` job (one approval
key; the Mac runs the CLI with --execute once and never again for that key); only a
read-back ``VERIFIED_SUCCESS`` turns it into an approved reply (the link filled in) for
the sender thread.  Anything else, a job the Mac never took within APPROVED_EXPIRE_SEC,
or one lost for PLATFORM_LOST_GRACE_SEC, ends ``failed``: nothing goes to the creator
and the console shows it red until an operator marks it dealt with.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from ..relay.attachments import KINDS as ATTACHMENT_KINDS, Held, attachment_kind
from ..relay.service import MAX_AGE_SEC, job_meta
from ..relay.store import PRM
from ..wechat.new_friends import is_friend_notice
from . import deals, gates, pii, platforms, retention
from .result import MAX_DRAFT_CHARS, MESSAGE_KINDS, DraftError, parse_draft
from .store import (
    APPROVED, COPILOT, DROPPED, EXECUTING, FAILED, HUMAN, LANE_REPLY, LANE_SYSTEM, MESSAGE_TOOL, MODES, NO_REPLY,
    PAUSED, PENDING,
    SENDING, SENT, SHIPMENT_STATES, STAGES, SUPERSEDED, PartyError, PrmStore,
)

logger = logging.getLogger(__name__)

SETTLE_SEC = 8               # messages within this time of each other are one job
OUTBOUND_TOLD = 6            # a draft job is told the deal's last this many replies (what really went out)
OUTBOUND_CHARS = 500
APPROVED_EXPIRE_SEC = 1800   # an approved draft not sent by then (the bot was stopped) is not sent
SEND_TICK_SEC = 5
OWN_MATCH_SEC = 1800         # our own line seen in WeChat within this long after sending is the bot's
OWN_FILE_SEC = 600           # an own picture/file this soon after the bot sent one is the bot's
READ_BACK_SEC = 120          # our line matched in WeChat and read again this soon after: the same send
RETENTION_CHECK_SEC = 3600
PLATFORM_LOST_GRACE_SEC = 1800   # an execution whose Mac went silent this long is given up on (red)
PLATFORM_OPS = ("platform_execute", "platform_preview")
HOLDING_TEXT = "收到，我确认一下马上回你"
# Mac 重启以前发的、后来 Windows 才报失败的：没有发送时间时往回找这么久（edge 后端的发送记录留一天）
RESTART_FAIL_SEC = 86400
CATCHUP_HOURS = 12           # PRM_CATCHUP_HOURS: a message read late is drafted up to this old (0-72)
OWN_FILE_EARLY_SEC = 60      # a file the bot sent shows in WeChat up to this long before its send returns
_QUOTE_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9]+:\s*")
_WXID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{3,63}$")
_VOICE = re.compile(r"^\[语音\]\s*(.*)$", re.S)


def _queued(msg: dict) -> bool:
    """Windows 的微信边缘排队转来的（Mac 端 edge 后端，带 edge_event_id）：Windows 只转它运行以后收到的、
    Mac 没确认的事件（按编号续读，不会重放历史），所以 Mac 离线多久再取都不是历史，不按 MAX_AGE_SEC 丢。
    PRM_CATCHUP_HOURS 大于 0 时它和本机读到的消息一样按补读的规矩走（晚到的起草但一律先审，太旧的只记下），
    等于 0 时照旧不看年龄。"""
    return msg.get("edge_event_id") not in (None, "")


def check_wxid(wxid: str, reserved: set) -> str:
    """The wxid as registered, or PartyError (in Chinese) why it cannot be a creator."""
    wxid = str(wxid or "").strip()
    if not wxid:
        raise PartyError("要填达人的 wxid")
    if wxid.endswith("@chatroom"):
        raise PartyError("这是群聊的 id，达人要登记私聊的 wxid")
    if not _WXID.match(wxid):
        raise PartyError("wxid 只能是字母开头的字母、数字、下划线或短横线（形如 wxid_xxx）")
    if wxid in reserved:
        raise PartyError("这是管理员或能用 cc 中转的人，不能同时登记成达人")
    return wxid


def message_text(msg: dict) -> str:
    """What a message says, as one line for the draft (pictures and files by name)."""
    try:
        msg_type = int(msg.get("msg_type", 1) or 1)
    except (TypeError, ValueError):
        msg_type = 1
    content = _QUOTE_PREFIX.sub("", str(msg.get("content", "") or "")).strip()
    kind = attachment_kind(msg)
    if kind == "image":
        return "[图片]"
    if kind == "file":
        return f"[文件] {str(msg.get('file_name') or '').strip()}"
    if msg_type == 34:
        voice = _VOICE.match(content)
        heard = voice.group(1).strip() if voice else ""
        return f"[语音] {heard}" if heard else "[语音]（没转出文字）"
    if msg_type == 47:
        return "[表情]"
    if msg_type == 43:
        return "[视频]"
    return content or "[消息]"


def _norm(text: str) -> str:
    return " ".join(str(text or "").split())


def _clock_text(at: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(at))


def late_note(earliest: float) -> str:
    """The deal's attention for a draft of messages read late (PRM_CATCHUP_HOURS)."""
    return f"这次的消息是机器人没在读微信时收到的（最早 {_clock_text(earliest)}），草稿要人看过再发"


def missed_note(count: int, earliest: float) -> str:
    """The deal's attention for messages too old to draft (older than PRM_CATCHUP_HOURS when read)."""
    return f"{count} 条消息在机器人没在读微信时收到（最早 {_clock_text(earliest)}），没有起草，请看一下"


def new_friend_note(count: int, earliest: float) -> str:
    """The deal's attention for the draft of a new friend's messages from before she was registered."""
    return (f"新加的好友，登记前发来的 {count} 条消息（最早 {_clock_text(earliest)}，含加好友时的验证消息）"
            "起草了这份草稿，要人看过再发")


def gap_notes(meta: dict) -> list[str]:
    """What a draft job's ``meta`` says about messages read late (``late``), recorded without a
    draft (``missed``) or written by a new friend before she was registered (``new_friend``), as
    attention lines (any of them: the draft waits for a person)."""
    notes = []
    late, missed, new_friend = meta.get("late"), meta.get("missed"), meta.get("new_friend")
    if isinstance(late, dict) and late.get("from"):
        notes.append(late_note(float(late["from"])))
    if isinstance(missed, dict) and missed.get("from"):
        notes.append(missed_note(int(missed.get("count") or 1), float(missed["from"])))
    if isinstance(new_friend, dict) and new_friend.get("from"):
        notes.append(new_friend_note(int(new_friend.get("count") or 1), float(new_friend["from"])))
    return notes


def _event_time(ev: dict) -> float:
    """When a recorded message was written (its own time when kept, else when it was recorded)."""
    try:
        return float((ev.get("detail") or {}).get("at") or ev["at"])
    except (TypeError, ValueError):
        return float(ev["at"])


def _later(deal: dict, at: float) -> float:
    """The deal's ``last_message_at`` after a message written at ``at`` (it never goes back)."""
    return max(float(deal.get("last_message_at") or 0), at)


EVENT_NAMES = {
    "stage": "改阶段", "mode": "改模式", "mode_expired": "人工接管到期", "takeover": "运营亲手回复，自动接管",
    "operator_message": "运营亲手回复", "creator_message": "接管期间达人的消息", "shipment": "填运单号",
    "shipment_status": "更新物流", "holding_reply": "自动回复「稍等」", "reveal": "查看完整号码",
    "wrong": "标为发错了", "purge": "清除收件信息", "purge_case": "清除 Mac 案卷里的收件信息",
    "platform_execute": "批准执行平台动作", "platform_result": "平台动作结果",
    "platform_preview": "重新预览平台动作", "platform_ack": "平台动作已处理",
}
# A creator's message recorded without a draft because it was read too late (PRM_CATCHUP_HOURS).
MISSED_EVENT_NAME = "机器人没在读时达人的消息（没有起草）"


def event_out(ev: dict) -> dict:
    """An event for the console: what happened, by whom, when (texts kept as stored: masked)."""
    detail = dict(ev.get("detail") or {})
    name = (MISSED_EVENT_NAME if ev["kind"] == "creator_message" and detail.get("missed")
            else EVENT_NAMES.get(ev["kind"], ev["kind"]))
    return {"id": ev["id"], "kind": ev["kind"], "name": name,
            "actor": ev.get("actor", ""), "at": ev["at"], "detail": detail}


def platform_out(p: dict) -> Optional[dict]:
    """A packet's platform action for the console (None for a plain reply): what it will do,
    the Mac's preview, the execution and whether it can be approved now."""
    if p.get("tool", MESSAGE_TOOL) == MESSAGE_TOOL:
        return None
    data = p.get("platform") or {}
    action = data.get("action")
    preview = data.get("preview") or {}
    execution = data.get("execution") or None
    verdict = preview.get("verdict") or "unavailable"
    name = (action or {}).get("action") or p.get("tool")
    return {
        "name": name, "label": platforms.label(name), "platform": (platforms.ACTIONS.get(name) or {}).get("platform"),
        "effect": (platforms.ACTIONS.get(name) or {}).get("effect", ""),
        "caveat": (platforms.ACTIONS.get(name) or {}).get("caveat", ""),
        "makes_link": bool((platforms.ACTIONS.get(name) or {}).get("link")),
        "placeholders": [{"name": k, "label": platforms.PLACEHOLDER_LABELS.get(k, k)}
                         for k in platforms.REPLY_FIELDS.get(name, {})],
        "params": platforms.describe(action, preview.get("catalog")),
        "requested": preview.get("requested"),
        "verdict": verdict, "verdict_label": platforms.VERDICTS.get(verdict, verdict),
        "problems": preview.get("problems") or [], "facts": preview.get("facts") or [],
        "steps": preview.get("steps") or [], "previewed_at": preview.get("previewed_at"),
        "previewing": bool(data.get("previewing")),
        "can_execute": p["status"] == PENDING and verdict == "ready" and bool(action) and not data.get("previewing"),
        "approved_at": data.get("approved_at"), "job_id": data.get("job_id"), "lost_at": data.get("lost_at"),
        "execution": None if execution is None else {
            "outcome": execution.get("outcome"),
            "outcome_label": platforms.outcome_text(execution.get("outcome") or "UNKNOWN"),
            "problems": execution.get("problems") or [], "facts": execution.get("facts") or [],
            "created": execution.get("created") or {}, "steps": execution.get("steps") or [],
            "late": bool(execution.get("late")), "finished_at": execution.get("finished_at"),
        },
        "acked_at": data.get("acked_at"),
    }


def gates_out(store: PrmStore) -> list[dict]:
    out = []
    for kind, gate in store.gates().items():
        stats = gates.window_stats(store.reviewed(kind, gate["reset_at"], gate["window"]))
        out.append({"kind": kind, "label": MESSAGE_KINDS.get(kind, kind), "route": gate["route"],
                    "locked": bool(gate["locked"]), "window": gate["window"], "min_unchanged": gate["min_unchanged"],
                    **stats})
    return out


def snapshot(store: PrmStore, *, running: bool, extra: Optional[dict] = None, limit: int = 40,
             retention_days: int = retention.RETENTION_DAYS, now: Optional[float] = None) -> dict:
    """What the console shows (GET /api/prm); also built from the database when the bot is stopped.
    Numbers appear only masked (``legend``); the full ones need ``reveal``."""
    now = time.time() if now is None else now
    parties = {p["id"]: p for p in store.parties(include_inactive=True)}
    newest = {}
    for deal in store.deals():
        newest[deal["party_id"]] = deal                # the newest deal of each creator
    pending = list(reversed(store.packets((PENDING, EXECUTING, APPROVED, SENDING), limit=200)))
    recent = store.packets((SENT, FAILED, DROPPED, SUPERSEDED, NO_REPLY), limit=limit)
    shown_cache: dict[int, dict] = {}

    def shown(party_id: int) -> dict:
        if party_id not in shown_cache:
            shown_cache[party_id] = pii.display(store.token_rows(party_id))
        return shown_cache[party_id]

    def packet_out(p: dict) -> dict:
        party = parties.get(p["party_id"]) or {}
        final = p["final_text"] if p.get("final_text") is not None else p["text"]
        return {
            "id": p["id"], "deal_id": p["deal_id"], "party_id": p["party_id"],
            "party_name": party.get("name", "?"), "job_id": p["job_id"], "status": p["status"],
            "kind": p["kind"], "kind_label": MESSAGE_KINDS.get(p["kind"], p["kind"]), "route": p["route"],
            "text": p["text"], "final_text": p.get("final_text"), "edited": p["edited"], "wrong": p["wrong"],
            "legend": pii.legend("\n".join([p["text"], final, p["intent"], p["reason"],
                                            json.dumps(p["slots"], ensure_ascii=False),
                                            json.dumps((p.get("platform") or {}).get("action") or {},
                                                       ensure_ascii=False)]), shown(p["party_id"])),
            "attachments": [{"name": a.get("name"), "size": a.get("size")} for a in p["attachments"]],
            "intent": p["intent"], "slots": p["slots"], "stage_proposal": p["stage_proposal"],
            "stage_proposal_label": STAGES.get(p["stage_proposal"], ""),
            "needs_attention": p["needs_attention"], "reason": p["reason"],
            "waybill": p["meta"].get("waybill", ""), "system": p["meta"].get("source") == "system",
            "outcome": p["outcome"], "error": p["error"],
            "created_at": p["created_at"], "decided_at": p["decided_at"], "sent_at": p["sent_at"],
            "tool": p.get("tool", MESSAGE_TOOL), "platform": platform_out(p),
        }

    def party_out(p: dict) -> dict:
        deal = newest.get(p["id"])
        if not deal:
            return {"id": p["id"], "wxid": p["wxid"], "name": p["name"], "note": p["note"],
                    "active": bool(p["active"]), "authorized_at": p["authorized_at"], "deal": None}
        mode = deals.effective_mode(deal, now)
        shipment = store.latest_shipment(deal["id"])
        slots = deal.get("slots", {})
        return {
            "id": p["id"], "wxid": p["wxid"], "name": p["name"], "note": p["note"],
            "active": bool(p["active"]), "authorized_at": p["authorized_at"],
            "has_numbers": bool(store.token_rows(p["id"])),       # cleared ones too: shown as （已清除）
            "deal": {
                "id": deal["id"], "stage": deal["stage"], "stage_label": STAGES.get(deal["stage"], ""),
                "mode": mode, "mode_label": MODES.get(mode, mode),
                "mode_until": deal.get("mode_until") if mode == HUMAN else None,
                "slots": slots, "legend": pii.legend(json.dumps(slots, ensure_ascii=False), shown(p["id"])),
                "stage_proposal": deal["stage_proposal"],
                "stage_proposal_label": STAGES.get(deal["stage_proposal"], ""),
                "attention": deal.get("attention", ""), "last_message_at": deal.get("last_message_at"),
                "delivered_at": deal.get("delivered_at"), "purged_at": deal.get("purged_at"),
                "purge_due_at": retention.purge_due_at(deal, retention_days),
                "shipment": {k: shipment[k] for k in ("carrier", "waybill", "status", "updated_at")}
                if shipment else None,
                "shipment_status_label": SHIPMENT_STATES.get((shipment or {}).get("status", ""), ""),
                "events": [event_out(e) for e in store.events(deal["id"], limit=12)],
            },
        }

    return {
        "ok": True, "running": running,
        "parties": [party_out(p) for p in parties.values()],
        "pending": [packet_out(p) for p in pending],
        "recent": [packet_out(p) for p in recent],
        "platform_alerts": [packet_out(p) for p in store.platform_failures()],
        "platform_actions": {name: spec["label"] for name, spec in platforms.ACTIONS.items()},
        "counts": store.counts(),
        "gates": gates_out(store),
        "stages": STAGES, "kinds": MESSAGE_KINDS, "modes": MODES, "shipment_states": SHIPMENT_STATES,
        **(extra or {}),
    }


class PrmService:
    def __init__(self, config, relay, *, store: Optional[PrmStore] = None,
                 send: Optional[Callable[[str, str], bool]] = None,
                 send_file: Optional[Callable[[str, str], bool]] = None,
                 clock: Callable[[], float] = time.time,
                 on_parties_changed: Optional[Callable[[], None]] = None,
                 scrub_messages: Optional[Callable[[str, Callable[[str], str]], int]] = None):
        self._config = config
        self._relay = relay
        self._store = store if store is not None else PrmStore(config.prm_db)
        self._store.seed_gates(gates.DEFAULT_GATES)
        self._send = send
        self._send_file = send_file
        self._clock = clock
        self.on_parties_changed = on_parties_changed
        # on_registered(wxid) -> bool: a creator was registered; True when her earlier messages are
        # on their way (a new friend: src/wechat/new_friends.py, back through take_new_friend).
        self.on_registered: Optional[Callable[[str], bool]] = None
        self._scrub_messages = scrub_messages
        self._takeover_sec = int(getattr(config, "prm_takeover_min", 120) or 120) * 60
        # PRM_CATCHUP_HOURS: a message read more than MAX_AGE_SEC late is drafted (always reviewed)
        # up to this old, only recorded beyond it; 0 = older than MAX_AGE_SEC is history.
        try:
            catchup_hours = int(getattr(config, "prm_catchup_hours", CATCHUP_HOURS))
        except (TypeError, ValueError):
            catchup_hours = CATCHUP_HOURS
        self._catchup_sec = min(max(catchup_hours, 0), 72) * 3600
        # One creator's message at a time: messages read at once (a catch-up after a restart,
        # a burst) find the job the one before made, so they are one job.
        self._handle_lock = threading.Lock()
        self._holding_sec = int(getattr(config, "prm_holding_min", 10) or 0) * 60
        self._holding_text = str(getattr(config, "prm_holding_text", "") or HOLDING_TEXT).strip() or HOLDING_TEXT
        self._retention_days = int(getattr(config, "prm_retention_days", retention.RETENTION_DAYS)
                                   or retention.RETENTION_DAYS)
        # created | delivered: when a sample order's 专属佣金 is drafted (next_step_note)
        self._commission_after = str(getattr(config, "prm_commission_after", "delivered") or "delivered")
        # 批准并执行: checking that a 专属 action is not open already and starting it is one step
        self._approve_lock = threading.Lock()
        # The admin and the cc users are never creators: their messages stay the relay's.
        self._reserved = {str(w).strip() for w in [getattr(config, "admin_wxid", ""),
                                                    *(getattr(config, "claude_relay_wxids", None) or [])]
                          if str(w).strip()}
        self._wxids = self._store.active_wxids()
        # What the bot itself sent lately, per chat: (normalized text, when, packet id); the last
        # file send (when, packet id).
        self._sent_lines: dict[str, deque] = {}
        self._sent_files: dict[str, tuple] = {}
        # Per chat, our own lines matched lately (normalized text -> when): read again within
        # READ_BACK_SEC they are the same send read back a second time (_match_own).
        self._read_back: dict[str, dict[str, float]] = {}
        self._sent_lock = threading.Lock()
        self._started = clock()               # what this run sent is in _sent_lines / _sent_files
        self._last_retention = 0.0
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        relay.register_kind(PRM, self.on_result)

    @property
    def store(self) -> PrmStore:
        return self._store

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Settle what a stopped bot left half done, then the sender thread."""
        now = self._clock()
        for p in self._store.packets((SENDING,), limit=500):
            self._store.move(p["id"], (SENDING,), FAILED, outcome="indeterminate", sent_at=now,
                             error="发送时机器人停了，不确定发出去没有；请到微信里看一眼，需要就手动补发")
        self._thread = threading.Thread(target=self._loop, name="prm-sender", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    # ── modes ─────────────────────────────────────────────────────────────

    def _mode(self, deal: dict, now: float) -> str:
        """The deal's mode now; a takeover whose time is up turns back to copilot (recorded)."""
        mode = deals.effective_mode(deal, now)
        if mode == COPILOT and deal.get("mode") == HUMAN:
            self._store.update_deal(deal["id"], now, mode=COPILOT, mode_until=None)
            self._store.add_event(deal["id"], deal["party_id"], "mode_expired", {}, actor="system", now=now)
        return mode

    # ── from WeChat (the route step) ──────────────────────────────────────

    def is_party_chat(self, chat_id: str) -> bool:
        """Is this private chat a registered creator's? (the WeChat backend reads such chats)"""
        if str(chat_id) not in self._wxids:
            self._wxids = self._store.active_wxids()
        return str(chat_id) in self._wxids

    def _mask(self, party_id: int, text: str, now: float) -> str:
        return pii.mask(text, lambda k, label, value: self._store.token_for(party_id, k, label, value, now))

    def handle(self, msg: dict) -> bool:
        """True when this is a registered creator's private message (it is then dealt with, silently).

        A message read more than MAX_AGE_SEC after it was written came in while the bot was not
        reading WeChat (stopped, updated, the database re-opened; or this PC's clock is off): up
        to PRM_CATCHUP_HOURS old it is drafted as usual but the draft always waits for a person
        (``meta.late``); older, it is only recorded and the deal asks a person to look
        (``_missed``).  PRM_CATCHUP_HOURS=0: older than MAX_AGE_SEC is history, as before (a
        message Windows' edge queued, ``_queued``, never is: drafted as it always was)."""
        if msg.get("is_group", True):
            return False
        chat_id = str(msg.get("chat_id", ""))
        if chat_id not in self._wxids:
            return False
        party = self._store.party_by_wxid(chat_id)
        if party is None:
            return False
        if is_friend_notice(msg):
            # 「我通过了你的朋友验证请求，现在我们可以开始聊天了」 and 「你已添加了…」: WeChat's, not hers
            logger.info("PRM: a friend notice in party %d's chat; not drafted", party["id"])
            return True
        self._take(msg, party, chat_id)
        return True

    def _take(self, msg: dict, party: dict, chat_id: str) -> None:
        """``handle`` for a registered creator's message.  What is recorded and the job it joins
        are decided under ``_handle_lock``; a picture or file is fetched outside it (that may wait
        for the WeChat window, and the backend's own lines wait for the lock)."""
        now = self._clock()
        sent_at = float(msg.get("timestamp", 0) or 0)
        age = now - sent_at if sent_at else 0.0
        late = age > MAX_AGE_SEC
        if late and not self._catchup_sec:
            if not _queued(msg):
                logger.info("PRM: an old message of party %d (%.0f s) is history, not drafted", party["id"], age)
                return
            late = False                   # Windows' queue never replays history: drafted as it always was
        at = sent_at or now
        line = message_text(msg)
        if late and age > self._catchup_sec:
            with self._handle_lock:
                self._missed(party, self._store.deal_of(party["id"], now), line, at, now)
            return
        with self._handle_lock:
            if self._recorded_for_a_person(party, line, at, now):
                return
        files: tuple = ()
        kind = attachment_kind(msg)
        if kind:
            collected, problems = self._relay.collect([Held(kind, msg, now)])
            files = tuple(collected)
            if problems:
                line += "（" + "；".join(problems) + "）"
            elif not files:
                line += f"（{ATTACHMENT_KINDS[kind]}没取到）"
        with self._handle_lock:
            if not self._recorded_for_a_person(party, line, at, now):    # (a person may have taken it meanwhile)
                self._submit(party, chat_id, line, files, at, age, late, now)

    def _recorded_for_a_person(self, party: dict, line: str, at: float, now: float) -> bool:
        """A person has the chat (taken over, paused): the message is recorded for the next draft,
        nothing queued; True then (under ``_handle_lock``)."""
        deal = self._store.deal_of(party["id"], now)
        mode = self._mode(deal, now)
        if mode == COPILOT:
            return False
        text = self._mask(party["id"], f"[{_clock_text(at)}] {line}", now)
        self._store.add_event(deal["id"], party["id"], "creator_message", {"text": text},
                              actor="creator", forwarded=False, now=now)
        self._store.update_deal(deal["id"], now, last_message_at=_later(deal, at))
        self._store.supersede(deal["id"], now, lane=LANE_REPLY)
        logger.info("PRM: party %d writes while the deal is %s; recorded, not drafted", party["id"], mode)
        return True

    def _submit(self, party: dict, chat_id: str, line: str, files: tuple, at: float, age: float,
                late: bool, now: float, new_friend: Optional[dict] = None) -> None:
        """The draft job of a creator's message (under ``_handle_lock``): the waiting drafts are
        out of date, the deal hears of the message, the job is made or joined.  ``new_friend``
        (``{"from", "count"}``, take_new_friend): a new friend's messages from before she was
        registered, one job of their own that waits for a person."""
        deal = self._store.deal_of(party["id"], now)
        text = self._mask(party["id"], f"[{_clock_text(at)}] {line}", now)
        stale = self._store.supersede(deal["id"], now, lane=LANE_REPLY)   # a 专属佣金 step stays
        self._store.update_deal(deal["id"], now, last_message_at=_later(deal, at))
        meta = {"party_id": party["id"], "deal_id": deal["id"], "name": party["name"]}
        outbound = self._outbound(deal["id"], party["id"], now)
        if outbound:
            meta["sent"] = outbound
        backlog = self._store.take_backlog(deal["id"])
        fresh = bool(backlog)
        if backlog:
            meta["history"] = [{"who": "operator" if ev["kind"] == "operator_message" else "creator",
                                "text": str(ev["detail"].get("text", "")),
                                **({"missed": True} if ev["detail"].get("missed") else {})} for ev in backlog]
            missed = [ev for ev in backlog if ev["kind"] == "creator_message" and ev["detail"].get("missed")]
            if missed:
                meta["missed"] = {"count": len(missed), "from": min(_event_time(ev) for ev in missed)}
        if late:
            # Read late: the draft always waits for a person (on_result).  A job of messages read
            # on time does not take it in; the next messages read late join this one.
            meta["late"] = {"from": at}
            if not fresh:
                open_job = self._relay.store.latest_open(chat_id, drafts_only=True, system=False)
                fresh = open_job is None or not job_meta(open_job).get("late")
            logger.warning("PRM: party %d's message was read %.0f min after it was written (the bot was not "
                           "reading WeChat, or this PC's clock is off); its draft waits for a person",
                           party["id"], age / 60)
        if new_friend:
            # Like ``late``: her messages from before the registration are one job, always reviewed.
            meta["new_friend"] = dict(new_friend)
            if not fresh:
                open_job = self._relay.store.latest_open(chat_id, drafts_only=True, system=False)
                fresh = open_job is None or not job_meta(open_job).get("new_friend")
        how, job_id = self._relay.submit(chat_id, chat_id, text, kind=PRM, files=files, settle_sec=SETTLE_SEC,
                                         meta=meta, fresh=fresh)
        logger.info("PRM: party %d message -> job #%d (%s)%s%s", party["id"], job_id, how,
                    f"; {stale} waiting draft(s) out of date" if stale else "",
                    f"; {len(backlog)} recorded line(s) told (a takeover, or read too late)" if backlog else "")

    def _missed(self, party: dict, deal: dict, line: str, at: float, now: float) -> None:
        """A creator's message older than PRM_CATCHUP_HOURS when it was read: too old to draft.
        It is recorded (the deal's history; the next draft job hears of it, ``take_backlog``),
        waiting drafts are out of date, and the deal asks a person to look (how many such
        messages wait, the earliest one's time)."""
        text = self._mask(party["id"], f"[{_clock_text(at)}] {line}", now)
        self._store.add_event(deal["id"], party["id"], "creator_message", {"text": text, "missed": True, "at": at},
                              actor="creator", forwarded=False, now=now)
        self._store.supersede(deal["id"], now, lane=LANE_REPLY)
        waiting = [ev for ev in self._store.backlog(deal["id"])
                   if ev["kind"] == "creator_message" and ev["detail"].get("missed")]
        earliest = min([_event_time(ev) for ev in waiting] or [at])
        self._store.update_deal(deal["id"], now, last_message_at=_later(deal, at),
                                attention=missed_note(len(waiting) or 1, earliest))
        logger.warning("PRM: party %d's message was read %.1f h after it was written (more than "
                       "PRM_CATCHUP_HOURS); recorded, not drafted, the deal asks a person to look",
                       party["id"], (now - at) / 3600)

    def _outbound(self, deal_id: int, party_id: int, now: float) -> list[dict]:
        """What the creator really got from us lately, oldest first, for the next draft: the Mac's
        case file holds the drafts as Claude wrote them, but an operator may have edited one
        before it went out, dropped one, or the send failed; holding replies are the bot's own.
        Told with every draft job (not once: a superseded job must not take it along); numbers
        masked like the creator's words."""
        items = []
        for p in self._store.outcomes(deal_id, limit=OUTBOUND_TOLD):
            final = p["final_text"] if p.get("final_text") is not None else p["text"]
            at = float(p.get("sent_at") or p.get("decided_at") or 0)
            item = {"at": at, "kind": p["kind"]}
            if p["status"] == SENT:
                item.update(what="sent", text=final)
                if p.get("edited") and str(p["text"]).strip() != str(final).strip():
                    item["draft"] = p["text"]
                if p.get("wrong"):
                    item["wrong"] = True
            elif p["status"] == DROPPED:
                item.update(what="dropped", draft=p["text"])
            else:
                item.update(what="failed", text=final)
            items.append(item)
        for ev in self._store.events(deal_id, limit=50):
            if ev["kind"] == "holding_reply":
                items.append({"at": float(ev["at"]), "kind": "holding", "what": "sent",
                              "text": str(ev["detail"].get("text") or self._holding_text)})
        items.sort(key=lambda i: i["at"])
        out = []
        for item in items[-OUTBOUND_TOLD:]:
            for key in ("text", "draft"):
                if key in item:
                    item[key] = self._mask(party_id, str(item[key]), now)[:OUTBOUND_CHARS]
            item["at"] = _clock_text(item["at"]) if item["at"] else ""
            out.append(item)
        return out

    def on_own_message(self, msg: dict) -> None:
        """A line this WeChat account sent in a creator's chat (the backend's hook).

        The bot's own line confirms its packet (``sent_confirmed``); anything else was typed
        by an operator on the phone or the PC: it is recorded for the next draft, and the
        deal goes to ``human`` for ``PRM_TAKEOVER_MIN`` (a new one extends it).  A line read
        more than MAX_AGE_SEC late (typed while the bot was not reading WeChat) is recorded
        the same way, but takes the chat over only until ``PRM_TAKEOVER_MIN`` after it was
        written, so not at all once that has passed.  PRM_CATCHUP_HOURS=0: such a line is
        history, as before (one Windows' edge queued, ``_queued``, counts as just written).
        """
        chat_id = str(msg.get("chat_id", ""))
        if chat_id not in self._wxids or is_friend_notice(msg):
            return                                         # (a friend notice is WeChat's, not an operator's)
        party = self._store.party_by_wxid(chat_id)
        if party is None:
            return
        now = self._clock()
        sent_at = float(msg.get("timestamp", 0) or 0)
        late = bool(sent_at) and now - sent_at > MAX_AGE_SEC
        if late and not self._catchup_sec:
            if not _queued(msg):
                return                                     # history (a chat read for the first time)
            late = False                                   # Windows' queue never replays history
        with self._handle_lock:
            self._own_line(msg, party, chat_id, now, sent_at, late)

    def _own_line(self, msg: dict, party: dict, chat_id: str, now: float, sent_at: float, late: bool) -> None:
        """``on_own_message`` for a creator's chat (under ``_handle_lock``)."""
        if msg.get("edge_operator"):
            # Mac 端：Windows 已经用发送记录比过了，对不上才报 operator_message，不再比一次
            matched, packet_id = False, None
        else:
            matched, packet_id = self._match_own(chat_id, msg, now)
        if msg.get("edge_seen") and not matched:
            return                                         # 我们发的读回来了，只是这次运行没记着它
        if matched:
            # Often before its send returned (WeChat shows a line at once); a failure stays one.
            if packet_id is not None and self._store.confirm_sent(packet_id):
                packet = self._store.packet(packet_id) or {}
                logger.info("PRM: packet %d seen in WeChat %.0f s after sending (sent_confirmed)", packet_id,
                            max(0.0, now - float(packet.get("sent_at") or now)))
            return
        if self._read_again(chat_id, msg, now):
            # 2026-10-27 联调：Windows 微信后端的消息编号先用 server_id 算，微信发出约 1 秒后才补上 server_id，
            # 同一行换了编号又读一次，Windows 当成运营亲手回复报上来
            logger.info("PRM: party %d: a line of ours read back again (WeChat changed its id), not a person's",
                        party["id"])
            return
        deal = self._store.deal_of(party["id"], now)
        text = self._mask(party["id"], f"[{_clock_text(sent_at or now)}] {message_text(msg)}", now)
        # ``at``: when it was written (read late, it is still an answer to what she wrote before it)
        self._store.add_event(deal["id"], party["id"], "operator_message", {"text": text, "at": sent_at or now},
                              actor="operator", forwarded=False, now=now)
        self._store.supersede(deal["id"], now, lane=LANE_REPLY)
        mode = deals.effective_mode(deal, now)
        if mode == PAUSED:
            logger.info("PRM: an operator wrote to party %d from WeChat; the deal stays paused", party["id"])
            return                                         # paused stays paused (the line is recorded)
        # Typed while the bot was not reading: the chat is a person's as long as it would have
        # been, counted from when the line was written (a later takeover is not cut short).
        until = (sent_at if late else now) + self._takeover_sec
        if until <= now:
            logger.info("PRM: an operator wrote to party %d from WeChat while the bot was not reading, %.0f min "
                        "ago; recorded, too long ago to take the chat over", party["id"], (now - sent_at) / 60)
            return
        if mode == HUMAN and until <= float(deal.get("mode_until") or 0):
            logger.info("PRM: an operator's earlier line in party %d's chat recorded; the takeover goes on",
                        party["id"])
            return
        self._store.update_deal(deal["id"], now, mode=HUMAN, mode_until=until)
        self._store.add_event(deal["id"], party["id"], "takeover", {"until": until}, actor="system", now=now)
        logger.info("PRM: an operator wrote to party %d from WeChat; the deal is a person's until %s",
                    party["id"], time.strftime("%H:%M", time.localtime(until)))

    def _note_sent(self, chat_id: str, text: str = "", packet_id: Optional[int] = None, file: bool = False) -> tuple:
        """What the bot is about to send, noted before it goes (WeChat shows a line, and the
        backend reads it back, before the send returns); ``_unnote_sent`` takes it back."""
        now = self._clock()
        with self._sent_lock:
            if file:
                before = self._sent_files.get(chat_id)
                self._sent_files[chat_id] = (now, packet_id)
                return chat_id, None, before
            item = (_norm(text), now, packet_id)
            self._sent_lines.setdefault(chat_id, deque(maxlen=50)).append(item)
            return chat_id, item, None

    def _unnote_sent(self, noted: tuple) -> None:
        """It did not go out: forget that line, or put the file record back as it was (an
        earlier file still counts as the bot's)."""
        chat_id, item, before = noted
        with self._sent_lock:
            if item is None:
                if before is None:
                    self._sent_files.pop(chat_id, None)
                else:
                    self._sent_files[chat_id] = before
                return
            lines = self._sent_lines.get(chat_id)
            if lines is not None and item in lines:     # (not if it was read back already)
                lines.remove(item)

    @staticmethod
    def _own_text(msg: dict) -> Optional[str]:
        """An own text line's normalized words (None for a picture or file)."""
        try:
            if int(msg.get("msg_type", 1) or 1) != 1:
                return None
        except (TypeError, ValueError):
            pass
        return _norm(_QUOTE_PREFIX.sub("", str(msg.get("content", "") or "")))

    def _read_again(self, chat_id: str, msg: dict, now: float) -> bool:
        """The same words as a line of ours matched in this chat within ``READ_BACK_SEC``: that
        line read again.  The times are ``_read_back``'s, set by ``_match_own`` on a real match only
        (a read-back read again does not move them, so the window is counted from the send's own
        read-back); this covers the Mac's ``edge_operator`` lines, which skip ``_match_own``."""
        text = self._own_text(msg)
        if not text:
            return False
        with self._sent_lock:
            at = (self._read_back.get(chat_id) or {}).get(text)
        return at is not None and now - at <= READ_BACK_SEC

    def _match_own(self, chat_id: str, msg: dict, now: float) -> tuple[bool, Optional[int]]:
        """Was this own line sent by the bot? ``(True, packet id or None)`` (``_match_sent``).

        A text line of ours that was matched in this chat, read again within READ_BACK_SEC,
        is the same send read back a second time, not an operator typing: WeChat 4.1
        writes a line it sends with no server id and fills one in a second later, and a backend
        that took that for a new message handed the row on twice (the WCDB backend's ids no
        longer change, src/wechat/wcdb_backend.py; this is the safety net).  The same words
        after that are a person's again."""
        matched, packet_id = self._match_sent(chat_id, msg, now)
        try:
            msg_type = int(msg.get("msg_type", 1) or 1)
        except (TypeError, ValueError):
            msg_type = 1
        if msg_type != 1:
            return matched, packet_id
        text = _norm(_QUOTE_PREFIX.sub("", str(msg.get("content", "") or "")))
        with self._sent_lock:
            seen = self._read_back.setdefault(chat_id, {})
            for line, at in list(seen.items()):
                if now - at > READ_BACK_SEC:
                    del seen[line]
            if matched:
                seen[text] = now
            elif text in seen:
                logger.info("PRM: our line in %s…'s chat read again %.0f s after it was matched (the same send, "
                            "a second id); not an operator's", chat_id[:8], now - seen[text])
                return True, None
            if not seen:
                del self._read_back[chat_id]
        return matched, packet_id

    def _match_sent(self, chat_id: str, msg: dict, now: float) -> tuple[bool, Optional[int]]:
        """Was this own line sent by the bot (``_match_own``)?  What was sent before this run is
        looked for around the time the line was written (read late after a restart: the bot
        sent it just before it stopped)."""
        try:
            msg_type = int(msg.get("msg_type", 1) or 1)
        except (TypeError, ValueError):
            msg_type = 1
        written = min(now, float(msg.get("timestamp", 0) or 0) or now)
        with self._sent_lock:
            if msg_type != 1:
                at, pid = self._sent_files.get(chat_id) or (None, None)
                if at is not None and now - at <= OWN_FILE_SEC:
                    return True, pid
            else:
                text = _norm(_QUOTE_PREFIX.sub("", str(msg.get("content", "") or "")))
                lines = self._sent_lines.get(chat_id) or deque()
                for item in list(lines):
                    if now - item[1] > OWN_MATCH_SEC:
                        lines.remove(item)
                    elif item[0] == text:
                        lines.remove(item)
                        return True, item[2]
        # Sent before this run (the bot restarted in between): the database, but only what
        # WeChat has not shown yet; this run's sends are all in memory, so an operator typing
        # the same words later is still seen as a person.
        party = self._store.party_by_wxid(chat_id)
        if party is None:
            return False, None
        if msg_type != 1:
            # A file of a draft sent just before a restart: WeChat shows it while the draft's
            # files are going out, before its send returns (``sent_at``).
            for packet in self._store.sent_since(chat_id, written - OWN_FILE_EARLY_SEC):
                if (packet["attachments"] and float(packet["sent_at"]) < self._started
                        and float(packet["sent_at"]) <= written + OWN_FILE_SEC):
                    return True, packet["id"]
            return False, None
        tokens = self._store.tokens(party["id"])
        for packet in self._store.sent_since(chat_id, written - OWN_MATCH_SEC):
            if packet["outcome"] != "sent_unconfirmed" or float(packet["sent_at"]) >= self._started:
                continue
            final = packet["final_text"] if packet["final_text"] is not None else packet["text"]
            if _norm(pii.unmask(final, tokens)) == text:
                return True, packet["id"]
        deal = self._store.deal_of(party["id"], now)
        for ev in self._store.events(deal["id"], limit=20):
            if (ev["kind"] == "holding_reply" and written - OWN_MATCH_SEC <= ev["at"] < self._started
                    and _norm(ev["detail"].get("text")) == text):
                return True, None
        return False, None

    def on_send_failed(self, chat_id: str, text: str, why: str, *, digest: str = "",
                       sent_at: Optional[float] = None) -> None:
        """Mac 端：Windows 报这条没发出去（失败、过期、不确定）。对应的草稿从「已发出」改成「没发出去」，
        运营在待审批页能看到原因；「稍等」这类不是草稿的，只记日志。

        ``digest``：Mac 重启以前发的，edge 后端只留着发出去的原文的 sha256（原文不写盘），``text`` 是空的：
        按摘要对草稿（换回号码以后的全文），从 ``sent_at`` 前后找起，不受 OWN_MATCH_SEC 限制。"""
        now = self._clock()
        if digest:
            self._send_failed_by_digest(chat_id, digest, why, sent_at, now)
            return
        wanted = _norm(text)
        with self._sent_lock:
            lines = self._sent_lines.get(chat_id) or deque()
            for item in reversed(list(lines)):             # 最新的那条：更早发出的同一句话可能还在等读回
                if item[0] == wanted:
                    lines.remove(item)                     # 没发出去，读回时不能再算我们的
                    break
        party = self._store.party_by_wxid(chat_id)
        if party is None:
            return
        tokens = self._store.tokens(party["id"])
        for packet in self._store.sent_since(chat_id, now - OWN_MATCH_SEC):
            final = packet["final_text"] if packet["final_text"] is not None else packet["text"]
            if packet["outcome"] == "sent_unconfirmed" and _norm(pii.unmask(final, tokens)) == wanted:
                if self._store.move(packet["id"], (SENT,), FAILED, outcome="failed", error=why[:200]):
                    logger.warning("PRM: packet %d was not sent by Windows: %s", packet["id"], why)
                return
        logger.warning("PRM: a send to party %d failed (%s); no waiting draft matches it", party["id"], why)

    def _send_failed_by_digest(self, chat_id: str, digest: str, why: str, sent_at: Optional[float],
                               now: float) -> None:
        party = self._store.party_by_wxid(chat_id)
        if party is None:
            return
        tokens = self._store.tokens(party["id"])
        since = (sent_at - OWN_MATCH_SEC) if sent_at else now - RESTART_FAIL_SEC
        for packet in self._store.sent_since(chat_id, since):
            final = packet["final_text"] if packet["final_text"] is not None else packet["text"]
            sent = pii.unmask(final, tokens)
            if packet["outcome"] == "sent_unconfirmed" and hashlib.sha256(sent.encode("utf-8")).hexdigest() == digest:
                if self._store.move(packet["id"], (SENT,), FAILED, outcome="failed", error=why[:200]):
                    logger.warning("PRM: packet %d was not sent by Windows (told after a restart): %s", packet["id"], why)
                return
        logger.warning("PRM: a send to party %d failed (%s, after a restart); no waiting draft matches it",
                       party["id"], why)

    # ── from the Mac (the relay's delivery thread) ────────────────────────

    def on_result(self, job: dict) -> bool:
        """A prm job ended: its draft becomes a packet (or the deal says what went wrong)."""
        meta = job_meta(job)
        try:
            party_id, deal_id = int(meta["party_id"]), int(meta["deal_id"])
        except (KeyError, TypeError, ValueError):
            logger.warning("PRM: job #%d has no party/deal; dropped", job["id"])
            return True
        deal = self._store.deal(deal_id)
        if deal is None or self._store.party(party_id) is None:
            return True
        now = self._clock()
        status = job["status"]
        if meta.get("op") in PLATFORM_OPS:
            return self._on_platform_job(job, meta, now)
        if meta.get("op") == "purge":                       # the Mac's case file was cleaned (or not)
            if status in ("done", "failed", "lost"):
                self._store.add_event(deal_id, party_id, "purge_case",
                                      {"ok": status == "done", "result": str(job.get("result_text") or "")[:200]},
                                      actor="mac", now=now)
            return True
        if status == "cancelled":
            return True
        # Messages read late (PRM_CATCHUP_HOURS): said in the deal's attention, the draft is reviewed.
        notes = gap_notes(meta)
        if status in ("failed", "lost"):
            try:
                extra = json.loads(job.get("result_json") or "{}")
            except ValueError:
                extra = {}
            extra = extra if isinstance(extra, dict) else {}
            why = "Mac 失联了" if status == "lost" else str(extra.get("error") or job.get("result_text") or "没有返回内容")
            self._store.update_deal(deal_id, now, attention="；".join([f"#{job['id']} 草稿没写成：{why[:120]}", *notes])[:300])
            logger.warning("PRM: job #%d %s; the deal needs a person", job["id"], status)
            return True
        if self._store.packet_for_job(job["id"]) is not None:
            return True                                # already drafted (a repeated delivery)
        try:
            draft = parse_draft(job.get("result_text") or "")
        except DraftError as e:
            self._store.update_deal(deal_id, now, attention="；".join(
                [f"#{job['id']} 的草稿格式不对（{e.code}），没有生成待审批", *notes])[:300])
            logger.warning("PRM: job #%d answer unusable: %s", job["id"], e)
            return True
        draft = self._mask_draft(party_id, draft, now)
        files = self._relay.store.outfiles(job["id"])
        attachments = [{"name": f["name"], "path": f["path"], "size": f["size"], "sha256": f["sha256"]} for f in files]
        # Every file the draft named (an older worker reports only the ones it sent back): fewer came,
        # the draft waits for a person whatever its kind's gate, and the deal and the draft say which.
        named = draft.attachments_named if draft.attachments_named is not None else draft.attachments_to_send
        # The Mac's action did not pass the bot's own check (a Mac with wider limits, say): the reply stays
        # a draft, without it, for a person (it was written as if the action had run).
        refused = (f"平台动作没过机器人这边的检查（{draft.platform_refused}），草稿里去掉了它，平台上什么都不会做；"
                   "回复是按执行成功写的，发之前请核对") if draft.platform_refused else ""
        short = ""
        if len(named) > len(attachments):
            lost = missing_files(named, [a["name"] for a in attachments], draft.attachments_missing)
            short = f"草稿要附 {len(named)} 个文件，只收到 {len(attachments)} 个" + (f"：{'、'.join(lost)}" if lost else "")
        mode = self._mode(deal, now)
        shipment = self._store.latest_shipment(deal_id)
        waybill = shipment["waybill"] if shipment else ""
        unknown = pii.unknown_tokens(draft.reply_draft, self._store.tokens(party_id))
        # Two lanes: the creator's replies and the bot's own next steps (meta.system_note); a newer
        # job of the other lane does not make a draft out of date, nor does a draft supersede the other's.
        lane = LANE_SYSTEM if meta.get("system_note") else LANE_REPLY
        newer = self._relay.store.latest_open(job["chat_id"], drafts_only=True, system=lane == LANE_SYSTEM)
        # A step the bot asked for whose draft has no platform action (the model left it out, or could
        # not fill it) and still says something: nothing will happen on the platform, whatever it says.
        stepless = ("开专属佣金这一步的草稿没带平台动作，平台上什么都不会做；回复要是说开好了，发之前请核对"
                    if lane == LANE_SYSTEM and draft.platform is None and not refused
                    and (draft.reply_draft or attachments) else "")
        route, decided_at = gates.REVIEW, None
        if draft.platform is not None:
            # A platform write: always a person's decision (8.1's gates do not apply).
            state = SUPERSEDED if mode != COPILOT or (newer is not None and newer["id"] > job["id"]) else PENDING
        elif not draft.reply_draft and not attachments:
            state = NO_REPLY
        elif mode != COPILOT:
            state = SUPERSEDED                         # a person has the chat
        elif newer is not None and newer["id"] > job["id"]:
            state = SUPERSEDED                         # the creator wrote again: a newer draft is coming
        elif notes or lane == LANE_SYSTEM:
            # Read late: a person decides, whatever the gate.  The bot's own next step too: a 专属佣金
            # step speaks of a platform write, so it never goes out by a gate (mac review F4).
            state = PENDING
        else:
            route, state, decided_at = deals.route_for(
                self._store, draft.message_kind, deal=deal, now=now, text=draft.reply_draft,
                needs_attention=draft.needs_attention or bool(refused), unknown_tokens=unknown, reason=draft.reason,
                waybill=waybill, attachments=attachments, files_named=len(named))
        packet_meta = {"waybill": waybill} if draft.message_kind == "shipping_notice" and waybill else {}
        if lane == LANE_SYSTEM:
            packet_meta["lane"] = LANE_SYSTEM
        tool, platform = MESSAGE_TOOL, None
        if draft.platform is not None:
            action = draft.platform.get("action")
            tool = (action or {}).get("action") or "platform.unchecked"
            platform = {"action": action, "preview": draft.platform}
        packet_id = self._store.add_packet(
            deal_id=deal_id, party_id=party_id, job_id=job["id"], chat_id=job["chat_id"],
            kind=draft.message_kind, text=draft.reply_draft, attachments=attachments, intent=draft.intent,
            slots=draft.slots, stage_proposal=draft.stage_proposal,
            needs_attention=draft.needs_attention or bool(short) or bool(refused) or bool(stepless),
            reason="；".join(r for r in (refused, stepless, short, draft.reason) if r)[:500],
            status=state, route=route, decided_at=decided_at, meta=packet_meta,
            tool=tool, platform=platform, now=now,
        )
        if state in (PENDING, APPROVED):
            self._store.supersede(deal_id, now, keep=packet_id, lane=lane)
        attention = [*notes, *([draft.reason] if draft.needs_attention and draft.reason else [])]
        if lane == LANE_SYSTEM and state == SUPERSEDED:
            attention.append(f"#{job['id']} 开专属佣金这一步的草稿没进待审批（人工接管中，或者又有了新的一步），请运营核对")
        if short:
            attention.append(short)
        if refused:
            attention.append(f"#{job['id']} {refused}")
        if stepless and state != SUPERSEDED:
            attention.append(f"#{job['id']} {stepless}")
        if draft.message_kind == "shipping_notice" and draft.reply_draft and not gates.waybill_ok(draft.reply_draft, waybill):
            attention.append("发货通知里的单号和系统记录对不上" if waybill else "还没有运单号，发货通知不能发")
        if draft.platform is not None and draft.platform.get("verdict") != "ready":
            why = "；".join(draft.platform.get("problems") or []) or platforms.VERDICTS.get(draft.platform.get("verdict"), "")
            attention.append(f"平台动作预览没通过：{why}"[:200])
        self._store.update_deal(deal_id, now, merge_slots=draft.slots,
                                stage_proposal=draft.stage_proposal, attention="；".join(attention)[:300])
        if state == APPROVED:
            self._wake.set()
        logger.info("PRM: job #%d -> packet %d (%s, %s, %s)", job["id"], packet_id, state, route, draft.message_kind)
        return True

    def _mask_draft(self, party_id: int, draft, now: float):
        """The draft with its numbers as the creator's placeholders, before anything of it is stored.
        The Mac reads her pictures, so a number it read off a screenshot comes back written out (win
        review F5): in the reply, the slots, the intent and reason, a sample order's receiver.  It
        becomes the same 〈手机N〉 as a number she types (``token_for``): the console shows it masked
        and 显示完整号码 is written down; she (``_deliver``) and the Mac running the action
        (``action_for_mac``) get the number back."""
        mask = self._masker(party_id, now)
        platform = draft.platform
        if platform is not None:
            platform = {**platform, "action": masked_action(platform.get("action"), mask)}
            if isinstance(platform.get("requested"), dict):      # what a draft asked for that could not be used
                platform["requested"] = {k: mask(v) if k in PERSONAL_PARAMS else v
                                         for k, v in platform["requested"].items()}
        return dataclasses.replace(draft, reply_draft=mask(draft.reply_draft), intent=mask(draft.intent),
                                   reason=mask(draft.reason), slots={k: mask(v) for k, v in draft.slots.items()},
                                   platform=platform, platform_refused=mask(draft.platform_refused))

    def _masker(self, party_id: int, now: float) -> Callable:
        """``_mask`` for one creator's texts; an empty one, or one that is no text, stays as it is."""
        return lambda text: self._mask(party_id, text, now) if isinstance(text, str) and text else text

    # ── from the console ──────────────────────────────────────────────────

    def register(self, wxid: str, name: str, note: str = "") -> dict:
        """Register a creator (an operator's decision in the console; never automatic).  When she is a
        new friend (``on_registered``), her messages since she was added become one reviewed draft
        (take_new_friend); ``catch_up`` in the answer says so."""
        wxid = check_wxid(wxid, self._reserved)
        name, note = str(name or "").strip(), str(note or "").strip()
        if not name or len(name) > 30:
            raise PartyError("称呼要填，最多 30 个字")
        if len(note) > 200:
            raise PartyError("备注最多 200 个字")
        party = self._store.add_party(wxid, name, note, self._clock())
        self._parties_changed()
        logger.info("PRM: party %d registered", party["id"])
        caught = False
        if self.on_registered is not None:
            try:
                caught = bool(self.on_registered(wxid))
            except Exception:
                logger.exception("PRM: party %d's earlier messages (a new friend) could not be asked for", party["id"])
        return {**party, "catch_up": caught}

    def take_new_friend(self, chat_id: str, messages: list[dict], *, verify_until: float = 0.0) -> int:
        """A new friend registered as a creator: her messages since she was added (the WeChat backend
        read them back on her chat's next poll: hers only, oldest first, at most PRM_CATCHUP_HOURS
        old) become one draft job that waits for a person (``meta.new_friend``), so her first words
        are not lost; the ones up to ``verify_until`` (the time she was added) are her friend-request
        greeting and say so.  WeChat's friend notices are left out.  Returns how many were taken."""
        chat_id = str(chat_id)
        party = self._store.party_by_wxid(chat_id)
        if party is None:
            return 0
        picked = [m for m in messages or [] if not m.get("is_group", False) and not is_friend_notice(m)]
        if not picked:
            logger.info("PRM: party %d is a new friend with no messages from before the registration", party["id"])
            return 0
        now = self._clock()
        times = [float(m.get("timestamp") or 0) or now for m in picked]
        info = {"from": min(times), "count": len(picked)}
        taken = 0
        for msg, at in zip(picked, times):
            line = message_text(msg)
            if verify_until and at <= verify_until and str(msg.get("msg_type", 1)) == "1":
                line = f"（加好友时的验证消息）{line}"
            files: tuple = ()
            kind = attachment_kind(msg)
            if kind:
                collected, problems = self._relay.collect([Held(kind, msg, now)])
                files = tuple(collected)
                if problems:
                    line += "（" + "；".join(problems) + "）"
                elif not files:
                    line += f"（{ATTACHMENT_KINDS[kind]}没取到）"
            with self._handle_lock:
                if self._recorded_for_a_person(party, line, at, now):
                    continue
                self._submit(party, chat_id, line, files, at, max(0.0, now - at), False, now, new_friend=info)
                taken += 1
        logger.info("PRM: party %d is a new friend: %d message(s) from before the registration -> one reviewed "
                    "draft job", party["id"], taken)
        return taken

    def set_active(self, party_id: int, active: bool) -> Optional[dict]:
        party = self._store.update_party(int(party_id), active=active, now=self._clock())
        self._parties_changed()
        return party

    def _parties_changed(self) -> None:
        self._wxids = self._store.active_wxids()
        if self.on_parties_changed is not None:
            try:
                self.on_parties_changed()
            except Exception:
                logger.exception("PRM: the WeChat backend could not reload its private chats")

    def deal_action(self, action: str, data: dict) -> dict:
        """Stage / adopt / mode / shipment / shipment_status from the console (deals.py)."""
        now = self._clock()
        try:
            before = self._store.deal(int(data.get("deal_id")))
        except (TypeError, ValueError):
            before = None
        result = apply_deal_action(self._store, action, data, now=now, takeover_sec=self._takeover_sec)
        if result.get("ok"):
            self._wake.set()
            after = self._store.deal(before["id"]) if before else None
            if after and after.get("delivered_at") and not before.get("delivered_at"):
                self._on_delivered(after, now)
        return result

    def _on_delivered(self, deal: dict, now: float) -> None:
        """The creator signed for the sample: with PRM_COMMISSION_AFTER=delivered the 专属佣金 of
        the deal's last created sample order is drafted now (``_ask_next_step``)."""
        if self._commission_after != "delivered":
            return
        for ev in self._store.events(deal["id"], limit=200):        # newest first
            detail = ev["detail"]
            if (ev["kind"] == "platform_result" and detail.get("action") == "jst.sample_order"
                    and detail.get("outcome") == platforms.VERIFIED):
                packet = self._store.packet(int(detail.get("packet") or 0))
                if packet is not None:
                    data = packet["platform"] or {}
                    self._ask_next_step(packet, next_step_note(data.get("action") or {}, data.get("execution") or {},
                                                               delivered=True), now)
                return

    def reveal(self, party_id: int, actor: str = "console") -> dict:
        return reveal_numbers(self._store, int(party_id), now=self._clock(), actor=actor)

    def decide(self, packet_id: int, action: str, text: Optional[str] = None) -> dict:
        """An operator's choice: send | edit (send ``text``) | drop a pending draft, or
        wrong (a sent one should not have gone out: its kind is reviewed again, from zero)."""
        packet = self._store.packet(int(packet_id))
        if packet is None:
            return {"ok": False, "error": f"没有找到草稿 {packet_id}"}
        now = self._clock()
        if action == "wrong":
            if not self._store.mark_wrong(packet["id"]):
                return {"ok": False, "error": "只有已发出的消息能标为发错了（或已经标过）"}
            self._store.set_gate(packet["kind"], gates.REVIEW, now, reset=True)
            self._store.add_event(packet["deal_id"], packet["party_id"], "wrong",
                                  {"packet": packet["id"], "kind": packet["kind"]}, actor="console", now=now)
            logger.info("PRM: packet %d marked wrong; %s drafts are reviewed again", packet["id"], packet["kind"])
            return {"ok": True, "status": "wrong"}
        if action == "drop":
            if not self._store.move(packet["id"], (PENDING,), DROPPED, decided_at=now):
                return {"ok": False, "error": "这条草稿已经处理过了"}
            return {"ok": True, "status": DROPPED}
        if action == "execute":
            return self._approve_execute(packet, text, now)
        if action == "repreview":
            return self._repreview(packet, now)
        if action == "ack":
            return self._ack(packet, now)
        if action not in ("send", "edit"):
            return {"ok": False, "error": f"不认识的操作 {action}"}
        if packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL:
            return {"ok": False, "error": "这条草稿带平台动作：要点「批准并执行」，平台上核验成功后才会发给达人"}
        final = packet["text"] if action == "send" else str(text if text is not None else "").strip()
        if len(final) > MAX_DRAFT_CHARS:
            return {"ok": False, "error": f"最多 {MAX_DRAFT_CHARS} 个字"}
        if not final and not packet["attachments"]:
            return {"ok": False, "error": "没有要发的内容"}
        unknown = pii.unknown_tokens(final, self._store.tokens(packet["party_id"]))
        if unknown:
            return {"ok": False, "error": f"草稿里有对不上的占位符：{'、'.join(unknown)}"}
        if packet["kind"] == "shipping_notice":
            waybill = self._waybill(packet)
            if not gates.waybill_ok(final, waybill):
                return {"ok": False, "error": f"发货通知里的单号和系统记录对不上（系统记录：{waybill or '还没有运单号'}）"}
        if self._send is None:
            return {"ok": False, "error": "机器人没有连着微信，现在不能发送"}
        if packet["attachments"] and self._send_file is None:
            return {"ok": False, "error": "这个微信后端不能发文件（只有 Windows 数据库直读能发）"}
        if not self._store.move(packet["id"], (PENDING,), APPROVED, decided_at=now, final_text=final,
                                edited=int(final != packet["text"])):
            return {"ok": False, "error": "这条草稿已经处理过了（或者达人又发了新消息，草稿已过时）"}
        self._wake.set()
        return {"ok": True, "status": APPROVED}

    # ── platform actions (platforms.py; the CLIs run on the Mac) ───────────

    def _approve_execute(self, packet: dict, text: Optional[str], now: float) -> dict:
        """批准并执行: the action goes to the Mac once (an approval key), the reply waits for
        its read-back.  The checks of a reply (length, placeholders, can it be sent) come
        first: a platform write whose reply could not go out is not started."""
        if packet.get("tool", MESSAGE_TOOL) == MESSAGE_TOOL:
            return {"ok": False, "error": "这条草稿没有平台动作"}
        data = packet["platform"]
        action, preview = data.get("action"), data.get("preview") or {}
        if not action or preview.get("verdict") != "ready":
            why = "；".join(preview.get("problems") or []) or platforms.VERDICTS.get(preview.get("verdict"), "")
            return {"ok": False, "error": f"预览没通过，不能执行：{why}"}
        if data.get("previewing"):
            return {"ok": False, "error": "正在重新预览，等结果出来再批准"}
        opened = self._opened_before(packet)
        if opened:
            logger.warning("PRM: packet %d not approved: its %s ran before for this product and creator",
                           packet["id"], action.get("action"))
            return {"ok": False, "error": opened}
        final = packet["text"] if text is None else str(text).strip()
        if len(final) > MAX_DRAFT_CHARS:
            return {"ok": False, "error": f"最多 {MAX_DRAFT_CHARS} 个字"}
        problems = platforms.reply_problems(final, action)
        if problems:
            return {"ok": False, "error": "；".join(problems)}
        tokens = self._store.tokens(packet["party_id"])
        unknown = pii.unknown_tokens(final, tokens)
        if unknown:
            return {"ok": False, "error": f"草稿里有对不上的占位符：{'、'.join(unknown)}"}
        mac_action, problem = action_for_mac(action, tokens)
        if problem:
            return {"ok": False, "error": problem}
        if self._send is None:
            return {"ok": False, "error": "机器人没有连着微信：执行成功后回复发不出去，现在不执行"}
        if packet["attachments"] and self._send_file is None:
            return {"ok": False, "error": "这个微信后端不能发文件（只有 Windows 数据库直读能发）"}
        approved = {**data, "key": uuid.uuid4().hex, "approved_at": now}
        with self._approve_lock:              # two drafts of one product approved at the same moment: one runs
            opened = self._opened_before(packet)
            moved = not opened and self._store.move(
                packet["id"], (PENDING,), EXECUTING, decided_at=now, final_text=final,
                edited=int(final != packet["text"]), platform=approved)
        if opened:
            return {"ok": False, "error": opened}
        if not moved:
            return {"ok": False, "error": "这条草稿已经处理过了（或者达人又发了新消息，草稿已过时）"}
        meta = {"op": "platform_execute", "party_id": packet["party_id"], "deal_id": packet["deal_id"],
                "packet_id": packet["id"], "key": approved["key"], "action": mac_action, "run": preview.get("run") or {}}
        try:
            _, job_id = self._relay.submit(packet["chat_id"], packet["chat_id"], "", kind=PRM, meta=meta,
                                           fresh=True, input_closed=True)
        except Exception as e:
            logger.exception("PRM: packet %d could not be queued for the Mac", packet["id"])
            self._store.move(packet["id"], (EXECUTING,), FAILED, outcome="platform_not_run", sent_at=now,
                             error=f"没能排进 Mac 的任务队列，平台没有动：{e}"[:300])
            return {"ok": False, "error": "没能排进 Mac 的任务队列，平台没有动"}
        self._store.set_platform(packet["id"], {**approved, "job_id": job_id}, (EXECUTING,))
        self._store.add_event(packet["deal_id"], packet["party_id"], "platform_execute",
                              {"packet": packet["id"], "action": action["action"], "job": job_id},
                              actor="console", now=now)
        logger.info("PRM: packet %d approved; %s goes to the Mac as job #%d", packet["id"], action["action"], job_id)
        return {"ok": True, "status": EXECUTING, "job": job_id}

    def _opened_before(self, packet: dict) -> str:
        """Why this draft's 专属 action (``ONCE_ACTIONS``) must not run: another draft of the deal has
        the same action for the same product and creator, verified on the platform (its reply sent,
        about to go, or held back) or running on the Mac now.  The ledger on the Mac only stops one
        approval from running twice; this stops a second draft (a chain picked up again while a step
        runs, a reply that carries a commission against kb/).  "" = it may run."""
        data = packet["platform"]
        action = data.get("action") or {}
        name = action.get("action")
        if name not in ONCE_ACTIONS:
            return ""
        item, creators = action_target(action, data)
        for other in self._store.deal_packets(packet["deal_id"], tools=(name,),
                                              statuses=(EXECUTING, APPROVED, SENDING, SENT, FAILED)):
            theirs = other["platform"] or {}
            their_item, their_creators = action_target(theirs.get("action") or {}, theirs)
            if other["id"] == packet["id"] or their_item != item or not (creators & their_creators):
                continue
            what = f"商品 {item} 给达人 {action.get('creator_platform_id')} 的「{platforms.label(name)}」"
            if other["status"] == EXECUTING:
                at = float(theirs.get("approved_at") or other.get("decided_at") or other["created_at"])
                return f"{what}正在执行（#{other['id']}，{_date_text(at)} 批准），不能再开一次。{REOPEN_HINT}"
            if other["status"] != FAILED or (theirs.get("execution") or {}).get("outcome") == platforms.VERIFIED:
                at = float(other.get("sent_at") or other.get("decided_at") or other["created_at"])
                return f"{what}已经在 #{other['id']}（{_date_text(at)}）开过了，平台上核验成功，不能再开一次。{REOPEN_HINT}"
        return ""

    def _repreview(self, packet: dict, now: float) -> dict:
        """Ask the Mac for a new preview of a waiting draft's action (after a login, say)."""
        if packet.get("tool", MESSAGE_TOOL) == MESSAGE_TOOL:
            return {"ok": False, "error": "这条草稿没有平台动作"}
        data = packet["platform"]
        action = data.get("action")
        if not action:
            return {"ok": False, "error": "这个平台动作的参数不对，预览不了；等达人再发消息时的新草稿"}
        if not self._store.set_platform(packet["id"], {**data, "previewing": now}, (PENDING,),
                                        expect=lambda current: not current.get("previewing")):
            return {"ok": False, "error": "这条草稿已经处理过了，或者已经在重新预览"}
        meta = {"op": "platform_preview", "party_id": packet["party_id"], "deal_id": packet["deal_id"],
                "packet_id": packet["id"], "action": action}
        try:
            _, job_id = self._relay.submit(packet["chat_id"], packet["chat_id"], "", kind=PRM, meta=meta,
                                           fresh=True, input_closed=True)
        except Exception:
            logger.exception("PRM: a new preview of packet %d could not be queued", packet["id"])
            self._store.set_platform(packet["id"], {**data, "previewing": None}, (PENDING,))
            return {"ok": False, "error": "没能排进 Mac 的任务队列"}
        self._store.add_event(packet["deal_id"], packet["party_id"], "platform_preview",
                              {"packet": packet["id"], "job": job_id}, actor="console", now=now)
        return {"ok": True, "status": PENDING, "job": job_id}

    def _ack(self, packet: dict, now: float) -> dict:
        """An operator has dealt with a platform action that ended red."""
        if packet.get("tool", MESSAGE_TOOL) == MESSAGE_TOOL or packet["status"] != FAILED:
            return {"ok": False, "error": "只有没完成的平台动作能标为已处理"}
        data = packet["platform"]
        if data.get("acked_at") or not self._store.set_platform(packet["id"], {**data, "acked_at": now}, (FAILED,)):
            return {"ok": False, "error": "已经标过了"}
        self._store.add_event(packet["deal_id"], packet["party_id"], "platform_ack", {"packet": packet["id"]},
                              actor="console", now=now)
        return {"ok": True}

    def _on_platform_job(self, job: dict, meta: dict, now: float) -> bool:
        """The Mac's answer to a platform job: a new preview, or the execution's outcome."""
        try:
            packet = self._store.packet(int(meta.get("packet_id")))
        except (TypeError, ValueError):
            packet = None
        if packet is None or packet.get("tool", MESSAGE_TOOL) == MESSAGE_TOOL:
            return True
        status = job["status"]
        if meta.get("op") == "platform_preview":
            if status in ("pending", "running"):
                return True
            data = packet["platform"]
            block, refused = None, ""
            if status == "done":
                try:
                    block = platforms.check_block(json.loads(job.get("result_text") or ""))
                except platforms.ActionRefused as e:
                    refused = f"新预览的动作没过机器人这边的检查（{e}）"
                except (ValueError, platforms.ActionError):
                    block = None
            if block is None:
                why = refused or {"lost": "Mac 失联了", "cancelled": "任务被取消了"}.get(status) or _job_error(job)
                preview = {**(data.get("preview") or {}), "problems": [f"重新预览没做成：{why}"[:200]]}
                try:
                    block = platforms.check_block(preview)
                except platforms.ActionError:
                    block = None
            if block:                                # (its receiver as the stored one: placeholders)
                block = {**block, "action": masked_action(block.get("action"), self._masker(packet["party_id"], now))}
            new = {**data, "previewing": None, **({"preview": block} if block else {})}
            self._store.set_platform(packet["id"], new, (PENDING,))
            return True
        # platform_execute
        data = packet["platform"]
        if str(meta.get("key") or "") != str(data.get("key") or ""):
            logger.warning("PRM: job #%d answers another approval of packet %d; ignored", job["id"], packet["id"])
            return True
        if status == "lost":
            if packet["status"] == EXECUTING:
                self._store.set_platform(packet["id"], {**data, "lost_at": now}, (EXECUTING,))
                logger.warning("PRM: the Mac went silent while executing packet %d; waiting for it", packet["id"])
            return True
        if status in ("pending", "running"):
            return True
        record = None
        try:
            record = platforms.check_record(json.loads(job.get("result_text") or ""))
        except (ValueError, platforms.ActionError):
            record = None
        if record is None:
            if status == "cancelled":
                record = platforms.record("UNKNOWN", ["执行任务被取消了；如果 Mac 已经开始执行，请到平台后台核对"])
            elif status == "failed":
                record = platforms.record("NOT_RUN", [f"Mac 没有执行：{_job_error(job)}"[:300]])
            else:
                record = platforms.record("UNKNOWN", ["Mac 的执行结果读不出来；请到平台后台核对"])
        self._platform_finished(packet, record, now, cancelled=status == "cancelled")
        return True

    def _platform_finished(self, packet: dict, record: dict, now: float, cancelled: bool = False) -> None:
        """VERIFIED_SUCCESS: the reply, link filled in, is approved for the sender; else red."""
        data = packet["platform"]
        action = data.get("action") or {}
        outcome = record["outcome"]
        executed = {**data, "execution": record, "lost_at": data.get("lost_at")}
        if packet["status"] != EXECUTING:
            # A late answer (the packet was given up on): kept for the operators, nothing sent.
            if packet["status"] == FAILED:
                record["late"] = True
                self._store.set_platform(packet["id"], {**executed, "acked_at": None}, (FAILED,))
                self._store.update_deal(packet["deal_id"], now, attention=(
                    f"#{packet['id']} 平台动作的结果晚到了：{platforms.outcome_text(outcome)}；回复没有自动发，请运营处理")[:300])
            return
        event = {"packet": packet["id"], "action": action.get("action"), "outcome": outcome,
                 "link": (record.get("created") or {}).get("link", "")}
        for key in ("item_id", "creator_platform_id"):      # which product, which creator (an older event has neither)
            if action.get(key):
                event[key] = str(action[key])
        if (record.get("created") or {}).get("sample_order_no"):
            event["sample_order_no"] = record["created"]["sample_order_no"]
        missing = []
        if outcome == platforms.VERIFIED and not cancelled:
            final, missing = platforms.fill_reply(
                packet["final_text"] if packet["final_text"] is not None else packet["text"], action,
                record.get("created"))
            if not missing:
                if self._store.move(packet["id"], (EXECUTING,), APPROVED, final_text=final, decided_at=now,
                                    platform=executed):
                    self._store.add_event(packet["deal_id"], packet["party_id"], "platform_result", event,
                                          actor="mac", now=now)
                    logger.info("PRM: packet %d %s verified on the platform; the reply goes out", packet["id"],
                                action.get("action"))
                    self._wake.set()
                    if action.get("action") != "jst.sample_order" or self._commission_after == "created":
                        self._ask_next_step(packet, next_step_note(action, record), now)
                return
        why = "；".join(record.get("problems") or [])
        if outcome == platforms.VERIFIED and missing:
            error = (f"平台上已经核验成功，但回复里的 {'、'.join(missing)} 平台没有给出，回复没有自动发出；"
                     "请核对后自己回达人")
        elif outcome == platforms.VERIFIED:        # verified, but the job was cancelled on the way
            error = "平台上已经核验成功，但执行任务中途被取消了，回复没有自动发出；请核对后自己回达人"
        else:
            error = (f"平台动作没有核验成功（{outcome}：{platforms.outcome_text(outcome)}），没有发微信"
                     + (f"：{why}" if why else "") + "。请到平台后台核对后自己处理，系统不会重试")[:500]
        if self._store.move(packet["id"], (EXECUTING,), FAILED, outcome=f"platform_{outcome.lower()}", sent_at=now,
                            error=error, platform=executed):
            self._store.add_event(packet["deal_id"], packet["party_id"], "platform_result", event, actor="mac", now=now)
            self._store.update_deal(packet["deal_id"], now, attention=f"#{packet['id']} {error}"[:300])
            logger.warning("PRM: packet %d %s ended %s; nothing sent", packet["id"], action.get("action"), outcome)

    def _ask_next_step(self, packet: dict, note: str, now: float) -> None:
        """A platform write that leads to the next one (a sample order → its 专属佣金): a draft
        job of its own, told what happened in ``meta.system_note``.  What it drafts is a platform
        action like any other: previewed on the Mac, approved by a person; and whatever it drafts
        waits for a person, whatever its kind's gate (``on_result``: the system lane).  Not for a creator the
        operators took off the list (inactive) or a deal a person has: the deal says so instead."""
        if not note:
            return
        deal = self._store.deal(packet["deal_id"])
        party = self._store.party(packet["party_id"])
        if deal is None or party is None:
            return
        if not party.get("active"):
            self._store.update_deal(deal["id"], now, attention=(
                f"#{packet['id']} 之后该开专属佣金了：这位达人已经停用（不在达人名单里），系统没有起草，请运营处理")[:300])
            logger.info("PRM: party %d is inactive; the next step after packet %d is left to a person",
                        party["id"], packet["id"])
            return
        if self._mode(deal, now) != COPILOT:
            self._store.update_deal(deal["id"], now, attention=(
                f"#{packet['id']} 之后该开专属佣金了：现在不在副驾驶模式，系统没有起草，请运营处理")[:300])
            return
        meta = {"party_id": party["id"], "deal_id": deal["id"], "name": party["name"], "system_note": note}
        outbound = self._outbound(deal["id"], party["id"], now)
        if outbound:
            meta["sent"] = outbound
        # input_closed: the creator's next message is a reply job of its own, not part of this step
        _, job_id = self._relay.submit(packet["chat_id"], packet["chat_id"], "", kind=PRM, meta=meta, fresh=True,
                                       input_closed=True)
        logger.info("PRM: packet %d done; job #%d drafts the next step", packet["id"], job_id)

    def sweep_executing(self) -> int:
        """Executions that cannot finish any more: a job the Mac has not taken within
        APPROVED_EXPIRE_SEC is cancelled (nothing was run); one whose Mac went silent for
        PLATFORM_LOST_GRACE_SEC, or whose job is gone, is given up on.  Returns how many."""
        done = 0
        now = self._clock()
        for packet in self._store.packets((EXECUTING,), limit=100):
            data = packet["platform"]
            job = self._relay.store.get(int(data.get("job_id") or 0)) if data.get("job_id") else None
            approved = float(data.get("approved_at") or packet["decided_at"] or now)
            record = None
            if job is None:
                if now - approved > 60:
                    record = platforms.record("UNKNOWN", ["找不到执行任务了；请到平台后台核对"])
            elif job["status"] == "pending" and now - approved > APPROVED_EXPIRE_SEC:
                if self._relay.store.cancel_if_pending(job["id"], now):
                    record = platforms.record("NOT_RUN", [f"Mac {APPROVED_EXPIRE_SEC // 60} 分钟没有接手，平台没有动"])
            elif job["status"] == "cancelled" and not job.get("result_text"):
                record = platforms.record("NOT_RUN", ["执行任务在 Mac 接手前被取消了，平台没有动"])
            elif data.get("lost_at") and job["status"] in ("lost", "running"):
                if job["status"] == "running" and float(job.get("heartbeat_at") or 0) > float(data["lost_at"]):
                    self._store.set_platform(packet["id"], {**data, "lost_at": None}, (EXECUTING,))   # it is back
                elif now - float(data["lost_at"]) > PLATFORM_LOST_GRACE_SEC:
                    record = platforms.record("UNKNOWN", [
                        f"Mac 失联超过 {PLATFORM_LOST_GRACE_SEC // 60} 分钟，不知道平台上执行了没有；请到平台后台核对"])
            if record is not None:
                self._platform_finished(self._store.packet(packet["id"]), record, now)
                done += 1
        return done

    def _waybill(self, packet: dict) -> str:
        """The system's record: the deal's latest waybill (a notice of an earlier one is refused)."""
        shipment = self._store.latest_shipment(packet["deal_id"])
        return shipment["waybill"] if shipment else ""

    def snapshot(self) -> dict:
        relay = self._relay.snapshot()
        return snapshot(self._store, running=True, retention_days=self._retention_days, now=self._clock(), extra={
            "mac_online": relay.get("mac_online", False),
            "worker_kinds": relay.get("worker_kinds", []),
            "can_send": self._send is not None, "can_send_files": self._send_file is not None,
            "takeover_min": self._takeover_sec // 60, "holding_min": self._holding_sec // 60,
            "retention_days": self._retention_days, "catchup_hours": self._catchup_sec // 3600,
        })

    # ── the sender thread: approved drafts, holding replies, clean-ups ────

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("PRM: sending round failed")
            self._wake.wait(SEND_TICK_SEC)
            self._wake.clear()

    def tick(self) -> None:
        self.sweep_executing()
        self.send_approved()
        self.send_holding_replies()
        now = self._clock()
        if now - self._last_retention >= RETENTION_CHECK_SEC:
            self._last_retention = now
            self.purge_due()

    def send_approved(self) -> int:
        """Send every approved packet, oldest first; returns how many went out."""
        sent = 0
        for packet in reversed(self._store.packets((APPROVED,), limit=50)):
            now = self._clock()
            if now - float(packet["decided_at"] or now) > APPROVED_EXPIRE_SEC:
                self._store.move(packet["id"], (APPROVED,), FAILED, outcome="expired", sent_at=now,
                                 error="批准后太久没发出去（机器人停过），没有发送；需要的话重新写一条")
                continue
            if not self._store.move(packet["id"], (APPROVED,), SENDING):
                continue                               # another round took it
            sent += self._deliver(self._store.packet(packet["id"]))
        return sent

    def _deliver(self, packet: dict) -> int:
        """Text first, then the files; stops at the first failure (never sent twice)."""
        tokens = self._store.tokens(packet["party_id"])
        final = packet["final_text"] if packet["final_text"] is not None else packet["text"]
        text = pii.unmask(final, tokens)
        problem = ""
        if pii.unknown_tokens(final, tokens):
            problem = "草稿里有对不上的占位符（号码可能已按规定清除）"
        elif packet["kind"] == "shipping_notice" and not gates.waybill_ok(final, self._waybill(packet)):
            problem = "发货通知里的单号和系统记录对不上"
        if not problem and text.strip():
            noted = self._note_sent(packet["chat_id"], text, packet["id"])
            try:
                if not self._send(packet["chat_id"], text):
                    self._unnote_sent(noted)
                    problem = "文字没发出去"
            except Exception:
                self._unnote_sent(noted)
                logger.exception("PRM: packet %d text not sent", packet["id"])
                problem = "文字发送出错"
        for item in packet["attachments"] if not problem else ():
            path = str(item.get("path") or "")
            if not Path(path).is_file():
                problem = f"文件「{item.get('name')}」在电脑上找不到了"
                break
            noted = self._note_sent(packet["chat_id"], file=True, packet_id=packet["id"])
            try:
                if not self._send_file(packet["chat_id"], path):
                    self._unnote_sent(noted)
                    problem = f"文件「{item.get('name')}」没发出去"
                    break
            except Exception:
                self._unnote_sent(noted)
                logger.exception("PRM: packet %d file not sent", packet["id"])
                problem = f"文件「{item.get('name')}」发送出错"
                break
        now = self._clock()
        if problem:
            if packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL:
                problem = f"平台动作已经核验成功，但回复没发出去：{problem}；请在微信里手动发给达人"
            self._store.move(packet["id"], (SENDING,), FAILED, outcome="failed", sent_at=now, error=problem)
            logger.warning("PRM: packet %d failed: %s", packet["id"], problem)
            self._update_gate(packet, now)
            return 0
        # The backend says the keystrokes went through; seeing the line in WeChat confirms it
        # (often already, before the send returned).
        self._store.mark_sent(packet["id"], now)
        logger.info("PRM: packet %d sent (%d chars, %d file(s))", packet["id"], len(text), len(packet["attachments"]))
        self._update_gate(packet, now)
        return 1

    def _update_gate(self, packet: dict, now: float) -> None:
        """After a person's decision went out: has the kind earned ``auto`` (8.1)?  A platform
        action's reply never counts (it is always reviewed)."""
        kind, route = packet["kind"], packet["route"]
        if route != gates.REVIEW or packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL:
            return
        gate = self._store.gate(kind)
        if gate is None or gate["locked"] or gate["route"] == gates.AUTO:
            return
        stats = gates.window_stats(self._store.reviewed(kind, gate["reset_at"], gate["window"]))
        if gates.earned(gate, stats):
            self._store.set_gate(kind, gates.AUTO, now)
            logger.info("PRM: %s drafts now go out without review (%d of the last %d sent unchanged)",
                        kind, stats["unchanged"], stats["decided"])

    def send_holding_replies(self) -> int:
        """One holding reply per round of the creator's messages whose draft waits too long
        (the Mac's replies only: a system notice, or a next step the bot asked for (the system
        lane: a 专属佣金 waiting for a person), answers nothing the creator asked).  None in a
        round she was answered already (``_answered``: an operator's own line, or something we
        sent, after her latest message)."""
        if self._send is None or self._holding_sec <= 0:
            return 0
        now = self._clock()
        sent, done = 0, set()
        for packet in reversed(self._store.packets((PENDING,), limit=200)):
            if packet["job_id"] is None or packet["deal_id"] in done:
                continue
            if (packet.get("meta") or {}).get("lane") == LANE_SYSTEM:
                continue
            if now - float(packet["created_at"]) < self._holding_sec:
                continue
            deal = self._store.deal(packet["deal_id"])
            if deal is None or self._mode(deal, now) != COPILOT:
                continue
            round_at = float(deal.get("last_message_at") or packet["created_at"])
            if deal.get("holding_round") is not None and float(deal["holding_round"]) >= round_at:
                continue                                   # already told in this round
            if self._answered(deal, packet["chat_id"], round_at):
                continue                                   # she heard from us after her message
            done.add(packet["deal_id"])
            noted = self._note_sent(packet["chat_id"], self._holding_text)
            try:
                ok = bool(self._send(packet["chat_id"], self._holding_text))
            except Exception:
                logger.exception("PRM: holding reply for deal %d failed", deal["id"])
                ok = False
            if not ok:
                self._unnote_sent(noted)
                continue                                   # tried again next round of the thread
            self._store.update_deal(deal["id"], now, holding_round=round_at)
            self._store.add_event(deal["id"], deal["party_id"], "holding_reply",
                                  {"text": self._holding_text, "packet": packet["id"]}, actor="system", now=now)
            sent += 1
            logger.info("PRM: draft %d waited %d min; holding reply sent", packet["id"], self._holding_sec // 60)
        return sent

    def _answered(self, deal: dict, chat_id: str, since: float) -> bool:
        """Did the creator hear from us after her latest message (written at ``since``)?  An
        operator's own line written then or later (even one read late, that took no chat over:
        an operator answered on the phone while the bot was stopped, win review F13), or a reply
        sent since (a draft, a shipping notice; one with nothing in it, as a 专属佣金 step's empty
        reply, does not count)."""
        if any(_event_time(ev) >= since for ev in self._store.events(deal["id"], limit=20, kinds=("operator_message",))):
            return True
        for packet in self._store.sent_since(chat_id, since):
            final = packet["final_text"] if packet["final_text"] is not None else packet["text"]
            if str(final or "").strip() or packet["attachments"]:
                return True
        return False

    def purge_due(self) -> int:
        """Clear the details of every deal done ``PRM_RETENTION_DAYS`` ago (8.2, ``retention.purge_due_at``)."""
        now = self._clock()
        done = 0
        for deal in self._store.deals_to_purge(now, self._retention_days):
            try:
                self.purge(deal, now)
                done += 1
            except Exception:
                logger.exception("PRM: clean-up of deal %d failed; tried again later", deal["id"])
        return done

    def purge(self, deal: dict, now: float) -> dict:
        party = self._store.party(deal["party_id"]) or {}
        chat_id = party.get("wxid", "")
        addresses = retention.address_values(deal.get("slots") or {})

        def rewrite(text: str) -> str:
            return retention.scrub(retention.clear_receivers(text), addresses)

        counts = {"numbers": self._store.purge_tokens(deal["party_id"], pii.masked_value, now)}
        self._store.update_deal(deal["id"], now, replace_slots=retention.cleared_slots(deal.get("slots") or {}),
                                purged_at=now)
        counts["drafts"] = self._store.rewrite_packets(deal["id"], rewrite)
        counts["events"] = self._store.rewrite_events(deal["id"], rewrite)
        if chat_id:
            counts["relay"] = self._relay.store.rewrite_chat(chat_id, PRM, rewrite)
            if self._scrub_messages is not None:
                counts["messages"] = self._scrub_messages(chat_id, rewrite)
            # The Mac's case file: a job the worker does itself (no model); the creator's
            # next message must not join it (input_closed), it is a draft job of its own.
            self._relay.submit(chat_id, chat_id, "", kind=PRM, fresh=True, input_closed=True,
                               meta={"op": "purge", "party_id": deal["party_id"], "deal_id": deal["id"]})
        self._store.add_event(deal["id"], deal["party_id"], "purge", counts, actor="system", now=now)
        logger.info("PRM: deal %d done %d days ago (delivered or quiet); receiving details cleared (%s)", deal["id"],
                    self._retention_days, ", ".join(f"{k} {v}" for k, v in counts.items()))
        return counts


# What of a platform action may hold a creator's own numbers: her receiver (a sample order) and the
# note for the operators.  The rest are platform ids and our own texts, sent as they are.
PERSONAL_PARAMS = ("receiver", "note")


def masked_action(action, mask: Callable[[str], str]):
    """``action`` with ``mask`` run over its ``PERSONAL_PARAMS`` (``_mask_draft``)."""
    if not isinstance(action, dict):
        return action
    out = dict(action)
    if isinstance(out.get("receiver"), dict):
        out["receiver"] = {k: mask(v) for k, v in out["receiver"].items()}
    if "note" in out:
        out["note"] = mask(out["note"])
    return out


def action_for_mac(action: dict, tokens: dict) -> tuple:
    """``(action, problem)``: the approved action as the Mac executes it.  A sample order's
    receiver is the draft's: its phone a 〈手机N〉 (the model never sees numbers; one the Mac read
    off a picture became one too, ``_mask_draft``), perhaps another one in its address; the
    numbers go in here, for the Mac that sends the order and nowhere else (the packet keeps the
    placeholders)."""
    receiver = action.get("receiver") if isinstance(action.get("receiver"), dict) else None
    if not receiver:
        return action, ""
    phone = str(receiver.get("phone") or "")
    unknown = pii.unknown_tokens(phone, tokens)
    if unknown:
        return action, f"收件电话的占位符对不上：{'、'.join(unknown)}（这位达人没有这个号码）"
    try:
        number = platforms.phone_of(pii.unmask(phone, tokens))
    except platforms.ActionError:
        return action, "收件电话换回号码以后不是手机号，不执行"
    if platforms.PHONE_TOKEN.match(number):
        return action, "收件电话还是占位符，不执行"
    unknown = pii.unknown_tokens(" ".join(str(v) for k, v in receiver.items() if k != "phone"), tokens)
    if unknown:
        return action, f"收货信息里的占位符对不上：{'、'.join(unknown)}（这位达人没有这个号码）"
    fields = {k: pii.unmask(v, tokens) if isinstance(v, str) else v for k, v in receiver.items()}
    return {**action, "receiver": {**fields, "phone": number}}, ""


# The 专属佣金 action of each shop: a sample order's products get theirs once it is created.
COMMISSION_ACTIONS = {"dy": "dy.designated_commission", "xhs": "xhs.direct_plan"}
# A creator's 专属 actions: one per product and creator (a second would open a second 百应 strategy,
# 千帆 plan or channel product); ``PrmService._opened_before`` refuses another approval.
ONCE_ACTIONS = ("dy.designated_commission", "xhs.direct_plan", "dy.channel_goods")
REOPEN_HINT = "点「不发」；要接着开下一个商品，把物流改回「运输中」再标「已签收」"


def action_target(action: dict, data: Optional[dict] = None) -> tuple:
    """``(item, creators)`` of a 专属 action: the product, and the creator as the draft named them
    plus, for a 千帆 plan, the buyer id the Mac's preview found (``data``: the packet's ``platform``),
    so a 小红书号 and the userId of one buyer are the same creator."""
    item = str(action.get("item_id") or "").strip()
    creators = {str(action.get("creator_platform_id") or "").strip().lower()}
    buyer = (((data or {}).get("preview") or {}).get("run") or {}).get("buyer_id")
    if buyer:
        creators.add(str(buyer).strip().lower())
    return item, creators - {""}


def _date_text(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at))


def missing_files(named: list, arrived: list, reported: list) -> list:
    """The files a draft named that did not come, for the note on the deal and the draft: what the
    Mac said it could not send back, with why (``reported``: ``[{"name", "why"}]``), else (an older
    worker says nothing) each named one whose file name is not among the ``arrived`` names."""
    if reported:
        return [f"{_file_name(r['name'])}（{r['why']}）" if r.get("why") else _file_name(r["name"]) for r in reported]
    left, out = [str(a) for a in arrived], []
    for name in named:
        base = _file_name(name)
        if base in left:
            left.remove(base)
        else:
            out.append(base)
    return out


def _file_name(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or str(path)


def next_step_note(action: dict, record: dict, *, delivered: bool = False) -> str:
    """What the next draft is told after a verified platform write, or "" when nothing follows:
    a sample order → open the 专属佣金 of its products (``delivered``: told when the creator
    signed for it, PRM_COMMISSION_AFTER); a 专属佣金 → the next product, if any (one action
    per draft, so one product each time; the case file says which are done).  Told again (the
    deal set back before 已签收 and signed for again, e.g. after the chain was cut off), the draft
    starts at the first product not done yet, so no 专属佣金 is opened twice.  A 抖店 order names
    the 抖音 UID 聚水潭 found (``creator_uid``): the draft may only know the creator's 抖音号.
    What to do comes first and the list of products last: a 专场 order's long list (up to
    ``platforms.MAX_ORDER_SKUS`` products) never pushes the instruction out of the note."""
    name = action.get("action")
    if name == "jst.sample_order":
        shop = str(action.get("shop") or "")
        if shop not in COMMISSION_ACTIONS:
            return ""
        created = record.get("created") or {}
        ids = [str(i.get("item_id")) for i in action.get("items") or [] if isinstance(i, dict)]
        if not ids and action.get("item_id"):
            ids = [str(action["item_id"])]
        number = str(created.get("sample_order_no") or "")
        what = f"{platforms.PLATFORMS[shop]}的商品，共 {len(ids)} 个，清单在最后"
        done = (f"达人已经签收了寄样单 {number}（{what}）。" if delivered else
                f"寄样单 {number} 已经在聚水潭建好，寄的是{what}。")
        uid = str(created.get("creator_uid") or "") if shop == "dy" else ""
        who = f"聚水潭建单时查到达人的抖音 UID 是 {uid}，creator_platform_id 就填它。" if uid else ""
        return (done + who +
                f"按 kb/合作政策.md 的「开专属佣金」给这些商品开专属佣金：这一份草稿填第一个还没开的商品的 {COMMISSION_ACTIONS[shop]}"
                "（看案卷，开过的不要再开），把哪些商品还没开记进案卷。"
                f"寄样单上的商品（按顺序）：{'、'.join(ids)}。")
    if name in COMMISSION_ACTIONS.values():
        return (f"商品 {action.get('item_id')} 的专属佣金（{action.get('commission_rate')}%）已经在平台上开好了"
                f"（达人 {action.get('creator_platform_id')}）。"
                "这位达人寄样的商品里还有没开专属佣金的，就给这位达人填下一个；都开完了就不要填平台动作。"
                "按 kb/合作政策.md 的「开专属佣金」更新案卷。")
    return ""


def _job_error(job: dict) -> str:
    """Why a relay job failed, from its result (the worker's ``error``)."""
    try:
        extra = json.loads(job.get("result_json") or "{}")
    except ValueError:
        extra = {}
    extra = extra if isinstance(extra, dict) else {}
    return str(extra.get("error") or job.get("result_text") or "没有返回内容")[:200]


def apply_deal_action(store: PrmStore, action: str, data: dict, *, now: float, takeover_sec: float) -> dict:
    """The console's deal actions (also when the bot is stopped: straight on the database)."""
    try:
        deal_id = data.get("deal_id")
        if action == "stage":
            deals.set_stage(store, deal_id, str(data.get("stage") or ""), now=now)
        elif action == "adopt":
            deals.adopt_proposal(store, deal_id, now=now)
        elif action == "mode":
            deals.set_mode(store, deal_id, str(data.get("mode") or ""), now=now, takeover_sec=takeover_sec)
        elif action == "shipment":
            _, packet_id = deals.add_shipment(store, deal_id, data.get("carrier", ""), data.get("waybill", ""), now=now)
            return {"ok": True, "packet": packet_id}
        elif action == "shipment_status":
            deals.set_shipment_status(store, deal_id, str(data.get("status") or ""), now=now)
        else:
            return {"ok": False, "error": f"不认识的操作 {action}"}
    except deals.DealError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def reveal_numbers(store: PrmStore, party_id: int, *, now: float, actor: str = "console") -> dict:
    """The full numbers behind a creator's placeholders, for an operator (8.2): every look is
    a ``reveal`` event (which placeholders, not the numbers)."""
    party = store.party(party_id)
    if party is None:
        return {"ok": False, "error": "没有找到这个达人"}
    rows = store.token_rows(party_id)
    deal = store.deal_of(party_id, now)
    store.add_event(deal["id"], party_id, "reveal", {"tokens": [r["token"] for r in rows]}, actor=actor, now=now)
    return {"ok": True, "numbers": [
        {"token": r["token"], "value": "" if r["purged_at"] else r["value"], "cleared": bool(r["purged_at"])}
        for r in rows]}
