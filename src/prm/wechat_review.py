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
console's legend), a shipping notice's waybill, its files, and how to answer.  A draft with a platform action lists the
action with every line its preview has (``_platform_lines``: what will be made, for whom, with which values; a sample
order's receiver only by province and city; the creator's platform ids, and the ids of the preview's plan, by their last
characters, in the problems too; the preview's verdict and problems), or says how many lines are left out (more than
LINES_MAX, or more than fit in NOTICE_CHARS, the relay's send limit: then it cannot run from WeChat); the pictures the
operator is meant to see (``meta.operator_files``: the relay's own files) follow it.  None for what goes out by itself
(a kind's gate ``auto``, 下班时段 and 自动模式 approve it at once: never pending; 开场白自动发: ``meta.auto_at`` while
``send_due_openings`` still takes it), a 百应 draft (``BUYIN_TOOL``: copied into 百应 by hand), a holding reply (no
packet), anything while ADMIN_WXID is empty (``status`` says why), nor a draft that was waiting already before the
switch was first on (``ON_AT`` in prm_meta, less BACKLOG_SEC: the console counts those; a run with the switch off
forgets ON_AT, the next switch-on starts afresh).  Each draft's row is written before its send (``telling``) and marked
told after it: a send cut off between the two is never sent again, the console says it may have gone out.  PER_ROUND
sends a round at most; a send WeChat does not take ends the round and the admin's chat is tried again 10 s later,
doubling, at most 2 minutes apart, each message 6 times in all (as the relay's approval requests, src/relay/service.py
``_send_asks``); no draft ever waits for its notification.

Answers (``answer``, the route step ``wechat_review``, before the relay or an admin command can take them): typed text
in the admin's own private chat, never a voice message (heard wrong, a 「不发」 could send: the bot says to type).  A
word that does something (1 / 2 / 发 / 不发 / 执行 / 好的 …, numbered or not) ends in nothing, a single 「。」, or one or
more of 「！!～~」 (``_command_word``, ``_END``): with 「.」「…」「...」「。。」 after it (hesitating, not finished) it does
nothing.
- Quoting a notification (WeChat 引用; ``quoted_content`` starts with 「#12 草稿」): 「1」「发」「发送」「好」「好的」「可以」「行」
  「ok」「同意」「嗯」「[强]」「👍」 send the draft as it was drafted (a proposed edit is dropped), 「2」「不发」「不要」「算了」 drop
  it, 「改 <你的话>」 sends those words instead (the console's 改了再发 checks: ``PrmService.edit_problem``); other words
  are kept as the proposed edit and the bot asks 「#12 改成：『…』 回 1 发出／2 不发」.  Never a proposal: a line for the
  relay (``cc …``; an admin command with other words refuses the whole message, as without a quote), another approval
  word, emoji only, a lone number, a text built around a yes (``_yes_like``: a short one, 「好的呀」「发吧」「没问题」…, or
  one made only of yes words, 「好好好好好好好好好」), any text of at most SHORT_CHARS letters (``_short``: 「妥」「冲」「安排」
  「go」「就按这个」…): the bot says how to answer; a short one starting with 改 (「改一下」) asks what to.
- Quoting that question: a yes word sends the proposal, only when the 『…』 quoted is the current one (else the bot shows
  the current one, as a question of its own).  A number in the words that is not the quoted draft's (「改 15 …」「发 15」
  「执行 15」 quoting #12) does nothing: the bot asks which.  A question about 执行 quoting it (「执行了吗」「执行了没」「要执行
  吗」「执行？」…: ``_execute_parts``) says what became of that draft (``_execute_status``), never runs it.
- Without a quote: 「发 12」 (the original draft), 「不发 12」 and 「执行 12」 for a draft told here, 「改 12 <你的话>」 for one
  still waiting (one told here and closed: what became of it); anything else passes on.  A bare 「1」 / 「2」 answers only
  the last thing the bot said in this chat (``note_sent``: the backend tells it every text it sent) when that was a
  notification or a 改成 question shown before the answer was written (on WeChat's clock: ``note_read_back``), its draft
  the only one told here that still waits (else the bot lists them), never in CLAUDE_RELAY_ONLY (it may be Claude's),
  never while a relay job of this chat runs or ended within RELAY_QUIET_SEC, never right after another told draft was
  closed in the console (CLOSED_RACE_SEC).  A bare one after the bot said something else meanwhile gets a hint.  A
  「执行」 alone or with words around it (``_execute_parts``: 「执行吧」「执行一下」「可以执行」…) is never run nor passed on
  while a platform draft told here waits or was the last thing said: the bot asks which (in either mode).  A question
  about 执行 (「执行了吗」「执行了没」「执行吗」「执行？」「执行好了吗」「要执行吗」…, 「执行 12 了吗」) says what became of the
  draft (its number) or of the platform drafts told here that wait or were told recently (``_status_rows``, at most
  STATUS_LISTED), never runs one; with none of them it is not the review's.  An unnumbered 「改 …」 while a notification
  is the last thing said: the bot asks which (with a relay job of this chat under way or just ended: 「要交给 Claude 请在
  前面加 cc」), except in CLAUDE_RELAY_ONLY (it is Claude's: it goes on).  In CLAUDE_RELAY_ONLY a 「执行」-like answer
  quoting another message (Claude's, say: the relay takes no quote there) gets one line saying so (QUOTED_ELSEWHERE).
- A draft for a 老板拉的群 is sent from WeChat only by an answer quoting its notification or question (the console's
  group confirm dialog); 「不发」 needs no quote.
- A platform action runs only by 「执行」 quoting its notification, or 「执行 12」 (the console's 批准并执行,
  ``PrmService.decide`` "execute", every check of it), and only the preview its notification showed (``preview_stamp``,
  checked again by ``decide`` in the step that moves the draft: one it said cannot run, or one previewed again since, is
  the console's); 1 / 好 / 发 never run one, 「改」 of its reply stays the console's.  When the execution ends (the reply
  sent after VERIFIED_SUCCESS, uncertain, or not done; a result that comes late too) the admin is told in one line.  A
  job the relay raised on: run when it is there all the same, 「可以再回一次」 only when it certainly is not, else red as
  not known (``PrmService._approve_execute``: QUEUE_UNSURE, its result told when it comes late).
- Several lines at once (WeChat merges bubbles sent within 2.2 s, src/wechat/burst.py): each answer line is done, an edit
  in a merged burst only as a proposal; the lines that are no answer go on to the later route steps as one message
  (``rest``), a cc line first (the relay reads the message's start); an admin command among other lines, or two or more
  cc / command lines: the whole message is refused (one line asks to send them separately).
Each answer gets one reply (approved and where it goes, already handled, out of date and what replaced it, the console's
own refusal); a draft approved here that then fails to send is told in one line; a send the bot was stopped in the
middle of (``indeterminate``) is told as unsure UNSURE_GRACE_SEC after the start, unless its read-back settled it (then
nothing: it went out), never as 「没发出去」.  A decision here and one in the console are the same decision: the packet's
compare-and-set moves (``decide``) let only the first through, the second hears it was handled.

Nothing of it while the switch is off (no notification, no row, the route step passes every message on), nor on the
Mac's edge side (WECHAT_BACKEND=edge: the admin's chat is read on Windows there).  The log has packet ids and counts
only: never a creator's name or words, never a draft.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
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
EXECUTE_WORDS = ("执行",)                                         # ... run its platform action (and send its reply)
YES_STEMS = ("发", "好", "行", "可", "同意", "确认", "对", "是", "嗯", "ok", "照", "没问题", "中")   # a yes, not words for her
YES_CHARS = 8               # ... in a text this short (punctuation and emoji left out): never a proposed edit
SHORT_CHARS = 4             # quoting a notification: a text this short (「妥」「冲」「安排」「go」「就按这个」) is never a proposed edit
RETRY_SEC = (10, 120)       # a send the admin's chat did not take: the chat again after 10 s, doubling, at most 2 min apart
SEND_TRIES = 6              # ... and a message not again (this run) after this many failed sends (about 4.5 minutes)
PER_ROUND = 3               # sends one round of the sender thread makes at most (a pile waits its turn)
BACKLOG_SEC = 12 * 3600     # the switch first on: drafts that started waiting this long before it are told, older not
ON_AT = "wechat_review_on_at"   # prm_meta: when the switch was first seen on
RELAY_QUIET_SEC = 600       # a bare 「1」 / 「2」 never counts while a relay job of the admin chat runs or ended this recently
CLOSED_RACE_SEC = 60        # ... nor when another told draft was closed in the console this recently
CLOCK_GUESS_SEC = 60        # ... nor written less than this after a notification WeChat's time of which is not known
BARE_MARGIN_SEC = 2         # ... (with this PC's clock measured against WeChat's: this much, a read-back's own delay)
HINT_SEC = 600              # a bare 「1」 / 「2」 after the bot said something else: a hint when a notification came this recently
UNSURE_GRACE_SEC = 180      # a send cut off by a stop is told as unsure this long after the start, unless it settles
SAID_LINES = 2              # her last lines a notification quotes
SAID_CHARS = 60             # ... each cut to this many characters
NOTE_CHARS = 120            # the draft's reason (when it asks for attention), an error told
FACT_CHARS = 400            # a platform action's line as the notification lists it (cut: the list is not whole)
LINES_MAX = 40              # ... and how many lines at most (more: 「还有 N 项，见控制台」, not run from WeChat)
NOTICE_CHARS = 1500         # ... and the notification at most this long (the relay's send limit, the default of
                            # CLAUDE_RELAY_MAX_CHARS): the platform lines that do not fit are left out the same way
READ_BACKS = 8              # the bot's own lines read back before their send was noted, remembered (``note_sent``)
READ_BACK_EARLY_SEC = 30    # ... for this long
TAIL_CHARS = 4              # a creator's platform id (UID, 抖音号) as a notification shows it: its last characters
FILES_NAMED = 5             # file names a notification lists at most
LISTED = 8                  # drafts a 「现在有 N 份在等你回」 names at most
STATUS_SEC = 6 * 3600       # a question about 执行 (「执行了吗」): the platform drafts told, answered or decided this recently
STATUS_LISTED = 3           # ... (or still waiting, still running) it answers about, at most this many
SEEN_IDS = 500              # the admin's answers remembered by message id (one handed on twice counts once)
PICTURE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")

_HEAD = re.compile(r"^\s*[#＃](\d{1,9})\s*(草稿|改成)")
_STAMP = re.compile(r"^\[\d{2}-\d{2} \d{2}:\d{2}\]\s*")
_QUOTE_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9]+:\s*")
_VOICE = re.compile(r"^\[语音\]\s*(.*)$", re.S)
_NUM = r"[#＃]?\s*(\d{1,9})"
_END = r"\s*(?:。|[！!～~]+)?"        # a command's one ending: a single 「。」, or 「！!～~」 (never 「.」「…」「...」「。。」)
_SEND_N = re.compile(rf"^(?:发送|发)\s*{_NUM}{_END}$")
_DROP_N = re.compile(rf"^不发\s*{_NUM}{_END}$")
_EXECUTE_N = re.compile(rf"^执行\s*{_NUM}{_END}$")
_NUMBERED = ((_SEND_N, "send", "发"), (_DROP_N, "drop", "不发"), (_EXECUTE_N, "execute", "执行"))
_EDIT_N = re.compile(rf"^改\s*{_NUM}(?:\s*[:：]\s*|\s+)(\S.*)$", re.S)
_EDIT_N_ONLY = re.compile(rf"^改\s*{_NUM}\s*[:：]?$")
_EDIT = re.compile(r"^改成?(?:\s*[:：]\s*|\s+)(\S.*)$", re.S)
_TRAILING = re.compile(r"[。.!~～]+$")
_COMMAND_END = re.compile(r"(?:。|[！!～~]+)$")
_EMOJI_TAG = re.compile(r"\[[^\[\]\s]{1,8}\]")
_EMOJI = re.compile("[\\s\U0001F000-\U0001FAFF☀-➿️‍]+")
_NOT_LETTERS = re.compile(r"[\W_]+")
_SALE = re.compile(r"（售价[^（）]*）")
_CREATOR_FACT = re.compile(r"（(\S+) ([^，）\s]+)，粉丝")      # a sample order's 达人 fact: （平台 平台号，粉丝 …
_ID_KEY = re.compile(r"(?:^|_)(?:id|uid|account)$", re.I)   # an id among a preview's plan (``run``): buyer_id …
_YES_ONLY = re.compile(r"(?:没问题|同意|确认|可以|ok|[发好行可对是嗯照中的啊呀吧啦哈了滴哒嘞哦噢呢喔])+")   # 「好好好好好好好好好」
_EXECUTE_BEFORE = ("那就", "那", "就", "可以", "请", "麻烦", "帮我", "帮忙", "去", "马上", "现在", "直接", "赶紧", "快", "好",
                   "行", "开始", "要")
_EXECUTE_AFTER = ("一下", "下", "吧", "啊", "呀", "哈", "呗", "哦", "喔", "嘛", "啦", "咯", "好", "完", "成功", "过")
_QUESTION_AFTER = ("没有", "吗", "么", "呢", "没", "了")   # ... after 执行, these make it a question (「执行了吗」「执行了没」)

NO_ADMIN_WHY = ("微信里审批草稿打开了，但没填管理员 wxid（ADMIN_WXID）：草稿发不到微信里。到「中转代理」页的设置里填好"
                "「管理员 wxid」，再重启机器人")
NO_SEND_WHY = "机器人没有连着微信：草稿发不到管理员微信里"
EDGE_WHY = "Mac 端（WECHAT_BACKEND=edge）不能在微信里审批草稿：管理员的私聊在 Windows 上读"
VOICE_TEXT = "回草稿要打字：引用那条通知回 1 发出、2 不发，或者回「发 12」这样带编号；语音不算（免得听错）"
REFUSE_TEXT = "这几句是一起到的，里面有好几条要交给 Mac 或者是管理命令：都没处理，请一句一句分开发"
HELP_YES = "要发原稿：引用通知回 1；要改：写「改 + 新的话」"
WHICH_EXECUTE = "执行哪一份？请引用那条通知回「执行」，或者回「执行 N」"
QUOTED_ELSEWHERE = "引用的消息交不到 Claude：要交给 Claude 请不引用直接发；要执行草稿请引用那条通知回「执行」"
QUEUE_RETRY = "没能排进 Mac 的任务队列，平台没有动，可以再回一次「执行」"
QUEUE_UNSURE = "排进 Mac 的任务队列时出了错，不知道排进去没有：平台上可能已经写了，先到平台后台核对，别重做；回复没有发"
UNCERTAIN = "结果不确定，平台上可能已经写了：先到平台后台核对，别重做"
VERIFIED_SEND_HEAD, VERIFIED_SEND_TAIL = "平台动作已经核验成功，但回复没发出去：", "；请在微信里手动发给达人"  # (service.py)


def _cut(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _word(text: str) -> str:
    """A short answer as it is compared: full-width letters and digits as plain ones, no spaces, trailing punctuation
    off, lower case (「执 行！」 is 「执行」)."""
    return _TRAILING.sub("", "".join(unicodedata.normalize("NFKC", str(text or "")).split())).lower()


def _emoji_only(text: str) -> bool:
    text = str(text or "").strip()
    return bool(text) and not _EMOJI.sub("", _EMOJI_TAG.sub("", text))


def _not_words(text: str) -> bool:
    """Words that are never a proposed edit: an approval word of the relay's, emoji only, a lone number."""
    word = _word(text)
    return word in ALLOW_WORDS or word in DENY_WORDS or _emoji_only(text) or word.isdigit()


def _letters(text) -> str:
    """A text's letters only: full-width as plain, emoji (and WeChat's 「[强]」 tags), punctuation and spaces left out,
    lower case."""
    return _NOT_LETTERS.sub("", _EMOJI_TAG.sub("", unicodedata.normalize("NFKC", str(text or "")))).lower()


def _yes_like(text: str) -> bool:
    """A text built around a yes: the admin approving, never words for her.  A short one (YES_STEMS, at most YES_CHARS
    letters once punctuation and emoji are left out: 「好的呀」「发吧」「可以的」「没问题」「OK的」「好的👌」…), or one of any
    length made only of yes words and their particles (「好好好好好好好好好」「嗯嗯嗯嗯嗯嗯嗯嗯嗯」「对对对对对对对对对」).  A yes
    word is there in both."""
    bare = _letters(text)
    if not bare or not any(stem in bare for stem in YES_STEMS):
        return False
    return len(bare) <= YES_CHARS or bool(_YES_ONLY.fullmatch(bare))


def _short(text: str) -> bool:
    """Quoting a notification: a text of at most SHORT_CHARS letters (punctuation and emoji left out) that does not
    start with 改 (「妥」「冲」「安排」「go」「没毛病」「就按这个」…): the admin reacting, never words for her (an edit says
    「改 …」)."""
    bare = _letters(text)
    return 0 < len(bare) <= SHORT_CHARS and not bare.startswith("改")


def _command_word(text) -> Optional[str]:
    """The word of an answer that can be a command (1 / 2 / 发 / 不发 / 执行 / 好的 …), or None: spaces out, then at
    most one ending off, a single 「。」 or one or more of 「！!～~」 (decided before full-width letters become plain
    ones), then full-width as plain and lower case (「执 行！」 is 「执行」, 「１。」 is 「1」).  Anything else at its end
    (「.」「…」「...」「。。」: hesitating, not finished) and it is no command (None never matches a word list)."""
    bare = _COMMAND_END.sub("", "".join(str(text or "").split()), count=1)
    if not bare or bare[-1] in "。.…．｡":
        return None
    return unicodedata.normalize("NFKC", bare).lower()


def _definite(content: str) -> bool:
    """An answer quoting a notification that is no words of the admin's own (with a command beside it, the command goes
    on alone): 1 / 2 / 执行 and their words (``_command_word``), 「改 …」 and 「改」, a numbered answer."""
    word = _command_word(content)
    return (word in SEND_WORDS or word in DROP_WORDS or word in EXECUTE_WORDS or _word(content) in ("改", "改成")
            or any(pattern.match(content) for pattern in (_EDIT, _EDIT_N, _EDIT_N_ONLY))
            or any(pattern.match(content) for pattern, _, _ in _NUMBERED))


def _execute_parts(text: str) -> Optional[tuple]:
    """A short text built around 「执行」 (punctuation, emoji and the words around it left out, at most YES_CHARS letters
    besides a draft number right after it): ``(number, question)``, else None.  The words before it
    (``_EXECUTE_BEFORE``: 「可以」「去」「那就」「要」…) and after it (``_EXECUTE_AFTER``: 「吧」「一下」「好」…, and
    ``_QUESTION_AFTER``); ``question``: a 「？」/「?」 in it, or 吗 / 么 / 呢 / 没 / 没有 / 了 after it (「执行了吗」「执行了没」
    「执行吗」「要执行吗」「执行好了吗」「执行了」): asking what became of a draft, never asking to run one.  Not one that
    says no (「不执行」「别执行」) nor a sentence (「执行这个方案吧」)."""
    letters = _letters(text)
    head, found, tail = letters.partition("执行")
    if not found or "执行" in tail:
        return None
    digits = re.match(r"\d{1,9}", tail)
    number = int(digits.group(0)) if digits else None
    tail = tail[digits.end():] if digits else tail
    if len(head) + len(found) + len(tail) > YES_CHARS:
        return None
    question = "?" in unicodedata.normalize("NFKC", str(text or ""))
    changed = True
    while changed and head:
        changed = False
        for word in _EXECUTE_BEFORE:
            if head.endswith(word):
                head, changed = head[:-len(word)], True
                break
    changed = True
    while changed and tail:
        changed = False
        for word in (*_QUESTION_AFTER, *_EXECUTE_AFTER):
            if tail.endswith(word):
                tail, changed = tail[:-len(word)], True
                question = question or word in _QUESTION_AFTER
                break
    return (number, question) if not head and not tail else None


def _asked(quoted: str) -> Optional[str]:
    """The proposal a 「#12 改成：『…』」 question showed (None when the quote does not hold it whole)."""
    start, end = quoted.find("『"), quoted.rfind("』")
    return quoted[start + 1:end] if 0 <= start < end else None


def _same(a, b) -> bool:
    return " ".join(str(a or "").split()) == " ".join(str(b or "").split())


def _tail(value) -> str:
    """A creator's platform id as a notification shows it: its last TAIL_CHARS characters."""
    value = str(value or "").strip()
    return value if len(value) <= TAIL_CHARS else "…" + value[-TAIL_CHARS:]


def preview_stamp(preview) -> str:
    """The fingerprint of a platform draft's preview: 「执行」 runs only the one its notification showed
    (``PrmService.decide`` "execute" ``stamp``: checked again in the step that moves the draft)."""
    blob = json.dumps(preview or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


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
    """Something the bot said in the admin's chat (``note_sent``) and when: this PC's time it went out (``at``), WeChat's
    time it shows (``wechat_at``, from its read-back: ``note_read_back``; None while not read back)."""

    at: float
    number: Optional[int]           # the draft of a notification / a 改成 question; None for anything else
    head: str = ""
    asked: Optional[str] = None
    text: str = ""
    wechat_at: Optional[float] = None


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

    ``decide(packet_id, action, text, stamp=…)`` and ``check(packet, text)`` are the service's ``decide`` /
    ``edit_problem``; ``lines(packet)`` her lines a draft answers (the job's); ``parts(text)`` how many messages a text
    goes out as (PRM_REPLY_PARTS); ``aim(packet)`` where a draft the bot started itself goes (``PrmService._aim``
    without failing it: None while she can be reached nowhere); ``relay_store`` the relay's jobs (a newer draft being
    written, a cc task running); ``local`` WeChat's time on this PC's clock and ``skew`` this PC's clock against WeChat's
    as measured (None: not yet); ``written(execution, outcome)`` what a platform action left on the platform
    (``service.written_state``); ``files_root`` the relay's files (the only pictures a notification sends);
    ``opening_auto``: PRM_OPENING_AUTO_MIN is on (an opening with its time set goes by itself)."""

    def __init__(self, store: PrmStore, config, *, clock: Callable[[], float] = time.time,
                 send: Optional[Callable[[str, str], bool]] = None,
                 send_file: Optional[Callable[[str, str], bool]] = None,
                 decide: Optional[Callable[..., dict]] = None, check: Optional[Callable[[dict, str], str]] = None,
                 lines: Optional[Callable[[dict], str]] = None, parts: Optional[Callable[[str], int]] = None,
                 aim: Optional[Callable[[dict], Optional[dict]]] = None, relay_store=None,
                 local: Optional[Callable[[float], float]] = None, skew: Optional[Callable[[], Optional[float]]] = None,
                 written: Optional[Callable[..., str]] = None, files_root=None, opening_auto: bool = False):
        self._store = store
        self._clock = clock
        self._send = send
        self._send_file = send_file
        self._decide = decide
        self._check = check
        self._lines = lines
        self._parts = parts
        self._aim = aim
        self._relay_store = relay_store
        self._local = local or (lambda written_at: written_at)
        self._skew = skew
        self._written = written
        self._files_root = Path(files_root) if files_root else None
        self._opening_auto = bool(opening_auto)
        self.admin = str(getattr(config, "admin_wxid", "") or "").strip()
        self._edge = str(getattr(config, "wechat_backend", "") or "") == "edge"
        self._switch = str(getattr(config, "prm_wechat_review", "off") or "off").strip().lower() == "on"
        self.on = self._switch and not self._edge     # (the route step and the sweep do nothing otherwise)
        self._relay_only = str(getattr(config, "claude_relay_only", "off") or "off").strip().lower() == "on"
        self._cc = prefix_pattern(str(getattr(config, "claude_relay_prefix", "cc") or "cc"))
        self._failures: dict[tuple, int] = {}       # ("draft" | "told" | "late", packet id) -> sends WeChat did not take
        self._chat_failures = 0                       # sends to the admin's chat not taken in a row (RETRY_SEC)
        self._wait_until = 0.0                        # ... and when the chat is tried again
        self._since: Optional[float] = None           # drafts that started waiting before this stay the console's
        self._started = clock()
        self._last: Optional[_Last] = None            # the last thing the bot said in the admin's chat
        self._last_review: Optional[_Last] = None     # ... the last of it that was a notification or a 改成 question
        self._said_lock = threading.RLock()           # ... set by the sender thread, read back by the poll thread
        self._read_backs: deque = deque(maxlen=READ_BACKS)   # (text, WeChat's time, this PC's) read back before noted
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
        of the decisions taken here (a send that failed, a platform action's end, its late result); PER_ROUND sends at
        most.  A send WeChat does not take ends the round, and the admin's chat waits before the next one (RETRY_SEC:
        10 s, doubling, at most 2 minutes apart); a message not taken SEND_TRIES times is not tried again this run
        (logged; the console still has the draft).  Returns how many messages went to the admin."""
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
                text, stamp, pictures = self._notice(packet)
            except Exception:
                logger.exception("PRM: the notification of draft %d could not be written; not told", packet["id"])
                self._failures[key] = SEND_TRIES
                continue
            current = self._store.packet(packet["id"])
            if current is None or current["status"] != PENDING:
                continue                                   # decided (or out of date) meanwhile
            if not self._store.add_wechat_review(packet["id"], packet["deal_id"], self._clock(), state=TELLING,
                                                 preview=stamp):
                continue                                   # (told already)
            tried += 1
            if not self._try(key, text, f"the notification of draft {packet['id']}"):
                self._store.drop_wechat_review(packet["id"], (TELLING,))     # (not sent: told again later)
                return sent
            now = self._clock()
            self._store.set_wechat_review(packet["id"], (TELLING,), now, state=NOTIFIED, notified_at=now)
            logger.info("PRM: draft %d told to the admin's chat (微信里审批)", packet["id"])
            self._send_pictures(packet["id"], pictures)
            sent += 1
        for row in self._store.wechat_reviews_to_tell():
            late = row.get("told_at") is not None          # (told already: its result came late)
            key = ("late" if late else "told", row["packet_id"])
            if self._failures.get(key, 0) >= SEND_TRIES:
                continue
            text = self._late_text(row) if late else self._outcome_text(row)
            if text is None:
                continue                                   # (an unsure send: it may still be read back)
            if tried >= PER_ROUND:
                return sent
            tried += 1
            if not self._try(key, text, f"what came of draft {row['packet_id']}"):
                return sent
            now = self._clock()
            if late or self._execution(row).get("late"):
                self._store.set_wechat_review(row["packet_id"], (ANSWERED,), now, late_told_at=now,
                                              **({} if late else {"told_at": now}))
            else:
                self._store.set_wechat_review(row["packet_id"], (ANSWERED,), now, told_at=now)
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

    def _pictures(self, packet: dict) -> list:
        """The pictures the operator is meant to see with a draft (``meta.operator_files``, read only when there: a list
        of ``{name, path, why}`` (the draft-context's photo comparison sheet, say) or of paths): ``(name, why, path)``
        of each that is a picture on this PC among the relay's own files (``files_root``); the others are logged."""
        files = (packet.get("meta") or {}).get("operator_files")
        out = []
        for item in files if isinstance(files, list) else []:
            path = item.get("path") if isinstance(item, dict) else item
            name = str((item.get("name") if isinstance(item, dict) else "") or Path(str(path or "")).name or "图片")
            why = str((item.get("why") if isinstance(item, dict) else "") or "")
            if not self._relay_file(path):
                logger.warning("PRM: a picture for the operator of draft %d is not one of the relay's pictures on this "
                               "PC; not sent", packet["id"])
                continue
            out.append((name, why, str(path)))
        return out

    def _relay_file(self, path) -> bool:
        """Is ``path`` a picture file among the relay's own files (never any other file of this PC)?"""
        if not isinstance(path, str) or not path or self._files_root is None:
            return False
        try:
            target = Path(path).resolve()
            return (target.suffix.lower() in PICTURE_SUFFIXES and target.is_file()
                    and target.is_relative_to(self._files_root.resolve()))
        except (OSError, ValueError):
            return False

    def _send_pictures(self, number: int, pictures: list) -> None:
        """The pictures right after their notification; one that cannot be sent is logged, never tried again."""
        if self._send_file is None:
            return
        for _, _, path in pictures:
            try:
                ok = bool(self._send_file(self.admin, path))
            except Exception:
                logger.exception("PRM: a picture for the operator of draft %d could not be sent", number)
                ok = False
            if not ok:
                logger.warning("PRM: a picture for the operator of draft %d was not sent to the admin's chat", number)

    @staticmethod
    def _json(text) -> dict:
        if isinstance(text, dict):
            return text
        try:
            value = json.loads(text or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _execution(self, row: dict) -> dict:
        return (self._json(row.get("platform")).get("execution") or {}) if row.get("decision") == "execute" else {}

    @staticmethod
    def _done(row: dict, execution: dict) -> str:
        """「<动作>（平台核验成功；寄样单号 …）」: a platform action verified, with what it made."""
        created = execution.get("created") or {}
        got = "；".join(f"{what} {created[key]}" for key, what in (
            ("link", "链接"), ("sample_order_no", "寄样单号"), ("channel_id", "渠道品 ID"), ("plan_name", "计划名"))
            if created.get(key))
        return f"{platforms.label(row.get('tool'))}（平台核验成功{'；' + got if got else ''}）"

    def _state(self, execution: dict, outcome: str) -> str:
        """What a platform action left on the platform, as the console tells it (``service.written_state``): verified,
        not_done (nothing written), uncertain (it may be there)."""
        if self._written is not None:
            return self._written(execution, outcome)
        return "verified" if execution.get("outcome") == platforms.VERIFIED else "uncertain"

    def _outcome_text(self, row: dict) -> Optional[str]:
        """What came of a decision taken here, in one line (None: not to be told yet): a send that failed; a platform
        action's end, as the console classes it (``_state``: verified; not done: 「执行没成功」; it may be on the platform:
        UNCERTAIN, never 「执行没成功」).  A send the bot was stopped in the middle of (``indeterminate``) waits for its
        read-back, or UNSURE_GRACE_SEC after the start."""
        number = row["packet_id"]
        name = (self._store.party(row["party_id"]) or {}).get("name") or "她"
        status, outcome, error = row.get("status"), str(row.get("outcome") or ""), str(row.get("error") or "")
        execute = row.get("decision") == "execute"
        execution = self._execution(row)
        verified = execution.get("outcome") == platforms.VERIFIED
        if status == FAILED and outcome == "indeterminate":
            if self._clock() - self._started < UNSURE_GRACE_SEC:
                return None
            head = f"#{number} 已执行：{self._done(row, execution)}，但回复" if verified else f"#{number} "
            if "文字已经发出去了" in error:
                return (f"{head}文字发出去了，文件不确定发出去没有（发的时候机器人停了），请看一眼微信再决定要不要手动"
                        f"补发给 {name}")
            return f"{head}不确定发出去没有（发的时候机器人停了），请看一眼微信再决定要不要手动发给 {name}"
        if not execute:
            return f"#{number} 没发出去：{_cut(error or '发送出错', NOTE_CHARS)}；需要的话请在微信里手动发给 {name}"
        if execution.get("late"):
            return self._late_text(row)
        if status == SENT:
            return f"#{number} 已执行：{self._done(row, execution)}，回复已发出"
        state = self._state(execution, outcome)
        if state == "verified":                          # (the console's words without their own head and tail)
            why = error.removeprefix(VERIFIED_SEND_HEAD).removesuffix(VERIFIED_SEND_TAIL) or "发送出错"
            why = self._hide(row.get("platform"), why)
            return (f"#{number} 已执行：{self._done(row, execution)}，但回复没有发出去（{_cut(why, NOTE_CHARS)}）；"
                    f"需要的话请在微信里手动发给 {name}")
        if status == DROPPED:                            # (an invite card had opened it already: nothing to run)
            error = self._hide(row.get("platform"), error.removesuffix("，没有执行") or "不用执行")
            return f"#{number} 没有执行：{_cut(error, NOTE_CHARS)}；回复没有发"
        result = str(execution.get("outcome") or ("NOT_RUN" if outcome == "platform_not_run" else "UNKNOWN"))
        problems = [p for p in execution.get("problems") or [] if isinstance(p, str) and p.strip()]
        what = f"{platforms.outcome_text(result)}（{result}）" + (f"：{self._hide(row.get('platform'), problems[0])}"
                                                                if problems else "")
        if state == "uncertain":
            return f"#{number} {UNCERTAIN}（{_cut(what, NOTE_CHARS)}）；回复没有发"
        return f"#{number} 执行没成功：{_cut(what, NOTE_CHARS)}，平台没有动，回复没有发；请到控制台看"

    def _late_text(self, row: dict) -> str:
        """A platform action's result that came after the bot had given up on it (``execution.late``): its reply was
        never sent by itself."""
        number = row["packet_id"]
        name = (self._store.party(row["party_id"]) or {}).get("name") or "她"
        execution = self._execution(row)
        result = str(execution.get("outcome") or "UNKNOWN")
        if result == platforms.VERIFIED:
            what = self._done(row, execution)
        else:
            what = f"{platforms.label(row.get('tool'))}：{platforms.outcome_text(result)}（{result}）"
        return f"#{number} 平台的结果晚到了：{what}；回复没有自动发，需要的话请在微信里手动回 {name}"

    def notification_text(self, packet: dict) -> str:
        """The message that tells the admin of a draft (see the module's docstring)."""
        return self._notice(packet)[0]

    def _notice(self, packet: dict) -> tuple[str, str, list]:
        """``(text, stamp, pictures)``: a draft's notification; the fingerprint of the preview it shows when it lets
        「执行」 run (``preview_stamp``; "" otherwise); the pictures to send after it (``_pictures``)."""
        number = packet["id"]
        meta = packet.get("meta") or {}
        name = (self._store.party(packet["party_id"]) or {}).get("name") or "?"
        group = meta.get("channel") == GROUP_CHANNEL
        platform = self._platform(packet)
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
        pictures = self._pictures(packet)
        if pictures:
            lines.append(f"（后面附 {len(pictures)} 张图给你看：" + "、".join(
                f"{_cut(n, 40)}" + (f"：{_cut(contact.mask_contacts(w), 60)}" if w else "") for n, w, _ in pictures) + "）")
        stamp = ""
        if platform:
            runnable = self._runnable(packet)
            go = ["引用这条回「执行」在平台上执行（核验成功才把上面的回复发给她）｜2 不发",
                  f"（发在群里：一定要引用这条回「执行」；不发也可以回「不发 {number}」）" if group
                  else f"（不引用就回「执行 {number}」「不发 {number}」）"]
            if (packet.get("platform") or {}).get("previewing"):
                stop = "正在重新预览，微信里不能执行：请到控制台看了结果再「批准并执行」；不要的话回 2"
            elif runnable:
                stop = "通知里没列全，微信里不能执行：请到控制台看了再「批准并执行」；不要的话回 2"
            else:
                stop = "预览没通过，微信里不能执行：请到控制台处理（「重新预览」）；不要的话回 2"
            # (the platform lines get what is left of NOTICE_CHARS, the longer of the two endings kept back)
            room = NOTICE_CHARS - len("\n".join(lines)) - 1 - max(len("\n".join(go)), len(stop)) - 1
            listed, whole = self._platform_lines(packet, room)
            lines += listed
            if runnable and whole:
                stamp = preview_stamp((packet.get("platform") or {}).get("preview"))
                lines += go
            else:
                lines.append(stop)
        else:
            lines.append(f"回 1 {'发到群里' if group else '发出'}｜2 不发｜改 + 你的话（直接发你改的）")
            lines.append(f"（发在群里：要引用这条回；不发也可以回「不发 {number}」）" if group
                         else f"（引用这条回；不引用就回「发 {number}」「不发 {number}」「改 {number} 你的话」）")
        return "\n".join(lines), stamp, pictures

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
    def _runnable(packet: dict) -> bool:
        """Can 「执行」 be asked for (the console's 批准并执行 button: a ready preview, not being previewed again)?"""
        data = packet.get("platform") or {}
        return ((data.get("preview") or {}).get("verdict") == "ready" and bool(data.get("action"))
                and not data.get("previewing"))

    @staticmethod
    def _creator_ids(action: dict, run=None) -> list:
        """The creator's platform ids an action names (UID, 抖音号, 小红书号) and the ids its preview's plan has (``run``:
        the 小红书 买手's userId, any ``…_id``): shown by their last characters only."""
        ids = [str(action.get(k) or "").strip() for k in ("creator_platform_id", "creator_douyin_id", "creator_uid",
                                                         "creator_account")]
        ids += [str(value).strip() for key, value in (run if isinstance(run, dict) else {}).items()
                if isinstance(value, (str, int)) and not isinstance(value, bool) and _ID_KEY.search(str(key))]
        return sorted({i for i in ids if len(i) > TAIL_CHARS}, key=len, reverse=True)

    def _ids(self, platform) -> list:
        """The ids ``_creator_ids`` finds in a draft's platform data (a dict, or its JSON as a row has it)."""
        data = self._json(platform)
        action = data.get("action") if isinstance(data.get("action"), dict) else {}
        preview = data.get("preview") if isinstance(data.get("preview"), dict) else {}
        return self._creator_ids(action, preview.get("run"))

    def _hide(self, platform, text) -> str:
        """``text`` (a problem, an error) as the admin's chat shows it: the ids of the draft's platform action
        (``_ids``) by their last characters, a labelled 微信号 / a phone number masked."""
        return self._masked("", text, self._ids(platform))[1]

    @classmethod
    def _masked(cls, label: str, value, ids: list) -> tuple[str, str]:
        """A line of a platform action as a notification shows it: the creator's platform ids (as a parameter, in a
        label or a value, a sample order's 达人 fact) by their last characters, the invite card's words by their length,
        a labelled 微信号 / a phone number masked."""
        label, value = str(label or ""), str(value if value is not None else "")
        if label in (platforms.PARAM_LABELS["creator_platform_id"], platforms.PARAM_LABELS["creator_douyin_id"],
                     platforms.PARAM_LABELS["creator_uid"], platforms.PARAM_LABELS["creator_account"]):
            return label, _tail(value)
        if label == platforms.PARAM_LABELS["invite_text"]:
            return label, f"（{len(value)} 字，见控制台）"
        if label == "达人":
            value = _CREATOR_FACT.sub(lambda m: f"（{m.group(1)} {_tail(m.group(2))}，粉丝", value)
        for creator_id in ids:
            label, value = label.replace(creator_id, _tail(creator_id)), value.replace(creator_id, _tail(creator_id))
        return label, contact.mask_contacts(value)

    def _platform_lines(self, packet: dict, room: Optional[int] = None) -> tuple[list, bool]:
        """``(lines, whole)``: a draft's platform action with every line its preview has (the console's preview block):
        what it is and the preview's verdict; a sample order's every product (name, and each SKU's code, colour and
        quantity), its courier and the receiver by province and city only (never a name, phone or street), the
        preview's other lines but the receiver's; any other action's parameters (``platforms.describe``, the receiver
        and the note left out) and every line of its preview (渠道类型, each SKU's price and stock, 价格检查, 有效期 …);
        the preview's problems.  The creator's platform ids by their last characters (``_masked``: in the problems too).
        At most LINES_MAX lines, each at most FACT_CHARS, all of them at most ``room`` characters (what the notification
        has left of NOTICE_CHARS).  ``whole``: every line listed in full (else 「还有 N 项，见控制台」, and 「执行」 is the
        console's)."""
        data = packet.get("platform") or {}
        preview = data.get("preview") or {}
        action = data.get("action") or {}
        name = action.get("action") or packet.get("tool")
        verdict = preview.get("verdict") or "unavailable"
        head = [f"平台动作：{platforms.label(name)}（{platforms.VERDICTS.get(verdict, verdict)}）"]
        if data.get("previewing"):
            head.append("· 正在重新预览，结果出来以前不能执行")
        facts = [f for f in preview.get("facts") or [] if isinstance(f, dict)]
        facts += [f for step in preview.get("steps") or [] if isinstance(step, dict)
                  for f in step.get("facts") or [] if isinstance(f, dict)]
        shown: list = []
        if name == "jst.sample_order":
            goods = [_SALE.sub("", str(f.get("value") or "")).strip() for f in facts if f.get("label") == "商品"]
            if goods:
                shown += [("寄样商品", value) for value in goods]
            else:                                          # (no preview of the products: their ids, as the console)
                shown += [(p["label"], p["value"]) for p in platforms.describe(action, preview.get("catalog"))
                          if p["label"] in ("商品", platforms.PARAM_LABELS["items"], platforms.PARAM_LABELS["skus"])]
            courier = next((str(f.get("value") or "") for f in facts if f.get("label") == "快递"), "")
            shown.append(("快递", courier or str(action.get("logistics") or "按这台 Mac 配的")))
            receiver = action.get("receiver") if isinstance(action.get("receiver"), dict) else {}
            province, city = str(receiver.get("province") or ""), str(receiver.get("city") or "")
            region = province + (city if city != province else "")
            shown.append(("收货", f"{region}（只写到市）" if region else "聚水潭里她名下的地址"))
            shown += [(f.get("label"), f.get("value")) for f in facts
                      if f.get("label") not in ("商品", "快递", platforms.RECEIPT_LABEL)]
        else:
            skip = (platforms.PARAM_LABELS["receiver"], platforms.PARAM_LABELS["note"])
            shown += [(p["label"], p["value"]) for p in platforms.describe(action, preview.get("catalog"))
                      if p["label"] not in skip]
            shown += [(f.get("label"), f.get("value")) for f in facts]
        problems = [p for p in preview.get("problems") or [] if isinstance(p, str)]
        problems += [p for step in preview.get("steps") or [] if isinstance(step, dict)
                     for p in step.get("problems") or [] if isinstance(p, str) and p not in problems]
        ids = self._creator_ids(action, preview.get("run"))
        rows = [self._masked(label, value, ids) for label, value in shown]
        rows += [self._masked("问题", p, ids) for p in problems]
        lines = [" ".join(f"{label}：{value}".split()) for label, value in rows]
        cut = not all(len(line) <= FACT_CHARS for line in lines)
        listed = [f"· {_cut(line, FACT_CHARS)}" for line in lines]
        room = NOTICE_CHARS if room is None else room

        def tail(kept: list) -> list:
            if len(kept) < len(listed):
                return [f"· 还有 {len(listed) - len(kept)} 项，见控制台"]
            return ["· 有几项太长没写全，见控制台"] if cut else []

        keep = listed if len(listed) <= LINES_MAX else listed[:LINES_MAX - 1]
        while keep and len("\n".join(head + keep + tail(keep))) > room:
            keep = keep[:-1]
        return head + keep + tail(keep), len(keep) == len(listed) and not cut

    # ── what the bot says in the admin's chat (the backend's ``set_sent_hook`` / ``set_read_back_hook``) ──

    def note_sent(self, chat_id: str, text: str) -> None:
        """A text the bot sent (the backend's hook: every one of them, a route reply's too, in the order sent).  In the
        admin's chat it is the last thing the bot said there: a bare 「1」 / 「2」 answers it only when it is a
        notification or a 改成 question."""
        if not self.admin or str(chat_id or "") != self.admin:
            return
        text = str(text or "")
        head = _HEAD.match(text)
        now = self._clock()
        with self._said_lock:
            said = _Last(now, int(head.group(1)) if head else None, head.group(2) if head else "",
                         _asked(text) if head and head.group(2) == "改成" else None, text, self._read_first(text, now))
            self._last = said
            if said.number is not None:
                self._last_review = said

    def _read_first(self, text: str, now: float) -> Optional[float]:
        """WeChat's time of ``text`` when it was read back before its send was noted (READ_BACK_EARLY_SEC at most
        before; taken once), else None."""
        for entry in reversed(self._read_backs):
            seen_text, written, seen_at = entry
            if now - seen_at <= READ_BACK_EARLY_SEC and _same(seen_text, text):
                self._read_backs.remove(entry)
                return written
        return None

    def note_read_back(self, chat_id: str, text: str, written: float) -> None:
        """A line of this account's read back from a private chat with WeChat's time (the backend's read-back hook):
        the last thing said in the admin's chat, as WeChat shows it (a bare answer is compared on one clock).  Under the
        lock ``note_sent`` takes, and only onto the very thing it read (a send noted meanwhile is never undone); a line
        read back before its send was noted is remembered for ``note_sent`` (READ_BACKS, READ_BACK_EARLY_SEC)."""
        if not self.admin or str(chat_id or "") != self.admin or not written:
            return
        text = str(text or "")
        with self._said_lock:
            matched = False
            for name in ("_last", "_last_review"):
                said = getattr(self, name)
                if said is not None and said.wechat_at is None and _same(said.text, text):
                    new = replace(said, wechat_at=float(written))
                    if getattr(self, name) is said:
                        setattr(self, name, new)
                        matched = True
            if not matched:
                self._read_backs.append((text, float(written), self._clock()))

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
        last = self._last
        if said is None:
            return False
        plan = self._plan(Said("text", said.content) if said.kind == "voice" else said, is_command,
                          self._burst(msg), last)
        return plan is not None and (not self._bare_only(plan) or self._bare_claims(last) or self._hint_due(last))

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
            return self._answer(msg, said, is_command, self._last)     # (one look at the last thing said)

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

    def _line(self, line: str, said: Said, last: Optional[_Last]) -> Optional[tuple]:
        """One line's answer: 「发 N」「不发 N」「执行 N」 for a draft told here (a single 「。」 or 「！!～~」 after the number,
        nothing else), 「改 N …」「改 N」 for one still waiting (``via_quote``: the message quotes that draft's
        notification or question; one told here and no longer waiting: what became of it); a line built around 「执行」
        (``_execute_parts``) not quoting anything: a question (「执行了吗」「执行 N 了没」) about a draft told here, or the
        platform drafts told here that wait or were told recently (``_status_rows``: what became of them), else, while a
        platform draft told here waits or was the last thing said, which one?; an unnumbered 「改 …」 while a
        notification is the last thing said, unless in CLAUDE_RELAY_ONLY (it is Claude's: it goes on) (which one?);
        None otherwise."""
        for pattern, action, _ in _NUMBERED:
            m = pattern.match(line)
            if m:
                number = int(m.group(1))
                return (action, number, said.number == number) if self._told(number) else None
        for pattern, edit in ((_EDIT_N, True), (_EDIT_N_ONLY, False)):
            m = pattern.match(line)
            if m:
                number = int(m.group(1))
                if self._open(number):
                    return ("edit", number, m.group(2).strip(), said.number == number) if edit else ("ask", number)
                return ("closed", number) if self._told(number) else None
        parts = None if said.quoted else _execute_parts(line)
        if parts is not None:
            number, question = parts
            if question and number is not None:        # (「执行 12 了吗」: what became of #12, never run)
                return ("execute_status", number) if self._told(number) else None
            if question:
                return ("execute_status", None) if self._status_rows() else None
            if number is None and self._execute_claims(last):
                return ("bare_execute",)               # (「执行吧」…: never run, never Claude's while a draft waits)
        if _EDIT.match(line) and last is not None and last.number is not None and not self._relay_only:
            return ("which_edit", last.number)         # (in CLAUDE_RELAY_ONLY: Claude's)
        return None

    def _relay_busy(self) -> bool:
        """Has a relay job of the admin's chat not ended, or ended within RELAY_QUIET_SEC (``relay_busy``)?"""
        return self._relay_store is not None and relay_busy(self._relay_store, self.admin, self._clock())

    def _rest(self, rest: list, is_command) -> tuple[list, bool]:
        """The lines of a message that are no answer as they go on (``(lines, refused)``): a cc line first (the relay
        reads the start of the message), an admin command alone (it is matched whole); two cc / command lines, or a
        command with other words: refused."""
        passing = [n for n, line in enumerate(rest) if self._passes(line, is_command)]
        if not passing:
            return rest, False
        if len(passing) > 1:
            return [], True
        first = rest[passing[0]]
        others = [line for n, line in enumerate(rest) if n != passing[0]]
        if self._cc.match(first):
            return [first, *others], False
        return ([first], False) if not others else ([], True)

    def _plan(self, said: Said, is_command, burst: bool, last: Optional[_Last]) -> Optional[Plan]:
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
            if rest and not self._cc.match(rest[0]) and content and not _definite(content):
                return Plan(refuse=REFUSE_TEXT)        # (a command with other words, as without a quote)
            number = said.number
            m = _EDIT_N.match(content)
            if m:
                n = int(m.group(1))
                item = ("edit", number, m.group(2).strip(), True) if n == number else ("mismatch", number, n, "改")
                return Plan([item], rest)
            m = _EDIT_N_ONLY.match(content)
            if m:
                n = int(m.group(1))
                return Plan([("ask", number) if n == number else ("mismatch", number, n, "改")], rest)
            for pattern, action, verb in _NUMBERED:
                m = pattern.match(content)
                if m:
                    n = int(m.group(1))
                    return Plan([(action, n, True) if n == number else ("mismatch", number, n, verb)], rest)
            return Plan([("quote", number, said.head, content, said.asked)], rest)
        if len(rows) == 1 and not said.quoted:
            word = _command_word(rows[0])
            if word in ("1", "2"):
                return Plan([("bare", int(word))])
        if len(rows) == 1 and said.quoted and _word(rows[0]) in ("1", "2"):
            return None                                # (quoting another message: not ours)
        if len(rows) == 1 and said.quoted and self._relay_only:
            parts = _execute_parts(rows[0])
            if parts is not None and parts[0] is None:  # (the relay takes no quote in CLAUDE_RELAY_ONLY: say so)
                return Plan([("quoted_elsewhere",)])
        items, rest, i = [], [], 0
        while i < len(raw):
            line = raw[i].strip()
            i += 1
            if not line:
                continue
            item = None if self._passes(line, is_command) else self._line(line, said, last)
            if item is None:
                rest.append(line)
                continue
            if item[0] == "edit":                      # the lines after it that are no answer: its words go on there
                more = []
                while i < len(raw) and (not raw[i].strip() or (not self._passes(raw[i], is_command)
                                                                and self._line(raw[i].strip(), said, last) is None)):
                    more.append(raw[i].rstrip())
                    i += 1
                if more:
                    item = ("edit", item[1], "\n".join([item[2], *more]).strip(), item[3])
            items.append(item)
        if not items:
            return None
        rest, refused = self._rest(rest, is_command)
        if refused:
            return Plan(refuse=REFUSE_TEXT)
        return Plan(items, rest)

    @staticmethod
    def _bare_only(plan: Plan) -> bool:
        return bool(plan.items) and plan.items[0][0] == "bare"

    def _bare_claims(self, last: Optional[_Last]) -> bool:
        """Is a bare 「1」 / 「2」 this feature's at all: never in CLAUDE_RELAY_ONLY (it may be Claude's), only when the last
        thing the bot said here was a notification or a 改成 question."""
        return not self._relay_only and last is not None and last.number is not None

    def _hint_due(self, last: Optional[_Last]) -> bool:
        """A bare 「1」 / 「2」 after the bot said something else (a relay's receipt after a merged burst, say), a
        notification or a question of a draft that still waits shortly before: not an answer, but not silence either
        (never in CLAUDE_RELAY_ONLY)."""
        review = self._last_review
        return (not self._relay_only and review is not None and (last is None or last.number is None)
                and self._clock() - review.at <= HINT_SEC and self._open(review.number))

    def _execute_claims(self, last: Optional[_Last]) -> bool:
        """Is a lone 「执行」 this feature's: a platform draft told here waits, or was the last thing the bot said."""
        if last is not None and last.number is not None:
            packet = self._store.packet(last.number)
            if packet is not None and self._platform(packet):
                return True
        return any(row.get("tool", MESSAGE_TOOL) not in (MESSAGE_TOOL, BUYIN_TOOL)
                   for row in self._store.wechat_reviews_open())

    def _answer(self, msg: dict, said: Said, is_command, last: Optional[_Last]) -> tuple[bool, Optional[str], Optional[str]]:
        burst = self._burst(msg)
        plan = self._plan(Said("text", said.content) if said.kind == "voice" else said, is_command, burst, last)
        if plan is None or (self._bare_only(plan) and not self._bare_claims(last) and not self._hint_due(last)):
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
            which = plan.items[0][1]
            if not self._bare_claims(last):
                return True, self._after_other(), None
            return True, self._bare(which, written, last), None
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
            return self._mismatch(*item[1:])
        if action == "ask":
            return f"#{item[1]} 要改成什么？回「改 {item[1]} 你的话」"
        if action == "which_edit":
            claude = "（要交给 Claude 请在前面加 cc）" if self._relay_busy() else ""
            return f"要改哪一份？引用那条通知回「改 你的话」，或者回「改 {item[1]} 你的话」{claude}"
        if action == "execute_status":
            return self._status_reply(item[1])
        if action == "quoted_elsewhere":
            return QUOTED_ELSEWHERE
        if action == "bare_execute":
            waiting = [f"#{r['packet_id']}" for r in self._store.wechat_reviews_open()
                       if r.get("tool", MESSAGE_TOOL) not in (MESSAGE_TOOL, BUYIN_TOOL)]
            return WHICH_EXECUTE + (f"（在等的：{'、'.join(waiting[:LISTED])}）" if waiting else "")
        row, packet = self._row(item[1])
        if row is None:
            return self._not_told(item[1], packet)
        closed = self._closed(packet, row)
        if closed:
            return closed
        if action == "closed":
            return f"#{item[1]} 已经处理过了"
        if action == "drop":
            return self._decided(row, packet, "drop", None)
        if action == "send":
            return self._send_original(row, packet, quoted=item[2])
        if action == "execute":
            return self._execute(row, packet, quoted=item[2])
        return self._edit(row, packet, item[2], quoted=item[3], propose=burst)

    @staticmethod
    def _mismatch(quoted: int, number: int, verb: str) -> str:
        """A number in the words that is not the quoted draft's: nothing done, which one?"""
        if verb == "改":
            return (f"你引用的是 #{quoted}，写的却是「改 {number}」：没有改。要改哪一份？引用那一份的通知回「改 你的话」，"
                    "或者回「改 编号 你的话」")
        return (f"你引用的是 #{quoted}，写的却是「{verb} {number}」：什么都没做。要处理哪一份？引用那一份的通知回，"
                f"或者不引用回「{verb} 编号」")

    def _status_rows(self) -> list:
        """The told platform drafts a question about 执行 is about: waiting, running on the Mac, or told, answered or
        decided within STATUS_SEC (``PrmStore.wechat_reviews_platform``), newest first."""
        return self._store.wechat_reviews_platform(self._clock() - STATUS_SEC, limit=STATUS_LISTED + 20)

    def _status_reply(self, number: Optional[int]) -> str:
        """A question about 执行 (「执行了吗」「执行了没」「要执行吗」「执行好了吗」…): what became of draft ``number``, or (None)
        of the platform drafts ``_status_rows`` gives, STATUS_LISTED at most (one line each).  Never runs anything."""
        if number is not None:
            row, packet = self._row(number)
            return self._not_told(number, packet) if row is None else self._execute_status(packet, row)
        rows = self._status_rows()
        lines = []
        for row in rows[:STATUS_LISTED]:
            packet = self._store.packet(row["packet_id"])
            if packet is not None:
                lines.append(self._execute_status(packet, row))
        if len(rows) > STATUS_LISTED:
            lines.append(f"（还有 {len(rows) - STATUS_LISTED} 份，见控制台）")
        return "\n".join(lines) or "最近没有发到这里的平台草稿"

    def _execute_status(self, packet: dict, row: Optional[dict]) -> str:
        """What became of a draft, in one line, for a question about 执行: waiting (and whether 「执行」 runs it from
        here), running on the Mac, done and its result as the line told when it ends (``_outcome_text``: verified and
        sent, not sent, uncertain, not done, a result that came late), dropped, out of date (``_stale``)."""
        number, status = packet["id"], packet["status"]
        if not self._platform(packet):
            return self._closed(packet, row) or f"#{number} 没有平台动作：要发就引用那条通知回 1（或者回「发 {number}」）"
        if status == PENDING:
            told = str((row or {}).get("preview") or "")
            if told and told == preview_stamp((packet.get("platform") or {}).get("preview")):
                how = "引用那条通知回「执行」" + ("" if self._group(packet) else f"（或者回「执行 {number}」）")
                return f"#{number} 还没执行（在等你确认）：要执行请{how}"
            return f"#{number} 还没执行（在等你确认）：微信里不能执行这一份，请到控制台看了再「批准并执行」"
        if status == EXECUTING:
            return f"#{number} 已经批准，正在平台上执行（做完了告诉你结果）"
        if status in (APPROVED, SENDING):
            return f"#{number} 已经执行完了：{self._done({'tool': packet.get('tool')}, self._execution_of(packet))}，回复正在发"
        if status == SUPERSEDED:
            return self._stale(packet)
        if status == DROPPED and packet.get("outcome") != "covered":
            return f"#{number} 没有执行：点了不发"
        told = self._outcome_text({"packet_id": number, "party_id": packet["party_id"], "status": status,
                                   "outcome": packet.get("outcome"), "error": packet.get("error"),
                                   "tool": packet.get("tool"), "platform": packet.get("platform"),
                                   "decision": "execute"})
        return told or f"#{number} 已经执行完了，回复发出去没有还在核对（发的时候机器人停了）"

    def _execution_of(self, packet: dict) -> dict:
        return self._json(packet.get("platform")).get("execution") or {}

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
        parts = _execute_parts(text)
        if parts is not None and parts[1]:            # (「执行了吗」 quoting it: what became of it, never run)
            asked_for = parts[0]
            if asked_for is not None and asked_for != number:
                return self._mismatch(number, asked_for, "执行")
            return self._execute_status(packet, row)
        closed = self._closed(packet, row)
        if closed:
            return closed
        command, word = _command_word(text), _word(text)
        if command in EXECUTE_WORDS:
            return self._execute(row, packet, quoted=True)
        if command in SEND_WORDS:
            if head == "改成" and not self._platform(packet):
                return self._send_asked(row, packet, asked)
            return self._send_original(row, packet, quoted=True)
        if command in DROP_WORDS:
            return self._decided(row, packet, "drop", None)
        edit = _EDIT.match(text.strip())
        if edit:
            return self._edit(row, packet, edit.group(1).strip(), quoted=True)
        letters = _letters(text)
        if word in ("改", "改成") or (letters.startswith("改") and len(letters) <= SHORT_CHARS):
            return f"#{number} 要改成什么？引用通知回「改 你的话」，或者回「改 {number} 你的话」"
        if self._platform(packet):
            return self._platform_hint(packet)
        if _not_words(text) or _yes_like(text) or _short(text):
            return self._help(packet, head)
        return self._propose(row, packet, text.strip())

    def _seen_after(self, last: _Last, written: float) -> bool:
        """Was the bare answer written after the bot said ``last``, on one clock: WeChat's time of both when ``last`` was
        read back; else this PC's time it went out against the answer's WeChat time on this PC's clock (the measured
        clock, BARE_MARGIN_SEC; none measured: CLOCK_GUESS_SEC)."""
        if not written:
            return False
        if last.wechat_at is not None:
            return written > last.wechat_at
        skew = self._skew() if self._skew is not None else None
        if skew is None:
            return written > last.at + CLOCK_GUESS_SEC
        return written + skew > last.at + BARE_MARGIN_SEC

    def _open_list(self, rows: list) -> str:
        names = "、".join(f"#{r['packet_id']} {(self._store.party(r['party_id']) or {}).get('name') or '?'}"
                         for r in rows[:LISTED]) + ("…" if len(rows) > LISTED else "")
        return (f"现在有 {len(rows)} 份在等你回：{names}；单独一个 1 / 2 不算：请引用那条通知回，或者回「发 {rows[0]['packet_id']}」"
                "这样带编号")

    def _after_other(self) -> str:
        """A bare 「1」 / 「2」 after the bot said something else (``_hint_due``): what it may have meant, nothing done."""
        review = self._last_review
        number = review.number
        rows = self._store.wechat_reviews_open()
        if len(rows) > 1:
            return self._open_list(rows)
        if review.head == "改成":
            return (f"机器人后来又说了别的，单独一个 1 / 2 不算：#{number} 的改稿还在等你确认，引用「#{number} 改成」那一问回 1，"
                    f"或者回「发 {number}」发原稿、「不发 {number}」")
        return f"机器人后来又说了别的，单独一个 1 / 2 不算：#{number} 还在等，引用那条通知回 1，或者回「发 {number}」「不发 {number}」"

    def _bare(self, which: int, written: float, last: _Last) -> str:
        """A bare 「1」 / 「2」 (``_bare_claims`` holds): the draft of the last thing the bot said here, unless that was said
        after the answer was written, a relay job of this chat is under way or just ended, another told draft waits too
        (the list), or another told draft was just closed in the console."""
        number = last.number
        if not self._seen_after(last, written):
            return f"#{number} 是刚发来的，你回的时候还没看到它：请引用它回，或者回「发 {number}」「不发 {number}」"
        if self._relay_busy():
            return (f"Claude 那边有任务在做或者刚做完，单独一个 1 / 2 不算：#{number} 请引用那条通知回，或者回「发 {number}」"
                    f"「不发 {number}」")
        rows = self._store.wechat_reviews_open()
        if len(rows) > 1 or (rows and rows[0]["packet_id"] != number):
            return self._open_list(rows)
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
        return packet.get("tool", MESSAGE_TOOL) not in (MESSAGE_TOOL, BUYIN_TOOL)

    @staticmethod
    def _group(packet: dict) -> bool:
        return (packet.get("meta") or {}).get("channel") == GROUP_CHANNEL

    @staticmethod
    def _group_hint(packet: dict, execute: bool = False) -> str:
        name = (packet.get("meta") or {}).get("group_name") or "老板拉的群"
        return f"#{packet['id']} 是发在群「{name}」里的，请引用那条通知回{'「执行」' if execute else ' 1 '}确认"

    @staticmethod
    def _platform_hint(packet: dict) -> str:
        return f"#{packet['id']} 带平台动作：引用那条通知回「执行」（或者回「执行 {packet['id']}」）才在平台上执行；不要的话回 2"

    @staticmethod
    def _platform_edit_refused(packet: dict) -> str:
        return f"#{packet['id']} 带平台动作：回复要改的话请在控制台「改了再执行」；不要的话回 2"

    def _help(self, packet: dict, head: str = "草稿") -> str:
        number = packet["id"]
        if self._platform(packet):
            return self._platform_hint(packet)
        if head == "改成":
            return f"#{number} 要发这份改稿：引用那一问回 1；{HELP_YES}"
        return f"#{number} {HELP_YES}"

    def _send_original(self, row: dict, packet: dict, *, quoted: bool) -> str:
        """The draft as it was drafted (a proposed edit dropped): not for a platform action (「执行」), and for a group only
        by an answer quoting it."""
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

    def _execute(self, row: dict, packet: dict, *, quoted: bool) -> str:
        """「执行」: the console's 批准并执行 (``decide`` "execute", every check of it): a platform action only, the preview
        its notification showed (its ``preview_stamp``, checked again in ``decide``'s move: not one the notification said
        cannot run, nor one previewed again since), a group's only by an answer quoting it."""
        number = packet["id"]
        if not self._platform(packet):
            return f"#{number} 没有平台动作：回「发 {number}」就行"
        if self._group(packet) and not quoted:
            return self._group_hint(packet, execute=True)
        told = str(row.get("preview") or "")
        if told and told == preview_stamp((packet.get("platform") or {}).get("preview")):
            return self._decided(row, packet, "execute", None, stamp=told)
        if told:
            return f"#{number} 通知发出以后重新预览过：请到控制台看了新的预览再「批准并执行」；不要的话回 2"
        if self._runnable(packet) and not self._listed_whole(packet):
            return f"#{number} 的平台动作通知里没列全，微信里不能执行：请到控制台看了再「批准并执行」；不要的话回 2"
        return f"#{number} 通知里的预览没通过（或者正在重新预览），微信里不能执行：请到控制台处理"

    def _listed_whole(self, packet: dict) -> bool:
        """Does the notification of this draft list its platform action whole (then it lets 「执行」 run: a stamp)?"""
        try:
            return bool(self._notice(packet)[1])
        except Exception:
            logger.exception("PRM: the notification of draft %d could not be written", packet["id"])
            return False

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

    def _decided(self, row: dict, packet: dict, action: str, text: Optional[str], *, stamp: Optional[str] = None) -> str:
        """The admin's decision through ``PrmService.decide`` (the console's own): the reply that says what came of it."""
        number = packet["id"]
        if self._decide is None:
            return f"#{number} 现在不能在微信里处理：请在控制台处理"
        try:
            result = self._decide(number, action, text, **({"stamp": stamp} if stamp else {}))
        except Exception:
            logger.exception("PRM: the admin's answer to draft %d could not be carried out", number)
            result = {"ok": False, "error": "出错了（详见日志）"}
        if not result.get("ok"):
            code = result.get("code")
            if code == "queue_failed":                 # (certainly not queued: the draft waits again)
                return f"#{number} {QUEUE_RETRY}"
            if code == "queue_unknown":                # (it may run: red, its result told here when it comes late)
                now = self._clock()
                self._store.set_wechat_review(number, (), now, state=ANSWERED, decision=action, answered_at=now,
                                              told_at=now)
                logger.warning("PRM: draft %d: the admin's 执行 in WeChat may or may not have been queued", number)
                return f"#{number} {QUEUE_UNSURE}"
            if code == "preview_changed":
                return f"#{number} 通知发出以后重新预览过：请到控制台看了新的预览再「批准并执行」；不要的话回 2"
            if code == "previewing":
                return f"#{number} 正在重新预览，微信里不能执行：请到控制台看了结果再「批准并执行」；不要的话回 2"
            fresh = self._store.packet(number) or packet
            closed = self._closed(fresh, row)
            if closed:
                return closed
            what = {"drop": "没改成不发", "execute": "没执行"}.get(action, "没发")
            return f"#{number} {what}：{self._hide(fresh.get('platform'), result.get('error') or '出错了')}"
        now = self._clock()
        self._store.set_wechat_review(number, (), now, state=ANSWERED, decision=action, answered_at=now,
                                      **({"proposed": ""} if action == "send" else {}))
        logger.info("PRM: draft %d decided by the admin in WeChat (%s)", number, action)
        if action == "drop":
            return f"#{number} 不发了（她再发消息时会有新的草稿）"
        if action == "execute":
            return f"#{number} 已批准执行：Mac 正在平台上执行，做完了告诉你结果（核验成功才把回复发给她）"
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
            what += f"（{_cut(self._hide(packet.get('platform'), packet['error']), 60)}）"
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
