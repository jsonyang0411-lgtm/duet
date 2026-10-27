# src/edge：Windows 微信边缘

协议说明在仓库根目录的 `edge-protocol.md`，以那个文件为准。这里说的是代码怎么用。

## 给 Mac 端用的三样

都只用 Python 标准库，Python 3.9 就能跑（测试里专门用 Mac 自带的 `/usr/bin/python3` 跑过一遍）。

| 文件 | 是什么 |
|---|---|
| `client.py` | `EdgeClient`：协议里每个接口一个方法。拒绝时抛 `EdgeError`（带 `status`、`code`、`detail`、`body`）；网络错误（`OSError`）留给调用方退避重试。不走系统代理 |
| `fake.py` | `FakeEdge`：假的 Windows 边缘，整个协议都在内存里实现，微信那一侧可以用代码写剧本 |
| `protocol.py` | 两边共用的名字、上限和检查 |

`http.py` 是 `/edge/*` 的 HTTP 处理。真的 Windows 边缘和假的都走这一份，所以假服务端的行为就是 Windows 的行为。

## 客户端的用法

```python
from src.edge.client import EdgeClient, EdgeError

edge = EdgeClient("http://<Windows 的局域网 IP>:8790", token)    # PRM_EDGE_TOKEN；不在同一个网络时用 Windows 的 Tailscale 地址（cross-network.md）
edge.put_parties(version, wxids)          # 名单有变化时推；另外 edge.health()["parties_version"] 和自己的对不上时也推

cursor = load_cursor()                    # Mac 已经存好的最后一个事件编号
while True:
    events = edge.events(after=cursor, wait=25)
    for ev in events:
        for f in ev.get("files", []):
            if not f.get("missing"):
                edge.download(ev["id"], f["n"], folder / f["name"])
        store(ev)                         # 按 ev["id"] 去重：事件可能来不止一次
        cursor = ev["id"]
    if events:
        save_cursor(cursor)
        edge.ack(cursor)                  # 事件和附件都存好以后才确认

up = edge.upload("/path/brief.pdf")
edge.send("pk-88", "wxid_a", "样品这周五前寄出～", uploads=[up["upload_id"]])
# 结果看事件：send_result（sent / failed / expired / indeterminate）、send_seen、send_unseen
```

## 假服务端的用法（写 Mac 端的测试）

```python
from src.edge.fake import FakeEdge

with FakeEdge(auto_send=False) as fake:   # 在 127.0.0.1 上找一个空端口起服务
    edge = EdgeClient(fake.url, fake.token)
    edge.put_parties(1, ["wxid_creator01"])
    fake.creator_says("wxid_creator01", "你好，我想申请寄样",
                      files=[("图片-1.jpg", picture_bytes)])
    fake.creator_says("wxid_creator01", "这个颜色还有吗？",
                      quote={"text": "这是新款的色卡～", "from": "us", "server_id": "77"})
    # ……Mac 端拉事件、起草、审批、edge.send("pk-1", …)
    fake.complete("pk-1")                 # 发出去了：send_result sent
    fake.seen("pk-1", delay_s=3)          # 读回来了：send_seen
    fake.operator_says("wxid_creator01", "我来回")    # 运营亲手回复：operator_message
    fake.notice("wxid_creator01", "对方撤回了一条消息")   # chat_notice
    fake.set_status(wechat="offline", detail="找不到微信主窗口")   # edge_status
    assert fake.sent() == [("wxid_creator01", "…", [])]
```

还能模拟这些情况：

- `fake.complete(send_id, "failed", "找不到聊天窗口")`：发送失败；
- `fake.unseen(send_id)`：读回时限过了还没对上；
- 在 `fake.complete` 之前调 `fake.seen(send_id)`：读回比发送动作返回还早（Windows 真机上常见）。和 Windows 一样，先记下，`complete` 报完 `send_result` 紧接着报 `send_seen`（`delay_s` 为 0）；同一条只报一次，没发出去的（`failed` 等）不报；
- `fake.start_sending(send_id)` 再 `fake.restart()`：发送进行中时 Windows 重启了，结果是 `indeterminate`；
- `fake.expire_due()`：发送请求过了 `ttl_sec`；
- `fake.fail_next(n, 原因)`：接下来 n 条发送失败。

`auto_send=True`（默认）时，收到发送请求马上就算发出去，并且马上读回。

## 协议测试

`tests/test_edge_protocol.py` 是一套用例，通过客户端跑在每一个 Windows 边缘上：现在是假服务端，Windows 端做好以后也跑真的。Mac 端可以照着这些用例写自己的测试。

```
python -m pytest tests/test_edge_protocol.py -q
```
