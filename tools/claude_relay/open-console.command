#!/bin/zsh
# 不在 Windows 旁边（比如晚上在家）时，用这台 Mac 的浏览器操作 Windows 上的控制台：
# 经 SSH 隧道把 Windows 本机的 127.0.0.1:7327 接到这台 Mac 的 127.0.0.1:7327（被占用就用 17327）。
# 控制台只认本机地址（Host / Origin 是 127.0.0.1），走隧道时浏览器打开的正是 127.0.0.1，
# 所以机器人不用改，7327 也不对网络开放。Windows 要先装好 OpenSSH 服务器、放好这台 Mac 的公钥（cross-network.md 第九节）。
# Windows 的地址取 worker 的 ~/.claude-relay/config.json 里 relay_url 的主机（worker 连的同一台）；
# 登录用户名第一次运行时问一次，存在 ~/.claude-relay/console-tunnel-user（要改就删掉这个文件再运行）。
# 关掉这个窗口（或 Ctrl+C）就断开；断线 10 秒后自己重连。

printf '\e]0;Windows 控制台（SSH 隧道）\a'
conf="${CLAUDE_RELAY_CONFIG:-$HOME/.claude-relay/config.json}"
host=$(/usr/bin/python3 -c 'import json, sys, urllib.parse
print(urllib.parse.urlsplit(json.load(open(sys.argv[1])).get("relay_url", "")).hostname or "")' "$conf" 2>/dev/null)
if [[ -z $host ]]; then
  echo "在 $conf 里没找到 relay_url（Mac worker 连 Windows 用的地址），先按 cross-network.md 把 worker 配好。"
  read -k1 "?按任意键关闭"; exit 1
fi
userfile="$HOME/.claude-relay/console-tunnel-user"
user=""
[[ -r $userfile ]] && user=$(<"$userfile")
if [[ -z $user ]]; then
  read "user?Windows 的登录用户名（C:\\Users 下面那个文件夹的名字）："
  [[ -z $user ]] && exit 1
  print -r -- "$user" > "$userfile"
fi

port=7327
lsof -nP -iTCP:$port -sTCP:LISTEN >/dev/null 2>&1 && port=17327
url="http://127.0.0.1:$port"

# 隧道通了就在浏览器里打开一次（以后断线重连，页面自己会接上）
(
  for i in {1..90}; do
    if curl -s -o /dev/null --max-time 2 "$url/"; then open "$url"; exit 0; fi
    sleep 1
  done
) &

while true; do
  echo "── $(date '+%m-%d %H:%M') 连接 $user@$host，浏览器用 $url"
  ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
      -o StrictHostKeyChecking=accept-new -L "127.0.0.1:$port:127.0.0.1:7327" "$user@$host"
  echo "隧道断了（ssh 退出码 $?），10 秒后重连；不用了就关掉这个窗口。"
  sleep 10 || break
done
