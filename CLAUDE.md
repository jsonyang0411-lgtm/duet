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
| 路由红线 | 7 个路由步骤的顺序和每一步（`tests/test_routing_steps.py`）；任何群消息（@ 机器人、cc、管理员命令、进群）、陌生私聊、旧 .env 里的聊天开关都得不到回复，被删的聊天模块不能再被导入（`tests/test_router_business.py`）；登记达人私聊里发「cc …」「同意」「同意好友」、图片、语音（说的是 cc 也一样）只生成草稿任务（副驾驶）/ 给 Mac 的事件（边缘），不回复、不建 cc 任务，`CLAUDE_RELAY_ONLY` 开关都一样，她在群里 @ 机器人也不回，`PRM_COPILOT` 和 `PRM_EDGE` 同开时机器人不启动（`tests/test_prm_route_redlines.py`，真的 WCDB 后端、路由、中转、PRM / 边缘接在假微信数据库上） |
| API 端点 | Diagnose 返回结构、POST allowlist（聊天机器人的接口已删除）、配置字段默认值、`/api/relay`（任务的文字、结果里的号码打码，`tests/test_relay_console_api.py`）、`/api/prm` 和 `/api/prm/parties`、`/packets`、`/deals`、`/reveal`、`/friend-adds`、`/auto`（`tests/test_prm_console_api.py`）、`/api/wechat-gaps`（GET 只读、机器人停着时不建库，POST 才标看过了，`tests/test_web_api.py`） |
| 页面加载 | 前端 SPA 正常渲染，不出现空白页或连接错误 |
| 主题系统 | 浅色模式默认（明日方舟风，2026-10 改版）、Sun/Moon 按钮切换到深色、localStorage 持久化 |
| 侧边栏导航 | 公司版（中转代理控制台）只有 运行状态/中转代理/待审批/合作单/微信连接/达人名单/运行日志 七页（Mac 端 `WECHAT_BACKEND=edge` 时没有「微信连接」）；旧链接回到运行状态 |
| 待审批 / 合作单页（达人副驾驶） | 草稿显示（号码只显示打码的）、改了再发、不发（确认框）、标为发错了、自动发送门槛表；登记达人（管理员 wxid 被拒）、停用；登记达人里新好友排最前（标「新好友」、验证消息打码、怎么加的），选中登记后她加好友以来的消息合成一份先审的草稿（`tests/test_prm_console_api.py` 也测接口）；自动登记的达人（`PRM_AUTO_REGISTER`）：`/api/prm` 的 `auto`、`auto_register`（运行中；停着读 .env）、不再是候选，停用以后机器人不会再登记她、她带 `inactive` 回到候选里，运营再登记就成了人登记的（`tests/test_prm_console_api.py`，开关的读取和校验在 `tests/test_config.py`，控制台设置里的「新好友自动登记成达人」在 `tests/test_prm_console_api.py`）；页面上：开着时「登记达人」卡片顶上的提醒、名单里的「自动登记」标签、停用以后她在「新加的好友」里标「已停用」、机器人不再登记她（`tests/test_functional.py::TestPrmPages`）；开场白：还没说过话的达人合作单里有「写开场白」（说过话的没有），点了以后草稿正在写时再点会说为什么不写，草稿在「待审批」里标「开场白」（`tests/test_functional.py::TestPrmPages`）；展开合作单：采纳 AI 建议、切人工接管 / 恢复副驾驶、填运单号 → 待审批出现发货通知、更新物流到已签收、显示完整号码（`tests/test_functional.py::TestPrmPages`，PRM 服务在内存里，不写 data/）。接管、门槛、稍等回复、寄样、30 天清理的逻辑在 `tests/test_prm_lifecycle.py`；机器人停在发送中、重启后补读到的那句：草稿改回已发出（带文件的写明文字已发出）、不算运营接管（`tests/test_prm_catchup.py`）；这个号自己发的一行按消息编号认：同一行又交来不算新的一句，别的行里和机器人一模一样的字（运营照着打的「好的」）照样接管，副驾驶和 Mac 端都测（`tests/test_stable_message_ids.py`、`tests/test_prm_lifecycle.py`、`tests/test_prm_over_edge.py`）；机器人发的每个文件只抵一条、类型要对得上（图片抵图片、文件抵文件，重启前发的、停在发送中的也一样），运营从手机发的图片、语音、表情照样接管（`tests/test_prm_lifecycle.py`、`tests/test_prm_catchup.py`）；这台电脑的钟比微信快几个小时（3 小时、运行中改成快 13 小时）：机器人自己的话读回来测出钟差以后实时消息照常起草（不当成晚到、不漏起草）、运营亲手回复照样接管，合作单提醒对时间、钟改对后提醒消失、提醒不算合作单的动静；只认这次运行登记后两分钟内读回的我们的文字（`tests/test_prm_catchup.py`）；微信自己写的系统行（撤回、拍一拍、红包提示）不起草、不顶掉草稿，达人撤回时没发出去的草稿改成人审、运营撤回只记录、新好友的提示照旧（`tests/test_prm_system_lines.py`，真的 WCDB 后端；Mac 端的 `chat_notice` 在 `tests/test_prm_over_edge.py`）；拍一拍（微信 4 里是 49 类、`local_type` 高 32 位 62，用真机的 XML）谁拍谁都是系统行：副驾驶不起草、不顶掉草稿、我们拍她不算运营亲手回复，边缘报 `chat_notice`，引用 / 链接 / 转账这些别的高位子类型照旧；微信原地改写的撤回（同一个编号，类型变 10000，文字在 `<revokemsg><content>`）：最近 5 分钟读过的行变成撤回就当一条新编号的系统行交出去，达人撤回时草稿改成人审、运营撤回只记、只报一次、连发合并压着的那一行先交，边缘报带 `recalled_message_id` 的 `chat_notice`，太早的行、不是达人的私聊不看（`tests/test_pat_and_recall.py`，真的 WCDB 后端，两种模式）；合作单的提醒一直显示到处理了：草稿没写成时她的话并进下一份草稿、欠着的开专属佣金那一步在接管到期 / 切回副驾驶 / 重新启用时补派、平台动作标红到「已处理」、撤回到运营做了决定（`tests/test_prm_notices.py`）；等审的草稿被她的新消息顶掉时，那份草稿里她的话按顺序并进新任务、`meta.sent` 说那份没发出去，发出去以后不再重复带，开专属佣金那一路、人工接管照旧（`tests/test_prm_notices.py`）；每份草稿带上我方实际发出的回复（改过的、没发的）在 `tests/test_prm.py`、`tests/test_prm_worker.py`；抖音百应私信（`PRM_BUYIN_SYNC_MIN`，默认关，读消息的是 douyin-engineering 的 `dy buyin im-inbox`（读 Mac 调试 Chrome 里开着的「精选联盟私信」页），契约在 `tools/claude_relay/README.md`）：开关关着什么任务都不排，开着每个间隔一个只读任务（不跑模型、不带 `--execute`、`--after` 带上一轮的 cursor），命令行没有这个命令时「待审批」页说读不了、照间隔再问，登录 / 风控 / 店铺不对 / worker 太旧也说，登记达人（达人名单的抖音号 / 平台 UID、寄样单里的抖音号，不按昵称，对上两位算谁都不是）的新话打码后起草，没登记的不建达人、只说有几位，同一个消息编号只处理一次，草稿是「百应私信 · 运营手动发」：一律先审（门槛 auto 也是，不算门槛、不能标发错了、不回微信「稍等」、不带平台动作和附件、发送线程不发），复制（带完整号码、记一笔）/ 改了再复制 / 已在百应发了（不算接管）/ 不发，和微信草稿互不顶替、百应的话不进微信的草稿任务、以后的微信草稿不当成我方回复，百应的一句不让微信再回「稍等」，人工接管 / 暂停时（草稿写到一半才接管的也是）不起草但合作单提醒、回到副驾驶时起草一份，开关关了 / 达人停用时提醒说实话、出错日志不刷屏，一个会话连续 3 轮处理不了就跳过并提醒（「待审批」页先说），超过 24 小时的不起草但提醒，草稿任务排不进去时这几条下一轮再来，更早的草稿后回来不顶替更新的，取消的读消息任务也不留原话，微信里的撤回不让它等人，没写成的有自己的提醒，90 天清理也清百应的草稿和任务；页面只给百应编号：Mac 按邀约台账认出我们邀约的会话（卡片和台账的 result 相差 60 秒以内、卡片上有邀约信息的前 30 个字、一对一才算，台账 / 清单读不了只记一行不带值的日志）、填抖音号和 `invited`，登记了这个抖音号的达人照常起草，邀约卡滚出去以后会话还记得；店里同事发的邀约卡（2026-10-30：那时候我们台账里没发过卡（从开始发到记下结果后 1 分钟都算，结果记得晚的卡照样是我们的），或者发过但卡上的话不是我们那句；没有结果的发卡记录算之后 10 分钟、话不知道；同一分钟话也一样的、卡上没字的、台账读不了的从不算）标 `other_invite`，「待审批」页不列名字、只说「另有 N 位是店里同事邀约的」，认出一次就一直记着、后来认出是我们的就不算（`tests/test_prm_buyin_worker.py`、`tests/test_prm_buyin.py`、`tests/test_functional.py::TestPrmPages`）；聊天页没开 / 断线 / 改版各有一句话；达人的联系方式卡片：Mac 去掉店铺自己的联系方式（她打回来的店铺微信号 / 手机号在她的话里也换掉），答复不进 outbox（从内存发，别的任务照旧），机器人只记打码的一行（「[联系方式卡片] 微信号 ab****yz、手机 138****0001（加微信 #id）」），我们的会话（我们邀约的、登记达人的）里每张新卡调一次 `on_buyin_contact`（出错不停），她打字留的联系方式（手机号、带标签的微信号）也调一次（`buyin_text`），记下的那一行微信号打码、手机号〈手机N〉，她的昵称里的也打码，别人的会话里卡片只数、她的话不看，「待审批」页标出我们邀约的、说别人的会话里有几张卡；完整号码不进 prm.db、事件、提醒、号码表、中转任务、日志、草稿任务（`tests/test_prm_buyin.py`；Mac 侧 `tests/test_prm_buyin_worker.py` 含系统 Python 3.9 冒烟；页面 `tests/test_functional.py::TestPrmPages`）；下班时段自动回复（`PRM_NIGHT_AUTO_HOURS`）：窗口怎么写（跨过半夜、18:00 算在里面、09:00 不算、写错的启动时拒绝）、空着时和以前一模一样、每条安全条件两个方向（这台电脑的钟和微信对不上时暂停、上次运行留下的钟提醒也算；只判最后一步：平台动作、人工接管、暂停、有更新的任务、并进后面任务的、补读、撤回、系统那一路、百应私信、新好友照旧先审；投诉不发；`plain_reason` 的每一项；红线：20%、25%、100%羊毛 发，30%、三十个点、百分之三十、坑位、返现 不发；模型要求注意的只发一句过渡话：kb 的夜间那句发，带数字、数量、日期、红线词、太长、没说会回她、带文件的不发；发出去以后合作单留提醒到「已处理」；还没发出去时她撤回就改回先审；下班前建、下班后写好的任务照旧先审），不算进门槛统计、标为发错了照旧、下班时段的「稍等」、任务带 `meta.night`（`tests/test_prm_night.py`），Mac 的提示里多一句下班时段、系统说明多一句、没有 `meta.night` 时提示不变、Python 3.9 能跑、端到端（真的中转和 worker）夜里的草稿自己发出（`tests/test_prm_worker.py`），控制台的 `night` / `night_alerts` / 「已处理」（运行中和停着时，`tests/test_prm_console_api.py`），两个设置（`tests/test_config.py`、`tests/test_prm_console_api.py`），「待审批」页的那一行状态、「夜间自动发出」标签、夜里没自动发的原因、提醒的「已处理」、标为发错了的确认框（`tests/test_functional.py::TestPrmPages`）；自动模式（2026-10-30，「待审批」页顶上的一键开关，`POST /api/prm/auto`；关着和以前一模一样）：默认关、开关存在库里（重启以后换一个 `PrmService` 读回来还开着）、读不出的当关、双击不改打开的时间、一行日志；开着、没设窗口：普通草稿自己发出（`route=night`、草稿 `meta.auto_mode`、记录写「自动模式发出」、不算进门槛统计、标为发错了照旧）、任务带 `meta.night = {auto: true, at}`；要注意的只发一句过渡话、提醒「自动模式下自动回了过渡的话」到「已处理」，不稳妥的、带文件的不发、原因说自动模式；打开时已经在等的照旧等、打开前建的任务照旧先审；关掉以后照旧等人、「稍等」说白天那句；钟对不上（这次测的、上次留下的）暂停；在窗口里照下班时段的说法，第二天白天还开着就是自动模式的（任务 `{auto: true}`、「稍等」说 `AUTO_HOLDING_TEXT`，不说「明早」）；平台动作、接管、暂停、有更新的任务、补读、撤回、系统那一路、百应私信、新好友、开场白照旧先审（`tests/test_prm_night.py`）；Mac 提示里的「现在是自动模式…过渡的话不说「明早」」、系统说明多「或自动模式」、端到端、Python 3.9（`tests/test_prm_worker.py`）；`/api/prm/auto` 运行中马上算数、停着时写进库下次启动照它、`auto_mode`、`night.auto`、`on` 不是 true / false 不改（`tests/test_prm_console_api.py`）；「待审批」页顶上的开关：先问（确认框说清楚会怎样，「再想想」什么都不改）、开着时顶上那一行「自动模式开着（HH:MM 打开）」、门槛表上那一行说自动模式、已经在等的草稿照旧等、自动模式发出的标「自动模式发出」（标为发错了的确认框也这么说）、「合作单」页顶上的提醒、「关掉自动模式」一下就关不用确认（`tests/test_functional.py::TestPrmPages`） |
| 平台联动（小红书 / 抖音 / 淘宝 / 聚水潭） | 白名单和参数检查（佣金最高 25%，Mac 的配置只能往低调）、商品目录、预览（不带 `--execute`）和执行（台账只执行一次、逐步核验、链接只认平台域名）对着假 `node`（`tests/platform_fakes.py`，答复按四个平台命令行的真实 JSON 形状），聚水潭寄样单的单子走 0600 临时文件、预览不带电话、台账和输出打码、子账号核对（`tests/test_prm_platforms.py`，含系统 Python 3.9 冒烟）；机器人侧：一律先审、批准并执行、只有 VERIFIED_SUCCESS 发、其余标红、失联 / 没人接手 / 找不到执行任务（60 秒后判 UNKNOWN）的兜底、重新预览、寄样单的〈手机N〉只在交给 Mac 的任务里换成号码、Mac 从截图里读出的明文号码存库前换成〈手机N〉、签收时按寄样单自己的执行记录找它（之间聊得再多也找得到，晚到的核验成功也算）、机器人停着时标的已签收在启动时（之后每轮）补派开专属佣金、再标一次也补、只派一次、专属动作的结果不确定（ACCEPTED_UNVERIFIED / INDETERMINATE / UNKNOWN / PARTIAL、写了一步又失败）时挡住同一商品的第二次批准，被要求核对后在标红那份上点「我已处理」才放行那一份（Mac 拿到台账以后出错交的 UNKNOWN 也算）、NOT_RUN（等平台命令锁时取消等）标红时说平台没有动、可以重新批准、多款寄样的开佣金链条由机器人记进度（哪款开好、哪款这一轮不开 / 没开成 / 结果不确定、哪款还没开，写进系统说明和合作单的提醒），一款开不成（预览不过点了「不发」、Mac 没接手、标红）不挡后面的、每款一轮只派一次、重开一轮只问没开好的、`PRM_COMMISSION_AFTER=created` 时签收（第一次、改回运输中再签收）也开新的一轮、采纳 AI 建议的「已签收」和手标的一样开佣金链（`tests/test_prm_platform_flow.py`）；交付后的清理也清寄样单的收货信息，预览和执行记录里聚水潭命令行原样输出的 JSON 和「收货」那一行也只留省市（`tests/test_prm_lifecycle.py`）；Mac 的执行日志里收货人只留省市、以前写的满 7 天剪（`tests/test_prm_platform_worker.py`）；Mac worker 端到端（`tests/test_prm_platform_worker.py`，含等锁时、真要写之前被取消就不执行，开始写以后不停）；控制台接口和「待审批」页（`tests/test_prm_console_api.py`、`tests/test_functional.py::TestPrmPages`） |
| 达人邀约批量（`tools/creator_invite`） | 全部离线、对着假 `node`（`tests/test_creator_invite.py`）：plan 只读（没有一条带 `--execute`）、两边找人（聚水潭 `find` + 抖音达人广场搜索，抖音新找到的每位 `jst lookup` 一次、两边都有的标出来、查不到 / 已在我的达人里的不进、最多查 `dy_lookup_max` 位、配置里的搜索只许筛选和页数）、筛选（台账里邀约过的、平台状态不是 1 的、帽子占比 ≥ 90% 单列为疑似同行自播号、女粉 < 60% 不进、专门带自己牌子的（名字像店铺、店铺矩阵号）和可能骗样的（等级低、卖得少、销售额不公开、粉多卖少）不进且各列一张表、`keep_ids` / `drop_ids`）、三类（专场型按直播销售额和等级，15～20 万粉算小达人）、三类轮流查状态（清单够数时三类都有，超出的列为备选）、配商品（主推 + 其余全部，最多 5 款；默认不查库存，设了 `min_stock` 才查、不配缺货的）、每 20 位一次状态查询；卡上默认不开专属佣金、只勾免费申样（比例和专属高佣要一起有）、run 要 `--confirm` 清单编号、预览绑定执行、预览说的每款样品和提醒记进台账、执行前先记 `attempt`（进程死掉或超时也不重发）、76 / 77 / 79 立刻停、`STATE_CONFLICT` 跳过、平台额度余量、每周和每天上限、清单出了以后又写进 `drop_ids` 的人跳过、接着上次、同时只能一个 run；follow（发过卡的达人按钮变成已建联 / 已拒绝就弹 Mac 通知、发卡 2 天内隔 25 分钟更早隔 115 分钟才再查、回了拒了的不再查、查不了两小时只提醒一次、夜里不查、正在发卡时跳过、通知文字作参数不拼进脚本）；两个放慢包装用真的 node 测请求间隔；`/usr/bin/python3`（3.9）跑一遍 plan |
| 加百应达人微信（`PRM_FRIEND_ADD`，默认关，功能 R） | 联系方式的规则（`tests/test_prm_contact.py`）：她话里的微信号只认带标签的（微信 / vx / wx / v信 / 薇 / 威 / 加V / ➕V），6–20 位、字母开头；手机号的各种写法（空格、短横线、+86、标签贴着号码）；不该认的（不带标签的英文串、链接、商品编码、订单号、抖音号、wxid、太长太短的）；打码形式（和百应那一路同一个规则：微信号不到 5 个字、手机号不到 8 位写 ****）和「[联系方式卡片] 微信号 ab****yz、手机 138****0001（加微信 #12）」「[联系方式卡片]（这次没读到号码）」「…（卡片上只有店铺自己的联系方式）」这几行；她打的第一个微信号和手机号（`first_contacts`）；控制台填的号的校验。存储（`tests/test_friend_adds_store.py`）：老库也建上三张表、同一条百应消息只有一条记录（手动填的每次是新的）、状态只按比较交换改、清号码留打码的、每次 add_friend 一行、关联同一个抖音号只留第一次的、结束满保留期连同次数记录删掉、8.2 清理再清号码并把昵称里的号码打码；关联的抖音号并进 `_creator_ids`（寄样单的在前、不分大小写去重、最多 5 个，UID 只用寄样单的，百应会话的编号是 `uid_im`）、带关联登记时先写关联和事件再补交她的话（补交的那份就带着抖音号）、已登记的只补关联、两边的抖音号对不上时合作单提醒（`tests/test_prm.py`）；清理（`tests/test_prm_lifecycle.py`）；寄样单只有 UID 时关联的抖音号让 Mac 查得到邀约卡（`tests/test_prm_platform_flow.py`）。加好友这条路（`tests/test_friend_adds.py`，好友管理是替身，管理员命令处理器、自动同意、PRM 服务是真的）：开关关着什么都不存不加；一次确认只调一次 add_friend，每一种 reason 对应的状态，风控提示只暂停一次、暂停着谁都不加、管理员发「自动同意好友 开」以后才能再加，点两次 / 两个页面同时点 / 另一条正在加都不会加第二次，间隔、每天上限（没发出去的也算），起不了线程算没发出去，机器人停在加的时候下次启动是「不确定」，没有好友管理组件时不加但能复制号码和「我在手机上加了」；百应那一路的回调只收我们邀约的、登记达人留的什么都不存（她的合作单里百应那一路已经记了打码的一行，那一行也没有「加微信」）、卡上是我们自己的号不收、号码没读到要人填、同一条读两次只有一条；手动填的校验；附言（昵称、.env 里的品牌、昵称里有号码时不用、30 字）；改号、请她加我们、不加清号码、看过微信以后的「发出去了 / 没发出去」、显示完整号码记一笔、是她登记并关联（带抖音号）、`PRM_AUTO_REGISTER` 先把她登记了的「是她」只补关联（不建第二个达人、不再补交、机器人起的称呼和备注不变）、只记下聊天不登记；号码按时清（14 天 / 7 天 / 30 天）、结束满保留期删掉；管理员的「待加达人」「加达人 N」「不加达人 N」（号码打码）；日志里从没有完整号码。`add_friend_checked`（`_cmd_add_friend` 的每个字不变）、`paused_why` 只读、`Bot._wire_friend_adds`（`tests/test_friend_manager.py`）；配置（`tests/test_config.py`）；达人私聊里发「待加达人」「加达人 1」「不加达人 1」只是她的草稿、什么都不加，群里的和陌生人的也不回（`tests/test_prm_route_redlines.py`、`tests/test_router_business.py`）。控制台：`/api/prm` 的 `friend_adds` 只有打码号码、`friend_add_status`，添加一次 / 第二次被拒、显示完整记一笔、关联以后合作单有抖音号和事件、登记时填抖音号、取消关联、只认 POST 和本机页面、机器人停着时能看不能做（`tests/test_prm_console_api.py`、`tests/test_web_api.py`），高级设置里错误显示在它说的那一项上（`tests/test_web_advanced.py`）；页面（`tests/test_functional.py::TestPrmPages`）：加微信卡（打码号码、附言、显示完整）、确认框写明会搜哪个号和不会重试、确定以后才加且只加一次、不加要确认、选聊天登记并关联、合作单页手动登记的校验、登记达人带抖音号和取消关联、开关关着页面上什么都没有。备注（`tests/test_friend_adds.py`）：「抖音-〈百应昵称〉」的规则（空白压成一个、整个最多 16 个字、昵称空或有打码的号码用抖音号、两样都没有或模板空就不设、模板来自 .env），和没删的记录重复时加「-抖音号后 4 位」（不分大小写；不加的记录不算、过期的算；还重复就不设），百应那一路建的记录也有；加的时候交给 `add_friend`、填没填上（`remark_set`）存进记录，「加达人 N」的回复说备注填没填上，日志里从不写备注；老库补上 `remark` / `remark_set` 两列（`tests/test_friend_adds_store.py`）；配置（`tests/test_config.py`）、高级设置里清空保存就是不设备注、错误显示在它自己那一项（`tests/test_web_advanced.py`）；控制台的 `remark` / `remark_set`（`tests/test_prm_console_api.py`）、加微信卡上附言下面一行「备注：抖音-…」、确认框里写着备注、加了以后「已填进好友申请」（`tests/test_functional.py::TestPrmPages`）；关联以后备注不在她身上（她自己加我们的、没认出备注栏、手选的聊天）时卡片留着、「建议在手机微信里给她设备注：抖音-…」、「复制备注」放进剪贴板、「备注设好了」以后卡片不见，她是通过我们带备注的申请加上的（或者显示名就是备注）就没有这一句，「登记达人」时一起关联的也一样（`tests/test_friend_adds.py`、`tests/test_prm_console_api.py`、`tests/test_functional.py::TestPrmPages`）。认人（`tests/test_friend_adds.py`）：她在我们这边显示的名字就是这条的备注、而且别的没结束的记录没有这个备注算确定（两条一样谁都不给），我方打招呼和附言一字不差而且只有这一条用这句才算确定、两条同样的附言或两条都在等就谁都不给、只有一条在等算很可能、她给的微信号就是新好友的 wxid 算确定、她加我们时验证消息里有百应昵称或「百应」算可能、没出申请窗口的那次 2 分钟内的新好友算很可能、「不是她」以后这位不再给这一条、好的候选不被差的顶掉、关联过的聊天不再给、记录建在她成为好友之后也会回头看、在登记达人里填了这个抖音号就一起关联、过期的有候选时还在卡片上、从不自动登记；真的 WCDB 后端接假库（`tests/test_new_friends.py`）：对方通过我们的申请时收到我方的打招呼、她通过以后显示名是我们填的备注（`wcdb_get_display_names`），两条在等、打招呼没读到也认得出是哪一条、两条申请里按打招呼认出是哪一条、是她以后补交的草稿任务只有她的话（好友提示和我方打招呼不交、不接管）而且 `meta.creator` 带着抖音号、她加我们时验证消息提到百应、老库补上 `own_greeting` 列、扫描接上 `on_new_friend`；边缘的 `friend_added` 还是那五个键（`tests/test_edge_protocol.py`）；控制台的候选标注和「不是她」（`tests/test_prm_console_api.py`）、卡片上的候选和「是她 / 不是她」、新加的好友标注和带上抖音号（`tests/test_functional.py::TestPrmPages`）。Mac 的邀约背景：`invite_history` 读台账和清单（新的在前、没发出去的不要、发没发出去不确定的标出来、清单给卡上的话和商品名、没有清单或读坏了就不带、`../` 的清单编号进不来、读不了抛 OSError，`tests/test_prm_platforms.py`，含系统 Python 3.9 冒烟）；`invite_block` 默认关、没有抖音号或没有卡就没有、比例 0 和 25 的说法、权益和买样、卡上的话关不掉标签、台账读不了只记日志，提示里放在我方回复之后、她的话之前，端到端：关联了抖音号的达人的草稿任务带上它、关掉就没有（`tests/test_prm_worker.py`）。第 7 步（合并 dy-im-inbox 以后，`tests/test_prm_buyin.py`）：功能打开时我们邀约的达人的卡片 / 打的字只成一条记录（同一个抖音号没结束的只有一条），完整号码只在 `friend_adds` 里（事件、草稿任务、读消息任务的统计、日志、快照都没有），别人的会话不建记录、登记达人的不建记录；号码没读到的那条被她后来打的字补上号码；「待审批」那一行「其中 M 位留了联系方式，见下面的「加微信」」、百应草稿卡上「她在这个会话里留了联系方式（…）→「加微信 #N」：状态」（页面在 `tests/test_functional.py::TestPrmPages`）；百应草稿任务的 `meta.creator` 带会话的抖音号（和关联的一起、不分大小写去重），Mac 打开 `prm_invite_background` 时百应草稿的提示里也有邀约背景（`tests/test_prm_buyin_worker.py`） |
| 微信边缘（PRM_EDGE） | `/edge/*` 协议：一套用例通过 Mac 端客户端同时跑假 Windows 服务端和真的 Windows 边缘（挂在中转的 HTTP 服务上），另外用 Mac 自带的 Python 3.9 跑一遍（`tests/test_edge_protocol.py`，含新好友事件 `friend_added` 和 `/edge/health` 的库编号 `db_id`）；Windows 这一侧的消息文字、读回和运营亲手回复、节奏、状态、清理、路由步骤（`tests/test_edge_service.py`）；应答被截断 / 代理回了页面 / 附件下载被截断都按网络问题退避（`tests/test_edge_protocol.py`）；Windows 重启后补交停机期间的话（带 `written_at`、只交一次、停机前读回过的我们的话不算运营的，`tests/test_edge_catchup.py`）；Mac 端素材发送失败标红、应答丢了先问 Windows 再定、接收循环什么错都不停、按 `db_id` 认出新库（`tests/test_edge_backend.py`、`tests/test_prm_over_edge.py`） |
| 新好友（`src/wechat/new_friends.py`） | 真机探针看到的三种加好友（电脑通过、手机通过、对方通过我们的申请）、只有「ㅤ」的私聊不算、群 / 公众号 / 企业微信 / 文件传输助手 / 自己不读、同一个 wxid 重启后也只报一次、起点以前的老好友不报、只读有变化的会话、出错不停、`how`（机器人点的通过算 `auto_accept`，按时间和 OCR 读到的名字）、边缘模式验证消息只在事件里、副驾驶模式打码存着一周后清掉；「我通过了你的朋友验证请求…」不起草、不当达人消息转发；对方通过我们的申请时事件带上我方的打招呼（`own_greeting`，副驾驶模式存一周）、`kind`、`added_at`，边缘事件不变；登记新好友后她加好友以来的消息合成一份先审的草稿，只交一次、最多 `PRM_CATCHUP_HOURS`（`tests/test_new_friends.py`，真的 WCDB 后端、路由、中转、PRM 接在假微信数据库上）；她通过了我们的申请、一句话都没说时改成一份开场白（2026-10-30：任务没有她的话、带 `meta.opening`，这一类门槛已经自动也先审、下班时段照旧先审、等多久都不发稍等回复、她的话并进还没被取走的开场白任务、她的新消息顶掉等审的开场白时回她的任务知道开场白没发出去、没写成的提醒留到再点「写开场白」；登记得比 `PRM_CATCHUP_HOURS` 晚、补读读不到她加好友的时候不自己写，合作单留一条「看一眼微信」的提醒，点「写开场白」、她的回复草稿回来、运营亲手回她时去掉（审查 2026-10-30）；控制台的「写开场白」只给还没说过话的达人：停用、不在副驾驶、草稿正在写或在待审批、她发过消息、找不到合作单都说为什么不写（她只在百应说过话、百应的草稿在待审批时照样能写，`tests/test_prm_buyin.py`），运营接管期间亲手打过招呼的，开场白任务带上那几句（只看不取，她下一份草稿照样听到），`tests/test_new_friends.py`；`/api/prm/deals` 的 `opening`、机器人停着时不能写在 `tests/test_prm_console_api.py`；Mac 提示里的那一句、没有 `<creator_message>`、端到端、Python 3.9 在 `tests/test_prm_worker.py`）；新好友自动登记（`PRM_AUTO_REGISTER`）：关着和以前一模一样；开着时扫描只排队（查看里不登记），PRM 的发送线程下一轮把她登记成正式达人（称呼是微信名、折叠空白、最多 30 字，没有就「新好友 <wxid 前 8 位>」；备注「自动登记（新好友，我们加她 / 她加我们 / 机器人自动通过 / 说不清）」；合作单记一条；一行日志、不带名字），她加好友以来的消息合成一份先审的草稿（这一类门槛已经自动、下班时段也照旧先审），之后的草稿和别的达人一样走门槛和下班时段；管理员、cc 用户、已经登记的、停用的（运营的停用一直算数）、群 / 公众号 / 文件传输助手从不自动登记；重启后再看到她不重复登记；登记出错只记日志、扫描照常、她留在候选里；扫描线程和发送线程真跑、限时不卡死；边缘模式只接 `friend_added`（`tests/test_new_friends.py`）；`FriendManager` 每通过一个申请都告诉 `on_accepted`（`tests/test_friend_manager.py`） |
| 同名的聊天 | 群和达人同名、两位达人同名、达人和好友同名：每个聊天按 wxid 读，达人的消息照常起草（副驾驶）/ 交给 Mac（边缘）；发送、回复、截图按名字在微信的搜索里找窗口，名字和另一个聊天一样就不做，返回带原因的 `SendRefused`（PRM 把草稿标成没发出去，边缘把原因写进 `send_result`）；设了不重复的备注、重启以后照常发；第一次给还没在读的达人发时名字取自 `data/private_chat_names.json`，从下一轮起读她（同名的照样读，只是不发）；数据库重开后 `WECHAT_GROUPS` 照旧（`tests/test_same_name_chats.py`，真的 WCDB 后端、路由、中转、PRM / 边缘接在假微信 4 数据库上；`tests/test_wcdb_backend.py`、`tests/test_ocr_windows.py`、`tests/test_edge_service.py`） |
| 发送前核对打开的聊天（`SEND_VERIFY_CHAT`） | 搜索回车以后读到的标题：是要发的聊天（空格、大小写、群的人数、OCR 读不出的表情都不影响）就发；是机器人知道的别的聊天（「Amy」搜出「Amy陈」「Amy粉丝群」）或要发私聊却是群就不粘贴、不重试、返回带原因的 `SendRefused`；屏幕还停在上一个聊天时再读；读不出、截断的、不认识的照旧发；读屏幕出错 10 分钟内不再读；关掉就不读；文件和截图也核对（`tests/test_send_verify_chat.py`） |
| 中转代理页 | 任务列表（取消排队/进行中的任务）、Mac 状态、中转设置（随机生成 token 只显示一次、保存条） |
| 跨网络（Tailscale，`cross-network.md`） | 地址属于哪种网络、本机给 Mac 用的地址（局域网在前，再加 Tailscale 网卡）、Mac 从哪个地址来（日志、`/api/relay` 的 `mac_link`、`cc 状态`）（`tests/test_link.py`、`tests/test_relay_console_api.py`）；worker `--check` 的线路报告、改 `relay_url` 不用重启、草稿附件边读边发（`tests/test_claude_relay_worker.py`）；Mac 端上传边读边发、Python 3.9 也能跑（`tests/test_edge_protocol.py`） |
| 微信连接页 | 私聊开关改动出现底部保存条、OCR 备选开关有自己的保存条，「放弃修改」能撤回 |
| 首次设置 | 3 步（微信密钥 → 微信设置 → 中转设置），所有请求在测试里拦截，不写 .env |
| Mac 端控制台（WECHAT_BACKEND=edge） | `/api/edge`、Windows 出问题时每页顶上的提醒条、Mac 端左栏不显示「微信连接」、审批卡上通知已进「待审批」时不显示复制按钮、Windows 报来的电脑微信掉线（`wechat_gap`）每页顶上提醒到点「看过了」（`tests/test_edge_console.py`，`/api/edge`、`/api/wechat-gaps` 用 Playwright 拦截） |
| 好友管理（`src/wechat/friend_manager.py`） | 微信窗口、键盘、OCR 都是替身（`tests/test_friend_manager.py`）：每个按钮只点一下（投递点击只给「+」和菜单、投递不成才物理点）、叫「微信」「Weixin」的窗口只有点了「添加朋友」后新出现的才算对话框、对话框被微信自己的窗口挡住时先调到前台再核对（别的程序在前台照样中止）、不在前台不点不输、OCR 找不到不盲点、屏幕上看到结果才算数、窗口放回原样和焦点还回去；4.0 的对话框（「通过朋友验证」「申请添加朋友」「添加朋友」）只认点击后新出现的、标题恰好对得上、不是聊天名的窗口，单独打开的「达人朋友群」「小朋友」从不被点、从不收到按键，往对话框里输入时那个窗口自己必须在前台，不在就停下、关掉对话框；`_Abort` 以外的错误（pywin32 报错、窗口句柄失效）管理员也收到「…出错（详见日志）」、已经同意的照样报、打开的对话框关掉、窗口复原、焦点归还、锁释放；自动同意每天最多 `AUTO_ACCEPT_DAILY_MAX` 个（数存在 `data/auto_accept_state.json`，重启不清零、第二天重新数），到了告诉管理员一次、窗口不再碰；点之后屏幕上新出现的风控提示（「操作过于频繁」等，`RISK_MARKERS`）或连续 3 次点了申请还在就暂停到管理员发「自动同意好友 开」，管理员手动同意 / 添加好友时遇到风控提示也暂停（加百应达人微信经同一个 `add_friend_checked`）；`查状态` 显示今天的数和上限；加好友带备注（加百应达人微信的「抖音-…」）：4.0 的「申请添加朋友」窗口里 OCR 认出「备注 / 备注名 / 设置备注」标签和它正下方那一行才点那一行、Ctrl+A、退格、粘贴（在附言之前，按键都要申请窗口自己在前台），认不出（没有标签、标签同一行右边有字、下面太远或没有、下面是「标签」/ 按钮这些别的栏）就什么都不点、INFO 日志只写窗口自己的字、申请照发，点没送出、剪贴板出错也照发（`remark_set` 假），申请窗口丢了前台照旧中止、两个窗口关掉、备注和附言都没输；不带备注时点击和按键和以前一模一样（逐个比 1a7037e 上记下的顺序），管理员的「添加好友」从不带备注（`add_friend` 的调用一字不差）；单窗口界面不填、只在日志里说一次 |
| 读微信数据库（`src/wechat/wcdb_client.py`、`wcdb_backend.py`） | 进 `wcdb_api.dll` 的每一次调用都持客户端的 `_DLL_LOCK`：轮询线程（会话、群成员、查名字、语音）和新好友扫描一起跑也从不两个线程同时在 DLL 里，关库等正在进行的调用做完、关掉以后不再进 DLL（`tests/test_wcdb_backend_locking.py`，真的客户端接在假 DLL 上）；DLL 回错误码是读失败不是空列表：只有一个聊天读不出就跳过它、别的照读、读回来以后一条不丢，一轮里每个聊天都读不出算数据库读不了（计数、`db_readable` 变假、开 OCR 备选、第 5 次重开数据库、重开以后新消息照常交），真的没有消息不算失败，`open()` 试密钥时会话列表报错误码算这个密钥不行，新好友扫描读不出一个私聊不耽误别的、下一次再读（`tests/test_wcdb_read_errors.py`、`tests/test_friend_probe_tool.py`） |
| 电脑微信掉线提醒（`src/wechat/presence.py`） | 怎么认：微信进程不在马上算、找不到主窗口连续两次才算、发消息占着窗口不算、两次看之间重启过（升级）也算；运行中掉线（`offline_while_running`）从最后一次确认在线到再次在线；启动时和机器人一起停过（`stopped_with_bot`：微信比库里最后一次在线晚启动、或者没开）补记一段，只停了机器人不算；控制台的字和「看过了」；回来以后登记 / 名单达人的私聊往回读 200 条，同步进来的旧行按顺序补交（边缘带 `written_at`，以前被 5 分钟的规则丢掉；副驾驶一份先审的草稿），掉线还没结束时同步进来的也交，`PRM_CATCHUP_HOURS=0` 不交，没掉线时旧行照旧当历史；`Bot._start_presence` 的接线（`tests/test_wechat_gaps.py`，真的 WCDB 后端）；`wechat_gap` 事件两边同一套用例、Mac 自带 Python 3.9 存得下（`tests/test_edge_protocol.py`）；Mac 端存到看过了为止、同一段只记一次、写不进去不确认、不认识的事件类型照样确认（`tests/test_edge_backend.py`）；Windows 控制台的提醒、还没恢复的那段没有按钮（`tests/test_functional.py::TestWechatGapAlert`） |
| 达人名单页 | 左栏「达人名单」只有达人名单、待处理两个子页（老的审批单链接落到名单）；名单的添加（带平台和抖音号 / 小红书号）/ 停用 / 从最近私聊挑选、已在名单的再加入不清掉原来填的、待处理的回复草稿标为已处理（`tests/test_influencer_console.py`，用临时配置和数据库） |

### 手动验证清单

部分功能无法自动化测试（需要微信客户端或 Mac 上的 worker），修改这些模块后需手动验证：

1. **Claude 中转** — 需要微信运行 + Mac worker（`tools/claude_relay/README.md`）
2. **密钥提取** — 需要微信客户端运行
3. **达人副驾驶 / 微信边缘** — 需要微信 + Mac（`prm-copilot-test.md`、`edge-protocol.md`）
4. **好友管理** — 需要 Windows 版微信窗口（`friend-management-test.md`，第五轮：单独打开的聊天窗口开着时同意 / 添加好友不往里面打字，「前往验证」弹出的确认窗口的真实标题）；加百应达人微信第一次真的加人时，看一眼「申请添加朋友」窗口的「备注」栏有没有填成「抖音-…」（加完在通讯录里看她的备注也行），没填上就把日志里「Friend management」开头的那几行发回来（「no remark field (备注) was seen …」那一行写着为什么没认出、窗口上读到了哪几个字）；新好友识别（`friend_added`、登记达人里的「新好友」）要在真机上分别试电脑通过、手机通过、对方通过我们的申请；对方通过我们的申请以后一句话不说：登记（或 `PRM_AUTO_REGISTER` 自动登记）以后「待审批」里应出现一份标「开场白」的草稿
5. **消息发送** — 需要微信窗口可见；和别的聊天同名的达人不发（日志「Not using the WeChat window」），给她设一个不重复的备注、重启后能发；`SEND_VERIFY_CHAT=on`（默认）时每次发送日志里应有「SEND_VERIFY_CHAT: the open chat is … as asked」（如果一直是「could not be confirmed」，说明这台机器读不出标题，核对没起作用，把那几行发回来）；再拿小号试一次包含关系：小号 A 的备注设成「测试A」、小号 B 设成「测试A2」，只登记 A，让机器人给 A 发：发到 A；把 A 的备注改成别的、让「测试A」只能搜到 B（重启机器人），再发：日志「Not sending to …」、没有发到 B，草稿标成没发出去、原因写着打开的是「测试A2」
6. **电脑微信掉线提醒** — 需要 Windows 真机：退出微信、手机上退出电脑微信、被另一台电脑挤下线、升级微信、重启电脑、只停机器人，各看一次日志里「WeChat presence」那几行和控制台顶上的提醒（被挤下线时登录窗口会不会被当成主窗口，还没确认）；勾选「同步最近的聊天记录」重新登录后，同步进来的消息补交成草稿
7. **读库出错** — 需要 Windows 真机：机器人开着时，在电脑微信里退出登录再登录（或升级微信后再登录）。看日志：出现「Poll error … (WcdbCallError)」（以前什么都没有、一直读到 0 条），约半分钟内「Reinitializing WCDB backend」「WCDB reinitialized successfully」，之后用小号给大号发一句，机器人照常收到（副驾驶出草稿 / 边缘报事件）；如果只出现「WCDB could not read …」而别的聊天照常，说明只是那一个聊天读不出（记下是哪类聊天）
8. **在家打开控制台**（`tools/claude_relay/open-console.command`）— 需要 Windows 装好 OpenSSH 服务器、放好 Mac 的公钥（`cross-network.md` 第九节）：Mac 双击脚本，浏览器自动打开控制台，能点「发送」；关掉终端窗口后页面连不上

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
