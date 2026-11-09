"""微信里审批草稿 (PRM_WECHAT_REVIEW, 2026-11-09, default off): the drafts that wait for a person in 「待审批」 are also
sent, once each, to the admin's private chat (ADMIN_WXID), and the admin decides them there, from the phone.

The user (2026-11-09): often away from the Windows PC, they ended up copying every draft out of the console into WeChat
by hand.  Now:

Notifications (``sweep``, on the PRM sender thread): every draft waiting for a person (a ``pending`` packet: a creator's
private chat, or a 老板拉的群 the bot took over) goes to the admin as one message (``notification_text``): its number
first (the packet id, the console's 「DRAFT #12」), the creator and where it goes (私聊 / 群里「群名」, with the console's
group warning; a draft the bot started itself is aimed first, as ``send_approved`` aims it, and waits untold while she
can be reached nowhere), her last line(s) it answers (short; numbers as their placeholders, addresses cut to province
and city, a labelled 微信号 masked), the draft in full with its placeholders (their masked numbers after it, as the
console's legend), a shipping notice's waybill, its files, and how to answer.  A draft with a platform action is
told too, with its action, but never run from WeChat: the notification sends the admin to the console's 「批准并执行」,
and WeChat can only drop it.  None for what goes out by itself (a kind's gate ``auto``, 下班时段 and
自动模式 approve it at once: never pending; 开场白自动发: ``meta.auto_at`` while ``send_due_openings`` still takes it), a
百应 draft (``BUYIN_TOOL``: copied into 百应 by hand), a holding reply (no packet), anything while ADMIN_WXID is empty
(``status`` says why), nor a draft that was waiting already before the switch was first on (``ON_AT`` in prm_meta, less
BACKLOG_SEC: the console counts those; a run with the switch off forgets ON_AT, the next switch-on starts afresh).  Each
draft's row is written before its send (``telling``) and marked told
after it: a send cut off between the two is never sent again, the console says it may have gone out.  PER_ROUND sends a
round at most; a send WeChat does not take ends the round and the admin's chat is tried again 10 s later, doubling, at
most 2 minutes apart, each message 6 times in all (as the relay's approval requests, src/relay/service.py
``_send_asks``); no draft ever waits for its notification.

Answers (``answer``, the route step ``wechat_review``, before the relay or an admin command can take them): typed text
in the admin's own private chat, never a voice message (heard wrong, a 「不发」 could send: the bot says to type).
- Quoting a notification (WeChat 引用; ``quoted_content`` starts with 「#12 草稿」): 「1」「发」「发送」「好」「好的」「可以」「行」
  「ok」「同意」「嗯」「[强]」「👍」 send the draft as it was drafted (a proposed edit is dropped), 「2」「不发」「不要」「算了」 drop
  it, 「改 <你的话>」 sends those words instead (the console's 改了再发 checks: ``PrmService.edit_problem``); other words
  are kept as the proposed edit and the bot asks 「#12 改成：『…』 回 1 发出／2 不发」.  Never a proposal: a line for the
  relay (``cc …``) or an admin command (it goes on to its step, untouched), another approval word, emoji only, a lone
  number (the bot says how to answer).
- Quoting that question: a yes word sends the proposal, only when the 『…』 quoted is the current one (else the bot shows
  the current one, as a question of its own).
- Without a quote: 「发 12」 (the original draft), 「不发 12」 for a draft told here, 「改 12 <你的话>」 for one still waiting;
  anything else passes on.  A bare 「1」 / 「2」 answers only the last thing the bot said in this chat (``note_sent``: the
  backend tells it every text it sent) when that was a notification or a 改成 question told before the answer was
  written, never in CLAUDE_RELAY_ONLY (it may be Claude's), never while a relay job of this chat runs or ended within
  RELAY_QUIET_SEC, never right after another told draft was closed (CLOSED_RACE_SEC).
- A draft for a 老板拉的群 is sent from WeChat only by an answer quoting its notification or question (the console's
  group confirm dialog); 「不发」 needs no quote.
- A draft with a platform action is never sent nor edited from WeChat: 1 / 好 / 发 / 改 say to use the console's
  「批准并执行」; 「2」 drops it.
- Several lines at once (WeChat merges bubbles sent within 2.2 s, src/wechat/burst.py): each answer line is done, an edit
  in a merged burst only as a proposal; the lines that are no answer (a cc task, an admin command, anything else) go on to
  the later route steps as one message (``rest``); two or more cc / command lines among them: the whole message is
  refused (one line asks to send them separately).
Each answer gets one reply (approved and where it goes, already handled, out of date and what replaced it, the console's
own refusal); a draft approved here that then fails to send is told in one line; a send the bot was stopped in the
middle of (``indeterminate``) is told as unsure UNSURE_GRACE_SEC after the start, unless its read-back settled it (then
nothing: it went out), never as 「没发出去」.  A decision here and one in the console are the same
decision: the packet's compare-and-set moves (``decide``) let only the first through, the second hears it was handled.

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
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..admin import HELP_COMMANDS
from ..relay.commands import ALLOW_WORDS, DENY_WORDS, prefix_pattern
from ..relay.service import MAX_AGE_SEC
from . import contact, deals, pii, platforms, retention
from .boss_groups import CHANNEL as GROUP_CHANNEL
from .store import (APPROVED, BUYIN_TOOL, COPILOT, DROPPED, EXECUTING, FAILED, MESSAGE_TOOL, NO_REPLY, PENDING, SENDING,
                    SENT, SUPERSEDED, PrmStore)

logger = logging.getLogger(__name__)

TELLING, NOTIFIED, PROPOSED, ANSWERED = "telling", "notified", "proposed", "answered"   # ``wechat_reviews.state``
SEND_WORDS = ("1", "发", "发送", "好", "好的", "可以", "行", "ok", "同意", "嗯", "[强]", "👍")   # quoting: send it
DROP_WORDS = ("2", "不发", "不要", "算了")                          # ... drop it
RETRY_SEC = (10, 120)       # a send the admin's chat did not take: the chat again after 10 s, doubling, at most 2 min apart
SEND_TRIES = 6              # ... and a message not again (this run) after this many failed sends (about 4.5 minutes)
PER_ROUND = 3               # sends one round of the sender thread makes at most (a pile waits its turn)
BACKLOG_SEC = 12 * 3600     # the switch first on: drafts that started waiting this long before it are told, older not
ON_AT = "wechat_review_on_at"   # prm_meta: when the switch was first seen on
RELAY_QUIET_SEC = 600       # a bare 「1」 / 「2」 never counts while a relay job of the admin chat runs or ended this recently
CLOSED_RACE_SEC = 60        # ... nor when another told draft stopped waiting this recently
UNSURE_GRACE_SEC = 180      # a send cut off by a stop is told as unsure this long after the start, unless it settles
SAID_LINES = 2              # her last lines a notification quotes
SAID_CHARS = 60             # ... each cut to this many characters
NOTE_CHARS = 120            # the draft's reason (when it asks for attention), an error told
FILES_NAMED = 5             # file names a notification lists at most
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
_EMOJI_TAG = re.compile(r"\[[^\[\]\s]{1,8}\]")
_EMOJI = re.compile("[\\s\U0001F000-\U0001FAFF☀-➿️‍]+")

NO_ADMIN_WHY = ("微信里审批草稿打开了，但没填管理员 wxid（ADMIN_WXID）：草稿发不到微信里。到「中转代理」页的设置里填好"
                "「管理员 wxid」，再重启机器人")
NO_SEND_WHY = "机器人没有连着微信：草稿发不到管理员微信里"
EDGE_WHY = "Mac 端（WECHAT_BACKEND=edge）不能在微信里审批草稿：管理员的私聊在 Windows 上读"
VOICE_TEXT = "回草稿要打字：引用那条通知回 1 发出、2 不发，或者回「发 12」这样带编号；语音不算（免得听错）"
REFUSE_TEXT = "这几句是一起到的，里面有好几条要交给 Mac 或者是管理命令：都没处理，请一句一句分开发"


def _cut(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _word(text: str) -> str:
    """A short answer as it is compared: full-width letters and digits as plain ones, trailing punctuation off, lower."""
    return _TRAILING.sub("", unicodedata.normalize("NFKC", str(text or "")).strip()).lower()


def _emoji_only(text: str) -> bool:
    text = str(text or "").strip()
    return bool(text) and not _EMOJI.sub("", _EMOJI_TAG.sub("", text))


def _not_words(text: str) -> bool:
    """Words that are never a proposed edit: an approval word of the relay's, emoji only, a lone number."""
    word = _word(text)
    return word in ALLOW_WORDS or word in DENY_WORDS or _emoji_only(text) or word.isdigit()


def _asked(quoted: str) -> Optional[str]:
    """The proposal a 「#12 改成：『…』」 question showed (None when the quote does not hold it whole)."""
    start, end = quoted.find("『"), quoted.rfind("』")
    return quoted[start + 1:end] if 0 <= start < end else None


def _same(a, b) -> bool:
    return " ".join(str(a or "").split()) == " ".join(str(b or "").split())


@dataclass(frozen=True)
class Said:
    """What a message of the admin says, before anything is looked up (``read``)."""

    kind: str                       # text | quote | voice
    content: str                    # the words typed (a voice message's: what was heard)
    number: Optional[int] = None    # quote: the draft the quoted notification / question is about
    head: str = ""                  # quote: 草稿 (a notification) or 改成 (the bot's question about a proposed edit)
    asked: Optional[str] = None     # quote of a question: the proposal it showed (None: not held whole)
    quoted: bool = False            # text quoting another message (a relay result, say): never a bare 1 / 2


def read(msg: dict) -> Optional[Said]:
    """What the admin's message says (pure): typed text (``msg_type`` 1), a quote (49 with ``quoted_content``), or a
    voice message's words; None for anything else.  A quote of another message than a notification or a question is
    ``text`` (only its numbered answers count)."""
    try:
        msg_type = int(msg.get("msg_type", 1) or 1)
    except (TypeError, ValueError):
        return None
    content = _QUOTE_PREFIX.sub("", str(msg.get("content", "") or "")).strip()
    if msg_type == 34:
        heard = _VOICE.match(content)
        said = heard.group(1).strip().rstrip("。.！!，,、 ") if heard else ""
        return Said("voice", said) if said else None
    quoted = _QUOTE_PREFIX.sub("", str(msg.get("quoted_content", "") or "")).strip()
    if not content or msg_type not in (1, 49) or (msg_type == 49 and not quoted):
        return None
    head = _HEAD.match(quoted) if quoted else None
    if head is None:
        return Said("text", content, quoted=bool(quoted))
    return Said("quote", content, int(head.group(1)), head.group(2), _asked(quoted) if head.group(2) == "改成" else None)


@dataclass
class Plan:
    """What a message asks: its answers (``items``, in order), the lines that are none (``rest``: they go on to the later
    route steps as one message), or one line refusing it all (``refuse``)."""

    items: list = field(default_factory=list)
    rest: list = field(default_factory=list)
    refuse: str = ""


@dataclass(frozen=True)
class _Last:
    """The last thing the bot said in the admin's chat (``note_sent``)."""

    at: float
    number: Optional[int]           # the draft of a notification / a 改成 question; None for anything else
    head: str = ""
    asked: Optional[str] = None


def relay_busy(relay_store, chat_id: str, now: float) -> bool:
    """Has a relay job of this chat (a cc task) not ended, or ended within RELAY_QUIET_SEC?"""
    for job in relay_store.recent(chat_id, limit=10):
        if job.get("status") in ("pending", "running"):
            return True
        if job.get("finished_at") and now - float(job["finished_at"]) < RELAY_QUIET_SEC:
            return True
    return False


class WechatReview:
    """微信里审批草稿 for one PRM service (``PrmService.wechat_review``); see the module's docstring.

    ``decide(packet_id, action, text)`` and ``check(packet, text)`` are the service's ``decide`` / ``edit_problem``;
    ``lines(packet)`` her lines a draft answers (the job's); ``parts(text)`` how many messages a text goes out as
    (PRM_REPLY_PARTS); ``aim(packet)`` where a draft the bot started itself goes (``PrmService._aim`` without failing it:
    None while she can be reached nowhere); ``relay_store`` the relay's jobs (a newer draft being written, a cc task
    running); ``local`` WeChat's time on this PC's clock; ``opening_auto``: PRM_OPENING_AUTO_MIN is on (an opening with
    its time set goes by itself)."""

    def __init__(self, store: PrmStore, config, *, clock: Callable[[], float] = time.time,
                 send: Optional[Callable[[str, str], bool]] = None,
                 decide: Optional[Callable[..., dict]] = None, check: Optional[Callable[[dict, str], str]] = None,
                 lines: Optional[Callable[[dict], str]] = None, parts: Optional[Callable[[str], int]] = None,
                 aim: Optional[Callable[[dict], Optional[dict]]] = None, relay_store=None,
                 local: Optional[Callable[[float], float]] = None, opening_auto: bool = False):
        self._store = store
        self._clock = clock
        self._send = send
        self._decide = decide
        self._check = check
        self._lines = lines
        self._parts = parts
        self._aim = aim
        self._relay_store = relay_store
        self._local = local or (lambda written: written)
        self._opening_auto = bool(opening_auto)
        self.admin = str(getattr(config, "admin_wxid", "") or "").strip()
        self._edge = str(getattr(config, "wechat_backend", "") or "") == "edge"
        self._switch = str(getattr(config, "prm_wechat_review", "off") or "off").strip().lower() == "on"
        self.on = self._switch and not self._edge     # (the route step and the sweep do nothing otherwise)
        self._relay_only = str(getattr(config, "claude_relay_only", "off") or "off").strip().lower() == "on"
        self._cc = prefix_pattern(str(getattr(config, "claude_relay_prefix", "cc") or "cc"))
        self._failures: dict[tuple, int] = {}       # ("draft" | "told", packet id) -> its sends WeChat did not take
        self._chat_failures = 0                       # sends to the admin's chat not taken in a row (RETRY_SEC)
        self._wait_until = 0.0                        # ... and when the chat is tried again
        self._since: Optional[float] = None           # drafts that started waiting before this stay the console's
        self._started = clock()
        self._last: Optional[_Last] = None            # the last thing the bot said in the admin's chat
        self._seen: deque = deque(maxlen=SEEN_IDS)
        self._lock = threading.Lock()                 # one answer at a time
        if not self._switch:
            self._forget_on_at()

    def _forget_on_at(self) -> None:
        """A run with the switch off: the next switch-on starts again from then (``ON_AT``), never with the drafts that
        waited meanwhile."""
        try:
            if self._store.meta_value(ON_AT):
                self._store.set_meta_value(ON_AT, "")
        except Exception:
            logger.exception("PRM: the 微信里审批草稿 switch-on time could not be cleared")

    # ── the console ───────────────────────────────────────────────────────

    def status(self) -> dict:
        """What 「待审批」 says (GET /api/prm ``wechat_review``): ``on`` (the switch), ``why`` it cannot work ("": it does),
        how many told drafts wait for the admin's answer (``open``), how many could not be told this run (``unsent``),
        how many may or may not have gone out (``unsure``: left ``telling``) and how many were waiting already before
        the switch was first on (``older``: the console's only)."""
        if not self._switch:
            return {"on": False}
        why = EDGE_WHY if self._edge else NO_ADMIN_WHY if not self.admin else NO_SEND_WHY if self._send is None else ""
        if why:
            return {"on": True, "why": why, "open": 0, "unsent": 0, "unsure": 0, "older": 0}
        unsent = sum(1 for (what, _), failures in list(self._failures.items())
                     if what == "draft" and failures >= SEND_TRIES)
        older = [p for p in self._store.wechat_review_backlog(self._cutoff()) if not self._goes_by_itself(p)]
        return {"on": True, "why": "", "open": len(self._store.wechat_reviews_open()), "unsent": unsent,
                "unsure": len(self._store.wechat_reviews_unsure()), "older": len(older)}

    def _cutoff(self) -> float:
        """Drafts that started waiting before this stay the console's: BACKLOG_SEC before the switch was first seen on
        (kept in prm_meta: a restart keeps it)."""
        if self._since is None:
            try:
                on_at = float(self._store.meta_value(ON_AT))
            except (TypeError, ValueError):
                on_at = self._clock()
                self._store.set_meta_value(ON_AT, on_at)
            self._since = on_at - BACKLOG_SEC
        return self._since

    # ── notifications (the sender thread) ─────────────────────────────────

    @property
    def ready(self) -> bool:
        return self.on and bool(self.admin) and self._send is not None

    def sweep(self) -> int:
        """One round of the sender thread: the drafts waiting for a person not told yet (oldest first), then what came
        of the decisions taken here (a send that failed, a platform action's end); PER_ROUND sends at most.  A send
        WeChat does not take ends the round, and the admin's chat waits before the next one (RETRY_SEC: 10 s,
        doubling, at most 2 minutes apart); a message not taken SEND_TRIES times is not tried again this run (logged;
        the console still has the draft).  Returns how many messages went to the admin."""
        if not self.ready or self._clock() < self._wait_until:
            return 0
        sent = tried = 0
        for packet in self._store.wechat_review_due(self._cutoff()):
            key = ("draft", packet["id"])
            if self._goes_by_itself(packet) or self._failures.get(key, 0) >= SEND_TRIES:
                continue
            if tried >= PER_ROUND:
                return sent
            if self._aim is not None:
                packet = self._aim(packet)
                if packet is None:
                    continue                               # (she can be reached nowhere yet: told once she can)
            try:
                text = self.notification_text(packet)
            except Exception:
                logger.exception("PRM: the notification of draft %d could not be written; not told", packet["id"])
                self._failures[key] = SEND_TRIES
                continue
            current = self._store.packet(packet["id"])
            if current is None or current["status"] != PENDING:
                continue                                   # decided (or out of date) meanwhile
            if not self._store.add_wechat_review(packet["id"], packet["deal_id"], self._clock(), state=TELLING):
                continue                                   # (told already)
            tried += 1
            if not self._try(key, text, f"the notification of draft {packet['id']}"):
                self._store.drop_wechat_review(packet["id"], (TELLING,))     # (not sent: told again later)
                return sent
            now = self._clock()
            self._store.set_wechat_review(packet["id"], (TELLING,), now, state=NOTIFIED, notified_at=now)
            logger.info("PRM: draft %d told to the admin's chat (微信里审批)", packet["id"])
            sent += 1
        for row in self._store.wechat_reviews_to_tell():
            key = ("told", row["packet_id"])
            if self._failures.get(key, 0) >= SEND_TRIES:
                continue
            text = self._outcome_text(row)
            if text is None:
                continue                                   # (an unsure send: it may still be read back)
            if tried >= PER_ROUND:
                return sent
            tried += 1
            if not self._try(key, text, f"what came of draft {row['packet_id']}"):
                return sent
            self._store.set_wechat_review(row["packet_id"], (ANSWERED,), self._clock(), told_at=self._clock())
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

    def _outcome_text(self, row: dict) -> Optional[str]:
        """What came of a decision taken here, in one line (None: not to be told yet): a send that failed.  A send the bot
        was stopped in the middle of (``indeterminate``) waits for its read-back, or UNSURE_GRACE_SEC after the start."""
        number = row["packet_id"]
        name = (self._store.party(row["party_id"]) or {}).get("name") or "她"
        status, outcome, error = row.get("status"), str(row.get("outcome") or ""), str(row.get("error") or "")
        if status == FAILED and outcome == "indeterminate":
            if self._clock() - self._started < UNSURE_GRACE_SEC:
                return None
            if "文字已经发出去了" in error:
                return (f"#{number} 文字发出去了，文件不确定发出去没有（发的时候机器人停了），请看一眼微信再决定要不要手动"
                        f"补发给 {name}")
            return f"#{number} 不确定发出去没有（发的时候机器人停了），请看一眼微信再决定要不要手动发给 {name}"
        return f"#{number} 没发出去：{_cut(error or '发送出错', NOTE_CHARS)}；需要的话请在微信里手动发给 {name}"

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
            lines.append(f"（发在群里：要引用这条回；不发也可以回「不发 {number}」）" if group
                         else f"（引用这条回；不引用就回「发 {number}」「不发 {number}」「改 {number} 你的话」）")
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

    # ── what the bot says in the admin's chat (the backend's ``set_sent_hook``) ──

    def note_sent(self, chat_id: str, text: str) -> None:
        """A text the bot sent (the backend's hook: every one of them, a route reply's too).  In the admin's chat it is
        the last thing the bot said there: a bare 「1」 / 「2」 answers it only when it is a notification or a 改成
        question."""
        if not self.admin or str(chat_id or "") != self.admin:
            return
        text = str(text or "")
        head = _HEAD.match(text)
        self._last = _Last(self._clock(), int(head.group(1)) if head else None, head.group(2) if head else "",
                           _asked(text) if head and head.group(2) == "改成" else None)

    def tell(self, text: str) -> None:
        """Say ``text`` in the admin's chat now (an answer's reply when the rest of its message goes on to the later
        route steps, one of several replies)."""
        if not text or self._send is None:
            return
        try:
            if not self._send(self.admin, text):
                logger.warning("PRM: a reply to the admin's answer was not sent")
        except Exception:
            logger.exception("PRM: a reply to the admin's answer could not be sent")

    # ── the admin's answers (the route step ``wechat_review``) ───────────

    def is_admin_line(self, msg: dict) -> bool:
        """The admin's own message in the admin's one-to-one chat (never a group, nobody else)."""
        return (self.on and bool(self.admin) and not msg.get("is_group", True)
                and str(msg.get("sender_id") or "") == self.admin and str(msg.get("chat_id") or "") == self.admin)

    def claims(self, msg: dict, *, is_command=None) -> bool:
        """Would ``answer`` take this message?  The route step's fallback when ``answer`` fails: such a message still
        never goes on to the relay."""
        if not self.is_admin_line(msg):
            return False
        said = read(msg)
        plan = self._plan(said, is_command, self._burst(msg)) if said is not None else None
        return plan is not None and (not self._bare_only(plan) or self._bare_claims())

    def answer(self, msg: dict, *, is_command=None) -> tuple[bool, Optional[str], Optional[str]]:
        """``(matched, reply, rest)`` for the route step: matched = an answer to the drafts told here (the route ends with
        ``reply``, None: silently; with ``rest``, those lines of it go on to the later steps instead, the reply said
        first).  Anything else is not matched and goes on as it is.  ``is_command``: the admin handler's
        (``AdminCommandHandler.is_command``): such a line is never an answer."""
        if not self.is_admin_line(msg):
            return False, None, None
        said = read(msg)
        if said is None:
            return False, None, None
        with self._lock:
            return self._answer(msg, said, is_command)

    @staticmethod
    def _burst(msg: dict) -> bool:
        """Several bubbles WeChat sent within the burst window, merged into one message (src/wechat/burst.py)."""
        try:
            return int(msg.get("_burst_count") or 1) > 1
        except (TypeError, ValueError):
            return False

    def _passes(self, line: str, is_command) -> bool:
        """A line for a later step, never an answer: a relay task (``cc …``, any case, any spacing) or an admin
        command (the help words too)."""
        line = str(line or "").strip()
        if self._cc.match(line) or line in HELP_COMMANDS:
            return True
        try:
            return bool(is_command(line)) if is_command is not None else False
        except Exception:
            logger.exception("PRM: an admin command could not be recognised")
            return False

    def _told(self, number: int) -> bool:
        return self._store.wechat_review(number) is not None

    def _open(self, number: int) -> bool:
        packet = self._store.packet(number)
        return packet is not None and packet["status"] == PENDING and self._told(number)

    def _line(self, line: str, said: Said) -> Optional[tuple]:
        """One line's answer: 「发 N」「不发 N」 for a draft told here, 「改 N …」「改 N」 for one still waiting
        (``via_quote``: the message quotes that draft's notification or question); None otherwise."""
        for pattern, action in ((_SEND_N, "send"), (_DROP_N, "drop")):
            m = pattern.match(line)
            if m:
                number = int(m.group(1))
                return (action, number, said.number == number) if self._told(number) else None
        m = _EDIT_N.match(line)
        if m:
            number = int(m.group(1))
            return ("edit", number, m.group(2).strip(), said.number == number) if self._open(number) else None
        m = _EDIT_N_ONLY.match(line)
        if m:
            number = int(m.group(1))
            return ("ask", number) if self._open(number) else None
        return None

    def _plan(self, said: Said, is_command, burst: bool) -> Optional[Plan]:
        """What the admin's message asks (None: not an answer, it goes on as it is)."""
        raw = said.content.split("\n")
        rows = [line.strip() for line in raw if line.strip()]
        if not rows or (not burst and self._passes(rows[0], is_command)):
            return None                                # (a cc task, a command, with all its lines: to its step)
        if said.kind == "quote":
            # a line for the relay or a command among the words: never in what goes to her, it goes on by itself
            passing = [n for n, line in enumerate(raw) if line.strip() and self._passes(line, is_command)]
            if len(passing) > 1:
                return Plan(refuse=REFUSE_TEXT)
            rest = [raw[n].strip() for n in passing]
            content = "\n".join(line for n, line in enumerate(raw) if n not in passing).strip()
            number = said.number
            m = _EDIT_N.match(content)
            if m:
                n = int(m.group(1))
                item = ("edit", number, m.group(2).strip(), True) if n == number else ("mismatch", number, n)
                return Plan([item], rest)
            m = _EDIT_N_ONLY.match(content)
            if m:
                n = int(m.group(1))
                return Plan([("ask", number) if n == number else ("mismatch", number, n)], rest)
            for pattern, action in ((_SEND_N, "send"), (_DROP_N, "drop")):
                m = pattern.match(content)
                if m:
                    n = int(m.group(1))
                    return Plan([(action, n, n == number)], rest)
            return Plan([("quote", number, said.head, content, said.asked)], rest)
        if len(rows) == 1 and _word(rows[0]) in ("1", "2"):
            return None if said.quoted else Plan([("bare", int(_word(rows[0])))])   # (quoting another message: not ours)
        items, rest, i = [], [], 0
        while i < len(raw):
            line = raw[i].strip()
            i += 1
            if not line:
                continue
            item = None if self._passes(line, is_command) else self._line(line, said)
            if item is None:
                rest.append(line)
                continue
            if item[0] == "edit":                      # the lines after it that are no answer: its words go on there
                more = []
                while i < len(raw) and (not raw[i].strip() or (not self._passes(raw[i], is_command)
                                                                and self._line(raw[i].strip(), said) is None)):
                    more.append(raw[i].rstrip())
                    i += 1
                if more:
                    item = ("edit", item[1], "\n".join([item[2], *more]).strip(), item[3])
            items.append(item)
        if not items:
            return None
        if sum(1 for line in rest if self._passes(line, is_command)) > 1:
            return Plan(refuse=REFUSE_TEXT)
        return Plan(items, rest)

    @staticmethod
    def _bare_only(plan: Plan) -> bool:
        return bool(plan.items) and plan.items[0][0] == "bare"

    def _bare_claims(self) -> bool:
        """Is a bare 「1」 / 「2」 this feature's at all: never in CLAUDE_RELAY_ONLY (it may be Claude's), only when the last
        thing the bot said here was a notification or a 改成 question."""
        return not self._relay_only and self._last is not None and self._last.number is not None

    def _answer(self, msg: dict, said: Said, is_command) -> tuple[bool, Optional[str], Optional[str]]:
        burst = self._burst(msg)
        if said.kind == "voice":
            heard = self._plan(Said("text", said.content), is_command, False)
            if heard is None or (self._bare_only(heard) and not self._bare_claims()):
                return False, None, None
        else:
            plan = self._plan(said, is_command, burst)
            if plan is None or (self._bare_only(plan) and not self._bare_claims()):
                return False, None, None
        msg_id = str(msg.get("message_id") or "")
        if msg_id and msg_id in self._seen:
            return True, None, None                    # (handed on twice: answered once)
        if msg_id:
            self._seen.append(msg_id)
        now = self._clock()
        written = float(msg.get("timestamp") or 0)
        if written and now - self._local(written) > MAX_AGE_SEC:
            logger.info("PRM: an answer to a draft notification read %.0f s after it was written; history, not acted on",
                        now - self._local(written))
            return True, None, None
        if said.kind == "voice":
            return True, VOICE_TEXT, None
        if plan.refuse:
            return True, plan.refuse, None
        if self._bare_only(plan):
            return True, self._bare(plan.items[0][1], written), None
        replies = [self._run(item, burst) for item in plan.items]
        replies = [r for r in replies if r]
        replies.sort(key=lambda r: bool(_HEAD.match(r)))     # (a 改成 question last: it is what a bare 1 answers)
        for reply in replies[:-1]:
            self.tell(reply)
        return True, replies[-1] if replies else None, "\n".join(plan.rest) if plan.rest else None

    def _run(self, item: tuple, burst: bool) -> str:
        """One answer of a message: its reply."""
        action = item[0]
        if action == "quote":
            return self._on_quote(*item[1:])
        if action == "mismatch":
            quoted, number = item[1], item[2]
            return (f"你引用的是 #{quoted}，写的却是「改 {number}」：没有改。要改哪一份？引用那一份的通知回「改 你的话」，"
                    "或者回「改 编号 你的话」")
        if action == "ask":
            return f"#{item[1]} 要改成什么？回「改 {item[1]} 你的话」"
        row, packet = self._row(item[1])
        if row is None:
            return self._not_told(item[1], packet)
        closed = self._closed(packet, row)
        if closed:
            return closed
        if action == "drop":
            return self._decided(row, packet, "drop", None)
        if action == "send":
            return self._send_original(row, packet, quoted=item[2])
        return self._edit(row, packet, item[2], quoted=item[3], propose=burst)

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

    def _on_quote(self, number: int, head: str, text: str, asked: Optional[str]) -> str:
        """An answer quoting the notification of ``number`` (``head`` 草稿), or the bot's question about its proposed edit
        (改成, ``asked``: the proposal that question showed)."""
        row, packet = self._row(number)
        if row is None:
            return self._not_told(number, packet)
        closed = self._closed(packet, row)
        if closed:
            return closed
        word = _word(text)
        if word in SEND_WORDS:
            if head == "改成" and not self._platform(packet):
                return self._send_asked(row, packet, asked)
            return self._send_original(row, packet, quoted=True)
        if word in DROP_WORDS:
            return self._decided(row, packet, "drop", None)
        edit = _EDIT.match(text.strip())
        if edit:
            return self._edit(row, packet, edit.group(1).strip(), quoted=True)
        if word in ("改", "改成"):
            return f"#{number} 要改成什么？引用通知回「改 你的话」，或者回「改 {number} 你的话」"
        if _not_words(text):
            return self._help(packet)
        return self._propose(row, packet, text.strip())

    def _bare(self, which: int, written: float) -> str:
        """A bare 「1」 / 「2」 (``_bare_claims`` holds): the draft of the last thing the bot said here, unless that was said
        after the answer was written, a relay job of this chat is under way or just ended, or another told draft just
        stopped waiting."""
        last = self._last
        number = last.number
        if last.at > (self._local(written) if written else self._clock()):
            return f"#{number} 是刚发来的，你回的时候还没看到它：请引用它回，或者回「发 {number}」「不发 {number}」"
        if self._relay_store is not None and relay_busy(self._relay_store, self.admin, self._clock()):
            return (f"Claude 那边有任务在做或者刚做完，单独一个 1 / 2 不算：#{number} 请引用那条通知回，或者回「发 {number}」"
                    f"「不发 {number}」")
        if self._store.wechat_reviews_closed_since(self._clock() - CLOSED_RACE_SEC, number):
            return f"刚有一份处理掉了，请带编号回（发 {number}）"
        row, packet = self._row(number)
        if row is None:
            return self._not_told(number, packet)
        closed = self._closed(packet, row)
        if closed:
            return closed
        if which == 2:
            return self._decided(row, packet, "drop", None)
        if self._group(packet):
            return self._group_hint(packet)
        if self._platform(packet):
            return self._platform_hint(packet)
        if last.head == "改成":
            return self._send_asked(row, packet, last.asked)
        return self._decided(row, packet, "send", None)

    @staticmethod
    def _platform(packet: dict) -> bool:
        return packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL

    @staticmethod
    def _group(packet: dict) -> bool:
        return (packet.get("meta") or {}).get("channel") == GROUP_CHANNEL

    @staticmethod
    def _group_hint(packet: dict) -> str:
        name = (packet.get("meta") or {}).get("group_name") or "老板拉的群"
        return f"#{packet['id']} 是发在群「{name}」里的，请引用那条通知回 1 确认"

    @staticmethod
    def _platform_hint(packet: dict) -> str:
        return f"#{packet['id']} 带平台动作：微信里不能执行，请在控制台点「批准并执行」；不要的话回 2"

    @staticmethod
    def _platform_edit_refused(packet: dict) -> str:
        return f"#{packet['id']} 带平台动作：微信里不能执行，请在控制台点「批准并执行」；不要的话回 2"

    def _help(self, packet: dict) -> str:
        number = packet["id"]
        if self._platform(packet):
            return self._platform_hint(packet)
        return f"#{number}：这句没当成回复，也没记成改稿。回 1 发出原稿、2 不发、「改 你的话」改了再发"

    def _send_original(self, row: dict, packet: dict, *, quoted: bool) -> str:
        """The draft as it was drafted (a proposed edit dropped): not for a platform action (the console's), and for a
        group only by an answer quoting it."""
        if self._platform(packet):
            return self._platform_hint(packet)
        if self._group(packet) and not quoted:
            return self._group_hint(packet)
        return self._decided(row, packet, "send", None)

    def _send_asked(self, row: dict, packet: dict, asked: Optional[str]) -> str:
        """「1」 to a 改成 question: its proposal, only when it is still the current one; else the current one is asked
        about again (a question of its own: what a bare 1 answers)."""
        current = str(row.get("proposed") or "").strip()
        if row.get("state") != PROPOSED or not current:
            return f"#{packet['id']} 现在没有改稿：要改就回「改 {packet['id']} 你的话」"
        if asked is not None and _same(asked, current):
            return self._decided(row, packet, "edit", current)
        note = "（引用里看不全那一份改稿，没发）" if asked is None else "（改稿换过了：你引用的不是现在这一份，没发）"
        return self._question(packet, current, note)

    def _edit(self, row: dict, packet: dict, text: str, *, quoted: bool, propose: bool = False) -> str:
        """「改 …」: sent instead of the draft (``propose``: a merged burst's, only proposed: never straight out)."""
        if self._platform(packet):
            return self._platform_edit_refused(packet)
        if self._group(packet) and not quoted:
            return self._group_hint(packet)
        if _word(text).isdigit():
            return f"#{packet['id']}：改的内容只有一个数字，没有改。要改请回「改 {packet['id']} 你的话」"
        if propose:
            return self._propose(row, packet, text)
        return self._decided(row, packet, "edit", text)

    def _question(self, packet: dict, text: str, note: str = "") -> str:
        """The bot's question about a proposed edit (a group's: answered only by a quote of it)."""
        if self._group(packet):
            return f"#{packet['id']} 改成：『{text}』\n{note}引用这条回 1 发到群里／2 不发"
        return f"#{packet['id']} 改成：『{text}』\n{note}回 1 发出／2 不发"

    def _propose(self, row: dict, packet: dict, text: str) -> str:
        """Other words for a draft: not sent; kept as its proposed edit, after the console's checks."""
        number = packet["id"]
        if self._platform(packet):
            return self._platform_edit_refused(packet)
        if _not_words(text):
            return self._help(packet)
        problem = self._check(packet, text) if self._check is not None else ""
        if problem:
            return f"#{number} 不能这样改：{problem}"
        now = self._clock()
        if not self._store.set_wechat_review(number, (TELLING, NOTIFIED, PROPOSED), now, state=PROPOSED, proposed=text,
                                             proposed_at=now):
            fresh = self._store.packet(number) or packet
            return self._closed(fresh, self._store.wechat_review(number) or row) or f"#{number} 已经处理过了"
        logger.info("PRM: draft %d: the admin's words in WeChat kept as its proposed edit (%d chars)", number, len(text))
        return self._question(packet, text)

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
            what = "没改成不发" if action == "drop" else "没发"
            return f"#{number} {what}：{result.get('error') or '出错了'}"
        now = self._clock()
        self._store.set_wechat_review(number, (), now, state=ANSWERED, decision=action, answered_at=now,
                                      **({"proposed": ""} if action == "send" else {}))
        logger.info("PRM: draft %d decided by the admin in WeChat (%s)", number, action)
        if action == "drop":
            return f"#{number} 不发了（她再发消息时会有新的草稿）"
        meta = packet.get("meta") or {}
        name = (self._store.party(packet["party_id"]) or {}).get("name") or "她"
        where = f"（群里「{meta.get('group_name') or '老板拉的群'}」）" if self._group(packet) else ""
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
        if packet.get("job_id") is not None and self._drafting(packet["chat_id"]):
            return f"#{number} 已过时：她又发了新消息，新的草稿还在写，写好了会发到这里"
        if newer:
            return f"#{number} 已过时：{said}新的草稿 #{max(p['id'] for p in newer)} 已经处理过了"
        if deal and deals.effective_mode(deal, self._clock()) != COPILOT:
            return f"#{number} 已过时：现在是人工接管（有人亲手回过她），这份不发了"
        return f"#{number} 已过时，没有发出去"

    def _drafting(self, chat_id: str) -> bool:
        """Is a newer reply draft of this chat being written on the Mac?"""
        if self._relay_store is None:
            return False
        return self._relay_store.latest_open(chat_id, drafts_only=True, system=False) is not None
