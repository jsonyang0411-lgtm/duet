"""Turn a decision-trace record into a sentence a person can read.

The trace (``data/decision_trace.jsonl``) says *what* happened to every message:
an outcome, a reason code, the steps it went through, which routing step ended
it.  This module only translates; nothing here changes behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Iterator

# reason code -> what it means, in plain words
REASONS = {
    "self_message": "这是机器人自己发的消息，不处理",
    "duplicate_or_store_error": "重复消息或入库失败，没有继续处理",
    "claude_relay": "cc 开头的私聊指令，转给 Mac 上的 Claude Code",
    "prm_copilot": "已登记达人的私聊：不回复，静默交给 Mac 起草，草稿在控制台等运营审批",
    "prm_edge": "名单里达人的私聊：不回复、不存进聊天记录，转给 Mac 的业务服务处理",
    "wechat_review": "管理员在自己的私聊里回草稿通知（微信里审批草稿）：按回复发出、不发或改了再发，回一句结果，不存进聊天记录",
    "admin_command": "管理员私聊发的账号命令（加好友、自动通过好友等），已执行",
    "silent_end": "不是中转指令、达人消息或管理员命令（群聊一律在这里），静默结束，不回复",
    "no_handler": "没有任何处理步骤认领这条消息",
    "reply": "已回复",
}

# the routing step that ended the route (src/routing/steps.py)
STEPS = {
    "self_message": "自己的消息",
    "edge": "转给 Mac 业务服务",
    "wechat_review": "微信里审批草稿",
    "persist": "入库",
    "claude_relay": "转给 Mac",
    "prm": "达人副驾驶",
    "admin_command": "管理员命令",
    "silence": "静默结束",
}


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value, limit: int = 80) -> str:
    return str(value if value is not None else "")[:limit]


def explain(record: dict) -> dict:
    """A readable account of one decision record.

    The trace is a file on disk that other versions wrote and people edit, so
    every field is checked before it is used.
    """
    record = _dict(record)
    extra = _dict(record.get("extra"))
    reason = _text(record.get("reason"), 60)
    outcome = _text(record.get("outcome"), 20)
    ended_by = _text(extra.get("ended_by"), 30)
    steps = [s for s in record.get("steps") or [] if isinstance(s, dict)] if isinstance(record.get("steps"), list) else []
    trail = []
    for step in steps:
        code = _text(step.get("code"), 40)
        detail = {k: v for k, v in step.items() if k != "code"}
        trail.append(code + (f" {_text(detail, 120)}" if detail else ""))
    lines = [f"结果：{'回复了' if outcome == 'reply' else '没有回复' if outcome in ('silent', 'ignored') else outcome}"
             f"；原因：{REASONS.get(reason, reason or '未记录')}（{reason}）"]
    if ended_by:
        lines.append(f"由哪一步结束：{STEPS.get(ended_by, ended_by)}（{ended_by}）")
    return {
        "message_id": _text(record.get("message_id"), 80),
        "chat": _text(record.get("group") or record.get("chat_id"), 80),
        "sender": _text(record.get("sender_name"), 40),
        "ts": record.get("ts") if isinstance(record.get("ts"), (int, float)) else None,
        "outcome": outcome,
        "reason": reason,
        "reason_text": REASONS.get(reason, reason),
        "ended_by": ended_by,
        "summary": lines,
        "trail": trail,
        "content": _text(record.get("content"), 300),
    }


# How much of the trace one request may read, newest part first.  The trace is
# rotated at 20 MB with three backups; a page of recent decisions never needs more.
MAX_SCAN_BYTES = 16 * 1024 * 1024


def _lines_newest_first(path: Path, backups: int = 3, budget: int | None = None) -> Iterator[str]:
    budget = MAX_SCAN_BYTES if budget is None else budget
    files = [path] + [path.with_name(f"{path.name}.{i}") for i in range(1, backups + 1)]
    for file in files:
        if budget <= 0:
            return
        try:
            size = file.stat().st_size
            start = max(0, size - budget)
            with open(file, "rb") as fh:
                fh.seek(start)
                data = fh.read(budget)
        except OSError:
            continue
        budget -= len(data)
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if start > 0 and lines:
            lines = lines[1:]                      # the first line was cut in half
        yield from reversed(lines)


def iter_records(path: Path, backups: int = 3) -> Iterator[dict]:
    """Decision records, newest first (rotated backups included, bounded read)."""
    for line in _lines_newest_first(path, backups):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if isinstance(record, dict) and record.get("kind") == "decision":
            yield record


def recent(path: Path, *, limit: int = 50, outcome: str = "", chat_id: str | Iterable[str] = "",
           message_id: str = "", since: float | None = None) -> list[dict]:
    """The newest decisions first, optionally filtered, each already explained."""
    limit = max(1, min(500, int(limit)))
    chats = {chat_id} if isinstance(chat_id, str) else set(chat_id)
    chats.discard("")
    found: list[dict] = []
    for line in _lines_newest_first(Path(path)):
        # cheap tests on the raw line first: most lines are not the one asked for
        if message_id and message_id not in line:
            continue
        if chats and not any(c in line for c in chats):
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(record, dict) or record.get("kind") != "decision":
            continue
        if message_id and record.get("message_id") != message_id:
            continue
        if outcome and record.get("outcome") != outcome:
            continue
        if chats and not ({record.get("chat_id"), record.get("group")} & chats):
            continue
        if since is not None:
            try:
                if float(record.get("ts", 0) or 0) < since:
                    break                          # newest first: everything further back is older
            except (TypeError, ValueError):
                continue
        found.append(explain(record))
        if len(found) >= limit:
            break
    return found


def format_text(items: Iterable[dict]) -> str:
    out = []
    for item in items:
        out.append(f"== {item['message_id']}  [{item['chat']}] {item['sender']}")
        if item["content"]:
            out.append(f"   「{item['content']}」")
        out.extend(f"   {line}" for line in item["summary"])
        if item["trail"]:
            out.append("   经过：" + " → ".join(item["trail"]))
        out.append("")
    return "\n".join(out)
