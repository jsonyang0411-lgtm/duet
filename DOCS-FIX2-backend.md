# DOCS-FIX2-backend：老板拉的群（功能 U）第二轮审查的后端修正（F2、F6、F10、F11、F12）

分支 `boss-groups-fix2-backend`（从 `boss-groups` 02fdb44）。只改了 `src/wechat/wcdb_backend.py`、
`tests/test_boss_groups_backend.py`、`tests/test_send_verify_chat.py`。没有新设置、没有新表。

合并顺序：先按 `DOCS-FIX-backend.md` 改参考手册和 CLAUDE.md，再按这里改。下面说「DOCS-FIX-backend 的…」指那份文件里
给出的文字（它们还没进参考手册）。

---

## A. CODEBASE_REFERENCE.md

### A.1 §1.1 环境变量：`SEND_VERIFY_CHAT` 那一行

DOCS-FIX-backend A.1 加的那一句换成：

> 或者要发**群**（老板拉的群）却打开了不带人数、读出来是机器人认识的私聊的标题（在读的、会话列表里的、控制台私聊名单里的私聊，按 `_ocr_key` 比：不管空格、大小写，OCR 读不出的表情不算；要发群「小美」、搜出来的是认识的联系人「小美美」），

`（WRONG_CHAT）` 后面照 DOCS-FIX-backend 加 `/ 群的是 NOT_A_GROUP`；「读不出、看不出是谁的照旧发（和以前一样）」后面加
「（发群时：人数被 OCR 截掉一半的「小美合作群(3」照样算群的标题，不认识的标题、群自己的名字没读到人数也照旧发）」。

### A.2 §1.2 模块级常量（`src/wechat/wcdb_backend.py`）

- DOCS-FIX-backend 的 `NOT_A_GROUP` 行，说明一列换成：「`SEND_VERIFY_CHAT`：发群时打开的聊天标题不带人数、读出来是机器人认识的私聊（`title_verdict` 是 `private`）拒发的原因」（值不变）。
- 加一行：

| 常量 | 类型 | 值 | 定义位置 | 说明 |
|---|---|---|---|---|
| `_CUT_COUNT` | `re.Pattern` | `\s*[\(（]\s*(?:\d+\s*人?\s*)?$` | `src/wechat/wcdb_backend.py` | `title_verdict` 发群时用：标题末尾被 OCR 截掉一半的人数（右括号没读到，或者更多：「小美合作群(3」「小美合作群（3」「小美合作群 (35人」「小美合作群(」），这样的标题照样算群的标题（第二轮审查 F10） |

### A.3 §2.7 `src/wechat/wcdb_backend.py`

**新增的行**

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `wechat_now()` | 老板拉的群（功能 U，第二轮审查 F2）：微信的钟现在几点，按数据库能说的：现在会话列表里最新的时间（秒，`new_friends.session_time`；每个会话先取 `last_timestamp`（它最后一行写的时间），没有再按 `session_time` 的次序取 `sort_timestamp` 等：`sort_timestamp` 是列表的排序键，不一定是哪一行的时间，草稿可能挪动它）。不会比微信的钟快；号很安静时就是最后一次有东西写进来的时候。PRM 用它记基线的时间，和群自己的微信时间（`joined_at` / `created_at`）比，不用这台电脑的钟（可能差几个小时）。数据库读不了（会话列表报错、没打开）、没有一个会话带时间是 None。**安静**：从不记名字（会话列表读不了时一行 debug） | 无 | `float \| None` |

**改了的行**

- DOCS-FIX-backend 的 `boss_groups(members_any, known, max_members)` 行：「行读不了的群这次不给（下次再问）；数据库读不了是 None。」换成
  「成员或行读不了的群（成员报错误码、一个成员都没给：群里至少有这个号，微信可能还没把新群的成员写进库；行报错误码、读的中途库关了）这次给 `{"chat": 群编号, "unreadable": True}`，只有这两个键，按它会话的时间排在该在的位置：它可能是老板的群，PRM 不把它算成看过了，下次扫描再问。数据库读不了（会话列表读不了、读的中途库关了）是 None。」
  返回值写成 `list[dict] | None`（每项是完整的一项或 `{chat, unreadable}`）。
- `wcdb_backend.title_verdict(...)` 那一行（参考手册原来的，加上 DOCS-FIX-backend 的改动）整行换成：

| 函数 | 描述 | 参数 | 返回值 |
|---|---|---|---|
| `wcdb_backend.title_verdict(title, name, others, is_group, private=frozenset())` / `_ocr_key(name)` | 读到的标题说明了什么：去掉人数（`clean_group_name`）后 `_ocr_key` 和要发的名字一样是 `ok`（要发群时要带人数才算）；在 `others`（别的聊天名的 `_ocr_key`）里是 `other`；要发私聊、标题却带人数是 `group`；要发群、标题不带人数、`_ocr_key` 在 `private`（机器人认识的私聊名的 `_ocr_key`，名字和群一样的也在里面：私聊「小美🌸」读出来是「小美」）里是 `private`；空的、截断的、不认识的都是 `unknown`（不下判断）。要发群时人数被 OCR 截掉一半（`_CUT_COUNT`：「小美合作群(3」）照样算带人数（名字对得上就是 `ok`）；不带人数、不认识的（没见过的联系人、群自己的名字没读到人数、被「…」截断的）是 `unknown`，照旧发（第二轮审查 F10：以前不带人数的一律拒发，OCR 读丢了右括号的电脑上群草稿全发不出去）。私聊的判断一点没变（`private` 和截掉一半的人数都不看）/ 只留字母、数字、汉字（OCR 读不出表情和多数符号）并 `casefold()` | `title: str, name: str, others: set, is_group: bool, private: set` / `name: str` | `str` / `str` |

- `_check_kwargs` / `_open_chat_check` / `_read_title` 那一行（DOCS-FIX-backend 改过的）：「「别的聊天」= …的名字。」后面加
  「发群时另给 `title_verdict` 一个 `private`：同样三处里不是群（编号不以 `@chatroom` 结尾、`_chat_is_group` 不为真）的聊天名的 `_ocr_key`（群自己不算；名字和群一样的私聊也算）；发私聊时是空的。」；
  「（日志 WARNING「Not sending to …」）」换成「（日志 WARNING「Not sending to <编号前 8 位>…: SEND_VERIFY_CHAT read the open chat's title as '<private|other|group>' (NOT_A_GROUP|WRONG_CHAT); nothing is pasted」：只有编号前 8 位和判断，没有名字；带名字的原因只回给调用的地方（PRM 写在草稿上）；私聊发送拒发的这一行也一样，第二轮审查 F12）」。
- `_window_target(talker, name="")` 那一行：「这两种日志都是 WARNING「Not using the WeChat window for …」」后面加「（`GROUP_NO_NAME` 那一行只写编号前 8 位，原因里本来没有名字；`NAMESAKE` 那一行照旧）」。

### A.4 §3.2 调用关系图

DOCS-FIX-backend A.4 括号里那句「contact_ids / boss_groups（→ _history_of）/ group_members 经 with_client(…)」改成
「contact_ids / boss_groups（→ _history_of）/ group_members / wechat_now 经 with_client(…)」（`get_sessions` 已经在列表里）。

### A.5 §4 功能U（老板拉的群）的后端部分

流程图（在 DOCS-FIX-backend A.5 改过的基础上）：

- `boss_groups(...)` 那行的结果写成 `→ [{chat, name, members, created_at, history_complete, joined_at}]，成员或行读不了的 {chat, unreadable: True}（开始得晚的在前）`；
- 它下面加一行 `  wechat_now()：会话列表里最新的时间（每个会话先取 last_timestamp）= 微信的钟（PRM 基线的时间）；读不了是 None`；
- DOCS-FIX-backend 加的 `  → 打开的标题不带人数（私聊、不认识的）：NOT_A_GROUP，SendRefused，什么都不打` 换成
  `  → 打开的标题不带人数、读出来是认识的私聊：NOT_A_GROUP，SendRefused，什么都不打（日志只有编号前 8 位和判断）；不认识的、人数被截掉一半的（「(3」）照旧发`。

表里改这几行（DOCS-FIX-backend 的那几行）：

| 条件 | 怎么做 |
|---|---|
| 是不是新群 | 末尾「行读不了的群这次不给，下次再问」换成「成员或行读不了的群这次给 `{chat, unreadable: True}`（只有这两个键；PRM 不算看过了，下次再问）。PRM 记基线的时间用微信的钟（`wechat_now`：会话列表里最新的时间），和群自己的微信时间比」 |
| 发到群里 | `SEND_VERIFY_CHAT` 读到的标题不带人数、读出来是机器人认识的私聊（在读的、会话列表里的、控制台私聊名单里的，按 `_ocr_key` 比：群「小美」搜出认识的「小美美」，或者名字只差一个表情的「小美🌸」）：再读几次（屏幕可能还停在上一个聊天），还是就 `NOT_A_GROUP`，不粘贴、不重试；是认识的别的群：`WRONG_CHAT`（和以前一样）；人数被 OCR 截掉一半（「小美合作群(3」「（3」）照样是群的标题；不认识的（没见过的联系人、群自己的名字没读到人数、被「…」截断的）、读不出的（空的、没有字、读屏幕出错）照旧发。和群同名的人：名字一样（`_name_key`）的早被 `NAMESAKE` 拦下（不打字）；只差表情、符号的读成同一个 `_ocr_key`，是 `NOT_A_GROUP`；不认识的人光看标题认不出，照旧发。私聊的发送一点没变 |
| 日志（改） | 再加：`SEND_VERIFY_CHAT` 拒发的 WARNING「Not sending to …」只写编号前 8 位和判断（`'private' (NOT_A_GROUP)` / `'other' (WRONG_CHAT)` / `'group' (WRONG_CHAT)`），`GROUP_NO_NAME` 的「Not using the WeChat window for …」只写编号前 8 位；`wechat_now` 不记名字 |

§4 末尾「还没在真机上走过：…已知的限制：」那一段：

- 「按群名搜出来的是机器人不认识的私聊（标题没有人数）时 `SEND_VERIFY_CHAT` 判 `unknown`、照样发（和别的聊天一样，原来的规矩没改）」改成
  「按群名搜出来的是机器人不认识的私聊（标题没有人数、名字不在在读的 / 会话列表 / 控制台私聊名单里）时 `SEND_VERIFY_CHAT` 判 `unknown`、照样发（认识的私聊才拒发：`NOT_A_GROUP`）」；
- 加「盯着的群的行在轮询线程里一条一条交给路由（`_hand_on_watched`，为了按顺序）：她的图片在电脑上找不到、要截聊天窗口时，截图要等窗口锁（正在发消息、加好友、拉三方群时要等它们做完）再截几秒，这期间轮询（所有聊天）都等着。只是慢一点，不丢消息；接受（第二轮审查 F11）」；
- 加「`wechat_now` 先取会话的 `last_timestamp`、没有才用 `sort_timestamp`：真机上置顶的、有草稿的会话的 `sort_timestamp` 是什么样还没看过」；
- 加「窗口那边发送失败的 ERROR「Send failure: group='…'」（`window_controller._log_failure`，以前就有，所有拒发的都记）还写着搜的名字：不在这次的改动里」。

---

## B. CLAUDE.md

### B.1 测试表「老板拉的群（后端，功能 U）」那一行（DOCS-FIX-backend B.1 的文字）

- 「每次运行读一次，行读不了的群这次不给、下次给」换成
  「每次运行读一次，成员或行读不了的群（成员报错误码、一个成员都还没有、行报错误码）这次只给 `{chat, unreadable: True}`（只有这两个键，按会话时间排）、下次给全的（连着两次读不了也不漏，第二轮审查 N5），基线里的、没有老板的照旧不给，库读不了照旧是 None；`wechat_now` 是会话列表里最新的时间（微信的钟：这台电脑的钟快 3 小时也一样；先取会话的 `last_timestamp`，毫秒换成秒，没有时间的不算），读不了库、报错误码、没打开、没有一个会话带时间是 None，不记名字」；
- 「**标题不带人数（不认识的联系人「小美美」，或者和群同名的「小美」）再读几次后 `NOT_A_GROUP`、不粘贴、文件和截图也不做**，屏幕还停在上一个聊天时再读到群的标题照发，读不出（空的、没有字、读屏幕出错）照旧发，被「…」截断、群名以它开头的不下判断，私聊的发送照旧（不认识的标题照发、群的标题拒发）」换成
  「**标题不带人数、读出来是认识的私聊（会话列表里的、控制台私聊名单里的、在读的「小美美」；名字只差一个表情的「小美🌸」读成群名「小美」也算）再读几次后 `NOT_A_GROUP`、不粘贴、文件和截图也不做**，带人数的群名照发，屏幕还停在上一个聊天时再读到群的标题照发，人数被 OCR 截掉一半（「小美合作群(3」「（3」「(3人」）照发、算对上了，不认识的标题（没见过的联系人、群自己的名字没带人数）、读不出的（空的、没有字、读屏幕出错）照旧发，私聊的发送照旧（不认识的标题、截掉一半的人数照发，群的标题、认识的别的私聊拒发 `WRONG_CHAT`）」；
- 末尾「日志里没有名字」换成「拒发的日志只有编号前 8 位和判断（`NOT_A_GROUP` / `WRONG_CHAT` / `GROUP_NO_NAME`），日志里没有名字」。

### B.2 测试表「发送前核对打开的聊天（`SEND_VERIFY_CHAT`）」那一行

DOCS-FIX-backend B.2 的「要发群却是不带人数的标题（私聊、不认识的，`tests/test_boss_groups_backend.py`）」换成
「要发群却是不带人数、认识的私聊的标题（`NOT_A_GROUP`；人数被截掉一半的「测试群(35」照样是群的、不认识的照发，`tests/test_boss_groups_backend.py`）」；
「文件和截图也核对」后面加「；拒发的日志只写编号前 8 位和判断」。

### B.3 手动验证清单第 11 条

DOCS-FIX-backend B.3 里「如果群开着却被拒、原因是「不是群（群聊的标题带人数）」，说明这台电脑读标题读不出群的人数，把「SEND_VERIFY_CHAT」那几行发回来（群名很长、标题被截断时也看一次）」换成
「如果群开着却被拒、原因是「不是群（群聊的标题带人数）」，看草稿上的原因里打开的是谁：是一个私聊（名字包含群名）就在微信里给群改个名（重启机器人）；打开的就是这个群，说明标题读出来像一个认识的私聊，把「SEND_VERIFY_CHAT」和「Not sending to」那几行发回来（日志里只有编号前 8 位，没有名字）。群名很长、标题被截断时也看一次」。

---

## C. 给 PRM 那一部分和协调者

1. **`wechat_now`**：`def wechat_now(self) -> Optional[float]`。和 spec 有一处不一样：每个会话先取 `last_timestamp`（它最后一行的时间，微信写的），没有才用 `session_time`（`sort_timestamp` 在前）。`sort_timestamp` 是会话列表的排序键：草稿、置顶可能挪它，要是这台电脑上留了草稿，它可能是这台电脑的钟，正是 F2 要躲的。假库只有 `sort_timestamp`，所以测试里就是 `session_time`。要严格照 spec 只用 `session_time`，删两行就行，告诉我。
2. **`boss_groups` 读不了的项**：`{"chat": 群编号, "unreadable": True}`，只有这两个键，没有 `name` / `members` / 时间；按它会话的时间排在列表里。三种情况：成员报错误码（真客户端的 `get_group_members` 出错时其实给空列表，所以主要是第二种）、一个成员都没有（新群的成员微信可能还没写进库）、行读不了（`_history_of` 是 None）。每次调用都会再给，直到读得出。数据库本身读不了照旧是 None。这个分支里的 PRM 还是旧的：拿到这样的项会当成「没有老板」记进 `_looked`（不崩），PRM 那边的修复跳过它们以后才对。
3. **F10 的范围**：拒发只看「标题不带人数、`_ocr_key` 是认识的私聊名」。「标题就是群名、打开的是一个人」能认出来的只有：同名（`_name_key`）的人，`NAMESAKE` 在打字前就拒了；只差表情 / 符号的人（`_ocr_key` 一样），`NOT_A_GROUP`。不认识的人光看标题分不出（群的人数也可能没读到），照旧发，这和功能 U 以前一样。被「…」截断的标题照旧按 `_ocr_key` 比（和 `other` 一样），没有另外的规矩。
4. **F12**：「Not sending to」是同一行日志，私聊发送拒发时也不再写名字（发送、拒发的判断都没变；`tests/test_same_name_chats.py` 只查 `NAMESAKE` 那一行，没动它）。还带名字的：`NAMESAKE` 的「Not using the WeChat window for …」（以前就有，测试查它的原因）、INFO「the open chat is '…', as asked」「could not be confirmed (read '…')」（CLAUDE.md 手动验证第 5、11 条靠它们）、窗口那边的「Send failure: group='…'」（`window_controller.py`，不在这次的文件里）。要都去掉名字的话告诉我。
5. **F11**：没改代码，限制写在 A.5。

## D. 跑过的测试

- `tests/test_boss_groups_backend.py tests/test_send_verify_chat.py`：148 passed。
- 指定的那些（`tests/test_boss_groups_backend.py tests/test_send_verify_chat.py tests/test_same_name_chats.py tests/test_wcdb_backend*.py tests/test_new_friends.py tests/test_three_party.py tests/test_boss_groups_e2e.py tests/test_prm_boss_groups.py`）：378 passed。
- 别的用到 `WcdbBackend` 的 19 个测试文件加 `tests/test_router_business.py`：687 passed, 14 subtests passed。
- 私聊发送的判断：新旧 `title_verdict` 在 2052 种（标题、名字、认识的聊天）组合上一个都没变。
