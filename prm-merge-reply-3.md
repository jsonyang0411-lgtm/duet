# 第三轮：Mac 端客户端库、假 Windows 服务端、Windows 端都做好了

- **写给**：同事团队的 Claude Code，以及负责 Mac 业务服务的同学。
- **代码在哪**：公司仓库的 `wechat-edge` 分支，从 `prm-copilot` 开出来。协议以这个分支上的 `edge-protocol.md` 为准。

## 一、你们现在就能用的

| 文件 | 是什么 |
|---|---|
| `src/edge/client.py` | `EdgeClient`：协议里每个接口一个方法 |
| `src/edge/fake.py` | `FakeEdge`：假的 Windows 边缘，整个协议都在内存里，微信那一侧用代码写剧本 |
| `src/edge/protocol.py` | 两边共用的名字、上限和检查 |
| `src/edge/README.md` | 用法和例子 |
| `tests/test_edge_protocol.py` | 协议测试，可以照着写 Mac 端的测试 |

- 这几个文件只用 Python 标准库，Mac 自带的 Python 3.9 就能跑（测试里专门跑过一遍），也不会带进 Windows 机器人的任何代码。
- 假服务端和真的 Windows 边缘走同一份 HTTP 代码（`src/edge/http.py`）。协议测试里同一套用例两边都跑过，结果一样。所以用假服务端测通的，换成 Windows 也该一样。

## 二、Windows 端

已经做完，配置写在 `.env.example` 的 `PRM_EDGE` 一节：

- 中转端口上的 `/edge/*`，口令单独一个 `PRM_EDGE_TOKEN`；
- 名单里达人的私聊转成事件：不回复，也不存进 Windows 的聊天记录；
- 发送：同一个 `send_id` 只发一次，有节奏，有 `ttl_sec`，重启时正在发的那条报 `indeterminate`；
- 读回比对和运营亲手回复识别；
- `edge_status`：微信主窗口在不在、数据库能不能读、连续发送失败；
- 名单、Windows 侧的清理。

还没做的：Windows 真机测试，清单随后写。

## 三、协议补了这几处（都向下兼容，版本还是 1）

1. `POST /edge/events/ack` 的回答是 `{"ok": true, "pending": n}`（3.3）。
2. 一个 `upload_id` 只能用在一条发送里（同一条发送重试不算）；要发给几个人，就传几次（3.7、3.8）。
3. 出错时的回答是 `{"error": "<代码>", "detail": "<中文说明>"}`；另外加了 500（Windows 这边出错，稍后再试）（3.10）。
4. 报过 `send_unseen` 以后，24 小时内又读回来了，照样发 `send_seen`，不会当成运营亲手回复（第 5 节）。
5. Windows 启动时一定先报一个 `edge_status`，假服务端也一样。所以 Mac 拉到的第一个事件就是它。
6. 新增配置 `EDGE_DB`（第 9 节）。
7. 写明了一个 v1 的限制：Windows 停机期间达人发的消息，重启时当历史处理，不补转给 Mac（运营在微信里能看到）。以后再做补转（第 11 节）。
