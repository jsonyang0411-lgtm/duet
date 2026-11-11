"""合作单指令 (PRM_WECHAT_COMMANDS, 2026-11-11, default off): the console's deal buttons, typed in the admin's WeChat chat.

The operator (2026-11-11) asked whether 「做手卡」 could be ordered from WeChat instead of clicked in the console, and
said 「做」 to this: with the switch on, a typed line 「<指令> <达人>」 in the admin's one-to-one chat with this account
(ADMIN_WXID; src/admin.py hands the text over, ``admin_command``) runs the same service method as the console's button,
with the same checks, gates and refusals (``PrmService.deal_action``: nothing of their logic is copied here), and is
answered in one short line, 「<她>：<what happened>」 or the refusal the action gave:

- 「做手卡 <达人>」: the deal action ``handcards`` with no products ticked (her latest sample order's, as the console's
  「做手卡」 when nothing is ticked): one draft job; its draft always waits for a person in 「待审批」 (``_ask_handcards``:
  no gate, 下班时段 or 自动模式 sends cards);
- 「补发货盘 <达人>」: ``huopan``, one draft job; its draft always waits for a person;
- 「写开场白 <达人>」: ``opening``, one draft job; its draft waits in 「待审批」 and goes as a 开场白 the console asked for
  goes (PRM_OPENING_AUTO_MIN: by itself when nobody decides on it, the answer says so);
- 「签收 <达人>」: what the console's 「更新物流 → 已签收」 sends (``shipment_status`` delivered, her one waybill), or
  without a waybill its 「改阶段 → 已签收」 (``stage`` delivered); what a signature starts (the 专属佣金 chain) starts as
  from the console, its drafts waiting for 批准并执行.  Two waybills or more, a deal past 已签收 (the stage would go
  back), or one that is 已签收 already: nothing is done, the answer points to the console;
- 「备注 <达人> <文字>」 / 「备注 <达人>：<文字>」: ``draft_note``, the whole 起草备注 replaced as the console's save does
  (longer than DRAFT_NOTE_CHARS: the console's refusal); 「备注 <达人> 清空」 clears it; 「备注 <达人>」 shows it;
- 「接管 <达人>」 / 「副驾驶 <达人>」 (or 「恢复副驾驶 <达人>」): ``mode`` human (PRM_TAKEOVER_MIN, the console's 人工接管) /
  copilot (its 恢复副驾驶; already copilot: nothing done);
- 「合作单 <达人>」: read only: the stage and the mode, the waybill and her latest sample order, where her drafts go, what
  of hers waits in 「待审批」 or is being written, whether a 起草备注 is set;
- 「指令」: these commands (the admin's 「帮助」 names them in one line, ``HELP_LINE``).

Nothing here sends anything to a creator by itself: each command only makes what the console's button makes (a draft
job, a note, a stage or a mode change), and a draft it brings waits or goes by the same rules as one clicked in the
console.  The deal's record names the admin as who did it (``ACTOR``: 「管理员（微信里）」 in the console, as 「接群」's).

Finding her (``_find``): her 称呼 as the console lists her (often the friend add's remark 「抖音-<昵称>」), with or without
the platform's prefix, spaces, separators (「-」「_」「·」「:」), full-width letters and case aside, whole or a piece of
it (a whole one first); only a creator on the list whose deal was not cleaned up (8.2).  One: done.  Several: numbered
(LISTED at most: her 称呼, the stage, the deal's number) and 「选 N」 runs the command once for the N-th, CHOICE_SEC long;
「选 N」 is a command only while such a question is open or was lately (ASKED_KEEP_SEC: then it says the question
expired, or was answered), so the words stay free otherwise (as group_link's 「是 N」).  None: one line, with the
CLOSE_LISTED closest names; the line is not handed on to the Mac's Claude (with CLAUDE_RELAY_ONLY the answer says to put
cc in front for Claude).

What is a command (``parse``): one line, 「<指令><空白><名字…>」 (a name of at most NAME_MAX characters), ending in
nothing or at most one 「。」 with any 「！!～~」 (wechat_review's ``_command_word``).  「做手卡怎么用」 (no blank after the
word), 「签收了吗」, 「合作单 小美呢」, 「做手卡 小美？」 (a question: ？ / 吗 / 呢 / 么 / 嘛 / 没 at its end, or 怎么 / 什么 /
为什么 / 如何 / 是不是 / 能不能 / 要不要 / 可不可以 in it), 「签收 小美…」 (hesitating) are none and go where they went
before.  A message whose first line is a command and that has more lines is refused whole (one command per message).
Only typed text in the admin's own one-to-one chat (never a group, a creator, anybody else; nothing while ADMIN_WXID is a
group's or a registered creator's: wechat_review's ``admin_problem``): a voice message or a quote with a command's words
gets one line saying to type it, and nothing is done.  The switch off: nothing of it (src/bot.py does not hand it to the
admin handler; every line goes where it went before).

Replies name her as 微信里审批草稿 does (her platform ids by their last characters, ``WechatReview._name_of``); waybills
and order numbers by their last 4.  The answers go to the admin's own chat only (the route step's reply); the log has
deal ids and counts only: never a name, never the words typed.
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from typing import Callable, Optional

from . import contact, deals, platforms
from .result import MESSAGE_KINDS
from .store import COPILOT, HUMAN, MODES, PENDING, SHIPMENT_STATES, STAGES
from .wechat_review import admin_problem

logger = logging.getLogger(__name__)

SWITCH = "prm_wechat_commands"       # the config's field (PRM_WECHAT_COMMANDS)
ACTOR = "admin"                      # the deal events' actor: the admin's typed command (as 「接群」's, group_link.py)
CHOICE_SEC = 300                     # 「选 N」 picks from a list of namesakes this long
ASKED_KEEP_SEC = 7200                # ... and after that still says the list expired, this long after it was given
LISTED = 10                          # namesakes a list numbers at most
CLOSE_LISTED = 3                     # close names a 「没有对得上的」 answer offers at most
CLOSE_CUTOFF = 0.5                   # ... how close (difflib's ratio)
NAME_MAX = 20                        # a name typed after the word: at most this many characters (spaces aside)
TAIL = 4                             # a waybill, an order number: its last characters

# The words and the deal action each one is (``_do``).  「恢复副驾驶」 is the console's button; 「副驾驶」 the short form.
HANDCARDS, HUOPAN, OPENING, SIGN, NOTE, TAKEOVER, BACK, STATUS = (
    "handcards", "huopan", "opening", "sign", "note", "takeover", "copilot", "status")
VERBS = {"做手卡": HANDCARDS, "补发货盘": HUOPAN, "写开场白": OPENING, "签收": SIGN, "备注": NOTE, "接管": TAKEOVER,
         "副驾驶": BACK, "恢复副驾驶": BACK, "合作单": STATUS}
HELP_WORDS = ("指令",)
CLEAR_WORDS = ("清空",)
# A deal past 已签收: 「签收」 would set its stage back (the console's 「更新物流」 would too): the console's to decide
AFTER_DELIVERY = ("brief", "draft", "revise", "published", "closed")

_VERB = re.compile(r"^(" + "|".join(sorted(map(re.escape, VERBS), key=len, reverse=True)) + r")[ \t]+(\S.*)$")
_CHOICE = re.compile(r"^选\s*(\d{1,3})$")
_ENDING = re.compile(r"[！!～~]*。?[！!～~]*$")        # a command's ending (wechat_review ``_COMMAND_END``)
_HESITATING = "。.…．｡"
_ASKING_END = ("吗", "呢", "么", "嘛", "没", "没有")
_ASKING_IN = ("怎么", "什么", "为什么", "如何", "是不是", "能不能", "要不要", "可不可以")
_NOTE_COLON = re.compile(r"^备注[ \t]+([^:：\n]+?)\s*[:：]\s*(.*)$")   # 「备注 小美：只发链接」
_SEPARATORS = re.compile(r"[\s\-－_—–·・:：]+")
_PREFIX = re.compile(r"^\s*(?:抖音|小红书|淘宝|快手|视频号)\s*[-－_—:：·]+\s*")   # the friend add's remark (抖音-…)
_VOICE = re.compile(r"^\[语音\]\s*(.*)$", re.S)
_QUOTE_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9]+:\s*")

HELP_TEXT = (
    "合作单指令（只在管理员私聊里打字发；名字写她在名单里的名字，写一部分也行）：\n"
    "做手卡 名字 — 按她最近一张寄样单的款做手卡，做好进「待审批」等你看\n"
    "补发货盘 名字 — 写一份补发货盘的草稿，进「待审批」等你看\n"
    "写开场白 名字 — 写一份开场白，进「待审批」\n"
    "签收 名字 — 合作单标「已签收」（和控制台「更新物流 → 已签收」一样）\n"
    "备注 名字 文字 — 换掉她的起草备注（「备注 名字 清空」清掉，「备注 名字」看现在的）\n"
    "接管 名字 / 副驾驶 名字 — 人工接管 / 恢复副驾驶\n"
    "合作单 名字 — 看她的合作单现在怎样\n"
    "好几位对得上时回「选 编号」；要交给 Claude 的话前面加 cc"
)
# One line for the admin's 「帮助」 (src/admin.py) and the relay's help (src/bot.py) while the switch is on
HELP_LINE = "\n  做手卡 / 补发货盘 / 写开场白 / 签收 / 备注 / 接管 / 副驾驶 / 合作单 + 名字   合作单指令（发「指令」看用法）"
VOICE_TEXT = "合作单指令要打字发（语音不算，免得听错）：比如「{example}」"
QUOTE_TEXT = "引用的消息不算合作单指令：不引用，直接打字发「{example}」"
OTHER_TEXT = "只认打字发的合作单指令：比如「{example}」"
MORE_TEXT = "一条消息只放一个合作单指令、一行写完（备注的文字也写在同一行）：都没处理，请分开发"
CC_HINT = "（要交给 Claude 请在前面加 cc）"
ERROR_TEXT = "处理合作单指令的时候出错了（详见日志）：请到控制台看一下"


@dataclass(frozen=True)
class Typed:
    """A command line (``parse``): the word's action (``VERBS``, or ``help``), the name as typed, 备注's words (None: none
    given; 「清空」: ""), and whether the message has more lines (refused whole)."""

    verb: str
    name: str = ""
    text: Optional[str] = None
    more: bool = False
    word: str = ""                   # the word as typed (「恢复副驾驶」, 「副驾驶」 …): what the answers repeat
    alt: Optional[tuple] = None      # 备注 read the other way: (name, words) (``parse``)


def switch_on(config) -> bool:
    """PRM_WECHAT_COMMANDS is on (a config object without it, an older caller's: off)."""
    return str(getattr(config, SWITCH, "off") or "off").strip().lower() == "on"


def _key(text) -> str:
    """A name as it is compared: full-width letters as plain ones, no case, no spaces nor separators."""
    return _SEPARATORS.sub("", unicodedata.normalize("NFKC", str(text or "")).casefold())


def _tail(value) -> str:
    """A waybill or an order number as the admin's chat shows it: its last TAIL characters."""
    value = str(value or "").strip()
    return value if len(value) <= TAIL else "…" + value[-TAIL:]


def _ended(line: str) -> Optional[str]:
    """``line`` without a command's ending (at most one 「。」 with any 「！!～~」, decided before full-width letters
    become plain ones); None when it hesitates (「…」「.」「。。」 at its end) or asks (a 「？」, 吗 / 呢 / 么 / 嘛 / 没 at its
    end, 怎么 / 什么 … in it): no command."""
    bare = _ENDING.sub("", line.rstrip(), count=1).rstrip()
    if not bare or bare[-1] in _HESITATING or "?" in unicodedata.normalize("NFKC", line):
        return None
    if bare.endswith(_ASKING_END) or any(word in bare for word in _ASKING_IN):
        return None
    return bare


def _note_words(words) -> Optional[str]:
    """备注's words as kept: None for none, "" for 「清空」 (its ending aside), else as typed."""
    words = str(words or "").strip()
    if not words:
        return None
    return "" if _ended(words) in CLEAR_WORDS else words


def parse(text) -> Optional[Typed]:
    """The command ``text`` is (pure; see the module's doc): ``Typed`` or None.  Only its first line is read; more
    lines make it ``more`` (refused whole by ``DealCommands``).  备注: 「备注 <名字>：<文字>」 (a colon after the name, the
    name may hold spaces) takes any words, a question too (it is plainly a note); 「备注 <名字> <文字>」 (the name up to
    the first blank) only when the whole line reads as a command; a line that reads both ways (「备注 小美 时间：周三」)
    keeps the other as ``alt`` (``DealCommands`` takes the one whose name matches); the words keep their own ending."""
    lines = str(text or "").replace("\u3000", " ").strip().split("\n")
    first, more = lines[0].strip(), any(line.strip() for line in lines[1:])
    colon = _NOTE_COLON.fullmatch(first)
    if colon is not None and not 1 <= len(_key(colon.group(1))) <= NAME_MAX:
        colon = None
    space = None
    bare = _ended(first)
    if bare is not None:
        if bare in HELP_WORDS:
            return Typed("help", more=more, word=bare)
        m = _VERB.fullmatch(bare)
        if m is not None:
            word, rest = m.group(1), m.group(2).strip()
            if VERBS[word] == NOTE:
                parts = rest.split(None, 1)
                typed_words = first[len(word):].strip().split(None, 1)       # (from the line as typed: its ending)
                space = Typed(NOTE, parts[0], _note_words(typed_words[1]) if len(parts) > 1 else None, more, word)
            else:
                space = Typed(VERBS[word], rest, None, more, word)
            if not 1 <= len(_key(space.name)) <= NAME_MAX:
                space = None
    if colon is not None:
        typed = Typed(NOTE, colon.group(1).strip(), _note_words(colon.group(2)), more, "备注")
        if space is not None and _key(space.name) != _key(typed.name):
            return replace(typed, alt=(space.name, space.text))
        return typed
    return space


def _clock_text(at, now: float) -> str:
    """When, as the answers say it: HH:MM today, MM-DD HH:MM another day."""
    try:
        when = time.localtime(float(at))
        day = time.strftime("%m-%d", when) != time.strftime("%m-%d", time.localtime(now))
        return time.strftime("%m-%d %H:%M" if day else "%H:%M", when)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


class DealCommands:
    """The admin's 合作单指令 of one PRM service (see the module's doc).  ``service``: the PrmService (its store,
    ``deal_action``, ``wechat_review`` for her 称呼 as the admin's chat shows it).  ``on``: PRM_WECHAT_COMMANDS."""

    help_line = HELP_LINE                          # the admin's 「帮助」 names the commands in it (src/admin.py)

    def __init__(self, service, config=None, *, clock: Callable[[], float] = time.time):
        self._service = service
        self._clock = clock
        # (never on the Mac's edge side, WECHAT_BACKEND=edge: the admin's chat is read on Windows there, as 微信里审批草稿's)
        self.on = switch_on(config) and str(getattr(config, "wechat_backend", "") or "") != "edge"
        self._admin = str(getattr(config, "admin_wxid", "") or "").strip()
        self._relay_only = str(getattr(config, "claude_relay_only", "off") or "off").strip().lower() == "on"
        self._lock = threading.Lock()              # one command at a time
        # the last list of namesakes: {"at", "typed", "deals": [deal id], "done"} (「选 N」 picks from it)
        self._asked: Optional[dict] = None

    # ── the admin's text (src/admin.py ``_command``) ─────────────────────

    def admin_command(self, text, msg: Optional[dict] = None) -> Optional[Callable[[], str]]:
        """The command ``text`` is (``run()`` answers it), or None (it goes on as it is).  ``msg``: the admin's message
        (src/routing/steps.py ``admin_command``): only typed text in the admin's own one-to-one chat runs one; a voice
        message or a quote with a command's words is answered that it must be typed; anybody else's, nothing.  Without
        ``msg`` (the relay asking whether a line is a command: ``AdminCommandHandler.is_command``) only the words count.
        「选 N」 only while a list is open or was lately (ASKED_KEEP_SEC)."""
        if not self.on:
            return None
        content = str(text or "")
        how = self._how(msg)
        if how is None:
            return None
        if how == "voice":
            heard = _VOICE.match(content.strip())
            content = heard.group(1).strip().rstrip("。.！!，,、 ") if heard else content
        typed = parse(content)
        if typed is None:
            match = _CHOICE.fullmatch(_ended(content.strip()) or "") if "\n" not in content.strip() else None
            asked = self._asked
            if match is None or asked is None or self._clock() - float(asked["at"]) > ASKED_KEEP_SEC:
                return None
            if how:
                return lambda: self._refusal(how, f"选 {match.group(1)}")
            number = int(match.group(1))
            return lambda: self._run(self._choose, number)
        if how:
            return lambda: self._refusal(how, self._example(typed))
        return lambda: self._run(self._command, typed)

    def _how(self, msg: Optional[dict]) -> Optional[str]:
        """What kind of message of the admin's ``msg`` is: "" typed text (or no message: words only), ``voice``,
        ``quote``, ``other``; None for a message that is not the admin's own in the admin's one-to-one chat."""
        if msg is None:
            return ""
        if (msg.get("is_group", True) or not self._admin or str(msg.get("sender_id") or "") != self._admin
                or str(msg.get("chat_id") or "") != self._admin or admin_problem(self._admin, self._service.store)):
            return None
        try:
            msg_type = int(msg.get("msg_type", 1) or 1)
        except (TypeError, ValueError):
            return "other"
        if msg_type == 34:
            return "voice"
        if _QUOTE_PREFIX.sub("", str(msg.get("quoted_content") or "")).strip():
            return "quote"
        return "" if msg_type == 1 else "other"

    @staticmethod
    def _example(typed: Typed) -> str:
        if typed.verb == "help":
            return typed.word
        return f"{typed.word} {contact.mask_contacts(typed.name)}" + (f" {typed.text}" if typed.text else "")

    @staticmethod
    def _refusal(how: str, example: str) -> str:
        template = {"voice": VOICE_TEXT, "quote": QUOTE_TEXT}.get(how, OTHER_TEXT)
        return template.format(example=example)

    def _run(self, fn: Callable[..., str], *args) -> str:
        try:
            with self._lock:
                return fn(*args)
        except Exception:
            logger.exception("PRM: 合作单指令: the admin's command failed")
            return ERROR_TEXT

    # ── the commands ─────────────────────────────────────────────────────

    def _command(self, typed: Typed) -> str:
        """A command line: refused whole with more lines; 「指令」: the list; else her deal by name (``_find``), the
        command done for one, a list for several, the closest names for none."""
        if typed.more:
            return MORE_TEXT
        if typed.verb == "help":
            return HELP_TEXT
        found, close, dead = self._find(typed.name)
        if not found and typed.alt is not None:          # (备注 read the other way: its name matches)
            other = self._find(typed.alt[0])
            if other[0]:
                typed = replace(typed, name=typed.alt[0], text=typed.alt[1], alt=None)
                found, close, dead = other
        if len(found) == 1:                            # (a list open meanwhile stays open: 「选 N」 is for that one)
            party, deal = found[0]
            return self._do(typed, party, deal)
        if found:
            return self._list(typed, found)
        logger.info("PRM: 合作单指令 %s: no deal matches (%d close, %d not live)", typed.verb, len(close), len(dead))
        asked = contact.mask_contacts(typed.name)
        if dead:
            head = f"对得上「{asked}」的只有停用的达人，或者收件信息清过的合作单：请到控制台看"
        else:
            head = f"没有达人对得上「{asked}」（只找名单里在用的达人）"
            if close:
                head += "；是不是：" + "、".join(self._shown(p, d) for p, d in close) + "？"
        return head + (CC_HINT if self._relay_only else "")

    def _list(self, typed: Typed, found: list) -> str:
        """Several deals match: numbered (LISTED at most), 「选 N」 picks one (CHOICE_SEC)."""
        listed = found[:LISTED]
        self._asked = {"at": self._clock(), "typed": typed, "deals": [int(d["id"]) for _, d in listed], "done": False}
        logger.info("PRM: 合作单指令 %s: %d deals match, %d listed", typed.verb, len(found), len(listed))
        asked = contact.mask_contacts(typed.name)
        head = (f"有 {len(found)} 位达人对得上「{asked}」：回「选 编号」{self._doing(typed)}（{CHOICE_SEC // 60} 分钟内有效）"
                if len(found) <= LISTED else
                f"有 {len(found)} 位达人对得上「{asked}」，只列了前 {LISTED} 位：回「选 编号」{self._doing(typed)}"
                f"（{CHOICE_SEC // 60} 分钟内有效），不在里面的话名字写全一点再发")
        lines = [head]
        for n, (party, deal) in enumerate(listed, 1):
            lines.append(f"{n}. {self._shown(party, deal)} · {STAGES.get(deal['stage'], deal['stage'])} · 合作单 #{deal['id']}")
        return "\n".join(lines)

    @staticmethod
    def _doing(typed: Typed) -> str:
        return {HANDCARDS: "做手卡", HUOPAN: "补发货盘", OPENING: "写开场白", SIGN: "标签收", NOTE: "改备注",
                TAKEOVER: "人工接管", BACK: "恢复副驾驶", STATUS: "看合作单"}.get(typed.verb, "")

    def _choose(self, number: int) -> str:
        """「选 N」: the open list's N-th deal, the command run once."""
        asked = self._asked
        if asked is None:
            return "现在没有在等你选的合作单指令"
        typed = asked["typed"]
        again = f"重新发「{self._example(typed)}」"
        if asked["done"]:
            return f"刚才那个「选」已经回过了：要再来一次请{again}"
        if self._clock() - float(asked["at"]) > CHOICE_SEC:
            return f"刚才那个列表已经过了 {CHOICE_SEC // 60} 分钟，作废了：要的话{again}"
        ids = asked["deals"]
        if not 1 <= number <= len(ids):
            return f"没有第 {number} 个：回「选 1」到「选 {len(ids)}」"
        deal = self._service.store.deal(ids[number - 1])
        party = self._service.store.party(deal["party_id"]) if deal else None
        if deal is None or party is None or not party.get("active") or deal.get("purged_at"):
            return f"第 {number} 个现在不在名单里了（停用了，或者收件信息清过了）：请到控制台看"
        asked["done"] = True
        return self._do(typed, party, deal)

    def _find(self, name: str) -> tuple[list, list, list]:
        """``(found, close, dead)`` for a name typed: the live deals (``(party, deal)``: a creator on the list, her
        newest deal, not cleaned up) whose 称呼 is it (with or without the platform's prefix: ``_key``), else those
        whose 称呼 holds it; the CLOSE_LISTED closest of the others when none does; and the deals not live that would
        match."""
        store = self._service.store
        newest: dict = {}
        for deal in store.deals():
            newest[deal["party_id"]] = deal
        want = _key(name)
        whole, piece, dead, keys = [], [], [], {}
        for party in store.parties(include_inactive=True):
            deal = newest.get(party["id"])
            if deal is None:
                continue
            names = {_key(party["name"]), _key(_PREFIX.sub("", str(party["name"] or "")))} - {""}
            live = bool(party.get("active")) and not deal.get("purged_at")
            hit = "whole" if want in names else "piece" if any(want in k for k in names) else ""
            if not live:
                if hit:
                    dead.append((party, deal))
                continue
            if hit == "whole":
                whole.append((party, deal))
            elif hit == "piece":
                piece.append((party, deal))
            for k in names:
                keys.setdefault(k, (party, deal))
        found = whole or piece
        close: list = []
        if not found:
            seen = set()
            for k in difflib.get_close_matches(want, list(keys), n=CLOSE_LISTED * 2, cutoff=CLOSE_CUTOFF):
                party, deal = keys[k]
                if party["id"] not in seen:
                    seen.add(party["id"])
                    close.append((party, deal))
            close = close[:CLOSE_LISTED]
        return found, close, dead

    def _shown(self, party: dict, deal: dict) -> str:
        """Her 称呼 as the admin's chat shows it (``WechatReview._name_of``: her platform ids by their last characters,
        numbers masked, one line, 20 characters at most)."""
        return self._service.wechat_review._name_of({"party_id": party["id"], "deal_id": deal["id"], "meta": {}},
                                                    default="（没有名字）")

    def _hide(self, party: dict, deal: dict, text) -> str:
        """``text`` (a refusal, a group's name, her note) as the admin's chat shows it (``WechatReview._hide``)."""
        return self._service.wechat_review._hide({"party_id": party["id"], "deal_id": deal["id"], "meta": {}}, text)

    def _do(self, typed: Typed, party: dict, deal: dict) -> str:
        """The command for her deal: the console's deal action (``PrmService.deal_action``, actor ``ACTOR``) and one line
        of what came of it."""
        who = self._shown(party, deal)
        handler = {HANDCARDS: self._handcards, HUOPAN: self._huopan, OPENING: self._opening, SIGN: self._sign,
                   NOTE: self._note, TAKEOVER: self._takeover, BACK: self._back, STATUS: self._status}[typed.verb]
        answer = handler(typed, party, deal)
        if typed.verb == STATUS:
            logger.info("PRM: 合作单指令 status: deal %d shown in WeChat", deal["id"])
            return answer
        return f"{who}：{answer}"

    def _action(self, action: str, data: dict) -> dict:
        """The console's deal action with the admin as the actor (``PrmService.deal_action``)."""
        out = self._service.deal_action(action, data, actor=ACTOR)
        out = out if isinstance(out, dict) else {"ok": False, "error": "出错了"}
        logger.info("PRM: 合作单指令: deal %s, %s from WeChat: %s", data.get("deal_id"), action,
                    "done" if out.get("ok") else "refused")
        return out

    def _refused(self, party: dict, deal: dict, out: dict, what: str) -> str:
        return f"没{what}：{self._hide(party, deal, out.get('error') or '出错了')}"

    def _handcards(self, typed: Typed, party: dict, deal: dict) -> str:
        """「做手卡」: the console's 「做手卡」 with nothing ticked (her latest sample order's products).  Its draft always
        waits for a person (``PrmService._ask_handcards``)."""
        from .service import sample_items                 # (the service imports this module)
        out = self._action("handcards", {"deal_id": deal["id"]})
        if not out.get("ok"):
            return self._refused(party, deal, out, "派做手卡")
        order = sample_items(self._service.store, deal["id"])
        items = len(order["items"]) if order and order.get("shop") == "dy" else 0
        which = (f"按她最近一张寄样单{' ' + _tail(order['order']) + ' ' if order.get('order') else ''}的 {items} 款"
                 if items else "她没有机器人建的抖店寄样单：Mac 按案卷里她选的款做")
        return f"已派做手卡的任务 #{out.get('job')}（{which}），做好会进「待审批」，要你看过才发"

    def _huopan(self, typed: Typed, party: dict, deal: dict) -> str:
        """「补发货盘」: the console's; its draft always waits for a person (``PrmService._ask_huopan``)."""
        out = self._action("huopan", {"deal_id": deal["id"]})
        if not out.get("ok"):
            return self._refused(party, deal, out, "派补发货盘")
        return f"已派补发货盘的任务 #{out.get('job')}，写好会进「待审批」，要你看过才发"

    def _opening(self, typed: Typed, party: dict, deal: dict) -> str:
        """「写开场白」: the console's; its draft goes as the console's 开场白 goes (PRM_OPENING_AUTO_MIN)."""
        out = self._action("opening", {"deal_id": deal["id"]})
        if not out.get("ok"):
            return self._refused(party, deal, out, "派写开场白")
        minutes = int(getattr(self._service, "_opening_auto_sec", 0) or 0) // 60
        how = (f"和控制台点的一样：{minutes} 分钟没人处理会照原样自动发（下班时段、自动模式写好就发），不想发就回「不发」或在控制台点"
               if minutes else "要你看过才发")
        return f"已派写开场白的任务 #{out.get('job')}，写好会进「待审批」，{how}"

    def _sign(self, typed: Typed, party: dict, deal: dict) -> str:
        """「签收」: the console's 「更新物流 → 已签收」 on her one waybill (``shipment_status`` delivered), or without one
        its 「改阶段 → 已签收」 (``stage`` delivered).  Two waybills or more, a deal past 已签收, or one 已签收 already:
        the console's."""
        store = self._service.store
        ships = store.shipments(deal["id"])
        stage = STAGES.get(deal["stage"], deal["stage"])
        if deal["stage"] in AFTER_DELIVERY:
            return f"没标：合作单已经是「{stage}」了，标签收会把阶段改回「已签收」：要改请到控制台"
        if len(ships) > 1:
            return (f"没标：她的合作单有 {len(ships)} 个运单号，不知道签收的是哪个：请到控制台她的合作单里"
                    "「更新物流」")
        ship = ships[0] if ships else None
        if deal["stage"] == "delivered" and (ship is None or ship["status"] == "delivered"):
            return "合作单本来就是「已签收」，没再标：要重新标请到控制台"
        if ship is not None:
            out = self._action("shipment_status", {"deal_id": deal["id"], "status": "delivered"})
            done = f"标成「已签收」了（{ship['carrier']} {_tail(ship['waybill'])}）"
        else:
            out = self._action("stage", {"deal_id": deal["id"], "stage": "delivered"})
            done = "合作单没有运单号，阶段改成了「已签收」"
        if not out.get("ok"):
            return self._refused(party, deal, out, "标签收")
        return done + "；签收以后该做的（比如专属佣金）和控制台点「已签收」一样起草，在「待审批」里等你批"

    def _note(self, typed: Typed, party: dict, deal: dict) -> str:
        """「备注」: the console's 起草备注 replaced (「清空」: cleared); no words: the note as it is."""
        before = str(deal.get("draft_note") or "")
        if typed.text is None:
            if not before:
                return "还没有起草备注（「备注 名字 文字」写一条）"
            shown = contact.mask_contacts(self._hide(party, deal, " / ".join(before.split("\n"))))
            return f"起草备注（{len(before)} 字）：{shown}"
        out = self._action("draft_note", {"deal_id": deal["id"], "text": typed.text})
        if not out.get("ok"):
            return self._refused(party, deal, out, "改起草备注")
        note = str(out.get("draft_note") or "")
        if note == before:
            return "起草备注本来就是这样，没改"
        if not note:
            return "起草备注清掉了"
        return f"起草备注换好了（{len(note)} 字）：之后她的草稿都按它写"

    def _takeover(self, typed: Typed, party: dict, deal: dict) -> str:
        """「接管」: the console's 人工接管 (PRM_TAKEOVER_MIN)."""
        out = self._action("mode", {"deal_id": deal["id"], "mode": HUMAN})
        if not out.get("ok"):
            return self._refused(party, deal, out, "改成人工接管")
        after = self._service.store.deal(deal["id"]) or {}
        until = _clock_text(after.get("mode_until"), self._clock()) if after.get("mode_until") else ""
        return (f"人工接管了{f'，到 {until} 自动回副驾驶' if until else ''}：这段时间她的消息只记录、不起草"
                "（「副驾驶 名字」提前恢复）")

    def _back(self, typed: Typed, party: dict, deal: dict) -> str:
        """「副驾驶」 / 「恢复副驾驶」: the console's 恢复副驾驶; already copilot: nothing done."""
        if deals.effective_mode(deal, self._clock()) == COPILOT:
            return "本来就是副驾驶：她的消息照常起草"
        out = self._action("mode", {"deal_id": deal["id"], "mode": COPILOT})
        if not out.get("ok"):
            return self._refused(party, deal, out, "恢复副驾驶")
        return "恢复副驾驶了：她的消息照常起草，接管期间双方说的话会带进下一份草稿"

    def _status(self, typed: Typed, party: dict, deal: dict) -> str:
        """「合作单」: read only (nothing changes, nothing recorded)."""
        store, now = self._service.store, self._clock()
        mode = deals.effective_mode(deal, now)
        mode_text = MODES.get(mode, mode)
        if mode == HUMAN and deal.get("mode_until"):
            mode_text += f"（到 {_clock_text(deal['mode_until'], now)}）"
        lines = [f"{self._shown(party, deal)}（合作单 #{deal['id']}）：阶段「{STAGES.get(deal['stage'], deal['stage'])}」"
                 f" · {mode_text}"]
        ships = store.shipments(deal["id"])
        sample = []
        if ships:
            ship = ships[0]
            sample.append(f"{ship['carrier']} {_tail(ship['waybill'])} "
                          f"{SHIPMENT_STATES.get(ship['status'], ship['status'])}"
                          + (f"（共 {len(ships)} 个运单号）" if len(ships) > 1 else ""))
        orders = store.sample_orders(deal_id=deal["id"])
        if orders:
            row = orders[-1]
            state = platforms.SAMPLE_STATES.get(str(row.get("status") or ""), "") or "还没同步到"
            sample.append(f"寄样单 {_tail(row['order_no'])} {state}")
        if sample:
            lines.append("寄样：" + "；".join(sample))
        lines.append("回复：" + self._where(party, deal))
        lines.append(self._waiting(party, deal))
        note = str(deal.get("draft_note") or "")
        if note:
            lines.append(f"起草备注：有（{len(note)} 字，「备注 名字」看）")
        return "\n".join(lines)

    def _where(self, party: dict, deal: dict) -> str:
        """Where her drafts go: her private chat, the groups linked to her deal (her lines there are answered there),
        her 三方群 (read only), and where what the bot starts itself goes (``PrmService._reach``)."""
        from .boss_groups import LINKED                   # (the service imports this module)
        service, store = self._service, self._service.store
        groups = [self._hide(party, deal, r.get("name") or "（没有群名）")
                  for r in store.boss_groups((LINKED,), deal_id=int(deal["id"])) if service.boss_groups.is_linked(r["chat"])]
        chat, group = service._reach(party)
        if chat is None:
            started = "机器人主动发的（开场白、货盘、手卡、发货通知）发不出去：她不是好友，也不在接手的群里了"
        elif group is not None:
            started = f"机器人主动发的（开场白、货盘、手卡、发货通知）发到群「{self._hide(party, deal, group.get('name') or '')}」"
        else:
            started = "机器人主动发的（开场白、货盘、手卡、发货通知）发私聊"
        parts = [("她在私聊和群「" + "」「".join(groups) + "」里说的话，在哪说就回哪") if groups else "私聊", started]
        three = deal.get("three_party") or {}
        if three.get("state") == "created" and three.get("chat") and not service.boss_groups.is_linked(three["chat"]):
            parts.append(f"三方群「{self._hide(party, deal, three.get('name') or '')}」机器人只读")
        return "；".join(parts)

    def _waiting(self, party: dict, deal: dict) -> str:
        """What of hers waits in 「待审批」 (numbers and kinds), and whether the Mac is writing a draft of hers."""
        store = self._service.store
        rows = store.deal_packets(deal["id"], statuses=(PENDING,))
        names = []
        for p in rows[:3]:
            tool = p.get("tool") or ""
            spec = platforms.ACTIONS.get(tool)
            label = str(spec["label"]).split(" · ")[-1] if spec else MESSAGE_KINDS.get(p.get("kind"), p.get("kind") or "")
            names.append(f"#{p['id']} {label}")
        text = (f"待审批：{len(rows)} 份（" + "、".join(names) + ("…" if len(rows) > 3 else "") + "）") if rows \
            else "待审批：没有她的草稿"
        relay = getattr(self._service, "_relay", None)
        chats = {party["wxid"]} | {r["chat"] for r in store.boss_groups(deal_id=int(deal["id"]))}
        try:
            writing = relay is not None and any(relay.store.latest_open(c, drafts_only=True, system=False) is not None
                                                for c in chats)
        except Exception:
            logger.exception("PRM: 合作单指令: whether a draft of deal %d is being written could not be read", deal["id"])
            writing = False
        return text + ("；Mac 正在写她的草稿" if writing else "")
