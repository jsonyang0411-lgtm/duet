"""What a relay message asks for: ``cc <task>`` and a few control words.

    cc 在 douyin 项目里跑测试      a task (Claude works in the worker's default folder)
    cc @douyin 跑测试               a task in the folder the Mac calls "douyin"
    cc 状态                         the last jobs and whether the Mac is online
    cc 取消 17 / cc 取消            cancel job 17 / the latest open job
    cc 重试 17                      queue job 17 again
    cc 新对话                       the next task starts a new Claude session
    cc / cc 帮助                    the help text
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PROJECT = re.compile(r"^@([A-Za-z0-9_.-]+)\s+(.+)$", re.S)
# "取消 17", "取消#17", "取消" -- but "取消一下那个提交" is a task.
_CONTROL = re.compile(r"^(取消|停止|cancel|stop|重试|retry)\s*(?:#?(\d+))?$", re.I)
_CANCEL = {"取消", "停止", "cancel", "stop"}

HELP_WORDS = {"帮助", "help", "命令", "?", "？"}
STATUS_WORDS = {"状态", "status", "列表", "list"}
RESET_WORDS = {"新对话", "新会话", "重置", "reset", "new"}


@dataclass(frozen=True)
class RelayCommand:
    kind: str               # task, status, cancel, retry, reset, help
    text: str = ""          # the task for Claude
    project: str = ""       # a project alias the worker maps to a folder
    job_id: int | None = None


def prefix_pattern(prefix: str) -> re.Pattern:
    """``cc`` at the start, alone or followed by a space or punctuation (not "ccc")."""
    return re.compile(
        rf"^\s*/?{re.escape(prefix)}(?![A-Za-z0-9_])\s*[:：,，]?\s*(.*)$", re.S | re.I,
    )


def parse(body: str) -> RelayCommand:
    """The text after the prefix."""
    body = body.strip()
    word = body.lower()
    if not body or word in HELP_WORDS:
        return RelayCommand("help")
    if word in STATUS_WORDS:
        return RelayCommand("status")
    if word in RESET_WORDS:
        return RelayCommand("reset")
    m = _CONTROL.match(body)
    if m:
        kind = "cancel" if m.group(1).lower() in _CANCEL else "retry"
        return RelayCommand(kind, job_id=int(m.group(2)) if m.group(2) else None)
    m = _PROJECT.match(body)
    if m:
        return RelayCommand("task", text=m.group(2).strip(), project=m.group(1))
    return RelayCommand("task", text=body)
