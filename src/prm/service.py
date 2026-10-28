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

What the deal says to a person (``attention``) is its notices (store.py): the latest draft's own
notes, which the next draft replaces, and the system's, which stay until they are dealt with (mac
review F1): a draft job that gave no draft (her lines in it go with her next draft job: answered,
not lost), a 专属佣金 step owed while a person had the chat or the creator was off the list (asked for
when that ends, ``sweep_owed_steps``), a platform action that ended red (until 已处理), her recall,
messages read too late to draft, this PC's clock off WeChat's (measured by the bot's own lines read back:
``_note_clock``; how late a message is read is reckoned by WeChat's clock then, win review F6).

Registering is always an operator's decision.  When the creator registered is a new friend
(src/wechat/new_friends.py found the line WeChat writes when two people become friends), her
messages since she was added, friend-request greeting included, become one draft job that waits
for a person (``take_new_friend``).  WeChat's friend notices (「我通过了你的朋友验证请求…」 in her
name, 「你已添加了…」) are never drafted, nor are its other lines in her chat (type 10000: a recall,
拍一拍, a red packet notice; ``_system_line``): her recall puts the drafts not sent yet before a person.

A draft with a platform action (platforms.py: 小红书 / 抖音 / 淘宝 CLIs on the Mac) comes
with the Mac's preview of it and always waits for a person, whatever its kind's gate; so does
every draft of a next step the bot asked for (a 专属佣金 step, ``meta.system_note``).  A verified
sample order's products get their 专属佣金 one at a time, the bot keeping the record (``_chain``:
which are open, which a person dropped, which ended red, which are left); a product that cannot be
opened does not hold up the ones after it, and each is asked for once per round (a signature).
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
import itertools
import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from ..edge import text as edge_text
from ..relay.attachments import KINDS as ATTACHMENT_KINDS, Held, attachment_kind
from ..relay.service import MAX_AGE_SEC, job_meta
from ..relay.store import PRM
from ..wechat.new_friends import is_friend_notice
from . import deals, gates, pii, platforms, retention
from .result import MAX_DRAFT_CHARS, MESSAGE_KINDS, DraftError, parse_draft
from .store import (
    APPROVED, COPILOT, DRAFT_NOTES, DROPPED, EXECUTING, FAILED, HUMAN, LANE_REPLY, LANE_SYSTEM, MESSAGE_TOOL, MODES,
    NO_REPLY, NOTICE_CHARS, PAUSED, PENDING,
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
READ_BACK_SEC = 1800         # an own row handled this long ago and handed on again (same id) is no new line
READ_BACK_ROWS = 500         # at most this many own rows a chat remembers
RETENTION_CHECK_SEC = 3600
PLATFORM_LOST_GRACE_SEC = 1800   # an execution whose Mac went silent this long is given up on (red)
PLATFORM_OPS = ("platform_execute", "platform_preview")
HOLDING_TEXT = "收到，我确认一下马上回你"
# Mac 重启以前发的、后来 Windows 才报失败的：没有发送时间时往回找这么久（edge 后端的发送记录留一天）
RESTART_FAIL_SEC = 86400
CATCHUP_HOURS = 12           # PRM_CATCHUP_HOURS: a message read late is drafted up to this old (0-72)
LINES_CHARS = 6000           # the creator's lines a notice of a draft not written keeps for her next draft
OWN_FILE_EARLY_SEC = 60      # a file the bot sent shows in WeChat up to this long before its send returns
# A draft the bot was stopped in the middle of sending (``start``), and what it says once its text was read back
UNSURE_ERROR = "发送时机器人停了，不确定发出去没有；请到微信里看一眼，需要就手动补发"
UNSURE_TEXT_SEEN = ("发送时机器人停了：文字已经发出去了（微信里读到了），素材文件不确定发出去没有；"
                    "请到微信里看一眼，需要就手动补发文件")
# This PC's clock against WeChat's (win review F6): measured when the bot's own line is read back
CLOCK_SAMPLE_SEC = 120       # read back this soon after it was noted, a line of ours measures the clock
CLOCK_SAMPLES = 3            # the smallest of the last this many measurements counts (a late read only adds)
CLOCK_NOISE_SEC = 60         # off by less than this: read-back noise, nothing is corrected
CLOCK_SKEW_SEC = 300         # off by this much or more: the deals say so (``clock_note``)
_QUOTE_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9]+:\s*")
_WXID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{3,63}$")


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
    """What a message says, as one line for the draft: the WeChat edge's line (src/edge/text.py), so the
    Mac is told the same in both deployments (win review F2): a picture, a file by its name (its file
    still goes the attachment way), voice by its words, a link or mini program by its title (and its
    address, once the backend finds one), 视频号, a transfer, a red packet, a location, a card…  A
    message Windows' edge forwarded (``_queued``: the Mac's edge backend) comes with that line already."""
    if _queued(msg):
        line = _QUOTE_PREFIX.sub("", str(msg.get("content", "") or "")).strip()
        if line:
            return line
    return edge_text.message_text(msg)


def _norm(text: str) -> str:
    return " ".join(str(text or "").split())


_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".heic", ".heif", ".tif", ".tiff")
_VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".avi", ".wmv", ".mkv", ".flv", ".3gp")


def file_kind(name: str) -> str:
    """What WeChat shows a file the bot pastes into a chat as, by its name: ``image`` (a picture),
    ``gif`` (a picture, or a sticker), ``video`` or ``file``."""
    suffix = Path(str(name or "")).suffix.lower()
    if suffix == ".gif":
        return "gif"
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _VIDEO_SUFFIXES:
        return "video"
    return "file"


def own_file_fits(kind: str, msg: dict) -> bool:
    """Can this own line in WeChat be a file of ``kind`` (``file_kind``) the bot sent (win review F4 (b))?
    A picture (type 3) is an image's or a GIF's, a sticker (47) only a GIF's, a video (43) a video's,
    a file (49 with a file name, or one the backend could not read) a file's, or an image's or video's
    WeChat sent as a file (its name says so); a voice message, a link, a mini program, a card … never.
    The Mac's ``edge_seen`` lines are our sends Windows already read back (their type says only "a file")."""
    if msg.get("edge_seen"):
        return True
    msg_type = edge_text.msg_type_of(msg)
    if msg_type == 3:
        return kind in ("image", "gif")
    if msg_type == 47:
        return kind == "gif"
    if msg_type == 43:
        return kind == "video"
    if msg_type == 49:
        name = str(msg.get("file_name") or "").strip()
        if name:
            return kind in ("file", file_kind(name))
        special = msg.get("special") if isinstance(msg.get("special"), dict) else {}
        appmsg = str(special.get("type") or "")
        return kind == "file" and appmsg in ("", "6")      # a file the backend could not read the name of
    return False


def _row_key(msg: dict) -> Optional[str]:
    """Which WeChat row an own line is, to know it when it is handed on again (``_match_own``): its
    message id (the WCDB backend's never changes for a row; the Mac's edge lines carry Windows' id, or
    their event's); without one, its type, words and the second WeChat says it was written (a row
    read twice has the same; the same words typed later have another).  None when there is nothing to
    know it by (no id, no time): such a line is never taken for a row read again."""
    msg_id = str(msg.get("message_id") or "").strip()
    if msg_id:
        return f"id:{msg_id}"
    event_id = msg.get("edge_event_id")
    if event_id not in (None, ""):
        return f"event:{event_id}"
    try:
        written = int(float(msg.get("timestamp") or 0))
    except (TypeError, ValueError):
        written = 0
    if not written:
        return None
    words = _norm(_QUOTE_PREFIX.sub("", str(msg.get("content", "") or ""))) or str(msg.get("file_name") or "")
    return f"line:{edge_text.msg_type_of(msg)}|{words}|{written}"


def _clock_text(at: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(at))


def late_note(earliest: float) -> str:
    """The deal's attention for a draft of messages read late (PRM_CATCHUP_HOURS)."""
    return f"这次的消息是机器人没在读微信时收到的（最早 {_clock_text(earliest)}），草稿要人看过再发"


def missed_note(count: int, earliest: float) -> str:
    """The deal's attention for messages too old to draft (older than PRM_CATCHUP_HOURS when read)."""
    return f"{count} 条消息在机器人没在读微信时收到（最早 {_clock_text(earliest)}），没有起草，请看一下"


def recall_note(at: float) -> str:
    """The deal's attention (and a draft's reason) when the creator took a message back."""
    return f"达人撤回了一条消息（{_clock_text(at)}），待审批的草稿可能是按撤回的内容写的，发之前请核对"


def clock_note(skew: float) -> str:
    """The deals' notice while this PC's clock is off WeChat's by CLOCK_SKEW_SEC or more (``skew``: this
    PC's clock minus WeChat's, as the bot's own lines read back measured it)."""
    hours, minutes = divmod(int(round(abs(skew) / 60)), 60)
    amount = (f"{hours} 小时" if hours else "") + (f"{minutes} 分钟" if minutes or not hours else "")
    return (f"这台电脑的时间比微信{'快' if skew > 0 else '慢'} {amount}（机器人发出的消息读回来时测的），"
            "消息是不是晚到已经按微信的时间算；请把电脑时间改对（Windows 设置 → 时间和语言 → 自动设置时间），"
            "改好以后机器人再发出消息时会重新测，这条提醒自己消失")


def failed_text(head: str, into: Optional[int] = None) -> str:
    """The notice of a draft job that gave no draft: what went wrong, and where her lines go."""
    return f"{head}；" + (f"她这几条消息已经并进 #{into} 一起起草" if into else
                         "她这几条消息会并进她下一条消息的草稿一起回，也可以直接在微信里回她")


def _merged_gap(have, info: dict) -> dict:
    """Two of a job's ``meta.late`` / ``missed`` / ``new_friend``: the earliest ``from``, counts added."""
    if not isinstance(have, dict) or not have.get("from"):
        return dict(info)
    out = {**have, "from": min(float(have["from"]), float(info.get("from") or have["from"]))}
    if "count" in have or "count" in info:
        out["count"] = int(have.get("count") or 0) + int(info.get("count") or 0)
    return out


def system_line(msg: dict) -> Optional[str]:
    """The words of one of WeChat's own lines in a chat (type 10000 / 10002: a recall, 拍一拍, a red
    packet or transfer notice …), or None for a line someone wrote.  Their sender is empty, so the
    Windows backend hands them on as the creator's (win review F1).  WeChat's lines about becoming
    friends are ``is_friend_notice``'s, handled before this."""
    return edge_text.notice_text(msg) if edge_text.is_notice(msg) else None


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
    "recall": "撤回了一条消息", "draft_failed": "草稿没写成", "next_step": "派开专属佣金的任务",
    "platform_recheck": "平台动作要先核对",
}
# A creator's message recorded without a draft because it was read too late (PRM_CATCHUP_HOURS).
MISSED_EVENT_NAME = "机器人没在读时达人的消息（没有起草）"


def event_out(ev: dict) -> dict:
    """An event for the console: what happened, by whom, when (texts kept as stored: masked)."""
    detail = dict(ev.get("detail") or {})
    name = (MISSED_EVENT_NAME if ev["kind"] == "creator_message" and detail.get("missed")
            else EVENT_NAMES.get(ev["kind"], ev["kind"]))
    if ev["kind"] == "recall":
        name = "运营撤回了一条消息" if detail.get("by") == "operator" else "达人撤回了一条消息"
    elif ev["kind"] == "draft_failed" and detail.get("job"):
        name = f"#{detail['job']} 草稿没写成"
    elif ev["kind"] == "next_step" and detail.get("job"):
        name = f"派开专属佣金的任务 #{detail['job']}" + (f"（商品 {detail['item']}）" if detail.get("item") else "")
    elif ev["kind"] == "platform_recheck" and detail.get("packet"):
        name = f"#{detail['packet']} 的结果不确定，#{detail.get('for')} 要再开同一个商品：先核对平台"
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
        "recheck_for": (data.get("recheck") or {}).get("for"),
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
        # Starting a 专属佣金 chain: looking whether it is under way and asking for its step is one step
        # (the console's 已签收 and the sender thread's sweep_deliveries must not both ask)
        self._chain_lock = threading.RLock()
        # The admin and the cc users are never creators: their messages stay the relay's.
        self._reserved = {str(w).strip() for w in [getattr(config, "admin_wxid", ""),
                                                    *(getattr(config, "claude_relay_wxids", None) or [])]
                          if str(w).strip()}
        self._wxids = self._store.active_wxids()
        # What the bot itself sent lately, per chat: (normalized text, when, packet id); each file
        # (kind, when, packet id, n): one own row of its kind each (win review F4 (b)).
        self._sent_lines: dict[str, deque] = {}
        self._sent_files: dict[str, deque] = {}
        self._file_seq = itertools.count(1)
        # A packet sent before this run (or cut off by a stop): the kinds of its files not read back yet.
        self._file_rows: dict[int, list] = {}
        # Per chat, the own rows handled lately (``_row_key`` -> when): handed on again within
        # READ_BACK_SEC they are no new line (_match_own).
        self._read_back: dict[str, dict[str, float]] = {}
        self._sent_lock = threading.Lock()
        self._started = clock()               # what this run sent is in _sent_lines / _sent_files
        # This PC's clock minus WeChat's (win review F6), measured by this run's own lines read back
        # (``_note_clock``); 0 until then and within CLOCK_NOISE_SEC.  ``_local`` puts WeChat's times
        # on this PC's clock wherever they are held against it (how late a message is read, a takeover).
        self._clock_samples: deque = deque(maxlen=CLOCK_SAMPLES)
        self._skew = 0.0
        self._clock_text: Optional[str] = None     # the deals' notice as last written (None: not this run)
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
        """Settle what a stopped bot left half done (a send cut off; a deal marked 已签收 meanwhile:
        ``sweep_deliveries``), then the sender thread."""
        now = self._clock()
        for p in self._store.packets((SENDING,), limit=500):
            # (read back after all, its line confirms it: ``_confirm``)
            self._store.move(p["id"], (SENDING,), FAILED, outcome="indeterminate", sent_at=now, error=UNSURE_ERROR)
        try:
            started = self.sweep_deliveries()
            if started:
                logger.info("PRM: %d deal(s) signed for while the bot was stopped: their 专属佣金 step asked for", started)
        except Exception:
            logger.exception("PRM: the deals signed for while the bot was stopped could not be looked at")
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

    # ── this PC's clock against WeChat's (win review F6) ──────────────────

    def _local(self, written: float) -> float:
        """When WeChat says something was written, on this PC's clock (``_skew``, what this run measured;
        nothing before it measured anything)."""
        return written + self._skew if written else written

    @staticmethod
    def _clock_sample(msg: dict, noted: float, now: float) -> Optional[float]:
        """What one of our lines read back says of this PC's clock: read (now, this PC) minus written
        (WeChat's time).  Only a line read back within CLOCK_SAMPLE_SEC of being noted (the delay adds to
        it), never the Mac's edge lines (their time is when Windows handed them on)."""
        if msg.get("edge_seen") or msg.get("edge_operator"):
            return None
        try:
            written = float(msg.get("timestamp") or 0)
        except (TypeError, ValueError):
            return None
        if not written or now - noted > CLOCK_SAMPLE_SEC:
            return None
        return now - written

    def _note_clock(self, sample: float, now: float) -> None:
        """A measurement of this PC's clock (``_clock_sample``).  The smallest of the last CLOCK_SAMPLES
        counts (a read-back delayed by a stalled database only adds to one; a clock put right counts at
        once); off by CLOCK_NOISE_SEC or more it corrects WeChat's times (``_local``); off by CLOCK_SKEW_SEC
        or more every deal says so (``clock_note``), until a measurement says it is right again."""
        with self._sent_lock:
            self._clock_samples.append(sample)
            low = min(self._clock_samples)
            skew = low if abs(low) >= CLOCK_NOISE_SEC else 0.0
            if skew != self._skew:
                logger.warning("PRM: this PC's clock is %+.0f s off WeChat's (measured by our own line read back)",
                               low)
            self._skew = skew
        text = clock_note(skew) if abs(skew) >= CLOCK_SKEW_SEC else ""
        if text == self._clock_text:
            return
        self._clock_text = text
        active = {p["id"] for p in self._store.parties()}
        for deal in self._store.deals():
            have = next((n for n in deal["notices"] if n["key"] == "clock"), None)
            if (text and deal["party_id"] in active and (have is None or have.get("text") != text)
                    or not text and have is not None):
                # (about this PC, not the deal: not a sign of life for the 8.2 clean-up)
                self._store.set_notice(deal["id"], "clock", text, now, touch=False)

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
        line = system_line(msg)
        if line is not None:
            self._system_line(msg, party, line)            # a recall, 拍一拍 …: WeChat's, not hers either
            return True
        self._take(msg, party, chat_id)
        return True

    def on_chat_notice(self, chat_id: str, text: str, at: Optional[float] = None, event_id=None) -> None:
        """Mac 端（edge 后端）：Windows 的微信边缘报来的私聊系统提示（``chat_notice``：撤回、拍一拍…；
        新好友那几句提示也是这样报的）。和 Windows 本机读到的系统行一样处理（``_system_line``）；
        新好友的提示照旧不管。"""
        chat_id = str(chat_id or "")
        if not self.is_party_chat(chat_id):
            return
        party = self._store.party_by_wxid(chat_id)
        msg = {"chat_id": chat_id, "msg_type": edge_text.SYSTEM_TYPE, "content": str(text or ""),
               "special": {"kind": "system", "title": str(text or "")}, "timestamp": at or 0, "is_group": False,
               "edge_event_id": event_id if event_id not in (None, "") else "chat_notice"}
        if party is None or is_friend_notice(msg):
            return
        self._system_line(msg, party, str(text or ""))

    def _system_line(self, msg: dict, party: dict, line: str, own: bool = False) -> None:
        """One of WeChat's own lines in a creator's chat (win review F1): never a draft, and the drafts
        waiting stay as they are (it is no new message from her).  Her recall: the drafts not sent yet
        wait for a person (they may answer what she took back), so does the draft of a job made before
        it (``on_result``), and the deal says so.  Our own recall (「你撤回了一条消息」, ``own``: handed
        on as this account's line) is only recorded; any other line (拍一拍, a red packet notice) only
        logged.  Older than a message of hers would still be drafted (PRM_CATCHUP_HOURS): history."""
        now = self._clock()
        written = float(msg.get("timestamp", 0) or 0)
        age = now - self._local(written) if written else 0.0
        written = written or now
        if age > MAX_AGE_SEC and ((not self._catchup_sec and not _queued(msg))
                                  or (self._catchup_sec and age > self._catchup_sec)):
            return
        who = edge_text.recall_by(line)
        if not who:
            logger.info("PRM: a WeChat system line in party %d's chat; not drafted", party["id"])
            return
        ours = own or who == "us"
        with self._handle_lock:
            deal = self._store.deal_of(party["id"], now)
            self._store.add_event(deal["id"], party["id"], "recall", {"by": "operator" if ours else "creator",
                                                                       "at": written},
                                  actor="operator" if ours else "creator", now=now)
            if ours:
                logger.info("PRM: an operator recalled a line in party %d's chat; recorded", party["id"])
                return
            note = recall_note(written)
            held = self._store.hold_for_review(deal["id"], note, now)
            # until an operator decides on one of her drafts or answers her by hand (``_person_decided``)
            self._store.set_notice(deal["id"], "recall", note, now, recalled_at=written)
        logger.info("PRM: party %d recalled a message; %d draft(s) not sent yet wait for a person", party["id"], held)

    def _recalled_since(self, deal: dict, since: float) -> Optional[float]:
        """When the creator took a message back, if a draft made at ``since`` must wait for a person for
        it: her latest recall came at ``since`` or later (a job made before it may hold what she
        recalled), or no operator has seen to her recall yet (its notice is there); else None."""
        for ev in self._store.events(deal["id"], limit=10, kinds=("recall",)):
            if ev["detail"].get("by") == "creator":
                if float(ev["at"]) >= since:
                    return float(ev["detail"].get("at") or ev["at"])
                break
        notice = next((n for n in deal.get("notices") or [] if n.get("key") == "recall"), None)
        return float(notice.get("recalled_at") or notice["at"]) if notice else None

    def _take(self, msg: dict, party: dict, chat_id: str) -> None:
        """``handle`` for a registered creator's message.  What is recorded and the job it joins
        are decided under ``_handle_lock``; a picture or file is fetched outside it (that may wait
        for the WeChat window, and the backend's own lines wait for the lock)."""
        now = self._clock()
        sent_at = float(msg.get("timestamp", 0) or 0)
        age = now - self._local(sent_at) if sent_at else 0.0     # (on this PC's clock: win review F6)
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
        registered, one job of their own that waits for a person.

        Her lines of the reply drafts this message puts out of date (they waited in 待审批, nobody sent
        them: ``_outdated``) and of draft jobs that gave no draft (``failed:`` notices, mac review F1) go
        before it, oldest job first, so the new draft answers them too; the job is told those drafts
        never went out (``meta.sent``, ``_outbound``'s ``unsent``).  Before (real machine, 2026-10-28) the
        new job had only her new line: 「请问佣金比例是多少呀」's draft waited, she sent a 拍一拍, and the
        question was never answered."""
        deal = self._store.deal_of(party["id"], now)
        text = self._mask(party["id"], f"[{_clock_text(at)}] {line}", now)
        waiting = self._store.deal_packets(deal["id"], statuses=(PENDING,))
        stale = self._store.supersede(deal["id"], now, lane=LANE_REPLY)   # a 专属佣金 step stays
        outdated = self._outdated(waiting, now) if stale else []
        self._store.update_deal(deal["id"], now, last_message_at=_later(deal, at))
        meta = {"party_id": party["id"], "deal_id": deal["id"], "name": party["name"]}
        outbound = self._outbound(deal["id"], party["id"], now, unsent=[packet for packet, _, _ in outdated])
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
        # Her lines of draft jobs that gave no draft (mac review F1), and of the drafts this message put out
        # of date (``outdated``): answered with this one, before it, oldest job first.
        carried = sorted((n for n in deal["notices"] if str(n["key"]).startswith("failed:")
                          and n.get("lane") == LANE_REPLY and (n.get("lines") or n.get("history"))
                          and (not n.get("into") or self._cancelled(n["into"]))), key=lambda n: int(n.get("job") or 0))
        earlier = sorted([*((int(n.get("job") or 0), str(n["lines"])) for n in carried if n.get("lines")),
                          *((int(job["id"]), lines) for _, job, lines in outdated)], key=lambda pair: pair[0])
        if earlier:
            text = "\n".join([*(lines for _, lines in earlier), text])
        if carried:
            told = [h for n in carried for h in n.get("history") or [] if isinstance(h, dict)]
            if told:
                meta["history"] = [*told, *meta.get("history", [])]
        for gap in [n.get("gap") or {} for n in carried] + [job_meta(job) for _, job, _ in outdated]:
            for key, info in gap.items():
                if key in ("late", "missed", "new_friend") and isinstance(info, dict):
                    meta[key] = _merged_gap(meta.get(key), info)
        if carried or outdated:
            fresh = True
        how, job_id = self._relay.submit(chat_id, chat_id, text, kind=PRM, files=files, settle_sec=SETTLE_SEC,
                                         meta=meta, fresh=fresh)
        for n in carried:
            self._store.set_notice(deal["id"], n["key"], failed_text(n.get("head") or n["text"], job_id), now,
                                   into=job_id)
        logger.info("PRM: party %d message -> job #%d (%s)%s%s%s%s", party["id"], job_id, how,
                    f"; {stale} waiting draft(s) out of date" if stale else "",
                    f"; {len(backlog)} recorded line(s) told (a takeover, or read too late)" if backlog else "",
                    f"; the lines of {len(carried)} draft job(s) that gave no draft go along" if carried else "",
                    f"; the lines of {len(outdated)} draft(s) not sent go along" if outdated else "")

    def _outdated(self, waiting: list, now: float) -> list:
        """The Mac's reply drafts of ``waiting`` (the deal's drafts in 待审批 a moment ago) that a message of hers
        has just put out of date (``superseded`` now: nobody sent them), with their jobs and her lines in them
        (the job's text and the follow-ups the Mac had: ``_job_lines``): ``[(packet, job, lines)]``.  Not a
        专属佣金 step's (the system lane is no reply of hers), nor one approved or dropped meanwhile."""
        out = []
        for packet in waiting:
            if packet.get("job_id") is None or (packet.get("meta") or {}).get("lane") == LANE_SYSTEM:
                continue
            packet = self._store.packet(packet["id"]) or packet
            if packet["status"] != SUPERSEDED or packet.get("decided_at") != now:
                continue
            job = self._relay.store.get(int(packet["job_id"]))
            lines = self._job_lines(job) if job is not None else ""
            if lines:
                out.append((packet, job, lines))
        return out

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
        self._store.update_deal(deal["id"], now, last_message_at=_later(deal, at))
        # until the draft job that hears of them comes back (on_result)
        self._store.set_notice(deal["id"], "missed", missed_note(len(waiting) or 1, earliest), now)
        logger.warning("PRM: party %d's message was read %.1f h after it was written (more than "
                       "PRM_CATCHUP_HOURS); recorded, not drafted, the deal asks a person to look",
                       party["id"], (now - at) / 3600)

    def _outbound(self, deal_id: int, party_id: int, now: float, unsent: list = ()) -> list[dict]:
        """What the creator really got from us lately, oldest first, for the next draft: the Mac's
        case file holds the drafts as Claude wrote them, but an operator may have edited one
        before it went out, dropped one, or the send failed; holding replies are the bot's own.
        Told with every draft job (not once: a superseded job must not take it along); numbers
        masked like the creator's words.  ``unsent``: drafts her new message just put out of date
        (``_submit``): told to the job that answers their lines as not sent (``what: failed``, the
        worker's 「这一条没发出去，达人没看到」; ``dropped`` would say an operator chose not to send it)."""
        items = []
        for p in unsent:
            items.append({"at": float(p.get("created_at") or 0), "kind": p["kind"], "what": "failed",
                          "text": p["final_text"] if p.get("final_text") is not None else p["text"]})
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
        late = bool(sent_at) and now - self._local(sent_at) > MAX_AGE_SEC
        if late and not self._catchup_sec:
            if not _queued(msg):
                return                                     # history (a chat read for the first time)
            late = False                                   # Windows' queue never replays history
        line = system_line(msg)
        if line is not None:                               # 「你撤回了一条消息」…: WeChat's, no takeover
            self._system_line(msg, party, line, own=True)
            return
        with self._handle_lock:
            self._own_line(msg, party, chat_id, now, sent_at, late)

    def _own_line(self, msg: dict, party: dict, chat_id: str, now: float, sent_at: float, late: bool) -> None:
        """``on_own_message`` for a creator's chat (under ``_handle_lock``)."""
        matched, packet_id = self._match_own(chat_id, msg, now)
        if msg.get("edge_seen") and not matched:
            return                                         # 我们发的读回来了，只是这次运行没记着它
        if matched:
            if packet_id is not None:
                self._confirm(packet_id, msg, self._local(sent_at) or now, now)
            return
        deal = self._store.deal_of(party["id"], now)
        text = self._mask(party["id"], f"[{_clock_text(sent_at or now)}] {message_text(msg)}", now)
        # ``at``: when it was written (read late, it is still an answer to what she wrote before it)
        self._store.add_event(deal["id"], party["id"], "operator_message", {"text": text, "at": sent_at or now},
                              actor="operator", forwarded=False, now=now)
        self._store.supersede(deal["id"], now, lane=LANE_REPLY)
        self._answered_by_hand(deal["id"], sent_at or now, now)
        mode = deals.effective_mode(deal, now)
        if mode == PAUSED:
            logger.info("PRM: an operator wrote to party %d from WeChat; the deal stays paused", party["id"])
            return                                         # paused stays paused (the line is recorded)
        # Typed while the bot was not reading: the chat is a person's as long as it would have
        # been, counted from when the line was written (a later takeover is not cut short).
        until = (self._local(sent_at) if late else now) + self._takeover_sec
        if until <= now:
            logger.info("PRM: an operator wrote to party %d from WeChat while the bot was not reading, %.0f min "
                        "ago; recorded, too long ago to take the chat over", party["id"],
                        (now - self._local(sent_at)) / 60)
            return
        if mode == HUMAN and until <= float(deal.get("mode_until") or 0):
            logger.info("PRM: an operator's earlier line in party %d's chat recorded; the takeover goes on",
                        party["id"])
            return
        self._store.update_deal(deal["id"], now, mode=HUMAN, mode_until=until)
        self._store.add_event(deal["id"], party["id"], "takeover", {"until": until}, actor="system", now=now)
        logger.info("PRM: an operator wrote to party %d from WeChat; the deal is a person's until %s",
                    party["id"], time.strftime("%H:%M", time.localtime(until)))

    def _confirm(self, packet_id: int, msg: dict, written: float, now: float) -> None:
        """A packet's line (or file) read back from WeChat.  Going out or sent: ``sent_confirmed``, often
        before its send returned (WeChat shows a line at once); a failure stays one.  A packet the bot was
        stopped in the middle of sending (failed, ``indeterminate``: ``start``) went out after all (win
        review F4 (d); before, its line was taken for an operator's and the chat was taken over): text
        only, it is sent again (``sent_at`` when WeChat shows it); with files, whether they went too is
        still unknown, so it stays red and says its text did go out."""
        if self._store.confirm_sent(packet_id):
            packet = self._store.packet(packet_id) or {}
            logger.info("PRM: packet %d seen in WeChat %.0f s after sending (sent_confirmed)", packet_id,
                        max(0.0, now - float(packet.get("sent_at") or now)))
            return
        packet = self._store.packet(packet_id)
        if packet is None or (packet["status"], packet["outcome"]) != (FAILED, "indeterminate"):
            return
        final = packet["final_text"] if packet["final_text"] is not None else packet["text"]
        text_seen = bool(self._own_text(msg)) or packet["error"] == UNSURE_TEXT_SEEN
        with self._sent_lock:
            files_left = self._file_rows.get(packet_id, [None]) if packet["attachments"] else []
        if (text_seen or not str(final or "").strip()) and not files_left:
            if self._store.settle_unsure(packet_id, sent_at=written):
                logger.info("PRM: packet %d, cut off by a stop, did go out (seen in WeChat): sent", packet_id)
        elif text_seen and packet["error"] != UNSURE_TEXT_SEEN and self._store.settle_unsure(
                packet_id, error=UNSURE_TEXT_SEEN):
            logger.info("PRM: packet %d, cut off by a stop: its text went out, its files are not all seen yet",
                        packet_id)

    def _answered_by_hand(self, deal_id: int, written: float, now: float) -> None:
        """An operator wrote to her by hand (at ``written``): a draft of hers that was not written, and
        her recall, are seen to by a person.  The first is shown no more (its lines still go with her next
        draft job: the case file must hear of them); the recall's notice goes."""
        def edit(notices):
            out = []
            for n in notices:
                if n["key"] == "recall" and written >= float(n.get("recalled_at") or n.get("at") or 0):
                    continue
                if (str(n["key"]).startswith("failed:") and n.get("lane") == LANE_REPLY
                        and written >= float(n.get("asked_at") or 0)):
                    n = {**n, "show": False}
                out.append(n)
            return out

        self._store.edit_notices(deal_id, edit, now)

    def _note_sent(self, chat_id: str, text: str = "", packet_id: Optional[int] = None,
                   file: "bool | str" = False) -> tuple:
        """What the bot is about to send, noted before it goes (WeChat shows a line, and the
        backend reads it back, before the send returns): a line, or one file (``file``: its path or
        name, whose kind is what WeChat shows it as, ``file_kind``; True: a plain file).  Returns the
        note; ``_unnote_sent`` takes it back."""
        now = self._clock()
        with self._sent_lock:
            if file:
                entry = (file_kind(file) if isinstance(file, str) else "file", now, packet_id, next(self._file_seq))
                self._sent_files.setdefault(chat_id, deque(maxlen=200)).append(entry)
                return chat_id, None, entry
            item = (_norm(text), now, packet_id)
            self._sent_lines.setdefault(chat_id, deque(maxlen=50)).append(item)
            return chat_id, item, None

    def _unnote_sent(self, noted: tuple) -> None:
        """It did not go out: forget that line or that file (the files sent before it still count as
        the bot's; not when it was read back already)."""
        chat_id, item, entry = noted
        with self._sent_lock:
            if entry is not None:
                files = self._sent_files.get(chat_id)
                if files is not None and entry in files:
                    files.remove(entry)
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

    def _match_own(self, chat_id: str, msg: dict, now: float) -> tuple[bool, Optional[int]]:
        """Was this own line sent by the bot? ``(True, packet id or None)`` (``_match_sent``).

        A row handled before and handed on again (the same ``_row_key``: the same message id, which
        the WCDB backend never changes for a row, src/wechat/wcdb_backend.py ``_message_ids``) is no
        new line: ``(True, None)``, nothing more is done with it, ours or an operator's (2026-10-27:
        WeChat 4.1 fills in a line's server id a second after writing it, and the ids that used it
        handed the bot's row on twice; an event the Mac gets twice).  Only the same row: the same
        words in another row are matched afresh, a person's when no send of ours is waiting for them
        (win review F4 (a): before, the same words within 120 s of a read-back were swallowed, and an
        operator answering 「好的」 as the bot just had took nothing over).  The Mac's ``edge_operator``
        lines were compared with the sends on Windows already: only a row handed on again is looked for."""
        key = _row_key(msg)
        with self._sent_lock:
            rows = self._read_back.setdefault(chat_id, {})
            for k, at in list(rows.items()):
                if now - at > READ_BACK_SEC:
                    del rows[k]
            if key is not None and key in rows:
                logger.info("PRM: a line in %s…'s chat handed on again %.0f s after it was handled (the same row); "
                            "nothing new", chat_id[:8], now - rows[key])
                return True, None
            if key is not None:
                rows[key] = now
                while len(rows) > READ_BACK_ROWS:
                    del rows[next(iter(rows))]
            if not rows:
                del self._read_back[chat_id]
        if msg.get("edge_operator"):
            return False, None             # Mac 端：Windows 已经用发送记录比过了，对不上才报 operator_message
        return self._match_sent(chat_id, msg, now)

    def _match_sent(self, chat_id: str, msg: dict, now: float) -> tuple[bool, Optional[int]]:
        """Was this own line sent by the bot (``_match_own``)?  What was sent before this run is
        looked for around the time the line was written (read late after a restart: the bot
        sent it just before it stopped)."""
        try:
            msg_type = int(msg.get("msg_type", 1) or 1)
        except (TypeError, ValueError):
            msg_type = 1
        written = min(now, self._local(float(msg.get("timestamp", 0) or 0)) or now)
        sample = None
        with self._sent_lock:
            if msg_type != 1:
                # Each file sent covers one row of its own kind, the oldest first (win review F4 (b):
                # before, any picture, voice message or sticker for OWN_FILE_SEC after a file was ours)
                files = self._sent_files.get(chat_id) or deque()
                for entry in list(files):
                    if now - entry[1] > OWN_FILE_SEC:
                        files.remove(entry)
                for entry in files:
                    if own_file_fits(entry[0], msg):
                        files.remove(entry)
                        return True, entry[2]
            else:
                text = _norm(_QUOTE_PREFIX.sub("", str(msg.get("content", "") or "")))
                lines = self._sent_lines.get(chat_id) or deque()
                for item in list(lines):
                    if now - item[1] > OWN_MATCH_SEC:
                        lines.remove(item)
                    elif item[0] == text:
                        lines.remove(item)
                        sample = (self._clock_sample(msg, item[1], now), item[2])
                        break
        if sample is not None:
            if sample[0] is not None:
                self._note_clock(sample[0], now)
            return True, sample[1]
        # Sent before this run (the bot restarted in between): the database, but only what
        # WeChat has not shown yet; this run's sends are all in memory, so an operator typing
        # the same words later is still seen as a person.
        party = self._store.party_by_wxid(chat_id)
        if party is None:
            return False, None
        # A draft the bot was stopped in the middle of sending (``indeterminate``: settled by ``start``
        # when this run began, after its line was written) is looked for too (win review F4 (d)).
        unsure = self._store.unsure_since(chat_id, written - OWN_MATCH_SEC)
        if msg_type != 1:
            # A file of a draft sent just before a restart: WeChat shows it while the draft's
            # files are going out, before its send returns (``sent_at``).
            before = [p for p in self._store.sent_since(chat_id, written - OWN_FILE_EARLY_SEC)
                      if p["attachments"] and float(p["sent_at"]) < self._started
                      and float(p["sent_at"]) <= written + OWN_FILE_SEC]
            for packet in [*before, *(p for p in unsure if p["attachments"])]:
                if self._file_row_of(packet, msg):
                    return True, packet["id"]
            return False, None
        tokens = self._store.tokens(party["id"])
        for packet in self._store.sent_since(chat_id, written - OWN_MATCH_SEC):
            if packet["outcome"] != "sent_unconfirmed" or float(packet["sent_at"]) >= self._started:
                continue
            final = packet["final_text"] if packet["final_text"] is not None else packet["text"]
            if _norm(pii.unmask(final, tokens)) == text:
                return True, packet["id"]
        for packet in unsure:
            final = packet["final_text"] if packet["final_text"] is not None else packet["text"]
            if str(final or "").strip() and _norm(pii.unmask(final, tokens)) == text:
                return True, packet["id"]
        deal = self._store.deal_of(party["id"], now)
        for ev in self._store.events(deal["id"], limit=20):
            if (ev["kind"] == "holding_reply" and written - OWN_MATCH_SEC <= ev["at"] < self._started
                    and _norm(ev["detail"].get("text")) == text):
                return True, None
        return False, None

    def _file_row_of(self, packet: dict, msg: dict) -> bool:
        """Is this own row one of the files of ``packet``, sent before this run (``_match_sent``)?  Each
        of its files covers one row of its kind, never more (``_file_rows`` counts them down)."""
        with self._sent_lock:
            left = self._file_rows.get(packet["id"])
            if left is None:
                left = self._file_rows[packet["id"]] = [file_kind(a.get("name") or a.get("path") or "")
                                                        for a in packet["attachments"]]
            for n, kind in enumerate(left):
                if own_file_fits(kind, msg):
                    del left[n]
                    return True
        return False

    def on_send_failed(self, chat_id: str, text: str, why: str, *, digest: str = "",
                       sent_at: Optional[float] = None, file: bool = False) -> None:
        """Mac 端：Windows 报这条没发出去（失败、过期、不确定）。对应的草稿从「已发出」改成「没发出去」，
        运营在待审批页能看到原因；「稍等」这类不是草稿的，只记日志。

        ``digest``：Mac 重启以前发的，edge 后端只留着发出去的原文的 sha256（原文不写盘），``text`` 是空的：
        按摘要对草稿（换回号码以后的全文），从 ``sent_at`` 前后找起，不受 OWN_MATCH_SEC 限制。
        ``file``：没发出去的是素材文件，``sent_at`` 是请 Windows 发它的时间：找那之后最先发完、带文件的草稿
        （文字可能已经发出去、读回了，照样标红，原因里写明文字发了）。"""
        now = self._clock()
        if file:
            self._file_send_failed(chat_id, why, sent_at, now)
            return
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

    def _file_send_failed(self, chat_id: str, why: str, sent_at: Optional[float], now: float) -> None:
        party = self._store.party_by_wxid(chat_id)
        if party is None:
            return
        asked = sent_at or now - RESTART_FAIL_SEC
        # 一条草稿先发文字、再一个个发文件，都交给 Windows 以后才记 sent_at：文件请求的时间不晚于它
        packets = [p for p in self._store.sent_since(chat_id, asked - 1)
                   if p["attachments"] and p["status"] == SENT]
        if not packets:
            logger.warning("PRM: a file send to party %d failed (%s); no sent draft with files matches it",
                           party["id"], why)
            return
        packet = min(packets, key=lambda p: float(p["sent_at"] or 0))
        error = f"素材文件没发出去（{why}）"
        if any(str(packet.get(k) or "").strip() for k in ("final_text", "text")):
            error += "；文字已经发出去了，只要在微信里补发文件"
        if self._store.move(packet["id"], (SENT,), FAILED, outcome="failed", error=error[:200]):
            logger.warning("PRM: packet %d: a file was not sent by Windows: %s", packet["id"], why)

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
            # (an operator cancelled it in the console): her lines it took along wait for the next one again
            self._store.edit_notices(deal_id, lambda notices: [
                {**n, "into": None, "text": failed_text(n.get("head") or n["text"])} if n.get("into") == job["id"]
                else n for n in notices], now)
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
            self._draft_failed(job, meta, f"#{job['id']} 草稿没写成：{why[:120]}", notes, now)
            if meta.get("system_note"):
                self._chain_refresh(deal_id, now)      # (not written: the chain waits for a person)
            logger.warning("PRM: job #%d %s; the deal needs a person", job["id"], status)
            return True
        if self._store.packet_for_job(job["id"]) is not None:
            return True                                # already drafted (a repeated delivery)
        try:
            draft = parse_draft(job.get("result_text") or "")
        except DraftError as e:
            self._draft_failed(job, meta, f"#{job['id']} 的草稿格式不对（{e.code}），没有生成待审批", notes, now)
            if meta.get("system_note"):
                self._chain_refresh(deal_id, now)
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
        # She took a message back after this job was made (the draft may answer what she recalled), or
        # nobody has seen to her recall yet: a person decides.
        recalled = self._recalled_since(deal, float(job.get("created_at") or 0)) if lane == LANE_REPLY else None
        recall = recall_note(recalled) if recalled is not None else ""
        # A job that failed (the Mac went silent) and came back after her lines went into a later job: out of date.
        handed_on = lane == LANE_REPLY and self._handed_on(deal, job["id"])
        route, decided_at = gates.REVIEW, None
        if draft.platform is not None:
            # A platform write: always a person's decision (8.1's gates do not apply).
            state = (SUPERSEDED if mode != COPILOT or handed_on or (newer is not None and newer["id"] > job["id"])
                     else PENDING)
        elif not draft.reply_draft and not attachments:
            state = NO_REPLY
        elif mode != COPILOT:
            state = SUPERSEDED                         # a person has the chat
        elif handed_on:
            state = SUPERSEDED                         # its lines went into a later draft job
        elif newer is not None and newer["id"] > job["id"]:
            state = SUPERSEDED                         # the creator wrote again: a newer draft is coming
        elif notes or recall or lane == LANE_SYSTEM:
            # Read late, or she recalled something meanwhile: a person decides, whatever the gate.  The
            # bot's own next step too: a 专属佣金 step speaks of a platform write, so it never goes out
            # by a gate (mac review F4).
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
            needs_attention=draft.needs_attention or bool(short) or bool(refused) or bool(stepless) or bool(recall),
            reason="；".join(r for r in (recall, refused, stepless, short, draft.reason) if r)[:500],
            status=state, route=route, decided_at=decided_at, meta=packet_meta,
            tool=tool, platform=platform, now=now,
        )
        if state in (PENDING, APPROVED):
            self._store.supersede(deal_id, now, keep=packet_id, lane=lane)
        attention = [*notes, *([draft.reason] if draft.needs_attention and draft.reason else [])]
        # (her recall: its own notice on the deal says so, the draft's reason too)
        # The draft job of her lines that had no draft before (or this job itself, back late): seen to.
        answered = self._store.drop_notices(deal_id, lambda n: n.get("into") == job["id"]
                                            or n["key"] == f"failed:{job['id']}", now)
        if any(n.get("into") == job["id"] for n in answered):
            attention.append(f"#{job['id']} 的草稿也回了 " + "、".join(f"#{n.get('job')}" for n in answered
                                                                   if n.get("into") == job["id"])
                             + " 没写成时她的消息")
        if meta.get("missed"):                         # told to the Mac now: the draft's notes say it
            self._store.drop_notices(deal_id, lambda n: n["key"] == "missed", now)
        if lane == LANE_SYSTEM and state == SUPERSEDED and mode != COPILOT:
            # A person took the chat while the step was drafted: it is owed, asked for again when that ends.
            step = meta.get("step") if isinstance(meta.get("step"), dict) else {}
            self._owe_step(deal, f"#{job['id']} 开专属佣金这一步的草稿没进待审批", str(meta["system_note"]),
                           step.get("packet"), bool(step.get("delivered")), now, item=step.get("item") or "")
        elif lane == LANE_SYSTEM and state == SUPERSEDED:
            attention.append(f"#{job['id']} 开专属佣金这一步的草稿没进待审批（又有了新的一步），请运营核对")
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
        self._store.update_deal(deal_id, now, merge_slots=draft.slots, stage_proposal=draft.stage_proposal)
        self._store.set_notice(deal_id, DRAFT_NOTES + lane, "；".join(attention), now)   # its own notes: replaced
        if lane == LANE_SYSTEM and state == NO_REPLY:
            self._chain_go(deal, now)                  # the step drafted nothing: the chain goes on
        elif lane == LANE_SYSTEM or tool in COMMISSION_ACTIONS.values():
            self._chain_refresh(deal_id, now)
        if state == APPROVED:
            self._wake.set()
        logger.info("PRM: job #%d -> packet %d (%s, %s, %s)", job["id"], packet_id, state, route, draft.message_kind)
        return True

    def _draft_failed(self, job: dict, meta: dict, head: str, notes: list, now: float) -> None:
        """A draft job that gave no draft (the Mac failed, went silent, answered nothing usable): a notice
        that stays until it is seen to (mac review F1; before, the next draft's notes wiped it and her
        message was never answered).  A reply job's notice keeps her lines (its text and the follow-ups
        the Mac had, ``_job_lines``) and what it was told (``history``, ``late`` / ``missed`` /
        ``new_friend``): her next draft job takes them along (``_submit``), and its draft, or this job's
        own coming back late, sees to the notice; an operator answering her by hand puts it away
        (``_answered_by_hand``).  A step the bot asked for (the system lane) is not asked again by
        itself: its notice goes when the next step is asked for or the deal is set back before 已签收."""
        deal_id, party_id, job_id = int(meta["deal_id"]), int(meta["party_id"]), int(job["id"])
        lane = LANE_SYSTEM if meta.get("system_note") else LANE_REPLY
        with self._handle_lock:                        # (``_submit`` takes these notices along)
            if any(int(ev["detail"].get("job") or 0) == job_id
                   for ev in self._store.events(deal_id, limit=50, kinds=("draft_failed",))):
                return                                 # lost first, then failed: said once
            self._store.add_event(deal_id, party_id, "draft_failed", {"job": job_id, "why": head[:200]},
                                  actor="mac", now=now)
            if meta.get("missed"):                     # this notice says it now
                self._store.drop_notices(deal_id, lambda n: n["key"] == "missed", now)
            if lane == LANE_SYSTEM:
                self._store.set_notice(deal_id, f"failed:{job_id}", "；".join(
                    [head.replace("草稿没写成", "开专属佣金这一步的草稿没写成", 1), *notes,
                     "系统不会自己再起草这一步，请运营处理"]), now, job=job_id, lane=lane)
                return
            # this job carried the lines of earlier ones: they are in its own lines now
            self._store.drop_notices(deal_id, lambda n: n.get("into") == job_id, now)
            head = "；".join([head, *notes])
            gap = {k: meta[k] for k in ("late", "missed", "new_friend") if isinstance(meta.get(k), dict)}
            self._store.set_notice(deal_id, f"failed:{job_id}", failed_text(head), now, job=job_id, lane=lane,
                                   head=head, lines=self._job_lines(job)[:LINES_CHARS], gap=gap,
                                   history=[h for h in meta.get("history") or [] if isinstance(h, dict)],
                                   asked_at=float(job.get("created_at") or now))

    def _job_lines(self, job: dict) -> str:
        """The creator's lines a draft job had: its text and the follow-ups the Mac was handed (those it
        never saw are a job of their own already: ``ClaudeRelay._requeue_leftovers``)."""
        rows = [str(r.get("text") or "") for r in (self._relay.store.followups(int(job["id"]), 0) or [])]
        try:
            later = int((json.loads(job.get("result_json") or "{}") or {}).get("followups_job") or 0)
        except (ValueError, TypeError, AttributeError):
            later = 0
        if later and rows:
            requeued = str((self._relay.store.get(later) or {}).get("text") or "")
            for n in range(len(rows), 0, -1):
                if "\n".join(rows[-n:]) == requeued:
                    rows = rows[:-n]
                    break
        return "\n".join(t for t in [str(job.get("text") or ""), *rows] if t.strip())

    def _cancelled(self, job_id) -> bool:
        """Was this job cancelled (an operator, in the console; one cancelled before the Mac took it is
        never handed back: ``RelayStore.cancel``)?"""
        job = self._relay.store.get(int(job_id or 0))
        return job is None or job["status"] == "cancelled"

    def _handed_on(self, deal: dict, job_id: int) -> bool:
        """Did this job give no draft before (lost, say) and her lines go into a later draft job?"""
        notice = next((n for n in deal.get("notices") or [] if n.get("key") == f"failed:{job_id}"), None)
        if notice is not None:
            return bool(notice.get("into"))
        return any(int(ev["detail"].get("job") or 0) == job_id
                   for ev in self._store.events(deal["id"], limit=50, kinds=("draft_failed",)))

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
        picked = [m for m in messages or [] if not m.get("is_group", False) and not is_friend_notice(m)
                  and system_line(m) is None]
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
        if active:
            self._wake.set()                           # a 专属佣金 step owed is asked for now (sweep_owed_steps)
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
            if after and after.get("delivered_at"):
                # 已签收: the 专属佣金 chain of this signature (once); marked 已签收 again, a chain that never
                # started is started now (mac review F2)
                again = after["stage"] == "delivered" and action in ("stage", "adopt", "shipment_status")
                if not self._examine_delivery(after, now, again=again):
                    self._chain_refresh(after["id"], now)     # (moved on past the sample, say)
            elif after and before.get("delivered_at") and not after.get("delivered_at"):
                # set back before 已签收 (to sign for it again, kb 2.1): what waited for the signature is not
                # owed now, and a step that was not written is picked up by signing for it again
                self._store.drop_notices(after["id"], lambda n: (n["key"] == "step" and n.get("delivered")) or (
                    str(n["key"]).startswith("failed:") and n.get("lane") == LANE_SYSTEM), now)
                self._chain_refresh(after["id"], now)
        return result

    def _examine_delivery(self, deal: dict, now: float, again: bool = False) -> bool:
        """A deal marked 已签收 (``delivered_at``): looked at once per signature (``deal.chain.seen``), in the
        console's ``deal_action`` or, for one marked straight into the database while the bot was stopped,
        by ``sweep_deliveries`` (mac review F2).  A signature that came after the deal's newest verified sample
        order (``_chain_order``) starts a round of that order's chain (``chain.order``, ``chain.since`` = the
        signature; what the signature before left is put away) and its first product not open is asked for
        (``_chain_next``), unless a bot from before ``deal.chain`` started it already (``_chain_started``).
        With PRM_COMMISSION_AFTER=created the order's verification started a round already (``_start_chain``);
        a signature starts a new one too (mac review F3: 运输中 → 已签收 did nothing in this mode), but only
        for a round this bot started for that order (a deal of a bot from before is only looked at).  ``again``: marked 已签收 once more, the signature known already: the
        round goes on if it can (a step that never went out, say).  True when this call started a round."""
        delivered = float(deal.get("delivered_at") or 0)
        if not delivered or deal.get("purged_at"):
            return False
        with self._chain_lock:
            deal = self._store.deal(deal["id"]) or deal
            chain = dict(deal.get("chain") or {})
            if chain.get("seen") is not None and delivered <= float(chain["seen"]):
                if again:
                    self._chain_go(deal, now, after=self._store.packet(int(chain.get("order") or 0)))
                return False
            before = "seen" not in chain          # never looked at: a deal of a bot from before deal.chain
            order = self._chain_order(deal["id"])
            chain["seen"] = delivered
            start = order is not None and _order_at(order) <= delivered and (
                self._commission_after == "delivered"
                or (chain.get("since") is not None and chain.get("order") == order["id"]))
            legacy = False
            if start:
                chain.update(order=order["id"], since=delivered)
                # set back and signed for again while the bot was stopped: the signature before's step is not owed
                self._store.drop_notices(deal["id"], lambda n: float(n.get("at") or 0) < delivered and (
                    (n["key"] == "step" and n.get("delivered")) or (
                        str(n["key"]).startswith("failed:") and n.get("lane") == LANE_SYSTEM)), now)
                legacy = before and self._chain_started(self._store.deal(deal["id"]) or deal, delivered)
            self._store.update_deal(deal["id"], now, chain=chain)
            deal = self._store.deal(deal["id"]) or deal
            if start and not legacy:
                self._chain_go(deal, now, after=order)
            elif start:
                self._chain_refresh(deal["id"], now)
            return start

    def _chain_started(self, deal: dict, since: float) -> bool:
        """Did a bot from before ``deal.chain`` start the chain of this signature (``since``)?  A step owed
        (``step``), its job queued or running, asked for (``next_step``), drafted (a draft of the system lane,
        a 专属佣金 draft) or not written (the system lane's ``failed:``) since then: its round is not asked
        for again (it goes on from what comes next)."""
        for n in deal.get("notices") or []:
            if n["key"] == "step" or (str(n["key"]).startswith("failed:") and n.get("lane") == LANE_SYSTEM
                                      and float(n.get("at") or 0) > since):
                return True
        if self._store.events(deal["id"], limit=1, kinds=("next_step",), since=since):
            return True                                # (asked in the same step as the signature: at it)
        party = self._store.party(deal["party_id"]) or {}
        if party.get("wxid") and self._relay.store.latest_open(party["wxid"], drafts_only=True, system=True):
            return True
        # (a draft comes back after its step was asked: one of the signature's own time is of the one before)
        return any(float(p["created_at"]) > since and (
            p.get("tool") in COMMISSION_ACTIONS.values() or (p.get("meta") or {}).get("lane") == LANE_SYSTEM)
            for p in self._store.deal_packets(deal["id"], since=since))

    def _start_chain(self, order: dict, now: float) -> None:
        """PRM_COMMISSION_AFTER=created: a sample order verified starts a round of its chain now."""
        with self._chain_lock:
            deal = self._store.deal(order["deal_id"])
            if deal is None:
                return
            self._store.update_deal(deal["id"], now, chain={**(deal.get("chain") or {}), "order": order["id"],
                                                            "since": now})
            self._chain_next(self._store.deal(deal["id"]) or deal, now, after=order)

    def _chain(self, deal: dict) -> Optional[dict]:
        """Where the deal's 专属佣金 chain stands, as the bot keeps it (mac review F5: before, only the Mac's case
        file knew, and a product that could not be opened stopped the ones after it).  The round is
        ``deal.chain``: the verified sample order it follows and since when (a signature; with
        PRM_COMMISSION_AFTER=created the order's verification).  ``items``: each product of the order, in its
        order, with its state (``item_state``) and the draft that says so; ``busy``: a step of it is under
        way (a product running or in 待审批, a step owed, its job queued or running, a step's draft waiting);
        ``next``: the product to ask for now (not busy: the first one not asked for or decided on in this
        round).  None: no round, or its order is not a 专属佣金 one (no ``COMMISSION_ACTIONS`` shop)."""
        chain = deal.get("chain") or {}
        if chain.get("since") is None or not chain.get("order"):
            return None
        order = self._store.packet(int(chain["order"]))
        if order is None or order["deal_id"] != deal["id"] or not _verified(order):
            return None
        action = (order["platform"] or {}).get("action") or {}
        shop = str(action.get("shop") or "")
        name = COMMISSION_ACTIONS.get(shop)
        ids = list(dict.fromkeys(order_items(action)))
        if not name or not ids:
            return None
        since = float(chain["since"])
        packets = self._store.deal_packets(deal["id"], tools=(name,))
        asks = self._store.events(deal["id"], limit=None, kinds=("next_step",), since=since)
        asked = {str(ev["detail"].get("item")) for ev in asks if ev["detail"].get("item")}
        items = []
        for item in ids:
            mine = [p for p in packets if action_target((p["platform"] or {}).get("action") or {}, p["platform"])[0] == item]
            state, packet = item_state(mine, item in asked, since)
            items.append({"id": item, "state": state, "packet": packet})
        party = self._store.party(deal["party_id"]) or {}
        job = self._relay.store.latest_open(party["wxid"], drafts_only=True, system=True) if party.get("wxid") else None
        drafting = None
        if job is not None:
            drafting = {"job": job["id"], "item": next((str(ev["detail"].get("item") or "") for ev in asks
                                                        if ev["detail"].get("job") == job["id"]), "")}
            for i in items:
                if i["id"] == drafting["item"] and i["state"] == "asked":
                    i["state"] = "drafting"            # (its step's draft is being written)
        busy = ""
        if any(i["state"] in ("running", "waiting") for i in items):
            busy = "held"
        elif any(n["key"] == "step" for n in deal.get("notices") or []):
            busy = "owed"
        elif job is not None:
            busy = "drafting"
        elif any((p.get("meta") or {}).get("lane") == LANE_SYSTEM
                 for p in self._store.deal_packets(deal["id"], statuses=(PENDING,))):
            busy = "draft"
        created = ((order["platform"] or {}).get("execution") or {}).get("created") or {}
        return {"order": order, "shop": shop, "action": name, "items": items, "since": since, "busy": busy,
                "drafting": drafting, "number": str(created.get("sample_order_no") or ""),
                "uid": str(created.get("creator_uid") or "") if shop == "dy" else "",
                "next": None if busy else next((i["id"] for i in items if i["state"] == "todo"), None)}

    def _chain_next(self, deal: dict, now: float, after: Optional[dict] = None) -> bool:
        """The 专属佣金 chain goes on (mac review F5), under ``_chain_lock``: unless a step of it is under way
        (``_chain``'s ``busy``), the round's next product is asked for (``_ask_next_step``: owed while a person
        has the deal or the creator is off the list), whatever became of the one before: opened, dropped
        (「不发」), red, nothing drafted; each product once per round.  A round with nothing left asks once
        more only right after a 专属佣金 of it was verified (``after``), so the case file hears it is done
        (kb 2.1: no action then).  With PRM_COMMISSION_AFTER=delivered a deal set back before 已签收 asks for
        nothing (the next signature starts a new round).  The deal's ``chain`` notice says where it stands.
        False: the deal has no chain (the caller's own way then)."""
        with self._chain_lock:
            deal = self._store.deal(deal["id"]) or deal
            progress = self._chain(deal)
            if progress is None:
                return False
            delivered = self._commission_after == "delivered"
            done = False
            if after is not None:
                after = self._store.packet(after["id"]) or after
                theirs = (after.get("platform") or {}).get("action") or {}
                done = (theirs.get("action") == progress["action"] and _verified(after)
                        and str(theirs.get("item_id") or "") in {i["id"] for i in progress["items"]})
            if not progress["busy"] and (progress["next"] or done) and not (delivered and not deal.get("delivered_at")):
                head = after if after is not None else progress["order"]
                # (a round of a signature says so: with PRM_COMMISSION_AFTER=created one may start at the order)
                signed = bool(deal.get("delivered_at")) and progress["since"] == float(deal["delivered_at"])
                self._ask_next_step(head, chain_note(progress, head, delivered=signed), now, delivered=delivered,
                                    item=progress["next"] or "")
                deal = self._store.deal(deal["id"]) or deal
                progress = self._chain(deal) or progress
            self._chain_notice(deal, progress, now)
            return True

    def _chain_go(self, deal: dict, now: float, after: Optional[dict] = None) -> None:
        """``_chain_next``, its failure logged (what called it is done all the same: the console's action, a
        draft that came back)."""
        try:
            self._chain_next(deal, now, after=after)
        except Exception:
            logger.exception("PRM: the 专属佣金 chain of deal %d could not go on", deal["id"])

    def _chain_refresh(self, deal_id: int, now: float) -> None:
        """The deal's ``chain`` notice, again (a draft of the chain came, a red one was dealt with …)."""
        try:
            with self._chain_lock:
                deal = self._store.deal(deal_id)
                if deal is not None:
                    self._chain_notice(deal, self._chain(deal), now)
        except Exception:
            logger.exception("PRM: the 专属佣金 notice of deal %d could not be written", deal_id)

    def _chain_notice(self, deal: dict, progress: Optional[dict], now: float) -> None:
        """The deal's ``chain`` notice (``chain_notice_text``): there while the round has something under way, a
        product left to ask for or asked for with nothing drafted, or a red one nobody marked dealt with; gone
        when every product is open or the rest was decided by a person (「不发」, 「我已处理」), when
        (PRM_COMMISSION_AFTER=delivered) the deal was set back before 已签收, and, for what is left or drafted
        nothing, once the deal moved on past the sample (``MOVED_ON``: the operators are done with it)."""
        text = ""
        if progress is not None and not (self._commission_after == "delivered" and not deal.get("delivered_at")):
            hint = RESTART_HINT if self._commission_after == "delivered" else RESTART_HINT_CREATED
            text = chain_notice_text(progress, hint, moved_on=deal.get("stage") in MOVED_ON)
        self._store.set_notice(deal["id"], "chain", text, now)

    def _chain_decided(self, packet: dict, now: float) -> None:
        """An operator decided on a draft of the chain (a 专属佣金 draft, or a step's draft without the action):
        the chain goes on (「不发」: that product is not opened in this round)."""
        if packet.get("tool") in COMMISSION_ACTIONS.values() or (packet.get("meta") or {}).get("lane") == LANE_SYSTEM:
            deal = self._store.deal(packet["deal_id"])
            if deal is not None:
                self._chain_go(deal, now, after=packet)

    def _after_platform(self, packet_id: int, record: dict, now: float, late: bool = False) -> None:
        """What a platform action's result means for the 专属佣金 chain: a sample order verified with
        PRM_COMMISSION_AFTER=created starts a round of its chain; a 专属佣金's result (open, red, late) lets the
        chain go on; for a deal without a chain (no verified sample order) a verified 专属佣金 asks for the next
        as before (the Mac picks it from the case file).  Failures are logged: the result is recorded already."""
        packet = self._store.packet(packet_id)
        if packet is None:
            return
        action = (packet["platform"] or {}).get("action") or {}
        name = action.get("action")
        try:
            if name == "jst.sample_order":
                if not late and self._commission_after == "created" and written_state(record) == "verified":
                    self._start_chain(packet, now)
            elif name in COMMISSION_ACTIONS.values():
                deal = self._store.deal(packet["deal_id"])
                if (deal is not None and not self._chain_next(deal, now, after=None if late else packet)
                        and not late and packet["status"] == APPROVED):
                    self._ask_next_step(packet, next_step_note(action, record), now)
        except Exception:
            logger.exception("PRM: the step after packet %d could not be asked for", packet_id)

    def sweep_deliveries(self) -> int:
        """The deals marked 已签收 the bot has not looked at (``deal.chain.seen``): marked straight into the
        database while it was stopped (the console then has no service: ``apply_deal_action``; mac review
        F2), or by a bot from before ``deal.chain``.  Looked at as if marked now (``_examine_delivery``):
        at start and every round of the sender thread.  Returns how many chains it started."""
        started = 0
        now = self._clock()
        for deal in self._store.delivered_deals():
            seen = (deal.get("chain") or {}).get("seen")
            if seen is not None and float(deal["delivered_at"]) <= float(seen):
                continue
            try:
                started += int(self._examine_delivery(deal, now))
            except Exception:
                logger.exception("PRM: deal %d's signature could not be looked at", deal["id"])
        return started

    def _chain_order(self, deal_id: int) -> Optional[dict]:
        """The deal's newest sample order verified on the platform, the one its 专属佣金 follow: found by the
        orders' own execution records, every one of them however much was said since (mac review F9: only
        the newest 200 events were looked at, and a takeover's chat pushed the order's out).  An order whose
        verification came late (given up on, then the Mac's answer) counts too: it is in 聚水潭."""
        for packet in self._store.deal_packets(deal_id, tools=("jst.sample_order",),
                                               statuses=(APPROVED, SENDING, SENT, FAILED)):     # newest first
            if ((packet["platform"] or {}).get("execution") or {}).get("outcome") == platforms.VERIFIED:
                return packet
        return None

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
            self._person_decided(packet, now)
            self._chain_decided(packet, now)          # 「不发」 a product: the next one is asked for (mac review F5)
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
        self._person_decided(packet, now)
        self._chain_decided(packet, now)
        self._wake.set()
        return {"ok": True, "status": APPROVED}

    def _person_decided(self, packet: dict, now: float) -> None:
        """An operator decided on a draft answering her: her recall (the draft said so) is seen to."""
        if (packet.get("meta") or {}).get("lane") != LANE_SYSTEM and packet.get("job_id") is not None:
            self._store.drop_notices(packet["deal_id"], lambda n: n["key"] == "recall", now)

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
        opened, unsure = self._opened_before(packet)
        if opened:
            logger.warning("PRM: packet %d not approved: its %s ran before for this product and creator%s",
                           packet["id"], action.get("action"), " (a result not known: to be checked)" if unsure else "")
            if unsure:
                self._ask_recheck(packet, unsure, now)
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
            opened, unsure = self._opened_before(packet)
            moved = not opened and self._store.move(
                packet["id"], (PENDING,), EXECUTING, decided_at=now, final_text=final,
                edited=int(final != packet["text"]), platform=approved)
        if opened:
            if unsure:
                self._ask_recheck(packet, unsure, now)
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
        self._person_decided(packet, now)
        if action["action"] in COMMISSION_ACTIONS.values():
            self._chain_refresh(packet["deal_id"], now)        # (that product is running now)
        logger.info("PRM: packet %d approved; %s goes to the Mac as job #%d", packet["id"], action["action"], job_id)
        return {"ok": True, "status": EXECUTING, "job": job_id}

    def _opened_before(self, packet: dict) -> tuple:
        """``(why, unsure)``: why this draft's 专属 action (``ONCE_ACTIONS``) must not run ("" = it may), and the
        drafts whose result an operator has to check on the platform first.  Another draft of the deal has the
        same action for the same product and creator (``platform_state``): running on the Mac now, verified on
        the platform (its reply sent, about to go, or held back), or ended with a result that may have opened it
        (mac review F6: ACCEPTED_UNVERIFIED, INDETERMINATE, UNKNOWN, PARTIAL …, a step of it that wrote): that
        one stops it until an operator, asked for this draft (``_ask_recheck``), found on the platform that it
        is not there and said so with 「我已处理」 on that red draft (``_released``).  One that wrote nothing
        (FAILED, STATE_CONFLICT, NOT_RUN, no step that wrote) does not count.  The ledger on the Mac only stops
        one approval from running twice; this stops a second draft (a chain picked up again while a step runs,
        a reply that carries a commission against kb/)."""
        data = packet["platform"]
        action = data.get("action") or {}
        name = action.get("action")
        if name not in ONCE_ACTIONS:
            return "", []
        item, creators = action_target(action, data)
        what = f"商品 {item} 给达人 {action.get('creator_platform_id')} 的「{platforms.label(name)}」"
        unsure = []
        for other in self._store.deal_packets(packet["deal_id"], tools=(name,),
                                              statuses=(EXECUTING, APPROVED, SENDING, SENT, FAILED)):
            theirs = other["platform"] or {}
            their_item, their_creators = action_target(theirs.get("action") or {}, theirs)
            if other["id"] == packet["id"] or their_item != item or not (creators & their_creators):
                continue
            state = platform_state(other)
            if state == "running":
                at = float(theirs.get("approved_at") or other.get("decided_at") or other["created_at"])
                return f"{what}正在执行（#{other['id']}，{_date_text(at)} 批准），不能再开一次。{REOPEN_HINT}", []
            if state == "verified":
                at = float(other.get("sent_at") or other.get("decided_at") or other["created_at"])
                return f"{what}已经在 #{other['id']}（{_date_text(at)}）开过了，平台上核验成功，不能再开一次。{REOPEN_HINT}", []
            if state == "uncertain" and not _released(other, packet["id"]):
                unsure.append(other)
        if not unsure:
            return "", []
        newest = unsure[0]
        outcome = ((newest["platform"] or {}).get("execution") or {}).get("outcome") or "UNKNOWN"
        at = float((newest["platform"] or {}).get("approved_at") or newest.get("decided_at") or newest["created_at"])
        ids = "、".join(f"#{u['id']}" for u in unsure)
        return (f"{what}上次在 {ids}（{_date_text(at)} 批准）的结果不确定（{outcome}：{platforms.outcome_text(outcome)}），"
                "平台上可能已经开了，不能直接再开。请先到平台后台核对：平台上确实没有，就在「待审批」最上面 "
                f"{ids} 的红色提醒上点「我已处理」，再回来批准这一份；平台上已经有了，点这一份的「不发」"), unsure

    def _ask_recheck(self, packet: dict, unsure: list, now: float) -> None:
        """This draft was refused for the uncertain result of ``unsure`` (``_opened_before``): each of them is
        red on 待审批 again (its 「我已处理」 shows once more, whether or not it was clicked before), its error
        and the deal's notice say what the operator is to check and what 「我已处理」 means now, and it keeps
        which draft asked (``recheck``: ``for``, ``at``, the error it had).  「我已处理」 after this lets this
        draft, and only this one, run (``_released``)."""
        asking = packet["id"]
        ask = (f"#{asking} 要再开同一个商品：先到平台后台核对。平台上确实没有，点这里的「我已处理」，再去批准 #{asking}；"
               f"平台上已经有了，先点 #{asking} 的「不发」，再点这里的「我已处理」")
        for other in unsure:
            before = ((other["platform"] or {}).get("recheck") or {})
            error = str((before.get("error") if "error" in before else other.get("error")) or "")
            done = self._store.edit_platform(
                other["id"], lambda data: {**data, "acked_at": None, "recheck": {"for": asking, "at": now, "error": error}},
                (FAILED,), error=lambda _: f"{ask}。上次的结果：{error}"[:500])
            if done is None:
                continue
            self._store.set_notice(other["deal_id"], f"platform:{other['id']}", f"#{other['id']} 的结果不确定，{ask}", now)
            self._store.add_event(other["deal_id"], other["party_id"], "platform_recheck",
                                  {"packet": other["id"], "for": asking}, actor="console", now=now)
            logger.info("PRM: packet %d's uncertain result is to be checked before packet %d may run", other["id"], asking)

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
        """An operator has dealt with a platform action that ended red.  Asked to check it for a draft of the
        same 专属 action (``recheck``): the operator found it is not on the platform, and that draft may run."""
        if packet.get("tool", MESSAGE_TOOL) == MESSAGE_TOOL or packet["status"] != FAILED:
            return {"ok": False, "error": "只有没完成的平台动作能标为已处理"}
        data = self._store.edit_platform(packet["id"], lambda d: None if d.get("acked_at") else {**d, "acked_at": now},
                                         (FAILED,))
        if data is None:
            return {"ok": False, "error": "已经标过了"}
        released = (data.get("recheck") or {}).get("for")
        self._store.add_event(packet["deal_id"], packet["party_id"], "platform_ack",
                              {"packet": packet["id"], **({"recheck_for": released} if released else {})},
                              actor="console", now=now)
        self._store.drop_notices(packet["deal_id"], lambda n: n["key"] == f"platform:{packet['id']}", now)
        if packet.get("tool") in COMMISSION_ACTIONS.values():
            self._chain_refresh(packet["deal_id"], now)
        if released:
            logger.info("PRM: packet %d checked on the platform (not there): packet %s may run", packet["id"], released)
        return {"ok": True, **({"released": released} if released else {})}

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
            if packet.get("tool") in COMMISSION_ACTIONS.values():
                self._chain_refresh(packet["deal_id"], now)    # (its preview may pass now)
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
                self._store.set_notice(packet["deal_id"], f"platform:{packet['id']}", (
                    f"#{packet['id']} 平台动作的结果晚到了：{platforms.outcome_text(outcome)}；回复没有自动发，请运营处理"), now)
                self._after_platform(packet["id"], record, now, late=True)
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
                    self._after_platform(packet["id"], record, now)
                return
        why = "；".join(record.get("problems") or [])
        if outcome == platforms.VERIFIED and missing:
            error = (f"平台上已经核验成功，但回复里的 {'、'.join(missing)} 平台没有给出，回复没有自动发出；"
                     "请核对后自己回达人")
        elif outcome == platforms.VERIFIED:        # verified, but the job was cancelled on the way
            error = "平台上已经核验成功，但执行任务中途被取消了，回复没有自动发出；请核对后自己回达人"
        elif outcome == "NOT_RUN" and written_state(record, packet.get("outcome", "")) == "not_done":
            # nothing ran (the Mac never took it, it was cancelled before it ran or while it waited for the
            # platform command lock): nothing to check on the platform
            error = (f"平台动作没有执行（{outcome}：{platforms.outcome_text(outcome)}），平台没有动，没有发微信"
                     + (f"：{why}" if why else "") + "。可以重新批准：同样的动作再起草一份时照常批准就行，系统不会自己重试")[:500]
        else:
            maybe = (action.get("action") in ONCE_ACTIONS
                     and written_state(record, packet.get("outcome", "")) == "uncertain")
            error = (f"平台动作没有核验成功（{outcome}：{platforms.outcome_text(outcome)}），没有发微信"
                     + (f"：{why}" if why else "") + "。请到平台后台核对后自己处理，系统不会重试"
                     + ("；平台上可能已经开了：同一个商品给这位达人再批准时，系统会先请你核对" if maybe else ""))[:500]
        if self._store.move(packet["id"], (EXECUTING,), FAILED, outcome=f"platform_{outcome.lower()}", sent_at=now,
                            error=error, platform=executed):
            self._store.add_event(packet["deal_id"], packet["party_id"], "platform_result", event, actor="mac", now=now)
            # until an operator marks it dealt with (``_ack``), whatever the drafts after it say
            self._store.set_notice(packet["deal_id"], f"platform:{packet['id']}", f"#{packet['id']} {error}", now)
            logger.warning("PRM: packet %d %s ended %s; nothing sent", packet["id"], action.get("action"), outcome)
            self._after_platform(packet["id"], record, now)       # one product red: the chain goes on

    def _ask_next_step(self, packet: dict, note: str, now: float, delivered: bool = False, item: str = "") -> None:
        """A platform write that leads to the next one (a sample order → its 专属佣金): a draft
        job of its own, told what happened in ``meta.system_note``.  What it drafts is a platform
        action like any other: previewed on the Mac, approved by a person; and whatever it drafts
        waits for a person, whatever its kind's gate (``on_result``: the system lane).  Not now for a
        creator the operators took off the list (inactive) or a deal a person has: the step is owed
        (a notice that stays, mac review F1) and asked for when that ends (``sweep_owed_steps``).
        ``delivered``: the step waits for the signature (no longer owed once the deal is set back); ``item``: the
        product it asks for (``_chain``; "" when the Mac picks it, or for the step that only tells it is done)."""
        if not note:
            return
        deal = self._store.deal(packet["deal_id"])
        party = self._store.party(packet["party_id"])
        if deal is None or party is None:
            return
        if self._step_blocked(deal, party, now):
            self._owe_step(deal, f"#{packet['id']} 之后该开专属佣金了", note, packet["id"], delivered, now, item=item)
            logger.info("PRM: party %d: the next step after packet %d is owed (inactive, or a person has the deal)",
                        party["id"], packet["id"])
            return
        self._store.drop_notices(deal["id"], lambda n: n["key"] == "step", now)     # this one goes now
        self._dispatch_step(deal, party, note, packet["id"], delivered, now, item=item)

    def _step_blocked(self, deal: dict, party: dict, now: float) -> str:
        """Why a 专属佣金 step cannot be asked for now ("" = it can)."""
        if not party.get("active"):
            return "这位达人已经停用（不在达人名单里），系统先不起草；重新启用后会自动起草"
        if self._mode(deal, now) != COPILOT:
            return "现在不在副驾驶模式，系统先不起草；切回副驾驶（或者人工接管到期）后会自动起草"
        return ""

    def _owe_step(self, deal: dict, head: str, note: str, packet_id, delivered: bool, now: float,
                  item: str = "") -> None:
        """The deal's ``step`` notice: «``head``：why it waits», and what ``sweep_owed_steps`` needs to ask
        for it (the ``system_note``, the packet it follows, whether it waits for the signature, the product)."""
        party = self._store.party(deal["party_id"]) or {}
        why = self._step_blocked(deal, party, now) or "系统一会儿就起草"
        self._store.set_notice(deal["id"], "step", f"{head}：{why}", now, head=head, note=note, packet=packet_id,
                               delivered=bool(delivered), item=str(item or ""))

    def _dispatch_step(self, deal: dict, party: dict, note: str, packet_id, delivered: bool, now: float,
                       item: str = "") -> int:
        """The draft job of a 专属佣金 step (``meta.system_note``; ``meta.step`` says which packet it
        follows, whether it waited for the signature and the product it asks for, should it be owed again);
        returns its id."""
        meta = {"party_id": party["id"], "deal_id": deal["id"], "name": party["name"], "system_note": note,
                "step": {"packet": packet_id, "delivered": bool(delivered), "item": str(item or "")}}
        outbound = self._outbound(deal["id"], party["id"], now)
        if outbound:
            meta["sent"] = outbound
        # input_closed: the creator's next message is a reply job of its own, not part of this step
        _, job_id = self._relay.submit(party["wxid"], party["wxid"], "", kind=PRM, meta=meta, fresh=True,
                                       input_closed=True)
        # what was asked for, when (``_chain``: each product once per round)
        self._store.add_event(deal["id"], party["id"], "next_step", {"job": job_id, "after": packet_id,
                                                                     "delivered": bool(delivered),
                                                                     "item": str(item or "")},
                              actor="system", now=now)
        # the chain goes on: a step that was not written before is seen to
        self._store.drop_notices(deal["id"], lambda n: str(n["key"]).startswith("failed:")
                                 and n.get("lane") == LANE_SYSTEM, now)
        logger.info("PRM: packet %s done; job #%d drafts the next step", packet_id, job_id)
        return job_id

    def sweep_owed_steps(self) -> int:
        """The 专属佣金 steps owed (the step came up while a person had the deal, or the creator was off
        the list): asked for once the deal is back in copilot mode (the takeover ran out, an operator
        switched it back) and the creator is on the list again; one that waited for the signature is
        dropped when the deal was set back before 已签收.  Returns how many were asked for."""
        asked = 0
        now = self._clock()
        for deal in self._store.deals_with_notice("step"):
            owed = next(n for n in deal["notices"] if n["key"] == "step")
            party = self._store.party(deal["party_id"])
            if party is None or (owed.get("delivered") and not deal.get("delivered_at")):
                self._store.drop_notices(deal["id"], lambda n: n["key"] == "step", now)
                continue
            why = self._step_blocked(deal, party, now)
            if why:
                if owed.get("head") and owed["text"] != f"{owed['head']}：{why}":
                    self._store.set_notice(deal["id"], "step", f"{owed['head']}：{why}", now)
                continue
            with self._chain_lock:                     # (the chain does not ask another meanwhile)
                if self._store.drop_notices(deal["id"], lambda n: n["key"] == "step", now):
                    self._dispatch_step(deal, party, str(owed.get("note") or ""), owed.get("packet"),
                                        bool(owed.get("delivered")), now, item=str(owed.get("item") or ""))
                    asked += 1
            self._chain_refresh(deal["id"], now)
        return asked

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
            "clock_skew_sec": int(round(self._skew)),
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
        self.sweep_owed_steps()
        self.sweep_deliveries()
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
                result = self._send(packet["chat_id"], text)
                if not result:
                    self._unnote_sent(noted)
                    problem = _refused(result) or "文字没发出去"
            except Exception:
                self._unnote_sent(noted)
                logger.exception("PRM: packet %d text not sent", packet["id"])
                problem = "文字发送出错"
        for item in packet["attachments"] if not problem else ():
            path = str(item.get("path") or "")
            if not Path(path).is_file():
                problem = f"文件「{item.get('name')}」在电脑上找不到了"
                break
            noted = self._note_sent(packet["chat_id"], file=path, packet_id=packet["id"])
            try:
                result = self._send_file(packet["chat_id"], path)
                if not result:
                    self._unnote_sent(noted)
                    why = _refused(result)
                    problem = f"文件「{item.get('name')}」没发出去" + (f"：{why}" if why else "")
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
        for packet in self._store.sent_since(chat_id, self._local(since)):
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
        counts["notices"] = self._store.rewrite_notices(deal["id"], rewrite)
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
# The end of ``_opened_before``'s refusal: the draft is not needed (a sample order's chain goes on by itself).
REOPEN_HINT = "点「不发」就行（寄样单的专属佣金：系统会接着开还没开的商品）"
# How a round's products not opened are asked for again (kb/合作政策.md 2.1): a new round, from a new signature.
RESTART_HINT = "要重开这一轮没开的商品：把物流改回「运输中」再标「已签收」"
# The same with PRM_COMMISSION_AFTER=created (the round starts when the order is made: a signature starts a new one).
RESTART_HINT_CREATED = "要重开这一轮没开的商品：标「已签收」时会重开（已经签收的，把物流改回「运输中」再标「已签收」）"
# A 专属佣金 chain's products as the bot sees them (``item_state``), in the order the Mac and the deal hear them.
CHAIN_STATES = (("opened", "已开好"), ("running", "正在执行"), ("waiting", "在待审批"), ("drafting", "正在起草"),
                ("dropped", "这一轮不开（运营点了「不发」）"), ("not_done", "没开成"),
                ("uncertain", "结果不确定（运营会先到平台后台核对）"), ("asked", "派过、草稿没开它"),
                ("todo", "还没开"))
CHAIN_LIST = 3              # product ids the deal's notice names per state (the rest are counted)
# Stages after the sample (the content): what a chain left or drafted nothing for no longer needs saying.
MOVED_ON = ("brief", "draft", "revise", "published", "closed")


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


def order_items(action: dict) -> list:
    """The products of a sample order (``jst.sample_order``), in its order: its ``items`` (an older action has
    only ``item_id``)."""
    ids = [str(i.get("item_id")) for i in action.get("items") or [] if isinstance(i, dict) and i.get("item_id")]
    if not ids and action.get("item_id"):
        ids = [str(action["item_id"])]
    return ids


# The results of an execution that wrote nothing on the platform, when no step of it wrote either.
NOT_WRITTEN = ("FAILED", "STATE_CONFLICT", "NOT_RUN")


def written_state(execution: Optional[dict], packet_outcome: str = "") -> str:
    """What an execution (its record) left on the platform: "verified" (VERIFIED_SUCCESS); "not_done"
    (FAILED, STATE_CONFLICT or NOT_RUN, and no step of it that ran with --execute wrote: the platform
    refused, nothing was sent, nothing ran); else "uncertain": it may be there (ACCEPTED_UNVERIFIED,
    INDETERMINATE, UNKNOWN, PARTIAL, OBSERVED_DESIRED_STATE …, or a step that wrote before a later one
    failed: a 渠道品 made, its binding refused).  No record at all is NOT_RUN only for a job that never
    reached the Mac (``platform_not_run``)."""
    execution = execution or {}
    outcome = execution.get("outcome") or ("NOT_RUN" if packet_outcome == "platform_not_run" else "UNKNOWN")
    if outcome == platforms.VERIFIED:
        return "verified"
    wrote = any(isinstance(step, dict) and step.get("execute") and step.get("outcome") not in NOT_WRITTEN
                for step in execution.get("steps") or [])
    return "not_done" if outcome in NOT_WRITTEN and not wrote else "uncertain"


def platform_state(packet: dict) -> str:
    """Where a platform draft's action stands: "running" (on the Mac now), "verified" (its reply approved,
    being sent or sent, or held back: a placeholder the platform gave no value for, the reply not sent, a
    late answer), "uncertain" / "not_done" (it ended red: ``written_state``), or "" (no decision: waiting,
    not sent, out of date)."""
    status = packet["status"]
    if status == EXECUTING:
        return "running"
    if status in (APPROVED, SENDING, SENT):
        return "verified"
    if status != FAILED:
        return ""
    return written_state((packet.get("platform") or {}).get("execution"), packet.get("outcome", ""))


def _verified(packet: dict) -> bool:
    """Was this platform draft's action verified on the platform (``platform_state``)?"""
    return platform_state(packet) == "verified"


def _released(other: dict, packet_id: int) -> bool:
    """Did an operator, asked to check ``other``'s uncertain result for draft ``packet_id`` (``_ask_recheck``),
    mark it 「我已处理」 after that: it is not on the platform, that draft may run?"""
    data = other.get("platform") or {}
    recheck = data.get("recheck") or {}
    return (recheck.get("for") == packet_id and data.get("acked_at") is not None
            and float(data["acked_at"]) >= float(recheck.get("at") or 0))


def item_state(packets: list, asked: bool, since: float) -> tuple:
    """``(state, packet)`` of one product of a 专属佣金 chain (``CHAIN_STATES``) from its drafts of the commission
    action, newest first: "opened" (one verified), "running", "waiting" (in 待审批); else what this round
    decided on (a draft of it since the round started, ``since``: "dropped" by an operator, "not_done" /
    "uncertain" red, ``written_state``); else "asked" (a step asked for it in this round and nothing of it
    came: no action drafted, the draft not written; "drafting" while its step's job runs: ``PrmService._chain``);
    else "todo", with the uncertain draft of an earlier round if there is one (to be checked before a new one
    runs: ``_opened_before``)."""
    states = [(platform_state(p), p) for p in packets]
    for want, state in (("verified", "opened"), ("running", "running")):
        hit = next((p for s, p in states if s == want), None)
        if hit is not None:
            return state, hit
    hit = next((p for p in packets if p["status"] == PENDING), None)
    if hit is not None:
        return "waiting", hit
    for s, p in states:
        if float(p["created_at"]) <= since:
            continue                                   # (a draft of the signature's own time is of the one before)
        if p["status"] == DROPPED:
            return "dropped", p
        if s in ("not_done", "uncertain"):
            return s, p
    if asked:
        return "asked", None
    return "todo", next((p for s, p in states if s == "uncertain"), None)


def _ids(rows: list, limit: int, packet: bool = False) -> str:
    """Products for a person: the first ``limit`` ids (with their draft, ``packet``), then how many more."""
    shown = [f"{r['id']}（#{r['packet']['id']}）" if packet and r.get("packet") else r["id"] for r in rows[:limit]]
    return "、".join(shown) + (f" 等 {len(rows)} 个" if len(rows) > limit else "")


def chain_note(progress: dict, after: dict, *, delivered: bool) -> str:
    """What a 专属佣金 step's draft is told (``meta.system_note``) by a bot that keeps the chain's record
    (``PrmService._chain``): what happened just now (``after``: the sample order, or the 专属佣金 draft opened,
    dropped, red), the one product to fill now (``progress["next"]``; none: the round is done, only the case
    file is updated), and where every product of the order stands, last (the bot's record: a 专场's long list
    never pushes the instruction out)."""
    order = progress["order"]
    shop, name, items = progress["shop"], progress["action"], progress["items"]
    what = f"{platforms.PLATFORMS[shop]}的商品，共 {len(items)} 个，清单在最后"
    theirs = (after.get("platform") or {}).get("action") or {}
    head = ""
    if after["id"] == order["id"]:
        head = (f"达人已经签收了寄样单 {progress['number']}（{what}）。" if delivered else
                f"寄样单 {progress['number']} 已经在聚水潭建好，寄的是{what}。")
    elif theirs.get("action") == name:
        item, state = str(theirs.get("item_id") or ""), platform_state(after)
        outcome = ((after.get("platform") or {}).get("execution") or {}).get("outcome") or ""
        if state == "verified":
            head = (f"商品 {item} 的专属佣金（{theirs.get('commission_rate')}%）已经在平台上开好了"
                    f"（达人 {theirs.get('creator_platform_id')}）。")
        elif after["status"] == DROPPED:
            head = f"商品 {item} 这一轮不开专属佣金了（运营点了「不发」）。"
        elif state == "uncertain":
            head = f"商品 {item} 的专属佣金结果不确定（#{after['id']} {outcome}），运营会先到平台后台核对，这一轮先跳过它。"
        elif state == "not_done":
            head = (f"商品 {item} 的专属佣金没开成（#{after['id']} {outcome}：{platforms.outcome_text(outcome)}），"
                    "这一轮先跳过它。")
    if not head:
        head = f"接着开寄样单 {progress['number']} 的专属佣金（{what}）。"
    who = f"聚水潭建单时查到达人的抖音 UID 是 {progress['uid']}，creator_platform_id 就填它。" if progress["uid"] else ""
    item = progress["next"]
    if item:
        rest = sum(1 for i in items if i["state"] == "todo" and i["id"] != item)
        earlier = next((i["packet"] for i in items if i["id"] == item), None)
        todo = (f"按 kb/合作政策.md 的「开专属佣金」：这一份草稿只填商品 {item} 的 {name}（一次一个商品，开过的不要再开），"
                + (f"这一轮这之后还要开 {rest} 个。" if rest else "这是这一轮最后一个要开的商品。")
                + (f"它上次在 #{earlier['id']} 的结果不确定，批准前运营会先到平台后台核对。" if earlier else ""))
    else:
        todo = "这一轮没有要开的商品了：这一份不要填平台动作，按 kb/合作政策.md 的「开专属佣金」把进度记进案卷。"
    told = []
    for state, label in CHAIN_STATES:
        rows = [i for i in items if i["state"] == state]
        if rows:
            told.append(f"{label}：" + "、".join(
                f"{r['id']}（#{r['packet']['id']}）" if state in ("not_done", "uncertain") and r.get("packet") else r["id"]
                for r in rows))
    return (head + who + todo + "机器人的记录（以它为准，记进案卷；寄样单上的商品按顺序）：" + "；".join(told) + "。")


def chain_notice_text(progress: dict, hint: str, moved_on: bool = False) -> str:
    """The deal's ``chain`` notice: how many of the order's products are open and what holds up or is left, the
    first few ids of each (``CHAIN_LIST``), and ``hint`` (how to ask again for those not opened) once nothing is
    under way.  "" when every product is open, or nothing is under way, left to ask for, asked for with nothing
    drafted or red and not marked dealt with; ``moved_on`` (the deal is past the sample): only under way and red."""
    items = progress["items"]
    by = {state: [i for i in items if i["state"] == state] for state, _ in CHAIN_STATES}
    if len(by["opened"]) == len(items):
        return ""
    red = [i for i in by["not_done"] + by["uncertain"] if not ((i["packet"] or {}).get("platform") or {}).get("acked_at")]
    if not (progress["busy"] or red or (not moved_on and (by["todo"] or by["asked"]))):
        return ""
    for limit in (CHAIN_LIST, 1, 0):
        parts = [f"开好 {len(by['opened'])}/{len(items)} 个"]
        if by["running"]:
            parts.append(f"正在执行 {_ids(by['running'], limit, True)}")
        if by["waiting"]:
            blocked = any(((i["packet"].get("platform") or {}).get("preview") or {}).get("verdict") not in (None, "ready")
                          for i in by["waiting"] if i.get("packet"))
            parts.append(f"在待审批 {_ids(by['waiting'], limit, True)}"
                         + ("：预览没通过，可以「重新预览」，或者点「不发」跳过它，系统接着开下一个" if blocked else ""))
        drafting = progress.get("drafting")
        if progress["busy"] == "drafting" and drafting:
            parts.append("正在起草" + (f" {drafting['item']}" if drafting["item"] else "") + f"（任务 #{drafting['job']}）")
        if progress["busy"] == "owed":
            parts.append("这一步欠着")
        if by["dropped"]:
            parts.append(f"这一轮不开 {_ids(by['dropped'], limit)}（运营点了「不发」）")
        if by["not_done"]:
            parts.append(f"没开成 {_ids(by['not_done'], limit, True)}")
        if by["uncertain"]:
            parts.append(f"结果不确定 {_ids(by['uncertain'], limit, True)}，先到平台后台核对")
        if by["asked"]:
            parts.append(f"派过、草稿没开 {_ids(by['asked'], limit)}")
        if by["todo"]:
            parts.append(f"还没开 {_ids(by['todo'], limit)}")
        text = f"专属佣金（寄样单 {progress['number']}）：" + "；".join(parts)
        if not progress["busy"]:
            text += f"。{hint}"
        if len(text) <= NOTICE_CHARS or limit == 0:
            return text
    return text


def _order_at(order: dict) -> float:
    """When a sample order was approved (verified, for one that went through at once): a signature before
    it is of another shipment."""
    return float(order.get("decided_at") or order["created_at"])


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


def _refused(result) -> str:
    """Why the backend did not even try a send (``wcdb_backend.SendRefused.reason``: a chat whose name
    another chat has, a chat it cannot find in WeChat), else "": the draft says it, so the operator
    knows to send it by hand or give her a remark of her own."""
    return str(getattr(result, "reason", "") or "").strip()


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
        ids = order_items(action)
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
