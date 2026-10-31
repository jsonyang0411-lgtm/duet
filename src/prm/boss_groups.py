"""老板拉的群 (PRM_BOSS_GROUPS, default off; 2026-10-31, the user: 「老板那边也有一点人是已经加了微信的，然后老板想把他和达人还有
我们这边也拉个群，让我们来负责和达人对接」, 「全自动接手」, 「起草后人审再发到群里，像之前的加个自动模式」).

Some creators are the boss's WeChat friends, not ours.  He makes a small group: himself (a name of PRM_GROUP_WITH), the
creator and this account; from then on we handle her.  The scan (``sweep``, every BOSS_SCAN_SEC on PRM's sender thread)
asks the WeChat backend for the group chats it does not know (``boss_groups``: neither in the baseline its first run
kept, prm_meta ``boss_groups_baseline``, nor found before) that have a boss among their members and at most
PRM_BOSS_GROUP_MAX members.  Her ``candidates`` are the members but us, the bosses, the admin and the cc users:

- exactly one: the bot takes over (``take_over``);
- two or more: ``pending``: the console asks which member she is (``pick``; 「这个群不是达人」 is ``ignore``);
- none: ``waiting``: its members are read again each scan for BOSS_WAIT_DAYS (one comes in: taken over), then it is
  ``ignored``.

A deal's own 三方群 (「拉三方群」 makes her, the boss and us a group too) is never one: every deal's ``three_party.chat`` goes
into the baseline, and a group found while her 「拉三方群」 is being made or ended unsure is kept ``ignored``.

Taking over: she is registered (her note 「老板介绍（老板拉的群：<群名>）」) unless she is an active creator already; the group
is linked to her deal (``deals.three_party`` ``{"state": "created", "by": "boss", "chat", "name", ...}``: the 三方群 block
shows it, the Mac hears a group is made; a deal with a group made or being made keeps it and the record says so); the
backend reads the group from then on (``watch_group``: its lines from shortly before it was found, or from its oldest
line, reach the route once, the boss's introduction too); the lines the bot's message store kept of it before (a group
WECHAT_GROUPS read: the route stored them and said nothing) are handed on once (``_replay``); then one draft job asks
for our first line in the group, unless her own words made a reply job already (PrmService ``_group_opening``: it
always waits for a person; her own line in the group outdates it).  A creator an operator stopped (停用) is never taken
over by the scan: the console asks.  The scan also reads a linked group's members: the deal says so when she is gone.
「群已经不在了」 on her deal (``gone``): the group is not read any more, her lines there are silent again.

What the bot starts itself for her (a shipping notice, a 专属佣金 step, a 开场白 from the console) goes to her private chat
when she has one (she wrote there, or the backend's ``chat_target`` names it), else to a linked group she is still in,
else nowhere (PrmService ``_reach`` / ``_aim``: the deal says so).  The draft jobs of such a creator tell where each
earlier reply went (``meta.sent[].where``).

In a linked group (PrmService ``_group_message``: the route's one exception to «a group is never answered»,
src/routing/steps.py) only HER lines are drafted, as in her private chat: the job says it is the group (``meta.channel``
"group", ``meta.group``: its name, the bosses, the other members' lines since we last answered there, ``job_group``),
the packet goes to the group, the gates, 下班时段 and 自动模式 apply as they are, no holding reply goes there.  The boss's
and the other members' lines are only kept for her next job (``add_context``); WeChat's own lines are never drafted.
Names stay in prm.db: the log has ids' first characters and counts only.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Callable, Optional

from .store import PartyError
from .three_party import CREATED, CREATING, NOTICE as GROUP_NOTICE, UNSURE, group_with

logger = logging.getLogger(__name__)

BOSS_SCAN_SEC = 60                 # the scan runs this often (PRM's sender thread)
BOSS_WAIT_DAYS = 3                 # a group with nobody but us and the boss is looked at this long
BOSS_WATCH_BACK_SEC = 300          # the watch hands on the group's lines from this long before it was found
BOSS_IDS_SEC = 600                 # the bosses' wxids (``contact_ids`` reads every group's members) are read this seldom
REPLAY_MAX = 200                   # the lines stored before a takeover handed on (the newest)
SEEN_IDS = 500                     # message ids of a linked group remembered (the route and the replay hand one on once)
IGNORED_KEEP_DAYS = 30             # an ignored group's row is dropped after this long (its id stays in the baseline)
MAX_DEFAULT, MAX_LOW, MAX_HIGH = 6, 3, 20      # PRM_BOSS_GROUP_MAX
LINES_KEPT = 30                    # the boss's / other members' lines a group keeps for her next job (the newest)
LINES_TOLD = 10                    # ... and tells one job
LINE_CHARS = 300
NAME_CHARS = 60                    # a group's name as kept
NAME_MAX = 30                      # her 称呼 (PrmService.register)
BASELINE = "boss_groups_baseline"  # prm_meta: the group ids there were when the feature first ran (JSON list)
PENDING, WAITING, LINKED, IGNORED, GONE = "pending", "waiting", "linked", "ignored", "gone"
STATES = (PENDING, WAITING, LINKED, IGNORED, GONE)
CHANNEL = "group"                  # a group job's and packet's ``meta.channel``
OPENING_HOW = "boss_group"         # the group 开场白's ``meta.opening`` ({"how", "by"} both this)
EVENT = "boss_group"               # the deal's record 「老板拉群介绍（群名）」
LEFT_NOTICE = "boss_group_left:"   # + the group id: the deal's notice while she is not in the group
BOSS, MEMBER = "老板", "群成员"      # who said a line kept for her next job (``meta.group.lines[].who``)
FILLER = "\u3164"                  # HANGUL FILLER, a blank-looking WeChat name (src/wechat/new_friends.py)
NO_BOSS_WHY = "先在设置里填 PRM_GROUP_WITH（老板在这个微信里的名字）"
NO_BACKEND_WHY = "这台电脑的微信读法找不到老板拉的群（要 Windows 版微信的数据库直读）"
NOBODY_WHY = f"{BOSS_WAIT_DAYS} 天里群里一直只有老板和我们，不再看这个群"
IGNORE_WHY = "运营说这个群不是达人"
GONE_WHY = "运营说这个群已经不在了"
STOPPED_WHY = "她在达人名单里停用过：要接手的话请在这里点「是她」"
OURS_WHY = "像是合作单上「拉三方群」建的群（那次正在建，或者不确定建没建成），不接手"


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


def candidates(members: list, bosses, reserved=()) -> list[dict]:
    """Who of a group's ``members`` (us left out already) may be her: not a boss (their wxids), not the admin or a cc user
    (``reserved``), no group id."""
    bosses, reserved = set(bosses), set(reserved)
    return [m for m in members or [] if m.get("wxid") and m["wxid"] not in bosses and m["wxid"] not in reserved
            and not str(m["wxid"]).endswith("@chatroom")]


def boss_groups_out(store, *, enabled: bool, why_not: str = "", boss=(), reserved=()) -> dict:
    """What the console shows (GET /api/prm ``boss_groups``; also the stopped bot's, from .env): on or off, why it cannot
    run now, the bosses (PRM_GROUP_WITH), the groups waiting for an operator to say who she is (``pending``: their
    name, only the members she may be (a 「是她」 each: never us, a boss, the admin or a cc user), how many are in it
    (``size``), when found, why), how many wait for her to come in and how many are taken over."""
    rows = store.boss_groups() if store is not None and enabled else []
    pending = [{"chat": r["chat"], "name": r["name"], "found_at": r["found_at"], "why": r["why"],
                "size": len(r["members"]) + 1,
                "members": [{"wxid": m["wxid"], "name": m.get("name") or ""}
                            for m in candidates(r["members"], {b.get("wxid") for b in r["boss"]}, reserved)]}
               for r in rows if r["state"] == PENDING]
    return {"enabled": bool(enabled), "why_not": str(why_not or "") if enabled else "", "boss": list(boss),
            "pending": pending, "waiting": sum(1 for r in rows if r["state"] == WAITING),
            "linked": sum(1 for r in rows if r["state"] == LINKED)}


def _clock_text(at) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(at)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


class BossGroups:
    """The 老板拉的群 of one PRM service (see the module's doc).  ``register(wxid, name, note)``: PrmService.register;
    ``opening(party, chat)``: PrmService._group_opening (``(job id, "")`` or ``(None, why not)``); ``handle(msg)``:
    PrmService.handle (a line stored before the takeover, handed on once); ``reserved``: the admin and the cc users.
    ``attach`` gives it the WeChat backend and the bot's message store (Bot._start_prm, before PrmService.start)."""

    def __init__(self, store, config=None, *, clock: Callable[[], float] = time.time, reserved=(),
                 register: Optional[Callable[..., dict]] = None,
                 opening: Optional[Callable[[dict, str], tuple]] = None,
                 handle: Optional[Callable[[dict], bool]] = None):
        self._store = store
        self._clock = clock
        self._reserved = {str(w) for w in reserved or () if str(w).strip()}
        self._register = register
        self._opening = opening
        self._handle = handle
        self._on = str(getattr(config, "prm_boss_groups", "off") or "off").strip().lower() == "on"
        self._with = group_with(getattr(config, "prm_group_with", ""))
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
        self._lock = threading.RLock()            # one scan, pick or ignore at a time
        self._scanned = 0.0
        self._why = ""                            # why the last scan could not look (the console's why_not)
        self._bosses: dict[str, str] = {}         # the bosses' wxids as last read (``contact_ids``) -> their names
        self._bosses_at = 0.0                     # ... when (BOSS_IDS_SEC)
        self._seen: dict[str, deque] = {}         # linked group -> the message ids handed on lately (``first_time``)
        self._seen_lock = threading.Lock()
        # chat -> party id of the linked groups (the route asks for every group message: no database look); replaced,
        # never changed in place (the route's thread reads it while the sender thread links a group)
        self._linked: dict[str, int] = {}
        if self._on:
            self._linked = {r["chat"]: int(r["party_id"]) for r in store.boss_groups((LINKED,)) if r.get("party_id")}

    # ── wiring and status ─────────────────────────────────────────────────

    def attach(self, backend, stored: Optional[Callable] = None) -> None:
        """The WeChat backend (spec §4: own_wxid, contact_ids, boss_groups, group_members, watch_group, unwatch_group;
        group_ids, chat_target) and ``stored`` (MessageStore.get_messages_since): with WECHAT_GROUPS reading every group,
        the route kept a group's lines before the takeover and the watch skips them (``_replay``)."""
        self._backend = backend
        self._stored = stored

    @property
    def on(self) -> bool:
        """PRM_BOSS_GROUPS is on."""
        return self._on

    def _can_scan(self) -> bool:
        return all(callable(getattr(self._backend, name, None))
                   for name in ("boss_groups", "group_ids", "own_wxid", "contact_ids"))

    def why_not(self) -> str:
        """Why no boss group is found or taken over now ("" = it is)."""
        if not self._with:
            return NO_BOSS_WHY
        if not self._can_scan():
            return NO_BACKEND_WHY
        return self._why

    def status(self) -> dict:
        return boss_groups_out(self._store, enabled=self._on, why_not=self.why_not() if self._on else "",
                               boss=self._with, reserved=self._reserved)

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
        """Does the backend know her private chat (WeChat lists it: she is our friend)?  ``chat_target`` names a private
        chat only when WeChat's session list (or the private chats it listed before) has it; False when it cannot say."""
        target = getattr(self._backend, "chat_target", None)
        if not callable(target):
            return False
        try:
            name, _ = target(str(wxid or ""))
        except Exception:
            logger.debug("PRM: boss groups: her private chat could not be looked up", exc_info=True)
            return False
        return bool(str(name or "").strip())

    def who(self, row: dict, sender: str) -> str:
        """Who wrote a line of the group that is not hers: 老板 (a boss of PRM_GROUP_WITH) or 群成员."""
        bosses = {str(b.get("wxid") or "") for b in row.get("boss") or []} | set(self._bosses)
        return BOSS if str(sender or "") in bosses else MEMBER

    def boss_said(self, row: dict, line: str) -> bool:
        """Does one of WeChat's own lines of the group name a boss (「"老板测试" 撤回了一条消息」: not hers)?"""
        names = {str(b.get("name") or "") for b in row.get("boss") or []} | set(self._with)
        return any(n and n in str(line or "") for n in names)

    def note_line(self, chat: str, written: float) -> None:
        """A line of the group was handled (where the watch starts after a restart)."""
        self._store.note_boss_group_line(chat, written)

    def add_context(self, chat: str, who: str, text: str, at: float) -> None:
        """The boss's (or another member's) line, masked, for her next job (``job_group``)."""
        self._store.add_boss_group_line(chat, {"who": who, "at": float(at), "text": str(text or "")[:LINE_CHARS]},
                                        LINES_KEPT)

    def job_group(self, chat: str) -> dict:
        """A group job's ``meta.group``: ``{"name", "boss" (their names), "lines"}``, ``lines`` the boss's and the other
        members' lines since we last answered there (``replied``), at most LINES_TOLD, oldest first, each ``{"who":
        老板 | 群成员, "at": "MM-DD HH:MM", "text"}`` (masked, at most LINE_CHARS)."""
        row = self._store.boss_group(chat) or {}
        cut = float(row.get("replied_at") or 0)
        lines = [x for x in row.get("lines") or [] if float(x.get("at") or 0) > cut][-LINES_TOLD:]
        boss = [str(b.get("name") or "") for b in row.get("boss") or [] if b.get("name")] or list(self._with)
        return {"name": str(row.get("name") or ""), "boss": boss,
                "lines": [{"who": str(x.get("who") or MEMBER), "at": _clock_text(x.get("at")),
                           "text": str(x.get("text") or "")[:LINE_CHARS]} for x in lines]}

    def replied(self, chat: str, at: float) -> None:
        """We answered in the group at ``at`` (a reply sent: when its job was made; an operator's own line): the lines
        kept from before it are not told again."""
        self._store.boss_group_replied(chat, at)

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
        """The scan when it is due (PrmService.tick, every BOSS_SCAN_SEC; the first one at once): ``scan``'s answer,
        None when it is not due."""
        now = self._clock()
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

    def _scan(self, now: float) -> Optional[int]:
        baseline = self._baseline()
        if baseline is None:                   # the first run: the groups there are now are never "new"
            ids = self._backend.group_ids()
            if ids is None:
                self._why = "微信数据库现在读不了，能读了以后才开始找老板拉的群"
                return None
            self._store.set_meta_value(BASELINE, json.dumps(sorted(str(i) for i in ids)))
            self._why = ""
            logger.info("PRM: boss groups: the %d group(s) there are now are never taken over (the baseline)", len(ids))
            return 0
        own = str(self._backend.own_wxid() or "")
        if not own:
            self._why = "还不知道这个微信号自己的 wxid（微信数据库还没读到），稍后自动再找"
            return None
        if not self._bosses_at or now - self._bosses_at >= BOSS_IDS_SEC:    # (it reads every group's members)
            ids = self._backend.contact_ids(list(self._with)) or {}
            self._bosses = {str(w): str(n) for n, w in ids.items() if w}
            self._bosses_at = now
        if not self._bosses:
            self._why = (f"通讯录里认不出老板（PRM_GROUP_WITH：{'、'.join(self._with)}）：要和这个微信号里他的备注或名字一样，"
                         "而且只有一个人叫这个名字")
            return None
        # The deals' own 三方群 (「拉三方群」: her, the boss and us, like a boss group) are never "new": kept in the
        # baseline as soon as a deal has one, so a deal's clean-up (it drops the group's id) does not make it new.
        made = {str((d.get("three_party") or {}).get("chat") or "") for d in self._store.deals()} - {""}
        if made - baseline:
            baseline |= made
            self._store.set_meta_value(BASELINE, json.dumps(sorted(baseline)))
        known = baseline | {r["chat"] for r in self._store.boss_groups()}
        found = self._backend.boss_groups(set(self._bosses), known, self._max)
        if found is None:
            self._why = "微信数据库现在读不了，老板拉的群稍后再找"
            return None
        self._why = ""
        taken = 0
        for group in reversed(list(found)):    # (newest first from the backend: the oldest is seen to first)
            taken += self._found(group, own, now)
        taken += self._look_again(own, now)
        self._look_at_linked(own, now)
        return taken

    def _present(self, members: list) -> list[dict]:
        """The bosses among ``members`` (``[{"wxid", "name"}]``, their names as PRM_GROUP_WITH writes them)."""
        there = {m["wxid"] for m in members}
        return [{"wxid": w, "name": n} for w, n in self._bosses.items() if w in there]

    def _ours(self, people: list) -> bool:
        """Is one of them a creator whose deal's 「拉三方群」 is being made or ended unsure (no group id kept: this may be
        that very group)?"""
        for m in people:
            party = self._store.party_by_wxid(m["wxid"], include_inactive=True)
            deal = self._store.deal_of(int(party["id"])) if party is not None else {}
            if str((deal.get("three_party") or {}).get("state") or "") in (CREATING, UNSURE):
                return True
        return False

    def _found(self, group: dict, own: str, now: float) -> int:
        """A group the backend found: kept (pending or waiting) and, with exactly one person besides us and the bosses,
        taken over; one that may be a deal's own 三方群 is kept ignored.  Returns 1 when it was taken over."""
        chat = str((group or {}).get("chat") or "")
        if not chat.endswith("@chatroom"):
            return 0
        members = clean_members(group.get("members"), own)
        present = self._present(members)
        if not present:
            return 0                           # (no boss in it after all: not kept, the backend asks again)
        people = candidates(members, self._bosses, self._reserved)
        ours = bool(people) and self._ours(people)
        try:
            made = float(group.get("created_at") or 0) or None
        except (TypeError, ValueError):
            made = None
        if not self._store.add_boss_group(chat, name=str(group.get("name") or "")[:NAME_CHARS], found_at=now,
                                          made_at=made, state=IGNORED if ours else PENDING if people else WAITING,
                                          members=members, boss=present, why=OURS_WHY if ours else "", now=now):
            return 0
        logger.info("PRM: boss group %s… found (%d member(s), %d boss(es), %d may be her)%s", chat[:8],
                    len(members) + 1, len(present), len(people),
                    ": maybe a deal's own 三方群 (being made or unsure), not taken over" if ours else "")
        if len(people) == 1 and not ours:
            return int(bool(self.take_over(chat, people[0]["wxid"], people[0]["name"], actor="system").get("ok")))
        return 0

    def _look_again(self, own: str, now: float) -> int:
        """The groups waiting for her to come in: their members read again; one comes in: taken over (two: pending);
        after BOSS_WAIT_DAYS they are ignored."""
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
            people = candidates(members, set(self._bosses) | {b.get("wxid") for b in row["boss"]}, self._reserved)
            if not people:
                if members != row["members"]:
                    self._store.update_boss_group(chat, now, states=(WAITING,), members=members)
                continue
            self._store.update_boss_group(chat, now, states=(WAITING,), state=PENDING, members=members,
                                          boss=self._present(members) or row["boss"])
            logger.info("PRM: boss group %s…: %d may be her now", chat[:8], len(people))
            if len(people) == 1:
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
        creator already, the group linked to her deal (``_link``), watched (``watch_group``), a group 开场白 asked for,
        the deal's record (``EVENT``), one log line without names.  ``actor``: system (the scan; a stopped creator is
        left for the console) or console (``pick``).  ``{"ok": True, "party_id", "deal_id"}`` or ``{"ok": False,
        "error"}`` (the group is pending then, ``why`` says it)."""
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
            deal = self._store.deal_of(int(party["id"]), now)
            kept = self._link(deal, row, now)
            self._store.update_boss_group(row["chat"], now, states=(PENDING, WAITING), state=LINKED,
                                          party_id=int(party["id"]), deal_id=int(deal["id"]), why="")
            self._linked = {**self._linked, row["chat"]: int(party["id"])}
            self._store.add_event(deal["id"], party["id"], EVENT,
                                  {"name": row["name"], "new": made, **({"kept": True} if kept else {})},
                                  actor="console" if actor == "console" else "system", now=now)
            since = self._since(row)
            self._watch(row["chat"], since)
            self._replay(row["chat"], since, now)  # (her words before it: a reply job, then no 开场白)
            job, why = self._opening(party, row["chat"]) if self._opening is not None else (None, "")
            logger.info("PRM: boss group %s… taken over (%s): party %d %s%s; %s", row["chat"][:8],
                        "the console" if actor == "console" else "the scan", party["id"],
                        "registered" if made else "linked",
                        ", her deal keeps its 三方群" if kept else "",
                        f"job #{job} drafts our first line in the group" if job else f"no 开场白 ({why or 'none'})")
            return {"ok": True, "party_id": int(party["id"]), "deal_id": int(deal["id"])}

    def _held(self, row: dict, why: str, code: str, now: float) -> dict:
        """Not taken over: the group waits for the console (``pending``, ``why``)."""
        self._store.update_boss_group(row["chat"], now, states=(PENDING, WAITING), state=PENDING, why=why[:200])
        logger.info("PRM: boss group %s… not taken over (%s); the console asks", row["chat"][:8], code)
        return {"ok": False, "error": why}

    def _link(self, deal: dict, row: dict, now: float) -> bool:
        """The deal's 三方群 is this group (``by`` boss), unless it has one made or being made (True: kept)."""
        if str((deal.get("three_party") or {}).get("state") or "") in (CREATED, CREATING):
            return True
        self._store.update_deal(deal["id"], now, three_party={
            "state": CREATED, "at": row["found_at"], "by": "boss", "chat": row["chat"], "name": row["name"],
            "members": len(row["members"]) + 1, "why": "老板拉的群"})
        self._store.drop_notices(deal["id"], lambda n: n.get("key") == GROUP_NOTICE, now)
        return False

    def _since(self, row: dict) -> float:
        """Where the backend starts reading the group: its last line handled; else BOSS_WATCH_BACK_SEC before it was
        found, or its oldest line (``made_at``: one made while the bot was stopped, the boss's introduction is read
        too), up to PRM_CATCHUP_HOURS back."""
        if row.get("last_line_at"):
            return float(row["last_line_at"])
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

    def start(self) -> int:
        """At start (PrmService.start): every group the bot took over is read again from its last line handled (the
        backend keeps its watches in memory).  Returns how many."""
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
                                              self._reserved) if m["wxid"] == wxid), None)
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
            self._store.update_boss_group(chat, now, states=(LINKED,), state=GONE, why=GONE_WHY)
            self._linked = {k: v for k, v in self._linked.items() if k != chat}
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
