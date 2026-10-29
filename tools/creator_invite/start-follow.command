#!/bin/zsh
# 达人回没回邀约卡（invite.py follow）：在一个终端窗口里每 30 分钟查一次，回了 / 拒了就在这台 Mac 上弹通知。
# 加进「系统设置 › 通用 › 登录项」开机就跑（和 start-prm-worker.command 一样）。用终端跑是因为仓库在「桌面」上：
# launchd 的后台任务读不了桌面（2026-10-29 试过：Operation not permitted），终端可以。
# 配置默认是 ~/.claude-relay/platform/creator-invites/config.json，要用别的就先设 CREATOR_INVITE_CONFIG。
# Ctrl+C 或关掉窗口就停；北京时间 follow_hours 之外、正在发卡时，那一轮自己跳过。

cd "${0:A:h}/../.." || { echo "repository folder not found"; read; exit 1; }
printf '\e]0;邀约卡回复提醒（follow）\a'
config="${CREATOR_INVITE_CONFIG:-$HOME/.claude-relay/platform/creator-invites/config.json}"
while true; do
  echo "── $(date '+%m-%d %H:%M') follow"
  /usr/bin/python3 tools/creator_invite/invite.py follow --config "$config"
  sleep 1800 || break
done
echo "follow stopped. Close this window, or run this file again to restart."
