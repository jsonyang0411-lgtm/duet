"""Mac 端的"微信后端"：不连本机微信，通过 Windows 的微信边缘（/edge/*，协议见 edge-protocol.md）收发。

Windows 只收发微信（D18）；这台 Mac 跑业务：路由、达人模块、控制台。这个后端把 Windows 转来的达人消息
交给路由（和其他后端一样调 callback），要发给达人的话通过 Windows 发出。

回复达人要人审（D19）：callback（路由）给出的回复**不会自动发出**，交给 reply_sink 存成待审核的草稿；
只有审核通过后，业务代码调用 send_text 才真正发出去。

连接用同事写的 src/edge/client.py（只用标准库）。读到哪个事件、名单推到哪个版本，存在一个小的 JSON 文件里，
重启以后接着来。名单的来源由 add_parties_source 给（达人模块的名单、PRM 登记的达人），合起来有变化就推给 Windows。

同事的 PRM（src/prm，PRM_COPILOT=on）可以直接挂在这个后端上，和挂在 Windows 的微信后端上一样：
- 发送：send_text / send_file 交给 Windows，同一个 send_id 只发一次；
- 「对不对得上」Windows 已经比过了：operator_message（运营亲手回复）和 send_seen（我们发的读回来了）
  都交给 set_own_message_hook 的钩子，分别带 edge_operator / edge_seen 标记；
- send_result 是失败、过期、不确定的，交给 set_send_failure_hook 的钩子，让草稿改成没发出去。
"""

from __future__ import annotations

import hashlib
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
# 发出去的请求在内存里记多久（等发送结果和读回用）。原文只放内存；写盘的只有摘要（EdgeSends）
SENT_KEEP_SEC = P.LATE_SEEN_SEC
FAILED_RESULTS = (P.FAILED, P.EXPIRED, P.INDETERMINATE)
# 同一条事件的附件因为网络断了连续下载失败这么多次，就不要这个附件了，消息照常往下走（别永远卡在这一条）
DOWNLOAD_TRIES = 3
# Windows 的事件库重建过（编号从 1 重新开始）以后，控制台提醒多久
EVENTS_RESET_NOTICE_SEC = 3600


class AttachmentNotYet(Exception):
    """附件这次没下载成（网络断了）：这一轮先不处理这条事件，等一会儿再试；不算连不上 Windows。"""


def text_digest(text: str) -> str:
    """发出去的原文的摘要（sha256）：写盘只写它，重启后用来和草稿对（PrmService.on_send_failed）。"""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


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


class EdgeSends:
    """发出去的请求，重启以后还要认得的那部分：send_id → {chat_id, sha256, is_file, at}。不写原文
    （原文里有换回来的号码）；超过 SENT_KEEP_SEC 的丢掉。写的时候先写临时文件再换。"""

    def __init__(self, path: Path):
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        self.items: Dict[str, Dict[str, Any]] = {str(k): v for k, v in data.items() if isinstance(v, dict)} \
            if isinstance(data, dict) else {}

    def add(self, send_id: str, chat_id: str, text: str, is_file: bool, now: float) -> None:
        self.items = {k: v for k, v in self.items.items() if now - float(v.get("at") or 0) <= SENT_KEEP_SEC}
        self.items[send_id] = {"chat_id": chat_id, "sha256": text_digest(text), "is_file": bool(is_file), "at": now}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, send_id: str) -> Optional[Dict[str, Any]]:
        return self.items.get(send_id)


def event_to_message(event: Dict[str, Any], files: List[str]) -> Dict[str, Any]:
    """Windows 的 creator_message / operator_message 事件 → 路由认识的标准消息（见 base.py）。
    事件里没有昵称，用 chat_id 代替。files 是已经下载到这台 Mac 上的附件（relay/attachments.py 直接用）。"""
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
    sent_files = [f for f in event.get("files") or [] if isinstance(f, dict) and not f.get("missing")]
    if sent_files and sent_files[0].get("kind") == "file":
        msg["file_name"] = str(sent_files[0].get("name") or "")
        msg["file_size"] = int(sent_files[0].get("size") or 0)
    quote = event.get("quote")
    if isinstance(quote, dict) and quote.get("text"):
        msg["quoted_content"] = str(quote["text"])
        msg["is_reply_to_bot"] = quote.get("from") == "us"
    return msg


def edge_problems(info: Dict[str, Any]) -> List[str]:
    """要提醒运营的事（控制台顶上的横条），按要先处理的顺序。"""
    out = []
    if info.get("events_reset"):
        out.append("Windows 的事件库重建过（编号从头开始了），Mac 已经从头接着读。重建之前没取走的达人消息可能丢了，"
                   "请到 Windows 的微信里看看最近有没有没回的达人")
    if not info.get("online"):
        ago = info.get("last_contact_sec_ago")
        out.append("连不上 Windows：达人的消息收不到，审核过的回复也发不出去。看看 Windows 那台电脑和 webot 开着没有、"
                   "EDGE_URL 和 PRM_EDGE_TOKEN 对不对" + (f"（上次联系上是 {ago // 60} 分钟前）" if ago and ago >= 60 else ""))
        return out
    if info.get("wechat") == P.OFFLINE:
        out.append("Windows 上的微信掉线了：达人的消息收不到，回复也发不出去。请到 Windows 上重新登录微信")
    if info.get("db") == P.UNREADABLE:
        out.append("Windows 读不了微信的数据库：达人的消息收不到。请在 Windows 的「微信连接」页看看")
    if info.get("send") == P.FAILING:
        out.append("Windows 连续几条没发出去" + (f"：{info['detail']}" if info.get("detail") else ""))
    return out


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
        self.sends_record = EdgeSends(self.state.path.with_name("edge_sends.json"))
        self.files_dir = Path(files_dir) if files_dir else data_dir / "edge_files"
        self._sleep = sleep
        self._clock = clock
        self._events_wait = events_wait
        self._stop = threading.Event()
        self._parties_sources: List[Callable[[], List[str]]] = []
        self._reply_sink: Callable[[str, str, Dict[str, Any]], None] = self._drop_reply
        self._event_hooks: List[Callable[[Dict[str, Any]], None]] = []
        self._own_message_hook: Optional[Callable[[Dict[str, Any]], None]] = None
        self._send_failure_hook: Optional[Callable[[str, str, str], None]] = None
        # 这次运行发出去的请求：send_id → (chat_id, 文字, 是不是文件, 什么时候)
        self._sends: Dict[str, tuple] = {}
        self._sends_lock = threading.Lock()
        self._last_health = 0.0
        # 附件因为网络断了下载失败：事件编号 → 连续失败几次（DOWNLOAD_TRIES 次以后不要那个附件）
        self._download_failures: Dict[int, int] = {}
        # Windows 的事件库重建过、Mac 从头读的时间（控制台提醒用）
        self.events_reset_at: Optional[float] = None
        # 给控制台和日志看的：Windows 最近一次的状态，最后一次联系上的时间
        self.edge_status: Dict[str, Any] = {}
        self.last_contact: Optional[float] = None
        self.online = False
        self.parties_count = 0   # 最近一次名单里有几位

    # ---- 接线 ----

    def add_parties_source(self, source: Callable[[], List[str]]) -> None:
        """达人名单的一个来源（达人模块里启用的达人、PRM 登记的达人）。几个来源合起来，有变化就推给 Windows。"""
        self._parties_sources.append(source)

    def refresh_private_chats_soon(self) -> None:
        """名单变了（PRM 登记或停用了达人）：下一轮就推，不等摘要比较。"""
        self.state.parties_digest = ""

    def set_private_filter(self, predicate: Callable[[str], bool]) -> None:
        """PRM 接线时会调。读哪些私聊由 Windows 按推过去的名单决定，这里不用再筛。"""

    def set_own_message_hook(self, hook: Callable[[Dict[str, Any]], None]) -> None:
        """这个微信号在达人私聊里发的话交给谁（PRM 的 on_own_message）：
        运营亲手发的带 edge_operator，我们发的读回来了带 edge_seen。"""
        self._own_message_hook = hook

    def set_send_failure_hook(self, hook: Callable[[str, str, str], None]) -> None:
        """发送失败、过期、不确定时交给谁：hook(chat_id, 文字, 原因)。发文件的不报（PRM 发文件前先发文字）。"""
        self._send_failure_hook = hook

    def parties(self) -> List[str]:
        wxids = set()
        for source in self._parties_sources:
            wxids.update(str(w) for w in source() if str(w))
        return sorted(wxids)

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
            except AttachmentNotYet as exc:
                # 附件这次没下载成：前面处理完的事件已经确认了，这一条下一轮再试（不算连不上 Windows）
                logger.warning("[edge] %s，稍后再试", exc)
                self._sleep(BACKOFF_SEC[0])
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
        return self._send(chat_id, content, (), send_id, False)

    def send_file(self, chat_id: str, path: str, send_id: Optional[str] = None) -> bool:
        """先把文件传给 Windows，再请它发出去。"""
        try:
            upload = self.client.upload(path)
        except (EdgeError, OSError) as exc:
            logger.error("[edge] 文件 %s 没传到 Windows：%s", Path(path).name, exc)
            return False
        return self._send(chat_id, "", (str(upload["upload_id"]),), send_id, True)

    def _send(self, chat_id: str, text: str, uploads: tuple, send_id: Optional[str], is_file: bool) -> bool:
        send_id = send_id or f"mac-{uuid.uuid4().hex[:20]}"
        try:
            answer = self.client.send(send_id, chat_id, text, uploads=uploads)
        except (EdgeError, OSError) as exc:
            logger.error("[edge] 发送请求 %s 没交给 Windows：%s", send_id, exc)
            return False
        now = self._clock()
        with self._sends_lock:
            self._sends = {k: v for k, v in self._sends.items() if now - v[3] <= SENT_KEEP_SEC}
            self._sends[send_id] = (chat_id, text, is_file, now)
            try:
                self.sends_record.add(send_id, chat_id, text, is_file, now)
            except OSError:
                logger.exception("[edge] 发送记录 %s 没写进 %s（重启以后认不出它的发送结果）", send_id,
                                 self.sends_record.path.name)
        logger.info("[edge] 发送请求 %s 已交给 Windows（%s），%s", send_id, answer.get("status"),
                    "1 个文件" if is_file else f"{len(text)} 字")
        return True

    # ---- 内部 ----

    def _handle_events(self, events: List[Dict[str, Any]], callback: MessageCallback) -> None:
        handled = self.state.cursor
        try:
            for event in events:
                self._handle_event(event, callback)
        finally:
            # 都处理、存好以后才确认（附件已经下载完了）；附件没下载成的那条和它后面的下一轮再来
            if self.state.cursor > handled:
                self.client.ack(self.state.cursor)

    def _handle_event(self, event: Dict[str, Any], callback: MessageCallback) -> None:
        event_id = int(event.get("id") or 0)
        if event_id <= self.state.cursor:
            return  # 至少送达一次：已经处理过的不再处理
        kind = event.get("type")
        if kind == P.CREATOR_MESSAGE:
            self._handle_creator_message(event, callback)
        else:
            if kind == P.OPERATOR_MESSAGE:
                self._own_line(dict(event_to_message(event, []), edge_operator=True))
            elif kind == P.SEND_SEEN:
                self._send_seen(event)
            elif kind == P.SEND_RESULT and event.get("status") in FAILED_RESULTS:
                self._send_failed(event)
            self._run_hooks(event)
        if event.get("type") == P.EDGE_STATUS:
            self.edge_status = {k: event.get(k) for k in ("wechat", "db", "send", "detail", "at")}
        self.state.cursor = event_id
        self.state.save()

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

    def _own_line(self, msg: Dict[str, Any]) -> None:
        if self._own_message_hook is None:
            return
        try:
            self._own_message_hook(msg)
        except Exception:
            logger.exception("[edge] 处理这个微信号自己发的消息出错（事件 %s）", msg.get("edge_event_id"))

    def _send_seen(self, event: Dict[str, Any]) -> None:
        """我们发的读回来了：按发送记录拼成一条「自己发的」消息交给钩子，PRM 据此把草稿标成已确认。"""
        with self._sends_lock:
            sent = self._sends.get(str(event.get("send_id") or ""))
        if sent is None:
            logger.info("[edge] 发送 %s 读回来了（不是这次运行发的，草稿保持「已发出、未确认」）", event.get("send_id"))
            return
        chat_id, text, is_file, _ = sent
        self._own_line({"chat_id": chat_id, "content": text, "msg_type": 49 if is_file else 1,
                        "timestamp": int(event.get("at") or self._clock()), "is_group": False,
                        "edge_seen": True, "edge_event_id": event.get("id")})

    def _send_failed(self, event: Dict[str, Any]) -> None:
        send_id = str(event.get("send_id") or "")
        with self._sends_lock:
            sent = self._sends.get(send_id)
            record = self.sends_record.get(send_id)
        why = {P.FAILED: "Windows 没发出去", P.EXPIRED: "在 Windows 上排队太久，过期没发",
               P.INDETERMINATE: "Windows 发送时停了，不确定发出去没有"}[event["status"]]
        detail = str(event.get("error") or "").strip()
        logger.warning("[edge] 发送 %s：%s%s", send_id, why, f"（{detail}）" if detail else "")
        if self._send_failure_hook is None:
            return
        reason = why + (f"：{detail}" if detail else "")
        try:
            if sent is not None:
                if not sent[2]:
                    self._send_failure_hook(sent[0], sent[1], reason)
            elif record is not None and not record.get("is_file"):
                # 重启以前发的：内存里没有原文了，用写盘的摘要去对草稿
                self._send_failure_hook(str(record["chat_id"]), "", reason, digest=str(record["sha256"]),
                                        sent_at=float(record.get("at") or 0) or None)
        except Exception:
            logger.exception("[edge] 处理发送失败出错（%s）", send_id)

    def _download_files(self, event: Dict[str, Any]) -> List[str]:
        """下载这条事件的附件。Windows 明确回了错误（文件没了等）的那个附件不要了，消息照常往下走；
        网络断了就抛 AttachmentNotYet（这一条下一轮再试），同一条连续 DOWNLOAD_TRIES 次都不成也不要了。"""
        saved = []
        event_id = int(event["id"])
        for f in event.get("files") or []:
            if f.get("missing"):
                continue
            name = Path(str(f.get("name") or f"file-{f.get('n')}")).name  # 只要文件名，不让它带路径
            dest = self.files_dir / str(event_id) / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                self.client.download(event_id, int(f["n"]), dest)
            except EdgeError as exc:
                logger.warning("[edge] 事件 %d 的附件 %s 取不到（%s %s），这条消息不带它往下走",
                               event_id, f.get("n"), exc.status, exc.code)
                continue
            except OSError as exc:
                tries = self._download_failures.get(event_id, 0) + 1
                if tries < DOWNLOAD_TRIES:
                    self._download_failures[event_id] = tries
                    raise AttachmentNotYet(f"事件 {event_id} 的附件 {f.get('n')} 没下载成（第 {tries} 次：{exc}）")
                logger.warning("[edge] 事件 %d 的附件 %s 连续 %d 次没下载成，这条消息不带它往下走",
                               event_id, f.get("n"), tries)
                continue
            saved.append(str(dest))
        self._download_failures.pop(event_id, None)
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
        if int(health.get("events_pending") or 0) > 0 and self.state.cursor > 0:
            self._check_events_reset()

    def _check_events_reset(self) -> None:
        """Windows 说还有没确认的事件，可是我们读到的位置后面一条都没有：要么是读完了还没来得及确认
        （读到的那一条 Windows 还有），要么是 Windows 的事件库重建了、编号从 1 重新开始（那一条 Windows 没有）。
        前一种补一个确认；后一种从头读，不然会一直静默跳过所有新事件。"""
        cursor = self.state.cursor
        if self.client.events(after=cursor, wait=0, limit=1):
            return
        last = self.client.events(after=cursor - 1, wait=0, limit=1)
        if last and int(last[0].get("id") or 0) == cursor:
            self.client.ack(cursor)
            return
        logger.warning("[edge] Windows 的事件库重建过（我们读到 %d，它那里没有这一条，后面也没有），从头接着读", cursor)
        self.state.cursor = 0
        self.state.save()
        self.events_reset_at = self._clock()

    def _sync_parties(self) -> None:
        try:
            wxids = self.parties()
        except Exception:
            # 名单没拿全就不推（推一份缺人的名单，Windows 就不转那几位的消息了），下一轮再试
            logger.exception("[edge] 名单来源出错，这一轮不推名单")
            return
        self.parties_count = len(wxids)
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

    def status(self) -> Dict[str, Any]:
        """给控制台看的（GET /api/edge）：连没连上 Windows、最后一次联系上是多久以前、Windows 报的状态、要提醒运营的事。"""
        now = self._clock()
        info = {
            "running": not self._stop.is_set(),
            "online": self.online,
            "last_contact_sec_ago": None if self.last_contact is None else round(now - self.last_contact),
            "wechat": self.edge_status.get("wechat", ""),
            "db": self.edge_status.get("db", ""),
            "send": self.edge_status.get("send", ""),
            "detail": str(self.edge_status.get("detail") or ""),
            "parties": self.parties_count,
            "events_reset": bool(self.events_reset_at and now - self.events_reset_at < EVENTS_RESET_NOTICE_SEC),
        }
        info["problems"] = edge_problems(info)
        return info

    def _mark_online(self, online: bool) -> None:
        if online:
            self.last_contact = self._clock()
        if online != self.online:
            logger.info("[edge] Windows %s", "连上了" if online else "连不上")
        self.online = online
