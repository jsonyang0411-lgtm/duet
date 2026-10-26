# Windows 微信边缘协议 v1（定稿）

- **用途**：Windows（微信这一侧，下面叫「边缘」）和 Mac（业务服务）之间的接口。Windows 开服务，Mac 来拉。
- **版本**：`protocol: 1`。2026-10-26 定稿，已经并入同事团队第二轮的 14 条意见（见 `prm-merge-reply-2.md`）。
- **实现**：
  - Windows 端由我们做，在公司仓库的 `wechat-edge` 分支（从 `prm-copilot` 开出来）。
  - 同时提供给 Mac 用的客户端库和假 Windows 服务端，只用 Python 标准库。
- **以本文件为准**。不兼容的改动要升 `protocol` 的版本号，并且双方都同意。

## 1. 谁管什么

**Windows（边缘）**
- 读名单里的私聊。
- 解图（微信 4 的 `.dat`）、语音转文字、取文件。
- 把消息写进事件流。
- 按 Mac 的发送请求发消息，同一个 `send_id` 只发一次。
- 执行账号级节奏。
- 读回比对，识别运营亲手回复。
- 报告自身状态。
- 本机不长期保存达人的内容。

**Mac（业务服务）**
- 起草（受限的 Claude）、审核、自动放行、「稍等」、接管状态。
- 审批单、聚水潭、小红书。
- 号码占位符、业务库、90 天清理。
- 维护达人名单。
- 控制台：只在这台 Mac 上用，只监听 127.0.0.1。

**名单以外的私聊和群聊**：照旧按 Windows 机器人现有的配置处理（`PRIVATE_CHATS`、`CLAUDE_RELAY_ONLY` 等），这个协议不管。

**名单里达人的私聊**：机器人永远不会自动回复，转发出错时也不会。

**管理员的 cc 中转**：不受影响，接口、口令都不变。

## 2. 连接

- **服务位置**：跑在 Windows 现有的中转服务上，同一个端口（默认 8790），路径前缀 `/edge/`。
  - 所以 Windows 要 `CLAUDE_RELAY=on`。
  - 还要 `PRIVATE_CHAT_ENABLED=true`（达人在私聊里）。
  - `PRM_EDGE=on` 和 `PRM_COPILOT=on`（旧的、在 Windows 上做业务的版本）不能同时打开。
- **口令**：Windows 的 `.env` 里设 `PRM_EDGE_TOKEN`，请求头写 `Authorization: Bearer <口令>`。
  - `/edge/*` 只认这个口令，`/relay/*` 只认 cc 的 `CLAUDE_RELAY_TOKEN`，两个不能互相代替。
  - 口令不对一律返回 401。
- **格式**：HTTP；请求和回答是 UTF-8 的 JSON；文件是原始字节。
- **网络**：
  - Windows 防火墙的 8790 端口只放行 Mac 的 IP。
  - TLS 暂时不做，所以达人的号码和地址会以明文经过局域网。
- **时间**：
  - 协议里只用相对时长（秒），不要求两台机器对时。
  - 事件里的 `at` 是 Windows 记下的时间（Unix 秒），只用来显示和参考。

## 3. 接口一览

```
GET  /edge/health
GET  /edge/events?after=<id>&wait=25&limit=50
POST /edge/events/ack                     {"upto": <id>}
GET  /edge/events/<id>/files/<n>
PUT  /edge/parties                        {"version": <n>, "wxids": [...]}
GET  /edge/private-chats
POST /edge/uploads?name=<文件名>           原始字节
POST /edge/sends                          {"send_id", "chat_id", "text", "uploads", "ttl_sec"}
GET  /edge/sends/<send_id>
```

### 3.1 `GET /edge/health`

```json
{"ok": true, "protocol": 1, "parties_version": 3,
 "wechat": "online", "db": "ok", "send": "ok", "detail": "",
 "can_send_files": true, "events_pending": 12}
```

- `parties_version`：Windows 现在用的名单版本，0 表示没有名单。和 Mac 自己的版本对不上（比如 Windows 重装过、库丢了），Mac 就重新推一次名单。
- `wechat`、`db`、`send`：含义见第 8 节。
- `events_pending`：还没确认的事件有几个。

### 3.2 `GET /edge/events?after=<id>&wait=25&limit=50`

- 返回编号大于 `after` 的事件，按编号从小到大：`{"events": [...]}`。
- 没有新事件就最多等 `wait` 秒（上限 30），然后返回空列表。
- `limit` 最大 200。
- 已经给过、但还没确认的事件会再给，Mac 按 `id` 去重。

### 3.3 `POST /edge/events/ack`

- 请求体：`{"upto": <id>}`，表示编号小于等于 `upto` 的事件，Mac 都已经存好了。
- 回答：`{"ok": true, "pending": <还没确认的事件数>}`。
- 确认是累积的。Mac 要先把事件（**连同附件**）存好，再确认。
- 确认以后，事件和附件在 Windows 上再留 7 天就删。7 天内还能再取，用来补救。

### 3.4 `GET /edge/events/<id>/files/<n>`

- 返回附件的原始字节。
- 没有这个附件，或者已经删了，返回 404。

### 3.5 `PUT /edge/parties`

- 请求体：`{"version": 4, "wxids": ["wxid_a", "wxid_b"]}`。名单整份替换，Mac 是唯一写名单的一方。
- **版本规则**：
  - 比 Windows 现有的大：生效，返回 200。
  - 和现有的相等、内容也一样：返回 200（重试是安全的）。
  - 和现有的相等但内容不同，或者比现有的小：返回 409，回答里带 `current_version`。
- **不收的 wxid**：群聊 id、格式不对的、`ADMIN_WXID`、`CLAUDE_RELAY_WXIDS` 里的。
  - 这些写进 `rejected: [{"wxid", "reason"}]`，其余照样生效。
- **Windows 的处理**：
  - 名单存在本地，Mac 不在线时也照样按名单读私聊。
  - 从名单里去掉的人，私聊不再转发；还在排队、发给他的消息按 `failed`（「不在名单里」）处理。

### 3.6 `GET /edge/private-chats`

- 返回：`{"chats": [{"wxid", "name"}]}`。这是机器人号最近的私聊，供 Mac 控制台登记时选人。
- 已经在名单里的，以及管理员和 cc 用户，不列出。

### 3.7 `POST /edge/uploads?name=<文件名>`

- 请求体是文件的原始字节，要带 `Content-Length`，最大 100 MB。
- 返回：`{"upload_id", "size", "sha256"}`。
- 一个 `upload_id` 只能用在一条发送里（同一条发送重试不算）；要发给几个人，就传几次。
- 24 小时内没被任何发送请求用到，就删掉。用过的，在那条发送结束 7 天后删掉。

### 3.8 `POST /edge/sends`

请求：

```json
{"send_id": "pk-88", "chat_id": "wxid_a", "text": "好的，样品这周五前寄出～",
 "uploads": ["up-123"], "ttl_sec": 1800}
```

- **`send_id`**：Mac 生成，1–64 个字符，只能用字母、数字和 `_-.:`。
  - 同一个 `send_id` 只发一次：重复提交、内容相同，返回现在的状态；内容不同，返回 409。
- **`chat_id`**：必须在名单里，否则返回 403。
- **`text`**：最长 1500 字，是要发出去的成品文字（号码已经换回真实的）。只发附件时可以为空。
- **`uploads`**：最多 9 个，只能是 `/edge/uploads` 返回的、还没用在别的发送里的编号。
- **`ttl_sec`**：60 到 86400，默认 1800。这么久还没发出去，就不发了（`expired`）。

返回：`{"send_id", "status": "queued"}`。

### 3.9 `GET /edge/sends/<send_id>`

- 返回：`{"send_id", "status", "error", "sent_at", "seen_at"}`。
- Mac 重启以后对账用。正常情况看事件就够了。

### 3.10 错误码

出错时回答的格式是 `{"error": "<代码>", "detail": "<中文说明>"}`，409 冲突时还带 `current_version`。

| 码 | 什么时候 |
|---|---|
| 400 | 请求格式不对：缺字段、太长、不认识的 `upload_id` |
| 401 | 口令不对 |
| 403 | 发给不在名单里的私聊 |
| 404 | 没有这个事件、附件或发送请求 |
| 409 | `send_id` 已经有了但内容不同；名单版本冲突 |
| 413 | 文件超过 100 MB |
| 500 | Windows 这边出错了（`detail` 里有错误类型），稍后再试 |
| 503 | Windows 正在停机，稍后再试 |

## 4. 事件

所有事件都有 `id`（一直增加）、`type`、`at`。

### 4.1 类型

| type | 字段 | 什么时候 |
|---|---|---|
| `creator_message` | `chat_id`、`message_id`、`msg_type`、`text`、`quote`（可选）、`files` | 名单里的达人发来一条消息 |
| `operator_message` | `chat_id`、`message_id`、`msg_type`、`text` | 这个微信号在达人私聊里发了一条，而且和发送记录对不上（运营亲手发的，电脑上或手机上） |
| `chat_notice` | `chat_id`、`text` | 私聊里的系统提示，比如「对方撤回了一条消息」，原样报 |
| `creator_message_recalled` | `chat_id`、`message_id` | **预留，v1 不发**。摸清微信 4 撤回在数据库里长什么样以后再实现；在那之前，撤回会以 `chat_notice` 报 |
| `send_result` | `send_id`、`status`、`error` | 一条发送有了结果：`sent`、`failed`、`expired`、`indeterminate` |
| `send_seen` | `send_id`、`delay_s` | 发出去的这条在微信里读回来了 |
| `send_unseen` | `send_id`、`window_s` | 读回时限过了还没对上（第 5 节） |
| `edge_status` | `wechat`、`db`、`send`、`detail` | Windows 状态有变化时报；Windows 启动时也报一次 |

- `message_id` 是微信消息的编号，Windows 已经按它去过重。
- 同一个私聊的事件按时间顺序给。

### 4.2 `text` 的写法

`text` 是**原文**，号码不打码。Mac 在拼给 Claude 的提示之前、写日志之前，自己换成占位符或打码。

| 微信消息 | `text` |
|---|---|
| 文字 | 原文 |
| 图片 | `[图片]`，附件是原图；没解开原图时用聊天窗口截图（`聊天截图-n.jpg`），文字里写明 |
| 语音 | `[语音] 转写`；转不出来写 `[语音]（没转出文字）` |
| 视频 | `[视频]` |
| 表情 | `[表情]` |
| 文件 | `[文件] 文件名`，附件是文件；微信还没下载完的，`files` 里写明原因 |
| 链接 | `[链接] 标题` |
| 小程序 | `[小程序] 标题` |
| 聊天记录 | `[聊天记录] 标题` |
| 名片 | `[名片] 昵称` |
| 位置 | `[位置] 地名` |
| 转账、红包 | `[转账]`、`[红包]` |
| 引用回复 | 达人回复的文字；另外带 `quote` |
| 其他 | `[消息]`，或 `[消息类型:N]` |

**不会丢消息**：不认识的类型也会照样发一个事件。

**`quote` 的格式**：`{"text": "被引用那句的文字", "from": "us", "server_id": "…"}`。
- `from` 是被引用那句是谁说的：`us`（这个微信号）、`creator`（达人）或 `other`。

**`files` 的格式**：`[{"n": 1, "name": "图片-1.jpg", "size": 84213, "kind": "image"}]`。
- 取不到的写成 `{"n": 2, "name": "合同.pdf", "missing": "电脑微信还没下载这个文件"}`。

### 4.3 例子

```json
{"id": 1201, "type": "creator_message", "at": 1761462000, "chat_id": "wxid_a", "message_id": "…",
 "msg_type": 1, "text": "这个颜色还有吗？", "quote": {"text": "这是新款的色卡～", "from": "us", "server_id": "…"},
 "files": []}
{"id": 1202, "type": "operator_message", "at": 1761462060, "chat_id": "wxid_a", "message_id": "…",
 "msg_type": 1, "text": "有的，我让同事给你寄"}
{"id": 1203, "type": "send_result", "at": 1761462100, "send_id": "pk-88", "status": "sent", "error": ""}
{"id": 1204, "type": "send_seen", "at": 1761462103, "send_id": "pk-88", "delay_s": 3}
{"id": 1205, "type": "edge_status", "at": 1761462200, "wechat": "online", "db": "ok", "send": "failing",
 "detail": "连续 3 条没发出去：找不到聊天窗口"}
```

## 5. 发送、读回、运营亲手回复

### 发送

- **顺序**：同一时间只发一条，按收到请求的顺序。一条请求里先发文字，再按顺序发附件；遇到第一个失败就停，剩下的不发。
- **状态变化**：`queued → sending → sent | failed | expired | indeterminate`。每到一个最终状态，就发一个 `send_result` 事件。
  - `expired`：`ttl_sec` 内没发出去。比如 Windows 停了很久，这条回复已经过时了。
  - `indeterminate`：处在 `sending` 时 Windows 停了或重启了，不知道发出去没有。**不会自动重发**，由 Mac 让运营去微信里看一眼再决定。
- **节奏**（Windows 执行，可以配置）：
  - 两条之间至少隔 `EDGE_SEND_MIN_GAP_SEC` 秒（默认 3）；
  - 每小时最多 `EDGE_SEND_PER_HOUR` 条（默认 60），这个上限只算 `/edge` 的发送。
  - 被节奏挡住的请求留在 `queued`，直到 `ttl_sec` 用完。
- **Mac 的节奏**：模拟打字的等待、夜间不主动发，这些由 Mac 自己控制，等够了再提交发送请求。

### 读回

- Windows 在每一部分（文字、每个附件）**发出之前**就记下它和时间（文字也存在库里，重启也在）；这一部分没发出去或出错，就撤销这条记录。微信几毫秒就把发出的消息写进数据库，读回常常比发送动作返回还早；先发后记的话，这条会被当成运营亲手回复。
- 这个微信号在达人私聊里的消息读回来时，先和记录比对：
  - **文字**：去掉多余空白以后完全一样，而且在 `EDGE_SEEN_TEXT_SEC`（默认 1800）秒内；
  - **只有附件的发送**：`EDGE_SEEN_FILE_SEC`（默认 600）秒内读回了这个号发的文件或图片。
- 对上了，发 `send_seen`；时限过了还没对上，发 `send_unseen`。
- 发送还没结束就读回来的，也算对上。Windows 先发这条的 `send_result`（`sent`），紧接着发 `send_seen`（`delay_s` 为 0），所以 Mac 收到的总是先 `send_result` 后 `send_seen`。结果是 `failed` 的（比如文字发出去了、附件没发出去），不发 `send_seen`。
- 报过 `send_unseen` 以后，24 小时内又读回来了，照样发 `send_seen`，不会当成运营亲手回复。
- 读回比对只认还没对上的发送：已经对上过的话，运营再打一遍，就是运营亲手回复。
- **真机实测**（2026-10-26，`prm-copilot-test.md` 第 2、4、6、7 步，Windows 电脑微信 4）：
  - 文字都在发出的同一秒读回（日志写 0 秒）；第 4 步的文字和文件都是在发送动作返回之前就读回来的。
  - 微信 4 把较长的文字（实测 114、124 字）压缩后存进数据库，解压后和发出的一字不差（Windows 从 `c1fd4a2` 起会解压，prm-copilot 上是 `9aff4aa`）。
  - 两个时限的默认值先不改：微信数据库暂时读不了的时候，读回会晚，留足余量。

### 运营亲手回复

- 和记录对不上的，就是运营亲手发的，发 `operator_message`（原文）。
- 运营在手机上发的消息，只要电脑微信当时在线，会同步进电脑的数据库，同样发 `operator_message`。
- 读回来时已经是 5 分钟以前的消息，当历史处理，不报。这种情况包括 Windows 刚启动，或电脑微信离线后才同步过来。
- **接管与否由 Mac 决定**：比如人工接管 2 小时、作废等着的草稿。Windows 不管。

## 6. Mac 不在线时

- Windows 照常读名单里的私聊，把消息写进事件流等着，不会自己回达人（「稍等」也不会）。
- 没确认的事件最多留 30 天。
- Mac 回来以后从自己的 `after` 接着拉，按顺序补处理；过了 `ttl_sec` 的发送请求，按 `expired` 处理。

## 7. 保存和清理

- **Windows 不长期保存达人的内容**：
  - 达人的消息不写进 Windows 的聊天记录库（转发步骤在 `persist` 之前，靠事件按 `message_id` 去重）。
  - 决策记录不写内容。
  - 日志只写编号和字数。
  - 同事团队 10-25 在 router、wcdb_backend、decision_trace 上做的日志打码，合并时保留。
- **事件和附件**：确认后 7 天删（`EDGE_KEEP_DAYS`），只删 Windows 自己存的副本。大文件是微信自己下载的原文件，Windows 不删。
- **上传**：24 小时没用到就删；用过的随发送结束 7 天后删。
- **90 天业务清理在 Mac**：从「处理完」起算，即签收、结案、审批单驳回或过期中最晚的那个；另外，90 天没有任何动静的合作单也清。
- **管不到的**：微信客户端自己的聊天记录（Windows 和手机上），聚水潭、小红书里的订单数据。

## 8. Windows 状态（`edge_status`）

| 字段 | 值 | 什么时候 |
|---|---|---|
| `wechat` | `online` / `offline` | 找不到微信主窗口（没开、没登录、被关了）时是 `offline` |
| `db` | `ok` / `unreadable` | 微信数据库连续读不了时是 `unreadable`。这时读不到私聊，达人的消息收不进来 |
| `send` | `ok` / `failing` | 连续 3 条发送失败时是 `failing`；下一条发成功就回到 `ok` |
| `detail` | 文字 | 原因，比如「连续 3 条没发出去：找不到聊天窗口」 |

- 状态有变化时报，Windows 启动时也报一次。`/edge/health` 随时能查当前状态。
- 微信弹出风控验证这类窗口，v1 还认不出来，只会表现为 `send: failing`。以后能识别时，`wechat` 会多一个值 `verify`。

## 9. Windows 的配置（`.env`）

```
PRM_EDGE=on                    # 打开边缘（要 CLAUDE_RELAY=on、PRIVATE_CHAT_ENABLED=true；不能和 PRM_COPILOT 同时开）
CLAUDE_RELAY_ONLY=on           # 除了 cc 任务和名单里的达人，别的消息一律不回（名单以外的私聊不自动回复）
PRM_EDGE_TOKEN=…               # 至少 32 个字符，和 CLAUDE_RELAY_TOKEN 不同
EDGE_DB=data/edge.db           # 事件、发送、上传、名单
EDGE_SEND_MIN_GAP_SEC=3
EDGE_SEND_PER_HOUR=60
EDGE_SEEN_TEXT_SEC=1800
EDGE_SEEN_FILE_SEC=600
EDGE_KEEP_DAYS=7
```

## 10. 测试

- **协议测试，两边跑同一套用例**：
  - Windows 这边：用真的边缘服务跑客户端库；
  - Mac 那边：用客户端库跑假服务端。
- **假 Windows 服务端**：在内存里跑，可以写剧本：塞入达人消息、运营亲手回复、撤回提示、状态变化；记下 Mac 发出的请求；模拟发送失败、`indeterminate`、`send_unseen`、重启。
- **真机**：边缘版的 Windows 清单，在 `prm-copilot-test.md` 基础上改。

## 11. 以后再做

- **Windows 停机期间达人发的消息**：v1 里，Windows 重启时把微信里已有的消息当历史，不补转给 Mac（运营在微信里能看到）。以后做「重启后补转最近几小时、Mac 还没收到的消息」，要先在真机上确认重启后能读到哪些消息。
- `creator_message_recalled`：要先在 Windows 真机上摸清撤回在数据库里的样子。
- 识别风控验证窗口（`wechat: verify`）。
- TLS。
- 群聊里的达人。
