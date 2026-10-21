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
    "welcome": "有新成员加入，发送了欢迎语",
    "full_feed_chat": "逐条监听的人设：把这条消息交给模型决定要不要回",
    "full_feed_local_skip": "逐条监听的人设：本地规则认为不值得回（太短、一个字的反应、冷却中等），没有调用模型",
    "image_question": "问了图片相关的问题，用看图回答",
    "quoted_image_question": "引用了一张图片并提问，用看图回答",
    "image_followup": "紧接着机器人回复后的图片/表情，按看图跟进回答",
    "image_message_no_followup": "收到图片/表情，但不是对机器人说的，没有回复",
    "private_chat": "私聊，直接交给模型",
    "quote_of_other_member": "引用了别的群友的话，这条是说给他的，不是对机器人说的",
    "stale_mention_ignored": "@ 的消息太旧（可能是启动时重放的历史），忽略",
    "feishu_export": "同步到飞书的命令",
    "help_command": "帮助命令",
    "lots_command": "抽签命令",
    "tarot_command": "塔罗命令",
    "admin_command": "管理员命令",
    "todo_command": "待办命令",
    "chat_request": "被 @ / 点名 / 关键词 / 跟聊触发，交给模型回答",
    "ai_reply": "模型给出了回复",
    "ai_intentional_silence": "模型判断不用回复（保持沉默）",
    "ai_blank_reply": "模型返回空内容",
    "ai_blank_direct_fallback": "被直接叫到但模型没给出可用回复，发了兜底句",
    "ai_blank_forced_fallback": "必须回复的群里模型没给出可用回复，发了兜底句",
    "ai_addressee_others": "模型判断这句话是对别人说的，没有回复",
    "ai_unusable_answer": "模型的答案无法解析，没有发送",
    "ai_error": "调用模型出错",
    "proactive_gate_closed": "主动发言的闸门没开（节奏、间隔、配额或打分不够）",
    "proactive_no_context": "主动发言时没有可用的上下文",
    "proactive_api_error": "主动发言时模型出错",
    "proactive_unusable_answer": "主动发言时模型的答案无法解析，没有发送",
    "proactive_model_silence": "闸门开了，但模型选择不插话",
    "proactive_reply": "主动插话",
    "no_handler": "没有任何处理步骤认领这条消息",
    "reply": "已回复",
}

# the routing step that ended the route (src/routing/steps.py)
STEPS = {
    "self_message": "自己的消息",
    "persist": "入库",
    "memory_hooks": "记忆",
    "full_feed": "逐条监听",
    "quoted_image": "引用图片",
    "image_message": "图片/表情",
    "close_image_window": "图片窗口",
    "private_chat": "私聊",
    "quote_of_other_member": "引用他人",
    "welcome": "欢迎新成员",
    "detect_trigger": "触发判断",
    "mention_gate": "@ 检查",
    "feishu_export": "飞书同步",
    "help_command": "帮助",
    "fun_commands": "抽签/塔罗",
    "admin_command": "管理员命令",
    "todo_command": "待办",
    "chat_request": "聊天",
    "ambient": "主动发言",
    "finish": "整理并发送",
}

ROUTES = {
    "at": "@ 机器人", "sticky": "上一条 @ 后的接话", "keyword": "关键词", "followup": "跟聊",
    "name_call": "喊了机器人的名字", "ambient": "没人叫它（旁听）",
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
    route, ended_by = _text(extra.get("route"), 30), _text(extra.get("ended_by"), 30)
    steps = [s for s in record.get("steps") or [] if isinstance(s, dict)] if isinstance(record.get("steps"), list) else []
    trail = []
    for step in steps:
        code = _text(step.get("code"), 40)
        detail = {k: v for k, v in step.items() if k not in ("code", "sources")}
        trail.append(code + (f" {_text(detail, 120)}" if detail else ""))
    decision = _dict(extra.get("decision"))
    addressing = _dict(extra.get("addressing"))
    lines = []
    if route:
        lines.append(f"怎么被注意到：{ROUTES.get(route, route)}")
    if addressing:
        signals = addressing.get("signals") if isinstance(addressing.get("signals"), list) else []
        lines.append(f"寻址等级：{_text(addressing.get('level', '?'), 20)}（{', '.join(_text(x, 20) for x in signals) or '无信号'}）")
    lines.append(f"结果：{'回复了' if outcome == 'reply' else '没有回复' if outcome in ('silent', 'ignored') else outcome}"
                 f"；原因：{REASONS.get(reason, reason or '未记录')}（{reason}）")
    if ended_by:
        lines.append(f"由哪一步结束：{STEPS.get(ended_by, ended_by)}（{ended_by}）")
    if decision:
        lines.append(
            f"模型的判断：{_text(decision.get('action', '?'), 20)}，对象 {_text(decision.get('addressee', '?'), 20)}，"
            f"状态 {_text(decision.get('status', '?'), 20)}，调用 {_text(decision.get('calls', '?'), 10)} 次"
            + (f"；理由：{_text(decision['reason'], 80)}" if decision.get("reason") else "")
        )
    follow = _dict(decision.get("follow_up"))
    if follow:
        waits = {"sender": "只等刚回答的那个人", "anyone": "等任何人", "none": "不等任何人"}.get(
            follow.get("expect"), _text(follow.get("expect"), 20))
        lines.append(f"回复后的跟聊：{waits}（最多 {_text(follow.get('seconds'), 10)} 秒）")
    memory = [s for s in steps if s.get("code") == "memory_v2"]
    if memory:
        m = memory[0]
        lines.append(f"用到的记忆：事实 {_text(m.get('facts', 0), 10)} 条，摘要 {_text(m.get('episodes', 0), 10)} 条，旧消息 {_text(m.get('messages', 0), 10)} 条")
    return {
        "message_id": _text(record.get("message_id"), 80),
        "chat": _text(record.get("group") or record.get("chat_id"), 80),
        "sender": _text(record.get("sender_name"), 40),
        "ts": record.get("ts") if isinstance(record.get("ts"), (int, float)) else None,
        "outcome": outcome,
        "reason": reason,
        "reason_text": REASONS.get(reason, reason),
        "ended_by": ended_by,
        "route": route,
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
