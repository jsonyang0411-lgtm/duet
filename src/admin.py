"""Admin command handler — parses and executes the business account's admin commands.

Commands are only accepted from the configured admin wxid in a private chat (checked in
the route step ``admin_command``); a group message never reaches this handler.
"""

import logging
import re
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

NO_FRIEND_MANAGER = "未启用好友管理组件（需要 Windows 版微信窗口），无法执行"
_ADD_FRIEND_CMD = re.compile(r"^(?:添加好友|加好友)(?:[:：\s]|$)", re.IGNORECASE)
_ADD_FRIEND_STRIP = re.compile(r"^(?:添加好友|加好友)\s*[:：]?\s*", re.IGNORECASE)
HELP_COMMANDS = ("帮助", "help", "命令")
STATUS_COMMANDS = ("查状态", "账号状态")
ACCEPT_FRIENDS_COMMANDS = ("同意好友", "通过好友", "接受好友", "同意申请")
AUTO_ACCEPT_COMMANDS = {                 # whitespace-normalised text -> switch on (True) / off (False)
    "自动同意好友 开": True, "自动同意好友 开启": True, "开启自动同意": True,
    "自动同意好友开": True, "自动同意好友开启": True,
    "自动同意好友 关": False, "自动同意好友 关闭": False, "关闭自动同意": False,
    "自动同意好友关": False, "自动同意好友关闭": False,
}
# 加百应达人微信 (src/prm/friend_adds.py, PRM_FRIEND_ADD): the records' list, one add, not to be added.
FRIEND_ADDS_COMMANDS = ("待加达人", "待加的达人")
FRIEND_ADD_COMMAND = re.compile(r"^(不加达人|加达人)\s*#?\s*(\d{1,9})$")
NO_FRIEND_ADDS = "加百应达人微信没打开（要达人副驾驶和 PRM_FRIEND_ADD）"
# 接手任何合作群 (src/prm/group_link.py): 「接群」, 「接群 N / 群名」, 「是 N」, 「不接」, 「不接群 群名」
GROUP_LINK_COMMAND = re.compile(r"^不?接群")
NO_GROUP_LINKS = "接群要先打开达人副驾驶（PRM_COPILOT）"


# The management commands in one line each: the Claude relay's help lists them when the
# bot only relays (CLAUDE_RELAY_ONLY=on), where this handler's own help does not fit.
RELAY_HELP = (
    "管理命令（只有管理员能用，不会交给 Mac）：\n"
    "查状态 / 添加好友 号码 [附言] / 同意好友 / 自动同意好友 开|关 / 待加达人 / 加达人 编号 / 不加达人 编号\n"
    "接群 / 接群 编号或群名 / 不接群 群名（接手一个合作群：她在群里说的话起草）"
)

HELP_TEXT = (
    "可用命令（只在管理员私聊里有效，群聊一律不回复）：\n"
    "  查状态                        微信连接、各开关和已处理的消息数\n"
    "  添加好友 <号码> [附言]     搜索并发送添加好友申请\n"
    "  同意好友 / 通过好友        立即检查并同意所有好友申请\n"
    "  自动同意好友 开 / 关        切换后台自动同意功能\n"
    "  待加达人                    百应达人留的、还没加的微信（号码打码）\n"
    "  加达人 <编号> / 不加达人 <编号>  发一次好友申请 / 记成不加\n"
    "  接群 / 接群 <编号或群名>    接手一个合作群：选谁是达人，她在群里说的话起草（不接群 <群名> 停掉）\n"
    "  帮助 / help                 显示本帮助\n"
    "开了中转时，cc 开头的私聊转给 Mac 上的 Claude Code（cc 帮助 看中转命令）"
)


class AdminCommandHandler:
    """Parse and execute admin commands from the bot's admin user.

    Commands (admin only, private chat):
      查状态 / 账号状态          → the Bot's status lines and the friend switches
      添加好友 <微信号/手机号> [附言] → 搜索并发送好友申请
      同意好友 / 通过好友        → 立即检查并同意所有待处理的好友申请
      自动同意好友 开 / 关      → 切换后台自动同意开关
      待加达人 / 加达人 N / 不加达人 N → 加百应达人微信（src/prm/friend_adds.py）的记录、发一次申请、不加
      接群 / 接群 N|群名 / 是 N / 不接 / 不接群 群名 → 接手任何合作群（src/prm/group_link.py；「是 N」「不接」只在
                                 问着的时候算命令）
      做手卡 / 补发货盘 / 写开场白 / 签收 / 备注 / 接管 / 副驾驶 / 合作单 <达人>、指令、选 N → 合作单指令
                                 （src/prm/admin_commands.py，PRM_WECHAT_COMMANDS 开着时；「选 N」只在问着的时候算命令）
      帮助 / help / 命令        → show help text

    Usage:
        handler = AdminCommandHandler(friend_manager=friend_manager)
        reply = handler.handle("同意好友")
    """

    def __init__(self, friend_manager=None, auto_friend_service=None,
                 status: Optional[Callable[[], Iterable[str]]] = None):
        """
        Args:
            friend_manager: Optional FriendManager instance for adding/accepting friends.
            auto_friend_service: Optional AutoFriendService instance for toggling auto-accept.
            status: Optional callable returning the Bot's status lines (WeChat, switches, counts).
        """
        self._friend = friend_manager
        self._auto_friend = auto_friend_service
        self._status = status
        self._friend_adds = None
        self._group_links = None
        self._deal_commands = None

    def set_friend_manager(self, friend_manager, auto_friend_service=None) -> None:
        """Attach or update the FriendManager and AutoFriendService instances."""
        self._friend = friend_manager
        if auto_friend_service is not None:
            self._auto_friend = auto_friend_service

    def set_status(self, status: Optional[Callable[[], Iterable[str]]]) -> None:
        """Attach the Bot's status lines for 查状态."""
        self._status = status

    def set_friend_adds(self, friend_adds) -> None:
        """加百应达人微信 (src/prm/friend_adds.py ``FriendAdds``) for 待加达人 / 加达人 N / 不加达人 N."""
        self._friend_adds = friend_adds

    def set_group_links(self, group_links) -> None:
        """接手任何合作群 (src/prm/group_link.py ``GroupLinks``) for 接群 / 是 N / 不接 / 不接群."""
        self._group_links = group_links

    def set_deal_commands(self, deal_commands) -> None:
        """合作单指令 (src/prm/admin_commands.py ``DealCommands``, PRM_WECHAT_COMMANDS on) for 做手卡 <达人> … / 指令 / 选 N."""
        self._deal_commands = deal_commands

    @property
    def can_add_friends(self) -> bool:
        """There is a friend manager to drive the WeChat window (加百应达人微信 shows its button only then)."""
        return self._friend is not None

    def handle(self, content: str, msg: Optional[dict] = None) -> str | None:
        """Parse and execute an admin command. Returns reply text or None.

        Args:
            content: The message text (the backend's ``wxid_…:`` quote prefix removed).
            msg: The message itself (the route step ``admin_command``): 合作单指令 runs only typed text in the
                admin's own chat (a voice message or a quote is told to type it); the other commands do not look.

        Returns:
            Reply text if the content was a recognized command, else None.
        """
        content = content.strip()
        if content in HELP_COMMANDS:
            return HELP_TEXT + (self._deal_commands.help_line if self._deal_commands is not None else "")
        command = self._command(content, msg)
        return command() if command is not None else None

    def redaction(self, content: str, msg: Optional[dict] = None) -> Optional[str]:
        """What the decision trace keeps instead of this line (the route step ``admin_command``): 合作单指令's
        ``DealCommands.trace_text`` for a line of theirs (names, a note's words: never in data/decision_trace.jsonl,
        as the review's step redacts its answers); None for every other line (kept as before)."""
        commands = self._deal_commands
        if commands is None:
            return None
        try:
            return commands.trace_text if commands.admin_command(content.strip(), msg) is not None else None
        except Exception:
            logger.exception("ADMIN: could not tell whether a line is a 合作单指令")
            return None

    def is_command(self, content: str) -> bool:
        """A management command (not the help), recognised without running it.

        The Claude relay asks this when the bot only relays, so 「添加好友 …」 from the
        admin stays a command instead of becoming a task for the Mac.
        """
        return self._command(content.strip()) is not None

    def _command(self, content: str, msg: Optional[dict] = None):
        """The management command ``content`` is, as ``run()``; else None (``msg``: the message, for 合作单指令)."""
        if content in STATUS_COMMANDS:
            return self._cmd_status
        # ── Friend commands (they drive the WeChat window and take seconds) ──
        if _ADD_FRIEND_CMD.match(content):
            return lambda: self._cmd_add_friend(content)
        if content in ACCEPT_FRIENDS_COMMANDS:
            return self._cmd_accept_friends
        switch = AUTO_ACCEPT_COMMANDS.get(" ".join(content.replace("：", " ").replace(":", " ").split()))
        if switch is not None:
            return lambda: self._cmd_auto_accept(switch)
        if content in FRIEND_ADDS_COMMANDS:
            return self._cmd_friend_adds
        match = FRIEND_ADD_COMMAND.match(content)
        if match:
            add, add_id = match.group(1) == "加达人", int(match.group(2))
            return lambda: self._cmd_friend_add(add, add_id)
        if self._deal_commands is not None:        # (「选 N」 only while its list is open)
            command = self._deal_commands.admin_command(content, msg)
            if command is not None:
                return command
        if self._group_links is not None:          # (「是 N」 and 「不接」 only while its question is open)
            return self._group_links.admin_command(content)
        if GROUP_LINK_COMMAND.match(content):
            return lambda: NO_GROUP_LINKS
        return None

    # ── Command implementations ────────────────────────────────────

    def _cmd_status(self) -> str:
        """Handle the '查状态' command."""
        lines = ["账号状态："]
        if self._status is not None:
            try:
                lines.extend(str(line) for line in self._status())
            except Exception:
                logger.exception("ADMIN: could not read the bot's status")
                lines.append("运行状态读取失败（详见日志）")
        if self._friend is None:
            lines.append("好友管理：未启用（需要 Windows 版微信窗口）")
        else:
            lines.append("好友管理：可用")
        if self._auto_friend is not None:
            note = self._auto_note() if self._auto_friend.enabled else ""
            lines.append(f"自动同意好友：{'开' if self._auto_friend.enabled else '关'}" + (f"（{note}）" if note else ""))
        return "\n".join(lines)

    def _auto_note(self) -> str:
        """The auto-accept worker's day (``status_note``: today's count against the cap, or why it is
        paused); "" when it cannot tell."""
        try:
            note = self._auto_friend.status_note()
        except Exception:
            logger.debug("ADMIN: the auto-accept status could not be read", exc_info=True)
            return ""
        return note if isinstance(note, str) else ""

    def _pause_auto(self, why: str) -> str:
        """WeChat held the account back during a command: auto-accept pauses too (the admin hears it in the
        command's answer); the words to add to that answer."""
        pause = getattr(self._auto_friend, "pause", None)
        if self._auto_friend is None or not callable(pause):
            return ""
        try:
            pause(why, tell=False)
        except Exception:
            logger.warning("ADMIN: auto-accept could not be paused", exc_info=True)
            return ""
        return "；自动同意好友也已暂停（发「自动同意好友 开」恢复）"

    def add_friend_checked(self, target: str, greeting: str = "", remark: str = "") -> dict:
        """One ``add_friend`` call with the breaker of the 添加好友 command: WeChat's risk prompt pauses the auto-accept
        too (``paused``: the words saying so, "" without an auto-accept service).  No friend manager: ``reason``
        ``no_manager``; an exception: logged, ``reason`` ``error``.  加百应达人微信 (src/prm/friend_adds.py) adds this
        way, with her ``remark`` (passed on only when there is one: the 添加好友 command calls ``add_friend(target,
        greeting)`` as it always did); the answer's ``message`` holds the target as typed (its callers decide what to
        show), ``remark_set`` whether the remark went into the application window."""
        if self._friend is None:
            return {"ok": False, "target": target, "reason": "no_manager", "message": f"{NO_FRIEND_MANAGER}添加好友操作"}
        try:
            res = (self._friend.add_friend(target, greeting, remark=remark) if remark
                   else self._friend.add_friend(target, greeting))
        except Exception:
            logger.exception("ADMIN: 添加好友 failed")
            return {"ok": False, "target": target, "reason": "error",
                    "message": "添加好友时出错，已中止（详见日志）：请在微信里看一下申请发出去没有"}
        res = dict(res) if isinstance(res, dict) else {"ok": False, "target": target, "reason": "error"}
        if res.get("reason") == "risk":                  # WeChat holds the account back
            res["paused"] = self._pause_auto("添加好友时微信提示操作太频繁")
        return res

    def create_group_checked(self, members, *, dry_run: bool = False) -> dict:
        """One ``create_group`` call (三方群, src/prm/three_party.py: the 「发起群聊」 window, ``dry_run`` for 「先试一下」)
        with the 添加好友 command's breaker: WeChat's risk prompt pauses the auto-accept too (``paused``: the words
        saying so).  No friend manager: ``reason`` ``no_manager``; an exception: logged, ``reason`` ``error``."""
        create = getattr(self._friend, "create_group", None)
        if not callable(create):
            return {"ok": False, "reason": "no_manager", "message": f"{NO_FRIEND_MANAGER}拉群操作"}
        try:
            res = create(list(members), dry_run=dry_run)
        except Exception:
            logger.exception("ADMIN: 拉群 failed")
            return {"ok": False, "reason": "error", "message": "拉群时出错，已中止（详见日志）：请在微信里看一下群建了没有"}
        res = dict(res) if isinstance(res, dict) else {"ok": False, "reason": "error",
                                                         "message": "拉群时出错，已中止：请在微信里看一下群建了没有"}
        if res.get("reason") == "risk":                  # WeChat holds the account back
            res["paused"] = self._pause_auto("拉群时微信提示操作太频繁")
        return res

    def _cmd_add_friend(self, content: str) -> str:
        """Handle the '添加好友' command."""
        if self._friend is None:
            return f"{NO_FRIEND_MANAGER}添加好友操作"

        rest = _ADD_FRIEND_STRIP.sub("", content).strip()
        parts = rest.split(None, 1)
        if not parts or not parts[0].strip():
            return "请指定要添加的微信号或手机号，例如：添加好友 13800000000 申请理由"

        target = parts[0].strip()
        reason = parts[1].strip() if len(parts) > 1 else ""
        res = self.add_friend_checked(target, reason)
        return str(res.get("message", "处理完成")) + str(res.get("paused") or "")

    def _cmd_friend_adds(self) -> str:
        """「待加达人」: 加百应达人微信's records not done yet (numbers masked)."""
        if self._friend_adds is None:
            return NO_FRIEND_ADDS
        return self._friend_adds.admin_pending_text()

    def _cmd_friend_add(self, add: bool, add_id: int) -> str:
        """「加达人 N」 (one add, the console's checks; the answer after the window action) / 「不加达人 N」."""
        if self._friend_adds is None:
            return NO_FRIEND_ADDS
        return self._friend_adds.admin_add(add_id) if add else self._friend_adds.admin_drop(add_id)

    def _cmd_accept_friends(self) -> str:
        """Handle the '同意好友' command."""
        if self._friend is None:
            return f"{NO_FRIEND_MANAGER}同意好友操作"

        try:
            accepted = self._friend.accept_friend_requests()
        except Exception:
            logger.exception("ADMIN: 同意好友 failed")
            return "同意好友时出错，已停止（详见日志）"
        risk = getattr(accepted, "risk", "")
        if isinstance(risk, str) and risk:
            done = f"（已同意 {len(accepted)} 位）" if accepted else ""
            return (f"微信提示「{risk}」，可能是操作太频繁被限制了，已停止{done}：先别再加好友、同意好友，过几个小时再试"
                    + self._pause_auto(f"同意好友时微信提示「{risk}」"))
        if getattr(accepted, "error", ""):
            done = f"，之前已同意 {len(accepted)} 位" if accepted else ""
            return f"同意好友时出错，已停止{done}（详见日志）"
        if accepted:
            return f"已成功同意 {len(accepted)} 位新好友申请"
        return "没有同意任何申请：没有待处理的申请，或微信窗口/识别出了问题（详见日志）"

    def _cmd_auto_accept(self, on: bool) -> str:
        """Handle the '自动同意好友 开/关' commands."""
        if self._auto_friend is None:
            return f"{NO_FRIEND_MANAGER}自动同意开关"
        if on:
            self._auto_friend.enable()
            note = self._auto_note()
            return "自动同意好友申请功能已开启" + (f"（{note}）" if note else "")
        self._auto_friend.disable()
        return "自动同意好友申请功能已关闭"
