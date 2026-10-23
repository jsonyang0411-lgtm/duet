"""Admin command handler — parses and executes admin-only commands.

Commands are only accepted from the configured admin wxid (checked in router).
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

NO_FRIEND_MANAGER = "未启用好友管理组件（需要 Windows 版微信窗口），无法执行"
ACCEPT_FRIENDS_COMMANDS = ("同意好友", "通过好友", "接受好友", "同意申请")
AUTO_ACCEPT_COMMANDS = {                 # whitespace-normalised text -> switch on (True) / off (False)
    "自动同意好友 开": True, "自动同意好友 开启": True, "开启自动同意": True,
    "自动同意好友 关": False, "自动同意好友 关闭": False, "关闭自动同意": False,
}


class AdminCommandHandler:
    """Parse and execute admin commands from the bot's admin user.

    Commands (admin only):
      改名 wxid_xxx = 新昵称    → add/update nickname mapping
      删除昵称 wxid_xxx         → remove nickname mapping
      刷新昵称                   → reload from JSON
      添加好友 <微信号/手机号> [附言] → 搜索并发送好友申请
      同意好友 / 通过好友        → 立即检查并同意所有待处理的好友申请
      自动同意好友 开 / 关      → 切换后台自动同意开关

    Commands (any user):
      帮助 / help / 命令        → show help text

    Usage:
        handler = AdminCommandHandler(nickname_service, friend_manager=friend_manager)
        reply = handler.handle("改名 wxid_abc = 张三", "AdminName")
    """

    def __init__(self, nickname_service, friend_manager=None, auto_friend_service=None):
        """
        Args:
            nickname_service: NicknameService instance for persistence.
            friend_manager: Optional FriendManager instance for adding/accepting friends.
            auto_friend_service: Optional AutoFriendService instance for toggling auto-accept.
        """
        self._nicks = nickname_service
        self._friend = friend_manager
        self._auto_friend = auto_friend_service

    def set_friend_manager(self, friend_manager, auto_friend_service=None) -> None:
        """Attach or update the FriendManager and AutoFriendService instances."""
        self._friend = friend_manager
        if auto_friend_service is not None:
            self._auto_friend = auto_friend_service

    def handle(self, content: str, requester_name: str) -> str | None:
        """Parse and execute an admin command. Returns reply text or None.

        Args:
            content: Cleaned message content (no @bot prefix).
            requester_name: Display name of the admin user.

        Returns:
            Reply text if the content was a recognized command, else None.
        """
        content = content.strip()

        # ── Help command (any user) ─────────────────────────────
        if content in ("帮助", "help", "命令"):
            return (
                f"@{requester_name} 可用命令：\n"
                "━━━━━━━━━━━━━━\n"
                "【管理命令】（仅管理员可用）：\n"
                "  改名 wxid_xxx = 昵称    添加/修改昵称映射\n"
                "  删除昵称 wxid_xxx         删除昵称映射\n"
                "  刷新昵称                    重新加载昵称缓存\n"
                "  添加好友 <号码> [附言]     搜索并发送添加好友申请\n"
                "  同意好友 / 通过好友        立即检查并同意所有好友申请\n"
                "  自动同意好友 开 / 关        切换后台自动同意功能\n\n"
                "【全员命令】：\n"
                "  抽签                          随机抽一支签\n"
                "  塔罗牌 / 抽塔罗             随机抽一张塔罗牌\n"
                "  帮助 / help                 显示本帮助（就是你正在看的这条）\n\n"
                "【AI 功能】（@bot 即可）：\n"
                "  总结：之前发了什么 / 总结一下 / 说了什么\n"
                "  对话：@bot + 你想说的话，直接聊天\n"
            )

        # ── 改名 command ────────────────────────────────────────
        if content.startswith("改名 "):
            return self._cmd_rename(content, requester_name)

        # ── 删除昵称 command ────────────────────────────────────
        if content.startswith("删除昵称 "):
            return self._cmd_delete_nickname(content, requester_name)

        # ── 刷新昵称 command ────────────────────────────────────
        if content.strip() == "刷新昵称":
            self._nicks.load(force=True)
            count = len(self._nicks.load())
            return f"@{requester_name} 昵称缓存已刷新，当前 {count} 条"

        # ── Friend commands (they drive the WeChat window and take seconds) ──
        if content.split(None, 1)[:1] in (["添加好友"], ["加好友"]):
            return self._cmd_add_friend(content, requester_name)

        if content in ACCEPT_FRIENDS_COMMANDS:
            return self._cmd_accept_friends(requester_name)

        switch = AUTO_ACCEPT_COMMANDS.get(" ".join(content.split()))
        if switch is not None:
            return self._cmd_auto_accept(switch, requester_name)

        return None

    # ── Command implementations ────────────────────────────────────

    def _cmd_rename(self, content: str, requester_name: str) -> str | None:
        """Handle the '改名' command."""
        rest = content[3:].strip()

        # Parse: "wxid_xxx = 新昵称"  or  "wxid_xxx 新昵称"
        if "=" in rest:
            parts = rest.split("=", 1)
        else:
            parts = rest.split(None, 1)  # split on first whitespace

        if len(parts) != 2:
            return None

        wxid = parts[0].strip()
        nickname = parts[1].strip()

        if not wxid or not nickname:
            return None

        self._nicks.update(wxid, nickname)
        return f"@{requester_name} 已更新：{wxid} → {nickname}"

    def _cmd_delete_nickname(self, content: str,
                             requester_name: str) -> str | None:
        """Handle the '删除昵称' command."""
        wxid = content[5:].strip()
        if not wxid:
            return None

        self._nicks.remove(wxid)
        return f"@{requester_name} 已删除：{wxid} 的昵称"

    def _cmd_add_friend(self, content: str, requester_name: str) -> str:
        """Handle the '添加好友' command."""
        if self._friend is None:
            return f"@{requester_name} {NO_FRIEND_MANAGER}添加好友操作"

        parts = content.split(None, 2)
        if len(parts) < 2:
            return f"@{requester_name} 请指定要添加的微信号或手机号，例如：添加好友 13800000000 申请理由"

        target = parts[1].strip()
        reason = parts[2].strip() if len(parts) > 2 else ""
        res = self._friend.add_friend(target, reason)
        return f"@{requester_name} {res.get('message', '处理完成')}"

    def _cmd_accept_friends(self, requester_name: str) -> str:
        """Handle the '同意好友' command."""
        if self._friend is None:
            return f"@{requester_name} {NO_FRIEND_MANAGER}同意好友操作"

        accepted = self._friend.accept_friend_requests()
        if accepted:
            return f"@{requester_name} 已成功同意 {len(accepted)} 位新好友申请"
        return f"@{requester_name} 没有同意任何申请：没有待处理的申请，或微信窗口/识别出了问题（详见日志）"

    def _cmd_auto_accept(self, on: bool, requester_name: str) -> str:
        """Handle the '自动同意好友 开/关' commands."""
        if self._auto_friend is None:
            return f"@{requester_name} {NO_FRIEND_MANAGER}自动同意开关"
        if on:
            self._auto_friend.enable()
            return f"@{requester_name} 自动同意好友申请功能已开启"
        self._auto_friend.disable()
        return f"@{requester_name} 自动同意好友申请功能已关闭"
