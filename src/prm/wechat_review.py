"""微信里审批草稿 (PRM_WECHAT_REVIEW, 2026-11-09, default off): the drafts that wait for a person in 「待审批」 are also
sent, once each, to the admin's private chat (ADMIN_WXID), so the admin can see them from the phone.

The user (2026-11-09): often away from the Windows PC, they ended up copying every draft out of the console into WeChat
by hand.

Notifications (``sweep``, on the PRM sender thread): every draft waiting for a person (a ``pending`` packet: a creator's
private chat, or a 老板拉的群 the bot took over) goes to the admin as one message (``notification_text``): its number
first (the packet id, the console's 「DRAFT #12」), the creator and where it goes (私聊 / 群里「群名」, with the console's
group warning), her last line(s) it answers (short; numbers as their placeholders, addresses cut to province and city,
a labelled 微信号 masked), the draft in full with its placeholders (their masked numbers after it, as the console's
legend), a shipping notice's waybill, its files, and how to answer.  None for what goes out by itself (a kind's gate
``auto``, 下班时段 and 自动模式 approve it at once: never pending; 开场白自动发: ``meta.auto_at`` while
``send_due_openings`` still takes it), a 百应 draft (``BUYIN_TOOL``: copied into 百应 by hand), a holding reply (no
packet), or anything while ADMIN_WXID is empty (``status`` says why).  A draft with a platform action is told too, with
its action, but only the console runs one: the notification sends the admin to its 「批准并执行」.  PER_ROUND sends a
round at most; a send WeChat does not take ends the round and the admin's chat is tried again 10 s later, doubling, at
most 2 minutes apart, each message 6 times in all (as the relay's approval requests, src/relay/service.py
``_send_asks``); no draft ever waits for its notification.  One row per draft told (``wechat_reviews`` in prm.db): a
draft told is never told again, after a restart too, and one not told yet is told then.

Nothing of it while the switch is off (no notification, no row), nor on the Mac's edge side (WECHAT_BACKEND=edge: the
admin's chat is read on Windows there).  The log has packet ids and counts only: never a creator's name or words, never
a draft.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, Optional

from . import contact, pii, platforms, retention
from .boss_groups import CHANNEL as GROUP_CHANNEL
from .store import MESSAGE_TOOL, PENDING, PrmStore

logger = logging.getLogger(__name__)

RETRY_SEC = (10, 120)       # a send the admin's chat did not take: the chat again after 10 s, doubling, at most 2 min apart
SEND_TRIES = 6              # ... and a message not again (this run) after this many failed sends (about 4.5 minutes)
PER_ROUND = 3               # sends one round of the sender thread makes at most (a pile waits its turn)
SAID_LINES = 2              # her last lines a notification quotes
SAID_CHARS = 60             # ... each cut to this many characters
NOTE_CHARS = 120            # the draft's reason (when it asks for attention), an error told
FILES_NAMED = 5             # file names a notification lists at most

_STAMP = re.compile(r"^\[\d{2}-\d{2} \d{2}:\d{2}\]\s*")

NO_ADMIN_WHY = ("微信里审批草稿打开了，但没填管理员 wxid（ADMIN_WXID）：草稿发不到微信里。到「中转代理」页的设置里填好"
                "「管理员 wxid」，再重启机器人")
NO_SEND_WHY = "机器人没有连着微信：草稿发不到管理员微信里"
EDGE_WHY = "Mac 端（WECHAT_BACKEND=edge）不能在微信里审批草稿：管理员的私聊在 Windows 上读"


def _cut(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


class WechatReview:
    """微信里审批草稿 for one PRM service (``PrmService.wechat_review``); see the module's docstring.

    ``lines(packet)`` her lines a draft answers (the job's); ``parts(text)`` how many messages a text goes out as
    (PRM_REPLY_PARTS); ``opening_auto``: PRM_OPENING_AUTO_MIN is on (an opening with its time set goes by itself)."""

    def __init__(self, store: PrmStore, config, *, clock: Callable[[], float] = time.time,
                 send: Optional[Callable[[str, str], bool]] = None, lines: Optional[Callable[[dict], str]] = None,
                 parts: Optional[Callable[[str], int]] = None, opening_auto: bool = False):
        self._store = store
        self._clock = clock
        self._send = send
        self._lines = lines
        self._parts = parts
        self._opening_auto = bool(opening_auto)
        self.admin = str(getattr(config, "admin_wxid", "") or "").strip()
        self._edge = str(getattr(config, "wechat_backend", "") or "") == "edge"
        self._switch = str(getattr(config, "prm_wechat_review", "off") or "off").strip().lower() == "on"
        self.on = self._switch and not self._edge     # (the sweep does nothing otherwise)
        self._failures: dict[tuple, int] = {}       # ("draft", packet id) -> its sends WeChat did not take
        self._chat_failures = 0                       # sends to the admin's chat not taken in a row (RETRY_SEC)
        self._wait_until = 0.0                        # ... and when the chat is tried again

    # ── the console ───────────────────────────────────────────────────────

    def status(self) -> dict:
        """What 「待审批」 says (GET /api/prm ``wechat_review``): ``on`` (the switch), ``why`` it cannot work ("": it does),
        how many told drafts still wait (``open``) and how many could not be told this run (``unsent``)."""
        if not self._switch:
            return {"on": False}
        why = EDGE_WHY if self._edge else NO_ADMIN_WHY if not self.admin else NO_SEND_WHY if self._send is None else ""
        unsent = sum(1 for (what, _), failures in list(self._failures.items())
                     if what == "draft" and failures >= SEND_TRIES)
        return {"on": True, "why": why, "open": 0 if why else len(self._store.wechat_reviews_open()),
                "unsent": 0 if why else unsent}

    # ── notifications (the sender thread) ─────────────────────────────────

    @property
    def ready(self) -> bool:
        return self.on and bool(self.admin) and self._send is not None

    def sweep(self) -> int:
        """One round of the sender thread: the drafts waiting for a person not told yet (oldest first), PER_ROUND sends
        at most.  A send WeChat does not take ends the round, and the admin's chat waits before the next one (RETRY_SEC:
        10 s, doubling, at most 2 minutes apart); a message not taken SEND_TRIES times is not tried again this run
        (logged; the console still has the draft).  Returns how many messages went to the admin."""
        if not self.ready or self._clock() < self._wait_until:
            return 0
        sent = tried = 0
        for packet in self._store.wechat_review_due():
            key = ("draft", packet["id"])
            if self._goes_by_itself(packet) or self._failures.get(key, 0) >= SEND_TRIES:
                continue
            if tried >= PER_ROUND:
                return sent
            try:
                text = self.notification_text(packet)
            except Exception:
                logger.exception("PRM: the notification of draft %d could not be written; not told", packet["id"])
                self._failures[key] = SEND_TRIES
                continue
            current = self._store.packet(packet["id"])
            if current is None or current["status"] != PENDING:
                continue                                   # decided (or out of date) meanwhile
            tried += 1
            if not self._try(key, text, f"the notification of draft {packet['id']}"):
                return sent
            self._store.add_wechat_review(packet["id"], packet["deal_id"], self._clock())
            logger.info("PRM: draft %d told to the admin's chat (微信里审批)", packet["id"])
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
            lines.append(f"（引用这条回；不引用就回「发 {number}」「不发 {number}」「改 {number} 你的话」）")
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
