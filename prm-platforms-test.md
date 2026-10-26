# 平台联动（小红书 / 抖音 / 淘宝）真机测试清单

对应 `prm-platforms` 分支（从 `wechat-edge` 开出）。代码和测试只在假命令行上跑过：**三个平台的命令行一次都没被这套代码真实调用过**。下面的步骤先只读（预览），再按书面约定做真实写入。

分工：写了「Mac：」的由 Mac 上的人做（三个平台项目、调试 Chrome、worker 都在这台 Mac 上）；写了「Windows：」的在跑机器人的电脑上做。

**真实写入的规矩**（和三个平台项目自己的规矩一样）：每一次真实写入之前，先写下来约定好——用哪个商品、哪个达人账号、写成什么（佣金率 / 价格 / 预算）、预期读回是什么、怎么复原。复原命令见第 6 步。没有约定就只做到第 4 步（预览）。

## 0. 准备

- [ ] Mac：三个平台项目按各自的 README 跑通只读命令（不经过 webot）：
  ```
  cd ~/Desktop/xiaohongshu-engineering && node bin/xhs.mjs account
  cd ~/Desktop/douyin-engineering && node bin/dy.mjs doctor --site fxg,buyin
  cd ~/Desktop/campaign-engineering && node bin/oc.mjs alimama account-check --transport http -f json
  ```
  期望：都是退出码 0。退出码 77 就先在调试 Chrome 里登录。
- [ ] Mac：草稿 worker 换成这个分支的代码（停掉「Claude relay worker (creator drafts)」窗口，在这个分支的检出里双击 `tools/claude_relay/start-prm-worker.command`）。
- [ ] Mac：`~/.claude-relay/config.json` 里加 `platforms`（只写要测的平台）：
  ```
  "platforms": {
    "xhs": "~/Desktop/xiaohongshu-engineering",
    "dy": "~/Desktop/douyin-engineering",
    "tb": "~/Desktop/campaign-engineering",
    "dy_expect_shop_id": "<抖店店铺 ID>",
    "max_commission_rate": 30,
    "max_day_budget": 50
  }
  ```
  测试时上限压低一点（佣金 30%、预算 50 元），测完再按业务改。
- [ ] Mac：`python3 tools/claude_relay/worker.py --check --kinds prm`。期望多两行：`platform actions: xhs=…, dy=…, tb=…; node …`（没有 `missing`），`product catalogue: none yet …`。
- [x] Windows：换到这个分支、`npm run build`、`pyinstaller build.spec`、重启机器人（`.env` 不用改；`PRM_COPILOT=on` 照旧）。
  - **实测结果**：✅ 已切换至 `prm-platforms` 分支，前端构建完成（`ui/dist`），`pyinstaller build.spec` 打包成功，新版 `dist\webot.exe` 已启动运行且 `/api/status` 与 `/api/prm` 显示就绪（`running: True`, `wechat_backend: wcdb`）。

## 1. 商品目录

- [ ] Mac：`python3 tools/claude_relay/worker.py --sync-products`。期望：每个平台一行「N 个商品（ok）」，最后「写好了：…/kb/products.json、…/kb/products.md」，退出码 0。
- [ ] Mac：打开 `prm_workspace/kb/products.md`，抽三个商品到各平台后台核对：商品 ID、标题、价格（元）对得上；小红书、抖音有 SKU。
- [ ] Mac：把一个平台的调试 Chrome 退出登录，再跑一次 `--sync-products`。期望：那个平台「没读成：退出码 77…」，md 里写「这次没读成…（下面是上一次同步的）」，退出码 1。登录回来再跑一次。

## 2. 知识库里写好测试用的规则

- [ ] Mac：在 `prm_workspace/kb/合作政策.md` 里加一段测试规则，例如：
  ```
  ## 测试：平台联动
  - 抖音商品 <测试商品 ID>：腰部达人专属佣金 20%，用百应指定达人佣金。
  - 抖店商品 <测试商品 ID>：给达人开渠道品，渠道价 <价格> 元，发专属链接。
  - 小红书商品 <测试商品 ID>：定向计划，佣金 15%。
  ```
  商品必须在 `kb/products.md` 里。测完删掉这段。

## 3. 起草和预览（只读）

用达人测试号（已登记）在微信上谈，例如：「好的，就按你们说的 20% 佣金，我的抖音 UID 是 <测试 UID>」。

- [ ] Windows：「待审批」出现草稿，上面有「平台动作：抖音百应 · 指定达人专属佣金」和绿色「预览通过，可以批准执行」。参数表里商品、达人 UID、佣金率和约定的一样。
- [ ] Windows：点「看预览明细」：命令行是 `node bin/dy.mjs buyin promotion-strategy-create-designated --id=… --uid=… --to=20 --end=long`，**没有 `--execute`**，退出码 0；「将写入」写的是 fixed commission 20%。
- [ ] Mac：`~/.claude-relay/worker-prm.log` 里有 `platform preview: dy.designated_commission` 和 `platform preview: ready`；`~/.claude-relay/logs/platform/` 里**没有**新文件（预览不记执行日志）。
- [ ] Windows：这条草稿上没有「发送」按钮，只有「批准并执行」「改了再执行」「重新预览」「不发」。
- [ ] 再谈一个超出规则的（例如「给我 60% 佣金吧」）。期望：要么 Claude 不填平台动作、标「要注意」，要么填了但预览是红的「参数不对」（超过这台 Mac 的上限），「批准并执行」不能点。
- [ ] 让达人发一个目录里没有的商品 ID 要求建计划。期望：没有能执行的平台动作（「不在商品目录里」）。

## 4. 预览被拦下的情况（只读）

- [ ] Mac：退出百应的登录。Windows：对第 3 步那条草稿点「重新预览」。期望：几十秒内结论变成「平台没登录或登录失效（退出码 77）」，「批准并执行」不能点。
- [ ] Mac：登录回来。Windows：再点「重新预览」。期望：回到「预览通过」。
- [ ] Windows：点「不发」→ 确认。期望：草稿变成「不发」，平台上什么都没发生。

## 5. 真实写入（每一项都要先有书面约定）

每一项都是：谈出带平台动作的草稿 → 预览通过 → 点「批准并执行」→ 确认框里核对参数 → 点「执行」。

- [ ] 抖音指定达人佣金（`dy.designated_commission`）。期望：
  - 草稿先变成「平台执行中（Mac 任务 #n）」，一两分钟内变成已发出；达人测试号收到回复。
  - Mac：`~/.claude-relay/logs/platform/` 里有这次的记录，`outcome` 是 `VERIFIED_SUCCESS`，`created.strategy_ids` 有策略 ID；`~/.claude-relay/platform/ledger/` 里有一个 `<32 位>.json`，`status: done`。
  - 百应后台：这个商品的策略抽屉里有一条新的「指定达人」策略，佣金和达人对得上。
  - 第一次可能是 `ACCEPTED_UNVERIFIED`（平台的抽屉读回慢，抖音项目自己的验证记录里出现过）：那样回复不会发，「待审批」页最上面红色「要人工处理」。到后台核对后点「我已处理」，自己回达人。
- [ ] 抖店渠道品（`dy.channel_goods`），回复里写了 `{链接}`。期望：执行记录里先 `create`（execute）、再 `bind`（预览）、再 `bind`（execute），达人收到的回复里 `{链接}` 换成了 `https://haohuo.jinritemai.com/...channel_id=...` 的链接。抖店后台：渠道品在审核中，绑的是这位达人。
- [ ] 小红书定向计划（`xhs.direct_plan`），达人给的是小红书号。期望：预览明细第一条是找买手，写着「买手：<昵称>（粉丝 …）…请核对是不是这位达人」——**先核对再批准**；第二条是只读整条演练（`tools/dry-execute.mjs`）。执行后千帆后台有一个叫「达人合作<编号>-<时间>」的定向计划。
- [ ] 小红书渠道商品（`xhs.channel_item`）：按小红书工具的规则，创建成功也只给 `ACCEPTED_UNVERIFIED`，所以**期望是红色**「要人工处理」、回复不发。千帆后台核对新渠道商品后点「我已处理」。
- [ ] 淘宝推广计划（`tb.campaign`）：日预算用最小值。期望：万相台里多一个「达人合作<编号>_<时间>」计划，状态暂停。到目前为止这条命令的读回都是 `ACCEPTED_UNVERIFIED`，所以多半也是红色、回复不发。

## 6. 复原（测完把平台改回去）

- 抖音指定达人佣金：`node bin/dy.mjs buyin promotion-strategy-close --product <商品 ID> --id <策略 ID> --designated --execute`（关了还能 `-recover`；`promotion-strategy-delete … --designated` 删掉不能恢复）。
- 抖店渠道品：`node bin/dy.mjs fxg channel-goods-status --product-id <主商品 ID> --channel-id <渠道品 ID> --to offline --execute`，再 `fxg channel-goods-delete …`（删除不能恢复）。
- 小红书定向计划：`node bin/xhs.mjs direct-plan --action stop --group-id <计划组 ID> --execute`（停止不能恢复），再 `--action delete`。
- 小红书渠道商品：`node bin/xhs.mjs channel-item --action delete --item-id <渠道商品 ID> --execute`（不能恢复）。
- 淘宝计划：本来就是暂停的；要删就在万相台里删。确认暂停：`node bin/oc.mjs alimama campaign-status --campaign-id <ID> --action pause --biz-code onebpDisplay --transport http -f json`（预览，读当前状态）。

每条都先不带 `--execute` 看一遍预览。

## 7. 兜底（可选）

- [ ] Mac：停掉草稿 worker，Windows 批准一条预览通过的草稿。期望：一直是「平台执行中」；30 分钟后变红「Mac 30 分钟没有接手，平台没有动」，Mac 再开起来也不会执行它。
- [ ] 记下：从点「执行」到达人收到回复用了多久（每个平台一条）。

## 结果

把每一项的结果、Mac 的 `~/.claude-relay/logs/platform/` 里对应的 JSON、控制台截图记下来。红色的项写清楚是哪个结果（`ACCEPTED_UNVERIFIED` / `INDETERMINATE` / …）和平台后台实际是什么样。
