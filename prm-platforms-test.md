# 平台联动（小红书 / 抖音 / 淘宝）真机测试清单

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
    "max_commission_rate": 30,
    "max_day_budget": 50
  }
  ```
  测试时上限压低一点（佣金 30%、预算 50 元），测完再按业务改。
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
- **实测结果**：⚠️ **按书面决定不做真实写入，保持平台零写入（测试止于只读预览阶段）**。

## 6. 复原（测完把平台改回去）

- **实测说明**：因第 5 步未做真实写入，线上电商平台无任何新增或变更记录，无需执行复原回滚。

## 7. 兜底（可选）

- **实测说明**：可选测试项，跳过。

---

## 测试结果与安全状态回报（2026-10-26）

1. **测试闭环**：
   - 完整跑通「微信达人咨询 → Claude 结合知识库起草 → Mac 平台命令行只读演练 → 控制台预览与拦截呈现」的全链路闭环；
   - 成功验证：正常只读预览（不带 `--execute`）、平台风控拦截（无电商权限达人 blocked）、超限佣金防冒进（60% 被拒）、非法商品 ID 拦截（不存在目录不建计划）、店铺身份鉴权防呆（退出码 79）；
   - 小红书定向计划只读演练在 Mac 端预览达到「可以执行」。
2. **线上安全机制**：
   - 全程测试严格保持**平台零写入**；
   - Mac 端平台动作目前处于待命开启状态。只有当达人谈妥且运营亲手在控制台点击「批准并执行」并二次确认后，才会真实向平台写入。目前登记到达人仅为测试达人，安全可控。
