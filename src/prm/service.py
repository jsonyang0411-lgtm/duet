"""PRM copilot (``PRM_COPILOT=on``): a registered creator's private messages get drafted
replies on the Mac, and an operator decides in the console what is sent.

The flow, all of it silent towards the creator::

    creator writes ──► route step ``prm`` ──► ``handle``: phone/ID numbers become
    placeholders, the text (and any picture or file) goes into a ``prm`` job of the
    Claude relay (src/relay; a burst of messages is one job) ──► the Mac worker drafts
    in its locked-down workspace ──► ``on_result``: the JSON is checked
    (result.py) and becomes a pending packet ──► the console: send / edit and send /
    drop ──► the sender thread puts the numbers back and sends through the WeChat
    backend (text first, then the material files).

Nothing here ever writes to a creator on its own: every packet waits for a person
(route ``review``; the auto-send rules of the assessment's 8.1 come later).  A new
message from the creator makes the drafts still waiting out of date (superseded).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from ..relay.attachments import KINDS as ATTACHMENT_KINDS, Held, attachment_kind
from ..relay.service import MAX_AGE_SEC, job_meta
from ..relay.store import PRM
from . import pii
from .result import MAX_DRAFT_CHARS, MESSAGE_KINDS, DraftError, parse_draft
from .store import (
    APPROVED, DROPPED, FAILED, NO_REPLY, PENDING, SENDING, SENT, STAGES, SUPERSEDED,
    PartyError, PrmStore,
)

logger = logging.getLogger(__name__)

SETTLE_SEC = 8               # messages within this time of each other are one job
APPROVED_EXPIRE_SEC = 1800   # an approved draft not sent by then (the bot was stopped) is not sent
SEND_TICK_SEC = 5
_QUOTE_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9]+:\s*")
_WXID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{3,63}$")
_VOICE = re.compile(r"^\[语音\]\s*(.*)$", re.S)


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
    """What a creator's message says, as one line for the draft (pictures and files by name)."""
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


def snapshot(store: PrmStore, *, running: bool, extra: Optional[dict] = None, limit: int = 40) -> dict:
    """What the console shows (GET /api/prm); also built from the database when the bot is stopped."""
    parties = {p["id"]: p for p in store.parties(include_inactive=True)}
    deals = {}
    for deal in store.deals():
        deals[deal["party_id"]] = deal                 # the newest deal of each creator
    pending = list(reversed(store.packets((PENDING, APPROVED, SENDING), limit=200)))
    recent = store.packets((SENT, FAILED, DROPPED, SUPERSEDED, NO_REPLY), limit=limit)

    def packet_out(p: dict) -> dict:
        party = parties.get(p["party_id"]) or {}
        tokens = store.tokens(p["party_id"])
        shown = p["final_text"] if p.get("final_text") is not None else p["text"]
        return {
            "id": p["id"], "deal_id": p["deal_id"], "party_id": p["party_id"],
            "party_name": party.get("name", "?"), "job_id": p["job_id"], "status": p["status"],
            "kind": p["kind"], "kind_label": MESSAGE_KINDS.get(p["kind"], p["kind"]), "route": p["route"],
            "text": p["text"], "final_text": p.get("final_text"), "edited": p["edited"],
            "legend": pii.legend(f"{p['text']}\n{shown}", tokens),
            "attachments": [{"name": a.get("name"), "size": a.get("size")} for a in p["attachments"]],
            "intent": p["intent"], "slots": p["slots"], "stage_proposal": p["stage_proposal"],
            "stage_proposal_label": STAGES.get(p["stage_proposal"], ""),
            "needs_attention": p["needs_attention"], "reason": p["reason"],
            "outcome": p["outcome"], "error": p["error"],
            "created_at": p["created_at"], "decided_at": p["decided_at"], "sent_at": p["sent_at"],
        }

    def party_out(p: dict) -> dict:
        deal = deals.get(p["id"]) or {}
        return {
            "id": p["id"], "wxid": p["wxid"], "name": p["name"], "note": p["note"],
            "active": bool(p["active"]), "authorized_at": p["authorized_at"],
            "deal": {
                "id": deal.get("id"), "stage": deal.get("stage", ""), "stage_label": STAGES.get(deal.get("stage", ""), ""),
                "mode": deal.get("mode", ""), "slots": deal.get("slots", {}),
                "stage_proposal": deal.get("stage_proposal", ""),
                "stage_proposal_label": STAGES.get(deal.get("stage_proposal", ""), ""),
                "attention": deal.get("attention", ""), "last_message_at": deal.get("last_message_at"),
            } if deal else None,
        }

    return {
        "ok": True, "running": running,
        "parties": [party_out(p) for p in parties.values()],
        "pending": [packet_out(p) for p in pending],
        "recent": [packet_out(p) for p in recent],
        "counts": store.counts(),
        "stages": STAGES, "kinds": MESSAGE_KINDS,
        **(extra or {}),
    }


class PrmService:
    def __init__(self, config, relay, *, store: Optional[PrmStore] = None,
                 send: Optional[Callable[[str, str], bool]] = None,
                 send_file: Optional[Callable[[str, str], bool]] = None,
                 clock: Callable[[], float] = time.time,
                 on_parties_changed: Optional[Callable[[], None]] = None):
        self._config = config
        self._relay = relay
        self._store = store if store is not None else PrmStore(config.prm_db)
        self._send = send
        self._send_file = send_file
        self._clock = clock
        self.on_parties_changed = on_parties_changed
        # The admin and the cc users are never creators: their messages stay the relay's.
        self._reserved = {str(w).strip() for w in [getattr(config, "admin_wxid", ""),
                                                    *(getattr(config, "claude_relay_wxids", None) or [])]
                          if str(w).strip()}
        self._wxids = self._store.active_wxids()
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

    # ── from WeChat (the route step) ──────────────────────────────────────

    def is_party_chat(self, chat_id: str) -> bool:
        """Is this private chat a registered creator's? (the WeChat backend reads such chats)"""
        return str(chat_id) in self._wxids

    def handle(self, msg: dict) -> bool:
        """True when this is a registered creator's private message (it is then dealt with, silently)."""
        if msg.get("is_group", True):
            return False
        chat_id = str(msg.get("chat_id", ""))
        if chat_id not in self._wxids:
            return False
        party = self._store.party_by_wxid(chat_id)
        if party is None:
            return False
        now = self._clock()
        sent_at = float(msg.get("timestamp", 0) or 0)
        if sent_at and now - sent_at > MAX_AGE_SEC:
            logger.info("PRM: an old message of party %d (%.0f s) is history, not drafted", party["id"], now - sent_at)
            return True
        deal = self._store.deal_of(party["id"], now)
        files: tuple = ()
        line = message_text(msg)
        kind = attachment_kind(msg)
        if kind:
            collected, problems = self._relay.collect([Held(kind, msg, now)])
            files = tuple(collected)
            if problems:
                line += "（" + "；".join(problems) + "）"
            elif not files:
                line += f"（{ATTACHMENT_KINDS[kind]}没取到）"
        clock_text = time.strftime("%H:%M", time.localtime(sent_at or now))
        text = pii.mask(f"[{clock_text}] {line}",
                        lambda k, label, value: self._store.token_for(party["id"], k, label, value, now))
        stale = self._store.supersede(deal["id"], now)
        self._store.update_deal(deal["id"], now, last_message_at=sent_at or now)
        how, job_id = self._relay.submit(
            chat_id, chat_id, text, kind=PRM, files=files, settle_sec=SETTLE_SEC,
            meta={"party_id": party["id"], "deal_id": deal["id"], "name": party["name"]},
        )
        logger.info("PRM: party %d message -> job #%d (%s)%s", party["id"], job_id, how,
                    f"; {stale} waiting draft(s) out of date" if stale else "")
        return True

    # ── from the Mac (the relay's delivery thread) ────────────────────────

    def on_result(self, job: dict) -> bool:
        """A prm job ended: its draft becomes a pending packet (or the deal says what went wrong)."""
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
        if status == "cancelled":
            return True
        if status in ("failed", "lost"):
            try:
                extra = json.loads(job.get("result_json") or "{}")
            except ValueError:
                extra = {}
            extra = extra if isinstance(extra, dict) else {}
            why = "Mac 失联了" if status == "lost" else str(extra.get("error") or job.get("result_text") or "没有返回内容")
            self._store.update_deal(deal_id, now, attention=f"#{job['id']} 草稿没写成：{why[:120]}")
            logger.warning("PRM: job #%d %s; the deal needs a person", job["id"], status)
            return True
        if self._store.packet_for_job(job["id"]) is not None:
            return True                                # already drafted (a repeated delivery)
        try:
            draft = parse_draft(job.get("result_text") or "")
        except DraftError as e:
            self._store.update_deal(deal_id, now, attention=f"#{job['id']} 的草稿格式不对（{e.code}），没有生成待审批")
            logger.warning("PRM: job #%d answer unusable: %s", job["id"], e)
            return True
        files = self._relay.store.outfiles(job["id"])
        attachments = [{"name": f["name"], "path": f["path"], "size": f["size"], "sha256": f["sha256"]} for f in files]
        newer = self._relay.store.latest_open(job["chat_id"])
        if not draft.reply_draft and not attachments:
            state = NO_REPLY
        elif newer is not None and newer["id"] > job["id"]:
            state = SUPERSEDED                         # the creator wrote again: a newer draft is coming
        else:
            state = PENDING
        packet_id = self._store.add_packet(
            deal_id=deal_id, party_id=party_id, job_id=job["id"], chat_id=job["chat_id"],
            kind=draft.message_kind, text=draft.reply_draft, attachments=attachments, intent=draft.intent,
            slots=draft.slots, stage_proposal=draft.stage_proposal, needs_attention=draft.needs_attention,
            reason=draft.reason, status=state, now=now,
        )
        if state == PENDING:
            self._store.supersede(deal_id, now, keep=packet_id)
        missing = len(draft.attachments_to_send) - len(attachments)
        attention = draft.reason if draft.needs_attention else ""
        if missing > 0:
            attention = (attention + "；" if attention else "") + f"草稿要附 {len(draft.attachments_to_send)} 个文件，只收到 {len(attachments)} 个"
        self._store.update_deal(deal_id, now, merge_slots=draft.slots,
                                stage_proposal=draft.stage_proposal, attention=attention[:300])
        logger.info("PRM: job #%d -> packet %d (%s, %s)", job["id"], packet_id, state, draft.message_kind)
        return True

    # ── from the console ──────────────────────────────────────────────────

    def register(self, wxid: str, name: str, note: str = "") -> dict:
        wxid = check_wxid(wxid, self._reserved)
        name, note = str(name or "").strip(), str(note or "").strip()
        if not name or len(name) > 30:
            raise PartyError("称呼要填，最多 30 个字")
        if len(note) > 200:
            raise PartyError("备注最多 200 个字")
        party = self._store.add_party(wxid, name, note, self._clock())
        self._parties_changed()
        logger.info("PRM: party %d registered", party["id"])
        return party

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

    def decide(self, packet_id: int, action: str, text: Optional[str] = None) -> dict:
        """An operator's choice for a pending draft: send | edit (send ``text``) | drop."""
        packet = self._store.packet(int(packet_id))
        if packet is None:
            return {"ok": False, "error": f"没有找到草稿 {packet_id}"}
        now = self._clock()
        if action == "drop":
            if not self._store.move(packet["id"], (PENDING,), DROPPED, decided_at=now):
                return {"ok": False, "error": "这条草稿已经处理过了"}
            return {"ok": True, "status": DROPPED}
        if action not in ("send", "edit"):
            return {"ok": False, "error": f"不认识的操作 {action}"}
        final = packet["text"] if action == "send" else str(text if text is not None else "").strip()
        if len(final) > MAX_DRAFT_CHARS:
            return {"ok": False, "error": f"最多 {MAX_DRAFT_CHARS} 个字"}
        if not final and not packet["attachments"]:
            return {"ok": False, "error": "没有要发的内容"}
        unknown = pii.unknown_tokens(final, self._store.tokens(packet["party_id"]))
        if unknown:
            return {"ok": False, "error": f"草稿里有对不上的占位符：{'、'.join(unknown)}"}
        if self._send is None:
            return {"ok": False, "error": "机器人没有连着微信，现在不能发送"}
        if packet["attachments"] and self._send_file is None:
            return {"ok": False, "error": "这个微信后端不能发文件（只有 Windows 数据库直读能发）"}
        if not self._store.move(packet["id"], (PENDING,), APPROVED, decided_at=now, final_text=final,
                                edited=int(final != packet["text"])):
            return {"ok": False, "error": "这条草稿已经处理过了（或者达人又发了新消息，草稿已过时）"}
        self._wake.set()
        return {"ok": True, "status": APPROVED}

    def snapshot(self) -> dict:
        relay = self._relay.snapshot()
        return snapshot(self._store, running=True, extra={
            "mac_online": relay.get("mac_online", False),
            "worker_kinds": relay.get("worker_kinds", []),
            "can_send": self._send is not None, "can_send_files": self._send_file is not None,
        })

    # ── sending what the operators approved ───────────────────────────────

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.send_approved()
            except Exception:
                logger.exception("PRM: sending round failed")
            self._wake.wait(SEND_TICK_SEC)
            self._wake.clear()

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
        text = pii.unmask(packet["final_text"] if packet["final_text"] is not None else packet["text"], tokens)
        problem = ""
        if text.strip():
            try:
                if not self._send(packet["chat_id"], text):
                    problem = "文字没发出去"
            except Exception:
                logger.exception("PRM: packet %d text not sent", packet["id"])
                problem = "文字发送出错"
        for item in packet["attachments"] if not problem else ():
            path = str(item.get("path") or "")
            if not Path(path).is_file():
                problem = f"文件「{item.get('name')}」在电脑上找不到了"
                break
            try:
                if not self._send_file(packet["chat_id"], path):
                    problem = f"文件「{item.get('name')}」没发出去"
                    break
            except Exception:
                logger.exception("PRM: packet %d file not sent", packet["id"])
                problem = f"文件「{item.get('name')}」发送出错"
                break
        now = self._clock()
        if problem:
            self._store.move(packet["id"], (SENDING,), FAILED, outcome="failed", sent_at=now, error=problem)
            logger.warning("PRM: packet %d failed: %s", packet["id"], problem)
            return 0
        # The backend says the keystrokes went through; nobody read the chat back yet.
        self._store.move(packet["id"], (SENDING,), SENT, outcome="sent_unconfirmed", sent_at=now)
        logger.info("PRM: packet %d sent (%d chars, %d file(s))", packet["id"], len(text), len(packet["attachments"]))
        return 1
