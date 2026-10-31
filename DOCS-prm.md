# DOCS-prm.md — 功能 U：老板拉的群（PRM 部分，分支 boss-groups-prm）

协调者合并进 `CODEBASE_REFERENCE.md` 和 `CLAUDE.md` 用。代码以 boss-groups-prm 分支为准；后端（`own_wxid` / `contact_ids` /
`boss_groups` / `group_members` / `watch_group` / `unwatch_group`、看着的群的自己的行交给 `on_own_message`）是后端那一部分建的，
本分支的测试用假后端（`tests/test_prm_boss_groups.py::Backend`）。

---

## 一、给 CODEBASE_REFERENCE.md

### 最后更新那一行（加在最前面）

2026-10-31（老板拉的群：老板拉了有他、达人和这个号的小群，机器人接手这位达人，群里只起草她的话、发在群里，审批 / 门槛 / 下班时段 /
自动模式照旧，开场白一律先审，群里不回「稍等」；`PRM_BOSS_GROUPS` 默认关，`PRM_BOSS_GROUP_MAX` 默认 6，功能 U；之前：……）

### §1 全局变量/参数清单（新增）

| 名称 | 类型 | 默认值 | 定义位置 | 消费位置 / 说明 |
|---|---|---|---|---|
| `PRM_BOSS_GROUPS` | `str` on/off | `"off"` | `src/config.py` | `BossGroups._on`：开着时 PRM 的发送线程每 60 秒找老板（`PRM_GROUP_WITH`）新拉的群并接手；关着什么都不做（不扫、不看、群照旧一律不回）。要 `PRM_COPILOT=on` 和非空的 `PRM_GROUP_WITH`，没有时控制台说「先在设置里填 PRM_GROUP_WITH（老板在这个微信里的名字）」（不拒绝启动）（功能 U） |
| `PRM_BOSS_GROUP_MAX` | `int` 3–20 | `6` | `src/config.py` | `BossGroups._max` → `backend.boss_groups(..., max_members)`：人数更多的群当成老板平时的群，不接手（功能 U） |
| `BOSS_SCAN_SEC` / `BOSS_WAIT_DAYS` / `BOSS_WATCH_BACK_SEC` / `BOSS_IDS_SEC` | `int` | `60` / `3` / `300` / `600` | `src/prm/boss_groups.py` | 扫描间隔；只有老板和我们的群等她进群的天数（之后 `ignored`）；接手时从发现前多久开始读群（有 `created_at` 时取更早的那个，最多 `PRM_CATCHUP_HOURS`）；老板 wxid（`contact_ids` 要读每个群的成员）多久才重新查一次 |
| `IGNORED_KEEP_DAYS` / `REPLAY_MAX` / `SEEN_IDS` | `int` | `30` / `200` / `500` | `src/prm/boss_groups.py` | 「不管了」的群的行留多久（删掉时群编号进基线，永远不算新群）；接手时从消息库补交的行最多几条；每个接手的群记住的消息编号（路由和补交只交一次） |
| `LINES_KEPT` / `LINES_TOLD` / `LINE_CHARS` | `int` | `30` / `10` / `300` | `src/prm/boss_groups.py` | 群里老板和别人的话：一个群最多留几句（最新的）、一份草稿任务最多带几句、每句最多几个字 |
| `BASELINE` | `str` | `"boss_groups_baseline"` | `src/prm/boss_groups.py` | `prm_meta` 的键：第一次扫描时已有的群的编号（JSON 列表），合作单自己的三方群编号也写进来 |
| `PENDING` / `WAITING` / `LINKED` / `IGNORED` / `GONE` | `str` | `"pending"` / `"waiting"` / `"linked"` / `"ignored"` / `"gone"` | `src/prm/boss_groups.py` | `boss_groups.state`：等运营认人 / 等她进群 / 已接手 / 不管了（运营说不是达人、3 天没人进群、像是我们自己拉的三方群） / 群已经不在了（运营在合作单点「群已经不在了」） |
| `CHANNEL` / `OPENING_HOW` / `EVENT` / `LEFT_NOTICE` | `str` | `"group"` / `"boss_group"` / `"boss_group"` / `"boss_group_left:"` | `src/prm/boss_groups.py` | 群里的草稿任务和草稿的 `meta.channel`；群开场白的 `meta.opening` 的 `how` 和 `by`；合作单的记录「老板拉群介绍（群名）」；她不在群里时合作单的提醒（+ 群编号） |
| `UNREACHABLE` / `UNREACHABLE_TEXT` / `GROUP_UNSENT` | `str` | `"unreachable"` / 「她不是我们的微信好友，也不在老板拉的群里了：机器人发不了消息给她」 / `"boss_group_unsent"` | `src/prm/service.py` | 机器人自己起的消息（发货通知、开专属佣金那一步、控制台的开场白）哪儿都发不到她时合作单的提醒（她再发来消息时消失）；发到群里的草稿没发出去（后端拒发：`GROUP_NO_NAME`、同名…）时合作单的提醒（之后群里有一条发出去就消失） |

### §2 函数清单

**新文件 `src/prm/boss_groups.py`**（模块函数）

| 函数 | 说明 |
|---|---|
| `her_name(name, wxid: str) -> str` | 登记她用的称呼：这个号看到的名字（空白、ㅤ 折叠，最多 30 字），没有就「老板介绍 <wxid 前 8 位>」 |
| `her_note(group_name: str) -> str` | 她的备注「老板介绍（老板拉的群：<群名>）」（最多 200 字） |
| `clean_members(raw, own: str = "") -> list[dict]` | 后端给的成员 `[{"wxid", "name"}]`：每个 wxid 一次，去掉我们自己（`own_wxid()`，按相等比） |
| `candidates(members: list, bosses, reserved=()) -> list[dict]` | 可能是她的人：去掉老板（wxid）、管理员和 cc 用户（`reserved`）、群编号 |
| `boss_groups_out(store, *, enabled: bool, why_not: str = "", boss=(), reserved=()) -> dict` | 控制台的 `boss_groups`（运行中和停着都用） |

**`class BossGroups`**（`PrmService.boss_groups`）

| 方法 | 说明 |
|---|---|
| `__init__(self, store, config=None, *, clock=time.time, reserved=(), register=None, opening=None, handle=None)` | `register` = `PrmService.register`，`opening` = `PrmService._group_opening`，`handle` = `PrmService.handle`（补交的行），`reserved` = 管理员 + cc 用户 |
| `attach(self, backend, stored=None) -> None` | 后端，和机器人消息库的 `get_messages_since`（`Bot._start_prm`，在 `prm.start()` 之前） |
| `on`（属性）/ `why_not(self) -> str` / `status(self) -> dict` | 开关；为什么现在找不了（没填老板、后端不支持、库读不了、不知道自己的 wxid、通讯录认不出老板）；控制台的 `boss_groups` |
| `is_linked(self, chat) -> bool` / `linked(self, chat) -> Optional[dict]` / `party_of(self, chat) -> Optional[dict]` | 是不是已接手的群（开关开着；内存里，路由每条群消息都问）；它的行；她的达人（在名单里的） |
| `party_has_group(self, party_id) -> bool` / `reach_group(self, party_id) -> Optional[dict]` | 这位达人有没有接手的群；她还在里面的最新那个群（机器人自己起的消息发到这里） |
| `knows_private(self, wxid) -> bool` | 后端认不认得她的私聊（`chat_target` 给得出名字 = 微信的会话列表里有她：是好友） |
| `who(self, row, sender) -> str` / `boss_said(self, row, line) -> bool` | 不是她的那句是「老板」还是「群成员」；群里的系统行（撤回）有没有写着老板的名字 |
| `note_line(self, chat, written)` / `add_context(self, chat, who, text, at)` / `job_group(self, chat) -> dict` / `replied(self, chat, at)` / `first_time(self, chat, message_id) -> bool` | 处理到的最后一行（重启后从这里读）；留一句老板 / 别人的话（打码）；草稿任务的 `meta.group`；我们在群里回过了（之前的话不再带）；这一行是不是第一次交来 |
| `sweep(self) -> Optional[int]` / `scan(self) -> Optional[int]` | 到点就扫（`PrmService.tick`）/ 马上扫一次：第一次只记基线；之后新群进 `boss_groups`；返回接手了几个 |
| `take_over(self, chat, wxid, name="", *, actor="system") -> dict` | 接手：登记（或只关联）→ 关联合作单 → `watch_group` → 补交消息库里存着的行 → 群开场白 → 记录 → 一行日志 |
| `start(self) -> int` | 启动时把接手的群重新 `watch_group`（从最后处理的那行起） |
| `pick(self, chat, wxid) -> dict` / `ignore(self, chat) -> dict` | 控制台「是她」/「这个群不是达人」 |
| `gone(self, deal, actor="console") -> bool` | 合作单「群已经不在了」：群改成 `gone`、`unwatch_group`、她在群里的话又一律不回、记一条 |
| `purge(self, deal) -> int` / `sweep_old(self, now) -> int` | 8.2 清理：成员名字、群名、留着的话、记录里的群名；「不管了」满 30 天删掉、编号进基线 |

**`src/prm/service.py`（`PrmService`，新增和改动）**

| 方法 | 说明 |
|---|---|
| `is_party_group(self, chat_id) -> bool` | 路由出错时的兜底：是不是接手的群 |
| `_group_message(self, msg) -> bool` | `handle` 的群消息：不是接手的群（或她停用了）→ False；她的话 → `_take(..., group=row)`；老板 / 别人的话 → `add_context`；系统行不起草（没写老板名字的撤回当成她的：没发出去的草稿改成人审） |
| `_take(self, msg, party, chat_id, group=None)` / `_submit(self, ..., new_friend=None, group=None)` | `group`：她在群里说的：任务带 `meta.channel` / `meta.group`；这个群还没被取走的开场白任务取消（被取走的，回来时算过时） |
| `_group_opening(self, party, chat) -> tuple` | 群开场白任务（`take_over` 调）：`meta.opening = {"how": "boss_group", "by": "boss_group"}`、`meta.channel`、`meta.group` |
| `_reach(self, party) -> tuple` / `_private_known(self, party) -> bool` / `_bot_started(self, packet) -> bool` / `_aim(self, packet) -> Optional[dict]` | 机器人自己起的消息发到哪：没有接手的群 → 她的私聊（和以前一样）；有：私聊（她在私聊里说过话，或后端认得她的私聊）→ 否则她还在的那个群 → 都没有：不发、合作单提醒 |
| `_lane_chat(self, party_id, chat_id) -> Optional[str]` / `_job_in(self, job_id, chat, party) -> bool` | 有接手的群的达人：新消息只让同一个聊天里在等的草稿过时（每份回复发到她说话的地方，私聊的话不在老板面前回） |
| `_opening_outdated(self, job, meta) -> bool` | 群开场白之后她在群里说了话（有更新的草稿任务）：开场白过时 |
| `_chat_party(self, chat_id)` / `_group_party(self, chat_id)` | 读回认草稿时：私聊按 wxid，群按接手的群 |
| `_no_opening(self, party, deal, now, chat="")` | 多了 `chat`（`_reach` 的结果；None = 哪儿都发不到） |
| 改：`handle`、`on_own_message`、`_own_line`、`_match_sent`、`_outbound`（`where`）、`on_result`、`_night_judge`（`channel` 是 group 也照下班时段的规矩）、`_draft_failed`、`_ask_opening`、`_dispatch_step`、`_step_blocked`、`deal_action`（`shipment` 后 `_aim`；`group_none` 后 `boss_groups.gone`）、`send_approved`、`_deliver`、`send_holding_replies`、`tick`、`start`、`purge`、`purge_due`、`snapshot`；模块函数 `opening_note`、`event_out`、`snapshot`（`packet_out` 的 `channel` / `group_name`，`party_out` 的 `source`） | |

**`src/prm/store.py`（`PrmStore`，新增）**

`add_boss_group(self, chat, *, name, found_at, state, members, boss, why="", made_at=None, now=None) -> bool`、
`boss_group(self, chat) -> Optional[dict]`、`boss_groups(self, states=(), *, deal_id=None) -> list[dict]`、
`update_boss_group(self, chat, now=None, *, states=(), **fields) -> bool`（比较交换）、`note_boss_group_line(self, chat, at)`、
`add_boss_group_line(self, chat, line, keep)`、`boss_group_replied(self, chat, at)`、`delete_boss_groups(self, *, state, before) -> list[str]`、
`scrub_boss_groups(self, deal_id, now=None) -> int`、`clear_event_keys(self, deal_id, kind, keys) -> int`、
`aim_packet(self, packet_id, chat_id, meta) -> bool`；模块函数 `boss_group_view(row)`，常量 `BOSS_GROUP_JSON` / `BOSS_GROUP_FIELDS`。

**其他**

- `src/routing/steps.py` `prm`：出错兜底改成「群：`is_party_group`，私聊：`is_party_chat`」；接手的群的消息 trace 打码写「[老板拉的群（已接手），内容不记录]」。
- `src/web/server.py`：`_boss_groups_stopped_status(store) -> dict`、`_prm_boss_groups(data, action) -> dict`；`POST /api/prm/boss-groups/pick`、`/api/prm/boss-groups/ignore`（`_POST_ONLY_PATHS`、只认本机页面）；停着时 `/api/prm` 的 `boss_groups` 按 .env。
- `src/web/advanced_config.py`：PRM 组最后两项 `PRM_BOSS_GROUPS`、`PRM_BOSS_GROUP_MAX`。
- `src/bot.py` `_start_prm`：`prm.boss_groups.attach(backend, stored=router 消息库的 get_messages_since)`，在 `prm.start()` 之前。

### §3 模块间关系图（加在 PRM 那一块）

```
PrmService.tick → boss_groups.sweep → scan → backend.group_ids（第一次：基线）/ own_wxid / contact_ids（10 分钟一次）
    → backend.boss_groups(老板 wxid, known = 基线 ∪ 已有的行 ∪ 每张合作单的 three_party.chat, PRM_BOSS_GROUP_MAX)
    → _found → 恰好一人：take_over / 两人以上：pending / 没人：waiting（_look_again：group_members，3 天）
    → _look_at_linked：group_members → 她不在了：合作单提醒
take_over → PrmService.register → deals.three_party（by boss）→ backend.watch_group → _replay（消息库）→ PrmService._group_opening
路由 prm 步骤 → PrmService.handle → _group_message → _take / add_context / _system_line
backend 自己的行 → PrmService.on_own_message（接手的群也交）→ _own_line → _confirm / 接管 / boss_groups.replied
控制台 POST /api/prm/boss-groups/pick|ignore → boss_groups.pick / ignore；POST /api/prm/deals group_none → three_party.mark → boss_groups.gone
```

### §4 功能U: 老板拉的群（`PRM_BOSS_GROUPS`，默认关）

用户 2026-10-31：「老板那边也有一点人是已经加了微信的，然后老板想把他和达人还有我们这边也拉个群，让我们来负责和达人对接」；「起草后人审再发到群里，像之前的加个自动模式」；「全自动接手」。

```
发送线程每 60 秒：boss_groups.sweep → scan
  第一次：backend.group_ids() → prm_meta boss_groups_baseline（那时已有的群永远不算新群）
  之后：own_wxid() → contact_ids(PRM_GROUP_WITH)（缓存 10 分钟）→ boss_groups(老板 wxid, known, PRM_BOSS_GROUP_MAX)
    known = 基线 ∪ boss_groups 的行 ∪ 每张合作单 three_party.chat（合作单自己的三方群写进基线：清理以后也不算新群）
  每个新群：成员 − 我们 − 老板 − 管理员 − cc 用户 = 候选
    恰好 1 个 → take_over（她的合作单「拉三方群」正在建 / 不确定：ignored，像是我们自己拉的群）
    2 个以上 → pending（控制台「是她」/「这个群不是达人」）
    0 个 → waiting：每次扫描读 group_members，3 天内进来 1 个 → take_over，2 个 → pending；3 天后 ignored
  已接手的群：读成员，她不在了 → 合作单提醒（她回来就消失）
take_over(群, 她)
  她是登记过的（在名单里）：只关联；停用过的：扫描不接手（pending，「她在达人名单里停用过…」），运营点「是她」才重新登记
  否则 PrmService.register(她的 wxid, 她的名字, 「老板介绍（老板拉的群：<群名>）」)
  → deals.three_party = {state: created, at: 发现时间, by: boss, chat, name, members: 人数, why: 老板拉的群}
     （合作单已经有建好的 / 正在建的三方群：保留原来的，记录里写「她原来的三方群保留」）
  → backend.watch_group(群, since)：since = 最后处理的那行；没有就 min(发现时间 − 300 秒, 群里最早一行 created_at)，最多往回 PRM_CATCHUP_HOURS
  → _replay：机器人消息库里这个群 since 以后存着的行（WECHAT_GROUPS 读到过、路由存了没回；watch 的补读会跳过存过的行）按顺序交给 handle 一次
  → _group_opening：群开场白任务（她已经说过话、有她的草稿任务时不写）
  → 合作单记录「老板拉群介绍（群名）」，一行日志（只有群编号前 8 位、达人编号、人数）
接手的群里一句话（路由 prm 步骤 → handle → _group_message）
  她说的 → _take → _submit(group)：任务 chat_id = 群，meta.channel = "group"，meta.group = {name, boss, lines}
     → 草稿 chat_id = 群，meta.channel / group_name → 审批 / 门槛 / 下班时段 / 自动模式照旧 → _deliver 发到群里（分几条发照旧）
  老板 / 别人说的 → 打码留着（最多 30 句），只给她的下一份草稿当背景（我们在群里最后一次回复的那个任务之后的，最多 10 句）
  系统行 → 不起草；没写老板名字的撤回当成她的（没发出去的草稿改人审）
我们在群里的行（后端的 own-message hook）→ on_own_message：机器人的 → 读回确认；运营亲手打的 → 接管 PRM_TAKEOVER_MIN
机器人自己起的消息（发货通知、开专属佣金、控制台的开场白）→ _reach：私聊（她在私聊说过话 / 后端 chat_target 认得她的私聊）
  → 否则她还在的接手的群 → 都没有：不发，合作单提醒「她不是我们的微信好友，也不在老板拉的群里了…」
合作单「群已经不在了」（group_none，three_party.chat 是接手的群）→ boss_groups.gone：state gone、unwatch_group、群里的话又一律不回、记一条
```

| 情况 | 怎么做 |
|---|---|
| 开关关着 | 什么都不做：不扫、不看、不调后端；群照旧一律不回；以前接手过的群（库里的行）也不起草 |
| 开场白 | 一律先审（门槛自动、下班时段、自动模式都不发）；没写成时提醒「请直接在老板拉的群里跟她打招呼」 |
| 群里的「稍等」 | 从不发（老板也看得到） |
| 她私聊和群里都说话 | 同一张合作单；每份回复发到她说话的那个聊天；一个聊天的新消息不让另一个聊天在等的草稿过时；草稿任务的 `meta.sent` 每条带 `where`（group / private，没有接手的群的达人不带，提示和以前一字不差） |
| 群发送被后端拒绝（`GROUP_NO_NAME` 等） | 草稿标成没发出去（原因写在草稿上），合作单提醒到群里下一条发出去 |
| 日志 | 从不写名字（她的、老板的、群名、成员的）和话，只写群编号前 8 位、达人 / 草稿编号、人数 |

数据（`src/prm/store.py`，新表）：

```sql
CREATE TABLE IF NOT EXISTS boss_groups (
    chat TEXT PRIMARY KEY,                -- 群编号 …@chatroom
    name TEXT NOT NULL DEFAULT '',        -- 微信给的群名（清理时清空）
    found_at REAL NOT NULL,               -- 扫描发现的时间
    made_at REAL,                         -- 后端给的群里最早一行（created_at）
    state TEXT NOT NULL,                  -- pending | waiting | linked | ignored | gone
    party_id INTEGER, deal_id INTEGER,    -- 接手以后：她的达人和合作单
    members TEXT NOT NULL DEFAULT '[]',   -- [{"wxid","name"}]，不含我们；名字只在 prm.db（清理时清空名字）
    boss TEXT NOT NULL DEFAULT '[]',      -- 群里的老板 [{"wxid","name"}]（name 是 PRM_GROUP_WITH 里写的）
    why TEXT NOT NULL DEFAULT '',
    last_line_at REAL,                    -- 处理到的最后一行（重启后 watch_group 从这里读）
    lines TEXT NOT NULL DEFAULT '[]',     -- 老板 / 别人的话 [{"who","at","text"}]，打码，给她的下一份草稿
    replied_at REAL,                      -- 我们在群里最后一次回复（那个任务建的时间 / 运营亲手打的时间）
    updated_at REAL NOT NULL
);
```

草稿任务的 `meta`（交给 Mac worker）：

```
她在群里的话：  {"party_id", "deal_id", "name", "channel": "group",
               "group": {"name": 群名, "boss": [老板的名字], "lines": [{"who": "老板"|"群成员", "at": "MM-DD HH:MM", "text"}]},
               "sent": [{..., "where": "group"|"private"}], ...其余照旧（creator / three_party / history / night / reply_parts）}
群开场白：      {"party_id", "deal_id", "name", "opening": {"how": "boss_group", "by": "boss_group"}, "channel": "group",
               "group": {...}, "three_party": {"at"}, "sent"?, "history"?, "reply_parts"?}（text 是空的）
发到群里的开专属佣金 / 控制台开场白：照旧的 meta 再加 "channel": "group", "group": {...}
没有接手的群的达人：没有 channel / group / where，和以前一模一样
```

草稿的 `meta`：`{"channel": "group", "group_name": 群名, ...}`；`packet_out` 加 `channel`（`"wechat"` / `"group"` / `"buyin"`）和 `group_name`；`party_out` 和它的 `deal` 加 `source`（`"boss_group"` / `""`）。

控制台接口：

```
GET  /api/prm → "boss_groups": {"enabled", "why_not", "boss": [名字],
                               "pending": [{"chat", "name", "size", "found_at", "why", "members": [{"wxid", "name"}]}],
                               "waiting": n, "linked": n}
     （members 只有可能是她的人：不含我们、老板、管理员、cc 用户；停着时 enabled 按 .env，why_not「机器人没有运行」）
POST /api/prm/boss-groups/pick   {"chat", "wxid"} → {"ok", "error"?, "party_id"?, "deal_id"?}   → PrmService.boss_groups.pick
POST /api/prm/boss-groups/ignore {"chat"}         → {"ok", "error"?}                           → PrmService.boss_groups.ignore
     （只认 POST、只认本机页面；停着时都说「机器人没有运行（或没打开达人副驾驶），现在不能处理老板拉的群」）
POST /api/prm/deals {"action": "group_none", "deal_id"} → three_party.mark → 接手的群：boss_groups.gone
```

还没在真机上走过：后端的几个读法（找新群、成员、看着的群的消息和自己的行）是后端那一部分写的，这里全是假后端。

### §5 配置文件和环境变量（`.env` 完整配置项里 PRM 那一段加两行）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PRM_BOSS_GROUPS` | `off` | 接手老板拉的群（功能 U）；要 `PRM_COPILOT=on` 和 `PRM_GROUP_WITH`；只认打开以后新出现的群；改完重启机器人 |
| `PRM_BOSS_GROUP_MAX` | `6` | 3–20；人数更多的群不接手 |

---

## 二、给 CLAUDE.md

### 测试表新的一行（放在「三方群」那一行后面）

| 老板拉的群（`PRM_BOSS_GROUPS`，默认关，功能 U） | PRM 这一侧（`tests/test_prm_boss_groups.py`，后端是假的、方法按 spec §4）：第一次扫描只记基线、那时已有的群从不接手、库读不了先不记；每分钟一次、说清为什么找不了（没填老板、不知道自己的 wxid、认不出老板、后端不支持）；除了我们和老板恰好一个人：登记（称呼、备注「老板介绍（老板拉的群：群名）」）、合作单的三方群 `by: boss`、`watch_group`（发现前 5 分钟，或群里最早一行，最多 `PRM_CATCHUP_HOURS`）、群开场白任务（`meta.opening` / `channel` / `group`）、记录「老板拉群介绍」、`source: boss_group`；两个以上：控制台只列可能是她的人（不含我们、老板、管理员、cc）和 `size`，「是她」只认这些人，「这个群不是达人」以后永远不再看、30 天后删掉编号进基线；没人：等她进群、进来就接手、两人进来改问控制台、3 天没人不再看；已登记的只关联、合作单已有三方群的保留原来的；停用过的扫描不接手、运营点「是她」才重新登记；合作单自己的三方群（「拉三方群」建的，清理以后也一样）从不接手，「拉三方群」正在建 / 不确定时看到的新群当成我们的不接手；重启后从最后一行重新看、不重复登记、不重复开场白；她在群里的话：开场白任务取消（被取走的回来时过时）、任务 `chat_id` 是群、草稿发到群里（分几条发、读回是机器人的、重启后读回也认）；老板和别人的话只当下一份草稿的背景（打码、最多 10 句、每句 300 字、从我们上次在群里回复的那个任务之后算），不起草不回；系统行不起草、没写老板名字的撤回让草稿改人审；运营在群里亲手打字：接管；门槛、下班时段、自动模式照旧发群里的草稿，开场白一律先审，群里从不回「稍等」；她私聊来消息：同一张合作单、各回各的聊天、互不顶掉、`meta.sent` 带 `where`（没接手群的达人不带）；机器人自己起的（发货通知、开专属佣金、控制台开场白）：她在私聊说过话或后端认得她的私聊 → 私聊，否则群，两样都没有（她退群了）→ 不发、合作单提醒，她再发消息提醒消失；停着时建的发货通知发的时候再定去哪；消息库里存着的接手前的话补交一次（她的话成草稿、不写开场白，同一条不交两次）；老板的 wxid 10 分钟查一次；群发送被后端拒绝（`GROUP_NO_NAME`）：草稿没发出去、合作单提醒；她退群：合作单提醒；「群已经不在了」：`gone`、`unwatch_group`、群里又一律不回、私聊照旧；清理清掉群名、成员名字、留着的话；日志里没有名字；关着时什么都不做。控制台（`tests/test_prm_console_api.py`）：`/api/prm` 的 `boss_groups`、合作单的 `source`、群草稿的 `channel` / `group_name`、「是她」「不管了」只认 POST 和本机页面、`group_none` 让群 `gone`，停着时按 .env 显示、都说不能做；设置（`tests/test_config.py`、`tests/test_web_advanced.py`）；红线（`tests/test_router_business.py`：接手的群里只有她的话成草稿任务，老板 / 管理员 / 别人的 @、cc、命令、进群、图片、语音都不回不建任务，别的群照旧，开关关着全都不回；`tests/test_prm_route_redlines.py`：真的 WCDB 后端读所有群，她在接手的群里的话成群的草稿任务，老板和别人的话、别的群里她的 cc 都不回不建任务） |

### 手动验证清单（加第 11 条）

11. **老板拉的群（`PRM_BOSS_GROUPS`）** — 需要 Windows 真机和老板的手机：设置里填好 `PRM_GROUP_WITH`、打开「接手老板拉的群」、重启机器人，等一分钟（第一次只记下已有的群）。让老板拉一个只有他、一个测试小号和这个号的群：一分钟内合作单上出现这位达人（标「老板介绍」，三方群块写「老板拉的群」），「待审批」里有一份标「群里 · 群名」的开场白，点发送以后群里看得到、老板的手机上也看得到；小号在群里说一句：「待审批」里出现草稿、发在群里；老板在群里说一句：不起草、不回；群里四个人（多一个小号）时，「合作单」页顶上问哪个是她。日志里「PRM: boss group」那几行只有群编号前 8 位、没有名字

---

## 三、和 spec 不一样的地方（都和协调者对过的除外）

- `packet_out` 的 `channel`：私聊仍是 `"wechat"`（不是 spec 写的 `"private"`），百应仍是 `"buyin"`，群是 `"group"`——界面现在按 `"buyin"` 认百应草稿，改名会改变关着时的行为。
- `boss_groups` 表比 spec 多三列：`made_at`（群里最早一行，算 watch 从哪读）、`lines`（老板 / 别人的话，给下一份草稿）、`replied_at`（我们上次在群里回复）；状态多一个 `gone`（协调者要的「群已经不在了」）。
- `members` 存的是除了我们以外的所有成员（老板也在里面）；控制台只给候选，`size` = 成员数 + 1。
- 「我们上次在群里回复」按那份回复的草稿任务建的时间算，不按发出去的时间：她说话之后、我们回复之前老板说的话，下一份草稿还看得到。
- 一个达人同时有私聊和接手的群时，新消息只让同一个聊天里的草稿过时（spec 没说；为了不在老板面前回私聊的话）。
- 群开场白：她在私聊里说过话也照样写（spec：接手以后写群开场白）。协调者的规则 2 管的是机器人自己起的其他消息。
- 「拉三方群」正在建 / 不确定时扫到的新群：记成 `ignored`（`OURS_WHY`），不接手（后端那一部分的第 1 点）。

## 四、待定

- 后端认「她是不是好友」只能用现成的 `chat_target`（微信会话列表里有她的私聊）：好友但从没聊过、会话被删了的，机器人自己起的消息会发到群里。要更准需要后端给一个查好友的方法。
- 她在群里用的群昵称和这个号给她的备注不一样时，群里「"xx" 撤回了一条消息」认不出是谁：只要没写老板的名字，就当成她的（草稿改人审，偏保守）。
- 群开场白和 kb 的「老板拉的群」一节归 worker 那一部分。
