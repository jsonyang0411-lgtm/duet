"""三方群 (2026-10-30; the boss's rule of 2026-10-29: once the talks with a creator reach the sample stage, the boss, the
creator and this WeChat account are in one group; the user: 「做a吧」; the boss's name is in this PC's .env only).

One click in her deal (合作单 「拉三方群」, ``start``) makes that group in the WeChat window: the friend manager's
``create_group`` (through the admin handler's ``create_group_checked``, the breaker of 添加好友) picks, in WeChat 4.0's
「发起群聊」 window, her chat's name (the backend's ``chat_target``: the name the send path types; a name another chat has
too is refused, as a send is) and the people of PRM_GROUP_WITH (names in this account's contacts, in this PC's .env
only), nobody else, and clicks 「完成」 once.  The bot never says anything in the group (the routing red line: group
messages are never answered) and does not rename it (WeChat's chat-info icon has no text to read): the console shows the
name PRM_GROUP_NAME suggests, to copy and set on the phone.  「先试一下」 (``start(..., dry_run=True)``) does all of it
but 「完成」 and makes nothing.

A deal's group is ``deals.three_party`` (JSON): ``{"state", "at", "by", "why", "chat", "name", "members"}``, ``state``
one of ``creating`` (one group at a time, nothing else of it meanwhile) → ``created`` (the window said done and the
database shows a new group with her in it: ``chat`` its id, ``name`` WeChat's name for it, ``members`` how many; marked
by hand: no ``chat``) / ``unsure`` (「完成」 may have been clicked but no such group showed up, or the bot stopped in the
middle: a person looks in WeChat and says 「建好了」 or 「没建成」; a deal notice says so until then) / ``failed``
(nothing was made: ``why``; it may be tried again).  WeChat's risk prompt pauses the auto-accept as 加好友 does, and no
group is made while it is paused.  Every later draft job of a deal whose group is made tells the Mac
(``job_meta``: the case file's 【待拉三方群】 becomes 【已拉三方群】).  Names are never logged.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

logger = logging.getLogger(__name__)

CREATING, CREATED, UNSURE, FAILED = "creating", "created", "unsure", "failed"
# A deal's group the bot drafts in (``by``): a 老板拉的群 it took over, or one an operator linked by hand (接手任何合作群,
# 2026-11-09): src/prm/boss_groups.py's, never read only here
TAKEN_BY = ("boss", "manual")
STATES = {CREATING: "正在建群", CREATED: "已建好", UNSURE: "不确定建没建成", FAILED: "没建成"}
EVENT = "three_party"                  # the deal's record of each step (``{"state", ...}``)
NOTICE = "three_party"                 # the deal's notice while a person has to look (``unsure``)
WITH_MAX = 3                           # PRM_GROUP_WITH: at most this many besides her
WITH_NAME_CHARS = 20
NAME_DEFAULT = "{品牌}×{昵称}"         # PRM_GROUP_NAME's default: {品牌} PRM_FRIEND_ADD_BRAND, {昵称} her 称呼
NAME_MAX_CHARS = 30                    # a WeChat group name
FIND_LOOKS = 10                        # after 「完成」 the database is looked at this many times for the new group,
FIND_STEP_SEC = 2.0                    # this far apart
WHY_CHARS = 300
WATCH_BACK_SEC = 300                   # a group just made is read from shortly before (its first lines)
_SPLIT = re.compile(r"[,，、;；]")
_SEPARATORS = " ×xX*·-—_|／/"


def group_with(raw) -> list[str]:
    """PRM_GROUP_WITH as names: split on commas (，、；), white space collapsed, empty ones and repeats dropped, at most
    WITH_MAX (a longer list is the config check's to refuse)."""
    out: list[str] = []
    for part in _SPLIT.split(str(raw or "")):
        name = " ".join(part.split())
        if name and name not in out:
            out.append(name)
    return out[:WITH_MAX]


def suggested_name(template: str, brand: str, name: str) -> str:
    """The group name PRM_GROUP_NAME suggests for her: {品牌} the brand (PRM_FRIEND_ADD_BRAND), {昵称} her 称呼, white
    space collapsed; with no brand, the separator it leaves at either end goes; at most NAME_MAX_CHARS."""
    text = str(template or NAME_DEFAULT).replace("{品牌}", str(brand or "").strip()).replace("{昵称}", str(name or "").strip())
    text = " ".join(text.split())
    if not str(brand or "").strip():
        text = text.strip(_SEPARATORS)
    return text[:NAME_MAX_CHARS]


def _spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="prm-three-party", daemon=True).start()


def _key(name: str) -> str:
    return "".join(ch for ch in str(name or "") if ch.isalnum()).casefold()


def state_out(deal: Optional[dict]) -> dict:
    """A deal's group as the console shows it (also the stopped bot's snapshot): ``{"state", "label", "at", "by", "why",
    "chat_name", "made"}`` (``made``: the bot made it; ``{}`` when there is none)."""
    group = dict((deal or {}).get("three_party") or {})
    state = str(group.get("state") or "")
    if state not in STATES:
        return {}
    return {"state": state, "label": STATES[state], "at": group.get("at"), "by": str(group.get("by") or ""),
            "why": str(group.get("why") or ""), "chat_name": str(group.get("name") or ""),
            "made": bool(group.get("chat"))}


class ThreeParty:
    """The deals' 三方群 (see the module's doc).  ``attach`` gives it the admin handler (``create_group_checked``,
    ``can_add_friends``), the auto-accept service (``paused_why``) and the WeChat backend (``chat_target``,
    ``group_ids``, ``find_group_with``) once the bot started its friend management (Bot.run).  ``spawn(fn)`` runs a
    group's making (a thread) and ``sleep`` paces the look for it: seams for tests."""

    def __init__(self, store, config=None, *, clock: Callable[[], float] = time.time, spawn: Callable = _spawn,
                 sleep: Callable[[float], None] = time.sleep):
        self._store = store
        self._clock = clock
        self.spawn = spawn
        self._sleep = sleep
        self._with = group_with(getattr(config, "prm_group_with", ""))
        self._template = str(getattr(config, "prm_group_name", NAME_DEFAULT) or NAME_DEFAULT)
        self._brand = str(getattr(config, "prm_friend_add_brand", "") or "").strip()
        self._admin = None
        self._auto = None
        self._backend = None
        self._lock = threading.Lock()
        self._busy: Optional[int] = None          # the deal whose group is being made or tried
        self._dry = False                         # ... tried only (「先试一下」: nothing is made)
        self._tries: dict[int, dict] = {}         # deal id -> its last 「先试一下」 ({"at", "ok", "message"})
        # 三方群里的话 (2026-11-02): where the watch of a deal's group starts again (PrmService._group_since)
        self.since_of: Optional[Callable[[dict], float]] = None

    # ── wiring (Bot.run) ──────────────────────────────────────────────────

    def attach(self, admin=None, auto=None, backend=None) -> None:
        self._admin, self._auto, self._backend = admin, auto, backend
        self.watch_all()                          # (wired after the PRM started, or before it: watched once either way)

    # ── 三方群里的话 (2026-11-02): her group read only, its lines for her private drafts ──────────────

    def deal_of_chat(self, chat: str) -> Optional[dict]:
        """The deal whose 三方群 this group is (made, its ``chat`` known: the bot made it, or found it), or None.  Not a
        老板拉的群 (``by`` boss) nor one linked by hand (``by`` manual): those are src/prm/boss_groups.py's."""
        chat = str(chat or "")
        if not chat.endswith("@chatroom"):
            return None
        for deal in self._store.deals():
            group = deal.get("three_party") or {}
            if group.get("state") == CREATED and group.get("chat") == chat and group.get("by") not in TAKEN_BY:
                return deal
        return None

    def reading(self) -> bool:
        """Can this PC read a group whatever WECHAT_GROUPS says (the backend's ``watch_group``)?"""
        return callable(getattr(self._backend, "watch_group", None))

    def watch_all(self) -> int:
        """Read every deal's 三方群 again (``watch_group``, read only: its lines go to PrmService._three_party_line),
        each from where ``since_of`` says (a second after its last line kept).  Returns how many."""
        if not self.reading():
            return 0
        watched = 0
        for deal in self._store.deals():
            group = deal.get("three_party") or {}
            if (group.get("state") == CREATED and str(group.get("chat") or "").endswith("@chatroom")
                    and group.get("by") not in TAKEN_BY):
                since = self.since_of(deal) if self.since_of is not None else float(group.get("at") or self._clock())
                self._watch(str(group["chat"]), since)
                watched += 1
        if watched:
            logger.info("PRM: 三方群: %d group(s) read again (read only)", watched)
        return watched

    def read_again(self, deal: Optional[dict]) -> bool:
        """This deal's 三方群 read only again (``watch_group``, from where ``since_of`` says), as ``watch_all`` does at
        start: a group linked by hand that the admin's 「不接群」 (or the console's 「不接了」) gave back (接手任何合作群,
        review 2026-11-09: PrmService.unlink_group).  False: nothing is read (no group, one drafted in, or a backend
        that cannot read groups)."""
        group = (deal or {}).get("three_party") or {}
        if (not self.reading() or group.get("state") != CREATED
                or not str(group.get("chat") or "").endswith("@chatroom") or group.get("by") in TAKEN_BY):
            return False
        since = self.since_of(deal) if self.since_of is not None else float(group.get("at") or self._clock())
        self._watch(str(group["chat"]), since)
        return True

    def _watch(self, chat: str, since: float) -> None:
        watch = getattr(self._backend, "watch_group", None)
        if not callable(watch):
            return
        try:
            watch(chat, since)
        except Exception:
            logger.exception("PRM: 三方群 %s… could not be watched", chat[:8])

    def _unwatch(self, chat: str) -> None:
        unwatch = getattr(self._backend, "unwatch_group", None)
        if not chat or not callable(unwatch):
            return
        try:
            unwatch(chat)
        except Exception:
            logger.exception("PRM: 三方群 %s… could not be unwatched", chat[:8])

    @property
    def enabled(self) -> bool:
        """PRM_GROUP_WITH names somebody: the deals offer 「拉三方群」."""
        return bool(self._with)

    def why_not(self) -> str:
        """Why no group can be made on this PC now ("" = it can)."""
        if not self._with:
            return "没设要一起拉进群的人（PRM_GROUP_WITH）"
        if self._admin is None or not bool(getattr(self._admin, "can_add_friends", False)):
            return "这台电脑没有好友管理组件（需要 Windows 版微信窗口），拉不了群"
        if not callable(getattr(self._backend, "chat_target", None)):
            return "这台电脑的微信读法认不出达人的聊天名字，拉不了群"
        return ""

    def status(self) -> dict:
        """For the console (GET /api/prm ``three_party``): on or off, who goes in besides her, why not now, whether one
        is being made, the name template with the brand filled in ({昵称} left for the console)."""
        return {"enabled": self.enabled, "with": list(self._with), "why": self.why_not() if self.enabled else "",
                "busy": self._busy, "name_template": suggested_name(self._template, self._brand, "{昵称}")}

    def deal_out(self, deal: Optional[dict]) -> dict:
        """``state_out`` and the deal's last 「先试一下」 (``tried``)."""
        out = state_out(deal)
        group = (deal or {}).get("three_party") or {}
        if out:                                   # (read only, for her private drafts: 三方群里的话)
            out["reading"] = bool(group.get("state") == CREATED and group.get("chat") and group.get("by") not in TAKEN_BY
                                  and self.reading())
        tried = self._tries.get(int((deal or {}).get("id") or 0))
        return {**out, "tried": dict(tried)} if tried else out

    @contextmanager
    def guard(self):
        """Hold the lock 「拉三方群」 starts a group under: while it is held no group is started (``start`` reads the deal
        again under it).  Yields the deal whose group is being made now, or None (none is, or only a 「先试一下」 runs:
        it makes nothing, second review 2026-10-31 F8).  老板拉的群 checks a deal's group and links one the boss made under it
        (src/prm/boss_groups.py ``take_over``): our own 三方群 is never taken for the boss's."""
        with self._lock:
            yield None if self._dry else self._busy

    def job_meta(self, deal: Optional[dict]) -> Optional[dict]:
        """What a draft job of this deal tells the Mac of its group (``meta.three_party``): ``{"at": "MM-DD HH:MM"}``
        once it is made (by the bot or marked by hand), else None."""
        group = (deal or {}).get("three_party") or {}
        if group.get("state") != CREATED:
            return None
        at = group.get("at")
        return {"at": time.strftime("%m-%d %H:%M", time.localtime(float(at))) if at else ""}

    # ── 「拉三方群」 / 「先试一下」 ─────────────────────────────────────────

    def start(self, deal_id, *, dry_run: bool = False, actor: str = "console") -> dict:
        """One group made (or, ``dry_run``, tried) for this deal's creator, in a thread of its own: everything that
        stands in the way is said at once (``{"ok": False, "error"}``), nothing is queued.  ``{"ok": True}`` once it
        started; the console watches the deal's ``three_party``."""
        try:
            deal = self._store.deal(int(deal_id))
        except (TypeError, ValueError):
            deal = None
        party = self._store.party(deal["party_id"]) if deal else None
        if deal is None or party is None:
            return {"ok": False, "error": "没有找到这个合作单"}
        why = self.why_not()
        if why:
            return {"ok": False, "error": why}
        if not party.get("active"):
            return {"ok": False, "error": "这位达人已经停用（不在达人名单里），不拉群"}
        state = str((deal.get("three_party") or {}).get("state") or "")
        if not dry_run and state == CREATED:
            return {"ok": False, "error": "这位达人的三方群已经建好了"}
        if not dry_run and state == UNSURE:
            return {"ok": False, "error": "上次不确定建没建成：先在微信里看一眼，点「建好了」或「没建成」"}
        paused = self._paused_why()
        if paused:
            return {"ok": False, "error": f"微信提示过操作太频繁，加好友和拉群都先停着（{paused}）：管理员发「自动同意好友 开」以后再拉"}
        try:
            name, refused = self._backend.chat_target(party["wxid"])
        except Exception:
            logger.warning("PRM: 三方群: her chat's name could not be looked up (deal %d)", deal["id"], exc_info=True)
            name, refused = "", "查她在微信里的名字时出错（详见日志）"
        if refused:
            return {"ok": False, "error": f"不能拉群：{refused}"}
        if not name:
            return {"ok": False, "error": "不知道她在微信里叫什么，没法在「发起群聊」里搜她"}
        if _key(name) in {_key(w) for w in self._with}:
            return {"ok": False, "error": "她在微信里的名字和要一起拉进群的人（PRM_GROUP_WITH）一样，分不清，不拉群"}
        members = [name, *self._with]
        with self._lock:
            if self._busy is not None:
                return {"ok": False, "error": "另一个群正在建（或正在试）：等它好了再点"}
            if not dry_run:
                # Read again under the lock: 老板拉的群 (src/prm/boss_groups.py ``take_over``) may have just linked a group
                # the boss made with her as this deal's (it checks and links under this lock too, ``guard``)
                state = str(((self._store.deal(deal["id"]) or {}).get("three_party") or {}).get("state") or "")
                if state == CREATED:
                    return {"ok": False, "error": "这位达人的三方群已经建好了"}
                if state == UNSURE:
                    return {"ok": False, "error": "上次不确定建没建成：先在微信里看一眼，点「建好了」或「没建成」"}
            self._busy, self._dry = deal["id"], bool(dry_run)
        now = self._clock()
        if not dry_run:
            self._set(deal, {"state": CREATING, "at": now, "by": actor}, now)
        try:
            self.spawn(lambda: self._run(deal["id"], party, members, dry_run, actor))
        except Exception:
            logger.warning("PRM: 三方群: the thread could not start (deal %d)", deal["id"], exc_info=True)
            with self._lock:
                self._busy, self._dry = None, False
            if not dry_run:
                self._set(deal, {"state": FAILED, "at": now, "by": actor, "why": "没能开始建群（详见日志），没有建群"}, now)
            return {"ok": False, "error": "没能开始建群（详见日志），没有建群"}
        logger.info("PRM: 三方群 %s for deal %d (%d member(s) besides this account)", "tried" if dry_run else "started",
                    deal["id"], len(members))
        return {"ok": True, "state": "trying" if dry_run else CREATING}

    def _run(self, deal_id: int, party: dict, members: list, dry_run: bool, actor: str) -> None:
        """The making (its thread): the groups there are before (``group_ids``), ONE ``create_group_checked``, then
        the database looked at for a new group with her in it (``find_group_with``)."""
        try:
            known = None if dry_run else self._backend.group_ids()
            result = self._admin.create_group_checked(members, dry_run=dry_run)
            result = result if isinstance(result, dict) else {"ok": False, "reason": "error"}
            now = self._clock()
            message = str(result.get("message") or "")[:WHY_CHARS] + str(result.get("paused") or "")
            if dry_run:
                self._tries[deal_id] = {"at": now, "ok": bool(result.get("ok")), "message": message[:WHY_CHARS]}
                logger.info("PRM: 三方群 tried for deal %d: %s", deal_id,
                            "picked, nothing made" if result.get("ok") else result.get("reason"))
                return
            deal = self._store.deal(deal_id) or {"id": deal_id}
            if not result.get("ok"):
                maybe = result.get("reason") in ("not_created", "risk", "error")      # 「完成」 may have been clicked
                self._set(deal, {"state": UNSURE if maybe else FAILED, "at": now, "by": actor,
                                 "why": message or "没建成"}, now)
                logger.info("PRM: 三方群 for deal %d not made: %s", deal_id, result.get("reason"))
                return
            found = self._find(str(party.get("wxid") or ""), known)
            if found is None:
                self._set(deal, {"state": UNSURE, "at": now, "by": actor,
                                 "why": "微信里点了「完成」，但数据库里没看到有她的新群（数据库读不了，或者还没写进去）："
                                        "请在微信里看一眼，再点「建好了」或「没建成」"}, now)
                logger.info("PRM: 三方群 for deal %d: 完成 was clicked but no new group with her was seen", deal_id)
                return
            self._set(deal, {"state": CREATED, "at": now, "by": actor, "chat": found["chat"],
                             "name": str(found.get("name") or "")[:60], "members": int(found.get("members") or 0)}, now)
            self._watch(str(found["chat"]), now - WATCH_BACK_SEC)   # read only from now on: its lines for her drafts
            logger.info("PRM: 三方群 for deal %d made (%d member(s) in it)", deal_id, int(found.get("members") or 0))
        except Exception:
            logger.exception("PRM: 三方群 for deal %d failed", deal_id)
            if dry_run:
                self._tries[deal_id] = {"at": self._clock(), "ok": False, "message": "试的时候出错（详见日志）"}
            else:
                now = self._clock()
                self._set(self._store.deal(deal_id) or {"id": deal_id},
                          {"state": UNSURE, "at": now, "by": actor,
                           "why": "拉群时出错（详见日志）：请在微信里看一下群建了没有，再点「建好了」或「没建成」"}, now)
        finally:
            with self._lock:
                self._busy, self._dry = None, False

    def _find(self, wxid: str, known) -> Optional[dict]:
        """The new group with her in it, looked for FIND_LOOKS times FIND_STEP_SEC apart (WeChat writes it to its
        database in a moment); None when the database could not be read before, or it never showed."""
        if known is None or not wxid:
            return None
        for look in range(FIND_LOOKS):
            found = self._backend.find_group_with(wxid, known)
            if found:
                return found
            if look + 1 < FIND_LOOKS:
                self._sleep(FIND_STEP_SEC)
        return None

    # ── 「建好了」 / 「没建成」, and a stop in the middle ────────────────────

    def mark(self, deal_id, made: bool, actor: str = "console") -> dict:
        """An operator says what is in WeChat: 「建好了」 (``made``: after an ``unsure`` or ``failed`` one, or a group
        made by hand with no record; the Mac hears of it from now on) or 「没建成」 (after an ``unsure`` or ``failed``
        one, or a made group that is gone: the deal has no group, 「拉三方群」 may be clicked again).  Not while one is
        being made."""
        try:
            deal = self._store.deal(int(deal_id))
        except (TypeError, ValueError):
            deal = None
        if deal is None:
            return {"ok": False, "error": "没有找到这个合作单"}
        group = dict(deal.get("three_party") or {})
        state = str(group.get("state") or "")
        if state == CREATING or self._busy == deal["id"]:
            return {"ok": False, "error": "这个群正在建，等它好了再说"}
        if (made and state not in ("", UNSURE, FAILED)) or (not made and state not in (UNSURE, FAILED, CREATED)):
            return {"ok": False, "error": "三方群已经是这个状态了"}
        now = self._clock()
        if made:
            self._set(deal, {"state": CREATED, "at": now, "by": actor, **({"chat": group["chat"]} if group.get("chat") else {})},
                      now)
            if group.get("chat") and group.get("by") not in TAKEN_BY:
                self._watch(str(group["chat"]), now - WATCH_BACK_SEC)
        else:
            self._set(deal, {}, now)
            if group.get("chat") and group.get("by") not in TAKEN_BY:
                self._unwatch(str(group["chat"]))       # gone: no longer read
        return {"ok": True, "state": CREATED if made else ""}

    def settle(self) -> int:
        """At start: a group a stopped bot was making is ``unsure`` (nobody knows whether 「完成」 was clicked).
        Returns how many."""
        settled = 0
        for deal in self._store.deals():
            if (deal.get("three_party") or {}).get("state") == CREATING:
                now = self._clock()
                self._set(deal, {"state": UNSURE, "at": now, "by": "system",
                                 "why": "机器人在建群的时候停了：请在微信里看一眼，再点「建好了」或「没建成」"}, now)
                settled += 1
        if settled:
            logger.info("PRM: 三方群: %d group(s) a stopped bot was making are unsure", settled)
        return settled

    # ── helpers ───────────────────────────────────────────────────────────

    def _paused_why(self) -> str:
        paused = getattr(self._auto, "paused_why", None)
        if not callable(paused):
            return ""
        try:
            return str(paused() or "")
        except Exception:
            logger.debug("PRM: 三方群: the auto-accept pause could not be read", exc_info=True)
            return ""

    def _set(self, deal: dict, group: dict, now: float) -> None:
        """The deal's group, its record (``EVENT``) and its notice (only while ``unsure``)."""
        deal_id = int(deal["id"])
        self._store.update_deal(deal_id, now, three_party=group)
        party_id = deal.get("party_id")
        if party_id is None:
            party_id = (self._store.deal(deal_id) or {}).get("party_id")
        detail = {k: group[k] for k in ("state", "by", "members") if k in group} or {"state": ""}
        if group.get("why"):
            detail["why"] = str(group["why"])[:200]
        self._store.add_event(deal_id, party_id, EVENT, detail,
                              actor="console" if group.get("by") == "console" else "system", now=now)
        if group.get("state") == UNSURE:
            self._store.set_notice(deal_id, NOTICE, "三方群" + str(group.get("why") or "不确定建没建成"), now)
        else:
            self._store.drop_notices(deal_id, lambda n: n["key"] == NOTICE, now)
