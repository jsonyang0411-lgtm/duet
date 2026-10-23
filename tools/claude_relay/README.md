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
| `cc 项目` | 现在用哪个项目、Mac 上能用哪些 |
| `cc 切到 douyin` | 之后的任务都在 douyin 里做（开新对话）；`@项目` 只管一个任务 |
| `cc` / `cc 帮助` | 显示上面这些 |

- 同一个私聊里的任务默认接着同一个 Claude 会话，所以可以说「再把覆盖率也看一下」。
- 机器人开着「只做中转」（`CLAUDE_RELAY_ONLY=on`）时，不用打 `cc`：你私聊发的每句话都是任务，「状态」「取消」「项目」这些照样能用。
- 可以发语音：Windows 机器人打开语音识别（`VOICE_ASR_ENABLED=true`）后，语音转成的文字就是任务，回执最后会写「听到的是：…」，识别错了就回「取消」。只做中转时直接说；没开只做中转时，语音也要以「cc」开头。没转出文字的语音会提示你改成打字。
- Mac 上改了 `config.json`（比如加项目），worker 会自动重新读取，不用重启。
- 结果太长（超过 `CLAUDE_RELAY_MAX_CHARS`，默认 1500 字）时，Windows 机器人会把完整结果存成 `中转结果-17.txt` 发给你，再发一条带开头的消息。用 `CLAUDE_RELAY_LONG_FILE=off` 关掉，或者机器人不能发文件（macOS 后端）时，照旧截断。
- 任务排过队（前面有任务、或者 Mac 当时不在线）的话，开始时会说「#17 开始了」。做得久的任务每 3 分钟发一次进度，比如「#17 进行中（6分钟）：第 14 步 · 改 service.py · 计划 3/7」，进度没变就不发。间隔用 Windows 上的 `CLAUDE_RELAY_PROGRESS_MIN` 改，0 表示不发。`cc 状态` 里也能看到正在做的那一步。
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
   - token 单独放在 `~/.claude-relay/token` 文件里（`token_file` 指向它），内容只有和 Windows 上 `CLAUDE_RELAY_TOKEN` 相同的那一串，`chmod 600`。这样改配置时 token 不会跟着出现，也可以直接写在 `token` 字段里。
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
5. 长期运行（推荐）：双击 `tools/claude_relay/start-worker.command`，它会在一个终端窗口里运行 worker（可以最小化）。
   - 第一次运行时，macOS 会问「终端」能不能访问这个文件夹，要点「允许」。
   - 插着电源时不睡眠，用电池时照常睡眠。
   - worker 崩溃后 30 秒自动重启；按 Ctrl+C 或关掉窗口就停止。
   - 想开机自动运行，就把这个文件加进「系统设置 → 通用 → 登录项」。或者在仓库根目录运行：
     ```
     osascript -e "tell application \"System Events\" to make login item at end with properties {path:\"$(pwd)/tools/claude_relay/start-worker.command\", hidden:false}"
     ```
   - 同一时间只会运行一个 worker（锁文件 `~/.claude-relay/worker.lock`），多开的会自动退出。
   - 日志在 `~/.claude-relay/worker.log`，每个任务的完整输出在 `~/.claude-relay/logs/`。
   - MacBook 合盖后仍会睡眠，除非接着电源和外接显示器。睡眠期间任务会在 Windows 上排队等待。
6. 为什么不用 launchd 后台服务：macOS 不允许后台服务读写「桌面」「文稿」「下载」这几个受保护的文件夹。worker 或项目放在这些文件夹里时，后台服务会报 `Operation not permitted`。
   - 项目都不在这些文件夹里时，可以改用 `com.webot.claude-relay.plist`：把 `/PATH/TO/webot-claude-relay` 换成实际路径，复制到 `~/Library/LaunchAgents/`，再执行 `launchctl bootstrap gui/$(id -u) <plist>`。
   - 也可以在「完全磁盘访问」里加入 python3 来解决，但这样所有用这个 Python 跑的脚本都能读写全部文件，权限太大，不推荐。

## 常见问题

- **`--check` 显示 unreachable**：Mac 开着 Clash。worker 已经绕过系统代理，但如果开了 TUN 模式，要在 Clash 里把局域网网段设为直连，例如 `IP-CIDR,192.168.0.0/16,DIRECT`。再确认 Windows 防火墙规则和 IP 没变。
- **HTTP 401**：两边的 token 不一致。
- **换 token**：先把新 token 写进 Mac 的 `~/.claude-relay/token`（或 `config.json` 的 `token`），正在运行的 worker 继续用旧的；等你把 Windows 的 `CLAUDE_RELAY_TOKEN` 换掉并重启机器人，worker 收到 401 后会自动重新读取 `config.json`，换成新的，不用重启 worker。
- **回执里说「Mac 不在线」**：60 秒内 worker 没来取过任务。看 `worker.log`。
- **#编号「失联」**：正在执行的任务超过 2 分钟没有心跳，通常是 Mac 睡眠或断网了。Mac 恢复后如果任务还在跑，结果会照常发回来；否则回复 `cc 重试 编号`。
- **安全**：能连上这个端口、又知道 token 的人，就能让 Mac 执行命令。token 不要外传，端口只对局域网开放。worker 给 Claude 的说明里要求它不做推送代码、发消息这类对外或难以撤销的事，除非任务里写明。
