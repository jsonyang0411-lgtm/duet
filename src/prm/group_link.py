"""接手任何合作群 (2026-11-09; the user: 「现在好麻烦…到群里后我一句句复制来和你探讨…然后控制台什么都识别不到」).

PRM drafts in a group by itself only when it is a 老板拉的群 the scan took over (PRM_BOSS_GROUPS, src/prm/boss_groups.py);
a deal's own 三方群 is read only (src/prm/three_party.py).  Every other cooperation group (one a creator's own 商务 made
with us, one an operator made by hand, one from before the scan's baseline) the console knew nothing of.  Here an
operator links any group to a creator with one command in the admin's one-to-one chat (ADMIN_WXID, typed text: src/admin.py
hands the text over, ``admin_command``):

- 「接群」: the groups active last that are not linked yet (the backend's ``recent_groups``, at most LIST_MAX), numbered,
  each with its name, how many are in it and when it was active last;
- 「接群 3」 (a number of the last list) or 「接群 <群名>」 (its name in WeChat, spaces and case aside; several of that name:
  listed and numbered; none: said so): who in it may be her, numbered (BossGroups ``candidates_of``: everybody but us, the
  admin, the cc users and the bosses of PRM_GROUP_WITH; the registered creators first, marked 已登记; one WeChat gives no
  name for as 「（没有名字）…<her wxid's last 4>」, never her whole wxid), 「回「是 2」；「不接」
  作罢」 (BossGroups ``offer``: the open question, which the console's card shows too); a deal's own 三方群: she is known
  from the deal, so only 「是 1」 to confirm; a group bigger than PRM_BOSS_GROUP_MAX may be linked all the same (an
  operator's explicit act), the question says how many are in it;
- 「是 N」: BossGroups ``link`` (she is registered unless she is a creator already, the group linked to her deal, what
  was said there lately its context, one draft when she waits for an answer, the watch), answered in one line;
- 「不接」: the question is dropped (it expires by itself OFFER_SEC after it was asked); with PRM_GROUPS_AUTO_LINK on
  the group is remembered as refused (BossGroups ``decline``: the scan never links it by itself nor tells about it);
- 「不接群 <群名>」: a linked group is not drafted in any more (PrmService.unlink_group, BossGroups ``stop``: its drafts not
  sent fail; her deal's 三方群 stays it, read only again; the console's 「不接了」 is the same).

A group the scan found that waits for 「是她」 on the console's 「老板拉的群」 card (PRM_BOSS_GROUPS on, the card's buttons on:
no ``why_not``) is not asked about here: the answer says to choose there (review 2026-11-09: two questions about one
group).  「是 N」 counts only the members
the question listed (MEMBERS_LISTED); more are chosen on the console's card.

群自己接上 (PRM_GROUPS_AUTO_LINK, 2026-11-11, BossGroups ``auto_scan``): the scan links a group by itself when exactly
one registered creator is in it and tells the admin's chat (「已自动接群：…不对就回「不接群 <群名>」」); a group it does not
link (two or more creators, or none in a group that is new or active) is told there once with how to link it here
(「接群 <群名>」): the commands above are the same.

「是 N」 and 「不接」 are commands only while a question is open or was lately (ASKED_KEEP_SEC: then they say it expired), so
the words stay free otherwise (with CLAUDE_RELAY_ONLY the admin's other lines go to the Mac).  A group linked by hand
works whenever PRM_COPILOT is on, whatever PRM_BOSS_GROUPS says.  The answers go to the admin's own chat and name the
group and her; the log has ids' first characters and counts only.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, Optional

from .boss_groups import LINKED, OFFER_SEC, PENDING

logger = logging.getLogger(__name__)

LIST_MAX = 10                  # 「接群」 lists at most this many groups
ASKED_KEEP_SEC = 7200          # 「是 N」 / 「不接」 after a question expired still say so, this long after it was asked
MEMBERS_LISTED = 30            # a question lists at most this many members in WeChat (the console's card lists all)
NO_NAME = "（没有群名）"
MEMBER_NO_NAME = "（没有名字）"     # a member WeChat gives no name for (``_nameless``: her wxid's last 4 after it)
NO_BACKEND = "这台电脑的微信读法列不出群（要 Windows 版微信的数据库直读），接不了群"
NO_READ = "微信数据库现在读不了，稍后再发「接群」"
_LINK = re.compile(r"^接群(?:\s*(.+))?$")
_STOP = re.compile(r"^不接群(?:\s*(.+))?$")
_CONFIRM = re.compile(r"^确认接群(?:\s*(.+))?$")    # 群自己接上 (review 2026-11-11 #2): a group the scan linked is hers
_YES = re.compile(r"^是\s*(\d{1,3})$")
CANCEL = "不接"


def _key(name) -> str:
    """A group's name as WeChat's search compares it (src/wechat/wcdb_backend.py ``_name_key``): no spaces, no case."""
    return "".join(str(name or "").split()).casefold()


def _nameless(wxid) -> str:
    """A member WeChat gives no name for, as the admin's chat lists it: never her whole wxid (integrated review
    2026-11-10 B-L7), 「（没有名字）…<last 4>」 (the console's card has the rest)."""
    wxid = str(wxid or "").strip()
    return MEMBER_NO_NAME + (wxid if len(wxid) <= 4 else "…" + wxid[-4:])


def _clock_text(at) -> str:
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(at)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


class GroupLinks:
    """The admin's 「接群」 commands of one PRM service (see the module's doc).  ``service``: the PrmService (its store,
    ``boss_groups``, ``three_party`` and ``deal_action``)."""

    def __init__(self, service, *, clock: Callable[[], float] = time.time):
        self._service = service
        self._clock = clock
        self._lock = threading.Lock()              # one command at a time (each may read WeChat's database)
        self._listed: Optional[dict] = None        # the last list of groups: {"at", "groups"} (「接群 N」 picks from it)
        self._asked: Optional[dict] = None         # the last question asked: {"at", "chat", "name"} (its offer: BossGroups')

    # ── the admin's text (src/admin.py ``_command``) ─────────────────────

    def admin_command(self, text) -> Optional[Callable[[], str]]:
        """The command ``text`` is (``run()`` answers it), or None: 「接群」 / 「接群 N」 / 「接群 群名」 / 「不接群 群名」
        always, 「是 N」 / 「不接」 only while a question is open or was lately (``ASKED_KEEP_SEC``)."""
        content = " ".join(str(text or "").replace("　", " ").split())
        match = _CONFIRM.fullmatch(content)
        if match:
            name = (match.group(1) or "").strip()
            return lambda: self._run(self.admin_confirm, name)
        match = _STOP.fullmatch(content)
        if match:
            name = (match.group(1) or "").strip()
            return lambda: self._run(self.admin_stop, name)
        match = _LINK.fullmatch(content)
        if match:
            arg = (match.group(1) or "").strip()
            return (lambda: self._run(self.admin_ask, arg)) if arg else (lambda: self._run(self.admin_list))
        asked = self._asked
        if asked is None or self._clock() - float(asked["at"]) > ASKED_KEEP_SEC:
            return None
        match = _YES.fullmatch(content)
        if match:
            number = int(match.group(1))
            return lambda: self._run(self.admin_yes, number)
        if content == CANCEL:
            return lambda: self._run(self.admin_cancel)
        return None

    def _run(self, fn: Callable[..., str], *args) -> str:
        try:
            with self._lock:
                return fn(*args)
        except Exception:
            logger.exception("PRM: 接群: the admin's command failed")
            return "接群的时候出错了（详见日志）"

    # ── the commands ─────────────────────────────────────────────────────

    def admin_list(self) -> str:
        """「接群」: the groups active last that are not linked yet, numbered (``_listed``: 「接群 N」 picks from it)."""
        read = self._reader()
        if read is None:
            return NO_BACKEND
        groups = read(LIST_MAX, skip=self._service.boss_groups.linked_chats())
        if groups is None:
            return NO_READ
        if not groups:
            return "最近没有还没接手的群"
        self._listed = {"at": self._clock(), "groups": list(groups)}
        logger.info("PRM: 接群: %d group(s) listed for the admin", len(groups))
        return "\n".join(["最近有动静、还没接手的群（回「接群 编号」接手；也可以「接群 群名」）：",
                          *(f"{n}. {self._describe(g)}" for n, g in enumerate(groups, 1))])

    def admin_ask(self, arg: str) -> str:
        """「接群 N」 (a number of the last list, OFFER_SEC long) or 「接群 群名」: who in that group she may be."""
        bg = self._service.boss_groups
        now = self._clock()
        listed = self._listed if self._listed and now - float(self._listed["at"]) <= OFFER_SEC else None
        if arg.isdigit() and listed is not None:
            n, groups = int(arg), listed["groups"]
            if not 1 <= n <= len(groups):
                return f"没有第 {n} 个：回「接群 1」到「接群 {len(groups)}」，或者重新发「接群」"
            return self._ask(groups[n - 1])
        read = self._reader()
        if read is None:
            return NO_BACKEND
        found = read(0, name=arg)
        if found is None:
            return NO_READ
        free = [g for g in found if not bg.is_linked(g["chat"])]
        if not free and found:
            name = found[0].get("name") or arg
            return f"群「{name}」已经接手了：她在群里说的话会起草；不接了发「不接群 {name}」"
        if not free:
            if arg.isdigit():
                return "先发「接群」看最近的群，再回「接群 编号」"
            return f"没找到叫「{arg}」的群（要和微信里的群名一样，不管空格和大小写）：发「接群」看最近的群"
        if len(free) > 1:
            self._listed = {"at": now, "groups": free}
            return "\n".join([f"有 {len(free)} 个群叫「{arg}」：回「接群 编号」选一个",
                              *(f"{n}. {self._describe(g)}" for n, g in enumerate(free, 1))])
        return self._ask(free[0])

    def _ask(self, group: dict) -> str:
        """The question for one group: who she is (BossGroups ``offer``); a deal's own 三方群: only her, to confirm."""
        service = self._service
        bg, chat = service.boss_groups, str(group["chat"])
        name = str(group.get("name") or "")
        shown = name or NO_NAME
        if bg.is_linked(chat):
            return f"群「{shown}」已经接手了：她在群里说的话会起草"
        if self._on_card(chat):
            return f"群「{shown}」是老板拉的群，在控制台「合作单」页顶上的「老板拉的群」卡片里等你选她：到那里点「是她」"
        found = bg.candidates_of(chat)
        deal = service.three_party.deal_of_chat(chat)
        party = service.store.party(int(deal["party_id"])) if deal is not None else None
        if party is not None:
            her = {"wxid": party["wxid"], "name": party["name"], "registered": bool(party["active"]),
                   "stopped": not party["active"]}
            found = {**(found or {"all": [{"wxid": party["wxid"], "name": party["name"]}], "boss": []}), "members": [her]}
            size = group.get("size") or len(found["all"]) + 1
            bg.offer(chat, name, size, found, deal_id=int(deal["id"]))
            self._asked = {"at": self._clock(), "chat": chat, "name": shown}
            return (f"群「{shown}」是 {party['name']} 的三方群（合作单 #{deal['id']}，「拉三方群」建的，现在只读）：接手以后她在群里"
                    f"说的话会起草，回复发在群里，群里的人都看得到。回「是 1」接手，「不接」作罢（{OFFER_SEC // 60} 分钟内有效）")
        if found is None:
            return f"群「{shown}」的成员现在读不出来：稍后再试"
        members = found["members"]
        if not members:
            return f"群「{shown}」里除了我们自己、老板、管理员和能用 cc 的人，没有别人：没有谁能当达人"
        size = group.get("size") or len(found["all"]) + 1
        bg.offer(chat, name, size, found)
        self._asked = {"at": self._clock(), "chat": chat, "name": shown}
        lines = [f"群「{shown}」（{size} 人）里谁是达人？回「是 编号」，「不接」作罢（{OFFER_SEC // 60} 分钟内有效）："]
        for n, m in enumerate(members[:MEMBERS_LISTED], 1):
            mark = "（已登记）" if m.get("registered") else "（停用过）" if m.get("stopped") else ""
            lines.append(f"{n}. {m.get('name') or _nameless(m['wxid'])}{mark}")
        if len(members) > MEMBERS_LISTED:
            lines.append(f"人太多，只列了前 {MEMBERS_LISTED} 位：别的到控制台「合作单」页顶上的卡片里选")
        if isinstance(size, int) and size > bg.max_members:
            lines.append(f"这个群有 {size} 人，比 PRM_BOSS_GROUP_MAX（{bg.max_members}）多，也能接：只起草你选的那个人的话，"
                         "别人的话只当背景")
        if not name:
            lines.append("群还没有名字：接手以后在微信里给群起个名字，机器人才能把回复发进去")
        return "\n".join(lines)

    def admin_yes(self, number: int) -> str:
        """「是 N」: the open question's N-th member is her (BossGroups ``link``); one line of what happened."""
        asked = self._asked
        if asked is None:
            return "现在没有在等你回答的「接群」"
        bg = self._service.boss_groups
        offer = bg.offered(asked["chat"])
        if offer is None:
            return self._not_open(asked)
        members = offer["members"][:MEMBERS_LISTED]           # (only the ones the question listed: review 2026-11-09)
        if not 1 <= number <= len(members):
            more = (f"；只列了前 {MEMBERS_LISTED} 位，别的到控制台「合作单」页顶上的卡片里选"
                    if len(offer["members"]) > MEMBERS_LISTED else "")
            return f"没有第 {number} 位：回「是 1」到「是 {len(members)}」，「不接」作罢{more}"
        out = bg.link(asked["chat"], members[number - 1]["wxid"], actor="admin")
        if not out.get("ok"):
            return f"没接手：{out.get('error') or '出错了'}"
        self._asked = None
        return self.linked_text(offer, out)

    def admin_cancel(self) -> str:
        """「不接」: the open question is dropped."""
        asked = self._asked
        if asked is None:
            return "现在没有在等你回答的「接群」"
        if self._service.boss_groups.offered(asked["chat"]) is None:
            return self._not_open(asked)
        # (``_asked`` stays: a 「是 N」 after it says so; 群自己接上: the scan leaves a group a person refused alone; review
        # 2026-11-11 #6: only the group the admin's own question is about, never one the scan asked about)
        self._service.boss_groups.decline(asked["chat"])
        logger.info("PRM: group %s…: the question who she is was dropped (the admin)", asked["chat"][:8])
        again = f"「接群 {asked['name']}」" if asked["name"] != NO_NAME else "「接群」"
        return f"好的，群「{asked['name']}」不接了，以后也不自动接（要接再发{again}）"

    def admin_confirm(self, name: str) -> str:
        """「确认接群 群名」 (群自己接上, review 2026-11-11 #2): a group the scan linked by itself is hers (BossGroups
        ``confirm``): its drafts no longer all wait for that reason.  No name: the one such group nobody confirmed."""
        bg, store = self._service.boss_groups, self._service.store
        rows = [r for r in store.boss_groups((LINKED,)) if bg.unconfirmed(r["chat"])]
        if name:
            named = [r for r in store.boss_groups((LINKED,)) if bg.is_linked(r["chat"]) and _key(r["name"]) == _key(name)
                     and r.get("manual") == 2]
            if not named:
                return f"没有机器人自动接的、叫「{name}」的群"
            if len(named) > 1:
                return f"有 {len(named)} 个自动接的群叫「{name}」：到控制台她的合作单上点「是她的群」"
            rows = named
        elif len(rows) != 1:
            return ("现在没有等你确认的自动接的群" if not rows else
                    f"有 {len(rows)} 个自动接的群在等你确认：回「确认接群 群名」，或者到控制台她的合作单上点「是她的群」")
        row = rows[0]
        out = bg.confirm(row["chat"], actor="admin")
        if not out.get("ok"):
            return f"没确认：{out.get('error') or '出错了'}"
        party = store.party(int(row.get("party_id") or 0)) if row.get("party_id") else None
        who = party["name"] if party else "她"
        if out.get("already"):
            return f"群「{row['name']}」早就确认过是 {who} 的群了"
        how = ("之后的草稿和老板拉的群一样照门槛、下班时段、自动模式发" if getattr(self._service, "_linked_groups_auto", False)
               else "之后的草稿照旧在「待审批」里等你看（「手动接的群也自动发」关着）")
        return f"好的：群「{row['name']}」确认是 {who} 的群了，{how}"

    def admin_stop(self, name: str) -> str:
        """「不接群 群名」: a linked group of that name is not drafted in any more (PrmService.unlink_group); the answer says
        what became of it: her 三方群 still, read only; or a group like any other (never answered); and the drafts for it
        not sent."""
        if not name:
            return "要写群名：「不接群 群名」"
        service = self._service
        bg, store = service.boss_groups, service.store
        rows = [r for r in store.boss_groups((LINKED,)) if bg.is_linked(r["chat"]) and _key(r["name"]) == _key(name)]
        if not rows:
            return self._refuse(name)
        if len(rows) > 1:
            return f"有 {len(rows)} 个接手的群叫「{name}」：到控制台她的合作单上点「不接了」"
        row = rows[0]
        party = store.party(int(row.get("party_id") or 0)) if row.get("party_id") else None
        out = service.unlink_group(row.get("deal_id"), row["chat"], actor="admin")
        if not out.get("ok"):
            return f"没停：{out.get('error') or '出错了'}"
        who = f"{party['name']}的" if party else "她的"
        unsent = f"；还没发的 {out['unsent']} 份群里的草稿不发了" if out.get("unsent") else ""
        if out.get("sending"):                         # (``_deliver`` stops before its next message, review 2026-11-09)
            unsent += f"；正在往群里发的 {out['sending']} 份不再往下发（已经发出去的留在群里）"
        if out.get("three_party"):
            reading = ("机器人只读、不在群里说话，群里的话照旧交给她私聊的草稿" if out.get("reading") else
                       "这台电脑读不了群，群里的话交不到她私聊的草稿")
            return (f"不接了：群「{row['name']}」里她的话不再起草；它还是{who}三方群，{reading}"
                    f"（合作单和私聊照旧）{unsent}")
        return (f"不接了：群「{row['name']}」里她的话不再起草，群里的话和别的群一样不回（{who}合作单和私聊照旧）"
                f"{unsent}")

    def _refuse(self, name: str) -> str:
        """「不接群 群名」 of a group that is not linked (review 2026-11-11 #6): the scan that links groups by itself is
        never to link it, ask or tell about it (BossGroups ``refuse``; the admin's own 「接群」 still links it)."""
        read = self._reader()
        if read is None:
            return f"没有接手叫「{name}」的群"
        found = read(0, name=name)
        if found is None:
            return NO_READ
        free = [g for g in found if not self._service.boss_groups.is_linked(g["chat"])]
        if not free:
            return f"没有叫「{name}」的群（要和微信里的群名一样，不管空格和大小写）"
        if len(free) > 1:
            return f"有 {len(free)} 个群叫「{name}」：到控制台「合作单」页顶上的卡片里点「不接」，或者先给群改个不一样的名字"
        group = free[0]
        self._service.boss_groups.refuse(group["chat"])
        if self._asked is not None and self._asked.get("chat") == group["chat"]:
            self._service.boss_groups.withdraw(group["chat"])
        shown = group.get("name") or name
        logger.info("PRM: group %s…: the admin says it is not to be linked by itself", str(group["chat"])[:8])
        return f"已记下：「{shown}」以后不自动接；要接再发「接群 {shown}」"

    # ── helpers ──────────────────────────────────────────────────────────

    def _reader(self) -> Optional[Callable]:
        read = getattr(self._service.boss_groups.backend, "recent_groups", None)
        return read if callable(read) else None

    def _describe(self, group: dict) -> str:
        """One group of a list: its name, how many are in it, when it was active last; a deal's own 三方群 says whose, a
        group waiting on the 「老板拉的群」 card says so."""
        size = group.get("size")
        bits = [str(group.get("name") or NO_NAME), f"{size} 人" if size else "人数读不出", _clock_text(group.get("last_at"))]
        deal = self._service.three_party.deal_of_chat(str(group["chat"]))
        party = self._service.store.party(int(deal["party_id"])) if deal is not None else None
        if party is not None:
            bits.append(f"{party['name']} 的三方群")
        if self._on_card(str(group["chat"])):
            bits.append("老板拉的群，在「老板拉的群」卡片里选")
        return " · ".join(b for b in bits if b)

    def _on_card(self, chat: str) -> bool:
        """Is this a group the scan found that waits for 「是她」 on the console's 「老板拉的群」 card (PRM_BOSS_GROUPS on, and
        the card's buttons work: no ``why_not``, review 2026-11-09: else it could be linked nowhere)?"""
        bg = self._service.boss_groups
        if not bg.on or bg.why_not():
            return False
        row = self._service.store.boss_group(chat)
        return row is not None and row["state"] == PENDING

    def _not_open(self, asked: dict) -> str:
        """「是 N」 / 「不接」 when the question is not open any more: linked meanwhile, expired, or dropped."""
        if self._service.boss_groups.is_linked(asked["chat"]):
            self._asked = None
            return f"群「{asked['name']}」已经接手了"
        if self._clock() - float(asked["at"]) > OFFER_SEC:
            return f"刚才那个「接群」已经过了 {OFFER_SEC // 60} 分钟，作废了：要接的话重新发「接群」"
        return f"群「{asked['name']}」这次不接了（已经作罢）：要接的话重新发「接群」"

    def linked_text(self, offer: dict, out: dict) -> str:
        """The answer to 「是 N」 in one line: whom the group went to, the catch-up, what to look at (also the body of
        the scan's own 「已自动接群」 line, PrmService._auto_link_notice; with PRM_GROUP_HER_SIDE on, in a group it counts
        in, it says her side's lines are drafted too).  How her drafts
        there go is PRM_LINKED_GROUPS_AUTO's (final check 2026-11-10 A5): off, every one waits in 「待审批」; on, as a
        老板拉的群's (a kind's gate, the 下班时段, 自动模式), but the draft of the lines that waited at 接群, which waits."""
        shown = offer.get("name") or NO_NAME
        head = (f"接手了：群「{shown}」→ {out['name']}"
                + ("（新登记的达人）" if out.get("registered") else "（已登记）") + f"，合作单 #{out['deal_id']}")
        bg = self._service.boss_groups
        size = offer.get("size")
        # (她那边的人, PRM_GROUP_HER_SIDE on, in a group of at most PRM_BOSS_GROUP_MAX: her side's lines are drafted too)
        side = bool(getattr(bg, "her_side_on", False)) and not (isinstance(size, int) and size > bg.max_members)
        who = "她和她那边的人（助理、商务）" if side else "她"
        if getattr(self._service, "_linked_groups_auto", False):
            bits = [f"之后{who}在群里说的话会起草，和老板拉的群一样照门槛、下班时段、自动模式发（「手动接的群也自动发」开着）；"
                    "接群这一刻她在等的那几句的草稿在「待审批」里等你看"]
        else:
            bits = [f"之后{who}在群里说的话会起草，在「待审批」里等你看"]
        caught = out.get("caught") or {}
        if not caught.get("read"):
            bits.append("群里之前的话这次读不出来：从现在起她说的话照常起草")
        else:
            if caught.get("context"):
                bits.append(f"群里最近 {caught['context']} 句记成了背景")
            if caught.get("waiting") and caught.get("job"):
                bits.append(f"她最后 {caught['waiting']} 句还没人回，派了一份草稿")
            elif caught.get("waiting") and caught.get("held"):
                bits.append(f"她最后 {caught['waiting']} 句还没人回：现在人工接管或暂停，只记下了，没起草")
            elif caught.get("waiting"):
                bits.append(f"她最后 {caught['waiting']} 句还没人回，没起草（太久以前的话）")
            else:
                bits.append("她最近没有在等我们回：等她在群里说话再起草")
        if out.get("kept"):
            bits.append("她原来的三方群保留")
        if not offer.get("name"):
            bits.append("群还没有名字：在微信里给群起个名字以后机器人才能发进去")
        return head + "；" + "；".join(bits)
