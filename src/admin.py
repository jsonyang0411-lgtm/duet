"""Admin command handler — parses and executes the business account's admin commands.

Commands are only accepted from the configured admin wxid in a private chat (checked in
the route step ``admin_command``); a group message never reaches this handler.
"""

import logging
import re
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

NO_FRIEND_MANAGER = "未启用好友管理组件（需要 Windows 版微信窗口），无法执行"
HELP_COMMANDS = ("帮助", "help", "命令")
STATUS_COMMANDS = ("查状态", "账号状态")
ACCEPT_FRIENDS_COMMANDS = ("同意好友", "通过好友", "接受好友", "同意申请")
AUTO_ACCEPT_COMMANDS = {                 # whitespace-normalised text -> switch on (True) / off (False)
    "自动同意好友 开": True, "自动同意好友 开启": True, "开启自动同意": True,
    "自动同意好友开": True, "自动同意好友开启": True,
    "自动同意好友 关": False, "自动同意好友 关闭": False, "关闭自动同意": False,
    "自动同意好友关": False, "自动同意好友关闭": False,
}


# The management commands in one line each: the Claude relay's help lists them when the
# bot only relays (CLAUDE_RELAY_ONLY=on), where this handler's own help does not fit.
RELAY_HELP = (
    "管理命令（只有管理员能用，不会交给 Mac）：\n"
    "查状态 / 添加好友 号码 [备注=名字] [附言] / 同意好友 / 自动同意好友 开|关"
)

HELP_TEXT = (
    "可用命令（只在管理员私聊里有效，群聊一律不回复）：\n"
    "  查状态                        微信连接、各开关和已处理的消息数\n"
    "  添加好友 <号码> [备注=名字] [附言]  搜索并发送添加好友申请\n"
    "  同意好友 / 通过好友        立即检查并同意所有好友申请\n"
    "  自动同意好友 开 / 关        切换后台自动同意功能\n"
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

    def set_friend_manager(self, friend_manager, auto_friend_service=None) -> None:
        """Attach or update the FriendManager and AutoFriendService instances."""
        self._friend = friend_manager
        if auto_friend_service is not None:
            self._auto_friend = auto_friend_service

    def set_status(self, status: Optional[Callable[[], Iterable[str]]]) -> None:
        """Attach the Bot's status lines for 查状态."""
        self._status = status

    def handle(self, content: str) -> str | None:
        """Parse and execute an admin command. Returns reply text or None.

        Args:
            content: The message text (the backend's ``wxid_…:`` quote prefix removed).

        Returns:
            Reply text if the content was a recognized command, else None.
        """
        content = content.strip()
        if content in HELP_COMMANDS:
            return HELP_TEXT
        command = self._command(content)
        return command() if command is not None else None

    def is_command(self, content: str) -> bool:
        """A management command (not the help), recognised without running it.

        The Claude relay asks this when the bot only relays, so 「添加好友 …」 from the
        admin stays a command instead of becoming a task for the Mac.
        """
        return self._command(content.strip()) is not None

    def _command(self, content: str):
        """The management command ``content`` is, as ``run()``; else None."""
        if content in STATUS_COMMANDS:
            return self._cmd_status
        # ── Friend commands (they drive the WeChat window and take seconds) ──
        if content.split(None, 1)[:1] in (["添加好友"], ["加好友"]):
            return lambda: self._cmd_add_friend(content)
        if content in ACCEPT_FRIENDS_COMMANDS:
            return self._cmd_accept_friends
        switch = AUTO_ACCEPT_COMMANDS.get(" ".join(content.split()))
        if switch is not None:
            return lambda: self._cmd_auto_accept(switch)
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

    def _cmd_add_friend(self, content: str) -> str:
        """Handle the '添加好友' command."""
        if self._friend is None:
            return f"{NO_FRIEND_MANAGER}添加好友操作"

        parts = content.split(None, 2)
        if len(parts) < 2:
            return "请指定要添加的微信号或手机号，例如：添加好友 13800000000 申请理由"

        target = parts[1].strip()
        reason = parts[2].strip() if len(parts) > 2 else ""
        remark = ""
        named = re.match(r"备注[=＝:：]\s*(\S+)\s*(.*)$", reason, re.S)   # 添加好友 号 备注=昵称-抖音 附言
        if named:
            remark, reason = named.group(1), named.group(2).strip()
        try:
            res = (self._friend.add_friend(target, reason, remark=remark) if remark
                   else self._friend.add_friend(target, reason))
        except Exception:
            logger.exception("ADMIN: 添加好友 failed")
            return "添加好友时出错，已中止（详见日志）：请在微信里看一下申请发出去没有"
        message = str(res.get("message", "处理完成"))
        if res.get("reason") == "risk":                  # WeChat holds the account back
            message += self._pause_auto("添加好友时微信提示操作太频繁")
        return message

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
