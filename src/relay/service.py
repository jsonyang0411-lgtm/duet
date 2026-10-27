"""Claude relay (``CLAUDE_RELAY=on``): WeChat takes the requests, Claude Code on a Mac does them.

The allowed people's private messages that start with the prefix (``cc``) are
queued (store.py).  The Mac worker (``tools/claude_relay/worker.py``) asks for
jobs over HTTP (http.py), runs ``claude -p`` and posts the result back; a
background thread sends it to the chat the request came from.  The bot never
waits for the Mac: a request gets a receipt at once, the result comes later.
In between (CLAUDE_RELAY_PROGRESS_MIN): "started" when a job had to wait, and
every few minutes how far a long one is (the worker's heartbeats carry it).
A result too long for one message goes as a .txt file when the backend can
send files (CLAUDE_RELAY_LONG_FILE), with a short message after it.
Pictures and files the allowed person sends are held and go with the next task
(attachments.py); the worker downloads them from GET /relay/jobs/<id>/files/<n>.
When Claude needs an approval, the worker's PermissionRequest hook asks here
(POST /relay/jobs/<id>/ask), the request goes to WeChat, and the hook waits for
the answer (GET /relay/asks/<n>).  Only a WeChat message of the allowed person
in that chat answers it ("同意" / "拒绝"); nothing over HTTP can.
A task sent while the chat's job runs goes to that job (a follow-up): the worker
reads it (GET /relay/jobs/<id>/messages) and hands it to the running Claude,
which sees it at its next step; "新任务 …" queues a job of its own instead.

Jobs of another kind (``prm``: a registered creator's messages, src/prm) come in
through ``submit`` and are silent: no receipt, no progress, no approval requests,
and their results go to the handler registered for the kind (``register_kind``)
instead of WeChat.  Only a worker that says it does that kind gets them
(``X-Relay-Kinds``); a prm job's worker can send files back (``outfile``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

from .attachments import HOLD_SEC, KINDS, Held, attachment_kind, resolve
from .commands import RelayCommand, parse, parse_answer, prefix_pattern
from .link import VIA as LINK_VIA, PeerLog
from .store import ADMIN, FINAL_STATES, KINDS as JOB_KINDS, RelayStore

logger = logging.getLogger(__name__)

LOST_AFTER_SEC = 120        # a running job without a heartbeat for this long is lost
ONLINE_WINDOW_SEC = 60      # the Mac is online if it asked for work within this time
MAX_AGE_SEC = 300           # older requests are history replayed at startup, not orders
RETRY_SEND_SEC = 30         # wait before sending a result again after a failed send
TICK_SEC = 5
MAX_WAIT_SEC = 30           # longest a worker's request for a job is held open
START_NOTE_AFTER_SEC = 20   # a job that waited this long in the queue gets a "started" message
SUMMARY_CHARS = 300         # the message after a result file starts with this much of it
KEEP_FILES_DAYS = 30        # result files older than this are deleted when a new one is written
ASK_TIMEOUT_SEC = (60, 3600)  # the range a worker may give an approval request to be answered
MAX_OUTFILE_BYTES = 100 * 1024 * 1024   # one file a prm job's worker sends back
MAX_OUTFILES = 9                        # files per prm job

_QUOTE_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9]+:\s*")
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# A voice message the backend turned into text (VOICE_ASR_ENABLED on Windows): "[语音] 跑一下测试。"
_VOICE = re.compile(r"^\[语音\]\s*(.*)$", re.S)
VOICE_NOT_HEARD = "这条语音没转出文字。请打字发，或者在 Windows 上打开语音识别（VOICE_ASR_ENABLED=true）。"

STATUS_NAMES = {
    "pending": "排队中", "running": "进行中", "done": "完成", "failed": "失败",
    "cancelled": "已取消", "lost": "失联",
}


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds}秒"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}分{seconds}秒" if seconds else f"{minutes}分钟"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}小时{minutes}分"


def progress_line(progress_json: str) -> str:
    """"第 14 步 · 改 service.py · 计划 3/7" from a heartbeat's progress ("" when there is none)."""
    try:
        progress = json.loads(progress_json or "{}")
    except ValueError:
        return ""
    if not isinstance(progress, dict):
        return ""
    steps = int(progress.get("steps") or 0)
    parts = [f"第 {steps} 步"] if steps else []
    if str(progress.get("doing") or "").strip():
        parts.append(str(progress["doing"]).strip())
    if progress.get("plan"):
        parts.append(f"计划 {progress['plan']}")
    return " · ".join(parts)


def _clean_progress(progress) -> Optional[dict]:
    """A heartbeat body as the store keeps it (None: the worker sent no progress)."""
    if not isinstance(progress, dict) or not progress:
        return None
    try:
        steps = max(0, int(progress.get("steps") or 0))
    except (TypeError, ValueError):
        steps = 0
    return {"steps": steps, "doing": str(progress.get("doing") or "")[:80],
            "plan": str(progress.get("plan") or "")[:10]}


def _preview(text: str, limit: int = 20) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def job_meta(job: dict) -> dict:
    """A job's ``meta`` as a dict ({} when it has none or it is not JSON)."""
    try:
        meta = json.loads(job.get("meta") or "{}")
    except (TypeError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


class ClaudeRelay:
    def __init__(self, config, *, store: Optional[RelayStore] = None,
                 send: Optional[Callable[[str, str], bool]] = None,
                 send_file: Optional[Callable[[str, str], bool]] = None,
                 files_dir: Optional[Path] = None,
                 reserved: Optional[Callable[[str], bool]] = None,
                 extra_help: str = "",
                 clock: Callable[[], float] = time.time):
        self._config = config
        self._store = store if store is not None else RelayStore(config.claude_relay_db)
        self._send = send
        # Long results as a file: needs a backend that can send files (Windows).
        self._send_file = send_file if getattr(config, "claude_relay_long_file", "on") == "on" else None
        db = str(getattr(config, "claude_relay_db", ":memory:"))
        self._files_dir = Path(files_dir) if files_dir is not None else (
            Path(tempfile.gettempdir()) / "webot-relay-files" if db == ":memory:"
            else (Path(db).parent / "claude_relay_files").resolve())
        self._file_sent: set[int] = set()       # jobs whose result file is in the chat already
        self._held: dict[str, list[Held]] = {}  # pictures and files waiting for the next task
        self._held_lock = threading.Lock()
        self._asks_changed = threading.Condition()
        self._followups_ready = threading.Condition()
        self._tick_lock = threading.Lock()      # one delivery round at a time (no message sent twice)
        self._clock = clock
        self._prefix_word = config.claude_relay_prefix
        self._prefix = prefix_pattern(config.claude_relay_prefix)
        allowed = list(config.claude_relay_wxids) or [config.admin_wxid]
        self._allowed = {str(w).strip() for w in allowed if str(w).strip()}
        self._max_chars = int(config.claude_relay_max_chars)
        # "Started" and progress messages (0 = only the receipt and the result).
        self._progress_sec = int(getattr(config, "claude_relay_progress_min", 3) or 0) * 60
        # CLAUDE_RELAY_ONLY: every private message of an allowed person is for the relay,
        # so "cc" can be left out.
        self._relay_only = getattr(config, "claude_relay_only", "off") == "on"
        # The admin's management commands (AdminCommandHandler.is_command): without "cc"
        # they stay commands even when every other line is a task; their list goes in the help.
        self._reserved = reserved
        self._admin_wxid = str(getattr(config, "admin_wxid", "") or "").strip()
        self._extra_help = extra_help
        # What the Mac worker said it offers (X-Relay-Projects on GET /relay/next).
        self._worker_projects: list[str] | None = None
        self._worker_default = ""
        # The job kinds the last worker asked for (X-Relay-Kinds; an older worker: admin only).
        self._worker_kinds: tuple[str, ...] = ()
        # Where the worker connects from: the LAN, Tailscale (cross-network.md), ...
        self._worker_link = PeerLog("CLAUDE_RELAY: the Mac worker")
        # Results of the other kinds go here instead of WeChat (register_kind).
        self._kind_handlers: dict[str, Callable[[dict], bool]] = {}
        self._jobs_ready = threading.Condition()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._last_seen = 0.0
        self._send_failed_at: dict[tuple[int, str], float] = {}
        self._thread: Optional[threading.Thread] = None
        self._server = None
        # PRM_EDGE: the WeChat edge answers /edge/* on this server (attach_edge).
        self.edge = None
        self.edge_token = b""

    # ── lifecycle ─────────────────────────────────────────────────────────

    def attach_edge(self, edge, token: str) -> None:
        """Let ``edge`` (src/edge EdgeService) answer /edge/* here with ``token``; None detaches."""
        self.edge = edge
        self.edge_token = str(token or "").encode() if edge is not None else b""

    def start(self) -> None:
        """The delivery thread, then the HTTP server (an OSError there is the caller's to log)."""
        self._thread = threading.Thread(target=self._loop, name="claude-relay", daemon=True)
        self._thread.start()
        from .http import serve
        self._server = serve(
            self, self._config.claude_relay_host, int(self._config.claude_relay_port),
            self._config.claude_relay_token,
        )
        logger.info(
            "CLAUDE_RELAY: waiting for the Mac worker on %s:%s (prefix %r, %d allowed sender(s))",
            self._config.claude_relay_host, self._config.claude_relay_port,
            self._prefix_word, len(self._allowed),
        )

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        with self._jobs_ready:
            self._jobs_ready.notify_all()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def address(self) -> tuple[str, int] | None:
        return self._server.server_address[:2] if self._server is not None else None

    # ── from WeChat ───────────────────────────────────────────────────────

    def handle(self, msg: dict) -> tuple[bool, Optional[str]]:
        """``(matched, reply)``: matched = this message was a relay request (reply may be None)."""
        if msg.get("is_group", True):
            return False, None
        content = _QUOTE_PREFIX.sub("", str(msg.get("content", "")))
        sender = str(msg.get("sender_id", ""))
        msg_type = int(msg.get("msg_type", 1) or 1)
        timestamp = float(msg.get("timestamp", 0) or 0)
        too_old = bool(timestamp) and self._clock() - timestamp > MAX_AGE_SEC
        heard = ""
        kind = attachment_kind(msg)
        if kind and sender in self._allowed:       # a picture or file for the next task
            if too_old:
                return False, None
            count = self._hold(str(msg["chat_id"]), Held(kind, msg, self._clock()))
            if self._relay_only:
                return True, (f"收到{KINDS[kind]}，下一个任务会带上它"
                              f"（现在有 {count} 个附件，{HOLD_SEC // 60} 分钟内有效）。")
            return False, None                     # the normal reply goes on; the next cc task takes it
        if msg_type == 34:                         # voice: its text, without the closing "。"
            voice = _VOICE.match(content)
            heard = voice.group(1).strip().rstrip("。.！!，,、 ") if voice else ""
            if not heard:
                if self._relay_only and sender in self._allowed:
                    return True, None if too_old else VOICE_NOT_HEARD
                return False, None
            content = heard
        if sender in self._allowed and not too_old:
            answer = self._answer(str(msg["chat_id"]), content, timestamp, by_voice=bool(heard))
            if answer is not None:
                return answer
        m = self._prefix.match(content)
        if m:
            body = m.group(1)
            if sender not in self._allowed:
                logger.info("CLAUDE_RELAY: %s is not allowed to use the relay; handled as a normal message",
                            sender)
                return False, None
        elif self._relay_only and sender in self._allowed and msg_type in (1, 34) and content.strip() \
                and not self._for_admin(sender, content):
            body = content                         # relay only: "cc" may be left out
        else:
            return False, None
        if too_old:
            logger.info("CLAUDE_RELAY: ignoring an old request (%.0f s): %s",
                        self._clock() - timestamp, content[:60])
            return True, None
        try:
            cmd = parse(body)
            reply = self._run(cmd, msg["chat_id"], sender)
            if heard and cmd.kind == "task":
                reply += f"\n听到的是：{cmd.text}"
            return True, reply
        except Exception:
            logger.exception("CLAUDE_RELAY: could not handle %r", content[:80])
            return True, "转给 Mac 失败了，详情看 bot.log。"

    def _for_admin(self, sender: str, content: str) -> bool:
        """A management command of the admin (the route's ``admin_command`` step answers it)."""
        try:
            return (self._reserved is not None and bool(self._admin_wxid) and sender == self._admin_wxid
                    and bool(self._reserved(content.strip())))
        except Exception:
            logger.exception("CLAUDE_RELAY: could not check for an admin command")
            return False

    def _run(self, cmd: RelayCommand, chat_id: str, sender: str) -> str:
        if cmd.kind == "help":
            return self.help_text()
        if cmd.kind == "status":
            return self.status_text(chat_id)
        if cmd.kind == "reset":
            self._store.reset_session(chat_id, self._clock())
            return "好，下一个任务开一个新对话（不带之前的上下文）。"
        if cmd.kind == "cancel":
            return self._cancel(chat_id, cmd.job_id)
        if cmd.kind == "retry":
            return self._retry(chat_id, cmd.job_id)
        if cmd.kind == "projects":
            return self.projects_text(chat_id)
        if cmd.kind == "switch":
            return self._switch(chat_id, cmd.project)
        if not cmd.queue and not cmd.project and not self._has_held(chat_id):
            added = self._store.add_followup(chat_id, cmd.text, self._clock())
            if added is not None:
                with self._followups_ready:
                    self._followups_ready.notify_all()
                logger.info("CLAUDE_RELAY: follow-up for running #%d: %s", added[0], cmd.text[:80])
                return (f"收到，已转给正在做的 #{added[0]}（Claude 下一步就能看到）。"
                        f"要另起一个任务就发「{self._command_text('新任务 内容')}」。")
        return self._queue(chat_id, sender, cmd.text, cmd.project or self._store.current_project(chat_id))

    # ── jobs of other kinds (src/prm) ─────────────────────────────────────

    def register_kind(self, kind: str, on_result: Callable[[dict], bool]) -> None:
        """Send the finished jobs of ``kind`` to ``on_result`` (True: handled) instead of WeChat."""
        if kind == ADMIN or kind not in JOB_KINDS:
            raise ValueError(f"cannot register job kind {kind!r}")
        self._kind_handlers[kind] = on_result

    def submit(self, chat_id: str, sender: str, text: str, *, kind: str, meta: dict,
               files: tuple = (), settle_sec: float = 0.0, fresh: bool = False,
               input_closed: bool = False) -> tuple[str, int]:
        """Queue a silent job of ``kind``: ``(how, job_id)``, how = merged | followup | queued.

        A job of this chat that no worker has taken yet gets the text (and is held
        ``settle_sec`` longer, so a burst of messages is one job); one that is
        running gets it as a follow-up (only text: files need a job of their own);
        else a new job.  ``fresh``: always a new job (its ``meta`` must reach the
        worker).  ``input_closed``: and no later message joins it.  Nothing is sent
        to WeChat.
        """
        if kind not in self._kind_handlers:
            raise ValueError(f"no handler for job kind {kind!r}")
        now = self._clock()
        ready_at = now + max(0.0, float(settle_sec))
        job_id = None if fresh else self._store.append_pending(chat_id, kind, text, files, ready_at)
        how = "merged"
        if job_id is None and not files and not fresh:
            added = self._store.add_followup(chat_id, text, now, kind=kind)
            if added is not None:
                job_id, how = added[0], "followup"
                with self._followups_ready:
                    self._followups_ready.notify_all()
        if job_id is None:
            job_id = self._store.add(chat_id, sender, text, "", now, files=files,
                                     kind=kind, meta=meta, ready_at=ready_at, input_closed=input_closed)
            how = "queued"
        with self._jobs_ready:
            self._jobs_ready.notify_all()
        logger.info("CLAUDE_RELAY: %s job #%d %s (%d chars%s)", kind, job_id, how, len(text),
                    f", {len(files)} file(s)" if files else "")
        return how, job_id

    def collect(self, held: list[Held]) -> tuple[list[tuple[str, str, int]], list[str]]:
        """Pictures and files for a job of another kind (see ``_collect``)."""
        return self._collect(held)

    def outfile(self, job_id: int, name: str, read: Callable[[int], bytes], length: int) -> Optional[dict]:
        """A file a ``prm`` job's worker sends back: ``{"id", "size", "sha256"}``, or None when
        the job is not a running prm job, the file is too big, or the job has enough files."""
        self._seen()
        job = self._store.get(job_id)
        if job is None or job.get("kind") == ADMIN or job["status"] not in ("running", "lost"):
            return None
        if not 0 < length <= MAX_OUTFILE_BYTES:
            return None
        name = _UNSAFE_NAME.sub("_", str(name)).strip(" .")[:120] or "file"
        folder = self._files_dir / "outfiles" / f"job-{job_id}"
        folder.mkdir(parents=True, exist_ok=True)
        path, n = folder / name, 2
        while path.exists():                          # two files of the same name
            path, n = folder / f"{Path(name).stem}({n}){Path(name).suffix}", n + 1
        digest, left = hashlib.sha256(), length
        part = path.with_name(path.name + ".part")
        try:
            with open(part, "wb") as out:
                while left > 0:
                    chunk = read(min(left, 1024 * 1024))
                    if not chunk:
                        raise OSError(f"the upload ended early ({length - left}/{length} bytes)")
                    digest.update(chunk)
                    out.write(chunk)
                    left -= len(chunk)
            part.replace(path)
        finally:
            part.unlink(missing_ok=True)
        file_id = self._store.add_outfile(job_id, path.name, str(path), length, digest.hexdigest(),
                                          MAX_OUTFILES)
        if file_id is None:
            path.unlink(missing_ok=True)
            return None
        logger.info("CLAUDE_RELAY: #%d sent back a file (%d bytes)", job_id, length)
        return {"id": file_id, "size": length, "sha256": digest.hexdigest()}

    # ── projects ──────────────────────────────────────────────────────────

    def note_worker(self, projects: list[str] | None, default: str = "",
                    kinds: tuple[str, ...] | None = None, address: str | None = None) -> None:
        """What the Mac worker offers (sent with every request for a job), and where from."""
        if projects is not None:
            self._worker_projects = [str(p) for p in projects if str(p).strip()]
            self._worker_default = str(default or "")
        if kinds is not None:
            self._worker_kinds = tuple(k for k in kinds if k in JOB_KINDS)
        if address:
            self._worker_link.note(address)

    def projects_text(self, chat_id: str) -> str:
        current = self._store.current_project(chat_id)
        now = current or (f"{self._worker_default}（Mac 的默认）" if self._worker_default else "Mac 的默认项目")
        if self._worker_projects is None:
            return f"现在的项目：{now}\nMac 还没连上过，不知道它有哪些项目。"
        names = "、".join(self._worker_projects) or "（Mac 上没有登记项目）"
        word = "" if self._relay_only else f"{self._prefix_word} "
        return f"现在的项目：{now}\nMac 上能用的：{names}\n换项目：{word}切到 项目名"

    def _switch(self, chat_id: str, project: str) -> str:
        if self._worker_projects is not None and project not in self._worker_projects:
            names = "、".join(self._worker_projects) or "（没有）"
            return f"Mac 上没有叫 {project} 的项目。能用的有：{names}"
        self._store.set_project(chat_id, project, self._clock())
        return f"好，之后的任务都在 {project} 里做（开了一个新对话）。"

    # ── pictures and files ────────────────────────────────────────────────

    def _hold(self, chat_id: str, held: Held) -> int:
        with self._held_lock:
            items = [h for h in self._held.get(chat_id, []) if held.at - h.at < HOLD_SEC] + [held]
            self._held[chat_id] = items
            return len(items)

    def _has_held(self, chat_id: str) -> bool:
        now = self._clock()
        with self._held_lock:
            return any(now - h.at < HOLD_SEC for h in self._held.get(chat_id, []))

    def _take_held(self, chat_id: str) -> list[Held]:
        now = self._clock()
        with self._held_lock:
            return [h for h in self._held.pop(chat_id, []) if now - h.at < HOLD_SEC]

    def _collect(self, held: list[Held]) -> tuple[list[tuple[str, str, int]], list[str]]:
        """The held pictures and files for a job: ``(files, problems)``.

        A picture is copied into the bot's own folder (WeChat's cache and the chat
        screenshots come and go); a file stays where WeChat downloaded it (it can be
        a gigabyte, and WeChat keeps it).
        """
        files, problems, names = [], [], set()
        folder = self._files_dir / "attachments" / f"{int(self._clock())}-{uuid.uuid4().hex[:8]}"
        for number, item in enumerate(held, 1):
            try:
                path, name, problem = resolve(self._config, item, number)
                if not path:
                    problems.append(problem)
                    continue
                stem, suffix = Path(name).stem, Path(name).suffix
                n = 2
                while name in names:                   # two files of the same name
                    name, n = f"{stem}({n}){suffix}", n + 1
                names.add(name)
                if item.kind == "file":
                    files.append((name, str(path), Path(path).stat().st_size))
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                copy = folder / name
                shutil.copyfile(path, copy)
                files.append((name, str(copy), copy.stat().st_size))
            except Exception:
                logger.exception("CLAUDE_RELAY: attachment %d could not be prepared", number)
                problems.append(f"第 {number} 个附件出错了，没带上")
        if held:
            self._prune_attachments()
        return files, problems

    def _prune_attachments(self) -> None:
        cutoff = time.time() - KEEP_FILES_DAYS * 86400
        for folder in (self._files_dir / "attachments").glob("*"):
            try:
                if folder.is_dir() and folder.stat().st_mtime < cutoff:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass

    def file_path(self, job_id: int, file_id: int) -> Optional[str]:
        """The copy the worker downloads (None: not a file of that job, or it is gone)."""
        self._seen()
        row = self._store.file(job_id, file_id)
        return row["path"] if row and Path(row["path"]).is_file() else None

    def _queue(self, chat_id: str, sender: str, text: str, project: str, note: str = "",
               files_of: Optional[int] = None) -> str:
        """Queue a task with the held pictures and files (a retry: with those of job ``files_of``)."""
        if files_of is None:
            files, problems = self._collect(self._take_held(chat_id))
        else:
            files = [(f["name"], f["path"], f["size"]) for f in self._store.files(files_of)]
            problems = []
        if files:
            note += f"，带 {len(files)} 个附件"
        job_id = self._store.add(chat_id, sender, text, project, self._clock(), files=files)
        with self._jobs_ready:
            self._jobs_ready.notify_all()
        logger.info("CLAUDE_RELAY: queued #%d%s%s: %s", job_id,
                    f" (@{project})" if project else "", f" with {len(files)} file(s)" if files else "",
                    text[:80])
        lines = [f"收到，已交给 Mac（#{job_id}{note}）。"]
        lines += [f"{p}。" for p in problems]
        ahead = self._store.pending_before(job_id)
        if ahead:
            lines.append(f"前面还有 {ahead} 个任务。")
        if not self.mac_online():
            lines.append("Mac 现在不在线，任务先排队，Mac 上线后自动开始。")
        return "\n".join(lines)

    def _own_job(self, chat_id: str, job_id: int) -> Optional[dict]:
        job = self._store.get(job_id)
        return job if job is not None and job["chat_id"] == chat_id else None

    # ── approvals ─────────────────────────────────────────────────────────

    def _answer(self, chat_id: str, content: str, sent_at: float,
                by_voice: bool = False) -> Optional[tuple[bool, Optional[str]]]:
        """「同意」/「拒绝」 (with or without "cc") for Claude's request in this chat, else None.

        Not by voice: a 「不同意」 heard as 「同意」 must never let a command run.
        """
        m = self._prefix.match(content)
        answer = parse_answer(m.group(1) if m else content)
        if answer is None:
            return None
        decision, job_id = answer
        self._expire_asks()                              # a late "同意" must not count
        ask = self._store.pending_ask(chat_id, job_id)
        # An answer typed before the request arrived is not an answer to it.
        if ask is None or (sent_at and sent_at < ask["created_at"] - 1):
            if m or self._relay_only:
                return True, "现在没有等你批准的操作。" if job_id is None else f"#{job_id} 现在没有等你批准的操作。"
            return None                                  # plain "好" in a normal chat
        if by_voice:
            return True, "批准要打字回复「同意」或「拒绝」，语音不算（免得听错）。"
        if not self._store.settle_ask(ask["id"], decision, self._clock()):
            return True, f"#{ask['job_id']} 的请求已经结束了。"
        with self._asks_changed:
            self._asks_changed.notify_all()
        logger.info("CLAUDE_RELAY: #%d request %d %s in WeChat", ask["job_id"], ask["id"], decision)
        if decision == "allow":
            return True, f"好，已批准 #{ask['job_id']}：{_preview(ask['detail'], 60)}"
        return True, f"好，#{ask['job_id']} 不做这一步（Claude 会换个办法，或者在结果里说明）。"

    def ask(self, job_id: int, body: dict) -> Optional[dict]:
        """The worker's hook asks for an approval: ``{"ask_id"}``, or None for a job that is not running."""
        self._seen()
        try:
            timeout = float(body.get("timeout_s") or 600)
        except (TypeError, ValueError):
            timeout = 600.0
        timeout = min(max(timeout, ASK_TIMEOUT_SEC[0]), ASK_TIMEOUT_SEC[1])
        job = self._store.get(job_id)
        if job is not None and job.get("kind", ADMIN) != ADMIN:
            # A creator's chat must never get an approval request: refused, the hook denies.
            logger.warning("CLAUDE_RELAY: #%d is a %s job; its approval request is refused", job_id, job["kind"])
            return None
        now = self._clock()
        tool = str(body.get("tool") or "?")[:60]
        detail = " ".join(str(body.get("detail") or tool).split())[:300]
        ask_id = self._store.add_ask(job_id, tool, detail, now, now + timeout)
        if ask_id is None:
            return None
        logger.info("CLAUDE_RELAY: #%d asks for approval %d: %s", job_id, ask_id, detail[:80])
        self._wake.set()
        return {"ask_id": ask_id, "timeout_s": timeout}

    def ask_status(self, ask_id: int, wait: float) -> Optional[dict]:
        """``{"status"}`` of a request, waiting up to ``wait`` seconds while it is pending."""
        self._seen()
        deadline = time.monotonic() + max(0.0, min(float(wait), MAX_WAIT_SEC))
        while True:
            ask = self._store.get_ask(ask_id)
            if ask is None:
                return None
            if ask["status"] == "pending" and self._clock() > ask["expires_at"]:
                self._expire_asks()
                continue
            remaining = deadline - time.monotonic()
            if ask["status"] != "pending" or remaining <= 0 or self._stopping.is_set():
                return {"status": ask["status"]}
            with self._asks_changed:
                self._asks_changed.wait(min(remaining, 1.0))

    def _expire_asks(self) -> None:
        if self._store.expire_asks(self._clock()):
            with self._asks_changed:
                self._asks_changed.notify_all()

    def _end_asks(self, job_id: int) -> None:
        if self._store.cancel_asks(job_id, self._clock()):
            with self._asks_changed:
                self._asks_changed.notify_all()

    def _send_asks(self) -> None:
        """New requests to WeChat; a note when one ran out unanswered."""
        for ask in self._store.unsent_asks():
            text = (f"#{ask['job_id']} 要你批准：{ask['detail']}\n"
                    f"回复「同意」或「拒绝」（{format_duration(ask['expires_at'] - ask['created_at'])}内不回就算拒绝）")
            try:
                sent = bool(self._send(ask["chat_id"], text))
            except Exception:
                logger.exception("CLAUDE_RELAY: approval request %d not sent", ask["id"])
                sent = False
            if sent:
                self._store.mark_ask_sent(ask["id"])
        self._expire_asks()
        for ask in self._store.expired_unnoted():
            try:
                self._send(ask["chat_id"], f"#{ask['job_id']} 的请求没等到回复，已按拒绝处理。")
            except Exception:
                logger.exception("CLAUDE_RELAY: expiry note of request %d not sent", ask["id"])
            self._store.mark_ask_noted(ask["id"])

    def _cancel(self, chat_id: str, job_id: Optional[int]) -> str:
        if job_id is None:
            job = self._store.latest_open(chat_id)
            if job is None:
                return "现在没有排队或进行中的任务。"
            job_id = job["id"]
        elif self._own_job(chat_id, job_id) is None:
            return f"没有找到 #{job_id}。"
        outcome = self._store.cancel(job_id, self._clock())
        if outcome == "cancelled":
            return f"#{job_id} 还没开始，已取消。"
        if outcome == "requested":
            self._end_asks(job_id)
            return f"#{job_id} 正在执行，已通知 Mac 停下来，停好后会告诉你。"
        return f"#{job_id} 已经结束了（{STATUS_NAMES.get(outcome, outcome)}）。"

    def _retry(self, chat_id: str, job_id: Optional[int]) -> str:
        if job_id is None:
            return f"重试要写编号，比如：{self._command_text('重试 17')}"
        job = self._own_job(chat_id, job_id)
        if job is None:
            return f"没有找到 #{job_id}。"
        if job["status"] not in FINAL_STATES:
            return f"#{job_id} 还在{STATUS_NAMES[job['status']]}，不用重试。"
        return self._queue(chat_id, job["sender_id"], job["text"], job["project"],
                           note=f"，重试 #{job_id}", files_of=job_id)

    def help_text(self) -> str:
        p = "" if self._relay_only else f"{self._prefix_word} "
        first = ("直接说要做的事 —— 交给 Mac 执行（不用打 cc）\n" if self._relay_only
                 else f"{p}任务内容 —— 交给 Mac 执行\n")
        return (
            "Claude 中转：让 Mac 上的 Claude Code 帮你干活，做完把结果发回来。\n"
            + first
            + f"{p}@项目 任务 —— 这一个任务在别的项目里做\n"
            f"{p}项目 —— 现在的项目和 Mac 上能用的项目\n"
            f"{p}切到 项目名 —— 之后的任务都在这个项目里做\n"
            f"{p}状态 —— 最近的任务、Mac 在不在线\n"
            f"{p}取消 [编号] —— 取消任务（不写编号就取消最近一个）\n"
            f"{p}重试 编号 —— 重新执行一个任务\n"
            f"{p}新对话 —— 下一个任务不带之前的上下文\n"
            f"任务进行中再发的话 —— 直接转给正在做的那个；要另起一个就发「{p}新任务 内容」\n"
            f"发图片或文件 —— 下一个任务会带上它们（{HOLD_SEC // 60} 分钟内）"
            + (f"\n\n{self._extra_help}" if self._relay_only and self._extra_help else "")
        )

    def status_text(self, chat_id: str) -> str:
        now = self._clock()
        if self.mac_online():
            via = LINK_VIA.get(self._worker_link.kind or "")
            lines = ["Mac：在线" + (f"（{via}）" if via else "")]
        elif self._last_seen:
            lines = [f"Mac：不在线（{format_duration(now - self._last_seen)}前联系过）"]
        else:
            lines = ["Mac：不在线（这次启动后还没连上过）"]
        project = self._store.current_project(chat_id)
        lines.append(f"项目：{project or (self._worker_default + '（默认）' if self._worker_default else 'Mac 的默认项目')}")
        jobs = self._store.recent(chat_id, 5)
        if not jobs:
            lines.append("还没有任务。")
        for job in jobs:
            state = STATUS_NAMES.get(job["status"], job["status"])
            if job["status"] == "running" and job["claimed_at"]:
                state += f" {format_duration(now - job['claimed_at'])}"
            lines.append(f"#{job['id']} {state} · {_preview(job['text'])}")
            if job["status"] == "running" and progress_line(job.get("progress", "")):
                lines.append(f"    {progress_line(job['progress'])}")
        ask = self._store.pending_ask(chat_id)
        if ask is not None:
            lines.append(f"#{ask['job_id']} 在等你批准：{ask['detail']}（回复「同意」或「拒绝」）")
        return "\n".join(lines)

    # ── from the Mac worker (http.py) ─────────────────────────────────────

    def snapshot(self) -> dict:
        """What the console shows: the Mac worker, the address, the counts."""
        now = self._clock()
        return {
            "listening": self._server is not None,
            "address": list(self.address) if self.address else None,
            "mac_online": self.mac_online(),
            "mac_last_seen_sec": round(now - self._last_seen) if self._last_seen else None,
            "mac_address": self._worker_link.address,
            "mac_link": self._worker_link.kind,
            "prefix": self._prefix_word,
            "counts": self._store.counts(),
            "worker_kinds": list(self._worker_kinds),
        }

    def cancel_from_console(self, job_id: int) -> str:
        """Cancel any job (the console is the owner's): see ``RelayStore.cancel`` for the result."""
        outcome = self._store.cancel(int(job_id), self._clock())
        if outcome == "requested":
            self._end_asks(int(job_id))
        return outcome

    @property
    def store(self) -> RelayStore:
        return self._store

    def mac_online(self) -> bool:
        return bool(self._last_seen) and self._clock() - self._last_seen < ONLINE_WINDOW_SEC

    def _seen(self) -> None:
        self._last_seen = self._clock()

    def next_job(self, wait: float, kinds: tuple[str, ...] = (ADMIN,)) -> Optional[dict]:
        """The oldest queued job of ``kinds`` (what the worker does), waiting up to ``wait`` seconds."""
        self._seen()
        deadline = time.monotonic() + max(0.0, min(float(wait), MAX_WAIT_SEC))
        try:
            while True:
                job = self._store.claim_next(self._clock(), kinds)
                if job is not None:
                    logger.info("CLAUDE_RELAY: #%d (%s) went to the Mac", job["id"], job.get("kind", ADMIN))
                    return {
                        "id": job["id"], "kind": job.get("kind", ADMIN), "meta": job_meta(job),
                        "text": job["text"], "project": job["project"],
                        "session_id": job["session_id"], "created_at": job["created_at"],
                        "files": [{"id": f["id"], "name": f["name"], "size": f["size"]}
                                  for f in self._store.files(job["id"])],
                    }
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stopping.is_set():
                    return None
                with self._jobs_ready:
                    # A job queued between the claim and this wait is found
                    # within a second at most.
                    self._jobs_ready.wait(min(remaining, 1.0))
        finally:
            self._seen()

    def heartbeat(self, job_id: int, progress: Optional[dict] = None) -> Optional[dict]:
        self._seen()
        return self._store.heartbeat(job_id, self._clock(), _clean_progress(progress))

    def finish(self, job_id: int, payload: dict) -> Optional[bool]:
        self._seen()
        text = str(payload.get("text", "") or "")
        taken = self._store.finish(
            job_id, ok=bool(payload.get("ok")), text=text,
            session_id=str(payload.get("session_id", "") or ""),
            payload={k: payload[k] for k in ("duration_s", "cost_usd", "log_path", "error", "denied")
                     if k in payload},
            now=self._clock(),
        )
        if taken:
            logger.info("CLAUDE_RELAY: #%d finished (ok=%s, %d chars)", job_id, payload.get("ok"), len(text))
            self._end_asks(job_id)
            self._requeue_leftovers(job_id)
            self._wake.set()
        return taken

    # ── follow-ups (from the worker) ───────────────────────────────────────

    def followups(self, job_id: int, after: int, wait: float) -> Optional[dict]:
        """``{"messages": [{"id", "text"}]}`` past ``after``, waiting up to ``wait`` seconds for one."""
        self._seen()
        deadline = time.monotonic() + max(0.0, min(float(wait), MAX_WAIT_SEC))
        while True:
            rows = self._store.followups(job_id, int(after))
            if rows is None:
                return None
            remaining = deadline - time.monotonic()
            if rows or remaining <= 0 or self._stopping.is_set():
                return {"messages": rows}
            with self._followups_ready:
                self._followups_ready.wait(min(remaining, 1.0))

    def close_input(self, job_id: int, upto: int) -> Optional[dict]:
        """A turn ended: what is still to hand on, or ``[]`` and the job takes no more."""
        self._seen()
        rows = self._store.close_input(job_id, int(upto))
        return None if rows is None else {"messages": rows}

    def _requeue_leftovers(self, job_id: int) -> None:
        """Follow-ups the job ended before seeing become the next job (not after a cancel)."""
        leftovers = self._store.take_leftovers(job_id)
        job = self._store.get(job_id)
        if not leftovers or job is None or job["status"] == "cancelled":
            return
        text = "\n".join(row["text"] for row in leftovers)
        kind = job.get("kind", ADMIN)
        meta = {k: v for k, v in job_meta(job).items() if k != "history"}    # told once, with the job
        new_id = self._store.add(job["chat_id"], job["sender_id"], text, job["project"], self._clock(),
                                 kind=kind, meta=meta)
        self._store.add_result_note(job_id, followups_job=new_id)
        with self._jobs_ready:
            self._jobs_ready.notify_all()
        logger.info("CLAUDE_RELAY: %d follow-up(s) of #%d came too late; queued as #%d",
                    len(leftovers), job_id, new_id)

    # ── back to WeChat ────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("CLAUDE_RELAY: delivery round failed")
            self._wake.wait(TICK_SEC)
            self._wake.clear()

    def tick(self) -> None:
        """Mark silent jobs lost, then send every result WeChat has not had yet.

        Safe to call from any thread: two rounds at once would both send the same result.
        """
        with self._tick_lock:
            self._tick()

    def _tick(self) -> None:
        now = self._clock()
        for job_id in self._store.mark_lost(now - LOST_AFTER_SEC):
            logger.warning("CLAUDE_RELAY: #%d lost (no heartbeat for %d s)", job_id, LOST_AFTER_SEC)
        self._hand_over(now)
        if self._send is None:
            self._expire_asks()
            return
        self._send_asks()
        if self._progress_sec > 0:
            self._send_progress(now)
        for job in self._store.undelivered():
            if job.get("kind", ADMIN) != ADMIN:
                continue                               # _hand_over's (never WeChat)
            key = (job["id"], job["status"])
            failed_at = self._send_failed_at.get(key)
            if failed_at is not None and now - failed_at < RETRY_SEND_SEC:
                continue
            try:
                sent = self._deliver(job)
            except Exception:
                logger.exception("CLAUDE_RELAY: sending the result of #%d failed", job["id"])
                sent = False
            if sent:
                self._store.mark_delivered(job["id"], job["status"])
                self._send_failed_at.pop(key, None)
            else:
                self._send_failed_at[key] = now
                logger.warning("CLAUDE_RELAY: result of #%d not sent; retry in %d s",
                               job["id"], RETRY_SEND_SEC)

    def _hand_over(self, now: float) -> None:
        """Finished (or lost) jobs of the other kinds go to their handler, never to WeChat.

        A handler that fails or says False gets the job again after RETRY_SEND_SEC.
        """
        for job in self._store.undelivered():
            kind = job.get("kind", ADMIN)
            if kind == ADMIN:
                continue
            handler = self._kind_handlers.get(kind)
            if handler is None:
                continue                               # nobody to give it to (yet): it waits
            key = (job["id"], job["status"])
            failed_at = self._send_failed_at.get(key)
            if failed_at is not None and now - failed_at < RETRY_SEND_SEC:
                continue
            try:
                handled = bool(handler(job))
            except Exception:
                logger.exception("CLAUDE_RELAY: the %s handler failed on #%d", kind, job["id"])
                handled = False
            if handled:
                self._store.mark_delivered(job["id"], job["status"])
                self._send_failed_at.pop(key, None)
            else:
                self._send_failed_at[key] = now

    def _send_progress(self, now: float) -> None:
        """"#3 开始了" for a job that waited; "#3 进行中（6分钟）：…" every few minutes after.

        A failed send is not retried: the next progress message comes on time anyway.
        Only the cc tasks: a creator never hears about jobs.
        """
        for job in self._store.running():
            if job.get("kind", ADMIN) != ADMIN:
                continue
            claimed = float(job["claimed_at"] or now)
            last = job["progress_sent_at"]
            if last is None and claimed - float(job["created_at"]) >= START_NOTE_AFTER_SEC:
                text, line = f"#{job['id']} 开始了（排队了 {format_duration(claimed - job['created_at'])}）。", ""
            else:
                line = progress_line(job["progress"])
                if now - float(last or claimed) < self._progress_sec or not line or line == job["progress_sent"]:
                    continue
                text = f"#{job['id']} 进行中（{format_duration(now - claimed)}）：{line}"
            try:
                self._send(job["chat_id"], text)
            except Exception:
                logger.exception("CLAUDE_RELAY: progress message for #%d failed", job["id"])
            self._store.note_progress_sent(job["id"], line, now)

    def _deliver(self, job: dict) -> bool:
        """Send a result: a long one as a file first, when the backend can (else cut short)."""
        body = str(job.get("result_text") or "").strip()
        if self._send_file is None or len(body) <= self._max_chars or job["status"] not in ("done", "failed"):
            return bool(self._send(job["chat_id"], self.result_message(job)))
        path = self._result_file(job)
        if job["id"] not in self._file_sent:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._prune_files()
                path.write_text(self._file_text(job), encoding="utf-8-sig")
                if self._send_file(job["chat_id"], str(path.resolve())):
                    time.sleep(1.0)
                    self._file_sent.add(job["id"])
                else:
                    logger.warning("CLAUDE_RELAY: the result file of #%d was not sent; cutting it short",
                                   job["id"])
            except Exception:
                logger.exception("CLAUDE_RELAY: the result file of #%d failed; cutting it short", job["id"])
        file_name = path.name if job["id"] in self._file_sent else ""
        return bool(self._send(job["chat_id"], self.result_message(job, file_name=file_name)))

    def _result_file(self, job: dict) -> Path:
        return self._files_dir / f"中转结果-{job['id']}.txt"

    def _file_text(self, job: dict) -> str:
        head = self.result_message({**job, "result_text": ""})
        return (f"{head}\n任务：{job['text']}\n项目：{job.get('project') or 'Mac 的默认项目'}\n"
                f"{'─' * 20}\n\n{str(job.get('result_text') or '').strip()}\n")

    def _prune_files(self) -> None:
        cutoff = time.time() - KEEP_FILES_DAYS * 86400
        for old in self._files_dir.glob("中转结果-*.txt"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass

    def result_message(self, job: dict, file_name: str = "") -> str:
        """The result for WeChat; with ``file_name`` (the whole result went as that file) only its start."""
        job_id, status = job["id"], job["status"]
        if status == "lost":
            return (
                f"#{job_id} 和 Mac 失去联系了（{LOST_AFTER_SEC // 60} 分钟没有心跳），可能只做了一半。"
                f"Mac 恢复后如果还在做，结果会照常发回来；要重新执行就回复「{self._command_text(f'重试 {job_id}')}」。"
            )
        try:
            extra = json.loads(job.get("result_json") or "{}")
        except ValueError:
            extra = {}
        head = {"done": "完成", "failed": "失败", "cancelled": "已取消"}.get(status, status)
        duration = float(extra.get("duration_s") or 0)
        head = f"#{job_id} {head}" + (f"（{format_duration(duration)}）" if duration >= 1 else "")
        body = str(job.get("result_text") or "").strip()
        if not body and status == "failed":
            body = str(extra.get("error") or "没有返回内容")
        if file_name:
            body = body[:SUMMARY_CHARS].rstrip() + f"…\n（结果太长，完整内容在上面的文件「{file_name}」里）"
        elif len(body) > self._max_chars:
            where = f"，完整结果在 Mac：{extra['log_path']}" if extra.get("log_path") else ""
            body = body[: self._max_chars].rstrip() + f"\n…（太长，已截断{where}）"
        if extra.get("followups_job"):
            body += f"\n（你在中途发的消息没赶上，已作为 #{extra['followups_job']} 接着做）"
        if extra.get("denied") and status in ("done", "failed"):
            # The task is over: a 「同意」 now answers nothing, so say how to do it again.
            body += (f"\n要重做就回复「{self._command_text(f'重试 {job_id}')}」，"
                     "需要批准时再回「同意」。")
        return f"{head}\n{body.strip()}" if body.strip() else head

    def _command_text(self, words: str) -> str:
        """A command as the person types it: 「重试 17」, or 「cc 重试 17」 when the prefix is needed."""
        return words if self._relay_only else f"{self._prefix_word} {words}"
