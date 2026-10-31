# DOCS-FIX2-prm.md — 功能 U：老板拉的群，第二次审查 2026-10-31 的修复（PRM 和页面部分，分支 boss-groups-fix2-prm）

协调者合并进 `CODEBASE_REFERENCE.md` 和 `CLAUDE.md` 用，在 `DOCS-FIX-prm.md` / `DOCS-FIX-wui.md` 的基础上改（写「改」的整行替换，
写「加」的是新行，写「删」的去掉）。编号是第二次审查的 F1–F9。后端那一部分同时加 `wechat_now()` 和 `boss_groups` 结果里的
`{"chat": id, "unreadable": True}`；本分支按这些名字写，单元测试用假后端（`tests/test_prm_boss_groups.py::Backend`），端到端测试
（`tests/test_boss_groups_e2e.py`）在本分支上还是没有 `wechat_now` 的后端：按下面「后端没有 `wechat_now`」那条走，合并后端以后
两种情况那几个测试都成立。

---

## 一、给 CODEBASE_REFERENCE.md

### 最后更新那一行（接在功能 U 审查修复那句后面）

……第二次审查 2026-10-31 修复：机器人停在发送中、或者发了没读回的群回复，重启以后读回来不再被当成运营亲手打的（没确认的不设下限，
和私聊一样）；「是不是新拉的群」按微信的钟判断（基线时间是记基线时后端的 `wechat_now()`）；只有微信「邀请你加入了群聊」那一行在
基线以后的群才自动接手，拿不准的放在「老板拉的群」卡片上等运营点「是她」，早于基线一天以上的才不管，还没有消息的群等有了消息再看；
后端读不了的群下次扫描再问，不算「没有老板」；回复发出去去掉它的任务被告诉的最大编号以前留着的所有话；「先试一下」不算她有三方群，
真的「拉三方群」正在建时老板拉的群先等着、建完再看；老板介绍的新好友补读的话也换掉老板的名字；`PRM_GROUP_WITH` 有认不出的名字时是
`/api/prm` 的 `boss_groups.warning`（卡片上提醒，按钮照常能点），`why_not` 只在扫描完全跑不了的时候。

### §1 全局变量/参数清单

| 名称 | 类型 | 默认值 | 定义位置 | 消费位置 / 说明 |
|---|---|---|---|---|
| 改 `NEW_MARGIN_SEC` | `int` | `600` | `src/prm/boss_groups.py` | 微信「…邀请你加入了群聊」那一行（`joined_at`）比基线早不超过这么久：还算「肯定是新拉的群」（扫描可以自己接手） |
| 加 `OLD_SEC` | `int` | `86400` | `src/prm/boss_groups.py` | 群对我们开始（`joined_at`，或者读到的最早一行 `created_at`）比基线早超过这么久：不是新拉的群（`ignored`「不是新拉的群」）；在这之间、又没有基线以后的「邀请你加入了群聊」那一行的：拿不准（`pending`） |
| 加 `NEW` / `MAYBE` / `OLD` / `EMPTY` | `str` | `"new"` / `"maybe"` / `"old"` / `"empty"` | `src/prm/boss_groups.py` | `_judge` 的四种结论：肯定新 / 拿不准 / 旧 / 还没有消息 |
| 改 `BASELINE_AT` | `str` | `"boss_groups_baseline_at"` | `src/prm/boss_groups.py` | `prm_meta`：基线是什么时候记的，**微信的钟**（记基线时后端的 `wechat_now()`：会话列表里最新的会话时间；后端没有它、或者给不出：这台电脑的钟减去这次运行测到的钟差）；群的微信时间只和它比，从不和这台电脑的钟比；老版本留下的基线没有它：下一次扫描按同样的办法记成当时 |
| 加 `MAYBE_WHY` / `NO_LINES_WHY` / `QUIET_WHY` / `MAKING_WHY` | `str` | 「拿不准是不是新拉的群：是的话点「是她」」/「群里还没有消息：有了以后再看是不是新拉的群」/「3 天里群里一直没有消息，不再看这个群」/「她的三方群正在建（合作单上的「拉三方群」）：建完以后再看这个群」 | `src/prm/boss_groups.py` | `boss_groups.why`：`pending`（拿不准；没有候选时是 `waiting`，她进来以后 `pending`）、`waiting`（还没有消息：扫描每次请后端再读它；她的三方群正在建：建完再看）、`ignored`（`BOSS_WAIT_DAYS` 里一直没有消息） |
| 改 `OURS_WHY` | `str` | 「像是合作单上「拉三方群」建的群（不确定那次建没建成），不接手」 | `src/prm/boss_groups.py` | 两个以上候选、有人的「拉三方群」是「不确定」时（以前「正在建」也算：现在是 `waiting`，`MAKING_WHY`） |
| 改 `UNKNOWN_WHY` | `str` | 「PRM_GROUP_WITH 里的{names}在这个微信里找不到 / 有重名」 | `src/prm/boss_groups.py` | 加上「：认出来以前，老板拉的群不会自动接手（找到的群在这里等你点「是她」）」是 `warning`（还有老板认得出时），也是那些群 `pending` 的原因；一个老板都认不出时加上「：要和这个微信号里他的备注或名字一样，而且只有一个人叫这个名字」是 `why_not`（扫描找不了） |

（`src/config.py` / `src/web/advanced_config.py` 没有新设置。）

### §2 函数清单

**`src/prm/boss_groups.py`（模块函数，加 / 改）**

| 函数 | 说明 |
|---|---|
| 改 `boss_groups_out(store, *, enabled, why_not="", boss=(), reserved=(), boss_call="老板", warning="") -> dict` | 多 `warning`（开着时原样给，关着是 ""）：扫描在跑、但有要提醒的（`PRM_GROUP_WITH` 有认不出的名字）；`why_not` 只说为什么完全跑不了（控制台按钮不能点）；`waiting` 也数等第一句话、等她的三方群建完的群 |
| 加 `waits_for_lines(row) -> bool` | 因为还没有消息在等的群（`waiting` 且 `why` 是 `NO_LINES_WHY`）：不放进扫描的 `known`，后端每次扫描再读它的消息（`_found`） |

**`class BossGroups`（加 / 改 / 删）**

| 方法 | 说明 |
|---|---|
| 改 `__init__(…)` | 签名不变；`local` 现在只用来算没有 `wechat_now` 的后端的基线时间（`_wechat_clock`）；多 `_warning`、`_taking` |
| 改 `why_not(self) -> str` | 只说扫描为什么完全跑不了：没填老板、后端不支持、库读不了、不知道自己的 wxid、一个老板都认不出 |
| 加 `warning(self) -> str` | 扫描在跑时要提醒的（"" = 没有）：`PRM_GROUP_WITH` 里有认不出的名字（没人叫、两个人叫）：没有群自动接手，找到的群等「是她」，按钮照常能点；`why_not` 有值时是 "" |
| 改 `status(self) -> dict` | 带上 `warning` |
| 加 `masks(self, party) -> bool` | 她的话交给 Mac 以前要不要 `hide`：有接手的群（`party_has_group`），或者正在被接手（已登记、群还没关联：`_taking`，后端的轮询线程这时可能读到她的新好友补读） |
| 加 `_wechat_clock(self, now) -> float` | 现在在微信的钟上：后端的 `wechat_now()`（会话列表里最新的会话时间）；没有、出错、给不出：这台电脑的钟减去这次运行测到的钟差（`local`，读回我们自己的话以前是 0） |
| 加 `_quoted(names) -> str`（静态） | 「X」、「Y」 |
| 改 `_scan(self, now) -> Optional[int]` | 记基线时 `BASELINE_AT = _wechat_clock(now)`；一个老板都认不出才是 `why_not`（说是哪几个名字），有老板认得出、别的名字认不出：`why_not` 空、`warning` 说是哪个名字；`known` 不含因为还没有消息在等的群；后端结果里不是 dict 的不看 |
| 改 `_note_looked(self, there, known, found) -> None` | `unreadable` 的群也算后端交回了：从不算「看过、没老板」 |
| 删 `_new(self, group) -> bool` | 换成 `_judge` |
| 加 `_judge(self, group) -> str` | 只用微信的时间、和 `BASELINE_AT` 比：`joined_at` 不早于基线前 `NEW_MARGIN_SEC` → `NEW`；`joined_at` 或 `created_at` 早于基线前 `OLD_SEC` → `OLD`；两个都没有、后端说整段历史都读了（`history_complete`）→ `EMPTY`；别的（没有「邀请你加入了群聊」那一行（整段历史是新的也算：删过聊天和记录的老群回来就是这样）、那一行在基线前一天以内、什么都不知道、旧后端）→ `MAYBE` |
| 加 `_sure(self, row) -> bool` | 存着的 `joined_at` 是不是「肯定新」（等着的群再定时用） |
| 加 `_making(self, people) -> bool` | 她们里有人的合作单「拉三方群」正在建（`creating`；「先试一下」不写状态、不算） |
| 改 `_ours(self, people) -> bool` | 只看「不确定」（以前「正在建」也算） |
| 改 `_has_group(self, wxid) -> bool` | 建好了 / 不确定（「建好了」手标的也算）；「正在建」不算（`_making`） |
| 加 `_decide(self, people, sure) -> tuple` | 不旧的群怎么办：`(state, why, 日志的一句)`：有人的三方群正在建 → `waiting` `MAKING_WHY`；一个候选、她有三方群 → `ignored` `HAS_GROUP_WHY`；两个以上、有人的「不确定」→ `ignored` `OURS_WHY`；没有候选 → `waiting`（拿不准的 why 是 `MAYBE_WHY`）；拿不准 → `pending` `MAYBE_WHY`；有认不出的老板名字 → `pending`（`_unknown_why`）；否则 `pending` ""，只有这时一个候选的扫描自己接手 |
| 改 `_found(self, group, own, now) -> int` | `unreadable` 的跳过（不记行、不算看过，下次扫描再问）；已有的行只有「还没有消息」那种再看；`_judge`：`OLD` → `ignored`「不是新拉的群」，`EMPTY` → `waiting` `NO_LINES_WHY`（成员变了就更新），其余 `_decide`；行里记 `joined_at` |
| 加 `_keep(self, chat, row, now, **fields) -> bool` | 新的一行，或者把「还没有消息」那一行更新成现在的结论（发现的时间不变） |
| 改 `_look_again(self, own, now) -> int` | 等着的群：`BOSS_WAIT_DAYS` 以后 `ignored`（一直没消息的 `QUIET_WHY`，别的 `NOBODY_WHY`）；「还没有消息」的不读成员（扫描的 `boss_groups` 会交回来）；别的重读成员、`_decide` 再定（她进来了、她的三方群建完了）：接手、`pending`、`ignored`，或者接着等；这次扫描刚定过的（`updated_at` 是现在）不再读 |
| 改 `take_over(self, chat, wxid, name="", *, actor="system") -> dict` | 签名不变；接手期间把她的 wxid 放进 `_taking`（`masks`），做的事在 `_take_over` |
| 加 `_take_over(self, row, wxid, name, actor) -> dict` | 原来 `take_over` 的内容（`_lock` 里）；扫描接手时在 `ThreeParty.guard` 里：她的合作单「拉三方群」正在建（`creating`，或者 `guard` 给出的正是她的合作单）→ `waiting` `MAKING_WHY`、不关联、之后再看，返回 `{"ok": False, "error": MAKING_WHY}`；建好了 / 不确定 → `ignored` `HAS_GROUP_WHY`（同以前） |

**`src/prm/three_party.py`（`ThreeParty`，改）**

| 方法 | 说明 |
|---|---|
| 改 `guard(self)` | 给出现在正在**建**群的合作单编号，或 None：没有在建的，或者只是「先试一下」（什么都不建，不算她有三方群） |
| 改 `start` / `_run` | 多记 `_dry`（这次是不是「先试一下」），和 `_busy` 一起设、一起清 |

**`src/prm/service.py`（`PrmService`，改）**

| 方法 | 说明 |
|---|---|
| 改 `_sent_part(self, packet, text, tokens, written, *, unconfirmed, group=False) -> bool` | 签名不变；群里的草稿：没确认的（`unconfirmed`：发了没读回 `sent_unconfirmed`、停在发送中 `indeterminate`）不设下限，和私聊一样（`start` 把停在发送中的 `sent_at` 改成重启的时间；这台电脑的钟快、重启后还没测出钟差时，读回的那一行比 `sent_at` 早）；确认过的照旧只认 `sent_at` 前后 120 秒；私聊不变 |
| 改 `_take` | `boss_groups.masks(party)` 代替 `party_has_group`（正在被接手的也换名字） |
| 改 `take_new_friend` | 有接手的群（或者正在被接手）的达人：新好友补读的每一句先 `hide`（老板的名字换成 `PRM_BOSS_CALL`） |

**`src/prm/store.py`（`PrmStore`，改 / 加）**

- 改 `boss_group_replied(self, chat, seqs=None, now=None) -> int`：去掉编号不大于 `seqs` 里最大那个的所有话（这份任务没被告诉的更早的话也去掉：它们在我们的回复以前）；没有编号的（旧版本留下的）算 0，下一次回复发出去就去掉；`seqs` 是空列表：只去掉没有编号的；`None`（运营在群里亲手打字）照旧全部。
- 改 `add_boss_group(self, chat, *, name, found_at, state, members, boss, why="", made_at=None, joined_at=None, now=None) -> bool`：多 `joined_at`。
- 加 模块函数 `_seq(line) -> int`：留着的一句的编号，没有或者坏了是 0。
- `BOSS_GROUP_FIELDS` 加 `joined_at`；`_ADDED_COLUMNS` 加 `("boss_groups", "joined_at", "REAL")`（老库补上）。

**页面**

| 文件 | 说明 |
|---|---|
| 改 `ui/src/components/BossGroupsCard.jsx` `BossGroupsCard` / `BossGroupRow({ g, warning, disabled, onPick, onIgnore })` | `warning` 是卡片上的一条黄色提醒（在说明上面），「是她」「这个群不是达人」照常能点；只有 `why_not` 让按钮都不能点；某个群的 `why` 就是卡片的 `warning` 时那一块不再写一遍；说明那一句改成「群里除了老板和我们只有她一个人、而且看得出是新拉的群时，机器人自己接手：…」 |
| 改 `ui/src/lib/prm.js` `bossGroupsLine(bg)` | 等着的群那一段改成「N 个群在等她进群或者等群里有消息（机器人每分钟再看一次）」（以前「…她进来以后机器人自己接手」：拿不准的群她进来以后是等人选） |

### §3 模块间关系图（改老板拉的群那几行）

```
PrmService.tick → boss_groups.sweep → _openings → scan（到点）
    → group_ids（第一次：基线 + BASELINE_AT = _wechat_clock(now)：后端 wechat_now()，没有就这台电脑的钟 − 钟差）
    → own_wxid / _read_bosses → contact_ids（一个都认不出：why_not；有认不出的：warning）
    → group_ids（现在的群）→ backend.boss_groups(老板 wxid, known = 基线 ∪ 已有的行（还没有消息的除外）∪ 合作单的三方群 ∪ 看过两次的群, …)
    → _note_looked（unreadable 算交回了）
    → _found（unreadable 跳过）→ _judge：OLD → ignored / EMPTY → waiting（NO_LINES_WHY）/ NEW、MAYBE → _decide
         → pending ""、一个候选 → take_over；pending MAYBE_WHY / 认不出名字 → 等「是她」；waiting MAKING_WHY；ignored
    → _look_again（等着的群：还没有消息的靠上面的 boss_groups；别的 group_members → _decide(_sure(row))）
take_over → _taking（masks）→ _take_over → register → ThreeParty.guard（正在建 → waiting MAKING_WHY；建好 / 不确定 → ignored）
    → _link → watch_group → _replay → opening_due
take_new_friend（新好友补读）→ boss_groups.masks → hide
on_own_message → _match_sent → _sent_part(group)：没确认的不设下限，确认过的 sent_at 前后 120 秒
_deliver（群）→ boss_groups.replied(群, group_told) → 去掉编号 ≤ max(group_told) 的（没有编号的算 0）
```

### §4 功能U 那一节（改的地方）

```
新不新（_judge，第二次审查 F2 / F4 / F5）：只用微信的时间，和 BASELINE_AT（微信的钟）比
  joined_at（微信「…邀请你加入了群聊」那一行）不早于基线前 10 分钟 → 肯定新：一个候选、名字都认得出、没有三方群挡着 → 自动接手
  joined_at 或 created_at 早于基线前一天 → ignored「不是新拉的群」
  一行都没有（history_complete、两个时间都是 None）→ waiting「群里还没有消息…」：不放进 known，后端每次扫描再读，3 天还没有 → ignored
  别的（没有那一行、那一行在基线前一天以内、时间不知道、旧后端）→ pending「拿不准是不是新拉的群：是的话点「是她」」（没有候选时 waiting，
    她进来以后 pending），从不自动接手、也不悄悄不管
后端读不了的群（{"chat", "unreadable": True}）：不记、不算看过，下次扫描再问（F6）
PRM_GROUP_WITH 有认不出的名字（还有认得出的）：照样找群，一律不自动接手；/api/prm boss_groups.warning 说是哪个名字，卡片按钮照常能点；
  一个都认不出：why_not（找不了）（F3）
「拉三方群」正在建（合作单 creating，或者 ThreeParty.guard 给出她的合作单；「先试一下」不算）：老板拉的群 waiting「她的三方群正在建…」，
  每次扫描再看，建完以后：没建成 → 照常判断（可能接手）；建好 / 不确定 → ignored「她已经有三方群」（F8）
回复发出去：去掉编号不大于它的任务被告诉的最大编号的所有话（F7；没有编号的算 0）
有接手的群（或者正在被接手）的达人：新好友补读的话也换掉老板的名字（F9）
重启后：停在发送中 / 发了没读回的群回复读回来，不设下限，和私聊一样（F1）
```

数据（`src/prm/store.py`）：`boss_groups` 表加一列（老库由 `_ADDED_COLUMNS` 补上）：

```sql
CREATE TABLE IF NOT EXISTS boss_groups (
    ...（同 DOCS-FIX-prm.md）
    opening_due REAL,
    joined_at REAL                        -- 微信「邀请你加入了群聊」那一行的时间（微信的钟）；没有是 NULL：等着的群再定时用（_sure）
);
```

控制台接口：

```
GET /api/prm → boss_groups 加 "warning": str（"" = 没有；运行中有认不出的 PRM_GROUP_WITH 名字时是那句话；停着、关着都是 ""）
  "why_not" 只在扫描完全跑不了的时候（控制台按钮不能点）
  pending 的 why 可能是「拿不准是不是新拉的群：是的话点「是她」」；waiting 也数「群里还没有消息」「她的三方群正在建」的群
```

后端接口（后端那一部分加，PRM 这样用）：

```
wechat_now() -> Optional[float]：会话列表里最新的会话时间（微信的钟）；只在记基线时调一次；没有这个方法、出错、给 None：这台电脑的钟 − 钟差
boss_groups(...) 的结果里 {"chat": id, "unreadable": True}：跳过，不算看过、不算没老板、不算不管，下次扫描再问
```

### §5 配置文件和环境变量

没有新设置。

---

## 二、给 CLAUDE.md

### 测试表「老板拉的群」那一行，PRM 这一侧（接在「审查 2026-10-31 的修复」那段后面）

第二次审查 2026-10-31 的修复（`tests/test_prm_boss_groups.py`）：群里分两条发的回复第一条发出去、机器人停了、没读回，过 1 分钟 / 5 分钟重启以后补读交来：是机器人的（不接管、草稿还是不确定，第二条没发），另一行同样的字是人打的；发了没读回、这台电脑比微信快 10 分钟、重启后读回来：群里和私聊都是机器人的（私聊和以前一样）；基线时间是后端的 `wechat_now()`（微信的钟）：这台电脑快 3 小时，基线后一分钟老板拉的群照样接手，一个月前（微信的钟）的群照样不管；后端没有 `wechat_now`：基线是这台电脑的钟减去测到的钟差（测到了照样接手，没测到不再悄悄不管，而是等「是她」）；只有微信「邀请你加入了群聊」那一行在基线以后（前 10 分钟以内）的群自动接手（那一行之前的消息再早也不要紧），一个月前被拉进来、整段历史一个月前开始、读到两天前的一行的群不管（「不是新拉的群」），整段历史是新的但没有那一行（删过聊天和记录的老群回来）、只读到最近几行、什么都不知道、那一行在基线前一天以内、旧后端（只给 `created_at`）都放在卡片上等「是她」（「拿不准是不是新拉的群：是的话点「是她」」），下一次扫描不再问，「是她」接手、「这个群不是达人」不管；还没有消息的群等着（算进「在等」的数、每次扫描请后端再读），有了「邀请你加入了群聊」那一行就接手，读到的是没有那一行的消息就等「是她」、是一个月前的就不管，3 天一直没有消息就不管；后端读不了（`unreadable`）的群不记、不算看过，能读了就接手；`PRM_GROUP_WITH` 第二个名字认不出：`why_not` 空、`warning` 说是哪个名字（不带老板的名字），只有她一个候选的群也等「是她」、点了就接手，名字改对以后 `warning` 没了，一个老板都认不出时才是 `why_not`；12 句老板的话、任务只被告诉最新 10 句，回复发出去以后 12 句都去掉，她下一份任务里没有；旧版本留下的没有编号的话下一次回复发出去就去掉；「先试一下」正在跑时扫描照样接手、试完不改；「拉三方群」正在建时老板拉的群等着（「她的三方群正在建…」，算进「在等」），建完以后没建成就接手、不确定或者建好了就「她已经有三方群」（两个候选、有人的正在建也等着，不确定的照旧不管），接手前在 `ThreeParty.guard` 里再看：建好了不管、正在建（状态写上了或者还没写上）等着；老板介绍的新好友补读的话里老板的名字换成「老板」（群接手以后、接手到一半时轮询线程先读到的也一样），没有老板拉的群的达人照旧。真的后端（`tests/test_boss_groups_e2e.py`）：删过聊天和记录的一个月前的群又有人说话：等「是她」、不自己接手不读，点「是她」以后接手、开始读；扫描在「拉三方群」建群中间跑：那个群先等着、建好以后「她已经有三方群」。控制台（`tests/test_prm_console_api.py`）：`boss_groups.warning`（运行中有认不出的名字时那句话、`why_not` 空、「是她」照样能用；停着、关着是 ""）。红线测试（`tests/test_router_business.py`、`tests/test_prm_route_redlines.py`）的假群带上「邀请你加入了群聊」那一行的时间。

### 测试表「待审批 / 合作单页」那一行，老板拉的群卡片那段（`tests/test_functional.py::TestPrmPages`）

「`why_not` 时一条提醒、按钮都不能点」后面加：「；`warning`（PRM_GROUP_WITH 有认不出的名字）时卡片上一条黄色提醒，「是她」「这个群不是达人」照常能点、点了照样发，原因就是这句话的群不再重复写，有自己原因（「拿不准」）的照写，接手以后提醒还在；「待审批」页说「要你看一眼」」；那一行数字里等着的群写「N 个群在等她进群或者等群里有消息（机器人每分钟再看一次）」。

### 手动验证清单第 11 条（老板拉的群）后面加一句

第二次审查以后：老板新拉的群，日志里应先有「PRM: boss group … found」，几秒内接手；如果卡片上出现「拿不准是不是新拉的群」，说明机器人没在群里读到「…邀请你加入了群聊」那一行（把「PRM: boss group」那几行发回来，只有编号前 8 位）。把 `PRM_GROUP_WITH` 改成带一个不存在的名字、重启：卡片上是黄色提醒、按钮能点，老板新拉的群等你点「是她」。

---

## 三、和审查不一样的地方、已知限制、待定

- **F1**：用的是决定里的第一种办法：没确认的群草稿（`sent_unconfirmed`、`indeterminate`）不设下限，和私聊一样；确认过的照旧 `sent_at` 前后 120 秒（它们的 `sent_at` 从不被 `start` 改）。已知限制：这台电脑的钟和微信差很多、重启后还没测出钟差时，**已经确认过**的群回复如果被后端又交一次（正常不会：重启从最后一行的下一秒读），仍可能当成运营亲手打的。
- **F2**：`wechat_now()` 是会话列表里最新的会话时间，比真正的「现在」早（号很久没动静时早很多）。新拉的群一定比它晚，所以不会漏；理论上，在那段没动静的时间里被拉进、又在开开关以前删掉聊天的老群会被当成新的（几乎不会有）。后端没有 `wechat_now`、出错或者给 None：这台电脑的钟减去这次运行测到的钟差（读回我们自己的话以前是 0）。老版本（8981db5）记的 `BASELINE_AT` 是这台电脑的钟；正式环境没跑过这个功能，没有转换。
- **F4**：「旧后端」（结果里没有 `joined_at` / `history_complete`）从不自动接手，找到的群都等「是她」（以前只看 `created_at`，没有也算新）。端到端测试用的真后端已经给 `joined_at`。
- **F5**：还没有消息的群存成 `waiting`（`NO_LINES_WHY`），靠扫描的 `boss_groups` 再读（后端对一行都没有的群不缓存历史）；后端不再交回它（比如老板退了群）时，等 3 天以后 `ignored`（`QUIET_WHY`）。
- **F8**：「不确定」照旧算她有三方群（`ignored`）；只有「正在建」改成等着。机器人在建群中间停了：重启时变成「不确定」，等着的那个群下次扫描就是「她已经有三方群」。运营在「正在建」时点「是她」照旧关联、保留原来的三方群（和以前一样）。
- **F9**：只换 `PRM_GROUP_WITH` 里的名字。**已知限制**：她或运营用别的叫法说老板（「王总」「老王」、老板的微信昵称和 `PRM_GROUP_WITH` 不一样）时，这些叫法会交给 Mac。除了审查要的补读，接手到一半（已登记、群还没关联）时轮询线程先读到的补读、私聊消息也换（`masks` / `_taking`）。
- **F3**：一个老板都认不出时还是 `why_not`（扫描什么都找不到），话改成「…：要和这个微信号里他的备注或名字一样，而且只有一个人叫这个名字」；只有部分认不出时才是 `warning`。卡片上原因就是 `warning` 的群那一块不再重复写。
- **页面**：「在等」那一段的话改了（`bossGroupsLine`），`tests/test_functional.py` 里对应的一处期望跟着改。
- **审查的复现**：n1、n1c、n4、n6、n7 原样通过；n3 期望 `ignored`，按决定改成 `pending`（拿不准）；n5 原样（后端什么都不交回）PRM 这边分不出来，靠后端的 `unreadable`，移植的测试用 `unreadable`；n2 原样的假后端没有微信的钟，移植的测试给假后端一个比这台电脑慢 3 小时的 `wechat_now`（原样跑时现在是 `pending`，不再悄悄不管）。
