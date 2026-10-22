# Claude 中转：微信收消息，Mac 上的 Claude Code 干活

Windows 上的机器人只负责收发微信消息。你私聊机器人发 `cc 任务内容`，机器人先回一句「收到（#编号）」，把任务放进自己的队列。Mac 上的 worker 主动来取任务，在指定项目目录里运行 `claude -p` 完成，再把结果交回机器人，机器人发回给你。

```
你(微信私聊) ─► Windows 机器人 ─► 任务队列 data/claude_relay.db
                    ▲                 │  Mac 每 25 秒来问一次（长轮询）
                    │                 ▼
                    └── 结果 ◄── Mac: worker.py ─► claude -p
```

- Mac 不用开任何端口，只有 Windows 开一个（默认 8790）。
- Mac 睡眠或断网时，任务留在 Windows 队列里，Mac 回来后接着做。
- 结果先写进 Mac 的 `~/.claude-relay/outbox/`，机器人确认收到后才删。Windows 重启也不会丢结果。
- 开关默认关闭（`CLAUDE_RELAY=off`）。关闭时机器人的行为和原来完全一样。

## 微信里怎么用

| 发 | 作用 |
|---|---|
| `cc 帮我看看 douyin 项目的测试为什么挂了` | 交给 Mac，在默认项目目录里执行 |
| `cc @douyin 跑一下测试` | 在 Mac 上叫 `douyin` 的项目目录里执行 |
| `cc 状态` | 最近 5 个任务，以及 Mac 是否在线 |
| `cc 取消` / `cc 取消 17` | 取消最近的任务或 #17：还没开始的直接取消，正在执行的会让 Mac 停下 |
| `cc 重试 17` | 重新执行 #17 |
| `cc 新对话` | 下一个任务开新会话（默认会接着上一个任务的上下文） |
| `cc` / `cc 帮助` | 显示上面这些 |

- 同一个私聊里的任务默认接着同一个 Claude 会话，所以可以说「再把覆盖率也看一下」。
- 只认 `CLAUDE_RELAY_WXIDS`（不填就是 `ADMIN_WXID`）发来的**私聊**。群里的消息、别人发的 `cc` 都当普通消息处理。
- 超过 5 分钟的旧消息不会执行，防止机器人重启时把历史消息当成新任务。

## Windows 端（机器人）

1. 生成一个随机 token，两边要填同一个：
   ```
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. 在 `.env` 里加上下面这些（`.env.example` 末尾有说明）：
   ```
   PRIVATE_CHAT_ENABLED=true
   PRIVATE_CHATS=你自己的微信名或wxid      # 只想让自己的私聊被处理时这样填；"*" 是所有私聊
   ADMIN_WXID=wxid_你自己
   CLAUDE_RELAY=on
   CLAUDE_RELAY_TOKEN=上一步生成的串
   CLAUDE_RELAY_PORT=8790
   ```
3. 防火墙只放行局域网访问这个端口。用管理员 PowerShell 运行，把网段换成你的：
   ```powershell
   New-NetFirewallRule -DisplayName "webot-claude-relay" -Direction Inbound -Protocol TCP `
     -LocalPort 8790 -RemoteAddress 192.168.1.0/24 -Action Allow
   ```
4. 在路由器里给 Windows 绑定一个固定 IP，然后重启机器人。日志 `data/bot.log` 里应该出现：
   `CLAUDE_RELAY: waiting for the Mac worker on 0.0.0.0:8790`

## Mac 端（worker）

1. worker 只用 Python 标准库，系统自带的 `/usr/bin/python3` 就够了。
2. 写配置文件 `~/.claude-relay/config.json`，参照同目录的 `config.example.json`：
   ```
   mkdir -p ~/.claude-relay && cp tools/claude_relay/config.example.json ~/.claude-relay/config.json
   chmod 600 ~/.claude-relay/config.json      # 里面有 token
   ```
   - `relay_url`：`http://<Windows 的局域网 IP>:8790`
   - `projects`：`cc @别名` 能用的项目目录。只有这里列出的目录才能被指定。
   - `default_project`：不写 `@别名` 时用哪个，可以填别名或目录。
   - `permission_mode`：默认 `auto`，每个操作由分类器审核。没人能现场批准，所以要人工批准的操作会被直接拒绝，结果里会列出被拒绝的操作。
   - `timeout_min`：单个任务的最长时间，超时自动停下。`max_budget_usd`：单个任务的花费上限，0 表示不限。
   - `claude_bin`：留空会依次查找 PATH、常见安装位置、VS Code 扩展里的 claude。
3. 自检，能连上机器人、能找到 claude 才算通过：
   ```
   python3 tools/claude_relay/worker.py --check
   ```
4. 先手动跑一个任务试试：`python3 tools/claude_relay/worker.py --once`，然后在微信里发 `cc 只回复：收到`。
5. 平时运行 `python3 tools/claude_relay/worker.py`，按 Ctrl+C 停止。也可以用 launchd 常驻：开机自启，崩溃自动重启，插着电源时不睡眠（用电池时照常睡眠）。
   ```
   sed "s#/PATH/TO/webot-claude-relay#$(pwd)#" tools/claude_relay/com.webot.claude-relay.plist \
     > ~/Library/LaunchAgents/com.webot.claude-relay.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.webot.claude-relay.plist
   # 停止：launchctl bootout gui/$(id -u)/com.webot.claude-relay
   ```
   - 日志在 `~/.claude-relay/worker.log`，每个任务的完整输出在 `~/.claude-relay/logs/`。
   - MacBook 合盖后仍会睡眠，除非接着电源和外接显示器。睡眠期间任务会在 Windows 上排队等待。

## 常见问题

- **`--check` 显示 unreachable**：Mac 开着 Clash。worker 已经绕过系统代理，但如果开了 TUN 模式，要在 Clash 里把局域网网段设为直连，例如 `IP-CIDR,192.168.0.0/16,DIRECT`。再确认 Windows 防火墙规则和 IP 没变。
- **HTTP 401**：两边的 token 不一致。
- **换 token**：先把新 token 写进 Mac 的 `config.json`，正在运行的 worker 继续用旧的；等你把 Windows 的 `CLAUDE_RELAY_TOKEN` 换掉并重启机器人，worker 收到 401 后会自动重新读取 `config.json`，换成新的，不用重启 worker。
- **回执里说「Mac 不在线」**：60 秒内 worker 没来取过任务。看 `worker.log`。
- **#编号「失联」**：正在执行的任务超过 2 分钟没有心跳，通常是 Mac 睡眠或断网了。Mac 恢复后如果任务还在跑，结果会照常发回来；否则回复 `cc 重试 编号`。
- **安全**：能连上这个端口、又知道 token 的人，就能让 Mac 执行命令。token 不要外传，端口只对局域网开放。worker 给 Claude 的说明里要求它不做推送代码、发消息这类对外或难以撤销的事，除非任务里写明。
