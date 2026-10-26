"""Mac 端的"微信后端"：不连本机微信，通过 Windows 的微信边缘（/edge/*，协议见 edge-protocol.md）收发。

Windows 只收发微信（D18）；这台 Mac 跑业务：路由、达人模块、控制台。这个后端把 Windows 转来的达人消息
交给路由（和其他后端一样调 callback），要发给达人的话通过 Windows 发出。

回复达人要人审（D19）：callback（路由）给出的回复**不会自动发出**，交给 reply_sink 存成待审核的草稿；
只有审核通过后，业务代码调用 send_text 才真正发出去。

连接用同事写的 src/edge/client.py（只用标准库）。读到哪个事件、名单推到哪个版本，存在一个小的 JSON 文件里，
重启以后接着来。名单的来源由 set_parties_source 给（达人模块的名单），有变化就推给 Windows。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.edge import protocol as P
from src.edge.client import EdgeClient, EdgeError

from .base import AbstractWeChatBackend, MessageCallback

logger = logging.getLogger(__name__)

# 连不上 Windows 时，等多久再试：越来越长，最多 60 秒
BACKOFF_SEC = (2, 4, 8, 15, 30, 60)
# 每隔多久问一次 Windows 的状态（顺便核对名单版本）
HEALTH_EVERY_SEC = 60
# 一次最多拉多少事件、最多等多少秒
EVENTS_LIMIT = 50
EVENTS_WAIT_SEC = 25


class EdgeState:
    """读到哪个事件（cursor）、推给 Windows 的名单版本和摘要。存在一个 JSON 文件里，写的时候先写临时文件再换。"""

    def __init__(self, path: Path):
        self.path = path
        self.cursor = 0
        self.parties_version = 0
        self.parties_digest = ""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.cursor = int(data.get("cursor") or 0)
        self.parties_version = int(data.get("parties_version") or 0)
        self.parties_digest = str(data.get("parties_digest") or "")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "cursor": self.cursor, "parties_version": self.parties_version, "parties_digest": self.parties_digest,
        }), encoding="utf-8")
        tmp.replace(self.path)


def event_to_message(event: Dict[str, Any], files: List[str]) -> Dict[str, Any]:
    """Windows 的 creator_message 事件 → 路由认识的标准消息（见 base.py）。事件里没有昵称，用 chat_id 代替。"""
    chat_id = str(event.get("chat_id") or "")
    msg: Dict[str, Any] = {
        "message_id": str(event.get("message_id") or f"edge-{event.get('id')}"),
        "chat_id": chat_id,
        "group_name": "",
        "sender_id": chat_id,
        "sender_name": chat_id,
        "content": str(event.get("text") or ""),
        "msg_type": int(event.get("msg_type") or 1),
        "timestamp": int(event.get("at") or time.time()),
        "is_at_mentioned": False,
        "is_group": False,
        "edge_event_id": event.get("id"),
        "edge_files": files,
    }
    quote = event.get("quote")
    if isinstance(quote, dict) and quote.get("text"):
        msg["quoted_content"] = str(quote["text"])
        msg["is_reply_to_bot"] = quote.get("from") == "us"
    return msg


class EdgeBackend(AbstractWeChatBackend):
    def __init__(
        self,
        config: Any = None,
        *,
        client: Optional[EdgeClient] = None,
        state_path: Optional[str] = None,
        files_dir: Optional[str] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        events_wait: float = EVENTS_WAIT_SEC,
    ):
        url = str(getattr(config, "edge_url", "") or "")
        token = str(getattr(config, "prm_edge_token", "") or "")
        self.client = client or EdgeClient(url, token)
        data_dir = Path(getattr(config, "db_path", "data/messages.db")).resolve().parent
        self.state = EdgeState(Path(state_path) if state_path else data_dir / "edge_client.json")
        self.files_dir = Path(files_dir) if files_dir else data_dir / "edge_files"
        self._sleep = sleep
        self._clock = clock
        self._events_wait = events_wait
        self._stop = threading.Event()
        self._parties_source: Callable[[], List[str]] = lambda: []
        self._reply_sink: Callable[[str, str, Dict[str, Any]], None] = self._drop_reply
        self._event_hooks: List[Callable[[Dict[str, Any]], None]] = []
        self._last_health = 0.0
        # 给控制台和日志看的：Windows 最近一次的状态，最后一次联系上的时间
        self.edge_status: Dict[str, Any] = {}
        self.last_contact: Optional[float] = None
        self.online = False

    # ---- 接线 ----

    def set_parties_source(self, source: Callable[[], List[str]]) -> None:
        """达人名单从哪来（一般是达人模块里启用的达人）。有变化就推给 Windows。"""
        self._parties_source = source

    def set_reply_sink(self, sink: Callable[[str, str, Dict[str, Any]], None]) -> None:
        """路由给出的回复交给谁：存成待审核的草稿（D19），不直接发。"""
        self._reply_sink = sink

    def add_event_hook(self, hook: Callable[[Dict[str, Any]], None]) -> None:
        """除达人消息以外的事件（运营亲手回复、撤回提示、发送结果、读回、Windows 状态）交给谁。"""
        self._event_hooks.append(hook)

    @staticmethod
    def _drop_reply(chat_id: str, reply: str, msg: Dict[str, Any]) -> None:
        logger.warning("[edge] %s 的回复没有地方存（没有接 reply_sink），不发出", chat_id[:20])

    # ---- 后端接口 ----

    def start(self, callback: MessageCallback) -> None:
        """一直拉 Windows 的事件，直到 stop()。连不上就退避重试，不会因为 Windows 没开而退出。"""
        self._stop.clear()
        failures = 0
        logger.info("[edge] 连接 Windows 微信边缘，从事件 %d 之后接着读", self.state.cursor)
        while not self._stop.is_set():
            try:
                self._maybe_check_health()
                self._sync_parties()
                events = self.client.events(after=self.state.cursor, wait=self._events_wait, limit=EVENTS_LIMIT)
                self._mark_online(True)
                failures = 0
                if events:
                    self._handle_events(events, callback)
            except EdgeError as exc:
                failures += 1
                self._mark_online(False)
                logger.error("[edge] Windows 拒绝了请求（%s %s）：%s", exc.status, exc.code, exc.detail)
                self._sleep(BACKOFF_SEC[min(failures, len(BACKOFF_SEC)) - 1])
            except OSError as exc:
                failures += 1
                self._mark_online(False)
                wait = BACKOFF_SEC[min(failures, len(BACKOFF_SEC)) - 1]
                if failures == 1 or failures % 10 == 0:
                    logger.warning("[edge] 连不上 Windows（%s），%d 秒后再试", exc, wait)
                self._sleep(wait)

    def stop(self) -> None:
        self._stop.set()

    def send_text(self, chat_id: str, content: str, send_id: Optional[str] = None) -> bool:
        """请 Windows 发一条。返回 True 只表示 Windows 收下了请求；发没发出去看 send_result / send_seen 事件。
        send_id 不给就生成一个；同一个 send_id 重复提交，Windows 只发一次。"""
        send_id = send_id or f"mac-{uuid.uuid4().hex[:20]}"
        try:
            answer = self.client.send(send_id, chat_id, content)
        except (EdgeError, OSError) as exc:
            logger.error("[edge] 发送请求 %s 没交给 Windows：%s", send_id, exc)
            return False
        logger.info("[edge] 发送请求 %s 已交给 Windows（%s），%d 字", send_id, answer.get("status"), len(content))
        return True

    # ---- 内部 ----

    def _handle_events(self, events: List[Dict[str, Any]], callback: MessageCallback) -> None:
        for event in events:
            event_id = int(event.get("id") or 0)
            if event_id <= self.state.cursor:
                continue  # 至少送达一次：已经处理过的不再处理
            if event.get("type") == P.CREATOR_MESSAGE:
                self._handle_creator_message(event, callback)
            else:
                self._run_hooks(event)
            if event.get("type") == P.EDGE_STATUS:
                self.edge_status = {k: event.get(k) for k in ("wechat", "db", "send", "detail", "at")}
            self.state.cursor = event_id
            self.state.save()
        # 都处理、存好以后才确认（附件已经下载完了）
        self.client.ack(self.state.cursor)

    def _handle_creator_message(self, event: Dict[str, Any], callback: MessageCallback) -> None:
        files = self._download_files(event)
        msg = event_to_message(event, files)
        try:
            reply = callback(msg)
        except Exception:
            logger.exception("[edge] 处理事件 %s 出错，这条不回复", event.get("id"))
            return
        if reply:
            try:
                self._reply_sink(msg["chat_id"], reply, msg)
            except Exception:
                logger.exception("[edge] 存草稿出错（事件 %s），这条不发", event.get("id"))

    def _download_files(self, event: Dict[str, Any]) -> List[str]:
        saved = []
        for f in event.get("files") or []:
            if f.get("missing"):
                continue
            name = Path(str(f.get("name") or f"file-{f.get('n')}")).name  # 只要文件名，不让它带路径
            dest = self.files_dir / str(event["id"]) / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            self.client.download(int(event["id"]), int(f["n"]), dest)
            saved.append(str(dest))
        return saved

    def _run_hooks(self, event: Dict[str, Any]) -> None:
        for hook in self._event_hooks:
            try:
                hook(event)
            except Exception:
                logger.exception("[edge] 处理 %s 事件出错", event.get("type"))

    def _maybe_check_health(self) -> None:
        now = self._clock()
        if self._last_health and now - self._last_health < HEALTH_EVERY_SEC:
            return
        health = self.client.health()
        self._last_health = now
        for key in ("wechat", "db", "send", "detail"):
            if key in health:
                self.edge_status[key] = health[key]
        if int(health.get("parties_version") or 0) != self.state.parties_version:
            # Windows 重装过、库丢了：下面 _sync_parties 会重新推一次
            self.state.parties_digest = ""

    def _sync_parties(self) -> None:
        wxids = sorted(set(self._parties_source()))
        digest = P.parties_digest(wxids)
        if digest == self.state.parties_digest:
            return
        version = self.state.parties_version + 1
        try:
            answer = self.client.put_parties(version, wxids)
        except EdgeError as exc:
            if exc.status != 409:
                raise
            # Windows 上的版本比我们记的新（比如这边的状态文件丢了）：从它的版本往后接着推
            version = int(exc.body.get("current_version") or 0) + 1
            answer = self.client.put_parties(version, wxids)
        for item in answer.get("rejected") or []:
            logger.warning("[edge] Windows 不收这个达人：%s（%s）", str(item.get("wxid"))[:20], item.get("reason"))
        self.state.parties_version = version
        self.state.parties_digest = digest
        self.state.save()
        logger.info("[edge] 达人名单已推给 Windows：版本 %d，%d 位", version, len(wxids))

    def _mark_online(self, online: bool) -> None:
        if online:
            self.last_contact = self._clock()
        if online != self.online:
            logger.info("[edge] Windows %s", "连上了" if online else "连不上")
        self.online = online
