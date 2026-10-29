"""加百应达人微信 (``PRM_FRIEND_ADD``, default off; CODEBASE_REFERENCE 功能 R).

A creator invited by a 百应 邀约卡 who left her contact becomes ONE WeChat friend request, made by the existing
``FriendManager.add_friend`` after an operator confirmed it in the console (or the admin said 「加达人 N」); once she
accepted and an operator said it is her, she is registered as a creator with her 抖音号 (``creator_links``), so the Mac
drafts her replies with her invite in mind.  It is no draft: she has no wxid yet, so no party, so the record lives in
a table of its own (``friend_adds``, src/prm/store.py) with states of its own.

Where a record comes from:

- ``paste``: an operator types her 抖音号 and the 微信号 / 手机号 she left (合作单 page); with no number she is waited
  for (``waiting``: she adds us).
- ``offer_from_buyin``: the 百应 inbox's hook (``PrmService.on_buyin_contact``).  dy-im-inbox calls it for each new
  联系方式卡片 (and, later, each contact in her own words) of a conversation WE invited (its ledger match gave
  ``invited``) or of a registered creator (``party_id``); another person's conversations are never stored, only
  counted there.  STEP 7, NOT WIRED YET: nothing calls it until dy-im-inbox's contact commit is merged; the event is
  the one agreed with webot-04 on 2026-10-29 (``creator.uid`` is the page's buyinId; ``contact`` the full values from
  ``card/msgs``, or ``{"unavailable": true, "why": older | not-answered | read-failed | no-card-id | own}``).

The safety rules (design §6):

- one confirmation is one ``add_friend`` call: under one lock the switch, the friend manager, nothing else adding,
  ``PRM_FRIEND_ADD_GAP_SEC`` since the last one, fewer than ``PRM_FRIEND_ADD_DAILY_MAX`` today and the auto-accept not
  paused are checked and the record goes to ``adding`` (compare-and-set); then one thread calls the admin handler's
  ``add_friend_checked`` once.  Anything missing is refused at once with the reason, never queued;
- WeChat's risk prompt pauses the auto-accept the way the admin's 添加好友 does (``add_friend_checked``), the record is
  ``blocked`` and every add waits until the admin says 自动同意好友 开; nothing is retried by itself, ever; a bot
  stopped in the middle finds the record ``unsure`` at its next start;
- the full numbers are on disk only in ``friend_adds.wechat / phone`` (``add_friend`` needs them): cleared when she is
  linked or dropped, 14 days after a request she did not accept, 7 days after nobody touched the record, 30 days at
  most; the logs, the console, the events and the admin's answers only ever have the masked form.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from typing import Callable, Optional

from . import contact
from .contact import KINDS, LABELS, PHONE, WECHAT, ContactError, masked_contact, parse_contact, parse_contacts
from .platforms import creator_account_ok

logger = logging.getLogger(__name__)

# ── states (src/prm/store.py friend_adds.state) ─────────────────────────────
OFFERED, NEEDS_VIEW, WAITING, ADDING, REQUESTED = "offered", "needs_view", "waiting", "adding", "requested"
NOT_FOUND, KNOWN, FAILED, UNSURE, BLOCKED = "not_found", "known", "failed", "unsure", "blocked"
LINKED, DROPPED, EXPIRED = "linked", "dropped", "expired"
OPEN = (OFFERED, NEEDS_VIEW, WAITING, ADDING, REQUESTED, NOT_FOUND, KNOWN, FAILED, UNSURE, BLOCKED)
ENDED = (LINKED, DROPPED, EXPIRED)
STATES = {
    OFFERED: "待确认", NEEDS_VIEW: "要在百应里看了号码再填", WAITING: "等她加我们", ADDING: "正在加",
    REQUESTED: "已发申请，等她通过", NOT_FOUND: "微信里搜不到", KNOWN: "已经是微信好友", FAILED: "没发出去",
    UNSURE: "不确定申请发出去没有", BLOCKED: "微信提示操作频繁，已暂停", LINKED: "已登记并关联", DROPPED: "不加",
    EXPIRED: "过期了（号码已清除）",
}
CAN_ADD_FROM = (OFFERED, FAILED, BLOCKED)          # unsure: an operator says 没发出去 first (-> failed)
SOURCES = {"paste": "运营手动填的", "buyin_card": "百应私信里的联系方式卡片", "buyin_text": "她在百应私信里打的字"}
USES = (WECHAT, PHONE)

# What add_friend's ``reason`` says about the request (friend_manager.add_friend): surely not sent, or not known.
SURE_NOT_SENT = ("invalid", "no_ocr", "no_window", "aborted", "no_search_box", "no_search_entry", "no_add_button",
                 "no_manager", "not_started")
REASONS = {
    "ok": "已发出好友申请",
    "not_found": "微信里搜不到这个号：号可能不对，或者她关了「可以通过微信号 / 手机号搜到我」",
    "already_friend": "她已经是微信好友，不用加",
    "invalid": "号码不对，什么也没搜",
    "no_ocr": "这台电脑的屏幕识别（OCR）用不了，什么也没做",
    "no_window": "没找到微信窗口，或者切不到前台，什么也没做",
    "aborted": "微信窗口的状态不对，中途停了，申请没发出去",
    "no_search_box": "没认出微信的搜索框，什么也没输入",
    "no_search_entry": "搜索结果里没有「网络查找」，没有发申请",
    "no_add_button": "没找到「添加到通讯录」按钮，没有发申请",
    "no_manager": "这台电脑没有好友管理组件（要 Windows 版微信窗口）",
    "not_started": "机器人没能开始加（详见日志），什么也没做",
    "not_sent": "申请窗口还开着，申请可能没发出去",
    "no_dialog": "没出现填附言的申请窗口：她可能设了「加我为朋友时不需要验证」（那就已经加上了），也可能是界面不一样",
    "error": "加好友时出错了（详见日志），不确定申请发出去没有",
    "risk": "微信提示「操作过于频繁」，已经停了：自动同意好友和控制台加好友都暂停，过几个小时由管理员发「自动同意好友 开」再继续",
    "restart": "加好友时机器人停了，不确定申请发出去没有",
    "by_phone": "运营说在手机上加了",
    "unsure_sent": "运营在微信里看过：申请发出去了",
    "unsure_not_sent": "运营在微信里看过：申请没发出去",
    "registered": "她已经是登记达人，不用加",
    "requested_days": "申请发出去 14 天她还没通过，号码按规定清除了",
    "untouched_days": "7 天没人处理，号码按规定清除了",
    "max_days": "留了 30 天，号码按规定清除了",
}

# ── settings (src/config.py; the tests' config objects have none: the defaults) ─
DAILY_MAX_DEFAULT, DAILY_MAX_RANGE = 10, (1, 30)
GAP_SEC_DEFAULT, GAP_SEC_RANGE = 120, (30, 3600)
GREETING_DEFAULT = "您好{昵称}，我是{品牌}，百应上联系您的～"
GREETING_MAX = 30                  # WeChat's own limit is not known yet (design §13): kept short
NICK_CHARS = 8                     # of her 百应 nickname in the greeting
NICKNAME_CHARS = 30
BRAND_CHARS = 20
REQUESTED_DAYS = 14                # a request she did not accept: its numbers go, the record expires
UNTOUCHED_DAYS = 7                 # a record nobody touched: its numbers go
VALUES_MAX_DAYS = 30               # however it stands
LINK_DAYS = 30                     # an expired record can still be linked this long after it was made
ADD_WAIT_SEC = 180                 # the admin's 加达人 N waits this long for the window action
SWEEP_SEC = 600
CLOSED_SHOWN = 20                  # ended records the console lists
ADMIN_LISTED = 10
INVITED_KEYS = ("aweme_id", "invited_at", "plan_id", "attempt", "category", "products", "commission_rate")
DAY = 86400


class FriendAddError(ValueError):
    """An action that cannot be done now (the message says why, in Chinese)."""


def _clip(value, limit: int) -> str:
    return " ".join(str(value if value is not None else "").split())[:limit]


def greeting_for(template, nickname, brand) -> str:
    """The greeting a record starts with: ``PRM_FRIEND_ADD_GREETING`` with ``{昵称}`` her 百应 nickname (its first 8
    characters; none when a number is in it) and ``{品牌}`` ``PRM_FRIEND_ADD_BRAND`` (this PC's .env; without one,
    「我是{品牌}，」 is left out), at most 30 characters (a shorter nickname, or none, when it would be longer)."""
    template = _clip(template, 200) or GREETING_DEFAULT
    brand = _clip(brand, BRAND_CHARS)
    nick = _clip(nickname, 200)
    if contact.has_masked(nick) or contact.find_contacts(nick):
        nick = ""
    if not brand:
        template = re.sub(r"我是\{品牌\}[，,、]?\s*", "", template).replace("{品牌}", "")
    text = template
    for chars in (NICK_CHARS, 4, 0):
        text = _clip(template.replace("{昵称}", nick[:chars]).replace("{品牌}", brand), 200)
        if len(text) <= GREETING_MAX:
            return text
    return text[:GREETING_MAX]


def check_greeting(text) -> str:
    """A greeting an operator typed, as add_friend gets it (one line); FriendAddError when it is too long."""
    text = _clip(text, 400)
    if len(text) > GREETING_MAX:
        raise FriendAddError(f"附言最多 {GREETING_MAX} 个字（现在 {len(text)} 个）")
    return text


def invited_of(value) -> dict:
    """The invite the Mac's ledger match gave a 百应 conversation (dy-im-inbox: ``{aweme_id, invited_at, plan_id,
    attempt, category, products, commission_rate}``), only those keys and only plain values; {} when there is none
    (no valid 抖音号: not ours)."""
    if not isinstance(value, dict):
        return {}
    aweme = str(value.get("aweme_id") or "").strip()
    if not creator_account_ok(aweme):
        return {}
    out = {"aweme_id": aweme}
    for key in ("invited_at", "plan_id", "attempt", "category"):
        if isinstance(value.get(key), (str, int, float)) and not isinstance(value.get(key), bool) and str(value[key]):
            out[key] = _clip(value[key], 64)
    products = value.get("products")
    if isinstance(products, list):
        out["products"] = [_clip(p, 40) for p in products if isinstance(p, (str, int)) and not isinstance(p, bool)][:10]
    rate = value.get("commission_rate")
    if isinstance(rate, (int, float)) and not isinstance(rate, bool) and 0 <= rate <= 80:
        out["commission_rate"] = rate
    return out


def _local_midnight(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def _clock_text(at) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(at)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def clear_clipboard_if(text: str) -> bool:
    """Windows: empty the clipboard when it still holds ``text`` (add_friend pastes the number and does not put the
    clipboard back; normally the greeting is pasted after it).  False elsewhere or when it holds something else."""
    if sys.platform != "win32" or not text:
        return False
    try:
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return False
            if str(win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or "").strip() != text:
                return False
            win32clipboard.EmptyClipboard()
            return True
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        logger.debug("FriendAdds: the clipboard could not be looked at", exc_info=True)
        return False


def _spawn(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="prm-friend-add", daemon=True).start()


class FriendAdds:
    """The records, the confirmation and the one add per confirmation (see the module's doc).

    ``register(wxid, name, note, link=...)`` is ``PrmService.register``; ``attach`` gives it the admin handler (its
    ``add_friend_checked`` and whether there is a friend manager), the auto-accept service (whether it is paused) and
    the admin's notifier, once the bot started its friend management (Bot.run).  ``spawn(fn)`` runs the add (a
    thread) and ``clear_clipboard(text)`` empties the clipboard after it when it still holds the number: seams for
    tests."""

    def __init__(self, store, config=None, *, clock: Callable[[], float] = time.time,
                 register: Optional[Callable[..., dict]] = None, spawn: Callable = _spawn,
                 clear_clipboard: Callable[[str], bool] = clear_clipboard_if):
        self._store = store
        self._clock = clock
        self._register = register
        self.spawn = spawn
        self.clear_clipboard = clear_clipboard
        self._enabled = str(getattr(config, "prm_friend_add", "off") or "off") == "on"
        self._daily_max = self._int(config, "prm_friend_add_daily_max", DAILY_MAX_DEFAULT, DAILY_MAX_RANGE)
        self._gap_sec = self._int(config, "prm_friend_add_gap_sec", GAP_SEC_DEFAULT, GAP_SEC_RANGE)
        self._template = _clip(getattr(config, "prm_friend_add_greeting", "") or GREETING_DEFAULT, 200) or GREETING_DEFAULT
        self._brand = _clip(getattr(config, "prm_friend_add_brand", "") or "", BRAND_CHARS)
        self._admin = None
        self._auto = None
        self._notify: Optional[Callable[[str], object]] = None
        self._lock = threading.RLock()               # a confirmation's checks and its compare-and-set; links
        self._last_sweep = 0.0

    @staticmethod
    def _int(config, name: str, default: int, bounds: tuple) -> int:
        try:
            value = int(getattr(config, name, default))
        except (TypeError, ValueError):
            value = default
        return min(max(value, bounds[0]), bounds[1])

    # ── wiring (Bot.run) ──────────────────────────────────────────────────

    def attach(self, admin=None, auto=None, notify: Optional[Callable[[str], object]] = None) -> None:
        """The admin handler (``add_friend_checked``, ``can_add_friends``), the auto-accept service (``paused_why``)
        and the admin's notifier (a risk prompt during a console add is told to the admin)."""
        self._admin, self._auto, self._notify = admin, auto, notify

    @property
    def enabled(self) -> bool:
        return self._enabled

    def can_add(self) -> bool:
        """The switch is on and this PC has the friend manager (a WeChat window to drive)."""
        return self._enabled and self._admin is not None and bool(getattr(self._admin, "can_add_friends", False))

    def _paused_why(self) -> str:
        paused = getattr(self._auto, "paused_why", None)
        if not callable(paused):
            return ""
        try:
            return str(paused() or "")
        except Exception:
            logger.debug("FriendAdds: the auto-accept pause could not be read", exc_info=True)
            return ""

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> int:
        """A bot stopped while it added (``adding``): not known whether the request went out (``unsure``); the
        attempts it left open end the same way.  Returns how many records."""
        now = self._clock()
        moved = 0
        for record in self._store.friend_adds(states=(ADDING,)):
            if self._store.move_friend_add(record["id"], (ADDING,), UNSURE, now=now, reason="restart"):
                moved += 1
                logger.warning("FriendAdds: #%d was being added when the bot stopped: unsure", record["id"])
        for attempt in self._store.friend_attempts(unfinished=True):
            self._store.finish_friend_attempt(attempt["id"], "restart", now=now)
        return moved

    # ── where a record comes from ─────────────────────────────────────────

    def greeting(self, nickname: str = "") -> str:
        return greeting_for(self._template, nickname, self._brand)

    def offer_from_buyin(self, event) -> Optional[dict]:
        """The 百应 inbox's hook (``PrmService.on_buyin_contact``, dy-im-inbox step 7): one call per new contact of a
        conversation; returns ``{"id", "state", "masked"}`` or None (the switch is off, not our conversation, nothing
        to add, our own number she sent back).  The same (conversation, message) again gives the same record."""
        if not self._enabled or not isinstance(event, dict):
            return None
        invited = invited_of(event.get("invited"))
        party_id = self._party_id(event.get("party_id"))
        if not invited and party_id is None:
            return None                               # another person's conversation: never stored
        given = event.get("contact") if isinstance(event.get("contact"), dict) else {}
        if given.get("unavailable") and str(given.get("why") or "") == "own":
            return None                               # the card held only our own number (the Mac emptied it)
        values = {}
        for kind in KINDS:
            try:
                value = parse_contact(kind, given.get(kind))
            except ContactError:
                logger.warning("FriendAdds: a %s from 百应 is not one the bot can search for; left out", LABELS[kind])
                value = ""
            if value:
                values[kind] = value
        unavailable = bool(given.get("unavailable")) and not values
        if not values and not unavailable:
            return None
        cid = _clip(event.get("conversation_id"), 128)
        mid = _clip(event.get("message_id"), 128)
        creator = event.get("creator") if isinstance(event.get("creator"), dict) else {}
        aweme = str(creator.get("aweme_id") or invited.get("aweme_id") or "").strip()
        aweme = aweme if creator_account_ok(aweme) else ""
        source = "buyin_text" if event.get("source") == "buyin_text" else "buyin_card"
        now = self._clock()
        with self._lock:
            known = self._store.friend_add_by_message(cid, mid) if mid else None
            if known is not None:
                return self._summary(known)
            fields = dict(source=source, conversation_id=cid, message_id=mid, aweme_id=aweme,
                          creator_uid=_clip(creator.get("uid"), 40), nickname=contact.clean_name(creator.get("nickname")),
                          masked=masked_contact(values), unavailable=unavailable, invited=invited)
            if party_id is not None:
                return self._registered_left_contact(party_id, values, fields, now)
            same = self._open_for(aweme)
            if same is not None:
                if same["state"] == NEEDS_VIEW and values:
                    self._store.move_friend_add(same["id"], (NEEDS_VIEW,), OFFERED, now=now, unavailable=False,
                                                masked=masked_contact(values), **self._values(values))
                    same = self._store.friend_add(same["id"])
                return self._summary(same)
            record, made = self._store.add_friend_add(
                state=OFFERED if values else NEEDS_VIEW, now=now, greeting=self.greeting(fields["nickname"]),
                **self._values(values), **fields)
        if made:
            logger.info("FriendAdds: #%d %s from 百应 (%s)", record["id"], record["state"],
                        contact.masked_line(values, head=source))
        return self._summary(record)

    def _registered_left_contact(self, party_id: int, values: dict, fields: dict, now: float) -> dict:
        """A registered creator left her contact in 百应: she is a WeChat friend already, nothing to add; the record
        is ``linked`` at once (no number kept) and her deal says so."""
        deal = self._store.deal_of(party_id, now)
        record, made = self._store.add_friend_add(
            state=LINKED, now=now, party_id=party_id, deal_id=deal["id"], reason="registered", linked_at=now,
            closed_at=now, cleared_at=now, **fields)
        if made:
            self._store.add_event(deal["id"], party_id, "friend_add_contact", {
                "add": record["id"], "line": contact.masked_line(values, record["id"]),
                "conversation": fields["conversation_id"]}, actor="creator", now=now)
            logger.info("FriendAdds: #%d a registered creator left her contact in 百应: nothing to add", record["id"])
        return self._summary(record)

    def paste(self, aweme_id, nickname="", contact_values=None, greeting=None, *, actor: str = "console") -> dict:
        """An operator types what a creator left in 百应 (合作单 page): her 抖音号 (required), her nickname, her 微信号 /
        手机号 (none: she is waited for, ``waiting``) and the greeting (else the default one)."""
        if not self._enabled:
            raise FriendAddError("加百应达人微信没打开（高级设置 › 达人副驾驶 › PRM_FRIEND_ADD）")
        aweme = str(aweme_id or "").strip()
        if not creator_account_ok(aweme):
            raise FriendAddError("要填她的抖音号（字母、数字、下划线、点或短横线，最多 40 个）")
        try:
            values = parse_contacts(contact_values or {})
        except ContactError as e:
            raise FriendAddError(str(e)) from e
        nick = contact.clean_name(nickname)
        text = check_greeting(greeting) if greeting not in (None, "") else self.greeting(nick)
        now = self._clock()
        with self._lock:
            same = self._open_for(aweme)
            if same is not None:
                raise FriendAddError(f"抖音号 {aweme} 已经有一条没处理完的 #{same['id']}（{STATES[same['state']]}）")
            linked = self._store.creator_links(account=aweme)
            if linked:
                party = self._store.party(linked[0]["party_id"]) or {}
                raise FriendAddError(f"抖音号 {aweme} 已经关联了登记达人「{party.get('name') or linked[0]['party_id']}」")
            record, _ = self._store.add_friend_add(
                state=OFFERED if values else WAITING, now=now, source="paste", aweme_id=aweme, nickname=nick,
                masked=masked_contact(values), greeting=text, **self._values(values))
        logger.info("FriendAdds: #%d %s by %s (paste, %s)", record["id"], record["state"], actor,
                    contact.masked_line(values, head="paste") if values else "no number")
        return {"ok": True, "id": record["id"], "state": record["state"]}

    @staticmethod
    def _values(values: dict) -> dict:
        return {kind: values.get(kind, "") for kind in KINDS}

    def _party_id(self, value) -> Optional[int]:
        try:
            party = self._store.party(int(value)) if value is not None and not isinstance(value, bool) else None
        except (TypeError, ValueError):
            return None
        return int(party["id"]) if party else None

    def _open_for(self, aweme: str) -> Optional[dict]:
        """The open record of this 抖音号 (one at a time)."""
        if not aweme:
            return None
        return next(iter(self._store.friend_adds(states=OPEN, aweme_id=aweme)), None)

    @staticmethod
    def _summary(record: dict) -> dict:
        return {"id": record["id"], "state": record["state"], "masked": dict(record.get("masked") or {})}

    # ── the one add ───────────────────────────────────────────────────────

    def today(self, now: Optional[float] = None) -> int:
        """add_friend calls made from here today (this PC's calendar day; the console's and the admin's 加达人 N)."""
        now = self._clock() if now is None else now
        return len(self._store.friend_attempts(since=_local_midnight(now)))

    def _last_at(self) -> Optional[float]:
        last = next(iter(self._store.friend_attempts()), None)
        return float(last["at"]) if last else None

    def _busy(self) -> Optional[dict]:
        return next(iter(self._store.friend_adds(states=(ADDING,))), None)

    def blocked_why(self, now: Optional[float] = None) -> str:
        """Why no add can start now ("" when one can): the switch, the friend manager, the auto-accept's pause,
        another add, the gap, the day's cap."""
        now = self._clock() if now is None else now
        if not self._enabled:
            return "加百应达人微信没打开（PRM_FRIEND_ADD）"
        if not self.can_add():
            return "这台电脑没有好友管理组件（要 Windows 版微信窗口），不能在这里加：可以复制号码在手机上加"
        paused = self._paused_why()
        if paused:
            return f"自动同意好友暂停着（{paused}）：控制台加好友也停着，管理员发「自动同意好友 开」以后再加"
        busy = self._busy()
        if busy is not None:
            return f"另一个加好友正在进行（#{busy['id']}），等它结束再点"
        last = self._last_at()
        if last is not None and now - last < self._gap_sec:
            wait = int(self._gap_sec - (now - last)) + 1
            return f"离上一次加好友（{_clock_text(last)}）不到 {self._gap_sec // 60 or 1} 分钟，再等 {wait} 秒"
        done = self.today(now)
        if done >= self._daily_max:
            return f"今天已经加了 {done} 个，到了每天的上限 {self._daily_max}（PRM_FRIEND_ADD_DAILY_MAX），明天再加"
        return ""

    def confirm(self, add_id, *, by: str = "console", greeting=None, use=None, wait: bool = False) -> dict:
        """An operator's 「添加该达人微信」 (``by`` console) or the admin's 加达人 N (``by`` admin, ``wait``: the answer
        says how it went): one add_friend call, or a refusal saying why.  ``use``: the 微信号 or the 手机号."""
        if not self._enabled:
            raise FriendAddError("加百应达人微信没打开（PRM_FRIEND_ADD）")
        now = self._clock()
        with self._lock:
            record = self._record(add_id)
            if record["state"] not in CAN_ADD_FROM:
                raise FriendAddError(self._why_not_addable(record))
            problem = self.blocked_why(now)
            if problem:
                raise FriendAddError(problem)
            use = use if use in USES else (WECHAT if record["wechat"] else PHONE)
            target = record.get(use) or ""
            if not target:
                raise FriendAddError(f"#{record['id']} 没有{LABELS[use]}可以加" + ("（号码已经按规定清除了）"
                                                                            if record.get("cleared_at") else ""))
            text = check_greeting(greeting) if greeting is not None else record["greeting"]
            if not self._store.move_friend_add(record["id"], (record["state"],), ADDING, now=now, greeting=text,
                                               reason=""):
                raise FriendAddError("这条刚刚被处理了，刷新看看")
            attempt = self._store.add_friend_attempt(record["id"], by=by, kind=use, now=now)
        shown = contact.masked(use, target)
        logger.info("FriendAdds: #%d adding by %s (%s %s)", record["id"], by, LABELS[use], shown)
        finished = threading.Event()

        def run() -> None:
            try:
                self._add(record["id"], attempt, use, target, text, by)
            finally:
                finished.set()

        try:
            self.spawn(run)
        except Exception:                             # (no thread: add_friend was never called)
            logger.exception("FriendAdds: #%d the add could not be started", record["id"])
            self._store.move_friend_add(record["id"], (ADDING,), FAILED, now=self._clock(), reason="not_started")
            self._store.finish_friend_attempt(attempt, "not_started", now=self._clock())
            finished.set()
        if wait:
            finished.wait(ADD_WAIT_SEC)
        after = self._store.friend_add(record["id"]) or record
        return {"ok": True, "id": after["id"], "state": after["state"], "reason": after.get("reason", ""),
                "target": shown, "greeting": text}

    def _add(self, add_id: int, attempt: int, use: str, target: str, text: str, by: str) -> None:
        """The add itself (in its own thread): one ``add_friend_checked``, its reason into the record's state.  Its
        ``message`` holds the number: it is never stored nor logged."""
        try:
            result = self._admin.add_friend_checked(target, text)
        except Exception:
            logger.exception("FriendAdds: #%d add_friend failed", add_id)
            result = {"ok": False, "reason": "error"}
        result = result if isinstance(result, dict) else {"ok": False, "reason": "error"}
        reason = "ok" if result.get("ok") else str(result.get("reason") or "error")[:40]
        try:
            self.clear_clipboard(target)
        except Exception:
            logger.debug("FriendAdds: the clipboard could not be cleared", exc_info=True)
        now = self._clock()
        state = (REQUESTED if reason == "ok" else NOT_FOUND if reason == "not_found" else KNOWN
                 if reason == "already_friend" else BLOCKED if reason == "risk" else FAILED
                 if reason in SURE_NOT_SENT else UNSURE)
        extra = {"requested_at": now} if state == REQUESTED else {}
        self._store.move_friend_add(add_id, (ADDING,), state, now=now, reason=reason, **extra)
        self._store.finish_friend_attempt(attempt, reason, now=now)
        log = logger.warning if state in (BLOCKED, UNSURE) else logger.info
        log("FriendAdds: #%d %s (%s; %s %s)", add_id, state, reason, LABELS[use], contact.masked(use, target))
        if state == BLOCKED and by != "admin":
            self._tell(f"控制台加百应达人微信（#{add_id}）时微信提示「操作过于频繁」：自动同意好友和控制台加好友都暂停了。"
                       "先别再加好友、同意好友，过几个小时再发「自动同意好友 开」继续。")

    def _tell(self, text: str) -> None:
        if self._notify is None:
            return
        try:
            self._notify(text)
        except Exception:
            logger.warning("FriendAdds: the admin could not be told", exc_info=True)

    def _record(self, add_id) -> dict:
        try:
            record = self._store.friend_add(int(add_id))
        except (TypeError, ValueError):
            record = None
        if record is None:
            raise FriendAddError("没有这一条加微信的记录")
        return record

    def _why_not_addable(self, record: dict) -> str:
        state = record["state"]
        if state == ADDING:
            return f"#{record['id']} 已经在加了"
        if state == REQUESTED:
            return f"#{record['id']} 已经发过申请（{_clock_text(record.get('requested_at'))}），等她通过"
        if state == UNSURE:
            return f"#{record['id']} 不确定上次发出去没有：先到微信里看一眼，点「发出去了」或「没发出去」"
        if state == NOT_FOUND:
            return f"#{record['id']} 上次微信里搜不到：改了号再加，或者请她加我们"
        if state == NEEDS_VIEW:
            return f"#{record['id']} 还没有号码：在百应里看了再填"
        return f"#{record['id']} {STATES.get(state, state)}，不能加"

    # ── an operator's other answers ───────────────────────────────────────

    def act(self, add_id, action: str, data: Optional[dict] = None, *, actor: str = "console") -> dict:
        """The console's actions on a record (``POST /api/prm/friend-adds``); FriendAddError says why one cannot be
        done.  ``reveal`` answers with the full numbers (written down); every other answer has them masked."""
        data = data if isinstance(data, dict) else {}
        if action == "add":
            return self.confirm(add_id, by=actor, greeting=data.get("greeting"), use=data.get("use"))
        if action == "reveal":
            return self.reveal(add_id, actor=actor)
        if action == "link":
            return self.link(add_id, data.get("wxid"), data.get("name"), actor=actor)
        moves = {
            "sent_by_phone": ((OFFERED, NEEDS_VIEW, WAITING, NOT_FOUND, FAILED, BLOCKED), REQUESTED, "by_phone"),
            "sent_already": ((UNSURE,), REQUESTED, "unsure_sent"),
            "not_sent": ((UNSURE,), FAILED, "unsure_not_sent"),
            "wait_her": ((OFFERED, NEEDS_VIEW, NOT_FOUND, FAILED, BLOCKED), WAITING, ""),
        }
        now = self._clock()
        with self._lock:
            record = self._record(add_id)
            if action in moves:
                froms, to, reason = moves[action]
                if record["state"] not in froms:
                    raise FriendAddError(f"#{record['id']} 现在是「{STATES.get(record['state'], record['state'])}」，不能这样改")
                extra = {"requested_at": now} if to == REQUESTED else {}
                if action == "sent_already":
                    last = next(iter(self._store.friend_attempts(add_id=record["id"])), None)
                    extra = {"requested_at": float(last["at"]) if last else now}
                if not self._store.move_friend_add(record["id"], (record["state"],), to, now=now, reason=reason, **extra):
                    raise FriendAddError("这条刚刚被处理了，刷新看看")
            elif action == "edit":
                self._edit(record, data, now)
            elif action == "drop":
                if record["state"] not in OPEN or record["state"] == ADDING:
                    raise FriendAddError(f"#{record['id']} 现在是「{STATES.get(record['state'], record['state'])}」，不能记成不加")
                if not self._store.move_friend_add(record["id"], (record["state"],), DROPPED, now=now, closed_at=now):
                    raise FriendAddError("这条刚刚被处理了，刷新看看")
                self._store.clear_friend_add_values(record["id"], now=now)
            elif action == "link_on_accept":
                self._store.update_friend_add(record["id"], now=now, link_on_accept=bool(data.get("on")))
            else:
                raise FriendAddError(f"不认识的操作 {action}")
            after = self._store.friend_add(record["id"])
        logger.info("FriendAdds: #%d %s by %s -> %s", after["id"], action, actor, after["state"])
        return {"ok": True, "id": after["id"], "state": after["state"]}

    def _edit(self, record: dict, data: dict, now: float) -> None:
        """改号 / 填号码 (not found, no number read, failed, waiting for her, not added yet): the values given replace
        those of their kind; the record waits for a confirmation again."""
        if record["state"] not in (OFFERED, NEEDS_VIEW, NOT_FOUND, FAILED, WAITING):
            raise FriendAddError(f"#{record['id']} 现在是「{STATES.get(record['state'], record['state'])}」，不能改号")
        try:
            given = parse_contacts(data.get("contact") or {})
        except ContactError as e:
            raise FriendAddError(str(e)) from e
        if not given:
            raise FriendAddError("要填她的微信号或手机号")
        values = {kind: given.get(kind) or record.get(kind) or "" for kind in KINDS}
        values = {k: v for k, v in values.items() if v}
        if not self._store.move_friend_add(record["id"], (record["state"],), OFFERED, now=now, reason="",
                                           unavailable=False, masked=masked_contact(values), cleared_at=None,
                                           **self._values(values)):
            raise FriendAddError("这条刚刚被处理了，刷新看看")

    def reveal(self, add_id, *, actor: str = "console") -> dict:
        """The full numbers for the operator's own page (显示完整 / 复制号码): every look is written down (``reveals``)."""
        now = self._clock()
        with self._lock:
            record = self._record(add_id)
            if not record["wechat"] and not record["phone"]:
                raise FriendAddError("号码已经按规定清除了" if record.get("cleared_at") else "这条没有号码")
            reveals = [*(record.get("reveals") or []), {"at": now, "actor": str(actor)[:20]}][-50:]
            self._store.update_friend_add(record["id"], now=now, reveals=reveals)
        logger.info("FriendAdds: #%d numbers shown in full (%s)", record["id"], actor)
        return {"ok": True, "id": record["id"], "wechat": record["wechat"], "phone": record["phone"]}

    def link(self, add_id, wxid, name=None, *, actor: str = "console") -> dict:
        """「是她，登记并关联」: always an operator's decision.  She is registered with her 抖音号 (``PrmService.register``
        with ``link``: the party, ``creator_links`` and the deal's event before her catch-up is asked for); a
        record whose 「她通过以后按这份邀约登记」 is off only keeps which chat she is.  The record ends ``linked``, its
        numbers cleared."""
        now = self._clock()
        with self._lock:
            record = self._record(add_id)
            if not self._linkable(record, now):
                raise FriendAddError(f"#{record['id']} 现在是「{STATES.get(record['state'], record['state'])}」，不能关联")
            wxid = str(wxid or "").strip()
            if not wxid:
                raise FriendAddError("要选是哪个聊天（她的 wxid）")
            party = None
            if record["link_on_accept"]:
                if self._register is None:
                    raise FriendAddError("达人副驾驶没有运行，现在不能登记")
                aweme = record["aweme_id"]
                invited = record.get("invited") or {}
                link = {"account": aweme, "nickname": record["nickname"], "add_id": record["id"], "source": "friend_add",
                        "invited_at": invited.get("invited_at", ""), "plan_id": invited.get("plan_id", "")}
                if record["creator_uid"] and record["source"].startswith("buyin"):
                    link.update(uid=record["creator_uid"], uid_source="buyin_im")
                note = f"百应邀约 · 抖音号 {aweme}" if aweme else "百应达人"
                party = self._register(wxid, _clip(name, NICKNAME_CHARS) or record["nickname"] or f"百应达人 {aweme}",
                                       note, link=link if aweme else None)
            deal = self._store.deal_of(party["id"], now) if party else None
            if not self._store.move_friend_add(
                    record["id"], (record["state"],), LINKED, now=now, wxid=wxid, linked_at=now, closed_at=now,
                    party_id=party["id"] if party else None, deal_id=deal["id"] if deal else None):
                raise FriendAddError("这条刚刚被处理了，刷新看看")
            self._store.clear_friend_add_values(record["id"], now=now)
        logger.info("FriendAdds: #%d linked by %s to %s… (%s)", record["id"], actor, wxid[:8],
                    f"party {party['id']}" if party else "not registered")
        return {"ok": True, "id": record["id"], "state": LINKED, "party_id": party["id"] if party else None,
                "catch_up": bool((party or {}).get("catch_up"))}

    def _linkable(self, record: dict, now: float) -> bool:
        if record["state"] == EXPIRED:
            return now - float(record["created_at"]) <= LINK_DAYS * DAY
        return record["state"] in OPEN and record["state"] not in (ADDING, BLOCKED)

    # ── the numbers' time (design §6.10) and the records' ─────────────────

    def sweep(self, now: Optional[float] = None, retention_days: int = 90, force: bool = False) -> int:
        """Clear the numbers that are due (14 days after a request she did not accept, 7 days untouched, 30 days at
        most; a record that cannot go on without them expires) and delete the records ended ``retention_days`` ago.
        At most once every SWEEP_SEC (``force``: now).  Returns how many records changed."""
        now = self._clock() if now is None else now
        if not force and now - self._last_sweep < SWEEP_SEC:
            return 0
        self._last_sweep = now
        changed = 0
        with self._lock:
            for record in self._store.friend_adds(states=OPEN):
                why = self._due(record, now)
                if not why:
                    continue
                if record["state"] in (WAITING, KNOWN) and why == "untouched_days":
                    changed += int(self._store.clear_friend_add_values(record["id"], now=now))
                    continue                          # (still waiting for her: only the numbers go)
                if self._store.move_friend_add(record["id"], (record["state"],), EXPIRED, now=now, reason=why,
                                               closed_at=now):
                    self._store.clear_friend_add_values(record["id"], now=now)
                    changed += 1
                    logger.info("FriendAdds: #%d expired (%s): its numbers cleared", record["id"], why)
            changed += self._store.delete_friend_adds(closed_before=now - max(1, int(retention_days)) * DAY)
        return changed

    @staticmethod
    def _due(record: dict, now: float) -> str:
        if record["state"] == ADDING:
            return ""
        if now - float(record["created_at"]) >= VALUES_MAX_DAYS * DAY:
            return "max_days"
        if record["state"] == REQUESTED and now - float(record.get("requested_at") or record["updated_at"]) >= REQUESTED_DAYS * DAY:
            return "requested_days"
        if record["state"] != REQUESTED and now - float(record["updated_at"]) >= UNTOUCHED_DAYS * DAY:
            if record["state"] in (WAITING, KNOWN) and not record["wechat"] and not record["phone"]:
                return ""
            return "untouched_days"
        return ""

    # ── the console, the admin ────────────────────────────────────────────

    def status(self, now: Optional[float] = None) -> dict:
        now = self._clock() if now is None else now
        last = self._last_at()
        busy = self._busy()
        return {"enabled": self._enabled, "can_add": self.can_add(), "today": self.today(now), "cap": self._daily_max,
                "gap_sec": self._gap_sec, "last_at": last,
                "next_at": last + self._gap_sec if last is not None and now - last < self._gap_sec else None,
                "paused_why": self._paused_why(), "busy_id": busy["id"] if busy else None,
                "blocked_why": self.blocked_why(now) if self._enabled else "", "greeting_max": GREETING_MAX,
                "default_greeting": self.greeting(""), "brand_set": bool(self._brand)}

    def admin_pending_text(self) -> str:
        """「待加达人」: the open records, at most ADMIN_LISTED, numbers masked."""
        if not self._enabled:
            return "加百应达人微信没打开（PRM_FRIEND_ADD）"
        rows = [r for r in self._store.friend_adds(states=OPEN)][:ADMIN_LISTED]
        if not rows:
            return "没有待加的百应达人"
        lines = ["待加的百应达人（号码打码；发「加达人 编号」加，「不加达人 编号」不加）："]
        for r in rows:
            shown = "、".join(f"{LABELS[k]} {v}" for k, v in (r.get("masked") or {}).items()) or "没有号码"
            when = f" {_clock_text(r['requested_at'])}" if r["state"] == REQUESTED and r.get("requested_at") else ""
            lines.append(f"#{r['id']} {r['nickname'] or '（没有昵称）'} {r['aweme_id'] or ''} {shown} {STATES[r['state']]}{when}")
        return "\n".join(lines)

    def admin_add(self, add_id) -> str:
        """「加达人 N」: the console's confirmation, the same checks; the answer after the window action."""
        if not self._enabled:
            return "加百应达人微信没打开（PRM_FRIEND_ADD）"
        try:
            out = self.confirm(add_id, by="admin", wait=True)
        except FriendAddError as e:
            return f"没有加：{e}"
        state, reason = out["state"], out.get("reason") or ""
        head = f"#{out['id']} "
        if state == REQUESTED:
            done = f"已向 {out['target']} 发好友申请（{head.strip()}" + (f"，附言：{out['greeting']}" if out["greeting"] else "") + "）"
            return f"{done}；今天已加 {self.today()}/{self._daily_max}"
        if state == ADDING:
            return f"{head}还在加（微信窗口还在操作），等一会儿发「待加达人」看结果"
        return f"{head}{STATES.get(state, state)}：{REASONS.get(reason, reason)}"

    def admin_drop(self, add_id) -> str:
        """「不加达人 N」."""
        if not self._enabled:
            return "加百应达人微信没打开（PRM_FRIEND_ADD）"
        try:
            self.act(add_id, "drop", actor="admin")
        except FriendAddError as e:
            return f"没有改：{e}"
        return f"#{int(add_id)} 记成不加了（号码已清除）"
