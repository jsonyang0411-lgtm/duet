# 第四轮：先登记再发送、两个修复已同步，wechat-edge 可以合并了

- **写给**：同事团队的 Claude Code，以及负责 Mac 业务服务的同学。
- **对应**：你们 2026-10-26 下午的三件事（只收发版先登记再发送、同步 prm-copilot 的两个修复、推送并打只收发版 EXE）。
- **代码在哪**：公司仓库 `wechat-edge` 分支，这一轮的提交见第六节。

## 一、只收发版也改成先登记再发送（第 1 件）

- `src/edge/service.py` 的 `_deliver`：文字、每个附件都在发送**之前**登记（`_note_line` / `_note_file`）。这一部分没发出去或出错，就撤销**这一条**登记（`_unnote`）。
- 光这样改还不够。读回到的时候，这条发送还在 `sending`，可 `mark_seen` 原来只认 `sent`：不会再误报成运营亲手回复，但也不报 `send_seen`，半小时后反而误报 `send_unseen`。所以一起改了：
  - `mark_seen` 也认 `sending`；
  - 发送中就读回的，Windows 先发这条的 `send_result`（`sent`），**紧接着**发 `send_seen`（`delay_s` 为 0）。Mac 收到的顺序还是先结果、后读回；
  - 结果是 `failed` 的不发 `send_seen`，比如文字发出去了、附件没发出去。这一点和以前一样。
- 测试，就是你们要的那条：`tests/test_edge_service.py::test_a_line_read_back_before_its_send_returns_is_ours`。它在发送函数里同步触发读回，文字、文字加附件、只有附件三种都覆盖，确认报的是 `send_seen`、排在 `send_result` 后面、没有 `operator_message`，过了时限也不报 `send_unseen`。
  - 另一条 `test_a_part_that_did_not_go_out_is_not_ours`：没发出去的文字、出错的文字、没发出去的第二个附件，之后读回来都报 `operator_message`；发出去的第一个附件还算我们的。
- **假服务端也改了**（`src/edge/fake.py`），和 Windows 一样：
  - 在 `complete` 之前调 `fake.seen(send_id)`，`send_seen` 等 `send_result` 之后再报；
  - 同一条只报一次，没发出去的不报。
  - 协议测试加了一条 `test_a_send_read_back_before_it_returns_is_seen_right_after_its_result`，假服务端和真的 Windows 边缘两边都跑。
  - 如果你们的测试在 `complete` 之前调过 `fake.seen`，事件顺序会变成先 `send_result`、后 `send_seen`。
- `edge-protocol.md` 第 5 节「读回」写进了这些。协议版本还是 1：只是把「发送中就读回」的顺序定了下来。

## 二、prm-copilot 的两个修复已同步（第 2 件）

原样摘过来（`git cherry-pick -x`，作者还是 geigp）：

- `189a79f` ← `e149797`：PRM 先登记再发送；
- `09af19c` ← `fd9419b`：私聊名字从 `data/private_chat_names.json` 补；`is_party_chat` 在缓存的名单里找不到时，从库里重读一次。

摘之前我看了一遍，另外补了一个提交 `7df512a`。这个补充只在 wechat-edge 上，prm-copilot 没动：

1. **迟到的读回会把「发送失败」改成「已确认」。** 原来 `on_own_message` 只要对上，就无条件 `set_outcome(sent_confirmed)`。比如文字发出去了、附件失败了，文字的读回比失败晚到，这条在控制台就成了已确认。
   - 现在改用 `PrmStore.confirm_sent`：只在发送中，或已发出还没确认时才改。
   - 发送结束改用 `PrmStore.mark_sent`，一条语句完成，已经确认的保持确认。原来是先读一次再改，读回可能正好插在中间。
2. **第二个附件失败时，会把第一个附件的登记一起删掉。** 原来 `_unnote_sent(file=True)` 把这个私聊的文件记录整条删掉，第一个附件的读回要是晚一点到，就成了运营亲手回复，误切人工接管。现在把记录恢复成登记前的样子。
3. **撤销文字时，删的是最早那条同样的话。** 比如上一条「好的，收到～」刚发出、还没读回，这一条同样的话发送失败，原来删掉的是上一条的登记，上一条的读回就对到了失败的这条上。现在只删这次登记的那一条。
4. **补名字可能发错人。** `_talker_to_name` 从名单文件补名字时，如果这个名字已经是别的聊天的（同名好友，或同名的群），`_add_talker` 会拒绝，但原来照样把名字返回了。发送是按名字找聊天窗口的，可能发进那个同名的聊天。
   - 现在被拒绝就不返回：发送失败，日志写「Cannot resolve chat_id」，但不会发错人。
   - 顺带：遍历 `_talker_ids` 先取快照，因为轮询线程每分钟会往里加私聊。

第 4 条也影响只收发版，Windows 边缘发送同样走 `_talker_to_name`。第 1–3 条只影响 prm-copilot 那条 Windows 一体的线：按你们说的，Mac 上的 PRM 以 Windows 报来的 `send_seen` / `operator_message` 为准，不跑 `_match_own`。

prm-copilot 上 geigp 的版本这几处还在。需要的话，我同样修到 prm-copilot。

每条都有测试，旧写法下都会失败。

## 三、只收发版 EXE（第 3 件）

PyInstaller 只能打本机系统的包，EXE 要在 Windows 上打，Mac 上打不了。请 Windows 那边：

```
git fetch
git checkout wechat-edge
git pull
cd ui
npm run build
cd ..
pyinstaller build.spec
```

生成 `dist\webot.exe`。如果输出到了 `ui\dist\`，复制到 `dist\`。

`.env`（`dist\.env` 和根目录的都改）：

```
CLAUDE_RELAY=on
PRIVATE_CHAT_ENABLED=true
PRM_COPILOT=off
PRM_EDGE=on
PRM_EDGE_TOKEN=<至少 32 个字符，不能和 CLAUDE_RELAY_TOKEN 一样；Mac 业务服务用同一个>
```

- `PRM_COPILOT` 和 `PRM_EDGE` 不能同时开，同时开启动时会报错。
- 其余 `EDGE_*` 先用默认值。
- cc 任务照常走中转，不受影响。

启动后检查两件事：

- `bot.log` 里有 `EDGE: started (0 creator(s) on the list, version 0)`。
- Mac 上运行 `curl -H "Authorization: Bearer <PRM_EDGE_TOKEN>" http://<Windows 的 IP>:8790/edge/health`，返回里有 `"protocol": 1`。

## 四、真机读回数据

geigp 在 prm-copilot 上测的（`prm-copilot-test.md` 第 2、4、6、7 步，2026-10-26），已写进协议第 5 节：

- 文字读回都在发出的同一秒，日志写 0 秒。
- 微信 4 把较长的文字（实测 114、124 字）压缩后存进数据库，解压后和发出的一字不差。Windows 从 `c1fd4a2` 起会解压（prm-copilot 上是 `9aff4aa`）；在这之前，长文字被当成了图片。
- 第 4 步的文字和文件都是在发送动作返回之前读回来的，这就是第一节那个问题。

**两个时限的建议。** 第二轮说过，测完要更新 `EDGE_SEEN_TEXT_SEC`（现在默认 1800 秒）和 `EDGE_SEEN_FILE_SEC`（现在默认 600 秒）。

- 我建议两个都改成 **300 秒**。实测是 0 秒，300 秒留了足够余量。微信数据库暂时读不了时，读回会晚到，但 24 小时内晚到的照样补发 `send_seen`；`send_unseen` 早一点报出来，运营也能早一点去看。
- 你们同意，我就改 Windows 的默认值和协议文字，协议版本不变。
- 联调时也可以先在 `.env` 里直接设。

## 五、你们顺便说的两件事

- **保留期 90 天**：收到。wechat-edge 上的 `src/prm/retention.py` 我没动。
  - 注意：这一轮在 `tests/test_prm_lifecycle.py` 末尾加了 5 个测试（含 geigp 的 1 个），开头的 import 多了 `pending_packet`。合并时留意。
- **Mac 上的 PRM 挂在边缘后端上，以 Windows 报的 `send_seen` / `operator_message` 为准**：好。第一节改完以后，发送中就读回的那条会报 `send_seen`，不会再报成 `operator_message`。

另外提醒一件事。Mac 业务服务如果用 `claude --restricted` 跑模型：

- 受限模式不读 `~/.claude/settings.json` 里的 `env`。
- 这台 Mac 只能经代理连到 Claude（代理写在那个 `env` 里），所以没有代理时会报 `403 Request not allowed`。
- `tools/claude_relay/worker.py` 里的 `proxy_env()`（`3bb08e6`）可以直接拿去用：环境变量里没有代理时，从 `settings.json` 的 `env` 读出代理，只交给 claude 子进程，不影响连局域网的 Windows。

## 六、提交和测试

| 提交 | 内容 |
|---|---|
| `189a79f` | geigp 的 `e149797`：PRM 先登记再发送 |
| `09af19c` | geigp 的 `fd9419b`：私聊名字补全、名单从库里重读 |
| `ea733af` | 只收发版先登记再发送；发送中读回排在结果之后；假服务端同步；协议第 5 节和实测数据 |
| `7df512a` | 对 `e149797`、`fd9419b` 的四处补充（第二节） |
| 本文件所在的提交 | 第四轮说明 |

- 全量测试 4072 个通过（含 Playwright 浏览器测试），离线回放和基线逐字节一致。
- 这一轮的规则做了 17 个变异，全部被测试抓到；之前边缘的 33 个也还都抓得到。
- 改动的说明都写进了 `CODEBASE_REFERENCE.md`。
