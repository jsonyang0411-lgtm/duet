# Mac 和 Windows 不在同一个网络：用 Tailscale 连起来

- **写给**：要配置的人；Windows 测试同学；同事团队（Mac 业务服务也要连 Windows）。
- **一句话**：两台电脑都装 Tailscale，Mac 上一直填 Windows 的 Tailscale 地址（100 开头）。在不在同一个局域网都不用改配置，代码也不用改。
- **代码在哪**：`cross-network` 分支（从 `wechat-edge` 开出），改的只是排查和细节，见第七节。

## 一、为什么这样做

- **连接方向**：一直是 Mac 主动连 Windows 的中转端口（默认 8790）。cc 任务走 `/relay/*`，只收发版走 `/edge/*`，都是 Mac 来取、来发。所以 Windows 只要能被这台 Mac 连到，不用对公网开放。
- **Tailscale**：两边都只往外连，路由器不用设端口映射，流量全程加密（WireGuard）。
  - 同一个局域网时，自动走局域网直连，和现在一样快（实测每次请求 0.02～0.03 秒）。前提是两台电脑上的代理软件都没开 TUN（虚拟网卡）模式：开着时，Tailscale 的 UDP 会被送去境外节点，打不通直连，只能绕美国的中转，每次请求慢到 0.4 秒。2026-10-27 实测就是 Windows 上 Clash Verge 的 Tun 模式造成的，关掉后恢复直连。
  - 不在一起时，自动走公网直连；打不通就绕道 Tailscale 的中转服务器（DERP）。
  - 每台电脑的 Tailscale 地址固定不变，换网络不用改配置。
- **不要这样做**：在路由器上把 8790 映射到公网。这个端口是明文 HTTP，口令和达人的地址、电话会在公网上裸传；而且拿到口令的人就能用公司的微信号给达人发消息。

## 二、Windows 上

1. 安装 Tailscale（官网 tailscale.com/download），登录公司的 Tailscale 账号。
2. 托盘图标 → 设置里打开 **Run unattended**（没人登录 Windows 也保持在线），并保持开机启动。
3. 记下 Windows 的 Tailscale 地址：托盘图标里显示的 `100.x.y.z`，或命令行 `tailscale ip -4`。
4. 防火墙放行 Tailscale 网段访问 8790（管理员 PowerShell；端口不是 8790 就换掉）：
   ```powershell
   New-NetFirewallRule -DisplayName "webot-claude-relay-tailscale" -Direction Inbound -Protocol TCP `
     -LocalPort 8790 -RemoteAddress 100.64.0.0/10 -Action Allow
   ```
   原来放行局域网的那条规则留着，同一个局域网时可以照常用局域网地址。
5. 代理软件（Clash Verge 之类）不要开 TUN（虚拟网卡）模式，系统代理就够了。
   - 一定要开，就在 TUN 的排除路由（route-exclude-address）里加上 `192.168.0.0/16` 和 `100.64.0.0/10`。
   - 只加直连（DIRECT）规则、或者把 tailscaled 设成直连都不够：包还是会先进 TUN 网卡，被改掉端口，Tailscale 就打不通直连了。
6. `.env` 里的 `CLAUDE_RELAY_HOST` 保持默认的 `0.0.0.0`（所有网卡都监听，包括 Tailscale 的）。
7. 控制台「中转代理」→「Mac 端怎么接」会多出一个 100 开头的地址，标着「Tailscale」。

## 三、Mac 上

1. 安装 Tailscale（App Store 或官网），登录**同一个**账号。
2. 把 Windows 的地址换成它的 Tailscale 地址。只改一次，以后不用再动。改之前先试：`RELAY_URL=http://100.x.y.z:8790 python3 tools/claude_relay/worker.py --check`（只这一次用这个地址，不改配置），往返在几十毫秒以内再换；明显比局域网地址慢，先按「没通过时怎么看」查清。要改的地方：
   - cc / 草稿 worker：`~/.claude-relay/config.json` 里的 `relay_url` 改成 `http://100.x.y.z:8790`。正在运行的 worker 会自动换过去，不用重启；日志 `worker.log` 里有一行 `relay_url changed: … -> …`。
   - 同事的 Mac 业务服务：连 Windows 的地址（`EdgeClient` 的第一个参数）同样改成 `http://100.x.y.z:8790`。
3. 代理软件（Clash 之类）：只开「系统代理」的不用管，Mac 端的两个客户端都不走代理。不要开 TUN / 增强模式；要开的话，和 Windows 一样，在排除路由里加上 `192.168.0.0/16` 和 `100.64.0.0/10`（第二节第 5 步）。
4. 自检：`python3 tools/claude_relay/worker.py --check`，最后几行应该是：
   ```
   bot http://100.x.y.z:8790: ok (job kinds: admin,prm)
   network: 100.x.y.z 是 Tailscale 地址，往返 25 ms（3 次的中位数）
     Tailscale：直连（经 192.168.1.20:41641，2 ms）
   ```
   - 第三行说「走中转 DERP(…)」，表示两边没能直连，能用但慢一些，见第五节。
   - 说「找不到 tailscale 命令」，是 Mac 上没有 Tailscale 的命令行：在 Tailscale 菜单里装上 CLI，或直接运行 `/Applications/Tailscale.app/Contents/MacOS/Tailscale ping <Windows 的机器名>`。

## 四、Tailscale 后台的访问规则

- Tailscale 账号里只有这两台电脑时，默认规则（互相都能访问）就够了。
- 加了别的设备，就在后台 **Access controls** 里收紧成：只有这台 Mac 能连 Windows 的 8790。把两个地址换成你们的：
  ```json
  {
    "acls": [
      {"action": "accept", "src": ["100.a.b.c"], "dst": ["100.x.y.z:8790"]}
    ]
  }
  ```
  `100.a.b.c` 是 Mac 的 Tailscale 地址，`100.x.y.z` 是 Windows 的。

## 五、国内网络

- 两边直连打不通时，Tailscale 会绕道它的中转服务器。这些服务器在海外（香港、东京等），会慢，高峰期可能偶尔断。
- 解决办法，任选一个：
  - 在国内的云服务器上自建一个中转（DERP），在后台的规则里加上它；
  - 用 Headscale 把整套都自建；
  - 换国内的组网产品（比如蒲公英），做法一样：Mac 填 Windows 在虚拟局域网里的地址。这时控制台和 `--check` 会把它当成「局域网」，这是对的。

## 六、延迟和断线

- **延迟**：直连时，每次请求多出的就是两地之间的网络延迟，同城几毫秒到二三十毫秒，跨省三五十毫秒；绕道海外中转多一两百毫秒。一条达人消息在两台电脑之间只来回几次，和 Claude 起草（实测 20–45 秒）、运营审核比起来感觉不到。微信里的读回确认在 Windows 本机做，不受影响。
- **大文件**：图片、视频、文件要在两台电脑之间传一遍，速度看上行带宽。走海外中转时，几十 MB 的视频可能要几十秒到几分钟。这个分支起，Mac 往 Windows 传文件都是边读边传，网慢也不会因为「总共超过 2 分钟」而失败。
- **断线**：
  - 达人的消息在 Mac 确认收到之前一直留在 Windows 上，重新连上后接着取；
  - 发送按 `send_id` 只发一次，重试不会重复发给达人；
  - cc worker 连不上时 5 秒后重试，一直连不上就把间隔拉长到最多 1 分钟；
  - Tailscale 换网络时地址不变，正在进行的连接一般不会断。

## 七、这个分支改了什么

| 改动 | 在哪 |
|---|---|
| `--check` 多报一段线路：Windows 在哪种网络上（局域网 / Tailscale / 公网）、往返多少毫秒、Tailscale 是直连还是走中转；明文 http 走公网时提醒；连不上而地址是局域网的，提示改用 Tailscale 地址 | `tools/claude_relay/worker.py` 的 `link_report` |
| 改了 `config.json` 的 `relay_url`，worker 下一轮就用新地址，不用重启 | `serve` |
| Mac 往 Windows 传文件边读边传：草稿附件（worker）、`EdgeClient.upload_bytes` | `RelayClient.upload`、`src/edge/client.py` |
| Windows 记下 Mac 从哪个地址连来：第一次和换地址时 `bot.log` 各记一行（`CLAUDE_RELAY: the Mac worker connects from 100.x.y.z (tailscale)`、`EDGE: the Mac …`） | `src/relay/link.py` |
| 控制台「中转代理」：Mac worker 一行显示「在线 · 经 Tailscale（100.…）」；「Mac 端怎么接」给每个地址标上局域网 / Tailscale；微信里 `cc 状态` 显示「Mac：在线（经 Tailscale）」 | `src/web/server.py`、`ui/` |

## 八、测试清单

### 0. 准备

- [ ] **Windows**：拉 `cross-network` 分支，重新打包 EXE，启动。
- [ ] **Windows**：按第二节装好 Tailscale，加好防火墙规则。记下 Tailscale 地址：`____________`
- [ ] **Mac**：按第三节装好 Tailscale；`worker.py` 用这个分支的（worker 跑在哪个工作树，就在那里切分支）。

### 1. 同一个局域网，走 Tailscale

- [ ] **两台都试**：运行 `tailscale ping <对方的 100 地址>`。期望：都是 via 局域网地址、几毫秒。哪台出现 via DERP，先按第二节第 5 步处理那台的代理软件。
- [ ] **Mac**：`relay_url` 改成 Windows 的 Tailscale 地址，不重启 worker。期望：`worker.log` 里有 `relay_url changed`，之后照常取任务。
- [ ] **Mac**：`--check`。期望：`network: … 是 Tailscale 地址`，Tailscale 那一行是「直连（经 192.168.…）」。记下往返：`____ ms`
- [ ] **Windows**：`bot.log` 有 `CLAUDE_RELAY: the Mac worker now connects from 100.… (tailscale)`。
- [ ] **Windows**：控制台「中转代理」的 Mac worker 一行显示「在线 · 经 Tailscale」，下面一行是 Mac 的 100 开头地址，「Mac 端怎么接」里的 100 开头地址标着 Tailscale。
- [ ] 微信发 `cc 只回复：收到`。期望：照常完成。再发 `cc 状态`，第一行是「Mac：在线（经 Tailscale）」。

### 2. 不在同一个网络

- [ ] **Mac**：断开公司 Wi-Fi，连手机热点。
- [ ] **Mac**：`--check`。期望：还是 Tailscale 地址，能连上。记下是直连还是中转：`________`，往返：`____ ms`
- [ ] 微信发 `cc 只回复：收到`。期望：照常完成。记下从发出到收到结果的时间：`____ 秒`
- [ ] 只收发版（同事的 Mac 业务服务连上以后）：达人测试号发一句话，期望 Mac 那边收到；Mac 那边发一条回复，期望达人收到，并且读回确认正常。

### 3. 换网络时不丢消息

- [ ] Mac 在 Wi-Fi 和手机热点之间切换一次，切换期间在微信里发一个 `cc` 任务。期望：一两分钟内任务被取走并完成；`worker.log` 里最多有几行 `cannot reach the bot … retry`。
- [ ] 只收发版：切换期间达人测试号发一句话。期望：连上以后 Mac 照样收到，只收到一次。

### 4. 大文件

- [ ] 不在同一个网络时，让 Mac 往 Windows 发一个 20–50 MB 的文件（cc 任务的结果文件，或只收发版的附件）。期望：完整到达。记下大小和用时：`____ MB / ____ 秒`

### 回报

把上面空着的数字填好，`--check` 的最后三行和 `bot.log` 里 `connects from` 的几行贴在这里，推到仓库。

## 没通过时怎么看

| 现象 | 可能的原因 | 怎么办 |
|---|---|---|
| `--check` 说「找不到 tailscale 命令」 | Mac 上没装 Tailscale 的命令行 | Tailscale 菜单里装 CLI；不影响使用，只是看不出直连还是中转 |
| 「tailscale ping 没有回音」 | Windows 上的 Tailscale 没开、没打开 Run unattended，或两台不在同一个账号 | 看 Windows 托盘图标；两台都在 Tailscale 后台的机器列表里才行 |
| ping 通了，但 `bot … unreachable` | Windows 防火墙没放行 100.64.0.0/10；机器人没在运行；端口不对 | 看第二节第 4 步；`bot.log` 里有没有 `waiting for the Mac worker on 0.0.0.0:8790` |
| 能连上但很慢，`--check` 说「走中转」 | 两边网络都打不了洞，走了海外中转 | 第五节：自建国内中转，或换国内组网产品 |
| 连不上 100 开头的地址 | 某一台的代理软件开着 TUN 模式，把流量接走了 | 关掉 TUN；或者在 TUN 的排除路由里加上 `192.168.0.0/16` 和 `100.64.0.0/10`（只加直连规则不够） |
| 同一个局域网里，走 Tailscale 每次请求也要零点几秒，`--check` 的往返比局域网地址慢得多 | 有一个方向没走直连，绕了 Tailscale 的中转。Mac 上 `tailscale status` 显示对端是 direct，只说明 Mac 发过去是直连，回程不一定。2026-10-27 实测的原因：Windows 上 Clash Verge 开着 Tun 模式 | 两台都运行 `tailscale ping <对方的 100 地址>`，都应该是 via 局域网地址、几毫秒。哪台显示 via DERP，就关掉哪台代理软件的 TUN（或者按上一行排除路由）。实测关掉 Windows 的 Tun 后，每次请求从 0.4 秒降到 0.03 秒 |
| 控制台没显示 100 开头的地址 | Windows 上 Tailscale 没连上 | 托盘图标里看状态；这个列表每次刷新页面都会重新取 |
