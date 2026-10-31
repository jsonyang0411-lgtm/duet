# DOCS-FIX-backend：老板拉的群（功能 U）后端部分的审查修正

分支 `boss-groups-fix-backend`（从 `boss-groups` 5188160）。只改了 `src/wechat/wcdb_backend.py` 和
`tests/test_boss_groups_backend.py`。协调者把下面的文字并进 `CODEBASE_REFERENCE.md` 和 `CLAUDE.md`：
**A.2 / A.3 里「DOCS-backend 的行」指 `DOCS-backend.md` 里的行**（它们还没进参考手册），先并 DOCS-backend，再按这里改。
没有新设置、没有新表。

---

## A. CODEBASE_REFERENCE.md

### A.1 §1.1 环境变量：`SEND_VERIFY_CHAT` 那一行，在「或者要发私聊却打开了带人数的群，」后面加

> 或者要发**群**（老板拉的群）却打开了读得出、但不带人数的标题（私聊，或者不认识的：要发群「小美」、搜出来的是联系人「小美美」，哪怕标题就叫「小美」），

并在 `（WRONG_CHAT）` 后面加 `/ 群的是 NOT_A_GROUP`。

### A.2 §1.2 模块级常量（`src/wechat/wcdb_backend.py`，加两行）

| 常量 | 类型 | 值 | 定义位置 | 说明 |
|---|---|---|---|---|
| `NOT_A_GROUP` | `str` | 「微信搜索「{name}」打开的是「{title}」，不是群（群聊的标题带人数），没有发：请手动发到群里，或者在微信里给群起一个不和别的聊天重名、也不被别的名字包含的群名（改好后重启 Windows 上的机器人）」 | `src/wechat/wcdb_backend.py` | `SEND_VERIFY_CHAT`：发群时打开的聊天标题不带人数（`title_verdict` 是 `private`）拒发的原因 |
| `JOINED_LINE` | `re.Pattern` | `邀请你(?:[和、].*?)?加入了?群聊` 或 `你(?:通过.*?)?加入了?群聊` 或 `你邀请.*?加入了?群聊`（后两个的「你」在开头，或者前面是空白、冒号、引号） | 同上 | `boss_groups` 的 `joined_at`：微信自己的行（10000 / 10002）里说这个号进了群的那几种：「"老板测试"邀请你加入了群聊」（后面可能有「，群聊参与人还有：…」）、「"老板测试"邀请你和"小美"加入了群聊」、「你加入了群聊」、「你通过扫描"老板测试"分享的二维码加入群聊」、「你邀请"小美"、"老板测试"加入了群聊」（这个号建的群）；别人的名字在引号里，「"你好呀"邀请"小美"加入了群聊」不算。所有写法都在这一个常量里 |

### A.3 §2.7 `src/wechat/wcdb_backend.py`

**新增的行**

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `has_private_chat(wxid)` | 老板拉的群（功能 U）：这个号和她有没有窗口找得到的私聊（她是我们的好友）：`_window_target` / `_talker_to_name` 用的地方里有一处给她的私聊一个名字就是真：在读的聊天（`_chat_names`，登记达人的）、上一次读的会话列表（`_session_names`）、`data/private_chat_names.json`（微信以前列过的私聊）。和别的聊天同名也算（她的私聊在；发过去会 `NAMESAKE` 拒发，给她设个不重复的备注就能发）。**安静**：不记任何日志、不读数据库、不因此去读她的聊天（`chat_target` 对不是好友的达人记 ERROR，找得到时还会开始读那个聊天）。群、公众号、微信自己的会话、这个号自己从不算。PRM 用它代替 `chat_target` 判断她的私聊 | `wxid: str` | `bool` |
| `watch_caught_up(chat)` | 这个群的补交（`_watch_catch_up`：`since` 以后的行交出去，老板的介绍在里面）在最近一次 `watch_group` 以后跑完了没有：`watch_group` 置假，补交一行不落地交完时置真（补交期间又 `watch_group` 了的不置，等下一轮那次）；还没跑（下一轮轮询；群读不出、数据库读不了时更久）、没在盯都是假。数据库重开后的再补交（`_rearm_watched`）不把它变回假。PRM 等它为真再要群开场白 | `chat: str` | `bool` |
| `_hand_on_watched(standardized, callback, *, screenshot=True)` | 盯着的群的一行交给路由，**就在轮询线程里**（补交、轮询、原地改写的撤回都走这里）：按微信写的顺序一条一条，不进线程池（线程池里她的图片和后面那句、老板在她前面的那句会乱序，PRM 按听到的顺序起草）。先记进 `_handed_set`，再 `_handle_message`；路由从不回群消息，所以这里不发东西；`screenshot`：找不到原图时可以截聊天窗口（补交的不截） | `standardized: dict, callback, screenshot: bool` | `None` |
| `_history_of(chat)`（代替 DOCS-backend 的 `_group_created_at(chat)`） | `boss_groups` 用：群最新 `CATCHUP_READ` 行说明了什么，每次运行读一次（`_group_history`）：`{created_at: 最早一行的时间或 None, history_complete: 回来的行少于 CATCHUP_READ, joined_at: 其中匹配 JOINED_LINE 的微信自己的行（10000 / 10002，`_system_text`）里最早那行的时间或 None}`；一行都没有的群是 complete、两个时间都是 None（不记住，下次再读）；读不了（数据库关了、错误码）是 None，只记 debug | `chat: str` | `dict \| None` |
| `wcdb_backend._system_text(content)` | 微信自己的一行（10000）的整段文字：纯文本，或 XML（撤回取 `replacemsg` / `revokemsg` 的 `content`，别的 XML 取全部文字），空白折叠、不截断；`_special_info` 的系统行（截到 200 字）和 `_history_of` 都用它（`_special_info` 的结果不变） | `content: str` | `str` |

**改了的行**

- DOCS-backend 的 `boss_groups(members_any, known, max_members)` 行：返回值的每一项再加两个键，描述换成：
  「…`[{chat, name, members, created_at, history_complete, joined_at}]`；从它最新 `CATCHUP_READ` 行（每次运行读一次，`_history_of`）：`created_at` = 最早那行的时间（新群就是这个号被拉进来的时候：微信不给新成员看之前的话）；`history_complete` = 回来的行少于 `CATCHUP_READ`（整个群的记录都读到了）；`joined_at` = 其中微信说这个号被拉进来 / 被邀请 / 和别人一起建群的那行的时间（`JOINED_LINE`，有几行取最早的），没有是 None。PRM 只接手对我们真是新的群。行读不了的群这次不给（下次再问）；数据库读不了是 None。」
- DOCS-backend 的 `watch_group(chat, since)` / `unwatch_group(chat)` 行：「它的每一行一条一条（不进连发合并）」改成「它的每一行一条一条、按顺序，在轮询线程里（不进连发合并、不进线程池，`_hand_on_watched`）」；「`since` 以后写的」改成「写在 `since` 或之后的（`timestamp >= since`）」；「`since` = 它处理过的最后一行」改成「`since` = 它处理过的最后一行 + 1 秒（这个号自己的行从不入库：停机前读回过的、在那一秒或更早的不会再交给 hook）」；末尾加「补交跑完以后 `watch_caught_up` 为真；再调一次 `watch_group` 又是假，直到那次补交跑完」。
- DOCS-backend 的 `_watch_catch_up(group_name, talker, since, callback)` 行：「按时间顺序一条一条交（不进合并、不进线程池）」后面加「（`_hand_on_watched`）」；「挑出 `timestamp >= since`」后面加「（PRM 重启后给最后处理那行 + 1 秒：那一秒的行不再交）」；末尾加「一行不落地交完（机器人没在中途停）而且这期间没有新的 `watch_group`：`watch_caught_up` 置真」。
- DOCS-backend 的 `_poll_group(...)` 改动：「新行一条一条 `_dispatch_message`」改成「新行一条一条、按顺序 `_hand_on_watched`（在轮询线程里，不进线程池）」。
- 参考手册 `_recall_in_place` / `_hand_on_recall` 那一行：「她的走路由（`_dispatch_message`）」后面加「；盯着的群里的走 `_hand_on_watched`（和群里别的行按顺序，在轮询线程里）」。
- 参考手册 `_handle_message(...)` 那一行：「处理单条消息(线程池)」改成「处理单条消息（线程池；盯着的群的行在轮询线程里，`_hand_on_watched`）」。
- 参考手册 `_window_target(talker, name="")` 那一行：「日志「Cannot resolve chat_id」」改成「日志 ERROR「Cannot resolve chat_id=<编号前 8 位>… to chat name」（只写编号前 8 位）」。
- 参考手册 `_check_kwargs` / `_open_chat_check` / `_read_title` 那一行：「`other` / `group` 隔 `VERIFY_PAUSE_SEC` 再读，读满 `VERIFY_LOOKS` 次还是就返回 `SendRefused(WRONG_CHAT)`」改成「`other` / `group` / `private` 隔 `VERIFY_PAUSE_SEC` 再读，读满 `VERIFY_LOOKS` 次还是就返回 `SendRefused`（`private` 是 `NOT_A_GROUP`，别的是 `WRONG_CHAT`）」。
- 参考手册 `title_verdict(title, name, others, is_group)` 那一行：「要发私聊、标题却带人数是 `group`」后面加「；要发群、标题读得出却不带人数是 `private`（私聊的，或者不认识的，哪怕和群同名；群的标题总带人数），只有被「…」截断、群名以读到的部分（至少 3 个字）开头的不下判断（`unknown`：人数可能一起被截掉了）；要发群时名字一样也要带人数才是 `ok`」；末尾加「私聊的判断一点没变」。

### A.4 §3.2 调用关系图（DOCS-backend 那一段改成）

```
              ├── _admit_catch_up_chats()
              ├── _admit_watched_groups()（watch_group 要读的群加进来 / 不再盯的拿掉；没名字的每分钟问一次名字）
              ├── for each chat in _chat_names:
              │     _poll_group(...)
              │       ├── 盯着的群有补交 → _watch_catch_up()（读回 200 行、全记已见、since 以后（>=）没存过没交过的按顺序：
              │       │     这个号的 → _tell_own_message → hook（PRM on_own_message），别的 → _hand_on_watched → _handle_message → 路由；
              │       │     交完 → watch_caught_up 为真）
              │       ├── …
              │       ├── 盯着的群：_line_is_own（没有发送人的行走路由）→ 自己的 → _tell_own_message()
              │       ├── 盯着的群：_hand_on_watched() → _handle_message()（在轮询线程里，按顺序；不进 MessageBurstBuffer、不进线程池）
              │       └── 盯着的群里原地改写的撤回：_hand_on_recall() → _hand_on_watched()

（轮询之外，PRM 的线程：contact_ids / boss_groups（→ _history_of）/ group_members 经 with_client(get_sessions | get_group_members |
  get_display_names | get_messages)，每个 with_client 一个调用；has_private_chat 只看内存里的 _chat_names / _session_names 和
  data/private_chat_names.json，不碰数据库；watch_group / unwatch_group / watch_caught_up 只动 _catch_up_lock 下的状态）
_reinitialize() → _resolve_groups() → _prime_known_ids() → _rearm_watched()
```

### A.5 §4 功能U（老板拉的群）的后端部分：DOCS-backend 的流程图和表

流程图里：

- `backend.boss_groups(...)` 那行的结果改成 `{chat, name, members, created_at, history_complete, joined_at}`；
- 接手那段改成 `接手 → backend.watch_group(chat, since) → 等 backend.watch_caught_up(chat) → 群开场白`；
- `→ 以后每行：` 后面加 `（在轮询线程里一条一条、按顺序）`；
- 发草稿那段加一行 `  → 打开的标题不带人数（私聊、不认识的）：NOT_A_GROUP，SendRefused，什么都不打`；
- 加一行 `机器人自己起的消息发到哪 → backend.has_private_chat(她)：有私聊（在读的、会话列表、json 名单）→ 私聊，否则群（不记日志）`。

表里加（或改）这几行：

| 条件 | 怎么做 |
|---|---|
| 是不是新群 | `boss_groups` 每项带 `history_complete`（最新 200 行读回来不到 200 行：整个群都读到了；新拉的群从「邀请你加入了群聊」那行开始，微信不给新成员看之前的话）和 `joined_at`（微信说这个号进群的那行的时间，`JOINED_LINE`，取最早的；没有是 None）。老群：200 行以外才有那行，`history_complete` 假、`joined_at` None。行读不了的群这次不给，下次再问 |
| 她有没有私聊 | `has_private_chat`：在读的聊天、会话列表、`data/private_chat_names.json` 里有她私聊的名字就是有（同名的也算）；不记日志、不读库 |
| 补交跑完了没有 | `watch_caught_up`：`watch_group` 以后的那次补交交完才是真；群读不出、数据库读不了时一直是假（PRM 要有自己的等待上限） |
| 顺序 | 盯着的群的每一行（补交、新行、撤回）在轮询线程里一条一条按顺序交给路由，不进线程池；别的聊天照旧进线程池。她的图片在电脑上找不到、要截聊天窗口时，这几秒轮询等着（和语音识别一样） |
| 交一次（改） | 「重启后 PRM 用「最后处理的那行」当 `since`」改成「重启后 PRM 用最后处理的那行（我们自己读回的行也算）+ 1 秒当 `since`，补交按 `timestamp >= since` 比：那一秒和更早的我们自己的行（从不入库）不会再交给 hook」 |
| 发到群里 | `SEND_VERIFY_CHAT` 读到的标题不带人数（私聊，或者不认识的，哪怕和群同名：群「小美」搜出联系人「小美美」或「小美」）：再读几次（屏幕可能还停在上一个聊天），还是就 `NOT_A_GROUP`，不粘贴、不重试；被「…」截断、群名以它开头的不下判断；读不出（空的、没有字、读屏幕出错）照旧发。私聊的发送一点没变 |
| 日志（改） | `_window_target` 的 ERROR「Cannot resolve chat_id=…」只写编号前 8 位 |

---

## B. CLAUDE.md

### B.1 测试表「老板拉的群（后端，功能 U）」那一行，换成

| 老板拉的群（后端，功能 U） | 真的 WCDB 后端、路由、消息库接在假微信 4 数据库上（`tests/test_boss_groups_backend.py`）：`contact_ids` 名字恰好一个人才给 wxid（不管空格、大小写；老板只在群里、没有私聊也认得出；私聊、群成员、控制台私聊名单里有第二个同名的就是 None；这个号自己从不算；读不了库全是 None）；`boss_groups` 只给基线以后的、有老板的、人数不超过上限的群（开始得晚的在前，`created_at` 是最早那行的时间），处理过的不再给，这个号那一项的 wxid 是 `own_wxid()`；成员的名字是这个号看到的（备注优先），没有用她成员行里的群昵称，都没有是空；`history_complete`（不到 200 行是真；正好 200 行、更多是假）和 `joined_at`（微信说这个号进群的那行：「邀请你加入了群聊」带不带「群聊参与人还有」、「邀请你和…加入了群聊」、「你加入了群聊」、扫码进群、「你邀请…加入了群聊」都认；别人进群、名字里有「你」的成员、改群名、撤回、被移出、成员自己打的同样的字都不认；拉进来又移出又拉进来取第一次；老群那行在 200 行以外是 None），每次运行读一次，行读不了的群这次不给、下次给；`group_members` 是现在的成员（她退群就少了她），读不了库 / 报错误码 / 一个成员都没有是 None；`has_private_chat`：她只在老板的群里是假，控制台私聊名单、会话列表里有她的私聊是真（不因此读她的聊天），登记达人在读的聊天是真、同名的也算（发送照旧拒），群、这个号、公众号、文件传输助手从不算，不记任何日志、不读库；`chat_target` 认不出名字时 ERROR 只写编号前 8 位；`watch_group` 以后 `WECHAT_GROUPS` 没写的群也读，`since` 以后（`>=`：`since` 前一秒的不交，正好 `since` 的交）的行按时间顺序、按群消息走路由（字段对、只交一次、一行一条不合并），她说「谢谢老板邀请」不被过滤，微信的系统行（拉人进群）走路由不当成我们的，自定义号的前缀也去掉；`watch_caught_up`：没在盯、补交还没跑是假，跑完是真（那时老板的介绍已经交出去），再 `watch_group` 又是假到下一轮，不再盯是假，群读不出时一直是假，补交期间又 `watch_group` 了的等下一次补交；线程池开着时盯着的群的每一行（补交的、新的、她的图片和后面的话、撤回）都在轮询线程里按写的顺序交给路由，别的群的照旧进线程池；`WECHAT_GROUPS=*` 时盯以前路由收到的（连发合并的也算）不再交；重启后（两种 `WECHAT_GROUPS`）只交停机期间的，运营在手机上打的给 hook，停机前读回过的我们的话（`since` = 它 + 1 秒）不再交给 hook、后一秒她的话照交；数据库重开后重开期间的照样交、只一次；读不出的群下一轮再补；不再盯以后只有 `WECHAT_GROUPS` 写着的才读（自己的话不给 hook），再盯补上没读到的；盯着的群里这个号的话给 hook（`is_group` 真）、从不进路由，别的群照旧（自己的话不给 hook、「邀请」照旧过滤、系统行照旧不交）；盯着的群里原地改写的撤回交出去；发送按群名搜到它（第一轮读之前也行，SEND_VERIFY_CHAT 读到带人数的群名照发），搜出来是别的聊天不粘贴，**标题不带人数（不认识的联系人「小美美」，或者和群同名的「小美」）再读几次后 `NOT_A_GROUP`、不粘贴、文件和截图也不做**，屏幕还停在上一个聊天时再读到群的标题照发，读不出（空的、没有字、读屏幕出错）照旧发，被「…」截断、群名以它开头的不下判断，私聊的发送照旧（不认识的标题照发、群的标题拒发），和别的群 / 私聊同名、只知道编号（没有群名）都返回 `SendRefused`、什么都不打，起了群名以后能发；日志里没有名字 |

### B.2 测试表「发送前核对打开的聊天（`SEND_VERIFY_CHAT`）」那一行

「或要发私聊却是群就不粘贴、不重试、返回带原因的 `SendRefused`」改成「或要发私聊却是群、要发群却是不带人数的标题（私聊、不认识的，`tests/test_boss_groups_backend.py`）就不粘贴、不重试、返回带原因的 `SendRefused`」。

### B.3 手动验证清单第 11 条（老板拉的群（后端））末尾加

> 发群消息时日志里应是「SEND_VERIFY_CHAT: the open chat is '群名', as asked」；如果群开着却被拒、原因是「不是群（群聊的标题带人数）」，说明这台电脑读标题读不出群的人数，把「SEND_VERIFY_CHAT」那几行发回来（群名很长、标题被截断时也看一次）。老板新拉的群被接手时说明 `joined_at` 认出了微信的那一行（「"老板"邀请你加入了群聊」或「…邀请你和…加入了群聊」）；如果新群一直被当成「不是新的」，把群顶上那行微信的字原样抄回来（不用写名字）。

---

## C. 给 PRM 那一部分和协调者

1. **`has_private_chat` 的意思**：名字知道就算有（三处：在读的、会话列表、json 名单），和以前 `chat_target` 给不给名字一样，只是安静、不读她的聊天。同名的也算：发过去会被 `NAMESAKE` 拒（草稿没发出去、原因写着），不会改发到群里。要「同名就当没有私聊、改发群里」的话告诉我。
2. **`history_complete` / `joined_at`**：`joined_at` 取最早那行（拉进来、移出、又拉进来：算第一次；我们自己建的群「你邀请…」也算）。一行都没有的群：`history_complete` 真、两个时间 None。行读不了的群那一次不出现在结果里（不是给一个 `history_complete` 假的项），下次扫描再问。每次运行读一次（第一次读到的结果一直用）。`history_complete` 靠 DLL 按 `limit` 给满 `CATCHUP_READ` 行（和重启补读同一个前提）。
3. **`watch_caught_up`**：数据库读不了（OCR 备选那段时间）、群一直读不出时一直是假，PRM 等开场白要有上限。数据库重开后的再补交不让它变回假。
4. **`since`**：补交照旧 `timestamp >= since`（时间是整秒），PRM 给「最后处理那行 + 1 秒」，那一秒和更早的我们的行不再交；同一秒里没处理到的她的行（停在两行之间）会漏掉：这是 +1 秒的代价，存过的行本来也不会再交。
5. **顺序**：盯着的群的行现在在轮询线程里同步交给路由（像补交一样）。PRM 的 `handle` 很快；只有她的图片在电脑上找不到、要截聊天窗口时，轮询会等几秒（拿窗口锁）。别的聊天照旧进线程池。
6. **复现测试**：只有这个分支时，`test_r1`（`since` 用她那行、没 +1）照旧失败：那是 PRM 那边改成最后处理那行（含我们自己的）+ 1 秒以后才过；后端这边的同一场景（`since` = 我们那行 + 1 秒）在 `test_after_a_restart_our_line_read_back_before_since_is_not_handed_on_again`。`test_r8` 照旧失败：`knows_private` 还在调 `chat_target`（ERROR 现在只写「wxid_xia…」），PRM 改用 `has_private_chat` 以后才过。e2e 里的 4 个 xfail 都还是 xfail（都是 PRM 那边的）。
7. **截断的标题**（spec 之外加的一条）：发群时标题不带人数一律拒发，只有被「…」截断、群名以读到的部分（至少 3 个字）开头的不下判断、照旧发：群名很长时微信可能把人数一起截掉，不然这种群永远发不出去。

## D. 跑过的测试

- `tests/test_boss_groups_backend.py`：71 passed。
- 指定的那些（`tests/test_boss_groups_backend.py tests/test_boss_groups_e2e.py tests/test_same_name_chats.py tests/test_wcdb_backend*.py tests/test_send_verify_chat.py tests/test_new_friends.py tests/test_three_party.py`）：235 passed, 4 xfailed。
- 所有用到 `WcdbBackend` 的 27 个测试文件加 `tests/test_router_business.py`：922 passed, 4 xfailed, 14 subtests passed。
