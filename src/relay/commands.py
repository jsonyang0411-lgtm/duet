"""What a relay message asks for: ``cc <task>`` and a few control words.

    cc 在 douyin 项目里跑测试      a task (Claude works in the worker's default folder)
    cc @douyin 跑测试               a task in the folder the Mac calls "douyin"
    cc 状态                         the last jobs and whether the Mac is online
    cc 取消 17 / cc 取消            cancel job 17 / the latest open job
    cc 重试 17                      queue job 17 again
    cc 新对话                       the next task starts a new Claude session
    cc 项目                         the projects the Mac offers, and the current one
    cc 切到 douyin                  later tasks run in "douyin" (a new session)
    cc 新任务 …                     a task of its own even while another one runs
                                    (otherwise a task sent meanwhile goes to the running one)
    cc / cc 帮助                    the help text

With CLAUDE_RELAY_ONLY=on the "cc" can be left out (see ClaudeRelay.handle).

While Claude waits for an approval (``parse_answer``): 同意 / 拒绝, optionally
with the job number ("同意 17"); no "cc" is needed for these.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PROJECT = re.compile(r"^@([^\s/\\:]+)\s+(.+)$", re.S)
# "取消 17", "取消#17", "取消" -- but "取消一下那个提交" is a task.
_CONTROL = re.compile(r"^(取消|停止|cancel|stop|重试|retry)\s*(?:#?(\d+))?$", re.I)
_CANCEL = {"取消", "停止", "cancel", "stop"}
# "新任务 跑测试", "排队：跑测试" -- but "排队中时再发" is a task of its own words.
_NEW_TASK = re.compile(r"^(?:新任务|排队)(?:\s*[:：]\s*|\s+)(.+)$", re.S)
_SWITCH = re.compile(
    r"^(?:切到|切换到|切回|切回到|进入|cd|use)\s*@?([A-Za-z0-9_.-]+|[\u4e00-\u9fa5]{1,20})$"
    r"|^(?:切换)\s*@?([A-Za-z0-9_.-]+)$",
    re.I,
)

HELP_WORDS = {"帮助", "help", "命令", "?", "？"}
STATUS_WORDS = {"状态", "status", "列表", "list"}
RESET_WORDS = {"新对话", "新会话", "重置", "reset", "new"}
PROJECT_WORDS = {"项目", "项目列表", "当前项目", "projects", "project"}

ALLOW_WORDS = {"同意", "批准", "允许", "可以", "好", "好的", "行", "ok", "okay", "yes", "y", "approve", "allow"}
DENY_WORDS = {"拒绝", "不同意", "不批准", "不允许", "不行", "不可以", "不要", "no", "n", "deny"}
_ANSWER = re.compile(r"^(\S+?)\s*[#＃]?(\d+)?\s*[。.!！]?$")


@dataclass(frozen=True)
class RelayCommand:
    kind: str               # task, status, cancel, retry, reset, help, projects, switch
    text: str = ""          # the task for Claude
    project: str = ""       # a project alias the worker maps to a folder
    job_id: int | None = None
    queue: bool = False     # "新任务 …": never a follow-up of the running task


def prefix_pattern(prefix: str) -> re.Pattern:
    """``cc`` at the start, alone or followed by a space or punctuation (not "ccc")."""
    return re.compile(
        rf"^\s*/?{re.escape(prefix)}(?![A-Za-z0-9_])\s*[:：,，]?\s*(.*)$", re.S | re.I,
    )


def parse_answer(body: str) -> tuple[str, int | None] | None:
    """``("allow" | "deny", job number or None)`` for an answer to an approval request, else None."""
    m = _ANSWER.match(body.strip())
    if not m:
        return None
    word = m.group(1).lower()
    if word in ALLOW_WORDS:
        decision = "allow"
    elif word in DENY_WORDS:
        decision = "deny"
    else:
        return None
    return decision, int(m.group(2)) if m.group(2) else None


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
    if word in PROJECT_WORDS:
        return RelayCommand("projects")
    m = _SWITCH.match(body)
    if m:
        project = m.group(1) or m.group(2)
        return RelayCommand("switch", project=project)
    m = _CONTROL.match(body)
    if m:
        kind = "cancel" if m.group(1).lower() in _CANCEL else "retry"
        return RelayCommand(kind, job_id=int(m.group(2)) if m.group(2) else None)
    m = _NEW_TASK.match(body)
    if m:
        inner = parse(m.group(1))
        if inner.kind == "task":
            return RelayCommand("task", text=inner.text, project=inner.project, queue=True)
    m = _PROJECT.match(body)
    if m:
        return RelayCommand("task", text=m.group(2).strip(), project=m.group(1))
    return RelayCommand("task", text=body)
