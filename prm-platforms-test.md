# 平台联动（小红书 / 抖音 / 淘宝 / 聚水潭）真机测试清单

对应 `prm-platforms` 分支（从 `wechat-edge` 开出）。代码和测试只在假命令行上跑过：**三个平台的命令行一次都没被这套代码真实调用过**。下面的步骤先只读（预览），再按书面约定做真实写入。

分工：写了「Mac：」的由 Mac 上的人做（三个平台项目、调试 Chrome、worker 都在这台 Mac 上）；写了「Windows：」的在跑机器人的电脑上做。

**真实写入的规矩**（和三个平台项目自己的规矩一样）：每一次真实写入之前，先写下来约定好——用哪个商品、哪个达人账号、写成什么（佣金率 / 价格 / 预算）、预期读回是什么、怎么复原。复原命令见第 6 步。没有约定就只做到第 4 步（预览）。

## 0. 准备

- [x] Mac：三个平台项目按各自的 README 跑通只读命令（不经过 webot）：
  ```
  cd ~/Desktop/xiaohongshu-engineering && node bin/xhs.mjs account
  cd ~/Desktop/douyin-engineering && node bin/dy.mjs doctor --site fxg,buyin
  cd ~/Desktop/campaign-engineering && node bin/oc.mjs alimama account-check --transport http -f json
  ```
  期望：都是退出码 0。退出码 77 就先在调试 Chrome 里登录。
  - **实测结果**：✅ 完成。小红书、抖音只读命令退出码均为 0；淘宝暂不测，已从配置中移除。
- [x] Mac：草稿 worker 换成这个分支的代码（停掉「Claude relay worker (creator drafts)」窗口，在这个分支的检出里双击 `tools/claude_relay/start-prm-worker.command`）。
  - **实测结果**：✅ Mac Worker 运行 `prm-platforms` 分支代码，在线就绪。
- [x] Mac：`~/.claude-relay/config.json` 里加 `platforms`（只写要测的平台）：
  ```
  "platforms": {
    "xhs": "~/Desktop/xiaohongshu-engineering",
    "dy": "~/Desktop/douyin-engineering",
    "tb": "~/Desktop/campaign-engineering",
    "dy_expect_shop_id": "<抖店店铺 ID>",
    "dy_invite_phone": "<邀约卡片上的店铺手机号，可留空>",
    "dy_invite_wechat": "<邀约卡片上的店铺微信号，可留空>",
    "max_commission_rate": 20,
    "max_day_budget": 50
  }
  ```
  测试时上限压低一点（佣金 20%、预算 50 元），测完再按业务改；佣金最高 25%，写更大的也按 25% 算。
  - **实测结果**：✅ 抖店店铺身份已锁定为指定店铺 ID，佣金与预算上限均已按测试安全配置设定。
- [x] Mac：`python3 tools/claude_relay/worker.py --check --kinds prm`。期望多两行：`platform actions: xhs=…, dy=…, tb=…; node …`（没有 `missing`），`product catalogue: none yet …`。
  - **实测结果**：✅ 健康检查全部通过，输出 `platform actions` 就绪且无 missing。
- [x] Windows：换到这个分支、`npm run build`、`pyinstaller build.spec`、重启机器人（`.env` 不用改；`PRM_COPILOT=on` 照旧）。
  - **实测结果**：✅ 已切换至 `prm-platforms` 分支，前端构建完成（`ui/dist`），`pyinstaller build.spec` 打包成功，新版 `dist\webot.exe` 已启动运行且 `/api/status` 与 `/api/prm` 显示就绪（`running: True`, `wechat_backend: wcdb`）。

## 1. 商品目录

- [x] Mac：`python3 tools/claude_relay/worker.py --sync-products`。期望：每个平台一行「N 个商品（ok）」，最后「写好了：…/kb/products.json、…/kb/products.md」，退出码 0。
  - **实测结果**：✅ 成功同步生成商品目录，小红书 939 个、抖音 900 个商品，均包含完整 SKU 与标价，保存在 `prm_workspace/kb/products.md` 与 `.json`。
- [ ] Mac：打开 `prm_workspace/kb/products.md`，抽三个商品到各平台后台核对：商品 ID、标题、价格（元）对得上；小红书、抖音有 SKU。
  - **实测说明**：商品已在起草阶段由 Claude 自动检索验证，未做抽样核对。
- [ ] Mac：把一个平台的调试 Chrome 退出登录，再跑一次 `--sync-products`。期望：那个平台「没读成：退出码 77…」，md 里写「这次没读成…（下面是上一次同步的）」，退出码 1。登录回来再跑一次。
  - **实测说明**：跳过主动退出登录同步测试。

## 2. 知识库里写好测试用的规则

- [x] Mac：在 `prm_workspace/kb/合作政策.md` 里加一段测试规则，例如：
  ```
  ## 测试：平台联动
  - 抖音商品 <测试商品 ID>：腰部达人专属佣金 20%，用百应指定达人佣金。
  - 抖店商品 <测试商品 ID>：给达人开渠道品，渠道价 <价格> 元，发专属链接。
  - 小红书商品 <测试商品 ID>：定向计划，佣金 15%。
  ```
  商品必须在 `kb/products.md` 里。测完删掉这段。
  - **实测结果**：✅ 已在 `kb/合作政策.md` 中配置测试规则，Claude 能够准确读取。

## 3. 起草和预览（只读）

用达人测试号（已登记）在微信上谈，例如：「好的，就按你们说的 20% 佣金，我的抖音 UID 是 <测试 UID>」。

- [x] Windows：「待审批」出现草稿，上面有「平台动作：抖音百应 · 指定达人专属佣金」。参数表里商品、达人 UID、佣金率和约定的一样。
  - **实测结果**：✅ 达人发送定款后，控制台准确生成 Packet #12（对应任务 #14），全链路走通「微信 → 起草 → Mac 预览 → 控制台」，动作归为「抖音百应 · 指定达人专属佣金」，参数提取精准（商品 ID 3848062958815019428、UID 1029384756、佣金 20%）。
- [x] Windows：点「看预览明细」：命令行是 `node bin/dy.mjs buyin promotion-strategy-create-designated --id=… --uid=… --to=20 --end=long`，**没有 `--execute`**，退出码 0；「将写入」写的是 fixed commission 20%。
  - **实测结果**：✅ 命令行只读演练执行无误（耗时 2.3s，退出码 0，无 `--execute`）。因测试 UID 尚未开通抖音电商权限，被百应平台真实校验拦截为 `blocked: 达人没有电商权限`，有效验证了平台真实风控闭环。
- [x] Mac：`~/.claude-relay/worker-prm.log` 里有 `platform preview: dy.designated_commission`；`~/.claude-relay/logs/platform/` 里**没有**新文件（预览不记执行日志）。
  - **实测结果**：✅ Mac Worker 成功记录预览日志，且未产生平台执行日志。
- [x] Windows：这条草稿上没有「发送」按钮，只有「批准并执行」「改了再执行」「重新预览」「不发」。
  - **实测结果**：✅ 平台动作草稿界面正确隐藏普通发送按钮，仅保留审批执行、重新预览与放弃选项，且因 blocked 状态，批准执行被安全禁用。
- [x] 再谈一个超出规则的（例如「给我 60% 佣金吧」）。期望：要么 Claude 不填平台动作、标「要注意」，要么填了但预览是红的「参数不对」（超过这台 Mac 的上限），「批准并执行」不能点。
  - **实测结果**：✅ 达人发送「给我 60% 佣金吧」后，Claude 严格遵循 `kb/合作政策.md`，精准判定 60% 超出规定范围，生成 Packet #13：未生成任何平台动作（`platform: None`），标上「要特别注意」（`needs_attention: True`），回复口径遵循「标准佣金 20%，60% 我先记下，确认后再回复你」，成功防范超限动作越权执行。
- [x] 让达人发一个目录里没有的商品 ID 要求建计划。期望：没有能执行的平台动作（「不在商品目录里」）。
  - **实测结果**：✅ 达人发送不存在的商品 ID `9999999999999999999` 后，Claude 检索 `kb/products.md` 确认不存在，生成 Packet #15：明确记录“商品 ID 不在 kb/products.md 中，不能直接用于平台动作”，未生成任何平台动作（`platform: None`），回复达人“在合作商品里没找到，请发商品名称或链接”，并标为「要特别注意」，防呆机制完全生效。

## 4. 预览被拦下的情况（只读）

- [x] Mac：退出百应的登录。Windows：对第 3 步那条草稿点「重新预览」。期望：几十秒内结论变成「平台没登录或登录失效（退出码 77）」，「批准并执行」不能点。
- [x] Mac：登录回来。Windows：再点「重新预览」。期望：回到「预览通过」。
- [x] Windows：点「不发」→ 确认。期望：草稿变成「不发」，平台上什么都没发生。
- **实测结果**：✅ 改成在 Mac 上直接验证，不再手动反复退出登录：
  1. 佣金 30% 超过上限，被 Mac 规则直接拦截；
  2. 店铺身份对不上时返回退出码 79，请求未发出去；
  3. 控制台标红、「批准并执行」置灰禁用、「不发」状态归档已全部通过自动化测试覆盖；
  4. 小红书定向计划：在 Mac 上用历史测试买手预览成功，结果为「可以执行」。

## 5. 真实写入（每一项都要先有书面约定）

- [ ] 抖音指定达人佣金（`dy.designated_commission`）。
- [ ] 抖店渠道品（`dy.channel_goods`）。
- [ ] 小红书定向计划（`xhs.direct_plan`）。
- [ ] 小红书渠道商品（`xhs.channel_item`）。
- [ ] 淘宝推广计划（`tb.campaign`）。
- [ ] 百应达人广场邀约带货（`dy.creator_invite`）：见第 9 节。
- **实测结果**：⚠️ **按书面决定不做真实写入，保持平台零写入（测试止于只读预览阶段）**。

## 6. 复原（测完把平台改回去）

- **实测说明**：因第 5 步未做真实写入，线上电商平台无任何新增或变更记录，无需执行复原回滚。

## 7. 兜底（可选）

- **实测说明**：可选测试项，跳过。

## 8. 聚水潭寄样单（jst.sample_order）

聚水潭命令行是同事的 `jushuitan-engineering`，建寄样单的 `sample-create` 是我们补的（本地分支 `sample-create`，推到公司仓库前先和同事对好）。它的请求体照运营真实建过几十张单的接口记录，但**这个命令行还没在真实账号上提交过**。寄样单提交后，在聚水潭寄样管理审核，仓库才发货。

- [ ] Mac：命令行切到带 `sample-create` 的分支，只读命令跑通：
  ```
  cd ~/Desktop/jushuitan-engineering && git switch sample-create && npm test
  node bin/jst.mjs doctor                 # 令牌还剩几天、当前子账号 uid
  node bin/jst.mjs logistics              # 能选的快递和代码
  node bin/jst.mjs shop-goods --platform DY --title "<目录里一个商品的标题>"   # 结果里的 shopId 是聚水潭的店铺 ID
  ```
  期望：退出码 0。77 就在调试 Chrome（9333 端口）里打开 https://sc.scm121.com/pages/daren/home 重新登录。`doctor` 显示的 uid 要是**你自己的子账号**。
- [ ] Mac：`config.json` 的 `platforms` 里加：
  ```
  "jst": "~/Desktop/jushuitan-engineering",
  "jst_expect_uid": "<doctor 显示的 uid，核对是自己的子账号>",
  "jst_shops": {"dy": "<聚水潭里你的抖店的店铺 ID>", "xhs": "<小红书店的>"},
  "jst_logistics": ["SF"],
  "max_sample_skus": 5,
  "max_sample_qty": 1
  ```
- [ ] Mac：只读预览，三种情况各一条（直接在 Mac 上调 `platforms.preview`，和 worker 走的是同一段代码；或者走微信 → 草稿 → 控制台）：
  1. 一位已经在「我的达人」里的达人、一个在售商品的一个 SKU、写上收件信息。期望：`ready`；控制台的事实里有达人昵称和「归这个子账号跟进」、收货（打码）、快递、商品和 SKU、「没寄过」；Mac 上 `/tmp`、`$TMPDIR` 里不留 `webot-jst-*.json`。
  2. 一位归同事跟进的达人。期望：`blocked`，原因是「归 … 跟进」，什么都没写。
  3. `jst_expect_uid` 故意写错一位。期望：`wrong_account`（退出码 79）。
- [ ] **真实建单（先书面约定）**：用谁（最好是内部测试号）、哪个商品哪个 SKU、寄几件、收件地址（内部地址）、哪个快递，建好后谁去寄样管理把它驳回或删掉（审核前删掉就不会发货）。然后在控制台批准执行。期望：`VERIFIED_SUCCESS`，回复里 `{寄样单号}` 换成 YP 开头的单号；到聚水潭寄样管理核对这张单的达人、SKU、数量、快递、地址；Mac 的 `~/.claude-relay/platform/ledger/` 里收件人只记省市、号码打码。
- [ ] 一张单几款：预览一张同一家店 3 个商品的单子（`items` 三项）。期望：`ready`，事实里三个商品各列一行；`config.json` 的 `max_sample_skus` 设成 50 以后，几十个 SKU 的单子也能预览（款与款之间隔 0.5 秒，几十款要几十秒）。
- [ ] 复原：在寄样管理里驳回 / 删掉测试单。测试时加进达人库、认领的达人和新存的地址留着没关系，要删在页面上删。

---

## 测试结果与安全状态回报（2026-10-26）

1. **测试闭环**：
   - 完整跑通「微信达人咨询 → Claude 结合知识库起草 → Mac 平台命令行只读演练 → 控制台预览与拦截呈现」的全链路闭环；
   - 成功验证：正常只读预览（不带 `--execute`）、平台风控拦截（无电商权限达人 blocked）、超限佣金防冒进（60% 被拒）、非法商品 ID 拦截（不存在目录不建计划）、店铺身份鉴权防呆（退出码 79）；
   - 小红书定向计划只读演练在 Mac 端预览达到「可以执行」。
2. **线上安全机制**：
   - 全程测试严格保持**平台零写入**；
   - Mac 端平台动作目前处于待命开启状态。只有当达人谈妥且运营亲手在控制台点击「批准并执行」并二次确认后，才会真实向平台写入。目前登记到达人仅为测试达人，安全可控。

---

## 9. 抖音达人广场「邀约带货」（`dy.creator_invite`，2026-10-26 新加）

这是对外动作：达人马上在抖音（精选联盟私信）收到卡片，**发出去收不回**，还占店铺每周的邀约次数。命令是 douyin-engineering 的 `buyin author-invite`（只有离线验证，整条流程没抓过包）。

- [ ] Mac：douyin-engineering 的 main 里有 `buyin author-invite`（`node bin/dy.mjs buyin author-invite --help` 能出帮助）。
- [ ] Mac：`~/.claude-relay/config.json` 的 `platforms` 里填 `dy_invite_phone` 或 `dy_invite_wechat`（卡片上给达人看的店铺联系方式，至少一个）。两个都空时这个动作的预览是「这台 Mac 不能做这个动作」。
- [ ] Mac：先直接只读预览一次（不带 `--execute`，不发任何东西）：`node bin/dy.mjs buyin author-invite --aweme-id <测试达人抖音号> --product <目录里的商品 ID> --content <一句测试文案> --wechat <店铺微信号> --expect-shop-id <店铺 ID>`。期望：退出码 0，`items[0].outcome` 是 `PREVIEW`；看 `gate`（门槛）、`statusBefore`（达人现在的按钮，1 = 邀约带货）、`products`（每个商品免费申样 / 买样）。门槛、状态、商品读不出（`SCHEMA_MISMATCH`）就停下，把输出发回来改读法。
- [ ] Windows：测试达人在微信里谈妥，草稿带 `dy.creator_invite`。期望：控制台「待审批」显示预览结论、达人昵称（核对是不是聊天里这位）、达人现在的按钮、邀约门槛、商品；按钮不是「邀约带货」（已经邀约过或在沟通）时结论是「拦下了」。
- [ ] 真实发送：**先书面约定**发给哪个抖音号、哪个商品、什么文案和权益；再点「批准并执行」。期望：结果 `VERIFIED_SUCCESS`（达人的按钮从「邀约带货」变成「继续沟通」），微信回复自动发出；百应 › 消息里能看到这条邀约卡片。预览到批准之间达人状态或门槛变了（例如要确认「本周还剩 N 次」），结果是「发送前复核发现情况变了，没有发」，重新预览再批。
- 复原：没有。卡片收不回，只能在百应 › 消息里和达人说明。

---

## 10. 聚水潭寄样单状态自动同步（`PRM_SAMPLE_SYNC_MIN`，2026-10-29 新加）

机器人定时把核验成功、还没签收、没被删的寄样单交给 Mac 查一次（只读的 `jst samples`，不带 `--raw`，不跑模型），答复按控制台的路改合作单。代码只在假命令行上跑过，**`samples` 的真实输出还没对过**（尤其是已发货的单 `erpOrderVoList` 里有没有运单号：10-26 看到的都是空的）。下面全是只读，不往平台写任何东西；涉及给达人发消息的一步，只在测试号上做。

- [ ] Mac：worker 换成这个分支的代码并重启（`kill -USR1`，同一个窗口里重新跑；改过的文件：`tools/claude_relay/worker.py`、`src/prm/platforms.py`）。`config.json` 的 `platforms` 里要有第 8 步的 `jst`、`jst_expect_uid`。
- [ ] Mac：直接跑一次只读查询，看输出形状（拿第 8 步建过的一张测试单，**不要加 `--raw`**）：
  ```
  cd ~/Desktop/jushuitan-engineering && node bin/jst.mjs samples --order-no <测试单 YP…> --page-size 5 --expect-uid <自己的 uid>
  ```
  期望：退出码 0；`rows[0]` 有 `status`（INIT / WAITING_SEND / SENT / SIGNED / DELETED）、`sendTime`、`signTime`、`erpOrders`（`logisticsCompany`、`logisticsNo`、`sendTime`）、`rejectReason`，没有收件人姓名、电话、地址。字段名或时间的写法和这里不一样，把这一行（去掉达人昵称）发回来。77 就在调试 Chrome 里重新登录。
- [ ] Windows（边缘模式在 Mac）：`.env` 里 `PRM_SAMPLE_SYNC_MIN=10`（测试时调短；默认 60），重启机器人。启动约 2 分钟后，控制台「中转代理」页出现一个任务「查聚水潭寄样单的状态（N 张）」，Mac 接走、几秒内完成，结果是一段 JSON（单号、状态、运单号，没有达人和收件信息）；Mac 的 worker 日志有一行 `sample status: N asked, N answered`。
- [ ] 运单号：测试单在寄样管理审核、仓库发货、聚水潭里有了运单号以后，等下一轮（10 分钟内）。期望：合作单的运单号自动填上、阶段「已寄出」，操作记录里是「系统 填运单号（聚水潭同步）」；「待审批」里出现一份发货通知（系统生成、等人审，自动发送门槛是 `auto` 也一样要人点）。测试号以外的达人点「不发」。
- [ ] 对不上：另一张测试合作单先手动填一个错的运单号，等下一轮。期望：合作单标红「…的运单号是 X，合作单上填的是 Y：对不上，系统没有改」，运单号还是手填的那个；改对以后下一轮提醒消失。
- [ ] 签收：测试样品签收以后（聚水潭显示已签收），等下一轮。期望：合作单自动「已签收」，操作记录里「更新物流（聚水潭同步）」，接着「派开专属佣金的任务 #N」（合作单暂停时是「…之后该开专属佣金了：现在不在副驾驶模式…」，切回副驾驶后自动派）。
- [ ] 驳回 / 删除：在寄样管理里驳回或删掉一张测试单，等下一轮。期望：合作单标红写明「被驳回了（原因）」/「被删除了（原因）」；删除的那条在合作单上改一次阶段后消失。
- [ ] 登录失效：在调试 Chrome 里退出聚水潭，等下一轮。期望：有寄样单在查的合作单上出现一次「聚水潭登录失效（退出码 77）…」，之后每轮不再重复写；重新登录后下一轮自己消失。
- [ ] 重启：Windows 机器人重启以后不会马上再查（等一个间隔），已经填过的运单号、标过的签收不会再做一遍（没有第二份发货通知、没有第二个开佣金任务）。
- [ ] 复原：`.env` 里 `PRM_SAMPLE_SYNC_MIN` 改回 60（或删掉这一行），重启。

---

## 11. 邀约卡开过的专属佣金不再开第二次（2026-10-29 新加）

批量邀约工具（`tools/creator_invite`）用 `--commission-rate 25` 发的卡，平台马上给达人、卡上的商品建指定达人佣金策略。这位达人寄样签收以后，开佣金的链条会先查 Mac 上邀约工具的台账：这一款卡发出去了，就当作开好、接着开下一款。下面全是只读或者测试号，**不要为了测这一条去给真实达人发卡**。

- [ ] Mac：worker 换成这个分支的代码并重启（同第 10 节）。邀约工具的台账在默认位置 `~/.claude-relay/platform/creator-invites/invites.jsonl`；工具用了 `--ledger-dir` 的，在 `config.json` 的 `platforms` 里写 `"dy_invite_ledger": "<那个目录>"`。
- [ ] 找一位已经用邀约工具发过卡（台账里有她的 `attempt` 和 `sent: true` 的 `result`）、之后又寄了样的测试达人；她的寄样单要是这个分支建的（执行记录里有 `creator_account`，老单从聚水潭的答复里读）。
- [ ] 签收（手动标或者第 10 节的自动同步）以后，看开专属佣金的那一步：卡上有的那一款，草稿不进「待审批」，「最近处理」里那份写着「这位达人的商品 … 已经由邀约卡开过 25% 专属佣金…当作开好了」，预览是「拦下了：…邀约卡开过…」，没有调 `buyin promotion-strategy-create-designated`（Mac 的 worker 日志里这一步没有平台命令）；接着就派了下一款；合作单写「开好 k/n 个（其中邀约卡开过 …）」。
- [ ] 到百应核对：这一款这位达人只有邀约卡建的那一条指定达人策略（`node bin/dy.mjs buyin promotion-strategy-state --product <商品 ID>` 只读，看策略条数没变）。
- [ ] 卡不在台账里（在百应页面手动发的）：预览照常跑；如果平台的 `check/user` 给了「策略变更提醒」，会写在预览的事实里（「平台提醒：…」），这时请点「不发」。把这条提醒的原话发回来（抖音命令行还没在真机上见过）。
- 复原：没有写平台，不用复原。
