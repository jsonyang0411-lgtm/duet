"""老板拉的群 (PRM_BOSS_GROUPS, default off; 2026-10-31, the user: 「老板那边也有一点人是已经加了微信的，然后老板想把他和达人还有
我们这边也拉个群，让我们来负责和达人对接」, 「全自动接手」, 「起草后人审再发到群里，像之前的加个自动模式」).

Some creators are the boss's WeChat friends, not ours.  He makes a small group: himself (a name of PRM_GROUP_WITH), the
creator and this account; from then on we handle her.  The scan (``sweep``, every BOSS_SCAN_SEC on PRM's sender thread)
asks the WeChat backend for the group chats it does not know (``boss_groups``: neither in the baseline its first run
kept, prm_meta ``boss_groups_baseline``, nor found before, nor looked at twice already with no boss in them or too many
members) that have a boss among their members and at most PRM_BOSS_GROUP_MAX members.  A group that is not new for us
(``_new``: it began, or we were added, more than NEW_MARGIN_SEC before the baseline, or the backend cannot tell: an old
group whose chat was deleted comes back like a new one) is kept ``ignored``.  Her ``candidates`` are the members but us,
the bosses (by wxid, and anyone named like one), the admin and the cc users:

- exactly one: the bot takes over (``take_over``);
- two or more: ``pending``: the console asks which member she is (``pick``; 「这个群不是达人」 is ``ignore``);
- none: ``waiting``: its members are read again each scan for BOSS_WAIT_DAYS (one comes in: taken over), then it is
  ``ignored``.

A name of PRM_GROUP_WITH that is no one contact's (none, or two: ``contact_ids``) stops every automatic takeover: the
console says which name, and the groups found wait for its 「是她」 (``pending``).  A deal's own 三方群 (「拉三方群」 makes
her, the boss and us a group too) is never one: every deal's ``three_party.chat`` goes into the baseline, and a group
whose one candidate has a 三方群 made, being made or unsure (「建好了」 by hand too: no group id kept) is kept ``ignored``;
the takeover checks again right before it links the group, under the lock 「拉三方群」 starts one under
(ThreeParty ``guard``).

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
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from contextlib import nullcontext
from typing import Callable, Optional

from .store import PartyError
from .three_party import CREATED, CREATING, NOTICE as GROUP_NOTICE, UNSURE, group_with

logger = logging.getLogger(__name__)

BOSS_SCAN_SEC = 60                 # the scan runs this often (PRM's sender thread)
BOSS_WAIT_DAYS = 3                 # a group with nobody but us and the boss is looked at this long
BOSS_WATCH_BACK_SEC = 300          # the watch hands on the group's lines from this long before it was found
BOSS_IDS_SEC = 3600                # the bosses' wxids (``contact_ids`` reads every group's members): read again this seldom
BOSS_IDS_RETRY_SEC = 600           # ... while a name of PRM_GROUP_WITH is no one contact's (it may be put right)
OPENING_WAIT_SEC = 90              # a backend that cannot say its watch caught up: the group 开场白 this long after the takeover
NEW_MARGIN_SEC = 600               # a group begun (we were added) up to this long before the baseline still counts as new
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
BASELINE_AT = "boss_groups_baseline_at"    # prm_meta: when (this PC's clock): a group begun before it is not new
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
OURS_WHY = "像是合作单上「拉三方群」建的群（那次正在建，或者不确定建没建成），不接手"
HAS_GROUP_WHY = "她已经有三方群"
NOT_NEW_WHY = "不是新拉的群"
UNKNOWN_WHY = "PRM_GROUP_WITH 里的{names}在这个微信里找不到 / 有重名"


def boss_call(raw) -> str:
    """PRM_BOSS_CALL as the bot uses it: white space collapsed, at most CALL_MAX characters, 「老板」 when empty."""
    return " ".join(str(raw or "").split())[:CALL_MAX] or CALL_DEFAULT


def _key(name) -> str:
    """A name as the backend compares names (src/wechat/wcdb_backend.py ``_name_key``): no spaces, no case."""
    return "".join(str(name or "").split()).casefold()


def _num(value) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def her_name(name, wxid: str) -> str:
    """The 称呼 she is registered with: her name as this account sees her (white space and fillers collapsed, at most 30
    characters), 「老板介绍 <wxid 前 8 位>」 without one."""
    text = " ".join(str(name or "").replace(FILLER, " ").split())[:NAME_MAX].strip()
    return text or f"老板介绍 {str(wxid or '')[:8]}"


def her_note(group_name: str) -> str:
    return f"老板介绍（老板拉的群：{str(group_name or '').strip() or '没有群名'}）"[:200]


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
                    boss_call: str = CALL_DEFAULT) -> dict:
    """What the console shows (GET /api/prm ``boss_groups``; also the stopped bot's, from .env): on or off, why it cannot
    run now, the bosses (PRM_GROUP_WITH) and what the Mac is told they are called (``boss_call``, PRM_BOSS_CALL), the
    groups waiting for an operator to say who she is (``pending``: their name, only the members she may be (a 「是她」
    each: never us, a boss, the admin or a cc user), how many are in it (``size``), when found, why), how many wait for
    her to come in and how many are taken over."""
    rows = store.boss_groups() if store is not None and enabled else []
    pending = [{"chat": r["chat"], "name": r["name"], "found_at": r["found_at"], "why": r["why"],
                "size": len(r["members"]) + 1,
                "members": [{"wxid": m["wxid"], "name": m.get("name") or ""}
                            for m in candidates(r["members"], {b.get("wxid") for b in r["boss"]}, reserved, boss)]}
               for r in rows if r["state"] == PENDING]
    return {"enabled": bool(enabled), "why_not": str(why_not or "") if enabled else "", "boss": list(boss),
            "boss_call": str(boss_call or CALL_DEFAULT), "pending": pending,
            "waiting": sum(1 for r in rows if r["state"] == WAITING),
            "linked": sum(1 for r in rows if r["state"] == LINKED)}


def _clock_text(at) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(at)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


class BossGroups:
    """The 老板拉的群 of one PRM service (see the module's doc).  ``register(wxid, name, note)``: PrmService.register;
    ``opening(party, chat)``: PrmService._group_opening (``(job id, "")`` or ``(None, why not)``); ``handle(msg)``:
    PrmService.handle (a line stored before the takeover, handed on once); ``reserved``: the admin and the cc users;
    ``local(at)``: a time WeChat gives on this PC's clock (PrmService._local); ``guard()``: ThreeParty.guard (the lock
    「拉三方群」 starts a group under, yielding the deal being made now).  ``attach`` gives it the WeChat backend and the
    bot's message store (Bot._start_prm, before PrmService.start)."""

    def __init__(self, store, config=None, *, clock: Callable[[], float] = time.time, reserved=(),
                 register: Optional[Callable[..., dict]] = None,
                 opening: Optional[Callable[[dict, str], tuple]] = None,
                 handle: Optional[Callable[[dict], bool]] = None,
                 local: Optional[Callable[[float], float]] = None,
                 guard: Optional[Callable] = None):
        self._store = store
        self._config = config
        self._clock = clock
        self._local = local if local is not None else (lambda at: at)
        self._guard = guard if guard is not None else (lambda: nullcontext(None))
        self._reserved = {str(w) for w in reserved or () if str(w).strip()}
        self._register = register
        self._opening = opening
        self._handle = handle
        self._on = str(getattr(config, "prm_boss_groups", "off") or "off").strip().lower() == "on"
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
        self._seen_lock = threading.Lock()
        # chat -> party id of the linked groups (the route asks for every group message: no database look); replaced,
        # never changed in place (the route's thread reads it while the sender thread links a group)
        self._linked: dict[str, int] = {}
        # linked group -> when it was taken over, while its 开场白 waits for the watch's catch-up (``_openings``)
        self._due: dict[str, float] = {}
        if self._on:
            rows = store.boss_groups((LINKED,))
            self._linked = {r["chat"]: int(r["party_id"]) for r in rows if r.get("party_id")}
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
    def on(self) -> bool:
        """PRM_BOSS_GROUPS is on."""
        return self._on

    @property
    def call(self) -> str:
        """What the Mac is told the boss is called (PRM_BOSS_CALL)."""
        return self._call

    def _can_scan(self) -> bool:
        return all(callable(getattr(self._backend, name, None))
                   for name in ("boss_groups", "group_ids", "own_wxid", "contact_ids"))

    def why_not(self) -> str:
        """Why no boss group is found or taken over by itself now ("" = it is)."""
        if not self._with:
            return NO_BOSS_WHY
        if not self._can_scan():
            return NO_BACKEND_WHY
        return self._why

    def status(self) -> dict:
        return boss_groups_out(self._store, enabled=self._on, why_not=self.why_not() if self._on else "",
                               boss=self._with, reserved=self._reserved, boss_call=self._call)

    # ── what the PRM service asks (any thread; no lock of this class is taken) ──

    def is_linked(self, chat: str) -> bool:
        """Is this a group the bot took over (and the feature on)?"""
        return self._on and str(chat or "") in self._linked

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
        """Is a group the bot took over linked to this creator (her drafts go to the chat she wrote in)?"""
        try:
            return self._on and int(party_id) in self._linked.values()
        except (TypeError, ValueError):
            return False

    def reach_group(self, party_id) -> Optional[dict]:
        """The newest group the bot took over for this creator that she is still in (no ``LEFT_NOTICE`` on her deal), or
        None: where a packet the bot starts itself goes when she has no private chat with us (PrmService ``_reach``)."""
        if not self.party_has_group(party_id):
            return None
        for row in self._store.boss_groups((LINKED,)):
            if int(row.get("party_id") or 0) != int(party_id):
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

    def note_line(self, chat: str, written: float) -> None:
        """A line of the group was handled, ours too (where the watch starts after a restart: ``_since``)."""
        self._store.note_boss_group_line(chat, written)

    def add_context(self, chat: str, who: str, text: str, at: float) -> Optional[int]:
        """The boss's (or another member's) line, masked, for her next job (``job_group``); its number."""
        return self._store.add_boss_group_line(chat, {"who": who, "at": float(at), "text": str(text or "")[:LINE_CHARS]},
                                               LINES_KEPT)

    def job_group(self, chat: str) -> dict:
        """A group job's ``meta.group``: ``{"name", "boss", "lines"}``: the group's name and ``boss`` [PRM_BOSS_CALL] (the
        bosses' names never go to the Mac: ``hide``), ``lines`` the boss's and the other members' lines kept and not
        answered yet, at most LINES_TOLD (the newest), oldest first, each ``{"who": 老板 | 群成员, "at": "MM-DD HH:MM",
        "text", "seq"}`` (masked, at most LINE_CHARS; ``seq`` its number: a reply sent drops these, ``replied``)."""
        row = self._store.boss_group(chat) or {}
        lines = [x for x in row.get("lines") or [] if isinstance(x, dict)][-LINES_TOLD:]
        return {"name": self.hide(row.get("name"))[:NAME_CHARS], "boss": [self._call],
                "lines": [{"who": str(x.get("who") or MEMBER), "at": _clock_text(x.get("at")),
                           "text": self.hide(x.get("text"))[:LINE_CHARS], "seq": int(x.get("seq") or 0)}
                          for x in lines]}

    def replied(self, chat: str, seqs=None) -> int:
        """We answered in the group: the kept lines numbered ``seqs`` go (the ones the sent reply's job was told:
        ``told_lines``); None (an operator's own line there): every line kept now.  A line kept later stays, whenever it
        was written (the watch's catch-up hands on old lines late).  Returns how many went."""
        return self._store.boss_group_replied(chat, seqs, self._clock())

    def first_time(self, chat: str, message_id) -> bool:
        """Is this line of a linked group handed on for the first time (the route and ``_replay`` may both have it)?"""
        key = str(message_id or "")
        if not key:
            return True
        with self._seen_lock:
            seen = self._seen.setdefault(str(chat), deque(maxlen=SEEN_IDS))
            if key in seen:
                return False
            seen.append(key)
            return True

    # ── the scan ──────────────────────────────────────────────────────────

    def sweep(self) -> Optional[int]:
        """Each round of PRM's sender thread (PrmService.tick): the group 开场白 whose watch caught up (``_openings``),
        then the scan when it is due (every BOSS_SCAN_SEC; the first one at once): ``scan``'s answer, None when it is
        not due."""
        now = self._clock()
        if self._due:
            self._openings(now)
        if self._scanned and now - self._scanned < BOSS_SCAN_SEC:
            return None
        return self.scan()

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
            self._store.set_meta_value(BASELINE_AT, repr(float(now)))
            self._why = ""
            logger.info("PRM: boss groups: the %d group(s) there are now are never taken over (the baseline)", len(ids))
            return 0
        if self._baseline_at() is None:        # (a baseline kept before its time was: it counts from now)
            self._store.set_meta_value(BASELINE_AT, repr(float(now)))
        own = str(self._backend.own_wxid() or "")
        if not own:
            self._why = "还不知道这个微信号自己的 wxid（微信数据库还没读到），稍后自动再找"
            return None
        self._read_bosses(names, now)
        if not self._bosses:
            self._why = (NO_READ_WHY if self._ids_failed else
                         self._unknown_why() if self._unknown else
                         f"通讯录里认不出老板（PRM_GROUP_WITH：{'、'.join(names)}）：要和这个微信号里他的备注或名字一样，"
                         "而且只有一个人叫这个名字")
            return None
        self._why = self._unknown_why() if self._unknown else ""
        # The deals' own 三方群 (「拉三方群」: her, the boss and us, like a boss group) are never "new": kept in the
        # baseline as soon as a deal has one, so a deal's clean-up (it drops the group's id) does not make it new.
        made = {str((d.get("three_party") or {}).get("chat") or "") for d in self._store.deals()} - {""}
        if made - baseline:
            baseline |= made
            self._store.set_meta_value(BASELINE, json.dumps(sorted(baseline)))
        known = baseline | {r["chat"] for r in self._store.boss_groups()} | self._looked
        there = self._group_ids()              # (the groups it gives none of back were looked at: ``_note_looked``)
        found = self._backend.boss_groups(set(self._bosses), known, self._max)
        if found is None:
            self._why = "微信数据库现在读不了，老板拉的群稍后再找"
            return None
        self._note_looked(there, known, found)
        taken = 0
        for group in reversed(list(found)):    # (newest first from the backend: the oldest is seen to first)
            taken += self._found(group, own, now)
        taken += self._look_again(own, now)
        self._look_at_linked(own, now)
        self._openings(now)
        return taken

    def _names(self) -> list[str]:
        """PRM_GROUP_WITH as the config says it now (another value: the bosses are read again, ``_read_bosses``)."""
        if self._config is not None:
            self._with = group_with(getattr(self._config, "prm_group_with", ""))
        return self._with

    def _unknown_why(self) -> str:
        names = "、".join(f"「{n}」" for n in self._unknown)
        return UNKNOWN_WHY.format(names=names) + "：认出来以前，老板拉的群不会自动接手（找到的群在这里等你点「是她」）"

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
        members): looked at twice, they are left out of the next scans (``known``) until the bosses are read again."""
        if there is None:
            return
        handed = {str(g.get("chat") or "") for g in found or [] if isinstance(g, dict)}
        quiet = {chat for chat in there if chat not in known and chat not in handed}
        self._looked |= quiet & self._once
        self._once = quiet - self._looked

    def _new(self, group: dict) -> bool:
        """Is this group new for us (``NOT_NEW_WHY`` otherwise)?  It began for us (``joined_at``: WeChat's line that we
        were added; else ``created_at``, its oldest line, only when the backend read its whole history,
        ``history_complete``) at most NEW_MARGIN_SEC before the baseline.  A backend that says neither: its
        ``created_at`` alone, when it gives one (as before)."""
        start_at = self._baseline_at() or 0.0
        joined = _num(group.get("joined_at"))
        made = _num(group.get("created_at"))
        if "joined_at" not in group and "history_complete" not in group:
            return made is None or self._local(made) >= start_at - NEW_MARGIN_SEC
        began = joined if joined is not None else made
        if began is None or (joined is None and not group.get("history_complete")):
            return False
        return self._local(began) >= start_at - NEW_MARGIN_SEC

    def _present(self, members: list) -> list[dict]:
        """The bosses among ``members`` (``[{"wxid", "name"}]``, their names as PRM_GROUP_WITH writes them)."""
        there = {m["wxid"] for m in members}
        return [{"wxid": w, "name": n} for w, n in self._bosses.items() if w in there]

    def _ours(self, people: list) -> bool:
        """Is one of them a creator whose deal's 「拉三方群」 is being made or ended unsure (no group id kept: this may be
        that very group)?"""
        return any(self._group_state(m["wxid"]) in (CREATING, UNSURE) for m in people)

    def _group_state(self, wxid: str) -> str:
        """Her deal's 三方群 state ("" when she has none, or no deal)."""
        party = self._store.party_by_wxid(wxid, include_inactive=True)
        deal = self._store.deal_of(int(party["id"])) if party is not None else {}
        return str((deal.get("three_party") or {}).get("state") or "")

    def _has_group(self, wxid: str) -> bool:
        """Has she a 三方群 made, being made or unsure (「建好了」 by hand too)?  A group whose one candidate she is may be
        that very one: never taken over (``HAS_GROUP_WHY``)."""
        return self._group_state(wxid) in (CREATING, UNSURE, CREATED)

    def _found(self, group: dict, own: str, now: float) -> int:
        """A group the backend found: kept (pending, waiting or ignored) and, with exactly one person besides us and the
        bosses and nothing in the way, taken over.  Returns 1 when it was taken over."""
        chat = str((group or {}).get("chat") or "")
        if not chat.endswith("@chatroom"):
            return 0
        members = clean_members(group.get("members"), own)
        present = self._present(members)
        if not present:
            self._looked.add(chat)             # (no boss in it after all: not looked at again until the bosses are read)
            return 0
        self._ask_again(members)
        joined = _num(group.get("joined_at"))
        made = joined if joined is not None else _num(group.get("created_at"))
        name = str(group.get("name") or "")[:NAME_CHARS]
        if not self._new(group):
            if self._store.add_boss_group(chat, name=name, found_at=now, made_at=made, state=IGNORED, members=members,
                                          boss=present, why=NOT_NEW_WHY, now=now):
                logger.info("PRM: boss group %s… found, but it is no new group (it began before the switch's baseline, "
                            "or when is not known); not taken over", chat[:8])
            return 0
        people = candidates(members, self._bosses, self._reserved, self._with)
        state, why, note = (PENDING if people else WAITING), "", ""
        if len(people) == 1 and self._has_group(people[0]["wxid"]):
            state, why, note = IGNORED, HAS_GROUP_WHY, ": she has a 三方群 (made, being made or unsure), not taken over"
        elif len(people) > 1 and self._ours(people):
            state, why, note = IGNORED, OURS_WHY, ": maybe a deal's own 三方群 (being made or unsure), not taken over"
        elif self._unknown and people:
            why, note = self._unknown_why(), ": a boss name is no one contact's, the console asks"
        if not self._store.add_boss_group(chat, name=name, found_at=now, made_at=made, state=state, members=members,
                                          boss=present, why=why, now=now):
            return 0
        logger.info("PRM: boss group %s… found (%d member(s), %d boss(es), %d may be her)%s", chat[:8],
                    len(members) + 1, len(present), len(people), note)
        if state == PENDING and len(people) == 1 and not why:
            return int(bool(self.take_over(chat, people[0]["wxid"], people[0]["name"], actor="system").get("ok")))
        return 0

    def _look_again(self, own: str, now: float) -> int:
        """The groups waiting for her to come in: their members read again; one comes in: taken over (two: pending; one
        with a 三方群 of her own: ignored; a boss name no one contact's: pending); after BOSS_WAIT_DAYS they are
        ignored."""
        taken = 0
        for row in self._store.boss_groups((WAITING,)):
            chat = row["chat"]
            if float(row["found_at"]) >= now:
                continue                       # (found in this very scan: its members were just read)
            if now - float(row["found_at"]) > BOSS_WAIT_DAYS * 86400:
                self._store.update_boss_group(chat, now, states=(WAITING,), state=IGNORED, why=NOBODY_WHY)
                logger.info("PRM: boss group %s…: nobody but us and the boss in %d days; not looked at any more",
                            chat[:8], BOSS_WAIT_DAYS)
                continue
            raw = self._backend.group_members(chat) if callable(getattr(self._backend, "group_members", None)) else None
            if raw is None:
                continue
            members = clean_members(raw, own)
            self._ask_again(members)
            people = candidates(members, set(self._bosses) | {b.get("wxid") for b in row["boss"]}, self._reserved,
                                self._with)
            if not people:
                if members != row["members"]:
                    self._store.update_boss_group(chat, now, states=(WAITING,), members=members)
                continue
            boss = self._present(members) or row["boss"]
            if len(people) == 1 and self._has_group(people[0]["wxid"]):
                self._store.update_boss_group(chat, now, states=(WAITING,), state=IGNORED, members=members, boss=boss,
                                              why=HAS_GROUP_WHY)
                logger.info("PRM: boss group %s…: the one who came in has a 三方群; not taken over", chat[:8])
                continue
            if len(people) > 1 and self._ours(people):
                self._store.update_boss_group(chat, now, states=(WAITING,), state=IGNORED, members=members, boss=boss,
                                              why=OURS_WHY)
                logger.info("PRM: boss group %s…: maybe a deal's own 三方群; not taken over", chat[:8])
                continue
            why = self._unknown_why() if self._unknown else ""
            self._store.update_boss_group(chat, now, states=(WAITING,), state=PENDING, members=members, boss=boss,
                                          why=why)
            logger.info("PRM: boss group %s…: %d may be her now%s", chat[:8], len(people),
                        "; a boss name is no one contact's, the console asks" if why else "")
            if len(people) == 1 and not why:
                taken += int(bool(self.take_over(chat, people[0]["wxid"], people[0]["name"], actor="system").get("ok")))
        return taken

    def _look_at_linked(self, own: str, now: float) -> None:
        """Is she still in each group the bot took over?  Gone: her deal says so (until she is back)."""
        if not callable(getattr(self._backend, "group_members", None)):
            return
        for row in self._store.boss_groups((LINKED,)):
            party = self._store.party(int(row["party_id"] or 0))
            deal = self._store.deal(int(row["deal_id"] or 0))
            raw = self._backend.group_members(row["chat"]) if party is not None and deal is not None else None
            if raw is None:
                continue
            there = any(m["wxid"] == party["wxid"] for m in clean_members(raw, own))
            key = LEFT_NOTICE + row["chat"]
            has = any(n.get("key") == key for n in deal["notices"])
            if not there and not has:
                self._store.set_notice(deal["id"], key, "她已经不在老板拉的群里了：那个群里不会再有她的消息可起草，"
                                                        "需要的话在微信里看一眼", now)
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
        console; one with a 三方群 made, being made or unsure, looked at again right before the link under ThreeParty's
        lock, is not taken over) or console (``pick``).  ``{"ok": True, "party_id", "deal_id"}`` or ``{"ok": False,
        "error"}`` (the group is pending then, ``why`` says it; ignored for her 三方群)."""
        with self._lock:
            row = self._store.boss_group(str(chat or ""))
            if row is None or row["state"] not in (PENDING, WAITING):
                return {"ok": False, "error": "这个群不在待认领的名单里（可能已经接手了，或者不管了）"}
            now = self._clock()
            wxid = str(wxid or "").strip()
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
            with self._guard() as busy:        # (「拉三方群」 starts none meanwhile; ``busy``: the deal it makes one for)
                deal = self._store.deal_of(int(party["id"]), now)
                state = str((deal.get("three_party") or {}).get("state") or "")
                if actor != "console" and (state in (CREATING, UNSURE, CREATED) or busy == deal["id"]):
                    self._store.update_boss_group(row["chat"], now, states=(PENDING, WAITING), state=IGNORED,
                                                  why=HAS_GROUP_WHY)
                    logger.info("PRM: boss group %s…: party %d's deal has a 三方群 (made, being made or unsure); not "
                                "taken over", row["chat"][:8], party["id"])
                    return {"ok": False, "error": HAS_GROUP_WHY}
                kept = self._link(deal, row, now)
                self._store.update_boss_group(row["chat"], now, states=(PENDING, WAITING), state=LINKED,
                                              party_id=int(party["id"]), deal_id=int(deal["id"]), why="",
                                              opening_due=now)
                self._linked = {**self._linked, row["chat"]: int(party["id"])}
            self._store.add_event(deal["id"], party["id"], EVENT,
                                  {"name": row["name"], "new": made, **({"kept": True} if kept else {})},
                                  actor="console" if actor == "console" else "system", now=now)
            since = self._since(row)
            self._watch(row["chat"], since)
            self._replay(row["chat"], since, now)  # (her words before it: a reply job, then no 开场白)
            self._due = {**self._due, row["chat"]: now}
            logger.info("PRM: boss group %s… taken over (%s): party %d %s%s; its 开场白 once its lines so far are read",
                        row["chat"][:8], "the console" if actor == "console" else "the scan", party["id"],
                        "registered" if made else "linked", ", her deal keeps its 三方群" if kept else "")
            self._openings(now)                # (a backend whose watch caught up at once)
            return {"ok": True, "party_id": int(party["id"]), "deal_id": int(deal["id"])}

    def _held(self, row: dict, why: str, code: str, now: float) -> dict:
        """Not taken over: the group waits for the console (``pending``, ``why``)."""
        self._store.update_boss_group(row["chat"], now, states=(PENDING, WAITING), state=PENDING, why=why[:200])
        logger.info("PRM: boss group %s… not taken over (%s); the console asks", row["chat"][:8], code)
        return {"ok": False, "error": why}

    def _link(self, deal: dict, row: dict, now: float) -> bool:
        """The deal's 三方群 is this group (``by`` boss), unless it has one made, being made or unsure (True: kept)."""
        if str((deal.get("three_party") or {}).get("state") or "") in (CREATED, CREATING, UNSURE):
            return True
        self._store.update_deal(deal["id"], now, three_party={
            "state": CREATED, "at": row["found_at"], "by": "boss", "chat": row["chat"], "name": row["name"],
            "members": len(row["members"]) + 1, "why": "老板拉的群"})
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
        handled (the backend keeps its watches in memory; a 开场白 not asked for yet waits for this catch-up).  Returns
        how many."""
        if not self._on:
            return 0
        rows = self._store.boss_groups((LINKED,))
        for row in rows:
            self._watch(row["chat"], self._since(row))
        if rows:
            logger.info("PRM: boss groups: the %d group(s) taken over are read again", len(rows))
        return len(rows)

    # ── the console (POST /api/prm/boss-groups/pick | ignore) ─────────────

    def pick(self, chat, wxid) -> dict:
        """「是她」: this member of a pending group is the creator (``take_over`` by the console; a creator an operator
        stopped is registered again).  Only a member she may be: not us, not a boss, not the admin or a cc user."""
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
        """「这个群不是达人」: a pending (or waiting) group is never looked at again (``ignored``)."""
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
        boss, PrmService.deal_action ``group_none``; ``deal`` as it was before): the group is ``gone``, the backend stops
        reading it (``unwatch_group``), her lines there are silent again, the deal's record says so.  The deal stays,
        her private chat too.  True when it was such a group."""
        chat = str(((deal or {}).get("three_party") or {}).get("chat") or "")
        with self._lock:
            row = self._store.boss_group(chat) if chat else None
            if row is None or row["state"] != LINKED or int(row.get("deal_id") or 0) != int(deal["id"]):
                return False
            now = self._clock()
            self._store.update_boss_group(chat, now, states=(LINKED,), state=GONE, why=GONE_WHY, opening_due=None)
            self._linked = {k: v for k, v in self._linked.items() if k != chat}
            self._due = {k: v for k, v in self._due.items() if k != chat}
            unwatch = getattr(self._backend, "unwatch_group", None)
            if callable(unwatch):
                try:
                    unwatch(chat)
                except Exception:
                    logger.warning("PRM: boss group %s…: the backend could not stop reading it", chat[:8], exc_info=True)
            self._store.add_event(deal["id"], deal["party_id"], EVENT, {"gone": True},
                                  actor="console" if actor == "console" else "system", now=now)
            logger.info("PRM: boss group %s… is gone (%s): party %d's lines there are not drafted any more", chat[:8],
                        actor, int(deal["party_id"]))
            return True

    # ── clean-ups (PrmService.purge / purge_due) ─────────────────────────

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
