# CLAUDE.md

## 每次修改必须提交 Git

每次对代码的修改完成后，必须执行 `git add` 和 `git commit`，写清楚改动内容和原因。

**只提交到本地，不推送到远端。** 只有用户明确要求时才执行 `git push`。

```bash
git add -A
git commit -m "<描述做了什么，为什么>"
```

## 打包（仅在发布 Windows 版本时）

在 Mac 上开发时不打包 EXE：PyInstaller 无法在 macOS 上生成 Windows 可执行文件。Mac 端直接从源码运行（`.venv/bin/python desktop_mac.py`）；前端有改动时，仍要执行 `cd ui && npm run build`。

只有在 Windows 上发布新版本时，才按下面的步骤重新构建并打包 EXE，确保 `dist/webot.exe` 包含最新改动。

```bash
# 1. 构建前端（如有前端改动）
cd ui && npm run build

# 2. 打包 EXE
pyinstaller build.spec

# 3. 复制到正确位置（如 PyInstaller 输出到了 ui/dist/）
cp ui/dist/webot.exe dist/webot.exe
```

## 每次修改必须进行功能测试

每次修改完成后，必须运行完整测试套件，并对修改涉及的功能做针对性验证。

### 运行全部测试

```bash
python -m pytest tests/ -v

# Mac 开发环境
.venv/bin/python -m pytest tests/ -v
```

Mac 开发环境的 `.venv` 是用 uv 创建的，里面没有 pip，装依赖要用 `uv pip install --python .venv/bin/python <包名>`。如果终端设置了 SOCKS 代理（`ALL_PROXY=socks5://...`），必须安装 `httpx[socks]`，否则 AI 后端相关的测试和实际调用都会报 ImportError。

测试分两层：
- **单元测试** (`test_config.py`, `test_routing_steps.py`, `test_router_business.py`, `test_web_api.py`, `test_window_controller.py` 等) — 毫秒级，验证逻辑正确性
- **功能测试** (`test_functional.py`) — 启动真实 HTTP 服务器 + Playwright 无头浏览器，模拟用户点击操作

### 功能测试覆盖要求

每次修改后，必须验证被修改功能在实际浏览器中可用：

| 测试类别 | 覆盖功能 |
|---------|---------|
| 路由红线 | 7 个路由步骤的顺序和每一步（`tests/test_routing_steps.py`）；任何群消息（@ 机器人、cc、管理员命令、进群）、陌生私聊、旧 .env 里的聊天开关都得不到回复，被删的聊天模块不能再被导入（`tests/test_router_business.py`） |
| API 端点 | Diagnose 返回结构、POST allowlist（聊天机器人的接口已删除）、配置字段默认值、`/api/relay`（任务的文字、结果里的号码打码，`tests/test_relay_console_api.py`）、`/api/prm` 和 `/api/prm/parties`、`/packets`、`/deals`、`/reveal`（`tests/test_prm_console_api.py`） |
| 页面加载 | 前端 SPA 正常渲染，不出现空白页或连接错误 |
| 主题系统 | 浅色模式默认（明日方舟风，2026-10 改版）、Sun/Moon 按钮切换到深色、localStorage 持久化 |
| 侧边栏导航 | 公司版（中转代理控制台）只有 运行状态/中转代理/待审批/合作单/微信连接/达人名单/运行日志 七页（Mac 端 `WECHAT_BACKEND=edge` 时没有「微信连接」）；旧链接回到运行状态 |
| 待审批 / 合作单页（达人副驾驶） | 草稿显示（号码只显示打码的）、改了再发、不发（确认框）、标为发错了、自动发送门槛表；登记达人（管理员 wxid 被拒）、停用；登记达人里新好友排最前（标「新好友」、验证消息打码、怎么加的），选中登记后她加好友以来的消息合成一份先审的草稿（`tests/test_prm_console_api.py` 也测接口）；展开合作单：采纳 AI 建议、切人工接管 / 恢复副驾驶、填运单号 → 待审批出现发货通知、更新物流到已签收、显示完整号码（`tests/test_functional.py::TestPrmPages`，PRM 服务在内存里，不写 data/）。接管、门槛、稍等回复、寄样、30 天清理的逻辑在 `tests/test_prm_lifecycle.py`；微信自己写的系统行（撤回、拍一拍、红包提示）不起草、不顶掉草稿，达人撤回时没发出去的草稿改成人审、运营撤回只记录、新好友的提示照旧（`tests/test_prm_system_lines.py`，真的 WCDB 后端；Mac 端的 `chat_notice` 在 `tests/test_prm_over_edge.py`）；合作单的提醒一直显示到处理了：草稿没写成时她的话并进下一份草稿、欠着的开专属佣金那一步在接管到期 / 切回副驾驶 / 重新启用时补派、平台动作标红到「已处理」、撤回到运营做了决定（`tests/test_prm_notices.py`）；每份草稿带上我方实际发出的回复（改过的、没发的）在 `tests/test_prm.py`、`tests/test_prm_worker.py` |
| 平台联动（小红书 / 抖音 / 淘宝 / 聚水潭） | 白名单和参数检查（佣金最高 25%，Mac 的配置只能往低调）、商品目录、预览（不带 `--execute`）和执行（台账只执行一次、逐步核验、链接只认平台域名）对着假 `node`（`tests/platform_fakes.py`，答复按四个平台命令行的真实 JSON 形状），聚水潭寄样单的单子走 0600 临时文件、预览不带电话、台账和输出打码、子账号核对（`tests/test_prm_platforms.py`，含系统 Python 3.9 冒烟）；机器人侧：一律先审、批准并执行、只有 VERIFIED_SUCCESS 发、其余标红、失联 / 没人接手的兜底、重新预览、寄样单的〈手机N〉只在交给 Mac 的任务里换成号码、Mac 从截图里读出的明文号码存库前换成〈手机N〉（`tests/test_prm_platform_flow.py`）；交付后的清理也清寄样单的收货信息（`tests/test_prm_lifecycle.py`）；Mac worker 端到端（`tests/test_prm_platform_worker.py`）；控制台接口和「待审批」页（`tests/test_prm_console_api.py`、`tests/test_functional.py::TestPrmPages`） |
| 微信边缘（PRM_EDGE） | `/edge/*` 协议：一套用例通过 Mac 端客户端同时跑假 Windows 服务端和真的 Windows 边缘（挂在中转的 HTTP 服务上），另外用 Mac 自带的 Python 3.9 跑一遍（`tests/test_edge_protocol.py`，含新好友事件 `friend_added` 和 `/edge/health` 的库编号 `db_id`）；Windows 这一侧的消息文字、读回和运营亲手回复、节奏、状态、清理、路由步骤（`tests/test_edge_service.py`） |
| 新好友（`src/wechat/new_friends.py`） | 真机探针看到的三种加好友（电脑通过、手机通过、对方通过我们的申请）、只有「ㅤ」的私聊不算、群 / 公众号 / 企业微信 / 文件传输助手 / 自己不读、同一个 wxid 重启后也只报一次、起点以前的老好友不报、只读有变化的会话、出错不停、`how`（机器人点的通过算 `auto_accept`，按时间和 OCR 读到的名字）、边缘模式验证消息只在事件里、副驾驶模式打码存着一周后清掉；「我通过了你的朋友验证请求…」不起草、不当达人消息转发；登记新好友后她加好友以来的消息合成一份先审的草稿，只交一次、最多 `PRM_CATCHUP_HOURS`（`tests/test_new_friends.py`，真的 WCDB 后端、路由、中转、PRM 接在假微信数据库上）；`FriendManager` 每通过一个申请都告诉 `on_accepted`（`tests/test_friend_manager.py`） |
| 中转代理页 | 任务列表（取消排队/进行中的任务）、Mac 状态、中转设置（随机生成 token 只显示一次、保存条） |
| 跨网络（Tailscale，`cross-network.md`） | 地址属于哪种网络、本机给 Mac 用的地址（局域网在前，再加 Tailscale 网卡）、Mac 从哪个地址来（日志、`/api/relay` 的 `mac_link`、`cc 状态`）（`tests/test_link.py`、`tests/test_relay_console_api.py`）；worker `--check` 的线路报告、改 `relay_url` 不用重启、草稿附件边读边发（`tests/test_claude_relay_worker.py`）；Mac 端上传边读边发、Python 3.9 也能跑（`tests/test_edge_protocol.py`） |
| 微信连接页 | 私聊开关改动出现底部保存条、OCR 备选开关有自己的保存条，「放弃修改」能撤回 |
| 首次设置 | 3 步（微信密钥 → 微信设置 → 中转设置），所有请求在测试里拦截，不写 .env |
| Mac 端控制台（WECHAT_BACKEND=edge） | `/api/edge`、Windows 出问题时每页顶上的提醒条、Mac 端左栏不显示「微信连接」、审批卡上通知已进「待审批」时不显示复制按钮（`tests/test_edge_console.py`，`/api/edge` 用 Playwright 拦截） |
| 达人名单页 | 左栏「达人名单」只有达人名单、待处理两个子页（老的审批单链接落到名单）；名单的添加（带平台和抖音号 / 小红书号）/ 停用 / 从最近私聊挑选、已在名单的再加入不清掉原来填的、待处理的回复草稿标为已处理（`tests/test_influencer_console.py`，用临时配置和数据库） |

### 手动验证清单

部分功能无法自动化测试（需要微信客户端或 Mac 上的 worker），修改这些模块后需手动验证：

1. **Claude 中转** — 需要微信运行 + Mac worker（`tools/claude_relay/README.md`）
2. **密钥提取** — 需要微信客户端运行
3. **达人副驾驶 / 微信边缘** — 需要微信 + Mac（`prm-copilot-test.md`、`edge-protocol.md`）
4. **好友管理** — 需要 Windows 版微信窗口（`friend-management-test.md`）；新好友识别（`friend_added`、登记达人里的「新好友」）要在真机上分别试电脑通过、手机通过、对方通过我们的申请
5. **消息发送** — 需要微信窗口可见

## 项目概述

webot — 商务微信号的网关：Claude 中转（cc 任务交给 Mac 上的 Claude Code）、达人副驾驶 / 微信边缘、管理员的账号命令。没有聊天模型、不需要 AI 密钥；群消息和陌生人的私聊一律不回（`src/routing/steps.py`）。

## 技术栈

- 后端：Python 3.13（Mac 开发环境为 `.venv` 中的 Python 3.12）
- 前端：React + Vite (ui/)
- 桌面：PyWebView (WebView2)
- 打包：PyInstaller (build.spec)
- 数据库：WCDB (微信加密数据库)，通过 wcdb_api.dll + DRM patch 直读

## 项目结构

```
src/bot.py              - Bot 主控
src/config.py           - 配置加载 (.env)
src/router.py           - 消息路由（跑 src/routing/steps.py 的 7 个步骤）
src/routing/            - 路由步骤：自己的消息 → 微信边缘 → 入库 → 中转 → 达人副驾驶 → 管理员命令 → 静默
src/admin.py            - 管理员私聊命令（查状态、添加好友、同意好友、自动同意好友）
src/relay/              - Claude 中转（Mac worker 来取任务）
src/prm/                - 达人副驾驶（草稿、合作单、门槛、清理、平台联动）
src/influencer/         - 达人名单（档位、主播类型、平台 UID，从聚水潭查；名单合一进 PRM）
src/edge/               - 微信边缘（给 Mac 业务服务收发微信）
src/wechat/             - 微信后端
  wcdb_backend.py       - WCDB 直读后端
  wcdb_client.py        - WCDB DLL 封装 (wcdb_api.dll)
  extract_key.py        - 密钥提取 (wx_key.dll Hook)
  window_controller.py  - 微信窗口操控 (键盘导航 + 消息发送)
  helpers.py            - 去重等工具
src/web/server.py       - Web UI 服务器 + HTTP API
src/send/、src/voice/   - 发送（打字停顿、排队、安静发送）、语音转文字
tools/claude_relay/     - Mac 上的 worker
desktop.py              - 桌面入口 (PyWebView)
ui/                     - React 前端
native/                 - 平台原生库 (windows/, macos/)
dist/                   - 打包输出
```

## 构建命令

```bash
# 构建前端
cd ui && npm run build

# 打包 EXE
pyinstaller build.spec

# 输出: dist/webot.exe
```

## 日志位置

- 源码运行: `data/bot.log`
- EXE 运行: `dist/data/bot.log`

## 代码参考手册同步更新

项目根目录下有 `CODEBASE_REFERENCE.md`（代码参考手册），包含所有函数、参数、调用关系的完整文档。每次修改代码后，**必须同步更新该文档**，确保文档与代码保持一致。

**例外**：达人商务模块（`src/influencer/`）的说明不写入本手册。改动原项目的共享代码（例如 `src/bot.py` 里建达人模块、接名单合一的地方，`src/web/server.py` 的转发）时，仍要按本节要求同步。

### 何时需要更新

| 代码变更 | 文档需要更新的内容 |
|---|---|
| 新增/删除函数 | §2 函数清单 — 添加/删除函数条目 |
| 修改函数签名（参数、返回值） | §2 函数清单 — 更新参数列表和返回值 |
| 新增/删除/重命名参数或环境变量 | §1 全局变量/参数清单 — 更新对应条目 |
| 修改函数间调用关系 | §3 模块间关系图 — 更新调用链 |
| 新增功能模块 | §4 功能模块展开 — 新增功能小节 |
| 修改现有功能逻辑 | §4 对应功能小节 — 更新调用链和参数表 |
| 新增/修改配置项 | §5 配置文件和环境变量 — 更新配置表 |
| 新增/修改数据库表结构 | §4 对应功能小节 — 更新 DDL 语句 |
| 新增源文件 | §1、§2、§3 — 添加新文件的函数和依赖关系 |

### 更新原则

1. **以代码为准**：文档必须反映代码的实际状态，不要凭记忆写
2. **函数签名精确匹配**：参数名、类型、默认值必须与源码一致
3. **调用链保持 ASCII 图的风格**：新增调用关系使用 `│ ├ └ →` 等字符
4. **交叉引用完整**：修改函数时，同时更新"调用了哪些函数"和"被哪些函数调用"
5. **环境变量表严格对齐**：名称、类型、默认值、定义位置、消费位置五列不缺

### 快速定位

修改代码时，用以下方式在文档中定位需要更新的位置：

```bash
# 查找某个函数在文档中的所有出现位置
grep "函数名" CODEBASE_REFERENCE.md

# 查找某个参数在文档中的所有出现位置
grep "参数名" CODEBASE_REFERENCE.md

# 查找某个文件在文档中的所有出现位置
grep "文件名" CODEBASE_REFERENCE.md
```
