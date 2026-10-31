# DOCS-backend: 老板拉的群（功能 U）的后端部分

Branch `boss-groups-backend` (from f06ba2a). Only `src/wechat/wcdb_backend.py` and the new
`tests/test_boss_groups_backend.py` changed. The coordinator merges the text below into
`CODEBASE_REFERENCE.md` and `CLAUDE.md`. The backend adds no setting and no table.

---

## A. CODEBASE_REFERENCE.md

### §1.2 模块级常量（`src/wechat/wcdb_backend.py`，加三行）

| 常量 | 类型 | 值 | 定义位置 | 说明 |
|---|---|---|---|---|
| `GROUP_NO_NAME` | `str` | 「机器人不知道这个群在微信里叫什么（群还没有群名，或者还没读到），没法在微信里找到它，没有发：请手动发，或者在微信里给群起一个不和别的聊天重名的群名」 | `src/wechat/wcdb_backend.py` | 老板拉的群（功能 U）：只知道编号的群（微信不给群名：没人起过名，或者还没读到）发送时的 `SendRefused.reason`（`_window_target`）：不然会把编号打进微信的搜索 |
| `NAMES_BATCH` | `int` | `200` | 同上 | `_display_names` 每次调 `get_display_names` 最多带几个 wxid |
| `_MEMBER_NAME_KEYS` | `tuple[str, ...]` | `remark`、`displayName`、`display_name`、`groupNickname`、`group_nickname`、`roomNickname`、`nickName`、`nickname`、`nick_name` | 同上 | `get_display_names` 不给名字的群成员，用她成员行里的这些字段（群昵称）；微信 4 的成员行到底带哪些字段要真机看（已知有 `username`） |

### §2.7 `src/wechat/wcdb_backend.py`（新增的行）

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `contact_ids(names)` | 老板拉的群（功能 U）：`PRM_GROUP_WITH` 里每个名字（老板在这个号通讯录里的名字，有备注是备注）→ 叫这个名字的那**一个** wxid；没人叫、两个以上叫都是 `None`（从不猜）。名字按同名规则比（`_name_key`：不管空格、大小写）。算数的人（`_people`）：现在会话列表里的私聊、`data/private_chat_names.json` 里的私聊、会话列表里**每个群**的成员（老板在他拉的每个群里），名字都来自 `get_display_names`；这个号自己从不算。键就是传进来的名字；数据库读不了时全是 `None`。日志只写个数 | `names: list[str]` | `dict[str, str \| None]` |
| `boss_groups(members_any, known, max_members)` | 会话列表里不在 `known`（PRM 的基线和已经处理过的群）里、成员里有 `members_any`（老板们的 wxid）之一、成员（含这个号）不超过 `max_members` 个的群，开始得晚的在前：`[{chat, name（微信给的群名，没有是 ""）, members（同 group_members）, created_at}]`；`created_at` = 它最新 `CATCHUP_READ` 行里最早那行的时间（新群就是这个号被拉进来的时候；每次运行只读一次，记在 `_group_started`），没有行是 `None`。不合条件的群不查名字。数据库读不了（会话列表报错误码、读的中途关了）是 `None`；出错只记 warning | `members_any: set[str], known: set[str], max_members: int` | `list[dict] \| None` |
| `group_members(chat)` | 群现在的成员，数据库里的顺序：`[{wxid, name}]`，`name` = 这个号看到的名字（`get_display_names`：备注或昵称），没有就用她成员行里的名字（群昵称，`_MEMBER_NAME_KEYS`），都没有是 `""`；**这个号自己那一项的 wxid 是 `own_wxid()`**（数据库里写的是不带数据目录后缀的 `wxid_abc`，`own_wxid()` 是 `wxid_abc_b6ce`：这样 PRM 用「成员 − own_wxid()」直接去掉自己）。数据库读不了、或者一个成员都没给（群里至少有这个号：空列表是没读出来）是 `None` | `chat: str` | `list[dict] \| None` |
| `watch_group(chat, since)` / `unwatch_group(chat)` | 从下一轮轮询起读这个群，不管 `WECHAT_GROUPS` 写没写它（`_admit_watched_groups`）；它的每一行一条一条（不进连发合并）按群消息走路由（标准化消息：`is_group` 真、`chat_id` = 群、`sender_id` = 成员 wxid、`sender_name`、`message_id`、`timestamp`、`content`、`msg_type`…），这个号自己的行交给 own-message hook（不进路由），微信自己的行（没有发送人：拉人进群、拍一拍、撤回）也走路由。先只做一次补交（`_watch_catch_up`）：`since` 以后写的、以前没交过的（消息库没存过、这次运行没交过）按时间顺序交出去。只在内存里：PRM 每次启动给每个已接手的群再调一次（`since` = 它处理过的最后一行）；再调一次就再补一次（不会交两次）。`chat` 不是群（不以 `@chatroom` 结尾）只记 warning / 不再盯：从下一轮起只有 `WECHAT_GROUPS` 写着的才读，和别的群一样（自己的话不给 hook、群消息静默），还没做的补交取消 | `chat: str, since: float` / `chat: str` | `None` / `None` |
| `_people()` / `_display_names(wxids)` / `_members_out(members, names)` / `_group_created_at(chat)` | `contact_ids` 的名单：wxid → 名字（见上；数据库现在给的名字盖过 json 里的；不含这个号、群、公众号、微信自己的会话），读不了是 `None` / 每 `NAMES_BATCH` 个调一次 `get_display_names`，只留有名字（不等于 wxid）的，数据库中途关了是 `None` / 成员行 → `[{wxid, name}]`（这个号的那项换成 `own_wxid()`）/ `created_at` 的读法（见 `boss_groups`），读不了是 `None`、不记 | — / `wxids: list[str]` / `members: list[tuple[str, dict]], names: dict` / `chat: str` | `dict \| None` / `dict \| None` / `list[dict]` / `float \| None` |
| `_is_watched(chat)` / `_watched_name(chat)` | 这个群在不在盯（`_watched`）/ 盯着的群在微信里叫什么：最近一次会话列表的名字，没有就 `get_display_names([chat])`（没名字时每 `SESSION_REFRESH_SEC` 秒最多问一次：新群可能过一会儿才有名字），知道了就记住（以后改了群名要重启机器人才认新名字，和改备注一样）；没有是 `""` | `chat: str` | `bool` / `str` |
| `_admit_watched_groups()` / `_rearm_watched()` | 每轮开头（`_poll_cycle`，在 `_admit_catch_up_chats` 之后）：盯着、还没在读的群按 `_watched_name` 的名字 `_add_talker(is_group=True)`（微信不给名字时先用编号读，发送拒绝），记进 `_watch_added`；用编号在读的群一有名字就换上；不再盯、而且只是因为盯才读的群（`_watch_added`）从 `_chat_names` 拿掉 / `_reinitialize` 里 `_prime_known_ids` 之后：每个盯着的群下一轮再补交一次（重开期间进来的被 priming 记成已见了；交过的、存过的不再交） | — | `None` |
| `_handed_set(talker)` / `_line_is_own(standardized, my_wxid)` | 这次运行交出去过的群消息编号（`DedupSet(CHAT_KNOWN_SIZE)`；`_dispatch_message` 记每条群消息，连发合并的连同里面每一行的编号；`_tell_own_message` 记盯着的群里这个号的行）：补交从不再交它们 / 是不是这个号的：盯着的群里只按 wxid（new_friends `is_me`），**没有发送人的行（微信的系统行）谁的都不是**，走路由（`_is_own_line` 把 `""` 当成这个号的：`""` 是任何 wxid 的前缀）；别的聊天就是 `_is_own_line` | `talker: str` / `standardized: dict, my_wxid: str` | `DedupSet` / `bool` |
| `_watch_catch_up(group_name, talker, since, callback)` | 盯着的群的补交，在轮询线程里、这个群自己的那一轮之前：往回读 `CATCHUP_READ` 行（`_read_messages`；DLL 回错误码就放回去、返回 `False`，这一轮不碰这个群），全部记成已见（`_known_set`；最近的普通行也记进 `_recallable`），挑出 `timestamp >= since`、消息库没存过（`_stored_of`）、这次运行没交过（`_handed_set`）、没压在连发合并里的，按时间顺序一条一条交（不进合并、不进线程池）：这个号的给 `_tell_own_message`，别的 `_handle_message(..., screenshot=False)` 走路由；语音行再带识别标准化一次；机器人停了就停（没交的下次启动 PRM 再盯时还在）。日志只写群编号前 8 位、几条 | `group_name: str, talker: str, since: float, callback` | `bool` |
| `wcdb_backend._strip_sender_prefix(content, sender)` / `_member_rows(rows)` / `_row_name(row, wxid)` | 去掉群消息开头的 `<发送人>:`，不管 id 长什么样（老账号的自定义号没有 `wxid_` 前缀，`_strip_wechat_group_sender_prefix` 只认 `wxid_`；只用在盯着的群）/ `wcdb_get_group_members` 的行 → `(wxid, 行)`，每人一次、原来的顺序 / 成员行里的名字（`_MEMBER_NAME_KEYS`，不等于 wxid），没有是 `""` | 见左 | `str` / `list[tuple[str, dict]]` / `str` |

### §2.7（改了的行，整行替换）

- `own_wxid()` / `db_readable()` / … 那一行：「账号自己的 wxid」改成「账号自己的 wxid（`myWxid`，微信 4 数据目录的名字，带后缀 `wxid_abc_b6ce`；它自己发的行和群成员列表里是不带后缀的 `wxid_abc`，new_friends `is_me`；`group_members` / `boss_groups` 里这个号那一项用的是 `own_wxid()`）」。
- `set_own_message_hook(hook)` / `_tell_own_message(standardized)`：「只限过滤器要的私聊（登记 / 名单里的达人；群聊和别的私聊不给）」改成「过滤器要的私聊（登记 / 名单里的达人），和盯着的群（`watch_group`，消息 `is_group` 真；同时记进 `_handed_set`）；别的群、别的私聊不给」。
- `_talker_to_name(talker_id)`：末尾加「盯着的群还没在读、或者微信不给名字时用编号在读：用 `_watched_name`（不加进 `_chat_names`，下一轮 `_admit_watched_groups` 加）」。
- `_window_target(talker, name="")`：加「名字就是群的编号（微信不给群名）时返回 `SendRefused(GROUP_NO_NAME)`、什么都不打（日志 WARNING「Not using the WeChat window for …」）」。
- `_poll_cycle(callback)`：`_admit_catch_up_chats()` 后面加 `_admit_watched_groups()`。
- `_reinitialize()`：`_prime_known_ids` 之后 `_rearm_watched()`（盯着的群重开期间的行照样交出去，只一次）。
- `_resolve_groups()`：重建 `_chat_names` 时清空 `_watch_added`（盯着的群下一轮重新加）。
- `_poll_group(...)`：加「盯着的群：有补交先 `_watch_catch_up`（读不出这一轮到此为止）；新行一条一条 `_dispatch_message`（不进连发合并：每行用自己的编号入库，重启后的补交才认得出交过没有）；自己的行按 `_line_is_own`（没有发送人的行走路由）；最近的普通行记进 `_recallable`（原地改写的撤回和私聊一样交出去）；OCR 刚结束那一轮的积压照样交（OCR 分不出群里是谁说的）」。
- `_recall_in_place` / `_hand_on_recall`：自己的判断改用 `_line_is_own`。
- `_standardize(...)`：加「盯着的群：不用关键字过滤（「邀请」「被移除」等是给当成文字的系统提示写的，也会把成员自己的话丢掉，比如「谢谢老板邀请」）；`<发送人>:` 前缀按发送人去掉（`_strip_sender_prefix`）」。
- `_dispatch_message`（§3.2 里提到）：群消息记进 `_handed_set`（连发合并的连同里面每行的编号）。

### §3.2 调用关系图（WcdbBackend 那一段加）

```
              ├── _admit_catch_up_chats()
              ├── _admit_watched_groups()（watch_group 要读的群加进来 / 不再盯的拿掉；没名字的每分钟问一次名字）
              ├── for each chat in _chat_names:
              │     _poll_group(...)
              │       ├── 盯着的群有补交 → _watch_catch_up()（读回 200 行、全记已见、since 以后没存过没交过的按顺序：
              │       │     这个号的 → _tell_own_message → hook（PRM on_own_message），别的 → _handle_message → 路由）
              │       ├── …
              │       ├── 盯着的群：_line_is_own（没有发送人的行走路由）→ 自己的 → _tell_own_message()
              │       └── 盯着的群：直接 _dispatch_message()（不进 MessageBurstBuffer）

（轮询之外，PRM 的线程：contact_ids / boss_groups / group_members 经 with_client(get_sessions | get_group_members |
  get_display_names | get_messages)，每个 with_client 一个调用；watch_group / unwatch_group 只改 _catch_up_lock 下的状态）
_reinitialize() → _resolve_groups() → _prime_known_ids() → _rearm_watched()
```

### §4 功能U（老板拉的群）的后端部分（和 PRM 的说明合在一起）

```
PRM 扫描（每 60 秒）
  → backend.contact_ids(PRM_GROUP_WITH 的名字)：会话列表的私聊 + private_chat_names.json + 每个群的成员 → 名字恰好一个人才给 wxid
  → backend.boss_groups(老板 wxid, 基线 ∪ 处理过的, PRM_BOSS_GROUP_MAX)：新群、有老板、人不多 → {chat, name, members, created_at}
  → 等着的群：backend.group_members(chat)
接手 → backend.watch_group(chat, since)
  → 下一轮：_admit_watched_groups 读它 → _watch_catch_up（since 以后没交过的按顺序）→ 以后每行：
       她 / 老板 / 别人 / 微信的行 → 路由（群消息；PRM 的步骤认出接手的群）
       这个号的行 → own-message hook（PRM on_own_message：读回确认、运营亲手打的接管）
发草稿 → backend.send_text(群编号, 文字)：按微信给的群名搜（SEND_VERIFY_CHAT 读标题，群带人数）
  → 和别的聊天同名：NAMESAKE；只知道编号：GROUP_NO_NAME；都是 SendRefused，什么都不打
```

| 条件 | 怎么做 |
|---|---|
| 关着（`PRM_BOSS_GROUPS=off`） | 没人调 `watch_group`，后端和以前一模一样：群照旧只读 `WECHAT_GROUPS` 的、全部静默、自己的话不给 hook、关键字过滤照旧。只有两处对所有群：`_dispatch_message` 记下交过的群消息编号（只在内存里），发给只知道编号的群会被拒（现在本来没有东西往群里发） |
| 认老板 | `contact_ids`：名字恰好一个人才算（私聊、json 名单、任何一个群的成员里有第二个同名的就是 `None`），这个号自己不算；给老板设一个不重复的备注、`PRM_GROUP_WITH` 写备注就能认出。每次都读所有群的成员：一次扫描调一次就够 |
| 这个号自己 | `own_wxid()` 带数据目录后缀，成员列表里没有：`group_members` / `boss_groups` 把这个号那一项写成 `own_wxid()`，PRM 用相等就能去掉 |
| 交一次 | 补交只交消息库没存过、这次运行没交过的（`_handed_set`：连发合并里的每一行也算）；盯着的群每行单独入库（不合并），重启后 PRM 用「最后处理的那行」当 `since`，交过的不会再来（边界那一行存过了，路由的 `persist` 也会挡） |
| 交不到的 | `WECHAT_GROUPS=*`（或写了这个群）而且群在上次启动 / 重开时就在：从那时到 `watch_group` 之间路由收到的行已经存了（静默），补交不再交（`persist` 也会挡）；PRM 要的话从消息库读（`MessageStore.get_messages_since(群, since)`） |
| 系统行 | 盯着的群里没有发送人的行（`msg_type` 10000，`sender_id` ""，`content`「[消息类型:10000]」，文字在 `special.title`，例如 `"老板测试"邀请"小美"加入了群聊`）走路由；原地改写的撤回和私聊一样：新编号、`recalled_id` |
| 群名 | 发送按微信给的群名搜；没有群名（没人起过名）先用编号读、发送拒绝（`GROUP_NO_NAME`），每分钟再问一次名字，有了就能发；改了群名要重启才认新名字（和改备注一样）；和别的聊天（群、私聊）同名不发（`NAMESAKE`） |
| 日志 | 只写群编号前 8 位、条数和个数，从不写名字 |

---

## B. CLAUDE.md

### 测试表加一行（「同名的聊天」那一行后面）

| 老板拉的群（后端，功能 U） | 真的 WCDB 后端、路由、消息库接在假微信 4 数据库上（`tests/test_boss_groups_backend.py`）：`contact_ids` 名字恰好一个人才给 wxid（不管空格、大小写；老板只在群里、没有私聊也认得出；私聊、群成员、控制台私聊名单里有第二个同名的就是 None；这个号自己从不算；读不了库全是 None）；`boss_groups` 只给基线以后的、有老板的、人数不超过上限的群（开始得晚的在前，`created_at` 是最早那行的时间），处理过的不再给，这个号那一项的 wxid 是 `own_wxid()`；成员的名字是这个号看到的（备注优先），没有用她成员行里的群昵称，都没有是空；`group_members` 是现在的成员（她退群就少了她），读不了库 / 报错误码 / 一个成员都没有是 None；`watch_group` 以后 `WECHAT_GROUPS` 没写的群也读，`since` 以后的行按时间顺序、按群消息走路由（字段对、只交一次、一行一条不合并），她说「谢谢老板邀请」不被过滤，微信的系统行（拉人进群）走路由不当成我们的，自定义号的前缀也去掉；`WECHAT_GROUPS=*` 时盯以前路由收到的（连发合并的也算）不再交；重启后（两种 `WECHAT_GROUPS`）只交停机期间的，运营在手机上打的给 hook；数据库重开后重开期间的照样交、只一次；读不出的群下一轮再补；不再盯以后只有 `WECHAT_GROUPS` 写着的才读（自己的话不给 hook），再盯补上没读到的；盯着的群里这个号的话给 hook（`is_group` 真）、从不进路由，别的群照旧（自己的话不给 hook、「邀请」照旧过滤、系统行照旧不交）；盯着的群里原地改写的撤回交出去；发送按群名搜到它（第一轮读之前也行，SEND_VERIFY_CHAT 读到带人数的群名照发），搜出来是别的聊天不粘贴，和别的群 / 私聊同名、只知道编号（没有群名）都返回 `SendRefused`、什么都不打，起了群名以后能发；日志里没有名字 |

### 手动验证清单加一条

11. **老板拉的群（后端）** — 需要 Windows 真机，老板用手机拉一个「老板、小号、这个号」的小群（先不起群名）：看日志里「老板拉的群: group … is read from now on」那一行有没有写「WeChat gives it no name yet」（没起名的群 `get_display_names` 给不给名字）；起名以后一分钟内能发；`group_members` 里有没有这个号自己那一项（`own_wxid()`）、名字对不对（小号是好友时是备注）；这个号在群里发的话进了 hook（PRM 当成运营亲手回的接管），拉人进群那行走了路由；小号退群时群里有没有一行（没有的话 PRM 只能靠 `group_members` 看出她走了）。发群消息时日志里「SEND_VERIFY_CHAT: the open chat is … as asked」

---

## C. Notes for the PRM agent and the coordinator

1. **Own entry.** `own_wxid()` is the data folder's name (`wxid_abc_b6ce`). The database lists us as `wxid_abc`.
   `group_members` and `boss_groups` give our entry under `own_wxid()`, so `members − {own_wxid()}` works by equality.
2. **三方群 groups look like boss groups.** A group made by 「拉三方群」 (her + the boss + us) is new, small and has the
   boss. Put the deals' `three_party.chat` ids into `known`. Otherwise the scan takes it over and asks for a
   "老板介绍" opening.
3. **`since` for the first watch.** If the bot was stopped when the boss made the group, `found_at − 300` misses his
   introduction. `min(found_at − 300, created_at)` catches it (`created_at` comes from `boss_groups`).
4. **Lines stored before the watch.** With `WECHAT_GROUPS=*` (or naming the group), a group present at the last
   start/re-open was already read. Its lines from then until `watch_group` were stored and silenced, so they are not
   handed on again. PRM can read them with `MessageStore.get_messages_since(group, since)`.
5. **`contact_ids` cost.** It reads every group's members (one DLL call each) and their names. Call it once per scan
   at most.
6. **The hook gets group lines.** `on_own_message` receives messages with `is_group: True` and `chat_id` = the
   group. Today it returns for any chat id that is not a party's wxid.
7. **Her leaving.** WeChat may show other members no line when someone quits. Re-reading `group_members` for linked
   groups is the reliable way to notice.
8. **SEND_VERIFY_CHAT gap (unchanged).** If the search for a group opens a private chat the bot does not know (title
   without a member count), `title_verdict` says `unknown` and the send goes ahead, as for any chat. The existing
   tests pin this rule, so I left it unchanged.

Tests run:
- `tests/test_boss_groups_backend.py`: 24 passed.
- The 25 existing test files that use `WcdbBackend`: 818 passed (list in the report).
