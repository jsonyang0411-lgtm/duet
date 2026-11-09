"""微信里审批草稿 (PRM_WECHAT_REVIEW, 2026-11-09, default off): the drafts that wait for a person in 「待审批」 are also
sent, once each, to the admin's private chat (ADMIN_WXID), and the admin decides them there, from the phone.

The user (2026-11-09): often away from the Windows PC, they ended up copying every draft out of the console into WeChat
by hand.  Now:

Notifications (``sweep``, on the PRM sender thread): every draft waiting for a person (a ``pending`` packet: a creator's
private chat, or a 老板拉的群 the bot took over) goes to the admin as one message (``notification_text``): its number
first (the packet id, the console's 「DRAFT #12」), the creator and where it goes (私聊 / 群里「群名」, with the console's
group warning), her last line(s) it answers (short; numbers as their placeholders, addresses cut to province and city,
a labelled 微信号 masked), the draft in full with its placeholders (their masked numbers after it, as the console's
legend), a shipping notice's waybill, its files, and how to answer.  None for what goes out by itself (a kind's gate
``auto``, 下班时段 and 自动模式 approve it at once: never pending; 开场白自动发: ``meta.auto_at`` while
``send_due_openings`` still takes it), a 百应 draft (``BUYIN_TOOL``: copied into 百应 by hand), a holding reply (no
packet), or anything while ADMIN_WXID is empty (``status`` says why on 「待审批」).  A draft with a platform action is
told too, with its action, but version 1 never runs one from WeChat: the notification sends the admin to the console's
「批准并执行」, and WeChat can only drop it.  PER_ROUND sends a round at most; a send WeChat does not take ends the round
and the admin's chat is tried again 10 s later, doubling, at most 2 minutes apart, each message 6 times in all (as the
relay's approval requests, src/relay/service.py ``_send_asks``); no draft ever waits for its notification.  One row
per draft told (``wechat_reviews`` in prm.db): a draft told is never told again, after a restart too, and one not told
yet is told then.

Answers (``answer``, the route step ``wechat_review``, before the relay or an admin command can take them): typed text
in the admin's own private chat, never a voice message (heard wrong, a 「不发」 could send: the bot says to type).
Quoting a notification (WeChat 引用; the backend's ``quoted_content`` starts with 「#12 草稿」): 「1」「发」「发送」「好」「好的」
「可以」「行」「ok」 send it as drafted, 「2」「不发」「不要」「算了」 drop it, 「改 <你的话>」 sends those words instead (the
console's 改了再发: its length, its placeholders, a shipping notice's waybill, ``PrmService.edit_problem``); any other text
is not sent: it is kept as the draft's proposed edit and the bot asks 「#12 改成：『…』 回 1 发出／2 不发」, after which a
「1」 quoting either message (or bare, while it is the only open question) sends it.  Without a quote: 「发 12」「不发 12」
「改 12 <你的话>」; a bare 「1」 / 「2」 only while exactly one notified draft waits (several: the bot lists them, so the
answer names one).  Each answer gets one reply: approved and where it goes, already handled, out of date (and the draft
that replaced it), or ``PrmService.decide``'s own refusal; a draft approved here whose send then fails is told in one
line (``sweep``).  A decision here and one in the console are the same decision: the packet's compare-and-set moves
(``decide``) let only the first through, the second hears it was handled.

Nothing of it while the switch is off (no notification, no row, the route step passes every message on), nor on the
Mac's edge side (WECHAT_BACKEND=edge: the admin's chat is read on Windows there).  The log has packet ids and counts
only: never a creator's name or words, never a draft.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from ..relay.service import MAX_AGE_SEC
from . import contact, deals, pii, platforms, retention
from .boss_groups import CHANNEL as GROUP_CHANNEL
from .store import (APPROVED, BUYIN_TOOL, COPILOT, DROPPED, EXECUTING, FAILED, MESSAGE_TOOL, NO_REPLY, PENDING, SENDING,
                    SENT, SUPERSEDED, PrmStore)

logger = logging.getLogger(__name__)

NOTIFIED, PROPOSED, ANSWERED = "notified", "proposed", "answered"   # ``wechat_reviews.state``
SEND_WORDS = ("1", "发", "发送", "好", "好的", "可以", "行", "ok")    # quoting a notification: send it
DROP_WORDS = ("2", "不发", "不要", "算了")                          # ... drop it
RETRY_SEC = (10, 120)       # a send the admin's chat did not take: the chat again after 10 s, doubling, at most 2 min apart
SEND_TRIES = 6              # ... and a message not again (this run) after this many failed sends (about 4.5 minutes)
PER_ROUND = 3               # sends one round of the sender thread makes at most (a pile waits its turn)
RECENT_SEC = 3600           # a bare 「1」 / 「2」 with nothing open answers about the last notification this recent
BARE_MARGIN_SEC = 5         # a bare answer counts for a notification told at most this long after it was written
SAID_LINES = 2              # her last lines a notification quotes
SAID_CHARS = 60             # ... each cut to this many characters
NOTE_CHARS = 120            # the draft's reason (when it asks for attention), an error told
FILES_NAMED = 5             # file names a notification lists at most
LISTED = 8                  # open drafts a list answer names at most
SEEN_IDS = 500              # the admin's answers remembered by message id (one handed on twice counts once)

_HEAD = re.compile(r"^\s*[#＃](\d{1,9})\s*(草稿|改成)")
_STAMP = re.compile(r"^\[\d{2}-\d{2} \d{2}:\d{2}\]\s*")
_QUOTE_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9]+:\s*")
_VOICE = re.compile(r"^\[语音\]\s*(.*)$", re.S)
_NUM = r"[#＃]?\s*(\d{1,9})"
_SEND_N = re.compile(rf"^(?:发送|发)\s*{_NUM}$")
_DROP_N = re.compile(rf"^不发\s*{_NUM}$")
_EDIT_N = re.compile(rf"^改\s*{_NUM}(?:\s*[:：]\s*|\s+)(\S.*)$", re.S)
_EDIT_N_ONLY = re.compile(rf"^改\s*{_NUM}\s*[:：]?$")
_EDIT = re.compile(r"^改成?(?:\s*[:：]\s*|\s+)(\S.*)$", re.S)
_TRAILING = re.compile(r"[\s。.!~～]+$")

NO_ADMIN_WHY = ("微信里审批草稿打开了，但没填管理员 wxid（ADMIN_WXID）：草稿发不到微信里。到「中转代理」页的设置里填好"
                "「管理员 wxid」，再重启机器人")
NO_SEND_WHY = "机器人没有连着微信：草稿发不到管理员微信里"
EDGE_WHY = "Mac 端（WECHAT_BACKEND=edge）不能在微信里审批草稿：管理员的私聊在 Windows 上读"
VOICE_TEXT = "回草稿要打字：引用那条通知回 1 发出、2 不发，或者回「发 12」这样带编号；语音不算（免得听错）"
REST_TEXT = "其余的话没有当成回复：要改草稿请回「改 编号 你的话」"


def _cut(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _word(text: str) -> str:
    """A short answer as it is compared: full-width letters and digits as plain ones, trailing punctuation off, lower."""
    return _TRAILING.sub("", unicodedata.normalize("NFKC", str(text or "")).strip()).lower()


@dataclass(frozen=True)
class Answer:
    """What a message of the admin asks of the drafts (``parse``)."""

    kind: str                  # quote | lines | edit | ask | voice
    number: Optional[int] = None
    head: str = ""             # quote: 草稿 (a notification) or 改成 (the bot's question about a proposed edit)
    text: str = ""             # quote: the words typed; edit: the new text
    lines: tuple = ()          # lines: ((action, number), ...): send / drop a number, bare 1 / 2
    rest: bool = False         # lines: other words came with them
    bare: bool = False         # lines / voice: only bare 「1」 / 「2」 (theirs only while a notification is about)
    quoted: Optional[int] = None   # edit / ask: the number of the notification it quotes (its words are for that
                                   # draft when its own number was never told)
    heard: Optional["Answer"] = None   # voice: what its words would be, typed


def _line(line: str, bare: bool) -> Optional[tuple]:
    m = _SEND_N.match(line)
    if m:
        return ("send", int(m.group(1)))
    m = _DROP_N.match(line)
    if m:
        return ("drop", int(m.group(1)))
    word = _word(line)
    if bare and word in ("1", "2"):
        return ("bare", int(word))
    return None


def _lines(content: str, bare: bool = True) -> Optional[Answer]:
    """「发 12」「不发 12」 (and, ``bare``, 「1」 / 「2」), one a line (a burst of them is one message): None unless the first
    line is one; the lines that are not come back as ``rest``."""
    rows = [line.strip() for line in content.split("\n") if line.strip()]
    if not rows or _line(rows[0], bare) is None:
        return None
    items = [item for item in (_line(line, bare) for line in rows) if item is not None]
    return Answer("lines", lines=tuple(items), rest=len(items) < len(rows),
                  bare=all(action == "bare" for action, _ in items))


def parse(msg: dict) -> Optional[Answer]:
    """What the admin's message asks of the drafts, or None when it is no answer to one (pure: nothing is read).

    Typed text only (``msg_type`` 1, or a quote: 49 with ``quoted_content``).  A voice message saying an answer is
    ``voice`` (it never counts).  Quoting something else than a notification or the bot's question: only 「发 12」
    「不发 12」「改 12 …」 (a 「1」 there is somebody else's)."""
    try:
        msg_type = int(msg.get("msg_type", 1) or 1)
    except (TypeError, ValueError):
        return None
    content = _QUOTE_PREFIX.sub("", str(msg.get("content", "") or "")).strip()
    if msg_type == 34:
        heard = _VOICE.match(content)
        said = heard.group(1).strip().rstrip("。.！!，,、 ") if heard else ""
        inner = parse({"msg_type": 1, "content": said}) if said else None
        return Answer("voice", bare=inner.bare, heard=inner) if inner is not None else None
    quoted = _QUOTE_PREFIX.sub("", str(msg.get("quoted_content", "") or "")).strip()
    if not content or msg_type not in (1, 49) or (msg_type == 49 and not quoted):
        return None
    head = _HEAD.match(quoted) if quoted else None
    of = int(head.group(1)) if head is not None else None
    m = _EDIT_N.match(content)
    if m:
        return Answer("edit", int(m.group(1)), text=m.group(2).strip(), quoted=of)
    m = _EDIT_N_ONLY.match(content)
    if m:
        return Answer("ask", int(m.group(1)), quoted=of)
    if head is not None:
        return _lines(content, bare=False) or Answer("quote", int(head.group(1)), head.group(2), content)
    return _lines(content, bare=not quoted)        # (quoting another message: 「发 12」 is still the draft's, a 1 is not)


class WechatReview:
    """微信里审批草稿 for one PRM service (``PrmService.wechat_review``); see the module's docstring.

    ``decide(packet_id, action, text)`` and ``check(packet, text)`` are the service's ``decide`` / ``edit_problem``;
    ``lines(packet)`` her lines a draft answers (the job's); ``parts(text)`` how many messages a text goes out as
    (PRM_REPLY_PARTS); ``drafting(chat)`` whether a newer draft of that chat is being written on the Mac; ``local``
    WeChat's time on this PC's clock; ``opening_auto``: PRM_OPENING_AUTO_MIN is on (an opening with its time set goes
    by itself)."""

    def __init__(self, store: PrmStore, config, *, clock: Callable[[], float] = time.time,
                 send: Optional[Callable[[str, str], bool]] = None,
                 decide: Optional[Callable[..., dict]] = None, check: Optional[Callable[[dict, str], str]] = None,
                 lines: Optional[Callable[[dict], str]] = None, parts: Optional[Callable[[str], int]] = None,
                 drafting: Optional[Callable[[str], bool]] = None, local: Optional[Callable[[float], float]] = None,
                 opening_auto: bool = False):
        self._store = store
        self._clock = clock
        self._send = send
        self._decide = decide
        self._check = check
        self._lines = lines
        self._parts = parts
        self._drafting = drafting
        self._local = local or (lambda written: written)
        self._opening_auto = bool(opening_auto)
        self.admin = str(getattr(config, "admin_wxid", "") or "").strip()
        self._edge = str(getattr(config, "wechat_backend", "") or "") == "edge"
        self._switch = str(getattr(config, "prm_wechat_review", "off") or "off").strip().lower() == "on"
        self.on = self._switch and not self._edge     # (the route step and the sweep do nothing otherwise)
        self._failures: dict[tuple, int] = {}       # ("draft" | "failed", packet id) -> its sends WeChat did not take
        self._chat_failures = 0                       # sends to the admin's chat not taken in a row (RETRY_SEC)
        self._wait_until = 0.0                        # ... and when the chat is tried again
        self._seen: deque = deque(maxlen=SEEN_IDS)
        self._lock = threading.Lock()                 # one answer at a time

    # ── the console ───────────────────────────────────────────────────────

    def status(self) -> dict:
        """What 「待审批」 says (GET /api/prm ``wechat_review``): ``on`` (the switch), ``why`` it cannot work ("": it does),
        how many told drafts wait for the admin's answer (``open``) and how many could not be told this run
        (``unsent``)."""
        if not self._switch:
            return {"on": False}
        why = EDGE_WHY if self._edge else NO_ADMIN_WHY if not self.admin else NO_SEND_WHY if self._send is None else ""
        unsent = sum(1 for (what, _), failures in list(self._failures.items())
                     if what == "draft" and failures >= SEND_TRIES)
        return {"on": True, "why": why, "open": 0 if why else len(self._store.wechat_reviews_open()),
                "unsent": 0 if why else unsent}

    # ── notifications (the sender thread) ─────────────────────────────────

    @property
    def ready(self) -> bool:
        return self.on and bool(self.admin) and self._send is not None

    def sweep(self) -> int:
        """One round of the sender thread: the drafts waiting for a person not told yet (oldest first), then the drafts
        approved here whose send failed; PER_ROUND sends at most.  A send WeChat does not take ends the round, and the
        admin's chat waits before the next one (RETRY_SEC: 10 s, doubling, at most 2 minutes apart); a message not
        taken SEND_TRIES times is not tried again this run (logged; the console still has the draft).  Returns how
        many messages went to the admin."""
        if not self.ready or self._clock() < self._wait_until:
            return 0
        sent = tried = 0
        for packet in self._store.wechat_review_due():
            key = ("draft", packet["id"])
            if self._goes_by_itself(packet) or self._failures.get(key, 0) >= SEND_TRIES:
                continue
            if tried >= PER_ROUND:
                return sent
            try:
                text = self.notification_text(packet)
            except Exception:
                logger.exception("PRM: the notification of draft %d could not be written; not told", packet["id"])
                self._failures[key] = SEND_TRIES
                continue
            current = self._store.packet(packet["id"])
            if current is None or current["status"] != PENDING:
                continue                                   # decided (or out of date) meanwhile
            tried += 1
            if not self._try(key, text, f"the notification of draft {packet['id']}"):
                return sent
            self._store.add_wechat_review(packet["id"], packet["deal_id"], self._clock())
            logger.info("PRM: draft %d told to the admin's chat (微信里审批)", packet["id"])
            sent += 1
        for row in self._store.wechat_reviews_failed():
            key = ("failed", row["packet_id"])
            if self._failures.get(key, 0) >= SEND_TRIES:
                continue
            if tried >= PER_ROUND:
                return sent
            name = (self._store.party(row["party_id"]) or {}).get("name") or "她"
            text = (f"#{row['packet_id']} 没发出去：{_cut(row.get('error') or '发送出错', NOTE_CHARS)}；"
                    f"需要的话请在微信里手动发给 {name}")
            tried += 1
            if not self._try(key, text, f"the failure of draft {row['packet_id']}"):
                return sent
            self._store.set_wechat_review(row["packet_id"], (ANSWERED,), self._clock(), told_failed_at=self._clock())
            sent += 1
        return sent

    def _goes_by_itself(self, packet: dict) -> bool:
        """开场白没人处理就自动发 (PRM_OPENING_AUTO_MIN): ``send_due_openings`` sends it by itself when its time comes."""
        due = (packet.get("meta") or {}).get("auto_at")
        return (self._opening_auto and isinstance(due, (int, float)) and not isinstance(due, bool)
                and not packet.get("needs_attention"))

    def _try(self, key: tuple, text: str, what: str) -> bool:
        """Send ``text`` to the admin; when WeChat does not take it, the message's failures and the chat's are counted
        and the chat's next try is set (RETRY_SEC); one that goes out clears both."""
        try:
            ok = bool(self._send(self.admin, text))
        except Exception:
            logger.exception("PRM: %s could not be sent to the admin's chat", what)
            ok = False
        if ok:
            self._failures.pop(key, None)
            self._chat_failures, self._wait_until = 0, 0.0
            return True
        failures = self._failures.get(key, 0) + 1
        self._failures[key] = failures
        self._chat_failures += 1
        delay = min(RETRY_SEC[0] * 2 ** (self._chat_failures - 1), RETRY_SEC[1])
        self._wait_until = self._clock() + delay
        if failures >= SEND_TRIES:
            logger.error("PRM: %s not sent to the admin's chat after %d tries; not tried again (the console still has "
                         "the draft)", what, failures)
        else:
            logger.warning("PRM: %s not sent to the admin's chat (try %d); the next try in %d s", what, failures, delay)
        return False

    def notification_text(self, packet: dict) -> str:
        """The message that tells the admin of a draft (see the module's docstring)."""
        number = packet["id"]
        meta = packet.get("meta") or {}
        name = (self._store.party(packet["party_id"]) or {}).get("name") or "?"
        group = meta.get("channel") == GROUP_CHANNEL
        platform = packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL
        lines = [f"#{number} 草稿 · {name} · " + (f"群里「{meta.get('group_name') or '老板拉的群'}」" if group else "私聊")]
        if group:
            lines.append("（发在老板拉的群里，不是她的私聊：老板和群里的人都看得到）")
        said = self.her_words(packet)
        if said:
            lines.append(f"她：{said}")
        elif meta.get("opening"):
            lines.append("（开场白：她还没跟我们说过话，这是我们先打招呼）")
        elif meta.get("huopan"):
            lines.append("（运营点了「补发货盘」：我们主动补发的货盘）")
        elif meta.get("signed"):
            lines.append("（聚水潭显示样品签收了：问她一句拿到没有、合不合适）")
        if platform:
            lines.append(self._platform_line(packet))
        if packet.get("kind") == "shipping_notice":
            bills = [(meta.get("carrier") or "", meta.get("waybill") or "")]
            bills += [(b.get("carrier") or "", b.get("waybill") or "") for b in meta.get("more_waybills") or []
                      if isinstance(b, dict)]
            shown = "、".join(" ".join(x for x in bill if x) for bill in bills if bill[1])
            lines.append(f"运单：{shown}（发出的内容里必须是这个单号）" if shown else "运单：还没有运单号")
        if packet.get("needs_attention") and packet.get("reason"):
            lines.append(f"要注意：{_cut(packet['reason'], NOTE_CHARS)}")
        text = str(packet.get("text") or "")
        lines.append(f"草稿：{text}" if text.strip() else "草稿：（只发附件，没有文字）")
        count = self._parts(text) if self._parts is not None and text.strip() else 1
        if count > 1:
            lines.append(f"（会分 {count} 条发）")
        legend = pii.legend(f"{text}\n{said}", pii.display(self._store.token_rows(packet["party_id"])))
        if legend:
            lines.append("号码：" + "；".join(f"{x['token']} = {x['masked']}" for x in legend)
                         + "（发出时换成真实号码；改的时候保留占位符）")
        files = [str(a.get("name") or "文件") for a in packet.get("attachments") or []]
        if files:
            lines.append(f"（附 {len(files)} 个文件：{'、'.join(files[:FILES_NAMED])}{'…' if len(files) > FILES_NAMED else ''}）")
        if platform:
            lines.append("这份带平台动作，请在控制台「批准并执行」；微信里只能回 2 不发")
        else:
            lines.append(f"回 1 {'发到群里' if group else '发出'}｜2 不发｜改 + 你的话（直接发你改的）")
            lines.append(f"（引用这条回；不引用就回「发 {number}」「不发 {number}」「改 {number} 你的话」）")
        return "\n".join(lines)

    def her_words(self, packet: dict) -> str:
        """Her last line(s) the draft answers, short (SAID_LINES, SAID_CHARS each): from its job's lines without their
        time; numbers stay their placeholders, an address is cut to province and city (``retention.scrub``, with the
        deal's addresses), a labelled 微信号 is masked.  "" for a draft with no line of hers (a shipping notice, a
        开场白)."""
        if packet.get("job_id") is None or self._lines is None:
            return ""
        said = [_STAMP.sub("", line).strip() for line in str(self._lines(packet) or "").split("\n")]
        said = [line for line in said if line]
        if not said:
            return ""
        deal = self._store.deal(packet["deal_id"]) or {}
        addresses = retention.address_values(deal.get("slots") or {})
        last = said[-SAID_LINES:]
        shown = [_cut(contact.mask_contacts(retention.scrub(line, addresses)), SAID_CHARS) for line in last]
        return ("…" if len(said) > len(last) else "") + "／".join(shown)

    @staticmethod
    def _platform_line(packet: dict) -> str:
        data = packet.get("platform") or {}
        preview = data.get("preview") or {}
        name = (data.get("action") or {}).get("action") or packet.get("tool")
        line = f"平台动作：{platforms.label(name)}"
        title = (preview.get("catalog") or {}).get("title")
        if title:
            line += f"（{_cut(title, 30)}）"
        verdict = preview.get("verdict") or "unavailable"
        if verdict != "ready":
            line += f"；预览没通过（{platforms.VERDICTS.get(verdict, verdict)}）"
        return line

    # ── the admin's answers (the route step ``wechat_review``) ───────────

    def is_admin_line(self, msg: dict) -> bool:
        """The admin's own message in the admin's one-to-one chat (never a group, nobody else)."""
        return (self.on and bool(self.admin) and not msg.get("is_group", True)
                and str(msg.get("sender_id") or "") == self.admin and str(msg.get("chat_id") or "") == self.admin)

    def claims(self, msg: dict) -> bool:
        """Would ``answer`` take this message (``_mine``)?  The route step's fallback when ``answer`` fails: such a
        message still never goes on to the relay."""
        if not self.is_admin_line(msg):
            return False
        parsed = parse(msg)
        return parsed is not None and self._mine(parsed)

    def answer(self, msg: dict) -> tuple[bool, Optional[str]]:
        """``(matched, reply)`` for the route step: matched = an answer to a draft notification (the route ends with
        ``reply``; None: silently, as for an answer read too late).  Anything else is not matched and goes on."""
        if not self.is_admin_line(msg):
            return False, None
        parsed = parse(msg)
        if parsed is None:
            return False, None
        with self._lock:
            return self._answer(msg, parsed)

    def _about(self) -> bool:
        """A bare 「1」 / 「2」 is an answer: a notification is open, or the last one is RECENT_SEC recent."""
        if self._store.wechat_reviews_open():
            return True
        latest = self._store.latest_wechat_review()
        return latest is not None and self._clock() - float(latest["notified_at"]) <= RECENT_SEC

    def _mine(self, parsed: Answer) -> bool:
        """Is ``parsed`` an answer to a draft, not a line that only looks like one (a 「1」 for the relay's Claude, a cc
        task 「改 3 处错别字」)?  A bare 「1」 / 「2」 while a notification is about (``_about``); 「改 N …」 / 「改 N」 for a
        draft told (or quoting a notification); every other form; a voice message as its words typed would be."""
        if parsed.kind == "voice":
            return parsed.heard is not None and self._mine(parsed.heard)
        if parsed.bare:
            return self._about()
        if parsed.kind in ("edit", "ask"):
            return parsed.quoted is not None or self._store.wechat_review(parsed.number) is not None
        return True

    def _answer(self, msg: dict, parsed: Answer) -> tuple[bool, Optional[str]]:
        if not self._mine(parsed):
            return False, None                             # (as before: the relay, an admin command, silence)
        msg_id = str(msg.get("message_id") or "")
        if msg_id and msg_id in self._seen:
            return True, None                              # (handed on twice: answered once)
        if msg_id:
            self._seen.append(msg_id)
        now = self._clock()
        written = float(msg.get("timestamp") or 0)
        if written and now - self._local(written) > MAX_AGE_SEC:
            logger.info("PRM: an answer to a draft notification read %.0f s after it was written; history, not acted on",
                        now - self._local(written))
            return True, None
        if parsed.kind == "voice":
            return True, VOICE_TEXT
        if parsed.kind == "quote":
            return True, self._on_quote(parsed.number, parsed.text)
        if parsed.kind == "lines":
            reply = "\n".join(r for r in (self._on_line(action, n, written) for action, n in parsed.lines) if r)
            return True, reply + (f"\n{REST_TEXT}" if parsed.rest else "")
        row, packet = self._row(parsed.number)             # edit / ask with a number
        if row is None:                                    # (``_mine``: it quotes a notification; its words are for that draft)
            return True, self._on_quote(parsed.quoted, _QUOTE_PREFIX.sub("", str(msg.get("content") or "")).strip())
        closed = self._closed(packet, row)
        if closed:
            return True, closed
        if parsed.kind == "ask":
            return True, f"#{parsed.number} 要改成什么？回「改 {parsed.number} 你的话」"
        return True, self._edit(row, packet, parsed.text)

    def _row(self, number) -> tuple[Optional[dict], Optional[dict]]:
        """The notification row and the packet of a draft number."""
        packet = self._store.packet(int(number))
        row = self._store.wechat_review(int(number)) if packet is not None else None
        return row, packet

    def _not_told(self, number: int, packet: Optional[dict]) -> str:
        """A number with no notification row: no such draft, one handled already (its row cleared with the deal's 8.2
        clean-up, say), or one never told here."""
        if packet is None:
            return f"没有找到草稿 #{number}"
        return self._closed(packet, None) or f"#{number} 没有发到微信里审批（只有发到这里的草稿能在微信里回）：请在控制台处理"

    def _on_quote(self, number: int, text: str) -> str:
        """An answer quoting the notification of ``number`` (or the bot's question about its proposed edit)."""
        row, packet = self._row(number)
        if row is None:
            return self._not_told(number, packet)
        closed = self._closed(packet, row)
        if closed:
            return closed
        word = _word(text)
        if word in SEND_WORDS:
            return self._send_it(row, packet)
        if word in DROP_WORDS:
            return self._drop(row, packet)
        edit = _EDIT.match(text.strip())
        if edit:
            return self._edit(row, packet, edit.group(1).strip())
        if word in ("改", "改成"):
            return f"#{number} 要改成什么？引用通知回「改 你的话」，或者回「改 {number} 你的话」"
        return self._propose(row, packet, text.strip())

    def _on_line(self, action: str, number: int, written: float) -> str:
        """One 「发 12」 / 「不发 12」 / bare 「1」 / 「2」 of a message."""
        if action == "bare":
            return self._bare(number, written)
        row, packet = self._row(number)
        if row is None:
            return self._not_told(number, packet)
        closed = self._closed(packet, row)
        if closed:
            return closed
        return self._send_it(row, packet) if action == "send" else self._drop(row, packet)

    def _bare(self, which: int, written: float) -> str:
        """A bare 「1」 / 「2」: the one open notification it can mean (told before it was written), else the list; with
        nothing open, what became of the last one."""
        rows = self._store.wechat_reviews_open()
        at = self._local(written) if written else self._clock()
        seen = [r for r in rows if float(r["notified_at"]) <= at + BARE_MARGIN_SEC]
        if len(rows) == 1 and len(seen) == 1:
            row = rows[0]
            packet = self._store.packet(int(row["packet_id"]))
            return self._send_it(row, packet) if which == 1 else self._drop(row, packet)
        if not rows:
            latest = self._store.latest_wechat_review()
            packet = self._store.packet(int(latest["packet_id"])) if latest else None
            return (self._closed(packet, latest) if packet is not None else "") or "现在没有等你回的草稿"
        names = "、".join(f"#{r['packet_id']} {(self._store.party(r['party_id']) or {}).get('name') or '?'}"
                         for r in rows[:LISTED]) + ("…" if len(rows) > LISTED else "")
        return (f"现在有 {len(rows)} 份在等你回：{names}；回「发 {rows[0]['packet_id']}」这样带编号，"
                "或者引用那条通知回 1")

    def _send_it(self, row: dict, packet: dict) -> str:
        """「1」: as drafted, or as the admin proposed (``proposed``)."""
        if packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL:
            return self._platform_text(packet)
        if row.get("state") == PROPOSED and row.get("proposed"):
            return self._decided(row, packet, "edit", row["proposed"])
        return self._decided(row, packet, "send", None)

    def _drop(self, row: dict, packet: dict) -> str:
        return self._decided(row, packet, "drop", None)

    def _edit(self, row: dict, packet: dict, text: str) -> str:
        if packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL:
            return self._platform_text(packet)
        return self._decided(row, packet, "edit", text)

    @staticmethod
    def _platform_text(packet: dict) -> str:
        return f"#{packet['id']} 带平台动作：微信里不能执行，请在控制台点「批准并执行」；不要的话回 2"

    def _propose(self, row: dict, packet: dict, text: str) -> str:
        """Other words quoting a notification: not sent; kept as its proposed edit, after the console's checks."""
        number = packet["id"]
        if packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL:
            return self._platform_text(packet)
        problem = self._check(packet, text) if self._check is not None else ""
        if problem:
            return f"#{number} 不能这样改：{problem}"
        now = self._clock()
        if not self._store.set_wechat_review(number, (NOTIFIED, PROPOSED), now, state=PROPOSED, proposed=text,
                                             proposed_at=now):
            fresh = self._store.packet(number) or packet
            return self._closed(fresh, self._store.wechat_review(number) or row) or f"#{number} 已经处理过了"
        logger.info("PRM: draft %d: the admin's words in WeChat kept as its proposed edit (%d chars)", number, len(text))
        group = (packet.get("meta") or {}).get("channel") == GROUP_CHANNEL
        return f"#{number} 改成：『{text}』\n回 1 {'发到群里' if group else '发出'}／2 不发"

    def _decided(self, row: dict, packet: dict, action: str, text: Optional[str]) -> str:
        """The admin's decision through ``PrmService.decide`` (the console's own): the reply that says what came of it."""
        number = packet["id"]
        if self._decide is None:
            return f"#{number} 现在不能在微信里处理：请在控制台处理"
        try:
            result = self._decide(number, action, text)
        except Exception:
            logger.exception("PRM: the admin's answer to draft %d could not be carried out", number)
            result = {"ok": False, "error": "出错了（详见日志）"}
        if not result.get("ok"):
            fresh = self._store.packet(number) or packet
            closed = self._closed(fresh, row)
            if closed:
                return closed
            return f"#{number} {'没改成不发' if action == 'drop' else '没发'}：{result.get('error') or '出错了'}"
        now = self._clock()
        self._store.set_wechat_review(number, (), now, state=ANSWERED, decision=action, answered_at=now)
        logger.info("PRM: draft %d decided by the admin in WeChat (%s)", number, action)
        if action == "drop":
            return f"#{number} 不发了（她再发消息时会有新的草稿）"
        meta = packet.get("meta") or {}
        name = (self._store.party(packet["party_id"]) or {}).get("name") or "她"
        where = f"（群里「{meta.get('group_name') or '老板拉的群'}」）" if meta.get("channel") == GROUP_CHANNEL else ""
        return f"#{number} {'按你改的批准了' if action == 'edit' else '已批准'}，正在发给 {name}{where}"

    def _closed(self, packet: Optional[dict], row: Optional[dict]) -> Optional[str]:
        """Why a notification is no longer open (None: its draft still waits)."""
        if packet is None:
            return None
        number, status = packet["id"], packet["status"]
        if status == PENDING:
            return None
        if status == SUPERSEDED:
            return self._stale(packet)
        what = {SENT: "已经发出去了", APPROVED: "已经批准，正在发", SENDING: "已经批准，正在发", DROPPED: "点了不发",
                FAILED: "没发出去", EXECUTING: "已经批准，正在平台上执行", NO_REPLY: "不用回复"}.get(status, status)
        if status == FAILED and packet.get("error"):
            what += f"（{_cut(packet['error'], 60)}）"
        by = "你在微信里回过了，" if (row or {}).get("state") == ANSWERED else ""
        return f"#{number} 已经处理过了：{by}{what}"

    def _stale(self, packet: dict) -> str:
        """A draft out of date (``superseded``): what replaced it (the newest later draft of the chat that is not out of
        date itself: waiting, or handled already), or that a newer one is being written, or that a person has the chat."""
        number = packet["id"]
        newer = [p for p in self._store.deal_packets(packet["deal_id"])
                 if p["id"] > number and p.get("tool") != BUYIN_TOOL and p["chat_id"] == packet["chat_id"]
                 and p["status"] != SUPERSEDED]
        deal = self._store.deal(packet["deal_id"]) or {}
        wrote = (packet.get("job_id") is not None
                 and float(deal.get("last_message_at") or 0) >= float(packet.get("created_at") or 0) - 60)
        said = "她又发了新消息，" if wrote else ""
        waiting = [p["id"] for p in newer if p["status"] == PENDING]
        if waiting:
            return f"#{number} 已过时：{said}新的草稿是 #{max(waiting)}"
        if packet.get("job_id") is not None and self._drafting is not None and self._drafting(packet["chat_id"]):
            return f"#{number} 已过时：她又发了新消息，新的草稿还在写，写好了会发到这里"
        if newer:
            return f"#{number} 已过时：{said}新的草稿 #{max(p['id'] for p in newer)} 已经处理过了"
        if deal and deals.effective_mode(deal, self._clock()) != COPILOT:
            return f"#{number} 已过时：现在是人工接管（有人亲手回过她），这份不发了"
        return f"#{number} 已过时，没有发出去"
