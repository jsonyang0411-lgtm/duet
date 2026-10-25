"""日志和追踪记录里的个人信息打码（src/pii.py，B14）。"""

import json
import logging

import pytest

from src.decision_trace import DecisionTracer, JsonlSink
from src.pii import mask_pii
from test_router import make_router

ADDRESS_MESSAGE = "张三 13812345678 上海市浦东新区世纪大道100号"


@pytest.mark.parametrize("text, expected", [
    (ADDRESS_MESSAGE, "〔姓名〕 138****5678 〔地址〕"),
    ("收件人：李四，电话13900001111，地址：广东省深圳市南山区科技园1栋",
     "收件人：〔姓名〕，电话139****1111，地址：〔地址〕"),
    ("手机号：13900001111", "手机号：139****1111"),
    ("身份证110101199003071234", "身份证110*************34"),
])
def test_personal_details_are_masked(text, expected):
    assert mask_pii(text) == expected


@pytest.mark.parametrize("text", [
    "想申请VC精华液的样品", "明天下午三点开会", "订单号 202610250001 到了吗", "", "老师好~",
])
def test_ordinary_text_is_left_alone(text):
    assert mask_pii(text) == text


def test_none_becomes_empty():
    assert mask_pii(None) == ""


def test_the_private_chat_log_line_is_masked(caplog):
    router, _ = make_router()
    msg = {
        "message_id": "p1", "chat_id": "wxid_mei", "sender_id": "wxid_mei", "sender_name": "小美",
        "content": ADDRESS_MESSAGE, "msg_type": 1, "timestamp": 1_760_000_000,
        "is_at_mentioned": False, "is_group": False,
    }
    with caplog.at_level(logging.INFO, logger="src.router"):
        router._handle_private_chat(msg)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "Private chat from" in logged
    assert "13812345678" not in logged and "世纪大道" not in logged and "张三" not in logged


def test_the_decision_trace_and_model_prompts_are_masked(tmp_path):
    tracer = DecisionTracer(JsonlSink(tmp_path / "d.jsonl"), JsonlSink(tmp_path / "l.jsonl"))
    trace = tracer.begin({"message_id": "m1", "chat_id": "wxid_mei", "content": ADDRESS_MESSAGE})
    tracer.record_llm_call(system_prompt="你是助手", user_prompt=f"用户说：{ADDRESS_MESSAGE}")
    trace.finish_reply("好的")
    written = (tmp_path / "d.jsonl").read_text(encoding="utf-8") + (tmp_path / "l.jsonl").read_text(encoding="utf-8")
    assert "13812345678" not in written and "世纪大道" not in written
    assert json.loads((tmp_path / "d.jsonl").read_text(encoding="utf-8").splitlines()[0])["content"].startswith("〔姓名〕")
