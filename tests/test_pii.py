"""日志和追踪记录里的个人信息打码（src/pii.py，B14）。"""

import json
import logging

import pytest

from src.decision_trace import DecisionTracer, JsonlSink
from src.pii import mask_pii

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


def test_the_decision_trace_is_masked(tmp_path):
    tracer = DecisionTracer(JsonlSink(tmp_path / "d.jsonl"))
    trace = tracer.begin({"message_id": "m1", "chat_id": "wxid_mei", "content": ADDRESS_MESSAGE})
    trace.finish_reply("好的")
    written = (tmp_path / "d.jsonl").read_text(encoding="utf-8")
    assert "13812345678" not in written and "世纪大道" not in written
    assert json.loads(written.splitlines()[0])["content"].startswith("〔姓名〕")
