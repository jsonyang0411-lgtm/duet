"""交给 Claude 回复（src/influencer/claude_brain.py）：怎么调用 claude -p、回复发出前的检查、
出错时退回规则流程，以及谈佣金仍由代码处理。claude -p 用替身代替，不真的调用。"""

import json
import subprocess
import time
from pathlib import Path

import pytest

from src.influencer.claude_brain import NO_KNOWLEDGE, ClaudeBrain, review_reply
from src.influencer.handler import COMMISSION_REPLY, HOLDING_REPLY, InfluencerBusinessHandler, build_influencer_handler
from src.influencer.mcp_server import ALLOWED_TOOLS
from src.influencer.settings import PERSONAL_INFO_RETENTION_SECONDS
from src.influencer.store import SOURCE_BOT, SOURCE_CONSOLE, InfluencerStore
from src.influencer.types import GateStatus
from test_influencer_acceptance import INFLUENCER, SAMPLE_REQUEST, FakeERP, FakeXHS, run
from test_influencer_store import NEXT_YEAR, make_packet

ROOT = Path(__file__).resolve().parent.parent


class Raw(str):
    """原样输出的 stdout（不包成 claude -p 的 JSON）。"""


class FakeClaude:
    """claude -p 替身：记下每次调用，按顺序给出回复。回复是异常时就抛出。"""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, args, prompt, timeout, cwd):
        self.calls.append({"args": list(args), "prompt": prompt, "cwd": cwd})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, Raw):
            return str(reply)
        if isinstance(reply, str):
            return json.dumps({"type": "result", "is_error": False, "result": reply}, ensure_ascii=False)
        return json.dumps(reply, ensure_ascii=False)


@pytest.fixture
def setup(tmp_path):
    db_path = str(tmp_path / "messages.db")
    store = InfluencerStore(db_path)
    store.add_contacts([INFLUENCER], source=SOURCE_CONSOLE)

    def make(*replies, knowledge=None):
        if knowledge is not None:
            (tmp_path / "influencer_knowledge.md").write_text(knowledge, encoding="utf-8")
        claude = FakeClaude(*replies)
        brain = ClaudeBrain(store, db_path, claude_path="claude", runner=claude)
        handler = InfluencerBusinessHandler(store, erp_client=FakeERP(), xhs_client=FakeXHS(), brain=brain)
        return handler, claude

    return make


def say(handler, text):
    return run(handler.process_incoming_message(INFLUENCER, text, nickname="小美"))


# ---- 调用方式 ----

def test_claude_gets_only_our_tools_and_none_of_its_own(setup):
    handler, claude = setup("老师好呀~")
    say(handler, "你好")
    args = claude.calls[0]["args"]
    assert args[:2] == ["claude", "-p"]
    assert args[args.index("--tools") + 1] == "", "Claude Code 自带的工具全部关掉"
    assert "--strict-mcp-config" in args
    assert args[args.index("--max-turns") + 1] == "6", "工具调用的轮数有上限"
    assert args[args.index("--allowedTools") + 1:] == list(ALLOWED_TOOLS)
    config = json.loads(args[args.index("--mcp-config") + 1])
    assert INFLUENCER in config["mcpServers"]["influencer"]["args"], "工具绑定在这位达人身上"


def test_claude_does_not_run_inside_the_project(setup):
    handler, claude = setup("老师好呀~")
    say(handler, "你好")
    assert not Path(claude.calls[0]["cwd"]).resolve().is_relative_to(ROOT), "免得读到项目的 CLAUDE.md"


def test_the_business_knowledge_goes_into_the_rules(setup):
    handler, claude = setup("老师好呀~", knowledge="品牌：示例美妆。主推 VC精华液。")
    say(handler, "你好")
    system = claude.calls[0]["args"][claude.calls[0]["args"].index("--system-prompt") + 1]
    assert "示例美妆" in system and "不谈佣金比例" in system


def test_without_business_knowledge_claude_is_told_to_hand_over(setup):
    handler, claude = setup("老师好呀~")
    say(handler, "你好")
    assert NO_KNOWLEDGE in claude.calls[0]["args"][claude.calls[0]["args"].index("--system-prompt") + 1]


def test_claude_remembers_the_conversation(setup):
    handler, claude = setup("老师好呀~ 有什么可以帮您", "有的老师，VC精华液有货哦")
    assert say(handler, "你好") == "老师好呀~ 有什么可以帮您"
    say(handler, "VC精华液有货吗")
    prompt = claude.calls[1]["prompt"]
    assert "达人：你好" in prompt and "我们：老师好呀~ 有什么可以帮您" in prompt
    assert "<达人消息>\nVC精华液有货吗\n</达人消息>" in prompt


# ---- 谈佣金仍由代码处理 ----

def test_a_commission_request_never_reaches_claude(setup):
    handler, claude = setup()
    assert say(handler, "佣金能不能高点呀") == COMMISSION_REPLY
    assert claude.calls == []


def test_someone_not_on_the_list_never_reaches_claude(setup):
    handler, claude = setup()
    assert run(handler.process_incoming_message("wxid_stranger", "你好")) is None
    assert claude.calls == []


# ---- 回复检查 ----

def test_a_reply_that_promises_too_much_is_not_sent(setup):
    handler, _ = setup("好的老师，明天就给您发顺丰~")
    assert say(handler, "什么时候能寄") == HOLDING_REPLY
    [item] = handler.store.list_inbox()
    assert "承诺了发货或到货时间" in item["content"] and "明天就给您发顺丰" in item["content"]


@pytest.mark.parametrize("reply, problem", [
    ("老师放心，明天就给您寄出~", "承诺了发货或到货时间"),
    ("已经安排好啦老师", "还没发货就说已安排或已发货"),
    ("给老师走顺丰哦", "还没发货就提到快递公司"),
    ("老师这么优秀，审核一定能通过的", "承诺了审核结果"),
    ("老师可以给到 20% 哦", "提到了佣金数字或加佣金"),
    ("我帮老师申请加佣", "提到了佣金数字或加佣金"),
    ("我是 AI 助手，可以帮您查询", "暴露了自己是 AI 或内部工具"),
])
def test_review_blocks_promises_commission_and_self_disclosure(reply, problem):
    assert problem in review_reply(reply, shipped=False)


def test_review_blocks_a_wall_of_text():
    assert "回复太长" in review_reply("好" * 500, shipped=False)


@pytest.mark.parametrize("reply", [
    "老师好呀~ 请问想申请哪一款产品的样品呢？",
    "已经提交审核啦，审核结果第一时间告诉老师~",
    "审核通过后我们会第一时间通知老师哦",
    "老师今天收到了吗？",
])
def test_review_lets_ordinary_replies_through(reply):
    assert review_reply(reply, shipped=False) == ""


def test_after_shipping_the_courier_can_be_mentioned():
    assert review_reply("老师的样品已经寄出啦，顺丰单号 SF123", shipped=True) == ""
    assert review_reply("老师，佣金可以给 30%", shipped=True) != "", "发货以后也不能谈佣金数字"


def test_a_shipped_request_is_recognised(setup):
    handler, _ = setup("老师的样品已经寄出啦，顺丰单号 SF123")
    handler.store.add_approval(make_packet("GATE-S", chat_id=INFLUENCER, expires_at=NEXT_YEAR), source=SOURCE_BOT)
    handler.store.transition("GATE-S", GateStatus.PENDING, GateStatus.EXECUTING)
    handler.store.transition("GATE-S", GateStatus.EXECUTING, GateStatus.FULFILLED, tracking_number="SF123")
    assert say(handler, "快递到哪了") == "老师的样品已经寄出啦，顺丰单号 SF123"


# ---- 出错时退回规则流程 ----

@pytest.mark.parametrize("failure", [
    RuntimeError("claude -p 退出码 1"),
    subprocess.TimeoutExpired("claude", 90),
    {"type": "result", "is_error": True, "result": "额度用完了"},
    Raw("这不是 JSON"),
])
def test_when_claude_fails_the_rules_take_over(setup, failure):
    handler, _ = setup(failure)
    reply = say(handler, SAMPLE_REQUEST)
    assert "收件人姓名" in reply, "规则流程接着问收件信息"


def test_the_conversation_is_deleted_after_90_days(setup):
    handler, _ = setup("老师好呀~")
    say(handler, "你好")
    handler.store.run_maintenance(now=time.time() + PERSONAL_INFO_RETENTION_SECONDS + 1)
    assert handler.store.recent_dialog(INFLUENCER) == []


# ---- 开关 ----

def test_claude_is_off_unless_switched_on(tmp_path, monkeypatch):
    monkeypatch.delenv("INFLUENCER_BRAIN", raising=False)
    assert build_influencer_handler(str(tmp_path / "a.db")).brain is None
    monkeypatch.setenv("INFLUENCER_BRAIN", "claude")
    assert isinstance(build_influencer_handler(str(tmp_path / "b.db")).brain, ClaudeBrain)
