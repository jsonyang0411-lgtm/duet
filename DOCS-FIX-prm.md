# DOCS-FIX-prm.md — 功能 U：老板拉的群，审查 2026-10-31 的修复（PRM 部分，分支 boss-groups-fix-prm）

协调者合并进 `CODEBASE_REFERENCE.md` 和 `CLAUDE.md` 用，在 `DOCS-prm.md` 的基础上改（下面写「改」的整行替换，写「加」的是新行）。
编号是审查的编号（1–12）。后端那一部分同时加 `has_private_chat(wxid)`、`boss_groups` 每项的 `history_complete` / `joined_at`、
`watch_caught_up(chat)`；本分支按这些名字写，单元测试用假后端（`tests/test_prm_boss_groups.py::Backend`），端到端测试
（`tests/test_boss_groups_e2e.py`）还是旧后端：没有这些方法时按下面「旧后端」那几条走。

---

## 一、给 CODEBASE_REFERENCE.md

### 最后更新那一行（接在功能 U 那句后面）

……审查 2026-10-31 修复：重启后我们自己在群里的话不再被当成运营亲手打的（自己的行也记成最后处理的一行、重启从下一秒读、
群里发过的草稿读回再认一次）；`PRM_GROUP_WITH` 有认不出的名字时不自动接手、说清是哪个名字；她已经有三方群（建好、正在建、
不确定、手标「建好了」）的群不接手；不是新拉的群（开始得比基线早）不接手；开场白等后端补读完群里已有的话再写；老板的名字不出
这台电脑（`PRM_BOSS_CALL`，默认「老板」）；关掉开关时没发出去的群草稿不再发。

### §1 全局变量/参数清单

| 名称 | 类型 | 默认值 | 定义位置 | 消费位置 / 说明 |
|---|---|---|---|---|
| 改 `BOSS_SCAN_SEC` / `BOSS_WAIT_DAYS` / `BOSS_WATCH_BACK_SEC` / `BOSS_IDS_SEC` | `int` | `60` / `3` / `300` / `3600` | `src/prm/boss_groups.py` | 扫描间隔；只有老板和我们的群等她进群的天数；接手时从发现前多久开始读群；老板 wxid（`contact_ids` 要读每个群的成员）都认出时多久重新查一次（原来 600） |
| 加 `BOSS_IDS_RETRY_SEC` | `int` | `600` | `src/prm/boss_groups.py` | `PRM_GROUP_WITH` 里有认不出的名字时，多久再查一次（可能已经改好了）；查失败（出错、库读不了）不记，下一次扫描就再查 |
| 加 `OPENING_WAIT_SEC` | `int` | `90` | `src/prm/boss_groups.py` | 后端说不出补读做完没有（没有 `watch_caught_up`，或者不能 `watch_group`）时，接手以后多久才写群开场白 |
| 加 `NEW_MARGIN_SEC` | `int` | `600` | `src/prm/boss_groups.py` | 群对我们开始（我们被拉进来，或者整段历史开始）比基线早不超过这么久，还算新拉的群 |
| 加 `KNOWS_SEC` | `int` | `60` | `src/prm/boss_groups.py` | `knows_private`：后端对一位达人「有没有私聊」的回答记多久（发送线程每 5 秒问一次欠着的开专属佣金） |
| 加 `BASELINE_AT` | `str` | `"boss_groups_baseline_at"` | `src/prm/boss_groups.py` | `prm_meta`：基线是什么时候记的（这台电脑的钟）；老版本留下的基线没有它：下一次扫描记成当时 |
| 加 `CALL_DEFAULT` / `CALL_MAX` | `str` / `int` | `"老板"` / `10` | `src/prm/boss_groups.py` | `PRM_BOSS_CALL` 的默认值和最多几个字（`boss_call`） |
| 加 `HAS_GROUP_WHY` / `NOT_NEW_WHY` / `UNKNOWN_WHY` / `NO_READ_WHY` | `str` | 「她已经有三方群」/「不是新拉的群」/「PRM_GROUP_WITH 里的{names}在这个微信里找不到 / 有重名」/「微信数据库现在读不了，认不出老板，稍后自动再找」 | `src/prm/boss_groups.py` | `boss_groups.why`（前两个是 `ignored` 的原因；第三个加上「：认出来以前，老板拉的群不会自动接手（找到的群在这里等你点「是她」）」是 `pending` 的原因，也是 `why_not`）；第四个是查老板失败时的 `why_not` |
| 加 `BOSS_GROUPS_OFF_TEXT` | `str` | 「老板拉的群关了，没发」 | `src/prm/service.py` | 开关关着时，以前开着时写的群草稿（在等、已批准、之后才写好的）不发，标成没发出去，原因是这句 |
| 加 `PRM_BOSS_CALL` | `str` 1–10 字 | `"老板"` | `src/config.py` | `BossGroups._call`：交给 Mac 的群草稿任务里怎么叫老板（`meta.group.boss`，群名和群里的话里老板的名字也换成它）；空着是「老板」，空白折叠 |

### §2 函数清单

**`src/prm/boss_groups.py`（模块函数，加 / 改）**

| 函数 | 说明 |
|---|---|
| 加 `boss_call(raw) -> str` | `PRM_BOSS_CALL` 的用法：空白折叠、最多 10 字、空着是「老板」（运行中和停着的控制台都用） |
| 加 `_key(name) -> str` | 名字按后端同名规则比（`_name_key`：去空格、不分大小写） |
| 改 `candidates(members: list, bosses, reserved=(), names=()) -> list[dict]` | 可能是她的人：去掉老板（wxid）、**名字和老板（`names` = PRM_GROUP_WITH）一样的人**（老板的 wxid 认不出时就是他）、管理员和 cc 用户、群编号 |
| 加 `told_lines(group) -> list[int]` | 一份任务的 `meta.group` 告诉了 Mac 哪几句（它们的 `seq`） |
| 改 `boss_groups_out(store, *, enabled, why_not="", boss=(), reserved=(), boss_call="老板") -> dict` | 多 `boss_call`；`pending` 的成员不含和老板同名的人 |

**`class BossGroups`（加 / 改）**

| 方法 | 说明 |
|---|---|
| 改 `__init__(self, store, config=None, *, clock=time.time, reserved=(), register=None, opening=None, handle=None, local=None, guard=None)` | `local` = `PrmService._local`（微信的时间换成这台电脑的钟，判断新群用）；`guard` = `ThreeParty.guard`（「拉三方群」开始建群用的那把锁）；开着时从库里读回还没写开场白的群（`opening_due`） |
| 加 `call`（属性） | `PRM_BOSS_CALL` |
| 改 `knows_private(self, wxid) -> bool` | 只问后端的 `has_private_chat`（不写日志），每位达人的回答记 60 秒；后端没有这个方法：当成不知道（False）；**从不调 `chat_target`**（它对叫不出名字的聊天写一条带完整 wxid 的 ERROR） |
| 改 `who(self, row, sender, name="") -> str` | 不是她的那句：老板的 wxid，或者名字和老板一样（这条消息给的发送人名字、群成员里的名字）→「老板」，否则「群成员」 |
| 加 `hide(self, text) -> str` | 把老板的名字（PRM_GROUP_WITH，不分空格大小写）换成 `PRM_BOSS_CALL`：交给 Mac 的东西里从来没有老板的名字 |
| 改 `add_context(self, chat, who, text, at) -> Optional[int]` | 留一句老板 / 别人的话，返回它的编号（`seq`） |
| 改 `job_group(self, chat) -> dict` | `{"name": 群名（老板名字换成称呼）, "boss": [PRM_BOSS_CALL], "lines": [{"who", "at", "text"（老板名字换成称呼）, "seq"}]}`：留着、还没回过的话里最新的 10 句（不再按时间截） |
| 改 `replied(self, chat, seqs=None) -> int` | 回过了：去掉编号是 `seqs` 的那几句（发出去的草稿的任务被告诉的那几句）；`None`（运营在群里亲手打字）：去掉现在留着的全部；之后才留下的话（不管什么时候写的）留着 |
| 改 `sweep(self) -> Optional[int]` | 发送线程每一轮：先看有没有该写的群开场白（`_openings`），再到点就扫 |
| 改 `scan` / `_scan` | 第一次记基线时也记 `BASELINE_AT`；每次扫描重读 `PRM_GROUP_WITH`（`_names`）；查老板（`_read_bosses`）；有认不出的名字：继续扫但不自动接手，`why_not` 说是哪个名字；`known` 里加上看过两次的群；最后看一遍该写的开场白 |
| 加 `_names(self) -> list[str]` / `_unknown_why(self) -> str` / `_readable(self) -> bool` / `_group_ids(self) -> Optional[set]` | 现在的 PRM_GROUP_WITH；认不出名字时的说法；`db_readable()`；会话列表里现在的群 |
| 加 `_read_bosses(self, names, now) -> None` | 老板的 wxid：都认出时一小时查一次、有认不出的 10 分钟、PRM_GROUP_WITH 变了或者扫描要求（`_ask_again`）马上查；出错或库读不了不记（下次扫描再查，日志只写一次）；查了以后看过的群清空（谁是老板可能变了） |
| 加 `_ask_again(self, members) -> None` | 成员里有和老板同名、但 wxid 不是已知老板的人：下次扫描重查老板（每个这样的人一次） |
| 加 `_note_looked(self, there, known, found) -> None` | 会话列表里后端看过、一个都没交回的群（没有老板，或者人太多）：看过两次以后不再让后端读它的成员，直到下次重查老板 |
| 加 `_new(self, group) -> bool` | 新拉的群：`joined_at`（微信「…邀请你加入了群聊」那行），或者整段历史都读到了（`history_complete`）时的 `created_at`，不早于基线前 10 分钟；后端两个都不给（旧后端）：只看 `created_at`（没有也算） |
| 加 `_group_state(self, wxid) -> str` / `_has_group(self, wxid) -> bool` | 她的合作单三方群的状态；有建好 / 正在建 / 不确定的三方群（「建好了」手标、没有群编号的也算） |
| 改 `_found(self, group, own, now) -> int` | 不是新拉的群：`ignored`（「不是新拉的群」）；只有一个候选、她已经有三方群：`ignored`（「她已经有三方群」）；两个以上候选、有人的三方群正在建 / 不确定：`ignored`（`OURS_WHY`）；有认不出的老板名字：`pending`，原因说是哪个名字；否则照旧 |
| 改 `_look_again(self, own, now) -> int` | 进来一个人时同样的三条（她有三方群：`ignored`；认不出老板名字：`pending`） |
| 改 `take_over(self, chat, wxid, name="", *, actor="system") -> dict` | 关联之前在 `ThreeParty.guard` 锁里再读一遍她的合作单：扫描接手时她的三方群建好 / 正在建 / 不确定，或者「拉三方群」正在给她建（`busy`）→ `ignored`（「她已经有三方群」）、不关联；关联以后记 `opening_due`，开场白等补读（`_openings`），不再马上写 |
| 改 `_link(self, deal, row, now) -> bool` | 合作单已有建好 / 正在建 / **不确定**的三方群时保留原来的（只有运营点「是她」会走到这里） |
| 改 `_since(self, row) -> float` | 有最后处理的一行时从**下一秒**读（`last_line_at + 1`；我们自己的行也算处理过） |
| 加 `_openings(self, now) -> int` / `_ask_opening(self, chat, now) -> int` | 接手的群的开场白：后端说补读做完了（`watch_caught_up`）才写，老板的介绍就在任务里；后端说不出：接手 90 秒以后；每个群只问一次（`opening_due` 清掉），重启以后接着等 |
| 改 `gone(self, deal, actor="console") -> bool` | 也清掉还没写的开场白 |

**`src/prm/service.py`（`PrmService`，加 / 改）**

| 方法 | 说明 |
|---|---|
| 加 模块函数 `is_group_packet(packet) -> bool` | 发到老板拉的群的草稿（`meta.channel` 是 group，或者聊天是群） |
| 加 模块函数 `_wrote(deal, chat_id, at) -> dict` | 她的一句话以后 `update_deal` 的字段：`last_message_at`（哪个聊天都算）和 `last_by_chat[chat_id]`（这个聊天自己的） |
| 加 `_redraft_group(self, party, chat_id, row, now) -> None` | 老板 / 别人在群里说话时她在这个群的草稿还在「待审批」（没批准、没发）：那份过时，她的话带上群里现在的话重新起草（`_submit` 不带新的一句）；在等的是群开场白：开场白重写（`_group_opening`）；私聊的草稿、开专属佣金那一步、已批准的、人工接管时都不动 |
| 改 `_submit(self, party, chat_id, line: Optional[str], files, at, age, late, now, new_friend=None, group=None)` | `line` 是 None：重新起草（她没有新的一句，不改 `last_message_at`、不去掉「发不了消息」的提醒）；**三方群的 meta 变量改名 `made`**（原来把参数 `group` 盖掉了：开关关着时她私聊的话也会取消控制台的开场白、另起一份，审查 6）；记 `last_by_chat` |
| 改 `_take` | 她在群里的话（和有接手群的达人的私聊话）先 `hide` |
| 改 `_recorded_for_a_person(self, party, line, at, now, chat_id="") -> bool` / `_missed(self, party, deal, line, at, now, chat_id="")` | 多 `chat_id`（记 `last_by_chat`） |
| 改 `on_own_message` | 接手的群里我们自己的每一行也 `note_line`（重启以后不会再交来） |
| 改 `_own_line` | 运营在群里（或有接手群的达人的私聊里）亲手打的话先 `hide` 再记；在群里打字：留着的话全部算回过了（`replied(chat)`） |
| 改 `_match_sent` / `_sent_part(self, packet, text, tokens, written, *, unconfirmed, group=False) -> bool` | `group`（接手的群）：这次运行以前发到这个群的草稿，确认过的也算，一份草稿（分几条发的每一条）只抵一次，写的时间在 `sent_at` 前后 120 秒（`PART_LATE_SEC`）以内；之后同样的字、另一行同样的字都是人打的 |
| 改 `_outbound` | 有接手群的达人：`meta.sent` 里的字也 `hide` |
| 改 `on_result` | 群草稿的 meta：`group_name` 用微信给的群名（控制台看的，不换称呼）、`group_told`（这份任务被告诉的那几句的编号） |
| 改 `_deliver` | 群草稿发出去以后 `replied(群, group_told)`：只去掉这几句（原来按任务建的时间截，之后才交来的早先的话也被去掉了，审查 5） |
| 改 `send_holding_replies` | 有接手群的达人：一轮「稍等」只看草稿那个聊天自己的话（`last_by_chat`）：她在群里说话不让私聊再回一次「稍等」（审查 7）；老合作单没有 `last_by_chat` 时照旧 |
| 改 `_group_opening` | 她已经在群里说过话（这个群有她的草稿任务）：不写开场白 |
| 改 `start` / `send_approved` | 开关关着：启动时在等 / 已批准的群草稿标成没发出去（`BOSS_GROUPS_OFF_TEXT`）；发送线程遇到群草稿也一样，不发（审查 12） |

**`src/prm/store.py`（`PrmStore`，改）**

- `add_boss_group_line(self, chat, line, keep) -> Optional[int]`：每句带编号 `seq`（`line_seq` 递增，从不重复），按写的时间排、留最新的 `keep` 句；不再按 `replied_at` 去掉。返回编号。
- `boss_group_replied(self, chat, seqs=None, now=None) -> int`：去掉编号是 `seqs` 的几句；`None` 去掉全部；记 `replied_at`；返回去掉了几句。
- `BOSS_GROUP_FIELDS` 加 `opening_due`；`update_deal` 多 `last_by_chat`（JSON）；`deal_view` 读 `last_by_chat`；`_ADDED_COLUMNS` 加三列（老库补上）。

**`src/prm/three_party.py`（`ThreeParty`，加 / 改）**

- 加 `guard(self)`（上下文管理器）：拿着「拉三方群」开始建群用的锁，给出现在正在建（或在试）的合作单编号或 None。`BossGroups.take_over` 在这把锁里再看她的三方群、关联老板拉的群。
- 改 `start`：拿到锁以后再读一遍合作单（老板拉的群可能刚关联成她的三方群）：建好了 / 不确定就不建。

**其他**

- `src/config.py`：`BotConfig.prm_boss_call: str = "老板"`；`_validate_config`：「PRM_BOSS_CALL must be 1 to 10 characters」；`load_config`：`PRM_BOSS_CALL`（空白折叠，空着是「老板」）。
- `src/web/advanced_config.py`：PRM 组 `PRM_BOSS_GROUPS` 后面加一项 `PRM_BOSS_CALL`（text，默认「老板」，「跟 Mac 怎么称呼老板」，要 `PRM_BOSS_GROUPS` 开着）；PRM 组最后三项是 `PRM_BOSS_GROUPS`、`PRM_BOSS_CALL`、`PRM_BOSS_GROUP_MAX`。
- `src/web/server.py` `_boss_groups_stopped_status`：停着时 `boss_call` 按 .env。

### §3 模块间关系图（改 PRM 那一块的老板拉的群几行）

```
PrmService.tick → boss_groups.sweep → _openings（每一轮：watch_caught_up / 90 秒）→ PrmService._group_opening
                                    → scan（到点）→ group_ids（第一次：基线 + BASELINE_AT）/ own_wxid / _read_bosses → contact_ids（一小时一次）
    → group_ids（现在的群）→ backend.boss_groups(老板 wxid, known = 基线 ∪ 已有的行 ∪ 合作单的三方群 ∪ 看过两次的群, PRM_BOSS_GROUP_MAX)
    → _note_looked → _found（_new / _has_group / _ours / 认不出的名字）→ take_over（ThreeParty.guard 里再看一次 → _link）
take_over → register → _link → watch_group(群, last_line_at + 1 或发现前 5 分钟) → _replay → opening_due（开场白等补读）
路由 prm 步骤 → handle → _group_message → 她的话 _take（hide）/ 别人的话 add_context → _redraft_group / 系统行
backend 自己的行 → on_own_message → note_line（我们的行也算）→ _own_line → _match_sent（_sent_part group）/ 接管（hide，replied 全部）
_deliver（群）→ boss_groups.replied(群, packet.meta.group_told)
```

### §4 功能U 那一节（改的地方）

```
扫描：
  有 PRM_GROUP_WITH 里认不出的名字（contact_ids 给 None：没人叫这个名字，或者两个人叫）→ why_not「PRM_GROUP_WITH 里的「X」在这个微信里找不到 / 有重名…」，
    照样找群，但一律不自动接手：找到的群 pending（原因就是这句），运营点「是她」
  和老板同名的成员从来不算候选（老板的 wxid 认不出时就是他），看到这样的人下次扫描重查老板（每人一次）
  不是新拉的群（joined_at，或者整段历史都读到时的 created_at，早于基线前 10 分钟；后端说不清）→ ignored「不是新拉的群」
  只有一个候选、她的合作单有建好 / 正在建 / 不确定的三方群（「建好了」手标的也算）→ ignored「她已经有三方群」
    接手（扫描的）在 ThreeParty.guard 锁里关联前再看一次（「拉三方群」正在给她建也算）
  会话列表里后端看过两次、没交回的群（没老板、人太多）不再读成员，直到下次重查老板（一小时，或 PRM_GROUP_WITH 变了）
  contact_ids 出错 / 库读不了：不记，下一次扫描就再查
接手以后：开场白等后端补读完群里已有的话（watch_caught_up；后端说不出：90 秒），每一轮都看，重启以后接着等
重启：watch_group 从最后处理的一行（我们的行也算）的下一秒读；就算又交来，群里发过的草稿（确认过的也算）每份 / 每条抵一次、前后 120 秒内
留着的老板 / 别人的话：每句一个编号；回复发出去只去掉它的任务被告诉的那几句；运营在群里打字去掉全部；她的群草稿在等的时候老板 / 别人说话 → 重新起草
老板的名字不出这台电脑：任务的 meta.group.boss = [PRM_BOSS_CALL]，群名、群里的话、她在群里的话、运营在群里的话、告诉 Mac 的我方回复里老板的名字都换成 PRM_BOSS_CALL
「稍等」按聊天算轮：她在群里说话不让私聊再回一次「稍等」
开关关着：以前留下的群草稿不发，标成没发出去「老板拉的群关了，没发」
```

数据（`src/prm/store.py`）：`boss_groups` 表加两列，`deals` 加一列（老库由 `_ADDED_COLUMNS` 补上）：

```sql
CREATE TABLE IF NOT EXISTS boss_groups (
    ...（同 DOCS-prm.md）
    updated_at REAL NOT NULL,
    line_seq INTEGER NOT NULL DEFAULT 0,  -- 给留着的话编号用到哪了（lines[].seq，从不重复）
    opening_due REAL                      -- 接手的时间，开场白还没写时（等补读）；写了 / 不写了是 NULL
);
-- deals 加 last_by_chat TEXT NOT NULL DEFAULT '{}'：她在每个聊天最后一句话的时间（微信的时间），「稍等」按聊天算轮
```

草稿任务 / 草稿的 meta（交给 Mac worker 的改动，worker 那一部分要知道）：

```
meta.group = {"name": 群名（老板的名字换成 PRM_BOSS_CALL）, "boss": [PRM_BOSS_CALL],
              "lines": [{"who": "老板"|"群成员", "at": "MM-DD HH:MM", "text": …（老板的名字换成称呼）, "seq": 编号}]}
   lines = 留着、还没回过的话里最新 10 句（以前是「我们上次回复以后的」）；seq 是 PRM 自己记账用的，不进提示
她在群里的话（任务的 text）、有接手群的达人的私聊话、运营亲手打的话（meta.history）、meta.sent 的字：老板的名字换成称呼
群开场白任务：接手以后、后端补读完群里已有的话才建（原来接手时马上建）
草稿 meta 加 group_told（[编号]，PRM 自己用）；group_name 是微信给的群名（控制台看的，不换称呼）
```

控制台接口：

```
GET /api/prm → boss_groups 加 "boss_call": PRM_BOSS_CALL（运行中；停着按 .env）
pending 的 why 可能是「PRM_GROUP_WITH 里的「X」在这个微信里找不到 / 有重名：认出来以前…」；ignored 的原因加「她已经有三方群」「不是新拉的群」（控制台不列 ignored）
```

### §5 配置文件和环境变量（PRM 那一段再加一行）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PRM_BOSS_CALL` | `老板` | 1–10 字；交给 Mac 起草时怎么称呼老板（老板的名字不出这台电脑）；改完重启机器人 |

---

## 二、给 CLAUDE.md

### 测试表「老板拉的群」那一行，PRM 这一侧的描述后面加

审查 2026-10-31 的修复（`tests/test_prm_boss_groups.py`）：我们自己在群里的话也记成最后处理的一行、重启从下一秒读，确认过的群回复重启后又交来还是机器人的（一条发的、分几条发的每一条），每份 / 每条只抵一次，另一行同样的字、晚 120 秒以上同样的字照样接管；`PRM_GROUP_WITH` 有认不出的名字时 `why_not` 说是哪个、找到的群（连只有她一个候选的也是）等运营点「是她」，和老板同名的成员不是候选也不能点、看到时下次扫描重查老板一次；她有建好 / 正在建 / 不确定 / 手标「建好了」/ 另一个老板拉的群做三方群时扫描不接手（「她已经有三方群」），两个候选里有人的三方群正在建 / 不确定也不接手，接手前在 `ThreeParty.guard` 里再看一次（状态刚写上、正在建都算），「拉三方群」在锁里再读一遍合作单，运营点「是她」保留原来的三方群；不是新拉的群（一个月前被拉进来、整段历史一个月前开始、只读到最近几行、什么都不知道）不接手、以后也不看，基线前 10 分钟内、基线后被拉进来的、旧后端（只给 `created_at`）照旧接手；开场白等后端说补读完（`watch_caught_up`）才写、带着老板的介绍，只写一次，重启以后接着等，她在补读里说过话就不写，后端说不出时 90 秒以后写；回复发出去只去掉它的任务被告诉的那几句（编号），之后才交来的早先的话留给下一份；她的群草稿在等时老板 / 别人说话：重新起草（她的话、群里现在的话、`meta.sent` 说那份没发），已批准的、私聊的不动，在等的开场白重写；运营在群里打字：留着的话全部算回过；开关关着时三方群建好、控制台开场白还没被取走、她私聊来消息：并进开场白任务（和 f06ba2a 一样）；她在群里说话不让私聊再回一次「稍等」，她私聊再说话是新的一轮；找她的私聊只问后端安静的 `has_private_chat`、一分钟一次，从不调 `chat_target`，欠着开专属佣金、哪儿都发不到她时发送线程一轮一轮跑也不写日志；老板的 wxid 一小时查一次、`PRM_GROUP_WITH` 变了马上查、查失败下一次扫描再查；看过两次没老板 / 人太多的群不再读成员、重查老板以后再看，等她进群的群每次扫描照读；老板的名字不出这台电脑（`PRM_BOSS_CALL`：任务的群名、`boss`、群里的话、她和运营在群里的话、我方回复，控制台照旧显示真名）；开关关掉以后，以前留下的群草稿（在等的、已批准的、之后才写好的）都不发、标成「老板拉的群关了，没发」。真的后端（`tests/test_boss_groups_e2e.py`）：开场白等补读、带着老板的介绍；重启以后读回过的我们的话不再交来、不误接管；找她的私聊不写错误日志、不写她的 wxid；老板在她的群草稿在等时说话：重新起草、批准以后发进群里、之后的话留着；会话被删、又回来的一个月前的群不接手；任务里没有老板的名字。设置（`tests/test_config.py`、`tests/test_web_advanced.py`：`PRM_BOSS_CALL` 默认「老板」、空着是「老板」、超过 10 字拒绝、错误落在它自己那一项、排在 `PRM_BOSS_GROUPS` 后面）；控制台（`tests/test_prm_console_api.py`：`boss_groups.boss_call` 运行中和停着时）。

### 手动验证清单第 11 条（老板拉的群）后面加一句

开场白不是接手的那一刻出现，而是机器人读完群里已有的话以后（通常几秒到一分多钟），草稿里应该提到老板在群里介绍她的话；
Mac 那边的提示里只出现「老板」（或 `PRM_BOSS_CALL` 设的称呼），不出现老板的真名；重启机器人以后，群里之前机器人发过的话不会让合作单变成「人工接管」。

---

## 三、和审查 / spec 不一样的地方，待定

- **旧后端（端到端测试现在用的）**：`boss_groups` 每项不给 `history_complete` / `joined_at` 时，只按 `created_at`（最近 200 行里最早一行）判断新不新，没有 `created_at` 也算新——不然端到端测试全部接手不了；后端那一部分加上这两个键以后自动按严格的规则（审查 4）。
- 没有 `watch_caught_up` 的后端：开场白接手 90 秒以后写；`watch_caught_up` 一直不为真（补读一直做不了）就一直等（不盲写）。开场白每一轮发送线程（5 秒）都看，不只每次扫描。
- 没有 `has_private_chat` 的后端：`knows_private` 当成不知道（False），不退回 `chat_target`。
- 「她已经有三方群」只管扫描的自动接手；运营在 pending 的群里点「是她」照样关联，保留原来的三方群（不确定的也保留）。
- 回复发出去去掉的是它的任务被告诉的那几句（`seq`）；没被告诉的更早的话（超过 10 句的）留着。运营亲手在群里打字去掉全部。
- 审查 9 也包括在等的群开场白（重写开场白）；她的草稿正在写（还没进待审批）时老板说的话不触发重写，留给下一份。
- 老板名字的替换比审查要的多：她在群里的话、运营的话、`meta.sent`、有接手群的达人私聊里的话也换。
- `last_by_chat` 对每位达人都记（开关关着也记，看不出区别），这样以后打开开关时私聊那一轮也对。
- 有认不出的老板名字时找到的群一直是 pending；名字改对以后不会自己接手，要运营点「是她」。
- 端到端的 xfail：去掉了两个（重启后读回过的我们的话；找私聊的错误日志），留下一个 `test_the_opening_the_mac_takes_tells_the_bosss_introduction`（要后端的 `watch_caught_up`：合并后端以后会 XPASS，那时去掉标记）。
