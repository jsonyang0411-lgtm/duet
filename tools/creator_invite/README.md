# 达人广场「邀约带货」两段式批量（tools/creator_invite）

在巨量百应「达人广场」给一批抖音达人发「邀约带货」卡片，分两段：

1. **plan（只读，什么都不发）**：找人、查状态、配话术和商品，出一份清单给人看。
2. **run（要用户对这一份清单明确同意）**：只发清单里的人，一位一位发，发一位记一位。

工具只调两个命令行，自己不碰平台：
- 聚水潭 `jushuitan-engineering`（`node bin/jst.mjs`）：`find` 找达人；配置了 `min_stock` 时还用 `goods-detail` 查真实可用库存。
- 抖音 `douyin-engineering`（`node bin/dy.mjs`）：`buyin author-invite-status` 一次查一批达人的邀约状态，`buyin author-invite` 预览 / 发送一张邀约卡。
  要用带 `--commission-rate` 和 `author-invite-status` 的版本（douyin-engineering 的分支 `author-invite-commission`，2026-10-29 已并入它的本地 main）。

只用 Python 标准库，macOS 自带的 `/usr/bin/python3`（3.9）就能跑。只在 Mac 上用：台账加锁用的是 `fcntl`。

## 准备

- 调试 Chrome（默认 `http://127.0.0.1:9222`）里登录了**百应**和**聚水潭**，并且**开着一个百应的标签页**。工具给抖音命令行加 `--no-open`，不会替你开新标签页。
- 一份配置（JSON），照 `config.example.json` 写，放在仓库外、权限 600。店铺 ID、聚水潭子账号、邀约卡上的微信号只写在那份里。

| 配置 | 意思 | 默认 |
|---|---|---|
| `dy_expect_shop_id` | 抖店店铺 ID：每条抖音命令都先核对登录的是不是这家店（不是就退出码 79） | 必填 |
| `jst_expect_uid` | 聚水潭子账号：每条聚水潭命令都先核对（本地核对令牌，不发请求） | 必填 |
| `invite_wechat` / `invite_phone` | 邀约卡上给达人看的联系方式，格式照抽屉的规则；至少一个 | — |
| `brand` | 话术里 `{品牌}` 换成什么 | 话术里有 `{品牌}` 就必填 |
| `commission_rate` | 专属佣金，整数，1 到 25（用户的上限；抖音命令行也拦） | 25 |
| `rights` | 权益：`high-commission`（专属高佣）、`video-material`（视频素材支持）、`free-sample`、`video-boost` | 专属高佣 + 视频素材支持 |
| `products` | 1 到 5 款（一张卡最多 5 款）：`{id 抖音商品 ID, name, price 售价（元）, styles 聚水潭款号}`；`styles` 只在 `min_stock` 大于 0 时必填 | 必填 |
| `min_stock` | 0 = 不看库存，能挂上卡的都配（老板 2026-10-29）；大于 0 时，一款的真实可用库存（各款号加起来）低于它就不配 | 0 |
| `find` | 找达人的条件：`goods_category`（帽子 1000003461，以后可以换中高端女装的类目）、`price_min` / `price_max`（件单价）、`queries`（几组粉丝范围，每组 `pages` 页，每页 20 位） | 见下 |
| `rules` | 分类和标记的门槛（见下） | 见下 |
| `templates` | 三类话术，`small` / `mid` / `big`；不写就用内置的三条 | 内置 |
| `weekly_cap` | 我们自己每周最多发几位 | 800 |
| `quota_margin` | 平台本周额度剩下不到这么多就停 | 50 |
| `gap_seconds` | 任意两次平台请求之间至少隔几秒（不能小于 3） | 3 |
| `wait_between` | 发两位之间随机等多少秒 | [15, 30] |
| `plan_max_age_days` | 清单多少天后作废、要重新 plan | 3 |
| `home` | 台账和清单的默认位置 | `~/.claude-relay/platform/creator-invites` |

## 第一段：plan

```bash
/usr/bin/python3 tools/creator_invite/invite.py plan --config <配置> --count 30 [--out-dir <目录>] [--ledger-dir <目录>]
```

做的事（全是只读命令，没有一条带 `--execute`）：

1. 只有 `min_stock` 大于 0 时：每款商品的每个款号 `jst goods-detail`，加起来是这款的真实可用库存，低于 `min_stock` 的不配。默认 0，不看库存、一个请求都不发。
2. 每组 `queries` 跑一次 `jst find --platform DY --goods-category … --price-min … --price-max … --not-added`（带粉丝范围、页数）。
3. 本地先筛：抖音号格式命令行不收的、台账里邀约过的（或不知道上次发出去没有的）去掉；**帽子占比 ≥ 90% 的标「疑似同行自播号」，默认不进清单，单独列一张表**；**女性粉丝低于 60% 的不进**（我们卖女帽，用户 2026-10-29；粉丝画像只给占比最大的性别：「男性 62%」算女性 38%）；粉丝不到 5000 的不进；帽子占比不到 1% 的、粉丝性别不知道的进清单但标出来给人看。
4. 剩下的排个先后：每一类里没有标记的在前、帽子占比高的在前，三类轮流（大主播/专场型、中大博主、小达人、大主播……），这样清单够数时三类都有。按这个顺序每 20 位跑一次 `dy buyin author-invite-status`：搜不到的、状态不是 1（「邀约带货」）的去掉；够 `--count` 位就不再查，查过、能邀、但超出人数的列为「备选」。浮层里本周的次数（按达人等级分档）记进清单。
5. 分三类、配商品和话术，写三个文件（权限 600）：`plan-<编号>.md`（给人看）、`.csv`（同样的列，Excel 能开）、`.json`（给第二段用）。

**三类**（按 `jst find` 给的字段，先判第一条）：
- **大主播 / 专场型**：按销售额定的主播类型是直播（`channelBySales`），近 30 天直播销售额区间下限 ≥ 500 万（`sales30dLive.min`），带货等级 ≥ LV4。
- **中大博主**：粉丝 ≥ 20 万。
- **小达人**（垂类穿搭 / 配饰）：粉丝 5000 到 20 万（15 万～20 万也算小达人，用户 2026-10-29）。

**配商品**：配置里的商品每位都配（最多 5 款，一张卡的上限）：主推是售价最接近这位达人件单价的那款（一样近取配置里靠前的），其余的按配置顺序附带。设了 `min_stock` 时缺货的不配。

**话术**（内置，`{品牌}` 换成配置里的 `brand`；字数照抖音命令行的算法，都在 150 字以内；只写「包邮」，不写快递公司，写了配置检查就不过）：
- 小达人：您好！我们是原创设计师帽饰品牌{品牌}，主打英伦复古、老钱风女帽，客单价高、转化好。很喜欢您的穿搭内容，诚邀您带我们的帽子：25%专属高佣，免费寄样包邮，另有视频素材支持。期待合作～
- 中大博主：您好！我们是设计师女帽品牌{品牌}（英伦复古/老钱风），一直关注您的内容，诚邀您合作带货：现货样品任选3顶，免费包邮寄出；25%专属高佣，另有视频素材支持。期待您的回复～
- 大主播 / 专场型：您好！我们是设计师女帽品牌{品牌}，主打英伦复古、老钱风全系帽饰。诚邀您做一场品牌专场：专场样品看中多少款寄多少款，全系现货锁库存；25%专属高佣，优质视频投流支持。期待合作！

卡片的权益统一是「专属高佣 + 视频素材支持」，不勾免费申样（这几款不支持免费申样，勾了也会显示「买样」），也不勾「优质视频投放」（用户 2026-10-29）；「免费寄样包邮」写在话术里，达人回复后走聚水潭寄样单。专属佣金长期有效，不设结束日期（用户 2026-10-29）。

## 第二段：run

```bash
/usr/bin/python3 tools/creator_invite/invite.py run --config <配置> --plan <plan-编号.json> --confirm <编号> [--limit N] [--ledger-dir <目录>]
```

- `--confirm` 必须写**这份清单的编号**（用户同意的是哪一份就写哪一份）；编号不对、清单超过 `plan_max_age_days`、清单里的专属佣金或权益和现在的配置不一样，都不发、退出码 2。
- 只发清单里的人，按清单顺序。每一位：
  1. 先跑一次**不带** `--execute` 的 `dy buyin author-invite`（预览）。只有预览说状态 1、门槛 -1（不弹确认）、没被拦的才往下走，其余记一笔「跳过」。
  2. 读预览里本周的次数（`statusBefore.weeklyQuota`）：剩下的不到 `quota_margin` 就**停**。
  3. 台账里先写一行 `attempt`（落盘后才往下），再带 `--expect-status 1 --expect-popwindow -1 --execute` 发送：预览和执行绑在一起，这期间状态或门槛变了，命令行就 `STATE_CONFLICT`、什么都不发。
  4. 台账里写一行 `result`。
- 两位之间随机等 15～30 秒（`wait_between`）；任意两次平台请求之间至少 3 秒。
- 一台 Mac 上同一个台账同时只能有一个 run（`invites.lock`）。

**什么时候停**（停下时打印停在清单第几位、为什么，执行记录写在台账目录的 `run-<时间>/report.md`）：

| 情况 | 怎么办 |
|---|---|
| 退出码 76（验证码 / 限流）、77（登录失效）、79（登录的不是这家店）——预览或发送时 | 立刻停，不再发任何请求；到调试 Chrome 里处理好再重跑 |
| 平台本周额度剩下不到 `quota_margin` | 停 |
| 我们自己本周已发（台账）到了 `weekly_cap` | 停（周一 0 点按北京时间算新的一周） |
| 发送超时、被打断（130）、没拿到结果 | 停；这位记为「不知道发出去没有」，**以后也不会再发**，到百应 › 消息里看 |
| 连续 3 位预览失败 | 停，查原因 |
| `STATE_CONFLICT` | 跳过这位、继续；什么都没发，以后的 plan 会重新查她 |
| `--limit` 到了 | 停 |

**接着上次停下的地方**：同一个命令重跑就行。台账里有 `attempt`、又没有「没发出去」的 `result` 的抖音号，永远不再发——哪怕进程在写完 `attempt` 之后死掉。

## 台账

`<home 或 --ledger-dir>/invites.jsonl`，一行一个 JSON，只追加，权限 600：

```
{"at": "2026-10-28T15:00:00+08:00", "event": "run-start", "plan_id": "P20261028-1500-ab12"}
{"at": "…", "event": "attempt", "attempt": "9f1c…", "plan_id": "…", "aweme_id": "…", "products": ["…", "…", "…"], "commission_rate": 25, "category": "small"}
{"at": "…", "event": "result", "attempt": "9f1c…", "aweme_id": "…", "exit": 0, "outcome": "VERIFIED_SUCCESS", "platform_writes": 1, "sent": true, "timed_out": false}
{"at": "…", "event": "skip", "aweme_id": "…", "why": "状态 2、门槛 -1（只发 状态 1、门槛 -1 的）"}
{"at": "…", "event": "stop", "aweme_id": "…", "phase": "preview", "exit": 76}
{"at": "…", "event": "run-end", "plan_id": "…", "sent": 12, "stopped": "…"}
```

`sent`：`true` 发出去了（`VERIFIED_SUCCESS`、`ACCEPTED_UNVERIFIED`、`INDETERMINATE`……，或命令行说发了写请求），`false` 没发（`STATE_CONFLICT`、发送前就被拦），`null` 不知道。联系方式不写进台账。

```bash
/usr/bin/python3 tools/creator_invite/invite.py status --config <配置> [--ledger-dir <目录>]   # 本周发了几位、哪些不知道发出去没有
```

## 放慢

两个命令行都不经 `bin/*.mjs` 直接跑，而是经本目录的两个包装（命令行的代码一行不改）：

- `paced-dy.mjs`：给抖音命令行页面传输的每个请求排队，等到离上一个平台请求（跨进程也算：台账目录的 `last-request` 记着时间）至少 `gap_seconds`。工具把 `DY_TRANSPORT` 去掉，保证走的是被放慢的页面传输。
- `paced-jst.mjs`：给聚水潭命令行换一个同样排队、放慢的 `fetch`。

每次运行的请求数记在 `work-…/requests.jsonl` 或 `run-…/requests.jsonl`（时间、方法、路径，不记参数、正文和应答）。

## 测试

`tests/test_creator_invite.py`：全部离线，用假的 `node`（答复照两个命令行的 JSON 形状，数据都是编的）。覆盖：plan 只读、筛选和分类（15～20 万算小达人、女粉 < 60% 不进）、配商品（主推 + 其余全部，最多 5 款）、默认不查库存、设了 `min_stock` 才查和不配缺货的、每 20 位一次状态查询；run 的预览绑定执行、不重发（进程在 `attempt` 之后死掉、发送超时）、76 / 77 / 79 立刻停、`STATE_CONFLICT` 跳过、平台额度和每周上限、接着上次、同时只能一个 run；两个包装用真的 node 测请求间隔；再用 `/usr/bin/python3`（3.9）跑一遍 plan。
