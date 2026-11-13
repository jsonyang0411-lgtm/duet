"""PRM copilot (``PRM_COPILOT=on``): a registered creator's private messages get drafted
replies on the Mac, and an operator decides in the console what is sent.

The flow, all of it silent towards the creator::

    creator writes ──► route step ``prm`` ──► ``handle``: phone/ID numbers become
    placeholders, the text (and any picture or file) goes into a ``prm`` job of the
    Claude relay (src/relay; a burst of messages is one job) ──► the Mac worker drafts
    in its locked-down workspace ──► ``on_result``: the JSON is checked
    (result.py) and becomes a packet ──► the console: send / edit and send / drop, or
    the kind's gate is ``auto`` (8.1, gates.py) ──► the sender thread puts the numbers
    back and sends through the WeChat backend (text first, then the material files).

A person takes over a chat (``mode`` = human for ``PRM_TAKEOVER_MIN``) when the
operators reply from this WeChat account themselves (the backend hands those lines to
``on_own_message``; the bot's own lines are recognised and confirm their packet
instead), or from the console; paused needs the console to end it.  Meanwhile the
creator's messages are only recorded; the next draft job is told what both sides
said (``history``).  A draft left waiting ``PRM_HOLDING_MIN`` minutes gets one
holding reply per round of the creator's messages (none when she was answered in that round
already: an operator's own line, or a reply sent after her message).  ``PRM_RETENTION_DAYS`` after a
deal is done (delivered, or quiet that long) its receiving details are cleared everywhere (8.2, retention.py),
the Mac's case file through a ``purge`` job.  The deal actions (stage, mode,
shipment) are in deals.py.

下班时段 (``PRM_NIGHT_AUTO_HOURS``, 2026-10-29; empty = off, everything as before): the operators go home at 18:00
and evenings are when creators answer most.  Inside the window (this PC's local time, its clock right: ``_clock_off``)
a reply draft that waits for a person at the gates' step still goes out by itself (route ``night``) when every night
rule holds (``_night_judge``, gates.py ``night_reason``); everything ``on_result`` sends to a person before that step
stays as by day.  Its job was told it is night (``meta.night``, made in ``_submit``); a flagged draft goes only as
one safe holding line and leaves the deal a notice until 已处理 (``_night_sent``, ``night_done``); a night send
never counts toward a kind's gate; holding replies inside the window say PRM_NIGHT_HOLDING_TEXT.

自动模式 (2026-10-30; off = everything as before): the console's one-press switch (待审批, ``POST /api/prm/auto``) puts
the night window's rules in force at any hour, until it is switched off, with no window set too (``set_auto_mode``;
kept in the store over a restart, ``auto_mode_of``).  Inside the window it is the night's, words and all; outside it
(by day: ``_auto_by_day``) the job says so (``meta.night`` ``{"auto": true, "at"}``: the Mac promises no 「明早」; a
draft of a job made in the window waits by day), the holding reply says AUTO_HOLDING_TEXT, and what it sent or held
says 自动模式 (the packet's ``meta.auto_mode``).  One judgement reads the switch once (``auto_on``).

What the deal says to a person (``attention``) is its notices (store.py): the latest draft's own
notes, which the next draft replaces, and the system's, which stay until they are dealt with (mac
review F1): a draft job that gave no draft (her lines in it go with her next draft job: answered,
not lost), a 专属佣金 step owed while a person had the chat or the creator was off the list (asked for
when that ends, ``sweep_owed_steps``), a platform action that ended red (until 已处理), her recall,
messages read too late to draft, this PC's clock off WeChat's (measured by the bot's own lines read back:
``_note_clock``; how late a message is read is reckoned by WeChat's clock then, win review F6).

Registering is an operator's decision, unless PRM_AUTO_REGISTER is on (2026-10-29): then every new friend
the new-friend scan finds is registered by the bot as a full creator (``new_friend_found`` queues her inside
the scan's look, kept in prm_meta over a restart; the sender thread registers her at the start of its next
round, ``register_found``, outside the scan's locks: ``register`` calls back into the scan through
``on_registered``), never the admin or a cc
user, never a wxid that has a party, active or stopped (an operator's 停用 sticks).  When the creator
registered is a new friend
(src/wechat/new_friends.py found the line WeChat writes when two people become friends), her
messages since she was added, friend-request greeting included, become one draft job that waits
for a person (``take_new_friend``).  WeChat's friend notices (「我通过了你的朋友验证请求…」 in her
name, 「你已添加了…」) are never drafted, nor are its other lines in her chat (type 10000: a recall,
拍一拍, a red packet notice; ``_system_line``): her recall puts the drafts not sent yet before a person.

A creator who has not written to us yet gets one draft of our first line (开场白, 2026-10-30), always
reviewed: a new friend whose catch-up found nothing of hers (she accepted our friend request, and
WeChat's notice is all her chat has), or an operator's 「写开场白」 in the console (``ask_opening``).  Its
job has no creator message and says so (``meta.opening``; the worker's ``opening_line``); her message
outdates it like any reply draft (the next job hears it was not sent), and no holding reply goes for it.
「补发货盘」 in the console (2026-11-04, ``ask_huopan``) is the same for a creator we did talk to: one job with no line of
hers that sends her the kb's 货盘 again (``meta.huopan``; the worker's ``huopan_line``), always reviewed.
直播手卡 (PRM_HANDCARDS, 2026-11-09, src/prm/handcards.py): every WeChat draft job says the Mac may make cards
(``meta.handcards_ok``); a draft that comes back with the Mac's cards (``Draft.handcards``) always waits for a person,
all of its cards or none of them (``handcards.keep_cards``: nor any card-like file of a failed set; the same-picture
filter leaves the cards alone), one whose cards were not made cannot go out as it is, and cards whose 双11 line is past
its end date by Beijing time cannot go at all (``decide``, ``cards_expired``).  「做手卡」 in the console
(``ask_handcards``) is one job like 补发货盘's (``meta.handcards``: the products of her latest sample order,
``sample_items``, or the ticked ones).
签收后问一句 (PRM_SIGNED_HELLO, 2026-11-07, default off): a sample order the 聚水潭 sync sees signed for, she has not
said she got it: one job with no line of hers asks her how the samples suit her (``meta.signed``; the worker's
``signed_line``, kb 场景 D2), inside PRM_SIGNED_HELLO_HOURS only, once per order; by day it always waits for a person
too (whatever its kind's gate), but in the night window or 自动模式 the night's rules may send it, like a reply draft.

An operator may tell the Mac how to write to a creator (起草备注, 2026-10-31: her WeChat signature said
「直接发链接和brief，感兴趣的会回复！勿催视频，不接语音」): the deal action ``draft_note`` keeps it on the deal
(``set_draft_note``; only an operator writes it, unlike her party ``note``), and every draft job of the deal carries it
(``meta.note``, masked like her words: ``_with_note``): her messages', the 开场白's, a 专属佣金 step's, her 百应 drafts'.
The worker puts it in <operator_note>; a deal without one gets the jobs it got before.  The clean-up cuts it like the
deal's other words (``purge``).

A draft with a platform action (platforms.py: 小红书 / 抖音 / 淘宝 CLIs on the Mac) comes
with the Mac's preview of it and always waits for a person, whatever its kind's gate; so does
every draft of a next step the bot asked for (a 专属佣金 step, ``meta.system_note``).  A verified
sample order's products get their 专属佣金 one at a time, the bot keeping the record (``_chain``:
which are open, which a person dropped, which ended red, which are left); a product that cannot be
opened does not hold up the ones after it, and each is asked for once per round (a signature).
批准并执行 moves it to ``executing`` and queues a ``platform_execute`` job (one approval
key; the Mac runs the CLI with --execute once and never again for that key); only a
read-back ``VERIFIED_SUCCESS`` turns it into an approved reply (the link filled in) for
the sender thread.  Anything else, a job the Mac never took within APPROVED_EXPIRE_SEC,
or one lost for PLATFORM_LOST_GRACE_SEC, ends ``failed``: nothing goes to the creator
and the console shows it red until an operator marks it dealt with.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import logging
import random
import re
import threading
import time
import unicodedata
import uuid
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from ..edge import text as edge_text
from ..relay.attachments import KINDS as ATTACHMENT_KINDS, Held, attachment_kind
from ..relay.service import KEEP_FILES_DAYS, MAX_AGE_SEC, job_meta
from ..relay.store import PRM
from ..send.bubbles import typing_delay
from ..wechat.new_friends import FILLER, is_friend_notice, is_person_chat
from . import contact, deals, draft_context, gates, handcards, pii, platforms, retention
from .boss_groups import (ASK_NOTICE, AUTO as GROUP_AUTO, AUTO_WHY, BOSS as BOSS_WHO, mask_name,
                          CHANNEL as GROUP_CHANNEL, EVENT as BOSS_EVENT, GONE as BOSS_GONE,
                          GONE_WHY as BOSS_GONE_WHY,
                          HER as GROUP_HER, HER_SIDE as GROUP_HER_SIDE, HER_SIDE_MARK, LINES_KEPT as GROUP_LINES_KEPT,
                          LINKED as BOSS_LINKED, LINKED_NOTICE, MEMBER as GROUP_MEMBER, NEW_NOTICE,
                          OPENING_HOW as BOSS_OPENING, PICK_HOW, PICKED_NOTICE, REACH_LAST_SPOKE, RECORD_LINE_CHARS,
                          REHOOK_LEFT, REHOOKED_NOTICE, US as GROUP_US, BossGroups, told_lines)
from .friend_adds import OPEN as FRIEND_ADD_OPEN, STATES as FRIEND_ADD_STATES, FriendAdds, friend_adds_out
from .group_link import GroupLinks
from .result import MAX_DRAFT_CHARS, MESSAGE_KINDS, PLATFORM_UNASKED, DraftError, parse_draft
from .three_party import EVENT as GROUP_EVENT, TAKEN_BY as GROUP_TAKEN_BY, ThreeParty, state_out as group_state_out
from .wechat_review import WechatReview, admin_problem, hats_of, preview_stamp
from .admin_commands import ACTOR as ADMIN_ACTOR, DealCommands
from .store import (
    APPROVED, BUYIN_TOOL, COPILOT, DRAFT_NOTES, DROPPED, EXECUTING, FAILED, HUMAN, LANE_BUYIN, LANE_REPLY, LANE_SYSTEM,
    DRAFT_NOTE_CHARS, MESSAGE_TOOL, MODES, NO_REPLY, NOTICE_CHARS, PAUSED, PENDING,
    SENDING, SENT, SHIPMENT_STATES, STAGES, SUPERSEDED, PartyError, PrmStore, draft_note_text,
)

logger = logging.getLogger(__name__)

SETTLE_SEC = 8               # messages within this time of each other are one job
OUTBOUND_TOLD = 6            # a draft job is told the deal's last this many replies (what really went out)
OUTBOUND_CHARS = 500
APPROVED_EXPIRE_SEC = 1800   # an approved draft not sent by then (the bot was stopped) is not sent
SEND_TICK_SEC = 5
OWN_MATCH_SEC = 1800         # our own line seen in WeChat within this long after sending is the bot's
PART_LATE_SEC = 120          # a part of a reply in parts is written before its send returned (``sent_at``), give or take
OWN_FILE_SEC = 600           # an own picture/file this soon after the bot sent one is the bot's
READ_BACK_SEC = 1800         # an own row handled this long ago and handed on again (same id) is no new line
READ_BACK_ROWS = 500         # at most this many own rows a chat remembers
RETENTION_CHECK_SEC = 3600
PLATFORM_LOST_GRACE_SEC = 1800   # an execution whose Mac went silent this long is given up on (red)
PLATFORM_OPS = ("platform_execute", "platform_preview")
HOLDING_TEXT = "收到，我确认一下马上回您"
# PRM_REPLY_PARTS (2026-10-30, 「一下子发一大段很怪」): a reply goes out as up to this many WeChat messages, one per
# paragraph of the draft (a blank line between two: ``reply_parts``), a typing pause apart (src/send/bubbles.py
# ``typing_delay``); 1 = one message, blank lines and all, as before.  A config object without it (an older caller's): 1.
REPLY_PARTS = 3
REPLY_PARTS_MAX = 5
_PARAGRAPH = re.compile(r"\n[ \t　]*\n")
# PRM_SEND_PACE (2026-11-04, 「回消息的时候尽量别一口气发一堆过去」): on, a reply's messages go out at a person's pace
# (``_deliver``): its text messages ``typing_delay`` with PACE_TEXT apart (2-5 s by length), its first file PACE_FIRST_FILE
# after its text, its files PACE_NEXT_FILE apart (seconds, at random in the range); off as before (``typing_delay``'s own
# 0.4-3.5 s between text messages, files at once).  A config object without it (an older caller's): off.
PACE_TEXT = {"base": 2.0, "per_char": 0.1, "cap": 5.0}
PACE_FIRST_FILE = (3.0, 6.0)
PACE_NEXT_FILE = (2.0, 4.0)
# 下班时段 (PRM_NIGHT_AUTO_HOURS, 2026-10-29): the holding reply inside the window (PRM_NIGHT_HOLDING_TEXT), and the key
# of a deal's notice of a holding line the window sent for a draft the model flagged (``night:<packet id>``, until 已处理)
NIGHT_HOLDING_TEXT = "收到～您说的我都记下啦，明早第一时间回复您哈～"
NIGHT_NOTICE = "night:"
NIGHT_DONE = "night_done"            # the deal action of 「已处理」 on such a notice (``night_done``)
# 自动模式 (2026-10-30): the console's switch (its ``prm_meta`` key: ``auto_mode_of``), and the holding reply while it is
# what applies (by day: no 「明早」; no setting of its own)
AUTO_MODE = "auto_mode"
AUTO_HOLDING_TEXT = "收到～您说的我都记下啦，稍后确认好第一时间回复您哈～"
# 开场白没人处理就自动发 (PRM_OPENING_AUTO_MIN, 2026-11-02): the route of our first line to a new friend that went out by
# itself after waiting (``send_due_openings``; never counts toward a kind's gate: ``PrmStore.reviewed`` takes ``review``
# only), and the words such a line must not say to go by itself (kb 第 0.5 节: samples are the boss's, in the 三方群)
# (2026-11-10, the user: an opening may say 「看中的款可以安排寄样给您试戴」, so 寄样 / 样品 / 试戴 no longer hold it; a
# promise still does: free, postage, a gift, how many)
OPENING_ROUTE = "opening"
OPENING_AUTO_WORDS = ("免费", "包邮", "邮费", "样衣", "寄出", "寄给", "送你", "送您", "送一", "任选", "看中多少", "寄几", "几顶")
OPENING_AUTO_GRACE_SEC = 120     # none goes the first minutes after a start: the catch-up may find she wrote meanwhile
# 同一张图不重复发 (2026-11-02, the user: the 开场白's 3 pictures went again with her 货盘 an hour later, by 自动模式): a file
# she got from us this recently (the same bytes) is left out of a new draft (``_repeated_files``)
REPEAT_FILE_SEC = 7 * 86400
# 她只回「好滴」这类 (2026-11-02, the user: after our 货盘 she said 「好滴」, its draft waited, and a 稍等 went out 10 minutes
# later): a round of her messages that only says she got it has nothing to hold, so no holding reply (``ack_only``)
ACK_WORDS = tuple(sorted(("好滴", "好的", "好嘞", "好哒", "好咧", "好吧", "好", "嗯嗯", "嗯", "哦哦", "哦", "噢", "收到", "谢谢",
                          "谢了", "感谢", "ok", "okk", "okay", "可以", "行", "没问题", "知道了", "了解", "明白", "辛苦", "我看看",
                          "我看一下", "我先看看", "我先看一下", "看看", "我考虑一下", "考虑一下", "我想想", "想想", "晚点回你",
                          "回头看"), key=len, reverse=True))
_ACK_STAMP = re.compile(r"^\[\d{2}-\d{2} \d{2}:\d{2}\]\s*")
_ACK_TAGS = re.compile(r"\[(?:表情|动画表情|微笑|OK|强|抱拳|握手|玫瑰|爱心|憨笑|呲牙|偷笑|可爱|害羞|愉快|耶|胜利|拥抱|鼓掌)\]")
_ACK_NOISE = re.compile("[\\s～~!！。.，,、…?？呀啊啦哈嘛呢哟哇喔\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]+")
# 三方群里的话 (2026-11-02, the user: 「第一点要知道」: what the boss settles in her 三方群, the styles, the samples, the
# commission, her private drafts must know): every line of her 三方群 the bot reads (the one 「拉三方群」 made: read only,
# never answered; a 老板拉的群 it took over) is kept on her deal (``GROUP_LINE`` events: masked, the bosses' names as
# PRM_BOSS_CALL), and each draft job of her private chat is told the latest GROUP_LINES_TOLD (``meta.three_party_chat``)
GROUP_LINE = "group_line"
GROUP_LINES_TOLD = 20
GROUP_LINES_SCAN = 200          # ... of the newest this many kept on the deal (her 三方群's only: ``_group_lines``)
GROUP_LINE_CHARS = 300
GROUP_WHO_LABELS = {"creator": "她", "us": "我们", "member": "群里的人"}   # (the boss: PRM_BOSS_CALL)
THREE_PARTY_WATCH_BACK_SEC = 300   # her 三方群 is read from shortly before it was made, a restart from its last line kept
# Mac 重启以前发的、后来 Windows 才报失败的：没有发送时间时往回找这么久（edge 后端的发送记录留一天）
RESTART_FAIL_SEC = 86400
CATCHUP_HOURS = 12           # PRM_CATCHUP_HOURS: a message read late is drafted up to this old (0-72)
LINES_CHARS = 6000           # the creator's lines a notice of a draft not written keeps for her next draft
OWN_FILE_EARLY_SEC = 60      # a file the bot sent shows in WeChat up to this long before its send returns
# A draft the bot was stopped in the middle of sending (``start``), and what it says once its text was read back
UNSURE_ERROR = "发送时机器人停了，不确定发出去没有；请到微信里看一眼，需要就手动补发"
# 批准并执行 refused (_approve_execute): the preview changed since it was read (or since the admin was shown it), a 重新预览
# started meanwhile; a job the relay certainly did not queue (the draft waits again; an edited reply is kept for the
# console's 「改了再执行」), one it is not known whether the relay queued (red as a result not known, its key kept: a late
# result is marked late; never 「再批准一次」)
PREVIEW_CHANGED = "预览刚刚变过（重新预览了）：请看了新的预览再批准"
PREVIEWING = "正在重新预览，等结果出来再批准"
QUEUE_FAILED = "没能排进 Mac 的任务队列，平台没有动：可以再批准一次"
QUEUE_UNKNOWN = "排进 Mac 的任务队列时出了错，不知道排进去没有：平台上可能已经写了，请先到平台后台核对，别再批准一次"
QUEUE_UNKNOWN_PROBLEM = "排进 Mac 的任务队列时出了错，不知道排进去没有"
UNSURE_TEXT_SEEN = ("发送时机器人停了：文字已经发出去了（微信里读到了），素材文件不确定发出去没有；"
                    "请到微信里看一眼，需要就手动补发文件")
# This PC's clock against WeChat's (win review F6): measured when the bot's own line is read back
CLOCK_SAMPLE_SEC = 120       # read back this soon after it was noted, a line of ours measures the clock
CLOCK_SAMPLES = 3            # the smallest of the last this many measurements counts (a late read only adds)
CLOCK_NOISE_SEC = 60         # off by less than this: read-back noise, nothing is corrected
CLOCK_SKEW_SEC = 300         # off by this much or more: the deals say so (``clock_note``)
# 聚水潭寄样单状态自动同步（PRM_SAMPLE_SYNC_MIN，2026-10-29）：机器人定时把核验成功、还没签收、没被删的寄样单
# 攒成一个任务交给 Mac（``meta.op = SAMPLE_OP``，worker 不跑模型，`jst samples` 只读），答复按控制台的路改合作单。
SAMPLE_OP = "sample_status"
SAMPLE_SYNC_CHAT = "prm:sample-status"      # the relay job's chat id: no WeChat chat, no creator, never a draft
# The deal's notice asking for the waybill of an order 聚水潭 says went out: its sample list gives none (2026-10-29, read
# on the Mac: ``logisticsList`` and the ERP orders' ``logisticsNo`` empty, signed orders too); gone once one is typed in.
SAMPLE_ASK = "sample-waybill:"
SAMPLE_SYNC_FIRST_SEC = 120                 # a bot that never asked: its first round this long after its start
SAMPLE_GIVE_UP_DAYS = 60                    # an order not signed for this long after it was made is not asked about
SAMPLE_MISSES = 2                           # rounds in a row an order is not in 聚水潭's answer before its deal says so
SAMPLE_ERRORS = 3                           # failed rounds in a row (a hiccup: no answer, the lock busy) before the deals say so
SAMPLE_RISK_PAUSE_SEC = 3600                # after a risk wall (76): the next round waits this long at least (and two intervals)
SAMPLE_RANK = {"INIT": 1, "WAITING_SEND": 2, "SENT": 3, "SIGNED": 4, "DELETED": 5}   # how far an order got: never back
# What the deals with orders still asked about say when a round could not be made (``sample_sync``): once, until
# a round goes through (the notice goes by itself then).
SAMPLE_PROBLEMS = {
    "login": "聚水潭登录失效（退出码 77），寄样单的运单号和签收没法自动同步：请在 Mac 的调试 Chrome 里重新登录聚水潭；"
             "登录好以后这条提醒自己消失",
    "wall": "聚水潭限流或者要人机验证（退出码 76），寄样单状态先停 {pause} 分钟再同步：请到 Mac 的调试 Chrome 里看一下；"
            "恢复以后这条提醒自己消失",
    "wrong_account": "Mac 的调试 Chrome 里登录的聚水潭不是这台 Mac 配的子账号（退出码 79），寄样单状态没有同步："
                     "请换回自己的子账号；恢复以后这条提醒自己消失",
    "invalid": "Mac 上的聚水潭命令行不认查寄样单的参数（{message}）：请更新 jushuitan-engineering",
    "unavailable": "Mac 上查不了聚水潭（{message}），寄样单的运单号和签收没法自动同步",
    "old_worker": "Mac 上的 worker 还不会查聚水潭寄样单的状态：更新 Mac 上的代码并重启 worker（在这之前运单号和签收照旧手动填）",
    "error": "寄样单状态连续 {count} 次没同步成（{message}）：运单号和签收先照旧手动填",
}
# 手动建的寄样单（2026-11-05，用户：「我们怎么能第一时间得知发货」）：每一轮也请 Mac 看一眼我们这个子账号最新的寄样单
# （``meta.mine``，`jst samples --mine`），机器人自己建的单照旧，别的（命令行、页面手动建的）有了运单号就处理一次：
# 达人的平台账号（抖音号 / 小红书号）恰好对上一位登记达人、合作单还在寄样这一步，就照「填运单号」填进她的合作单
# （发货通知一律等人审）；对不上、对上两位、合作单过了寄样、合作单上填了别的运单号，就在「待审批」顶上出一行，
# 到「知道了」为止。手动建的单只填运单号：不标已签收、不开专属佣金（签收照旧人标）。第一次读到时已经发货的单只记下、
# 不处理（``HAND_BASELINE``：上线前寄出的单运营早就知道了）。
HAND_BASELINE = "hand_samples_baseline_at"  # prm_meta: when our hand-made orders were first read (shipped ones then: left)
HAND_KEEP_DAYS = 30                         # a hand-made order first seen this long ago is forgotten (its line too)
HAND_LINES = 20                             # 「待审批」 lines of hand-made orders shown at most (the newest)
HAND_ERRORS = 2                             # rounds in a row the hand-made orders could not be read before the page says so
HAND_WAYBILL_WAIT_SEC = 2 * 3600            # sent in 聚水潭 without a waybill this long (its time): the page says so
# 拆成几个包裹的寄样单（2026-11-06，用户：「等发货单全了再说」）：仓库先发有货的那一包，别的等到货；每个包裹都有了运单号
# 才填进合作单、出发货通知（通知里写全几个运单号）。只发了一部分时机器人自己建的单在合作单上留一条提醒，手动建的单什么都
# 不出；第一包发出 PRM_SAMPLE_WAYBILL_DAYS 天了别的还没运单号，合作单的提醒（手动建的单：「待审批」顶上一行，``HAND_SPLIT``）
# 改成请人去聚水潭看一下，要先通知达人就在合作单上「填运单号」。
HAND_SPLIT = "split"                        # a hand-made order's line: only part of it went out (the rest acts)
HAND_PROBLEMS = {
    "old_worker": "Mac 上的 worker 还是旧版本，查不了手动建的寄样单：在 Mac 上重启 worker（手动建的单发货了先不会提醒）",
    "unavailable": "查不了手动建的寄样单（{message}）：手动建的单发货了先不会提醒",
    "login": "聚水潭登录失效，查不了手动建的寄样单：请在 Mac 的调试 Chrome 里重新登录聚水潭",
    "wall": "聚水潭限流或者要人机验证，这一轮没查手动建的寄样单：恢复以后自己接着查",
    "wrong_account": "Mac 的调试 Chrome 里登录的聚水潭不是这台 Mac 配的子账号，查不了手动建的寄样单",
    "error": "手动建的寄样单连续 {count} 次没查成（{message}）：发货了先不会提醒",
}
# 抖音百应私信（PRM_BUYIN_SYNC_MIN，2026-10-29，默认关）：邀约带货的达人可能在百应自己的私信（精选联盟私信）里回，不在微信里。
# 机器人定时请 Mac 读一次新消息（``meta.op = BUYIN_OP``，worker 不跑模型，`dy buyin im-inbox` 只读，契约见
# tools/claude_relay/README.md「百应私信」），登记达人的新话由 Mac 起草一份百应私信草稿（``meta.channel = BUYIN_CHANNEL``，
# 聊天编号 ``BUYIN_CHAT + 会话编号``），待审批里运营复制、自己到百应里发，机器人哪儿都不发它。
BUYIN_OP = "buyin_inbox"
BUYIN_SYNC_CHAT = "prm:buyin-inbox"         # the poll job's chat id: no WeChat chat, no creator, never a draft
BUYIN_CHAT = "buyin:"                       # a 百应 draft job's (and packet's) chat id: this + the conversation id
BUYIN_CHANNEL = "buyin"                     # ``meta.channel`` of those jobs and packets
BUYIN_FIRST_SEC = 120                       # a bot that never asked: its first round this long after its start
BUYIN_MAX_AGE_SEC = 86400                   # her 百应 line read this long after it was written: recorded, not drafted
BUYIN_HISTORY = 20                          # earlier lines of the conversation a draft job is told
BUYIN_SEEN = 500                            # message ids a conversation remembers (the newest)
BUYIN_SAID_CHARS = 1500                     # her lines a 百应 draft keeps for the console
BUYIN_UNMATCHED_DAYS = 7                    # a conversation of nobody on the list is shown, and kept, this long
BUYIN_ERRORS = 3                            # failed rounds in a row (a hiccup) before the console says so
BUYIN_RISK_PAUSE_SEC = 3600                 # after a risk wall (76): the next round waits this long at least
BUYIN_CONV_TRIES = 3                        # rounds in a row one conversation fails before it is skipped (the deal says so)
BUYIN_LOG_EVERY_SEC = 600                   # a held conversation that keeps failing to be drafted: logged this seldom
BUYIN_KINDS = {"card": "[卡片]", "contact": "[联系方式卡片]", "other": "[其他消息]"}
# A creator's contact card (kind "contact") is recorded only masked (``buyin_contact_line``): its WeChat id and phone in
# full live only in memory while a round is taken in, and in the ``on_buyin_contact`` call (never in prm.db, events,
# notices, pii tokens, relay jobs or logs).  A contact she types in her own words (``contact.find_contacts``: a 微信号
# only after a label) goes to the same call (source "buyin_text") in a conversation that is ours; her line is kept
# with her phone as 〈手机N〉 (pii, as on WeChat) and the 微信号 masked (``contact.mask_contacts``).
BUYIN_CONTACT_UNREAD = contact.CARD_HEAD + contact.UNREAD
BUYIN_CONTACT_OWN = contact.CARD_HEAD + contact.OWN
# What the 待审批 page says when a round could not be made (until one goes through).
BUYIN_PROBLEMS = {
    "login": "抖音百应登录失效（退出码 77），百应私信读不了：请在 Mac 的调试 Chrome 里重新登录百应；登录好以后这条提醒自己消失",
    "wall": "抖音百应限流或者要人机验证（退出码 76），百应私信先停 {pause} 分钟再读：请到 Mac 的调试 Chrome 里看一下",
    "wrong_account": "Mac 的调试 Chrome 里登录的百应不是这台 Mac 配的店铺（退出码 79），百应私信没有读：请换回自己的店铺",
    "invalid": "Mac 上的抖音命令行不认读百应私信的参数（{message}）：请更新 douyin-engineering",
    "unavailable": "百应私信读不了（{message}）；在这之前达人在百应里的消息请直接到百应里看、在百应里回",
    "old_worker": "Mac 上的 worker 还不会读百应私信：更新 Mac 上的代码并重启 worker（在这之前请直接到百应里看）",
    "error": "百应私信连续 {count} 次没读成（{message}）：先直接到百应里看",
    "conversations": "百应私信里有 {count} 个会话连续 {rounds} 轮没处理成（{message}）：下一轮再试；连续 {tries} 轮不成的会跳过，"
                     "登记达人的合作单上会提醒，请直接到百应里看",
}
_QUOTE_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9]+:\s*")
_WXID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{3,63}$")


def _queued(msg: dict) -> bool:
    """Windows 的微信边缘排队转来的（Mac 端 edge 后端，带 edge_event_id）：Windows 只转它运行以后收到的、
    Mac 没确认的事件（按编号续读，不会重放历史），所以 Mac 离线多久再取都不是历史，不按 MAX_AGE_SEC 丢。
    PRM_CATCHUP_HOURS 大于 0 时它和本机读到的消息一样按补读的规矩走（晚到的起草但一律先审，太旧的只记下），
    等于 0 时照旧不看年龄。"""
    return msg.get("edge_event_id") not in (None, "")


def check_wxid(wxid: str, reserved: set) -> str:
    """The wxid as registered, or PartyError (in Chinese) why it cannot be a creator."""
    wxid = str(wxid or "").strip()
    if not wxid:
        raise PartyError("要填达人的 wxid")
    if wxid.endswith("@chatroom"):
        raise PartyError("这是群聊的 id，达人要登记私聊的 wxid")
    if not _WXID.match(wxid):
        raise PartyError("wxid 只能是字母开头的字母、数字、下划线或短横线（形如 wxid_xxx）")
    if wxid in reserved:
        raise PartyError("这是管理员或能用 cc 中转的人，不能同时登记成达人")
    return wxid


# Where a creator's 抖音 link came from (``creator_links.source``): 加百应达人微信's 「是她，登记并关联」, or the 抖音号 an
# operator gave 登记达人.
LINK_SOURCES = ("friend_add", "register")
CREATOR_IDS_NOTICE = "creator_ids"          # the deal's notice: her 抖音号 from two sources that do not agree


def sample_creator_ids(store: PrmStore, deal_id: int) -> tuple:
    """The 抖音号 and 抖音 UID the deal's verified 抖店 sample orders found: ``(douyin_ids, uid)``.  An order the worker
    recorded before ``created.creator_account`` gives it from the CLI's own answer (its ``daren.platformNo``)."""
    douyin, uid = [], ""
    for packet in store.deal_packets(deal_id, tools=("jst.sample_order",), statuses=(APPROVED, SENDING, SENT, FAILED)):
        data = packet["platform"] or {}
        action, execution = data.get("action") or {}, data.get("execution") or {}
        if execution.get("outcome") != platforms.VERIFIED or action.get("creator_platform") != "dy":
            continue
        created = execution.get("created") or {}
        for value in (created.get("creator_account"), action.get("creator_account"), _platform_no(execution)):
            value = str(value or "").strip()
            if value and platforms.creator_account_ok(value) and value not in douyin:
                douyin.append(value)
        uid = uid or str(created.get("creator_uid") or action.get("creator_uid") or "")
    return douyin, uid


def sample_creator_uids(store: PrmStore, deal_id: int) -> tuple:
    """Every 抖音 UID the deal's verified 抖店 sample orders found, newest first, and how many such orders there are:
    ``(uids, orders)`` (a creator with two 抖音 accounts has two: review 2026-11-09 F4)."""
    uids, orders = [], 0
    for packet in store.deal_packets(deal_id, tools=("jst.sample_order",), statuses=(APPROVED, SENDING, SENT, FAILED)):
        data = packet["platform"] or {}
        action, execution = data.get("action") or {}, data.get("execution") or {}
        if execution.get("outcome") != platforms.VERIFIED or action.get("creator_platform") != "dy":
            continue
        orders += 1
        uid = str((execution.get("created") or {}).get("creator_uid") or action.get("creator_uid") or "")
        if uid and uid not in uids:
            uids.append(uid)
    return uids, orders


def creator_ids_notice(store: PrmStore, deal_id: int, now: float) -> None:
    """Her 抖音号 from her verified sample order and from an operator's link do not agree: the deal says so
    (``CREATOR_IDS_NOTICE``) until they do; a wrong link could make a 专属佣金 look opened by an invite card
    (design §8.4).  Not a sign of life of the deal (``touch`` False)."""
    deal = store.deal(deal_id)
    if deal is None:
        return
    sample, _ = sample_creator_ids(store, deal_id)
    linked = [str(link["account"]) for link in store.creator_links(deal["party_id"]) if link["platform"] == "dy"]
    text = ""
    if sample and linked and {s.lower() for s in sample} != {a.lower() for a in linked}:
        text = (f"合作单里的抖音号对不上：关联的 {'、'.join(linked)}，寄样单的 {'、'.join(sample)}，请核对。"
                "开专属佣金时按台账判断「邀约卡开过没有」会受影响")
    if text or any(n["key"] == CREATOR_IDS_NOTICE for n in deal["notices"]):
        store.set_notice(deal_id, CREATOR_IDS_NOTICE, text, now, touch=False)


def link_creator(store: PrmStore, party: dict, link: dict, *, now: float, actor: str = "console") -> bool:
    """Who a registered creator is on 抖音 (``check_link``'s ``link``): ``creator_links`` (the first link of an account
    stays) and a ``friend_add_link`` event on her deal (her 抖音号, the record, when the invite card went out; never a
    number); the deal says so when her sample order knows another 抖音号 (``creator_ids_notice``).  True when new."""
    deal = store.deal_of(party["id"], now)
    made = store.add_creator_link(party["id"], link["account"], uid=link["uid"], uid_source=link["uid_source"],
                                  nickname=link["nickname"], source=link["source"], add_id=link["add_id"], now=now)
    detail = {"account": link["account"], "source": link["source"], "new": made}
    for key in ("add_id", "invited_at", "plan_id"):
        if link.get(key):
            detail[key] = link[key]
    store.add_event(deal["id"], party["id"], "friend_add_link", detail, actor=actor, now=now)
    creator_ids_notice(store, deal["id"], now)
    return made


def unlink_creator(store: PrmStore, party_id: int, account: str, *, now: float, actor: str = "console") -> dict:
    """An operator says a creator's 抖音 link was wrong (取消关联): it goes, her deal says so (``creator_unlinked``)."""
    party = store.party(int(party_id))
    if party is None:
        return {"ok": False, "error": "没有找到这个达人"}
    if not store.delete_creator_link(party["id"], str(account or "").strip()):
        return {"ok": False, "error": "这位达人没有关联这个抖音号"}
    deal = store.deal_of(party["id"], now)
    store.add_event(deal["id"], party["id"], "creator_unlinked", {"account": str(account).strip()}, actor=actor, now=now)
    creator_ids_notice(store, deal["id"], now)
    return {"ok": True}


def check_link(link) -> Optional[dict]:
    """``register``'s ``link`` as it is kept (``{account, uid, uid_source, nickname, source, add_id, invited_at,
    plan_id}``), None for none, or PartyError (in Chinese) why it cannot be."""
    if link is None:
        return None
    if not isinstance(link, dict):
        raise PartyError("关联的抖音号不对")
    account = str(link.get("account") or "").strip()
    if not platforms.creator_account_ok(account):
        raise PartyError("抖音号只能是字母、数字、下划线、点或短横线（最多 40 个）")
    source = str(link.get("source") or "register")
    if source not in LINK_SOURCES:
        raise PartyError(f"不认识的关联来源 {source}")
    uid = " ".join(str(link.get("uid") or "").split())[:40]
    try:
        add_id = int(link["add_id"]) if link.get("add_id") not in (None, "") else None
    except (TypeError, ValueError):
        add_id = None
    return {"account": account, "uid": uid, "uid_source": str(link.get("uid_source") or "")[:20] if uid else "",
            "nickname": contact.clean_name(link.get("nickname")), "source": source, "add_id": add_id,
            "invited_at": " ".join(str(link.get("invited_at") or "").split())[:40],
            "plan_id": " ".join(str(link.get("plan_id") or "").split())[:64]}


def message_text(msg: dict) -> str:
    """What a message says, as one line for the draft: the WeChat edge's line (src/edge/text.py), so the
    Mac is told the same in both deployments (win review F2): a picture, a file by its name (its file
    still goes the attachment way), voice by its words, a link or mini program by its title (and its
    address, once the backend finds one), 视频号, a transfer, a red packet, a location, a card…  A
    message Windows' edge forwarded (``_queued``: the Mac's edge backend) comes with that line already."""
    if _queued(msg):
        line = _QUOTE_PREFIX.sub("", str(msg.get("content", "") or "")).strip()
        if line:
            return line
    return edge_text.message_text(msg)


def ack_only(text: str) -> bool:
    """Is a draft job's text (her lines, ``[MM-DD HH:MM] …`` each) nothing but 「好滴」「收到」「谢谢」「我看看」, a sticker or
    an emoji (``ACK_WORDS``, ``_ACK_TAGS``): she only says she got it?  False for an empty text (a 开场白 has none) or for
    any line with more in it (a question, a picture, 「好滴，哪款适合我」)."""
    lines = [_ACK_STAMP.sub("", line).strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return False
    for line in lines:
        rest = _ACK_NOISE.sub("", _ACK_TAGS.sub("", line)).lower()
        while rest:
            word = next((w for w in ACK_WORDS if rest.startswith(w)), None)
            if word is None:
                return False
            rest = rest[len(word):]
    return True


def _norm(text: str) -> str:
    return " ".join(str(text or "").split())


def reply_parts(text: str, most: int = REPLY_PARTS) -> list[str]:
    """The WeChat messages a reply goes out as (``_deliver``): its paragraphs (a blank line between two, white space on
    it or not), each stripped, the empty ones dropped, those past ``most`` joined to the last one (a line break
    between).  ``most`` 1: the text as it is, one message.  [] for a text of nothing but white space."""
    if not str(text or "").strip():
        return []
    if most <= 1:
        return [text]
    parts = [p.strip() for p in _PARAGRAPH.split(str(text).replace("\r\n", "\n"))]
    parts = [p for p in parts if p]
    if len(parts) > most:
        parts = parts[:most - 1] + ["\n".join(parts[most - 1:])]
    return parts


_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".heic", ".heif", ".tif", ".tiff")
_VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".avi", ".wmv", ".mkv", ".flv", ".3gp")


def file_kind(name: str) -> str:
    """What WeChat shows a file the bot pastes into a chat as, by its name: ``image`` (a picture),
    ``gif`` (a picture, or a sticker), ``video`` or ``file``."""
    suffix = Path(str(name or "")).suffix.lower()
    if suffix == ".gif":
        return "gif"
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _VIDEO_SUFFIXES:
        return "video"
    return "file"


def own_file_fits(kind: str, msg: dict) -> bool:
    """Can this own line in WeChat be a file of ``kind`` (``file_kind``) the bot sent (win review F4 (b))?
    A picture (type 3) is an image's or a GIF's, a sticker (47) only a GIF's, a video (43) a video's,
    a file (49 with a file name, or one the backend could not read) a file's, or an image's or video's
    WeChat sent as a file (its name says so); a voice message, a link, a mini program, a card … never.
    The Mac's ``edge_seen`` lines are our sends Windows already read back (their type says only "a file")."""
    if msg.get("edge_seen"):
        return True
    msg_type = edge_text.msg_type_of(msg)
    if msg_type == 3:
        return kind in ("image", "gif")
    if msg_type == 47:
        return kind == "gif"
    if msg_type == 43:
        return kind == "video"
    if msg_type == 49:
        name = str(msg.get("file_name") or "").strip()
        if name:
            return kind in ("file", file_kind(name))
        special = msg.get("special") if isinstance(msg.get("special"), dict) else {}
        appmsg = str(special.get("type") or "")
        return kind == "file" and appmsg in ("", "6")      # a file the backend could not read the name of
    return False


def _row_key(msg: dict) -> Optional[str]:
    """Which WeChat row an own line is, to know it when it is handed on again (``_match_own``): its
    message id (the WCDB backend's never changes for a row; the Mac's edge lines carry Windows' id, or
    their event's); without one, its type, words and the second WeChat says it was written (a row
    read twice has the same; the same words typed later have another).  None when there is nothing to
    know it by (no id, no time): such a line is never taken for a row read again."""
    msg_id = str(msg.get("message_id") or "").strip()
    if msg_id:
        return f"id:{msg_id}"
    event_id = msg.get("edge_event_id")
    if event_id not in (None, ""):
        return f"event:{event_id}"
    try:
        written = int(float(msg.get("timestamp") or 0))
    except (TypeError, ValueError):
        written = 0
    if not written:
        return None
    words = _norm(_QUOTE_PREFIX.sub("", str(msg.get("content", "") or ""))) or str(msg.get("file_name") or "")
    return f"line:{edge_text.msg_type_of(msg)}|{words}|{written}"


def _clock_text(at: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(at))


def late_note(earliest: float) -> str:
    """The deal's attention for a draft of messages read late (PRM_CATCHUP_HOURS)."""
    return f"这次的消息是机器人没在读微信时收到的（最早 {_clock_text(earliest)}），草稿要人看过再发"


def missed_note(count: int, earliest: float) -> str:
    """The deal's attention for messages too old to draft (older than PRM_CATCHUP_HOURS when read)."""
    return f"{count} 条消息在机器人没在读微信时收到（最早 {_clock_text(earliest)}），没有起草，请看一下"


def recall_note(at: float) -> str:
    """The deal's attention (and a draft's reason) when the creator took a message back."""
    return f"达人撤回了一条消息（{_clock_text(at)}），待审批的草稿可能是按撤回的内容写的，发之前请核对"


def _skew_text(skew: float) -> str:
    """「快 3 小时」 / 「慢 12 分钟」: this PC's clock against WeChat's (``skew``: this PC's minus WeChat's)."""
    hours, minutes = divmod(int(round(abs(skew) / 60)), 60)
    amount = (f"{hours} 小时" if hours else "") + (f"{minutes} 分钟" if minutes or not hours else "")
    return f"{'快' if skew > 0 else '慢'} {amount}"


def clock_note(skew: float) -> str:
    """The deals' notice while this PC's clock is off WeChat's by CLOCK_SKEW_SEC or more (``skew``: this
    PC's clock minus WeChat's, as the bot's own lines read back measured it)."""
    return (f"这台电脑的时间比微信{_skew_text(skew)}（机器人发出的消息读回来时测的），"
            "消息是不是晚到已经按微信的时间算；请把电脑时间改对（Windows 设置 → 时间和语言 → 自动设置时间），"
            "改好以后机器人再发出消息时会重新测，这条提醒自己消失")


def night_clock_text(skew: Optional[float], what: str = "下班时段") -> str:
    """Why the night window (``what``: or 自动模式 by day) sends nothing by itself (``PrmService._clock_off``): this PC's
    clock is off WeChat's by ``skew`` as this run measured it, or (None) a run before measured it off and nothing
    measured it since."""
    if skew is None:
        return (f"上次运行测到这台电脑的时间和微信对不上，这次还没测过：{what}先不自动发"
                "（机器人发出的消息读回来、测过时间以后自动恢复）")
    return (f"这台电脑的时间比微信{_skew_text(skew)}：{what}先不自动发；请把电脑时间改对，"
            "改好以后机器人再发出消息时会重新测，自动恢复")


def night_status(window, now: float, suspended: str = "", error: str = "", auto: bool = False) -> dict:
    """What the console says of the 下班时段 (PRM_NIGHT_AUTO_HOURS; ``window``: ``gates.night_window``, None = off):
    ``{"on", "window" ("18:00–09:00"), "inside" (now, by this PC's clock), "suspended" (why nothing goes out by itself
    at night now: ``night_clock_text``), "error" (a setting the bot would refuse), "auto"}``.  ``auto``: 自动模式 is on
    (the console's switch); the answer's ``auto`` says it is what puts the night rules in force now (no window, or
    now outside it), and ``suspended`` then speaks for it with no window too."""
    if window is None:
        return {"on": False, "window": "", "inside": False, "suspended": suspended if auto else "", "error": error,
                "auto": bool(auto)}
    inside = gates.in_night(window, now)
    return {"on": True, "window": gates.night_window_text(window), "inside": inside,
            "suspended": suspended, "error": "", "auto": bool(auto) and not inside}


def auto_mode_of(store: PrmStore) -> dict:
    """The console's 自动模式 as the store keeps it (``write_auto_mode``): ``{"on", "since" (when it was last switched,
    None never), "by"}``; off when it was never switched or cannot be read."""
    try:
        data = json.loads(store.meta_value(AUTO_MODE) or "{}")
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    return {"on": data.get("on") is True, "since": _float(data.get("since")), "by": str(data.get("by") or "")}


def write_auto_mode(store: PrmStore, on: bool, *, now: float, actor: str = "console") -> dict:
    """Switch 自动模式 on or off (``auto_mode_of`` afterwards; one log line).  Switched to what it is already, nothing
    changes (a double click keeps the time it was switched on).  Also when the bot is stopped: straight on the
    database, it applies at the next start."""
    have = auto_mode_of(store)
    if have["on"] == bool(on):
        return have
    store.set_meta_value(AUTO_MODE, json.dumps({"on": bool(on), "since": now, "by": str(actor)}))
    logger.info("PRM: auto mode switched %s (%s)%s", "on" if on else "off", actor,
                ": a draft that passes the night window's rules goes out by itself until it is switched off" if on
                else ": drafts wait for a person again (unless their kind's gate or the night window says otherwise)")
    return auto_mode_of(store)


def failed_text(head: str, into: Optional[int] = None, lines: bool = True) -> str:
    """The notice of a draft job that gave no draft: what went wrong, and where her lines go (``lines`` False: a notice
    of the records it was told only, ``history``: where they go)."""
    if not lines:
        return f"{head}；" + (f"这些记录已经交给 #{into} 一起起草" if into else "这些记录会交给她下一份私聊的草稿")
    return f"{head}；" + (f"她这几条消息已经并进 #{into} 一起起草" if into else
                         "她这几条消息会并进她下一条消息的草稿一起回，也可以直接在微信里回她")


def _merged_gap(have, info: dict) -> dict:
    """Two of a job's ``meta.late`` / ``missed`` / ``new_friend``: the earliest ``from``, counts added."""
    if not isinstance(have, dict) or not have.get("from"):
        return dict(info)
    out = {**have, "from": min(float(have["from"]), float(info.get("from") or have["from"]))}
    if "count" in have or "count" in info:
        out["count"] = int(have.get("count") or 0) + int(info.get("count") or 0)
    if have.get("linked") or info.get("linked"):
        out["linked"] = True                           # (a group linked by hand registered her: nothing goes by itself)
    return out


def system_line(msg: dict) -> Optional[str]:
    """The words of one of WeChat's own lines in a chat (type 10000 / 10002: a recall, 拍一拍, a red
    packet or transfer notice …), or None for a line someone wrote.  Their sender is empty, so the
    Windows backend hands them on as the creator's (win review F1).  WeChat's lines about becoming
    friends are ``is_friend_notice``'s, handled before this."""
    return edge_text.notice_text(msg) if edge_text.is_notice(msg) else None


def new_friend_note(count: int, earliest: float) -> str:
    """The deal's attention for the draft of a new friend's messages from before she was registered."""
    return (f"新加的好友，登记前发来的 {count} 条消息（最早 {_clock_text(earliest)}，含加好友时的验证消息）"
            "起草了这份草稿，要人看过再发")


def opening_note(opening: dict) -> str:
    """The deal's attention for a 开场白 draft (``meta.opening``)."""
    if opening.get("by") == "console":
        return "运营点了「写开场白」：这份是我们先打招呼的话，要人看过再发"
    if opening.get("by") == BOSS_OPENING:
        return "老板在群里介绍的达人：这份是我们在群里先打招呼的开场白，老板也看得到，要人看过再发"
    who = "她通过了我们的好友申请" if opening.get("how") == "requested" else "新加的好友"
    return f"{who}：这份是我们先打招呼的开场白，要人看过再发"


HUOPAN_NOTE = "运营点了「补发货盘」：这份是我们主动补发的货盘，要人看过再发"
HANDCARDS_NOTE = "运营点了「做手卡」：这份是我们主动发的手卡，要人看过再发"


def gap_notes(meta: dict) -> list[str]:
    """What a draft job's ``meta`` says about messages read late (``late``), recorded without a
    draft (``missed``) or written by a new friend before she was registered (``new_friend``), our
    first line to a creator who had not written (``opening``) and the 货盘 an operator asked to send
    her again (``huopan``), as attention lines (any of them: the draft waits for a person)."""
    notes = []
    late, missed, new_friend = meta.get("late"), meta.get("missed"), meta.get("new_friend")
    if isinstance(late, dict) and late.get("from"):
        notes.append(late_note(float(late["from"])))
    if isinstance(missed, dict) and missed.get("from"):
        notes.append(missed_note(int(missed.get("count") or 1), float(missed["from"])))
    if isinstance(new_friend, dict) and new_friend.get("from"):
        notes.append(new_friend_note(int(new_friend.get("count") or 1), float(new_friend["from"])))
    if isinstance(meta.get(OPENING), dict):
        notes.append(opening_note(meta[OPENING]))
    if isinstance(meta.get(HUOPAN), dict):
        notes.append(HUOPAN_NOTE)
    if isinstance(meta.get(HANDCARDS), dict):
        notes.append(HANDCARDS_NOTE)
    return notes


def history_of(backlog: list) -> list[dict]:
    """A draft job's ``meta.history``: the recorded lines it is told (``take_backlog``: what both sides said while a
    person had the chat, her messages read too late to draft), oldest first."""
    return [{"who": "operator" if ev["kind"] == "operator_message" else "creator",
             "text": str(ev["detail"].get("text", "")),
             **({"missed": True} if ev["detail"].get("missed") else {})} for ev in backlog]


def _event_time(ev: dict) -> float:
    """When a recorded message was written (its own time when kept, else when it was recorded)."""
    try:
        return float((ev.get("detail") or {}).get("at") or ev["at"])
    except (TypeError, ValueError):
        return float(ev["at"])


def _more_waybills(packet: dict) -> list:
    """The other packages' waybills a shipping notice names besides the deal's own (``deals.add_shipment``'s
    ``more_waybills``, an order the warehouse sent in several, 2026-11-06): the notice may carry them too."""
    more = (packet.get("meta") or {}).get("more_waybills")
    return [str(m["waybill"]) for m in more if isinstance(m, dict) and m.get("waybill")] if isinstance(more, list) else []


def _float(value) -> Optional[float]:
    """A number kept as text (``PrmStore.meta_value``), or None."""
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _json_value(text):
    """A value kept as JSON text (``PrmStore.meta_value``), or None."""
    try:
        return json.loads(text) if text not in (None, "") else None
    except (TypeError, ValueError):
        return None


def is_group_packet(packet: dict) -> bool:
    """A packet for a 老板拉的群 (its meta says so, or it goes to a group chat): only PRM_BOSS_GROUPS makes them, and a
    group an operator linked by hand (接手任何合作群: ``BossGroups.by_hand``, whatever the switch says)."""
    return ((packet.get("meta") or {}).get("channel") == GROUP_CHANNEL
            or str(packet.get("chat_id") or "").endswith("@chatroom"))


def is_buyin(packet: dict) -> bool:
    """Is this a 抖音百应 private-message draft (an operator sends it in 百应 by hand; never the bot)?"""
    return packet.get("tool") == BUYIN_TOOL


def _moved_on(deal: dict) -> str:
    """Why a 签收后问一句 is not asked for: the deal is past the sample already (a person marked it, or moved it on)."""
    return f"合作单已经是「{STAGES.get(deal['stage'], deal['stage'])}」了（有人先标了），不再问"


def _later(deal: dict, at: float) -> float:
    """The deal's ``last_message_at`` after a message written at ``at`` (it never goes back)."""
    return max(float(deal.get("last_message_at") or 0), at)


def _wrote(deal: dict, chat_id: str, at: float, side: bool = False) -> dict:
    """``update_deal``'s fields after a message of hers written at ``at`` in ``chat_id``: ``last_message_at`` (any chat)
    and that chat's own time in ``last_by_chat`` (老板拉的群: a creator who writes in a group and in private has a 稍等
    round of each chat, ``send_holding_replies``).  Neither goes back.  ``side`` (她那边的人, PRM_GROUP_HER_SIDE): a line
    of her side's other people in that group: its time is kept under SIDE_SPOKE + the chat (``_spoke``, ``_her_last``)."""
    by_chat = dict(deal.get("last_by_chat") or {})
    key = (SIDE_SPOKE if side else "") + str(chat_id)
    by_chat[key] = max(float(by_chat.get(key) or 0), at)
    return {"last_message_at": _later(deal, at), "last_by_chat": by_chat}


def reach_meta(why: str) -> dict:
    """A group job's (and its packet's) ``meta.reach`` when PRM_REACH_LAST_SPOKE sent it there (``_reaching``'s why
    REACH_LAST_SPOKE): ``{"reach": "last_spoke"}``; else nothing (a group job as before)."""
    return {"reach": REACH_LAST_SPOKE} if why == REACH_LAST_SPOKE else {}


EVENT_NAMES = {
    "stage": "改阶段", "mode": "改模式", "mode_expired": "人工接管到期", "takeover": "运营亲手回复，自动接管",
    "operator_message": "运营亲手回复", "creator_message": "接管期间达人的消息", "shipment": "填运单号",
    "shipment_status": "更新物流", "holding_reply": "自动回复「稍等」", "reveal": "查看完整号码",
    "wrong": "标为发错了", "purge": "清除收件信息", "purge_case": "清除 Mac 案卷里的收件信息",
    "platform_execute": "批准执行平台动作", "platform_result": "平台动作结果",
    "platform_preview": "重新预览平台动作", "platform_ack": "平台动作已处理",
    "recall": "撤回了一条消息", "draft_failed": "草稿没写成", "next_step": "派开专属佣金的任务",
    "step_unasked": "Mac 问不到机器人，那一步过一会儿再派",
    "platform_recheck": "平台动作要先核对", "sample_status": "聚水潭寄样单状态",
    "commission_covered": "邀约卡开过专属佣金，当作开好",
    "night_send": "夜间自动发出", "night_done": "夜间自动回的过渡话已处理",
    "group_left_done": "不接的群里她没回的话已处理",              # (GROUP_LEFT_DONE)
    "opening_auto": "开场白没人处理，自动发出", GROUP_LINE: "三方群里的一句话",
    "auto_register": "自动登记（新好友）", "opening": "派写开场白的任务", "huopan": "派补发货盘的任务",
    "handcards": "派做手卡的任务",
    "signed": "聚水潭显示签收，派问一句的任务",
    "buyin_message": "百应私信", "buyin_copy": "复制百应私信草稿（含完整号码）", "buyin_sent": "运营已在百应手动发出",
    "buyin_draft_failed": "百应私信的草稿没写成",
    "friend_add_link": "关联抖音号", "creator_unlinked": "取消关联抖音号",
    GROUP_EVENT: "三方群", BOSS_EVENT: "老板拉群介绍", "draft_note": "改起草备注",
    "photo_confirm": "对过图了", "photo_unconfirm": "撤销「对过图了」",
}
# 三方群 (src/prm/three_party.py): the console's deal actions (「拉三方群」, 「先试一下」, 「建好了」, 「没建成」) and how the
# deal's record names each step
GROUP_ACTIONS = ("group", "group_try", "group_made", "group_none")
# 接手任何合作群 (src/prm/group_link.py, 2026-11-09): 「不接了」 on a group linked to the deal (``chat``; none: her 三方群):
# not drafted in any more (``unlink_group``: her 三方群 stays it, read only again; review 2026-11-09)
GROUP_UNLINK = "group_unlink"
GROUP_EVENT_NAMES = {"creating": "开始拉三方群", "created": "三方群建好了", "unsure": "三方群不确定建没建成",
                     "failed": "三方群没建成", "": "三方群的记录清掉了（没建成）"}
# 起草备注 (2026-10-31): the console's deal action, and its event (how long the note is now: never its words)
DRAFT_NOTE = "draft_note"
# PRM_DRAFT_CONTEXT (2026-11-09): 「对过图了，用 <编码>」 on a sample order the Mac held for photos, and its 「撤销」: the
# console's deal actions and their events ({code, packet}: no names).  The deal's confirmed codes go with every draft and
# new-preview job (draft_context.job_context's ``confirmed``); words in the 起草备注 confirm nothing.
PHOTO_CONFIRM = "photo_confirm"
PHOTO_UNCONFIRM = "photo_unconfirm"
WECHAT_ACTOR = "wechat"             # an event's actor: the admin's quoted 「对过图 1」 in WeChat (wechat_review.py)
# 合作单指令 (PRM_WECHAT_COMMANDS, src/prm/admin_commands.py, 2026-11-11): an operator's change from the console or typed in
# the admin's chat (``ADMIN_ACTOR``, 「接群」's too): ``deal_action`` sees to a deleted order's notice for either
OPERATOR_ACTORS = ("console", ADMIN_ACTOR)
# A creator's message recorded without a draft because it was read too late (PRM_CATCHUP_HOURS).
MISSED_EVENT_NAME = "机器人没在读时达人的消息（没有起草）"
# PRM_AUTO_REGISTER (2026-10-29): the deal's event of a new friend the bot registered, and how she was added (the
# new-friend scan's ``how``) as her note and the event say it.
AUTO_EVENT = "auto_register"
# prm_meta: the new friends ``new_friend_found`` queued that ``register_found`` has not seen to yet (a JSON list of
# {wxid, name, how}), kept across a restart (``resume_found``; review 2026-10-30)
AUTO_QUEUE = "auto_register_queue"
AUTO_HOWS = {"requested": "我们加她", "manual": "她加我们", "auto_accept": "机器人自动通过", "unknown": "说不清"}
AUTO_NAME_CHARS = 30         # a creator's 称呼 (``register``)
AUTO_NOTE_CHARS = 200        # her note
# 开场白 (2026-10-30): the job's ``meta.opening`` ({"how": the new-friend scan's, "by": new_friend | console}), the
# packet's ``meta.opening``, the deal's event (``OPENING``: which job, how, by) and the console's deal action
# (「写开场白」, ``ask_opening``).
OPENING = "opening"
# ... a 开场白 the admin ordered in WeChat (合作单指令's 「写开场白 <达人>」, src/prm/admin_commands.py; decision 2026-11-11):
# its job's ``meta.opening.held``; it never goes out by itself (no PRM_OPENING_AUTO_MIN, 下班时段 or 自动模式: ``_opening_auto``)
OPENING_HELD = "held"
# A new friend registered more than PRM_CATCHUP_HOURS after she was added, with nothing of hers in the read-back: what
# she wrote before it is not known, so no 开场白 by itself; this notice (``opening`` too) asks a person to look (review
# 2026-10-30).  A 开场白 asked for, a reply draft of hers, or an operator writing to her by hand sees to it.
OPENING_UNSURE = "opening_unsure"
OPENING_UNSURE_TEXT = ("她是加好友过了很久才登记的，超出了补读的时间（PRM_CATCHUP_HOURS）：补读的这段里她没说话，更早的不知道，"
                       "所以没自动写开场白；看一眼微信，她还没说过话就在合作单里点「写开场白」")
# 补发货盘 (2026-11-04, the user: a creator we talked to went quiet, send her the 货盘 again and ask what she thinks): the
# console's deal action (「补发货盘」, ``ask_huopan``), the job's ``meta.huopan`` ({"by": "console"}), the packet's
# ``meta.huopan`` (the console's tag; no holding reply for it unless a line of hers joined the job), the deal's event
# (``HUOPAN``: which job) and the notice of one the Mac did not write (``huopan``).  Its draft always waits for a person
# (``gap_notes``).
HUOPAN = "huopan"
# 直播手卡 (PRM_HANDCARDS, 2026-11-09): the console's deal action (「做手卡」, ``ask_handcards``), the job's
# ``meta.handcards`` ({"by": "console", "items": [{"item_id", "sku_ids"}], "order"?}), the packet's ``meta.handcards``
# (``handcards.packet_meta`` of the Mac's verdict, ``console`` for a 做手卡's), the deal's event (which job) and the
# notice of one the Mac did not make (``handcards``).
HANDCARDS = "handcards"
SAMPLE_TOOL = "jst.sample_order"
# A second sample order for one deal (review 2026-11-10 A-L5: 接群 gives her a private and a group lane, each may have one
# waiting, and both ran): approving one while another of the deal runs, was verified within this many days or waits in
# 待审批 too is refused once, until an operator says 「我已处理」 on that one (``_second_order``; a 补寄 stays possible)
SECOND_ORDER_DAYS = 7
# 签收后问一句 (PRM_SIGNED_HELLO, 2026-11-07, the user: 「可以加」; kb 场景 D2 「聚水潭显示签收」): a sample order the 聚水潭 sync
# sees signed for (the bot's own, or ours made by hand whose waybill went into her deal) while her deal had not been
# marked 已签收 by a person: one draft job with no line of hers asks her how the samples suit her (``meta.signed``
# {"order", "at"}: the order and its signature as MM-DD HH:MM; the worker's ``signed_line``).  Each signature is noted
# once (store ``signed_hellos``: owed / asked / skipped) and asked for only inside PRM_SIGNED_HELLO_HOURS (this PC's
# clock) while her chat allows (on the list, copilot mode, reachable, no draft of hers being written or waiting: owed
# till then, ``sweep_signed_hellos``); none when she wrote after the signature, the deal moved on, or the signature is
# SIGNED_HELLO_DAYS old.  Its draft never goes by its kind's gate: by day (outside the 下班时段, 自动模式 off) it always
# waits for a person, the draft and the deal saying why (``SIGNED_DAY_NOTE``; the user was told 「白天进待审批，你点了
# 才发」); in the 下班时段 or 自动模式 the night's rules judge it like a reply draft (``_night_judge``).  The packet's
# ``meta.signed`` (the order), the deal's event (``SIGNED``), the notice of one not written (``signed``); no holding
# reply for it unless a line of hers joined the job; her message after it outdates it.
SIGNED = "signed"
SIGNED_HELLO_DAYS = 3                       # a signature this old is not asked about (a first round, a long outage)
SIGNED_HELLO_HOURS = "10:00-20:00"          # PRM_SIGNED_HELLO_HOURS when unset (or not HH:MM-HH:MM)
SIGNED_DAY_NOTE = "签收后问一句：白天要人看过再发"
# 老板拉的群 (PRM_BOSS_GROUPS, src/prm/boss_groups.py): what the bot starts itself (a 专属佣金 step, a shipping notice, a
# 开场白 from the console) goes to her private chat, or to the group when she has none (``_reach``); with neither (she
# left the group) nothing is sent and the deal's notice (``UNREACHABLE``) says so until she writes to us again.
UNREACHABLE = "unreachable"
UNREACHABLE_TEXT = "她不是我们的微信好友，也不在老板拉的群里了：机器人发不了消息给她"
# ... PRM_REACH_LAST_SPOKE (off, the default; 2026-11-11, a real case: a creator's team talks only in their group, the
# deal also has an old private chat, 「做手卡」's cards went to the private chat where nobody looks): "on": what the bot
# starts itself goes to a linked group she is still in (one it drafts in: never a deal's read-only 三方群) when the
# latest line of her side on the deal (hers; with PRM_GROUP_HER_SIDE on her side's too: ``last_by_chat``) was in that
# group; her latest line in her private chat, or none at all: as before.  Decided when the message is made
# (``_reaching`` ``made``): a job is drafted for the chat it goes to, a shipping notice (no job) once, the first time it
# is aimed (``_aim``: its ``meta.reach``); it never moves after that but by the rule above (she has no private chat).
# A job and its packet sent to the group by this rule say so (``meta.reach`` REACH_LAST_SPOKE: the console's why); one
# that stays in her private chat by it, a shipping notice, ``meta.reach`` REACH_PRIVATE (decided: not again).
REACH_PRIVATE = "private"                 # (REACH_LAST_SPOKE: src/prm/boss_groups.py, wechat_review reads it too)
# ... her side's other people's lines (PRM_GROUP_HER_SIDE) are kept apart in ``last_by_chat`` (this + the group's id):
# they count for where her side last spoke only while that switch is on (``_spoke``), her own lines are the chat's key
SIDE_SPOKE = "side:"
# ... and a draft that did not go out to such a group (a refusal: the window does not know the group's name, a namesake):
# the deal says so until a later one goes out there
GROUP_UNSENT = "boss_group_unsent"
# ... and a draft for such a group while PRM_BOSS_GROUPS is off (made while it was on): never sent, it fails with this
BOSS_GROUPS_OFF_TEXT = "老板拉的群关了，没发"
# 接手任何合作群 (src/prm/group_link.py, 2026-11-09): of her lines waiting for an answer when an operator links a group by
# hand, the newest this many are one draft job (``_link_catch_up``); the older ones are context
LINK_TAIL = 5
# ... a draft for a group the bot does not draft in any more (「不接群」, 「不接了」, 「群已经不在了」: the link ended) never goes
# out: it fails with this (review 2026-11-09: one made before went into the group all the same; ``_group_closed``)
GROUP_CLOSED_HEAD = "群已经不接了"
GROUP_CLOSED_TEXT = f"{GROUP_CLOSED_HEAD}，没发"
# ... in a group linked by hand a line of anybody but her and the boss has her waiting group draft drafted again at most
# this often (a big group: not a Mac job per line); one that comes sooner is owed till then (``sweep_owed_redrafts``)
REDRAFT_GAP_SEC = 120
# ... and at most this many times while one draft of hers waits (review 2026-11-09: a chatty group made a Mac job every
# REDRAFT_GAP_SEC all the same); after that her waiting draft's card says how many lines it did not hear (``unheard``),
# and the console's 「重新起草」 or her next line drafts it again
REDRAFT_MAX = 3
# ... her lines in a group whose link ended before they were answered are never carried into a draft of another chat
# (review 2026-11-09: they were answered in her private chat, by its gate unreviewed): the deal's notice says how many
# wait there (this + the group's chat id), until 「已处理」 (the deal action GROUP_LEFT_DONE) or her next line there
GROUP_LEFT = "group_left:"
GROUP_LEFT_DONE = "group_left_done"
# ... a recall under a name we do not know is hers only when she wrote in that group lately (WeChat takes a line back
# within 2 minutes; a little more for the reading)
HER_RECALL_SEC = 180
# ... (decision 2026-11-10, reviews A D1 and C): everybody in a group linked by hand reads what goes there, so its drafts
# always wait for a person: no kind gate, 下班时段, 自动模式, 开场白自动发 or 签收后问一句 sends one (``_hand_hold``; a
# 老板拉的群's keep their way).  PRM_LINKED_GROUPS_AUTO=on: they go as a 老板拉的群's, but for the draft of the lines that
# waited at 接群 (its job's ``meta.link_first``: the 接群 answer said 「在待审批里等你看」), which always waits.
LINK_FIRST = "link_first"
HAND_GROUP_HELD = "运营手动接的群：草稿一律等你看过再发（群里的人都看得到；PRM_LINKED_GROUPS_AUTO 关着）"
LINK_FIRST_HELD = "接群时她在等我们回的那几句：这份草稿等你看过再发"
# ... a group the scan linked by itself (群自己接上, PRM_GROUPS_AUTO_LINK): the same rule, said in its own words
AUTO_GROUP_HELD = "机器人自动接的群：草稿一律等你看过再发（群里的人都看得到；PRM_LINKED_GROUPS_AUTO 关着）"
# ... (review 2026-11-11 #2) and until a person confirmed it (a draft there sent by a person, 「确认接群」, the console's
# 「是她的群」: ``BossGroups.confirm``), whatever PRM_LINKED_GROUPS_AUTO says
AUTO_UNCONFIRMED_HELD = ("机器人自动接的群，还没人确认是她的群：草稿一律等你看过再发（你发出去一份、回「确认接群 群名」或者在合作单上点"
                         "「是她的群」以后，照「手动接的群也自动发」）")
# 她那边的人 (PRM_GROUP_HER_SIDE, review 2026-11-11 #1): her side is anybody there not known to be ours, so a draft whose
# job holds a line of theirs (HER_SIDE_MARK) always waits for a person: no gate, 下班时段, 自动模式, PRM_LINKED_GROUPS_AUTO
HER_SIDE_HELD = "她那边的人（不是登记的那个号）说的话：机器人不能确定这个人是谁，这份草稿一律等你看过再发"
# 老板拉的群里机器人自己选人 (PRM_BOSS_GROUP_PICK, 2026-11-13): a 老板拉的群 the scan took over by choosing whom the deal hangs
# on, until a person confirms it (as a group the scan linked by itself: ``BossGroups.unconfirmed``)
PICK_UNCONFIRMED_HELD = ("机器人自己选人接手的老板拉的群，还没人确认：草稿一律等你看过再发（你发出去一份、回「确认接群 群名」或者在合作单上"
                         "点「是她的群」以后，照老板拉的群的规矩）")
# 群里的话都先审 (PRM_GROUPS_REVIEW, 2026-11-13; the user: 「群里的消息都经过审批再发…私信依旧可以自动回复」): every draft for a
# group chat waits for a person, whatever the gates, 下班时段, 自动模式, 开场白自动发, 签收后问一句, PRM_LINKED_GROUPS_AUTO say
GROUPS_REVIEW_HELD = "发在群里的话一律先审（「群里的话都先审」PRM_GROUPS_REVIEW 开着）；私聊照旧"
# ... the hold reasons a draft's card shows by day too (``meta.hold``; the others are said as before)
SHOWN_HOLDS = (AUTO_UNCONFIRMED_HELD, HER_SIDE_HELD, PICK_UNCONFIRMED_HELD, GROUPS_REVIEW_HELD)
GROUP_CONFIRM = "group_confirm"            # the deal action of the console's 「是她的群」 ({chat})
# ... and in the lines it sends the admin's chat (``_auto_link_notice``), a group nobody named
NO_GROUP_NAME = "一个没有群名的群"


def auto_name(name, wxid: str) -> str:
    """The 称呼 PRM_AUTO_REGISTER gives a new friend: her WeChat name as the scan read it, whitespace (and U+3164
    fillers, a blank-looking nickname) collapsed, at most 30 characters; 「新好友 <wxid 前 8 位>」 without one."""
    text = " ".join(str(name or "").replace(FILLER, " ").split())[:AUTO_NAME_CHARS].strip()
    return text or f"新好友 {str(wxid or '')[:8]}"


def auto_note(how: str) -> str:
    """Her note: 「自动登记（新好友，<how>）」 (``AUTO_HOWS``; an unknown ``how`` is 说不清)."""
    return f"自动登记（新好友，{AUTO_HOWS.get(str(how or ''), AUTO_HOWS['unknown'])}）"[:AUTO_NOTE_CHARS]


def event_out(ev: dict) -> dict:
    """An event for the console: what happened, by whom, when (texts kept as stored: masked)."""
    detail = dict(ev.get("detail") or {})
    name = (MISSED_EVENT_NAME if ev["kind"] == "creator_message" and detail.get("missed")
            else EVENT_NAMES.get(ev["kind"], ev["kind"]))
    if ev["kind"] == "recall":
        name = "运营撤回了一条消息" if detail.get("by") == "operator" else "达人撤回了一条消息"
    elif ev["kind"] == "draft_failed" and detail.get("job"):
        name = f"#{detail['job']} 草稿没写成"
    elif ev["kind"] == "next_step" and detail.get("job"):
        name = (f"派{'建专属链接' if detail.get('action') == LINK_ACTION else '开专属佣金'}的任务 #{detail['job']}"
                + (f"（商品 {detail['item']}）" if detail.get("item") else ""))
    elif ev["kind"] == "step_unasked" and detail.get("job"):
        name = (f"#{detail['job']} 那一步没起草成：Mac 问不到 Windows 上的机器人（第 {detail.get('tries') or 1} 次）"
                + (f"（商品 {detail['item']}）" if detail.get("item") else ""))
    elif ev["kind"] == "platform_recheck" and detail.get("packet") and detail.get("second"):
        name = f"#{detail.get('for')} 要给她再建一张寄样单（#{detail['packet']} 已经有了）：先问过运营"
    elif ev["kind"] == "platform_recheck" and detail.get("packet"):
        name = f"#{detail['packet']} 的结果不确定，#{detail.get('for')} 要再开同一个商品：先核对平台"
    elif ev["kind"] == "sample_status" and detail.get("order"):
        status = str(detail.get("status") or "")
        name = (f"聚水潭寄样单 {detail['order']}：{platforms.SAMPLE_STATES.get(status, status)}"
                + (f"（{detail['waybill']}）" if detail.get("waybill") else "")
                + (f"，原因：{detail['reason']}" if detail.get("reason") else ""))
    elif ev["kind"] == "commission_covered" and detail.get("item"):
        name = f"商品 {detail['item']} 邀约卡开过 {detail.get('rate')}% 专属佣金，当作开好"
    elif ev["kind"] == "night_send" and detail.get("packet"):
        name = (f"{'自动模式发出' if detail.get('auto') else '夜间自动发出'} #{detail['packet']}"
                + ("（模型要求注意，发的是一句过渡的话）" if detail.get("holding") else ""))
    elif ev["kind"] == "night_done" and detail.get("packet"):
        name = f"{'自动模式' if detail.get('auto') else '夜间'}自动回的过渡话 #{detail['packet']} 已处理"
    elif ev["kind"] == "wrong" and detail.get("night"):
        name = "标为发错了（自动模式发出的）" if detail.get("auto") else "标为发错了（夜间自动发出的）"
    elif ev["kind"] == AUTO_EVENT:
        name = f"自动登记（新好友，{AUTO_HOWS.get(str(detail.get('how') or ''), AUTO_HOWS['unknown'])}）"
    elif ev["kind"] == OPENING and detail.get("job"):
        name = f"派写开场白的任务 #{detail['job']}" + ("（运营点的）" if detail.get("by") == "console" else
                                                     "（老板拉的群）" if detail.get("by") == BOSS_OPENING else
                                                     "（新好友还没说话）")
    elif ev["kind"] == HUOPAN and detail.get("job"):
        name = f"派补发货盘的任务 #{detail['job']}（运营点的）"
    elif ev["kind"] == HANDCARDS and detail.get("job"):
        name = (f"派做手卡的任务 #{detail['job']}（运营点的，"
                + (f"{detail['items']} 款）" if detail.get("items") else "按案卷里她选的款）"))
    elif ev["kind"] == BOSS_EVENT and detail.get("gone") and detail.get("read_only"):
        name = "不接这个群了（运营说的），群里她的消息不再起草：它还是她的三方群，机器人只读"
    elif ev["kind"] == BOSS_EVENT and detail.get("gone") and detail.get("stopped") and not detail.get("manual"):
        name = "老板拉的群不接了（运营说的），群里她的消息不再起草"
    elif ev["kind"] == BOSS_EVENT and detail.get("picked") and detail.get("confirmed"):
        # 老板拉的群里机器人自己选人 (PRM_BOSS_GROUP_PICK): a person says the group the scan took over so is hers
        name = ("确认了机器人自己选人接手的老板拉的群" + (f"（{detail['name']}）" if detail.get("name") else "")
                + ("：运营发出去了一份草稿" if detail.get("how") == "sent" else
                   "：运营改挂了合作单" if detail.get("how") == "rehook" else ""))
    elif ev["kind"] == BOSS_EVENT and detail.get("picked") and detail.get("rehooked"):
        name = ("合作单改挂在群里她那边的另一个人身上" + (f"（{detail['name']}）" if detail.get("name") else "")
                + ("：原来挂的那个人退群了" if detail.get("rehooked") == REHOOK_LEFT else "：运营改的"))
    elif ev["kind"] == BOSS_EVENT and detail.get("picked") and not detail.get("gone"):
        name = ("老板拉群介绍" + (f"（{detail['name']}）" if detail.get("name") else "")
                + f"：机器人自己选人接手（{PICK_HOW.get(str(detail['picked']), '群里不止一个人可能是她')}）"
                + ("，她原来的三方群保留" if detail.get("kept") else ""))
    elif ev["kind"] == BOSS_EVENT and detail.get("auto") and detail.get("confirmed"):   # 群自己接上: a person says so
        name = ("确认了自动接的群" + (f"（{detail['name']}）" if detail.get("name") else "")
                + ("：运营发出去了一份草稿" if detail.get("how") == "sent" else "") + "，之后的草稿照「手动接的群也自动发」")
    elif ev["kind"] == BOSS_EVENT and detail.get("manual") and detail.get("auto"):   # 群自己接上: the scan's own link
        name = ("自动接的群不接了（运营说的），群里她的消息不再起草" if detail.get("gone") else
                "自动接的群" + (f"（{detail['name']}）" if detail.get("name") else "")
                + f"：{detail.get('why') or AUTO_WHY}" + ("，她原来的三方群保留" if detail.get("kept") else ""))
    elif ev["kind"] == BOSS_EVENT and detail.get("manual"):      # 接手任何合作群: a group an operator linked by hand
        name = ("手动接的群不接了（运营说的），群里她的消息不再起草" if detail.get("gone") else
                "手动接的群" + (f"（{detail['name']}）" if detail.get("name") else "")
                + ("，她原来的三方群保留" if detail.get("kept") else ""))
    elif ev["kind"] == PHOTO_CONFIRM and detail.get("code"):
        name = (f"对过图了，用 {detail['code']}" + (f"（#{detail['packet']} 的寄样单）" if detail.get("packet") else "")
                + ("（在微信里回的）" if ev.get("actor") == WECHAT_ACTOR else ""))
    elif ev["kind"] == "platform_preview" and detail.get("auto"):
        name = "扣着的帽子都对过图了：自动重新预览"
    elif ev["kind"] == PHOTO_UNCONFIRM and detail.get("code"):
        name = f"撤销了 {detail['code']} 的「对过图了」"
    elif ev["kind"] == BOSS_EVENT:
        name = ("老板拉的群不在了（运营说的），群里她的消息不再起草" if detail.get("gone") else
                "老板拉群介绍" + (f"（{detail['name']}）" if detail.get("name") else "")
                + ("，她原来的三方群保留" if detail.get("kept") else ""))
    elif ev["kind"] == GROUP_LINE:
        name = f"三方群里（{detail.get('label') or GROUP_WHO_LABELS.get(str(detail.get('who') or ''), '群里的人')}）"
    elif ev["kind"] == "buyin_message":
        name = "百应私信：我方（运营在百应里回的）" if detail.get("from") == "shop" else "百应私信：达人"
    elif ev["kind"] == GROUP_EVENT:
        name = GROUP_EVENT_NAMES.get(str(detail.get("state") or ""), "三方群") + (
            "（运营点的）" if ev.get("actor") == "console" and detail.get("state") in ("creating", "created", "") else "")
    elif ev["kind"] == "creator_unlinked" and detail.get("account"):
        name = f"取消关联抖音号 {detail['account']}"
    elif ev["kind"] == DRAFT_NOTE:
        name = f"改起草备注（{detail['chars']} 字）" if detail.get("chars") else "清掉起草备注"
    elif ev["kind"] == "friend_add_link" and detail.get("account"):
        name = (f"关联抖音号 {detail['account']}" + (f"（加微信 #{detail['add_id']}）" if detail.get("add_id") else "")
                + (f"，{str(detail['invited_at'])[:16].replace('T', ' ')} 发的邀约卡" if detail.get("invited_at") else ""))
    elif ev["kind"] in ("shipment", "shipment_status", "stage") and detail.get("source") == "jst":
        name = f"{name}（聚水潭同步{' · 手动建的单' if detail.get('hand') else ''}）"
    return {"id": ev["id"], "kind": ev["kind"], "name": name,
            "actor": ev.get("actor", ""), "at": ev["at"], "detail": detail}


def platform_out(p: dict) -> Optional[dict]:
    """A packet's platform action for the console (None for a plain reply): what it will do,
    the Mac's preview, the execution and whether it can be approved now.  A 百应 draft is none."""
    if p.get("tool", MESSAGE_TOOL) in (MESSAGE_TOOL, BUYIN_TOOL):
        return None
    data = p.get("platform") or {}
    action = data.get("action")
    preview = data.get("preview") or {}
    execution = data.get("execution") or None
    verdict = preview.get("verdict") or "unavailable"
    name = (action or {}).get("action") or p.get("tool")
    return {
        "name": name, "label": platforms.label(name), "platform": (platforms.ACTIONS.get(name) or {}).get("platform"),
        "effect": (platforms.ACTIONS.get(name) or {}).get("effect", ""),
        "caveat": (platforms.ACTIONS.get(name) or {}).get("caveat", ""),
        "makes_link": bool((platforms.ACTIONS.get(name) or {}).get("link")),
        "placeholders": [{"name": k, "label": platforms.PLACEHOLDER_LABELS.get(k, k)}
                         for k in platforms.REPLY_FIELDS.get(name, {})],
        "params": platforms.describe(action, preview.get("catalog")),
        "requested": preview.get("requested"),
        "verdict": verdict, "verdict_label": platforms.VERDICTS.get(verdict, verdict),
        "problems": preview.get("problems") or [], "facts": preview.get("facts") or [],
        "steps": preview.get("steps") or [], "previewed_at": preview.get("previewed_at"),
        "previewing": bool(data.get("previewing")),
        "can_execute": p["status"] == PENDING and verdict == "ready" and bool(action) and not data.get("previewing"),
        "approved_at": data.get("approved_at"), "job_id": data.get("job_id"), "lost_at": data.get("lost_at"),
        "execution": None if execution is None else {
            "outcome": execution.get("outcome"),
            "outcome_label": platforms.outcome_text(execution.get("outcome") or "UNKNOWN"),
            "problems": execution.get("problems") or [], "facts": execution.get("facts") or [],
            "created": execution.get("created") or {}, "steps": execution.get("steps") or [],
            "late": bool(execution.get("late")), "finished_at": execution.get("finished_at"),
        },
        "acked_at": data.get("acked_at"),
        "recheck_for": (data.get("recheck") or {}).get("for"),
    }


def gates_out(store: PrmStore) -> list[dict]:
    out = []
    for kind, gate in store.gates().items():
        stats = gates.window_stats(store.reviewed(kind, gate["reset_at"], gate["window"]))
        out.append({"kind": kind, "label": MESSAGE_KINDS.get(kind, kind), "route": gate["route"],
                    "locked": bool(gate["locked"]), "window": gate["window"], "min_unchanged": gate["min_unchanged"],
                    **stats})
    return out


HAND_PLATFORM_LABELS = {"DY": "抖音", "XHS": "小红书", "KS": "快手", "WXSPH": "视频号"}


def hand_samples_out(store: PrmStore, limit: int = HAND_LINES) -> list:
    """The 「待审批」 lines of our hand-made sample orders not seen to yet (``listed``, the newest first): the order,
    whose (her nickname as 聚水潭 gives it and her platform; the registered creator's name when one fits), the courier
    and waybill, when it was sent, why it did not go into a deal."""
    parties = {}
    out = []
    for row in store.hand_samples(shown=True, limit=limit):
        name = ""
        if row.get("party_id"):
            if row["party_id"] not in parties:
                parties[row["party_id"]] = store.party(int(row["party_id"])) or {}
            name = parties[row["party_id"]].get("name", "")
        out.append({"order_no": row["order_no"], "platform": row["platform"],
                    "platform_label": HAND_PLATFORM_LABELS.get(row["platform"], row["platform"] or ""),
                    "nickname": row["nickname"], "party_name": name, "carrier": row["carrier"],
                    "waybill": row["waybill"], "sent_at": row["sent_at"], "why": row["why"]})
    return out


def snapshot(store: PrmStore, *, running: bool, extra: Optional[dict] = None, limit: int = 40,
             retention_days: int = retention.RETENTION_DAYS, now: Optional[float] = None,
             group_of: Callable[[dict], dict] = group_state_out, photo_check: bool = False,
             reach_of: Optional[Callable[[dict, dict], Optional[dict]]] = None) -> dict:
    """What the console shows (GET /api/prm); also built from the database when the bot is stopped.
    Numbers appear only masked (``legend``); the full ones need ``reveal``.  ``night_alerts``: the deals' notices
    of holding lines the night window (``auto``: 自动模式 by day) sent (``NIGHT_NOTICE``), each until 「已处理」
    (``night_done``); ``group_alerts``: her lines in a group that closed before they were answered (``GROUP_LEFT``),
    each until 「已处理」 (``group_left_done``).  ``group_of(deal)``: a deal's 三方群 (``three_party.state_out``; the
    running bot's adds its last 「先试一下」).  ``photo_check`` (PRM_DRAFT_CONTEXT on): a pending sample order the Mac did
    not check by photo (``draft_context.unchecked``), or whose last preview went by a 「对过图了」 taken back since
    (``withdrawn``), shows 「批准并执行」 off and why; its ``photo_held`` lists the hats the Mac held for photos
    (``held_out``: the console's 「对过图了」 and 「撤销」).  ``reach_of(party, deal)`` (the running bot's, with
    PRM_REACH_LAST_SPOKE on: PrmService ``_reach_out``): where what the bot starts itself for her would go now and why,
    a deal's ``reach`` (only for a creator with a linked group; never from the stopped console)."""
    now = time.time() if now is None else now
    parties = {p["id"]: p for p in store.parties(include_inactive=True)}
    newest = {}
    night_alerts, group_alerts = [], []
    for deal in store.deals():
        newest[deal["party_id"]] = deal                # the newest deal of each creator
        for n in deal["notices"]:
            if str(n.get("key") or "").startswith(NIGHT_NOTICE) and n.get("text"):
                night_alerts.append({"deal_id": deal["id"], "party_id": deal["party_id"],
                                     "party_name": (parties.get(deal["party_id"]) or {}).get("name", "?"),
                                     "packet": n.get("packet"), "text": n["text"], "at": n.get("at"),
                                     "auto": bool(n.get("auto"))})
            elif str(n.get("key") or "").startswith(GROUP_LEFT) and n.get("text"):
                group_alerts.append({"deal_id": deal["id"], "party_id": deal["party_id"],
                                     "party_name": (parties.get(deal["party_id"]) or {}).get("name", "?"),
                                     "chat": str(n["key"])[len(GROUP_LEFT):], "text": n["text"], "at": n.get("at")})
    pending = list(reversed(store.packets((PENDING, EXECUTING, APPROVED, SENDING), limit=200)))
    recent = store.packets((SENT, FAILED, DROPPED, SUPERSEDED, NO_REPLY), limit=limit)
    # 微信里审批草稿: when each was told to the admin's chat, and what can be done for it from there (the 「已发到微信」
    # tag's words): ``wechat_console``, its pictures did not reach the chat (only 不发 there); ``wechat_execute``, a
    # platform draft 「执行」 runs from WeChat (the preview it was told with, still approvable, its pictures there);
    # ``wechat_photo``, a sample order whose held hats an answer quoting its notice confirms (「对过图 1」, 2026-11-10)
    told = store.wechat_review_states(p["id"] for p in pending)
    shown_cache: dict[int, dict] = {}
    confirmed_cache: dict[int, dict] = {}            # PRM_DRAFT_CONTEXT: each deal's 「对过图了」 codes
    # 老板拉的群 (src/prm/boss_groups.py): the creators a boss introduced in a group the bot took over (「老板介绍」, also
    # once the group is gone); a group linked by hand (接手任何合作群) is no introduction
    boss_parties = {int(r["party_id"]) for r in store.boss_groups((BOSS_LINKED, BOSS_GONE))
                    if r.get("party_id") and not r.get("manual")}
    # ... and the groups linked by hand, by deal: the ones that are not her deal's 三方群 are listed with it
    by_hand: dict[int, list] = {}
    for r in store.boss_groups((BOSS_LINKED,)):
        if r.get("manual") and r.get("deal_id"):
            by_hand.setdefault(int(r["deal_id"]), []).append(
                {"chat": r["chat"], "name": r["name"], "at": r["found_at"],
                 **({"auto": True} if r.get("manual") == GROUP_AUTO else {})})   # (群自己接上: the scan linked it)

    def shown(party_id: int) -> dict:
        if party_id not in shown_cache:
            shown_cache[party_id] = pii.display(store.token_rows(party_id))
        return shown_cache[party_id]

    def confirmed_of(deal_id: int) -> dict:
        if deal_id not in confirmed_cache:
            confirmed_cache[deal_id] = {str(r["code"]): r["at"] for r in store.photo_confirmations(deal_id)}
        return confirmed_cache[deal_id]

    def packet_out(p: dict) -> dict:
        party = parties.get(p["party_id"]) or {}
        final = p["final_text"] if p.get("final_text") is not None else p["text"]
        platform = platform_out(p)
        held: list = []
        if photo_check and p.get("tool") == "jst.sample_order" and p["status"] == PENDING:
            # PRM_DRAFT_CONTEXT: the hats its last preview held for photos (「对过图了，用 <编码>」) or let go only by a
            # confirmation (「撤销」); one taken back holds the draft until a new preview
            held = draft_context.held_out(p, confirmed_of(p["deal_id"]))
            gone = draft_context.withdrawn(p, confirmed_of(p["deal_id"]))
            if platform and platform["can_execute"] and gone:
                platform = {**platform, "can_execute": False,
                            "problems": [*platform["problems"], draft_context.withdrawn_reason(gone)]}
        if (p.get("tool") == "jst.sample_order" and p["status"] == PENDING and platform and platform["can_execute"]
                and draft_context.sheets_gone(p)):
            # its 对图单 gone from this PC: off, why, as ``_approve_execute`` refuses it (final check 2026-11-10 B)
            platform = {**platform, "can_execute": False, "problems": [*platform["problems"], draft_context.SHEET_GONE]}
        if p.get("tool") == "jst.sample_order" and p["status"] == PENDING and platform and platform["can_execute"]:
            # a hat its last preview held that nobody confirmed since: off, why, the switch on or off, as
            # ``_approve_execute`` refuses it (review 2026-11-10 A-L4; 合并 fix1110-s: the switch off too)
            waiting = draft_context.held_unconfirmed(p, confirmed_of(p["deal_id"]))
            if waiting:
                platform = {**platform, "can_execute": False,
                            "problems": [*platform["problems"], draft_context.held_reason(waiting, photo_check)]}
        if photo_check and platform and platform["can_execute"] and draft_context.unchecked(p):
            platform = {**platform, "can_execute": False,
                        "problems": [*platform["problems"], draft_context.unchecked_reason(p)]}
        return {
            "id": p["id"], "deal_id": p["deal_id"], "party_id": p["party_id"],
            "party_name": party.get("name", "?"), "job_id": p["job_id"], "status": p["status"],
            "kind": p["kind"], "kind_label": MESSAGE_KINDS.get(p["kind"], p["kind"]), "route": p["route"],
            "text": p["text"], "final_text": p.get("final_text"), "edited": p["edited"], "wrong": p["wrong"],
            "legend": pii.legend("\n".join([p["text"], final, p["intent"], p["reason"],
                                            json.dumps(p["slots"], ensure_ascii=False),
                                            json.dumps((p.get("platform") or {}).get("action") or {},
                                                       ensure_ascii=False), str(p["meta"].get("said") or "")]),
                                 shown(p["party_id"])),
            "attachments": [{"name": a.get("name"), "size": a.get("size")} for a in p["attachments"]],
            "intent": p["intent"], "slots": p["slots"], "stage_proposal": p["stage_proposal"],
            "stage_proposal_label": STAGES.get(p["stage_proposal"], ""),
            "needs_attention": p["needs_attention"], "reason": p["reason"],
            "waybill": p["meta"].get("waybill", ""), "system": p["meta"].get("source") == "system",
            "outcome": p["outcome"], "error": p["error"],
            "created_at": p["created_at"], "decided_at": p["decided_at"], "sent_at": p["sent_at"],
            "tool": p.get("tool", MESSAGE_TOOL), "platform": platform,
            "night_held": str(p["meta"].get("night_held") or ""), "opening": bool(p["meta"].get(OPENING)),
            "huopan": bool(p["meta"].get(HUOPAN)), "signed": bool(p["meta"].get(SIGNED)),
            "handcards": ({**p["meta"][HANDCARDS], "expired": cards_expired(p, now)}
                          if isinstance(p["meta"].get(HANDCARDS), dict) else None),
            "channel": BUYIN_CHANNEL if is_buyin(p) else GROUP_CHANNEL if p["meta"].get("channel") == GROUP_CHANNEL
            else "wechat", "group_name": str(p["meta"].get("group_name") or ""), "buyin": buyin_packet_out(store, p),
            "auto_mode": bool(p["meta"].get(AUTO_MODE)),
            "auto_at": p["meta"].get("auto_at") if isinstance(p["meta"].get("auto_at"), (int, float)) else None,
            "auto_held": str(p["meta"].get("auto_held") or ""),
            **({"group_manual": True} if p["meta"].get("group_manual") else {}),   # 接手任何合作群: no 老板 need be there
            **({"hold": str(p["meta"]["hold"])} if p["meta"].get("hold") else {}),   # (review 2026-11-11 #1, #2)
            # ... PRM_REACH_LAST_SPOKE: one the bot started goes to the group because her side last spoke there
            **({"reach": REACH_LAST_SPOKE} if p["meta"].get("channel") == GROUP_CHANNEL
               and p["meta"].get("reach") == REACH_LAST_SPOKE else {}),
            # ... the group's lines her waiting draft did not hear (REDRAFT_MAX automatic redrafts done)
            **({"unheard": int(p["meta"]["unheard"])} if isinstance(p["meta"].get("unheard"), int)
               and p["meta"]["unheard"] > 0 else {}),
            "wechat_at": (told.get(p["id"]) or {}).get("notified_at"),
            **({"wechat_console": not told[p["id"]]["pictures_ok"]} if p["id"] in told else {}),
            **({"wechat_execute": bool(told[p["id"]]["pictures_ok"] and told[p["id"]]["preview"] and platform
                                       and platform["can_execute"] and told[p["id"]]["preview"]
                                       == preview_stamp((p.get("platform") or {}).get("preview")))}
               if p["id"] in told and platform is not None else {}),
            **({"wechat_photo": True} if p["id"] in told and told[p["id"]]["pictures_ok"] and any(
                hat["ok"] and h["code"] == hat["code"] and h["held"] and not h["confirmed"]
                for hat in hats_of(told[p["id"]].get("hats")) for h in held) else {}),
            "operator_files": draft_context.operator_files_out(p),    # PRM_DRAFT_CONTEXT: the operators' only
            "photo_held": held,
        }

    def unanswered(deal: dict) -> bool:
        """She wrote but never had a real line from us (a 稍等 aside, ``has_answer``) and no WeChat draft of hers waits
        in 待审批: the console offers 「写开场白」 all the same (2026-11-02; a draft being written on the Mac is not seen
        here: asked for then, ``_no_opening`` says so)."""
        if not deal.get("last_message_at") or store.has_answer(deal["id"]):
            return False
        return not any((p.get("meta") or {}).get("lane") not in (LANE_SYSTEM, LANE_BUYIN)
                       for p in store.deal_packets(deal["id"], statuses=(PENDING, APPROVED, SENDING)))

    def party_out(p: dict) -> dict:
        deal = newest.get(p["id"])
        source = "boss_group" if p["id"] in boss_parties else ""
        if not deal:
            return {"id": p["id"], "wxid": p["wxid"], "name": p["name"], "note": p["note"],
                    "active": bool(p["active"]), "auto": bool(p.get("auto_registered")),
                    "authorized_at": p["authorized_at"], "source": source, "deal": None}
        mode = deals.effective_mode(deal, now)
        shipment = store.latest_shipment(deal["id"])
        slots = deal.get("slots", {})
        return {
            "id": p["id"], "wxid": p["wxid"], "name": p["name"], "note": p["note"],
            "active": bool(p["active"]), "auto": bool(p.get("auto_registered")), "authorized_at": p["authorized_at"],
            "has_numbers": bool(store.token_rows(p["id"])),       # cleared ones too: shown as （已清除）
            "source": source,
            "deal": {
                "source": source,
                "links": links_out(store, p["id"]),
                "id": deal["id"], "stage": deal["stage"], "stage_label": STAGES.get(deal["stage"], ""),
                "mode": mode, "mode_label": MODES.get(mode, mode),
                "mode_until": deal.get("mode_until") if mode == HUMAN else None,
                "slots": slots, "legend": pii.legend(json.dumps(slots, ensure_ascii=False), shown(p["id"])),
                "stage_proposal": deal["stage_proposal"],
                "stage_proposal_label": STAGES.get(deal["stage_proposal"], ""),
                "attention": deal.get("attention", ""), "last_message_at": deal.get("last_message_at"),
                "unanswered": unanswered(deal),
                "delivered_at": deal.get("delivered_at"), "purged_at": deal.get("purged_at"),
                "purge_due_at": retention.purge_due_at(deal, retention_days),
                "shipment": {k: shipment[k] for k in ("carrier", "waybill", "status", "updated_at")}
                if shipment else None,
                "shipment_status_label": SHIPMENT_STATES.get((shipment or {}).get("status", ""), ""),
                "events": [event_out(e) for e in store.events(deal["id"], limit=60) if e["kind"] != GROUP_LINE][:12],
                "three_party": group_of(deal),
                "draft_note": str(deal.get("draft_note") or ""),      # 起草备注: as the operators wrote it
                "card_items": card_items_out(store, deal["id"]),      # 「做手卡」: her latest sample order's products
                **linked_groups(deal),
                **reached(p, deal),
                # 群自己接上 (review 2026-11-11 #2): its groups the scan linked that nobody confirmed (「是她的群」)
                **({"unconfirmed_groups": unconfirmed[int(deal["id"])]} if int(deal["id"]) in unconfirmed else {}),
            },
        }

    unconfirmed: dict[int, list] = {}
    for r in store.boss_groups((BOSS_LINKED,)):
        # (老板拉的群里机器人自己选人: a 老板拉的群 the scan took over by choosing whom the deal hangs on, too: ``picked``)
        picked = bool(r.get("picked")) and not r.get("manual")
        if (r.get("manual") == GROUP_AUTO or picked) and r.get("confirmed_at") is None and r.get("deal_id"):
            unconfirmed.setdefault(int(r["deal_id"]), []).append(
                {"chat": r["chat"], "name": r["name"], **({"picked": PICK_HOW.get(str(r["picked"]), "")} if picked
                                                           else {})})

    def reached(party: dict, deal: dict) -> dict:
        """PRM_REACH_LAST_SPOKE: ``{"reach": {"where", "name"?, "why"}}`` (``reach_of``), or nothing at all."""
        if reach_of is None:
            return {}
        try:
            out = reach_of(party, deal)
        except Exception:                              # (the console's page never fails over it)
            logger.exception("PRM: where party %s's messages go could not be told", party.get("id"))
            out = None
        return {"reach": out} if out else {}

    def linked_groups(deal: dict) -> dict:
        """接手任何合作群: the groups linked by hand to this deal that are not its 三方群 (that one says so itself):
        ``{"linked_groups": [{chat, name, at}]}`` (``chat``: for the console's 「不接了」, ``group_unlink``; ``auto``
        True on one the scan linked by itself, 群自己接上), or nothing at all."""
        mine = (deal.get("three_party") or {}).get("chat")
        groups = [dict(g) for g in by_hand.get(int(deal["id"]), []) if g["chat"] != mine]
        return {"linked_groups": groups} if groups else {}

    return {
        "ok": True, "running": running,
        "parties": [party_out(p) for p in parties.values()],
        "pending": [packet_out(p) for p in pending],
        "recent": [packet_out(p) for p in recent],
        # (a sample order asked about for a second one, not red itself: its line is what the operator is asked)
        "platform_alerts": [{**packet_out(p), **({"error": str(((p.get("platform") or {}).get("recheck") or {})
                                                                 .get("ask") or "")} if p["status"] != FAILED else {})}
                            for p in store.platform_failures()],
        "night_alerts": night_alerts, "group_alerts": group_alerts,
        "hand_samples": hand_samples_out(store),
        "buyin": buyin_out(store, now),
        "friend_adds": friend_adds_out(store, now),
        "platform_actions": {name: spec["label"] for name, spec in platforms.ACTIONS.items()},
        "counts": store.counts(),
        "gates": gates_out(store),
        "stages": STAGES, "kinds": MESSAGE_KINDS, "modes": MODES, "shipment_states": SHIPMENT_STATES,
        **(extra or {}),
    }


def sample_items(store: PrmStore, deal_id: int) -> Optional[dict]:
    """Her latest sample order the bot made (a ``jst.sample_order`` draft whose execution was verified in 聚水潭), for
    「做手卡」: ``{"order", "at", "shop", "items": [{"item_id", "sku_ids"}]}``; None without one (an order made by hand
    names no products here)."""
    for packet in store.deal_packets(deal_id, tools=(SAMPLE_TOOL,)):
        data = packet.get("platform") or {}
        execution = data.get("execution") or {}
        if execution.get("outcome") != platforms.VERIFIED:
            continue
        action = data.get("action") or {}
        entries = action.get("items") or ([{"item_id": action["item_id"], "skus": action.get("skus") or []}]
                                          if action.get("item_id") else [])
        items = [{"item_id": str(e["item_id"]), "sku_ids": [str(k.get("sku_id")) for k in e.get("skus") or []
                                                           if isinstance(k, dict) and k.get("sku_id")]}
                 for e in entries if isinstance(e, dict) and e.get("item_id")]
        return {"order": str((execution.get("created") or {}).get("sample_order_no") or ""), "at": _order_at(packet),
                "shop": str(action.get("shop") or ""), "items": items[:handcards.MAX_CARDS]}
    return None


def card_items_out(store: PrmStore, deal_id: int) -> Optional[dict]:
    """「做手卡」's products for the console: her latest sample order's (``sample_items``), each with how many SKUs she got
    (only a 抖店 order's: the cards are of the 抖店 products); None without one."""
    order = sample_items(store, deal_id)
    if order is None:
        return None
    return {"order": order["order"], "at": order["at"], "shop": order["shop"],
            "items": [{"item_id": i["item_id"], "skus": len(i["sku_ids"])} for i in order["items"]]}


CARDS_FAILED_TEXT = "手卡没做成，草稿里没附手卡：不能原样发，改了再发（别说手卡做好了），或者不发"
CARDS_GONE_TEXT = "手卡文件已清理：重新做手卡"     # a waiting draft's card file not on this PC any more (final check B)


def cards_gone(packet: dict, attachments: Optional[list] = None) -> bool:
    """A draft whose 直播手卡 go with it (made, not left out) though a card file is not on this PC any more (a sweep, by
    hand): it is not sent (``decide``, ``_approve_execute``: CARDS_GONE_TEXT; final check 2026-11-10 B, the words went
    and the cards failed after).  ``attachments``: the ones that would go (``decide``'s, an out-of-date card left
    out)."""
    cards = (packet.get("meta") or {}).get(HANDCARDS)
    if not (isinstance(cards, dict) and cards.get("ok") and not cards.get("left_out")):
        return False
    for item in packet.get("attachments") if attachments is None else attachments:
        if isinstance(item, dict) and handcards.is_card_file(item.get("name")):
            try:
                if not Path(str(item.get("path") or "")).is_file():
                    return True
            except OSError:
                return True
    return False


def cards_failed(packet: dict) -> bool:
    """A draft whose 直播手卡 were not made (``meta.handcards.ok`` false): its words cannot go out as they are
    (``PrmService.decide``, ``_approve_execute``)."""
    cards = (packet.get("meta") or {}).get(HANDCARDS)
    return isinstance(cards, dict) and not cards.get("ok")


def cards_expired(packet: dict, now: float) -> bool:
    """A draft whose 直播手卡 carry a 双11 line past its end date by Beijing time (``handcards.promo_expired``): it cannot
    go out as it is, nor be sent once approved (``send_approved``); 改了再发 sends only the edited words, the cards left
    out (``left_out``, ``decide``); for cards, they are made again."""
    cards = (packet.get("meta") or {}).get(HANDCARDS)
    return (isinstance(cards, dict) and bool(cards.get("ok")) and not cards.get("left_out")
            and handcards.promo_expired(cards, handcards.beijing_today(now)))


def card_ids(items) -> tuple:
    """The products ticked for 「做手卡」: ``(ids, "")`` (抖店 item ids, digits, each once, at most handcards.MAX_CARDS;
    None when none were given: her latest sample order's), or ``(None, why not)``."""
    if items is None:
        return None, ""
    if not isinstance(items, list):
        return None, "要做手卡的商品要写成商品 ID 的列表"
    ids: list = []
    for raw in items:
        text = str(raw if isinstance(raw, (str, int)) and not isinstance(raw, bool) else "").strip()
        if not re.fullmatch(r"\d{6,25}", text):
            return None, f"「{str(raw)[:30]}」不是抖店的商品 ID（一串数字）"
        if text not in ids:
            ids.append(text)
    if len(ids) > handcards.MAX_CARDS:
        return None, f"一次最多做 {handcards.MAX_CARDS} 款（一份草稿最多 {handcards.MAX_CARDS} 个文件）"
    return ids, ""


def links_out(store: PrmStore, party_id: int) -> list[dict]:
    """Who a creator is on 抖音 (``creator_links``) for her deal: her 抖音号, where the link came from, and for a
    加百应达人微信 record when its invite card went out (功能 R)."""
    out = []
    for link in store.creator_links(party_id):
        record = store.friend_add(link["add_id"]) if link.get("add_id") else None
        invited = (record or {}).get("invited") or {}
        out.append({"account": link["account"], "platform": link["platform"], "source": link["source"],
                    "nickname": link["nickname"], "add_id": link["add_id"], "created_at": link["created_at"],
                    "invited_at": invited.get("invited_at") or None, "plan_id": invited.get("plan_id") or None})
    return out


def buyin_packet_out(store: PrmStore, p: dict) -> Optional[dict]:
    """A 百应 draft's own part for the console (None for any other packet): the conversation, her nickname there, her
    lines it answers (masked) and, when our side wrote in that conversation after them (an operator in 百应), when."""
    if not is_buyin(p):
        return None
    meta = p.get("meta") or {}
    cid = str(meta.get("conversation") or "")
    thread = store.buyin_thread(cid) if cid else None
    upto = _float(meta.get("upto")) or 0.0
    shop = _float((thread or {}).get("last_shop_at"))
    return {"conversation": cid, "name": str(meta.get("buyin_name") or ""), "said": str(meta.get("said") or ""),
            "lines": int(meta.get("lines") or 0), "replied_at": shop if shop and shop > upto else None,
            "friend_add": buyin_friend_add(store, cid)}


def buyin_friend_add(store: PrmStore, cid: str) -> Optional[dict]:
    """The newest 加微信 record (src/prm/friend_adds.py) made from this 百应 conversation, for its draft's card
    (``{"id", "state", "label", "open", "masked"}``, masked values only), or None."""
    record = next(iter(store.friend_adds(conversation_id=cid, limit=1)), None) if cid else None
    if record is None:
        return None
    return {"id": record["id"], "state": record["state"], "label": FRIEND_ADD_STATES.get(record["state"], record["state"]),
            "open": record["state"] in FRIEND_ADD_OPEN, "masked": dict(record.get("masked") or {})}


def buyin_mask_wechat(value) -> str:
    """A WeChat id as the bot keeps and shows it: its first 2 and last 2 characters (shorter than 5: ****;
    ``contact.masked``, the rule 加百应达人微信 shows them by too)."""
    return contact.masked(contact.WECHAT, value) or "****"


def buyin_mask_phone(value) -> str:
    """A phone as the bot keeps and shows it: 3 digits, ****, the last 4 (shorter than 8: ****, as 3 + 4 would show all
    of it; ``contact.masked``)."""
    return contact.masked(contact.PHONE, value) or "****"


def buyin_contact_line(card, add_id: Optional[int] = None) -> str:
    """A creator's 百应 contact card as the bot records it (``contact.masked_line``): "[联系方式卡片] 微信号 ab****yz、手机
    138****0001", with "（加微信 #<id>）" when ``on_buyin_contact`` gave an id; ``BUYIN_CONTACT_UNREAD`` when the card's
    values were not read, ``BUYIN_CONTACT_OWN`` when it held only the shop's own (``platforms.im_strip_own``).  Never a
    full value."""
    return contact.masked_line(card if isinstance(card, dict) else {}, add_id)


def buyin_text_line(text: str, add_id: Optional[int] = None) -> str:
    """A line of hers (already through the pii tokens: her phone is 〈手机N〉) as the bot records it: a 微信号 she typed
    after a label masked (``contact.mask_contacts``), and "（加微信 #<id>）" when ``on_buyin_contact`` gave an id for
    the contact in it."""
    return contact.mask_contacts(text) + (f"（加微信 #{add_id}）" if add_id is not None else "")


def buyin_out(store: PrmStore, now: float) -> dict:
    """What the 待审批 page says of the 百应 private messages (PRM_BUYIN_SYNC_MIN): why the last rounds could not be
    made (``status``, None when the last one went through), when the last round was asked for, the conversations
    of nobody on the list whose lines wait for an answer (``unmatched``: their 百应 nicknames, when the CLI gave them,
    and ``invited`` when one is our invite: her 抖音号, when and by which plan; nothing of what they wrote is kept), how
    many of those have a 加微信 record not done with (``contacts_left``: by the conversation or her 抖音号; the
    cards are above the drafts), and how many contact cards came in the conversations that are not ours
    (``others_contacts``: neither on the list nor our invite; their values are never kept).  The conversations of
    somebody else's invite card (``other_invite``: a colleague's, 2026-10-30) are not listed, only counted
    (``others_invited``): their answers are not ours to handle."""
    status = _json_value(store.meta_value("buyin_status"))
    unmatched = store.buyin_threads(matched=False)
    answering = [t for t in unmatched if int(t.get("unanswered") or 0) > 0
                 and now - float(t.get("last_creator_at") or now) <= BUYIN_UNMATCHED_DAYS * 86400]
    waiting = [t for t in answering if not t.get("other_invite")]
    adds = store.friend_adds(states=FRIEND_ADD_OPEN)
    cids = {a["conversation_id"] for a in adds if a.get("conversation_id")}
    awemes = {str(a["aweme_id"]).lower() for a in adds if a.get("aweme_id")}
    return {"status": status if isinstance(status, dict) and status.get("text") else None,
            "asked_at": _float(store.meta_value("buyin_sync_at")),
            "unmatched": [{"name": t["nickname"], "count": int(t["unanswered"]), "at": t.get("last_creator_at"),
                           "invited": {k: str(t["invited"].get(k) or "") for k in ("aweme_id", "invited_at", "plan_id")}
                           if t.get("invited") else None}
                          for t in waiting[:20]],
            "unmatched_count": len(waiting),
            # of those, the ones with a 加微信 record not done with (by the conversation, or her 抖音号)
            "contacts_left": sum(1 for t in waiting if t["conversation_id"] in cids
                                 or (t.get("aweme_id") and str(t["aweme_id"]).lower() in awemes)),
            "others_contacts": sum(int(t.get("contacts") or 0) for t in unmatched if not t.get("invited")),
            "others_invited": sum(1 for t in answering if t.get("other_invite"))}


class PrmService:
    def __init__(self, config, relay, *, store: Optional[PrmStore] = None,
                 send: Optional[Callable[[str, str], bool]] = None,
                 send_file: Optional[Callable[[str, str], bool]] = None,
                 clock: Callable[[], float] = time.time,
                 on_parties_changed: Optional[Callable[[], None]] = None,
                 scrub_messages: Optional[Callable[[str, Callable[[str], str]], int]] = None,
                 pause: Optional[Callable[[float], bool]] = None):
        self._config = config
        self._relay = relay
        # the workers hear that this bot knows 专属链接 the shop's way (GET /relay/health: a newer Mac puts none into a
        # draft for an older bot, which would keep its {链接}: review 2026-11-09 F5)
        features = getattr(relay, "features", None)
        if isinstance(features, list) and platforms.CHANNEL_FEATURE not in features:
            features.append(platforms.CHANNEL_FEATURE)
        self._store = store if store is not None else PrmStore(config.prm_db)
        self._store.seed_gates(gates.DEFAULT_GATES)
        self._send = send
        self._send_file = send_file
        self._clock = clock
        self.on_parties_changed = on_parties_changed
        # on_registered(wxid) -> bool: a creator was registered; True when her earlier messages are
        # on their way (a new friend: src/wechat/new_friends.py, back through take_new_friend).
        self.on_registered: Optional[Callable[[str], bool]] = None
        self._scrub_messages = scrub_messages
        self._takeover_sec = int(getattr(config, "prm_takeover_min", 120) or 120) * 60
        # PRM_CATCHUP_HOURS: a message read more than MAX_AGE_SEC late is drafted (always reviewed)
        # up to this old, only recorded beyond it; 0 = older than MAX_AGE_SEC is history.
        try:
            catchup_hours = int(getattr(config, "prm_catchup_hours", CATCHUP_HOURS))
        except (TypeError, ValueError):
            catchup_hours = CATCHUP_HOURS
        self._catchup_sec = min(max(catchup_hours, 0), 72) * 3600
        # One creator's message at a time: messages read at once (a catch-up after a restart,
        # a burst) find the job the one before made, so they are one job.
        self._handle_lock = threading.Lock()
        self._holding_sec = int(getattr(config, "prm_holding_min", 10) or 0) * 60
        self._holding_text = str(getattr(config, "prm_holding_text", "") or HOLDING_TEXT).strip() or HOLDING_TEXT
        # PRM_OPENING_AUTO_MIN (0: off, as before; a config object without it, an older caller's: off): our first line to a
        # new friend waits this long in 待审批 for a person, then goes out by itself if it passed the checks
        # (``_opening_auto``, ``send_due_openings``); in the 下班时段 or 自动模式 at once.
        try:
            self._opening_auto_sec = min(max(int(getattr(config, "prm_opening_auto_min", 0) or 0), 0), 1440) * 60
        except (TypeError, ValueError):
            self._opening_auto_sec = 0
        # PRM_REPLY_PARTS: a reply's paragraphs go out as messages of their own (``_deliver``), and the Mac is told it may
        # write them (``_reply_meta``).  Never on the Mac's edge side (WECHAT_BACKEND=edge): Windows tells it of a send
        # that failed by the send's whole text (``on_send_failed``), which one part of a draft is not.
        try:
            parts = int(getattr(config, "prm_reply_parts", 1) or 1)
        except (TypeError, ValueError):
            parts = 1
        self._reply_parts = 1 if getattr(config, "wechat_backend", "") == "edge" else min(max(parts, 1), REPLY_PARTS_MAX)
        # PRM_SEND_PACE: a reply's messages go out at a person's pace (``_deliver``; off: as before)
        self._pace = str(getattr(config, "prm_send_pace", "off") or "off").strip().lower() == "on"
        # PRM_NIGHT_AUTO_HOURS (empty: off, everything as before; a config object without it, an older caller's: off):
        # inside the window a reply draft that waits for a person by the gates may still go out by itself when every
        # night rule holds (``_night_judge``); its job is told it is night (``meta.night``, ``_submit``) and holding
        # replies say PRM_NIGHT_HOLDING_TEXT.  Nothing of it while this PC's clock is off WeChat's (``_clock_off``).
        # 自动模式 (the console's switch, ``auto_mode``: in the store, no setting) puts the same rules in force at any hour.
        try:
            self._night = gates.night_window(getattr(config, "prm_night_auto_hours", "") or "")
        except ValueError:
            logger.warning("PRM: PRM_NIGHT_AUTO_HOURS is not HH:MM-HH:MM (like 18:00-09:00); the night window is off")
            self._night = None
        self._night_holding_text = (str(getattr(config, "prm_night_holding_text", "") or NIGHT_HOLDING_TEXT).strip()
                                    or NIGHT_HOLDING_TEXT)
        self._retention_days = int(getattr(config, "prm_retention_days", retention.RETENTION_DAYS)
                                   or retention.RETENTION_DAYS)
        # created | delivered: when a sample order's 专属佣金 is drafted (next_step_note)
        self._commission_after = str(getattr(config, "prm_commission_after", "delivered") or "delivered")
        # PRM_SAMPLE_SYNC_MIN (0 = off; a config object without it, an older caller's: off): where the verified
        # sample orders stand in 聚水潭, asked of the Mac once an interval (``sweep_sample_status``)
        try:
            self._sample_sync_sec = max(0, int(getattr(config, "prm_sample_sync_min", 0) or 0)) * 60
            self._waybill_days = max(1, int(getattr(config, "prm_sample_waybill_days", 3) or 3))
            self._sign_days = max(1, int(getattr(config, "prm_sample_sign_days", 10) or 10))
        except (TypeError, ValueError):
            self._sample_sync_sec, self._waybill_days, self._sign_days = 0, 3, 10
        # PRM_SIGNED_HELLO (off: nothing of it; a config object without it, an older caller's: off): a signature the
        # sync sees is noted (``_signed_seen``) and asked about inside PRM_SIGNED_HELLO_HOURS (``sweep_signed_hellos``)
        self._signed_hello = str(getattr(config, "prm_signed_hello", "off") or "off").strip().lower() == "on"
        try:
            self._hello_hours = gates.night_window(getattr(config, "prm_signed_hello_hours", "") or SIGNED_HELLO_HOURS)
        except ValueError:
            logger.warning("PRM: PRM_SIGNED_HELLO_HOURS is not HH:MM-HH:MM (like 10:00-20:00); %s is used",
                           SIGNED_HELLO_HOURS)
            self._hello_hours = gates.night_window(SIGNED_HELLO_HOURS)
        self._hello_waits: dict = {}               # order -> why its hello waits, as last logged (a line per change)
        # PRM_HANDCARDS (off: nothing of it; a config object without it, an older caller's: off): WeChat draft jobs say the
        # Mac may make 直播手卡 (``_reply_meta``) and the console's 「做手卡」 asks for them (``ask_handcards``).  A draft that
        # comes back with cards waits for a person all the same, switch or not (``on_result``).
        self._handcards = str(getattr(config, "prm_handcards", "off") or "off").strip().lower() == "on"
        # PRM_DRAFT_CONTEXT (off: nothing of it; a config object without it: off): a creator's draft jobs carry
        # ``meta.draft_context`` (``_reply_meta``): the Mac reads her 聚水潭 sample orders by it and matches her pictures
        # against its hat photo index, and checks a sample order by photo (src/prm/draft_context.py)
        self._draft_context = str(getattr(config, "prm_draft_context", "off") or "off").strip().lower() == "on"
        self._sample_lock = threading.Lock()       # one round's job queued, or one answer applied, at a time
        self._sample_errors = 0                    # failed rounds in a row (SAMPLE_ERRORS)
        self._sample_said = ""                     # what the deals were last told of a round that could not be made
        self._hand_problem = ""                    # why our hand-made orders are not read (the 「待审批」 page says it)
        self._hand_errors = 0                      # rounds in a row they could not be read (HAND_ERRORS)
        # PRM_BUYIN_SYNC_MIN (0 = off, the default; a config object without it: off): the new 抖音百应 private messages,
        # asked of the Mac once an interval (``sweep_buyin``); a registered creator's become a 百应 draft an operator
        # copies and sends in 百应 by hand
        try:
            self._buyin_sec = max(0, int(getattr(config, "prm_buyin_sync_min", 0) or 0)) * 60
        except (TypeError, ValueError):
            self._buyin_sec = 0
        self._buyin_lock = threading.RLock()       # one round queued or applied, or one decision on a 百应 draft, at a time
        self._buyin_errors = 0                     # failed rounds in a row (BUYIN_ERRORS)
        self._buyin_conv_errors: dict = {}         # conversation id -> rounds in a row it failed (BUYIN_CONV_TRIES)
        self._buyin_fail_rounds = 0                # rounds in a row with a conversation that failed
        self._buyin_held_logged: dict = {}         # held notice key -> when its drafting failure was last logged
        # creator_directory() -> [{"wxid", "platform", "platform_no", "platform_uid"}]: the 达人名单 (src/influencer),
        # who a 百应 creator is by her 抖音号 / 抖音 UID (Bot._wire_prm_directory); None: only the ids the deals' verified
        # 抖店 sample orders found (``_creator_ids``)
        self.creator_directory: Optional[Callable[[], list]] = None
        # 加百应达人微信 (PRM_FRIEND_ADD, default off; src/prm/friend_adds.py): the records, one add_friend per
        # confirmation (Bot.run attaches the admin handler and the auto-accept)
        self.friend_adds = FriendAdds(self._store, config, clock=clock, register=self.register)
        # 三方群 (src/prm/three_party.py): 「拉三方群」 in a deal makes the group in the WeChat window (Bot.run attaches it)
        self.three_party = ThreeParty(self._store, config, clock=clock)
        self.three_party.since_of = self._group_since  # (her 三方群 read again from its last line kept)
        # on_buyin_contact(event) -> {"id", "state", "masked"} | None: told of each new contact card a creator sent in a
        # 百应 conversation that is ours (our invite, or a registered creator's), with its values in full
        # (``_buyin_contacts``), and of each contact she typed in her own words there: 加百应达人微信's offer_from_buyin
        # (None while PRM_FRIEND_ADD is off: the card is recorded masked all the same).  None: nobody is told.
        self.on_buyin_contact: Optional[Callable[[dict], Optional[dict]]] = self.friend_adds.offer_from_buyin
        # 批准并执行: checking that a 专属 action is not open already and starting it is one step
        self._approve_lock = threading.Lock()
        # Starting a 专属佣金 chain: looking whether it is under way and asking for its step is one step
        # (the console's 已签收 and the sender thread's sweep_deliveries must not both ask)
        self._chain_lock = threading.RLock()
        # The admin and the cc users are never creators: their messages stay the relay's.
        self._reserved = {str(w).strip() for w in [getattr(config, "admin_wxid", ""),
                                                    *(getattr(config, "claude_relay_wxids", None) or [])]
                          if str(w).strip()}
        # 老板拉的群 (PRM_BOSS_GROUPS, default off; src/prm/boss_groups.py): the groups a boss made with a creator and us,
        # found and taken over by the sender thread (``tick``); her lines there are drafted (``_group_message``).
        # Bot._start_prm attaches the backend.
        self.boss_groups = BossGroups(self._store, config, clock=clock, reserved=self._reserved, register=self.register,
                                      opening=self._group_opening, handle=self.handle, local=self._local,
                                      guard=self.three_party.guard, catch_up=self._link_catch_up,
                                      on_close=self._close_group, notice=self._auto_link_notice,
                                      tell=self._tell_admin, hook_busy=self._hook_busy,
                                      parties_changed=self._parties_changed)
        # 接手任何合作群 (src/prm/group_link.py, 2026-11-09): the admin's 「接群」 commands (Bot._wire_friend_adds hands
        # them over from src/admin.py); a group linked by hand works whatever PRM_BOSS_GROUPS says
        self.group_links = GroupLinks(self, clock=clock)
        # ... PRM_LINKED_GROUPS_AUTO (off, the default; a config object without it: off): its drafts always wait for a
        # person (``_hand_hold``); on: they go by the gates, the 下班时段 and 自动模式 as a 老板拉的群's
        self._linked_groups_auto = (str(getattr(config, "prm_linked_groups_auto", "off") or "off").strip().lower()
                                    == "on")
        # ... PRM_GROUPS_REVIEW (off, the default; 2026-11-13): on, nothing goes into any group by itself: every draft for a
        # group chat waits for a person (``_hand_hold``: GROUPS_REVIEW_HELD); private chats as before
        self._groups_review = str(getattr(config, "prm_groups_review", "off") or "off").strip().lower() == "on"
        # ... PRM_REACH_LAST_SPOKE (off, the default; a config object without it: off): what the bot starts itself goes
        # where her side last spoke (``_reaching``, REACH_LAST_SPOKE)
        self._reach_last_spoke = (str(getattr(config, "prm_reach_last_spoke", "off") or "off").strip().lower() == "on")
        # ... a group linked by hand: when her waiting group draft was last drafted again, and the groups where one is
        # owed (``_group_message``, ``sweep_owed_redrafts``: REDRAFT_GAP_SEC)
        self._redrafted: dict[str, float] = {}
        self._redraft_owed: dict[str, float] = {}
        # ... how often others' lines had her waiting draft drafted again since her last line, and how many lines of
        # theirs came since it was last drafted (REDRAFT_MAX; ``_unheard``)
        self._auto_redrafts: dict[str, int] = {}
        self._lines_since: dict[str, int] = {}
        # PRM_AUTO_REGISTER (a config object without it, an older caller's: off): the new friends the scan found
        # (``new_friend_found``: wxid, name, how; never her greeting), registered by the sender thread (``register_found``)
        self._auto_register = str(getattr(config, "prm_auto_register", "off") or "off").strip().lower() == "on"
        self._found: deque = deque()
        self._found_lock = threading.Lock()
        self._wxids = self._store.active_wxids()
        # What the bot itself sent lately, per chat: (normalized text, when, packet id); each file
        # (kind, when, packet id, n): one own row of its kind each (win review F4 (b)).
        self._sent_lines: dict[str, deque] = {}
        self._sent_files: dict[str, deque] = {}
        self._file_seq = itertools.count(1)
        # A packet sent before this run (or cut off by a stop): the kinds of its files not read back yet.
        self._file_rows: dict[int, list] = {}
        # ... and of a reply in parts, the parts not read back yet (``_sent_part``: one line each, like the files).
        self._text_rows: dict[int, list] = {}
        # Per chat, the own rows handled lately (``_row_key`` -> when): handed on again within
        # READ_BACK_SEC they are no new line (_match_own).
        self._read_back: dict[str, dict[str, float]] = {}
        self._sent_lock = threading.Lock()
        self._group_seen: dict[str, deque] = {}    # her 三方群 -> the message ids kept this run (``_first_group_line``)
        self._group_seen_lock = threading.Lock()
        self._started = clock()               # what this run sent is in _sent_lines / _sent_files
        # This PC's clock minus WeChat's (win review F6), measured by this run's own lines read back
        # (``_note_clock``); 0 until then and within CLOCK_NOISE_SEC.  ``_local`` puts WeChat's times
        # on this PC's clock wherever they are held against it (how late a message is read, a takeover).
        self._clock_samples: deque = deque(maxlen=CLOCK_SAMPLES)
        self._skew = 0.0
        self._clock_text: Optional[str] = None     # the deals' notice as last written (None: not this run)
        self._last_retention = 0.0
        self._wake = threading.Event()
        self._stopping = threading.Event()
        # pause(sec) between two messages of a reply: True when the bot is stopping (the rest of it is not sent)
        self._pause = pause if pause is not None else self._stopping.wait
        self._thread: Optional[threading.Thread] = None
        # 微信里审批草稿 (PRM_WECHAT_REVIEW, default off; src/prm/wechat_review.py): the drafts waiting for a person told to
        # the admin's private chat (the sender thread, ``tick``), the admin's answers there (the route step wechat_review)
        self.wechat_review = WechatReview(
            self._store, config, clock=clock, send=send, send_file=send_file, decide=self.decide,
            confirm=lambda deal_id, packet_id, code: self.photo_confirm(deal_id, packet_id, code, actor=WECHAT_ACTOR),
            check=self.edit_problem, lines=self._packet_lines, parts=lambda text: len(reply_parts(text, self._reply_parts)),
            aim=lambda packet: self._aim(packet, wait=True), relay_store=relay.store, local=self._local,
            skew=self._clock_low, written=written_state, files_root=getattr(relay, "files_dir", None),
            opening_auto=self._opening_auto_sec > 0)
        # 合作单指令 (PRM_WECHAT_COMMANDS, default off; src/prm/admin_commands.py): the console's deal buttons typed in the
        # admin's chat, 「做手卡 <达人>」 … (Bot._wire_friend_adds hands them over from src/admin.py while it is on)
        self.deal_commands = DealCommands(self, config, clock=clock)
        relay.register_kind(PRM, self.on_result)

    @property
    def store(self) -> PrmStore:
        return self._store

    @property
    def files_root(self) -> Optional[Path]:
        """The relay's files (``ClaudeRelay.files_dir``: the Mac's outfiles, its photo sheets): GET /api/prm/file serves a
        draft's picture only from inside it (review 2026-11-10).  None for a relay that keeps none."""
        root = getattr(self._relay, "files_dir", None)
        return Path(root) if root is not None else None

    @property
    def auto_register(self) -> bool:
        """PRM_AUTO_REGISTER is on: Bot._start_new_friends hands the scan's new friends to ``new_friend_found``."""
        return self._auto_register

    @property
    def auto_mode(self) -> dict:
        """自动模式 as the store keeps it now (``auto_mode_of``: read each time, so a write of the stopped bot's console
        counts too)."""
        return auto_mode_of(self._store)

    def set_auto_mode(self, on: bool, actor: str = "console") -> dict:
        """The console's 自动模式 switch (``POST /api/prm/auto``): ``{"ok": True, "auto_mode"}``.  It counts from the next
        job and draft on (``_night_active``); the drafts waiting already stay waiting, as when the window starts."""
        state = write_auto_mode(self._store, bool(on), now=self._clock(), actor=actor)
        self._wake.set()                               # (the holding replies of this round say the right words)
        return {"ok": True, "auto_mode": state}

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Settle what a stopped bot left half done (a send cut off; a deal marked 已签收 meanwhile:
        ``sweep_deliveries``), then the sender thread."""
        now = self._clock()
        try:
            self.friend_adds.start()               # (an add cut off by a stop: unsure)
        except Exception:
            logger.exception("PRM: the friend-add records could not be looked at")
        try:
            self.three_party.settle()              # (a group cut off by a stop: unsure)
        except Exception:
            logger.exception("PRM: the deals' 三方群 could not be looked at")
        try:
            self.boss_groups.start()               # (老板拉的群 the bot took over: read again from their last line)
        except Exception:
            logger.exception("PRM: the boss groups taken over could not be read again")
        try:
            self.three_party.watch_all()           # (her own 三方群: read again, read only, from its last line kept)
        except Exception:
            logger.exception("PRM: the deals' 三方群 could not be read again")
        # A draft for a group goes out only while the bot drafts in it (``_group_closed``): 老板拉的群 switched off, a draft
        # for such a group still waiting is never sent (``send_approved`` too); nor one for a group whose link ended
        # (接手任何合作群, review 2026-11-09); a group linked by hand is not the switch's
        stale = [(p, self._group_closed(p["chat_id"], p["party_id"]))
                 for p in self._store.packets((PENDING, APPROVED), limit=500) if is_group_packet(p)]
        stale = [why for p, why in stale if why and self._fail_closed(p, why, now)]
        off, closed = stale.count(BOSS_GROUPS_OFF_TEXT), stale.count(GROUP_CLOSED_TEXT)
        if off:
            logger.info("PRM: %d draft(s) for boss groups not sent: PRM_BOSS_GROUPS is off", off)
        if closed:
            logger.info("PRM: %d draft(s) for groups not drafted in any more not sent", closed)
        for p in self._store.packets((SENDING,), limit=500):
            # (read back after all, its line confirms it: ``_confirm``)
            self._store.move(p["id"], (SENDING,), FAILED, outcome="indeterminate", sent_at=now, error=UNSURE_ERROR)
        try:
            started = self.sweep_deliveries()
            if started:
                logger.info("PRM: %d deal(s) signed for while the bot was stopped: their 专属佣金 step asked for", started)
        except Exception:
            logger.exception("PRM: the deals signed for while the bot was stopped could not be looked at")
        try:
            self._buyin_scrub_jobs()               # (a 百应 inbox answer kept as it came, by a slip before: a summary)
        except Exception:
            logger.exception("PRM: the 百应 inbox jobs could not be looked at")
        self._thread = threading.Thread(target=self._loop, name="prm-sender", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    # ── modes ─────────────────────────────────────────────────────────────

    def _mode(self, deal: dict, now: float) -> str:
        """The deal's mode now; a takeover whose time is up turns back to copilot (recorded)."""
        mode = deals.effective_mode(deal, now)
        if mode == COPILOT and deal.get("mode") == HUMAN:
            self._store.update_deal(deal["id"], now, mode=COPILOT, mode_until=None)
            self._store.add_event(deal["id"], deal["party_id"], "mode_expired", {}, actor="system", now=now)
        return mode

    # ── this PC's clock against WeChat's (win review F6) ──────────────────

    def _local(self, written: float) -> float:
        """When WeChat says something was written, on this PC's clock (``_skew``, what this run measured;
        nothing before it measured anything)."""
        return written + self._skew if written else written

    def _clock_low(self) -> Optional[float]:
        """This PC's clock against WeChat's as this run measured it, below CLOCK_NOISE_SEC too (the smallest sample: this
        PC's time minus WeChat's), or None while nothing was measured (微信里审批草稿's bare answers)."""
        with self._sent_lock:
            return min(self._clock_samples) if self._clock_samples else None

    @staticmethod
    def _clock_sample(msg: dict, noted: float, now: float) -> Optional[float]:
        """What one of our lines read back says of this PC's clock: read (now, this PC) minus written
        (WeChat's time).  Only a line read back within CLOCK_SAMPLE_SEC of being noted (the delay adds to
        it), never the Mac's edge lines (their time is when Windows handed them on)."""
        if msg.get("edge_seen") or msg.get("edge_operator"):
            return None
        try:
            written = float(msg.get("timestamp") or 0)
        except (TypeError, ValueError):
            return None
        if not written or now - noted > CLOCK_SAMPLE_SEC:
            return None
        return now - written

    def _note_clock(self, sample: float, now: float) -> None:
        """A measurement of this PC's clock (``_clock_sample``).  The smallest of the last CLOCK_SAMPLES
        counts (a read-back delayed by a stalled database only adds to one; a clock put right counts at
        once); off by CLOCK_NOISE_SEC or more it corrects WeChat's times (``_local``); off by CLOCK_SKEW_SEC
        or more every deal says so (``clock_note``), until a measurement says it is right again."""
        with self._sent_lock:
            self._clock_samples.append(sample)
            low = min(self._clock_samples)
            skew = low if abs(low) >= CLOCK_NOISE_SEC else 0.0
            if skew != self._skew:
                logger.warning("PRM: this PC's clock is %+.0f s off WeChat's (measured by our own line read back)",
                               low)
            self._skew = skew
        text = clock_note(skew) if abs(skew) >= CLOCK_SKEW_SEC else ""
        if text == self._clock_text:
            return
        self._clock_text = text
        active = {p["id"] for p in self._store.parties()}
        for deal in self._store.deals():
            have = next((n for n in deal["notices"] if n["key"] == "clock"), None)
            if (text and deal["party_id"] in active and (have is None or have.get("text") != text)
                    or not text and have is not None):
                # (about this PC, not the deal: not a sign of life for the 8.2 clean-up)
                self._store.set_notice(deal["id"], "clock", text, now, touch=False)

    # ── 下班时段 (PRM_NIGHT_AUTO_HOURS, 2026-10-29) and 自动模式 (2026-10-30) ──

    def _clock_off(self, what: str = "下班时段") -> str:
        """Why this PC's clock cannot be trusted with the night window ("" = it can): this run measured it off
        WeChat's by CLOCK_SKEW_SEC or more (the deals' clock notice, ``_note_clock``), or, before this run measured
        anything, a deal still has the clock notice of a run before (nothing measured it right since).  ``what``:
        which is held (``night_clock_text``)."""
        if abs(self._skew) >= CLOCK_SKEW_SEC:
            return night_clock_text(self._skew, what)
        if self._clock_text is None and self._store.deals_with_notice("clock"):
            return night_clock_text(None, what)
        return ""

    def _in_window(self, now: float) -> bool:
        """Is ``now`` inside the 下班时段 (the window set, by this PC's clock)?"""
        return self._night is not None and gates.in_night(self._night, now)

    def _auto_by_day(self, now: float, auto_on: Optional[bool] = None) -> bool:
        """Is it 自动模式 that puts the night rules in force now (switched on, ``now`` outside the window or none set)?
        Inside the window the night's own words count, 自动模式 or not.  ``auto_on``: the switch as the caller read it
        (one judgement reads it once: review 2026-10-30); None reads it now."""
        on = self.auto_mode["on"] if auto_on is None else auto_on
        return not self._in_window(now) and on

    def _night_active(self, now: float, auto_on: Optional[bool] = None) -> bool:
        """Do the night rules apply now (the 下班时段: ``now`` inside the window by this PC's clock; or 自动模式 switched
        on in the console, at any hour) with a clock to trust?  ``auto_on``: as ``_auto_by_day``'s."""
        on = self.auto_mode["on"] if auto_on is None else auto_on
        return (self._in_window(now) or on) and not self._clock_off()

    def _reply_meta(self, meta: dict) -> dict:
        """A WeChat reply's draft job (her message's, a 开场白's, a 专属佣金 step's) with ``meta.reply_parts`` when
        PRM_REPLY_PARTS is above 1: the Mac may write the reply as that many messages, a blank line between two
        (worker.parts_line); ``_deliver`` sends them so.  With PRM_HANDCARDS ``meta.handcards_ok``: the Mac may make 直播手卡
        when she asks for them (worker.handcards_rule).  With PRM_DRAFT_CONTEXT on, ``meta.draft_context`` too
        (``_draft_context_meta``).  The same dict."""
        if self._reply_parts > 1:
            meta["reply_parts"] = self._reply_parts
        if self._handcards:
            meta[handcards.OK_KEY] = True
        if self._draft_context:
            meta[draft_context.KEY] = self._draft_context_meta(meta.get("deal_id"))
        return meta

    def _draft_context_meta(self, deal_id) -> dict:
        """PRM_DRAFT_CONTEXT: ``meta.draft_context`` of a draft job of the deal (``draft_context.job_context``: the
        sample orders her deal knows, the ones the bot made, her platform accounts but one another active creator has
        too: ``_hand_index``, as the hand-made orders go; the codes an operator confirmed for the deal, 「对过图了」).
        Whatever goes wrong, the key is there (empty lists): the Mac still says what it could not find, and still checks
        a sample order by photo (then holding what only a confirmation let go)."""
        try:
            deal = self._store.deal(int(deal_id))
            party = self._store.party(deal["party_id"]) if deal else None
            if party:
                accounts = draft_context.accounts_of(self._store, party, self._creator_ids(deal["id"]),
                                                     self.creator_directory)
                accounts = draft_context.unambiguous(accounts, self._hand_index()["keys"], party["wxid"])
                return draft_context.job_context(self._store, deal["id"], accounts)
        except Exception:
            logger.exception("PRM: the draft context of deal %s could not be put together", deal_id)
        return {"orders": [], "verified": [], "accounts": [], "confirmed": []}

    def _confirmed_codes(self, deal_id) -> dict:
        """PRM_DRAFT_CONTEXT: the codes an operator confirmed for the deal (「对过图了」), code -> when."""
        try:
            return {str(r["code"]): r["at"] for r in self._store.photo_confirmations(int(deal_id))}
        except (TypeError, ValueError):
            return {}

    def _drop_job_sheets(self, job_id: int) -> None:
        """PRM_DRAFT_CONTEXT: a job's operator-only files that no draft keeps (its answer failed or cannot be read)
        deleted, and its outfiles named like a sheet: her photo is on them."""
        draft_context.delete_files([f["path"] for f in self._relay.store.operator_files(job_id)]
                                   + [f["path"] for f in self._relay.store.outfiles(job_id)
                                      if str(f.get("name") or "").startswith(draft_context.SHEET_PREFIX)])

    def _drop_cards(self, files) -> int:
        """直播手卡 (review 2026-11-10 B-L3): the card files among ``files`` (attachments, ``{"name", "path"}``:
        ``handcards.is_card_file``) deleted, a draft having closed without sending them (dropped, out of date, failed,
        past its 双11 line, left out by 改了再发, a verdict that failed): her 称呼 is printed on a card.  Only files inside
        the relay's folder (``files_root``).  Returns how many went."""
        root = self.files_root
        if root is None:
            return 0
        paths = []
        for f in files or []:
            if not isinstance(f, dict) or not handcards.is_card_file(f.get("name")) or not f.get("path"):
                continue
            try:
                inside = Path(str(f["path"])).resolve().is_relative_to(root.resolve())
            except (OSError, ValueError, RuntimeError):
                inside = False
            if inside:
                paths.append(str(f["path"]))
        return draft_context.delete_files(paths)

    def sweep_cards(self) -> int:
        """直播手卡 on this PC (``purge_due``; review 2026-11-10 B-L3): the card files of every draft that closed without
        sending them (dropped, out of date, failed: ``_drop_cards``), and every card file and zip in the relay's
        outfiles older than KEEP_FILES_DAYS by its own time, sent or not (``handcards.sweep_files``).  Returns how many
        went."""
        gone = sum(self._drop_cards(p["attachments"]) for p in self._store.card_packets((DROPPED, SUPERSEDED, FAILED)))
        root = self.files_root
        if root is not None:
            # (never the cards of a draft still waiting or going out, however old: final check 2026-11-10 B)
            live = [str(a.get("path") or "") for p in self._store.card_packets((PENDING, APPROVED, SENDING, EXECUTING))
                    for a in p["attachments"] if isinstance(a, dict) and handcards.is_card_file(a.get("name"))]
            gone += handcards.sweep_files(root, self._clock(), KEEP_FILES_DAYS, keep=[p for p in live if p])
        if gone:
            logger.info("PRM: %d 直播手卡 file(s) deleted (a draft not sent, or older than %d days)", gone,
                        KEEP_FILES_DAYS)
        return gone

    def _with_note(self, meta: dict, deal: dict, now: float) -> dict:
        """A draft job of the deal (her message's, a 开场白's, a 专属佣金 step's, a 百应 draft's) with ``meta.note`` when the
        deal has a 起草备注 (``draft_note``, the operators' words for the Mac: worker.operator_note_block), masked like her
        words: a phone or ID number as its placeholder (〈手机1〉), a 微信号 after a label masked (``contact.mask_contacts``,
        as her 百应 lines).  No note: no key (the job as it was before).  The same dict."""
        note = str(deal.get("draft_note") or "").strip()
        if note:
            meta["note"] = contact.mask_contacts(self._mask(deal["party_id"], note, now))
        return meta

    def _night_meta(self, now: float) -> dict:
        """A draft job's ``meta.night`` (``_submit``): inside the window ``{"window": "18:00-09:00", "at": "21:30"}``,
        自动模式 by day ``{"auto": True, "at": "14:30"}`` (worker.night_line words each)."""
        at = time.strftime("%H:%M", time.localtime(now))
        if self._in_window(now):
            return {"window": gates.night_window_text(self._night, "-"), "at": at}
        return {"auto": True, "at": at}

    def _night_status(self, now: float) -> dict:
        auto = self.auto_mode["on"]
        held = "自动模式" if auto and not self._in_window(now) else "下班时段"
        return night_status(self._night, now, self._clock_off(held) if self._night is not None or auto else "",
                            auto=auto)

    def _night_judge(self, meta: dict, draft, *, refused: str, short: str, unknown: list, waybill: str,
                     attachments: list, named: int, mode: str, now: float,
                     auto_on: Optional[bool] = None) -> Optional[str]:
        """A reply draft that reached the gates (``on_result``'s last step: no platform action, a reply, the deal in
        copilot mode, no newer job, nothing read late or recalled, not the system lane) and waits for a person by
        them: may it go out by itself in the night window (or 自动模式, ``_night_active``)?  None: neither is in play
        (the window off, or now outside it, and 自动模式 off; nothing is said); "": it goes (route ``gates.NIGHT``); else
        why it waits as by day (``meta.night_held``; 自动模式 by day says so, ``_auto_by_day``).
        Over ``gates.night_reason`` (complaints, ``plain_reason``, the red line, a flagged draft only as one holding
        line): this PC's clock is right, the job was made under the night rules with a clock to trust (``meta.night``:
        the Mac was told it is night or 自动模式 and wrote by the kb's rules for it; a draft of a job from before the
        window or the switch, or from while the clock was off, waits; 自动模式 by day, one of a job made in the window
        waits too: the Mac wrote by the night's words, its holding line may promise 「明早」, review 2026-10-30), it is
        a WeChat draft (another channel's, ``meta.channel``, never), its platform action was not refused or covered,
        all its files came.  ``auto_on``: the switch as ``on_result`` read it (its packet's tag says the same)."""
        auto_on = self.auto_mode["on"] if auto_on is None else auto_on
        window = self._in_window(now)
        if not window and not auto_on:
            return None
        auto = not window                              # 自动模式 by day: its own words
        if self._clock_off():
            return "这台电脑的时间和微信对不上，" + ("自动模式先不自动发" if auto else "下班时段不自动发")
        if not isinstance(meta.get("night"), dict):
            return ("草稿任务建的时候自动模式还没打开（或者电脑时间和微信对不上），Mac 起草时不知道是自动模式" if auto else
                    "草稿任务建的时候没算作下班时段（还没到点，或者电脑时间和微信对不上），Mac 起草时不知道是下班时段")
        if auto and meta["night"].get("auto") is not True:
            return "草稿任务是下班时段建的，Mac 按夜里的说法写的（过渡的话可能说「明早」），白天的自动模式不自动发"
        if str(meta.get("channel") or "wechat") not in ("wechat", GROUP_CHANNEL):     # (老板拉的群: like her private chat)
            return "不是微信私聊的草稿"
        if refused:
            # (a draft that never had an action and still says {链接}: its own reason, not 「被去掉了」: review F7)
            return "平台动作被去掉了，回复是按执行成功写的" if draft.platform_refused else refused[:200]
        if short:
            return short
        return gates.night_reason(draft.message_kind, needs_attention=draft.needs_attention, mode=mode,
                                  unknown_tokens=unknown, reason=draft.reason, text=draft.reply_draft, waybill=waybill,
                                  attachments=attachments, files_named=named, when="自动模式下" if auto else "夜里")

    def _opening_auto(self, meta: dict, draft, *, refused: str, short: str, unknown: list, waybill: str,
                      attachments: list, named: int, mode: str, recall: str, lane: str, now: float,
                      auto_on: bool) -> Optional[tuple]:
        """开场白没人处理就自动发 (PRM_OPENING_AUTO_MIN, 2026-11-02; the user: a friend who accepted after the operators had
        gone home heard nothing all weekend): may this waiting draft, our first line to a new friend, go out by itself when
        nobody decides on it?  None: not in play (the setting 0; not a first line: a 开场白 of ours, ``meta.opening``, or the
        draft of a new friend's messages from before she was registered, ``meta.new_friend``, with none read late or
        missed; a 老板拉的群's, the boss reads it there; a platform action; the system lane; a 开场白 the admin ordered in
        WeChat, ``meta.opening.held``: 合作单指令, it always waits for a person).  ``(due, "")``: it goes at
        ``due`` (``send_due_openings``), PRM_OPENING_AUTO_MIN from now, at once in the 下班时段 or 自动模式
        (``_night_active``).  ``(None, why)``: it waits for a person as before, the card says why (``meta.auto_held``):
        her recall, files short, a platform action taken out, the model's attention, what ``gates.night_reason`` minds
        (a complaint, placeholders, a red line), or a word of samples (``OPENING_AUTO_WORDS``: kb 第 0.5 节)."""
        if self._opening_auto_sec <= 0 or lane != LANE_REPLY or draft.platform is not None:
            return None
        if isinstance(meta.get(OPENING), dict) and meta[OPENING].get(OPENING_HELD):
            return None                                # (合作单指令's 「写开场白」: a person always decides)
        first = isinstance(meta.get(OPENING), dict) or (isinstance(meta.get("new_friend"), dict)
                                                        and not meta["new_friend"].get("linked"))
        if (not first or meta.get("channel") == GROUP_CHANNEL
                or any(isinstance(meta.get(k), dict) for k in ("late", "missed"))):
            return None
        if recall:
            return None, "她撤回了消息，要人看过"
        if refused:
            return None, refused
        if short:
            return None, short
        if draft.needs_attention:
            return None, "模型要求运营注意"
        why = gates.night_reason(draft.message_kind, needs_attention=False, mode=mode, unknown_tokens=unknown,
                                 reason=draft.reason, text=draft.reply_draft, waybill=waybill, attachments=attachments,
                                 files_named=named)
        if why:
            return None, why
        said = next((w for w in OPENING_AUTO_WORDS if w in (draft.reply_draft or "")), "")
        if said:
            return None, f"说到了「{said}」：寄样留到三方群由老板谈（kb 第 0.5 节），这份不自动发"
        return (now if self._night_active(now, auto_on) else now + self._opening_auto_sec), ""

    def _hand_hold(self, chat_id: str, meta: Optional[dict] = None) -> str:
        """Why a draft for this chat waits for a person whatever its kind's gate, the 下班时段, 自动模式, 开场白自动发 or
        签收后问一句 would do ("" = nothing of it holds it): the chat is a group linked by hand (接手任何合作群,
        ``BossGroups.by_hand``) and PRM_LINKED_GROUPS_AUTO is off (``HAND_GROUP_HELD``: everybody in it reads what goes
        there, decision 2026-11-10; one the scan linked by itself, 群自己接上: ``AUTO_GROUP_HELD``), or the draft answers
        the lines that waited at 接群 (its job's ``meta.link_first``, ``LINK_FIRST_HELD``), whatever the switch says.  A
        老板拉的群 the scan took over keeps its way, but one it took over by choosing whom the deal hangs on
        (PRM_BOSS_GROUP_PICK) until a person confirmed it (``PICK_UNCONFIRMED_HELD``).  PRM_GROUPS_REVIEW on: any group
        chat (``GROUPS_REVIEW_HELD``, before every other reason: they all wait then); a private chat never."""
        chat = str(chat_id or "")
        if self._groups_review and chat.endswith("@chatroom"):
            return GROUPS_REVIEW_HELD
        if not self.boss_groups.by_hand(chat):
            return PICK_UNCONFIRMED_HELD if self.boss_groups.unconfirmed(chat) else ""
        if isinstance(meta, dict) and meta.get(LINK_FIRST):
            return LINK_FIRST_HELD
        if self.boss_groups.unconfirmed(str(chat_id or "")):
            return AUTO_UNCONFIRMED_HELD           # (review 2026-11-11 #2: nobody confirmed the scan's link yet)
        if self._linked_groups_auto:
            return ""
        return AUTO_GROUP_HELD if self.boss_groups.by_scan(str(chat_id or "")) else HAND_GROUP_HELD

    def _side_line_in(self, job: Optional[dict]) -> bool:
        """她那边的人 (review 2026-11-11 #1): does this draft job hold a line of her side's other people (HER_SIDE_MARK
        before it: ``_take``; joined or carried along, its text and follow-ups: ``_job_lines``)?"""
        if not job or not self.boss_groups.her_side_on:
            return False
        return HER_SIDE_MARK in str(job.get("text") or "") or HER_SIDE_MARK in self._job_lines(job)

    def _night_sent(self, packet: dict, now: float) -> None:
        """A draft the night window (or 自动模式 by day: the packet's ``meta.auto_mode``, its record and notice say so)
        sent went out (``_deliver``): the deal's record says so, and one the model flagged (it went as a holding line)
        leaves a notice that stays until an operator says 已处理 (``night_done``); the packet keeps its
        ``needs_attention``."""
        auto = bool((packet.get("meta") or {}).get(AUTO_MODE))
        detail = {"packet": packet["id"], "kind": packet["kind"], "holding": bool(packet["needs_attention"])}
        self._store.add_event(packet["deal_id"], packet["party_id"], "night_send",
                              {**detail, **({"auto": True} if auto else {})}, actor="system", now=now)
        if packet["needs_attention"]:
            why = str(packet.get("reason") or "").strip() or "模型要求运营注意"
            self._store.set_notice(packet["deal_id"], f"{NIGHT_NOTICE}{packet['id']}",
                                   f"{'自动模式下自动' if auto else '夜间已自动'}回了过渡的话，要注意：{why}", now,
                                   packet=packet["id"], **({"auto": True} if auto else {}))
            # (the draft's own notes said just that: said once, by the notice that stays)
            self._store.drop_notices(packet["deal_id"], lambda n: n["key"] == DRAFT_NOTES + LANE_REPLY
                                     and n.get("text") == why[:NOTICE_CHARS], now)

    # ── from WeChat (the route step) ──────────────────────────────────────

    def is_party_chat(self, chat_id: str) -> bool:
        """Is this private chat a registered creator's? (the WeChat backend reads such chats)"""
        if str(chat_id) not in self._wxids:
            self._wxids = self._store.active_wxids()
        return str(chat_id) in self._wxids

    def _mask(self, party_id: int, text: str, now: float) -> str:
        return pii.mask(text, lambda k, label, value: self._store.token_for(party_id, k, label, value, now))

    def handle(self, msg: dict) -> bool:
        """True when this is a registered creator's private message (it is then dealt with, silently).

        A message read more than MAX_AGE_SEC after it was written came in while the bot was not
        reading WeChat (stopped, updated, the database re-opened; or this PC's clock is off): up
        to PRM_CATCHUP_HOURS old it is drafted as usual but the draft always waits for a person
        (``meta.late``); older, it is only recorded and the deal asks a person to look
        (``_missed``).  PRM_CATCHUP_HOURS=0: older than MAX_AGE_SEC is history, as before (a
        message Windows' edge queued, ``_queued``, never is: drafted as it always was).  A group message is not hers
        (False: the route ends it in silence), unless it is a 老板拉的群 the bot took over (``_group_message``)."""
        if msg.get("is_group", True):
            return self._group_message(msg)
        chat_id = str(msg.get("chat_id", ""))
        if chat_id not in self._wxids:
            return False
        party = self._store.party_by_wxid(chat_id)
        if party is None:
            return False
        if is_friend_notice(msg):
            # 「我通过了你的朋友验证请求，现在我们可以开始聊天了」 and 「你已添加了…」: WeChat's, not hers
            logger.info("PRM: a friend notice in party %d's chat; not drafted", party["id"])
            return True
        line = system_line(msg)
        if line is not None:
            self._system_line(msg, party, line)            # a recall, 拍一拍 …: WeChat's, not hers either
            return True
        self._take(msg, party, chat_id)
        return True

    def is_party_group(self, chat_id: str) -> bool:
        """Is this group a 老板拉的群 the bot took over (PRM_BOSS_GROUPS on)?  The route's fallback when ``handle`` fails."""
        return self.boss_groups.is_linked(str(chat_id or ""))

    def _group_party(self, chat_id: str) -> Optional[dict]:
        """Her party, when this group is a 老板拉的群 the bot took over and she is on the list."""
        return self.boss_groups.party_of(chat_id)

    def _chat_party(self, chat_id: str) -> Optional[dict]:
        """The creator whose chat this is: her private chat (her wxid), or a 老板拉的群 linked to her deal."""
        return self._store.party_by_wxid(chat_id) or self._group_party(chat_id)

    def _group_message(self, msg: dict) -> bool:
        """``handle`` for a group message (src/prm/boss_groups.py): False (not PRM's) unless the group is a 老板拉的群 the
        bot took over and she is on the list.  Then (True, always silent) her own line is drafted as in her private chat
        (``_take``: the job says it is the group, ``meta.channel`` / ``meta.group``, and its packet goes there); the boss's
        and the other members' lines are only kept, masked, for her next job (``meta.group.lines``), and one that comes
        while her group draft waits in 待审批 has it drafted again with it (``_redraft_group``; in a group linked by
        hand the boss's at once, anybody else's at most every REDRAFT_GAP_SEC); WeChat's own lines are never drafted (a
        recall that is hers, ``BossGroups.recall_is_hers``: the drafts not sent wait for a person).  The lines are kept
        on her deal for her private drafts only when the group is her deal's 三方群 (``_three_party_lines``).
        她那边的人 (PRM_GROUP_HER_SIDE on, 2026-11-11): a line of a member who is not ours nor a registered creator
        (``BossGroups.her_side``: her assistant, her 商务) is drafted as her own is (``_take`` with ``side``: the same
        job, gates, 下班时段, 自动模式, redrafts and what counts as answered; marked HER_SIDE_MARK in the job's text), and
        a recall of such a member's is hers; with the switch off it is context only, as before."""
        chat_id = str(msg.get("chat_id", ""))
        row = self.boss_groups.linked(chat_id)
        if row is None:
            deal = self.three_party.deal_of_chat(chat_id)
            return self._three_party_line(msg, deal) if deal is not None else False
        party = self._store.party(int(row["party_id"] or 0)) if row is not None else None
        if party is None or not party.get("active"):
            return False
        if not self.boss_groups.first_time(chat_id, msg.get("message_id"), sender=str(msg.get("sender_id") or "")):
            return True                                # (handed on by the route and the takeover's replay: once)
        now = self._clock()
        written = float(msg.get("timestamp", 0) or 0)
        self.boss_groups.note_line(chat_id, written or now)
        line = system_line(msg)
        if line is not None or msg.get("is_system_join") or is_friend_notice(msg):
            # (a recall: ours, or hers as ``recall_is_hers`` says, review 2026-11-09: one WeChat wrote over a line in
            # place is whoever wrote that line, else none that names a boss or another member we know; in a group linked
            # by hand a name we do not know only when she wrote there within HER_RECALL_SEC)
            recalled = self.boss_groups.sender_of(chat_id, msg.get("recalled_id")) if msg.get("recalled_id") else ""
            # (her last line there, ``last_by_chat``, is on WeChat's clock: so is the recall's, ``written``; with
            # none, now on WeChat's clock: this PC's minus the skew measured, final check 2026-11-09)
            her_last = self._her_last(self._store.deal_of(party["id"], now), chat_id)
            recent = bool(her_last) and (written or now - self._skew) - her_last <= HER_RECALL_SEC
            # (她那边的人: a recall of her side's other people is hers too; the one who wrote the recalled line may have
            # come in since the members were last read)
            hers = self.boss_groups.side_ids(row, party)
            if recalled and self.boss_groups.her_side(row, party, recalled):
                hers = hers | {recalled}
            if line is not None and (edge_text.recall_by(line) == "us"
                                     or self.boss_groups.recall_is_hers(row, party, line, sender=recalled,
                                                                        recent=recent, side=hers)):
                self._system_line(msg, party, line)
            else:
                logger.info("PRM: a WeChat system line in boss group %s…; not drafted", chat_id[:8])
            return True
        said = message_text(self.boss_groups.record_msg(msg, party, row))   # (a 聊天记录: no boss's name in it)
        if self._three_party_lines(row, chat_id, party):
            sender = str(msg.get("sender_id") or "")
            said_by = self.boss_groups.who(row, sender, str(msg.get("sender_name") or ""))
            kind = ("creator" if sender == party["wxid"] else "boss" if said_by == BOSS_WHO
                    else "us" if said_by == GROUP_US else "member")     # (us: one of ours, PRM_GROUP_OURS)
            self._group_line(party, chat_id, kind, said, self._local(written) if written else now, now)
        speaker = str(msg.get("sender_id") or "")
        # 她那边的人 (PRM_GROUP_HER_SIDE): her assistant's, her 商务's line is drafted as hers
        side = (speaker != party["wxid"] and not msg.get("is_own")
                and self.boss_groups.her_side(row, party, speaker, str(msg.get("sender_name") or "")))
        if speaker != party["wxid"] and not side:
            who = self.boss_groups.who(row, str(msg.get("sender_id") or ""), str(msg.get("sender_name") or ""))
            self.boss_groups.add_context(chat_id, who, self._mask(party["id"], said, now),
                                         self._local(written) if written else now)
            logger.info("PRM: a line of %s in boss group %s… kept for party %d's next draft (not answered)",
                        "the boss" if who == BOSS_WHO else "one of ours" if who == GROUP_US else "another member",
                        chat_id[:8], party["id"])
            if row.get("manual"):
                # 接手任何合作群 (review 2026-11-09): a big group's members do not make a Mac job each: the boss's line
                # (and one of our own people's, PRM_GROUP_OURS) has her waiting draft drafted again at once, anybody
                # else's at most every REDRAFT_GAP_SEC (one owed till then, ``sweep_owed_redrafts``) and at most
                # REDRAFT_MAX times (then her draft's card counts them)
                if who not in (BOSS_WHO, GROUP_US):
                    self._lines_since[chat_id] = self._lines_since.get(chat_id, 0) + 1
                    if self._auto_redrafts.get(chat_id, 0) >= REDRAFT_MAX:
                        self._unheard(party, chat_id)
                        return True
                    if now - self._redrafted.get(chat_id, 0.0) < REDRAFT_GAP_SEC:
                        self._redraft_owed.setdefault(chat_id, now)
                        return True
                self._group_redraft(party, chat_id, row, now,
                                    how="others" if who not in (BOSS_WHO, GROUP_US) else "boss")
                return True
            self._redraft_group(party, chat_id, row, now)
            return True
        # (her own line, or one of her side's: its draft job hears the group's lines; others' lines may have her drafts
        # drafted again; her lines a closed link of this group left unanswered: she writes there again)
        self._reset_redrafts(chat_id)
        deal = self._store.deal_of(party["id"], now)
        if any(n.get("key") == GROUP_LEFT + chat_id for n in deal["notices"]):
            self._store.drop_notices(deal["id"], lambda n: n["key"] == GROUP_LEFT + chat_id, now)
        if side:
            logger.info("PRM: a line of her side (not the registered account, %s…) in group %s… is drafted as party "
                        "%d's", speaker[:6], chat_id[:8], party["id"])
        self._take(msg, party, chat_id, group=row, side=side)
        return True

    def _her_last(self, deal: dict, chat_id: str) -> float:
        """When her side last wrote in this chat (``last_by_chat``, WeChat's clock; 0.0 never): her own lines, and with
        PRM_GROUP_HER_SIDE on her side's other people's too (SIDE_SPOKE)."""
        by_chat = deal.get("last_by_chat") or {}
        times = [by_chat.get(str(chat_id))]
        if self.boss_groups.her_side_on:
            times.append(by_chat.get(SIDE_SPOKE + str(chat_id)))
        return max([float(t or 0) for t in times])

    def _tell_admin(self, text: str) -> Optional[bool]:
        """One line said in the admin's own chat by the bot itself (群自己接上, BossGroups ``tell``): True when it went,
        False when WeChat did not take it now, None when there is no admin's chat to tell (no sender, the Mac's edge
        backend, ADMIN_WXID empty, a group's id or a registered creator's wxid: ``wechat_review.admin_problem``).
        Whatever PRM_WECHAT_REVIEW says: the answer to such a line is the admin's 「接群」 / 「不接群」 command."""
        admin = self.wechat_review.admin
        edge = str(getattr(self._config, "wechat_backend", "") or "") == "edge"
        if self._send is None or edge or admin_problem(admin, self._store):
            return None
        try:
            return bool(self._send(admin, str(text)))
        except Exception:
            logger.exception("PRM: a line about a group could not be sent to the admin's chat")
            return False

    def _auto_link_notice(self, kind: str, info: dict) -> str:
        """群自己接上 (BossGroups ``notice``): the one line the admin's chat gets about a group the scan looked at
        (``info``: the group's ``chat`` / ``name`` / ``size``, and by ``kind``: LINKED_NOTICE, her ``party`` and the
        link's answer ``out``; ASK_NOTICE, the registered ``creators`` in it; NEW_NOTICE / ACTIVE_NOTICE, the creator
        who has a linked group already as ``party``, or None).  The group's name and her 称呼 as that chat may show them
        (``WechatReview.shown`` / ``creator_name``: her platform ids by their last characters, numbers masked).  The
        answer it suggests names the group only when the name is one the 「接群」 commands would find (not empty, not
        changed by the masking); else it points at the list or the console."""
        review = self.wechat_review
        party = info.get("party")
        raw = " ".join(str(info.get("name") or "").split())
        name = review.shown(raw, party)
        # (a name cut short, review 2026-11-11 #10: the 「接群」 commands would not find it: the list or the console)
        exact = bool(name) and name == raw and not info.get("long")
        group = f"群「{name}」" if name else NO_GROUP_NAME
        size = f"（{info['size']} 人）" if info.get("size") else ""
        pick = f"回「接群 {name}」" if exact else "发「接群」从单子里选它"
        if kind in (PICKED_NOTICE, REHOOKED_NOTICE):
            return self._picked_notice(kind, info, name, exact, group, size)
        if kind == LINKED_NOTICE:
            out = info["out"]
            her = review.creator_name(party, out.get("deal_id"))
            said = self.group_links.linked_text({**info, "name": name}, {**out, "name": her})
            said = said[len("接手了："):] if said.startswith("接手了：") else said
            undo = f"不对就回「不接群 {name}」" if exact else "不对就到控制台她的合作单上点这个群的「不接了」"
            sure = (f"对的话回「确认接群 {name}」" if exact else "对的话到控制台她的合作单上点「是她的群」")
            return (f"已自动接群（{AUTO_WHY}）：{said}；{sure}（确认以前、或者你发出去一份以前，这个群的草稿都等你看）；{undo}")
        refuse = (f"不是合作群就回「不接群 {name}」（以后不再问）" if exact
                  else "不是合作群就到控制台「合作单」页顶上点「不接」")
        if kind == ASK_NOTICE:
            creators = [review.creator_name(p) for p in info.get("creators") or [] if p]
            if info.get("why"):                # (one creator, but a doubt: BossGroups._auto_doubt)
                return (f"{group}{size}里有一位登记过的达人（{'、'.join(creators) or '她'}），但{info['why']}，机器人没自动接："
                        f"{pick}选她，或者到控制台「合作单」页顶上选；{refuse}")
            return (f"{group}{size}里有 {len(creators)} 位登记过的达人（{'、'.join(creators)}），机器人不猜是谁的群，没自动接："
                    f"{pick}选一位，或者到控制台「合作单」页顶上选；{refuse}")
        why = f"（里面的 {review.creator_name(party)} 已经有接手的群，没自动接）" if party else ""
        if kind == NEW_NOTICE:
            head = f"新{group}{size}" if name else f"{NO_GROUP_NAME}{size}是新的，"
        else:
            head = f"{group}{size}有新消息，"
        return f"{head}还没接{why}：{pick}" + (f"；不是合作群就回「不接群 {name}」，以后不再提醒" if exact else "")

    def _picked_notice(self, kind: str, info: dict, name: str, exact: bool, group: str, size: str) -> str:
        """老板拉的群里机器人自己选人 (PRM_BOSS_GROUP_PICK; ``_auto_link_notice``): the admin's one line when the scan took
        a 老板拉的群 over by choosing whom its deal hangs on (PICKED_NOTICE), or moved the deal to another of her side
        because that member left (REHOOKED_NOTICE): the group, the deal's 称呼 and number (another deal of the same 称呼
        is not merged: said so), the member it hangs on and why, that everybody of her side is drafted alike and only
        that member's private chat counts, what was read of the group, and how to confirm, move it or drop it."""
        review = self.wechat_review
        party, out = info.get("party") or {}, info.get("out") or {}
        call = review.creator_name(party, out.get("deal_id")) if party.get("id") else "她"
        deal = f"合作单「{call}」（{out.get('deal_id')} 号" + ("，新开的" if out.get("registered") else "") + "）"
        why = PICK_HOW.get(str(info.get("how") or ""), "")
        sure = f"对的话回「确认接群 {name}」" if exact else "对的话到控制台她的合作单上点「是她的群」"
        change = f"挂错了人回「接群 {name}」改挂" if exact else "挂错了人到控制台「合作单」页顶上「老板拉的群」里改挂"
        undo = (f"不是合作群就回「不接群 {name}」" if exact else "不是合作群就到控制台她的合作单上点这个群的「不接了」")
        if kind == REHOOKED_NOTICE:
            was = review.shown(out.get("was") or "", party) or "原来挂的那个人"
            now_ = review.shown(out.get("now") or "", party) or "（没有名字）"
            unsure = self.boss_groups.unconfirmed(str(info.get("chat") or ""))
            return (f"{group}{size}里{deal}挂着的「{was}」退群了：改挂在「{now_}」身上（{why}）；群里她那边的人说的话照旧都起草，"
                    f"私聊只算「{now_}」的。" + (f"{sure}；" if unsure else "") + f"{change}；{undo}")
        hook = review.shown(info.get("hook") or "", party) or "（没有名字）"
        bits = [f"已自动接手老板拉的{group}{size}：{deal}挂在「{hook}」身上，因为{why}"]
        same = [s for s in info.get("same") or [] if isinstance(s, dict)]
        if same:
            bits.append(f"另有合作单也叫「{call}」（" + "、".join(f"{s['id']} 号" for s in same) + "），没合并，两边分开记")
        bits.append(f"群里她那边的 {int(info.get('others') or 0) + 1} 个人说的话都一样起草（我们自己、老板说的不起草），"
                    f"私聊只算「{hook}」的")
        caught = out.get("caught") or {}
        if not caught.get("read"):
            bits.append("群里之前的话这次读不出来：从现在起她那边说的话照常起草")
        else:
            if caught.get("context"):
                bits.append(f"群里最近 {caught['context']} 句记成了背景")
            if caught.get("waiting") and caught.get("job"):
                bits.append(f"她那边最后 {caught['waiting']} 句还没人回，派了一份草稿")
            elif caught.get("waiting") and caught.get("held"):
                bits.append(f"她那边最后 {caught['waiting']} 句还没人回：现在人工接管或暂停，只记下了，没起草")
            elif caught.get("waiting"):
                bits.append(f"她那边最后 {caught['waiting']} 句还没人回，没起草（太久以前的话）")
        if out.get("kept"):
            bits.append("她原来的三方群保留")
        bits.append("确认以前这个群的草稿都等你看")
        return "；".join(bits) + f"。{sure}；{change}；{undo}"

    def _link_catch_up(self, party: dict, chat: str, since: float) -> Optional[dict]:
        """接手任何合作群 (src/prm/group_link.py; BossGroups.link, ``catch_up``): what was said in a group an operator just
        linked to her by hand, read once (the backend's ``group_lines``: since ``since``, BossGroups.catch_up_since), is
        context for her next job (``add_context``: the boss's, the other members', ours and her own lines, 老板 /
        群成员 / 我们 / 她, with PRM_GROUP_HER_SIDE on her side's other people's as 她那边 (they wait for an answer as
        hers do), the newest GROUP_LINES_KEPT; ``meta.group.lines``) and is kept on her deal for her private
        drafts (``_group_line``: the lines it has not kept yet, her own 三方群 having been read before; only a group
        that is her deal's 三方群, ``_three_party_lines``).  No 开场白: the talk is going on.  Her lines after our last
        one there (none of ours: all of hers) are lines waiting for an
        answer: the newest LINK_TAIL of them go to one draft job, as if she wrote them now (``_take``: ``meta.link_first``,
        its draft always waits for a person, PRM_LINKED_GROUPS_AUTO or not; a person having her deal records them), the
        rest are context.  WeChat's own lines
        are skipped; a line handed on already (``first_time``) is not read twice.  ``{"read": True, "since": the second
        after the newest line read (the watch starts there), "context": how many lines kept, "waiting": how many of
        hers wait, "job": the draft job's id or None, "held": a person has her deal (her lines are only recorded)}``;
        ``{"read": False}`` when the backend cannot read it now."""
        read = getattr(self.boss_groups.backend, "group_lines", None)
        lines = None
        row = self._store.boss_group(chat) or {}
        # (她那边的人, PRM_GROUP_HER_SIDE: her side's other people count as her: their voice messages are heard, their
        # lines after our last one wait for an answer too)
        side_ids = self.boss_groups.side_ids(row, party)
        if callable(read):
            try:
                lines = read(chat, since, voices=(party["wxid"], *sorted(side_ids)))
            except Exception:
                logger.warning("PRM: group %s…: what was said there could not be read", chat[:8], exc_info=True)
        if lines is None:
            logger.info("PRM: group %s… linked by hand: its lines so far could not be read; from now on", chat[:8])
            return {"read": False}
        now = self._clock()
        deal = self._store.deal_of(party["id"], now)
        three_party = self._three_party_lines(row, chat, party)    # (her private drafts hear them, or not)
        kept_until = max([float(ev["detail"].get("at") or ev["at"])
                          for ev in self._store.events(deal["id"], limit=200, kinds=(GROUP_LINE,))
                          if str(ev["detail"].get("chat") or "") == chat] or [0.0])
        items, ours_at, newest = [], 0.0, None
        for msg in lines:
            if not isinstance(msg, dict):
                continue
            written = float(msg.get("timestamp") or 0)
            newest = written if newest is None else max(newest, written)
            if not self.boss_groups.first_time(chat, msg.get("message_id"), sender=str(msg.get("sender_id") or "")):
                continue
            if system_line(msg) is not None or msg.get("is_system_join") or is_friend_notice(msg):
                continue
            sender = str(msg.get("sender_id") or "")
            if msg.get("is_own"):
                ours_at = max(ours_at, written)
                items.append((msg, written, "us"))
            elif sender == party["wxid"]:
                items.append((msg, written, "creator"))
            elif sender in side_ids or self.boss_groups.her_side(row, party, sender, str(msg.get("sender_name") or "")):
                items.append((msg, written, "side"))
            else:
                who = self.boss_groups.who(row, sender, str(msg.get("sender_name") or ""))
                if who == GROUP_US:            # (one of our own people, PRM_GROUP_OURS: ours, it answers her too)
                    ours_at = max(ours_at, written)
                items.append((msg, written, "boss" if who == BOSS_WHO else "us" if who == GROUP_US else "member"))
        waiting = [msg for msg, written, who in items if who in ("creator", "side") and written > ours_at]
        tail = {id(msg) for msg in waiting[-LINK_TAIL:]}
        sided = {id(msg) for msg, _, who in items if who == "side"}
        kept = [item for item in items if id(item[0]) not in tail][-GROUP_LINES_KEPT:]
        labels = {"us": GROUP_US, "creator": GROUP_HER, "side": GROUP_HER_SIDE, "boss": BOSS_WHO, "member": GROUP_MEMBER}
        told = 0
        for msg, written, who in sorted(kept + [item for item in items if id(item[0]) in tail], key=lambda i: i[1]):
            at = self._local(written) if written else now
            said = message_text(self.boss_groups.record_msg(msg, party, row))
            if three_party and at > kept_until:        # (her private drafts hear the group's lines: ``_group_lines``)
                self._group_line(party, chat, "member" if who == "side" else who, said, at, now)
            if id(msg) not in tail:
                self.boss_groups.add_context(chat, labels[who], self._mask(party["id"], said, now), at)
                told += 1
        if newest is not None:
            self.boss_groups.note_line(chat, newest)
        group = self._store.boss_group(chat)
        held = bool(tail) and self._mode(self._store.deal_of(party["id"], now), now) != COPILOT
        for msg in [msg for msg in waiting if id(msg) in tail]:
            # (its draft always waits: ``_hand_hold``)
            self._take(msg, party, chat, group=group, at_link=True, side=id(msg) in sided)
        job = self._relay.store.latest_open(chat, drafts_only=True, system=False) if tail else None
        logger.info("PRM: group %s… linked by hand: %d line(s) read, %d kept as context, %d of hers wait%s", chat[:8],
                    len(items), told, len(tail), f" -> job #{job['id']}" if job else "")
        return {"read": True, "since": (newest + 1) if newest is not None else since, "context": told,
                "waiting": len(tail), "job": int(job["id"]) if job else None, "held": held}

    def _three_party_lines(self, row: dict, chat: str, party: dict) -> bool:
        """Are a linked group's lines kept on her deal (``_group_line``: her private drafts are told them,
        ``meta.three_party_chat``, ``_group_lines``)?  When it is her deal's 三方群, or a group with a boss of
        PRM_GROUP_WITH in it besides (a 老板拉的群 linked while her deal kept the 三方群 「拉三方群」 made: told apart from
        it, each group's lines labelled, second check 2026-11-09); never a group without a boss (接手任何合作群, review
        2026-11-09: her private drafts were told a group of hers and her 商务's was 「她的三方群（老板、她和我们）」)."""
        group = self._store.deal_of(int(party["id"])).get("three_party") or {}
        mine = group.get("state") == "created" and str(group.get("chat") or "") == str(chat or "")
        return mine or self.boss_groups.has_boss(row)

    def _group_closed(self, chat: str, party_id=None) -> str:
        """Why a draft for this chat may not go out now ("" = it may): a draft for a group goes out only while the bot
        drafts in it for her (review 2026-11-09): the group linked (``BossGroups.linked_party``), and to this creator
        (``party_id``; None: to anybody), not linked again to another one while her job was written.  A group the scan
        took over while PRM_BOSS_GROUPS is off (its row still linked): BOSS_GROUPS_OFF_TEXT; any other group (its link
        ended: 「不接群」, 「不接了」, 「群已经不在了」, or it went to another creator): GROUP_CLOSED_TEXT.  "" for a
        private chat."""
        chat = str(chat or "")
        if not chat.endswith("@chatroom"):
            return ""
        linked = self.boss_groups.linked_party(chat)
        if linked is not None and (party_id is None or int(linked) == int(party_id or 0)):
            return ""
        row = self._store.boss_group(chat)
        if (linked is None and row is not None and row["state"] == BOSS_LINKED and not row.get("manual")
                and not self.boss_groups.on):
            return BOSS_GROUPS_OFF_TEXT
        return GROUP_CLOSED_TEXT

    def _fail_closed(self, packet: Optional[dict], why: str, now: float) -> bool:
        """A group draft that may not go out (``_group_closed``: ``why``) fails, waiting, approved or being sent, the
        reason on its card, shown as a draft that failed (``route`` review: not 「夜间自动发出」 / 「自动发送」, review
        2026-11-09); a platform draft's action not run yet: 「平台没有动」 (``platform_not_run``), verified already
        (approved): its reply is for a person to send, and the deal says so (``platform:<id>``: the action was done, a
        sample order say, until 「我已处理」).  One the bot approved itself (a gate, the night window, 自动模式) has no
        ``decided_at`` then: no person decided it, the gate's count never takes it for a person's unchanged decision
        (「不算进门槛统计」, as ``hold_for_review``; second check 2026-11-09).  False: it was in none of those states."""
        if packet is None:
            return False
        error, outcome, done = why, "failed", False
        if packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL:
            if packet["status"] == PENDING:
                error, outcome = f"{why}，平台没有动", "platform_not_run"
            else:
                error, done = f"平台动作已经核验成功，但回复没发出去：{why}；请在微信里手动发给达人", True
        fields = {} if packet.get("route", gates.REVIEW) == gates.REVIEW else {"decided_at": None}
        if not self._store.move(packet["id"], (PENDING, APPROVED, SENDING), FAILED, outcome=outcome, sent_at=now,
                                error=error, route=gates.REVIEW, **fields):
            return False
        if done:
            self._store.set_notice(packet["deal_id"], f"platform:{packet['id']}", f"#{packet['id']} {error}", now)
        return True

    def _closed_result(self, job: dict, meta: dict, packet_id: int, unanswered: bool = True) -> bool:
        """``on_result``: the draft of a job whose group closed for her meanwhile (``_group_closed``) failed, and it
        answers nothing (review 2026-11-09: it saw to her notices, a 签收后问一句's, and merged its slots and stage into
        her deal): as a job that gave no draft (``_group_job_closed``: what it was told goes on, her lines stay that
        group's), nothing else of the deal changes.  Also one not to be sent anyway (superseded; ``unanswered`` False:
        no reply needed, her lines are none to count)."""
        what = f"{step_word(meta)}这一步的草稿" if meta.get("system_note") else "的草稿"
        self._group_job_closed(job, meta, f"#{job['id']} {what}写好时{GROUP_CLOSED_HEAD}，没发", self._clock(),
                               unanswered=unanswered)
        logger.info("PRM: job #%d -> packet %d for a group not drafted in for her any more; not sent", job["id"],
                    packet_id)
        return True

    def _group_job_closed(self, job: dict, meta: dict, head: str, now: float, unanswered: bool = True) -> None:
        """A draft job of a group whose link ended (cancelled in the queue: ``_close_group``; or back after it:
        ``_closed_result``), as a job that gave no draft (``_draft_failed``), but for her lines (second check
        2026-11-09: they were answered in her private chat): lines she wrote in a group are never carried into a draft
        of another chat; the deal says how many wait in that group (``_note_group_left``).  What it was told besides
        goes to her next draft job: the records it took (``history``: a takeover's lines, ones read too late; only a
        reply job takes them, ``take_backlog``) and how they came (``late`` / ``missed`` / ``new_friend``), on a notice
        of records only (``job`` None: her private chat's next job takes it).  The notices it carried go (their lines
        and records are its own).  A 专属佣金 step: its notice, the chain looked at again.  A job with none of her lines
        and no records (a 开场白, a 补发货盘, a 签收后问一句, a 「做手卡」: told the records, not given them) leaves
        nothing.  Said once for a job (lost first, then failed or back: its lines counted once, as ``_draft_failed``;
        final check 2026-11-09).  ``unanswered`` False: her lines are not counted (the Mac found no reply needed)."""
        deal_id, party_id, job_id = int(meta["deal_id"]), int(meta["party_id"]), int(job["id"])
        if meta.get("system_note"):
            self._draft_failed(job, meta, head, gap_notes(meta), now)
            self._chain_refresh(deal_id, now)
            return
        lines = [line for line in self._job_lines(job).split("\n") if line.strip()] if unanswered else []
        taken = not any(isinstance(meta.get(k), dict) for k in (OPENING, HUOPAN, SIGNED, HANDCARDS))
        history = [h for h in meta.get("history") or [] if isinstance(h, dict)] if taken else []
        gap = {k: meta[k] for k in ("late", "missed", "new_friend") if isinstance(meta.get(k), dict)} if taken else {}
        with self._handle_lock:                        # (``_submit`` takes these notices along)
            if any(int(ev["detail"].get("job") or 0) == job_id
                   for ev in self._store.events(deal_id, limit=50, kinds=("draft_failed",))):
                return                                 # (said once: lost first, then failed or back)
            self._store.drop_notices(deal_id, lambda n: n.get("into") == job_id, now)
            if lines:
                self._note_group_left(deal_id, str(job["chat_id"]), len(lines), now)
            if history:
                self._store.set_notice(deal_id, f"failed:{job_id}", failed_text(head, lines=False), now, job=None,
                                       lane=LANE_REPLY, head=head, lines="", gap=gap, history=history,
                                       asked_at=float(job.get("created_at") or now))
            if lines or history:
                self._store.add_event(deal_id, party_id, "draft_failed", {"job": job_id, "why": head[:200]},
                                      actor="system", now=now)

    def _note_group_left(self, deal_id: int, chat: str, count: int, now: float) -> None:
        """Her ``count`` lines in this group got no reply before its link ended: the deal's notice (``GROUP_LEFT``) says
        how many wait there, all of them so far (until 「已处理」, ``group_left_done``, or her next line there); after
        「群已经不在了」 (the row's ``why``) not 「请到群里直接回」."""
        key = f"{GROUP_LEFT}{chat}"
        have = next((n for n in (self._store.deal(deal_id) or {}).get("notices") or [] if n.get("key") == key), None)
        total = int((have or {}).get("count") or 0) + int(count)
        row = self._store.boss_group(chat) or {}
        name = str(row.get("name") or "") or "（没有群名）"
        gone = row.get("state") == BOSS_GONE and row.get("why") == BOSS_GONE_WHY     # (「群已经不在了」: no group to answer in)
        self._store.set_notice(deal_id, key, f"她在群「{name}」里还有 {total} 句话没回（"
                               + ("群已经不在了），需要的话请在微信里找她" if gone else
                                  f"{GROUP_CLOSED_HEAD}），要回的话请到群里直接回"), now, count=total, chat=chat)

    def _left_lines(self, deal_id: int, chat: str, party: dict, now: float) -> int:
        """``_close_group``: the notices of this group's jobs that gave no draft before (``_draft_failed``) keep her
        lines for a later draft of the group, which will not come: their lines are counted (returned), what they kept
        besides (``history``, the gap) goes to her next draft job, on a notice of records only."""
        count = 0

        def edit(notices):
            nonlocal count
            out = []
            for n in notices:
                if (str(n["key"]).startswith("failed:") and n.get("lane") == LANE_REPLY and n.get("job")
                        and self._job_in(n.get("job"), chat, party)
                        and not (n.get("into") and not self._cancelled(n["into"]))):   # (into a job being written: its)
                    count += len([line for line in str(n.get("lines") or "").split("\n") if line.strip()])
                    if n.get("history"):
                        out.append({**n, "lines": "", "job": None, "into": None,
                                    "text": failed_text(n.get("head") or n["text"], lines=False)})
                    continue
                out.append(n)
            return out

        self._store.edit_notices(deal_id, edit, now)
        return count

    def _close_group(self, chat: str, now: float) -> int:
        """A group's link ended (``BossGroups._drop``: 「不接群」, 「不接了」, 「群已经不在了」): its drafts waiting or approved
        fail (GROUP_CLOSED_TEXT, the card says so: ``_fail_closed``) and its draft jobs no worker has taken are
        cancelled (``_group_job_closed``: the records they took go to her next job; a 专属佣金 step's notice, the chain
        looked at again; one being written: the same when it comes back, ``on_result``; a platform execution is never
        cancelled: its reply fails at the send).  Her lines there nobody answered (in those drafts and jobs, kept by
        notices of jobs that gave no draft, ``_left_lines``) are never carried into another chat's draft: the deal says
        how many (``_note_group_left``).  Returns how many drafts."""
        row = self._store.boss_group(chat) or {}
        party = self._store.party(int(row["party_id"])) if row.get("party_id") else None
        failed = unanswered = 0
        for p in self._store.packets((PENDING, APPROVED), limit=500):
            if p["chat_id"] != chat or not self._fail_closed(p, GROUP_CLOSED_TEXT, now):
                continue
            failed += 1
            job = self._relay.store.get(int(p["job_id"])) if p.get("job_id") else None
            if job is not None and (p.get("meta") or {}).get("lane") != LANE_SYSTEM:
                unanswered += len([line for line in self._job_lines(job).split("\n") if line.strip()])
        cancelled = 0
        for job in self._relay.store.recent(chat, limit=50):
            meta = job_meta(job)
            if (job.get("kind") != PRM or job.get("status") != "pending" or meta.get("op")
                    or not self._relay.store.cancel_if_pending(int(job["id"]), now)):
                continue
            cancelled += 1
            # (review 2026-11-09, two checks: what it took is not lost, her lines stay this group's: _group_job_closed)
            if meta.get("deal_id") and meta.get("party_id"):
                what = f"{step_word(meta)}这一步的草稿任务" if meta.get("system_note") else "的草稿任务"
                self._group_job_closed(job, meta, f"#{job['id']} {what}取消了（{GROUP_CLOSED_HEAD}）", now)
        if party is not None and row.get("deal_id"):
            unanswered += self._left_lines(int(row["deal_id"]), chat, party, now)
            if unanswered:
                self._note_group_left(int(row["deal_id"]), chat, unanswered, now)
        self._reset_redrafts(chat)
        if failed or cancelled:
            logger.info("PRM: group %s… is not drafted in any more: %d draft(s) not sent, %d job(s) cancelled",
                        chat[:8], failed, cancelled)
        return failed

    def sweep_owed_redrafts(self) -> int:
        """接手任何合作群 (review 2026-11-09): the redrafts owed in groups linked by hand (``_group_message``: others' lines
        that came within REDRAFT_GAP_SEC of the last one) once that gap is over: her waiting group draft drafted again
        with the group's lines now (``_redraft_group``).  Returns how many groups."""
        if not self._redraft_owed:
            return 0
        now = self._clock()
        done = 0
        for chat in list(self._redraft_owed):
            if now - self._redrafted.get(chat, 0.0) < REDRAFT_GAP_SEC:
                continue
            self._redraft_owed.pop(chat, None)
            row = self.boss_groups.linked(chat)
            party = self._store.party(int(row["party_id"] or 0)) if row is not None and row.get("party_id") else None
            if party is None or not party.get("active"):
                continue
            if self._auto_redrafts.get(chat, 0) >= REDRAFT_MAX:
                self._unheard(party, chat)             # (drafted again often enough: the card counts the lines)
                continue
            if self._group_redraft(party, chat, row, now, how="others"):
                done += 1
        return done

    def _group_redraft(self, party: dict, chat: str, row: dict, now: float, how: str) -> bool:
        """A group linked by hand: her waiting draft drafted again (``_redraft_group``), noted for the gap and the cap
        (``how``: "others", their lines had it drafted again, one of REDRAFT_MAX; "boss", his line: not counted;
        "console", 「重新起草」: counted from 0 again)."""
        if not self._redraft_group(party, chat, row, now):
            return False
        self._redrafted[chat] = now
        self._redraft_owed.pop(chat, None)
        self._lines_since.pop(chat, None)
        if how == "others":
            self._auto_redrafts[chat] = self._auto_redrafts.get(chat, 0) + 1
        elif how == "console":
            self._auto_redrafts.pop(chat, None)
        return True

    def _unheard(self, party: dict, chat: str) -> None:
        """REDRAFT_MAX redrafts done by others' lines: her group drafts waiting in 待审批 say how many lines of the group
        came since they were drafted (``meta.unheard``: the card offers 「重新起草」), none is drafted again."""
        count = self._lines_since.get(chat, 0)
        self._redraft_owed.pop(chat, None)
        if count <= 0:
            return
        deal = self._store.deal_of(int(party["id"]))
        for packet in self._waiting_group_drafts(deal["id"], chat):
            self._store.note_packet(packet["id"], {"unheard": count})

    def _reset_redrafts(self, chat: str) -> None:
        """A group's redraft counters start again (``_group_redraft``, REDRAFT_MAX, ``_unheard``): her new line there,
        her waiting draft there decided (sent, dropped), the group closed."""
        for kept in (self._redraft_owed, self._auto_redrafts, self._lines_since):
            kept.pop(chat, None)

    def _waiting_group_drafts(self, deal_id: int, chat: str) -> list:
        """Her drafts for this group waiting in 待审批 (``_redraft_group``): a job's, not 百应's nor a 专属佣金 step's."""
        return [p for p in self._store.deal_packets(deal_id, statuses=(PENDING,))
                if p["chat_id"] == chat and p.get("job_id") is not None and not is_buyin(p)
                and (p.get("meta") or {}).get("lane") != LANE_SYSTEM]

    def _redraft_by_hand(self, packet: dict, now: float) -> dict:
        """The console's 「重新起草」 on her group draft waiting in 待审批 (review 2026-11-09: once others' lines had it
        drafted again REDRAFT_MAX times it says how many lines it did not hear): drafted again with the group's lines
        now (``_group_redraft``), the cap counted from 0 again.  Only one of her reply drafts there
        (``_waiting_group_drafts``); a 开场白 only when it would be written again (``_group_opening_why``: else it stays,
        the answer says why: never ok for what did not happen)."""
        chat = str(packet["chat_id"])
        row = self.boss_groups.linked(chat)
        if (packet["status"] != PENDING or not is_group_packet(packet) or row is None
                or int(row.get("party_id") or 0) != int(packet["party_id"])):
            return {"ok": False, "error": "只有群里还在「待审批」等着的草稿能重新起草"}
        waiting = self._waiting_group_drafts(packet["deal_id"], chat)
        if packet["id"] not in {p["id"] for p in waiting}:
            # (second check 2026-11-09: a shipping notice, a 专属佣金 step answered ok and drafted her other draft again)
            return {"ok": False, "error": "只有她在群里的回复草稿能重新起草（发货通知、开专属佣金这一步不行）"}
        party = self._store.party(int(packet["party_id"]))
        if party is None or not party.get("active"):
            return {"ok": False, "error": "这位达人已经停用，不起草"}
        if (packet.get("meta") or {}).get(OPENING):
            why = self._group_opening_why(party, chat, self._store.deal_of(party["id"], now), now,
                                          ignore={p["id"] for p in waiting})
            if why:
                return {"ok": False, "error": f"这份开场白没重新起草：{why}"}
        if not self._group_redraft(party, chat, row, now, how="console"):
            return {"ok": False, "error": "这份草稿没能重新起草（人工接管或暂停时不起草，或者它已经处理过了）"}
        logger.info("PRM: packet %d drafted again (the console)", packet["id"])
        return {"ok": True, "status": "redrafted"}

    def _three_party_line(self, msg: dict, deal: dict) -> bool:
        """``handle`` for a line of her own 三方群 (the one 「拉三方群」 made, ``ThreeParty.deal_of_chat``; 2026-11-02): read
        only.  Always True and silent: never a draft job, never a reply, no holding reply; the line is kept on her deal
        for her private drafts (``_group_line``), WeChat's own lines (joins, recalls, 拍一拍) are not.  A deal whose
        creator was removed from the list keeps nothing.  The same line handed on twice (a catch-up) is kept once."""
        chat_id = str(msg.get("chat_id", ""))
        party = self._store.party(int(deal.get("party_id") or 0))
        if party is None or not party.get("active"):
            return True
        if not self._first_group_line(chat_id, msg.get("message_id")):
            return True
        if system_line(msg) is not None or msg.get("is_system_join") or is_friend_notice(msg):
            return True
        now = self._clock()
        written = float(msg.get("timestamp", 0) or 0)
        sender = str(msg.get("sender_id") or "")
        said_by = self.boss_groups.who({}, sender, str(msg.get("sender_name") or ""))
        who = ("creator" if sender == party["wxid"] else "boss" if said_by == BOSS_WHO
               else "us" if said_by == GROUP_US else "member")     # (us: one of ours, PRM_GROUP_OURS)
        self._group_line(party, chat_id, who, message_text(self.boss_groups.record_msg(msg, party)),
                         self._local(written) if written else now, now)
        logger.info("PRM: a line in party %d's 三方群 %s… kept for her private drafts (read only, not answered)",
                    party["id"], chat_id[:8])
        return True

    def _own_three_party_line(self, msg: dict, deal: dict) -> None:
        """This account's own line in her 三方群 (``on_own_message``): the bot never writes there, so it is an operator's;
        kept as ours (``_group_line``, who ``us``) for her private drafts.  No takeover: the group is where the boss and
        the operators talk, her private chat stays the copilot's.  WeChat's own lines there are not kept."""
        party = self._store.party(int(deal.get("party_id") or 0))
        if party is None or not party.get("active") or system_line(msg) is not None:
            return
        chat_id = str(msg.get("chat_id", ""))
        if not self._first_group_line(chat_id, msg.get("message_id")):
            return
        now = self._clock()
        written = float(msg.get("timestamp", 0) or 0)
        self._group_line(party, chat_id, "us", message_text(self.boss_groups.record_msg(msg, party)),
                         self._local(written) if written else now, now)
        logger.info("PRM: our own line in party %d's 三方群 %s… kept for her private drafts (no takeover)",
                    party["id"], chat_id[:8])

    def _first_group_line(self, chat_id: str, message_id) -> bool:
        """Is this line of her 三方群 new to this run (its message id; a line without one always is)?"""
        if message_id in (None, ""):
            return True
        key = str(message_id)
        with self._group_seen_lock:
            seen = self._group_seen.setdefault(chat_id, deque(maxlen=500))
            if key in seen:
                return False
            seen.append(key)
            return True

    def _group_line(self, party: dict, chat_id: str, who: str, text: str, at: float, now: float) -> None:
        """One line of her 三方群 kept on her deal (``GROUP_LINE``): who wrote it (creator | us | boss | member), when
        (this PC's clock), the words masked like hers (phone and ID numbers as her placeholders) with the bosses' names
        as PRM_BOSS_CALL, at most GROUP_LINE_CHARS."""
        cap = RECORD_LINE_CHARS if str(text or "").startswith(edge_text.RECORD_PREFIX) else GROUP_LINE_CHARS
        said = self._mask(party["id"], self.boss_groups.hide(str(text or "")), now)[:cap]
        if not said.strip():
            return
        deal = self._store.deal_of(party["id"], now)
        label = self.boss_groups.call if who == "boss" else GROUP_WHO_LABELS.get(who, "群里的人")
        self._store.add_event(deal["id"], party["id"], GROUP_LINE,
                              {"chat": chat_id, "who": who, "label": label, "text": said, "at": float(at)},
                              actor="system", now=now)

    def _group_lines(self, deal_id: int) -> list[dict]:
        """What a draft job of her private chat is told of her 三方群 (``meta.three_party_chat``): the latest
        GROUP_LINES_TOLD lines kept (``_group_line``), oldest first, ``{"who": label, "at": "MM-DD HH:MM", "text"}``;
        [] for none.  The groups told: her deal's 三方群 (its chat known) and the groups with a boss linked to the deal
        besides (``_three_party_lines``); none of them: every line kept, as before.  Lines of more than one group: each
        says its group (``group``: 「三方群「…」」 / 「群「…」」, bosses' names as PRM_BOSS_CALL; second check 2026-11-09:
        mixed, or dropped); one group's: no ``group`` (the Mac's prompt as before)."""
        deal = self._store.deal(deal_id) or {}
        group = deal.get("three_party") or {}
        main = str(group.get("chat") or "") if group.get("state") == "created" else ""
        # (review 2026-11-11 #7: the groups' names masked for the Mac, ``mask_name``)
        labels = ({main: f"三方群「{mask_name(self.boss_groups.hide(group.get('name') or '')) or '（没有群名）'}」"}
                  if main else {})
        for row in self._store.boss_groups((BOSS_LINKED,), deal_id=deal_id):
            if row["chat"] not in labels and self.boss_groups.has_boss(row):
                labels[row["chat"]] = f"群「{mask_name(self.boss_groups.hide(row.get('name') or '')) or '（没有群名）'}」"
        events = self._store.events(deal_id, limit=GROUP_LINES_SCAN if labels else GROUP_LINES_TOLD,
                                    kinds=(GROUP_LINE,))
        lines = [ev for ev in events if not labels or str(ev["detail"].get("chat") or "") in labels][:GROUP_LINES_TOLD]
        several = len({str(ev["detail"].get("chat") or "") for ev in lines}) > 1
        out = []
        for ev in sorted(lines, key=lambda e: (float(e["detail"].get("at") or e["at"]), e["id"])):
            detail = ev["detail"]
            item = {"who": str(detail.get("label") or GROUP_WHO_LABELS.get(str(detail.get("who") or ""), "群里的人")),
                    "at": _clock_text(float(detail.get("at") or ev["at"])), "text": str(detail.get("text") or "")}
            if several:
                item["group"] = labels.get(str(detail.get("chat") or ""), "另一个群")
            out.append(item)
        return out

    def _group_since(self, deal: dict) -> float:
        """Where the watch of her 三方群 starts after a restart (``ThreeParty.watch_all``): a second after its last line
        kept, or shortly before the group was made (THREE_PARTY_WATCH_BACK_SEC) when none is."""
        chat = str((deal.get("three_party") or {}).get("chat") or "")
        kept = [float(ev["detail"].get("at") or ev["at"])
                for ev in self._store.events(deal["id"], limit=50, kinds=(GROUP_LINE,))
                if str(ev["detail"].get("chat") or "") == chat]
        if kept:
            return max(kept) + 1
        return float((deal.get("three_party") or {}).get("at") or self._clock()) - THREE_PARTY_WATCH_BACK_SEC

    def _redraft_group(self, party: dict, chat_id: str, row: dict, now: float) -> bool:
        """The boss (or another member) said something in her 老板拉的群 while her draft for the group waits in 待审批
        (pending: not approved, not sent): that draft did not hear it, so it is out of date and drafted again with the
        group's lines now, as her own new line there would (``_submit`` with no line: her lines of it go along); the
        group's 开场白 is asked for again (``_group_opening``).  Her private drafts, a 专属佣金 step, a draft approved
        already, and a draft while a person has her deal are left as they are.  True: one was drafted again."""
        opening = False
        with self._handle_lock:
            deal = self._store.deal_of(party["id"], now)
            if self._mode(deal, now) != COPILOT:
                return False
            waiting = self._waiting_group_drafts(deal["id"], chat_id)
            if not waiting:
                return False
            jobs = [self._relay.store.get(int(p["job_id"])) for p in waiting]
            if any(job is not None and self._job_lines(job) for job in jobs):
                self._submit(party, chat_id, None, (), now, 0.0, False, now, group=row)
            elif any((p.get("meta") or {}).get(OPENING) for p in waiting):
                # (second check 2026-11-09: superseded first, a 开场白 that would not be written again was gone: it stays)
                if self._group_opening_why(party, chat_id, deal, now, ignore={p["id"] for p in waiting}):
                    return False
                self._store.supersede(deal["id"], now, lane=LANE_REPLY, chat_id=chat_id)
                opening = True
            else:
                return False
        if opening:
            job_id, why = self._group_opening(party, chat_id)
            logger.info("PRM: boss group %s…: the 开场白 waiting for a person is drafted again with the new line there%s",
                        chat_id[:8], f" (job #{job_id})" if job_id else f"; not: {why}")
        return True

    def on_chat_notice(self, chat_id: str, text: str, at: Optional[float] = None, event_id=None) -> None:
        """Mac 端（edge 后端）：Windows 的微信边缘报来的私聊系统提示（``chat_notice``：撤回、拍一拍…；
        新好友那几句提示也是这样报的）。和 Windows 本机读到的系统行一样处理（``_system_line``）；
        新好友的提示照旧不管。"""
        chat_id = str(chat_id or "")
        if not self.is_party_chat(chat_id):
            return
        party = self._store.party_by_wxid(chat_id)
        msg = {"chat_id": chat_id, "msg_type": edge_text.SYSTEM_TYPE, "content": str(text or ""),
               "special": {"kind": "system", "title": str(text or "")}, "timestamp": at or 0, "is_group": False,
               "edge_event_id": event_id if event_id not in (None, "") else "chat_notice"}
        if party is None or is_friend_notice(msg):
            return
        self._system_line(msg, party, str(text or ""))

    def _system_line(self, msg: dict, party: dict, line: str, own: bool = False) -> None:
        """One of WeChat's own lines in a creator's chat (win review F1): never a draft, and the drafts
        waiting stay as they are (it is no new message from her).  Her recall: the drafts not sent yet
        wait for a person (they may answer what she took back), so does the draft of a job made before
        it (``on_result``), and the deal says so.  Our own recall (「你撤回了一条消息」, ``own``: handed
        on as this account's line) is only recorded; any other line (拍一拍, a red packet notice) only
        logged.  Older than a message of hers would still be drafted (PRM_CATCHUP_HOURS): history."""
        now = self._clock()
        written = float(msg.get("timestamp", 0) or 0)
        age = now - self._local(written) if written else 0.0
        written = written or now
        if age > MAX_AGE_SEC and ((not self._catchup_sec and not _queued(msg))
                                  or (self._catchup_sec and age > self._catchup_sec)):
            return
        who = edge_text.recall_by(line)
        if not who:
            logger.info("PRM: a WeChat system line in party %d's chat; not drafted", party["id"])
            return
        ours = own or who == "us"
        with self._handle_lock:
            deal = self._store.deal_of(party["id"], now)
            self._store.add_event(deal["id"], party["id"], "recall", {"by": "operator" if ours else "creator",
                                                                       "at": written},
                                  actor="operator" if ours else "creator", now=now)
            if ours:
                logger.info("PRM: an operator recalled a line in party %d's chat; recorded", party["id"])
                return
            note = recall_note(written)
            held = self._store.hold_for_review(deal["id"], note, now)
            # until an operator decides on one of her drafts or answers her by hand (``_person_decided``)
            self._store.set_notice(deal["id"], "recall", note, now, recalled_at=written)
        logger.info("PRM: party %d recalled a message; %d draft(s) not sent yet wait for a person", party["id"], held)

    def _recalled_since(self, deal: dict, since: float) -> Optional[float]:
        """When the creator took a message back, if a draft made at ``since`` must wait for a person for
        it: her latest recall came at ``since`` or later (a job made before it may hold what she
        recalled), or no operator has seen to her recall yet (its notice is there); else None."""
        for ev in self._store.events(deal["id"], limit=10, kinds=("recall",)):
            if ev["detail"].get("by") == "creator":
                if float(ev["at"]) >= since:
                    return float(ev["detail"].get("at") or ev["at"])
                break
        notice = next((n for n in deal.get("notices") or [] if n.get("key") == "recall"), None)
        return float(notice.get("recalled_at") or notice["at"]) if notice else None

    def _take(self, msg: dict, party: dict, chat_id: str, group: Optional[dict] = None, at_link: bool = False,
              side: bool = False) -> None:
        """``handle`` for a registered creator's message.  What is recorded and the job it joins
        are decided under ``_handle_lock``; a picture or file is fetched outside it (that may wait
        for the WeChat window, and the backend's own lines wait for the lock).  ``group``: the row of the
        老板拉的群 she wrote in (``_group_message``; ``chat_id`` is the group's).  ``at_link``: one of her lines that
        waited when the group was linked by hand (``_link_catch_up``: ``_submit``'s ``at_link``).  ``side`` (她那边的人,
        PRM_GROUP_HER_SIDE): the line is not the registered account's but one of her side's other people's in that
        group: it is taken as hers, HER_SIDE_MARK before its words wherever they go (the job, the deal's record)."""
        now = self._clock()
        sent_at = float(msg.get("timestamp", 0) or 0)
        age = now - self._local(sent_at) if sent_at else 0.0     # (on this PC's clock: win review F6)
        late = age > MAX_AGE_SEC
        if late and not self._catchup_sec:
            if not _queued(msg):
                logger.info("PRM: an old message of party %d (%.0f s) is history, not drafted", party["id"], age)
                return
            late = False                   # Windows' queue never replays history: drafted as it always was
        at = sent_at or now
        if group is not None or self.boss_groups.masks(party):
            # 老板拉的群: the bosses' names never go to the Mac (PRM_BOSS_CALL), a 聊天记录 she forwards included
            line = self.boss_groups.hide(message_text(self.boss_groups.record_msg(msg, party, group)))
        else:
            line = message_text(msg)
        if side and group is not None:
            line = HER_SIDE_MARK + line
        if late and age > self._catchup_sec:
            with self._handle_lock:
                self._missed(party, self._store.deal_of(party["id"], now), line, at, now, chat_id=chat_id, side=side)
            return
        with self._handle_lock:
            if self._recorded_for_a_person(party, line, at, now, chat_id=chat_id, side=side):
                return
        files: tuple = ()
        kind = attachment_kind(msg)
        if kind:
            collected, problems = self._relay.collect([Held(kind, msg, now)])
            files = tuple(collected)
            if problems:
                line += "（" + "；".join(problems) + "）"
            elif not files:
                line += f"（{ATTACHMENT_KINDS[kind]}没取到）"
        with self._handle_lock:
            if not self._recorded_for_a_person(party, line, at, now, chat_id=chat_id, side=side):   # (a person may have
                # it now)
                self._submit(party, chat_id, line, files, at, age, late, now, group=group, at_link=at_link, side=side)

    def _recorded_for_a_person(self, party: dict, line: str, at: float, now: float, chat_id: str = "",
                               side: bool = False) -> bool:
        """A person has the chat (taken over, paused): the message is recorded for the next draft,
        nothing queued; True then (under ``_handle_lock``).  ``chat_id``: where she wrote (her private chat when
        empty); ``side``: a line of her side's other people (``_wrote``)."""
        deal = self._store.deal_of(party["id"], now)
        mode = self._mode(deal, now)
        if mode == COPILOT:
            return False
        text = self._mask(party["id"], f"[{_clock_text(at)}] {line}", now)
        self._store.add_event(deal["id"], party["id"], "creator_message", {"text": text},
                              actor="creator", forwarded=False, now=now)
        self._store.update_deal(deal["id"], now, **_wrote(deal, chat_id or party["wxid"], at, side=side))
        self._store.supersede(deal["id"], now, lane=LANE_REPLY)
        logger.info("PRM: party %d writes while the deal is %s; recorded, not drafted", party["id"], mode)
        return True

    def _submit(self, party: dict, chat_id: str, line: Optional[str], files: tuple, at: float, age: float,
                late: bool, now: float, new_friend: Optional[dict] = None, group: Optional[dict] = None,
                at_link: bool = False, side: bool = False) -> None:
        """The draft job of a creator's message (under ``_handle_lock``): the waiting drafts are
        out of date, the deal hears of the message, the job is made or joined.  ``new_friend``
        (``{"from", "count"}``, take_new_friend): a new friend's messages from before she was
        registered, one job of their own that waits for a person.  ``group``: she wrote in a 老板拉的群 the bot
        took over (``chat_id`` is the group's): the job says so (``meta.channel`` "group", ``meta.group``), a 开场白 of
        that group not drafted yet is out of date (her line is a reply job of its own); ``at_link`` (a group linked by
        hand, ``_link_catch_up``): one of her lines that waited at 接群, all of them one job of their own whose draft
        always waits (``meta.link_first``, ``_hand_hold``; like ``late``: a line read on time may join it; a job that
        takes its lines along, its draft outdated or never written, keeps the mark).  A draft of a group linked by hand
        that always waits (``_hand_hold``) is never told it is night (no ``meta.night``: the Mac writes as by day).  A
        creator with such a group
        writes in two chats: a message puts out of date only the drafts of the chat it came in (``_lane_chat``), each
        reply goes where she wrote.  ``line`` None (``_redraft_group``): no new line of hers, the boss or another member
        said something there while her group draft waited: that draft is out of date and her lines in it are drafted
        again, with the group's lines now.  ``side`` (她那边的人): the line is one of her side's other people's (the deal
        keeps its time apart, ``_wrote``).

        Her lines of the reply drafts this message puts out of date (they waited in 待审批, nobody sent
        them: ``_outdated``) and of draft jobs that gave no draft (``failed:`` notices, mac review F1) go
        before it, oldest job first, so the new draft answers them too; the job is told those drafts
        never went out (``meta.sent``, ``_outbound``'s ``unsent``).  Before (real machine, 2026-10-28) the
        new job had only her new line: 「请问佣金比例是多少呀」's draft waited, she sent a 拍一拍, and the
        question was never answered."""
        deal = self._store.deal_of(party["id"], now)
        text = self._mask(party["id"], f"[{_clock_text(at)}] {line}", now) if line is not None else ""
        waiting = self._store.deal_packets(deal["id"], statuses=(PENDING,))
        lane_chat = self._lane_chat(party["id"], chat_id)
        stale = self._store.supersede(deal["id"], now, lane=LANE_REPLY, chat_id=lane_chat)   # a 专属佣金 step stays
        if line is not None and any(n.get("key") == UNREACHABLE for n in deal["notices"]):
            self._store.drop_notices(deal["id"], lambda n: n["key"] == UNREACHABLE, now)    # she wrote: reachable
        outdated = self._outdated(waiting, now) if stale else []
        if line is not None:
            self._store.update_deal(deal["id"], now, **_wrote(deal, chat_id, at, side=side))
        meta = {"party_id": party["id"], "deal_id": deal["id"], "name": party["name"]}
        if group is not None:
            meta["channel"] = GROUP_CHANNEL            # 老板拉的群: the Mac writes for the group, its packet goes there
            meta["group"] = self.boss_groups.job_group(chat_id)
        creator = self._creator_ids(deal["id"])
        if creator:
            meta["creator"] = creator                  # her 抖音号: the Mac looks up the invite ledger by it
        made = self.three_party.job_meta(deal)         # (not ``group``: that is the 老板拉的群 she wrote in)
        if made:
            meta["three_party"] = made                 # 三方群 made: the case file's 【待拉三方群】 is done
        if group is None:
            told = self._group_lines(deal["id"])
            if told:
                meta["three_party_chat"] = told         # what was said in her 三方群 lately (read only, ``_group_line``)
        outbound = self._outbound(deal["id"], party["id"], now, unsent=[packet for packet, _, _ in outdated])
        if outbound:
            meta["sent"] = outbound
        backlog = self._store.take_backlog(deal["id"])
        fresh = bool(backlog)
        if backlog:
            meta["history"] = history_of(backlog)
            missed = [ev for ev in backlog if ev["kind"] == "creator_message" and ev["detail"].get("missed")]
            if missed:
                meta["missed"] = {"count": len(missed), "from": min(_event_time(ev) for ev in missed)}
        if late:
            # Read late: the draft always waits for a person (on_result).  A job of messages read
            # on time does not take it in; the next messages read late join this one.
            meta["late"] = {"from": at}
            if not fresh:
                open_job = self._relay.store.latest_open(chat_id, drafts_only=True, system=False)
                fresh = open_job is None or not job_meta(open_job).get("late")
            logger.warning("PRM: party %d's message was read %.0f min after it was written (the bot was not "
                           "reading WeChat, or this PC's clock is off); its draft waits for a person",
                           party["id"], age / 60)
        if new_friend:
            # Like ``late``: her messages from before the registration are one job, always reviewed.
            meta["new_friend"] = dict(new_friend)
            if not fresh:
                open_job = self._relay.store.latest_open(chat_id, drafts_only=True, system=False)
                fresh = open_job is None or not job_meta(open_job).get("new_friend")
        if at_link and group is not None:
            # 接群: her lines that waited are one job of their own (its meta reaches the Mac), always reviewed
            meta[LINK_FIRST] = True
            if not fresh:
                open_job = self._relay.store.latest_open(chat_id, drafts_only=True, system=False)
                fresh = open_job is None or not job_meta(open_job).get(LINK_FIRST)
        # Her lines of draft jobs that gave no draft (mac review F1), and of the drafts this message put out
        # of date (``outdated``): answered with this one, before it, oldest job first.  Only a job's of this chat
        # (review 2026-11-09: her lines in a group were answered in her private chat once its link ended; a notice of
        # records only, ``job`` None, goes to her private chat's)
        carried = sorted((n for n in deal["notices"] if str(n["key"]).startswith("failed:")
                          and n.get("lane") == LANE_REPLY and (n.get("lines") or n.get("history"))
                          and (not n.get("into") or self._cancelled(n["into"]))
                          and self._job_in(n.get("job"), chat_id, party)), key=lambda n: int(n.get("job") or 0))
        earlier = sorted([*((int(n.get("job") or 0), str(n["lines"])) for n in carried if n.get("lines")),
                          *((int(job["id"]), lines) for _, job, lines in outdated if lines)], key=lambda pair: pair[0])
        if earlier:
            text = "\n".join([*(lines for _, lines in earlier), *([text] if text else [])])
        if carried:
            told = [h for n in carried for h in n.get("history") or [] if isinstance(h, dict)]
            if told:
                meta["history"] = [*told, *meta.get("history", [])]
        for gap in [n.get("gap") or {} for n in carried] + [job_meta(job) for _, job, _ in outdated]:
            for key, info in gap.items():
                if key in ("late", "missed", "new_friend") and isinstance(info, dict):
                    meta[key] = _merged_gap(meta.get(key), info)
            if group is not None and gap.get(LINK_FIRST) is True:
                meta[LINK_FIRST] = True                # (the lines that waited at 接群 go along: it still waits)
        if carried or outdated:
            fresh = True
        if (not any(isinstance(meta.get(k), dict) for k in ("late", "missed", "new_friend")) and self._night_active(now)
                and not (group is not None and (side or self._hand_hold(chat_id, meta)))):
            # 下班时段 (or 自动模式): the Mac writes by the kb's rules for it (worker.prm_prompt), its draft may go out by
            # itself (``_night_judge``).  Not for a job that always waits for a person (read late, a new friend's, a
            # group linked by hand's: ``_hand_hold``).
            meta["night"] = self._night_meta(now)
        if (self._opening_auto_sec and group is None and isinstance(meta.get("new_friend"), dict)
                and not meta["new_friend"].get("linked")
                and not any(isinstance(meta.get(k), dict) for k in ("late", "missed"))):
            meta["opening_auto"] = self._opening_auto_sec // 60   # the Mac hears its draft may go out by itself (worker)
        if group is not None:
            opening = self._relay.store.latest_open(chat_id, drafts_only=True, system=False)
            if opening is not None and isinstance(job_meta(opening).get(OPENING), dict):
                # she spoke in the group: the reply answers her, our 开场白 there is out of date (not drafted at all if
                # the Mac has not taken it; drafted, ``on_result`` finds it outdated, ``_opening_outdated``)
                self._relay.store.cancel_if_pending(int(opening["id"]), now)
                fresh = True
        how, job_id = self._relay.submit(chat_id, chat_id, text, kind=PRM, files=files, settle_sec=SETTLE_SEC,
                                         meta=self._reply_meta(self._with_note(meta, deal, now)), fresh=fresh)
        for n in carried:
            self._store.set_notice(deal["id"], n["key"], failed_text(n.get("head") or n["text"], job_id,
                                                                     lines=bool(n.get("lines"))), now,
                                   into=job_id)
        logger.info("PRM: party %d %s -> job #%d (%s)%s%s%s%s", party["id"],
                    "message" if line is not None else "group draft drafted again (a new line of the others there)",
                    job_id, how,
                    f"; {stale} waiting draft(s) out of date" if stale else "",
                    f"; {len(backlog)} recorded line(s) told (a takeover, or read too late)" if backlog else "",
                    f"; the lines of {len(carried)} draft job(s) that gave no draft go along" if carried else "",
                    f"; the lines of {len(outdated)} draft(s) not sent go along" if outdated else "")

    def _outdated(self, waiting: list, now: float) -> list:
        """The Mac's reply drafts of ``waiting`` (the deal's drafts in 待审批 a moment ago) that a message of hers
        has just put out of date (``superseded`` now: nobody sent them), with their jobs and her lines in them
        (the job's text and the follow-ups the Mac had: ``_job_lines``): ``[(packet, job, lines)]``; a 开场白, a
        补发货盘 or a 签收后问一句 with no line of hers too (``lines`` "": the job answering her is told it was not sent).
        Not a 专属佣金 step's (the system lane is no reply of hers), nor one approved or dropped meanwhile."""
        out = []
        for packet in waiting:
            if packet.get("job_id") is None or (packet.get("meta") or {}).get("lane") == LANE_SYSTEM or is_buyin(packet):
                continue                               # (a 百应 draft is no WeChat reply: its lines stay out of WeChat's)
            packet = self._store.packet(packet["id"]) or packet
            if packet["status"] != SUPERSEDED or packet.get("decided_at") != now:
                continue
            job = self._relay.store.get(int(packet["job_id"]))
            lines = self._job_lines(job) if job is not None else ""
            started = (any((packet.get("meta") or {}).get(k) for k in (OPENING, HUOPAN, SIGNED))
                       or bool(((packet.get("meta") or {}).get(HANDCARDS) or {}).get("console")))
            if lines or (job is not None and started):
                out.append((packet, job, lines))
        return out

    def _reach(self, party: dict, made: bool = False, deal: Optional[dict] = None) -> tuple:
        """Where a packet the bot starts itself goes (no line of hers it answers: a 专属佣金 step, a shipping notice, a
        开场白 asked for in the console, a 补发货盘, a 做手卡, a 签收后问一句): ``(chat id, the linked group's row or None)``,
        as ``_reaching`` decides it."""
        chat, group, _ = self._reaching(party, made=made, deal=deal)
        return chat, group

    def _reaching(self, party: dict, made: bool = False, deal: Optional[dict] = None) -> tuple:
        """The one place that decides where what the bot starts itself goes: ``(chat id, the linked group's row or None,
        why)``.  Her private chat, as before; for a creator with a linked group (a 老板拉的群, one linked by hand or by the
        scan) only when she ever wrote to us there or the backend knows it (she is our friend), else that group (the
        newest she is still in, ``BossGroups.reach_group``); ``(None, None, "")`` when neither (she left it): nothing is
        sent (``UNREACHABLE``).  ``made``: the message is being made now (a job about to be asked for, a shipping notice
        aimed the first time): with PRM_REACH_LAST_SPOKE on, a linked group she is still in where the latest line of her
        side on the deal was written (``_spoke``) comes first (why REACH_LAST_SPOKE); her private chat by that rule is
        why REACH_PRIVATE.  ``why`` is "" otherwise (the rule as before).  ``deal``: hers, when the caller has it."""
        if not self.boss_groups.party_has_group(party["id"]):
            return party["wxid"], None, REACH_PRIVATE if made and self._reach_last_spoke else ""
        if made and self._reach_last_spoke:
            row = self._spoke(party, deal)
            if row is not None:
                return row["chat"], row, REACH_LAST_SPOKE
        known = self._private_known(party)
        if known is None:                      # (it cannot be told now: her private chat, never a group for that)
            logger.debug("PRM: whether party %d has a private chat with us cannot be told now; her private chat it is",
                        party["id"])
        if known is not False:
            return party["wxid"], None, REACH_PRIVATE if made and self._reach_last_spoke else ""
        row = self.boss_groups.reach_group(party["id"])
        return (row["chat"], row, "") if row is not None else (None, None, "")

    def _spoke(self, party: dict, deal: Optional[dict] = None) -> Optional[dict]:
        """PRM_REACH_LAST_SPOKE: the linked group she is still in (``BossGroups.reach_groups``: one the bot drafts in,
        never a read-only 三方群) where the latest line of her side on her deal was written (``_last_spoken``), or None:
        her latest line was in her private chat, in a group she left or the bot does not draft in, or there is none."""
        where = self._last_spoken(party, deal)
        if not where.endswith("@chatroom"):
            return None
        return next((row for row in self._spoke_groups(party) if row["chat"] == where), None)

    def _spoke_groups(self, party: dict) -> list:
        """The groups PRM_REACH_LAST_SPOKE may send what the bot starts to (re-review 2026-11-11 N2, N3): the linked
        groups she is still in that the bot drafts in (``BossGroups.reach_groups``) with at most PRM_BOSS_GROUP_MAX
        members (a bigger one, linked by hand all the same: as with the switch off)."""
        most = self.boss_groups.max_members
        return [row for row in self.boss_groups.reach_groups(party["id"]) if len(row.get("members") or []) + 1 <= most]

    def _last_spoken(self, party: dict, deal: Optional[dict] = None) -> str:
        """The chat of the latest line of her side on her deal ("" none): ``last_by_chat`` (WeChat's clock) as it is now,
        her own lines, and with PRM_GROUP_HER_SIDE on her side's other people's too (SIDE_SPOKE); a tie goes to her
        private chat."""
        deal = (self._store.deal(int(deal["id"])) if deal and deal.get("id") else None) or self._store.deal_of(
            party["id"], self._clock())               # (as it is now: the caller's may be from before a line of hers)
        side = self.boss_groups.her_side_on
        best, where = 0.0, ""
        for key, at in (deal.get("last_by_chat") or {}).items():
            chat = str(key)
            if chat.startswith(SIDE_SPOKE):
                if not side:
                    continue
                chat = chat[len(SIDE_SPOKE):]
            try:
                at = float(at or 0)
            except (TypeError, ValueError):
                continue
            if at > best or (at == best and where.endswith("@chatroom") and not chat.endswith("@chatroom")):
                best, where = at, chat
        return where

    def _reach_out(self, party: dict, deal: dict) -> Optional[dict]:
        """The deal page's ``reach`` (PRM_REACH_LAST_SPOKE on, a creator with a linked group; ``snapshot``'s
        ``reach_of``): where what the bot starts itself for her would go now (``_reaching`` with ``made``) and why:
        ``{"where": "group", "name", "why": "last_spoke" | "no_private"}``, ``{"where": "private", "why": "last_private"
        (her latest line was there) | "never" (she wrote nowhere yet) | "elsewhere" (her latest line was in a group she
        left or the bot does not draft in)}``, ``{"where": "none"}`` (nowhere: ``UNREACHABLE``).  None: no linked group."""
        if not self.boss_groups.party_has_group(party["id"]):
            return None
        chat, group, why = self._reaching(party, made=True, deal=deal)
        if chat is None:
            return {"where": "none"}
        if group is not None:
            return {"where": "group", "name": str(group.get("name") or "")[:60],
                    "why": "last_spoke" if why == REACH_LAST_SPOKE else "no_private"}
        if self._private_known(party) is None:
            return {"where": "private", "why": "unknown"}
        latest = self._last_spoken(party, deal)
        return {"where": "private", "why": ("never" if not latest else "elsewhere" if latest.endswith("@chatroom")
                                            else "last_private")}

    def _private_known(self, party: dict) -> Optional[bool]:
        """Has she a private chat with us: a draft job of her lines there, or the backend lists it (``knows_private``)?
        None: the backend could not say now (re-review 2026-11-11 N1: never read as "no", ``_reaching``)."""
        if any(job.get("kind") == PRM and str(job.get("text") or "").strip() and not job_meta(job).get("op")
               for job in self._relay.store.recent(party["wxid"], limit=50)):
            return True
        return self.boss_groups.knows_private(party["wxid"])

    def _bot_started(self, packet: dict) -> bool:
        """A packet the bot started itself (``_reach``): a shipping notice (no job), a 专属佣金 step, a 开场白, a 补发货盘,
        a 签收后问一句, a 做手卡."""
        meta = packet.get("meta") or {}
        return (packet.get("job_id") is None or meta.get("lane") == LANE_SYSTEM or bool(meta.get(OPENING))
                or bool(meta.get(HUOPAN)) or bool(meta.get(SIGNED)) or bool((meta.get(HANDCARDS) or {}).get("console")))

    def _aim(self, packet: Optional[dict], *, wait: bool = False, sending: bool = False) -> Optional[dict]:
        """A packet the bot started itself still aimed at her private chat, for a creator with a linked group: where she
        can be reached (``_reaching``): the group (its chat, the console's tag), or nowhere: it fails, nothing is sent, the
        deal says so (``UNREACHABLE``).  Returns the packet as it is now (None: failed so).  ``wait`` (微信里审批草稿, before
        a draft is told: it says where it goes): reached nowhere, nothing is changed and None comes back (told later).
        PRM_REACH_LAST_SPOKE on: a packet with no job (a shipping notice) is decided once, the first time it is aimed
        (``made``: where her side last spoke; ``meta.reach`` says how it went, REACH_PRIVATE when it stays); a job's packet
        went where its job was asked for (``_reaching`` with ``made`` then): only the rule as before moves it.
        ``sending`` (``send_approved``, re-review 2026-11-11 N1, N3): it is approved and about to go: where it would go
        now is not where it was when it was approved (her private chat now a group; a group PRM_REACH_LAST_SPOKE picked
        that is not one it may pick any more: she left it, it is no longer read, it grew past PRM_BOSS_GROUP_MAX): it
        is not sent, it goes back to 待审批 aimed at the new place, saying so (``_retarget``); None comes back."""
        if packet is None or not self._bot_started(packet) or is_buyin(packet):
            return packet
        party = self._store.party(packet["party_id"])
        if party is None:
            return packet
        if packet["chat_id"] != party["wxid"]:
            if (sending and (packet.get("meta") or {}).get("reach") == REACH_LAST_SPOKE
                    and packet["chat_id"] not in {row["chat"] for row in self._spoke_groups(party)}):
                chat, group, why = self._reaching(party)
                if chat is None:
                    return self._unreachable(packet, party)
                return self._retarget(packet, chat, group, why)
            return packet
        fresh = self._reach_last_spoke and packet.get("job_id") is None and not (packet.get("meta") or {}).get("reach")
        chat, group, why = self._reaching(party, made=fresh, deal=self._store.deal(int(packet["deal_id"] or 0)) if fresh
                                          else None)
        if chat == party["wxid"]:
            if fresh and self._store.aim_packet(packet["id"], chat, {"reach": REACH_PRIVATE}):
                return self._store.packet(packet["id"])        # (decided: it stays in her private chat)
            return packet
        now = self._clock()
        if chat is None and wait:
            return None
        if chat is None:
            return self._unreachable(packet, party)
        if sending:
            return self._retarget(packet, chat, group, why)
        moved = str((packet.get("meta") or {}).get("hold") or "").startswith("发送对象变了")   # (sent back before)
        self._store.aim_packet(packet["id"], chat, {"channel": GROUP_CHANNEL, "group_name": str(group["name"])[:60],
                                                    **({"group_manual": True} if group.get("manual") else {}),
                                                    **({"reach": why} if why else {}),
                                                    **({"hold": f"发送对象变了：现在会发到群「{group['name'] or '（没有群名）'}」，"
                                                                "请再看一眼"} if moved else {})})
        logger.info("PRM: packet %d goes to boss group %s… (party %d %s)", packet["id"], chat[:8], party["id"],
                    "last wrote there: PRM_REACH_LAST_SPOKE" if why == REACH_LAST_SPOKE else "has no private chat with us")
        return self._store.packet(packet["id"])

    def _unreachable(self, packet: dict, party: dict) -> None:
        """``_aim``: she can be reached nowhere: the packet fails, nothing is sent, the deal says so (``UNREACHABLE``)."""
        now = self._clock()
        self._store.move(packet["id"], (PENDING, APPROVED, SENDING), FAILED, outcome="failed", sent_at=now,
                         error=f"{UNREACHABLE_TEXT}，没有发")
        self._store.set_notice(packet["deal_id"], UNREACHABLE, f"{UNREACHABLE_TEXT}：#{packet['id']} 没有发，"
                                                               "请在微信里找到她手动发", now)
        logger.warning("PRM: packet %d: party %d is in no chat with us (no private chat, gone from the boss group); "
                       "not sent", packet["id"], party["id"])
        return None

    def _retarget(self, packet: dict, chat: str, group: Optional[dict], why: str = "") -> None:
        """Re-review 2026-11-11 N1: an approved packet whose target changed since a person (or its gate) approved it is
        not sent: it goes back to 待审批 (route review), aimed at where it would go now (``chat``; ``group``: that group's
        row, or None for her private chat), its card saying so (``meta.hold``, 「发送对象变了：现在会发到…，请再看一眼」);
        a second approval sends it there.  Returns None (nothing to send now).
        Merge 2026-11-11: ``meta.retarget`` counts the times (微信里审批草稿 tells such a draft again, 「第 N 次通知」 with
        the new target: ``WechatReview.retargeted``, ``attention_seen``; an answer to the older notice does nothing); the
        holds it was made with stay (``meta.opening`` / ``huopan`` / ``handcards``, no ``auto_at``: nothing sends a waiting
        draft by itself).  A platform draft (its action run and verified, its reply approved with it) never goes back to
        be approved again (that would offer the action once more): it fails as one whose group closed does, its reply
        for a person to send by hand (``_fail_closed``: 「平台动作已经核验成功，但回复没发出去：…」).
        The words a person approved stay the draft's (final check of merge-1111, R1): an edit (「改了再发」, the admin's
        「改 N …」) becomes its ``text`` while it waits again, so the card, the fresh notice and a 「发送」 all have it; the
        Mac's own words are kept in ``meta.drafted`` and put back with the next decision (``decide``: ``edited`` and the
        gates' count still compare what was sent with what the Mac wrote).  The night window's and 自动模式's words of
        that approval (``night_held``, ``auto_mode``) go: a person decides now."""
        name = str((group or {}).get("name") or "")
        where = f"群「{name or '（没有群名）'}」" if group is not None else "她的私聊"
        hold = f"发送对象变了：现在会发到{where}，请再看一眼"
        if packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL:
            if self._fail_closed(packet, hold.replace("，请再看一眼", ""), self._clock()):
                logger.warning("PRM: packet %d (a platform draft): where its reply goes changed since it was approved; "
                               "not sent, for a person to send by hand", packet["id"])
            return None
        meta = {k: v for k, v in (packet.get("meta") or {}).items()
                if k not in ("channel", "group_name", "group_manual", "reach", "group_told", "decided_chat", "auto_at",
                             "night_held", AUTO_MODE)}
        if group is not None:
            meta.update(channel=GROUP_CHANNEL, group_name=name[:60], **({"group_manual": True} if group.get("manual")
                                                                          else {}), **reach_meta(why))
        meta["hold"] = hold
        approved = packet.get("final_text")
        if approved is not None and str(approved) != str(packet["text"]):
            meta.setdefault("drafted", packet["text"])       # (the Mac's words: back with the next decision)
            # (before it waits again: nobody decides an approved one meanwhile, and a send in between sends these too)
            self._store.reword_packet(packet["id"], (APPROVED, SENDING), str(approved), clear_final=True)
        meta["retarget"] = int(meta.get("retarget") or 0) + 1 if isinstance(meta.get("retarget"), int) else 1
        if "auto_at" in (packet.get("meta") or {}):
            meta["auto_held"] = meta["hold"]   # (开场白自动发: its time gone, the card says why it did not go by itself)
        if self._store.aim_packet(packet["id"], chat, {}) and self._store.move(
                packet["id"], (APPROVED, SENDING), PENDING, decided_at=None, route=gates.REVIEW, meta=meta):
            logger.warning("PRM: packet %d: where it goes changed since it was approved (now %s); back to 待审批",
                           packet["id"], "a group" if group is not None else "her private chat")
            try:
                self.wechat_review.retargeted(packet["id"])   # (told in WeChat: told again, the new target in it)
            except Exception:
                logger.exception("PRM: packet %d: its notice in WeChat could not be opened again", packet["id"])
        return None

    def _moved_since_decided(self, packet: dict) -> bool:
        """``send_approved`` (re-review 2026-11-11 N1): a packet a person approved for one chat (``meta.decided_chat``,
        ``decide``) that is aimed at another now goes back to 待审批 (``_retarget``): True then."""
        decided = str((packet.get("meta") or {}).get("decided_chat") or "")
        if not decided or decided == str(packet.get("chat_id") or ""):
            return False
        chat = str(packet["chat_id"])
        row = self._store.boss_group(chat) if chat.endswith("@chatroom") else None
        self._retarget(packet, chat, row, str((packet.get("meta") or {}).get("reach") or ""))
        return True

    def _lane_chat(self, party_id: int, chat_id: str) -> Optional[str]:
        """Whose waiting reply drafts a new one puts out of date: for a creator with a 老板拉的群 (PRM_BOSS_GROUPS) only
        those of the chat it is in (she writes in the group and in private: each reply goes where she wrote, and her
        private lines are never answered in front of the boss); else None: all of the deal's, as before."""
        return chat_id if self.boss_groups.party_has_group(party_id) else None

    def _job_in(self, job_id, chat: Optional[str], party: dict) -> bool:
        """Was this draft job one of ``chat`` (None: any chat)?  A job no longer kept counts as her private chat's."""
        if chat is None:
            return True
        job = self._relay.store.get(int(job_id or 0)) if job_id else None
        return str((job or {}).get("chat_id") or party["wxid"]) == chat

    def _missed(self, party: dict, deal: dict, line: str, at: float, now: float, chat_id: str = "",
                side: bool = False) -> None:
        """A creator's message older than PRM_CATCHUP_HOURS when it was read: too old to draft.
        It is recorded (the deal's history; the next draft job hears of it, ``take_backlog``),
        waiting drafts are out of date, and the deal asks a person to look (how many such
        messages wait, the earliest one's time).  ``side``: a line of her side's other people (``_wrote``)."""
        text = self._mask(party["id"], f"[{_clock_text(at)}] {line}", now)
        self._store.add_event(deal["id"], party["id"], "creator_message", {"text": text, "missed": True, "at": at},
                              actor="creator", forwarded=False, now=now)
        self._store.supersede(deal["id"], now, lane=LANE_REPLY)
        waiting = [ev for ev in self._store.backlog(deal["id"])
                   if ev["kind"] == "creator_message" and ev["detail"].get("missed")]
        earliest = min([_event_time(ev) for ev in waiting] or [at])
        self._store.update_deal(deal["id"], now, **_wrote(deal, chat_id or party["wxid"], at, side=side))
        # until the draft job that hears of them comes back (on_result)
        self._store.set_notice(deal["id"], "missed", missed_note(len(waiting) or 1, earliest), now)
        logger.warning("PRM: party %d's message was read %.1f h after it was written (more than "
                       "PRM_CATCHUP_HOURS); recorded, not drafted, the deal asks a person to look",
                       party["id"], (now - at) / 3600)

    def _outbound(self, deal_id: int, party_id: int, now: float, unsent: list = ()) -> list[dict]:
        """What the creator really got from us lately, oldest first, for the next draft: the Mac's
        case file holds the drafts as Claude wrote them, but an operator may have edited one
        before it went out, dropped one, or the send failed; holding replies are the bot's own.
        Told with every draft job (not once: a superseded job must not take it along); numbers
        masked like the creator's words.  ``unsent``: drafts her new message just put out of date
        (``_submit``): told to the job that answers their lines as not sent (``what: failed``, the
        worker's 「这一条没发出去，达人没看到」; ``dropped`` would say an operator chose not to send it).
        A creator with a 老板拉的群 (PRM_BOSS_GROUPS): each item says where it went (``where``: group | private), and the
        bosses' names are PRM_BOSS_CALL in it (``hide``: they never go to the Mac); without one there is no ``where``
        (the prompt as before).  A group an operator linked by hand (接手任何合作群) also says ``manual`` (the Mac does not
        say 老板拉的群 of it)."""
        where = self.boss_groups.party_has_group(party_id)
        manual: dict[str, bool] = {}

        def place(p: dict) -> dict:
            if not where:
                return {}
            chat = str(p.get("chat_id") or "")
            if not chat.endswith("@chatroom"):
                return {"where": "private"}
            if chat not in manual:
                manual[chat] = self.boss_groups.by_hand(chat)
            return {"where": "group", **({"manual": True} if manual[chat] else {})}   # (接手任何合作群)

        items = []
        for p in unsent:
            items.append({"at": float(p.get("created_at") or 0), "kind": p["kind"], "what": "failed",
                          "text": p["final_text"] if p.get("final_text") is not None else p["text"], **place(p)})
        for p in self._store.outcomes(deal_id, limit=OUTBOUND_TOLD):
            if _covered(p) is not None:
                continue                               # not run (the invite card opened it): nothing she was told
            final = p["final_text"] if p.get("final_text") is not None else p["text"]
            at = float(p.get("sent_at") or p.get("decided_at") or 0)
            item = {"at": at, "kind": p["kind"], **place(p)}
            if p["status"] == SENT:
                item.update(what="sent", text=final)
                files = [str(a.get("name") or "") for a in p.get("attachments") or [] if a.get("name")]
                if files:
                    item["files"] = files[:10]             # (同一张图不重复发: the Mac sees which pictures she got)
                if p.get("edited") and str(p["text"]).strip() != str(final).strip():
                    item["draft"] = p["text"]
                if p.get("wrong"):
                    item["wrong"] = True
            elif p["status"] == DROPPED:
                item.update(what="dropped", draft=p["text"])
            else:
                item.update(what="failed", text=final)
            items.append(item)
        for ev in self._store.events(deal_id, limit=50):
            if ev["kind"] == "holding_reply":                # (never in a group)
                items.append({"at": float(ev["at"]), "kind": "holding", "what": "sent",
                              "text": str(ev["detail"].get("text") or self._holding_text),
                              **({"where": "private"} if where else {})})
        items.sort(key=lambda i: i["at"])
        out = []
        for item in items[-OUTBOUND_TOLD:]:
            for key in ("text", "draft"):
                if key in item:
                    said = self.boss_groups.hide(item[key]) if where else str(item[key])
                    item[key] = self._mask(party_id, said, now)[:OUTBOUND_CHARS]
            item["at"] = _clock_text(item["at"]) if item["at"] else ""
            out.append(item)
        return out

    def _repeated_files(self, deal_id: int, attachments: list, now: float) -> list:
        """同一张图不重复发 (2026-11-02): the files of a new draft she got from us within REPEAT_FILE_SEC already (the same
        bytes, ``sha256``, in a draft of her deal that was sent, in any of her chats), each with when it went:
        ``[(attachment, sent_at)]``; [] for none."""
        if not attachments:
            return []
        got: dict = {}
        for p in self._store.outcomes(deal_id, limit=50):
            sent_at = float(p.get("sent_at") or 0)
            if p["status"] != SENT or now - sent_at > REPEAT_FILE_SEC:
                continue
            for a in p.get("attachments") or []:
                if a.get("sha256"):
                    got[a["sha256"]] = min(got.get(a["sha256"], sent_at), sent_at)
        return [(a, got[a["sha256"]]) for a in attachments if a.get("sha256") in got]

    def on_own_message(self, msg: dict) -> None:
        """A line this WeChat account sent in a creator's chat (the backend's hook).

        The bot's own line confirms its packet (``sent_confirmed``); anything else was typed
        by an operator on the phone or the PC: it is recorded for the next draft, and the
        deal goes to ``human`` for ``PRM_TAKEOVER_MIN`` (a new one extends it).  A line read
        more than MAX_AGE_SEC late (typed while the bot was not reading WeChat) is recorded
        the same way, but takes the chat over only until ``PRM_TAKEOVER_MIN`` after it was
        written, so not at all once that has passed.  PRM_CATCHUP_HOURS=0: such a line is
        history, as before (one Windows' edge queued, ``_queued``, counts as just written).  In a 老板拉的群 the bot took
        over (the backend hands its own lines too) the same rules hold: the read-back confirms the group packets, an
        operator's line there by hand takes her deal over.  Each of our lines there is the group's last line handled
        too (``note_line``: after a restart the watch starts after it, so it is not handed on again), and one the watch
        hands on again all the same is matched against the group's packets sent before (``_sent_part``).
        """
        chat_id = str(msg.get("chat_id", ""))
        group = bool(msg.get("is_group")) and self.boss_groups.is_linked(chat_id)
        if msg.get("is_group") and not group:
            deal = self.three_party.deal_of_chat(chat_id)
            if deal is not None:
                self._own_three_party_line(msg, deal)
            return
        if not group and (chat_id not in self._wxids or is_friend_notice(msg)):
            return                                         # (a friend notice is WeChat's, not an operator's)
        party = self._group_party(chat_id) if group else self._store.party_by_wxid(chat_id)
        if party is None:
            return
        now = self._clock()
        sent_at = float(msg.get("timestamp", 0) or 0)
        if group:
            self.boss_groups.note_line(chat_id, sent_at or now)   # (review 2026-10-31: read back, never handed on again)
        late = bool(sent_at) and now - self._local(sent_at) > MAX_AGE_SEC
        if late and not self._catchup_sec:
            if not _queued(msg):
                return                                     # history (a chat read for the first time)
            late = False                                   # Windows' queue never replays history
        line = system_line(msg)
        if line is not None:                               # 「你撤回了一条消息」…: WeChat's, no takeover
            self._system_line(msg, party, line, own=True)
            return
        with self._handle_lock:
            self._own_line(msg, party, chat_id, now, sent_at, late)

    def _own_line(self, msg: dict, party: dict, chat_id: str, now: float, sent_at: float, late: bool) -> None:
        """``on_own_message`` for a creator's chat (under ``_handle_lock``)."""
        matched, packet_id = self._match_own(chat_id, msg, now)
        if msg.get("edge_seen") and not matched:
            return                                         # 我们发的读回来了，只是这次运行没记着它
        if matched:
            if packet_id is not None:
                self._confirm(packet_id, msg, self._local(sent_at) or now, now)
            return
        deal = self._store.deal_of(party["id"], now)
        if chat_id != party["wxid"] or self.boss_groups.party_has_group(party["id"]):
            # 老板拉的群: the bosses' names never go to the Mac (a 聊天记录 we forward included)
            said = self.boss_groups.hide(message_text(self.boss_groups.record_msg(msg, party)))
        else:
            said = message_text(msg)
        text = self._mask(party["id"], f"[{_clock_text(sent_at or now)}] {said}", now)
        # ``at``: when it was written (read late, it is still an answer to what she wrote before it)
        self._store.add_event(deal["id"], party["id"], "operator_message", {"text": text, "at": sent_at or now},
                              actor="operator", forwarded=False, now=now)
        self._store.supersede(deal["id"], now, lane=LANE_REPLY)
        self._answered_by_hand(deal["id"], sent_at or now, now)
        if chat_id != party["wxid"]:                       # 老板拉的群: answered there, every line kept so far is told
            self.boss_groups.replied(chat_id)
        mode = deals.effective_mode(deal, now)
        if mode == PAUSED:
            logger.info("PRM: an operator wrote to party %d from WeChat; the deal stays paused", party["id"])
            return                                         # paused stays paused (the line is recorded)
        # Typed while the bot was not reading: the chat is a person's as long as it would have
        # been, counted from when the line was written (a later takeover is not cut short).
        until = (self._local(sent_at) if late else now) + self._takeover_sec
        if until <= now:
            logger.info("PRM: an operator wrote to party %d from WeChat while the bot was not reading, %.0f min "
                        "ago; recorded, too long ago to take the chat over", party["id"],
                        (now - self._local(sent_at)) / 60)
            return
        if mode == HUMAN and until <= float(deal.get("mode_until") or 0):
            logger.info("PRM: an operator's earlier line in party %d's chat recorded; the takeover goes on",
                        party["id"])
            return
        self._store.update_deal(deal["id"], now, mode=HUMAN, mode_until=until)
        self._store.add_event(deal["id"], party["id"], "takeover", {"until": until}, actor="system", now=now)
        logger.info("PRM: an operator wrote to party %d from WeChat; the deal is a person's until %s",
                    party["id"], time.strftime("%H:%M", time.localtime(until)))

    def _confirm(self, packet_id: int, msg: dict, written: float, now: float) -> None:
        """A packet's line (or file) read back from WeChat.  Going out or sent: ``sent_confirmed``, often
        before its send returned (WeChat shows a line at once); a failure stays one.  A packet the bot was
        stopped in the middle of sending (failed, ``indeterminate``: ``start``) went out after all (win
        review F4 (d); before, its line was taken for an operator's and the chat was taken over): it is
        sent again (``sent_at`` when WeChat shows it) once its text, if any (a reply in parts: each part,
        ``_text_rows``), and each of its files (one row each, ``_file_rows``) are read back; till then it
        stays red, and once its text is seen it says the text did go out (``UNSURE_TEXT_SEEN``)."""
        if self._store.confirm_sent(packet_id):
            packet = self._store.packet(packet_id) or {}
            logger.info("PRM: packet %d seen in WeChat %.0f s after sending (sent_confirmed)", packet_id,
                        max(0.0, now - float(packet.get("sent_at") or now)))
            return
        packet = self._store.packet(packet_id)
        if packet is None or (packet["status"], packet["outcome"]) != (FAILED, "indeterminate"):
            return
        final = packet["final_text"] if packet["final_text"] is not None else packet["text"]
        with self._sent_lock:
            files_left = self._file_rows.get(packet_id, [None]) if packet["attachments"] else []
            parts_left = self._text_rows.get(packet_id)   # (a reply in parts: its text is seen once every part is)
        text_seen = (bool(self._own_text(msg)) and not parts_left) or packet["error"] == UNSURE_TEXT_SEEN
        if (text_seen or not str(final or "").strip()) and not files_left:
            if self._store.settle_unsure(packet_id, sent_at=written):
                logger.info("PRM: packet %d, cut off by a stop, did go out (seen in WeChat): sent", packet_id)
        elif text_seen and packet["error"] != UNSURE_TEXT_SEEN and self._store.settle_unsure(
                packet_id, error=UNSURE_TEXT_SEEN):
            logger.info("PRM: packet %d, cut off by a stop: its text went out, its files are not all seen yet",
                        packet_id)

    def _answered_by_hand(self, deal_id: int, written: float, now: float) -> None:
        """An operator wrote to her by hand (at ``written``): a draft of hers that was not written, and
        her recall, are seen to by a person.  The first is shown no more (its lines still go with her next
        draft job: the case file must hear of them); the recall's notice goes, and so does the one asking
        whether she needs a 开场白 (``OPENING_UNSURE``)."""
        def edit(notices):
            out = []
            for n in notices:
                if n["key"] == "recall" and written >= float(n.get("recalled_at") or n.get("at") or 0):
                    continue
                if n["key"] == OPENING_UNSURE and written >= float(n.get("at") or 0):
                    continue                           # a person greeted her: no 开场白 to think of
                if (str(n["key"]).startswith("failed:") and n.get("lane") == LANE_REPLY
                        and written >= float(n.get("asked_at") or 0)):
                    n = {**n, "show": False}
                out.append(n)
            return out

        self._store.edit_notices(deal_id, edit, now)

    def _note_sent(self, chat_id: str, text: str = "", packet_id: Optional[int] = None,
                   file: "bool | str" = False) -> tuple:
        """What the bot is about to send, noted before it goes (WeChat shows a line, and the
        backend reads it back, before the send returns): a line, or one file (``file``: its path or
        name, whose kind is what WeChat shows it as, ``file_kind``; True: a plain file).  Returns the
        note; ``_unnote_sent`` takes it back."""
        now = self._clock()
        with self._sent_lock:
            if file:
                entry = (file_kind(file) if isinstance(file, str) else "file", now, packet_id, next(self._file_seq))
                self._sent_files.setdefault(chat_id, deque(maxlen=200)).append(entry)
                return chat_id, None, entry
            item = (_norm(text), now, packet_id)
            self._sent_lines.setdefault(chat_id, deque(maxlen=50)).append(item)
            return chat_id, item, None

    def _unnote_sent(self, noted: tuple) -> None:
        """It did not go out: forget that line or that file (the files sent before it still count as
        the bot's; not when it was read back already)."""
        chat_id, item, entry = noted
        with self._sent_lock:
            if entry is not None:
                files = self._sent_files.get(chat_id)
                if files is not None and entry in files:
                    files.remove(entry)
                return
            lines = self._sent_lines.get(chat_id)
            if lines is not None and item in lines:     # (not if it was read back already)
                lines.remove(item)

    @staticmethod
    def _own_text(msg: dict) -> Optional[str]:
        """An own text line's normalized words (None for a picture or file)."""
        try:
            if int(msg.get("msg_type", 1) or 1) != 1:
                return None
        except (TypeError, ValueError):
            pass
        return _norm(_QUOTE_PREFIX.sub("", str(msg.get("content", "") or "")))

    def _match_own(self, chat_id: str, msg: dict, now: float) -> tuple[bool, Optional[int]]:
        """Was this own line sent by the bot? ``(True, packet id or None)`` (``_match_sent``).

        A row handled before and handed on again (the same ``_row_key``: the same message id, which
        the WCDB backend never changes for a row, src/wechat/wcdb_backend.py ``_message_ids``) is no
        new line: ``(True, None)``, nothing more is done with it, ours or an operator's (2026-10-27:
        WeChat 4.1 fills in a line's server id a second after writing it, and the ids that used it
        handed the bot's row on twice; an event the Mac gets twice).  Only the same row: the same
        words in another row are matched afresh, a person's when no send of ours is waiting for them
        (win review F4 (a): before, the same words within 120 s of a read-back were swallowed, and an
        operator answering 「好的」 as the bot just had took nothing over).  The Mac's ``edge_operator``
        lines were compared with the sends on Windows already: only a row handed on again is looked for."""
        key = _row_key(msg)
        with self._sent_lock:
            rows = self._read_back.setdefault(chat_id, {})
            for k, at in list(rows.items()):
                if now - at > READ_BACK_SEC:
                    del rows[k]
            if key is not None and key in rows:
                logger.info("PRM: a line in %s…'s chat handed on again %.0f s after it was handled (the same row); "
                            "nothing new", chat_id[:8], now - rows[key])
                return True, None
            if key is not None:
                rows[key] = now
                while len(rows) > READ_BACK_ROWS:
                    del rows[next(iter(rows))]
            if not rows:
                del self._read_back[chat_id]
        if msg.get("edge_operator"):
            return False, None             # Mac 端：Windows 已经用发送记录比过了，对不上才报 operator_message
        return self._match_sent(chat_id, msg, now)

    def _match_sent(self, chat_id: str, msg: dict, now: float) -> tuple[bool, Optional[int]]:
        """Was this own line sent by the bot (``_match_own``)?  What was sent before this run is
        looked for around the time the line was written (read late after a restart: the bot
        sent it just before it stopped)."""
        try:
            msg_type = int(msg.get("msg_type", 1) or 1)
        except (TypeError, ValueError):
            msg_type = 1
        written = min(now, self._local(float(msg.get("timestamp", 0) or 0)) or now)
        sample = None
        with self._sent_lock:
            if msg_type != 1:
                # Each file sent covers one row of its own kind, the oldest first (win review F4 (b):
                # before, any picture, voice message or sticker for OWN_FILE_SEC after a file was ours)
                files = self._sent_files.get(chat_id) or deque()
                for entry in list(files):
                    if now - entry[1] > OWN_FILE_SEC:
                        files.remove(entry)
                for entry in files:
                    if own_file_fits(entry[0], msg):
                        files.remove(entry)
                        return True, entry[2]
            else:
                text = _norm(_QUOTE_PREFIX.sub("", str(msg.get("content", "") or "")))
                lines = self._sent_lines.get(chat_id) or deque()
                for item in list(lines):
                    if now - item[1] > OWN_MATCH_SEC:
                        lines.remove(item)
                    elif item[0] == text:
                        lines.remove(item)
                        sample = (self._clock_sample(msg, item[1], now), item[2])
                        break
        if sample is not None:
            if sample[0] is not None:
                self._note_clock(sample[0], now)
            return True, sample[1]
        # Sent before this run (the bot restarted in between): the database, but only what
        # WeChat has not shown yet; this run's sends are all in memory, so an operator typing
        # the same words later is still seen as a person.  (A 老板拉的群's packets are its chat's.)
        party = self._chat_party(chat_id)
        if party is None:
            return False, None
        # A draft the bot was stopped in the middle of sending (``indeterminate``: settled by ``start``
        # when this run began, after its line was written) is looked for too (win review F4 (d)).
        unsure = self._store.unsure_since(chat_id, written - OWN_MATCH_SEC)
        if msg_type != 1:
            # A file of a draft sent just before a restart: WeChat shows it while the draft's
            # files are going out, before its send returns (``sent_at``).
            before = [p for p in self._store.sent_since(chat_id, written - OWN_FILE_EARLY_SEC)
                      if p["attachments"] and float(p["sent_at"]) < self._started
                      and float(p["sent_at"]) <= written + OWN_FILE_SEC]
            for packet in [*before, *(p for p in unsure if p["attachments"])]:
                if self._file_row_of(packet, msg):
                    return True, packet["id"]
            return False, None
        tokens = self._store.tokens(party["id"])
        # 老板拉的群: after a restart the watch may hand on a line of ours read back before it (review 2026-10-31): a
        # packet of the group counts, confirmed or not, once, around its send (``_sent_part``'s ``group``)
        group = chat_id != party["wxid"] and self.boss_groups.is_linked(chat_id)
        for packet in self._store.sent_since(chat_id, written - OWN_MATCH_SEC):
            if float(packet["sent_at"]) < self._started and self._sent_part(
                    packet, text, tokens, written, unconfirmed=packet["outcome"] == "sent_unconfirmed", group=group):
                return True, packet["id"]
        for packet in unsure:
            if self._sent_part(packet, text, tokens, written, unconfirmed=True, group=group):
                return True, packet["id"]
        deal = self._store.deal_of(party["id"], now)
        for ev in self._store.events(deal["id"], limit=20):
            if (ev["kind"] == "holding_reply" and written - OWN_MATCH_SEC <= ev["at"] < self._started
                    and _norm(ev["detail"].get("text")) == text):
                return True, None
        return False, None

    def _sent_part(self, packet: dict, text: str, tokens: dict, written: float, *, unconfirmed: bool,
                   group: bool = False) -> bool:
        """Is ``text`` (an own text line's normalized words, written at ``written``) a line ``packet`` went out as before
        this run (``_match_sent``)?  A reply in one message: its text, while it is not confirmed (``unconfirmed``).  A
        reply in parts (``reply_parts``): each part once (``_text_rows`` counts them down; the first one read back
        confirmed it), and only written before its send returned (give or take PART_LATE_SEC: the same words typed
        later are a person's); or its whole text once (it went out in one message, before PRM_REPLY_PARTS).  ``group``
        (a 老板拉的群's packet, review 2026-10-31: the watch hands a line of ours read back before a restart on again): a
        reply in one message counts too when it is confirmed, once, like a part; a confirmed one only when written
        within PART_LATE_SEC of its send.  One not confirmed (``unconfirmed``: sent but not read back, or cut off by a
        stop) has no lower bound, as in her private chat (second review 2026-10-31 F1: the ``sent_at`` of one cut off
        is the restart's, ``start``, and this PC's clock may be ahead of WeChat's before this run measured it)."""
        final = packet["final_text"] if packet["final_text"] is not None else packet["text"]
        if not str(final or "").strip():
            return False
        whole = pii.unmask(final, tokens)
        parts = reply_parts(whole, self._reply_parts)
        if len(parts) <= 1 and not group:
            return unconfirmed and _norm(whole) == text
        if written > float(packet["sent_at"] or 0) + PART_LATE_SEC:
            return False
        if group and not unconfirmed and written < float(packet["sent_at"] or 0) - PART_LATE_SEC:
            return False
        with self._sent_lock:
            left = self._text_rows.get(packet["id"])
            if left is None:
                left = self._text_rows[packet["id"]] = [_norm(p) for p in parts]
            if text in left:
                left.remove(text)
                return True
            if text == _norm(whole) and len(left) == len(parts):
                left.clear()
                return True
        return False

    def _file_row_of(self, packet: dict, msg: dict) -> bool:
        """Is this own row one of the files of ``packet``, sent before this run (``_match_sent``)?  Each
        of its files covers one row of its kind, never more (``_file_rows`` counts them down)."""
        with self._sent_lock:
            left = self._file_rows.get(packet["id"])
            if left is None:
                left = self._file_rows[packet["id"]] = [file_kind(a.get("name") or a.get("path") or "")
                                                        for a in packet["attachments"]]
            for n, kind in enumerate(left):
                if own_file_fits(kind, msg):
                    del left[n]
                    return True
        return False

    def on_send_failed(self, chat_id: str, text: str, why: str, *, digest: str = "",
                       sent_at: Optional[float] = None, file: bool = False) -> None:
        """Mac 端：Windows 报这条没发出去（失败、过期、不确定）。对应的草稿从「已发出」改成「没发出去」，
        运营在待审批页能看到原因；「稍等」这类不是草稿的，只记日志。

        ``digest``：Mac 重启以前发的，edge 后端只留着发出去的原文的 sha256（原文不写盘），``text`` 是空的：
        按摘要对草稿（换回号码以后的全文），从 ``sent_at`` 前后找起，不受 OWN_MATCH_SEC 限制。
        ``file``：没发出去的是素材文件，``sent_at`` 是请 Windows 发它的时间：找那之后最先发完、带文件的草稿
        （文字可能已经发出去、读回了，照样标红，原因里写明文字发了）。"""
        now = self._clock()
        if file:
            self._file_send_failed(chat_id, why, sent_at, now)
            return
        if digest:
            self._send_failed_by_digest(chat_id, digest, why, sent_at, now)
            return
        wanted = _norm(text)
        with self._sent_lock:
            lines = self._sent_lines.get(chat_id) or deque()
            for item in reversed(list(lines)):             # 最新的那条：更早发出的同一句话可能还在等读回
                if item[0] == wanted:
                    lines.remove(item)                     # 没发出去，读回时不能再算我们的
                    break
        party = self._store.party_by_wxid(chat_id)
        if party is None:
            return
        tokens = self._store.tokens(party["id"])
        for packet in self._store.sent_since(chat_id, now - OWN_MATCH_SEC):
            final = packet["final_text"] if packet["final_text"] is not None else packet["text"]
            if packet["outcome"] == "sent_unconfirmed" and _norm(pii.unmask(final, tokens)) == wanted:
                if self._store.move(packet["id"], (SENT,), FAILED, outcome="failed", error=why[:200]):
                    logger.warning("PRM: packet %d was not sent by Windows: %s", packet["id"], why)
                return
        logger.warning("PRM: a send to party %d failed (%s); no waiting draft matches it", party["id"], why)

    def _file_send_failed(self, chat_id: str, why: str, sent_at: Optional[float], now: float) -> None:
        party = self._store.party_by_wxid(chat_id)
        if party is None:
            return
        asked = sent_at or now - RESTART_FAIL_SEC
        # 一条草稿先发文字、再一个个发文件，都交给 Windows 以后才记 sent_at：文件请求的时间不晚于它
        packets = [p for p in self._store.sent_since(chat_id, asked - 1)
                   if p["attachments"] and p["status"] == SENT]
        if not packets:
            logger.warning("PRM: a file send to party %d failed (%s); no sent draft with files matches it",
                           party["id"], why)
            return
        packet = min(packets, key=lambda p: float(p["sent_at"] or 0))
        error = f"素材文件没发出去（{why}）"
        if any(str(packet.get(k) or "").strip() for k in ("final_text", "text")):
            error += "；文字已经发出去了，只要在微信里补发文件"
        if self._store.move(packet["id"], (SENT,), FAILED, outcome="failed", error=error[:200]):
            logger.warning("PRM: packet %d: a file was not sent by Windows: %s", packet["id"], why)

    def _send_failed_by_digest(self, chat_id: str, digest: str, why: str, sent_at: Optional[float],
                               now: float) -> None:
        party = self._store.party_by_wxid(chat_id)
        if party is None:
            return
        tokens = self._store.tokens(party["id"])
        since = (sent_at - OWN_MATCH_SEC) if sent_at else now - RESTART_FAIL_SEC
        for packet in self._store.sent_since(chat_id, since):
            final = packet["final_text"] if packet["final_text"] is not None else packet["text"]
            sent = pii.unmask(final, tokens)
            if packet["outcome"] == "sent_unconfirmed" and hashlib.sha256(sent.encode("utf-8")).hexdigest() == digest:
                if self._store.move(packet["id"], (SENT,), FAILED, outcome="failed", error=why[:200]):
                    logger.warning("PRM: packet %d was not sent by Windows (told after a restart): %s", packet["id"], why)
                return
        logger.warning("PRM: a send to party %d failed (%s, after a restart); no waiting draft matches it",
                       party["id"], why)

    # ── from the Mac (the relay's delivery thread) ────────────────────────

    def on_result(self, job: dict) -> bool:
        """A prm job ended: its draft becomes a packet (or the deal says what went wrong)."""
        meta = job_meta(job)
        if meta.get("op") == SAMPLE_OP:                 # 聚水潭's answer about the sample orders: no creator, no draft
            return self._on_sample_status(job, meta)
        if meta.get("op") == BUYIN_OP:                  # the new 百应 private messages: drafts come of them, not of it
            return self._on_buyin_inbox(job, meta)
        try:
            party_id, deal_id = int(meta["party_id"]), int(meta["deal_id"])
        except (KeyError, TypeError, ValueError):
            logger.warning("PRM: job #%d has no party/deal; dropped", job["id"])
            return True
        deal = self._store.deal(deal_id)
        if deal is None or self._store.party(party_id) is None:
            return True
        now = self._clock()
        status = job["status"]
        if meta.get("channel") == BUYIN_CHANNEL:        # a 百应 private-message draft: its own way, never WeChat's
            return self._on_buyin_draft(job, meta, deal, now)
        if meta.get("op") in PLATFORM_OPS:
            return self._on_platform_job(job, meta, now)
        if meta.get("op") == "purge":                       # the Mac's case file was cleaned (or not)
            if status in ("done", "failed", "lost"):
                self._store.add_event(deal_id, party_id, "purge_case",
                                      {"ok": status == "done", "result": str(job.get("result_text") or "")[:200]},
                                      actor="mac", now=now)
            return True
        # A job of a group whose link ended for her (``_group_closed``) that gives no draft: as one cancelled at the
        # link's end (``_group_job_closed``: her lines stay that group's, what it was told goes on; second check
        # 2026-11-09: a failed one's notice kept her lines for a draft of the group that never comes)
        closed = self._group_closed(str(job.get("chat_id") or ""), party_id) == GROUP_CLOSED_TEXT
        if closed and status in ("cancelled", "failed", "lost"):
            if status != "lost":
                self._drop_job_sheets(job["id"])       # (PRM_DRAFT_CONTEXT: her photo is on a sheet; 合并 draft-context)
            what = f"{step_word(meta)}这一步的草稿" if meta.get("system_note") else "的草稿"
            self._group_job_closed(job, meta, f"#{job['id']} {what}没写成（{GROUP_CLOSED_HEAD}）", now)
            return True
        if status == "cancelled":
            # (an operator cancelled it in the console): her lines it took along wait for the next one again
            self._drop_job_sheets(job["id"])           # (PRM_DRAFT_CONTEXT: a sheet it sent first: her photo is on it)
            self._store.edit_notices(deal_id, lambda notices: [
                {**n, "into": None, "text": failed_text(n.get("head") or n["text"], lines=bool(n.get("lines")))}
                if n.get("into") == job["id"] else n for n in notices], now)
            return True
        # Messages read late (PRM_CATCHUP_HOURS): said in the deal's attention, the draft is reviewed.
        notes = gap_notes(meta)
        if status in ("failed", "lost"):
            try:
                extra = json.loads(job.get("result_json") or "{}")
            except ValueError:
                extra = {}
            extra = extra if isinstance(extra, dict) else {}
            why = "Mac 失联了" if status == "lost" else str(extra.get("error") or job.get("result_text") or "没有返回内容")
            if status == "failed":
                self._drop_job_sheets(job["id"])       # (lost: the answer may still come, with them)
            self._draft_failed(job, meta, f"#{job['id']} 草稿没写成：{why[:120]}", notes, now)
            if meta.get("system_note"):
                self._chain_refresh(deal_id, now)      # (not written: the chain waits for a person)
            logger.warning("PRM: job #%d %s; the deal needs a person", job["id"], status)
            return True
        if self._store.packet_for_job(job["id"]) is not None:
            return True                                # already drafted (a repeated delivery)
        try:
            draft = parse_draft(job.get("result_text") or "")
        except DraftError as e:
            self._drop_job_sheets(job["id"])
            if closed:
                self._group_job_closed(job, meta, f"#{job['id']} 的草稿格式不对（{e.code}），{GROUP_CLOSED_HEAD}", now)
                return True
            self._draft_failed(job, meta, f"#{job['id']} 的草稿格式不对（{e.code}），没有生成待审批", notes, now)
            if meta.get("system_note"):
                self._chain_refresh(deal_id, now)
            logger.warning("PRM: job #%d answer unusable: %s", job["id"], e)
            return True
        draft = self._mask_draft(party_id, draft, now)
        if draft.platform_held == PLATFORM_UNASKED and closed and (meta.get("system_note") or not draft.reply_draft):
            # (its group closed meanwhile: as any draft back after its link ended, ``_closed_result``; her lines stay
            # that group's, a step is not owed again: 合并 channel-goods-convention 2026-11-09)
            what = f"{step_word(meta)}这一步的草稿" if meta.get("system_note") else "的草稿"
            self._drop_job_sheets(job["id"])           # (no draft keeps its sheets: 合并 draft-context)
            self._group_job_closed(job, meta, f"#{job['id']} {what}写好时{GROUP_CLOSED_HEAD}，没发", now)
            return True
        if draft.platform_held == PLATFORM_UNASKED and self._unasked(job, meta, deal, draft, notes, now):
            self._drop_job_sheets(job["id"])           # (no draft keeps its sheets: 合并 draft-context)
            return True
        if self._draft_context and draft.platform is not None:
            # PRM_DRAFT_CONTEXT: a sample order the Mac did not check by photo (an older worker, one not restarted) is held
            draft = dataclasses.replace(draft, platform=draft_context.ungated(draft.platform, draft.gated))
        # A 专属佣金 the invite card (邀约卡 with a 专属佣金) opened already, as the Mac's preview found it in the invite
        # ledger (``covered``): a step of the chain is dropped and counts as open (the chain goes on); a reply keeps its
        # words for a person, without the action.
        lane = LANE_SYSTEM if meta.get("system_note") else LANE_REPLY
        cover = (draft.platform or {}).get("covered") if draft.platform is not None else None
        covered_note = ""
        if cover:
            item = str(((draft.platform or {}).get("action") or {}).get("item_id") or "")
            covered_note = cover_text(cover, item)
            if lane == LANE_REPLY:
                draft = dataclasses.replace(draft, platform=None)
                cover = None
        # PRM_DRAFT_CONTEXT: the files the Mac sent back for the operators only (a sample order's photo sheet, on the
        # relay's operator-only channel; an outfile named like one) are kept on the packet (``meta.operator_files``),
        # never attachments for her; past MAX_OPERATOR_FILES they are deleted
        files, operator_files, extra_sheets = draft_context.take_operator_files(
            self._relay.store.operator_files(job["id"]), draft.operator_files, self._relay.store.outfiles(job["id"]))
        draft_context.delete_files(extra_sheets)
        attachments = [{"name": f["name"], "path": f["path"], "size": f["size"], "sha256": f["sha256"]} for f in files]
        # Every file the draft named (an older worker reports only the ones it sent back): fewer came,
        # the draft waits for a person whatever its kind's gate, and the deal and the draft say which.
        named = draft.attachments_named if draft.attachments_named is not None else draft.attachments_to_send
        # 直播手卡 (src/prm/handcards.py): all of the Mac's cards or none of them; a draft with its verdict always waits
        # for a person (below), and an operator's 「做手卡」 that came back without one is a failure too
        cards = draft.handcards
        if cards is not None and meta.get(handcards.OK_KEY) is not True and not isinstance(meta.get(HANDCARDS), dict):
            # a verdict on a job that offered no cards (an older Mac worker's, PRM_HANDCARDS off): as if none came,
            # the draft goes its usual way, like a newer worker's that leaves such a request out
            logger.info("PRM: job #%d came back with a 手卡 verdict it was not offered: ignored", job["id"])
            cards = None
        if cards is None and isinstance(meta.get(HANDCARDS), dict) and (draft.reply_draft or attachments):
            cards = handcards.check_made({"ok": False, "problems": ["草稿没要手卡（Mac 没说做哪几款）"]})
        if cards is not None:
            came = attachments
            attachments, cards = handcards.keep_cards(cards, attachments)
            self._drop_cards([a for a in came if a not in attachments])     # (none of them goes: none is kept either)
            if not cards["ok"]:
                named = [n for n in named if _file_name(n) not in set(cards["cards"])]
        # 同一张图不重复发: a file she got from us within REPEAT_FILE_SEC (the same bytes) is left out of the draft, which
        # says so; the names it named lose those files too (not 「只收到 N 个」).  Not the 直播手卡 of a good verdict: all
        # of them go, or none (a card made again the same way is the same bytes)
        repeated = (self._repeated_files(deal_id, [a for a in attachments if not (
            cards is not None and cards["ok"] and a["name"] in cards["cards"])], now) if lane == LANE_REPLY else [])
        repeat_note = ""
        if repeated:
            gone = [a for a, _ in repeated]
            attachments = [a for a in attachments if a not in gone]
            left = list(named)
            for a in gone:
                match = next((n for n in left if _file_name(n) == a["name"]), None)
                if match is not None:
                    left.remove(match)
            named = left
            repeat_note = (f"{'、'.join(a['name'] for a in gone)} 她 {_clock_text(min(at for _, at in repeated))} 起已经收到过，"
                           f"这次没再发（同一张图 {REPEAT_FILE_SEC // 86400} 天内不重复发）")
        # The Mac's action did not pass the bot's own check (a Mac with wider limits, say): the reply stays
        # a draft, without it, for a person (it was written as if the action had run).
        refused = (f"平台动作没过机器人这边的检查（{draft.platform_refused}），草稿里去掉了它，平台上什么都不会做；"
                   "回复是按执行成功写的，发之前请核对") if draft.platform_refused else ""
        if covered_note and not cover:
            refused = f"{covered_note}，草稿里去掉了平台动作，平台上什么都不会做；回复是按执行成功写的，发之前请核对"
        # A reply that still says {链接}, {计划名} …: only a platform action that ran fills those in, and this draft has
        # none (an old worker's, a refused one, or the model forgot it): a person sees it, it never goes by itself
        unfilled = unfilled_placeholders(draft.reply_draft) if draft.platform is None else []
        if unfilled and not refused:
            refused = unfilled_text(unfilled)
        short = ""
        if len(named) > len(attachments):
            lost = missing_files(named, [a["name"] for a in attachments], draft.attachments_missing)
            short = f"草稿要附 {len(named)} 个文件，只收到 {len(attachments)} 个" + (f"：{'、'.join(lost)}" if lost else "")
        mode = self._mode(deal, now)
        shipment = self._store.latest_shipment(deal_id)
        waybill = shipment["waybill"] if shipment else ""
        unknown = pii.unknown_tokens(draft.reply_draft, self._store.tokens(party_id))
        # Two lanes: the creator's replies and the bot's own next steps (meta.system_note); a newer
        # job of the other lane does not make a draft out of date, nor does a draft supersede the other's.
        newer = self._relay.store.latest_open(job["chat_id"], drafts_only=True, system=lane == LANE_SYSTEM)
        # A step the bot asked for whose draft has no platform action (the model left it out, or could
        # not fill it) and still says something: nothing will happen on the platform, whatever it says.
        stepless = (f"{step_word(meta)}这一步的草稿没带平台动作，平台上什么都不会做；回复要是说"
                    f"{'建' if step_word(meta) == '建专属链接' else '开'}好了，发之前请核对"
                    if lane == LANE_SYSTEM and draft.platform is None and not refused
                    and (draft.reply_draft or attachments) else "")
        # 专属链接 for another UID than hers (a step of the chain: than its round's order's, ``chain_uid``), or a product
        # whose 专属佣金 the bot never opened for that UID: a person looks
        step = meta.get("step") if isinstance(meta.get("step"), dict) else {}
        chain_uid = (str(step.get("uid") or "") if lane == LANE_SYSTEM and step.get("action") == LINK_ACTION else "")
        doubts = (self._link_doubts(deal_id, draft.platform.get("action") or {}, chain_uid=chain_uid)
                  if draft.platform is not None and (draft.platform.get("action") or {}).get("action") == LINK_ACTION
                  else "")
        # ... and one not made the shop's way (not 专属达人带货, not 9 折, not 100 a colour): a person looks (review
        # 2026-11-10 A-D4; ``convention_text``)
        convention = (convention_text(draft.platform.get("action") or {}, draft.platform)
                      if draft.platform is not None and (draft.platform.get("action") or {}).get("action") == LINK_ACTION
                      else "")
        doubts = "；".join(d for d in (convention, doubts) if d)
        # She took a message back after this job was made (the draft may answer what she recalled), or
        # nobody has seen to her recall yet: a person decides.
        recalled = self._recalled_since(deal, float(job.get("created_at") or 0)) if lane == LANE_REPLY else None
        recall = recall_note(recalled) if recalled is not None else ""
        # A job that failed (the Mac went silent) and came back after her lines went into a later job: out of date.
        handed_on = lane == LANE_REPLY and self._handed_on(deal, job["id"])
        route, decided_at = gates.REVIEW, None
        night_held = None                              # 下班时段: why a draft judged by the night window waits
        auto_on = self.auto_mode["on"]                 # 自动模式, read once: the judgement and the packet's tag agree
        signed = isinstance(meta.get(SIGNED), dict) and lane == LANE_REPLY      # a 签收后问一句's draft
        # 接手任何合作群: a draft for a group linked by hand waits for a person (PRM_LINKED_GROUPS_AUTO off; the lines that
        # waited at 接群 whatever it says)
        hand_held = self._hand_hold(str(job.get("chat_id") or ""), meta)
        # 她那边的人 (review 2026-11-11 #1): a line of her side's other people is anybody's not known to be ours: its draft
        # always waits for a person, whatever the gate, the 下班时段, 自动模式 or PRM_LINKED_GROUPS_AUTO say
        if self._side_line_in(job):
            hand_held = HER_SIDE_HELD
        if draft.platform is not None:
            # A platform write: always a person's decision (8.1's gates do not apply).
            state = (SUPERSEDED if mode != COPILOT or handed_on or (newer is not None and newer["id"] > job["id"])
                     else PENDING)
            if cover and state == PENDING:
                state, decided_at = DROPPED, now       # opened by the invite card: nothing for a person to approve
        elif not draft.reply_draft and not attachments:
            state = NO_REPLY
        elif mode != COPILOT:
            state = SUPERSEDED                         # a person has the chat
        elif handed_on:
            state = SUPERSEDED                         # its lines went into a later draft job
        elif newer is not None and newer["id"] > job["id"]:
            state = SUPERSEDED                         # the creator wrote again: a newer draft is coming
        elif self._opening_outdated(job, meta):
            state = SUPERSEDED                         # 老板拉的群: she spoke there after it, the reply answers her
        elif notes or recall or lane == LANE_SYSTEM or cards is not None:
            # Read late, or she recalled something meanwhile: a person decides, whatever the gate.  The
            # bot's own next step too: a 专属佣金 step speaks of a platform write, so it never goes out
            # by a gate (mac review F4).  A draft with 直播手卡 (made or not) too: a person looks at the cards first.
            state = PENDING
        elif hand_held:
            # 接手任何合作群 (decision 2026-11-10): everybody in the group reads it, a person decides, whatever its kind's
            # gate, the 下班时段, 自动模式 or 签收后问一句 say (``_hand_hold``); at night or in 自动模式 its card says why
            state = PENDING
            if self._in_window(now) or auto_on:
                night_held = hand_held
        else:
            route, state, decided_at = deals.route_for(
                self._store, draft.message_kind, deal=deal, now=now, text=draft.reply_draft,
                needs_attention=draft.needs_attention or bool(refused), unknown_tokens=unknown, reason=draft.reason,
                waybill=waybill, attachments=attachments, files_named=len(named))
            if signed and state == APPROVED:
                # 签收后问一句 never goes by its kind's gate: by day a person sends it (the user was told 「白天进待审批，
                # 你点了才发」, ``SIGNED_DAY_NOTE``), in the 下班时段 or 自动模式 the night's rules judge it (below)
                route, state, decided_at = gates.REVIEW, PENDING, None
            if state == PENDING:
                # 下班时段 (PRM_NIGHT_AUTO_HOURS) or 自动模式: it may still go out by itself; only here, never before this step
                night_held = self._night_judge(meta, draft, refused=refused, short=short, unknown=unknown,
                                               waybill=waybill, attachments=attachments, named=len(named), mode=mode,
                                               now=now, auto_on=auto_on)
                if night_held == "":
                    route, state, decided_at = gates.NIGHT, APPROVED, now
        # 签收后问一句 waiting by day (outside the 下班时段, 自动模式 off): the draft and the deal say why
        day_held = SIGNED_DAY_NOTE if signed and state == PENDING and not self._in_window(now) and not auto_on else ""
        # 开场白没人处理就自动发 (PRM_OPENING_AUTO_MIN): our first line to a new friend waits, and may go out by itself later
        opening_auto = (self._opening_auto(meta, draft, refused=refused, short=short, unknown=unknown, waybill=waybill,
                                           attachments=attachments, named=len(named), mode=mode, recall=recall,
                                           lane=lane, now=now, auto_on=auto_on)
                        if state == PENDING and route == gates.REVIEW and cards is None and not hand_held else None)
        packet_meta = {"waybill": waybill} if draft.message_kind == "shipping_notice" and waybill else {}
        if opening_auto is not None:
            due, held = opening_auto
            if held:
                packet_meta["auto_held"] = held[:200]
            else:
                packet_meta["auto_at"] = due
        if lane == LANE_SYSTEM:
            packet_meta["lane"] = LANE_SYSTEM
        if chain_uid:
            packet_meta["chain_uid"] = chain_uid       # (its link binds this UID or none: ``_approve_execute``)
        if isinstance(meta.get(OPENING), dict):
            packet_meta[OPENING] = True                # (the console's tag; no holding reply for it)
        if isinstance(meta.get(HUOPAN), dict):
            packet_meta[HUOPAN] = True                 # (the console's tag)
        if cards is not None:                          # 直播手卡: the console's tag, the operator's note on the cards
            packet_meta[HANDCARDS] = {**handcards.packet_meta(cards),
                                      "console": isinstance(meta.get(HANDCARDS), dict)}
        if isinstance(meta.get(SIGNED), dict):
            packet_meta[SIGNED] = str(meta[SIGNED].get("order") or "")[:40] or True   # (the order it asks about)
        if meta.get("channel") == GROUP_CHANNEL:       # 老板拉的群: it goes to the group (the console's tag)
            told = meta.get("group") if isinstance(meta.get("group"), dict) else {}
            kept = self._store.boss_group(str(job["chat_id"])) or {}
            # (the group's name as WeChat gives it, for the console: the job's has the bosses' names hidden; and the
            # numbers of the group's lines the job was told: sent, they go, ``_deliver``)
            packet_meta.update(channel=GROUP_CHANNEL, group_name=str(kept.get("name") or told.get("name") or "")[:60],
                               group_told=told_lines(told), **({"group_manual": True} if kept.get("manual") else {}),
                               **reach_meta(str(meta.get("reach") or "")))   # (PRM_REACH_LAST_SPOKE: why it is there)
            chat = str(job["chat_id"])
            if (kept.get("manual") and lane == LANE_REPLY and self._auto_redrafts.get(chat, 0) >= REDRAFT_MAX
                    and self._lines_since.get(chat, 0) > 0):
                # (the last redraft allowed was written while others went on: what it did not hear, on its card)
                packet_meta["unheard"] = self._lines_since[chat]
        by_auto = night_held is not None and self._auto_by_day(now, auto_on)
        if by_auto:
            packet_meta[AUTO_MODE] = True              # 自动模式 by day sent or held it: the console and the record say so
        if night_held:
            packet_meta["night_held"] = night_held[:200]
        if hand_held in SHOWN_HOLDS and state == PENDING:
            packet_meta["hold"] = hand_held[:200]       # (review 2026-11-11 #1, #2: why it waits, by day too)
        if operator_files and state == PENDING and not closed:
            packet_meta[draft_context.OPERATOR_FILES] = operator_files      # (the console shows them, never sent to her)
        elif operator_files:
            # (nobody will look at this draft: not waiting, or its group's link ended meanwhile and it fails at once,
            # ``_closed_result``: 合并 draft-context)
            draft_context.delete_files([f["path"] for f in operator_files])
        if draft.gated:
            packet_meta[draft_context.KEY] = draft_context.mark(True)     # (the Mac checked its sample order by photo)
            # (the codes it held for photos: the console's 「对过图了」; those it let go only by one: 「撤销」 holds it)
            for key, codes in ((draft_context.PHOTO_HELD, draft.photo_held),
                               (draft_context.PHOTO_CONFIRMED, draft.photo_confirmed)):
                if codes and draft.platform is not None:
                    packet_meta[key] = list(codes)
            if draft.photo_held and draft.photo_hats is not None and draft.platform is not None:
                # where an operator sees each held hat, as the Mac's data (微信里「对过图」 numbers them by it)
                packet_meta[draft_context.PHOTO_HATS] = list(draft.photo_hats)
        tool, platform = MESSAGE_TOOL, None
        if draft.platform is not None:
            action = draft.platform.get("action")
            tool = (action or {}).get("action") or "platform.unchecked"
            platform = {"action": action, "preview": draft.platform}
        packet_id = self._store.add_packet(
            deal_id=deal_id, party_id=party_id, job_id=job["id"], chat_id=job["chat_id"],
            kind=draft.message_kind, text=draft.reply_draft, attachments=attachments, intent=draft.intent,
            slots=draft.slots, stage_proposal=draft.stage_proposal,
            needs_attention=(draft.needs_attention or bool(short) or bool(refused) or bool(stepless) or bool(recall)
                             or bool(doubts) or bool(cards is not None and handcards.flagged(cards))) and state != DROPPED,
            reason="；".join(r for r in (recall, covered_note if state == DROPPED else "", refused, stepless, doubts,
                                         handcards.attention(cards) if cards is not None and handcards.flagged(cards)
                                         else "", short, repeat_note, day_held, draft.reason) if r)[:500],
            status=state, route=route, decided_at=decided_at, meta=packet_meta,
            tool=tool, platform=platform, now=now,
        )
        # A draft for a group whose link ended meanwhile (「不接群」…, or it went to another creator) never goes out
        # (review 2026-11-09): it fails at once (or did already: the link's end, ``_close_group``) and answers nothing;
        # PRM_BOSS_GROUPS off: ``send_approved`` says so
        if self._group_closed(job["chat_id"], party_id) == GROUP_CLOSED_TEXT and state in (PENDING, APPROVED):
            self._fail_closed(self._store.packet(packet_id), GROUP_CLOSED_TEXT, now)
            return self._closed_result(job, meta, packet_id)
        if (self._group_closed(job["chat_id"], party_id) == GROUP_CLOSED_TEXT and lane == LANE_REPLY
                and state in (SUPERSEDED, NO_REPLY)):
            # (not to be sent anyway: it answers nothing all the same, final check 2026-11-09; no reply needed: her
            # lines are none to count)
            return self._closed_result(job, meta, packet_id, unanswered=state == SUPERSEDED)
        if state == DROPPED:
            self._store.set_outcome(packet_id, "covered")
            self._store.add_event(deal_id, party_id, "commission_covered", {
                "packet": packet_id, "item": str(((platform or {}).get("action") or {}).get("item_id") or ""),
                "rate": cover.get("rate"), "at": cover.get("at"), "aweme_id": cover.get("aweme_id")},
                actor="mac", now=now)
        if state in (PENDING, APPROVED):
            self._store.supersede(deal_id, now, keep=packet_id, lane=lane,
                                  chat_id=self._lane_chat(party_id, job["chat_id"]) if lane == LANE_REPLY else None)
        attention = [*notes, *([day_held] if day_held else []),
                     *([draft.reason] if draft.needs_attention and draft.reason else [])]
        if state == NO_REPLY and isinstance(meta.get(HUOPAN), dict):
            # 补发货盘 the Mac wrote nothing for (she had the 货盘 within 7 days, say): the deal says why, not 「要人看过再发」
            attention = [*(n for n in notes if n != HUOPAN_NOTE), f"「补发货盘」这次没写：{(draft.reason or 'Mac 没说为什么')[:200]}"]
        if state == NO_REPLY and isinstance(meta.get(HANDCARDS), dict):
            # 「做手卡」 the Mac made nothing of (it could not tell which products, say): the deal says why
            why = "；".join((draft.handcards or {}).get("problems") or []) or draft.reason or "Mac 没说为什么"
            attention = [*(n for n in notes if n != HANDCARDS_NOTE), f"「做手卡」这次没做：{why[:200]}"]
        elif cards is not None:
            # 直播手卡: the deal says the cards wait for a person (or why none came), once
            attention = [*(n for n in attention if n != HANDCARDS_NOTE), handcards.attention(cards)]
        # (her recall: its own notice on the deal says so, the draft's reason too)
        # The draft job of her lines that had no draft before (or this job itself, back late): seen to; and a 开场白, a
        # 补发货盘 or a 签收后问一句 that was not written, by any later reply draft.
        answered = self._store.drop_notices(deal_id, lambda n: n.get("into") == job["id"]
                                            or n["key"] == f"failed:{job['id']}"
                                            or (lane == LANE_REPLY and bool(n.get(OPENING) or n.get(HUOPAN)
                                                                            or n.get(SIGNED) or n.get(HANDCARDS))), now)
        # (a notice of records only had none of her lines)
        carried = [n for n in answered if n.get("into") == job["id"] and n.get("lines")]
        if carried:
            attention.append(f"#{job['id']} 的草稿也回了 " + "、".join(f"#{n.get('job')}" for n in carried)
                             + " 没写成时她的消息")
        if meta.get("missed"):                         # told to the Mac now: the draft's notes say it
            self._store.drop_notices(deal_id, lambda n: n["key"] == "missed", now)
        if lane == LANE_SYSTEM and state == SUPERSEDED and mode != COPILOT:
            # A person took the chat while the step was drafted: it is owed, asked for again when that ends.
            self._owe_step(deal, f"#{job['id']} {step_word(meta)}这一步的草稿没进待审批", str(meta["system_note"]),
                           step.get("packet"), bool(step.get("delivered")), now, item=step.get("item") or "",
                           action=step.get("action") or "", prefix=step.get("prefix") or "", uid=step.get("uid") or "",
                           tries=int(step.get("tries") or 0))
        elif lane == LANE_SYSTEM and state == SUPERSEDED:
            attention.append(f"#{job['id']} {step_word(meta)}这一步的草稿没进待审批（又有了新的一步），请运营核对")
        if short:
            attention.append(short)
        if refused:
            attention.append(f"#{job['id']} {refused}")
        if stepless and state != SUPERSEDED:
            attention.append(f"#{job['id']} {stepless}")
        if doubts and state == PENDING:
            attention.append(f"#{job['id']} {doubts}")
        if draft.message_kind == "shipping_notice" and draft.reply_draft and not gates.waybill_ok(draft.reply_draft, waybill):
            attention.append("发货通知里的单号和系统记录对不上" if waybill else "还没有运单号，发货通知不能发")
        if draft.platform is not None and draft.platform.get("verdict") != "ready" and not cover:
            why = "；".join(draft.platform.get("problems") or []) or platforms.VERDICTS.get(draft.platform.get("verdict"), "")
            attention.append(f"平台动作预览没通过：{why}"[:200])
        # 专属链接: this draft says the deal asks for links now (the creator or the boss asked for them): a chain that is
        # done, or between two steps, goes on with the links (``_chain``); one under way picks them up as it goes on
        slots = self._link_slots(deal, draft.slots, now)
        wish_before, wish = link_wish(deal.get("slots")), link_wish({**(deal.get("slots") or {}), **slots})
        self._store.update_deal(deal_id, now, merge_slots=slots, stage_proposal=draft.stage_proposal)
        self._store.set_notice(deal_id, DRAFT_NOTES + lane, "；".join(attention), now)   # its own notes: replaced
        if lane == LANE_SYSTEM and state == NO_REPLY:
            self._chain_go(deal, now)                  # the step drafted nothing: the chain goes on
        elif state == DROPPED:
            self._chain_go(deal, now, after=self._store.packet(packet_id))    # opened by the invite card: the next one
        elif lane == LANE_SYSTEM or tool in CHAIN_TOOLS:
            self._chain_refresh(deal_id, now)
            if lane == LANE_SYSTEM and wish:
                self._links_quietly(deal, now)         # (a deal without a chain: its links left, the notice of them)
        self._links_after_slots(deal, wish_before, wish, now)
        if state == APPROVED:
            self._wake.set()
        logger.info("PRM: job #%d -> packet %d (%s, %s, %s)", job["id"], packet_id, state, route, draft.message_kind)
        if night_held:
            logger.info("PRM: packet %d was drafted in %s and waits for a person: %s", packet_id,
                        "auto mode" if by_auto else "the night window", night_held)
        return True

    def _draft_failed(self, job: dict, meta: dict, head: str, notes: list, now: float) -> None:
        """A draft job that gave no draft (the Mac failed, went silent, answered nothing usable): a notice
        that stays until it is seen to (mac review F1; before, the next draft's notes wiped it and her
        message was never answered).  A reply job's notice keeps her lines (its text and the follow-ups
        the Mac had, ``_job_lines``) and what it was told (``history``, ``late`` / ``missed`` /
        ``new_friend``): her next draft job takes them along (``_submit``), and its draft, or this job's
        own coming back late, sees to the notice; an operator answering her by hand puts it away
        (``_answered_by_hand``).  A step the bot asked for (the system lane) is not asked again by
        itself: its notice goes when the next step is asked for or the deal is set back before 已签收."""
        deal_id, party_id, job_id = int(meta["deal_id"]), int(meta["party_id"]), int(job["id"])
        lane = LANE_SYSTEM if meta.get("system_note") else LANE_REPLY
        with self._handle_lock:                        # (``_submit`` takes these notices along)
            if any(int(ev["detail"].get("job") or 0) == job_id
                   for ev in self._store.events(deal_id, limit=50, kinds=("draft_failed",))):
                return                                 # lost first, then failed: said once
            self._store.add_event(deal_id, party_id, "draft_failed", {"job": job_id, "why": head[:200]},
                                  actor="mac", now=now)
            if meta.get("missed"):                     # this notice says it now
                self._store.drop_notices(deal_id, lambda n: n["key"] == "missed", now)
            if lane == LANE_SYSTEM:
                # the step it was (建专属链接 or 开专属佣金, ``step_word``), said once: a head naming it already (a
                # group's that closed, ``_group_job_closed``) keeps its words
                word = f"{step_word(meta)}这一步的草稿"
                self._store.set_notice(deal_id, f"failed:{job_id}", "；".join(
                    [head if word in head else head.replace("草稿没写成", f"{word}没写成", 1), *notes,
                     "系统不会自己再起草这一步，请运营处理"]), now, job=job_id, lane=lane)
                return
            lines = self._job_lines(job)
            if isinstance(meta.get(OPENING), dict) and not lines:
                # our first line, and she has said nothing: no line of hers to take along; a later reply draft, a new
                # 开场白, or an operator writing to her by hand sees to it
                self._store.set_notice(deal_id, f"failed:{job_id}", "；".join(
                    [head.replace("草稿没写成", "开场白没写成", 1),
                     "请直接在老板拉的群里跟她打招呼" if meta.get("channel") == GROUP_CHANNEL
                     else "可以在合作单里点「写开场白」再写一次，或者直接在微信里跟她打招呼"]), now, job=job_id, lane=lane,
                    opening=True, asked_at=float(job.get("created_at") or now))
                return
            if isinstance(meta.get(HUOPAN), dict) and not lines:
                # 补发货盘, and she said nothing meanwhile: no line of hers to take along; a later reply draft, another
                # 补发货盘, or an operator writing to her by hand sees to it
                self._store.set_notice(deal_id, f"failed:{job_id}", "；".join(
                    [head.replace("草稿没写成", "补发货盘的草稿没写成", 1),
                     "可以在合作单里再点一次「补发货盘」，或者直接在微信里把货盘发给她"]), now, job=job_id, lane=lane,
                    huopan=True, asked_at=float(job.get("created_at") or now))
                return
            if isinstance(meta.get(HANDCARDS), dict) and not lines:
                # 「做手卡」, and she said nothing meanwhile (the Mac cannot make cards, say): another click, a later reply
                # draft, or an operator sending her cards by hand sees to it
                self._store.set_notice(deal_id, f"failed:{job_id}", "；".join(
                    [head.replace("草稿没写成", "做手卡的草稿没写成", 1),
                     "可以在合作单里再点一次「做手卡」，或者请 Mac 上的 Claude 用 live-handcard 做好发她"]), now, job=job_id,
                    lane=lane, handcards=True, asked_at=float(job.get("created_at") or now))
                return
            if isinstance(meta.get(SIGNED), dict) and not lines:
                # 签收后问一句, and she said nothing meanwhile: not asked again by itself; a later reply draft or an operator
                # writing to her by hand sees to it
                self._store.set_notice(deal_id, f"failed:{job_id}", "；".join(
                    [head.replace("草稿没写成", "聚水潭显示签收后问她一句的草稿没写成", 1),
                     "需要的话直接在微信里问她样品拿到了没有、合不合适"]), now, job=job_id, lane=lane,
                    signed=True, asked_at=float(job.get("created_at") or now))
                return
            # this job carried the lines of earlier ones: they are in its own lines now
            self._store.drop_notices(deal_id, lambda n: n.get("into") == job_id, now)
            head = "；".join([head, *notes])
            gap = {k: meta[k] for k in ("late", "missed", "new_friend") if isinstance(meta.get(k), dict)}
            if meta.get(LINK_FIRST) is True:
                gap[LINK_FIRST] = True                 # (the lines that waited at 接群: their next draft waits too)
            self._store.set_notice(deal_id, f"failed:{job_id}", failed_text(head), now, job=job_id, lane=lane,
                                   head=head, lines=lines[:LINES_CHARS], gap=gap,
                                   history=[h for h in meta.get("history") or [] if isinstance(h, dict)],
                                   asked_at=float(job.get("created_at") or now))

    def _job_lines(self, job: dict) -> str:
        """The creator's lines a draft job had: its text and the follow-ups the Mac was handed (those it
        never saw are a job of their own already: ``ClaudeRelay._requeue_leftovers``)."""
        rows = [str(r.get("text") or "") for r in (self._relay.store.followups(int(job["id"]), 0) or [])]
        try:
            later = int((json.loads(job.get("result_json") or "{}") or {}).get("followups_job") or 0)
        except (ValueError, TypeError, AttributeError):
            later = 0
        if later and rows:
            requeued = str((self._relay.store.get(later) or {}).get("text") or "")
            for n in range(len(rows), 0, -1):
                if "\n".join(rows[-n:]) == requeued:
                    rows = rows[:-n]
                    break
        return "\n".join(t for t in [str(job.get("text") or ""), *rows] if t.strip())

    def _cancelled(self, job_id) -> bool:
        """Was this job cancelled (an operator, in the console; one cancelled before the Mac took it is
        never handed back: ``RelayStore.cancel``)?"""
        job = self._relay.store.get(int(job_id or 0))
        return job is None or job["status"] == "cancelled"

    def _handed_on(self, deal: dict, job_id: int) -> bool:
        """Did this job give no draft before (lost, say) and her lines go into a later draft job?"""
        notice = next((n for n in deal.get("notices") or [] if n.get("key") == f"failed:{job_id}"), None)
        if notice is not None:
            return bool(notice.get("into"))
        return any(int(ev["detail"].get("job") or 0) == job_id
                   for ev in self._store.events(deal["id"], limit=50, kinds=("draft_failed",)))

    def _opening_outdated(self, job: dict, meta: dict) -> bool:
        """Is this the 开场白 of a 老板拉的群 (``meta.opening`` with ``meta.channel`` group) after which her own line there
        made a draft job (she spoke first: that job's reply answers her; no greeting after it)?"""
        if meta.get("channel") != GROUP_CHANNEL or not isinstance(meta.get(OPENING), dict):
            return False
        return any(int(j["id"]) > int(job["id"]) and j.get("kind") == PRM and str(j.get("text") or "").strip()
                   and not job_meta(j).get("op") for j in self._relay.store.recent(job["chat_id"], limit=20))

    def _mask_draft(self, party_id: int, draft, now: float):
        """The draft with its numbers as the creator's placeholders, before anything of it is stored.
        The Mac reads her pictures, so a number it read off a screenshot comes back written out (win
        review F5): in the reply, the slots, the intent and reason, a sample order's receiver.  It
        becomes the same 〈手机N〉 as a number she types (``token_for``): the console shows it masked
        and 显示完整号码 is written down; she (``_deliver``) and the Mac running the action
        (``action_for_mac``) get the number back."""
        mask = self._masker(party_id, now)
        platform = draft.platform
        if platform is not None:
            platform = {**platform, "action": masked_action(platform.get("action"), mask)}
            if isinstance(platform.get("requested"), dict):      # what a draft asked for that could not be used
                platform["requested"] = {k: mask(v) if k in PERSONAL_PARAMS else v
                                         for k, v in platform["requested"].items()}
        return dataclasses.replace(draft, reply_draft=mask(draft.reply_draft), intent=mask(draft.intent),
                                   reason=mask(draft.reason), slots={k: mask(v) for k, v in draft.slots.items()},
                                   platform=platform, platform_refused=mask(draft.platform_refused))

    def _masker(self, party_id: int, now: float) -> Callable:
        """``_mask`` for one creator's texts; an empty one, or one that is no text, stays as it is."""
        return lambda text: self._mask(party_id, text, now) if isinstance(text, str) and text else text

    # ── from the console ──────────────────────────────────────────────────

    def register(self, wxid: str, name: str, note: str = "", link: Optional[dict] = None, *,
                 catch_up: bool = True, opening: bool = True) -> dict:
        """Register a creator (an operator's decision in the console; with PRM_AUTO_REGISTER the bot registers
        new friends the same way, ``auto_register_friend``).  When she is a new friend (``on_registered``), her
        messages since she was added become one reviewed draft (take_new_friend); ``catch_up`` in the answer
        says so.  ``catch_up`` False: her new-friend catch-up is not asked for (no ``on_registered``).
        ``opening`` False (接手任何合作群's 「是 N」, BossGroups._link_by_hand; review 2026-11-10 A-F4, final check
        A7): it is, without a 开场白 (``on_registered(wxid, opening=False)``: with PRM_OPENING_AUTO_MIN one went out
        by itself, though the operator only linked a group), her earlier private lines still one draft, which waits
        for a person and never goes by itself (``take_new_friend``'s ``opening``).

        ``link``: who she is on 抖音 (加百应达人微信's 「是她，登记并关联」, or the 抖音号 登记达人 was given; ``check_link``).
        In this order: the party, the link (``creator_links``), the deal's ``friend_add_link`` event, and only then
        ``on_registered``, so her catch-up job is told her 抖音号 (``_creator_ids``).  A creator registered already only
        gets the link and its event (``linked`` in the answer): with PRM_AUTO_REGISTER the bot has registered her
        already by the time a person says 「是她」."""
        return self._register(wxid, name, note, link=link, catch_up=catch_up, opening=opening)

    def _register(self, wxid: str, name: str, note: str, how: Optional[str] = None,
                  link: Optional[dict] = None, catch_up: bool = True, opening: bool = True) -> dict:
        """``register``; ``how`` (PRM_AUTO_REGISTER: how the new friend was added): the bot's registration, only of a
        wxid with no party at all (``PrmStore.add_party(auto=True)``), the deal's record says so (``AUTO_EVENT``) and
        the log line names only the wxid's first 8 characters; the bot never links (``link`` is a person's)."""
        wxid = check_wxid(wxid, self._reserved)
        name, note = str(name or "").strip(), str(note or "").strip()
        if not name or len(name) > 30:
            raise PartyError("称呼要填，最多 30 个字")
        if len(note) > 200:
            raise PartyError("备注最多 200 个字")
        link = check_link(link)
        now = self._clock()
        if link is not None:
            existing = self._store.party_by_wxid(wxid)
            if existing is not None:
                self._link_creator(existing, link, now)
                return {**existing, "catch_up": False, "linked": True}
        party = self._store.add_party(wxid, name, note, now, auto=how is not None)
        if how is not None:
            deal = self._store.deal_of(party["id"], now)
            self._store.add_event(deal["id"], party["id"], AUTO_EVENT, {"how": how}, actor="system", now=now)
        if link is not None:
            self._link_creator(party, link, now)
        self._parties_changed()
        if how is not None:
            logger.info("PRM: new friend %s… registered automatically as party %d (PRM_AUTO_REGISTER; %s)",
                        wxid[:8], party["id"], how)
        else:
            logger.info("PRM: party %d registered", party["id"])
        caught = False
        if self.on_registered is not None and catch_up:
            try:
                caught = bool(self.on_registered(wxid) if opening else self.on_registered(wxid, opening=False))
            except Exception:
                logger.exception("PRM: party %d's earlier messages (a new friend) could not be asked for", party["id"])
        return {**party, "catch_up": caught}

    # ── PRM_AUTO_REGISTER: every new friend is registered by the bot ──────

    def new_friend_found(self, event: dict) -> None:
        """The new-friend scan's ``on_found`` (Bot._start_new_friends, copilot mode with PRM_AUTO_REGISTER=on): her
        wxid, name and how she was added are queued (never her greeting) and the sender thread is woken; it registers
        her at the start of its next round (``register_found``).  Never ``register`` here: the scan calls this inside
        its look, holding its ``_scan_lock``, and ``register`` calls back into the scan (``on_registered`` =
        ``NewFriendService.registered``, then the backend's ``catch_up_chat``).  The queue is kept in prm_meta too
        (``AUTO_QUEUE``): the scan reports a new friend once, ever, and a restart before that round would lose her
        (``resume_found``).  Never raises (a sink that raises would get the event again at every look); off, it does
        nothing."""
        if not self._auto_register:
            return
        try:
            item = {"wxid": str(event.get("wxid") or "").strip(), "name": str(event.get("name") or ""),
                    "how": str(event.get("how") or "")}
        except Exception:
            logger.warning("PRM: a new friend's event could not be read; not registered automatically", exc_info=True)
            return
        with self._found_lock:
            self._found.append(item)
            self._keep_found()
        self._wake.set()

    def register_found(self) -> int:
        """The new friends ``new_friend_found`` queued, registered one by one (oldest first) on the sender thread
        (``tick``), with none of the scan's locks held.  Returns how many were registered; one that is not (a rule
        says no, an error) is logged and stays in 登记达人's candidates for a person.  Each one seen to leaves the
        queue, the kept one too (``AUTO_QUEUE``).  Never raises."""
        with self._found_lock:
            found = list(self._found)
        done = 0
        for event in found:
            if self.auto_register_friend(event) is not None:
                done += 1
            with self._found_lock:                     # seen to: off the queue
                try:
                    self._found.remove(event)
                except ValueError:
                    pass
                self._keep_found()
        return done

    def resume_found(self) -> int:
        """At start, once the new-friend scan is wired (Bot._start_new_friends, PRM_AUTO_REGISTER on): the new friends
        queued before a restart and not seen to (``AUTO_QUEUE``; review 2026-10-30: the queue lived in memory only)
        go back on the queue, ahead of any queued since, and the sender thread is woken.  Returns how many."""
        kept = self._kept_found()
        with self._found_lock:
            back = [e for e in kept if e not in self._found]
            self._found.extendleft(reversed(back))
            if back:
                self._keep_found()
        if back:
            logger.info("PRM: %d new friend(s) queued before the restart are registered in the next round", len(back))
            self._wake.set()
        return len(back)

    def _kept_found(self) -> list:
        """The queue as ``AUTO_QUEUE`` keeps it (``[{wxid, name, how}]``; [] none, or unreadable: logged)."""
        try:
            data = json.loads(self._store.meta_value(AUTO_QUEUE) or "[]")
        except Exception:
            logger.warning("PRM: the kept queue of new friends could not be read; not registered automatically",
                           exc_info=True)
            return []
        items = [e for e in (data if isinstance(data, list) else []) if isinstance(e, dict)]
        return [{"wxid": str(e.get("wxid") or "").strip(), "name": str(e.get("name") or ""),
                 "how": str(e.get("how") or "")} for e in items if str(e.get("wxid") or "").strip()]

    def _keep_found(self) -> None:
        """``AUTO_QUEUE`` = the queue now (under ``_found_lock``).  An error is logged, never raised: she is still
        registered in this run, only a restart before it would lose her."""
        try:
            self._store.set_meta_value(AUTO_QUEUE, json.dumps(list(self._found), ensure_ascii=False))
        except Exception:
            logger.warning("PRM: the queue of new friends could not be kept for a restart", exc_info=True)

    def auto_register_friend(self, event: dict) -> Optional[dict]:
        """Register one new friend (``{wxid, name, how}``) as a full creator, as an operator's 登记 does
        (``_register``: her messages since she was added become one reviewed draft through ``on_registered``):
        称呼 ``auto_name``, note ``auto_note``, the deal's record ``AUTO_EVENT``, one log line with the wxid's first 8
        characters.  None when she is not registered (``_not_auto``: the admin or a cc user, not a person's private
        chat, a party already, active or stopped) or an error came (logged; never raised)."""
        wxid = ""
        try:
            wxid = str(event.get("wxid") or "").strip()
            how = str(event.get("how") or "").strip() or "unknown"
            why = self._not_auto(wxid)
            if why:
                logger.info("PRM: new friend %s… not registered automatically: %s", wxid[:8], why)
                return None
            return self._register(wxid, auto_name(event.get("name"), wxid), auto_note(how), how=how)
        except PartyError:
            # (its text may hold a creator's name: never logged) - a party made meanwhile, by a person
            logger.info("PRM: new friend %s… not registered automatically: she has a party already (made meanwhile)",
                        wxid[:8])
        except Exception:
            logger.warning("PRM: new friend %s… could not be registered automatically; she stays in 登记达人's list "
                           "for a person", wxid[:8], exc_info=True)
        return None

    def _not_auto(self, wxid: str) -> str:
        """Why PRM_AUTO_REGISTER leaves this wxid alone ("" = it may register her): not a person's private chat (a
        group, an official account, 文件传输助手: the scan never reports them either), the admin or a cc user, no wxid
        a creator can have (``check_wxid``), or a party with this wxid already, active or stopped (an operator's 停用
        sticks; a person may register her again in the console).  (``is_person_chat`` reads the WCDB backend's list
        of WeChat's own chats: that module is loaded wherever the scan runs, Windows wcdb; elsewhere the import fails,
        which ``auto_register_friend`` logs, and she stays a candidate.)"""
        if not is_person_chat(wxid):
            return "not a person's private chat"
        if wxid in self._reserved:
            return "the admin or a cc user (never a creator)"
        try:
            check_wxid(wxid, self._reserved)
        except PartyError:
            return "not a wxid a creator can have"
        party = self._store.party_by_wxid(wxid, include_inactive=True)
        if party is not None:
            return "she has a party already (stopped: an operator's 停用 stays)" if not party["active"] else \
                "she is registered already"
        return ""

    def _link_creator(self, party: dict, link: dict, now: float) -> None:
        """Who a registered creator is on 抖音 (``check_link``'s ``link``): ``creator_links`` (the first link of an
        account stays) and a ``friend_add_link`` event on her deal (her 抖音号, the record, when the invite card went
        out; never a number); the deal says so when her sample order knows another 抖音号 (``_creator_ids_notice``)."""
        made = link_creator(self._store, party, link, now=now)
        logger.info("PRM: party %d linked to a 抖音号 (%s%s)", party["id"], link["source"], "" if made else ", known already")
        if link["source"] == "register":           # registered by hand with the 抖音号 a 加微信 record waits for
            try:
                self.friend_adds.linked_by_register(party, link["account"], party["wxid"])
            except Exception:
                logger.exception("PRM: the 加微信 records of party %d's 抖音号 could not be closed", party["id"])

    def unlink_creator(self, party_id: int, account: str) -> dict:
        """取消关联 from the console (``unlink_creator``)."""
        out = unlink_creator(self._store, party_id, account, now=self._clock())
        if out.get("ok"):
            logger.info("PRM: party %d unlinked from a 抖音号", int(party_id))
        return out

    def take_new_friend(self, chat_id: str, messages: list[dict], *, verify_until: float = 0.0, how: str = "",
                        whole: bool = False, opening: bool = True) -> int:
        """A new friend registered as a creator: her messages since she was added (the WeChat backend
        read them back on her chat's next poll: hers only, oldest first, at most PRM_CATCHUP_HOURS
        old) become one draft job that waits for a person (``meta.new_friend``), so her first words
        are not lost; the ones up to ``verify_until`` (the time she was added) are her friend-request
        greeting and say so.  WeChat's friend notices are left out.  Nothing of hers (she accepted our
        friend request and has not written): one 开场白 draft job instead (``_ask_opening``; ``how``: how she
        was added, the new-friend scan's), only when the read-back reached back to when she was added (``whole``,
        ``NewFriendService.registered``; registered later than PRM_CATCHUP_HOURS, she may have written before the
        window: review 2026-10-30).  A creator a boss introduced (老板拉的群 taken over, or being taken over) has the
        bosses' names hidden in them (``hide``).  ``opening`` False (registered by 「是 N」, a group linked by
        hand: final check 2026-11-10 A7): never a 开场白 (nor its notice), and her lines' draft never goes by
        itself (``meta.new_friend`` ``linked``: no PRM_OPENING_AUTO_MIN, no holding reply).  Returns how many of
        her messages were taken."""
        chat_id = str(chat_id)
        party = self._store.party_by_wxid(chat_id)
        if party is None:
            return 0
        picked = [m for m in messages or [] if not m.get("is_group", False) and not is_friend_notice(m)
                  and system_line(m) is None]
        if not picked and not opening:
            logger.info("PRM: party %d was registered for a group linked by hand and has no private messages to catch "
                        "up on; no 开场白", party["id"])
            return 0
        if not picked:
            if not whole:
                now = self._clock()
                with self._handle_lock:
                    deal = self._store.deal_of(party["id"], now)
                    if not deal.get("last_message_at"):
                        self._store.set_notice(deal["id"], OPENING_UNSURE, OPENING_UNSURE_TEXT, now, opening=True)
                logger.info("PRM: party %d is a new friend with no messages in the catch-up window, which does not reach "
                            "back to when she was added; no 开场白 (she may have written before it), a notice asks a "
                            "person to look", party["id"])
                return 0
            with self._handle_lock:
                job_id, why = self._ask_opening(party, how=how)
            logger.info("PRM: party %d is a new friend with no messages from before the registration; %s", party["id"],
                        f"job #{job_id} drafts our first line (开场白)" if job_id else f"no 开场白: {why}")
            return 0
        now = self._clock()
        times = [float(m.get("timestamp") or 0) or now for m in picked]
        info = {"from": min(times), "count": len(picked), **({} if opening else {"linked": True})}
        taken = 0
        for msg, at in zip(picked, times):
            if self.boss_groups.masks(party):
                # 老板拉的群 (second review 2026-10-31 F9): a creator the boss introduced (her group taken over, or being
                # taken over now) speaks of him: his name never goes to the Mac, as in her live lines (``_take``)
                line = self.boss_groups.hide(message_text(self.boss_groups.record_msg(msg, party)))
            else:
                line = message_text(msg)
            if verify_until and at <= verify_until and str(msg.get("msg_type", 1)) == "1":
                line = f"（加好友时的验证消息）{line}"
            files: tuple = ()
            kind = attachment_kind(msg)
            if kind:
                collected, problems = self._relay.collect([Held(kind, msg, now)])
                files = tuple(collected)
                if problems:
                    line += "（" + "；".join(problems) + "）"
                elif not files:
                    line += f"（{ATTACHMENT_KINDS[kind]}没取到）"
            with self._handle_lock:
                if self._recorded_for_a_person(party, line, at, now):
                    continue
                self._submit(party, chat_id, line, files, at, max(0.0, now - at), False, now, new_friend=info)
                taken += 1
        logger.info("PRM: party %d is a new friend: %d message(s) from before the registration -> one reviewed "
                    "draft job", party["id"], taken)
        return taken

    def ask_opening(self, deal_id, actor: str = "console") -> dict:
        """「写开场白」 in the console: one 开场白 draft job for a creator who has not written to us yet
        (``_ask_opening``, by console; ``actor``: who asked, the event's, ``ADMIN_ACTOR`` for 合作单指令's 「写开场白 <达人>」).
        ``{"ok": True, "job"}``, or ``{"ok": False, "error"}`` saying why not."""
        try:
            deal = self._store.deal(int(deal_id))
        except (TypeError, ValueError):
            deal = None
        party = self._store.party(deal["party_id"]) if deal else None
        if deal is None or party is None:
            return {"ok": False, "error": "没有找到这个合作单"}
        with self._handle_lock:
            job_id, why = self._ask_opening(party, by="console", actor=actor)
        return {"ok": True, "job": job_id} if job_id else {"ok": False, "error": why}

    def _ask_opening(self, party: dict, *, how: str = "", by: str = "new_friend", actor: str = "console") -> tuple:
        """One draft job of our first line to a creator who has not written to us, or wrote but never had a real line
        from us (``meta.opening``: how she came, who asked, and ``wrote`` true for the latter: her line was the friend
        request's 「我是…」, its draft was dropped, only a 稍等 went out, 2026-11-02; told what we sent her,
        ``meta.sent``, what an operator wrote to her by hand, ``meta.history``, and the deal's 起草备注, ``meta.note``),
        under ``_handle_lock``: ``(job id, "")``, or ``(None, why not)`` (``_no_opening``).  It always waits for a person (``gap_notes``), never by night, never gets a holding
        reply; a message of hers joins it, or outdates its draft (``_outdated``), like any reply draft's."""
        now = self._clock()
        deal = self._store.deal_of(party["id"], now)
        # (老板拉的群: no private chat with her, it goes to the group; PRM_REACH_LAST_SPOKE: the console's 「写开场白」 goes
        # where her side last spoke, a new friend's 开场白 answers her friend request: her private chat as before)
        chat, group, reach = self._reaching(party, made=by == "console", deal=deal)
        why = self._no_opening(party, deal, now, chat)
        if why:
            return None, why
        how = str(how or "")
        held = by == "console" and actor == ADMIN_ACTOR    # (合作单指令: it always waits for a person)
        meta = {"party_id": party["id"], "deal_id": deal["id"], "name": party["name"],
                OPENING: {"how": how, "by": by, **({"wrote": True} if deal.get("last_message_at") else {}),
                          **({OPENING_HELD: True} if held else {})}}
        if group is not None:
            meta.update(channel=GROUP_CHANNEL, group=self.boss_groups.job_group(chat), **reach_meta(reach))
        else:
            told = self._group_lines(deal["id"])
            if told:
                meta["three_party_chat"] = told         # what was said in her 三方群 lately
            if self._opening_auto_sec and not held:
                meta["opening_auto"] = self._opening_auto_sec // 60   # the Mac hears its draft may go out by itself
        creator = self._creator_ids(deal["id"])
        if creator:
            meta["creator"] = creator
        outbound = self._outbound(deal["id"], party["id"], now)
        if outbound:
            meta["sent"] = outbound
        # What an operator wrote to her by hand while a person had the chat (she has written nothing: no line of hers
        # is recorded): told, not taken, so her next draft job still hears of it should this one give no draft.
        told = history_of(self._store.backlog(deal["id"]))
        if told:
            meta["history"] = told
        _, job_id = self._relay.submit(chat, chat, "", kind=PRM, meta=self._reply_meta(self._with_note(meta, deal, now)), fresh=True)
        self._store.add_event(deal["id"], party["id"], OPENING, {"job": job_id, "how": how, "by": by},
                              actor=actor if by == "console" else "system", now=now)
        # one not written before: this one is on its way
        self._store.drop_notices(deal["id"], lambda n: bool(n.get(OPENING)), now)
        logger.info("PRM: party %d %s; job #%d drafts our first line (开场白, %s)", party["id"],
                    "wrote but never had a real line from us" if deal.get("last_message_at") else "has not written to us",
                    job_id, by)
        return job_id, ""

    def _no_opening(self, party: dict, deal: dict, now: float, chat: Optional[str] = "") -> str:
        """Why no 开场白 is drafted for her now ("" = it is): off the list, a person has the chat, she wrote to us
        already on WeChat and had a real line from us (``PrmStore.has_answer``: her messages are drafted; written to but
        never answered, a 稍等 aside, she may have one, 2026-11-02), or a WeChat reply draft of hers is being written or waits in
        待审批 (a 百应 one is another channel: it neither counts as her WeChat line nor stands in the way).  ``chat``:
        where it would go (``_reach``; "" her private chat, None nowhere: 老板拉的群 she left, no private chat)."""
        if not party.get("active"):
            return "这位达人已经停用（不在达人名单里），不写开场白"
        if self._mode(deal, now) != COPILOT:
            return "现在不在副驾驶模式（人工接管或暂停），不写开场白"
        if deal.get("last_message_at") and self._store.has_answer(deal["id"]):
            return "她已经发过消息了：她的消息会自己起草，不用开场白"
        if chat is None:
            return f"{UNREACHABLE_TEXT}，开场白发不出去"
        if self._relay.store.latest_open(chat or party["wxid"], drafts_only=True, system=False) is not None:
            return "她的草稿正在写，写好会出现在「待审批」里"
        if any((p.get("meta") or {}).get("lane") not in (LANE_SYSTEM, LANE_BUYIN)
               for p in self._store.deal_packets(deal["id"], statuses=(PENDING, APPROVED, SENDING))):
            return "「待审批」里已经有她的草稿"
        return ""

    def ask_huopan(self, deal_id, actor: str = "console") -> dict:
        """「补发货盘」 in the console (2026-11-04): one draft job that sends her the 货盘 again (``_ask_huopan``; ``actor``:
        who asked, the event's).  ``{"ok": True, "job"}``, or ``{"ok": False, "error"}`` saying why not."""
        try:
            deal = self._store.deal(int(deal_id))
        except (TypeError, ValueError):
            deal = None
        party = self._store.party(deal["party_id"]) if deal else None
        if deal is None or party is None:
            return {"ok": False, "error": "没有找到这个合作单"}
        with self._handle_lock:
            job_id, why = self._ask_huopan(party, actor=actor)
        return {"ok": True, "job": job_id} if job_id else {"ok": False, "error": why}

    def _ask_huopan(self, party: dict, actor: str = "console") -> tuple:
        """One draft job of our line that sends her the 货盘 (kb 第 3.0 节: the picture, the Excel as it says) again,
        with no new line of hers (``meta.huopan`` {"by": "console"}; told what we sent her, ``meta.sent``, what an
        operator wrote to her by hand, ``meta.history``, her 三方群's, and the deal's 起草备注, ``meta.note``), under
        ``_handle_lock``: ``(job id, "")``, or ``(None, why not)`` (``_no_huopan``).  Its draft always waits for a person
        (``gap_notes``): never by a gate, by night, in 自动模式 or by PRM_OPENING_AUTO_MIN; no holding reply goes for it
        unless a message of hers joined the job; her message after it outdates the draft (``_outdated``: her job hears it
        was not sent)."""
        now = self._clock()
        deal = self._store.deal_of(party["id"], now)
        # (老板拉的群: no private chat with her, it goes to the group; PRM_REACH_LAST_SPOKE: where her side last spoke)
        chat, group, reach = self._reaching(party, made=True, deal=deal)
        why = self._no_huopan(party, deal, now, chat)
        if why:
            return None, why
        meta = {"party_id": party["id"], "deal_id": deal["id"], "name": party["name"], HUOPAN: {"by": "console"}}
        if group is not None:
            meta.update(channel=GROUP_CHANNEL, group=self.boss_groups.job_group(chat), **reach_meta(reach))
        else:
            told = self._group_lines(deal["id"])
            if told:
                meta["three_party_chat"] = told         # what was said in her 三方群 lately
        made = self.three_party.job_meta(deal)
        if made:
            meta["three_party"] = made                 # 三方群 made: the samples are talked over there
        creator = self._creator_ids(deal["id"])
        if creator:
            meta["creator"] = creator
        outbound = self._outbound(deal["id"], party["id"], now)
        if outbound:
            meta["sent"] = outbound                    # (the Mac sees whether the 货盘 went out within 7 days)
        told = history_of(self._store.backlog(deal["id"]))
        if told:
            meta["history"] = told                     # told, not taken: her next draft job still hears of it
        _, job_id = self._relay.submit(chat, chat, "", kind=PRM, meta=self._reply_meta(self._with_note(meta, deal, now)), fresh=True)
        self._store.add_event(deal["id"], party["id"], HUOPAN, {"job": job_id}, actor=actor, now=now)
        # one not written before: this one is on its way
        self._store.drop_notices(deal["id"], lambda n: bool(n.get(HUOPAN)), now)
        logger.info("PRM: party %d: job #%d sends her the 货盘 again (补发货盘, console)", party["id"], job_id)
        return job_id, ""

    def _no_huopan(self, party: dict, deal: dict, now: float, chat: Optional[str] = "") -> str:
        """Why no 补发货盘 is drafted for her now ("" = it is): off the list, a person has the chat, she cannot be reached
        (``_reach``: ``chat`` None), or a WeChat draft of hers is being written or waits in 待审批 (a 百应 one is another
        channel and does not stand in the way).  Whether she had the 货盘 within 7 days is the Mac's to see (the case
        file, ``meta.sent``); the bot leaves out a file she got within REPEAT_FILE_SEC all the same."""
        if not party.get("active"):
            return "这位达人已经停用（不在达人名单里），不补发货盘"
        if self._mode(deal, now) != COPILOT:
            return "现在不在副驾驶模式（人工接管或暂停），不补发货盘"
        if chat is None:
            return f"{UNREACHABLE_TEXT}，货盘发不出去"
        if self._relay.store.latest_open(chat or party["wxid"], drafts_only=True, system=False) is not None:
            return "她的草稿正在写，写好会出现在「待审批」里"
        if any((p.get("meta") or {}).get("lane") not in (LANE_SYSTEM, LANE_BUYIN)
               for p in self._store.deal_packets(deal["id"], statuses=(PENDING, APPROVED, SENDING))):
            return "「待审批」里已经有她的草稿：先处理那一份"
        return ""

    def ask_handcards(self, deal_id, items=None, actor: str = "console") -> dict:
        """「做手卡」 in the console (2026-11-09): one draft job that makes her 直播手卡 (``_ask_handcards``).  ``items``: the
        ticked products (抖店 item ids; [] none: the ones she picked, as the case file has them); None: her latest
        sample order's.  ``actor``: who asked, the event's.  ``{"ok": True, "job"}``, or ``{"ok": False, "error"}`` saying
        why not."""
        try:
            deal = self._store.deal(int(deal_id))
        except (TypeError, ValueError):
            deal = None
        party = self._store.party(deal["party_id"]) if deal else None
        if deal is None or party is None:
            return {"ok": False, "error": "没有找到这个合作单"}
        ids, why = card_ids(items)
        if why:
            return {"ok": False, "error": why}
        with self._handle_lock:
            job_id, why = self._ask_handcards(party, ids, actor=actor)
        return {"ok": True, "job": job_id} if job_id else {"ok": False, "error": why}

    def _ask_handcards(self, party: dict, ids: Optional[list], actor: str = "console") -> tuple:
        """One draft job of our line that makes her 直播手卡, with no new line of hers (``meta.handcards`` {"by": "console",
        "items": [{"item_id", "sku_ids"}], "order"?}: the ticked products, ``ids``, each with the SKUs she got when it is in
        her latest 抖店 sample order; ``ids`` None: that order's products; none at all: the Mac finds them in the case
        file, or makes none), told what we sent her, an operator's lines, her 三方群's and the deal's 起草备注 like
        ``_ask_huopan``'s, under ``_handle_lock``: ``(job id, "")``, or ``(None, why not)`` (``_no_handcards``).  Its draft
        always waits for a person; no holding reply goes for it unless a message of hers joined the job."""
        now = self._clock()
        deal = self._store.deal_of(party["id"], now)
        chat, group, reach = self._reaching(party, made=True, deal=deal)   # (PRM_REACH_LAST_SPOKE: where her side spoke)
        why = self._no_handcards(party, deal, now, chat)
        if why:
            return None, why
        order = sample_items(self._store, deal["id"])
        known = {i["item_id"]: i for i in (order or {}).get("items") or []} if (order or {}).get("shop") == "dy" else {}
        picked = list(known.values()) if ids is None else [known.get(i) or {"item_id": i, "sku_ids": []} for i in ids]
        ask = {"by": "console", "items": picked}
        if order and order["order"] and any(i["item_id"] in known for i in picked):
            ask["order"] = order["order"]
        meta = {"party_id": party["id"], "deal_id": deal["id"], "name": party["name"], HANDCARDS: ask}
        if group is not None:
            meta.update(channel=GROUP_CHANNEL, group=self.boss_groups.job_group(chat), **reach_meta(reach))
        else:
            told = self._group_lines(deal["id"])
            if told:
                meta["three_party_chat"] = told
        made = self.three_party.job_meta(deal)
        if made:
            meta["three_party"] = made
        creator = self._creator_ids(deal["id"])
        if creator:
            meta["creator"] = creator
        outbound = self._outbound(deal["id"], party["id"], now)
        if outbound:
            meta["sent"] = outbound
        told = history_of(self._store.backlog(deal["id"]))
        if told:
            meta["history"] = told
        _, job_id = self._relay.submit(chat, chat, "", kind=PRM, meta=self._reply_meta(self._with_note(meta, deal, now)),
                                       fresh=True)
        self._store.add_event(deal["id"], party["id"], HANDCARDS, {"job": job_id, "items": len(picked)}, actor=actor,
                              now=now)
        self._store.drop_notices(deal["id"], lambda n: bool(n.get(HANDCARDS)), now)
        logger.info("PRM: party %d: job #%d makes her 直播手卡 (%d product(s), console)", party["id"], job_id, len(picked))
        return job_id, ""

    def _no_handcards(self, party: dict, deal: dict, now: float, chat: Optional[str] = "") -> str:
        """Why no 做手卡 is asked for now ("" = it is): PRM_HANDCARDS off, then as ``_no_huopan``: off the list, a person
        has the chat, she cannot be reached, a WeChat draft of hers is being written or waits in 待审批."""
        if not self._handcards:
            return "手卡没打开（Windows 的 PRM_HANDCARDS），不能做手卡"
        if not party.get("active"):
            return "这位达人已经停用（不在达人名单里），不做手卡"
        if self._mode(deal, now) != COPILOT:
            return "现在不在副驾驶模式（人工接管或暂停），不做手卡"
        if chat is None:
            return f"{UNREACHABLE_TEXT}，手卡发不出去"
        if self._relay.store.latest_open(chat or party["wxid"], drafts_only=True, system=False) is not None:
            return "她的草稿正在写，写好会出现在「待审批」里"
        if any((p.get("meta") or {}).get("lane") not in (LANE_SYSTEM, LANE_BUYIN)
               for p in self._store.deal_packets(deal["id"], statuses=(PENDING, APPROVED, SENDING))):
            return "「待审批」里已经有她的草稿：先处理那一份"
        return ""

    def set_draft_note(self, deal_id, text, actor: str = "console") -> dict:
        """「起草备注」 in the console (the deal action ``draft_note`` {text}): what the operators tell the Mac about this
        creator, which every draft job of the deal carries from now on (``_with_note``); "" clears it.  Each line's
        whitespace is collapsed, empty lines dropped, the lines kept (``store.draft_note_text``); longer than
        DRAFT_NOTE_CHARS is refused, not cut.  (It confirms no hat for a sample order: that is 「对过图了」,
        ``photo_confirm``.)  A change is one event
        (``chars``: how long the note is now; its words are in no event and no log line).  ``{"ok": True, "draft_note"}``,
        or ``{"ok": False, "error"}`` saying why not."""
        try:
            deal = self._store.deal(int(deal_id))
        except (TypeError, ValueError):
            deal = None
        if deal is None:
            return {"ok": False, "error": "没有找到这张合作单"}
        if not isinstance(text, str):
            return {"ok": False, "error": "要给出起草备注的文字（空着就是清掉）"}
        note = draft_note_text(text, limit=None)
        if len(note) > DRAFT_NOTE_CHARS:
            return {"ok": False, "error": f"起草备注最多 {DRAFT_NOTE_CHARS} 个字（现在 {len(note)} 个）"}
        if note == str(deal.get("draft_note") or ""):
            return {"ok": True, "draft_note": note}    # (a second click: nothing changed, nothing recorded)
        now = self._clock()
        self._store.update_deal(deal["id"], now, draft_note=note)
        self._store.add_event(deal["id"], deal["party_id"], DRAFT_NOTE, {"chars": len(note)}, actor=actor, now=now)
        logger.info("PRM: deal %d's draft note %s (%d characters)", deal["id"], "written" if note else "cleared",
                    len(note))
        return {"ok": True, "draft_note": note}

    def photo_confirm(self, deal_id, packet_id, code, actor: str = "console") -> dict:
        """「对过图了，用 <编码>」 in the console (the deal action ``photo_confirm`` {packet, code}; PRM_DRAFT_CONTEXT): an
        operator compared the pictures and lets a hat the Mac held go, for the whole deal, until 「撤销」
        (``photo_unconfirm``); the next preview of a sample order of the deal takes it (「重新预览」), like an earlier
        order of hers that went out.  Only a code the draft's last preview held for photo reasons (or let go by a
        confirmation taken back since: ``draft_context.confirmable``), on a sample order still waiting that is not being
        previewed again; anything else is refused.  One event ({code, packet}: no names; ``actor`` ``WECHAT_ACTOR``
        when the admin's quoted 「对过图 1」 in WeChat did it, ``WechatReview``); a second click records nothing.  Once
        it leaves no hat of this draft held and unconfirmed, the bot asks for the new preview itself (2026-11-10: as
        「重新预览」 does, ``_repreview``; never for a hat the preview already let go, nor after 「撤销」).  ``{"ok":
        True, "code", "confirmed": True, "left", "repreview"}`` (``left``: the codes still held and unconfirmed;
        ``repreview``: the new preview was started, with ``repreview_error`` why not when it should have been, and
        ``mac_online``: a worker that makes it was heard from lately, ``_worker_online``; False, the job waits for the
        Mac and the answer says so: review 2026-11-10), or ``{"ok": False, "error"}``."""
        if not self._draft_context:
            return {"ok": False, "error": "对图没有打开（PRM_DRAFT_CONTEXT），不用点「对过图了」"}
        try:
            deal = self._store.deal(int(deal_id))
            packet = self._store.packet(int(packet_id))
        except (TypeError, ValueError):
            deal, packet = None, None
        if deal is None:
            return {"ok": False, "error": "没有找到这张合作单"}
        if packet is None or packet["deal_id"] != deal["id"]:
            return {"ok": False, "error": "没有找到这份草稿"}
        if packet.get("tool") != "jst.sample_order":
            return {"ok": False, "error": "这份草稿不是寄样单"}
        if packet["status"] != PENDING:
            return {"ok": False, "error": "这份草稿已经处理过了"}
        if (packet.get("platform") or {}).get("previewing"):
            return {"ok": False, "error": "正在重新预览：等新预览出来再点"}
        wanted = draft_context.code_of(code)
        if not wanted or wanted not in draft_context.confirmable(packet):
            return {"ok": False, "error": f"这份草稿现在的预览没有扣着 {wanted or '这个编码'}：只能确认预览里扣着的那顶"}
        now = self._clock()
        if self._store.confirm_photo(deal["id"], wanted, packet["id"], now):
            self._store.add_event(deal["id"], deal["party_id"], PHOTO_CONFIRM, {"code": wanted, "packet": packet["id"]},
                                  actor=actor, now=now)
            logger.info("PRM: deal %d: %s confirmed by photo in %s (packet %d)", deal["id"], wanted,
                        "WeChat" if actor == WECHAT_ACTOR else "the console", packet["id"])
        out = {"ok": True, "code": wanted, "confirmed": True, "repreview": False,
               "left": draft_context.held_unconfirmed(packet, self._confirmed_codes(deal["id"]))}
        if not out["left"] and wanted in draft_context.codes_of(packet.get("meta") or {}, draft_context.PHOTO_HELD):
            # the last held hat of this draft is confirmed: the new preview that takes the confirmations, by itself
            started = self._repreview(packet, now, auto=True)
            out["repreview"] = bool(started.get("ok"))
            if started.get("ok"):
                out["mac_online"] = self._worker_online()
                if not out["mac_online"]:
                    logger.warning("PRM: packet %d: previewed again by itself, but no worker on the Mac was heard from "
                                   "lately; the job waits for it", packet["id"])
            if not started.get("ok"):
                out["repreview_error"] = str(started.get("error") or "")
                logger.warning("PRM: packet %d: its held hats are all confirmed, but the new preview did not start",
                               packet["id"])
        return out

    def _worker_online(self) -> bool:
        """Was a worker on the Mac that does the PRM's jobs heard from lately (the relay's ``worker_kinds``: within its
        KINDS_WINDOW_SEC; a relay that cannot say: its ``mac_online``, else yes)?  A new preview asked for meanwhile
        waits in the queue until it is back."""
        try:
            kinds = getattr(self._relay, "worker_kinds", None)
            if callable(kinds):
                return PRM in kinds()
            online = getattr(self._relay, "mac_online", None)
            return bool(online()) if callable(online) else True
        except Exception:
            logger.exception("PRM: whether the Mac is online could not be read")
            return True

    def photo_unconfirm(self, deal_id, code, actor: str = "console") -> dict:
        """「撤销」 of a 「对过图了」 (the deal action ``photo_unconfirm`` {code}): the code is not confirmed for the deal any
        more; a waiting draft whose last preview went by it cannot be approved until a new preview
        (``draft_context.withdrawn``).  One event ({code}); a second click records nothing.  ``{"ok": True, "code",
        "confirmed": False}`` or ``{"ok": False, "error"}``."""
        try:
            deal = self._store.deal(int(deal_id))
        except (TypeError, ValueError):
            deal = None
        if deal is None:
            return {"ok": False, "error": "没有找到这张合作单"}
        wanted = draft_context.code_of(code)
        if not wanted:
            return {"ok": False, "error": "编码不对"}
        now = self._clock()
        if self._store.unconfirm_photo(deal["id"], wanted):
            self._store.add_event(deal["id"], deal["party_id"], PHOTO_UNCONFIRM, {"code": wanted}, actor=actor, now=now)
            logger.info("PRM: deal %d: the photo confirmation of %s taken back in the console", deal["id"], wanted)
        return {"ok": True, "code": wanted, "confirmed": False}

    def _group_opening(self, party: dict, chat: str) -> tuple:
        """The 开场白 of a 老板拉的群 the bot just took over (src/prm/boss_groups.py ``take_over``): one draft job of our
        first line in the group (``meta.opening`` ``{"how": "boss_group", "by": "boss_group"}``, ``meta.channel`` group,
        ``meta.group``; told what we sent her and what an operator wrote to her by hand, as ``_ask_opening``):
        ``(job id, "")``, or ``(None, why not)``.  It always waits for a person (``gap_notes``), never gets a holding
        reply; her own line in the group outdates it (``_submit``, ``_opening_outdated``)."""
        with self._handle_lock:
            now = self._clock()
            deal = self._store.deal_of(party["id"], now)
            why = self._group_opening_why(party, chat, deal, now)
            if why:
                return None, why
            meta = {"party_id": party["id"], "deal_id": deal["id"], "name": party["name"],
                    OPENING: {"how": BOSS_OPENING, "by": BOSS_OPENING}, "channel": GROUP_CHANNEL,
                    "group": self.boss_groups.job_group(chat)}
            creator = self._creator_ids(deal["id"])
            if creator:
                meta["creator"] = creator
            group = self.three_party.job_meta(deal)
            if group:
                meta["three_party"] = group
            outbound = self._outbound(deal["id"], party["id"], now)
            if outbound:
                meta["sent"] = outbound
            told = history_of(self._store.backlog(deal["id"]))
            if told:
                meta["history"] = told
            _, job_id = self._relay.submit(chat, chat, "", kind=PRM, meta=self._reply_meta(self._with_note(meta, deal, now)),
                                           fresh=True)
            self._store.add_event(deal["id"], party["id"], OPENING, {"job": job_id, "how": BOSS_OPENING,
                                                                     "by": BOSS_OPENING}, actor="system", now=now)
            logger.info("PRM: party %d was introduced in boss group %s…; job #%d drafts our first line there", party["id"],
                        chat[:8], job_id)
            return job_id, ""

    def _group_opening_why(self, party: dict, chat: str, deal: dict, now: float, ignore=()) -> str:
        """Why no group 开场白 is drafted now ("" = it is; ``_group_opening``): off the list, a person has her deal, a
        draft of hers there is being written, she spoke there already, or a draft for the group waits (but ``ignore``:
        the drafts a redraft would put out of date, ``_redraft_group``)."""
        if not party.get("active"):
            return "这位达人已经停用（不在达人名单里），不写开场白"
        if self._mode(deal, now) != COPILOT:
            return "现在不在副驾驶模式（人工接管或暂停），不写开场白"
        if self._relay.store.latest_open(chat, drafts_only=True, system=False) is not None:
            return "群里她的草稿正在写"
        if any(j.get("kind") == PRM and str(j.get("text") or "").strip() and not job_meta(j).get("op")
               for j in self._relay.store.recent(chat, limit=20)):
            return "她已经在群里说过话了：她的话会自己起草，不用开场白"
        if any(p["chat_id"] == chat and p["id"] not in ignore
               for p in self._store.deal_packets(deal["id"], statuses=(PENDING, APPROVED, SENDING))):
            return "「待审批」里已经有群里的草稿"
        return ""

    def confirm_group(self, deal_id, chat, actor: str = "console") -> dict:
        """群自己接上 (review 2026-11-11 #2): the console's 「是她的群」 on a group of this deal the scan linked by itself
        (``BossGroups.confirm``): from now on its drafts go as PRM_LINKED_GROUPS_AUTO says."""
        try:
            deal = self._store.deal(int(deal_id))
        except (TypeError, ValueError):
            deal = None
        row = self._store.boss_group(str(chat or ""))
        if deal is None or row is None or int(row.get("deal_id") or 0) != int(deal["id"]):
            return {"ok": False, "error": "这个群没有接在这张合作单上"}
        out = self.boss_groups.confirm(row["chat"], actor=actor)
        if out.get("ok"):
            self._wake.set()
        return out

    def unlink_group(self, deal_id, chat="", actor: str = "console") -> dict:
        """接手任何合作群: a group linked to this deal is not drafted in any more (``deal_action`` ``group_unlink``, the
        console's 「不接了」; the admin's 「不接群 群名」, src/prm/group_link.py): ``BossGroups.stop`` (its drafts not sent
        fail, ``_close_group``).  ``chat`` empty: the deal's 三方群.  A group that is her deal's 三方群 stays it (review
        2026-11-09: 「群已经不在了」, ``group_none``, is for a group that is gone): what the link overwrote is put back
        (``three_party.was``; a group that became her 三方群 by being linked, a 老板拉的群 too, as one 「建好了」 by the
        console) and it is read only again (``ThreeParty.read_again``: its lines for her private drafts).  The deal and
        her private chat go on.  ``{"ok": True, "three_party": it stays her 三方群, "reading": it is read only now,
        "unsent": how many of its drafts were not sent, "sending": how many were being sent (``_deliver`` stops them
        before their next message)}`` or ``{"ok": False, "error"}``."""
        try:
            deal = self._store.deal(int(deal_id))
        except (TypeError, ValueError):
            deal = None
        group = dict((deal or {}).get("three_party") or {})
        row = self._store.boss_group(str(chat or group.get("chat") or ""))
        if (deal is None or row is None or row["state"] != BOSS_LINKED or int(row.get("deal_id") or 0) != deal["id"]
                or not self.boss_groups.is_linked(row["chat"])):
            return {"ok": False, "error": "这个群现在没有接手在这张合作单上"}
        mine = group.get("chat") == row["chat"] and group.get("state") == "created"
        unsent = sum(1 for p in self._store.packets((PENDING, APPROVED), limit=500) if p["chat_id"] == row["chat"])
        sending = sum(1 for p in self._store.packets((SENDING,), limit=100) if p["chat_id"] == row["chat"])
        if not self.boss_groups.stop(row["chat"], actor=actor, read_only=mine):
            return {"ok": False, "error": "这个群现在没有接手在这张合作单上"}
        if not mine:
            return {"ok": True, "three_party": False, "reading": False, "unsent": unsent, "sending": sending}
        now = self._clock()
        was = group.pop("was", None)
        was = was if isinstance(was, dict) else {}
        back = {**{k: v for k, v in group.items() if k != "why"}, **{k: was[k] for k in ("at", "why") if k in was}}
        back["by"] = was["by"] if str(was.get("by") or "") not in ("", *GROUP_TAKEN_BY) else "console"
        self._store.update_deal(deal["id"], now, three_party=back)
        reading = self.three_party.read_again(self._store.deal(deal["id"]))
        logger.info("PRM: deal %d's 三方群 is %s again (%s)", deal["id"], "read only" if reading else "not drafted in",
                    actor)
        return {"ok": True, "three_party": True, "reading": reading, "unsent": unsent, "sending": sending}

    def _hook_busy(self, party: dict) -> str:
        """老板拉的群里机器人自己选人 (BossGroups.rehook ``hook_busy``): why the member a deal hangs on cannot be changed now
        ("" = it can): a draft of that member's private chat waits, is approved or being sent, or a job of that chat
        is queued or being written (it would go to somebody the deal no longer hangs on)."""
        wxid = str(party.get("wxid") or "")
        deal = self._store.deal_of(int(party["id"]), self._clock())
        if any(p["chat_id"] == wxid for p in self._store.deal_packets(deal["id"], statuses=(PENDING, APPROVED, SENDING))):
            return "合作单现在挂着的那个人的私聊里有草稿在等：先在「待审批」里处理掉，再改挂"
        if self._relay.store.latest_open(wxid) is not None:
            return "合作单现在挂着的那个人的私聊里有一份草稿正在写：写好、处理掉以后再改挂"
        return ""

    def set_active(self, party_id: int, active: bool) -> Optional[dict]:
        party = self._store.update_party(int(party_id), active=active, now=self._clock())
        self._parties_changed()
        if active:
            self._wake.set()                           # a 专属佣金 step owed is asked for now (sweep_owed_steps)
        return party

    def _parties_changed(self) -> None:
        self._wxids = self._store.active_wxids()
        if self.on_parties_changed is not None:
            try:
                self.on_parties_changed()
            except Exception:
                logger.exception("PRM: the WeChat backend could not reload its private chats")

    def deal_action(self, action: str, data: dict, *, actor: str = "console", review: bool = False,
                    notice: bool = True, note: Optional[dict] = None, more=()) -> dict:
        """Stage / adopt / mode / shipment / shipment_status from the console (deals.py).  The 聚水潭 sync
        (``_sample_answer``) takes the same way with ``actor`` system, ``review`` (its shipping notice always waits
        for a person), ``notice`` (none for an order signed for already), ``note`` (which order, in the events) and
        ``more`` (the other packages' waybills of an order sent in several: named in the notice, never from the page).
        An operator's change of the deal's stage or shipment sees to a deleted order's notice.  ``night_done``
        (「已处理」 on a holding line the night window sent) only drops that notice; ``opening`` (「写开场白」) asks
        for one 开场白 draft (``ask_opening``); ``huopan`` (「补发货盘」) one draft that sends her the 货盘 again
        (``ask_huopan``); ``handcards`` {items?} (「做手卡」) one draft with her 直播手卡 (``ask_handcards``); ``group`` /
        ``group_try`` / ``group_made`` / ``group_none`` are 三方群's
        (``three_party``: 「拉三方群」, 「先试一下」, 「建好了」, 「没建成」); ``draft_note`` {text} keeps the deal's 起草备注
        (``set_draft_note``); ``photo_confirm`` {packet, code} / ``photo_unconfirm`` {code} are 「对过图了」 and its
        「撤销」 (PRM_DRAFT_CONTEXT: ``photo_confirm``).  ``actor`` ``ADMIN_ACTOR``: 合作单指令's (src/prm/admin_commands.py,
        the admin's typed 「做手卡 <达人>」 …): the same as the console's, the events say who."""
        if action == OPENING:
            return self.ask_opening(data.get("deal_id"), actor=actor)
        if action == HUOPAN:
            return self.ask_huopan(data.get("deal_id"), actor=actor)
        if action == HANDCARDS:
            return self.ask_handcards(data.get("deal_id"), data.get("items"), actor=actor)
        if action == DRAFT_NOTE:
            return self.set_draft_note(data.get("deal_id"), data.get("text"), actor=actor)
        if action == GROUP_UNLINK:
            return self.unlink_group(data.get("deal_id"), data.get("chat"), actor=actor)
        if action == GROUP_CONFIRM:
            return self.confirm_group(data.get("deal_id"), data.get("chat"), actor=actor)
        if action == PHOTO_CONFIRM:
            return self.photo_confirm(data.get("deal_id"), data.get("packet"), data.get("code"), actor=actor)
        if action == PHOTO_UNCONFIRM:
            return self.photo_unconfirm(data.get("deal_id"), data.get("code"), actor=actor)
        if action in GROUP_ACTIONS:
            deal_id = data.get("deal_id")
            if action in ("group", "group_try"):
                return self.three_party.start(deal_id, dry_run=action == "group_try", actor=actor)
            try:
                before = self._store.deal(int(deal_id)) if action == "group_none" else None
            except (TypeError, ValueError):
                before = None
            result = self.three_party.mark(deal_id, made=action == "group_made", actor=actor)
            if result.get("ok") and before is not None:
                self.boss_groups.gone(before, actor=actor)     # 老板拉的群: not read any more, her lines there silent
            return result
        now = self._clock()
        if action in (NIGHT_DONE, GROUP_LEFT_DONE):
            return apply_deal_action(self._store, action, data, now=now, takeover_sec=self._takeover_sec, actor=actor)
        try:
            before = self._store.deal(int(data.get("deal_id")))
        except (TypeError, ValueError):
            before = None
        result = apply_deal_action(self._store, action, data, now=now, takeover_sec=self._takeover_sec, actor=actor,
                                   review=review, notice=notice, note=note, more=more)
        if result.get("ok") and action == "shipment" and result.get("packet"):
            self._aim(self._store.packet(int(result["packet"])))   # 老板拉的群: the shipping notice goes where she is
        if result.get("ok"):
            self._wake.set()
            if actor in OPERATOR_ACTORS and action != "mode" and before:
                self._drop_dead_samples(before["id"], now)
            if actor in OPERATOR_ACTORS and action == "shipment" and before:      # the waybill 聚水潭 did not give: typed
                self._store.drop_notices(before["id"], lambda n: n["key"].startswith(SAMPLE_ASK), now)
            after = self._store.deal(before["id"]) if before else None
            if after and after.get("delivered_at"):
                # 已签收: the 专属佣金 chain of this signature (once); marked 已签收 again, a chain that never
                # started is started now (mac review F2)
                again = after["stage"] == "delivered" and action in ("stage", "adopt", "shipment_status")
                if not self._examine_delivery(after, now, again=again):
                    self._chain_refresh(after["id"], now)     # (moved on past the sample, say)
            elif after and before.get("delivered_at") and not after.get("delivered_at"):
                # set back before 已签收 (to sign for it again, kb 2.1): what waited for the signature is not
                # owed now, and a step that was not written is picked up by signing for it again
                self._store.drop_notices(after["id"], lambda n: (n["key"] == "step" and n.get("delivered")) or (
                    str(n["key"]).startswith("failed:") and n.get("lane") == LANE_SYSTEM), now)
                self._chain_refresh(after["id"], now)
        return result

    def _examine_delivery(self, deal: dict, now: float, again: bool = False) -> bool:
        """A deal marked 已签收 (``delivered_at``): looked at once per signature (``deal.chain.seen``), in the
        console's ``deal_action`` or, for one marked straight into the database while the bot was stopped,
        by ``sweep_deliveries`` (mac review F2).  A signature that came after the deal's newest verified sample
        order (``_chain_order``) starts a round of that order's chain (``chain.order``, ``chain.since`` = the
        signature; what the signature before left is put away) and its first product not open is asked for
        (``_chain_next``), unless a bot from before ``deal.chain`` started it already (``_chain_started``).
        With PRM_COMMISSION_AFTER=created the order's verification started a round already (``_start_chain``);
        a signature starts a new one too (mac review F3: 运输中 → 已签收 did nothing in this mode), but only
        for a round this bot started for that order (a deal of a bot from before is only looked at).  ``again``: marked 已签收 once more, the signature known already: the
        round goes on if it can (a step that never went out, say).  True when this call started a round."""
        delivered = float(deal.get("delivered_at") or 0)
        if not delivered or deal.get("purged_at"):
            return False
        with self._chain_lock:
            deal = self._store.deal(deal["id"]) or deal
            chain = dict(deal.get("chain") or {})
            if chain.get("seen") is not None and delivered <= float(chain["seen"]):
                if again:
                    self._chain_go(deal, now, after=self._store.packet(int(chain.get("order") or 0)))
                return False
            before = "seen" not in chain          # never looked at: a deal of a bot from before deal.chain
            order = self._chain_order(deal["id"])
            chain["seen"] = delivered
            start = order is not None and _order_at(order) <= delivered and (
                self._commission_after == "delivered"
                or (chain.get("since") is not None and chain.get("order") == order["id"]))
            legacy = False
            if start:
                chain.update(order=order["id"], since=delivered)
                # set back and signed for again while the bot was stopped: the signature before's step is not owed
                self._store.drop_notices(deal["id"], lambda n: float(n.get("at") or 0) < delivered and (
                    (n["key"] == "step" and n.get("delivered")) or (
                        str(n["key"]).startswith("failed:") and n.get("lane") == LANE_SYSTEM)), now)
                legacy = before and self._chain_started(self._store.deal(deal["id"]) or deal, delivered)
            self._store.update_deal(deal["id"], now, chain=chain)
            deal = self._store.deal(deal["id"]) or deal
            if start and not legacy:
                self._chain_go(deal, now, after=order)
            elif start:
                self._chain_refresh(deal["id"], now)
            return start

    def _chain_started(self, deal: dict, since: float) -> bool:
        """Did a bot from before ``deal.chain`` start the chain of this signature (``since``)?  A step owed
        (``step``), its job queued or running, asked for (``next_step``), drafted (a draft of the system lane,
        a 专属佣金 draft) or not written (the system lane's ``failed:``) since then: its round is not asked
        for again (it goes on from what comes next)."""
        for n in deal.get("notices") or []:
            if n["key"] == "step" or (str(n["key"]).startswith("failed:") and n.get("lane") == LANE_SYSTEM
                                      and float(n.get("at") or 0) > since):
                return True
        if self._store.events(deal["id"], limit=1, kinds=("next_step",), since=since):
            return True                                # (asked in the same step as the signature: at it)
        party = self._store.party(deal["party_id"]) or {}
        if party.get("wxid") and self._relay.store.latest_open(party["wxid"], drafts_only=True, system=True):
            return True
        # (a draft comes back after its step was asked: one of the signature's own time is of the one before)
        return any(float(p["created_at"]) > since and (
            p.get("tool") in COMMISSION_ACTIONS.values() or (p.get("meta") or {}).get("lane") == LANE_SYSTEM)
            for p in self._store.deal_packets(deal["id"], since=since))

    def _start_chain(self, order: dict, now: float) -> None:
        """PRM_COMMISSION_AFTER=created: a sample order verified starts a round of its chain now."""
        with self._chain_lock:
            deal = self._store.deal(order["deal_id"])
            if deal is None:
                return
            self._store.update_deal(deal["id"], now, chain={**(deal.get("chain") or {}), "order": order["id"],
                                                            "since": now})
            self._chain_next(self._store.deal(deal["id"]) or deal, now, after=order)

    def _chain(self, deal: dict) -> Optional[dict]:
        """Where the deal's 专属佣金 chain stands, as the bot keeps it (mac review F5: before, only the Mac's case
        file knew, and a product that could not be opened stopped the ones after it).  The round is
        ``deal.chain``: the verified sample order it follows and since when (a signature; with
        PRM_COMMISSION_AFTER=created the order's verification).  ``items``: each product of the order, in its
        order, with its state (``item_state``) and the draft that says so; ``busy``: a step of it is under
        way (a product running or in 待审批, a step owed, its job queued or running, a step's draft waiting);
        ``next``: the product to ask for now (not busy: the first one not asked for or decided on in this
        round) and ``next_action`` what for.  ``links``: a 抖店 order of a deal that asks for 专属链接
        (``link_wish``): each item has its ``link`` state too (``LINK_STATES``, of its dy.channel_goods drafts) and
        ``link_packet``, and a product's link is the step right after its 专属佣金 opened (``waits`` until then).
        None: no round, or its order is not a 专属佣金 one (no ``COMMISSION_ACTIONS`` shop)."""
        chain = deal.get("chain") or {}
        if chain.get("since") is None or not chain.get("order"):
            return None
        order = self._store.packet(int(chain["order"]))
        if order is None or order["deal_id"] != deal["id"] or not _verified(order):
            return None
        action = (order["platform"] or {}).get("action") or {}
        shop = str(action.get("shop") or "")
        name = COMMISSION_ACTIONS.get(shop)
        ids = list(dict.fromkeys(order_items(action)))
        if not name or not ids:
            return None
        since = float(chain["since"])
        packets = self._store.deal_packets(deal["id"], tools=(name,))
        # (a step whose draft the Mac could not make, not able to ask this bot what it knows, was not asked: it is owed
        # and asked again, ``_unasked``; review 2026-11-09 N1)
        asks = self._step_asks(deal["id"], since)
        asked = {str(ev["detail"].get("item")) for ev in asks if ev["detail"].get("item")
                 and ev["detail"].get("action", name) in ("", name)}
        # 专属链接 (a 抖店 order, the deal asking for links): each product's link, after its 专属佣金
        links = link_wish(deal.get("slots")) if name == COMMISSION_ACTIONS["dy"] else None
        link_packets = self._store.deal_packets(deal["id"], tools=(LINK_ACTION,)) if links else []
        # (a link step that was asked without a prefix, for her to be asked one, is asked again once the deal has one:
        # review 2026-11-09 #3; one whose draft was dropped at a prefix too long, once the deal has another that fits
        # and it was not asked with that one yet: F1)
        prefix = (links or {}).get("prefix", "")
        link_asks = [ev["detail"] for ev in asks if ev["detail"].get("item") and ev["detail"].get("action") == LINK_ACTION]
        linked = {str(a.get("item")) for a in link_asks if a.get("prefix") or not prefix}
        relinked = {str(a.get("item")) for a in link_asks if a.get("prefix") == prefix}
        items = []
        for item in ids:
            mine = [p for p in packets if action_target((p["platform"] or {}).get("action") or {}, p["platform"])[0] == item]
            state, packet = item_state(mine, item in asked, since)
            entry = {"id": item, "state": state, "packet": packet}
            if links:
                theirs = [p for p in link_packets
                          if action_target((p["platform"] or {}).get("action") or {}, p["platform"])[0] == item]
                link, link_packet = item_state(theirs, item in linked, since)
                if link == "dropped" and item not in relinked and link_again(link_packet, prefix):
                    # dropped while stopped at a prefix too long: one fits now (F1)
                    link, link_packet = "todo", next((p for p in theirs if platform_state(p) == "uncertain"), None)
                if link == "todo" and state not in ("opened", "covered"):
                    link = "waits"                     # (binding her needs its 专属佣金 open)
                entry.update(link=link, link_packet=link_packet)
            items.append(entry)
        party = self._store.party(deal["party_id"]) or {}
        job = self._step_job(party)
        drafting = None
        if job is not None:
            ask = next((ev["detail"] for ev in asks if ev["detail"].get("job") == job["id"]), {})
            drafting = {"job": job["id"], "item": str(ask.get("item") or ""), "action": str(ask.get("action") or "")}
            for i in items:
                if i["id"] != drafting["item"]:
                    continue
                if drafting["action"] == LINK_ACTION:
                    if i.get("link") == "asked":
                        i["link"] = "drafting"         # (its step's draft is being written)
                elif i["state"] == "asked":
                    i["state"] = "drafting"
        busy = ""
        if any(i["state"] in ("running", "waiting") or i.get("link") in ("running", "waiting") for i in items):
            busy = "held"
        elif any(n["key"] == "step" for n in deal.get("notices") or []):
            busy = "owed"
        elif job is not None:
            busy = "drafting"
        elif any((p.get("meta") or {}).get("lane") == LANE_SYSTEM
                 for p in self._store.deal_packets(deal["id"], statuses=(PENDING,))):
            busy = "draft"
        created = ((order["platform"] or {}).get("execution") or {}).get("created") or {}
        # the next step: the first product (in the order's order) whose 专属佣金 is left, or whose link is left after
        # its 专属佣金 opened (the link is the step right after it)
        nxt, nxt_action = None, ""
        for i in items if not busy else ():
            if i["state"] == "todo":
                nxt, nxt_action = i["id"], name
                break
            if links and i["state"] in ("opened", "covered") and i.get("link") == "todo":
                nxt, nxt_action = i["id"], LINK_ACTION
                break
        return {"order": order, "shop": shop, "action": name, "items": items, "since": since, "busy": busy,
                "drafting": drafting, "number": str(created.get("sample_order_no") or ""),
                "uid": str(created.get("creator_uid") or "") if shop == "dy" else "",
                "links": links, "next": nxt, "next_action": nxt_action}

    def _chain_next(self, deal: dict, now: float, after: Optional[dict] = None, why: str = "") -> bool:
        """The 专属佣金 chain goes on (mac review F5), under ``_chain_lock``: unless a step of it is under way
        (``_chain``'s ``busy``), the round's next product is asked for (``_ask_next_step``: owed while a person
        has the deal or the creator is off the list), whatever became of the one before: opened, dropped
        (「不发」), red, nothing drafted; each product once per round.  A deal that asks for 专属链接: a product's
        link is asked for right after its 专属佣金 opened, then the next product's 专属佣金 (``_chain``).  A round
        with nothing left asks once more only right after a 专属佣金 (or, with links, a link) of it was verified
        (``after``), so the case file hears it is done (kb 2.1: no action then).  With
        PRM_COMMISSION_AFTER=delivered a deal set back before 已签收 asks for nothing (the next signature starts a
        new round).  The deal's ``chain`` notice says where it stands.  The step's note starts with what just happened
        (``after``; the order itself: it was made or signed for), else with ``why`` (her prefix came, say: review
        2026-11-09 F7), never with a signature that is not why; a link step carries the UID of the round's order
        (``meta.step.uid``: its draft binds that one, F4).
        False: the deal has no chain (the caller's own way then)."""
        with self._chain_lock:
            deal = self._store.deal(deal["id"]) or deal
            progress = self._chain(deal)
            if progress is None:
                return False
            delivered = self._commission_after == "delivered"
            done = False
            if after is not None:
                after = self._store.packet(after["id"]) or after
                theirs = (after.get("platform") or {}).get("action") or {}
                tools = (progress["action"], LINK_ACTION) if progress.get("links") else (progress["action"],)
                done = (theirs.get("action") in tools and (_verified(after) or _covered(after) is not None)
                        and str(theirs.get("item_id") or "") in {i["id"] for i in progress["items"]})
            if not progress["busy"] and (progress["next"] or done) and not (delivered and not deal.get("delivered_at")):
                head = after if after is not None else progress["order"]
                # (a round of a signature says so: with PRM_COMMISSION_AFTER=created one may start at the order)
                signed = bool(deal.get("delivered_at")) and progress["since"] == float(deal["delivered_at"])
                link = progress.get("next_action") == LINK_ACTION
                self._ask_next_step(head, chain_note(progress, after, delivered=signed, why=why), now,
                                    delivered=delivered, item=progress["next"] or "", action=LINK_ACTION if link else "",
                                    prefix=(progress.get("links") or {}).get("prefix", "") if link else "",
                                    uid=progress["uid"] if link else "")
                deal = self._store.deal(deal["id"]) or deal
                progress = self._chain(deal) or progress
            self._chain_notice(deal, progress, now)
            return True

    def _chain_go(self, deal: dict, now: float, after: Optional[dict] = None, why: str = "") -> None:
        """``_chain_next``; a deal without a chain: its 专属链接 left (``_links_without_chain``: a step decided on, a step
        that drafted nothing, frees it to ask the next; review 2026-11-09 F2).  Failures are logged (what called it is
        done all the same: the console's action, a draft that came back)."""
        try:
            if not self._chain_next(deal, now, after=after, why=why):
                self._links_without_chain(deal, now, why=why)
        except Exception:
            logger.exception("PRM: the 专属佣金 chain of deal %d could not go on", deal["id"])

    def _chain_refresh(self, deal_id: int, now: float) -> None:
        """The deal's ``chain`` notice, again (a draft of the chain came, a red one was dealt with …)."""
        try:
            with self._chain_lock:
                deal = self._store.deal(deal_id)
                if deal is not None:
                    self._chain_notice(deal, self._chain(deal), now)
        except Exception:
            logger.exception("PRM: the 专属佣金 notice of deal %d could not be written", deal_id)

    def _chain_notice(self, deal: dict, progress: Optional[dict], now: float) -> None:
        """The deal's ``chain`` notice (``chain_notice_text``): there while the round has something under way, a
        product left to ask for or asked for with nothing drafted, or a red one nobody marked dealt with; gone
        when every product is open or the rest was decided by a person (「不发」, 「我已处理」), when
        (PRM_COMMISSION_AFTER=delivered) the deal was set back before 已签收, and, for what is left or drafted
        nothing, once the deal moved on past the sample (``MOVED_ON``: the operators are done with it)."""
        text = ""
        if progress is not None and not (self._commission_after == "delivered" and not deal.get("delivered_at")):
            hint = RESTART_HINT if self._commission_after == "delivered" else RESTART_HINT_CREATED
            text = chain_notice_text(progress, hint, moved_on=deal.get("stage") in MOVED_ON)
        self._store.set_notice(deal["id"], "chain", text, now)

    def _chain_decided(self, packet: dict, now: float) -> None:
        """An operator decided on a draft of the chain (a 专属佣金 or 专属链接 draft, or a step's draft without the
        action): the chain goes on (「不发」: that product is not opened, or its link not made, in this round)."""
        if packet.get("tool") in CHAIN_TOOLS or (packet.get("meta") or {}).get("lane") == LANE_SYSTEM:
            deal = self._store.deal(packet["deal_id"])
            if deal is not None:
                self._chain_go(deal, now, after=packet)

    def _links_wished(self, deal: dict, now: float) -> None:
        """A draft just recorded that the deal asks for 专属链接, or another prefix (``LINK_SLOT``): the chain goes on with
        the links (a link step asked without that prefix is asked again now: ``_chain``); a deal without a chain (no
        verified sample order) gets the step of its first product whose 专属佣金 is open and whose link is left
        (``_links_without_chain``; review 2026-11-09 #3).  The step's note says why it comes now.  Failures are logged:
        the draft is recorded already."""
        links = link_wish((self._store.deal(deal["id"]) or deal).get("slots")) or {}
        why = (f"合作单刚记下她要的专属链接前缀「{links['prefix']}」。" if links.get("prefix") else
               f"合作单刚记下她要专属链接（{links.get('said', '')}）。")
        self._chain_go(deal, now, why=why)

    def _link_slots(self, deal: dict, slots: dict, now: float) -> dict:
        """A draft's slots as they go into the deal: a 「专属链接前缀」 (``LINK_SLOT``) that says neither yes nor no
        (``link_reading``: 「好的」「可以」「是」「要不要都行」…) is left out, the deal keeps what it had and says so
        (``LINKS_UNCLEAR_NOTICE``: review 2026-11-09 N3, before such a value quietly stopped the links); a clear one
        puts that notice away."""
        if LINK_SLOT not in (slots or {}):
            return slots
        said = " ".join(str(slots.get(LINK_SLOT) or "").split())
        if link_reading(said) != "unclear":
            self._store.set_notice(deal["id"], LINKS_UNCLEAR_NOTICE, "", now)
            return slots
        before = " ".join(str((deal.get("slots") or {}).get(LINK_SLOT) or "").split())
        self._store.set_notice(deal["id"], LINKS_UNCLEAR_NOTICE,
                               f"专属链接那一栏写的「{said[:40]}」看不出要不要，先按原来的办"
                               + (f"（原来写的是「{before[:40]}」）" if before else "（原来没说要链接）"), now)
        logger.info("PRM: deal %d: a draft's 专属链接 slot said neither yes nor no; the deal keeps its own", deal["id"])
        return {k: v for k, v in slots.items() if k != LINK_SLOT}

    def _unasked(self, job: dict, meta: dict, deal: dict, draft, notes: list, now: float) -> bool:
        """The Mac left a 专属链接 out of this draft, not able to ask this bot what it knows (``PLATFORM_UNASKED``:
        GET /relay/health did not answer, asked again and again; review 2026-11-09 N1).  A step (the system lane) is not
        counted as drafted: it is owed again and asked a little later (``STEP_RETRY_SEC``), ``STEP_UNASKED_TRIES``
        times at most, then it waits for a person like a step that was not written (``_draft_failed``).  Her reply with
        nothing left of it: her lines wait for her next draft, as when a draft was not written.  True when it was dealt
        with here (no packet is made); False: a reply that still says something goes on as a draft (flagged)."""
        step = meta.get("step") if isinstance(meta.get("step"), dict) else {}
        if not meta.get("system_note"):
            if draft.reply_draft:
                return False
            self._draft_failed(job, meta, f"#{job['id']} 草稿没写成：Mac 问不到 Windows 上的机器人，专属链接没起草"
                                          "（她的消息等下一份草稿一起回）", notes, now)
            return True
        if any(int(ev["detail"].get("job") or 0) == int(job["id"])
               for ev in self._store.events(deal["id"], limit=50, kinds=("step_unasked",))):
            return True                                # (the same answer delivered twice)
        tries = int(step.get("tries") or 0) + 1
        self._store.add_event(deal["id"], int(meta["party_id"]), "step_unasked",
                              {"job": job["id"], "item": str(step.get("item") or ""), "tries": tries,
                               **({"gave_up": True} if tries > STEP_UNASKED_TRIES else {})}, actor="mac", now=now)
        if tries > STEP_UNASKED_TRIES:
            self._draft_failed(job, meta, f"#{job['id']} 草稿没写成：Mac 连着 {STEP_UNASKED_TRIES} 次问不到 Windows 上的机器人",
                               notes, now)
        else:
            self._owe_step(deal, f"#{job['id']} {step_word(meta)}这一步没起草成", str(meta["system_note"]),
                           step.get("packet"), bool(step.get("delivered")), now, item=str(step.get("item") or ""),
                           action=str(step.get("action") or ""), prefix=str(step.get("prefix") or ""),
                           uid=str(step.get("uid") or ""), retry_at=now + STEP_RETRY_SEC, tries=tries,
                           why=f"Mac 问不到 Windows 上的机器人（网络不通？），{STEP_RETRY_SEC // 60} 分钟后系统再派一次")
        logger.warning("PRM: job #%d: the Mac could not ask the bot what it knows; the step is %s", job["id"],
                       "given up on" if tries > STEP_UNASKED_TRIES else "asked again later")
        self._chain_refresh(deal["id"], now)
        return True

    def _links_after_slots(self, deal: dict, before: Optional[dict], wish: Optional[dict], now: float) -> None:
        """A draft's slots were merged into the deal (a WeChat draft, or a 百应 one: review 2026-11-09 F2): links asked for
        for the first time, or with another prefix, go on (``_links_wished``); no longer asked for, the deal's
        ``LINKS_NOTICE`` goes."""
        if wish and (not before or (wish["prefix"], wish.get("long")) != (before["prefix"], before.get("long"))):
            self._links_wished(deal, now)
        elif before and not wish:
            self._store.set_notice(deal["id"], LINKS_NOTICE, "", now)
            self._chain_refresh(deal["id"], now)

    def _links_without_chain(self, deal: dict, now: float, why: str = "") -> bool:
        """A deal without a sample order's chain that asks for 专属链接: the first of its products whose link is left
        (``_links_left``) is asked for, the way the step after its 专属佣金 is (``next_step_note``, ``why`` first), when
        the deal has a prefix that fits and no step of it is under way (``_links_busy``); the deal's ``LINKS_NOTICE``
        says which products' links are left and why they wait ("" when none is, or the deal moved on past the sample:
        ``MOVED_ON``).  Asked again whenever the deal can go on (``_chain_go``: a step decided on or drafted nothing, a
        system draft came back, a wish recorded; review 2026-11-09 F2).  True when a step was asked for."""
        deal = self._store.deal(deal["id"]) or deal
        links = link_wish(deal.get("slots"))
        if links is None or self._chain(deal) is not None:   # (a chain's own notice says where its links stand)
            self._store.set_notice(deal["id"], LINKS_NOTICE, "", now)
            return False
        left = self._links_left(deal, links)
        asked = False
        if left and links.get("prefix") and not self._links_busy(deal):
            commission, item, action = left.pop(0)
            record = (commission["platform"] or {}).get("execution") or {}
            self._ask_next_step(commission, next_step_note(action, record, links=links, why=why), now, item=item,
                                action=LINK_ACTION, prefix=links["prefix"])
            asked = True
            deal = self._store.deal(deal["id"]) or deal
        text = "" if deal.get("stage") in MOVED_ON else links_notice_text(links, [i for _, i, _ in left],
                                                                          bool(self._links_busy(deal)))
        self._store.set_notice(deal["id"], LINKS_NOTICE, text, now)
        return asked

    def _links_quietly(self, deal: dict, now: float) -> None:
        """``_links_without_chain``, its failure logged (a draft that came back is recorded already)."""
        try:
            self._links_without_chain(deal, now)
        except Exception:
            logger.exception("PRM: the 专属链接 of deal %d could not be looked at", deal["id"])

    def _links_left(self, deal: dict, links: dict) -> list:
        """The products of a deal without a chain whose 专属链接 is left: ``[(its 专属佣金 packet, item, action)]`` in the
        order the 专属佣金 were made, each one open for her (verified, or the invite card's), its link without a draft
        that counts (one only out of date does not), and not asked for already: with a prefix (any: once), or, while
        the deal knows none, at all (one asked without a prefix is asked again once it has one: review 2026-11-09 #3).
        One whose link draft was dropped at a prefix too long is left again once the deal has another that fits
        (``link_again``), until it is asked with that one (F1)."""
        prefix = links.get("prefix", "")
        made = [(*action_target((p["platform"] or {}).get("action") or {}, p["platform"]), p)
                for p in self._store.deal_packets(deal["id"], tools=(LINK_ACTION,)) if p["status"] != SUPERSEDED]
        asks = [ev["detail"] for ev in self._step_asks(deal["id"]) if ev["detail"].get("action") == LINK_ACTION]
        left, seen = [], set()
        for commission in reversed(self._store.deal_packets(deal["id"], tools=(COMMISSION_ACTIONS["dy"],))):
            if not (_verified(commission) or _covered(commission) is not None):
                continue
            action = (commission["platform"] or {}).get("action") or {}
            item, creators = action_target(action, commission["platform"])
            if item in seen:
                continue
            seen.add(item)
            theirs = [p for their_item, them, p in made if their_item == item and creators & them]
            if any(not link_again(p, prefix) for p in theirs):
                continue                               # made, running, waiting, or decided on by a person
            mine = [a for a in asks if str(a.get("item")) == item]
            if any(a.get("prefix") == prefix for a in mine):
                continue                               # asked for with the deal's prefix (or, none known, without)
            if not theirs and any(a.get("prefix") for a in mine):
                continue                               # asked for with a prefix already: once
            left.append((commission, item, action))
        return left

    def _step_asks(self, deal_id: int, since: Optional[float] = None) -> list:
        """The deal's ``next_step`` events (since ``since``) but those whose job's draft the Mac could not make, not
        able to ask this bot what it knows (``step_unasked``: the step is owed and asked again; review 2026-11-09 N1).
        The one it gave up on (``gave_up``: after ``STEP_UNASKED_TRIES``) counts: the product stays asked until an
        operator acts (a new round), never dispatched afresh by the chain's next move (M1)."""
        unasked = {int(ev["detail"].get("job") or 0)
                   for ev in self._store.events(deal_id, limit=None, kinds=("step_unasked",), since=since)
                   if not ev["detail"].get("gave_up")}
        return [ev for ev in self._store.events(deal_id, limit=None, kinds=("next_step",), since=since)
                if int(ev["detail"].get("job") or 0) not in unasked]

    def _links_busy(self, deal: dict) -> str:
        """Why no step of a deal without a chain can be asked for now ("" = it can): its system job is queued or
        running (``drafting``), a step is owed (``owed``), a step's draft waits in 待审批 (``draft``)."""
        party = self._store.party(deal["party_id"]) or {}
        if any(n["key"] == "step" for n in deal.get("notices") or []):
            return "owed" if not self._step_job(party) else "drafting"
        return self._step_under_way(deal, party)

    def _step_under_way(self, deal: dict, party: dict) -> str:
        """A step of the deal under way: its system job queued or running (``drafting``), or its draft waiting in 待审批
        (``draft``); "" when neither.  Another step asked now would put that one out of date (the system lane keeps one
        draft): it waits (``sweep_owed_steps``, ``_after_platform``; review 2026-11-09 N2)."""
        if self._step_job(party):
            return "drafting"
        if any((p.get("meta") or {}).get("lane") == LANE_SYSTEM
               for p in self._store.deal_packets(deal["id"], statuses=(PENDING,))):
            return "draft"
        return ""

    def _step_job(self, party: dict) -> Optional[dict]:
        """Her open system-lane job (a step being drafted): in her private chat, or in a linked group she is still in,
        where her steps go (``_reaching``: a creator who is only in a 老板拉的群; review 2026-11-09 N2: only her wxid was
        looked at; PRM_REACH_LAST_SPOKE: the group where her side last spoke, which may not be the newest)."""
        if not party:
            return None
        chats = [party.get("wxid")]
        try:                                           # (PRM_REACH_LAST_SPOKE: any linked group she is still in)
            chats += [row["chat"] for row in self.boss_groups.reach_groups(party["id"])]
        except Exception:                              # (looking for a job never stops the chain)
            logger.exception("PRM: where party %s's steps go could not be told", party.get("id"))
        for chat in dict.fromkeys(c for c in chats if c):
            job = self._relay.store.latest_open(chat, drafts_only=True, system=True)
            if job is not None:
                return job
        return None

    def _after_platform(self, packet_id: int, record: dict, now: float, late: bool = False) -> None:
        """What a platform action's result means for the 专属佣金 chain: a sample order verified with
        PRM_COMMISSION_AFTER=created starts a round of its chain; a 专属佣金's result (open, red, late) lets the
        chain go on; for a deal without a chain (no verified sample order) a verified 专属佣金 asks for the next
        as before (the Mac picks it from the case file).  Failures are logged: the result is recorded already."""
        packet = self._store.packet(packet_id)
        if packet is None:
            return
        action = (packet["platform"] or {}).get("action") or {}
        name = action.get("action")
        try:
            if name == "jst.sample_order":
                if not late and self._commission_after == "created" and written_state(record) == "verified":
                    self._start_chain(packet, now)
            elif name in CHAIN_TOOLS:
                deal = self._store.deal(packet["deal_id"])
                if deal is None:
                    return
                links = link_wish(deal.get("slots")) if name in (LINK_ACTION, COMMISSION_ACTIONS["dy"]) else None
                if name == LINK_ACTION and not links:
                    return                             # a link nobody asked the chain for: its reply carries it
                if self._chain_next(deal, now, after=None if late else packet):
                    return
                if not late and packet["status"] == APPROVED:
                    # no chain (no verified sample order): the next step as before, the Mac picks it from the case
                    # file; a 专属佣金 of a deal asking for links is followed by that product's link.  Another step of
                    # hers under way (drafted, in 待审批) is not put out of date: this one is owed until it is done; one
                    # owed already keeps its place (her links left are asked for later: ``_links_without_chain``;
                    # review 2026-11-09 N2)
                    item = str(action.get("item_id") or "")
                    linked = bool(links) and name != LINK_ACTION and self._link_made(deal["id"], item)
                    link = bool(links) and name != LINK_ACTION and not linked
                    note = next_step_note(action, record, links=links, linked=linked)
                    party = self._store.party(deal["party_id"]) or {}
                    under_way = self._step_under_way(deal, party)
                    kw = dict(item=item if link else "", action=LINK_ACTION if link else "",
                              prefix=links.get("prefix", "") if link else "")
                    if any(n["key"] == "step" for n in deal.get("notices") or []):
                        # (that one tells the Mac this is done too: it does not draft it again, review M3)
                        self._owed_also(deal, action, now)
                        logger.info("PRM: deal %d: the step after packet %d is not asked for (another is owed)",
                                    deal["id"], packet["id"])
                    elif under_way and note:
                        self._owe_step(deal, f"#{packet['id']} 之后该{'建专属链接' if link else '开专属佣金'}了", note,
                                       packet["id"], False, now,
                                       why={"drafting": "手头还有一步在起草，那一步交回来系统就派",
                                            "draft": "手头还有一步的草稿在待审批，处理掉系统就派"}[under_way], **kw)
                    else:
                        self._ask_next_step(packet, note, now, **kw)
                if links:
                    self._links_without_chain(deal, now)   # (what is left, and the deal's notice of it)
        except Exception:
            logger.exception("PRM: the step after packet %d could not be asked for", packet_id)

    def _owed_also(self, deal: dict, action: dict, now: float) -> None:
        """A step after a verified 专属佣金 (or 专属链接) not asked for because another is owed (``_after_platform``): the
        owed one's note says this one is done too, so the Mac does not draft it again (review 2026-11-09 M3)."""
        owed = next((n for n in deal.get("notices") or [] if n["key"] == "step"), None)
        if owed is None or not owed.get("note"):
            return
        item = str(action.get("item_id") or "")
        if action.get("action") == LINK_ACTION:
            also = f"另外，商品 {item} 的专属链接也已经建好、绑好达人了，不要再建。"
        else:
            also = (f"另外，商品 {item} 的专属佣金（{action.get('commission_rate')}%）也已经在平台上开好了"
                    f"（达人 {action.get('creator_platform_id')}），开过的不要再开。")
        if also not in str(owed["note"]):
            self._store.set_notice(deal["id"], "step", owed["text"], now, note=str(owed["note"]) + also)

    def _plain_next_step(self, owed: dict) -> str:
        """The note of the step an owed 专属链接 step's packet asks for without links (``next_step_note``): after a
        verified 专属佣金, the next product's; "" when nothing follows (after a link, say)."""
        packet = self._store.packet(int(owed.get("packet") or 0)) if str(owed.get("packet") or "").isdigit() else None
        if packet is None:
            return ""
        data = packet.get("platform") or {}
        return next_step_note(data.get("action") or {}, data.get("execution") or {})

    def _link_made(self, deal_id: int, item: str) -> bool:
        """Is this product's 专属链接 made for the deal's creator (a dy.channel_goods draft of it verified, or running)?"""
        return any(action_target((p["platform"] or {}).get("action") or {}, p["platform"])[0] == item
                   and platform_state(p) in ("verified", "running")
                   for p in self._store.deal_packets(deal_id, tools=(LINK_ACTION,)))

    def _link_leftovers(self, packet: dict) -> list:
        """The channel products earlier drafts of this 专属链接 (the deal's, the same product and creator) made and did
        not bind her (``leftover_of``): the Mac makes no other while one of them is still there unbound (review
        2026-11-09 F3; its binding check, filtered on her UID, cannot see them)."""
        data = packet["platform"] or {}
        item, creators = action_target(data.get("action") or {}, data)
        out = []
        for other in self._store.deal_packets(packet["deal_id"], tools=(LINK_ACTION,)):
            theirs = other["platform"] or {}
            their_item, their_creators = action_target(theirs.get("action") or {}, theirs)
            channel = leftover_of(other)
            if other["id"] != packet["id"] and their_item == item and creators & their_creators and channel \
                    and channel not in out:
                out.append(channel)
        return out[:5]

    def _link_doubts(self, deal_id: int, action: dict, chain_uid: str = "") -> str:
        """What a person must look at in a 专属链接 draft before approving it ("" when nothing; review 2026-11-09 #2,
        F4): a step of the chain (``chain_uid``: its round's sample order's UID) that binds another UID (refused at
        approval too, ``_approve_execute``); any other draft whose UID is none of her verified 抖店 sample orders'
        (``sample_creator_uids``: she may have two 抖音 accounts), or the bot knows none of hers (``uid_doubt_text``);
        and the bot never opened (nor saw the invite card open) this product's 专属佣金 for that UID (binding needs her
        in its strategy: one opened by hand in 百应 is fine).  Creators are compared the way ``_opened_before`` does
        (``action_target``)."""
        item, creators = action_target(action)
        uid = str(action.get("creator_platform_id") or "")
        if chain_uid:
            out = [wrong_uid_text(uid, chain_uid)] if chain_uid != uid else []
        else:
            known, orders = sample_creator_uids(self._store, deal_id)
            out = [] if uid in known else [uid_doubt_text(uid, known, orders)]
        for p in self._store.deal_packets(deal_id, tools=(COMMISSION_ACTIONS["dy"],)):
            their_item, theirs = action_target((p["platform"] or {}).get("action") or {}, p["platform"])
            if (their_item == item and creators & theirs
                    and (platform_state(p) in ("verified", "running") or _covered(p) is not None)):
                break
        else:
            out.append(f"商品 {item} 在系统里还没给 UID {uid} 开过专属佣金：绑定要她已经在这个商品的指定达人佣金里"
                       "（在百应里手动开过就不要紧），不然渠道品建好了也绑不上她")
        return "；".join(out)

    def sweep_deliveries(self) -> int:
        """The deals marked 已签收 the bot has not looked at (``deal.chain.seen``): marked straight into the
        database while it was stopped (the console then has no service: ``apply_deal_action``; mac review
        F2), or by a bot from before ``deal.chain``.  Looked at as if marked now (``_examine_delivery``):
        at start and every round of the sender thread.  Returns how many chains it started."""
        started = 0
        now = self._clock()
        for deal in self._store.delivered_deals():
            seen = (deal.get("chain") or {}).get("seen")
            if seen is not None and float(deal["delivered_at"]) <= float(seen):
                continue
            try:
                started += int(self._examine_delivery(deal, now))
            except Exception:
                logger.exception("PRM: deal %d's signature could not be looked at", deal["id"])
        return started

    def _chain_order(self, deal_id: int) -> Optional[dict]:
        """The deal's newest sample order verified on the platform, the one its 专属佣金 follow: found by the
        orders' own execution records, every one of them however much was said since (mac review F9: only
        the newest 200 events were looked at, and a takeover's chat pushed the order's out).  An order whose
        verification came late (given up on, then the Mac's answer) counts too: it is in 聚水潭."""
        for packet in self._store.deal_packets(deal_id, tools=("jst.sample_order",),
                                               statuses=(APPROVED, SENDING, SENT, FAILED)):     # newest first
            if ((packet["platform"] or {}).get("execution") or {}).get("outcome") == platforms.VERIFIED:
                return packet
        return None

    def reveal(self, party_id: int, actor: str = "console") -> dict:
        return reveal_numbers(self._store, int(party_id), now=self._clock(), actor=actor)

    def decide(self, packet_id: int, action: str, text: Optional[str] = None, *, stamp: Optional[str] = None) -> dict:
        """An operator's choice: send | edit (send ``text``) | drop a pending draft, redraft (a group draft waiting:
        「重新起草」, ``_redraft_by_hand``), or
        wrong (a sent one should not have gone out: its kind is reviewed again, from zero; one the night
        window or 自动模式 sent the same, its record says so).  A 抖音百应 draft (``BUYIN_TOOL``) has its own: copy |
        manual_sent | drop (``_decide_buyin``); copy / manual_sent are refused for any other draft.  ``stamp`` (execute,
        微信里审批草稿): the fingerprint of the preview the admin was shown (``preview_stamp``): only that one runs."""
        packet = self._store.packet(int(packet_id))
        if packet is None:
            return {"ok": False, "error": f"没有找到草稿 {packet_id}"}
        now = self._clock()
        if is_buyin(packet):
            return self._decide_buyin(packet, action, text, now)
        if action in ("copy", "manual_sent"):
            return {"ok": False, "error": "只有百应私信的草稿能复制、标为已在百应发了"}
        if action == "wrong":
            if not self._store.mark_wrong(packet["id"]):
                return {"ok": False, "error": "只有已发出的消息能标为发错了（或已经标过）"}
            self._store.set_gate(packet["kind"], gates.REVIEW, now, reset=True)
            night = packet["route"] == gates.NIGHT
            self._store.add_event(packet["deal_id"], packet["party_id"], "wrong",
                                  {"packet": packet["id"], "kind": packet["kind"], **({"night": True} if night else {}),
                                   **({"auto": True} if night and (packet.get("meta") or {}).get(AUTO_MODE) else {})},
                                  actor="console", now=now)
            logger.info("PRM: packet %d marked wrong; %s drafts are reviewed again", packet["id"], packet["kind"])
            return {"ok": True, "status": "wrong"}
        # (a draft the target-change rule sent back: its ``text`` holds the words a person approved, ``meta.drafted``
        # the Mac's; the decision puts the Mac's back, so a record compares what went out with what the Mac wrote)
        drafted = (packet.get("meta") or {}).get("drafted")
        original = drafted if isinstance(drafted, str) else packet["text"]
        if action == "drop":
            if not self._store.move(packet["id"], (PENDING,), DROPPED, decided_at=now):
                return {"ok": False, "error": "这条草稿已经处理过了"}
            if original != packet["text"]:
                self._store.reword_packet(packet["id"], (DROPPED,), original)
            self._drop_cards(packet["attachments"])   # (直播手卡 not sent: her 称呼 is on them)
            self._person_decided(packet, now)
            self._group_draft_decided(packet)
            self._chain_decided(packet, now)          # 「不发」 a product: the next one is asked for (mac review F5)
            return {"ok": True, "status": DROPPED}
        if action == "execute":
            return self._approve_execute(packet, text, now, stamp=stamp)
        if action == "repreview":
            return self._repreview(packet, now)
        if action == "ack":
            return self._ack(packet, now)
        if action == "redraft":
            return self._redraft_by_hand(packet, now)
        if action not in ("send", "edit"):
            return {"ok": False, "error": f"不认识的操作 {action}"}
        if packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL:
            return {"ok": False, "error": "这条草稿带平台动作：要点「批准并执行」，平台上核验成功后才会发给达人"}
        final = packet["text"] if action == "send" else str(text if text is not None else "").strip()
        attachments, changed = packet["attachments"], {}
        if cards_expired(packet, now):
            if action == "send" or final == packet["text"]:
                return {"ok": False, "error": handcards.EXPIRED_TEXT}     # (the cards themselves are out of date)
            # 改了再发 (the console asked 「过期的手卡不会发出，只发这段话」 first): the edited words go, the out-of-date
            # cards are left out of the draft
            attachments = [a for a in attachments if not handcards.is_card_file(a.get("name"))]
            changed = {"attachments": attachments,
                       "meta": {**packet["meta"], HANDCARDS: {**packet["meta"][HANDCARDS], "left_out": True}}}
        if cards_failed(packet) and final == packet["text"]:
            # 直播手卡 not made: its words (「手卡整理好啦」) cannot go out as they are, without the cards
            return {"ok": False, "error": CARDS_FAILED_TEXT}
        if cards_gone(packet, attachments):
            return {"ok": False, "error": CARDS_GONE_TEXT}           # (a card file gone: the words would go alone)
        problem = self.edit_problem(packet, final, attachments=attachments)
        if problem:
            return {"ok": False, "error": problem}
        if self._send is None:
            return {"ok": False, "error": "机器人没有连着微信，现在不能发送"}
        if attachments and self._send_file is None:
            return {"ok": False, "error": "这个微信后端不能发文件（只有 Windows 数据库直读能发）"}
        # (re-review 2026-11-11 N1: the chat the person was shown; at send time another one sends nothing, ``send_approved``)
        if "meta" in changed:
            changed["meta"] = {**changed["meta"], "decided_chat": packet["chat_id"]}
        else:
            self._store.merge_packet_meta(packet["id"], {"decided_chat": packet["chat_id"]}, (PENDING,))
        if not self._store.move(packet["id"], (PENDING,), APPROVED, decided_at=now, final_text=final,
                                edited=int(final != original), **changed):
            return {"ok": False, "error": "这条草稿已经处理过了（或者达人又发了新消息，草稿已过时）"}
        if original != packet["text"]:           # (sent back before: the Mac's words on record again, ``final_text`` sent)
            self._store.reword_packet(packet["id"], (APPROVED, SENDING, SENT, FAILED), original)
        if changed:
            self._drop_cards([a for a in packet["attachments"] if a not in attachments])   # (the cards left out)
        self._person_decided(packet, now)
        self._group_draft_decided(packet)
        self._chain_decided(packet, now)
        self._wake.set()
        return {"ok": True, "status": APPROVED}

    def _group_draft_decided(self, packet: dict) -> None:
        """An operator sent or dropped her reply draft for a group: its redraft counters start again
        (``_reset_redrafts``; second check 2026-11-09: the cap is one waiting draft's)."""
        meta = packet.get("meta") or {}
        if is_group_packet(packet) and packet.get("job_id") is not None and meta.get("lane") != LANE_SYSTEM:
            self._reset_redrafts(str(packet["chat_id"]))

    def edit_problem(self, packet: dict, text: str, attachments: Optional[list] = None) -> str:
        """Why ``text`` cannot go out as this draft ("" when it can): longer than MAX_DRAFT_CHARS, nothing to send, a
        platform placeholder no action fills in ({链接} …, ``unfilled_placeholders``), a placeholder she has no number
        for, a shipping notice whose waybill is not the system's.  ``decide``'s checks of what is sent (the console's
        改了再发), and 微信里审批草稿's of a proposed edit (src/prm/wechat_review.py).  ``attachments``: the files that
        would go with it (``decide``: out-of-date 直播手卡 left out), the draft's own when None."""
        if len(text) > MAX_DRAFT_CHARS:
            return f"最多 {MAX_DRAFT_CHARS} 个字"
        if not text and not (packet["attachments"] if attachments is None else attachments):
            return "没有要发的内容"
        unfilled = unfilled_placeholders(text)
        if unfilled:
            return unfilled_text(unfilled)
        unknown = pii.unknown_tokens(text, self._store.tokens(packet["party_id"]))
        if unknown:
            return f"草稿里有对不上的占位符：{'、'.join(unknown)}"
        if packet["kind"] == "shipping_notice":
            waybill = self._waybill(packet)
            if not gates.waybill_ok(text, waybill, _more_waybills(packet)):
                return f"发货通知里的单号和系统记录对不上（系统记录：{waybill or '还没有运单号'}）"
        return ""

    def _packet_lines(self, packet: dict) -> str:
        """Her lines a draft answers (微信里审批草稿's notification): its job's text and follow-ups (``_job_lines``); ""
        for a draft of no job (a shipping notice) or one whose job is gone."""
        job = self._relay.store.get(int(packet["job_id"])) if packet.get("job_id") is not None else None
        return self._job_lines(job) if job is not None else ""

    def _person_decided(self, packet: dict, now: float) -> None:
        """An operator decided on a draft answering her: her recall (the draft said so) is seen to.  Not by a 百应
        draft's decision (her recall was in WeChat; ``_decide_buyin`` never comes here)."""
        if is_buyin(packet):
            return
        if (packet.get("meta") or {}).get("lane") != LANE_SYSTEM and packet.get("job_id") is not None:
            self._store.drop_notices(packet["deal_id"], lambda n: n["key"] == "recall", now)

    # ── platform actions (platforms.py; the CLIs run on the Mac) ───────────

    def _approve_execute(self, packet: dict, text: Optional[str], now: float, *, stamp: Optional[str] = None) -> dict:
        """批准并执行: the action goes to the Mac once (an approval key), the reply waits for
        its read-back.  The checks of a reply (length, placeholders, can it be sent) come
        first: a platform write whose reply could not go out is not started.  The move to executing happens only
        while the draft's platform data is what these checks read (a 重新预览 landing in between: refused, it is
        looked at again; ``_same_json``); with ``stamp`` (微信里审批草稿) only while its preview is the one the admin was
        shown.  ``text`` None: the draft as drafted (what the console and the notification show).  When the relay
        raises while queueing the job, whether it is there decides (``_job_queued``): there, it runs as approved;
        certainly not there, the draft waits again (the platform did not move: approve it again), an edited reply kept
        in ``final_text`` for the console's 「改了再执行」 box; not known, the draft is red as a result not known, its
        key kept (a late result is marked late), never to be approved again unchecked."""
        if packet.get("tool", MESSAGE_TOOL) == MESSAGE_TOOL:
            return {"ok": False, "error": "这条草稿没有平台动作"}
        data = packet["platform"]
        action, preview = data.get("action"), data.get("preview") or {}
        if stamp is not None and preview_stamp(preview) != stamp:
            return {"ok": False, "error": PREVIEW_CHANGED, "code": "preview_changed"}
        if not action or preview.get("verdict") != "ready":
            why = "；".join(preview.get("problems") or []) or platforms.VERDICTS.get(preview.get("verdict"), "")
            return {"ok": False, "error": f"预览没通过，不能执行：{why}"}
        if data.get("previewing"):
            return {"ok": False, "error": PREVIEWING, "code": "previewing"}
        if self._draft_context and draft_context.unchecked(packet):
            # PRM_DRAFT_CONTEXT: its last preview was not checked by photo (a draft from before the switch was on too), or
            # by an older rule (a v1 / v2 mark: 「这份是旧版 Mac 按旧规则核的…」)
            return {"ok": False, "error": draft_context.unchecked_reason(packet)}   # (each says to 重新预览)
        gone = draft_context.withdrawn(packet, self._confirmed_codes(packet["deal_id"])) if self._draft_context else []
        if gone:
            # PRM_DRAFT_CONTEXT: its last preview let a hat go only by a 「对过图了」 that was taken back (「撤销」) since
            return {"ok": False, "error": draft_context.withdrawn_reason(gone)}
        if action.get("action") == SAMPLE_TOOL and draft_context.sheets_gone(packet):
            # its 对图单 is not on this PC any more: nobody can look at it now (final check 2026-11-10 B)
            return {"ok": False, "error": draft_context.SHEET_GONE}
        held = (draft_context.held_unconfirmed(packet, self._confirmed_codes(packet["deal_id"]))
                if action.get("action") == SAMPLE_TOOL else [])
        if held:
            # its last preview held a hat for photos nobody confirmed since: never run, whatever the verdict says (review
            # 2026-11-10 A-L4: a second line, the switch on or off; a ready preview holds none)
            return {"ok": False, "error": draft_context.held_reason(held, self._draft_context)}
        if action.get("action") == LINK_ACTION:
            # a step of the chain binds its round's sample order's UID (``meta.chain_uid``): another is not run; any
            # other link draft was flagged when it came if its UID is none of hers (``_link_doubts``: she may have two
            # 抖音 accounts, review 2026-11-09 F4)
            chain_uid = str((packet.get("meta") or {}).get("chain_uid") or "")
            if chain_uid and chain_uid != str(action.get("creator_platform_id") or ""):
                return {"ok": False, "error": wrong_uid_text(str(action.get("creator_platform_id") or ""), chain_uid)}
        opened, unsure = self._opened_before(packet)
        if opened:
            if action.get("action") == SAMPLE_TOOL:
                logger.warning("PRM: packet %d not approved: another sample order of the deal runs, was verified lately "
                               "or waits too (%d to be said so first)", packet["id"], len(unsure))
            else:
                logger.warning("PRM: packet %d not approved: its %s ran before for this product and creator%s",
                               packet["id"], action.get("action"), " (a result not known: to be checked)" if unsure else "")
            if unsure:
                self._ask_recheck(packet, unsure, now)
            return {"ok": False, "error": opened}
        final = packet["text"] if text is None else str(text).strip()
        if cards_expired(packet, now):
            return {"ok": False, "error": handcards.EXPIRED_TEXT}
        if cards_failed(packet) and final == packet["text"]:
            return {"ok": False, "error": CARDS_FAILED_TEXT}      # (直播手卡 not made: as in ``decide``)
        if cards_gone(packet):
            return {"ok": False, "error": CARDS_GONE_TEXT}        # (as in ``decide``)
        if len(final) > MAX_DRAFT_CHARS:
            return {"ok": False, "error": f"最多 {MAX_DRAFT_CHARS} 个字"}
        problems = platforms.reply_problems(final, action)
        if problems:
            return {"ok": False, "error": "；".join(problems)}
        tokens = self._store.tokens(packet["party_id"])
        unknown = pii.unknown_tokens(final, tokens)
        if unknown:
            return {"ok": False, "error": f"草稿里有对不上的占位符：{'、'.join(unknown)}"}
        mac_action, problem = action_for_mac(action, tokens)
        if problem:
            return {"ok": False, "error": problem}
        if self._send is None:
            return {"ok": False, "error": "机器人没有连着微信：执行成功后回复发不出去，现在不执行"}
        if packet["attachments"] and self._send_file is None:
            return {"ok": False, "error": "这个微信后端不能发文件（只有 Windows 数据库直读能发）"}
        approved = {**data, "key": uuid.uuid4().hex, "approved_at": now}
        with self._approve_lock:              # two drafts of one product approved at the same moment: one runs
            opened, unsure = self._opened_before(packet)
            # (the platform data as read above: a 重新预览 landing meanwhile is not run unseen)
            moved = not opened and self._store.move(
                packet["id"], (PENDING,), EXECUTING, expect=lambda current: _same_json(current, data), decided_at=now,
                final_text=final, edited=int(final != packet["text"]), platform=approved)
        if opened:
            if unsure:
                self._ask_recheck(packet, unsure, now)
            return {"ok": False, "error": opened}
        if not moved:
            fresh = self._store.packet(packet["id"]) or {}
            if fresh.get("status") == PENDING:
                if (fresh.get("platform") or {}).get("previewing"):
                    return {"ok": False, "error": PREVIEWING, "code": "previewing"}
                return {"ok": False, "error": PREVIEW_CHANGED, "code": "preview_changed"}
            return {"ok": False, "error": "这条草稿已经处理过了（或者达人又发了新消息，草稿已过时）"}
        meta = {"op": "platform_execute", "party_id": packet["party_id"], "deal_id": packet["deal_id"],
                "packet_id": packet["id"], "key": approved["key"], "action": mac_action, "run": preview.get("run") or {}}
        creator = self._creator_ids(packet["deal_id"])
        if creator:
            meta["creator"] = creator                  # (the Mac looks at the invite ledger once more before it writes)
        if action["action"] == LINK_ACTION:
            leftover = self._link_leftovers(packet)
            if leftover:
                meta["leftover"] = leftover            # (the Mac makes no second one while these are there unbound)
        try:
            _, job_id = self._relay.submit(packet["chat_id"], packet["chat_id"], "", kind=PRM, meta=meta,
                                           fresh=True, input_closed=True)
        except Exception:
            logger.exception("PRM: packet %d: its execution could not be queued for the Mac", packet["id"])
            queued, job_id = self._job_queued(approved["key"])
            if queued is None:                       # (not known: it may run; red, its key kept)
                current = self._store.packet(packet["id"])
                if current is not None and current["status"] == EXECUTING:
                    self._platform_finished(current, platforms.record("UNKNOWN", [QUEUE_UNKNOWN_PROBLEM]), now)
                return {"ok": False, "error": QUEUE_UNKNOWN, "code": "queue_unknown"}
            if not queued:                           # (nothing was queued, nothing ran: it can be approved again)
                edited = final != packet["text"]
                self._store.move(packet["id"], (EXECUTING,), PENDING, decided_at=None,
                                 final_text=final if edited else None, edited=int(edited), platform=data)
                return {"ok": False, "error": QUEUE_FAILED, "code": "queue_failed"}
            logger.warning("PRM: packet %d: its execution is queued as job #%d all the same (what failed came after)",
                           packet["id"], job_id)
        self._store.set_platform(packet["id"], {**approved, "job_id": job_id}, (EXECUTING,))
        self._store.add_event(packet["deal_id"], packet["party_id"], "platform_execute",
                              {"packet": packet["id"], "action": action["action"], "job": job_id},
                              actor="console", now=now)
        self._person_decided(packet, now)
        if action["action"] in CHAIN_TOOLS:
            self._chain_refresh(packet["deal_id"], now)        # (that product is running now)
        logger.info("PRM: packet %d approved; %s goes to the Mac as job #%d", packet["id"], action["action"], job_id)
        return {"ok": True, "status": EXECUTING, "job": job_id}

    def _job_queued(self, key: str) -> tuple:
        """After the relay raised while queueing the execution of approval ``key``: is its job there all the same?
        ``(True, job id)``: it is (what failed came after it was written: it runs); ``(False, None)``: it is not (the
        relay store rolls a failed write back, so nothing hands it to the Mac later); ``(None, None)``: not known (the
        jobs could not be looked up)."""
        try:
            job = self._relay.store.job_with_key(key)
        except Exception:
            logger.exception("PRM: the relay's jobs could not be looked up after a failed queueing")
            return None, None
        return (True, int(job["id"])) if job is not None else (False, None)

    def _opened_before(self, packet: dict) -> tuple:
        """``(why, unsure)``: why this draft's 专属 action (``ONCE_ACTIONS``) must not run ("" = it may), and the
        drafts whose result an operator has to check on the platform first.  Another draft of the deal has the
        same action for the same product and creator (``platform_state``): running on the Mac now, verified on
        the platform (its reply sent, about to go, or held back), or ended with a result that may have opened it
        (mac review F6: ACCEPTED_UNVERIFIED, INDETERMINATE, UNKNOWN, PARTIAL …, a step of it that wrote): that
        one stops it until an operator, asked for this draft (``_ask_recheck``), found on the platform that it
        is not there and said so with 「我已处理」 on that red draft (``_released``).  One that wrote nothing
        (FAILED, STATE_CONFLICT, NOT_RUN, no step that wrote) does not count.  The ledger on the Mac only stops
        one approval from running twice; this stops a second draft (a chain picked up again while a step runs,
        a reply that carries a commission against kb/)."""
        data = packet["platform"]
        action = data.get("action") or {}
        name = action.get("action")
        if name == SAMPLE_TOOL:
            return self._second_order(packet)
        if name not in ONCE_ACTIONS:
            return "", []
        item, creators = action_target(action, data)
        what = f"商品 {item} 给达人 {action.get('creator_platform_id')} 的「{platforms.label(name)}」"
        unsure = []
        for other in self._store.deal_packets(packet["deal_id"], tools=(name,),
                                              statuses=(EXECUTING, APPROVED, SENDING, SENT, FAILED)):
            theirs = other["platform"] or {}
            their_item, their_creators = action_target(theirs.get("action") or {}, theirs)
            if other["id"] == packet["id"] or their_item != item or not (creators & their_creators):
                continue
            state = platform_state(other)
            if state == "running":
                at = float(theirs.get("approved_at") or other.get("decided_at") or other["created_at"])
                return f"{what}正在执行（#{other['id']}，{_date_text(at)} 批准），不能再开一次。{REOPEN_HINT}", []
            if state == "verified":
                at = float(other.get("sent_at") or other.get("decided_at") or other["created_at"])
                return f"{what}已经在 #{other['id']}（{_date_text(at)}）开过了，平台上核验成功，不能再开一次。{REOPEN_HINT}", []
            if state == "uncertain" and not _released(other, packet["id"]):
                unsure.append(other)
        if not unsure:
            return "", []
        newest = unsure[0]
        outcome = ((newest["platform"] or {}).get("execution") or {}).get("outcome") or "UNKNOWN"
        at = float((newest["platform"] or {}).get("approved_at") or newest.get("decided_at") or newest["created_at"])
        ids = "、".join(f"#{u['id']}" for u in unsure)
        channels = [c for c in (leftover_of(u) for u in unsure) if c]
        if channels:
            # 专属链接: the channel product is there, not bound (review 2026-11-09 F3)
            return (f"{what}上次在 {ids}（{_date_text(at)} 批准）建好了渠道品 {'、'.join(channels)}、没绑上她（{outcome}），"
                    "不能再建一个。先到抖店「渠道商品管理」处理它：在那里把她绑上了（链接自己发给她），就点这一份的「不发」，"
                    f"再点 {ids} 的「我已处理」；把它下架删掉了，就在「待审批」最上面 {ids} 的红色提醒上点「我已处理」，"
                    "再回来批准这一份（它还在、没绑上她时，Mac 不会再建）"), unsure
        return (f"{what}上次在 {ids}（{_date_text(at)} 批准）的结果不确定（{outcome}：{platforms.outcome_text(outcome)}），"
                "平台上可能已经开了，不能直接再开。请先到平台后台核对：平台上确实没有，就在「待审批」最上面 "
                f"{ids} 的红色提醒上点「我已处理」，再回来批准这一份；平台上已经有了，点这一份的「不发」"), unsure

    def _second_order(self, packet: dict) -> tuple:
        """``_opened_before`` for a sample order (``SAMPLE_TOOL``; review 2026-11-10 A-L5): ``(why, others)`` while another
        jst.sample_order of the deal is running on the Mac, was verified within SECOND_ORDER_DAYS, ended with a result
        not known within them (``platform_state`` "uncertain": INDETERMINATE, UNKNOWN, ACCEPTED_UNVERIFIED …, it may
        be in 聚水潭: final check 2026-11-10 A2), or waits in 待审批 too (another lane: her private chat and a group
        linked by hand), each one an operator has not said 「我已处理」 on for this draft since it was refused
        (``_released``).  ``others`` are asked about (``_ask_recheck``: red on 待审批 with 「我已处理」; an uncertain one
        says to look in 聚水潭 first); after that this draft runs, a genuine 补寄 too."""
        now = self._clock()
        made, running, waiting, uncertain = [], [], [], []
        for other in self._store.deal_packets(packet["deal_id"], tools=(SAMPLE_TOOL,),
                                              statuses=(PENDING, EXECUTING, APPROVED, SENDING, SENT, FAILED)):
            if other["id"] == packet["id"] or _released(other, packet["id"]):
                continue
            state = platform_state(other)
            at = float(other.get("decided_at") or other.get("sent_at") or other["created_at"])
            if state == "running":
                running.append(other)
            elif state == "verified":
                if now - at <= SECOND_ORDER_DAYS * 86400:
                    made.append(other)
            elif state == "uncertain":
                if now - at <= SECOND_ORDER_DAYS * 86400:
                    uncertain.append(other)
            elif other["status"] == PENDING:
                waiting.append(other)
        others = [*running, *made, *uncertain, *waiting]
        if not others:
            return "", []

        def ids(rows):
            return "、".join(f"#{r['id']}" for r in rows)

        said = [*([f"{SECOND_ORDER_DAYS} 天内已经建过寄样单 {ids(made)}"] if made else []),
                *([f"有一张寄样单 {ids(running)} 正在建"] if running else []),
                *([f"有寄样单 {ids(uncertain)} 的结果不确定（聚水潭里可能已经建了：先到聚水潭寄样管理核对）"]
                  if uncertain else []),
                *([f"还有一份寄样单 {ids(waiting)} 在等" if len(waiting) == 1 else
                   f"还有 {len(waiting)} 份寄样单 {ids(waiting)} 在等"] if waiting else [])]
        return (f"这位达人{' ' if made else ''}{'，'.join(said)}：确定要再建一张，先在「待审批」最上面 {ids(others)} 的红色提醒上"
                "点「我已处理」，再回来批准这一份；不用再建，点这一份的「不发」"), others

    def _ask_recheck(self, packet: dict, unsure: list, now: float) -> None:
        """This draft was refused for the uncertain result of ``unsure`` (``_opened_before``): each of them is
        red on 待审批 again (its 「我已处理」 shows once more, whether or not it was clicked before), its error
        and the deal's notice say what the operator is to check and what 「我已处理」 means now, and it keeps
        which draft asked (``recheck``: ``for``, ``at``, the error it had).  「我已处理」 after this lets this
        draft, and only this one, run (``_released``)."""
        asking = packet["id"]
        second = ((packet.get("platform") or {}).get("action") or {}).get("action") == SAMPLE_TOOL
        for other in unsure:
            channel = leftover_of(other)
            ask = (f"#{asking} 要给她再建一张寄样单：先到聚水潭寄样管理核对这一张建了没有。确实没有、或者要补寄，点这里的"
                   f"「我已处理」，再去批准 #{asking}；已经有了、不用再建，先点 #{asking} 的「不发」，再点这里的「我已处理」"
                   if second and other["status"] == FAILED else          # (its result not known: final check A2)
                   f"#{asking} 要给她再建一张寄样单：确定要再寄（补寄、多寄），点这里的「我已处理」，再去批准 #{asking}；"
                   f"不用再建，先点 #{asking} 的「不发」，再点这里的「我已处理」" if second else
                   f"#{asking} 要再建同一个商品的专属链接：渠道品 {channel} 删掉了，点这里的「我已处理」，再去批准 #{asking}；"
                   f"在抖店把她绑上了（链接自己发给她），先点 #{asking} 的「不发」，再点这里的「我已处理」" if channel else
                   f"#{asking} 要再开同一个商品：先到平台后台核对。平台上确实没有，点这里的「我已处理」，再去批准 #{asking}；"
                   f"平台上已经有了，先点 #{asking} 的「不发」，再点这里的「我已处理」")
            if other["status"] != FAILED:
                # (a second sample order: the other one runs, was verified or waits.  Its own 「我已处理」 is the recheck's,
                # ``acked_at`` inside it: the packet's own, a red result's, stays as it is)
                done = self._store.edit_platform(
                    other["id"], lambda data: {**data, "recheck": {"for": asking, "at": now, "ask": ask[:500],
                                                                   "acked_at": None}}, (other["status"],))
                if done is not None:
                    self._store.add_event(other["deal_id"], other["party_id"], "platform_recheck",
                                          {"packet": other["id"], "for": asking, "second": True}, actor="console", now=now)
                    logger.info("PRM: packet %d is a second sample order of the deal of packet %d; an operator says so "
                                "first", asking, other["id"])
                continue
            before = ((other["platform"] or {}).get("recheck") or {})
            error = str((before.get("error") if "error" in before else other.get("error")) or "")
            done = self._store.edit_platform(
                other["id"], lambda data: {**data, "acked_at": None, "recheck": {"for": asking, "at": now, "error": error}},
                (FAILED,), error=lambda _: f"{ask}。上次的结果：{error}"[:500])
            if done is None:
                continue
            self._store.set_notice(other["deal_id"], f"platform:{other['id']}", f"#{other['id']} 的结果不确定，{ask}", now)
            self._store.add_event(other["deal_id"], other["party_id"], "platform_recheck",
                                  {"packet": other["id"], "for": asking}, actor="console", now=now)
            logger.info("PRM: packet %d's uncertain result is to be checked before packet %d may run", other["id"], asking)

    def _repreview(self, packet: dict, now: float, *, auto: bool = False) -> dict:
        """Ask the Mac for a new preview of a waiting draft's action (after a login, say).  ``auto``: the bot's own, once
        the last held hat of a sample order was confirmed (``photo_confirm``): its event says so."""
        if packet.get("tool", MESSAGE_TOOL) == MESSAGE_TOOL:
            return {"ok": False, "error": "这条草稿没有平台动作"}
        data = packet["platform"]
        action = data.get("action")
        if not action:
            return {"ok": False, "error": "这个平台动作的参数不对，预览不了；等达人再发消息时的新草稿"}
        # ``preview_key``: only the answer of this, the latest new preview, counts (an earlier one lost on the way may
        # answer late: it never overwrites a newer preview, its sheet, its mark; _on_platform_job)
        key = uuid.uuid4().hex
        if not self._store.set_platform(packet["id"], {**data, "previewing": now, "preview_key": key}, (PENDING,),
                                        expect=lambda current: not current.get("previewing")):
            return {"ok": False, "error": "这条草稿已经处理过了，或者已经在重新预览"}
        meta = {"op": "platform_preview", "party_id": packet["party_id"], "deal_id": packet["deal_id"],
                "packet_id": packet["id"], "action": action, "preview_key": key}
        creator = self._creator_ids(packet["deal_id"])
        if creator:
            meta["creator"] = creator
        if self._draft_context and action.get("action") == "jst.sample_order":
            # PRM_DRAFT_CONTEXT: the Mac checks the sample order by photo again (with the codes an operator confirmed for
            # the deal, 「对过图了」; and her deal's 起草备注, as a draft job carries it)
            meta[draft_context.KEY] = self._draft_context_meta(packet["deal_id"])
            deal = self._store.deal(packet["deal_id"])
            if deal is not None:
                self._with_note(meta, deal, now)
        try:
            _, job_id = self._relay.submit(packet["chat_id"], packet["chat_id"], "", kind=PRM, meta=meta,
                                           fresh=True, input_closed=True)
        except Exception:
            logger.exception("PRM: a new preview of packet %d could not be queued", packet["id"])
            self._store.set_platform(packet["id"], {**data, "previewing": None}, (PENDING,))
            return {"ok": False, "error": "没能排进 Mac 的任务队列"}
        self._store.add_event(packet["deal_id"], packet["party_id"], "platform_preview",
                              {"packet": packet["id"], "job": job_id, **({"auto": True} if auto else {})},
                              actor="system" if auto else "console", now=now)
        if auto:
            logger.info("PRM: packet %d: every held hat is confirmed; previewed again by itself (job #%d)",
                        packet["id"], job_id)
        return {"ok": True, "status": PENDING, "job": job_id}

    def _ack(self, packet: dict, now: float) -> dict:
        """An operator has dealt with a platform action that ended red.  Asked to check it for a draft of the
        same 专属 action (``recheck``): the operator found it is not on the platform, and that draft may run.  A sample
        order that is not red, asked about for a second one of the deal (``_second_order``: running, verified, waiting):
        the operator means the second one, which may run then (the recheck's own ``acked_at``)."""
        recheck = (packet.get("platform") or {}).get("recheck") or {}
        second = packet["status"] != FAILED and bool(recheck) and not recheck.get("acked_at")
        if packet.get("tool", MESSAGE_TOOL) == MESSAGE_TOOL or (packet["status"] != FAILED and not second):
            return {"ok": False, "error": "只有没完成的平台动作能标为已处理"}
        if second:
            data = self._store.edit_platform(
                packet["id"], lambda d: None if not d.get("recheck") or d["recheck"].get("acked_at") else
                {**d, "recheck": {**d["recheck"], "acked_at": now}}, (packet["status"],))
        else:
            data = self._store.edit_platform(packet["id"], lambda d: None if d.get("acked_at") else {**d, "acked_at": now},
                                             (FAILED,))
        if data is None:
            return {"ok": False, "error": "已经标过了"}
        released = (data.get("recheck") or {}).get("for")
        self._store.add_event(packet["deal_id"], packet["party_id"], "platform_ack",
                              {"packet": packet["id"], **({"recheck_for": released} if released else {})},
                              actor="console", now=now)
        self._store.drop_notices(packet["deal_id"], lambda n: n["key"] == f"platform:{packet['id']}", now)
        if packet.get("tool") in CHAIN_TOOLS:
            self._chain_refresh(packet["deal_id"], now)
        if released:
            logger.info("PRM: packet %d checked on the platform (not there): packet %s may run", packet["id"], released)
        return {"ok": True, **({"released": released} if released else {})}

    def _on_platform_job(self, job: dict, meta: dict, now: float) -> bool:
        """The Mac's answer to a platform job: a new preview, or the execution's outcome."""
        try:
            packet = self._store.packet(int(meta.get("packet_id")))
        except (TypeError, ValueError):
            packet = None
        if packet is None or packet.get("tool", MESSAGE_TOOL) == MESSAGE_TOOL:
            return True
        status = job["status"]
        if meta.get("op") == "platform_preview":
            if status in ("pending", "running"):
                return True
            data = packet["platform"]
            if str(meta.get("preview_key") or "") != str(data.get("preview_key") or ""):
                # an earlier new preview's answer (lost on the way, then late): a newer one was asked for since
                self._drop_job_sheets(job["id"])
                logger.info("PRM: job #%d answers an earlier new preview of packet %d; ignored", job["id"], packet["id"])
                return True
            block, refused = None, ""
            if status == "done":
                try:
                    block = platforms.check_block(json.loads(job.get("result_text") or ""))
                except platforms.ActionRefused as e:
                    refused = f"新预览的动作没过机器人这边的检查（{e}）"
                except (ValueError, platforms.ActionError):
                    block = None
            answered = block is not None                 # (the Mac's new preview itself, not the old one marked)
            gated = draft_context.gated_of(job.get("result_text"))
            if answered and self._draft_context:
                block = draft_context.ungated(block, gated)
            if block is None:
                why = refused or {"lost": "Mac 失联了", "cancelled": "任务被取消了"}.get(status) or _job_error(job)
                preview = {**(data.get("preview") or {}), "problems": [f"重新预览没做成：{why}"[:200]]}
                try:
                    block = platforms.check_block(preview)
                except platforms.ActionError:
                    block = None
            if block:                                # (its receiver as the stored one: placeholders)
                block = {**block, "action": masked_action(block.get("action"), self._masker(packet["party_id"], now))}
            # ``previews``: how many new previews of this draft ended (微信里审批草稿 tells a draft told there again after
            # each: ``WechatReview._shown``)
            new = {**data, "previewing": None, "previews": int(data.get("previews") or 0) + 1,
                   **({"preview": block} if block else {})}
            marks = None
            if answered and (block.get("action") or {}).get("action") == "jst.sample_order" and (
                    self._draft_context or draft_context.KEY in (packet.get("meta") or {})):
                # PRM_DRAFT_CONTEXT: the packet keeps whether its last preview was checked by photo (approving asks); once
                # it has a mark, with the switch off too (a preview nothing checked never keeps an older one's mark); and
                # which codes it held for photos (「对过图了」) and let go only by a confirmation (「撤销」 holds it).
                # Written with the preview in one step (review 2026-11-10 A-L3: in two, an approval in between paired the
                # new ready preview with the old marks)
                marks = {
                    draft_context.KEY: draft_context.mark(gated),
                    draft_context.PHOTO_HELD: draft_context.codes_of(job.get("result_text"), draft_context.PHOTO_HELD)
                    if gated else [],
                    draft_context.PHOTO_CONFIRMED: draft_context.codes_of(job.get("result_text"),
                                                                          draft_context.PHOTO_CONFIRMED)
                    if gated else [],
                    # (None: a worker from before it, the bot reads the preview's words: draft_context.held_hats)
                    draft_context.PHOTO_HATS: draft_context.hat_marks(job.get("result_text")) if gated else None}
            # PRM_DRAFT_CONTEXT: the new preview's sheet replaces the draft's, and a new preview without one clears it (the
            # old sheet was not made for it); the old files are deleted.  A sheet that came with an answer that cannot be
            # read, for a draft no longer waiting, or past MAX_OPERATOR_FILES is deleted.  The sheets are written with
            # the preview and its marks in the same step (2026-11-10: 微信里审批草稿 tells the new preview again with its
            # pictures; in two steps a notice in between had the new preview with the old sheet)
            files, extra_sheets = draft_context.preview_operator_files(
                job.get("result_text"), self._relay.store.operator_files(job["id"]), self._relay.store.outfiles(job["id"]))
            old = ([f["path"] for f in draft_context.files_of(packet) if f["path"] not in {g["path"] for g in files}]
                   if answered else [])
            sheets = {draft_context.OPERATOR_FILES: files} if answered and (files or old) else {}
            written = self._store.set_platform(packet["id"], new, (PENDING,),
                                               meta={**(marks or {}), **sheets} if marks is not None or sheets else None)
            draft_context.delete_files(extra_sheets)
            if sheets:
                draft_context.delete_files(old if written else [f["path"] for f in files])
            elif files and status != "lost":         # (lost: its answer may still come, with them; else the sweep)
                draft_context.delete_files([f["path"] for f in files])
            if packet.get("tool") in CHAIN_TOOLS:
                self._chain_refresh(packet["deal_id"], now)    # (its preview may pass now)
            return True
        # platform_execute
        data = packet["platform"]
        if str(meta.get("key") or "") != str(data.get("key") or ""):
            logger.warning("PRM: job #%d answers another approval of packet %d; ignored", job["id"], packet["id"])
            return True
        if status == "lost":
            if packet["status"] == EXECUTING:
                self._store.set_platform(packet["id"], {**data, "lost_at": now}, (EXECUTING,))
                logger.warning("PRM: the Mac went silent while executing packet %d; waiting for it", packet["id"])
            return True
        if status in ("pending", "running"):
            return True
        record = None
        try:
            record = platforms.check_record(json.loads(job.get("result_text") or ""))
        except (ValueError, platforms.ActionError):
            record = None
        if record is None:
            if status == "cancelled":
                record = platforms.record("UNKNOWN", ["执行任务被取消了；如果 Mac 已经开始执行，请到平台后台核对"])
            elif status == "failed":
                record = platforms.record("NOT_RUN", [f"Mac 没有执行：{_job_error(job)}"[:300]])
            else:
                record = platforms.record("UNKNOWN", ["Mac 的执行结果读不出来；请到平台后台核对"])
        self._platform_finished(packet, record, now, cancelled=status == "cancelled")
        return True

    def _platform_finished(self, packet: dict, record: dict, now: float, cancelled: bool = False) -> None:
        """VERIFIED_SUCCESS: the reply, link filled in, is approved for the sender; else red."""
        data = packet["platform"]
        action = data.get("action") or {}
        outcome = record["outcome"]
        executed = {**data, "execution": record, "lost_at": data.get("lost_at")}
        if packet["status"] != EXECUTING:
            # A late answer (the packet was given up on): kept for the operators, nothing sent.
            if packet["status"] == FAILED:
                record["late"] = True
                self._store.set_platform(packet["id"], {**executed, "acked_at": None}, (FAILED,))
                self._store.set_notice(packet["deal_id"], f"platform:{packet['id']}", (
                    f"#{packet['id']} 平台动作的结果晚到了：{platforms.outcome_text(outcome)}；回复没有自动发，请运营处理"), now)
                self._after_platform(packet["id"], record, now, late=True)
            return
        event = {"packet": packet["id"], "action": action.get("action"), "outcome": outcome,
                 "link": (record.get("created") or {}).get("link", "")}
        for key in ("item_id", "creator_platform_id"):      # which product, which creator (an older event has neither)
            if action.get(key):
                event[key] = str(action[key])
        if (record.get("created") or {}).get("sample_order_no"):
            event["sample_order_no"] = record["created"]["sample_order_no"]
        if outcome == "NOT_RUN" and record.get("covered") and action.get("action") == "dy.designated_commission":
            # the invite card went out between the preview and the approval: the Mac wrote nothing, the product is
            # open all the same (not red: there is nothing to check)
            note = cover_text(record["covered"], str(action.get("item_id") or ""))
            if self._store.move(packet["id"], (EXECUTING,), DROPPED, outcome="covered", decided_at=now,
                                error=f"{note}，没有执行"[:500], platform=executed):
                self._store.add_event(packet["deal_id"], packet["party_id"], "platform_result", event, actor="mac", now=now)
                self._store.add_event(packet["deal_id"], packet["party_id"], "commission_covered", {
                    "packet": packet["id"], "item": str(action.get("item_id") or ""), "rate": record["covered"].get("rate"),
                    "at": record["covered"].get("at"), "aweme_id": record["covered"].get("aweme_id")}, actor="mac", now=now)
                logger.info("PRM: packet %d not run: the invite card opened its 专属佣金 already", packet["id"])
                self._after_platform(packet["id"], record, now)
            return
        missing = []
        if outcome == platforms.VERIFIED and not cancelled:
            final, missing = platforms.fill_reply(
                packet["final_text"] if packet["final_text"] is not None else packet["text"], action,
                record.get("created"))
            if not missing:
                if self._store.move(packet["id"], (EXECUTING,), APPROVED, final_text=final, decided_at=now,
                                    platform=executed):
                    self._store.add_event(packet["deal_id"], packet["party_id"], "platform_result", event,
                                          actor="mac", now=now)
                    logger.info("PRM: packet %d %s verified on the platform; the reply goes out", packet["id"],
                                action.get("action"))
                    self._wake.set()
                    self._after_platform(packet["id"], record, now)
                    if action.get("action") == "jst.sample_order":    # her 抖音号 as the order found it
                        self._creator_ids_notice(packet["deal_id"], now)
                return
        why = "；".join(record.get("problems") or [])
        if outcome == platforms.VERIFIED and missing:
            error = (f"平台上已经核验成功，但回复里的 {'、'.join(missing)} 平台没有给出，回复没有自动发出；"
                     "请核对后自己回达人")
        elif outcome == platforms.VERIFIED:        # verified, but the job was cancelled on the way
            error = "平台上已经核验成功，但执行任务中途被取消了，回复没有自动发出；请核对后自己回达人"
        elif outcome == "NOT_RUN" and written_state(record, packet.get("outcome", "")) == "not_done":
            # nothing ran (the Mac never took it, it was cancelled before it ran or while it waited for the
            # platform command lock): nothing to check on the platform
            error = (f"平台动作没有执行（{outcome}：{platforms.outcome_text(outcome)}），平台没有动，没有发微信"
                     + (f"：{why}" if why else "") + "。可以重新批准：同样的动作再起草一份时照常批准就行，系统不会自己重试")[:500]
        elif action.get("action") == LINK_ACTION and leftover_of({"platform": executed}):
            # the channel product is there and not bound: which one, and the two ways back (review 2026-11-09 F3)
            error = leftover_text(outcome, why, record.get("created") or {})
        else:
            maybe = (action.get("action") in ONCE_ACTIONS
                     and written_state(record, packet.get("outcome", "")) == "uncertain")
            error = (f"平台动作没有核验成功（{outcome}：{platforms.outcome_text(outcome)}），没有发微信"
                     + (f"：{why}" if why else "") + "。请到平台后台核对后自己处理，系统不会重试"
                     + ("；平台上可能已经开了：同一个商品给这位达人再批准时，系统会先请你核对" if maybe else ""))[:500]
        if self._store.move(packet["id"], (EXECUTING,), FAILED, outcome=f"platform_{outcome.lower()}", sent_at=now,
                            error=error, platform=executed):
            self._store.add_event(packet["deal_id"], packet["party_id"], "platform_result", event, actor="mac", now=now)
            # until an operator marks it dealt with (``_ack``), whatever the drafts after it say
            self._store.set_notice(packet["deal_id"], f"platform:{packet['id']}", f"#{packet['id']} {error}", now)
            logger.warning("PRM: packet %d %s ended %s; nothing sent", packet["id"], action.get("action"), outcome)
            self._after_platform(packet["id"], record, now)       # one product red: the chain goes on

    def _ask_next_step(self, packet: dict, note: str, now: float, delivered: bool = False, item: str = "",
                       action: str = "", prefix: str = "", uid: str = "") -> None:
        """A platform write that leads to the next one (a sample order → its 专属佣金 → with links, its 专属链接): a draft
        job of its own, told what happened in ``meta.system_note``.  What it drafts is a platform
        action like any other: previewed on the Mac, approved by a person; and whatever it drafts
        waits for a person, whatever its kind's gate (``on_result``: the system lane).  Not now for a
        creator the operators took off the list (inactive) or a deal a person has: the step is owed
        (a notice that stays, mac review F1) and asked for when that ends (``sweep_owed_steps``).
        ``delivered``: the step waits for the signature (no longer owed once the deal is set back); ``item``: the
        product it asks for (``_chain``; "" when the Mac picks it, or for the step that only tells it is done);
        ``action``: ``LINK_ACTION`` for a 专属链接 step ("" for a 专属佣金 one), ``prefix`` the title prefix its note
        gave ("": none known, the Mac is to ask her; such a step is asked again once the deal has one), ``uid`` the UID
        its draft is to bind (a step of the chain: its round's order's; its packet keeps it as ``meta.chain_uid``)."""
        if not note:
            return
        deal = self._store.deal(packet["deal_id"])
        party = self._store.party(packet["party_id"])
        if deal is None or party is None:
            return
        if self._step_blocked(deal, party, now):
            self._owe_step(deal, f"#{packet['id']} 之后该{'建专属链接' if action == LINK_ACTION else '开专属佣金'}了", note,
                           packet["id"], delivered, now, item=item, action=action, prefix=prefix, uid=uid)
            logger.info("PRM: party %d: the next step after packet %d is owed (inactive, or a person has the deal)",
                        party["id"], packet["id"])
            return
        self._store.drop_notices(deal["id"], lambda n: n["key"] == "step", now)     # this one goes now
        self._dispatch_step(deal, party, note, packet["id"], delivered, now, item=item, action=action, prefix=prefix,
                            uid=uid)

    def _step_blocked(self, deal: dict, party: dict, now: float) -> str:
        """Why a 专属佣金 step cannot be asked for now ("" = it can)."""
        if not party.get("active"):
            return "这位达人已经停用（不在达人名单里），系统先不起草；重新启用后会自动起草"
        if self._mode(deal, now) != COPILOT:
            return "现在不在副驾驶模式，系统先不起草；切回副驾驶（或者人工接管到期）后会自动起草"
        if self._reach(party)[0] is None:
            return f"{UNREACHABLE_TEXT}，系统先不起草；她回到群里或者加了好友、发来消息以后会自动起草"
        return ""

    def _owe_step(self, deal: dict, head: str, note: str, packet_id, delivered: bool, now: float,
                  item: str = "", action: str = "", prefix: str = "", uid: str = "", why: str = "",
                  retry_at: float = 0.0, tries: int = 0) -> None:
        """The deal's ``step`` notice: «``head``：why it waits» (``why``, else ``_step_blocked``'s), and what
        ``sweep_owed_steps`` needs to ask for it (the ``system_note``, the packet it follows, whether it waits for the
        signature, the product, whether it is a 专属链接 step: ``action``; not before ``retry_at``, and how many times
        the Mac could not draft it already: ``tries``, review 2026-11-09 N1)."""
        party = self._store.party(deal["party_id"]) or {}
        why = why or self._step_blocked(deal, party, now) or "系统一会儿就起草"
        self._store.set_notice(deal["id"], "step", f"{head}：{why}", now, head=head, note=note, packet=packet_id,
                               delivered=bool(delivered), item=str(item or ""), action=str(action or ""),
                               prefix=str(prefix or ""), uid=str(uid or ""), retry_at=float(retry_at or 0),
                               tries=int(tries or 0))

    def _dispatch_step(self, deal: dict, party: dict, note: str, packet_id, delivered: bool, now: float,
                       item: str = "", action: str = "", prefix: str = "", uid: str = "", tries: int = 0) -> int:
        """The draft job of a 专属佣金 (or 专属链接: ``action``) step (``meta.system_note``; ``meta.step`` says which
        packet it follows, whether it waited for the signature, the product it asks for and, for a link, its action,
        should it be owed again); returns its id."""
        link = {"action": action, "prefix": str(prefix or ""), **({"uid": str(uid)} if uid else {})} if action else {}
        meta = {"party_id": party["id"], "deal_id": deal["id"], "name": party["name"], "system_note": note,
                "step": {"packet": packet_id, "delivered": bool(delivered), "item": str(item or ""), **link,
                         **({"tries": int(tries)} if tries else {})}}
        creator = self._creator_ids(deal["id"])
        if creator:
            meta["creator"] = creator
        outbound = self._outbound(deal["id"], party["id"], now)
        if outbound:
            meta["sent"] = outbound
        # 老板拉的群: no private chat with her, the step goes to the group (``_step_blocked`` holds one for nowhere);
        # PRM_REACH_LAST_SPOKE: where her side last spoke
        chat, group, reach = self._reaching(party, made=True, deal=deal)
        chat = chat or party["wxid"]
        if group is not None:
            meta.update(channel=GROUP_CHANNEL, group=self.boss_groups.job_group(chat), **reach_meta(reach))
        # input_closed: the creator's next message is a reply job of its own, not part of this step
        _, job_id = self._relay.submit(chat, chat, "", kind=PRM, meta=self._reply_meta(self._with_note(meta, deal, now)),
                                       fresh=True, input_closed=True)
        # what was asked for, when (``_chain``: each product once per round; a link step says so).  Her UID only by its
        # last UID_TAIL characters (review 2026-11-10: the job's ``meta.step`` has it whole; the purge cuts the prefix)
        told = {**link, **({"uid": str(uid)[-UID_TAIL:]} if uid and action else {})}
        self._store.add_event(deal["id"], party["id"], "next_step", {"job": job_id, "after": packet_id,
                                                                     "delivered": bool(delivered),
                                                                     "item": str(item or ""), **told},
                              actor="system", now=now)
        # the chain goes on: a step that was not written before is seen to
        self._store.drop_notices(deal["id"], lambda n: str(n["key"]).startswith("failed:")
                                 and n.get("lane") == LANE_SYSTEM, now)
        logger.info("PRM: packet %s done; job #%d drafts the next step", packet_id, job_id)
        return job_id

    def sweep_owed_steps(self) -> int:
        """The 专属佣金 (and 专属链接) steps owed (the step came up while a person had the deal, or the creator was off
        the list): asked for once the deal is back in copilot mode (the takeover ran out, an operator
        switched it back) and the creator is on the list again; one that waited for the signature is
        dropped when the deal was set back before 已签收.  Returns how many were asked for."""
        asked = 0
        now = self._clock()
        for deal in self._store.deals_with_notice("step"):
            owed = next(n for n in deal["notices"] if n["key"] == "step")
            party = self._store.party(deal["party_id"])
            if party is None or (owed.get("delivered") and not deal.get("delivered_at")):
                self._store.drop_notices(deal["id"], lambda n: n["key"] == "step", now)
                continue
            if float(owed.get("retry_at") or 0) > now:
                continue                               # (asked again a little later: the Mac could not draft it)
            why = self._step_blocked(deal, party, now)
            if not why:
                # another step of hers under way (being drafted, its draft in 待审批): asked now it would put that one
                # out of date (review 2026-11-09 N2)
                why = {"drafting": "手头还有一步在起草，那一步交回来系统就派",
                       "draft": "手头还有一步的草稿在待审批，处理掉系统就派"}.get(self._step_under_way(deal, party), "")
            if why:
                if owed.get("head") and owed["text"] != f"{owed['head']}：{why}":
                    self._store.set_notice(deal["id"], "step", f"{owed['head']}：{why}", now)
                continue
            with self._chain_lock:                     # (the chain does not ask another meanwhile)
                stale = owed_out_of_date(owed, link_wish(deal.get("slots")))
                if self._store.drop_notices(deal["id"], lambda n: n["key"] == "step", now):
                    if stale:
                        # a link step whose note asks for a prefix she has given since (or another one), or links
                        # she no longer wants: asked afresh, with what the deal says now (review 2026-11-09 F7);
                        # without a chain and links no longer wanted, the step its packet asks for without them (the
                        # next 专属佣金: the Mac is still told what is left, review M2)
                        plain = (self._plain_next_step(owed) if self._chain(deal) is None
                                 and link_wish(deal.get("slots")) is None else "")
                        if plain:
                            self._dispatch_step(deal, party, stale + plain, owed.get("packet"),
                                                bool(owed.get("delivered")), now)
                            asked += 1
                        else:
                            self._chain_go(deal, now, why=stale)
                    else:
                        self._dispatch_step(deal, party, str(owed.get("note") or ""), owed.get("packet"),
                                            bool(owed.get("delivered")), now, item=str(owed.get("item") or ""),
                                            action=str(owed.get("action") or ""), prefix=str(owed.get("prefix") or ""),
                                            uid=str(owed.get("uid") or ""), tries=int(owed.get("tries") or 0))
                        asked += 1
            self._chain_refresh(deal["id"], now)
        return asked

    def sweep_executing(self) -> int:
        """Executions that cannot finish any more: a job the Mac has not taken within
        APPROVED_EXPIRE_SEC is cancelled (nothing was run); one whose Mac went silent for
        PLATFORM_LOST_GRACE_SEC, or whose job is gone, is given up on.  Returns how many."""
        done = 0
        now = self._clock()
        for packet in self._store.packets((EXECUTING,), limit=100):
            data = packet["platform"]
            job = self._relay.store.get(int(data.get("job_id") or 0)) if data.get("job_id") else None
            approved = float(data.get("approved_at") or packet["decided_at"] or now)
            record = None
            if job is None:
                if now - approved > 60:
                    record = platforms.record("UNKNOWN", ["找不到执行任务了；请到平台后台核对"])
            elif job["status"] == "pending" and now - approved > APPROVED_EXPIRE_SEC:
                if self._relay.store.cancel_if_pending(job["id"], now):
                    record = platforms.record("NOT_RUN", [f"Mac {APPROVED_EXPIRE_SEC // 60} 分钟没有接手，平台没有动"])
            elif job["status"] == "cancelled" and not job.get("result_text"):
                record = platforms.record("NOT_RUN", ["执行任务在 Mac 接手前被取消了，平台没有动"])
            elif data.get("lost_at") and job["status"] in ("lost", "running"):
                if job["status"] == "running" and float(job.get("heartbeat_at") or 0) > float(data["lost_at"]):
                    self._store.set_platform(packet["id"], {**data, "lost_at": None}, (EXECUTING,))   # it is back
                elif now - float(data["lost_at"]) > PLATFORM_LOST_GRACE_SEC:
                    record = platforms.record("UNKNOWN", [
                        f"Mac 失联超过 {PLATFORM_LOST_GRACE_SEC // 60} 分钟，不知道平台上执行了没有；请到平台后台核对"])
            if record is not None:
                self._platform_finished(self._store.packet(packet["id"]), record, now)
                done += 1
        return done

    # ── 聚水潭寄样单状态（PRM_SAMPLE_SYNC_MIN） ────────────────────────────

    def sweep_sample_status(self) -> Optional[int]:
        """PRM_SAMPLE_SYNC_MIN (0 = off): once an interval, the verified sample orders not signed for nor deleted
        (``_live_sample_orders``) go to the Mac in one job (``meta.op = SAMPLE_OP``, the order numbers), with
        ``meta.mine``: the Mac also reads our sub-account's newest orders made by hand (2026-11-05, ``_hand_answer``),
        so a round goes even when the bot made none: the worker reads 聚水潭 without the model and the answer comes
        back to ``on_result`` (``_on_sample_status``).  One job at a time (the last one still queued or running: none);
        when the last round was is kept in the database, so a restart does not ask again at once; after a risk wall
        (76) the next round waits longer.  Returns the job id when one was queued.  Errors are logged (the sender
        thread goes on)."""
        if self._sample_sync_sec <= 0:
            return None
        now = self._clock()
        try:
            with self._sample_lock:
                if now < self._sample_due(now) or self._relay.store.latest_open(SAMPLE_SYNC_CHAT) is not None:
                    return None
                self._store.set_meta_value("sample_sync_at", now)
                orders = self._live_sample_orders(now)
                text = (f"查聚水潭寄样单的状态（{len(orders)} 张，另看手动建的单）" if orders
                        else "查聚水潭寄样单的状态（手动建的单）")
                _, job_id = self._relay.submit(SAMPLE_SYNC_CHAT, SAMPLE_SYNC_CHAT, text, kind=PRM,
                                               meta={"op": SAMPLE_OP, "orders": orders, "mine": True}, fresh=True,
                                               input_closed=True)
        except Exception:
            logger.exception("PRM: the sample orders' status could not be asked for")
            return None
        logger.info("PRM: job #%d asks 聚水潭 where %d sample order(s) stand, and ours made by hand", job_id, len(orders))
        return job_id

    def _sample_due(self, now: float) -> float:
        """When the next round is due: an interval after the last one (kept in the database), not before a risk wall's
        pause ends; a bot that never asked: SAMPLE_SYNC_FIRST_SEC after its start."""
        last = _float(self._store.meta_value("sample_sync_at"))
        if last is None:
            return self._started + SAMPLE_SYNC_FIRST_SEC
        pause = _float(self._store.meta_value("sample_sync_pause_until")) or 0.0
        return max(min(last, now) + self._sample_sync_sec, pause)

    def _register_sample_orders(self, now: float) -> None:
        """Every sample order verified in 聚水潭 (a ``jst.sample_order`` draft whose execution is VERIFIED_SUCCESS, a
        late one too) is kept (``PrmStore.add_sample_order``) once: asked about from then on, unless its deal is
        cleared (8.2), it is older than SAMPLE_GIVE_UP_DAYS, or the deal was marked 已签收 after it already (by
        hand).  A new order of a deal sees to an earlier one's deletion notice (``_drop_dead_samples``)."""
        for packet in self._store.packets_of(("jst.sample_order",), statuses=(APPROVED, SENDING, SENT, FAILED)):
            execution = (packet["platform"] or {}).get("execution") or {}
            number = str((execution.get("created") or {}).get("sample_order_no") or "")
            if execution.get("outcome") != platforms.VERIFIED or not number or self._store.sample_order(number):
                continue
            made = _order_at(packet)
            deal = self._store.deal(packet["deal_id"])
            done, applied = "", {}
            if deal is None or deal.get("purged_at"):
                done = "gone"
            elif now - made > SAMPLE_GIVE_UP_DAYS * 86400:
                done = "old"
            elif deal.get("delivered_at") and float(deal["delivered_at"]) >= made:
                done, applied = "signed", {"signed": "by_hand"}
            if self._store.add_sample_order(number, packet_id=packet["id"], deal_id=packet["deal_id"],
                                            party_id=packet["party_id"], made_at=made, done=done, applied=applied,
                                            now=now) and not done:
                self._drop_dead_samples(packet["deal_id"], now)

    def _live_sample_orders(self, now: float) -> list:
        """The order numbers to ask about now (at most ``platforms.MAX_SAMPLE_ORDERS``, oldest first): kept, not
        done; one past SAMPLE_GIVE_UP_DAYS or of a deal cleared (8.2) is done from now on."""
        self._register_sample_orders(now)
        out = []
        for row in self._store.sample_orders(live=True):
            deal = self._store.deal(row["deal_id"])
            if deal is None or deal.get("purged_at"):
                self._store.update_sample_order(row["order_no"], now, done="gone")
            elif now - float(row["made_at"]) > SAMPLE_GIVE_UP_DAYS * 86400:
                self._store.update_sample_order(row["order_no"], now, done="old")
            else:
                out.append(row["order_no"])
        return out[:platforms.MAX_SAMPLE_ORDERS]

    def _on_sample_status(self, job: dict, meta: dict) -> bool:
        """The Mac's answer to a sample-status job (``platforms.check_sample_result``): each order's answer
        (``_sample_answer``), the ones 聚水潭 did not have (``_sample_missing``), and how the round went
        (``_sample_round_end``).  A job lost or cancelled waits for the next round (a lost one's late answer still
        counts); one that failed is a failed round."""
        status = job["status"]
        if status not in ("done", "failed"):
            return True
        now = self._clock()
        asked = [str(n) for n in meta.get("orders") or [] if isinstance(n, str)]
        result, stopped = None, None
        if status == "done":
            try:
                result = platforms.check_sample_result(json.loads(job.get("result_text") or ""), asked)
            except (ValueError, platforms.ActionError):
                stopped = {"verdict": "error", "exit_code": None, "message": "Mac 交回的寄样单状态读不出来"}
        else:
            why = _job_error(job)
            stopped = {"verdict": "old_worker" if "达人编号" in why else "error", "exit_code": None, "message": why}
        with self._sample_lock:
            if result is not None:
                for number, info in result["orders"].items():
                    try:
                        self._sample_answer(number, info, now)
                    except Exception:                  # one order's trouble does not stop the others
                        logger.exception("PRM: sample order %s: its status could not be applied", number)
                for number in result["missing"]:
                    self._sample_missing(number, now)
                stopped = result["stopped"]
            self._sample_round_end(stopped, now)
            if meta.get("mine") is True:
                self._hand_answer(result, stopped, now)
        return True

    # ── our sample orders made by hand (``mine``, 2026-11-05) ─────────────

    def _hand_answer(self, result: Optional[dict], stopped: Optional[dict], now: float) -> None:
        """Our sub-account's newest orders made by hand (``result["mine"]``, ``platforms.check_sample_result``), each
        acted on once it has a waybill (``_hand_sample``); none read (an old worker answers without them, or the read
        stopped: ``mine_stopped``, the job's ``stopped``): the 「待审批」 page says why (``HAND_PROBLEMS``; a hiccup only
        after HAND_ERRORS rounds in a row).  The first answer read only records what was shipped already
        (``HAND_BASELINE``).  Hand-made orders first seen HAND_KEEP_DAYS ago are forgotten."""
        mine = result.get("mine") if result is not None else None
        if mine is None:
            why = (result or {}).get("mine_stopped") or stopped
            if result is not None and why is None:
                verdict, message = "old_worker", ""
            else:
                verdict, message = str((why or {}).get("verdict") or "error"), str((why or {}).get("message") or "")
            if verdict in ("unavailable", "invalid"):
                verdict = "unavailable"
            if verdict in ("old_worker", "unavailable", "login", "wall", "wrong_account"):
                text = HAND_PROBLEMS[verdict].format(message=message[:120])
            else:
                self._hand_errors += 1
                if self._hand_errors < HAND_ERRORS:
                    logger.warning("PRM: our hand-made sample orders were not read (%s: %s); asked again next round",
                                   verdict, message[:200])
                    return
                text = HAND_PROBLEMS["error"].format(count=self._hand_errors, message=message[:120])
            if text != self._hand_problem:
                logger.warning("PRM: our hand-made sample orders are not read: %s", text)
            self._hand_problem = text
            return
        if self._hand_problem or self._hand_errors:
            logger.info("PRM: our hand-made sample orders are read again")
        self._hand_problem, self._hand_errors = "", 0
        baseline = self._store.meta_value(HAND_BASELINE) is None
        index = self._hand_index() if mine else {}
        for number, info in sorted(mine.items(), key=lambda kv: (kv[1].get("applied_at") or 0, kv[0])):
            try:
                self._hand_sample(number, info, index, baseline, now)
            except Exception:                          # one order's trouble does not stop the others
                logger.exception("PRM: hand-made sample order %s could not be applied", number)
        if baseline:
            self._store.set_meta_value(HAND_BASELINE, now)
        gone = self._store.purge_hand_samples(now - HAND_KEEP_DAYS * 86400)
        if gone:
            logger.info("PRM: %d hand-made sample order(s) first seen %d days ago forgotten", gone, HAND_KEEP_DAYS)
        self._store.purge_signed_hellos(now - HAND_KEEP_DAYS * 86400)   # (签收后问一句's notes: as long)

    def _hand_sample(self, number: str, info: dict, index: dict, baseline: bool, now: float) -> None:
        """One of our hand-made orders: the bot's own orders (``sample_orders``) and orders made SAMPLE_GIVE_UP_DAYS
        ago are left; one never seen is kept (``waiting``; ``baseline`` when the first answer read finds it shipped
        already: never acted on); a waiting one with a waybill is acted on once (``_hand_place``): ``filled`` (its
        waybill went into her deal, or was there already) or ``listed`` (a 「待审批」 line, ``why``).  Sent in
        聚水潭 without a waybill (its list may give the waybill a little later): still waiting, a line after
        HAND_WAYBILL_WAIT_SEC from its send time (the shipping notice needs one); when the waybill comes after all, it
        is acted on then (a line seen to already comes back with the waybill).  Only part of it went out
        (``platforms.sample_split``): nothing yet, a line after PRM_SAMPLE_WAYBILL_DAYS (``_hand_split``); acted on
        once the rest has waybills too (or 聚水潭 says it is signed for)."""
        if self._store.sample_order(number) is not None:
            return                                     # the bot's own order: its sync has it
        applied = info.get("applied_at")
        if applied is not None and now - float(applied) > SAMPLE_GIVE_UP_DAYS * 86400:
            return
        daren = info.get("daren") or {}
        ships = info.get("ships") or []
        ship = ships[0] if ships else None
        shipped = ship is not None or info.get("status") in ("SENT", "SIGNED")
        split = platforms.sample_split(info)
        row = self._store.hand_sample(number)
        if row is None:
            self._store.add_hand_sample(number, platform=daren.get("platform", ""), account=daren.get("platform_no", ""),
                                        nickname=daren.get("nickname", ""), applied_at=applied,
                                        status=info.get("status", ""),
                                        state="baseline" if baseline and shipped else "waiting", now=now)
            row = self._store.hand_sample(number)
            if row is None or row["state"] != "waiting":
                return
        late_bill = row["state"] == "listed" and not row["waybill"] and ship is not None   # "没给运单号", then one came
        if row["state"] == HAND_SPLIT and split is None:
            late_bill = True                           # its line said only part went out; the rest has now
        if row["state"] == "filled" and info.get("status") == "SIGNED":
            self._hand_signed(number, row, info, now)  # 签收后问一句 (PRM_SIGNED_HELLO): her deal has its waybill
        if row["state"] not in ("waiting", HAND_SPLIT) and not late_bill:
            return                                     # acted on once (or shipped before we looked)
        if not shipped:
            if info.get("status") and info["status"] != row["status"]:
                self._store.update_hand_sample(number, now, status=info["status"])
            return
        fields = {"status": info.get("status", "")}
        if ship is None:                               # sent, but 聚水潭 gives no waybill (yet)
            went = row["sent_at"] or info.get("sent_at") or now
            if now - float(went) < HAND_WAYBILL_WAIT_SEC:
                if row["sent_at"] is None or info.get("status") != row["status"]:
                    self._store.update_hand_sample(number, now, sent_at=went, **fields)
                return
            self._store.update_hand_sample(number, now, state="listed", sent_at=went,
                                           why="聚水潭显示已发货，但没给运单号：请到聚水潭寄样管理看运单号", **fields)
            logger.info("PRM: hand-made sample order %s sent without a waybill: the 待审批 page says so", number)
            return
        if split is not None:
            self._hand_split(number, row, ships, split, fields, now)
            return
        fields.update(carrier=ship["carrier"], waybill=ship["waybill"],
                      sent_at=ship.get("sent_at") or info.get("sent_at") or row["sent_at"] or now)
        if late_bill:
            fields["dismissed_at"] = None              # the waybill is news: its line comes back if it is a line
        state, why, party, deal = self._hand_place(number, info, daren, ships, index, applied, now)
        self._store.update_hand_sample(number, now, state=state, why=why,
                                       party_id=party["id"] if party else None, deal_id=deal["id"] if deal else None,
                                       **fields)
        logger.info("PRM: hand-made sample order %s shipped (%s): %s", number, ship["waybill"],
                    f"waybill into deal {deal['id']}" if state == "filled" else f"listed on 待审批 ({why})")
        if state == "filled" and info.get("status") == "SIGNED":
            self._hand_signed(number, self._store.hand_sample(number) or {}, info, now)   # signed at first sight

    def _hand_split(self, number: str, row: dict, ships: list, split: tuple, fields: dict, now: float) -> None:
        """A hand-made order only part of which went out (``platforms.sample_split``, ``(out, of)``): nothing goes
        into her deal and no line is shown yet; when the first package went is kept (``sent_at``: its time in
        聚水潭, else now); PRM_SAMPLE_WAYBILL_DAYS after it a 「待审批」 line (``HAND_SPLIT``, the first waybill, ``why``
        says how many went and to look in 聚水潭) until 「知道了」; the rest going out later still acts on it."""
        out, of = split
        went = row["sent_at"] or ships[0].get("sent_at") or now
        if now - float(went) < self._waybill_days * 86400:
            if row["sent_at"] is None or fields.get("status") != row["status"]:
                self._store.update_hand_sample(number, now, sent_at=went, **fields)
            return
        bills = "、".join(f"{s['carrier'] or '快递'} {s['waybill']}" for s in ships)
        why = (f"拆成了 {of} 个包裹，{int((now - float(went)) // 86400)} 天了只发出 {out} 个（{bills}），另外的还没有运单号："
               "请到聚水潭寄样管理看一下；要先给达人发货通知，就在她的合作单上填运单号")
        if row["state"] == HAND_SPLIT and row["why"] == why and fields.get("status") == row["status"]:
            return                                     # nothing new to say (the line keeps its place)
        first = row["state"] != HAND_SPLIT
        self._store.update_hand_sample(number, now, state=HAND_SPLIT, why=why, carrier=ships[0]["carrier"],
                                       waybill=ships[0]["waybill"], sent_at=went,
                                       **({"dismissed_at": None} if first else {}), **fields)
        if first:
            logger.info("PRM: hand-made sample order %s: %d of %d packages out for %d days: the 待审批 page says so",
                        number, out, of, self._waybill_days)

    def _hand_place(self, number: str, info: dict, daren: dict, ships: list, index: dict, applied,
                    now: float) -> tuple:
        """Where a shipped hand-made order's waybill goes: ``(state, why, party, deal)``.  Her platform account
        (``_hand_index``) fits exactly one registered creator whose deal is still before the sample is signed for
        (``deals.BEFORE_DELIVERY``): into her deal the console's way (``deal_action`` ``shipment``, actor system,
        ``review``: the shipping notice waits for a person, naming every package's waybill; none when 聚水潭 says
        signed for already), or nothing when the deal has this waybill since the order was made; a different one
        there, the deal past the sample, two creators or none: a line (``listed``) saying so.  Never marks the deal
        已签收, never a 专属佣金."""
        ship = ships[0]
        platform, account = daren.get("platform") or "", str(daren.get("platform_no") or "")
        wxids = set(index.get("keys", {}).get((platform, account.lower()), ())) if account else set()
        if not wxids:
            return "listed", "没登记的达人", None, None
        if len(wxids) > 1:
            return "listed", f"平台账号对得上 {len(wxids)} 位登记的达人，系统没有填", None, None
        party = index["parties"][next(iter(wxids))]
        deal = self._store.deal_of(party["id"], now)
        if deal["stage"] not in deals.BEFORE_DELIVERY:
            return "listed", f"{party['name']} 的合作单已经过了寄样这一步，系统没有填", party, deal
        made = float(applied) if applied is not None else 0.0     # (when 聚水潭 did not say: any shipment of hers)
        current = self._sample_shipment_of(deal["id"], made)
        if current is not None:
            if current["waybill"] == ship["waybill"]:
                return "filled", "", party, deal          # typed in already: nothing more
            return ("listed", f"{party['name']} 的合作单上填的是 {current['carrier']} {current['waybill']}，对不上，"
                              "系统没有改", party, deal)
        result = self.deal_action("shipment", {"deal_id": deal["id"], "carrier": ship["carrier"] or "快递",
                                               "waybill": ship["waybill"]},
                                  actor="system", review=True, notice=info.get("status") != "SIGNED",
                                  note={"source": "jst", "order": number, "hand": True},
                                  more=[(s["carrier"] or "快递", s["waybill"]) for s in ships[1:]])
        if not result.get("ok"):
            why = " ".join(str(result.get("error") or "没说原因").split())[:80]
            return "listed", f"运单号没能填进 {party['name']} 的合作单（{why}）", party, deal
        return "filled", "", party, deal

    def _hand_index(self) -> dict:
        """Who a hand-made order is for: her platform and account (any case) -> the wxids of the registered (active)
        creators known by them: the 达人名单's platform and 抖音号 / 小红书号 (``creator_directory``), the 抖音号 the
        deals' verified 抖店 sample orders found and the links (``_creator_ids``), the 小红书 links.  Her nickname is
        never used."""
        parties = {p["wxid"]: p for p in self._store.parties()}
        index: dict = {"keys": {}, "parties": parties}

        def add(platform: str, account, wxid: str) -> None:
            account = str(account or "").strip()
            if platform and account:
                index["keys"].setdefault((platform, account.lower()), set()).add(wxid)

        rows: list = []
        if self.creator_directory is not None:
            try:
                rows = list(self.creator_directory() or [])
            except Exception:
                logger.exception("PRM: the creator list could not be read for the hand-made sample orders")
        for row in rows:
            wxid = str((row or {}).get("wxid") or "")
            if wxid in parties:
                add(str(row.get("platform") or "").strip().upper(), row.get("platform_no"), wxid)
        for wxid, party in parties.items():
            ids = self._creator_ids(self._store.deal_of(party["id"])["id"])
            for douyin in ids.get("douyin_ids") or []:
                add("DY", douyin, wxid)
            for link in self._store.creator_links(party["id"]):
                if link["platform"] == "xhs":
                    add("XHS", link["account"], wxid)
        return index

    def hand_sample_seen(self, order_no) -> dict:
        """「知道了」 on a 「待审批」 line of a hand-made order: not shown again."""
        number = str(order_no or "").strip()
        if not number:
            return {"ok": False, "error": "要给出寄样单号"}
        if not self._store.dismiss_hand_sample(number, self._clock()):
            return {"ok": False, "error": "这一行已经不在了"}
        return {"ok": True}

    def _sample_answer(self, number: str, info: dict, now: float) -> None:
        """One order's answer from 聚水潭, each change acted on once (what was done is kept in the order's
        ``applied``; its status only goes forward, ``SAMPLE_RANK``; signed and deleted orders are not asked about
        again), only for the deal's newest verified order (``_chain_order``: the one its 专属佣金 follow) and only
        while the deal is not past the sample (``deals.BEFORE_DELIVERY``):

        - a waybill the deal has none of since the order was made is filled in the console's way (``deal_action``
          ``shipment``, actor system): 已寄出 and a shipping notice from the template that always waits for a person
          (none when 聚水潭 says it is signed for already); a waybill an operator typed that is not 聚水潭's is kept,
          the deal says so;
        - 已签收 marks the deal 已签收 the console's way (``shipment_status``; ``stage`` when the deal has no
          shipment of this order): the 专属佣金 chain starts, a step owed while a person has the deal or the creator
          is off the list, as ever;
        - deleted or rejected: the deal says so, with the reason (a deletion until a person sees to it:
          ``_drop_dead_samples``);
        - sent in 聚水潭 without a waybill (its sample list gives none: ``SAMPLE_ASK``) and none typed in: the deal
          asks for it (the shipping notice needs it) until an operator types it in;
        - not sent PRM_SAMPLE_WAYBILL_DAYS after the order was made, not signed for PRM_SAMPLE_SIGN_DAYS after it
          was sent: the deal says so.
        What the deal says of the order is its notice ``sample:<order>`` (and ``SAMPLE_ASK<order>``), written again
        with every answer (gone when there is nothing to say)."""
        row = self._store.sample_order(number)
        if row is None or row["done"]:
            return
        deal = self._store.deal(row["deal_id"])
        if deal is None or deal.get("purged_at"):
            self._store.update_sample_order(number, now, done="gone", checked_at=now)
            return
        applied = dict(row["applied"])
        before, rank = SAMPLE_RANK.get(row["status"], 0), SAMPLE_RANK.get(info["status"], 0)
        status = info["status"] if info["status"] and rank >= before else row["status"]
        ships = info["ships"]
        ship = ships[0] if ships else None
        split = platforms.sample_split({**info, "status": status})     # only part of it went out: wait for the rest
        made = float(row["made_at"])
        fields = {"status": status, "checked_at": now, "misses": 0, "reason": info["reason"]}
        if ship:
            fields.update(carrier=ship["carrier"], waybill=ship["waybill"])
            if row["waybill"] != ship["waybill"] or row["sent_at"] is None:
                fields["sent_at"] = ship["sent_at"] or info["sent_at"] or now
        elif row["sent_at"] is None and (info["sent_at"] or status == "SENT"):
            fields["sent_at"] = info["sent_at"] or now     # sent without a waybill (SAMPLE_ASK): 聚水潭's own time
        if status == "SIGNED":
            fields["signed_at"] = info["signed_at"] or row["signed_at"] or now
        if info["status"] and rank > before:
            self._store.add_event(deal["id"], row["party_id"], "sample_status", {
                "order": number, "status": info["status"], **({"waybill": ship["waybill"]} if ship else {}),
                **({"reason": info["reason"]} if info["reason"] else {})}, actor="system", now=now)
        newest = self._chain_order(deal["id"])
        latest = newest is not None and newest["id"] == row["packet_id"]
        notes = []
        if status == "DELETED":
            why = f"（{info['reason']}）" if info["reason"] else "（聚水潭没写原因）"
            note = (f"聚水潭寄样单 {number} 被删除了{why}：样品不会寄出，这张单的专属佣金也不会开；要重寄就让达人重新确认、"
                    "重新建寄样单（新单建好，或者在这里改一次阶段，这条提醒就去掉）")
            applied["dead"] = f"DELETED|{info['reason']}"
            self._store.update_sample_order(number, now, applied=applied, done="deleted", **fields)
            self._store.set_notice(deal["id"], f"sample:{number}", note, now)
            self._store.set_notice(deal["id"], f"{SAMPLE_ASK}{number}", "", now)
            logger.warning("PRM: sample order %s was deleted in 聚水潭; deal %d says so", number, deal["id"])
            return
        if info["reason"] and status != "SIGNED":
            notes.append(f"聚水潭寄样单 {number} 被驳回了（{info['reason']}）：请到聚水潭寄样管理处理（改好重新提交，或者删掉）")
            applied["dead"] = f"REJECTED|{info['reason']}"
        if ship:
            # the deal's shipment of this order (the newest one since it was made): the sync's own, or one an operator typed
            current = self._sample_shipment_of(deal["id"], made)
            if current is not None and current["waybill"] == ship["waybill"]:
                applied["waybill"] = ship["waybill"]
            elif current is not None and current["id"] != applied.get("shipment"):
                notes.append(f"聚水潭寄样单 {number} 的运单号是 {ship['carrier'] or '快递'} {ship['waybill']}，合作单上填的是 "
                             f"{current['carrier']} {current['waybill']}：对不上，系统没有改，请核对")
            elif not latest:
                notes.append(f"聚水潭寄样单 {number} 有了运单号 {ship['carrier'] or '快递'} {ship['waybill']}，但它不是这张合作单"
                             "最新的寄样单，系统没有填进合作单")
            elif deal["stage"] in deals.BEFORE_DELIVERY and split is not None:
                notes.append(self._split_note(number, ships, split, applied, now))
            elif deal["stage"] in deals.BEFORE_DELIVERY:
                filled = self._sample_shipment(deal, number, ships, signed=status == "SIGNED")
                if filled is not None:
                    applied.update(waybill=ship["waybill"], shipment=filled["id"])
                    applied.pop("split_since", None)
                    deal = self._store.deal(deal["id"]) or deal
            if len(ships) > 1:
                notes.append(f"聚水潭寄样单 {number} 有 {len(ships)} 个运单号（"
                             + "、".join(f"{s['carrier']} {s['waybill']}".strip() for s in ships)
                             + f"），合作单上记的是 {ship['waybill']}")
        ask = ""
        if status == "SIGNED":
            fields["done"] = "signed"
            notes = []                                 # signed for: what was missing or did not match is moot now
            if latest and deal["stage"] in deals.BEFORE_DELIVERY:
                # 签收后问一句 (PRM_SIGNED_HELLO): noted before the sync marks the deal (nobody marked it: she has not said)
                self._signed_seen(number, deal, int(row["party_id"]), fields["signed_at"], hand=False, now=now)
                if self._sample_signed(deal, number, made):
                    applied["signed"] = fields["signed_at"]
            else:
                applied["signed"] = "left"             # an older order, or a deal past the sample: left as it is
                if latest:
                    self._signed_seen(number, deal, int(row["party_id"]), fields["signed_at"], hand=False, now=now,
                                      skip=_moved_on(deal))
        elif latest and deal["stage"] in deals.BEFORE_DELIVERY:
            late, ask = self._sample_late(number, status, ship, row, deal, made, fields, now)
            notes += late
        self._store.update_sample_order(number, now, applied=applied, **fields)
        self._store.set_notice(deal["id"], f"sample:{number}", "；".join(notes), now)
        self._store.set_notice(deal["id"], f"{SAMPLE_ASK}{number}", ask, now)

    def _sample_late(self, number: str, status: str, ship, row: dict, deal: dict, made: float, fields: dict,
                     now: float) -> tuple:
        """The deal's reminders for an order not signed for, and its ask for the waybill: ``(notes, ask)``.  Not sent
        (no waybill, none typed in either) PRM_SAMPLE_WAYBILL_DAYS after it was made; sent in 聚水潭 without a waybill
        and none typed in: ``ask`` (``SAMPLE_ASK``); sent PRM_SAMPLE_SIGN_DAYS ago (聚水潭's time when it gave no
        waybill) and not signed for."""
        typed = self._sample_shipment_of(deal["id"], made)
        went = fields.get("sent_at") or row.get("sent_at")
        ask = ""
        if ship is None and typed is None:
            if not went:
                if now - made >= self._waybill_days * 86400:
                    state = platforms.SAMPLE_STATES.get(status, status or "状态不明")
                    return [f"聚水潭寄样单 {number} 建好 {int((now - made) // 86400)} 天了还没发货（{state}）："
                            "请到聚水潭寄样管理看一下是不是没审核或者没发货"], ""
                return [], ""
            ask = (f"聚水潭显示寄样单 {number} 已发货（{_clock_text(float(went))}），但没给运单号：要给达人发发货通知，"
                   "请在合作单上填运单号")
        sent = float(went or (typed or {}).get("created_at") or now)
        if now - sent >= self._sign_days * 86400:
            which = (f"{ship['carrier'] or '快递'} {ship['waybill']}" if ship
                     else f"{typed['carrier']} {typed['waybill']}" if typed else "聚水潭没给运单号")
            return [f"聚水潭寄样单 {number} 发货 {int((now - sent) // 86400)} 天了还没签收（{which}）：请查一下物流"], ask
        return [], ask

    def _split_note(self, number: str, ships: list, split: tuple, applied: dict, now: float) -> str:
        """What the deal says of an order only part of which went out (``platforms.sample_split``, ``(out, of)``):
        nothing goes into the deal and no shipping notice is drafted until every package has a waybill; when the
        first was seen so is kept in the order's ``applied`` (``split_since``, so a restart keeps the count); from
        PRM_SAMPLE_WAYBILL_DAYS after it the note asks for a look in 聚水潭 (a waybill typed on the deal still sends a
        notice the console's way)."""
        out, of = split
        since = _float(applied.get("split_since"))
        if since is None:
            since = applied["split_since"] = now
        bills = "、".join(f"{s['carrier'] or '快递'} {s['waybill']}" for s in ships)
        if now - since >= self._waybill_days * 86400:
            return (f"聚水潭寄样单 {number} 拆成了 {of} 个包裹，{int((now - since) // 86400)} 天了只发出 {out} 个（{bills}），"
                    "另外的还没有运单号：请到聚水潭寄样管理看一下；要先给达人发货通知，就在合作单上填运单号")
        return f"聚水潭寄样单 {number} 拆成了 {of} 个包裹，先发了 {out} 个（{bills}）：等全部发出，系统再给达人发货通知"

    def _sample_shipment_of(self, deal_id: int, made: float) -> Optional[dict]:
        """The deal's newest shipment since the order was made (one of an earlier order is not this one's)."""
        return next((s for s in self._store.shipments(deal_id) if float(s["created_at"]) >= made - 1), None)

    def _sample_shipment(self, deal: dict, number: str, ships: list, *, signed: bool) -> Optional[dict]:
        """The order's waybill into the deal the console's way (``deal_action`` ``shipment``, actor system): the
        deal goes to 已寄出 and a shipping notice from the template waits for a person whatever its kind's gate
        (``review``); none when 聚水潭 says it is signed for already.  Several packages: the deal keeps the first
        waybill, the notice names every one (``more``).  The new shipment, or None."""
        ship = ships[0]
        result = self.deal_action("shipment", {"deal_id": deal["id"], "carrier": ship["carrier"] or "快递",
                                               "waybill": ship["waybill"]},
                                  actor="system", review=True, notice=not signed,
                                  note={"source": "jst", "order": number},
                                  more=[(s["carrier"] or "快递", s["waybill"]) for s in ships[1:]])
        if not result.get("ok"):
            logger.warning("PRM: sample order %s: its waybill could not go into deal %d (%s)", number, deal["id"],
                           result.get("error"))
            return None
        logger.info("PRM: sample order %s: waybill %s into deal %d%s", number, ship["waybill"], deal["id"],
                    "" if signed else f"; shipping notice #{result.get('packet')} waits for a person")
        return self._store.latest_shipment(deal["id"])

    def _sample_signed(self, deal: dict, number: str, made: float) -> bool:
        """The deal marked 已签收 the console's way, by the order's shipment when the deal has one of it
        (``shipment_status``), else by its stage: the 专属佣金 chain of the signature starts (``deal_action`` →
        ``_examine_delivery``)."""
        if self._sample_shipment_of(deal["id"], made) is not None:
            action, data = "shipment_status", {"status": "delivered"}
        else:
            action, data = "stage", {"stage": "delivered"}
        result = self.deal_action(action, {"deal_id": deal["id"], **data}, actor="system",
                                  note={"source": "jst", "order": number})
        if not result.get("ok"):
            logger.warning("PRM: sample order %s: deal %d could not be marked 已签收 (%s)", number, deal["id"],
                           result.get("error"))
            return False
        logger.info("PRM: sample order %s signed for in 聚水潭: deal %d marked 已签收", number, deal["id"])
        return True

    # ── 签收后问一句 (PRM_SIGNED_HELLO, 2026-11-07) ──────────────────────────

    def _signed_seen(self, number: str, deal: dict, party_id: int, signed_at: Optional[float], hand: bool, now: float,
                     skip: str = "") -> None:
        """The sync saw ``number`` signed for (``signed_at``: 聚水潭's time, else now) on ``deal`` (``hand``: one of ours
        made by hand).  Noted once (``signed_hellos``, never again however often it is read): owed, for
        ``sweep_signed_hellos`` to ask for; ``skip`` (why not: the deal moved on before the sync saw it) or signed
        SIGNED_HELLO_DAYS ago or more (a first round after the switch went on, a long outage): noted as skipped.
        Switched off: nothing is noted."""
        if not self._signed_hello or self._store.signed_hello(number) is not None:
            return
        at = float(signed_at or now)
        if not skip and now - at >= SIGNED_HELLO_DAYS * 86400:
            skip = f"签收已经 {SIGNED_HELLO_DAYS} 天以上了"
        if self._store.add_signed_hello(number, deal_id=deal["id"], party_id=party_id, signed_at=at, hand=hand,
                                        state="skipped" if skip else "owed", why=skip, now=now):
            logger.info("PRM: sample order %s signed for in 聚水潭 (deal %d): %s", number, deal["id"],
                        "no draft asks her about it (see signed_hellos)" if skip else "a draft asking her about it is owed")
            if not skip:
                self._wake.set()

    def _hand_signed(self, number: str, row: dict, info: dict, now: float) -> None:
        """One of ours made by hand, its waybill in her deal (``filled``: ``row``'s deal and party), signed for in 聚水潭:
        noted like the bot's own (``_signed_seen``), skipped when the deal is past the sample already (a person marked
        it, or moved it on).  The deal itself is never marked 已签收 (hand-made orders: as before)."""
        if not self._signed_hello or not row.get("deal_id") or self._store.signed_hello(number) is not None:
            return
        deal = self._store.deal(int(row["deal_id"]))
        if deal is None or deal.get("purged_at"):
            return
        skip = "" if deal["stage"] in deals.BEFORE_DELIVERY else _moved_on(deal)
        self._signed_seen(number, deal, int(row.get("party_id") or deal["party_id"]), info.get("signed_at"),
                          hand=True, now=now, skip=skip)

    def sweep_signed_hellos(self) -> int:
        """Every owed 签收后问一句 (``_signed_seen``): asked for once the hours and her chat allow (``_hello_check``), noted
        as skipped once it is not to be any more, else left owed (why it waits is logged once per change).  Switched
        off: nothing (what is owed waits).  Returns how many were asked for."""
        if not self._signed_hello:
            return 0
        asked = 0
        for row in self._store.signed_hellos(state="owed"):
            try:
                with self._handle_lock:                # (her message makes its job under the same lock)
                    asked += int(self._hello_step(self._store.signed_hello(row["order_no"]) or row, self._clock()))
            except Exception:
                logger.exception("PRM: sample order %s: the draft asking her about it could not be made", row["order_no"])
        return asked

    def _hello_step(self, row: dict, now: float) -> bool:
        """One owed 签收后问一句: asked for (True), skipped, or left waiting."""
        number = row["order_no"]
        if row["state"] != "owed":
            return False
        deal = self._store.deal(int(row["deal_id"]))
        party = self._store.party(int(row["party_id"])) if deal is not None else None
        skip, wait = self._hello_check(row, deal, party, now)
        if skip:
            self._store.update_signed_hello(number, now, state="skipped", why=skip)
            self._hello_waits.pop(number, None)
            logger.info("PRM: sample order %s: no draft asks her about the signature any more", number)
            return False
        if wait:
            if wait != row["why"]:
                self._store.update_signed_hello(number, now, why=wait)
            if self._hello_waits.get(number) != wait:
                self._hello_waits[number] = wait
                logger.info("PRM: sample order %s: the draft asking her about the signature waits (%s)", number,
                            "outside PRM_SIGNED_HELLO_HOURS" if wait.startswith("不在") else "her chat is busy or held")
            return False
        job_id = self._ask_signed_hello(row, deal, party, now)
        self._store.update_signed_hello(number, now, state="asked", why="", job_id=job_id)
        self._hello_waits.pop(number, None)
        return True

    def _hello_check(self, row: dict, deal: Optional[dict], party: Optional[dict], now: float) -> tuple:
        """``(skip, wait)`` for an owed 签收后问一句 ("" both: ask now).  Skipped: the deal or creator is gone or cleared,
        she wrote after the signature (her reply draft answers her), the deal moved on (the bot's own order: past
        已签收, which the sync set; ours made by hand: past the sample, a person marked it), the signature is
        SIGNED_HELLO_DAYS old.  Waits: outside PRM_SIGNED_HELLO_HOURS (this PC's clock), off the list, a person has
        the chat (or it is paused), she cannot be reached, a draft of hers is being written or waits in 待审批 (a 百应
        one is another channel), as ``_no_huopan`` has it."""
        if deal is None or party is None or deal.get("purged_at"):
            return "合作单不在了", ""
        signed = float(row["signed_at"])
        if float(deal.get("last_message_at") or 0) > signed:
            return "她签收之后发过消息：回她的草稿照常写", ""
        stages = deals.BEFORE_DELIVERY if row["hand"] else (*deals.BEFORE_DELIVERY, "delivered")
        if deal["stage"] not in stages:
            return _moved_on(deal), ""
        if now - signed >= SIGNED_HELLO_DAYS * 86400:
            return f"签收 {SIGNED_HELLO_DAYS} 天了还没问成", ""
        if not gates.in_night(self._hello_hours, now):
            return "", f"不在 {gates.night_window_text(self._hello_hours)} 之内，到点再问"
        if not party.get("active"):
            return "", "这位达人已经停用（不在达人名单里）"
        if self._mode(deal, now) != COPILOT:
            return "", "现在不在副驾驶模式（人工接管或暂停）"
        chat, _ = self._reach(party, made=True, deal=deal)     # (where ``_ask_signed_hello`` would ask)
        if chat is None:
            return "", UNREACHABLE_TEXT
        if self._relay.store.latest_open(chat, drafts_only=True, system=False) is not None:
            return "", "她的草稿正在写"
        if any((p.get("meta") or {}).get("lane") not in (LANE_SYSTEM, LANE_BUYIN)
               for p in self._store.deal_packets(deal["id"], statuses=(PENDING, APPROVED, SENDING))):
            return "", "「待审批」里已经有她的草稿"
        return "", ""

    def _ask_signed_hello(self, row: dict, deal: dict, party: dict, now: float) -> int:
        """The draft job of one 签收后问一句 (``meta.signed``: the order and its signature, MM-DD HH:MM; told what we sent
        her, ``meta.sent``, what an operator wrote to her by hand, ``meta.history``, her 三方群's and the deal's 起草备注,
        ``meta.note``; in the night window or 自动模式 ``meta.night``: its draft goes by those rules, ``_night_judge``),
        to her private chat or the 老板拉的群 she is reached in (``_reach``); returns its id."""
        chat, group, reach = self._reaching(party, made=True, deal=deal)   # (PRM_REACH_LAST_SPOKE: where her side spoke)
        signed = {"order": row["order_no"], "at": time.strftime("%m-%d %H:%M", time.localtime(float(row["signed_at"])))}
        meta = {"party_id": party["id"], "deal_id": deal["id"], "name": party["name"], SIGNED: signed}
        if group is not None:
            meta.update(channel=GROUP_CHANNEL, group=self.boss_groups.job_group(chat), **reach_meta(reach))
        else:
            told = self._group_lines(deal["id"])
            if told:
                meta["three_party_chat"] = told         # what was said in her 三方群 lately
        made = self.three_party.job_meta(deal)
        if made:
            meta["three_party"] = made
        creator = self._creator_ids(deal["id"])
        if creator:
            meta["creator"] = creator
        outbound = self._outbound(deal["id"], party["id"], now)
        if outbound:
            meta["sent"] = outbound                    # (the Mac sees whether we asked her already)
        told = history_of(self._store.backlog(deal["id"]))
        if told:
            meta["history"] = told                     # told, not taken: her next draft job still hears of it
        if self._night_active(now) and not (group is not None and self._hand_hold(chat)):
            meta["night"] = self._night_meta(now)      # the night window / 自动模式: its draft may go out by itself
            # (not in a group linked by hand that always waits: ``_hand_hold``)
        _, job_id = self._relay.submit(chat, chat, "", kind=PRM, meta=self._reply_meta(self._with_note(meta, deal, now)),
                                       fresh=True)
        self._store.add_event(deal["id"], party["id"], SIGNED, {"job": job_id, "order": row["order_no"]},
                              actor="system", now=now)
        self._store.drop_notices(deal["id"], lambda n: bool(n.get(SIGNED)), now)   # one not written before: on its way
        logger.info("PRM: sample order %s signed for: job #%d asks party %d how the samples suit her", row["order_no"],
                    job_id, party["id"])
        return job_id

    def _sample_missing(self, number: str, now: float) -> None:
        """An order 聚水潭 did not answer for: after SAMPLE_MISSES rounds in a row its deal says so."""
        row = self._store.sample_order(number)
        if row is None or row["done"]:
            return
        misses = int(row["misses"] or 0) + 1
        self._store.update_sample_order(number, now, misses=misses, checked_at=now)
        if misses >= SAMPLE_MISSES:
            self._store.set_notice(row["deal_id"], f"sample:{number}", (
                f"聚水潭里查不到寄样单 {number}（连续 {misses} 次）：可能被删掉了，或者不在这台 Mac 登录的子账号下，"
                "请到聚水潭寄样管理核对"), now)

    def _sample_round_end(self, stopped: Optional[dict], now: float) -> None:
        """How the round went.  Through: what a round that could not be made said goes (``sample_sync``).  Stopped by
        a login lapsed (77), a risk wall (76: the next round waits SAMPLE_RISK_PAUSE_SEC, two intervals at least),
        another sub-account (79), a CLI that is not there or too old, a worker too old: every deal with an order
        still asked about says so at once (``SAMPLE_PROBLEMS``), once (the same words are not written again); a
        hiccup (no answer, the lock busy) only after SAMPLE_ERRORS rounds in a row."""
        if stopped is None:
            if self._sample_errors or self._sample_said:
                logger.info("PRM: the sample orders' status came through again")
            self._sample_errors, self._sample_said = 0, ""
            for deal in self._store.deals_with_notice("sample_sync"):
                self._store.edit_notices(deal["id"], lambda notices: [n for n in notices if n["key"] != "sample_sync"],
                                         now, touch=False)
            return
        verdict, message = stopped.get("verdict") or "error", str(stopped.get("message") or "")
        pause = 0
        if verdict == "wall":
            pause = max(2 * self._sample_sync_sec, SAMPLE_RISK_PAUSE_SEC)
            self._store.set_meta_value("sample_sync_pause_until", now + pause)
        if verdict in SAMPLE_PROBLEMS and verdict != "error":
            text = SAMPLE_PROBLEMS[verdict].format(pause=pause // 60, message=message[:120])
        else:
            self._sample_errors += 1
            if self._sample_errors < SAMPLE_ERRORS:
                logger.warning("PRM: the sample orders' status did not come through (%s: %s); asked again next round",
                               verdict, message[:200])
                return
            text = SAMPLE_PROBLEMS["error"].format(count=self._sample_errors, message=message[:120])
        if text != self._sample_said:
            logger.warning("PRM: the sample orders' status is not synced: %s", text)
            self._sample_said = text
        for deal_id in sorted({row["deal_id"] for row in self._store.sample_orders(live=True)}):
            # (about the Mac and 聚水潭, not the deal: no sign of life for the 8.2 clean-up)
            self._store.set_notice(deal_id, "sample_sync", text, now, touch=False)

    def _drop_dead_samples(self, deal_id: int, now: float) -> None:
        """A deleted order's notice is seen to: a new order of the deal was verified, or an operator changed the
        deal's stage or shipment in the console."""
        dead = {f"sample:{row['order_no']}" for row in self._store.sample_orders(live=False, deal_id=deal_id)
                if row["done"] == "deleted"}
        if dead:
            self._store.drop_notices(deal_id, lambda n: n["key"] in dead, now)

    # ── 抖音百应私信（PRM_BUYIN_SYNC_MIN） ─────────────────────────────────

    def sweep_buyin(self) -> Optional[int]:
        """PRM_BUYIN_SYNC_MIN (0 = off, the default): once an interval, one job asks the Mac for the new 抖音百应 private
        messages (``meta.op = BUYIN_OP``, the cursor of the last answer): the worker runs `dy buyin im-inbox` without
        the model (read-only) and the answer comes back to ``on_result`` (``_on_buyin_inbox``).  One job at a time (the
        last one still queued or running: none, so a Mac that is away gets no pile of them); when the last round was is
        kept in the database (a restart does not ask again at once); after a risk wall the next round waits longer.
        Returns the job id when one was queued.  Errors are logged (the sender thread goes on)."""
        if self._buyin_sec <= 0:
            return None
        now = self._clock()
        try:
            with self._buyin_lock:
                if now < self._buyin_due(now) or self._relay.store.latest_open(BUYIN_SYNC_CHAT) is not None:
                    return None
                self._store.set_meta_value("buyin_sync_at", now)
                cursor = platforms.im_cursor(_json_value(self._store.meta_value("buyin_cursor")))
                _, job_id = self._relay.submit(BUYIN_SYNC_CHAT, BUYIN_SYNC_CHAT, "读抖音百应私信（精选联盟）", kind=PRM,
                                               meta={"op": BUYIN_OP, "cursor": cursor, "limit": platforms.IM_LIMIT},
                                               fresh=True, input_closed=True)
        except Exception:
            logger.exception("PRM: the 百应 private messages could not be asked for")
            return None
        logger.info("PRM: job #%d asks for the new 百应 private messages", job_id)
        return job_id

    def _buyin_due(self, now: float) -> float:
        """When the next 百应 round is due: an interval after the last one (kept in the database), not before a risk
        wall's pause ends; a bot that never asked: BUYIN_FIRST_SEC after its start."""
        last = _float(self._store.meta_value("buyin_sync_at"))
        if last is None:
            return self._started + BUYIN_FIRST_SEC
        pause = _float(self._store.meta_value("buyin_pause_until")) or 0.0
        return max(min(last, now) + self._buyin_sec, pause)

    def _on_buyin_inbox(self, job: dict, meta: dict) -> bool:
        """The Mac's answer to a 百应 inbox job (``platforms.check_im_result``): each conversation
        (``_buyin_conversation``), the cursor for the next round (only when the round went through), how the round
        went (``_buyin_round_end``); the conversations of nobody on the list quiet for BUYIN_UNMATCHED_DAYS are
        forgotten.  A job lost or cancelled waits for the next round (the cursor stays: it reads the same again); one
        that failed is a failed round.  Whatever the status, the job then keeps only a summary of its answer (the
        creators' words as they wrote them were in it, nobody's on the list too; a job cancelled while the Mac was
        finishing it has its full answer: review 2026-10-29).  A conversation that could not be taken in (its draft
        job could not be queued, say) is not marked seen and the cursor stays, so the next round has it again."""
        status = job["status"]
        if status not in ("done", "failed"):
            self._relay.store.replace_result(job["id"], json.dumps(
                {"op": BUYIN_OP, "summary": None, "status": status}, ensure_ascii=False))
            return True
        now = self._clock()
        result, stopped = None, None
        if status == "done":
            try:
                result = platforms.check_im_result(json.loads(job.get("result_text") or ""))
            except (ValueError, platforms.ActionError):
                stopped = {"verdict": "error", "exit_code": None, "message": "Mac 交回的百应私信读不出来"}
        else:
            why = _job_error(job)
            stopped = {"verdict": "old_worker" if "达人编号" in why else "error", "exit_code": None, "message": why}
        summary = {"conversations": 0, "messages": 0, "drafts": 0, "recorded": 0, "unmatched": 0, "failed": 0,
                   "skipped": 0, "invited": 0, "contacts": 0, "others_contacts": 0, "typed_contacts": 0}
        why = ""
        with self._buyin_lock:
            if result is not None:
                index = self._buyin_index()
                for conv in result["conversations"]:
                    cid = conv["conversation_id"]
                    try:
                        self._buyin_conversation(conv, index, now, summary)
                        self._buyin_conv_errors.pop(cid, None)
                    except Exception as e:             # one conversation's trouble does not stop the others
                        tries = self._buyin_conv_errors.get(cid, 0) + 1
                        why = why or f"{type(e).__name__}: {e}"[:160]
                        if tries >= BUYIN_CONV_TRIES and self._buyin_give_up(conv, index, now, summary, why):
                            self._buyin_conv_errors.pop(cid, None)
                            logger.exception("PRM: a 百应 conversation failed %d rounds in a row; skipped (its messages "
                                             "count as seen, the deal says so)", tries)
                            continue
                        self._buyin_conv_errors[cid] = tries
                        summary["failed"] += 1
                        logger.exception("PRM: a 百应 conversation could not be taken in (%d round(s) in a row); read "
                                         "again next round", tries)
                stopped = result["stopped"]
                if stopped is None and not summary["failed"] and result["cursor"] is not None:
                    self._store.set_meta_value("buyin_cursor", json.dumps(result["cursor"], ensure_ascii=False))
            self._buyin_round_end(stopped, now, failed=summary["failed"], why=why)
            self._store.drop_buyin_threads(unmatched_before=now - BUYIN_UNMATCHED_DAYS * 86400)
        self._relay.store.replace_result(job["id"], json.dumps(
            {"op": BUYIN_OP, "summary": summary, "stopped": stopped}, ensure_ascii=False))
        if result is not None:
            logger.info("PRM: 百应 inbox #%d: %d conversation(s), %d new message(s), %d draft job(s), %d recorded only, "
                        "%d of nobody on the list, %d our invite(s); %d contact card(s) in ours, %d in others'; "
                        "%d line(s) of hers with a contact typed in, in ours",
                        job["id"], summary["conversations"], summary["messages"], summary["drafts"],
                        summary["recorded"], summary["unmatched"], summary["invited"], summary["contacts"],
                        summary["others_contacts"], summary["typed_contacts"])
        return True

    def _buyin_index(self) -> dict:
        """Who a 百应 creator is: her 抖音 UID and her 抖音号 (any case) -> the wxids of the registered creators (active)
        known by them: the 达人名单's 平台 UID and 抖音号 (``creator_directory``; not a 小红书 one) and the ids the deals'
        verified 抖店 sample orders found (``_creator_ids``).  Her nickname is never used."""
        parties = {p["wxid"]: p for p in self._store.parties()}
        index: dict = {"uid": {}, "aweme": {}, "parties": parties}

        def add(table: str, key, wxid: str) -> None:
            key = str(key or "").strip()
            if key:
                index[table].setdefault(key.lower() if table == "aweme" else key, set()).add(wxid)

        rows: list = []
        if self.creator_directory is not None:
            try:
                rows = list(self.creator_directory() or [])
            except Exception:
                logger.exception("PRM: the creator list could not be read for the 百应 conversations")
        for row in rows:
            wxid = str((row or {}).get("wxid") or "")
            if wxid not in parties or str(row.get("platform") or "").strip().upper() == "XHS":
                continue
            add("uid", row.get("platform_uid"), wxid)
            add("aweme", row.get("platform_no"), wxid)
        for wxid, party in parties.items():
            ids = self._creator_ids(self._store.deal_of(party["id"])["id"])
            add("uid", ids.get("uid"), wxid)
            for douyin in ids.get("douyin_ids") or []:
                add("aweme", douyin, wxid)
        return index

    def _buyin_party(self, creator: dict, index: dict) -> Optional[dict]:
        """The registered creator a 百应 conversation is (``_buyin_index``), or None: nobody, or more than one (never
        a guess)."""
        wxids = set(index["uid"].get(creator.get("uid") or "", ())) | set(
            index["aweme"].get(str(creator.get("aweme_id") or "").lower(), ()) if creator.get("aweme_id") else ())
        if len(wxids) > 1:
            logger.warning("PRM: a 百应 conversation fits %d registered creators; taken as nobody's", len(wxids))
        return index["parties"][next(iter(wxids))] if len(wxids) == 1 else None

    @staticmethod
    def _buyin_text(msg: dict) -> str:
        """One 百应 message as a line of the draft (a card or another kind says so)."""
        head = BUYIN_KINDS.get(msg.get("kind"), "")
        return " ".join(t for t in (head, str(msg.get("text") or "")) if t) or "[其他消息]"

    def _buyin_conversation(self, conv: dict, index: dict, now: float, summary: dict) -> None:
        """One conversation of a 百应 round (under ``_buyin_lock``): only the messages it has not seen (by id).  A
        registered creator's (``_buyin_party``): every new line, hers and ours (an operator in 百应: no takeover, it
        is no WeChat line), is recorded masked as a ``buyin_message`` event (once per message id), hers count as a
        sign of life for the 8.2 clean-up (the deal's ``updated_at``; never its ``last_message_at``: that is her
        latest WeChat line, the round of the WeChat holding reply, review 2026-10-29), and new lines of hers become a
        draft job (``_buyin_draft_job``).  The messages count as seen only once that is done (a job that could not be
        queued raises: they come again with the next round).  Nobody's on the list: no party is made (a wxid cannot
        be made up, and the 达人名单 would get it); only her nickname, how many of her lines wait since our last one
        and when, for the 待审批 page's line (none of her words is kept), and our invite when it is one.

        Who she is: her 抖音 UID / 抖音号 as this round or an earlier one gave them (the 百应 page itself gives neither,
        only her 百应 id: the Mac's invite ledger match gives her 抖音号 while our invite card is in what it read, and
        the conversation keeps it, with ``invited``, after the card has scrolled out).  Her contact cards (kind
        "contact") are recorded only masked (``buyin_contact_line``); in a conversation that is ours (our invite, or a
        registered creator's) each new one goes to ``on_buyin_contact`` with its values (``_buyin_contacts``), in any
        other only their number is kept (``others_contacts``).  So does each new line of hers in ours with a contact
        typed in it (``contact.first_contacts``: a 微信号 only after a label; source "buyin_text"); her lines are
        recorded with the 微信号 masked (``buyin_text_line``), the phone a 〈手机N〉 as on WeChat; her nickname has
        both masked."""
        cid = conv["conversation_id"]
        creator = conv["creator"]
        thread = self._store.buyin_thread(cid) or {}
        known = set(thread.get("seen") or [])
        new = [m for m in conv["messages"] if m["id"] not in known]
        summary["conversations"] += 1
        summary["messages"] += len(new)
        stamp = [(m, float(m["at"] or now)) for m in new]
        hers = [at for m, at in stamp if m["from"] == "creator"]
        ours = [at for m, at in stamp if m["from"] == "shop"]
        invited = conv.get("invited") or thread.get("invited") or None
        # somebody else's invite card on this shop (the Mac's ``invite_match``: a colleague's), kept once seen; ours wins
        other = invited is None and bool(conv.get("other_invite") or thread.get("other_invite"))
        fields = {"nickname": contact.mask_contacts(pii.mask_numbers(creator.get("nickname") or ""))
                  or thread.get("nickname") or "",
                  "creator_uid": creator.get("uid") or thread.get("creator_uid") or "",
                  "aweme_id": creator.get("aweme_id") or thread.get("aweme_id") or "",
                  "buyin_id": creator.get("buyin_id") or thread.get("buyin_id") or "", "invited": invited,
                  "other_invite": other,
                  "seen": [*(thread.get("seen") or []), *(m["id"] for m in new)][-BUYIN_SEEN:],
                  "last_creator_at": max([float(thread.get("last_creator_at") or 0), *hers]) or None,
                  "last_shop_at": max([float(thread.get("last_shop_at") or 0), *ours]) or None}
        party = self._buyin_party({"uid": fields["creator_uid"], "aweme_id": fields["aweme_id"]}, index)
        if conv.get("invited"):
            summary["invited"] += 1
        cards = [(m, at) for m, at in stamp if m["kind"] == "contact"]
        mine = party is not None or invited is not None
        # her own words with a contact typed in (only in ours: another person's conversation is never looked into)
        typed = [(m, at, found) for m, at in stamp if mine and m["from"] == "creator" and m["kind"] == "text"
                 for found in (contact.first_contacts(m.get("text")),) if found]
        added: dict = {}
        if cards and not mine:
            summary["others_contacts"] += len(cards)    # somebody else's conversation: not ours to handle
            fields["contacts"] = int(thread.get("contacts") or 0) + len(cards)
        elif cards or typed:
            summary["contacts"] += len(cards)
            summary["typed_contacts"] += len(typed)
            added = self._buyin_contacts(cid, cards, fields, party, invited, typed=typed)
        if party is None:
            waiting = int(thread.get("unanswered") or 0) if thread.get("party_id") is None else 0
            for m, _ in sorted(stamp, key=lambda pair: pair[1]):
                waiting = waiting + 1 if m["from"] == "creator" else 0
            self._store.save_buyin_thread(cid, now, party_id=None, unanswered=waiting, **fields)
            if waiting:
                summary["unmatched"] += 1
            return
        deal = self._store.deal_of(party["id"], now)
        recorded = {d.get("id") for d in self._buyin_lines(deal["id"], cid)}
        for m, at in sorted(stamp, key=lambda pair: pair[1]):
            if m["id"] in recorded:
                continue                               # (recorded by a round whose job could not be queued)
            if m["kind"] == "contact":                 # masked here, never through the pii tokens (they keep values)
                text = f"[{_clock_text(at)}] {buyin_contact_line(m.get('contact'), added.get(m['id']))}"
            else:                                      # her phone a 〈手机N〉 (as on WeChat), a 微信号 masked
                text = buyin_text_line(self._mask(party["id"], f"[{_clock_text(at)}] {self._buyin_text(m)}", now),
                                       added.get(m["id"]))
            self._store.add_event(deal["id"], party["id"], "buyin_message", {
                "conversation": cid, "id": m["id"], "from": m["from"], "kind": m["kind"], "text": text, "at": at},
                actor="creator" if m["from"] == "creator" else "operator", now=now)
        if hers:
            self._store.update_deal(deal["id"], now)   # a sign of life (updated_at), not her WeChat round
        answered = max(float(fields["last_shop_at"] or 0), float(thread.get("answered_upto") or 0))
        if hers:
            self._buyin_draft_job(party, deal, cid, now, summary, answered=answered, nickname=fields["nickname"],
                                  aweme=fields["aweme_id"])
        self._store.save_buyin_thread(cid, now, party_id=party["id"], deal_id=deal["id"], unanswered=0, **fields)
        self._buyin_notices_answered(deal["id"], cid, answered, now)

    def _buyin_contacts(self, cid: str, cards: list, fields: dict, party: Optional[dict],
                        invited: Optional[dict], typed: tuple | list = ()) -> dict:
        """Tell ``on_buyin_contact`` (when it is set) of each new contact card ``cards`` (``[(message, at)]``) of a 百应
        conversation that is ours, and of each new line of hers with a contact typed in (``typed``: ``[(message, at,
        {"wechat"?, "phone"?})]``, ``contact.first_contacts``), oldest first, once per message the conversation had not
        seen (calling it again for the same one, after a round that could not be taken in, is for the hook to bear).
        The event::

            {"source": "buyin_card" | "buyin_text", "conversation_id", "message_id", "at",
             "creator": {"aweme_id", "uid": <her 百应 id or "">, "nickname": <numbers masked>},
             "contact": {"wechat": <full or "">, "phone": <full or "">, "unavailable": bool, "why": <str or "">},
             "party_id": <the registered creator's id or None>, "invited": <our invite or None>}

        Its answer ``{"id": int, "state", "masked"}`` gives the id the masked line names (message id -> id); a hook
        that raises is logged without any value and the round goes on."""
        hook = getattr(self, "on_buyin_contact", None)
        if not callable(hook):
            return {}
        told = []
        for m, at in cards:
            card = m.get("contact") if isinstance(m.get("contact"), dict) else {"unavailable": True, "why": "unknown"}
            unavailable = bool(card.get("unavailable")) or not (card.get("wechat") or card.get("phone"))
            told.append((at, m, "buyin_card", {"wechat": "" if unavailable else str(card.get("wechat") or ""),
                                               "phone": "" if unavailable else str(card.get("phone") or ""),
                                               "unavailable": unavailable,
                                               "why": str(card.get("why") or "unknown") if unavailable else ""}))
        for m, at, found in typed:
            told.append((at, m, "buyin_text", {"wechat": str(found.get("wechat") or ""),
                                               "phone": str(found.get("phone") or ""), "unavailable": False, "why": ""}))
        out = {}
        for at, m, source, values in sorted(told, key=lambda item: item[0]):
            event = {"source": source, "conversation_id": cid, "message_id": m["id"], "at": at,
                     "creator": {"aweme_id": fields["aweme_id"], "uid": fields["buyin_id"],
                                 "nickname": fields["nickname"]},
                     "contact": values, "party_id": party["id"] if party is not None else None,
                     "invited": dict(invited) if invited else None}
            try:
                answer = hook(event)
            except Exception as e:                     # never the values: the event holds them, the message may too
                logger.warning("PRM: on_buyin_contact failed on a 百应 %s (%s); the round goes on",
                               "contact card" if source == "buyin_card" else "line with a contact typed in",
                               type(e).__name__)
                continue
            if isinstance(answer, dict) and isinstance(answer.get("id"), int) and not isinstance(answer["id"], bool):
                out[m["id"]] = answer["id"]
        return out

    def _buyin_lines(self, deal_id: int, cid: str) -> list:
        """The conversation's lines the deal recorded (``buyin_message`` events: masked), oldest first."""
        rows = [dict(ev["detail"]) for ev in self._store.events(deal_id, limit=500, kinds=("buyin_message",))
                if ev["detail"].get("conversation") == cid]
        return sorted(rows, key=lambda d: float(d.get("at") or 0))

    def _buyin_conversations_of(self, deal_id: int) -> set:
        """The 百应 conversations a deal had (its threads, and those its recorded lines name)."""
        named = {str(ev["detail"].get("conversation") or "")
                 for ev in self._store.events(deal_id, limit=None, kinds=("buyin_message",))}
        return {c for c in named | {t["conversation_id"] for t in self._store.buyin_threads(deal_id=deal_id)} if c}

    def _buyin_draft_job(self, party: dict, deal: dict, cid: str, now: float, summary: dict, *, answered: float,
                         nickname: str = "", aweme: str = "") -> Optional[int]:
        """The draft job of a registered creator's 百应 lines not answered yet (under ``_buyin_lock``): after
        ``answered`` (our last line in that conversation, or the lines of a 百应 draft an operator decided on).  Those
        not older than BUYIN_MAX_AGE_SEC go as her message, the conversation before them as its history.  Its own chat
        (``BUYIN_CHAT`` + the conversation id): no WeChat job takes them in nor they a WeChat line; a queued job of the
        same conversation is cancelled (its lines are in this one).  Nothing is lost silently (review 2026-10-29): her
        lines read too late have the deal's notice ``buyin_old:<conversation>``; while a person has the deal (人工接管 /
        暂停, a WeChat takeover included) they are not drafted and ``buyin_held:<conversation>`` says so, until the deal
        is back in copilot mode (``sweep_buyin_held`` drafts them then) or they are answered (``_buyin_notices_answered``).
        Like a WeChat draft its meta has her 抖音号 (``creator``: ``_creator_ids`` and the conversation's ``aweme``) for
        the Mac's invite background (worker ``invite_block``), and the deal's 起草备注 (``note``, ``_with_note``).  None
        when no job was made."""
        lines = self._buyin_lines(deal["id"], cid)
        waiting = [d for d in lines if d.get("from") == "creator" and float(d.get("at") or 0) > answered]
        if not waiting:
            summary["recorded"] += 1
            return None
        hers = [d for d in waiting if now - float(d.get("at") or 0) <= BUYIN_MAX_AGE_SEC]
        stale = [d for d in waiting if d not in hers]
        if stale:
            self._store.set_notice(deal["id"], f"buyin_old:{cid}", (
                f"她在百应私信里有 {len(stale)} 条消息读到时已经过了 {BUYIN_MAX_AGE_SEC // 3600} 小时（最早 "
                f"{_clock_text(min(float(d['at']) for d in stale))}），没有起草：请到百应里看、在百应里回"), now,
                conversation=cid, upto=max(float(d["at"]) for d in stale))
        mode = self._mode(deal, now)
        if mode != COPILOT:
            summary["recorded"] += 1
            self._buyin_hold(deal["id"], cid, waiting, mode, now)
            logger.info("PRM: party %d wrote in 百应 while the deal is %s; recorded, not drafted (the deal says so)",
                        party["id"], mode)
            return None
        if not hers:
            summary["recorded"] += 1
            return None
        ids = {d.get("id") for d in hers}
        upto = max(float(d.get("at") or 0) for d in hers)
        history = [{"who": "operator" if d.get("from") == "shop" else "creator", "text": str(d.get("text") or "")}
                   for d in lines if d.get("id") not in ids and float(d.get("at") or 0) <= upto][-BUYIN_HISTORY:]
        creator = self._creator_ids(deal["id"])
        aweme = str(aweme or "").strip()
        if (aweme and platforms.creator_account_ok(aweme)
                and aweme.lower() not in {d.lower() for d in creator.get("douyin_ids") or []}):
            creator = {"uid": "", **creator, "douyin_ids": [*(creator.get("douyin_ids") or []), aweme][:5]}
        meta = {"party_id": party["id"], "deal_id": deal["id"], "name": party["name"], "channel": BUYIN_CHANNEL,
                "conversation": cid, "buyin_name": nickname, "upto": upto, "lines": len(hers),
                **({"creator": creator} if creator else {}), **({"history": history} if history else {})}
        chat = BUYIN_CHAT + cid
        older = self._relay.store.latest_open(chat, drafts_only=True)
        if older is not None and older["status"] == "pending":
            self._relay.store.cancel_if_pending(older["id"], now)
        _, job_id = self._relay.submit(chat, chat, "\n".join(str(d.get("text") or "") for d in hers), kind=PRM,
                                       meta=self._with_note(meta, deal, now), fresh=True, input_closed=True)
        self._store.drop_notices(deal["id"], lambda n: n["key"] == f"buyin_held:{cid}", now)
        summary["drafts"] += 1
        logger.info("PRM: party %d's %d new 百应 line(s) -> job #%d", party["id"], len(hers), job_id)
        return job_id

    def _buyin_held_text(self, notice: dict, mode: str, active: bool) -> str:
        """What a ``buyin_held`` notice says, honestly for now: while a person has the deal she will be drafted for
        after it; with the switch off, or the creator off the list, nothing will be (re-enabling her, or the switch,
        drafts it then)."""
        count = int(notice.get("count") or 1)
        earliest = _clock_text(float(notice.get("earliest") or notice.get("at") or 0))
        said = f"她在百应私信里发了 {count} 条消息（最早 {earliest}），没有起草："
        if self._buyin_sec <= 0:
            return said + "百应私信的起草已经关了（PRM_BUYIN_SYNC_MIN=0），请直接到百应里回"
        if not active:
            return said + "这位达人已停用，不会起草（重新启用后会起草一份），请直接到百应里回"
        if mode != COPILOT:
            return f"{MODES.get(mode, mode)}期间{said}恢复副驾驶后会起草一份，急的话直接到百应里回"
        return said + "马上起草一份"

    def _buyin_hold(self, deal_id: int, cid: str, waiting: list, mode: str, now: float) -> None:
        """Her 百应 lines ``waiting`` are not drafted because a person has the deal: its notice ``buyin_held:<cid>``
        (how many, the earliest, ``upto`` the newest: ``_buyin_notices_answered``), drafted once the deal is back in
        copilot mode (``sweep_buyin_held``)."""
        if not waiting:
            return
        data = {"conversation": cid, "count": len(waiting), "earliest": min(float(d["at"]) for d in waiting),
                "upto": max(float(d["at"]) for d in waiting)}
        self._store.set_notice(deal_id, f"buyin_held:{cid}", self._buyin_held_text(data, mode, True), now, **data)

    def _buyin_give_up(self, conv: dict, index: dict, now: float, summary: dict, why: str) -> bool:
        """A conversation that failed BUYIN_CONV_TRIES rounds in a row (``_on_buyin_inbox``): its new messages count as
        seen, so the cursor moves on and the others are not crowded out, and a registered creator's deal says how many
        of her lines were skipped (``buyin_skipped:<conversation>``, until she is answered); nothing of it is drafted.
        False when even that could not be done (the round counts it as failed then)."""
        cid = conv["conversation_id"]
        try:
            thread = self._store.buyin_thread(cid) or {}
            known = set(thread.get("seen") or [])
            new = [m for m in conv["messages"] if m["id"] not in known]
            self._store.save_buyin_thread(cid, now, seen=[*(thread.get("seen") or []), *(m["id"] for m in new)][-BUYIN_SEEN:])
            summary["skipped"] += 1
            hers = [float(m["at"] or now) for m in new if m["from"] == "creator"]
            try:
                party = self._buyin_party({"uid": conv["creator"].get("uid") or thread.get("creator_uid") or "",
                                           "aweme_id": conv["creator"].get("aweme_id") or thread.get("aweme_id") or ""},
                                          index)
            except Exception:
                party = None
            if party is not None and hers:
                deal = self._store.deal_of(party["id"], now)
                self._store.set_notice(deal["id"], f"buyin_skipped:{cid}", (
                    f"她在百应私信里有 {len(hers)} 条消息（最早 {_clock_text(min(hers))}），机器人连续 {BUYIN_CONV_TRIES} 轮"
                    f"都处理不了（{why[:80]}），跳过了、没有起草：请到百应里看、在百应里回"), now, conversation=cid,
                    upto=max(hers))
            return True
        except Exception:
            logger.exception("PRM: a 百应 conversation could not even be skipped")
            return False

    def _buyin_notices_answered(self, deal_id: int, cid: str, answered: float, now: float) -> None:
        """Her 百应 lines up to ``answered`` are answered (our line in 百应, an operator's decision on a 百应 draft): the
        deal's notices of that conversation's lines held back, read too late or skipped that they cover go."""
        keys = (f"buyin_held:{cid}", f"buyin_old:{cid}", f"buyin_skipped:{cid}")
        self._store.drop_notices(deal_id, lambda n: n["key"] in keys and float(n.get("upto") or 0) <= answered, now)

    def sweep_buyin_held(self) -> int:
        """Her 百应 lines held back while a person had the deal (``buyin_held:<conversation>``): once the deal is back in
        copilot mode (the takeover ran out, an operator switched it back), she is on the list and the switch is on, one
        draft job of the lines still not answered (``_buyin_draft_job``); nothing left to draft (answered meanwhile, or
        all too old: their own notice), the notice goes.  Otherwise the notice is kept and says why nothing is drafted
        (``_buyin_held_text``: a person has the deal, she is off the list, the switch is off; ``touch=False``).  A
        conversation whose job cannot be queued is tried again every round, its failure logged once BUYIN_LOG_EVERY_SEC.
        Returns how many jobs were queued."""
        now = self._clock()
        asked = 0
        for deal in self._store.deals_with_notice_prefix("buyin_held:"):
            party = self._store.party(deal["party_id"])
            active = bool(party and party.get("active"))
            mode = self._mode(deal, now)
            for notice in [n for n in deal["notices"] if str(n["key"]).startswith("buyin_held:")]:
                cid = str(notice.get("conversation") or notice["key"].split(":", 1)[1])
                if self._buyin_sec <= 0 or not active or mode != COPILOT:
                    text = self._buyin_held_text(notice, mode, active)
                    if text != notice.get("text"):
                        self._store.set_notice(deal["id"], notice["key"], text, now, touch=False)
                    continue
                try:
                    with self._buyin_lock:
                        thread = self._store.buyin_thread(cid) or {}
                        answered = max(float(thread.get("last_shop_at") or 0), float(thread.get("answered_upto") or 0))
                        summary = {"drafts": 0, "recorded": 0}
                        job_id = self._buyin_draft_job(party, self._store.deal(deal["id"]) or deal, cid, now, summary,
                                                       answered=answered, nickname=thread.get("nickname") or "",
                                                       aweme=thread.get("aweme_id") or "")
                        if job_id is None:
                            self._store.drop_notices(deal["id"], lambda n: n["key"] == notice["key"], now)
                        else:
                            asked += 1
                        self._buyin_held_logged.pop(notice["key"], None)
                except Exception:
                    last = self._buyin_held_logged.get(notice["key"])
                    if last is None or now - last >= BUYIN_LOG_EVERY_SEC:
                        self._buyin_held_logged[notice["key"]] = now
                        logger.exception("PRM: the 百应 lines held back in deal %d could not be drafted; tried again "
                                         "every round (said again in %d min if it goes on)", deal["id"],
                                         BUYIN_LOG_EVERY_SEC // 60)
        if asked:
            logger.info("PRM: %d 百应 conversation(s) held back while a person had the deal: drafted now", asked)
        return asked

    def _buyin_scrub_jobs(self) -> int:
        """A 百应 inbox job handled already that still holds the Mac's answer as it came (a bot from before this
        rule, a job cut off): only a summary stays (the clean-up after any slip; ``purge_due`` and ``start``).  A job
        not handed to ``on_result`` yet is left alone.  Returns how many."""
        done = 0
        for job in self._relay.store.recent(BUYIN_SYNC_CHAT, limit=500):
            if job["status"] in ("pending", "running") or not job.get("delivered"):
                continue
            try:
                value = json.loads(job.get("result_text") or "")
            except ValueError:
                value = None
            if isinstance(value, dict) and "conversations" in value:
                self._relay.store.replace_result(job["id"], json.dumps(
                    {"op": BUYIN_OP, "summary": None, "status": job["status"]}, ensure_ascii=False))
                done += 1
        if done:
            logger.info("PRM: %d 百应 inbox job(s) still held the answer as it came; only a summary is kept now", done)
        return done

    def _buyin_round_end(self, stopped: Optional[dict], now: float, failed: int = 0, why: str = "") -> None:
        """How the 百应 round went (``buyin_status`` in the database, what the 待审批 page says).  Through: nothing to
        say.  Stopped by a login lapsed (77), a risk wall (76: the next round waits BUYIN_RISK_PAUSE_SEC, two intervals
        at least), another shop (79), a CLI without the command or too old, a worker too old: said at once
        (``BUYIN_PROBLEMS``); a hiccup (no answer, the lock busy) only after BUYIN_ERRORS rounds in a row.  The next
        round comes after the interval as ever (a CLI that has no reader yet is asked once an interval, no more).  A round
        that went through but had ``failed`` conversations (not taken in, read again next round): said from the second
        such round in a row (``BUYIN_PROBLEMS["conversations"]``), until a round has none (one failing BUYIN_CONV_TRIES
        rounds is skipped: ``_buyin_give_up``)."""
        if stopped is None and failed:
            self._buyin_errors = 0
            self._buyin_fail_rounds += 1
            if self._buyin_fail_rounds < 2:
                logger.warning("PRM: %d 百应 conversation(s) not taken in (%s); read again next round", failed, why)
                return
            stopped = {"verdict": "conversations", "message": why}
        elif stopped is None:
            self._buyin_fail_rounds = 0
            if self._buyin_errors or self._store.meta_value("buyin_status"):
                logger.info("PRM: the 百应 private messages came through again")
            self._buyin_errors = 0
            self._store.set_meta_value("buyin_status", "")
            return
        verdict, message = stopped.get("verdict") or "error", str(stopped.get("message") or "")
        pause = 0
        if verdict == "wall":
            pause = max(2 * self._buyin_sec, BUYIN_RISK_PAUSE_SEC)
            self._store.set_meta_value("buyin_pause_until", now + pause)
        if verdict == "conversations":
            text = BUYIN_PROBLEMS["conversations"].format(count=failed, rounds=self._buyin_fail_rounds,
                                                          message=message[:120], tries=BUYIN_CONV_TRIES)
        elif verdict in BUYIN_PROBLEMS and verdict != "error":
            text = BUYIN_PROBLEMS[verdict].format(pause=pause // 60, message=message[:160])
        else:
            self._buyin_errors += 1
            if self._buyin_errors < BUYIN_ERRORS:
                logger.warning("PRM: the 百应 private messages did not come through (%s: %s); asked again next round",
                               verdict, message[:200])
                return
            text = BUYIN_PROBLEMS["error"].format(count=self._buyin_errors, message=message[:160])
        old = _json_value(self._store.meta_value("buyin_status"))
        if not isinstance(old, dict) or old.get("text") != text:
            logger.warning("PRM: the 百应 private messages are not read: %s", text)
            self._store.set_meta_value("buyin_status", json.dumps({"verdict": verdict, "text": text, "at": now},
                                                                  ensure_ascii=False))

    def _on_buyin_draft(self, job: dict, meta: dict, deal: dict, now: float) -> bool:
        """A 百应 draft job ended (``meta.channel == BUYIN_CHANNEL``).  Its draft is a packet an operator copies and sends
        in 百应 by hand (``BUYIN_TOOL``, chat ``BUYIN_CHAT`` + the conversation): always waiting for a person (no gate:
        ``deals.route_for`` is never asked), no files, no platform action (one the draft names is left out, unrun),
        a newer 百应 draft of the same conversation supersedes it and only it (never a WeChat draft, nor the other way).
        A person has the deal (人工接管 / 暂停): superseded, like every draft then.  A job that gave no draft: the deal
        says so (``buyin:<conversation>``) until the next 百应 draft of that conversation or a decision; nothing of it
        goes into a WeChat draft job (the WeChat ``failed:`` notices are not used)."""
        party_id, deal_id = int(meta["party_id"]), int(deal["id"])
        cid = str(meta.get("conversation") or "")
        key = f"buyin:{cid}"
        status = job["status"]
        if status == "cancelled":
            return True                                # (a newer one of the same conversation has its lines)
        if self._store.packet_for_job(job["id"]) is not None:
            return True                                # already drafted (a repeated delivery)
        head = ""
        draft = None
        if status in ("failed", "lost"):
            head = "Mac 失联了" if status == "lost" else _job_error(job)
        else:
            try:
                draft = parse_draft(job.get("result_text") or "")
            except DraftError as e:
                head = f"草稿格式不对（{e.code}）"
        if draft is None:
            self._store.add_event(deal_id, party_id, "buyin_draft_failed", {"job": job["id"], "conversation": cid,
                                                                             "why": head[:200]}, actor="mac", now=now)
            self._store.set_notice(deal_id, key, f"#{job['id']} 百应私信的草稿没写成（{head[:120]}）：她在百应里的消息请直接"
                                   "到百应里回", now, conversation=cid)
            logger.warning("PRM: 百应 draft job #%d gave no draft: %s", job["id"], head[:200])
            return True
        draft = self._mask_draft(party_id, draft, now)
        notes = []
        if draft.platform is not None or draft.platform_refused:
            notes.append("百应私信的草稿不带平台动作：草稿里写的平台动作去掉了，平台上什么都不会做；回复要是说已经开好、建好了，发之前请核对")
        named = draft.attachments_named if draft.attachments_named is not None else draft.attachments_to_send
        if named or self._relay.store.outfiles(job["id"]):
            notes.append("百应私信不带附件：草稿要附的文件没有附上" + (f"（{'、'.join(named[:3])}）" if named else "")
                         + "，需要的话到百应里自己发")
        mode = self._mode(deal, now)
        newer = self._relay.store.latest_open(job["chat_id"], drafts_only=True)
        # a newer job of the conversation came back first (its draft is pending, or decided): this one has fewer of her
        # lines and never puts that one out of date (review 2026-10-29)
        drafted = any(p["chat_id"] == job["chat_id"] and int(p.get("job_id") or 0) > int(job["id"])
                      for p in self._store.deal_packets(deal_id, tools=(BUYIN_TOOL,)))
        if not draft.reply_draft:
            state = NO_REPLY
        elif mode != COPILOT:
            state = SUPERSEDED                         # a person has the deal (a WeChat takeover too): held back
        elif (newer is not None and newer["id"] > job["id"]) or drafted:
            state = SUPERSEDED                         # she wrote again in 百应: a newer draft is coming, or here
        else:
            state = PENDING
        if state == SUPERSEDED and mode != COPILOT:
            # a person took the deal while it was drafted (a WeChat takeover too): her lines wait for the end of it
            # (``sweep_buyin_held`` drafts them once), the deal says so (re-review 2026-10-29: nothing was left)
            with self._buyin_lock:
                thread = self._store.buyin_thread(cid) or {}
                answered = max(float(thread.get("last_shop_at") or 0), float(thread.get("answered_upto") or 0))
                self._buyin_hold(deal_id, cid, [d for d in self._buyin_lines(deal_id, cid) if d.get("from") == "creator"
                                                and float(d.get("at") or 0) > answered], mode, now)
        packet_meta = {"channel": BUYIN_CHANNEL, "lane": LANE_BUYIN, "conversation": cid,
                       "buyin_name": str(meta.get("buyin_name") or ""), "upto": _float(meta.get("upto")),
                       "lines": int(meta.get("lines") or 0), "said": str(job.get("text") or "")[:BUYIN_SAID_CHARS]}
        packet_id = self._store.add_packet(
            deal_id=deal_id, party_id=party_id, job_id=job["id"], chat_id=job["chat_id"], kind=draft.message_kind,
            text=draft.reply_draft, attachments=[], intent=draft.intent, slots=draft.slots,
            stage_proposal=draft.stage_proposal, needs_attention=draft.needs_attention or bool(notes),
            reason="；".join(r for r in (*notes, draft.reason) if r)[:500], status=state, route=gates.REVIEW,
            meta=packet_meta, tool=BUYIN_TOOL, platform=None, now=now)
        if state == PENDING:
            self._store.supersede(deal_id, now, keep=packet_id, lane=LANE_BUYIN, chat_id=job["chat_id"])
        elif state == NO_REPLY:
            self._buyin_answered(self._store.packet(packet_id) or {}, now)     # nothing to say to those lines
        # (she may ask for 专属链接 in 百应 too: the deal goes on with them as after a WeChat draft, review 2026-11-09 F2)
        slots = self._link_slots(deal, draft.slots, now)
        wish_before, wish = link_wish(deal.get("slots")), link_wish({**(deal.get("slots") or {}), **slots})
        self._store.update_deal(deal_id, now, merge_slots=slots, stage_proposal=draft.stage_proposal)
        self._links_after_slots(deal, wish_before, wish, now)
        self._store.drop_notices(deal_id, lambda n: n["key"] == key, now)
        self._store.set_notice(deal_id, DRAFT_NOTES + LANE_BUYIN, "；".join(
            [*notes, *([draft.reason] if draft.needs_attention and draft.reason else [])]), now)
        logger.info("PRM: 百应 job #%d -> packet %d (%s, %s)", job["id"], packet_id, state, draft.message_kind)
        return True

    def _buyin_answered(self, packet: dict, now: float) -> None:
        """An operator decided on a 百应 draft (sent it in 百应 by hand, or not): her lines it answered are answered (the
        next draft of the conversation does not take them again)."""
        cid = str((packet.get("meta") or {}).get("conversation") or "")
        upto = _float((packet.get("meta") or {}).get("upto"))
        if not cid or upto is None:
            return
        with self._buyin_lock:
            thread = self._store.buyin_thread(cid)
            if thread is not None and float(thread.get("answered_upto") or 0) < upto:
                self._store.save_buyin_thread(cid, now, answered_upto=upto)
            if thread is not None:
                self._buyin_notices_answered(packet["deal_id"], cid, max(upto, float(thread.get("last_shop_at") or 0)),
                                             now)

    def _decide_buyin(self, packet: dict, action: str, text: Optional[str], now: float) -> dict:
        """An operator's choice on a 百应 draft (``decide``): copy (the reply with the full numbers back, for 百应: each
        copy is written down like 显示完整号码; ``text``: as edited, kept for the record) | manual_sent (the operator
        says it went out in 百应: sent, ``sent_by_hand``, ``text`` what went out when it was edited; no takeover, no
        gate, the bot sends nothing) | drop (不发).  The bot never sends it: send / edit / execute / wrong are refused."""
        if action not in ("copy", "manual_sent", "drop"):
            if action == "wrong":
                return {"ok": False, "error": "百应私信是运营在百应里手动发的，不能标为发错了（它也不算进微信的自动发送门槛）"}
            return {"ok": False, "error": "百应私信的草稿机器人发不了：点「复制」到百应里发给达人，发完点「已在百应发了」"}
        cid = str((packet.get("meta") or {}).get("conversation") or "")
        if action == "drop":
            if not self._store.move(packet["id"], (PENDING,), DROPPED, decided_at=now):
                return {"ok": False, "error": "这条草稿已经处理过了"}
            self._buyin_answered(packet, now)
            self._store.drop_notices(packet["deal_id"], lambda n: n["key"] == f"buyin:{cid}", now)
            return {"ok": True, "status": DROPPED}
        have = packet["final_text"] if packet.get("final_text") is not None else packet["text"]
        final = have if text is None else str(text).strip()
        if len(final) > MAX_DRAFT_CHARS:
            return {"ok": False, "error": f"最多 {MAX_DRAFT_CHARS} 个字"}
        if not final:
            return {"ok": False, "error": "没有要发的内容"}
        tokens = self._store.tokens(packet["party_id"])
        unknown = pii.unknown_tokens(final, tokens)
        if unknown:
            return {"ok": False, "error": f"草稿里有对不上的占位符：{'、'.join(unknown)}（号码可能已按规定清除）"}
        edited = int(final != packet["text"])
        if action == "copy":
            if packet["status"] != PENDING:
                return {"ok": False, "error": "这条草稿已经处理过了"}
            if final != have and not self._store.move(packet["id"], (PENDING,), PENDING, final_text=final,
                                                      edited=edited):
                return {"ok": False, "error": "这条草稿已经处理过了"}
            used = sorted({m.group(0) for m in pii.TOKEN.finditer(final)})
            self._store.add_event(packet["deal_id"], packet["party_id"], "buyin_copy",
                                  {"packet": packet["id"], "edited": bool(edited), "tokens": used}, actor="console",
                                  now=now)
            return {"ok": True, "status": PENDING, "text": pii.unmask(final, tokens)}
        # manual_sent: also for a draft a newer one superseded a moment ago (it was copied and sent before)
        if not self._store.move(packet["id"], (PENDING, SUPERSEDED), SENT, decided_at=now, sent_at=now,
                                final_text=final, edited=edited, outcome="sent_by_hand"):
            return {"ok": False, "error": "这条草稿已经处理过了"}
        self._buyin_answered(packet, now)
        self._store.drop_notices(packet["deal_id"], lambda n: n["key"] == f"buyin:{cid}", now)
        self._store.add_event(packet["deal_id"], packet["party_id"], "buyin_sent",
                              {"packet": packet["id"], "conversation": cid, "edited": bool(edited)}, actor="console",
                              now=now)
        logger.info("PRM: 百应 draft %d sent in 百应 by hand (an operator says)", packet["id"])
        return {"ok": True, "status": SENT}

    def _creator_ids(self, deal_id: int) -> dict:
        """The creator's 抖音号 (and 抖音 UID): ``{"douyin_ids", "uid"}`` ({} when none), told to the Mac with her draft
        jobs (``meta.creator``) so its preview of a 专属佣金 can look her up in the invite ledger
        (``platforms.invite_cover``), and read by the 百应 inbox to know her conversations (``_buyin_index``).  First
        those her deal's verified 抖店 sample orders found (``_sample_creator_ids``), then those an operator linked
        her to (``creator_links``: 加百应达人微信 or 登记达人's 抖音号; any case once, at most 5 in all).  ``uid`` stays
        the sample order's (聚水潭's platformUid: what a 专属佣金 is opened for); a link's 百应 conversation id is
        ``uid_im`` (only for the 百应 inbox, never for a 专属佣金: not known to be the same kind of id)."""
        douyin, uid = self._sample_creator_ids(deal_id)
        seen, uid_im = {d.lower() for d in douyin}, ""
        deal = self._store.deal(deal_id)
        for link in self._store.creator_links(deal["party_id"]) if deal else []:
            if link["platform"] != "dy":
                continue
            account = str(link["account"] or "").strip()
            if account and platforms.creator_account_ok(account) and account.lower() not in seen:
                douyin.append(account)
                seen.add(account.lower())
            if link["uid_source"] == "buyin_im" and link["uid"]:
                uid_im = uid_im or str(link["uid"])
        out = {"douyin_ids": douyin[:5], "uid": uid} if douyin or uid else {}
        if uid_im:
            out["uid_im"] = uid_im
        return out

    def _sample_creator_ids(self, deal_id: int) -> tuple:
        """``sample_creator_ids`` of this service's store."""
        return sample_creator_ids(self._store, deal_id)

    def _creator_ids_notice(self, deal_id: int, now: float) -> None:
        """``creator_ids_notice`` of this service's store."""
        creator_ids_notice(self._store, deal_id, now)

    def _waybill(self, packet: dict) -> str:
        """The system's record: the deal's latest waybill (a notice of an earlier one is refused)."""
        shipment = self._store.latest_shipment(packet["deal_id"])
        return shipment["waybill"] if shipment else ""

    def snapshot(self) -> dict:
        relay = self._relay.snapshot()
        return snapshot(self._store, running=True, retention_days=self._retention_days, now=self._clock(),
                        group_of=self.three_party.deal_out, photo_check=self._draft_context,
                        reach_of=self._reach_out if self._reach_last_spoke else None, extra={
            "mac_online": relay.get("mac_online", False),
            "worker_kinds": relay.get("worker_kinds", []),
            "can_send": self._send is not None, "can_send_files": self._send_file is not None,
            "takeover_min": self._takeover_sec // 60, "holding_min": self._holding_sec // 60,
            "retention_days": self._retention_days, "catchup_hours": self._catchup_sec // 3600,
            "clock_skew_sec": int(round(self._skew)), "sample_sync_min": self._sample_sync_sec // 60,
            "night": self._night_status(self._clock()), "auto_register": self._auto_register,
            "buyin_sync_min": self._buyin_sec // 60, "friend_add_status": self.friend_adds.status(),
            "auto_mode": self.auto_mode, "three_party": self.three_party.status(),
            "reply_parts": self._reply_parts, "boss_groups": self.boss_groups.status(),
            "opening_auto_min": self._opening_auto_sec // 60,
            "hand_samples_problem": self._hand_problem if self._sample_sync_sec > 0 else "",
            "handcards": self._handcards,
            "draft_context": self._draft_context,     # (PRM_DRAFT_CONTEXT: the 起草备注's 「对过图了」 hint, review C-8)
            "wechat_review": self.wechat_review.status(),
        })

    # ── the sender thread: approved drafts, holding replies, clean-ups ────

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("PRM: sending round failed")
            self._wake.wait(SEND_TICK_SEC)
            self._wake.clear()

    def tick(self) -> None:
        self.register_found()                          # PRM_AUTO_REGISTER: the new friends the scan queued
        self.boss_groups.sweep()                       # PRM_BOSS_GROUPS: the groups a boss made with us (when due)
        self.sweep_owed_redrafts()                     # 接手任何合作群: a big group's redraft, at most every 2 minutes
        self.sweep_executing()
        self.send_due_openings()
        self.send_approved()
        try:                                           # PRM_WECHAT_REVIEW: the drafts waiting told to the admin's chat
            self.wechat_review.sweep()
        except Exception:
            logger.exception("PRM: telling the admin's chat of the drafts failed; tried again next round")
        self.send_holding_replies()
        self.sweep_owed_steps()
        self.sweep_deliveries()
        self.sweep_sample_status()
        self.sweep_signed_hellos()                     # PRM_SIGNED_HELLO: the 签收后问一句 owed (inside its hours)
        self.sweep_buyin()
        self.sweep_buyin_held()
        now = self._clock()
        if now - self._last_retention >= RETENTION_CHECK_SEC:
            self._last_retention = now
            self.purge_due()

    def send_due_openings(self) -> int:
        """开场白没人处理就自动发 (PRM_OPENING_AUTO_MIN): each first line to a new friend whose time came (``meta.auto_at``,
        ``_opening_auto``) and that still waits with nobody deciding on it is approved as it is (route ``OPENING_ROUTE``;
        the deal records ``opening_auto``), for ``send_approved`` to send; not while a person has the chat (or it is
        paused), not one held for a person since (her recall: ``needs_attention``), not when a newer draft of hers is
        coming.  The setting 0: none (those marked before wait for a person).  None the first OPENING_AUTO_GRACE_SEC after
        a start: what she wrote while the bot was stopped is read back then and puts the waiting draft out of date.
        Returns how many were approved."""
        if self._opening_auto_sec <= 0 or self._clock() - self._started < OPENING_AUTO_GRACE_SEC:
            return 0
        went = 0
        for packet in reversed(self._store.packets((PENDING,), limit=200)):
            due = (packet.get("meta") or {}).get("auto_at")
            now = self._clock()
            if isinstance(due, bool) or not isinstance(due, (int, float)) or due > now or packet["needs_attention"]:
                continue
            if self._hand_hold(packet["chat_id"]):
                continue                               # (a group linked by hand: its drafts always wait for a person)
            deal = self._store.deal(packet["deal_id"])
            if deal is None or self._mode(deal, now) != COPILOT:
                continue                               # a person has the chat: it waits for them
            newer = self._relay.store.latest_open(packet["chat_id"], drafts_only=True, system=False)
            if newer is not None and packet["job_id"] is not None and newer["id"] > packet["job_id"]:
                continue                               # she wrote again: her new draft answers her (this one goes stale)
            if not self._store.move(packet["id"], (PENDING,), APPROVED, decided_at=now, route=OPENING_ROUTE):
                continue                               # a person decided on it meanwhile
            waited = int(max(0.0, now - float(packet["created_at"])) // 60)
            self._store.add_event(packet["deal_id"], packet["party_id"], "opening_auto",
                                  {"packet": packet["id"], "waited_min": waited}, actor="system", now=now)
            logger.info("PRM: packet %d (our first line to party %d) waited %d min with nobody deciding; it goes out by "
                        "itself (PRM_OPENING_AUTO_MIN)", packet["id"], packet["party_id"], waited)
            went += 1
        return went

    def send_approved(self) -> int:
        """Send every approved packet, oldest first; returns how many went out."""
        sent = 0
        for packet in reversed(self._store.packets((APPROVED,), limit=50)):
            now = self._clock()
            if is_buyin(packet):                       # (never approved; were one: the bot does not send it anywhere)
                self._store.move(packet["id"], (APPROVED,), PENDING, decided_at=None)
                continue
            if now - float(packet["decided_at"] or now) > APPROVED_EXPIRE_SEC:
                self._store.move(packet["id"], (APPROVED,), FAILED, outcome="expired", sent_at=now,
                                 error="批准后太久没发出去（机器人停过），没有发送；需要的话重新写一条")
                continue
            if cards_expired(packet, now):
                # 直播手卡 approved before their 双11 line's end date, sent after it (Beijing time): not sent at all (a
                # platform action's reply: the action itself was verified, the reply is for a person to send)
                error = handcards.EXPIRED_TEXT
                if packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL:
                    error = f"平台动作已经核验成功，但回复没发出去：{error}；请在微信里手动发给达人"
                if self._store.move(packet["id"], (APPROVED,), FAILED, outcome="failed", sent_at=now, error=error):
                    self._drop_cards(packet["attachments"])   # (out of date: never sent, her 称呼 is on them)
                logger.warning("PRM: packet %d: its 手卡 went out of date before it was sent; not sent", packet["id"])
                continue
            if not self._store.move(packet["id"], (APPROVED,), SENDING):
                continue                               # another round took it
            if self._closed_now(self._store.packet(packet["id"]), now):
                continue
            # (老板拉的群: one the bot started; re-review 2026-11-11 N1, N3: where it goes is where it was approved for,
            # else back to 待审批)
            aimed = self._aim(self._store.packet(packet["id"]), sending=True)
            if (aimed is not None and not self._moved_since_decided(aimed)
                    and not self._closed_now(aimed, now)                 # (re-aimed: only to a group drafted in now)
                    and not self._held_by_hand_group(aimed)):            # (... and by itself not into one by hand)
                sent += self._deliver(aimed)
        return sent

    def _held_by_hand_group(self, packet: dict) -> bool:
        """``send_approved``: a packet approved by itself (its kind's gate, the 下班时段 or 自动模式, 开场白自动发: ``route``)
        that goes to a group linked by hand whose drafts always wait (``_hand_hold``: one the bot started itself for her
        private chat and aimed at the group once she had none, ``_aim``) goes back to 待审批 for a person (``route``
        review; its 开场白自动发 time gone, ``meta.auto_held`` saying why).  True then: it is not sent."""
        if packet.get("route") not in (gates.AUTO, gates.NIGHT, OPENING_ROUTE):
            return False
        why = self._hand_hold(str(packet.get("chat_id") or ""))
        if not why:
            return False
        meta = {k: v for k, v in (packet.get("meta") or {}).items() if k != "auto_at"}
        if "auto_at" in (packet.get("meta") or {}):
            meta["auto_held"] = why[:200]
        if why in (GROUPS_REVIEW_HELD, PICK_UNCONFIRMED_HELD):
            meta["hold"] = why[:200]           # (its card says why, as a draft held when it was made: SHOWN_HOLDS)
        if not self._store.move(packet["id"], (APPROVED, SENDING), PENDING, decided_at=None, route=gates.REVIEW,
                                meta=meta):
            return False
        logger.info("PRM: packet %d (approved by itself: %s) goes to a group whose drafts wait for a person now (%s)",
                    packet["id"], packet["route"], "PRM_GROUPS_REVIEW" if why == GROUPS_REVIEW_HELD else
                    "its pick is not confirmed" if why == PICK_UNCONFIRMED_HELD else "PRM_LINKED_GROUPS_AUTO off")
        return True

    def _cut_off(self, packet: dict, why: str, parts: tuple, files: tuple) -> int:
        """``_deliver``: the group closed for her while her reply went out (``_group_closed``): nothing more is sent.
        Nothing out yet: as ``_fail_closed``.  Else the draft is not sent whole: the card says what went out and that
        the rest is not sent (never 「请手动补发」: nobody drafts in that group now, review 2026-11-09), a platform
        draft's deal says so (``platform:<id>``); no notice of a send that failed (GROUP_UNSENT)."""
        (texts, all_texts), (sent_files, all_files) = parts, files
        now = self._clock()
        if not texts and not sent_files:
            self._fail_closed(packet, why, now)
            logger.warning("PRM: packet %d: its group is not drafted in for her any more; not sent", packet["id"])
            return 0
        went = []
        if all_texts:                                  # (one text message not out: no file went out either)
            went.append(f"分 {all_texts} 条发，前 {texts} 条发出去了" if all_texts > 1 else "文字发出去了")
        if all_files:
            went.append(f"{all_files} 个文件发出去 {sent_files} 个")
        error = f"{'，'.join(went)}；{GROUP_CLOSED_HEAD}，剩下的没发"
        if packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL:
            error = f"平台动作已经核验成功，回复{error}"
        if self._store.move(packet["id"], (SENDING,), FAILED, outcome="failed", sent_at=now, error=error[:500]):
            if packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL:
                self._store.set_notice(packet["deal_id"], f"platform:{packet['id']}", f"#{packet['id']} {error}", now)
        logger.warning("PRM: packet %d: its group is not drafted in for her any more; %d of %d message(s), %d of %d "
                       "file(s) went out, the rest not sent", packet["id"], texts, all_texts, sent_files, all_files)
        return 0

    def _closed_now(self, packet: Optional[dict], now: float) -> bool:
        """``send_approved``: a group draft whose group the bot does not draft in now (``_group_closed``) fails, nothing
        is sent (``_fail_closed``); True then."""
        why = (self._group_closed(packet["chat_id"], packet["party_id"])
               if packet is not None and is_group_packet(packet) else "")
        if not why or not self._fail_closed(packet, why, now):
            return False
        if why == BOSS_GROUPS_OFF_TEXT:
            logger.warning("PRM: packet %d is for a boss group and PRM_BOSS_GROUPS is off; not sent", packet["id"])
        else:
            logger.warning("PRM: packet %d is for a group not drafted in any more; not sent", packet["id"])
        return True

    def _send_pause(self, part: str = "", *, after_text: bool = False, file_n: Optional[int] = None) -> float:
        """Seconds to wait before the next message of a reply (``_deliver``): a text message (``part``) a typing pause
        after the one before it, PRM_SEND_PACE's slower one when on (PACE_TEXT); a file (``file_n``: which, from 0) none
        when off, else PACE_FIRST_FILE after the text (none for a draft of files only), PACE_NEXT_FILE after a file."""
        if file_n is None:
            return typing_delay(part, **PACE_TEXT) if self._pace else typing_delay(part)
        if not self._pace or (file_n == 0 and not after_text):
            return 0.0
        return random.uniform(*(PACE_NEXT_FILE if file_n else PACE_FIRST_FILE))

    def _deliver(self, packet: dict) -> int:
        """Text first (its paragraphs as messages of their own, PRM_REPLY_PARTS), then the files; stops at the first
        failure (never sent twice).  PRM_SEND_PACE on: a person's pace between them (``_send_pause``).  A 百应 draft is
        never sent (``send_approved`` keeps it back already; this is the second guard): back to waiting, nothing sent."""
        if is_buyin(packet):
            self._store.move(packet["id"], (SENDING, APPROVED), PENDING, decided_at=None)
            logger.warning("PRM: packet %d is a 百应 draft: the bot does not send it (an operator does, in 百应)",
                           packet["id"])
            return 0
        tokens = self._store.tokens(packet["party_id"])
        final = packet["final_text"] if packet["final_text"] is not None else packet["text"]
        text = pii.unmask(final, tokens)
        problem = ""
        if pii.unknown_tokens(final, tokens):
            problem = "草稿里有对不上的占位符（号码可能已按规定清除）"
        elif packet["kind"] == "shipping_notice" and not gates.waybill_ok(final, self._waybill(packet),
                                                                          _more_waybills(packet)):
            problem = "发货通知里的单号和系统记录对不上"
        # The text as PRM_REPLY_PARTS messages (``reply_parts``), each noted before it goes, a typing pause apart; a part
        # that fails ends it (never sent twice), and says how many went out.
        parts = reply_parts(text, self._reply_parts) if not problem else []
        # A group's reply: the group looked at again before each message (review 2026-11-09: 「不接群」 while a reply in
        # parts went out, the rest went into the group all the same; with PRM_SEND_PACE whole seconds apart)
        in_group = is_group_packet(packet)
        cut, sent_parts, sent_files = "", 0, 0
        for n, part in enumerate(parts):
            if n and self._pause(self._send_pause(part)):
                # The bot is stopping (its WeChat backend stopped first): the rest is not sent, and the draft stays
                # sending, as one cut off by a stop: the next start settles it as 「不确定发出去没有」 and knows the parts
                # read back by it (``_sent_part``), never an operator's (marked failed now, they would be).
                logger.warning("PRM: packet %d: the bot stopped after %d of its %d messages; the rest not sent",
                               packet["id"], n, len(parts))
                return 0
            cut = self._group_closed(packet["chat_id"], packet["party_id"]) if in_group else ""
            if cut:
                break
            noted = self._note_sent(packet["chat_id"], part, packet["id"])
            try:
                result = self._send(packet["chat_id"], part)
                if not result:
                    self._unnote_sent(noted)
                    problem = _refused(result) or "文字没发出去"
            except Exception:
                self._unnote_sent(noted)
                logger.exception("PRM: packet %d text not sent", packet["id"])
                problem = "文字发送出错"
            if problem:
                if n:
                    problem = (f"分 {len(parts)} 条发，前 {n} 条发出去了，第 {n + 1} 条没发出去（{problem}）；"
                               "剩下的请在微信里手动补发")
                break
            sent_parts += 1
        files = packet["attachments"] if not problem and not cut else []
        for n, item in enumerate(files):
            path = str(item.get("path") or "")
            if not Path(path).is_file():
                problem = f"文件「{item.get('name')}」在电脑上找不到了"
                break
            pause = self._send_pause(after_text=bool(parts), file_n=n)
            if pause and self._pause(pause):
                # (stopping: as a stop between two text messages above, the draft stays sending)
                logger.warning("PRM: packet %d: the bot stopped after %d of its %d files; the rest not sent",
                               packet["id"], n, len(files))
                return 0
            cut = self._group_closed(packet["chat_id"], packet["party_id"]) if in_group else ""
            if cut:
                break
            noted = self._note_sent(packet["chat_id"], file=path, packet_id=packet["id"])
            try:
                result = self._send_file(packet["chat_id"], path)
                if not result:
                    self._unnote_sent(noted)
                    why = _refused(result)
                    problem = f"文件「{item.get('name')}」没发出去" + (f"：{why}" if why else "")
                    break
            except Exception:
                self._unnote_sent(noted)
                logger.exception("PRM: packet %d file not sent", packet["id"])
                problem = f"文件「{item.get('name')}」发送出错"
                break
            sent_files += 1
        if cut:
            return self._cut_off(packet, cut, (sent_parts, len(parts)), (sent_files, len(packet["attachments"])))
        now = self._clock()
        group = (packet.get("meta") or {}).get("channel") == GROUP_CHANNEL
        if problem:
            if packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL:
                problem = f"平台动作已经核验成功，但回复没发出去：{problem}；请在微信里手动发给达人"
            self._store.move(packet["id"], (SENDING,), FAILED, outcome="failed", sent_at=now, error=problem)
            logger.warning("PRM: packet %d failed: %s", packet["id"], problem)
            if group:                                  # 老板拉的群: easy to miss there; the deal says so
                where = "群里" if self.boss_groups.by_hand(packet["chat_id"]) else "老板拉的群里"
                self._store.set_notice(packet["deal_id"], GROUP_UNSENT,
                                       f"#{packet['id']} 没发到{where}（{problem}）：请在微信里手动发", now)
            self._update_gate(packet, now)
            return 0
        # The backend says the keystrokes went through; seeing the line in WeChat confirms it
        # (often already, before the send returned).
        self._store.mark_sent(packet["id"], now)
        if group:
            # 老板拉的群: the group's lines its job was told are answered (by number: one kept later stays, whenever it was
            # written, review 2026-10-31)
            self.boss_groups.replied(packet["chat_id"], (packet.get("meta") or {}).get("group_told") or [])
            self._store.drop_notices(packet["deal_id"], lambda n: n["key"] == GROUP_UNSENT, now)
            if (packet["route"] == gates.REVIEW and self.boss_groups.unconfirmed(packet["chat_id"])
                    and (packet.get("meta") or {}).get("decided_chat") == packet["chat_id"]):
                # 群自己接上 (review #2): a person sent a draft into the group the scan linked: it is hers
                self.boss_groups.confirm(packet["chat_id"], actor="system", how="sent")
        logger.info("PRM: packet %d sent (%d chars%s, %d file(s))", packet["id"], len(text),
                    f" in {len(parts)} messages" if len(parts) > 1 else "", len(packet["attachments"]))
        if packet["route"] == gates.NIGHT:
            self._night_sent(packet, now)
        self._update_gate(packet, now)
        return 1

    def _update_gate(self, packet: dict, now: float) -> None:
        """After a person's decision went out: has the kind earned ``auto`` (8.1)?  A platform
        action's reply never counts (it is always reviewed)."""
        kind, route = packet["kind"], packet["route"]
        if route != gates.REVIEW or packet.get("tool", MESSAGE_TOOL) != MESSAGE_TOOL:
            return
        gate = self._store.gate(kind)
        if gate is None or gate["locked"] or gate["route"] == gates.AUTO:
            return
        stats = gates.window_stats(self._store.reviewed(kind, gate["reset_at"], gate["window"]))
        if gates.earned(gate, stats):
            self._store.set_gate(kind, gates.AUTO, now)
            logger.info("PRM: %s drafts now go out without review (%d of the last %d sent unchanged)",
                        kind, stats["unchanged"], stats["decided"])

    def send_holding_replies(self) -> int:
        """One holding reply per round of the creator's messages whose draft waits too long
        (the Mac's replies only: a system notice, or a next step the bot asked for (the system
        lane: a 专属佣金 waiting for a person), answers nothing the creator asked).  None in a
        round she was answered already (``_answered``: an operator's own line, or something we
        sent, after her latest message), nor for a 开场白 of a creator who has not written (no round), nor for a
        补发货盘 or a 签收后问一句 with no line of hers in its job (we started it: her last line may be an unanswered 「好滴」).
        In the 下班时段 (``_night_active``) it says PRM_NIGHT_HOLDING_TEXT; 自动模式 by day (``_auto_by_day``: no
        「明早」) AUTO_HOLDING_TEXT.  A creator with a 老板拉的群 (she writes there and in private): a round is the draft's
        own chat's lines (``last_by_chat``): her line in the group starts no round in her private chat."""
        if self._send is None or self._holding_sec <= 0:
            return 0
        now = self._clock()
        sent, done = 0, set()
        text = None
        for packet in reversed(self._store.packets((PENDING,), limit=200)):
            if packet["job_id"] is None or packet["deal_id"] in done:
                continue
            if (packet.get("meta") or {}).get("lane") == LANE_SYSTEM or is_buyin(packet):
                continue                               # (a 百应 draft: she wrote in 百应, no WeChat 「稍等」 for it)
            if (packet.get("meta") or {}).get("channel") == GROUP_CHANNEL or (
                    self._groups_review and str(packet.get("chat_id") or "").endswith("@chatroom")):
                continue                               # 老板拉的群: no 「稍等」 in a group (the boss reads it too)
            if self._opening_auto_sec and "auto_at" in (packet.get("meta") or {}) and not packet["needs_attention"]:
                continue                               # our first line goes out by itself soon (send_due_openings)
            if now - float(packet["created_at"]) < self._holding_sec:
                continue
            deal = self._store.deal(packet["deal_id"])
            if deal is None or self._mode(deal, now) != COPILOT:
                continue
            last = deal.get("last_message_at")
            if self.boss_groups.party_has_group(packet["party_id"]):
                last = (deal.get("last_by_chat") or {}).get(packet["chat_id"]) or (
                    None if deal.get("last_by_chat") else last)  # (a deal from before last_by_chat: as before)
            if (packet.get("meta") or {}).get(OPENING) and not last:
                continue                                   # our first line: she has said nothing to hold
            round_at = float(last or packet["created_at"])
            if deal.get("holding_round") is not None and float(deal["holding_round"]) >= round_at:
                continue                                   # already told in this round
            if self._answered(deal, packet["chat_id"], round_at):
                continue                                   # she heard from us after her message
            job = self._relay.store.get(packet["job_id"])
            caught = job_meta(job).get("new_friend") if job is not None else None
            if isinstance(caught, dict) and caught.get("linked"):
                continue                                   # (registered by 「是 N」: nothing of her chat by itself)
            if (packet.get("meta") or {}).get(HUOPAN) and (job is None or not self._job_lines(job)):
                continue                                   # 补发货盘: we started it, she said nothing to hold
            if (packet.get("meta") or {}).get(SIGNED) and (job is None or not self._job_lines(job)):
                continue                                   # 签收后问一句: the same
            if (((packet.get("meta") or {}).get(HANDCARDS) or {}).get("console")
                    and (job is None or not self._job_lines(job))):
                continue                                   # 做手卡: the same
            if job is not None and ack_only(job.get("text") or ""):
                continue                                   # she only said she got it (好滴): nothing to hold
            done.add(packet["deal_id"])
            if text is None:
                auto_on = self.auto_mode["on"]         # read once: the words of the night and of 自动模式 agree
                text = ((AUTO_HOLDING_TEXT if self._auto_by_day(now, auto_on) else self._night_holding_text)
                        if self._night_active(now, auto_on) else self._holding_text)
            noted = self._note_sent(packet["chat_id"], text)
            try:
                ok = bool(self._send(packet["chat_id"], text))
            except Exception:
                logger.exception("PRM: holding reply for deal %d failed", deal["id"])
                ok = False
            if not ok:
                self._unnote_sent(noted)
                continue                                   # tried again next round of the thread
            self._store.update_deal(deal["id"], now, holding_round=round_at)
            self._store.add_event(deal["id"], deal["party_id"], "holding_reply",
                                  {"text": text, "packet": packet["id"]}, actor="system", now=now)
            sent += 1
            logger.info("PRM: draft %d waited %d min; holding reply sent", packet["id"], self._holding_sec // 60)
        return sent

    def _answered(self, deal: dict, chat_id: str, since: float) -> bool:
        """Did the creator hear from us after her latest message (written at ``since``)?  An
        operator's own line written then or later (even one read late, that took no chat over:
        an operator answered on the phone while the bot was stopped, win review F13), or a reply
        sent since (a draft, a shipping notice; one with nothing in it, as a 专属佣金 step's empty
        reply, does not count)."""
        if any(_event_time(ev) >= since for ev in self._store.events(deal["id"], limit=20, kinds=("operator_message",))):
            return True
        for packet in self._store.sent_since(chat_id, self._local(since)):
            final = packet["final_text"] if packet["final_text"] is not None else packet["text"]
            if str(final or "").strip() or packet["attachments"]:
                return True
        return False

    def purge_due(self) -> int:
        """Clear the details of every deal done ``PRM_RETENTION_DAYS`` ago (8.2, ``retention.purge_due_at``)."""
        now = self._clock()
        done = 0
        try:
            self._buyin_scrub_jobs()
        except Exception:
            logger.exception("PRM: the 百应 inbox jobs could not be looked at; tried again later")
        try:                                       # 加百应达人微信: the numbers' time, the ended records' (design §6.10)
            self.friend_adds.sweep(now, retention_days=self._retention_days, force=True)
        except Exception:
            logger.exception("PRM: the friend-add records could not be cleaned up; tried again later")
        try:                                       # 老板拉的群: ignored groups after IGNORED_KEEP_DAYS
            self.boss_groups.sweep_old(now)
        except Exception:
            logger.exception("PRM: the ignored boss groups could not be cleaned up; tried again later")
        try:                                       # PRM_DRAFT_CONTEXT: the photo sheets (her picture) after 30 days
            draft_context.sweep(self._store, now, files_dir=self._relay.files_dir, relay_store=self._relay.store)
        except Exception:
            logger.exception("PRM: the operator files could not be cleaned up; tried again later")
        try:                                       # 直播手卡: a draft's not sent, and any after 30 days (her 称呼 on them)
            self.sweep_cards()
        except Exception:
            logger.exception("PRM: the 直播手卡 files could not be cleaned up; tried again later")
        for deal in self._store.deals_to_purge(now, self._retention_days):
            try:
                self.purge(deal, now)
                done += 1
            except Exception:
                logger.exception("PRM: clean-up of deal %d failed; tried again later", deal["id"])
        return done

    def purge(self, deal: dict, now: float) -> dict:
        party = self._store.party(deal["party_id"]) or {}
        chat_id = party.get("wxid", "")
        addresses = retention.address_values(deal.get("slots") or {})

        def rewrite(text: str) -> str:
            return retention.scrub(retention.clear_receivers(text), addresses)

        counts = {"numbers": self._store.purge_tokens(deal["party_id"], pii.masked_value, now)}
        # 加百应达人微信: her records' numbers (cleared when she was linked: once more), numbers in her 百应 nickname
        counts["friend_adds"] = self._store.scrub_friend_adds(deal["party_id"], contact.clean_name, now)
        # 起草备注 (the operators' words, perhaps her signature's): cut like the deal's notices, a 微信号 after a label
        # masked as in her 百应 nickname (``contact.clean_name``)
        note = str(deal.get("draft_note") or "")
        fields = {"draft_note": contact.mask_contacts(rewrite(note))} if note else {}
        if note:
            counts["draft_note"] = int(fields["draft_note"] != note)
        self._store.update_deal(deal["id"], now, replace_slots=retention.cleared_slots(deal.get("slots") or {}),
                                purged_at=now, **fields)
        group = deal.get("three_party") or {}
        if group.get("name") or group.get("chat"):     # 三方群: WeChat's name for it holds hers
            self._store.update_deal(deal["id"], now, three_party={k: group[k] for k in ("state", "at", "by") if k in group})
        # (every group ever linked to the deal: one linked again to another creator since too, review 2026-11-10 B-L5)
        boss_chats = self.boss_groups.chats_of(deal["id"])
        if boss_chats:                                 # 老板拉的群: the names in its row and record, its lines kept
            counts["boss_groups"] = self.boss_groups.purge(deal)
        counts["drafts"] = self._store.rewrite_packets(deal["id"], rewrite)
        # 微信里审批草稿: the rows of her drafts told to the admin (what the admin wrote for them), with her drafts
        counts["wechat_reviews"] = self._store.purge_wechat_reviews(deal["id"], rewrite, now)
        counts["events"] = self._store.rewrite_events(deal["id"], rewrite)
        # 专属链接 steps (review 2026-11-10): the prefix (her name in it) and her UID (older events kept it whole) cut
        counts["steps"] = self._store.edit_event_details(deal["id"], "next_step", step_scrubbed)
        counts["notices"] = self._store.rewrite_notices(deal["id"], rewrite)
        # 聚水潭's words for a rejected or deleted order ("地址不全…"): kept with the order and in the sync's answers
        counts["samples"] = (self._store.rewrite_sample_orders(deal["id"], rewrite, now)
                             + self._relay.store.rewrite_chat(SAMPLE_SYNC_CHAT, PRM, rewrite))
        # Her 百应 conversations' draft jobs (their texts: her 百应 lines, masked), and the inbox round's (a summary)
        counts["buyin"] = (sum(self._relay.store.rewrite_chat(BUYIN_CHAT + cid, PRM, rewrite)
                               for cid in self._buyin_conversations_of(deal["id"]))
                           + self._relay.store.rewrite_chat(BUYIN_SYNC_CHAT, PRM, rewrite))
        if chat_id:
            counts["relay"] = self._relay.store.rewrite_chat(chat_id, PRM, rewrite)
            if self._scrub_messages is not None:
                counts["messages"] = self._scrub_messages(chat_id, rewrite)
            for chat in boss_chats:                    # (her lines there: the group's jobs and its archive)
                counts["relay"] += self._relay.store.rewrite_chat(chat, PRM, rewrite)
                if self._scrub_messages is not None:
                    counts["messages"] += self._scrub_messages(chat, rewrite)
            # The Mac's case file: a job the worker does itself (no model); the creator's
            # next message must not join it (input_closed), it is a draft job of its own.
            self._relay.submit(chat_id, chat_id, "", kind=PRM, fresh=True, input_closed=True,
                               meta={"op": "purge", "party_id": deal["party_id"], "deal_id": deal["id"]})
        self._store.add_event(deal["id"], deal["party_id"], "purge", counts, actor="system", now=now)
        logger.info("PRM: deal %d done %d days ago (delivered or quiet); receiving details cleared (%s)", deal["id"],
                    self._retention_days, ", ".join(f"{k} {v}" for k, v in counts.items()))
        return counts


# What of a platform action may hold a creator's own numbers: her receiver (a sample order) and the
# note for the operators.  The rest are platform ids and our own texts, sent as they are.
PERSONAL_PARAMS = ("receiver", "note")


def masked_action(action, mask: Callable[[str], str]):
    """``action`` with ``mask`` run over its ``PERSONAL_PARAMS`` (``_mask_draft``)."""
    if not isinstance(action, dict):
        return action
    out = dict(action)
    if isinstance(out.get("receiver"), dict):
        out["receiver"] = {k: mask(v) for k, v in out["receiver"].items()}
    if "note" in out:
        out["note"] = mask(out["note"])
    return out


def action_for_mac(action: dict, tokens: dict) -> tuple:
    """``(action, problem)``: the approved action as the Mac executes it.  A sample order's
    receiver is the draft's: its phone a 〈手机N〉 (the model never sees numbers; one the Mac read
    off a picture became one too, ``_mask_draft``), perhaps another one in its address; the
    numbers go in here, for the Mac that sends the order and nowhere else (the packet keeps the
    placeholders)."""
    receiver = action.get("receiver") if isinstance(action.get("receiver"), dict) else None
    if not receiver:
        return action, ""
    phone = str(receiver.get("phone") or "")
    unknown = pii.unknown_tokens(phone, tokens)
    if unknown:
        return action, f"收件电话的占位符对不上：{'、'.join(unknown)}（这位达人没有这个号码）"
    try:
        number = platforms.phone_of(pii.unmask(phone, tokens))
    except platforms.ActionError:
        return action, "收件电话换回号码以后不是手机号，不执行"
    if platforms.PHONE_TOKEN.match(number):
        return action, "收件电话还是占位符，不执行"
    unknown = pii.unknown_tokens(" ".join(str(v) for k, v in receiver.items() if k != "phone"), tokens)
    if unknown:
        return action, f"收货信息里的占位符对不上：{'、'.join(unknown)}（这位达人没有这个号码）"
    fields = {k: pii.unmask(v, tokens) if isinstance(v, str) else v for k, v in receiver.items()}
    return {**action, "receiver": {**fields, "phone": number}}, ""


# The 专属佣金 action of each shop: a sample order's products get theirs once it is created.
COMMISSION_ACTIONS = {"dy": "dy.designated_commission", "xhs": "xhs.direct_plan"}
# 专属链接 (2026-11-09): a 抖店 product's link (platforms.py: the shop's way) is the step after its 专属佣金 (binding her
# needs her in its strategy), when the deal asks for links.  Whether it does is the drafting Claude's call (the creator
# or the boss spoke of 「专属链接」 or 「前缀」, or asked plainly for a link of her own; a bare 「发个链接」「链接发我」
# 「有链接吗」 is the product's ordinary link, kb/products.md: never this slot, worker's rule, review 2026-11-10 C1): its
# draft says so in the deal's slot LINK_SLOT, the prefix (or 「要」 while she has not said one), or an empty string when
# she does not want them (``link_reading``, ``link_wish``).
LINK_ACTION = "dy.channel_goods"
LINK_SLOT = "专属链接前缀"
# How a slot is read (``link_reading``; review 2026-11-09 F6, N3).  Not understood (``unclear``) first: one of
# LINK_DOUBT_WORDS (「要不要都行」「要是需要再说」「不是很想要」).  Yes: a 要 that is not denied (``LINK_YES``: none of
# 不 别 没 无 甭 勿 莫 right before it or one before that: 「不要」「不需要」「不想要」 deny, 「需要」 does not; not 「主要」
# 「重要」「只要」「要求」「要是」: 「没问题，要」「无所谓，要」「要（前缀不用写）」 say yes), or a prefix (``_slot_prefix``)
# whose words around it say none of LINK_NO_WORDS (「【小美专属】不要了」 does).  No: empty, one of LINK_NO_ALONE, or
# one of LINK_NO_WORDS without a yes (「算了」「先不弄了」「以后再说」「No」).  Anything else (「好的」「可以」「是」
# 「专属链接」) is not understood: the deal keeps what it had (``PrmService._link_slots``).
LINK_YES = re.compile(r"(?<![不别没无甭勿莫主重只])(?<![不别没无甭勿莫].)要(?![求是])")
LINK_NO_WORDS = ("不要", "不用", "不需要", "不想", "算了", "先不", "以后", "再说", "取消", "暂不", "别")
LINK_NO_ALONE = ("无", "没有", "否", "不", "不了", "no", "nope")
LINK_DOUBT_WORDS = ("要不要", "要是", "不是很", "不太", "不一定", "看情况", "再看看", "考虑", "可能", "也许", "待定")
# Where a slot's prefix may be, when it is in no bracket: its last part (split here) that ends in 专属.
_SLOT_PARTS = re.compile(r"[\s，,。.;；:：、!！?？]+|前缀(?:是|写成|写|为)?")
# A deal without a chain whose 专属链接 wait (``PrmService._links_without_chain``): its notice says which and why.
LINKS_NOTICE = "links"
# A draft wrote the 「专属链接前缀」 slot in words that say neither yes nor no: the deal kept its own (``_link_slots``).
LINKS_UNCLEAR_NOTICE = "links_unclear"
# A step whose 专属链接 the Mac left out, not able to ask this bot what it knows (``PrmService._unasked``): asked again
# this long after, this many times at most.
STEP_RETRY_SEC = 300
STEP_UNASKED_TRIES = 3
# The platform actions of the chain: what an operator decides on them moves it on (``_chain_decided``).
CHAIN_TOOLS = (*COMMISSION_ACTIONS.values(), LINK_ACTION)
# A product's link in the chain (``PrmService._chain``), as the Mac and the deal hear it; ``waits``: its 专属佣金 is not
# open (not this round: binding her would be refused).
LINK_STATES = (("opened", "已建好"), ("running", "正在执行"), ("waiting", "在待审批"), ("drafting", "正在起草"),
               ("dropped", "这一轮不建（运营点了「不发」）"), ("not_done", "没建成"),
               ("uncertain", "结果不确定（运营会先到平台后台核对）"), ("asked", "派过、草稿没建它"),
               ("todo", "还没建"), ("waits", "等它的专属佣金开好"))


def _slot_prefix(value: str) -> tuple:
    """The prefix a slot names, as written, and the slot without it: ``(part, rest)``; ``part`` "" when it names none.
    A 【…】, or a part in another pair of brackets or quotes (``platforms.PREFIX_PAIRS``) with 专属 in it, else its last
    part (``_SLOT_PARTS``) that ends in 专属 after a name, without a 要 (「小美专属」「前缀：小美专属」)."""
    pairs = {"【": "】", **platforms.PREFIX_PAIRS}
    for i, ch in enumerate(value):
        close = pairs.get(ch)
        j = value.find(close, i + 1) if close else -1
        if j > i and (ch == "【" or "专属" in value[i:j + 1]):
            return value[i:j + 1], value[:i] + value[j + 1:]
    parts = [p for p in _SLOT_PARTS.split(value) if p]
    last = parts[-1] if parts else ""
    if last.endswith("专属") and last != "专属" and "要" not in last:
        at = value.rindex(last)
        return last, value[:at] + value[at + len(last):]
    return "", value


def _link_read(value) -> tuple:
    """``(reading, wish)`` of a 「专属链接前缀」 value (format characters and runs of spaces aside): ``reading`` is
    "yes", "no" or "unclear" (see ``LINK_YES``), ``wish`` the ``link_wish`` of a yes (else None)."""
    value = " ".join("".join(ch for ch in str(value or "") if unicodedata.category(ch) != "Cf").split())
    if not value:
        return "no", None
    part, rest = _slot_prefix(value)
    if any(word in rest for word in LINK_DOUBT_WORDS):
        return "unclear", None
    if not (LINK_YES.search(rest) or (part and not any(word in rest for word in LINK_NO_WORDS))):
        said_no = rest.strip().lower() in LINK_NO_ALONE or any(word in rest for word in LINK_NO_WORDS)
        return ("no" if said_no else "unclear"), None
    out = {"prefix": "", "said": value[:40]}
    if "专属" in part:
        try:
            prefix = platforms.title_prefix_of(part)
        except platforms.ActionError:
            prefix = ""
        if prefix and platforms.title_length(prefix) > platforms.TITLE_PREFIX_MAX:
            out["long"] = prefix
        else:
            out["prefix"] = prefix
    return "yes", out


def link_reading(value) -> str:
    """"yes", "no" or "unclear": what a 「专属链接前缀」 value says (``_link_read``)."""
    return _link_read(value)[0]


def link_wish(slots) -> Optional[dict]:
    """Does the deal ask for 专属链接 (its slot ``LINK_SLOT``, the drafting Claude's word)?  None: not (no slot, a no,
    or words that say neither: ``link_reading``; a deal never keeps those, ``PrmService._link_slots``).  Else
    ``{"prefix", "said"}`` (and ``long``): ``prefix`` the shop's 「【某某专属】」 when the slot names one
    (``_slot_prefix``, 专属 in it, ``platforms.title_prefix_of``) that fits the usual limit
    (``platforms.TITLE_PREFIX_MAX``), else "" (she asked for links: her prefix is the case file's, or still to be
    asked); ``long``: hers when it is above that limit (review 2026-11-09 F1: never dictated, the step is told to cut it
    short or ask her); ``said``: the slot, cut short."""
    return _link_read((slots or {}).get(LINK_SLOT))[1]


def link_prefix_hint(links: dict) -> str:
    """What a 专属链接 step's note says of its title_prefix: the deal's prefix when it fits (``link_wish``); hers cut
    short when it is longer (the name's first characters and 专属, ``platforms.short_prefix``), or her to be asked
    (review 2026-11-09 F1); else to be taken from the case file, or asked."""
    if links.get("prefix"):
        return f"title_prefix 填「{links['prefix']}」"
    if links.get("long"):
        short = platforms.short_prefix(links["long"])
        return (f"她要的前缀「{links['long']}」是 {platforms.title_length(links['long'])} 个字符，超过一般商品的上限 "
                f"{platforms.TITLE_PREFIX_MAX} 个（汉字算 2 个、【】各算 1 个），不能照写：title_prefix 写短一点，留名字的前几个字加"
                "「专属」" + (f"（比如「{short}」）" if short else "") + "；拿不准就不填平台动作，在回复里问她前缀怎么写")
    return ("title_prefix 按她要的前缀填（看案卷，写成「【某某专属】」，最多 12 个字符、汉字算 2 个）；还不知道前缀就不填"
            "平台动作，在回复里问她前缀写什么")


def owed_out_of_date(owed: dict, wish: Optional[dict]) -> str:
    """Why an owed step (the deal's ``step`` notice) is not dispatched with the note it was owed with ("" = it is): a
    专属链接 step owed before the deal had the prefix it has now (with another one, or none), or links the deal no longer
    asks for (review 2026-11-09 F7: its old note would ask her for a prefix she has given).  The text starts the new
    step's note."""
    if str(owed.get("action") or "") != LINK_ACTION:
        return ""
    if wish is None:
        return "这一步欠着的时候，合作单改成了不要专属链接。"
    if wish.get("prefix") and wish["prefix"] != str(owed.get("prefix") or ""):
        return f"这一步欠着的时候，合作单记下了她要的专属链接前缀「{wish['prefix']}」。"
    return ""


def links_notice_text(links: dict, items: list, busy: bool) -> str:
    """The ``LINKS_NOTICE`` of a deal without a chain (review 2026-11-09 F2): the products whose 专属链接 is left
    (``items``; "" when none) and why they wait: a step under way (``busy``), her prefix not known yet, or too long."""
    if not items:
        return ""
    shown = "、".join(items[:CHAIN_LIST]) + (f" 等 {len(items)} 个" if len(items) > CHAIN_LIST else "")
    if links.get("prefix"):
        why = "手头这一步办完系统就接着派，一次一个商品" if busy else "系统接着派"
    elif links.get("long"):
        why = (f"她要的前缀「{links['long']}」超过 {platforms.TITLE_PREFIX_MAX} 个字符，要她给个短一点的"
               "（记进合作单以后系统就派）")
    else:
        why = "还不知道前缀：问到她要的前缀、记进合作单以后系统就派"
    return f"专属链接（她要：{links.get('said', '')}）：商品 {shown} 的还没建，{why}"


def link_again(packet: dict, prefix: str) -> bool:
    """A 专属链接 draft dropped (「不发」) while its preview stopped at its title prefix (``platforms.prefix_stopped``),
    and the deal has another prefix now that fits (``prefix``): it does not count, the product is asked for again
    (review 2026-11-09 F1)."""
    data = packet.get("platform") or {}
    return (bool(prefix) and packet.get("status") == DROPPED and platforms.prefix_stopped(data.get("preview"))
            and str((data.get("action") or {}).get("title_prefix") or "") != prefix)


def wrong_uid_text(uid: str, known: str) -> str:
    """A 专属链接 step of the chain that would bind another UID than its round's sample order's (``known``): refused at
    approval (review 2026-11-09 F4)."""
    return (f"链接要绑的 UID 不是这一轮寄样单上的（寄样单上的 UID：{known}；草稿写的是 {uid}）：这一份点「不发」；"
            "要给另一个号建，等她在对话里要，或者在抖店手建")


def uid_doubt_text(uid: str, known: list, orders: int) -> str:
    """A 专属链接 draft (not a step of the chain) whose UID is not one of her verified 抖店 sample orders' (``known``;
    ``orders``: how many she has): flagged, not refused (she may have two 抖音 accounts: review 2026-11-09 F4)."""
    if known:
        return (f"链接要绑的 UID {uid} 不在她核验成功的抖店寄样单上（寄样单上的 UID：{'、'.join(known[:3])}）："
                "批准前核对是不是她的（她可能有两个抖音号）")
    if orders:
        return f"她核验成功的抖店寄样单上没记下抖音 UID：批准前核对链接要绑的 UID {uid} 是她的"
    return f"系统不知道这位达人的抖音 UID（她没有核验成功的抖店寄样单）：批准前核对链接要绑的 UID {uid} 是她的"


def convention_text(action: dict, block: Optional[dict] = None) -> str:
    """A 专属链接 draft (``dy.channel_goods``) not made the shop's way (review 2026-11-10 A-D4; platforms.py: 专属达人带货,
    each colour's 9 折 of its daily price floored to the yuan, CHANNEL_STOCK_PER_SKU a colour): 「没按店里的规矩：…」 naming
    each way it is not, or "" (made the shop's way).  ``block``: its preview (``run.skus``: the daily prices the price is
    held to; none read, the price the draft wrote is named only)."""
    out = []
    if action.get("exclusive") is False:
        out.append("不是专属达人带货（写的是达人带货" + ("" if action.get("stock_per_sku") else "，共用主商品库存") + "）")
    fixed = platforms._amount(action.get("price")) if action.get("price") not in (None, "") else None
    if fixed is not None:
        skus = [s for s in (((block or {}).get("run") or {}).get("skus") or []) if isinstance(s, dict)]
        off = []
        for sku in skus:
            live = platforms.live_price(sku.get("main_price"))
            if live is None or live != fixed:
                off.append(f"SKU {sku.get('id')} 日常价 {sku.get('main_price') or '没读到'} 元" +
                           (f"的 9 折是 {platforms._plain_price(live)} 元" if live is not None else ""))
        price = platforms._plain_price(fixed)
        if not skus:
            out.append(f"渠道价不是 9 折（统一写的 {price} 元，不是每个颜色日常价的 9 折）")
        elif off:
            out.append(f"渠道价不是 9 折（统一写的 {price} 元；" + "、".join(off[:3])
                       + (f" 等 {len(off)} 个颜色" if len(off) > 3 else "") + "）")
    stock = action.get("stock_per_sku")
    if isinstance(stock, int) and not isinstance(stock, bool) and stock != platforms.CHANNEL_STOCK_PER_SKU:
        out.append(f"每个颜色的库存写的是 {stock} 件，不是 {platforms.CHANNEL_STOCK_PER_SKU} 件")
    return ("没按店里的规矩：" + "；".join(out)) if out else ""


UID_TAIL = 4                    # a creator's 抖音 UID in a ``next_step`` event: its last characters only
PREFIX_CLEARED = "（已清除）"     # a ``next_step`` event's 专属链接 prefix after the 8.2 clean-up (``step_scrubbed``)


def step_scrubbed(detail: dict) -> dict:
    """A ``next_step`` event's details after the 8.2 clean-up (``PrmService.purge``, review 2026-11-10): her UID cut to its
    last UID_TAIL characters, a 专属链接 prefix (her name in it, 「【小美专属】」) only a mark that the step was asked with
    one (PREFIX_CLEARED: ``_chain`` and ``_links_left`` still count it asked with a prefix, never asked again)."""
    out = dict(detail or {})
    if out.get("uid"):
        out["uid"] = str(out["uid"])[-UID_TAIL:]
    if out.get("prefix"):
        out["prefix"] = PREFIX_CLEARED
    return out


def unfilled_placeholders(text) -> list:
    """The platform placeholders a text still says ({链接}, {计划名}, {渠道品ID} …: ``platforms.PLACEHOLDER_LABELS``):
    only a platform action that ran fills them in, so a plain message must not carry them (review 2026-11-09 #4)."""
    text = str(text or "")
    return [k for k in platforms.PLACEHOLDER_LABELS if k in text]


def unfilled_text(unfilled: list) -> str:
    """Why a plain message that still says ``unfilled`` cannot go out."""
    return (f"回复里还有 {'、'.join(unfilled)}（" + "、".join(platforms.PLACEHOLDER_LABELS[k] for k in unfilled)
            + "）：这些只有平台动作执行成功以后才换成平台给的值，这份草稿没有平台动作，原样发出去她会看到花括号；改掉再发")


def step_word(meta: Optional[dict]) -> str:
    """What a step job of the chain drafts, for a person: 建专属链接 (``meta.step.action``) or 开专属佣金."""
    step = (meta or {}).get("step") if isinstance((meta or {}).get("step"), dict) else {}
    return "建专属链接" if step.get("action") == LINK_ACTION else "开专属佣金"
# A creator's 专属 actions: one per product and creator (a second would open a second 百应 strategy,
# 千帆 plan or channel product); ``PrmService._opened_before`` refuses another approval.
ONCE_ACTIONS = ("dy.designated_commission", "xhs.direct_plan", "dy.channel_goods")
# The end of ``_opened_before``'s refusal: the draft is not needed (a sample order's chain goes on by itself).
REOPEN_HINT = "点「不发」就行（寄样单的专属佣金：系统会接着开还没开的商品）"
# How a round's products not opened are asked for again (kb/合作政策.md 2.1): a new round, from a new signature.
RESTART_HINT = "要重开这一轮没开的商品：把物流改回「运输中」再标「已签收」"
# The same with PRM_COMMISSION_AFTER=created (the round starts when the order is made: a signature starts a new one).
RESTART_HINT_CREATED = "要重开这一轮没开的商品：标「已签收」时会重开（已经签收的，把物流改回「运输中」再标「已签收」）"
# A 专属佣金 chain's products as the bot sees them (``item_state``), in the order the Mac and the deal hear them.
CHAIN_STATES = (("opened", "已开好"), ("covered", "邀约卡开过（当作开好）"), ("running", "正在执行"), ("waiting", "在待审批"),
                ("drafting", "正在起草"),
                ("dropped", "这一轮不开（运营点了「不发」）"), ("not_done", "没开成"),
                ("uncertain", "结果不确定（运营会先到平台后台核对）"), ("asked", "派过、草稿没开它"),
                ("todo", "还没开"))
CHAIN_LIST = 3              # product ids the deal's notice names per state (the rest are counted)
# Stages after the sample (the content): what a chain left or drafted nothing for no longer needs saying.
MOVED_ON = ("brief", "draft", "revise", "published", "closed")


def action_target(action: dict, data: Optional[dict] = None) -> tuple:
    """``(item, creators)`` of a 专属 action: the product, and the creator as the draft named them
    plus, for a 千帆 plan, the buyer id the Mac's preview found (``data``: the packet's ``platform``),
    so a 小红书号 and the userId of one buyer are the same creator."""
    item = str(action.get("item_id") or "").strip()
    creators = {str(action.get("creator_platform_id") or "").strip().lower()}
    buyer = (((data or {}).get("preview") or {}).get("run") or {}).get("buyer_id")
    if buyer:
        creators.add(str(buyer).strip().lower())
    return item, creators - {""}


def order_items(action: dict) -> list:
    """The products of a sample order (``jst.sample_order``), in its order: its ``items`` (an older action has
    only ``item_id``)."""
    ids = [str(i.get("item_id")) for i in action.get("items") or [] if isinstance(i, dict) and i.get("item_id")]
    if not ids and action.get("item_id"):
        ids = [str(action["item_id"])]
    return ids


# The results of an execution that wrote nothing on the platform, when no step of it wrote either.
NOT_WRITTEN = ("FAILED", "STATE_CONFLICT", "NOT_RUN")


def _same_json(a, b) -> bool:
    """Two JSON values the same, key order aside, a NaN the same as a NaN (as ``preview_stamp`` reads them: a preview
    with a NaN in it is never 「changed」 by being read twice)."""
    def dump(value) -> str:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return dump(a) == dump(b)


def written_state(execution: Optional[dict], packet_outcome: str = "") -> str:
    """What an execution (its record) left on the platform: "verified" (VERIFIED_SUCCESS); "not_done"
    (FAILED, STATE_CONFLICT or NOT_RUN, and no step of it that ran with --execute wrote: the platform
    refused, nothing was sent, nothing ran); else "uncertain": it may be there (ACCEPTED_UNVERIFIED,
    INDETERMINATE, UNKNOWN, PARTIAL, OBSERVED_DESIRED_STATE …, or a step that wrote before a later one
    failed: a 渠道品 made, its binding refused).  No record at all is NOT_RUN only for a job that never
    reached the Mac (``platform_not_run``)."""
    execution = execution or {}
    outcome = execution.get("outcome") or ("NOT_RUN" if packet_outcome == "platform_not_run" else "UNKNOWN")
    if outcome == platforms.VERIFIED:
        return "verified"
    wrote = any(isinstance(step, dict) and step.get("execute") and step.get("outcome") not in NOT_WRITTEN
                for step in execution.get("steps") or [])
    return "not_done" if outcome in NOT_WRITTEN and not wrote else "uncertain"


def platform_state(packet: dict) -> str:
    """Where a platform draft's action stands: "running" (on the Mac now), "verified" (its reply approved,
    being sent or sent, or held back: a placeholder the platform gave no value for, the reply not sent, a
    late answer), "uncertain" / "not_done" (it ended red: ``written_state``), or "" (no decision: waiting,
    not sent, out of date)."""
    status = packet["status"]
    if status == EXECUTING:
        return "running"
    if status in (APPROVED, SENDING, SENT):
        return "verified"
    if status != FAILED:
        return ""
    return written_state((packet.get("platform") or {}).get("execution"), packet.get("outcome", ""))


def _verified(packet: dict) -> bool:
    """Was this platform draft's action verified on the platform (``platform_state``)?"""
    return platform_state(packet) == "verified"


def _covered(packet: dict) -> Optional[dict]:
    """The invite card's cover of a 专属佣金 draft the bot did not run (``dropped`` / ``covered``: the Mac found the
    creator's 邀约卡 with a 专属佣金 for this product in the invite ledger, at the preview or at the execution), or None."""
    if packet.get("status") != DROPPED or packet.get("outcome") != "covered":
        return None
    data = packet.get("platform") or {}
    return (data.get("preview") or {}).get("covered") or (data.get("execution") or {}).get("covered") or {}


def cover_text(cover: dict, item: str) -> str:
    """「这位达人的商品 X 已经由邀约卡开过 25% 专属佣金（2026-10-28 15:00 发的邀约卡）」"""
    when = str(cover.get("at") or "")[:16].replace("T", " ")
    return (f"这位达人的商品 {item} 已经由邀约卡开过 {cover.get('rate')}% 专属佣金"
            + (f"（{when} 发的邀约卡）" if when else "") + "，平台上已经有指定达人佣金策略，当作开好了")


def leftover_of(packet: dict) -> str:
    """The channel product a 专属链接 draft made and did not see bound ("" when none): its execution has a channel id
    and did not end verified (PARTIAL after the create, the bind refused, a result not known)."""
    data = packet.get("platform") or {}
    if (data.get("action") or {}).get("action") != LINK_ACTION:
        return ""
    execution = data.get("execution") or {}
    channel = str((execution.get("created") or {}).get("channel_id") or "")
    return channel if channel.isdigit() and execution.get("outcome") != platforms.VERIFIED else ""


def leftover_text(outcome: str, why: str, created: dict) -> str:
    """A 专属链接 that made its channel product and did not bind her (review 2026-11-09 F3): the product, the two ways
    back, what 「我已处理」 means, the link, what the Mac said (in that order: a long link or reason is cut off last)."""
    channel, link = str(created.get("channel_id") or ""), str(created.get("link") or "")
    return (f"渠道品 {channel} 建好了、没绑上她（{outcome}：{platforms.outcome_text(outcome)}），没有发微信。两条路："
            f"① 在抖店「渠道商品管理」把她绑到渠道品 {channel} 上，链接自己发给她；② 把它下架、删掉。处理好了点「我已处理」"
            "（它还在、又没绑上她时，系统不会再给她建这个商品的链接）"
            + (f"。链接：{link}" if link else "") + (f"。Mac 说：{why}" if why else ""))[:500]


def _released(other: dict, packet_id: int) -> bool:
    """Did an operator, asked to check ``other``'s uncertain result for draft ``packet_id`` (``_ask_recheck``),
    mark it 「我已处理」 after that: it is not on the platform, that draft may run?"""
    data = other.get("platform") or {}
    recheck = data.get("recheck") or {}
    if recheck.get("for") != packet_id:
        return False
    # (a red result's 「我已处理」 is the packet's own ``acked_at``; a second sample order's, of a draft that is not red,
    # the recheck's: ``PrmService._second_order``)
    acked = [float(a) for a in (data.get("acked_at"), recheck.get("acked_at")) if isinstance(a, (int, float))]
    return any(a >= float(recheck.get("at") or 0) for a in acked)


def item_state(packets: list, asked: bool, since: float) -> tuple:
    """``(state, packet)`` of one product of a 专属佣金 chain (``CHAIN_STATES``) from its drafts of the commission
    action, newest first: "opened" (one verified), "running", "waiting" (in 待审批); else what this round
    decided on (a draft of it since the round started, ``since``: "dropped" by an operator, "not_done" /
    "uncertain" red, ``written_state``); else "asked" (a step asked for it in this round and nothing of it
    came: no action drafted, the draft not written; "drafting" while its step's job runs: ``PrmService._chain``);
    else "todo", with the uncertain draft of an earlier round if there is one (to be checked before a new one
    runs: ``_opened_before``)."""
    states = [(platform_state(p), p) for p in packets]
    hit = next((p for s, p in states if s == "verified"), None)
    if hit is not None:
        return "opened", hit
    hit = next((p for p in packets if _covered(p) is not None), None)
    if hit is not None:                                # the invite card opened it (long-term): open, whatever the round
        return "covered", hit
    hit = next((p for s, p in states if s == "running"), None)
    if hit is not None:
        return "running", hit
    hit = next((p for p in packets if p["status"] == PENDING), None)
    if hit is not None:
        return "waiting", hit
    for s, p in states:
        if float(p["created_at"]) <= since:
            continue                                   # (a draft of the signature's own time is of the one before)
        if p["status"] == DROPPED:
            return "dropped", p
        if s in ("not_done", "uncertain"):
            return s, p
    if asked:
        return "asked", None
    return "todo", next((p for s, p in states if s == "uncertain"), None)


def _ids(rows: list, limit: int, packet: bool = False) -> str:
    """Products for a person: the first ``limit`` ids (with their draft, ``packet``), then how many more."""
    shown = [f"{r['id']}（#{r['packet']['id']}）" if packet and r.get("packet") else r["id"] for r in rows[:limit]]
    return "、".join(shown) + (f" 等 {len(rows)} 个" if len(rows) > limit else "")


def _link_steps_left(items: list, current: str, current_action: str) -> int:
    """How many steps a round of a deal asking for 专属链接 still has after this one (``current``'s
    ``current_action``): the 专属佣金 left, and the links left of the products whose 专属佣金 is open or still to be
    opened this round."""
    left = 0
    for i in items:
        if i["state"] == "todo" and not (i["id"] == current and current_action != LINK_ACTION):
            left += 1
        if (i.get("link") in ("todo", "waits") and i["state"] in ("opened", "covered", "todo")
                and not (i["id"] == current and current_action == LINK_ACTION)):
            left += 1
    return left


def chain_note(progress: dict, after: Optional[dict], *, delivered: bool, why: str = "") -> str:
    """What a 专属佣金 step's draft is told (``meta.system_note``) by a bot that keeps the chain's record
    (``PrmService._chain``): what happened just now (``after``: the sample order, or the 专属佣金 draft opened,
    dropped, red; with links, the 专属链接 draft made, dropped, red), the one product to fill now
    (``progress["next"]``, for ``next_action``: its 专属佣金, or its 专属链接 with the prefix the deal's slot holds;
    none: the round is done, only the case file is updated), and where every product of the order stands, last (the
    bot's record: a 专场's long list never pushes the instruction out).  Nothing just happened (``after`` None): it
    starts with ``why`` and goes on with the round (review 2026-11-09 F7: not 「达人已经签收了寄样单…」 when that is
    not why).  A deal that does not ask for links hears what it heard before, word for word."""
    order = progress["order"]
    shop, name, items = progress["shop"], progress["action"], progress["items"]
    links = progress.get("links")
    what = f"{platforms.PLATFORMS[shop]}的商品，共 {len(items)} 个，清单在最后"
    theirs = ((after or {}).get("platform") or {}).get("action") or {}
    head = ""
    if after is None:
        pass
    elif after["id"] == order["id"]:
        head = (f"达人已经签收了寄样单 {progress['number']}（{what}）。" if delivered else
                f"寄样单 {progress['number']} 已经在聚水潭建好，寄的是{what}。")
    elif theirs.get("action") == name:
        item, state = str(theirs.get("item_id") or ""), platform_state(after)
        outcome = ((after.get("platform") or {}).get("execution") or {}).get("outcome") or ""
        if state == "verified":
            head = (f"商品 {item} 的专属佣金（{theirs.get('commission_rate')}%）已经在平台上开好了"
                    f"（达人 {theirs.get('creator_platform_id')}）。")
        elif _covered(after) is not None:
            cover = _covered(after)
            head = (f"商品 {item} 已经由邀约卡开过 {cover.get('rate')}% 专属佣金（{str(cover.get('at') or '')[:16].replace('T', ' ')}"
                    "发的邀约卡），平台上已经有指定达人佣金策略，当作开好了，不用再开。")
        elif after["status"] == DROPPED:
            head = f"商品 {item} 这一轮不开专属佣金了（运营点了「不发」）。"
        elif state == "uncertain":
            head = f"商品 {item} 的专属佣金结果不确定（#{after['id']} {outcome}），运营会先到平台后台核对，这一轮先跳过它。"
        elif state == "not_done":
            head = (f"商品 {item} 的专属佣金没开成（#{after['id']} {outcome}：{platforms.outcome_text(outcome)}），"
                    "这一轮先跳过它。")
    elif theirs.get("action") == LINK_ACTION:
        item, state = str(theirs.get("item_id") or ""), platform_state(after)
        execution = (after.get("platform") or {}).get("execution") or {}
        outcome = execution.get("outcome") or ""
        if state == "verified":
            channel = str((execution.get("created") or {}).get("channel_id") or "")
            head = (f"商品 {item} 的专属链接已经建好、绑好达人了" + (f"（渠道品 {channel}）" if channel else "")
                    + "，链接随回复发给她。")
        elif after["status"] == DROPPED:
            head = f"商品 {item} 这一轮不建专属链接了（运营点了「不发」）。"
        elif state == "uncertain":
            head = f"商品 {item} 的专属链接结果不确定（#{after['id']} {outcome}），运营会先到平台后台核对，这一轮先跳过它。"
        elif state == "not_done":
            head = (f"商品 {item} 的专属链接没建成（#{after['id']} {outcome}：{platforms.outcome_text(outcome)}），"
                    "这一轮先跳过它。")
    if not head:
        head = why + f"接着开寄样单 {progress['number']} 的专属佣金" + ("、建专属链接" if links else "") + f"（{what}）。"
    who = f"聚水潭建单时查到达人的抖音 UID 是 {progress['uid']}，creator_platform_id 就填它。" if progress["uid"] else ""
    item, which = progress["next"], progress.get("next_action") or name
    entry = next((i for i in items if i["id"] == item), {}) if item else {}
    if item and which == LINK_ACTION:
        rest = _link_steps_left(items, item, which)
        earlier = entry.get("link_packet")
        todo = (f"这位达人要专属链接（{links['said']}）。按 kb/合作政策.md 的「专属链接」：这一份草稿只填商品 {item} 的 "
                f"{LINK_ACTION}（它的专属佣金已经开好了；{link_prefix_hint(links)}；价格、库存不用填，系统按店里的做法算：日常价 9 折取整、"
                "每个颜色 100 件）；reply_draft 写建好以后发给她的话，链接用 {链接}。"
                + (f"这一轮这之后还有 {rest} 步。" if rest else "这是这一轮最后一步。")
                + (f"它上次在 #{earlier['id']} 的结果不确定，批准前运营会先到平台后台核对。" if earlier else ""))
    elif item:
        earlier = entry.get("packet")
        if links:
            rest = _link_steps_left(items, item, which)
            todo = (f"按 kb/合作政策.md 的「开专属佣金」：这一份草稿只填商品 {item} 的 {name}（一次一个商品，开过的不要再开）；"
                    + ("这位达人要专属链接：这个商品的专属佣金开好以后系统会再派一步建它的链接，这一份不要填 dy.channel_goods。"
                       if entry.get("link") in ("todo", "waits") else "")
                    + (f"这一轮这之后还有 {rest} 步。" if rest else "这是这一轮最后一步。"))
        else:
            rest = sum(1 for i in items if i["state"] == "todo" and i["id"] != item)
            todo = (f"按 kb/合作政策.md 的「开专属佣金」：这一份草稿只填商品 {item} 的 {name}（一次一个商品，开过的不要再开），"
                    + (f"这一轮这之后还要开 {rest} 个。" if rest else "这是这一轮最后一个要开的商品。"))
        todo += f"它上次在 #{earlier['id']} 的结果不确定，批准前运营会先到平台后台核对。" if earlier else ""
    elif links:
        todo = ("这一轮没有要开的专属佣金、要建的专属链接了：这一份不要填平台动作，按 kb/合作政策.md 的「开专属佣金」"
                "「专属链接」把进度记进案卷。")
    else:
        todo = "这一轮没有要开的商品了：这一份不要填平台动作，按 kb/合作政策.md 的「开专属佣金」把进度记进案卷。"
    told = []
    for state, label in CHAIN_STATES:
        rows = [i for i in items if i["state"] == state]
        if rows:
            told.append(f"{label}：" + "、".join(
                f"{r['id']}（#{r['packet']['id']}）" if state in ("not_done", "uncertain") and r.get("packet") else r["id"]
                for r in rows))
    for state, label in LINK_STATES if links else ():
        rows = [i for i in items if i.get("link") == state]
        if rows:
            told.append(f"专属链接 · {label}：" + "、".join(
                f"{r['id']}（#{r['link_packet']['id']}）" if state in ("not_done", "uncertain") and r.get("link_packet")
                else r["id"] for r in rows))
    return (head + who + todo + "机器人的记录（以它为准，记进案卷；寄样单上的商品按顺序）：" + "；".join(told) + "。")


def chain_notice_text(progress: dict, hint: str, moved_on: bool = False) -> str:
    """The deal's ``chain`` notice: how many of the order's products are open and what holds up or is left, the
    first few ids of each (``CHAIN_LIST``), and ``hint`` (how to ask again for those not opened) once nothing is
    under way; a deal that asks for 专属链接 (``progress["links"]``): the same of the links after it.  "" when every
    product is open (and, with links, every link made), or nothing is under way, left to ask for, asked for with
    nothing drafted or red and not marked dealt with; ``moved_on`` (the deal is past the sample): only under way and
    red."""
    items = progress["items"]
    links = progress.get("links")
    by = {state: [i for i in items if i["state"] == state] for state, _ in CHAIN_STATES}
    lby = {state: [{"id": i["id"], "packet": i.get("link_packet")} for i in items if i.get("link") == state]
           for state, _ in LINK_STATES} if links else {}
    opened = len(by["opened"]) + len(by["covered"])         # one the invite card opened counts as open
    if opened == len(items) and (not links or len(lby["opened"]) == len(items)):
        return ""
    red = [i for i in by["not_done"] + by["uncertain"] if not ((i["packet"] or {}).get("platform") or {}).get("acked_at")]
    red += [i for i in lby.get("not_done", []) + lby.get("uncertain", [])
            if not ((i["packet"] or {}).get("platform") or {}).get("acked_at")]
    left = by["todo"] or by["asked"] or lby.get("todo") or lby.get("asked")
    if not (progress["busy"] or red or (not moved_on and left)):
        return ""
    for limit in (CHAIN_LIST, 1, 0):
        parts = [f"开好 {opened}/{len(items)} 个" + (f"（其中邀约卡开过 {_ids(by['covered'], limit)}）" if by["covered"]
                                                     else "")]
        if by["running"]:
            parts.append(f"正在执行 {_ids(by['running'], limit, True)}")
        if by["waiting"]:
            blocked = any(((i["packet"].get("platform") or {}).get("preview") or {}).get("verdict") not in (None, "ready")
                          for i in by["waiting"] if i.get("packet"))
            parts.append(f"在待审批 {_ids(by['waiting'], limit, True)}"
                         + ("：预览没通过，可以「重新预览」，或者点「不发」跳过它，系统接着开下一个" if blocked else ""))
        drafting = progress.get("drafting")
        if progress["busy"] == "drafting" and drafting:
            parts.append("正在起草" + (f" {drafting['item']}" if drafting["item"] else "")
                         + ("的专属链接" if drafting.get("action") == LINK_ACTION else "") + f"（任务 #{drafting['job']}）")
        if progress["busy"] == "owed":
            parts.append("这一步欠着")
        if by["dropped"]:
            parts.append(f"这一轮不开 {_ids(by['dropped'], limit)}（运营点了「不发」）")
        if by["not_done"]:
            parts.append(f"没开成 {_ids(by['not_done'], limit, True)}")
        if by["uncertain"]:
            parts.append(f"结果不确定 {_ids(by['uncertain'], limit, True)}，先到平台后台核对")
        if by["asked"]:
            parts.append(f"派过、草稿没开 {_ids(by['asked'], limit)}")
        if by["todo"]:
            parts.append(f"还没开 {_ids(by['todo'], limit)}")
        text = f"专属佣金（寄样单 {progress['number']}）：" + "；".join(parts)
        if links:
            lparts = [f"建好 {len(lby['opened'])}/{len(items)} 个"]
            if lby["running"]:
                lparts.append(f"正在执行 {_ids(lby['running'], limit, True)}")
            if lby["waiting"]:
                blocked = any(((i["packet"].get("platform") or {}).get("preview") or {}).get("verdict") not in (None, "ready")
                              for i in lby["waiting"] if i.get("packet"))
                lparts.append(f"在待审批 {_ids(lby['waiting'], limit, True)}"
                              + ("：预览没通过，可以「重新预览」，或者点「不发」跳过它" if blocked else ""))
            if lby["dropped"]:
                lparts.append(f"这一轮不建 {_ids(lby['dropped'], limit)}（运营点了「不发」）")
            if lby["not_done"]:
                lparts.append(f"没建成 {_ids(lby['not_done'], limit, True)}")
            if lby["uncertain"]:
                lparts.append(f"结果不确定 {_ids(lby['uncertain'], limit, True)}，先到平台后台核对")
            if lby["asked"]:
                lparts.append(f"派过、草稿没建 {_ids(lby['asked'], limit)}")
            if lby["todo"]:
                lparts.append(f"还没建 {_ids(lby['todo'], limit)}")
            text += "。专属链接：" + "；".join(lparts)
        if not progress["busy"]:
            text += f"。{hint}"
        if len(text) <= NOTICE_CHARS or limit == 0:
            return text
    return text


def _order_at(order: dict) -> float:
    """When a sample order was approved (verified, for one that went through at once): a signature before
    it is of another shipment."""
    return float(order.get("decided_at") or order["created_at"])


def _date_text(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at))


def missing_files(named: list, arrived: list, reported: list) -> list:
    """The files a draft named that did not come, for the note on the deal and the draft: what the
    Mac said it could not send back, with why (``reported``: ``[{"name", "why"}]``), else (an older
    worker says nothing) each named one whose file name is not among the ``arrived`` names."""
    if reported:
        return [f"{_file_name(r['name'])}（{r['why']}）" if r.get("why") else _file_name(r["name"]) for r in reported]
    left, out = [str(a) for a in arrived], []
    for name in named:
        base = _file_name(name)
        if base in left:
            left.remove(base)
        else:
            out.append(base)
    return out


def _file_name(path: str) -> str:
    return str(path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or str(path)


def _refused(result) -> str:
    """Why the backend did not even try a send (``wcdb_backend.SendRefused.reason``: a chat whose name
    another chat has, a chat it cannot find in WeChat), else "": the draft says it, so the operator
    knows to send it by hand or give her a remark of her own."""
    return str(getattr(result, "reason", "") or "").strip()


def next_step_note(action: dict, record: dict, *, delivered: bool = False, links: Optional[dict] = None,
                   linked: bool = False, why: str = "") -> str:
    """What the next draft is told after a verified platform write, or "" when nothing follows:
    a sample order → open the 专属佣金 of its products (``delivered``: told when the creator
    signed for it, PRM_COMMISSION_AFTER); a 专属佣金 → the next product, if any (one action
    per draft, so one product each time; the case file says which are done); for a deal that asks for 专属链接
    (``links``: ``link_wish``) a 百应 专属佣金 → that product's link, unless it is made already (``linked``), and a link
    → the next product's 专属佣金 or link (without ``links`` a link has no next step).  Told again (the
    deal set back before 已签收 and signed for again, e.g. after the chain was cut off), the draft
    starts at the first product not done yet, so no 专属佣金 is opened twice.  A 抖店 order names
    the 抖音 UID 聚水潭 found (``creator_uid``): the draft may only know the creator's 抖音号.
    What to do comes first and the list of products last: a 专场 order's long list (up to
    ``platforms.MAX_ORDER_SKUS`` products) never pushes the instruction out of the note.  ``why``: why a link step
    comes now, said first (her prefix was recorded after the 专属佣金 opened: review 2026-11-09 F7)."""
    name = action.get("action")
    if name == "jst.sample_order":
        shop = str(action.get("shop") or "")
        if shop not in COMMISSION_ACTIONS:
            return ""
        created = record.get("created") or {}
        ids = order_items(action)
        number = str(created.get("sample_order_no") or "")
        what = f"{platforms.PLATFORMS[shop]}的商品，共 {len(ids)} 个，清单在最后"
        done = (f"达人已经签收了寄样单 {number}（{what}）。" if delivered else
                f"寄样单 {number} 已经在聚水潭建好，寄的是{what}。")
        uid = str(created.get("creator_uid") or "") if shop == "dy" else ""
        who = f"聚水潭建单时查到达人的抖音 UID 是 {uid}，creator_platform_id 就填它。" if uid else ""
        return (done + who +
                f"按 kb/合作政策.md 的「开专属佣金」给这些商品开专属佣金：这一份草稿填第一个还没开的商品的 {COMMISSION_ACTIONS[shop]}"
                "（看案卷，开过的不要再开），把哪些商品还没开记进案卷。"
                f"寄样单上的商品（按顺序）：{'、'.join(ids)}。")
    if name in COMMISSION_ACTIONS.values():
        done = (f"商品 {action.get('item_id')} 的专属佣金（{action.get('commission_rate')}%）已经在平台上开好了"
                f"（达人 {action.get('creator_platform_id')}）。")
        if links and name == COMMISSION_ACTIONS["dy"] and not linked:
            hint = link_prefix_hint(links)
            return (why + done + f"这位达人要专属链接（{links['said']}）。按 kb/合作政策.md 的「专属链接」：这一份草稿只填商品 "
                    f"{action.get('item_id')} 的 {LINK_ACTION}（creator_platform_id 填 {action.get('creator_platform_id')}；"
                    f"{hint}；价格、库存不用填，系统按店里的做法算：日常价 9 折取整、每个颜色 100 件）；reply_draft 写建好"
                    "以后发给她的话，链接用 {链接}。按 kb/合作政策.md 的「专属链接」更新案卷。")
        return (done + "这位达人寄样的商品里还有没开专属佣金的，就给这位达人填下一个；都开完了就不要填平台动作。"
                "按 kb/合作政策.md 的「开专属佣金」更新案卷。")
    if name == LINK_ACTION and links:
        return (f"商品 {action.get('item_id')} 的专属链接已经建好、绑好达人了（达人 {action.get('creator_platform_id')}），"
                "链接随回复发给她。她要专属链接的商品里还有没弄完的，就填下一步：一次一个商品，先开它的 "
                f"{COMMISSION_ACTIONS['dy']}，开好了再建它的 {LINK_ACTION}；都弄完了就不要填平台动作。"
                "按 kb/合作政策.md 的「专属链接」更新案卷。")
    return ""


def _platform_no(execution: dict) -> str:
    """The creator's 抖音号 / 小红书号 in a sample order's own CLI answer (a step's ``raw``: jst sample-create's
    ``daren.platformNo``), "" when there is none."""
    for step in execution.get("steps") or []:
        raw = step.get("raw") if isinstance(step, dict) else None
        if not isinstance(raw, str) or "platformNo" not in raw:
            continue
        data = platforms.parse_json(raw)
        daren = data.get("daren") if isinstance(data, dict) and isinstance(data.get("daren"), dict) else {}
        if daren.get("platformNo"):
            return str(daren["platformNo"])
    return ""


def _job_error(job: dict) -> str:
    """Why a relay job failed, from its result (the worker's ``error``)."""
    try:
        extra = json.loads(job.get("result_json") or "{}")
    except ValueError:
        extra = {}
    extra = extra if isinstance(extra, dict) else {}
    return str(extra.get("error") or job.get("result_text") or "没有返回内容")[:200]


def apply_deal_action(store: PrmStore, action: str, data: dict, *, now: float, takeover_sec: float,
                      actor: str = "console", review: bool = False, notice: bool = True,
                      note: Optional[dict] = None, more=()) -> dict:
    """The console's deal actions (also when the bot is stopped: straight on the database); the 聚水潭 sync's too
    (``actor`` system, ``review`` / ``notice`` / ``note`` / ``more``: ``deals.add_shipment``).  ``night_done``
    {deal_id, packet}: 「已处理」 on a holding line the night window sent (``night_done``); ``group_left_done`` {deal_id,
    chat}: 「已处理」 on her lines a closed group left unanswered (``group_left_done``).  ``opening``, the 三方群 ones,
    ``draft_note`` and ``photo_confirm`` / ``photo_unconfirm`` are the running bot's only (``PrmService.deal_action``): a
    stopped one says so."""
    try:
        deal_id = data.get("deal_id")
        if action == "stage":
            deals.set_stage(store, deal_id, str(data.get("stage") or ""), now=now, actor=actor, note=note)
        elif action == "adopt":
            deals.adopt_proposal(store, deal_id, now=now, actor=actor)
        elif action == "mode":
            deals.set_mode(store, deal_id, str(data.get("mode") or ""), now=now, takeover_sec=takeover_sec, actor=actor)
        elif action == "shipment":
            _, packet_id = deals.add_shipment(store, deal_id, data.get("carrier", ""), data.get("waybill", ""), now=now,
                                              actor=actor, review=review, notice=notice, note=note, more=more)
            return {"ok": True, "packet": packet_id}
        elif action == "shipment_status":
            deals.set_shipment_status(store, deal_id, str(data.get("status") or ""), now=now, actor=actor, note=note)
        elif action == NIGHT_DONE:
            return night_done(store, deal_id, data.get("packet"), now=now, actor=actor)
        elif action == GROUP_LEFT_DONE:
            return group_left_done(store, deal_id, data.get("chat"), now=now, actor=actor)
        elif action == OPENING:                        # (running: PrmService.ask_opening)
            return {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能写开场白"}
        elif action == HUOPAN:                         # (running: PrmService.ask_huopan)
            return {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能补发货盘"}
        elif action == HANDCARDS:                      # (running: PrmService.ask_handcards)
            return {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能做手卡"}
        elif action in GROUP_ACTIONS:                  # (running: PrmService.three_party)
            return {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能拉群"}
        elif action == GROUP_UNLINK:                   # (running: PrmService.unlink_group)
            return {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能停掉接手的群"}
        elif action == GROUP_CONFIRM:                  # (running: PrmService.confirm_group)
            return {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能确认接手的群"}
        elif action == DRAFT_NOTE:                     # (running: PrmService.set_draft_note)
            return {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能改起草备注"}
        elif action in (PHOTO_CONFIRM, PHOTO_UNCONFIRM):   # (running: PrmService.photo_confirm / photo_unconfirm)
            return {"ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能确认对图"}
        else:
            return {"ok": False, "error": f"不认识的操作 {action}"}
    except deals.DealError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def night_done(store: PrmStore, deal_id, packet=None, *, now: float, actor: str = "console") -> dict:
    """「已处理」 on a deal's notice of a holding line the night window (or 自动模式: the notice's ``auto``, the record
    keeps it) sent (``PrmService._night_sent``, ``NIGHT_NOTICE``): it goes, and the deal's record says it was seen to.
    ``packet`` None: every such notice of the deal.  Also when the bot is stopped (straight on the database)."""
    deal = deals._deal(store, deal_id)
    try:
        key = None if packet in (None, "") else f"{NIGHT_NOTICE}{int(packet)}"
    except (TypeError, ValueError):
        return {"ok": False, "error": "草稿编号不对"}
    dropped = store.drop_notices(deal["id"], lambda n: n["key"] == key if key
                                 else str(n["key"]).startswith(NIGHT_NOTICE), now)
    if not dropped:
        return {"ok": False, "error": "这条提醒已经处理过了"}
    for n in dropped:
        store.add_event(deal["id"], deal["party_id"], NIGHT_DONE,
                        {"packet": n.get("packet"), **({"auto": True} if n.get("auto") else {})}, actor=actor, now=now)
    return {"ok": True}


def group_left_done(store: PrmStore, deal_id, chat, *, now: float, actor: str = "console") -> dict:
    """「已处理」 on a deal's notice of her lines in a group whose link ended before they were answered (``GROUP_LEFT``,
    接手任何合作群 review 2026-11-09): it goes, the deal's record says so.  Also when the bot is stopped."""
    deal = deals._deal(store, deal_id)
    key = f"{GROUP_LEFT}{str(chat or '')}"
    if not store.drop_notices(deal["id"], lambda n: n["key"] == key, now):
        return {"ok": False, "error": "这条提醒已经处理过了"}
    store.add_event(deal["id"], deal["party_id"], GROUP_LEFT_DONE, {}, actor=actor, now=now)
    return {"ok": True}


def reveal_numbers(store: PrmStore, party_id: int, *, now: float, actor: str = "console") -> dict:
    """The full numbers behind a creator's placeholders, for an operator (8.2): every look is
    a ``reveal`` event (which placeholders, not the numbers)."""
    party = store.party(party_id)
    if party is None:
        return {"ok": False, "error": "没有找到这个达人"}
    rows = store.token_rows(party_id)
    deal = store.deal_of(party_id, now)
    store.add_event(deal["id"], party_id, "reveal", {"tokens": [r["token"] for r in rows]}, actor=actor, now=now)
    return {"ok": True, "numbers": [
        {"token": r["token"], "value": "" if r["purged_at"] else r["value"], "cleared": bool(r["purged_at"])}
        for r in rows]}
