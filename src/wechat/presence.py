"""Is this PC's WeChat logged in, and when was it not (Windows, WCDB backend).

PC WeChat that is not logged in receives nothing.  A creator's messages of that time reach the phone
only, and WeChat brings them to the PC after the next login only when 「同步最近的聊天记录」 is ticked on
the phone (README 常见问题, confirmed on the real machine 2026-10-28); the rows it brings have new ids
and their old times.  So a stretch without PC WeChat is written down (a *gap*) and shown in the
console until an operator says she has looked (看过了); when WeChat is back, the backend reads the
creators' chats back (``WcdbBackend.catch_up_after_gap``) and, with the WeChat edge, the Mac hears of
the gap (a ``wechat_gap`` event, edge-protocol.md 4.1.2).

The signals (``probe``, every CHECK_SEC seconds, Windows only):

- WeChat's process (``Weixin.exe`` / ``WeChat.exe``, psutil): none running is offline at once.  Its
  start time moving on between two probes means WeChat was restarted in between (an update): a gap
  even when no probe caught it away.
- Its main window (``WcdbBackend.wechat_window_found``: a WeChat window with the main title or a Qt /
  Chrome class, at least 200×200): missing in OFFLINE_CONFIRM probes in a row is offline (one miss is
  not).  While a send holds the window there is no answer, and that probe does not count.
- Not the database: it is read straight from WeChat's files and stays readable while WeChat is logged
  out, and what came in while it could not be read is read later (the restart catch-up); the edge
  reports it as ``db: unreadable``.

Pushed off by a login elsewhere (被挤下线) or logged out from the phone: if WeChat then shows only its
login window and that window passes for the main window, no gap is seen.  To be checked on the real
machine: each change is logged with what the probe saw.

A gap is kept in the bot's database (DB_PATH, ``wechat_gaps``): from the last time WeChat was confirmed
online (written down every LAST_ONLINE_WRITE_SEC while it is, ``wechat_presence``) to the first time it
is confirmed online again.  ``reason`` (edge-protocol.md, stable values):

- ``offline_while_running`` (运行中掉线): seen going away while the bot ran;
- ``stopped_with_bot`` (和机器人一起停了): found at the bot's start: WeChat's process started after the
  last time the bot saw it online (the PC restarted, WeChat was updated or quit while the bot was
  stopped), or WeChat is not there at the start.  It ends at WeChat's start when WeChat is online at
  the bot's start, else when it is seen online.  The bot alone being stopped (WeChat logged in all
  along) is no gap: the restart catch-up hands on what came in meanwhile (PRM_CATCHUP_HOURS).

``on_offline(gap)`` hears a gap opened (the backend hands on rows written since its start that show up
late), ``on_back(gap)`` a gap closed.  Nothing here stops the bot: a probe or a hook that fails is logged.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from ..edge import protocol as P

logger = logging.getLogger(__name__)

CHECK_SEC = 30                   # how often WeChat is looked at
OFFLINE_CONFIRM = 2              # probes in a row without the main window before it counts as offline
LAST_ONLINE_WRITE_SEC = 60       # how often "WeChat was online at …" is written down while it is
RESTART_SLACK_SEC = 2            # a process start time later than the last one by more than this: restarted
KEEP_SEEN_DAYS = 30              # gaps an operator has seen are deleted this long after
WECHAT_PROCESSES = ("weixin.exe", "wechat.exe")

OFFLINE_WHILE_RUNNING = P.GAP_OFFLINE_WHILE_RUNNING
STOPPED_WITH_BOT = P.GAP_STOPPED_WITH_BOT


# ── what is seen ────────────────────────────────────────────────────────────

def wechat_start_times() -> Optional[list]:
    """When each running WeChat process started (an empty list: none runs); None without psutil."""
    try:
        import psutil
    except Exception:
        return None
    times = []
    for proc in psutil.process_iter(["name", "create_time"]):
        try:
            if str(proc.info.get("name") or "").lower() in WECHAT_PROCESSES:
                times.append(float(proc.info.get("create_time") or 0))
        except Exception:
            continue
    return times


def probe(window_found: Callable[[], Optional[bool]], start_times: Callable[[], Optional[list]] = wechat_start_times) -> dict:
    """``{"online": True | False | None, "process": True | False | None, "started_at": float | None,
    "detail": 中文}``: is PC WeChat logged in now?  ``online`` None: no answer this time (a send holds
    the window); ``process`` None: psutil is missing; ``started_at``: the oldest WeChat process."""
    starts = start_times()
    if starts is not None and not starts:
        return {"online": False, "process": False, "started_at": None, "detail": "微信进程不在（电脑微信没开或退出了）"}
    started = min(starts) if starts else None
    process = None if starts is None else True
    try:
        window = window_found()
    except Exception:
        window = False
    if window is None:
        return {"online": None, "process": process, "started_at": started, "detail": "正在发消息，这次不看窗口"}
    if not window:
        return {"online": False, "process": process, "started_at": started,
                "detail": "找不到微信主窗口（没登录、被挤下线或窗口被关了）"}
    return {"online": True, "process": process, "started_at": started, "detail": "微信在线"}


# ── the bot's database ──────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wechat_gaps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at   REAL,
    reason     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    found_at   REAL NOT NULL,
    seen_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_wechat_gaps_ended ON wechat_gaps(ended_at);
CREATE TABLE IF NOT EXISTS wechat_presence (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _gap_out(row) -> dict:
    return {"id": int(row["id"]), "from": float(row["started_at"]),
            "to": None if row["ended_at"] is None else float(row["ended_at"]),
            "reason": str(row["reason"]), "detail": str(row["detail"] or ""), "found_at": float(row["found_at"]),
            "seen_at": None if row["seen_at"] is None else float(row["seen_at"])}


class PresenceStore:
    """The gaps (``wechat_gaps``) and the last time WeChat was confirmed online (``wechat_presence``),
    in the bot's database (DB_PATH) on a connection of its own."""

    def __init__(self, path: str = "data/messages.db"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def last_online(self) -> Optional[float]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM wechat_presence WHERE key = 'last_online'").fetchone()
        try:
            return float(row["value"]) if row else None
        except (TypeError, ValueError):
            return None

    def set_last_online(self, at: float) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO wechat_presence (key, value) VALUES ('last_online', ?)",
                               (repr(float(at)),))
            self._conn.commit()

    def open_gap(self) -> Optional[dict]:
        """The gap going on (WeChat not confirmed online since), or None."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM wechat_gaps WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
                                     ).fetchone()
        return _gap_out(row) if row else None

    def add_gap(self, started_at: float, reason: str, detail: str, found_at: float) -> dict:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO wechat_gaps (started_at, reason, detail, found_at) VALUES (?, ?, ?, ?)",
                (float(started_at), reason, str(detail or ""), float(found_at)))
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM wechat_gaps WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _gap_out(row)

    def close_gap(self, gap_id: int, ended_at: float) -> Optional[dict]:
        """End a gap (never before it began); the gap, or None when it was not open."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE wechat_gaps SET ended_at = MAX(started_at, ?) WHERE id = ? AND ended_at IS NULL",
                (float(ended_at), int(gap_id)))
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM wechat_gaps WHERE id = ?", (int(gap_id),)).fetchone()
        return _gap_out(row) if cur.rowcount == 1 and row else None

    def gap(self, gap_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM wechat_gaps WHERE id = ?", (int(gap_id),)).fetchone()
        return _gap_out(row) if row else None

    def unseen(self, limit: int = 20) -> list:
        """The ended gaps no operator has marked 看过了, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM wechat_gaps WHERE ended_at IS NOT NULL AND seen_at IS NULL ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [_gap_out(r) for r in rows]

    def mark_seen(self, gap_id: int, at: float) -> bool:
        """看过了: an ended gap is not shown any more (one going on cannot be)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE wechat_gaps SET seen_at = ? WHERE id = ? AND ended_at IS NOT NULL AND seen_at IS NULL",
                (float(at), int(gap_id)))
            self._conn.commit()
        return cur.rowcount == 1

    def prune(self, before: float) -> int:
        """Delete the gaps seen before ``before``."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM wechat_gaps WHERE seen_at IS NOT NULL AND seen_at < ?",
                                     (float(before),))
            self._conn.commit()
        return cur.rowcount


# ── the watch ───────────────────────────────────────────────────────────────

class WechatPresence:
    def __init__(self, store: PresenceStore, probe: Callable[[], dict], *, clock: Callable[[], float] = time.time,
                 interval: float = CHECK_SEC, on_offline: Optional[Callable[[dict], None]] = None,
                 on_back: Optional[Callable[[dict], None]] = None):
        self._store = store
        self._probe = probe
        self._clock = clock
        self._interval = max(1.0, float(interval))
        self.on_offline = on_offline
        self.on_back = on_back
        self._lock = threading.Lock()
        self._last_online: Optional[float] = None     # the last probe of this run that saw WeChat online
        self._written = 0.0                           # when last_online was last written down
        self._started_at: Optional[float] = None      # WeChat's process start the last probe saw
        self._misses = 0                              # probes in a row that saw WeChat offline
        self._from: Optional[float] = None            # where the next gap starts, when not the last online
        self._reason = OFFLINE_WHILE_RUNNING          # ... and why
        self._pruned = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def store(self) -> PresenceStore:
        return self._store

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self, run_thread: bool = True) -> None:
        """Look once now (what happened while the bot was stopped), then every CHECK_SEC seconds.  A
        first look that fails (the database busy …) is logged; the watch goes on."""
        try:
            self._startup()
        except Exception:
            logger.exception("WeChat presence: the first look failed; watching from now on")
        if run_thread:
            self._thread = threading.Thread(target=self._run, name="wechat-presence", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.check()
            except Exception:
                logger.exception("WeChat presence: the check failed; trying again in %.0f s", self._interval)

    def _look(self) -> dict:
        try:
            return dict(self._probe() or {})
        except Exception:
            logger.exception("WeChat presence: the probe failed")
            return {"online": None}

    # ── the checks ────────────────────────────────────────────────────────

    def _startup(self) -> None:
        """At the bot's start: a gap left open by the last run goes on; WeChat started after the last
        time the bot saw it online (or not there at all) is a ``stopped_with_bot`` gap."""
        now = self._clock()
        seen = self._look()
        opened = closed = None
        with self._lock:
            online, started = seen.get("online"), seen.get("started_at")
            self._started_at = started
            last = self._store.last_online()
            gap = self._store.open_gap()
            if gap is None and last is not None:
                restarted = started is not None and started > last + RESTART_SLACK_SEC
                if restarted or (online is False and seen.get("process") is False):
                    detail = ("电脑微信在机器人停着的时候重新启动过（比如重启了电脑、升级了微信）" if restarted
                              else "机器人启动时电脑微信没开")
                    gap = opened = self._store.add_gap(last, STOPPED_WITH_BOT, detail, now)
                elif online is False:
                    # WeChat runs since before the bot stopped but its window is not there: if the next
                    # probe agrees, the gap starts at the last time the bot saw it online
                    self._misses, self._from, self._reason = 1, last, STOPPED_WITH_BOT
            if gap is not None:
                if online is True:
                    end = started if started is not None and started > gap["from"] else now
                    closed = self._store.close_gap(gap["id"], end)
                    opened = None
                elif opened is None:
                    opened = gap                       # left open by the last run: still going on
            if online is True:
                self._saw_online(now)
            self._prune(now)
        self._tell(opened, closed, seen)

    def check(self) -> None:
        """One probe: online again ends the gap going on; offline (no process, or no main window
        OFFLINE_CONFIRM times in a row) begins one; WeChat restarted between two probes is a gap too."""
        now = self._clock()
        seen = self._look()
        opened = closed = None
        with self._lock:
            online, started = seen.get("online"), seen.get("started_at")
            restarted = (started is not None and self._started_at is not None
                         and started > self._started_at + RESTART_SLACK_SEC)
            if started is not None:
                self._started_at = started
            gap = self._store.open_gap()
            if online is True:
                self._misses = 0
                if gap is None and restarted and self._last_online is not None:
                    gap = self._store.add_gap(self._last_online, OFFLINE_WHILE_RUNNING,
                                              "电脑微信重新启动过（升级，或者退出后又打开了）", now)
                if gap is not None:
                    closed = self._store.close_gap(gap["id"], now)
                self._saw_online(now)
                self._from, self._reason = None, OFFLINE_WHILE_RUNNING
            elif online is False:
                self._misses += 1
                if gap is None and (seen.get("process") is False or self._misses >= OFFLINE_CONFIRM):
                    since = self._from or self._last_online or self._store.last_online() or now
                    opened = self._store.add_gap(since, self._reason, str(seen.get("detail") or ""), now)
            self._prune(now)
        self._tell(opened, closed, seen)

    def _saw_online(self, now: float) -> None:
        self._last_online = now
        if now - self._written >= LAST_ONLINE_WRITE_SEC:
            self._store.set_last_online(now)
            self._written = now

    def _prune(self, now: float) -> None:
        if now - self._pruned >= 86400:
            self._pruned = now
            self._store.prune(now - KEEP_SEEN_DAYS * 86400)

    def _tell(self, opened: Optional[dict], closed: Optional[dict], seen: dict) -> None:
        """Log a change (with what the probe saw: the real machine shows what being pushed off looks
        like) and tell the hooks, outside the lock."""
        if opened is not None:
            logger.warning("WeChat presence: PC WeChat is not online since %s (%s; %s): gap #%d",
                           _clock_text(opened["from"]), opened["reason"], seen.get("detail") or opened["detail"],
                           opened["id"])
            self._hook(self.on_offline, opened)
        if closed is not None:
            logger.warning("WeChat presence: PC WeChat is online again; gap #%d from %s to %s (%s)", closed["id"],
                           _clock_text(closed["from"]), _clock_text(closed["to"]), closed["reason"])
            self._hook(self.on_back, closed)

    @staticmethod
    def _hook(hook, gap: dict) -> None:
        if hook is None:
            return
        try:
            hook(dict(gap))
        except Exception:
            logger.exception("WeChat presence: a hook failed for gap #%s", gap.get("id"))

    # ── the console ───────────────────────────────────────────────────────

    def console(self) -> dict:
        """``{"open": the gap going on or None, "gaps": the ended ones not seen yet}``."""
        return console_state(self._store, running=True)

    def mark_seen(self, gap_id: int) -> bool:
        return self._store.mark_seen(gap_id, self._clock())


def console_state(store: PresenceStore, *, running: bool) -> dict:
    """What the console shows of the store; a gap going on only while the bot runs (stopped, nobody
    is watching WeChat: it may be back already)."""
    return {"open": store.open_gap() if running else None, "gaps": store.unseen()}


def stored_state(path) -> dict:
    """``console_state`` of the bot's database while the bot is stopped, read only: a database or a
    table that is not there yet is not made (the console asks every few seconds)."""
    empty = {"open": None, "gaps": []}
    path = Path(path)
    if not path.is_file():
        return empty
    try:
        conn = sqlite3.connect(f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return empty
    try:
        conn.row_factory = sqlite3.Row
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'wechat_gaps'").fetchone() is None:
            return empty
        rows = conn.execute("SELECT * FROM wechat_gaps WHERE ended_at IS NOT NULL AND seen_at IS NULL "
                            "ORDER BY id DESC LIMIT 20").fetchall()
        return {"open": None, "gaps": [_gap_out(r) for r in rows]}
    except sqlite3.Error:
        return empty
    finally:
        conn.close()


# ── what the operators read ─────────────────────────────────────────────────

WHY = {
    OFFLINE_WHILE_RUNNING: "机器人在运行，发现电脑微信不在线了（退出了、没登录或被挤下线）。",
    STOPPED_WITH_BOT: "机器人停着的时候电脑微信也不在线（比如重启了电脑、升级了微信，或者退出了微信）。",
}
CHECK_PHONE = "这段时间达人发来的消息，机器人可能没收到：请在手机微信上把达人的私聊看一遍，没回的直接在手机上回。"
SYNC_HINT = ("重新登录电脑微信时，请在手机的确认页上勾选「同步最近的聊天记录」，这样掉线期间的消息会同步到电脑，"
             "机器人会自动补交成草稿。")
OPEN_HINT = ("达人现在发来的消息机器人收不到。请尽快在这台电脑上重新登录微信，登录时在手机的确认页上勾选"
             "「同步最近的聊天记录」；这段时间的消息请先在手机微信上看一遍。")


def _clock_text(at: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(float(at)))


def _span_text(start: float, end: float) -> str:
    same_day = time.localtime(start)[:3] == time.localtime(end)[:3]
    return f"{_clock_text(start)} 到 {time.strftime('%H:%M' if same_day else '%m-%d %H:%M', time.localtime(end))}"


def _duration_text(seconds: float) -> str:
    minutes = max(1, int(round(max(0.0, seconds) / 60)))
    if minutes < 60:
        return f"约 {minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"约 {hours} 小时" + (f" {rest} 分钟" if rest else "")
    return f"约 {hours // 24} 天"


def gap_notice(gap: dict) -> dict:
    """``{"title", "text"}`` of a gap for the operators: when PC WeChat was not online, that creators'
    messages of that time may not have reached the bot (look on the phone), and to tick 「同步最近的聊天
    记录」 when logging in again.  A gap going on (``to`` None) says it is not over."""
    start, end = float(gap["from"]), gap.get("to")
    why = WHY.get(str(gap.get("reason") or ""), "")
    if end is None:
        return {"title": f"电脑微信从 {_clock_text(start)} 起不在线，还没恢复", "text": why + OPEN_HINT}
    end = float(end)
    return {"title": f"电脑微信 {_span_text(start, end)} 不在线（{_duration_text(end - start)}）",
            "text": why + CHECK_PHONE + SYNC_HINT}


def gap_for_console(gap: dict) -> dict:
    """A gap as the console gets it: its id, times, reason and words."""
    return {"id": gap.get("id"), "from": gap.get("from"), "to": gap.get("to"), "reason": gap.get("reason"),
            **gap_notice(gap)}
