# 功能 U（老板拉的群）· UI 部分的文档补充

Branch `boss-groups-ui` (spec §9). The coordinator merges the text below into `CODEBASE_REFERENCE.md` and `CLAUDE.md`
(neither file is edited on this branch). The UI reads the PRM side's new `/api/prm` fields and posts its two endpoints
exactly as spec §6.7 gives them; the Playwright tests serve them by route interception.

## 1. CODEBASE_REFERENCE.md §2.19 前端组件（新增一行，另三行各加一段）

新增一行（放在 `FriendAddCard.jsx` 那行后面）：

| 文件 | 组件 / 函数 | 说明 |
|---|---|---|
| `BossGroupsCard.jsx` | `BossGroupsCard({d, onDone})`、`BossGroupRow({g, disabled, onPick, onIgnore})` | 老板拉的群（`PRM_BOSS_GROUPS`，功能 U）。「合作单」页最上面的卡片「老板拉的群」（`GET /api/prm` 的 `boss_groups`；`enabled` 假或没有这个字段时什么都不显示）：副标题「老板（PRM_GROUP_WITH 的名字）把他微信里的达人和我们这个号拉进的小群：之后由我们跟她对接」，`why_not` 不空时一条黄色提醒（这时所有按钮不能点：机器人停着时照样列出找到的群）；一段说明（只有她一个人时机器人自己接手：登记、群里打招呼的开场白等人看过再发；之后她在群里说的话和私聊的草稿一个规矩发在群里，老板也看得到；老板和别人说的不起草）；一行数字（`bossGroupsLine`）；每个待选的群（`pending`）一块 `BossGroupRow`：群名、几点发现、「群里不止一个人可能是她，点哪个是她」，每个成员一行（名字或「（没有名字）」、wxid、「是她」→ `pickBossGroup(chat, wxid)`，一按就发、不问：开场白反正等人看过），「这个群不是达人」→ 确认框（「「群名」不是达人的群？」：从这里去掉、以后不再问、群里谁说什么都不起草不回，「再想想」/「不是达人」）→ `ignoreBossGroup(chat)`；成功的提示「已接手「群名」：<名字> 登记成达人，在群里打招呼的开场白写好后在「待审批」里等你看过再发」/「记下了：「群名」不是达人的群，机器人不管它」，失败的（`{ok: false, error}`）提示 `error` |

`ReviewPanel.jsx` 那行的说明后面加：

> 老板拉的群（`PRM_BOSS_GROUPS`，功能 U）：`channel === 'group'` 的草稿（`inGroup`）右上角多一个黄色标签「群里 · <group_name>」（`GroupTag`，带群图标；长群名用 `pages.css` 的 `.prm-group-tag .prm-group-name` 省略成「…」，手机宽度也不挤出卡片；鼠标停上去是完整群名）；「发送」「确认发送」先弹确认框「#n 发到群里？」：「这条回复会发在老板拉的群「群名」里，不是她的私聊：老板和群里的人都看得到。」（分几条发时加「会分 N 条发。」），「再想想」什么都不发，「发到群里」才 `POST /api/prm/packets`，提示「已确认，正在发到群里」；带平台动作的群草稿「批准并执行」的确认框多一行同样的话；最近处理过的表里类型写「群里 · <类型>」（鼠标停上去是群名）；没有 `channel`、`private`、`wechat` 的草稿和以前一模一样（不问、直接发）。`boss_groups` 开着且有待选的群时页面顶上一行（`bossGroupNotes`）「老板拉了 N 个新群，群里不止一个人可能是她：到「合作单」页顶上的「老板拉的群」里选哪个是她，选了机器人才接手。」

`DealsPanel.jsx` 那行的说明后面加：

> 老板拉的群（功能 U）：页面顶上（自动模式提醒下面、登记达人上面）`BossGroupsCard`；已登记的达人表里 `source === 'boss_group'`（达人上或她的合作单上，`bossIntroduced`）的名字后面标「老板介绍」；`ThreePartyBlock`：`three_party.by === 'boss'` 的已建好的群写「老板拉的群（HH:MM 发现）：微信里叫「群名」」和「老板把她和我们拉进了这个群，之后由我们跟她对接：她在群里说的话照常起草，回复发在群里，老板也看得到；老板和别人说的话不起草。」，没有建议群名和「复制群名」（群是老板建的、老板起的名），「群已经不在了」照旧（`group_none`）；别的三方群和以前一样。

`lib/prm.js` 那行：函数列表加 `pickBossGroup`、`ignoreBossGroup`、`inGroup`、`bossIntroduced`、`bossGroupsLine`、`bossGroupNotes`，说明后面加：

> 老板拉的群（功能 U）：`pickBossGroup(chat, wxid)`：`POST /api/prm/boss-groups/pick {chat, wxid}`；`ignoreBossGroup(chat)`：`POST /api/prm/boss-groups/ignore {chat}`；`inGroup(p)`：草稿的 `channel === 'group'`（别的值、没有都是私聊）；`bossIntroduced(p)`：达人或她的合作单的 `source === 'boss_group'`；`bossGroupsLine(bg)`：「已接手 N 个群；M 个群在等她进群（群里现在只有老板和我们，她进来以后机器人自己接手）；K 个群要你选哪个是她」（是 0 的那两段不写；什么都没有时「还没有发现老板新拉的群。」）；`bossGroupNotes(d)`：`[[kind, text]]`，`boss_groups.enabled` 且 `pending` 不空时一条 `warn`。

§4 功能 U 小节里控制台那一段（PRM 部分写接口，这里只写页面）：

> 页面：「合作单」页顶上的「老板拉的群」卡片（`BossGroupsCard.jsx`：待选的群、每人「是她」、「这个群不是达人」先确认、一行数字、`why_not`，关着什么都没有）、名单里的「老板介绍」、三方群一块写「老板拉的群」；「待审批」里群草稿的「群里 · 群名」标签和发送前的确认框（发在群里、老板也看得到），页面顶上有待选的群时一行提醒。读的字段：`boss_groups` `{enabled, why_not, boss, pending: [{chat, name, members: [{wxid, name}], found_at}], waiting, linked}`、草稿的 `channel`（`"group"`）和 `group_name`、达人 / 合作单的 `source`（`"boss_group"`）、合作单 `three_party` 的 `by`（`"boss"`）、`chat_name`、`at`。

## 2. CLAUDE.md 测试表：「待审批 / 合作单页（达人副驾驶）」那一行后面加

> 老板拉的群（`PRM_BOSS_GROUPS`，功能 U；页面测试里 `/api/prm` 的新字段和两个 POST 用 Playwright 拦截给出，别的都是内存里真的 PRM 服务）：「待审批」里 `channel` 为 `group` 的草稿标「群里 · 群名」（手机宽度时长群名省略、标签不出卡片），「发送」「确认发送」先弹确认框（发在老板拉的群里、不是她的私聊、老板和群里的人都看得到），「再想想」什么都不发，确认以后才发（改过的字照样记下），最近处理过的表里写「群里 · 类型」；没有 `channel`（以前的机器人）或 `private` 的草稿和以前一模一样、「发送」直接发不问；合作单名单里 `source` 为 `boss_group`（在达人上或在合作单上）的标「老板介绍」、人登记的没有，她的三方群一块（`three_party.by` 为 `boss`）写「老板拉的群（HH:MM 发现）：微信里叫「…」」和机器人在群里做什么、没有建议群名和「复制群名」，没有群的合作单照旧能「拉三方群」；合作单页顶上的「老板拉的群」卡片（在「登记达人」上面）：待选的群的群名、成员和 wxid（没有名字的写「（没有名字）」），「是她」一按就发 `{chat, wxid}`、不问，群从列表里去掉、已接手的数加一，「这个群不是达人」先问（「再想想」什么都不发），确认才发 `{chat}`，一行数字（已接手几个、几个在等她进群、几个要你选），`why_not` 时一条提醒、按钮都不能点，关着（`enabled` 假）或以前的机器人没有这个字段时两页都什么都没有；「待审批」页顶上「老板拉了 N 个新群…选哪个是她」，选完就没有了；设置里的 `PRM_BOSS_GROUPS`（开关）、`PRM_BOSS_GROUP_MAX`（范围 3–20）显示在合作单下面的设置里（`src/prm/boss_groups.py` 合进来以后才跑，之前跳过）（`tests/test_functional.py::TestPrmPages`）

（如果功能 U 单独成一行，这段放进那一行的「页面」部分也行。）

## 3. 真机检查清单（建议并进 PRM 部分的那一条）

> 老板拉的群（`PRM_BOSS_GROUPS`）— 页面这边：第一次有老板拉的群被接手时，「待审批」里那份开场白标着「开场白」和「群里 · 群名」，点「发送」先弹「发到群里？」的确认框；发出以后看它是不是真的在那个群里（不是她的私聊）。合作单名单里她标「老板介绍」，展开以后三方群那块写「老板拉的群」。群里不止一个可能是她的人时，合作单页顶上出现那个群，「是她」以后群从卡片上消失、「待审批」里出现群里的开场白。

## 4. Notes for the coordinator

- `members` of a pending group: every listed member gets a 「是她」 button, so the PRM side should list the
  candidates only (spec §2: without us, the bosses, ADMIN_WXID, the cc users). If it lists every member, the UI needs
  a per-member flag (for example `boss: true`) to leave the button out.
- 「群已经不在了」 stays on a boss group's 三方群 block and posts `group_none` as for any made group. Today
  `ThreeParty.mark(made=False)` only clears `deals.three_party`: the PRM side decides whether that also unwatches the
  group / changes its `boss_groups` row.
- Not in spec §9's list, added so the console "asks" where operators look: the one-line note on 待审批 while groups
  wait for a pick; the group line in the 批准并执行 confirmation; 「群里 · 类型」 in the 最近处理过的 table.
- `channel`: only `"group"` is a group draft and `"buyin"` stays the 百应 card; `"wechat"` (today's value),
  `"private"` or no field are all her private chat, so either naming on the PRM side works.
- The settings test (`test_deals_page_shows_the_boss_group_settings`) skips until `src/prm/boss_groups.py` exists, then
  checks the real advanced_config rows (keys `PRM_BOSS_GROUPS`, `PRM_BOSS_GROUP_MAX`, range 3–20).
