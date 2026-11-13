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
- 「选款链接 <达人>」 (2026-11-13): ``site_link``, one job that makes her link of the 选款网站 (PRM_SAMPLE_SITE_MIN;
  src/prm/sample_site.py) and a fixed sentence with it; its draft always waits for a person;
- 「写开场白 <达人>」: ``opening``, one draft job; ordered from WeChat it always waits for a person too (decision
  2026-11-11: ``meta.opening.held``, never by PRM_OPENING_AUTO_MIN, the 下班时段 or 自动模式; told in WeChat like any
  waiting draft while PRM_WECHAT_REVIEW is on); a click in the console goes as before;
- 「签收 <达人>」: what the console's 「更新物流 → 已签收」 sends (``shipment_status`` delivered, her one waybill), or
  without a waybill its 「改阶段 → 已签收」 (``stage`` delivered), the latter only for a deal at a sample stage
  (SAMPLE_STAGES) or with a sample order of its own; what a signature starts (the 专属佣金 chain) starts as from the
  console, its drafts waiting for 批准并执行.  Two waybills or more, a deal past 已签收 (the stage would go back), one
  that is 已签收 already, or one with neither a waybill nor samples: nothing is done, the answer points to the console;
- 「备注 <达人> <文字>」 / 「备注 <达人>：<文字>」: ``draft_note``, the whole 起草备注 replaced as the console's save does
  (longer than DRAFT_NOTE_CHARS: the console's refusal); the answer shows the note it replaced and the new one;
  「备注 <达人> 清空」 clears it; 「备注 <达人> 撤销」 puts back the note this command replaced last (in memory,
  UNDO_SEC, once); 「备注 <达人>」 shows it;
- 「接管 <达人>」 / 「副驾驶 <达人>」 (or 「恢复副驾驶 <达人>」): ``mode`` human (PRM_TAKEOVER_MIN, the console's 人工接管) /
  copilot (its 恢复副驾驶; already copilot: nothing done);
- 「合作单 <达人>」: read only: the stage and the mode, the waybill and her latest sample order, where her drafts go and
  where what the bot starts itself goes and why (the sends' own decision, ``PrmService._reaching``: PRM_REACH_LAST_SPOKE
  may send it to the group where her side last spoke), a group of hers the scan linked that nobody confirmed (how to:
  「确认接群 <群名>」), what of hers waits in 「待审批」 or is being written, whether a 起草备注 is set;
- 「指令」: these commands (the admin's 「帮助」 names them in one line, ``HELP_LINE``).

Nothing here sends anything to a creator by itself: each command only makes what the console's button makes (a draft
job, a note, a stage or a mode change), and a draft it brings waits or goes by the same rules as one clicked in the
console (a 开场白 ordered here: always waits).  The deal's record names the admin as who did it (``ACTOR``:
「管理员（微信里）」 in the console, as 「接群」's).  No reply carries a relay job's number as 「#N」 (in this chat 「发 N」
「不发 N」 act on draft numbers); a deal's number is written 「合作单 N 号」, a draft's 「#N」 as the review's notices do.

Finding her (``_find``): her 称呼 as the console lists her (often the friend add's remark 「抖音-<昵称>」), with or without
the platform's prefix, spaces, separators (「-」「_」「·」「:」), full-width letters and case aside; a name of NAME_MIN to
NAME_MAX characters (else one line saying so); only a creator on the list whose deal was not cleaned up (8.2).
- Her whole name: done (two or more with that name: numbered, 「选 N」).  A stopped creator's or a cleaned deal's whole
  name: never a piece of another's instead; the answer says she is stopped / her deal was cleaned and points to the
  console.
- A piece of names only: a command that changes something always lists them numbered (one too) and 「选 N」 runs it once
  for the N-th, CHOICE_SEC long; 「合作单」 and showing a 备注 act on one at once.  The list holds LISTED at most (her
  称呼, the stage, the deal's number).  「选 N」 is a command only while such a list is open or was lately
  (ASKED_KEEP_SEC: then it says the list expired, was answered, or was closed by a later command), so the words stay free
  otherwise (as group_link's 「是 N」).
- None: one line, with the CLOSE_LISTED closest names; the line is not handed on to the Mac's Claude (with
  CLAUDE_RELAY_ONLY the answer says to put cc in front for Claude).

What is a command (``parse``): one line, 「<指令><空白><名字…>」, ending in nothing or at most one 「。」 with any 「！!～~」
(wechat_review's ``_command_word``).  「做手卡怎么用」 (no blank after the word), 「签收了吗」, 「合作单 小美呢」, 「做手卡
小美？」 (a question: ？ / 吗 / 呢 / 么 / 嘛 / 没 at its end, or 怎么 / 怎样 / 什么 / 为什么 / 如何 / 多久 / 多少 / 哪 … in it),
「签收 小美…」 (hesitating) are none and go where they went before.  备注: once the line is 「备注」 + a blank + a name
that matches a live creator, the rest is her note: in the colon form 「备注 名字：文字」 whatever it says, in the blank form
only words that read as no question nor hesitation (a question there writes nothing, one line says to use the colon:
the admin may have been asking); its readings (``Typed.readings``: 「备注 名字：文字」, and the name up to each blank) are
tried, the longest name that matches wins (「备注 Lucy Anne 只发链接」: her name whole, never a piece of it in the note),
and a stopped creator's or a cleaned deal's whole name longer than that one refuses it all (said so, nothing written);
a 备注 line that names nobody follows the rules above.  A
message whose first line is a command and that has more lines is refused whole (one command per message).  Only typed
text in the admin's own one-to-one chat (never a group, a creator, anybody else; nothing while ADMIN_WXID is a group's or
a registered creator's: wechat_review's ``admin_problem``), written lately (MAX_AGE_SEC on this PC's clock, as the
review's answers: an older line WeChat read late gets one line saying it was not done): a voice message or a quote with a
command's words gets one line saying to type it, and nothing is done.  The switch off: nothing of it (src/bot.py does not
hand it to the admin handler; every line goes where it went before).

Replies name her as 微信里审批草稿 does (her platform ids by their last characters, ``WechatReview._name_of``); waybills
and order numbers by their last 4; what the admin typed is echoed with its numbers masked.  The answers go to the admin's
own chat only (the route step's reply); the decision trace keeps ``TRACE_TEXT`` instead of the line (src/routing/steps.py
``admin_command``); the log has deal ids and counts only: never a name, never the words typed.
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

from ..relay.service import MAX_AGE_SEC
from . import contact, deals, platforms
from .result import MESSAGE_KINDS
from .store import COPILOT, HUMAN, MODES, PENDING, SHIPMENT_STATES, STAGES
from .wechat_review import admin_problem

logger = logging.getLogger(__name__)

SWITCH = "prm_wechat_commands"       # the config's field (PRM_WECHAT_COMMANDS)
ACTOR = "admin"                      # the deal events' actor: the admin's typed command (as 「接群」's, group_link.py)
CHOICE_SEC = 300                     # 「选 N」 picks from a list of namesakes this long
ASKED_KEEP_SEC = 7200                # ... and after that still says the list expired, this long after it was given
UNDO_SEC = 7200                      # 「备注 名字 撤销」 puts back the note replaced this recently (once)
LISTED = 10                          # namesakes a list numbers at most
CLOSE_LISTED = 3                     # close names a 「没有对得上的」 answer offers at most
CLOSE_CUTOFF = 0.5                   # ... how close (difflib's ratio)
NAME_MIN = 2                         # a name typed after the word: at least this many characters (spaces aside) ...
NAME_MAX = 20                        # ... and at most this many
TAIL = 4                             # a waybill, an order number: its last characters
ECHO_CHARS = 30                      # what the admin typed, as an answer repeats it: at most this many characters

# The words and the deal action each one is (``_do``).  「恢复副驾驶」 is the console's button; 「副驾驶」 the short form.
HANDCARDS, HUOPAN, OPENING, SIGN, NOTE, TAKEOVER, BACK, STATUS = (
    "handcards", "huopan", "opening", "sign", "note", "takeover", "copilot", "status")
SITE_LINK = "site_link"              # 「选款链接」: the deal action of src/prm/sample_site.py (its LINK)
VERBS = {"做手卡": HANDCARDS, "补发货盘": HUOPAN, "写开场白": OPENING, "签收": SIGN, "备注": NOTE, "接管": TAKEOVER,
         "副驾驶": BACK, "恢复副驾驶": BACK, "合作单": STATUS, "选款链接": SITE_LINK}
HELP_WORDS = ("指令",)
CLEAR_WORDS = ("清空",)
UNDO_WORDS = ("撤销",)
# A deal past 已签收: 「签收」 would set its stage back (the console's 「更新物流」 would too): the console's to decide
AFTER_DELIVERY = ("brief", "draft", "revise", "published", "closed")
# 「签收」 without a waybill: only a deal at one of these stages (store.STAGES: 收集收件信息 待寄出 已寄出 运输中 物流异常)
# or one with a sample order of its own; any other stage is the console's (it never had samples sent)
SAMPLE_STAGES = ("address", "to_ship", "shipped", "transit", "ship_error")
TRACE_TEXT = "[合作单指令，内容不记录]"   # what the decision trace keeps of such a line (src/routing/steps.py)

_VERB = re.compile(r"^(" + "|".join(sorted(map(re.escape, VERBS), key=len, reverse=True)) + r")[ \t]+(\S.*)$")
_NOTE_HEAD = re.compile(r"^备注[ \t]+(\S.*)$")
_NOTE_COLON = re.compile(r"^([^:：\n]+?)\s*[:：]\s*(.*)$")   # 「备注 小美：只发链接」 (the part after 备注)
_CHOICE = re.compile(r"^选\s*(\d{1,3})$")
_ENDING = re.compile(r"[！!～~]*。?[！!～~]*$")        # a command's ending (wechat_review ``_COMMAND_END``)
_HESITATING = "。.…．｡"
_ASKING_END = ("吗", "呢", "么", "嘛", "没", "没有")
_ASKING_IN = ("怎么", "怎样", "什么", "为什么", "为啥", "啥", "如何", "多久", "多少", "哪些", "哪个", "哪里", "是不是",
              "能不能", "要不要", "可不可以")
_SEPARATORS = re.compile(r"[\s\-－_—–·・:：]+")
_PREFIX = re.compile(r"^\s*(?:抖音|小红书|淘宝|快手|视频号)\s*[-－_—:：·]+\s*")   # the friend add's remark (抖音-…)
_VOICE = re.compile(r"^\[语音\]\s*(.*)$", re.S)
_QUOTE_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9]+:\s*")

HELP_TEXT = (
    "合作单指令（只在管理员私聊里打字发；名字写她在名单里的名字，写全了直接做，只写一部分时先列出来让你回「选 编号」）：\n"
    "做手卡 名字 — 按她最近一张寄样单的款做手卡，做好进「待审批」等你看\n"
    "补发货盘 名字 — 写一份补发货盘的草稿，进「待审批」等你看\n"
    "选款链接 名字 — 给她建一个选款网站的链接，带链接的那句话进「待审批」等你看（要先打开选款网站）\n"
    "写开场白 名字 — 写一份开场白，进「待审批」等你看\n"
    "签收 名字 — 合作单标「已签收」（和控制台「更新物流 → 已签收」一样）\n"
    "备注 名字 文字 — 换掉她的起草备注（「备注 名字 清空」清掉，「备注 名字 撤销」放回刚才换掉的，「备注 名字」看现在的）\n"
    "接管 名字 / 副驾驶 名字 — 人工接管 / 恢复副驾驶\n"
    "合作单 名字 — 看她的合作单现在怎样（机器人主动发的发到哪、为什么）\n"
    "接群、确认接群、不接群这些群的指令和别的管理命令：发「帮助」看\n"
    "要交给 Claude 的话前面加 cc"
)
# One line for the admin's 「帮助」 (src/admin.py ``help_text``: among its commands) while the switch is on, and the
# relay's help (src/bot.py; CLAUDE_RELAY_ONLY), whose lines are written without the indent (merge 2026-11-11)
HELP_LINE = "\n  做手卡 / 补发货盘 / 选款链接 / 写开场白 / 签收 / 备注 / 接管 / 副驾驶 / 合作单 + 名字   合作单指令（发「指令」看用法）"
RELAY_LINE = "\n做手卡 / 补发货盘 / 选款链接 / 写开场白 / 签收 / 备注 / 接管 / 副驾驶 / 合作单 名字（合作单指令：发「指令」看用法）"
VOICE_TEXT = "合作单指令要打字发（语音不算，免得听错）：比如「{example}」"
QUOTE_TEXT = "引用的消息不算合作单指令：不引用，直接打字发「{example}」"
OTHER_TEXT = "只认打字发的合作单指令：比如「{example}」"
TOO_OLD_TEXT = "这条合作单指令是 {minutes} 分钟前写的（微信这会儿才读到），太久了，没做：要做的话请再发一次"
MORE_TEXT = "一条消息只放一个合作单指令、一行写完（备注的文字也写在同一行）：都没处理，请分开发"
SHORT_TEXT = "名字至少写两个字（她在名单里的名字，写一部分也行）：没做"
LONG_TEXT = f"名字太长了（最多 {NAME_MAX} 个字，只写她在名单里的名字）：没做"
CC_HINT = "（要交给 Claude 请在前面加 cc）"
ERROR_TEXT = "处理合作单指令的时候出错了（详见日志）：请到控制台看一下"
NO_UNDO_TEXT = f"没有能撤销的起草备注（只能撤销 {UNDO_SEC // 3600} 小时内用「备注」换掉的最后一次，撤销一次）"
# 「备注 名字 <words that ask or hesitate>」 (the blank form): nothing written; {name} the name as typed (masked)
QUESTION_TEXT = "这句像是在问话，没改备注：要写成备注请用冒号「备注 {name}：…」"
QUESTION_CC = "；要问 Claude 请在前面加 cc"


@dataclass(frozen=True)
class Typed:
    """A command line (``parse``): the word's action (``VERBS``, or ``help``), the name as typed, 备注's words (None: none
    given; 「清空」: ""), whether the message has more lines (refused whole), and for 备注 every way the line reads
    (``readings``: ``(name, words, colon)``, the one taken first; ``colon``: 「名字：文字」) and whether it would be no
    command but for her name (``asking``: a question or a hesitation)."""

    verb: str
    name: str = ""
    text: Optional[str] = None
    more: bool = False
    word: str = ""                   # the word as typed (「恢复副驾驶」, 「副驾驶」 …): what the answers repeat
    readings: tuple = ()
    asking: bool = False


@dataclass
class Found:
    """Who a name typed matches (``DealCommands._find``): ``(party, deal)`` pairs of live deals whose 称呼 is it
    (``whole``) or holds it (``piece``); of deals not live (a stopped creator, a deal cleaned up): whole and any; the live
    names by their key (for the close ones)."""

    whole: list = field(default_factory=list)
    piece: list = field(default_factory=list)
    dead_whole: list = field(default_factory=list)
    dead: list = field(default_factory=list)
    keys: dict = field(default_factory=dict)

    @property
    def live(self) -> bool:
        return bool(self.whole or self.piece)


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


def _echo(text) -> str:
    """What the admin typed, as an answer repeats it: one line, phone / ID numbers and labelled 微信号 masked, ECHO_CHARS
    at most."""
    return contact.clean_name(text, ECHO_CHARS)


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
    """备注's words as kept: None for none, "" for 「清空」 (its ending aside), else as typed (撤销 too: ``_note``)."""
    words = str(words or "").strip()
    if not words:
        return None
    return "" if _ended(words) in CLEAR_WORDS else words


def _note_readings(rest: str) -> tuple:
    """Every way 「备注 <rest>」 reads, ``(name, words, colon)``: 「名字：文字」 first (a colon after the name, which may hold
    spaces: ``colon`` True), then the name up to each blank in turn, the words after it (the whole: no words, the note
    shown)."""
    out: list = []
    colon = _NOTE_COLON.fullmatch(rest)
    if colon is not None and _key(colon.group(1)):
        out.append((colon.group(1).strip(), _note_words(colon.group(2)), True))
    for m in re.finditer(r"\S+", rest):
        name, words = rest[:m.end()].strip(), rest[m.end():].strip()
        out.append((name, _note_words(words), False))
    seen, kept = set(), []
    for name, words, by_colon in out:
        if (_key(name), words) not in seen and _key(name):
            seen.add((_key(name), words))
            kept.append((name, words, by_colon))
    return tuple(kept)


def parse(text) -> Optional[Typed]:
    """The command ``text`` is (pure; see the module's doc): ``Typed`` or None.  Only its first line is read; more
    lines make it ``more`` (refused whole by ``DealCommands``).  A 备注 line is one whatever it says, its ``readings``
    for ``DealCommands`` to try and ``asking`` when it would not read as a command (then it is one only if a reading's
    name matches a live creator); the words keep their own ending."""
    lines = str(text or "").replace("　", " ").strip().split("\n")
    first, more = lines[0].strip(), any(line.strip() for line in lines[1:])
    bare = _ended(first)
    note = _NOTE_HEAD.fullmatch(first)
    if note is not None:
        readings = _note_readings(note.group(1).strip())
        if not readings:
            return None
        name, words, _ = readings[0]
        return Typed(NOTE, name, words, more, "备注", readings, asking=bare is None)
    if bare is None:
        return None
    if bare in HELP_WORDS:
        return Typed("help", more=more, word=bare)
    m = _VERB.fullmatch(bare)
    if m is None or VERBS[m.group(1)] == NOTE:
        return None
    return Typed(VERBS[m.group(1)], m.group(2).strip(), None, more, m.group(1))


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
    ``deal_action``, ``wechat_review`` for her 称呼 as the admin's chat shows it, ``_local`` for WeChat's times).
    ``on``: PRM_WECHAT_COMMANDS."""

    help_line = HELP_LINE                          # the admin's 「帮助」 names the commands in it (src/admin.py)
    trace_text = TRACE_TEXT                        # the decision trace keeps this of such a line (src/routing/steps.py)

    def __init__(self, service, config=None, *, clock: Callable[[], float] = time.time):
        self._service = service
        self._clock = clock
        # (never on the Mac's edge side, WECHAT_BACKEND=edge: the admin's chat is read on Windows there, as 微信里审批草稿's)
        self.on = switch_on(config) and str(getattr(config, "wechat_backend", "") or "") != "edge"
        self._admin = str(getattr(config, "admin_wxid", "") or "").strip()
        self._relay_only = str(getattr(config, "claude_relay_only", "off") or "off").strip().lower() == "on"
        self._lock = threading.Lock()              # one command at a time
        # the last list of namesakes: {"at", "typed", "deals": [deal id], "done", "closed"} (「选 N」 picks from it)
        self._asked: Optional[dict] = None
        # 「备注 名字 撤销」: per deal, the note a 备注 replaced and what it wrote, when (UNDO_SEC, once)
        self._undo: dict[int, dict] = {}

    # ── the admin's text (src/admin.py ``_command``) ─────────────────────

    def admin_command(self, text, msg: Optional[dict] = None) -> Optional[Callable[[], str]]:
        """The command ``text`` is (``run()`` answers it), or None (it goes on as it is).  ``msg``: the admin's message
        (src/routing/steps.py ``admin_command``): only typed text in the admin's own one-to-one chat, written lately,
        runs one; a voice message or a quote with a command's words is answered that it must be typed, an old line that
        it was too old; anybody else's, nothing.  Without ``msg`` (the relay asking whether a line is a command:
        ``AdminCommandHandler.is_command``) only the words count.  「选 N」 only while a list is open or was lately
        (ASKED_KEEP_SEC)."""
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
        if typed is not None and typed.verb == NOTE and typed.asking and not self._note_named(typed):
            typed = None                               # (a question naming nobody: no command, it goes on)
        if typed is None:
            match = _CHOICE.fullmatch(_ended(content.strip()) or "") if "\n" not in content.strip() else None
            asked = self._asked
            if match is None or asked is None or self._clock() - float(asked["at"]) > ASKED_KEEP_SEC:
                return None
            example = f"选 {match.group(1)}"
            number = int(match.group(1))
            run = lambda: self._run(self._choose, number)      # noqa: E731
        else:
            example = self._example(typed)
            run = lambda: self._run(self._command, typed)      # noqa: E731
        age = self._age(msg)
        if age is not None:
            return lambda: self._too_old(age)
        if how:
            return lambda: self._refusal(how, example)
        return run

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

    def _age(self, msg: Optional[dict]) -> Optional[float]:
        """How long ago the message was written when that is more than MAX_AGE_SEC (WeChat's time on this PC's clock,
        ``PrmService._local``, as the review's answers: a line WeChat read late, after a re-login, is history); else
        None."""
        try:
            written = float((msg or {}).get("timestamp") or 0)
        except (TypeError, ValueError):
            return None
        if not written:
            return None
        local = getattr(self._service, "_local", None)
        age = self._clock() - (local(written) if callable(local) else written)
        return age if age > MAX_AGE_SEC else None

    def _too_old(self, age: float) -> str:
        logger.info("PRM: 合作单指令: a command line read %.0f s after it was written; not run", age)
        return TOO_OLD_TEXT.format(minutes=max(1, int(round(age / 60))))

    @staticmethod
    def _example(typed: Typed) -> str:
        """The command as an answer repeats it: the word and the name (masked), never a note's words."""
        if typed.verb == "help":
            return typed.word
        return f"{typed.word} {_echo(typed.name)}" + (" …" if typed.verb == NOTE and typed.text is not None else "")

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
        """A command line: refused whole with more lines; 「指令」: the list; else her deal by name (``_find``): a whole
        name done at once (namesakes listed), a stopped creator's or a cleaned deal's whole name said so, a piece of
        names listed (for 「合作单」 and showing a 备注, one done at once), none: the closest names.  Any earlier list is
        closed (its 「选 N」 runs nothing now)."""
        if typed.more:
            return MORE_TEXT
        if typed.verb == "help":
            return HELP_TEXT
        if self._asked is not None and not self._asked["done"]:
            self._asked["closed"] = True
        found = None
        if typed.verb == NOTE:
            typed, found, by_colon = self._note_reading(typed)
            if found is not None and found.live and typed.asking and not by_colon and typed.text is not None:
                logger.info("PRM: 合作单指令 note: the blank form reads as a question; nothing written")
                return QUESTION_TEXT.format(name=_echo(typed.name)) + (QUESTION_CC if self._relay_only else "")
        size = len(_key(typed.name))
        if size < NAME_MIN:
            return SHORT_TEXT
        if size > NAME_MAX:
            return LONG_TEXT + (CC_HINT if self._relay_only else "")
        found = found or self._find(typed.name)
        changes = typed.verb != STATUS and not (typed.verb == NOTE and typed.text is None)
        if len(found.whole) == 1:
            return self._do(typed, *found.whole[0])
        if found.whole:
            return self._list(typed, found.whole, whole=True)
        if found.dead_whole or (not found.piece and found.dead):
            return self._dead(typed, found.dead_whole or found.dead)
        if len(found.piece) == 1 and not changes:
            return self._do(typed, *found.piece[0])
        if found.piece:
            return self._list(typed, found.piece, whole=False)
        close = self._close(typed.name, found)
        logger.info("PRM: 合作单指令 %s: no deal matches (%d close)", typed.verb, len(close))
        head = f"没有达人对得上「{_echo(typed.name)}」（只找名单里在用的达人）"
        if close:
            head += "；是不是：" + "、".join(self._shown(p, d) for p, d in close) + "？"
        return head + (CC_HINT if self._relay_only else "")

    def _note_named(self, typed: Typed) -> bool:
        """Does a reading of this 备注 line name a live creator (``_find``)?"""
        return any(NAME_MIN <= len(_key(name)) <= NAME_MAX and self._find(name).live for name, _, _ in typed.readings)

    def _note_reading(self, typed: Typed) -> tuple:
        """备注: ``(typed, found, colon)``: the reading whose name matches a live creator, the longest such name (her name
        with its spaces whole: no piece of it in the note), what it matched and whether it is the colon form; a stopped
        creator's or a cleaned deal's whole name longer than that one (or with no live one): that reading and only what
        is not live (``_command`` refuses it so: never a shorter live name instead, review 2026-11-11 N1); none: the line
        as first read, None."""
        best = dead = None
        for name, words, by_colon in typed.readings:
            size = len(_key(name))
            if not NAME_MIN <= size <= NAME_MAX:
                continue
            found = self._find(name)
            if found.live and (best is None or size > len(_key(best[0]))):
                best = (name, words, found, by_colon)
            if found.dead_whole and (dead is None or size > len(_key(dead[0]))):
                dead = (name, words, found, by_colon)
        if dead is not None and (best is None or len(_key(dead[0])) > len(_key(best[0]))):
            name, words, found, by_colon = dead
            return replace(typed, name=name, text=words), Found(dead_whole=found.dead_whole, dead=found.dead), by_colon
        if best is None:
            return typed, None, typed.readings[0][2] if typed.readings else False
        name, words, found, by_colon = best
        return replace(typed, name=name, text=words), found, by_colon

    def _dead(self, typed: Typed, rows: list) -> str:
        """The name matches only a stopped creator or a cleaned deal: said so, the console's."""
        asked = _echo(typed.name)
        logger.info("PRM: 合作单指令 %s: the name matches %d deal(s) not live", typed.verb, len(rows))
        if len(rows) == 1:
            party, deal = rows[0]
            if not party.get("active"):
                return f"「{asked}」是停用的达人（不在达人名单里）：没做，请到控制台看"
            return f"「{asked}」的合作单收件信息已经按规定清过了：没做，请到控制台看"
        return f"对得上「{asked}」的只有停用的达人，或者收件信息清过的合作单：没做，请到控制台看"

    def _close(self, name: str, found: Found) -> list:
        """The CLOSE_LISTED live deals whose names are closest to ``name`` (difflib, CLOSE_CUTOFF)."""
        close, seen = [], set()
        for k in difflib.get_close_matches(_key(name), list(found.keys), n=CLOSE_LISTED * 2, cutoff=CLOSE_CUTOFF):
            party, deal = found.keys[k]
            if party["id"] not in seen:
                seen.add(party["id"])
                close.append((party, deal))
        return close[:CLOSE_LISTED]

    def _list(self, typed: Typed, rows: list, *, whole: bool) -> str:
        """Namesakes, or names holding the piece typed: numbered (LISTED at most), 「选 N」 runs the command once for the
        N-th (CHOICE_SEC)."""
        listed = rows[:LISTED]
        self._asked = {"at": self._clock(), "typed": typed, "deals": [int(d["id"]) for _, d in listed], "done": False,
                       "closed": False}
        logger.info("PRM: 合作单指令 %s: %d deals match (%s), %d listed", typed.verb, len(rows),
                    "whole name" if whole else "a piece", len(listed))
        asked, doing, valid = _echo(typed.name), self._doing(typed), f"（{CHOICE_SEC // 60} 分钟内有效）"
        if whole:
            head = f"有 {len(rows)} 位达人都叫「{asked}」：回「选 编号」{doing}{valid}"
        elif len(rows) == 1:
            head = f"「{asked}」只是名字的一部分，对得上 1 位：是她就回「选 1」{doing}{valid}"
        else:
            head = f"有 {len(rows)} 位达人的名字里有「{asked}」：回「选 编号」{doing}{valid}"
        if len(rows) > LISTED:
            head += f"；只列了前 {LISTED} 位，不在里面的话名字写全一点再发"
        lines = [head]
        for n, (party, deal) in enumerate(listed, 1):
            lines.append(f"{n}. {self._shown(party, deal)} · {STAGES.get(deal['stage'], deal['stage'])} · "
                         f"合作单 {deal['id']} 号")
        return "\n".join(lines)

    @staticmethod
    def _doing(typed: Typed) -> str:
        if typed.verb == NOTE and typed.text is None:
            return "看备注"
        return {HANDCARDS: "做手卡", HUOPAN: "补发货盘", OPENING: "写开场白", SIGN: "标签收", NOTE: "改备注",
                TAKEOVER: "人工接管", BACK: "恢复副驾驶", STATUS: "看合作单", SITE_LINK: "发选款链接"}.get(typed.verb, "")

    def _choose(self, number: int) -> str:
        """「选 N」: the open list's N-th deal, the command run once."""
        asked = self._asked
        if asked is None:
            return "现在没有在等你选的合作单指令"
        typed = asked["typed"]
        again = f"重新发「{self._example(typed)}」"
        if asked["done"]:
            return f"刚才那个「选」已经回过了：要再来一次请{again}"
        if asked.get("closed"):
            return f"刚才那个列表作废了（后来又发了别的合作单指令）：要的话{again}"
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

    def _find(self, name: str) -> Found:
        """Who ``name`` matches (``Found``): each creator's newest deal, live (on the list, not cleaned up: 8.2) or not,
        by her 称呼 with and without the platform's prefix (``_key``), whole or holding it."""
        store = self._service.store
        newest: dict = {}
        for deal in store.deals():
            newest[deal["party_id"]] = deal
        want = _key(name)
        found = Found()
        for party in store.parties(include_inactive=True):
            deal = newest.get(party["id"])
            if deal is None:
                continue
            names = {_key(party["name"]), _key(_PREFIX.sub("", str(party["name"] or "")))} - {""}
            live = bool(party.get("active")) and not deal.get("purged_at")
            whole = want in names
            piece = not whole and any(want in k for k in names)
            if not live:
                if whole:
                    found.dead_whole.append((party, deal))
                if whole or piece:
                    found.dead.append((party, deal))
                continue
            if whole:
                found.whole.append((party, deal))
            elif piece:
                found.piece.append((party, deal))
            for k in names:
                found.keys.setdefault(k, (party, deal))
        return found

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
                   NOTE: self._note, TAKEOVER: self._takeover, BACK: self._back, STATUS: self._status,
                   SITE_LINK: self._site_link}[typed.verb]
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
        return f"已派做手卡的任务（{which}），做好会进「待审批」，要你看过才发"

    def _huopan(self, typed: Typed, party: dict, deal: dict) -> str:
        """「补发货盘」: the console's; its draft always waits for a person (``PrmService._ask_huopan``)."""
        out = self._action("huopan", {"deal_id": deal["id"]})
        if not out.get("ok"):
            return self._refused(party, deal, out, "派补发货盘")
        return "已派补发货盘的任务，写好会进「待审批」，要你看过才发"

    def _site_link(self, typed: Typed, party: dict, deal: dict) -> str:
        """「选款链接」: her link of the 选款网站 in a fixed sentence (``SampleSite.ask_link``); its draft always waits for
        a person."""
        out = self._action(SITE_LINK, {"deal_id": deal["id"]})
        if not out.get("ok"):
            return self._refused(party, deal, out, "派发选款链接")
        return "已派发选款链接的任务，带链接的那句话会进「待审批」，要你看过才发"

    def _opening(self, typed: Typed, party: dict, deal: dict) -> str:
        """「写开场白」: the console's, but its draft never goes by itself (``meta.opening.held``: no PRM_OPENING_AUTO_MIN,
        下班时段 or 自动模式; ``PrmService._ask_opening``, ``_opening_auto``)."""
        out = self._action("opening", {"deal_id": deal["id"]})
        if not out.get("ok"):
            return self._refused(party, deal, out, "派写开场白")
        return "已派写开场白的任务，写好会进「待审批」，要你看过才发"

    def _has_samples(self, deal: dict) -> bool:
        """A sample order of her deal: one the 聚水潭 sync keeps, or a verified one the bot made (``sample_items``)."""
        from .service import sample_items                 # (the service imports this module)
        store = self._service.store
        return bool(store.sample_orders(deal_id=deal["id"])) or sample_items(store, deal["id"]) is not None

    def _sign(self, typed: Typed, party: dict, deal: dict) -> str:
        """「签收」: the console's 「更新物流 → 已签收」 on her one waybill (``shipment_status`` delivered), or without one
        its 「改阶段 → 已签收」 (``stage`` delivered) for a deal at a sample stage or with a sample order.  Two waybills or
        more, a deal past 已签收, one 已签收 already, or one with neither a waybill nor samples: the console's."""
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
        if ship is None and deal["stage"] not in SAMPLE_STAGES and not self._has_samples(deal):
            return f"没标：合作单是「{stage}」，没有运单号也没有寄样单：要标签收请到控制台"
        if ship is not None:
            out = self._action("shipment_status", {"deal_id": deal["id"], "status": "delivered"})
            done = f"标成「已签收」了（{ship['carrier']} {_tail(ship['waybill'])}）"
        else:
            out = self._action("stage", {"deal_id": deal["id"], "stage": "delivered"})
            done = "合作单没有运单号，阶段改成了「已签收」"
        if not out.get("ok"):
            return self._refused(party, deal, out, "标签收")
        return done + "；签收以后该做的（比如专属佣金）和控制台点「已签收」一样起草，在「待审批」里等你批"

    def _note_shown(self, party: dict, deal: dict, text: str) -> str:
        """A 起草备注 as the admin's chat shows it: one line (「 / 」 between its lines), her ids, addresses and numbers
        masked (``_hide``), 「（没有）」 when there is none."""
        if not text:
            return "（没有）"
        return contact.mask_contacts(self._hide(party, deal, " / ".join(text.split("\n"))))

    def _note(self, typed: Typed, party: dict, deal: dict) -> str:
        """「备注」: the console's 起草备注 replaced (「清空」: cleared; the answer shows what it replaced); 「撤销」: the
        note this command replaced last put back (``_undo``); no words: the note as it is."""
        before = str(deal.get("draft_note") or "")
        if typed.text is None:
            if not before:
                return "还没有起草备注（「备注 名字 文字」写一条）"
            return f"起草备注（{len(before)} 字）：{self._note_shown(party, deal, before)}"
        if _ended(typed.text) in UNDO_WORDS:
            return self._undo_note(party, deal, before)
        out = self._action("draft_note", {"deal_id": deal["id"], "text": typed.text})
        if not out.get("ok"):
            return self._refused(party, deal, out, "改起草备注")
        note = str(out.get("draft_note") or "")
        if note == before:
            return "起草备注本来就是这样，没改"
        now = self._clock()
        self._undo = {k: v for k, v in self._undo.items() if now - v["at"] <= UNDO_SEC}
        self._undo[int(deal["id"])] = {"before": before, "after": note, "at": now}
        undo = "（「备注 名字 撤销」能放回原来的）" if before else ""
        if not note:
            return f"起草备注清掉了。原来：{self._note_shown(party, deal, before)}{undo}"
        return (f"起草备注换好了。原来：{self._note_shown(party, deal, before)} 现在：{self._note_shown(party, deal, note)}"
                f"{undo}")

    def _undo_note(self, party: dict, deal: dict, now_note: str) -> str:
        """「备注 名字 撤销」: the note the last 备注 of this deal replaced, put back once, within UNDO_SEC, unless the
        note changed since (in the console, say)."""
        entry = self._undo.get(int(deal["id"]))
        if entry is None or self._clock() - entry["at"] > UNDO_SEC:
            self._undo.pop(int(deal["id"]), None)
            return NO_UNDO_TEXT
        if now_note != entry["after"]:
            return "起草备注在那以后又改过了（不是这条指令改的），没撤销：请到控制台看"
        out = self._action("draft_note", {"deal_id": deal["id"], "text": entry["before"]})
        if not out.get("ok"):
            return self._refused(party, deal, out, "撤销起草备注")
        self._undo.pop(int(deal["id"]), None)
        return f"起草备注放回去了：{self._note_shown(party, deal, str(out.get('draft_note') or ''))}"

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
        """「合作单」: read only (nothing changes, nothing recorded).  Drafts by their numbers as the review's notices
        show them (「#12」); the deal's in words (「合作单 7 号」)."""
        store, now = self._service.store, self._clock()
        mode = deals.effective_mode(deal, now)
        mode_text = MODES.get(mode, mode)
        if mode == HUMAN and deal.get("mode_until"):
            mode_text += f"（到 {_clock_text(deal['mode_until'], now)}）"
        lines = [f"{self._shown(party, deal)}（合作单 {deal['id']} 号）：阶段「{STAGES.get(deal['stage'], deal['stage'])}」"
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
        lines.extend(self._unconfirmed(party, deal))
        lines.append(self._waiting(party, deal))
        note = str(deal.get("draft_note") or "")
        if note:
            lines.append(f"起草备注：有（{len(note)} 字，「备注 名字」看）")
        return "\n".join(lines)

    def _where(self, party: dict, deal: dict) -> str:
        """Where her drafts go: her private chat, the groups linked to her deal (her lines there are answered there),
        her 三方群 (read only), and where what the bot starts itself goes, and why (``_started``)."""
        from .boss_groups import LINKED                   # (the service imports this module)
        service, store = self._service, self._service.store
        groups = [self._hide(party, deal, r.get("name") or "（没有群名）")
                  for r in store.boss_groups((LINKED,), deal_id=int(deal["id"])) if service.boss_groups.is_linked(r["chat"])]
        parts = [("她在私聊和群「" + "」「".join(groups) + "」里说的话，在哪说就回哪") if groups else "私聊",
                 self._started(party, deal)]
        three = deal.get("three_party") or {}
        if three.get("state") == "created" and three.get("chat") and not service.boss_groups.is_linked(three["chat"]):
            parts.append(f"三方群「{self._hide(party, deal, three.get('name') or '')}」机器人只读")
        return "；".join(parts)

    def _started(self, party: dict, deal: dict) -> str:
        """Where what the bot starts itself for her goes now, and why: the very decision its sends take
        (``PrmService._reaching`` with ``made``, as the console's 写开场白 / 补发货盘 / 做手卡, a 专属佣金 step, a shipping
        notice aimed the first time, 签收后问一句; merge 2026-11-11: PRM_REACH_LAST_SPOKE may send them to the group where
        her side last spoke although she has a private chat).  The reasons are the console's (``_reach_out``)."""
        from .boss_groups import REACH_LAST_SPOKE        # (the service imports this module)
        service = self._service
        what = "机器人主动发的（开场白、货盘、手卡、发货通知）"
        chat, group, why = service._reaching(party, made=True, deal=deal)
        if chat is None:
            return f"{what}发不出去：她不是好友，也不在接手的群里了"
        if group is not None:
            reason = "她那边最近是在这个群里说的话" if why == REACH_LAST_SPOKE else "她没有和我们的私聊"
            return f"{what}发到群「{self._hide(party, deal, group.get('name') or '（没有群名）')}」（{reason}）"
        if not service.boss_groups.party_has_group(party["id"]):
            return f"{what}发私聊"
        if service._private_known(party) is None:
            # (the backend cannot say now whether she has a private chat: the deal page's reach "unknown"; the sends
            # take her private chat meanwhile, never a group for that, and nothing approved moves unseen)
            return f"{what}现在说不准发到哪：机器人现在读不出她有没有私聊，先按私聊算"
        if not getattr(service, "_reach_last_spoke", False):
            return f"{what}发私聊"
        latest = service._last_spoken(party, deal)
        reason = ("她还没说过话" if not latest else "她最近说话的那个群机器人现在不在里面起草" if latest.endswith("@chatroom")
                  else "她最近是在私聊里说的话")
        return f"{what}发私聊（{reason}）"

    def _unconfirmed(self, party: dict, deal: dict) -> list:
        """群自己接上 (PRM_GROUPS_AUTO_LINK; merge 2026-11-11): one line per group of her deal the scan linked by itself that
        nobody confirmed yet (``BossGroups.unconfirmed_of``): every draft there waits for that, and how to confirm it.
        The group's name as the scan's own notice shows it (``WechatReview.shown`` with her party, PrmService
        ``_auto_link_notice``); 「确认接群 <群名>」 only when that name is one the command finds (not masked, not cut
        short), else the console's 「是她的群」."""
        from .boss_groups import NAME_CHARS               # (the service imports this module)
        review, out = self._service.wechat_review, []
        for row in self._service.boss_groups.unconfirmed_of(int(deal["id"])):
            raw = " ".join(str(row.get("name") or "").split())
            name = review.shown(raw, party)
            exact = bool(name) and name == raw and len(raw) < NAME_CHARS     # (a kept name of NAME_CHARS: maybe cut)
            group = f"群「{name}」" if name else "一个没有群名的群"
            how = f"回「确认接群 {name}」" if exact else "到控制台她的合作单上点「是她的群」"
            out.append(f"{group}是机器人自动接的，还没人确认是她的群：那里的草稿都等你看；对的话{how}")
        return out

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
