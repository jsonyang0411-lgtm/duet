"""老板拉的群 (PRM_BOSS_GROUPS, default off; 2026-10-31, the user: 「老板那边也有一点人是已经加了微信的，然后老板想把他和达人还有
我们这边也拉个群，让我们来负责和达人对接」, 「全自动接手」, 「起草后人审再发到群里，像之前的加个自动模式」).

Some creators are the boss's WeChat friends, not ours.  He makes a small group: himself (a name of PRM_GROUP_WITH), the
creator and this account; from then on we handle her.  The scan (``sweep``, every BOSS_SCAN_SEC on PRM's sender thread)
asks the WeChat backend for the group chats it does not know (``boss_groups``: neither in the baseline its first run
kept, prm_meta ``boss_groups_baseline``, nor found before, nor looked at twice already with no boss in them or too many
members) that have a boss among their members and at most PRM_BOSS_GROUP_MAX members; one whose lines or members could
not be read now (``{"chat", "unreadable": True}``) is asked for again at the next scan, never taken for one without a
boss.  Is a group new for us (``_judge``: WeChat's times against the baseline's, ``BASELINE_AT``, kept on WeChat's clock,
the backend's ``wechat_now`` then; never this PC's clock)?  Certainly new only when WeChat's line that we were added
(``joined_at``) is at most NEW_MARGIN_SEC older than the baseline; begun for us more than OLD_SEC before it: kept
``ignored`` (「不是新拉的群」); no line in it yet: ``waiting`` (the backend hands it back at each scan until its lines are
there); any other (no line that we were added: an old group whose chat was deleted with its history comes back looking
new) waits for a person (``pending``, 「拿不准是不是新拉的群」).  Her ``candidates`` are the members but us, the bosses (by
wxid, and anyone named like one), the admin and the cc users:

- exactly one, the group certainly new and every boss name known: the bot takes over (``take_over``);
- one otherwise, or two or more: ``pending``: the console asks which member she is (``pick``; 「这个群不是达人」 is
  ``ignore``);
- none: ``waiting``: its members are read again each scan for BOSS_WAIT_DAYS (one comes in: as above), then it is
  ``ignored``.

A name of PRM_GROUP_WITH that is no one contact's (none, or two: ``contact_ids``) stops every automatic takeover: the
console warns which name (``warning``; ``why_not`` only says why the scan cannot run at all), and the groups found wait
for its 「是她」 (``pending``).  A deal's own 三方群 (「拉三方群」 makes her, the boss and us a group too) is never one: every
deal's ``three_party.chat`` goes into the baseline, and a group whose one candidate has a 三方群 made or unsure (「建好了」
by hand too: no group id kept) is kept ``ignored``; while 「拉三方群」 makes one for her (a 「先试一下」 makes nothing: it does
not count) the group waits and is looked at again once it is done (``waiting``); the takeover checks again right before
it links the group, under the lock 「拉三方群」 starts one under (ThreeParty ``guard``).

Taking over: she is registered (her note 「老板介绍（老板拉的群：<群名>）」) unless she is an active creator already; the group
is linked to her deal (``deals.three_party`` ``{"state": "created", "by": "boss", "chat", "name", ...}``: the 三方群 block
shows it, the Mac hears a group is made; a deal with a group made, being made or unsure keeps it when an operator picks
her, and the record says so); the backend reads the group from then on (``watch_group``: its lines from shortly before it
was found, or from its oldest line, reach the route once, the boss's introduction too); the lines the bot's message
store kept of it before (a group WECHAT_GROUPS read: the route stored them and said nothing) are handed on once
(``_replay``); once the backend says its catch-up ran (``watch_caught_up``; one that cannot say: OPENING_WAIT_SEC after
the takeover) one draft job asks for our first line in the group, the boss's introduction in its context, unless her own
words made a reply job already (PrmService ``_group_opening``: it always waits for a person; her own line in the group
outdates it).  A creator an operator stopped (停用) is never taken over by the scan: the console asks.  The scan also
reads a linked group's members: the deal says so when she is gone.  「群已经不在了」 on her deal (``gone``): the group is
not read any more, her lines there are silent again.  After a restart a linked group is read again from the second after
its last line handled, ours included (``_since``).

接手任何合作群 (2026-11-09, src/prm/group_link.py): an operator links any group to a creator by hand, from the admin's
WeChat chat (「接群」) or the console's 「是她」 on the open question (``offer``: who in it may be her; OFFER_SEC).  Such a
group (``manual``) is linked whatever PRM_BOSS_GROUPS says (that switch only rules the scan; with it off, only the groups
linked by hand are read and drafted in): ``link`` registers her unless she is a creator already (her note
「手动接的群（<群名>）」), links the group to her deal (``_link``: her own 三方群 that 「拉三方群」 made, read only till then,
is drafted in from now on, its ``by`` ``manual``; a group with a boss of PRM_GROUP_WITH in it becomes her 三方群 the same
way; one without a boss is no 三方群, kb 第 0.5 节: the console lists it with her deal), and has what was said there lately
read once (``catch_up``, PrmService._link_catch_up: context for her next job, ours and hers too; one draft when she waits
for an answer) before the watch starts after it.  No 开场白.  The other members may be her 商务, the brand's people,
anyone: their lines are context only, as the boss's are; a job of such a group says so (``job_group``: ``manual``, how
many ``others``, a boss only when one of PRM_GROUP_WITH is in it).  「不接群」, 「不接了」 (``stop``) or 「群已经不在了」
ends it as ``gone`` does.

What the bot starts itself for her (a shipping notice, a 专属佣金 step, a 开场白 from the console) goes to her private chat
when she has one (she wrote there, or the backend's quiet ``has_private_chat`` says it is there: ``knows_private``),
else to a linked group she is still in, else nowhere (PrmService ``_reach`` / ``_aim``: the deal says so).  The draft
jobs of such a creator tell where each earlier reply went (``meta.sent[].where``).

In a linked group (PrmService ``_group_message``: the route's one exception to «a group is never answered»,
src/routing/steps.py) only HER lines are drafted, as in her private chat: the job says it is the group (``meta.channel``
"group", ``meta.group``: its name, what we call the boss, the other members' lines kept and not answered yet,
``job_group``), the packet goes to the group, the gates, 下班时段 and 自动模式 apply as they are, no holding reply goes
there.  The boss's and the other members' lines are only kept for her next job (``add_context``, each with a number);
one that comes while her group draft waits in 待审批 has it drafted again with it (PrmService ``_redraft_group``); a reply
sent drops the lines its job was told, by number (``replied``).  WeChat's own lines are never drafted.  The bosses'
names never leave this PC: a job calls him PRM_BOSS_CALL (``hide``: in the group's name and the lines told too).  Names
stay in prm.db: the log has ids' first characters and counts only.

群自己接上 (PRM_GROUPS_AUTO_LINK, default off; 2026-11-11, the user: 「把所有群都读了吧」: a group her own team made was
never read, nobody sent 「接群」, and 「拉三方群」 keeps failing on the real machine, so groups are made by hand).  With it
on, ``auto_scan`` (every BOSS_SCAN_SEC, after the boss scan, on the same thread: ``sweep``) lists every group chat of
this account (the backend's ``recent_groups`` without sizes) and looks at each one that is not linked, not a deal's own
三方群 (made, being made or unsure with its id: read only), not one a person stopped (``gone``) or refused (「这个群不是
达人」, 「不接」: ``decline``), not one the boss scan still waits on, with at most PRM_BOSS_GROUP_MAX members, old groups
too (who she is comes from the list of creators, never from when the group was made).  Its members are read when it is
first seen, when something was said in it since, and every AUTO_MEMBERS_SEC (``candidates_of``; a group that cannot be
read now is asked for again at the next scan, never taken for one without a creator).  Of the ones she may be:

- exactly one is an active registered creator whose deal is not cleaned up (8.2) and who has no other linked group the
  bot reads: the group is linked to her deal as a link by hand is (``_link_offer`` with ``auto``: nobody is registered,
  no 开场白, the catch-up of what was said lately, one draft when her side waits, and that draft always waits for a
  person; the row's ``manual`` is AUTO, the deal's record says 「自动接的群…群里只有她一位登记过的达人」), and the admin is
  told once in WeChat (「已自动接群：… 不对就回「不接群 <群名>」」).  While 「拉三方群」 makes a group for her the scan waits
  (this may be that very group); a deal with a 三方群 made or unsure keeps it, as a link by hand does (``_link``);
- two or more are registered creators: never a guess: the console asks which (``asked_by_scan``: the card's ``manual``
  entries with ``by`` scan, until answered), and the admin is told once in WeChat how to choose (「接群 <群名>」);
- none (or the one creator cannot be linked by itself: she has a linked group already, her deal is cleaned up): nothing
  is done; a group that was there, and silent since, when the switch first ran stays quiet (the baseline: ``AUTO_STATE``
  ``base`` and ``at``, WeChat's clock), one that appeared or was active after it gets one WeChat line (「…还没接：回
  「接群 <群名>」」).

No notice is ever repeated for a group (``AUTO_STATE``: ``told`` / ``asked``; the lines owed, ``owed``, go AUTO_TELL_MAX
a scan and survive a restart).  With the switch off none of this runs and nothing is kept; the groups it linked stay
linked (they are groups linked by hand in every other way: PRM_LINKED_GROUPS_AUTO rules what goes out by itself).

她那边的人 (PRM_GROUP_HER_SIDE, default off; 2026-11-11): in a linked group a line of a member who is not ours (us, a
boss, the admin, a cc user) and no registered creator (``her_side``) is drafted as her own line is (PrmService
``_group_message``): her assistant, her 商务.  A job of such a group says so (``job_group``: ``her_side`` True) and each
such line is marked HER_SIDE_MARK in the job's text; that person's name never goes to the Mac.  Only in a group of at
most PRM_BOSS_GROUP_MAX members: in a bigger one (an operator may link one by hand) the others stay context.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict, deque
from contextlib import nullcontext
from typing import Callable, Optional

from ..edge import text as edge_text
from ..wechat.new_friends import is_me
from .store import PartyError
from .three_party import CREATED, CREATING, NOTICE as GROUP_NOTICE, UNSURE, group_with

logger = logging.getLogger(__name__)

BOSS_SCAN_SEC = 60                 # the scan runs this often (PRM's sender thread)
BOSS_WAIT_DAYS = 3                 # a group with nobody but us and the boss is looked at this long
BOSS_WATCH_BACK_SEC = 300          # the watch hands on the group's lines from this long before it was found
BOSS_IDS_SEC = 3600                # the bosses' wxids (``contact_ids`` reads every group's members): read again this seldom
BOSS_IDS_RETRY_SEC = 600           # ... while a name of PRM_GROUP_WITH is no one contact's (it may be put right)
OPENING_WAIT_SEC = 90              # a backend that cannot say its watch caught up: the group 开场白 this long after the takeover
NEW_MARGIN_SEC = 600               # we were added up to this long before the baseline: still certainly a new group
OLD_SEC = 86400                    # a group begun for us (we were added, or its oldest line read) this long before it: old
NEW, MAYBE, OLD, EMPTY = "new", "maybe", "old", "empty"     # ``_judge``: is a group new for us
KNOWS_SEC = 60                     # ``knows_private``: the backend's answer for one creator is kept this long
REPLAY_MAX = 200                   # the lines stored before a takeover handed on (the newest)
SEEN_IDS = 500                     # message ids of a linked group remembered (the route and the replay hand one on once)
IGNORED_KEEP_DAYS = 30             # an ignored group's row is dropped after this long (its id stays in the baseline)
MAX_DEFAULT, MAX_LOW, MAX_HIGH = 6, 3, 20      # PRM_BOSS_GROUP_MAX
LINES_KEPT = 30                    # the boss's / other members' lines a group keeps for her next job (the newest)
LINES_TOLD = 10                    # ... and tells one job
LINE_CHARS = 300
NAME_CHARS = 60                    # a group's name as kept
NAME_MAX = 30                      # her 称呼 (PrmService.register)
CALL_DEFAULT, CALL_MAX = "老板", 10  # PRM_BOSS_CALL: what the Mac is told the boss is called
BASELINE = "boss_groups_baseline"  # prm_meta: the group ids there were when the feature first ran (JSON list)
# prm_meta: when, on WeChat's clock (the backend's ``wechat_now`` then: the newest session time; a backend without it:
# this PC's clock less the skew this run measured): a group we were added to before it is no certainly new one
BASELINE_AT = "boss_groups_baseline_at"
PENDING, WAITING, LINKED, IGNORED, GONE = "pending", "waiting", "linked", "ignored", "gone"
STATES = (PENDING, WAITING, LINKED, IGNORED, GONE)
CHANNEL = "group"                  # a group job's and packet's ``meta.channel``
OPENING_HOW = "boss_group"         # the group 开场白's ``meta.opening`` ({"how", "by"} both this)
EVENT = "boss_group"               # the deal's record 「老板拉群介绍（群名）」
LEFT_NOTICE = "boss_group_left:"   # + the group id: the deal's notice while she is not in the group
BOSS, MEMBER = "老板", "群成员"      # who said a line kept for her next job (``meta.group.lines[].who``)
FILLER = "ㅤ"                  # HANGUL FILLER, a blank-looking WeChat name (src/wechat/new_friends.py)
NO_BOSS_WHY = "先在设置里填 PRM_GROUP_WITH（老板在这个微信里的名字）"
NO_BACKEND_WHY = "这台电脑的微信读法找不到老板拉的群（要 Windows 版微信的数据库直读）"
NO_READ_WHY = "微信数据库现在读不了，认不出老板，稍后自动再找"
NOBODY_WHY = f"{BOSS_WAIT_DAYS} 天里群里一直只有老板和我们，不再看这个群"
IGNORE_WHY = "运营说这个群不是达人"
GONE_WHY = "运营说这个群已经不在了"
STOPPED_WHY = "她在达人名单里停用过：要接手的话请在这里点「是她」"
OURS_WHY = "像是合作单上「拉三方群」建的群（不确定那次建没建成），不接手"
HAS_GROUP_WHY = "她已经有三方群"
NOT_NEW_WHY = "不是新拉的群"
MAYBE_WHY = "拿不准是不是新拉的群：是的话点「是她」"
NO_LINES_WHY = "群里还没有消息：有了以后再看是不是新拉的群"
QUIET_WHY = f"{BOSS_WAIT_DAYS} 天里群里一直没有消息，不再看这个群"
MAKING_WHY = "她的三方群正在建（合作单上的「拉三方群」）：建完以后再看这个群"
UNKNOWN_WHY = "PRM_GROUP_WITH 里的{names}在这个微信里找不到 / 有重名"
# A forwarded 聊天记录 where the bosses' names never go to the Mac (``record_msg``): who said each line
HER_LABEL, US_LABEL = "她", "我们"
OTHER_LABELS = "甲乙丙丁戊己庚辛壬癸"      # anybody else, in the order they first speak; past ten of them: OTHERS_LABEL
OTHERS_LABEL = "其他人"
# a kept line that is a forwarded 聊天记录 (``line_chars``; room for the names ``hide`` and the masking change)
RECORD_LINE_CHARS = edge_text.RECORD_CHARS + 100
# 接手任何合作群 (2026-11-09, src/prm/group_link.py): a group an operator links by hand
MANUAL = "manual"                  # deals.three_party.by of such a group; its record's ``manual`` (``EVENT``)
OFFER_SEC = 1800                   # the open 「接群」 question (``offer``) counts this long
LINES_TOLD_MANUAL = 20             # ... a job of such a group is told this many of the lines kept (the catch-up's too)
HER, US = "她", "我们"              # who said a line kept of such a group: her lines and ours from before the link
MANUAL_FALLBACK = "群里的达人"       # her 称呼 without a name (``her_name``)
MANUAL_WHY = "手动接的群"
MANUAL_GONE_WHY = "运营说不接这个群了"
MANUAL_LEFT_TEXT = "她已经不在这个接手的群里了：那个群里不会再有她的消息可起草，需要的话在微信里看一眼"
# 群自己接上 (PRM_GROUPS_AUTO_LINK, 2026-11-11): ``auto_scan``
AUTO = 2                           # boss_groups.manual of a group the scan linked by itself (1: an operator, by hand)
# prm_meta, JSON: {"at": when the switch first ran (WeChat's clock), "base": [the groups there were then], "told": {chat:
# when its 「还没接」 line was queued}, "asked": {chat: when its 「不止一位达人」 line was queued}, "no": {chat: when a
# person refused it}, "owed": [[chat, the line for the admin's chat]]}
AUTO_STATE = "groups_auto_link"
AUTO_MEMBERS_SEC = 3600            # a group nothing was said in is read again (its members) this seldom; a big one too
AUTO_TELL_MAX = 3                  # the lines for the admin's chat one scan sends at most
AUTO_TELL_TRIES = 6                # a line WeChat does not take is tried at this many scans, then dropped
AUTO_LINK_TRIES = 3                # a link that fails is tried at this many scans a run
AUTO_WHY = "群里只有她一位登记过的达人"
AUTO_LINK_WHY = "自动接的群"       # deals.three_party.why of such a group that is her 三方群 (a boss in it)
AUTO_GONE_WHY = "她不是在用的达人了"
AUTO_HAS_GROUP, AUTO_PURGED, AUTO_MAKING = "group", "purged", "making"   # why the one creator is not linked by itself
SCAN = "scan"                      # an open question's ``by``: the scan asks (``asked_by_scan``), not the admin
LINKED_NOTICE, ASK_NOTICE, NEW_NOTICE, ACTIVE_NOTICE = "linked", "ask", "new", "active"   # ``notice``'s kinds
# 她那边的人 (PRM_GROUP_HER_SIDE, 2026-11-11): before a line of her side's other people in a job's text; who said a line
# kept of them (``meta.group.lines[].who``: the catch-up's)
HER_SIDE_MARK = "〔她那边的人〕"
HER_SIDE = "她那边"


def boss_call(raw) -> str:
    """PRM_BOSS_CALL as the bot uses it: white space collapsed, at most CALL_MAX characters, 「老板」 when empty."""
    return " ".join(str(raw or "").split())[:CALL_MAX] or CALL_DEFAULT


def line_chars(text) -> int:
    """How long a kept line of the group may be: LINE_CHARS, a forwarded 聊天记录's RECORD_LINE_CHARS."""
    return RECORD_LINE_CHARS if str(text or "").startswith(edge_text.RECORD_PREFIX) else LINE_CHARS


def _hashes(wxid) -> set:
    """A wxid as a forwarded 聊天记录 may name its person (``<dataitemsource>``: the wxid itself, or a hash of it), lower
    case; empty for no wxid."""
    wxid = str(wxid or "").strip()
    if not wxid:
        return set()
    raw = wxid.encode("utf-8")
    return {wxid.lower(), hashlib.md5(raw).hexdigest(), hashlib.sha1(raw).hexdigest(), hashlib.sha256(raw).hexdigest()}


def _names_re(names) -> Optional[re.Pattern]:
    """Each of ``names`` (two characters or more, spaces and case aside) in a text, the longest first; None for none."""
    keys = sorted({"".join(str(n or "").split()) for n in names} - {""}, key=len, reverse=True)
    keys = [k for k in keys if len(k) >= 2]
    if not keys:
        return None
    return re.compile("|".join(r"\s*".join(re.escape(ch) for ch in k) for k in keys), re.IGNORECASE)


def _key(name) -> str:
    """A name as the backend compares names (src/wechat/wcdb_backend.py ``_name_key``): no spaces, no case."""
    return "".join(str(name or "").split()).casefold()


def _num(value) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def her_name(name, wxid: str, fallback: str = "老板介绍") -> str:
    """The 称呼 she is registered with: her name as this account sees her (white space and fillers collapsed, at most 30
    characters), 「老板介绍 <wxid 前 8 位>」 without one (``fallback``: a group linked by hand, MANUAL_FALLBACK)."""
    text = " ".join(str(name or "").replace(FILLER, " ").split())[:NAME_MAX].strip()
    return text or f"{fallback} {str(wxid or '')[:8]}"


def her_note(group_name: str) -> str:
    return f"老板介绍（老板拉的群：{str(group_name or '').strip() or '没有群名'}）"[:200]


def manual_note(group_name: str) -> str:
    """Her note when a group linked by hand registered her: 「手动接的群（<群名>）」."""
    return f"{MANUAL_WHY}（{str(group_name or '').strip() or '没有群名'}）"[:200]


def offer_out(offer: Optional[dict]) -> Optional[dict]:
    """The open 「接群」 question as the console shows it (``boss_groups_out``'s ``manual``): the group, how many are in
    it, when it was asked and until when it counts, whether it is a deal's own 三方群, and who may be her (each
    ``registered`` / ``stopped``, the registered first).  One the scan asks (群自己接上, ``BossGroups.asked_by_scan``)
    also has ``by`` "scan" and no ``until``."""
    if not offer:
        return None
    scan = offer.get("by") == SCAN                 # (the scan's own question: open until it is answered)
    return {"chat": offer["chat"], "name": offer["name"], "size": offer.get("size"), "asked_at": offer["at"],
            "until": None if scan else offer["at"] + OFFER_SEC, "three_party": bool(offer.get("deal_id")),
            "members": [{"wxid": m["wxid"], "name": m.get("name") or "", "registered": bool(m.get("registered")),
                         "stopped": bool(m.get("stopped"))} for m in offer["members"]],
            **({"by": SCAN} if scan else {})}


def clean_members(raw, own: str = "") -> list[dict]:
    """The backend's members (``[{"wxid", "name"}]``) as kept: each wxid once, never ours (``own``)."""
    out: list[dict] = []
    for m in raw or []:
        if not isinstance(m, dict):
            continue
        wxid = str(m.get("wxid") or "").strip()
        if wxid and wxid != own and all(x["wxid"] != wxid for x in out):
            out.append({"wxid": wxid, "name": str(m.get("name") or "")[:NAME_CHARS]})
    return out


def candidates(members: list, bosses, reserved=(), names=()) -> list[dict]:
    """Who of a group's ``members`` (us left out already) may be her: not a boss (their wxids, and anyone whose name is
    one of the bosses' ``names``, PRM_GROUP_WITH: a boss whose wxid is not known), not the admin or a cc user
    (``reserved``), no group id."""
    bosses, reserved = set(bosses), set(reserved)
    keys = {_key(n) for n in names or ()} - {""}
    return [m for m in members or [] if m.get("wxid") and m["wxid"] not in bosses and m["wxid"] not in reserved
            and not str(m["wxid"]).endswith("@chatroom") and _key(m.get("name")) not in keys]


def told_lines(group) -> list[int]:
    """The numbers of the kept lines a job's ``meta.group`` was told (``job_group``)."""
    out = []
    for item in (group or {}).get("lines") or [] if isinstance(group, dict) else []:
        try:
            seq = int(item.get("seq") or 0) if isinstance(item, dict) else 0
        except (TypeError, ValueError):
            seq = 0
        if seq:
            out.append(seq)
    return out


def boss_groups_out(store, *, enabled: bool, why_not: str = "", boss=(), reserved=(),
                    boss_call: str = CALL_DEFAULT, warning: str = "", offer: Optional[dict] = None, asks=()) -> dict:
    """What the console shows (GET /api/prm ``boss_groups``; also the stopped bot's, from .env): on or off, why it cannot
    run at all now (``why_not``: the console's buttons are off then), what it warns of while it runs (``warning``, ""
    when nothing: a name of PRM_GROUP_WITH no one contact has, so no group is taken over by itself; the buttons stay
    on), the bosses (PRM_GROUP_WITH) and what the Mac is told they are called (``boss_call``, PRM_BOSS_CALL), the
    groups waiting for an operator to say who she is (``pending``: their name, only the members she may be (a 「是她」
    each: never us, a boss, the admin or a cc user), how many are in it (``size``), when found, why), how many wait (for
    her to come in, for their first lines, for her 三方群 being made) and how many are taken over.  接手任何合作群
    (2026-11-09): the open 「接群」 question (``offer``) is ``manual`` (``offer_out``, a list of it) and the groups linked
    by hand are ``manual_linked``, whatever ``enabled`` says, each key only when there is one (the counts above are
    the scan's groups: as before when nobody linked a group by hand).  群自己接上 (2026-11-11): the questions the scan
    asks (``asks``: a group with two or more registered creators in it) are ``manual`` entries too, after the admin's;
    the groups it linked count in ``manual_linked``."""
    rows = store.boss_groups() if store is not None and enabled else []
    rows = [r for r in rows if not r.get("manual")]
    pending = [{"chat": r["chat"], "name": r["name"], "found_at": r["found_at"], "why": r["why"],
                "size": len(r["members"]) + 1,
                "members": [{"wxid": m["wxid"], "name": m.get("name") or ""}
                            for m in candidates(r["members"], {b.get("wxid") for b in r["boss"]}, reserved, boss)]}
               for r in rows if r["state"] == PENDING]
    out = {"enabled": bool(enabled), "why_not": str(why_not or "") if enabled else "",
           "warning": str(warning or "") if enabled else "", "boss": list(boss),
           "boss_call": str(boss_call or CALL_DEFAULT), "pending": pending,
           "waiting": sum(1 for r in rows if r["state"] == WAITING),
           "linked": sum(1 for r in rows if r["state"] == LINKED)}
    shown = [o for o in (offer_out(offer), *(offer_out(a) for a in asks or ()
                                             if not offer or a.get("chat") != offer.get("chat"))) if o is not None]
    if shown:
        out["manual"] = shown
    by_hand = sum(1 for r in (store.boss_groups((LINKED,)) if store is not None else []) if r.get("manual"))
    if by_hand:
        out["manual_linked"] = by_hand
    return out


def _clock_text(at) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(at)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def waits_for_lines(row: Optional[dict]) -> bool:
    """A group kept ``waiting`` because it had no line yet (``NO_LINES_WHY``): the scan asks the backend for it again
    (its times), until its lines are there or BOSS_WAIT_DAYS are over."""
    return bool(row) and row.get("state") == WAITING and row.get("why") == NO_LINES_WHY


class BossGroups:
    """The 老板拉的群 of one PRM service (see the module's doc).  ``register(wxid, name, note, opening=True)``:
    PrmService.register (a link by hand registers with ``opening`` False: her new-friend catch-up without a 开场白,
    nothing of it by itself);
    ``opening(party, chat)``: PrmService._group_opening (``(job id, "")`` or ``(None, why not)``); ``handle(msg)``:
    PrmService.handle (a line stored before the takeover, handed on once); ``reserved``: the admin and the cc users;
    ``local(at)``: a time WeChat gives on this PC's clock (PrmService._local: only the baseline's time of a backend
    without ``wechat_now`` is worked out with it, ``_wechat_clock``); ``guard()``: ThreeParty.guard (the lock 「拉三方群」
    starts a group under, yielding the deal being made now, never one only tried); ``catch_up(party, chat, since)``:
    PrmService._link_catch_up (a group linked by hand: what was said there since ``since`` read once, ``link``);
    ``on_close(chat, now)``: PrmService._close_group (a group's link ended, ``_drop``: its drafts not sent fail, its
    job not taken is cancelled); ``notice(kind, info)``: PrmService._auto_link_notice (群自己接上: the line for the
    admin's chat, names as that chat may show them) and ``tell(text)``: PrmService._tell_admin (True: sent; False: WeChat
    did not take it now; None: there is no admin's chat to tell).  ``attach`` gives it the WeChat backend and the bot's
    message store (Bot._start_prm, before PrmService.start)."""

    def __init__(self, store, config=None, *, clock: Callable[[], float] = time.time, reserved=(),
                 register: Optional[Callable[..., dict]] = None,
                 opening: Optional[Callable[[dict, str], tuple]] = None,
                 handle: Optional[Callable[[dict], bool]] = None,
                 local: Optional[Callable[[float], float]] = None,
                 guard: Optional[Callable] = None,
                 catch_up: Optional[Callable[[dict, str, float], Optional[dict]]] = None,
                 on_close: Optional[Callable[[str, float], int]] = None,
                 notice: Optional[Callable[[str, dict], str]] = None,
                 tell: Optional[Callable[[str], Optional[bool]]] = None):
        self._store = store
        self._config = config
        self._clock = clock
        self._local = local if local is not None else (lambda at: at)
        self._guard = guard if guard is not None else (lambda: nullcontext(None))
        self._reserved = {str(w) for w in reserved or () if str(w).strip()}
        self._register = register
        self._opening = opening
        self._handle = handle
        self._catch_up = catch_up
        self._on_close = on_close
        self._notice = notice
        self._tell = tell
        self._on = str(getattr(config, "prm_boss_groups", "off") or "off").strip().lower() == "on"
        # 群自己接上 / 她那边的人 (2026-11-11; a config object without them, an older caller's: off)
        self._auto = str(getattr(config, "prm_groups_auto_link", "off") or "off").strip().lower() == "on"
        self._her_side = str(getattr(config, "prm_group_her_side", "off") or "off").strip().lower() == "on"
        self._with = group_with(getattr(config, "prm_group_with", ""))
        self._call = boss_call(getattr(config, "prm_boss_call", ""))
        try:
            most = int(getattr(config, "prm_boss_group_max", MAX_DEFAULT) or MAX_DEFAULT)
        except (TypeError, ValueError):
            most = MAX_DEFAULT
        self._max = min(max(most, MAX_LOW), MAX_HIGH)
        try:      # a group made while the bot was stopped is read back from when it was made, up to PRM_CATCHUP_HOURS
            back = int(getattr(config, "prm_catchup_hours", 12))
        except (TypeError, ValueError):
            back = 12
        self._back_sec = max(min(max(back, 0), 72) * 3600, BOSS_WATCH_BACK_SEC)
        self._backend = None
        self._stored: Optional[Callable] = None   # the bot's message store's get_messages_since (``_replay``)
        self._lock = threading.RLock()            # one scan, pick, ignore or opening at a time
        self._scanned = 0.0
        self._why = ""                            # why the last scan could not look (the console's why_not)
        self._warning = ""                        # what the last scan that looked warns of (the console's warning)
        self._taking: frozenset = frozenset()     # wxids being taken over now (registered, the group not linked yet)
        self._bosses: dict[str, str] = {}         # the bosses' wxids as last read (``contact_ids``) -> their names
        self._bosses_at = 0.0                     # ... when (BOSS_IDS_SEC; 0: read again at the next scan)
        self._bosses_for: tuple = ()              # ... for these names of PRM_GROUP_WITH
        self._unknown: list[str] = []             # the names of PRM_GROUP_WITH no one wxid has: no takeover by itself
        self._ids_failed = False                  # the last read of them failed (said once in the log)
        self._asked_again: set[str] = set()       # members named like a boss who is none: the bosses read again once each
        self._looked: set[str] = set()            # groups looked at twice with no boss or too many members (not again
        self._once: set[str] = set()              # until the bosses are read again) / ... looked at once
        self._knows: dict[str, tuple] = {}        # wxid -> (when, has a private chat): ``knows_private``
        self._hide_for: Optional[tuple] = None    # the names ``_hide_re`` was made of
        self._hide_re = None
        self._seen: dict[str, deque] = {}         # linked group -> the message ids handed on lately (``first_time``)
        self._said: dict[str, OrderedDict] = {}   # ... -> who wrote each of them (``sender_of``: a recall in place)
        self._seen_lock = threading.Lock()
        # chat -> party id of the linked groups the bot reads and drafts in: the scan's while PRM_BOSS_GROUPS is on, the
        # ones linked by hand always (the route asks for every group message: no database look); replaced, never
        # changed in place (the route's thread reads it while the sender thread links a group)
        self._linked: dict[str, int] = {}
        self._manual: frozenset = frozenset()    # ... of them, the ones linked by hand (接手任何合作群)
        # linked group -> when it was taken over, while its 开场白 waits for the watch's catch-up (``_openings``)
        self._due: dict[str, float] = {}
        self._offer: Optional[dict] = None       # the open 「接群」 question (``offer``; OFFER_SEC)
        self._manual_looked = 0.0                # is she still in the groups linked by hand: looked at then (``sweep``)
        # 群自己接上 (``auto_scan``): when it last looked; each group as last read this run (chat -> {"at", "last_at",
        # "found": ``candidates_of``'s answer or None, "big"}); the questions it asks (chat -> an offer, ``by`` scan:
        # replaced, never changed in place); what it keeps (``AUTO_STATE``, read once); tries of a line or a link
        self._auto_scanned = 0.0
        self._auto_seen: dict[str, dict] = {}
        self._asks: dict[str, dict] = {}
        self._auto_kept: Optional[dict] = None
        self._auto_tries: dict[tuple, int] = {}
        self._auto_unsaid = False                # no admin's chat to tell: said once in the log
        rows = store.boss_groups((LINKED,))
        live = [r for r in rows if r.get("party_id") and (self._on or r.get("manual"))]
        self._linked = {r["chat"]: int(r["party_id"]) for r in live}
        self._manual = frozenset(r["chat"] for r in live if r.get("manual"))
        if self._on:
            self._due = {r["chat"]: float(r["opening_due"]) for r in rows if r.get("opening_due") is not None}

    # ── wiring and status ─────────────────────────────────────────────────

    def attach(self, backend, stored: Optional[Callable] = None) -> None:
        """The WeChat backend (spec §4: own_wxid, contact_ids, boss_groups, group_members, watch_group, unwatch_group,
        watch_caught_up, has_private_chat; group_ids, db_readable) and ``stored`` (MessageStore.get_messages_since): with
        WECHAT_GROUPS reading every group, the route kept a group's lines before the takeover and the watch skips them
        (``_replay``)."""
        self._backend = backend
        self._stored = stored

    @property
    def backend(self):
        """The WeChat backend ``attach`` gave (None before): src/prm/group_link.py lists and reads groups with it."""
        return self._backend

    @property
    def on(self) -> bool:
        """PRM_BOSS_GROUPS is on."""
        return self._on

    @property
    def max_members(self) -> int:
        """PRM_BOSS_GROUP_MAX as the scan uses it (3–20)."""
        return self._max

    @property
    def auto(self) -> bool:
        """PRM_GROUPS_AUTO_LINK is on (``auto_scan``)."""
        return self._auto

    @property
    def her_side_on(self) -> bool:
        """PRM_GROUP_HER_SIDE is on (``her_side``)."""
        return self._her_side

    @property
    def call(self) -> str:
        """What the Mac is told the boss is called (PRM_BOSS_CALL)."""
        return self._call

    def _can_scan(self) -> bool:
        return all(callable(getattr(self._backend, name, None))
                   for name in ("boss_groups", "group_ids", "own_wxid", "contact_ids"))

    def why_not(self) -> str:
        """Why the scan cannot run at all now ("" = it can): no boss named, a backend that cannot, the database, no
        boss known (the console's buttons are off while it says so)."""
        if not self._with:
            return NO_BOSS_WHY
        if not self._can_scan():
            return NO_BACKEND_WHY
        return self._why

    def warning(self) -> str:
        """What the scan warns of while it runs ("" = nothing): a name of PRM_GROUP_WITH no one contact has (none, or
        two): no group is taken over by itself, the groups found wait for the console's 「是她」, whose buttons stay on
        (second review 2026-10-31 F3: before, it was ``why_not`` and the console turned them off)."""
        return self._warning if self._on and not self.why_not() else ""

    def status(self) -> dict:
        return boss_groups_out(self._store, enabled=self._on, why_not=self.why_not() if self._on else "",
                               boss=self._with, reserved=self._reserved, boss_call=self._call, warning=self.warning(),
                               offer=self.offered(), asks=list(self._asks.values()))

    # ── what the PRM service asks (any thread; no lock of this class is taken) ──

    def linked_party(self, chat: str) -> Optional[int]:
        """The creator a group the bot reads and drafts in now is linked to (``is_linked``), or None: a draft of another
        creator's (the group linked again to her 商务, say) is no draft of this group's any more (PrmService
        ``_group_closed``, review 2026-11-09)."""
        return self._linked.get(str(chat or ""))

    def is_linked(self, chat: str) -> bool:
        """Is this a group the bot reads and drafts in: one the scan took over (the feature on), or one an operator
        linked by hand (接手任何合作群: whatever the switch says)?"""
        return str(chat or "") in self._linked

    def linked_chats(self) -> set:
        """The groups ``is_linked`` says yes to (「接群」 does not list them)."""
        return set(self._linked)

    def by_hand(self, chat: str) -> bool:
        """Was this group linked by hand (接手任何合作群, its row's ``manual``; linked now or gone since)?  Only words hang on
        it: its drafts and the Mac say 群 instead of 老板拉的群.  Whether a draft goes out is ``is_linked``'s (linked by
        hand: whatever PRM_BOSS_GROUPS says; gone: never, PrmService._group_closed)."""
        chat = str(chat or "")
        if not chat.endswith("@chatroom"):
            return False
        if chat in self._manual:
            return True
        row = self._store.boss_group(chat)
        return bool(row and row.get("manual"))

    def by_scan(self, chat: str) -> bool:
        """Did the scan link this group by itself (群自己接上, its row's ``manual`` AUTO; linked now or gone since)?  It
        is a group linked by hand in every other way (``by_hand`` is True too): only words hang on it."""
        chat = str(chat or "")
        if not chat.endswith("@chatroom"):
            return False
        row = self._store.boss_group(chat)
        return bool(row and row.get("manual") == AUTO)

    def _own(self) -> str:
        """This account's wxid as the backend says it now ("" when it cannot)."""
        ask = getattr(self._backend, "own_wxid", None)
        if not callable(ask):
            return ""
        try:
            return str(ask() or "")
        except Exception:
            return ""

    def her_side(self, row: dict, party: dict, sender: str, name: str = "") -> bool:
        """她那边的人 (PRM_GROUP_HER_SIDE on): is ``sender``, who wrote a line in her linked group, somebody of her side
        who is not the registered account (her assistant, her 商务)?  Anybody but her, us (``is_me``: this account's
        lines carry its wxid without the data folder's suffix), a boss (``who``: his wxid, or a member named like one),
        the admin, a cc user, and a registered creator (a stopped one too: another creator's words are never hers).
        Only in a group of at most PRM_BOSS_GROUP_MAX members (the row's, as last read: a big group an operator linked
        by hand keeps everybody else's lines as context, never a Mac job per member).  False with the switch off, and
        for a line without a sender (WeChat's own)."""
        sender = str(sender or "").strip()
        if (not self._her_side or not sender or sender.endswith("@chatroom")
                or sender == str((party or {}).get("wxid") or "") or sender in self._reserved
                or is_me(sender, self._own()) or len((row or {}).get("members") or []) + 1 > self._max):
            return False
        if self.who(row or {}, sender, name) == BOSS:
            return False
        return self._store.party_by_wxid(sender, include_inactive=True) is None

    def side_ids(self, row: dict, party: dict) -> set:
        """The members of her linked group who are of her side (``her_side``, by the row's members as last read)."""
        if not self._her_side:
            return set()
        return {str(m.get("wxid") or "") for m in (row or {}).get("members") or []
                if self.her_side(row, party, str(m.get("wxid") or ""), str(m.get("name") or ""))}

    def linked(self, chat: str) -> Optional[dict]:
        """The row of a group the bot took over, or None (not one, or the feature off)."""
        if not self.is_linked(chat):
            return None
        row = self._store.boss_group(str(chat))
        return row if row is not None and row["state"] == LINKED else None

    def party_of(self, chat: str) -> Optional[dict]:
        """Her party, when this is a group the bot took over and she is still on the list (active)."""
        row = self.linked(chat)
        party = self._store.party(int(row["party_id"])) if row is not None and row.get("party_id") else None
        return party if party is not None and party.get("active") else None

    def party_has_group(self, party_id) -> bool:
        """Is a group the bot took over (or one linked by hand) linked to this creator (her drafts go to the chat she
        wrote in)?"""
        try:
            return int(party_id) in self._linked.values()
        except (TypeError, ValueError):
            return False

    def masks(self, party: Optional[dict]) -> bool:
        """Do her words go to the Mac with the bosses' names hidden (``hide``)?  A creator with a group the bot took
        over, or one being taken over right now (registered, her group not linked yet: her new-friend catch-up may be
        read meanwhile, second review 2026-10-31 F9; ``_taking`` only holds one while the scan, a 「是她」 or a link by
        hand takes her)."""
        party = party or {}
        return self.party_has_group(party.get("id")) or str(party.get("wxid") or "") in self._taking

    def reach_group(self, party_id) -> Optional[dict]:
        """The newest group the bot took over for this creator that she is still in (no ``LEFT_NOTICE`` on her deal), or
        None: where a packet the bot starts itself goes when she has no private chat with us (PrmService ``_reach``).
        Only a group the bot reads and drafts in now (``is_linked``: a row the scan took over while PRM_BOSS_GROUPS was
        on is none while it is off, review 2026-11-09)."""
        if not self.party_has_group(party_id):
            return None
        for row in self._store.boss_groups((LINKED,)):
            if int(row.get("party_id") or 0) != int(party_id) or row["chat"] not in self._linked:
                continue
            deal = self._store.deal(int(row.get("deal_id") or 0)) or {}
            if not any(n.get("key") == LEFT_NOTICE + row["chat"] for n in deal.get("notices") or []):
                return row
        return None

    def knows_private(self, wxid: str) -> bool:
        """Does the backend have her private chat (she is our friend)?  Its quiet ``has_private_chat`` (no log), its
        answer kept KNOWS_SEC per creator (the sender thread asks every round for a step owed); False when it cannot
        say.  Never the send path's ``chat_target`` (it logs an error with her wxid for a chat it cannot name)."""
        wxid = str(wxid or "")
        now = self._clock()
        kept = self._knows.get(wxid)
        if kept is not None and now - kept[0] < KNOWS_SEC:
            return kept[1]
        ask = getattr(self._backend, "has_private_chat", None)
        known = False
        if callable(ask) and wxid:
            try:
                known = bool(ask(wxid))
            except Exception:
                logger.debug("PRM: boss groups: a private chat could not be looked up", exc_info=True)
        self._knows[wxid] = (now, known)
        return known

    def who(self, row: dict, sender: str, name: str = "") -> str:
        """Who wrote a line of the group that is not hers: 老板 (a boss of PRM_GROUP_WITH: his wxid, or a member named like
        him) or 群成员.  ``name``: the sender's name as the message gives it."""
        sender = str(sender or "")
        if sender in {str(b.get("wxid") or "") for b in row.get("boss") or []} | set(self._bosses):
            return BOSS
        keys = {_key(n) for n in self._with} - {""}
        names = [name, *(m.get("name") for m in row.get("members") or [] if m.get("wxid") == sender)]
        return BOSS if keys and any(_key(n) in keys for n in names if n) else MEMBER

    def boss_said(self, row: dict, line: str) -> bool:
        """Does one of WeChat's own lines of the group name a boss (「"老板测试" 撤回了一条消息」: not hers)?"""
        names = {str(b.get("name") or "") for b in row.get("boss") or []} | set(self._with)
        return any(n and n in str(line or "") for n in names)

    def recall_is_hers(self, row: dict, party: dict, line: str, sender: str = "", recent: bool = True,
                       side=()) -> bool:
        """Is one of WeChat's lines of a linked group somebody's recall (``edge.text.recall_by`` "them": not ours) that
        is hers (PrmService ``_system_line``: every draft of hers not sent waits for a person)?  ``sender``: who wrote
        the line WeChat took back, when it rewrote that line in place (the backend's ``recalled_id``, ``sender_of``):
        hers only when it was hers.  Else by the name in it: hers when it is her name (as this account sees her in the
        group, or her 称呼; spaces and case aside); never when it is a boss's (``boss_said``: PRM_GROUP_WITH, the bosses'
        names kept) or another member's we know (review 2026-11-09).  Any other name: in a 老板拉的群 hers (she is the
        one there besides us and the boss); in a group linked by hand only when she wrote there lately (``recent``:
        within HER_RECALL_SEC, PrmService; second check 2026-11-09: a stranger's recall, the boss's under his nickname,
        held all her drafts, her private ones too).  ``side`` (她那边的人, ``side_ids``): the members whose lines are
        drafted as hers: a recall of theirs is hers too."""
        if edge_text.recall_by(line) != "them":
            return False
        her = str(party.get("wxid") or "")
        hers = {her} | {str(w) for w in side or ()}
        if sender:
            return sender in hers
        if self.boss_said(row, line):
            return False
        said = " ".join(str(line or "").split()).split(edge_text.RECALL_MARK, 1)[0]
        who = _key(said.strip().strip('"“”「」\'').strip())
        members = row.get("members") or []
        if who and who in {_key(m.get("name")) for m in members if m.get("wxid") in hers} | {_key(party.get("name"))}:
            return True
        known = {_key(m.get("name")) for m in members if m.get("wxid") not in hers}
        known |= {_key(b.get("name")) for b in row.get("boss") or []} | {_key(n) for n in self._with}
        if who and who in known - {""}:
            return False
        return recent if row.get("manual") else True

    def hide(self, text) -> str:
        """``text`` with each boss's name (PRM_GROUP_WITH: spaces and case aside) as PRM_BOSS_CALL: what goes to the Mac
        (a group job's name, the lines it is told, her words and ours there) never has the bosses' names."""
        text = str(text or "")
        if not text:
            return text
        names = tuple(sorted({n for n in self._with if _key(n)}, key=len, reverse=True))
        if names != self._hide_for:
            self._hide_re = re.compile("|".join(r"\s*".join(re.escape(ch) for ch in n if not ch.isspace())
                                                for n in names), re.IGNORECASE) if names else None
            self._hide_for = names
        pattern = self._hide_re
        return pattern.sub(lambda m: self._call, text) if pattern is not None else text

    def record_msg(self, msg: dict, party: Optional[dict] = None, row: Optional[dict] = None) -> dict:
        """A forwarded 聊天记录 (src/edge/text.py ``record_of``) told where the bosses' names never go to the Mac (a 老板拉的
        群 and her lines there, her 三方群, a creator the boss introduced: ``hide``'s places): a copy of ``msg`` whose
        record calls each person 老板 [PRM_BOSS_CALL] (a boss: his wxid, or a hash of it, among the ids WeChat wrote of
        the line, or a name of PRM_GROUP_WITH, or his name in this account), 她 (``party``: her wxid or its hash, her
        称呼, her name as a member of ``row``), 我们 (this account), and anybody else 甲, 乙 … in the order they first
        speak (OTHER_LABELS; then OTHERS_LABEL).  Forwarded by one of the bosses or by her, with two people in it of
        whom the other is known: the one left is who forwarded it.  The names of the people found are their labels in
        the words too (two characters or more), and the record's title (it may name anybody) is left out.  Any other
        message: ``msg`` itself."""
        record = edge_text.record_of(msg)
        if record is None:
            return msg
        party, row = party or {}, row or {}
        her = str(party.get("wxid") or "")
        bosses = ({str(b.get("wxid") or "") for b in row.get("boss") or []} | set(self._bosses)) - {""}
        boss_ids = set().union(*(_hashes(w) for w in bosses)) if bosses else set()
        own = ""
        if callable(getattr(self._backend, "own_wxid", None)):
            try:
                own = str(self._backend.own_wxid() or "")
            except Exception:
                own = ""
        boss_keys = ({_key(n) for n in self._with} | {_key(b.get("name")) for b in row.get("boss") or []}
                     | {_key(n) for n in self._bosses.values()}) - {""}
        her_keys = ({_key(party.get("name"))} | {_key(m.get("name")) for m in row.get("members") or []
                                                 if her and m.get("wxid") == her}) - {""}
        sender = str(msg.get("sender_id") or "")
        if her and sender == her:
            her_keys = (her_keys | {_key(msg.get("sender_name"))}) - {""}
        own_keys = {_key(getattr(self._config, "bot_display_name", ""))} - {""}

        def role(item: dict) -> str:
            ids = {str(i).lower() for i in item.get("ids") or []}
            key = _key(item.get("name"))
            if ids & boss_ids or key in boss_keys:
                return "boss"
            if ids & _hashes(her) or key in her_keys:
                return "her"
            if ids & _hashes(own) or key in own_keys:
                return "us"
            return ""

        items = [i for i in record.get("items") or [] if isinstance(i, dict)]
        roles: dict[str, str] = {}                     # each person by the key of the name the record gives
        spoken: dict[str, set] = {}                    # ... and the names it gives them
        for item in items:
            key = _key(item.get("name"))
            if not key:
                continue
            spoken.setdefault(key, set()).add(" ".join(str(item.get("name") or "").split()))
            roles[key] = roles.get(key) or role(item)
        forwarded = ("boss" if sender in bosses or self.who(row, sender, str(msg.get("sender_name") or "")) == BOSS
                     else "her" if her and sender == her else "")
        unknown = [k for k, r in roles.items() if not r]
        if forwarded and len(roles) == 2 and len(unknown) == 1 and forwarded not in roles.values():
            roles[unknown[0]] = forwarded               # (her chat with him, forwarded by one of the two)
        labels, others = {"boss": self._call, "her": HER_LABEL, "us": US_LABEL}, iter(OTHER_LABELS)
        called = {key: labels[r] if r else next(others, OTHERS_LABEL) for key, r in roles.items()}
        by_name = {n: called[key] for key, names in spoken.items() for n in names}
        pattern = _names_re(by_name)
        lookup = {"".join(n.split()).casefold(): label for n, label in by_name.items()}

        def unnamed(words: str) -> str:
            if pattern is None:
                return words
            return pattern.sub(lambda m: lookup.get("".join(m.group(0).split()).casefold(), m.group(0)), words)

        told = [{"name": called.get(_key(i.get("name")), "") if _key(i.get("name")) else "",
                 "text": unnamed(str(i.get("text") or "")), "ids": []} for i in items]
        special = dict(msg.get("special") or {})
        special["record"] = {**record, "title": "", "items": told}
        return {**msg, "special": special}

    def note_line(self, chat: str, written: float) -> None:
        """A line of the group was handled, ours too (where the watch starts after a restart: ``_since``)."""
        self._store.note_boss_group_line(chat, written)

    def add_context(self, chat: str, who: str, text: str, at: float) -> Optional[int]:
        """The boss's (or another member's) line, masked, for her next job (``job_group``); its number.  At most LINE_CHARS
        (a forwarded 聊天记录: RECORD_LINE_CHARS, ``line_chars``)."""
        text = str(text or "")
        return self._store.add_boss_group_line(chat, {"who": who, "at": float(at), "text": text[:line_chars(text)]},
                                               LINES_KEPT)

    def job_group(self, chat: str) -> dict:
        """A group job's ``meta.group``: ``{"name", "boss", "lines"}``: the group's name and ``boss`` [PRM_BOSS_CALL] (the
        bosses' names never go to the Mac: ``hide``), ``lines`` the boss's and the other members' lines kept and not
        answered yet, at most LINES_TOLD (the newest), oldest first, each ``{"who": 老板 | 群成员, "at": "MM-DD HH:MM",
        "text", "seq"}`` (masked, at most LINE_CHARS, a forwarded 聊天记录 RECORD_LINE_CHARS; ``seq`` its number: a reply
        sent drops these, ``replied``).  A group linked by hand (接手任何合作群) also has ``manual`` True and ``others``:
        how many are in it besides her, us and a boss; ``boss`` only when one of PRM_GROUP_WITH is in it ([] else); its
        ``lines`` are the newest LINES_TOLD_MANUAL and may be hers (她) and ours (我们) too (the catch-up's).  她那边的人
        (PRM_GROUP_HER_SIDE on): ``her_side`` True (the lines of her side's other people are drafted too, each marked
        HER_SIDE_MARK in the job's text; a kept one's ``who`` is HER_SIDE); no key at all with the switch off."""
        row = self._store.boss_group(chat) or {}
        manual = bool(row.get("manual"))
        lines = [x for x in row.get("lines") or [] if isinstance(x, dict)][-(LINES_TOLD_MANUAL if manual else LINES_TOLD):]
        out = {"name": self.hide(row.get("name"))[:NAME_CHARS], "boss": [self._call],
               "lines": [{"who": str(x.get("who") or MEMBER), "at": _clock_text(x.get("at")),
                          "text": self.hide(x.get("text"))[:line_chars(x.get("text"))], "seq": int(x.get("seq") or 0)}
                         for x in lines]}
        if manual:
            party = self._store.party(int(row.get("party_id") or 0)) if row.get("party_id") else None
            her = str((party or {}).get("wxid") or "")
            bosses = self._bosses_in(row)
            out.update(manual=True, others=sum(1 for m in row.get("members") or []
                                               if m.get("wxid") not in bosses and m.get("wxid") != her))
            if not bosses:
                out["boss"] = []
        if self._her_side:
            out["her_side"] = True
        return out

    def has_boss(self, row: dict) -> bool:
        """Is a boss of PRM_GROUP_WITH among this group's members (``_bosses_in``)?  A group linked by hand without one
        is no 三方群 (``_link``) and its lines are no 三方群 lines (PrmService ``_group_line``, review 2026-11-09)."""
        return bool(self._bosses_in(row or {}))

    def _bosses_in(self, row: dict) -> set:
        """The wxids of the bosses (PRM_GROUP_WITH: by wxid, or a member named like one) among a group's members."""
        keys = {_key(n) for n in self._with} - {""}
        known = {str(b.get("wxid") or "") for b in row.get("boss") or []} | set(self._bosses)
        return {str(m.get("wxid") or "") for m in row.get("members") or []
                if m.get("wxid") in known or (keys and _key(m.get("name")) in keys)} - {""}

    def replied(self, chat: str, seqs=None) -> int:
        """We answered in the group: the kept lines numbered ``seqs`` go (the ones the sent reply's job was told:
        ``told_lines``); None (an operator's own line there): every line kept now.  A line kept later stays, whenever it
        was written (the watch's catch-up hands on old lines late).  Returns how many went."""
        return self._store.boss_group_replied(chat, seqs, self._clock())

    def first_time(self, chat: str, message_id, sender: str = "") -> bool:
        """Is this line of a linked group handed on for the first time (the route and ``_replay`` may both have it)?
        ``sender``: who wrote it, kept for a recall of it (``sender_of``), the last SEEN_IDS lines."""
        key = str(message_id or "")
        if not key:
            return True
        with self._seen_lock:
            seen = self._seen.setdefault(str(chat), deque(maxlen=SEEN_IDS))
            if key in seen:
                return False
            seen.append(key)
            if sender:
                said = self._said.setdefault(str(chat), OrderedDict())
                said[key] = str(sender)
                while len(said) > SEEN_IDS:
                    said.popitem(last=False)
            return True

    def sender_of(self, chat: str, message_id) -> str:
        """Who wrote this line of a linked group (``first_time``), "" when it is not known (not handed on lately, or
        before a restart): a recall WeChat wrote over it in place names its row (``recalled_id``)."""
        with self._seen_lock:
            return str((self._said.get(str(chat)) or {}).get(str(message_id or "")) or "")

    # ── the scan ──────────────────────────────────────────────────────────

    def sweep(self) -> Optional[int]:
        """Each round of PRM's sender thread (PrmService.tick): the group 开场白 whose watch caught up (``_openings``),
        then the scan when it is due (every BOSS_SCAN_SEC; the first one at once): ``scan``'s answer, None when it is
        not due.  The groups linked by hand are looked at every BOSS_SCAN_SEC too (is she still in them,
        ``_look_at_linked``) while the scan does not run (the switch off, or it cannot look: ``why_not``).  Then, with
        PRM_GROUPS_AUTO_LINK on, the scan that links groups by itself when it is due (``auto_scan``)."""
        now = self._clock()
        if self._due:
            self._openings(now)
        if self._manual and (not self._on or self.why_not()) and now - self._manual_looked >= BOSS_SCAN_SEC:
            self._manual_looked = now
            try:
                own = str(self._backend.own_wxid() or "") if callable(getattr(self._backend, "own_wxid", None)) else ""
                if own:
                    self._look_at_linked(own, now, manual_only=True)
            except Exception:
                logger.exception("PRM: the groups linked by hand could not be looked at; again in %d s", BOSS_SCAN_SEC)
        taken = None if self._scanned and now - self._scanned < BOSS_SCAN_SEC else self.scan()
        if self._auto and (not self._auto_scanned or now - self._auto_scanned >= BOSS_SCAN_SEC):
            self.auto_scan()                   # 群自己接上: after the boss scan (a new 老板拉的群 is its to take over)
        return taken

    def scan(self) -> Optional[int]:
        """One scan now: None when it did not look (off, the backend cannot, something in the way: ``why_not``), else how
        many groups it took over (the first run only keeps the baseline: 0).  Never raises."""
        if not self._on or not self._with or not self._can_scan():
            return None
        now = self._clock()
        self._scanned = now
        try:
            with self._lock:
                return self._scan(now)
        except Exception:
            logger.exception("PRM: boss groups: the scan failed; it looks again in %d s", BOSS_SCAN_SEC)
            return None

    def _baseline(self) -> Optional[set]:
        raw = self._store.meta_value(BASELINE)
        if raw in (None, ""):
            return None
        try:
            ids = json.loads(raw)
        except ValueError:
            ids = []
        return {str(i) for i in ids} if isinstance(ids, list) else set()

    def _baseline_at(self) -> Optional[float]:
        return _num(self._store.meta_value(BASELINE_AT))

    def _scan(self, now: float) -> Optional[int]:
        names = self._names()
        if not names:
            return None
        baseline = self._baseline()
        if baseline is None:                   # the first run: the groups there are now are never "new"
            ids = self._backend.group_ids()
            if ids is None:
                self._why = "微信数据库现在读不了，能读了以后才开始找老板拉的群"
                return None
            self._store.set_meta_value(BASELINE, json.dumps(sorted(str(i) for i in ids)))
            self._store.set_meta_value(BASELINE_AT, repr(float(self._wechat_clock(now))))
            self._why = ""
            logger.info("PRM: boss groups: the %d group(s) there are now are never taken over (the baseline)", len(ids))
            return 0
        if self._baseline_at() is None:        # (a baseline kept before its time was: it counts from now)
            self._store.set_meta_value(BASELINE_AT, repr(float(self._wechat_clock(now))))
        own = str(self._backend.own_wxid() or "")
        if not own:
            self._why = "还不知道这个微信号自己的 wxid（微信数据库还没读到），稍后自动再找"
            return None
        self._read_bosses(names, now)
        if not self._bosses:                   # (no boss known at all: nothing can be looked for)
            self._why = (NO_READ_WHY if self._ids_failed else
                         UNKNOWN_WHY.format(names=self._quoted(self._unknown or names))
                         + "：要和这个微信号里他的备注或名字一样，而且只有一个人叫这个名字")
            return None
        # A boss known, another name no one contact's: the scan goes on, no group is taken over by itself; the console
        # warns (``warning``) and its buttons stay on (second review 2026-10-31 F3)
        self._why, self._warning = "", (self._unknown_why() if self._unknown else "")
        # The deals' own 三方群 (「拉三方群」: her, the boss and us, like a boss group) are never "new": kept in the
        # baseline as soon as a deal has one, so a deal's clean-up (it drops the group's id) does not make it new.
        made = {str((d.get("three_party") or {}).get("chat") or "") for d in self._store.deals()} - {""}
        if made - baseline:
            baseline |= made
            self._store.set_meta_value(BASELINE, json.dumps(sorted(baseline)))
        # (a group kept waiting for its first lines is asked for again: the backend reads its lines anew, ``_found``)
        known = baseline | {r["chat"] for r in self._store.boss_groups() if not waits_for_lines(r)} | self._looked
        there = self._group_ids()              # (the groups it gives none of back were looked at: ``_note_looked``)
        found = self._backend.boss_groups(set(self._bosses), known, self._max)
        if found is None:
            self._why = "微信数据库现在读不了，老板拉的群稍后再找"
            return None
        self._note_looked(there, known, found)
        taken = 0
        for group in reversed(list(found)):    # (newest first from the backend: the oldest is seen to first)
            taken += self._found(group, own, now) if isinstance(group, dict) else 0
        taken += self._look_again(own, now)
        self._look_at_linked(own, now)
        self._openings(now)
        return taken

    def _names(self) -> list[str]:
        """PRM_GROUP_WITH as the config says it now (another value: the bosses are read again, ``_read_bosses``)."""
        if self._config is not None:
            self._with = group_with(getattr(self._config, "prm_group_with", ""))
        return self._with

    @staticmethod
    def _quoted(names) -> str:
        return "、".join(f"「{n}」" for n in names)

    def _unknown_why(self) -> str:
        """The console's ``warning`` (and a group's ``why``) while a name of PRM_GROUP_WITH is no one contact's."""
        return (UNKNOWN_WHY.format(names=self._quoted(self._unknown))
                + "：认出来以前，老板拉的群不会自动接手（找到的群在这里等你点「是她」）")

    def _wechat_clock(self, now: float) -> float:
        """This moment on WeChat's clock (the baseline's time, ``BASELINE_AT``; second review 2026-10-31 F2: a PC whose
        clock is hours off took every new group for an old one): the backend's ``wechat_now`` (the newest session time
        of WeChat's session list), else (a backend without it, or nothing there) this PC's clock less the skew this run
        measured (``local``; none until one of our own lines was read back)."""
        ask = getattr(self._backend, "wechat_now", None)
        if callable(ask):
            try:
                at = _num(ask())
            except Exception:
                logger.debug("PRM: boss groups: WeChat's time could not be read", exc_info=True)
                at = None
            if at is not None and at > 0:
                return at
        return now - (self._local(now) - now)

    def _readable(self) -> bool:
        readable = getattr(self._backend, "db_readable", None)
        if not callable(readable):
            return True
        try:
            return bool(readable())
        except Exception:
            return False

    def _group_ids(self) -> Optional[set]:
        try:
            ids = self._backend.group_ids()
        except Exception:
            return None
        return {str(i) for i in ids} if ids is not None else None

    def _read_bosses(self, names: list, now: float) -> None:
        """The bosses' wxids (``contact_ids``: it reads every group's members) once BOSS_IDS_SEC (BOSS_IDS_RETRY_SEC while
        a name is no one contact's), at once for another PRM_GROUP_WITH or when a scan asked (``_ask_again``).  A read
        that failed (an error, the database not readable) is not kept: the next scan asks again."""
        names = tuple(names)
        every = BOSS_IDS_RETRY_SEC if self._unknown else BOSS_IDS_SEC
        if self._bosses_at and names == self._bosses_for and now - self._bosses_at < every:
            return
        try:
            ids = self._backend.contact_ids(list(names))
        except Exception:
            if not self._ids_failed:
                logger.warning("PRM: boss groups: the bosses' wxids could not be read; asked again at each scan",
                               exc_info=True)
            self._ids_failed = True
            return
        if not isinstance(ids, dict) or (not any(ids.values()) and not self._readable()):
            if not self._ids_failed:
                logger.warning("PRM: boss groups: the bosses' wxids could not be read (the database); asked again at "
                               "each scan")
            self._ids_failed = True
            return
        self._ids_failed = False
        unknown = [n for n in names if not ids.get(n)]
        if unknown and unknown != self._unknown:
            logger.info("PRM: boss groups: %d of the %d name(s) of PRM_GROUP_WITH are no one contact's: no group is taken "
                        "over by itself until they are", len(unknown), len(names))
        self._bosses = {str(w): str(n) for n, w in ids.items() if w}
        self._unknown, self._bosses_at, self._bosses_for = unknown, now, names
        self._looked.clear()                   # (who is a boss may have changed: every group is looked at again)
        self._once.clear()

    def _ask_again(self, members: list) -> None:
        """A member named like a boss (PRM_GROUP_WITH) whose wxid is no boss's: the bosses are read again at the next
        scan (once for each such member)."""
        keys = {_key(n) for n in self._with} - {""}
        for m in members:
            wxid = m.get("wxid")
            if wxid in self._bosses or wxid in self._asked_again or _key(m.get("name")) not in keys:
                continue
            self._asked_again.add(wxid)
            self._bosses_at = 0.0
            logger.info("PRM: boss groups: a member named like a boss is no boss known; the bosses are read again at the "
                        "next scan")
            return

    def _note_looked(self, there: Optional[set], known: set, found) -> None:
        """The groups of the session list the backend looked at and gave none of back (no boss in them, or too many
        members): looked at twice, they are left out of the next scans (``known``) until the bosses are read again.  One
        it could not read now (``{"chat", "unreadable": True}``) is given back: never counted (second review 2026-10-31
        F6: before, a group whose lines could not be read twice was not looked at for an hour)."""
        if there is None:
            return
        handed = {str(g.get("chat") or "") for g in found or [] if isinstance(g, dict)}
        quiet = {chat for chat in there if chat not in known and chat not in handed}
        self._looked |= quiet & self._once
        self._once = quiet - self._looked

    def _judge(self, group: dict) -> str:
        """Is this group new for us (second review 2026-10-31 F2, F4, F5)?  WeChat's times, never this PC's clock,
        against the baseline's (``BASELINE_AT``, on WeChat's clock): NEW, certainly (WeChat's line that we were added,
        ``joined_at``, at most NEW_MARGIN_SEC older than the baseline: the scan may take it over by itself); OLD (we were
        added, or its oldest line read, ``created_at``, more than OLD_SEC before it: 「不是新拉的群」); EMPTY (no line in it
        yet: the backend read its whole history, ``history_complete``, and found none; looked at again at the next
        scans); else MAYBE (no line that we were added, as an old group whose chat was deleted with its history has when
        it comes back, or one of the day before the baseline, or times not known: a person decides)."""
        base = self._baseline_at()
        joined, made = _num(group.get("joined_at")), _num(group.get("created_at"))
        if base is None:
            return MAYBE
        if joined is not None and joined >= base - NEW_MARGIN_SEC:
            return NEW
        if any(at is not None and at < base - OLD_SEC for at in (joined, made)):
            return OLD
        if joined is None and made is None and group.get("history_complete"):
            return EMPTY
        return MAYBE

    def _sure(self, row: dict) -> bool:
        """Is a group kept certainly new (``_judge``'s NEW, by the ``joined_at`` kept with it)?"""
        base, joined = self._baseline_at(), _num(row.get("joined_at"))
        return base is not None and joined is not None and joined >= base - NEW_MARGIN_SEC

    def _present(self, members: list) -> list[dict]:
        """The bosses among ``members`` (``[{"wxid", "name"}]``, their names as PRM_GROUP_WITH writes them)."""
        there = {m["wxid"] for m in members}
        return [{"wxid": w, "name": n} for w, n in self._bosses.items() if w in there]

    def _ours(self, people: list) -> bool:
        """Is one of them a creator whose deal's 「拉三方群」 ended unsure (no group id kept: this may be that very
        group)?"""
        return any(self._group_state(m["wxid"]) == UNSURE for m in people)

    def _making(self, people: list) -> bool:
        """Is 「拉三方群」 making a group for one of them now (her deal's 三方群 ``creating``; a 「先试一下」 makes nothing and
        writes no state)?  This may be that very group: it waits and is looked at again once that is done (second review
        2026-10-31 F8: never ignored for it)."""
        return any(self._group_state(m["wxid"]) == CREATING for m in people)

    def _group_state(self, wxid: str) -> str:
        """Her deal's 三方群 state ("" when she has none, or no deal)."""
        party = self._store.party_by_wxid(wxid, include_inactive=True)
        deal = self._store.deal_of(int(party["id"])) if party is not None else {}
        return str((deal.get("three_party") or {}).get("state") or "")

    def _has_group(self, wxid: str) -> bool:
        """Has she a 三方群 made or unsure (「建好了」 by hand too)?  A group whose one candidate she is may be that very
        one: never taken over (``HAS_GROUP_WHY``)."""
        return self._group_state(wxid) in (UNSURE, CREATED)

    def _decide(self, people: list, sure: bool) -> tuple:
        """What becomes of a boss group that is not old (``_judge``), ``people`` its members but us and the bosses
        (``candidates``), ``sure`` whether it is certainly new: ``(state, why, a note for the log)``.  Only ``(PENDING,
        "")`` with one of them is taken over by the scan itself: certainly new, every boss name known, nothing in the
        way (second review 2026-10-31 F4: any doubt waits for the console's 「是她」, nothing is ignored silently)."""
        if self._making(people):
            return WAITING, MAKING_WHY, ": a 三方群 is being made for her (拉三方群), looked at again once it is done"
        if len(people) == 1 and self._has_group(people[0]["wxid"]):
            return IGNORED, HAS_GROUP_WHY, ": she has a 三方群 (made or unsure), not taken over"
        if len(people) > 1 and self._ours(people):
            return IGNORED, OURS_WHY, ": maybe a deal's own 三方群 (unsure), not taken over"
        if not people:
            return WAITING, ("" if sure else MAYBE_WHY), ""
        if not sure:
            return PENDING, MAYBE_WHY, ": maybe no new group (no line that we were added), the console asks"
        if self._unknown:
            return PENDING, self._unknown_why(), ": a boss name is no one contact's, the console asks"
        return PENDING, "", ""

    def _found(self, group: dict, own: str, now: float) -> int:
        """A group the backend found: kept (``_judge``: ignored when old, waiting when it has no line yet; else
        ``_decide``: pending, waiting or ignored) and, with exactly one person besides us and the bosses, certainly new
        and nothing in the way, taken over.  One the backend could not read now (``unreadable``) is left for the next
        scan, never kept or counted; one kept waiting for its first lines comes back here (``waits_for_lines``).
        Returns 1 when it was taken over."""
        chat = str((group or {}).get("chat") or "")
        if not chat.endswith("@chatroom") or group.get("unreadable"):
            return 0                           # (its lines or members could not be read now: asked for at the next scan)
        members = clean_members(group.get("members"), own)
        present = self._present(members)
        if not present:
            self._looked.add(chat)             # (no boss in it after all: not looked at again until the bosses are read)
            return 0
        row = self._store.boss_group(chat)
        if row is not None and not waits_for_lines(row):
            return 0                           # (kept already)
        self._ask_again(members)
        joined = _num(group.get("joined_at"))
        made = joined if joined is not None else _num(group.get("created_at"))
        name = str(group.get("name") or "")[:NAME_CHARS]
        fields = {"name": name, "made_at": made, "joined_at": joined, "members": members, "boss": present}
        how = self._judge(group)
        if how == EMPTY:
            if row is None and self._keep(chat, None, now, state=WAITING, why=NO_LINES_WHY, **fields):
                logger.info("PRM: boss group %s… found (%d member(s)), no line in it yet; looked at again at each scan",
                            chat[:8], len(members) + 1)
            elif row is not None and members != row["members"]:
                self._store.update_boss_group(chat, now, states=(WAITING,), name=name, members=members, boss=present)
            return 0
        if how == OLD:
            if self._keep(chat, row, now, state=IGNORED, why=NOT_NEW_WHY, **fields):
                logger.info("PRM: boss group %s… found, but it is no new group (begun for us over a day before the "
                            "switch's baseline); not taken over", chat[:8])
            return 0
        people = candidates(members, self._bosses, self._reserved, self._with)
        state, why, note = self._decide(people, how == NEW)
        if not self._keep(chat, row, now, state=state, why=why, **fields):
            return 0
        logger.info("PRM: boss group %s… found (%d member(s), %d boss(es), %d may be her)%s", chat[:8],
                    len(members) + 1, len(present), len(people), note)
        if state == PENDING and len(people) == 1 and not why:
            return int(bool(self.take_over(chat, people[0]["wxid"], people[0]["name"], actor="system").get("ok")))
        return 0

    def _keep(self, chat: str, row: Optional[dict], now: float, **fields) -> bool:
        """A group found kept: a new row, or the row of one that waited for its first lines brought up to date (it keeps
        when it was found).  True when it changed."""
        if row is None:
            return self._store.add_boss_group(chat, found_at=now, now=now, **fields)
        return self._store.update_boss_group(chat, now, states=(WAITING,), **fields)

    def _look_again(self, own: str, now: float) -> int:
        """The groups kept waiting: after BOSS_WAIT_DAYS they are ignored.  One with no line yet is handed back by the
        backend at each scan (``_found``); the others' members are read again, and each is decided again (``_decide``:
        she came in, or the 三方群 being made for her is done: taken over, pending or ignored; else it waits on)."""
        taken = 0
        for row in self._store.boss_groups((WAITING,)):
            chat = row["chat"]
            if max(float(row["found_at"]), float(row.get("updated_at") or 0)) >= now:
                continue                       # (found, or decided, in this very scan: its members were just read)
            quiet = waits_for_lines(row)
            if now - float(row["found_at"]) > BOSS_WAIT_DAYS * 86400:
                self._store.update_boss_group(chat, now, states=(WAITING,), state=IGNORED,
                                              why=QUIET_WHY if quiet else NOBODY_WHY)
                logger.info("PRM: boss group %s…: %s in %d days; not looked at any more", chat[:8],
                            "no line" if quiet else "nobody but us and the boss", BOSS_WAIT_DAYS)
                continue
            if quiet:
                continue                       # (asked for again through ``boss_groups``: its lines and its members)
            raw = self._backend.group_members(chat) if callable(getattr(self._backend, "group_members", None)) else None
            if raw is None:
                continue
            members = clean_members(raw, own)
            self._ask_again(members)
            people = candidates(members, set(self._bosses) | {b.get("wxid") for b in row["boss"]}, self._reserved,
                                self._with)
            state, why, note = self._decide(people, self._sure(row))
            if state == WAITING:
                if (members, why) != (row["members"], row["why"]):
                    self._store.update_boss_group(chat, now, states=(WAITING,), members=members, why=why)
                continue
            boss = self._present(members) or row["boss"]
            self._store.update_boss_group(chat, now, states=(WAITING,), state=state, members=members, boss=boss,
                                          why=why)
            logger.info("PRM: boss group %s…: %d may be her now%s", chat[:8], len(people), note)
            if state == PENDING and len(people) == 1 and not why:
                taken += int(bool(self.take_over(chat, people[0]["wxid"], people[0]["name"], actor="system").get("ok")))
        return taken

    def _look_at_linked(self, own: str, now: float, manual_only: bool = False) -> None:
        """Is she still in each group the bot took over (``manual_only``: each one linked by hand)?  Gone: her deal says
        so (until she is back).  The row's members are the group's now (second check 2026-11-09: a recall is told apart
        by the names this account sees, ``recall_is_hers``; the bosses kept, ``boss``, stay)."""
        if not callable(getattr(self._backend, "group_members", None)):
            return
        for row in self._store.boss_groups((LINKED,)):
            if manual_only and not row.get("manual"):
                continue
            party = self._store.party(int(row["party_id"] or 0))
            deal = self._store.deal(int(row["deal_id"] or 0))
            raw = self._backend.group_members(row["chat"]) if party is not None and deal is not None else None
            if raw is None:
                continue
            members = clean_members(raw, own)
            there = any(m["wxid"] == party["wxid"] for m in members)
            if members and members != row["members"]:
                self._store.update_boss_group(row["chat"], now, states=(LINKED,), members=members)
            key = LEFT_NOTICE + row["chat"]
            has = any(n.get("key") == key for n in deal["notices"])
            if not there and not has:
                self._store.set_notice(deal["id"], key, MANUAL_LEFT_TEXT if row.get("manual") else
                                       "她已经不在老板拉的群里了：那个群里不会再有她的消息可起草，需要的话在微信里看一眼", now)
                logger.info("PRM: boss group %s…: party %d is not in it any more; her deal says so", row["chat"][:8],
                            party["id"])
            elif there and has:
                self._store.drop_notices(deal["id"], lambda n: n.get("key") == key, now)

    # ── taking over ───────────────────────────────────────────────────────

    def take_over(self, chat: str, wxid: str, name: str = "", *, actor: str = "system") -> dict:
        """She (``wxid``; ``name``: as this account sees her) is this group's creator: registered unless she is an active
        creator already, the group linked to her deal (``_link``), watched (``watch_group``), the lines kept before
        handed on (``_replay``), the deal's record (``EVENT``), one log line without names; the group 开场白 is asked
        for once the watch caught up (``_openings``).  ``actor``: system (the scan: a stopped creator is left for the
        console; one with a 三方群 made or unsure, looked at again right before the link under ThreeParty's lock, is not
        taken over; while 「拉三方群」 makes one for her, never a 「先试一下」, the group waits and is looked at again once that
        is done) or console (``pick``).  ``{"ok": True, "party_id", "deal_id"}`` or ``{"ok": False, "error"}`` (the group
        is pending then, ``why`` says it; ignored for her 三方群; waiting while hers is being made)."""
        with self._lock:
            row = self._store.boss_group(str(chat or ""))
            if row is None or row["state"] not in (PENDING, WAITING):
                return {"ok": False, "error": "这个群不在待认领的名单里（可能已经接手了，或者不管了）"}
            wxid = str(wxid or "").strip()
            # (registered and not linked yet, her new-friend catch-up may be read: it is masked all the same, ``masks``)
            self._taking = self._taking | {wxid}
            try:
                return self._take_over(row, wxid, name, actor)
            finally:
                self._taking = self._taking - {wxid}

    def _take_over(self, row: dict, wxid: str, name: str, actor: str) -> dict:
        """``take_over`` (under ``_lock``)."""
        now = self._clock()
        party = self._store.party_by_wxid(wxid)
        made = False
        if party is None:
            if actor != "console" and self._store.party_by_wxid(wxid, include_inactive=True) is not None:
                return self._held(row, STOPPED_WHY, "a stopped creator", now)
            if self._register is None:
                return self._held(row, "机器人这边没法登记达人", "no registering", now)
            try:
                party = self._register(wxid, her_name(name, wxid), her_note(row["name"]))
            except PartyError as e:
                return self._held(row, f"没登记成：{e}", "registering failed", now)
            made = True
        with self._guard() as busy:            # (「拉三方群」 starts none meanwhile; ``busy``: the deal it makes one for)
            deal = self._store.deal_of(int(party["id"]), now)
            state = str((deal.get("three_party") or {}).get("state") or "")
            if actor != "console" and (state == CREATING or busy == deal["id"]):
                # (this may be the very group it makes: second review 2026-10-31 F8, looked at again once it is done)
                self._store.update_boss_group(row["chat"], now, states=(PENDING, WAITING), state=WAITING,
                                              why=MAKING_WHY)
                logger.info("PRM: boss group %s…: a 三方群 is being made for party %d; looked at again once it is done",
                            row["chat"][:8], party["id"])
                return {"ok": False, "error": MAKING_WHY}
            if actor != "console" and state in (UNSURE, CREATED):
                self._store.update_boss_group(row["chat"], now, states=(PENDING, WAITING), state=IGNORED,
                                              why=HAS_GROUP_WHY)
                logger.info("PRM: boss group %s…: party %d's deal has a 三方群 (made or unsure); not taken over",
                            row["chat"][:8], party["id"])
                return {"ok": False, "error": HAS_GROUP_WHY}
            kept = self._link(deal, row, now)
            self._store.update_boss_group(row["chat"], now, states=(PENDING, WAITING), state=LINKED,
                                          party_id=int(party["id"]), deal_id=int(deal["id"]), why="", opening_due=now)
            self._linked = {**self._linked, row["chat"]: int(party["id"])}
        self._store.add_event(deal["id"], party["id"], EVENT,
                              {"name": row["name"], "chat": row["chat"], "new": made, **({"kept": True} if kept else {})},
                              actor="console" if actor == "console" else "system", now=now)
        since = self._since(row)
        self._watch(row["chat"], since)
        self._replay(row["chat"], since, now)  # (her words before it: a reply job, then no 开场白)
        self._due = {**self._due, row["chat"]: now}
        logger.info("PRM: boss group %s… taken over (%s): party %d %s%s; its 开场白 once its lines so far are read",
                    row["chat"][:8], "the console" if actor == "console" else "the scan", party["id"],
                    "registered" if made else "linked", ", her deal keeps its 三方群" if kept else "")
        self._openings(now)                    # (a backend whose watch caught up at once)
        return {"ok": True, "party_id": int(party["id"]), "deal_id": int(deal["id"])}

    def _held(self, row: dict, why: str, code: str, now: float) -> dict:
        """Not taken over: the group waits for the console (``pending``, ``why``)."""
        self._store.update_boss_group(row["chat"], now, states=(PENDING, WAITING), state=PENDING, why=why[:200])
        logger.info("PRM: boss group %s… not taken over (%s); the console asks", row["chat"][:8], code)
        return {"ok": False, "error": why}

    def _link(self, deal: dict, row: dict, now: float, manual: bool = False, why: str = MANUAL_WHY) -> bool:
        """The deal's 三方群 is this group (``by`` boss; ``manual``, a group linked by hand: ``by`` manual, at ``now``),
        unless it has one made, being made or unsure (True: kept).  Linked by hand, her own 三方群 (「拉三方群」 made it,
        read only till now) is that group: it is drafted in from now on (``by`` manual, what it was kept as ``was``:
        「不接群」 puts it back, PrmService.unlink_group; not kept).  A group with no boss of PRM_GROUP_WITH in it is no
        三方群 (kb 第 0.5 节: the samples are the boss's there): the deal's stays as it is (none: 「拉三方群」 still makes
        one), the console lists the group with the deal (``linked_groups``).  ``why``: the 三方群's ``why`` of a group
        linked so (MANUAL_WHY; the scan's own link, 群自己接上: AUTO_LINK_WHY)."""
        group = deal.get("three_party") or {}
        state = str(group.get("state") or "")
        if manual and state == CREATED and group.get("chat") == row["chat"]:
            # (what it was is kept, ``was``: 「不接群」 puts it back, a read only 三方群 again, PrmService.unlink_group)
            was = {k: group[k] for k in ("by", "at", "why") if k in group}
            kept = {k: v for k, v in group.items() if k != "was"}
            self._store.update_deal(deal["id"], now,
                                    three_party={**kept, "at": now, "by": MANUAL, "why": why, "was": was})
            return False
        if state in (CREATED, CREATING, UNSURE):
            return True
        if manual and not self._bosses_in(row):
            return False
        self._store.update_deal(deal["id"], now, three_party={
            "state": CREATED, "at": now if manual else row["found_at"], "by": MANUAL if manual else "boss",
            "chat": row["chat"], "name": row["name"], "members": len(row["members"]) + 1,
            "why": why if manual else "老板拉的群"})
        self._store.drop_notices(deal["id"], lambda n: n.get("key") == GROUP_NOTICE, now)
        return False

    def _since(self, row: dict) -> float:
        """Where the backend starts reading the group: the second after its last line handled (ours too: read back
        before a stop, it is not handed on again); else BOSS_WATCH_BACK_SEC before it was found, or its oldest line
        (``made_at``: one made while the bot was stopped, the boss's introduction is read too), up to PRM_CATCHUP_HOURS
        back."""
        if row.get("last_line_at"):
            return float(row["last_line_at"]) + 1
        found = float(row["found_at"])
        start = min(found - BOSS_WATCH_BACK_SEC, float(row.get("made_at") or found))
        return max(start, found - self._back_sec)

    def _replay(self, chat: str, since: float, now: float) -> int:
        """The group's lines the bot's message store kept from before the takeover (WECHAT_GROUPS read it: the route
        kept them and said nothing; the watch skips stored lines) handed on once, oldest first (``handle``: the boss's
        introduction is context for her next job, her own words a reply job; none twice: ``first_time``).  Never ours.
        Returns how many."""
        if self._stored is None or self._handle is None:
            return 0
        try:
            rows = self._stored(chat, int(since), until_ts=int(now) + 1, limit=REPLAY_MAX) or []
        except Exception:
            logger.warning("PRM: boss group %s…: the lines kept before the takeover could not be read", chat[:8],
                           exc_info=True)
            return 0
        own = str(self._backend.own_wxid() or "") if callable(getattr(self._backend, "own_wxid", None)) else ""
        done = 0
        for row in rows:
            if not isinstance(row, dict) or (own and str(row.get("sender_id") or "") == own):
                continue
            try:
                self._handle({**row, "chat_id": chat, "is_group": True})
                done += 1
            except Exception:
                logger.exception("PRM: boss group %s…: a line kept before the takeover could not be handed on", chat[:8])
        if done:
            logger.info("PRM: boss group %s…: %d line(s) kept before the takeover handed on", chat[:8], done)
        return done

    def _watch(self, chat: str, since: float) -> None:
        watch = getattr(self._backend, "watch_group", None)
        if not callable(watch):
            logger.warning("PRM: boss group %s…: this WeChat backend cannot read it; her lines there are not drafted",
                           chat[:8])
            return
        try:
            watch(chat, since)
        except Exception:
            logger.warning("PRM: boss group %s…: the backend could not start reading it", chat[:8], exc_info=True)

    def _openings(self, now: float) -> int:
        """The group 开场白 of each group taken over whose lines so far reached PRM: once the backend says the watch's
        catch-up ran (``watch_caught_up``: the boss's introduction is in the job's context); a backend that cannot say
        (or cannot watch), OPENING_WAIT_SEC after the takeover.  Each is asked for once.  Returns how many jobs."""
        if not self._due or self._opening is None:
            return 0
        caught = getattr(self._backend, "watch_caught_up", None)
        say = callable(caught) and callable(getattr(self._backend, "watch_group", None))
        asked = 0
        for chat, since in list(self._due.items()):
            if say:
                try:
                    ready = bool(caught(chat))
                except Exception:
                    ready = False
            else:
                ready = now - since >= OPENING_WAIT_SEC
            if ready:
                asked += self._ask_opening(chat, now)
        return asked

    def _ask_opening(self, chat: str, now: float) -> int:
        with self._lock:
            if chat not in self._due:
                return 0                       # (asked for meanwhile)
            self._due = {k: v for k, v in self._due.items() if k != chat}
            self._store.update_boss_group(chat, now, opening_due=None)
            row = self._store.boss_group(chat)
            party = (self._store.party(int(row["party_id"])) if row is not None and row["state"] == LINKED
                     and row.get("party_id") else None)
            if party is None:
                return 0
            try:
                job, why = self._opening(party, chat)
            except Exception:
                logger.exception("PRM: boss group %s…: its 开场白 could not be asked for", chat[:8])
                return 0
        logger.info("PRM: boss group %s…: %s", chat[:8],
                    f"job #{job} drafts our first line there" if job else f"no 开场白 ({why or 'none'})")
        return int(bool(job))

    def start(self) -> int:
        """At start (PrmService.start): every group the bot took over is read again from the second after its last line
        handled (the backend keeps its watches in memory; a 开场白 not asked for yet waits for this catch-up).  With
        PRM_BOSS_GROUPS off only the groups linked by hand (接手任何合作群).  Returns how many."""
        rows = [r for r in self._store.boss_groups((LINKED,)) if self._on or r.get("manual")]
        for row in rows:
            self._watch(row["chat"], self._since(row))
        if rows:
            logger.info("PRM: boss groups: the %d group(s) taken over are read again", len(rows))
        return len(rows)

    # ── the console (POST /api/prm/boss-groups/pick | ignore) ─────────────

    def pick(self, chat, wxid) -> dict:
        """「是她」: this member of a pending group is the creator (``take_over`` by the console; a creator an operator
        stopped is registered again).  Only a member she may be: not us, not a boss, not the admin or a cc user.  The
        open 「接群」 question's group (接手任何合作群, ``offered``; one the scan asks about, ``asked_by_scan``) is linked
        by hand (``link``), whatever PRM_BOSS_GROUPS says."""
        if self.offered(chat) is not None or self.asked_by_scan(chat) is not None:
            return self.link(chat, wxid, actor="console")
        if not self._on:
            return {"ok": False, "error": "没打开「老板拉的群」（PRM_BOSS_GROUPS）"}
        with self._lock:
            row = self._store.boss_group(str(chat or ""))
            if row is None or row["state"] != PENDING:
                return {"ok": False, "error": "这个群不在待认领的名单里（可能已经接手了，或者不管了）"}
            wxid = str(wxid or "").strip()
            her = next((m for m in candidates(row["members"], set(self._bosses) | {b.get("wxid") for b in row["boss"]},
                                              self._reserved, self._with) if m["wxid"] == wxid), None)
            if her is None:
                return {"ok": False, "error": "只能选这个群里的人（不能选我们自己、老板，也不能选管理员和能用 cc 的人）"}
            return self.take_over(row["chat"], her["wxid"], her.get("name") or "", actor="console")

    def ignore(self, chat) -> dict:
        """「这个群不是达人」: a pending (or waiting) group is never looked at again (``ignored``).  The open 「接群」
        question's group (``offered``; one the scan asks about, ``asked_by_scan``): the question is dropped (「不接」:
        ``decline``, the scan that links groups by itself leaves the group alone from then on), nothing else changes."""
        if self.offered(chat) is not None or self.asked_by_scan(chat) is not None:
            self.decline(chat)
            logger.info("PRM: group %s…: the question who she is was dropped (the console)", str(chat)[:8])
            return {"ok": True}
        if not self._on:
            return {"ok": False, "error": "没打开「老板拉的群」（PRM_BOSS_GROUPS）"}
        with self._lock:
            if not self._store.update_boss_group(str(chat or ""), self._clock(), states=(PENDING, WAITING),
                                                 state=IGNORED, why=IGNORE_WHY):
                return {"ok": False, "error": "这个群不在待认领的名单里（可能已经接手了，或者不管了）"}
            logger.info("PRM: boss group %s… is no creator's (the console); not looked at again", str(chat)[:8])
            return {"ok": True}

    def gone(self, deal: Optional[dict], actor: str = "console") -> bool:
        """「群已经不在了」 on a deal whose 三方群 is a 老板拉的群 the bot took over (``three_party`` with its ``chat``, by the
        boss, PrmService.deal_action ``group_none``; ``deal`` as it was before), or a group linked by hand (``by``
        manual): the group is ``gone``, the backend stops reading it (``unwatch_group``), her lines there are silent
        again, the deal's record says so.  The deal stays, her private chat too.  True when it was such a group."""
        chat = str(((deal or {}).get("three_party") or {}).get("chat") or "")
        with self._lock:
            row = self._store.boss_group(chat) if chat else None
            if row is None or row["state"] != LINKED or int(row.get("deal_id") or 0) != int(deal["id"]):
                return False
            return self._drop(row, int(deal["id"]), int(deal["party_id"]), actor)

    def _drop(self, row: dict, deal_id: int, party_id: int, actor: str, read_only: bool = False,
              stopped: bool = False) -> bool:
        """``gone`` / ``stop`` (``stopped``; under ``_lock``): the group ``gone``, not read any more
        (``unwatch_group``), its drafts not sent and its job not taken given up (``on_close``, review 2026-11-09: a
        draft made before went into the group all the same), the deal's record (``read_only``: her 三方群 goes back to
        being read only, PrmService.unlink_group).  The row keeps of its members only her and the bosses (``boss``, the
        bosses' wxids as last read) and none of its lines (review 2026-11-10 B-L5: a big group linked by hand kept every
        member's wxid and name)."""
        now = self._clock()
        chat = row["chat"]
        manual = bool(row.get("manual"))
        party = self._store.party(int(party_id)) if party_id else None
        keep = {str((party or {}).get("wxid") or "")} | {str(b.get("wxid") or "") for b in row.get("boss") or []}
        keep = (keep | set(self._bosses)) - {""}
        members = [m for m in row.get("members") or [] if str(m.get("wxid") or "") in keep]
        if not self._store.update_boss_group(chat, now, states=(LINKED,), state=GONE,
                                             why=MANUAL_GONE_WHY if stopped else GONE_WHY, opening_due=None,
                                             members=members, lines=[]):
            return False
        self._linked = {k: v for k, v in self._linked.items() if k != chat}
        self._manual = self._manual - {chat}
        self._due = {k: v for k, v in self._due.items() if k != chat}
        unwatch = getattr(self._backend, "unwatch_group", None)
        if callable(unwatch):
            try:
                unwatch(chat)
            except Exception:
                logger.warning("PRM: boss group %s…: the backend could not stop reading it", chat[:8], exc_info=True)
        if self._on_close is not None:
            try:
                self._on_close(chat, now)
            except Exception:
                logger.exception("PRM: boss group %s…: its drafts could not be given up", chat[:8])
        self._store.add_event(deal_id, party_id, EVENT, {"gone": True, "chat": chat, **({"manual": True} if manual else {}),
                                                         **({"auto": True} if row.get("manual") == AUTO else {}),
                                                         **({"stopped": True} if stopped else {}),
                                                         **({"read_only": True} if read_only else {})},
                              actor=actor if actor in ("console", "admin") else "system", now=now)
        logger.info("PRM: %s %s… is gone (%s): party %d's lines there are not drafted any more",
                    "group linked by hand" if manual else "boss group", chat[:8], actor, party_id)
        return True

    # ── 接手任何合作群 (2026-11-09): an operator links a group by hand (src/prm/group_link.py) ──────────

    def candidates_of(self, chat: str) -> Optional[dict]:
        """Who of a group's members she may be, for a link by hand: ``{"all": its members but us (``clean_members``),
        "boss": the bosses of PRM_GROUP_WITH among them (``_present``), "members": the ones she may be (``candidates``:
        never us, a boss, the admin or a cc user), each with ``registered`` (an active creator) and ``stopped`` (one an
        operator stopped), the registered first}``.  The bosses' wxids are read when due (``_read_bosses``).  None when
        its members cannot be read now, or this account's wxid is not known yet (we could not be told apart)."""
        backend = self._backend
        read = getattr(backend, "group_members", None)
        own = str(backend.own_wxid() or "") if callable(getattr(backend, "own_wxid", None)) else ""
        if not callable(read) or not own:
            return None
        raw = read(str(chat or ""))
        if raw is None:
            return None
        with self._lock:
            names = self._names()
            if names and callable(getattr(backend, "contact_ids", None)):
                self._read_bosses(names, self._clock())
            members = clean_members(raw, own)
            present = self._present(members)
            people = candidates(members, set(self._bosses), self._reserved, names)
        flagged = []
        for m in people:
            party = self._store.party_by_wxid(m["wxid"], include_inactive=True)
            flagged.append({**m, "registered": bool(party and party.get("active")),
                            "stopped": bool(party and not party.get("active"))})
        flagged.sort(key=lambda m: not m["registered"])        # (the registered first, else as WeChat lists them)
        return {"all": members, "boss": present, "members": flagged}

    def offer(self, chat: str, name: str, size, found: dict, deal_id: Optional[int] = None) -> dict:
        """The admin's 「接群」 asks who in this group she is (``found``: ``candidates_of``; ``deal_id``: the deal whose own
        三方群 it is, she alone): the open question, for 「是 N」 and the console's card (``status``), until OFFER_SEC
        after it is asked or answered.  One at a time: a new one replaces it."""
        offer = {"chat": str(chat), "name": str(name or "")[:NAME_CHARS], "size": size, "at": self._clock(),
                 "members": [dict(m) for m in found.get("members") or []], "all": list(found.get("all") or []),
                 "boss": list(found.get("boss") or []), "deal_id": deal_id}
        self._offer = offer
        logger.info("PRM: group %s…: the admin asks which of %d member(s) she is%s", offer["chat"][:8],
                    len(offer["members"]), " (her deal's own 三方群)" if deal_id else "")
        return dict(offer)

    def offered(self, chat: Optional[str] = None) -> Optional[dict]:
        """The open 「接群」 question (``offer``; of this group when ``chat`` is given), None when there is none or it is
        older than OFFER_SEC."""
        offer = self._offer
        if offer is None or self._clock() - float(offer["at"]) > OFFER_SEC:
            return None
        if chat is not None and offer["chat"] != str(chat or ""):
            return None
        return offer

    def withdraw(self, chat: Optional[str] = None) -> bool:
        """The open question (of this group when ``chat`` is given) is dropped, the scan's own of that group too
        (``asked_by_scan``).  True when there was one."""
        had = False
        if chat is not None and str(chat or "") in self._asks:
            self._asks = {k: v for k, v in self._asks.items() if k != str(chat or "")}
            had = True
        offer = self._offer
        if offer is None or (chat is not None and offer["chat"] != str(chat or "")):
            return had
        self._offer = None
        return True

    def asked_by_scan(self, chat: Optional[str] = None) -> Optional[dict]:
        """群自己接上: the question the scan asks about this group (two or more registered creators in it: the console's
        card, ``status``; open until a person answers it, the group is linked, or it has fewer creators), or None.
        Without ``chat``: any one of them."""
        if chat is None:
            return next(iter(self._asks.values()), None)
        return self._asks.get(str(chat or ""))

    def decline(self, chat: Optional[str] = None) -> bool:
        """「不接」 said by a person (the admin's, src/prm/group_link.py; the console's on a question, ``ignore``): the
        open question is dropped (``withdraw``) and, with PRM_GROUPS_AUTO_LINK on, the group is remembered as refused
        (``AUTO_STATE`` ``no``): the scan never links it by itself, asks or tells about it again; an operator's own
        「接群」 still links it.  True when there was a question."""
        target = str(chat or "") if chat is not None else str((self._offer or {}).get("chat") or "")
        had = self.withdraw(chat)
        if had and self._auto and target:
            with self._lock:
                kept = self._auto_state()
                kept["no"][target] = self._clock()
                self._auto_save(kept)
        return had

    def link(self, chat, wxid, *, actor: str = "admin") -> dict:
        """「是 N」 (the admin, src/prm/group_link.py) or the console's 「是她」 on the open question (``offered``): this
        member is the creator of this group, linked by hand whatever PRM_BOSS_GROUPS says.  She is registered unless
        she is an active creator already (her 称呼 her name, her note 「手动接的群（<群名>）」; one an operator stopped is
        registered again; never her new-friend catch-up nor a 开场白 of her private chat: review 2026-11-10 A-F4); the
        group is linked to her deal (``_link``: her own 三方群 is drafted in from now on; a deal
        with another group made, being made or unsure keeps it, the record says so), its row ``linked`` and ``manual``
        (``found_at`` now; the lines kept before go), the deal's record says so (``EVENT`` with ``manual``).  Then, out
        of the lock, what was said there lately is read once (``catch_up``: context for her next job, one draft job when
        she waits for an answer) and the backend reads the group from the second after it (``watch_group``).  No
        开场白.  ``{"ok": True, "party_id", "deal_id", "name", "registered", "kept", "caught"}`` (``caught``: the
        catch-up's answer, ``{"read": False}`` when the group could not be read) or ``{"ok": False, "error"}`` (nothing
        changed)."""
        chat = str(chat or "")
        offer = self.offered(chat) or self.asked_by_scan(chat)
        if offer is None:
            if self.is_linked(chat):
                return {"ok": False, "error": "这个群已经接手了"}
            return {"ok": False, "error": f"这个群不在等你选（「接群」问的话 {OFFER_SEC // 60} 分钟内有效）：重新发「接群」"}
        wxid = str(wxid or "").strip()
        her = next((m for m in offer["members"] if m["wxid"] == wxid), None)
        if her is None:
            return {"ok": False, "error": "只能选这个群里的人（不能选我们自己、老板，也不能选管理员和能用 cc 的人）"}
        return self._link_offer(offer, her, actor)

    def _link_offer(self, offer: dict, her: dict, actor: str, auto: bool = False) -> dict:
        """``link``, once the group (``offer``: ``{"chat", "name", "members", "all", "boss"}``) and who she is are
        known; also the scan's own link (``auto``, 群自己接上, ``_auto_link``: nobody is registered, and it only says
        ``{"ok": False, "retry": True}`` while 「拉三方群」 makes a group for her)."""
        chat, wxid = str(offer["chat"]), str(her["wxid"])
        with self._lock:
            if self.is_linked(chat):
                self.withdraw(chat)
                return {"ok": False, "error": "这个群已经接手了"}
            # (registered and not linked yet, her new-friend catch-up may be read: it is masked all the same, ``masks``)
            self._taking = self._taking | {wxid}
            try:
                out = self._link_by_hand(offer, her, actor, auto=auto)
            finally:
                self._taking = self._taking - {wxid}
            if not out.get("ok"):
                return out
            self.withdraw(chat)
        party = out.pop("party")
        caught = None
        if self._catch_up is not None:
            try:
                caught = self._catch_up(party, chat, self.catch_up_since(self._store.boss_group(chat) or {}))
            except Exception:
                logger.exception("PRM: group %s…: what was said there before it was linked could not be read", chat[:8])
        caught = dict(caught) if isinstance(caught, dict) else {"read": False}
        since = caught.get("since")
        since = float(since) if isinstance(since, (int, float)) else self._wechat_time(self._clock())
        # (after a restart the watch starts here too, ``_since``: never the lines from before the link; review
        # 2026-11-09: linked while unreadable, a restart read from BOSS_WATCH_BACK_SEC before the link)
        self.note_line(chat, since - 1)
        self._watch(chat, since)
        return {**out, "caught": {k: v for k, v in caught.items() if k != "since"}}

    def _link_by_hand(self, offer: dict, her: dict, actor: str, auto: bool = False) -> dict:
        """``link`` (under ``_lock``): register her if need be, link the group to her deal, its row, the record.
        ``auto`` (the scan's own link, 群自己接上): only an active creator, never registered here; not while 「拉三方群」
        makes a group for her (``retry``: this may be that very group, looked at again at the next scan), nor when she
        has a linked group meanwhile or her deal is cleaned up; the row's ``manual`` is AUTO, the record says ``auto``
        and why (AUTO_WHY)."""
        now = self._clock()
        chat = offer["chat"]
        party = self._store.party_by_wxid(her["wxid"])
        made = False
        if party is None and auto:
            return {"ok": False, "error": AUTO_GONE_WHY}
        if party is None:
            if self._register is None:
                return {"ok": False, "error": "机器人这边没法登记达人"}
            try:
                # (no 开场白 of her private chat, nothing of it by itself: the operator only linked a group, review
                # 2026-11-10 A-F4; her earlier private lines still one draft that waits, final check A7:
                # ``PrmService.register``'s ``opening``)
                party = self._register(her["wxid"], her_name(her.get("name"), her["wxid"], MANUAL_FALLBACK),
                                       manual_note(offer["name"]), opening=False)
            except PartyError as e:
                return {"ok": False, "error": f"没登记成：{e}"}
            made = True
        with self._guard() as busy:            # (「拉三方群」 starts none meanwhile; ``busy``: the deal it makes one for)
            deal = self._store.deal_of(int(party["id"]), now)
            if auto:
                cannot = self._auto_cannot(party, deal, busy)
                if cannot:
                    return {"ok": False, "error": cannot, **({"retry": True} if cannot == AUTO_MAKING else {})}
            if self._store.boss_group(chat) is None:
                self._store.add_boss_group(chat, name=offer["name"], found_at=now, state=LINKED, members=offer["all"],
                                           boss=offer["boss"], now=now, manual=True)
            self._store.update_boss_group(chat, now, name=offer["name"], found_at=now, state=LINKED,
                                          members=offer["all"], boss=offer["boss"], party_id=int(party["id"]),
                                          deal_id=int(deal["id"]), why="", opening_due=None, lines=[],
                                          manual=AUTO if auto else 1)
            kept = self._link(deal, self._store.boss_group(chat), now, manual=True,
                              why=AUTO_LINK_WHY if auto else MANUAL_WHY)
            self._linked = {**self._linked, chat: int(party["id"])}
            self._manual = self._manual | {chat}
            self._due = {k: v for k, v in self._due.items() if k != chat}
        self._store.add_event(deal["id"], party["id"], EVENT,
                              {"name": offer["name"], "chat": chat, "new": made, "manual": True,
                               **({"auto": True, "why": AUTO_WHY} if auto else {}),
                               **({"kept": True} if kept else {})},
                              actor=actor if actor in ("console", "admin") else "system", now=now)
        logger.info("PRM: group %s… linked %s (%s): party %d %s%s", chat[:8],
                    "by the scan itself: the one registered creator in it" if auto else "by hand", actor, party["id"],
                    "registered" if made else "linked", ", her deal keeps its 三方群" if kept else "")
        return {"ok": True, "party": party, "party_id": int(party["id"]), "deal_id": int(deal["id"]),
                "name": str(party["name"]), "registered": made, "kept": kept}

    def catch_up_since(self, row: dict) -> float:
        """From when a group linked by hand is read once (``link``), on WeChat's clock: PRM_CATCHUP_HOURS back (at least
        BOSS_WATCH_BACK_SEC), and never before the second after its last line handled (a group linked again)."""
        start = self._wechat_time(self._clock()) - self._back_sec
        last = _num(row.get("last_line_at"))
        return max(start, last + 1) if last else start

    def _wechat_time(self, now: float) -> float:
        """This PC's time ``now`` on WeChat's clock, as near as this run knows it (``local``: the skew it measured)."""
        return now - (self._local(now) - now)

    def stop(self, chat: str, actor: str = "admin", read_only: bool = False) -> bool:
        """「不接群 <群名>」 (src/prm/group_link.py) and the console's 「不接了」 (PrmService.unlink_group): the group is not
        read nor drafted in any more, as ``gone`` (``read_only``: it is her 三方群, read only from now on, the record says
        so).  True when it was linked."""
        with self._lock:
            row = self._store.boss_group(str(chat or ""))
            if row is None or row["state"] != LINKED or not self.is_linked(row["chat"]):
                return False
            return self._drop(row, int(row.get("deal_id") or 0), int(row.get("party_id") or 0), actor, read_only,
                              stopped=True)

    # ── 群自己接上 (PRM_GROUPS_AUTO_LINK, 2026-11-11): the groups link themselves when it is clear whose they are ──

    def _can_auto(self) -> bool:
        return all(callable(getattr(self._backend, name, None))
                   for name in ("recent_groups", "group_members", "own_wxid"))

    def _auto_state(self) -> dict:
        """What the scan keeps (``AUTO_STATE`` in prm_meta, read once a run): ``{"at", "base", "told", "asked", "no",
        "owed"}``; ``at`` None until its first look (the baseline)."""
        if self._auto_kept is None:
            try:
                raw = json.loads(self._store.meta_value(AUTO_STATE) or "{}")
            except ValueError:
                raw = {}
            raw = raw if isinstance(raw, dict) else {}
            kept = {"at": _num(raw.get("at")),
                    "base": [str(c) for c in raw.get("base") or [] if isinstance(c, str)]
                    if isinstance(raw.get("base"), list) else []}
            for key in ("told", "asked", "no"):
                found = raw.get(key) if isinstance(raw.get(key), dict) else {}
                kept[key] = {str(c): _num(at) or 0.0 for c, at in found.items()}
            kept["owed"] = [[str(i[0]), str(i[1])] for i in raw.get("owed") or []
                            if isinstance(i, (list, tuple)) and len(i) == 2] if isinstance(raw.get("owed"), list) else []
            self._auto_kept = kept
        return self._auto_kept

    def _auto_save(self, kept: dict) -> None:
        self._auto_kept = kept
        self._store.set_meta_value(AUTO_STATE, json.dumps(kept, ensure_ascii=False, sort_keys=True))

    def auto_scan(self) -> Optional[dict]:
        """One look at every group of this account now (see the module's doc): None when it did not look (the switch
        off, a backend that cannot list groups, this account's wxid not known yet, the database not readable), else
        ``{"linked", "asked", "told", "unread"}``: how many groups it linked by itself, asked about and queued a
        「还没接」 line for, and how many could not be read now (asked for again at the next one).  Never raises."""
        if not self._auto or not self._can_auto():
            return None
        now = self._clock()
        self._auto_scanned = now
        try:
            return self._auto_scan(now)
        except Exception:
            logger.exception("PRM: groups: the scan that links them by itself failed; it looks again in %d s",
                             BOSS_SCAN_SEC)
            return None

    def _auto_never(self, kept: dict) -> tuple:
        """The groups the scan never looks at, and the ones it only links (never asks or tells about):
        ``(never, held)``.  Never: one linked (also a 老板拉的群 whose switch is off now), one a person stopped
        (``gone``) or said is no creator's (IGNORE_WHY), one a person refused (``no``), a deal's own 三方群 (made, being
        made or unsure with its id: read only), and while PRM_BOSS_GROUPS is on one its scan waits on (for her to come
        in, for its lines, for her 三方群 being made).  Held: one waiting on the 「老板拉的群」 card for 「是她」 (that card
        asks already)."""
        never, held = set(kept["no"]), set()
        for row in self._store.boss_groups():
            state = row["state"]
            if state in (LINKED, GONE) or (state == IGNORED and row.get("why") == IGNORE_WHY):
                never.add(row["chat"])
            elif state == WAITING and self._on:
                never.add(row["chat"])
            elif state == PENDING and self._on:
                held.add(row["chat"])
        never |= {str((d.get("three_party") or {}).get("chat") or "") for d in self._store.deals()} - {""}
        return never | set(self._linked), held

    def _auto_cannot(self, party: dict, deal: Optional[dict] = None, busy=None) -> str:
        """Why the one registered creator of a group is not linked by the scan itself ("" = she is): 「拉三方群」 makes a
        group for her now (AUTO_MAKING: looked at again), her deal is cleaned up (AUTO_PURGED), she has a linked group
        the bot reads already (AUTO_HAS_GROUP).  A deal with a 三方群 made or unsure keeps it, as a link by hand does
        (``_link``)."""
        deal = deal if deal is not None else self._store.deal_of(int(party["id"]), self._clock())
        if str((deal.get("three_party") or {}).get("state") or "") == CREATING or (busy is not None
                                                                                   and busy == deal["id"]):
            return AUTO_MAKING
        if deal.get("purged_at"):
            return AUTO_PURGED
        if self.party_has_group(party["id"]):
            return AUTO_HAS_GROUP
        return ""

    def _auto_scan(self, now: float) -> Optional[dict]:
        own = self._own()
        if not own:
            return None
        with self._lock:
            kept = self._auto_state()
            never, held = self._auto_never(kept)
        listed = self._backend.recent_groups(0, skip=never, sizes=False)
        if listed is None:
            return None                        # (the database cannot be read now: no baseline is kept of nothing)
        listed = [g for g in listed if isinstance(g, dict) and str(g.get("chat") or "").endswith("@chatroom")
                  and g["chat"] not in never]
        if kept["at"] is None:                 # the first look: the groups there are now stay quiet while they are
            with self._lock:
                kept["at"] = float(self._wechat_clock(now))
                kept["base"] = sorted({str(g["chat"]) for g in listed} | never)
                self._auto_save(kept)
            logger.info("PRM: groups: the scan that links them by itself starts; the %d group(s) there are now are "
                        "not told about while nothing is said in them", len(kept["base"]))
        active = self._store.active_wxids()
        out = {"linked": 0, "asked": 0, "told": 0, "unread": 0}
        asks: dict[str, dict] = {}
        for group in listed:
            chat = str(group["chat"])
            try:
                what = self._auto_group(kept, group, active, chat in held, asks, now)
            except Exception:                  # (one group's trouble never stops the others': it is looked at again)
                logger.exception("PRM: group %s…: the scan could not look at it; again at the next one", chat[:8])
                self._auto_seen.pop(chat, None)
                what = "unread"
            if what == "unread" and chat in self._asks:
                asks.setdefault(chat, self._asks[chat])
            if what:
                out[what] += 1
        # (a question answered while this scan ran, 「是她」 or 「不接」, is not put back)
        self._asks = {chat: ask for chat, ask in asks.items() if chat not in kept["no"] and chat not in self._linked}
        self._auto_tell(kept)
        if out["linked"] or out["asked"] or out["told"]:
            logger.info("PRM: groups: of %d looked at, %d linked by the scan itself, %d asked about (two or more "
                        "registered creators), %d told as not linked yet; %d not readable now", len(listed),
                        out["linked"], out["asked"], out["told"], out["unread"])
        return out

    def _auto_group(self, kept: dict, group: dict, active: set, held: bool, asks: dict, now: float) -> str:
        """One group of the scan's list: "linked" (it has exactly one registered creator, and she was linked), "asked"
        (two or more: the question is in ``asks``; counted when its line for the admin's chat is queued, once),
        "told" (none it can link by itself, the group new or active since the baseline: its line queued, once),
        "unread" (not readable now), or "" (nothing to do).  ``held``: it waits on the 「老板拉的群」 card, which asks
        who she is already: only linked, never asked or told about."""
        chat = str(group["chat"])
        last_at = _num(group.get("last_at"))
        found = self._auto_read(chat, last_at, now)
        if found is None:
            return "unread"                    # (asked for again at the next scan, never taken for "nobody in it")
        size = len(found["all"]) + 1
        people = found["members"]
        if size > self._max or not people:
            return ""
        creators = [m for m in people if m["wxid"] in active]
        offer = {"chat": chat, "name": str(group.get("name") or "")[:NAME_CHARS], "size": size, "at": now,
                 "deal_id": None, "by": SCAN,
                 "members": [{**m, "registered": m["wxid"] in active} for m in people],
                 "all": list(found["all"]), "boss": list(found["boss"])}
        if len(creators) > 1:
            if held:
                return ""
            asks[chat] = {**offer, "at": (self._asks.get(chat) or offer)["at"]}
            if chat in kept["asked"]:
                return ""
            self._auto_owe(kept, "asked", chat, ASK_NOTICE,
                           {**offer, "creators": [self._store.party_by_wxid(m["wxid"]) for m in creators]}, now)
            return "asked"
        party = self._store.party_by_wxid(creators[0]["wxid"]) if creators else None
        cannot = self._auto_cannot(party) if party is not None else ""
        if party is not None and not cannot:
            return "linked" if self._auto_link(kept, offer, creators[0], party, now) else ""
        fresh = chat not in kept["base"] or (last_at or 0.0) > kept["at"]
        if cannot == AUTO_MAKING or held or chat in kept["told"] or not fresh:
            return ""
        self._auto_owe(kept, "told", chat, NEW_NOTICE if chat not in kept["base"] else ACTIVE_NOTICE,
                       {**offer, "party": party if cannot == AUTO_HAS_GROUP else None}, now)
        return "told"

    def _auto_read(self, chat: str, last_at: Optional[float], now: float) -> Optional[dict]:
        """Who of this group she may be (``candidates_of``), read now when it was not read this run, could not be read
        last time, something was said in it since (a group over PRM_BOSS_GROUP_MAX: not for that), or AUTO_MEMBERS_SEC
        went by; else as last read.  None: not readable now."""
        seen = self._auto_seen.get(chat)
        if (seen is None or seen["found"] is None or now - seen["at"] >= AUTO_MEMBERS_SEC
                or (seen["last_at"] != last_at and not seen["big"])):
            found = self.candidates_of(chat)
            seen = {"at": now, "last_at": last_at, "found": found,
                    "big": found is not None and len(found["all"]) + 1 > self._max}
            self._auto_seen[chat] = seen
        return seen["found"]

    def _auto_link(self, kept: dict, offer: dict, her: dict, party: dict, now: float) -> bool:
        """The group has exactly one registered creator: linked to her deal by the scan itself (``_link_offer`` with
        ``auto``), and the line that says so queued for the admin's chat.  A link that fails is tried at
        AUTO_LINK_TRIES scans a run (one that waits for 「拉三方群」: at every scan)."""
        chat = offer["chat"]
        if self._auto_tries.get(("link", chat), 0) >= AUTO_LINK_TRIES:
            return False
        out = self._link_offer(offer, her, "system", auto=True)
        if not out.get("ok"):
            if not out.get("retry"):
                self._auto_tries[("link", chat)] = self._auto_tries.get(("link", chat), 0) + 1
                logger.warning("PRM: group %s…: the scan could not link it to party %d by itself (%s)", chat[:8],
                               party["id"], out.get("error") or "no reason given")
            return False
        self._auto_owe(kept, "", chat, LINKED_NOTICE, {**offer, "party": party, "out": out}, now)
        return True

    def _auto_owe(self, kept: dict, mark: str, chat: str, kind: str, info: dict, now: float) -> None:
        """A line for the admin's chat is owed for this group (``notice`` words it), and the group is marked (``mark``:
        "told" / "asked"; never said twice).  Kept in prm_meta: it goes at this scan or a later one, a restart between
        too (``_auto_tell``)."""
        text = ""
        if self._notice is not None:
            try:
                text = str(self._notice(kind, info) or "")
            except Exception:
                logger.exception("PRM: group %s…: its line for the admin's chat could not be written", chat[:8])
        with self._lock:
            if mark:
                kept[mark][chat] = now
            if text:
                kept["owed"].append([chat, text])
            self._auto_save(kept)

    def _auto_tell(self, kept: dict) -> int:
        """The lines owed go to the admin's chat, oldest first, AUTO_TELL_MAX a scan.  One WeChat does not take ends
        the round and is tried again at the next scans (AUTO_TELL_TRIES in all, then dropped: the log says so); with no
        admin's chat to tell (``tell`` None) they are dropped at once.  Returns how many went."""
        sent = 0
        while kept["owed"] and sent < AUTO_TELL_MAX:
            chat, text = kept["owed"][0]
            try:
                went = self._tell(text) if self._tell is not None else None
            except Exception:
                logger.exception("PRM: group %s…: its line could not be sent to the admin's chat", chat[:8])
                went = False
            if went is False:
                tries = self._auto_tries.get(("tell", chat), 0) + 1
                self._auto_tries[("tell", chat)] = tries
                if tries < AUTO_TELL_TRIES:
                    break
                logger.error("PRM: group %s…: its line was not sent to the admin's chat after %d tries; dropped",
                             chat[:8], tries)
            elif went is None and not self._auto_unsaid:
                self._auto_unsaid = True
                logger.info("PRM: groups: there is no admin's chat to tell (ADMIN_WXID); the scan's lines are dropped")
            with self._lock:
                kept["owed"] = kept["owed"][1:]
                self._auto_save(kept)
            self._auto_tries.pop(("tell", chat), None)
            sent += int(went is True)
        return sent

    # ── clean-ups (PrmService.purge / purge_due) ─────────────────────────

    def chats_of(self, deal_id) -> list:
        """Every group ever linked to this deal (8.2's clean-up, PrmService.purge; review 2026-11-10 B-L5): the rows that
        are its own now, and the ones its record names (``EVENT`` with ``chat``): a group linked again to another creator
        since is still scrubbed of this one's lines."""
        chats = [r["chat"] for r in self._store.boss_groups(deal_id=int(deal_id))]
        for ev in self._store.events(int(deal_id), limit=None, kinds=(EVENT,)):
            chat = str((ev.get("detail") or {}).get("chat") or "")
            if chat.endswith("@chatroom") and chat not in chats:
                chats.append(chat)
        return chats

    def purge(self, deal: dict) -> int:
        """8.2 clean-up of a deal: its groups' member names, group names and kept lines, the group's name in its record."""
        changed = self._store.scrub_boss_groups(int(deal["id"]), self._clock())
        self._store.clear_event_keys(int(deal["id"]), EVENT, ("name",))
        return changed

    def sweep_old(self, now: float) -> int:
        """Ignored groups' rows go IGNORED_KEEP_DAYS after; their ids join the baseline (never "new" again)."""
        baseline = self._baseline()
        if baseline is None:
            return 0
        gone = self._store.delete_boss_groups(state=IGNORED, before=now - IGNORED_KEEP_DAYS * 86400)
        if gone:
            self._store.set_meta_value(BASELINE, json.dumps(sorted(baseline | set(gone))))
            logger.info("PRM: boss groups: %d ignored group(s) dropped after %d days", len(gone), IGNORED_KEEP_DAYS)
        return len(gone)
