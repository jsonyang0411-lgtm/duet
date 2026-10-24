"""达人索样的多轮对话：信息分几条消息发来，逐步补齐（src/influencer/handler.py）。"""

from src.influencer.handler import HOLDING_REPLY, build_influencer_handler
from src.influencer.store import SOURCE_CONSOLE
from test_influencer_acceptance import (
    INFLUENCER,
    SAMPLE_REQUEST,
    FakeERP,
    age_draft,
    make_handler,
    pending_packets,
    private_msg,
    router_with,
    run,
)
from test_router import make_router


def say(handler, text):
    return run(handler.process_incoming_message(INFLUENCER, text, nickname="小美"))


def test_missing_fields_are_asked_for_one_by_one_until_complete():
    handler = make_handler()
    say(handler, "想申请眼霜样品")
    reply = say(handler, "13812345678 上海市浦东新区世纪大道100号")
    assert "收件人姓名" in reply and "联系电话" not in reply
    assert pending_packets(handler) == []
    say(handler, "张三")
    [packet] = pending_packets(handler)
    assert (packet.recipient_name, packet.recipient_phone) == ("张三", "13812345678")
    assert packet.requested_product == "眼霜"
    assert "想申请眼霜样品" in packet.evidence


def test_the_reply_confirms_the_recipient_with_a_masked_phone():
    handler = make_handler()
    say(handler, "想申请眼霜样品")
    reply = say(handler, "张三 13812345678 上海市浦东新区世纪大道100号")
    assert "138****5678" in reply and "13812345678" not in reply


def test_an_abandoned_request_expires():
    handler = make_handler()
    say(handler, "想申请眼霜样品")
    age_draft(handler, INFLUENCER, 2 * 24 * 3600)
    assert say(handler, "好的") is None


class PartlyOutOfStockERP(FakeERP):
    async def check_inventory(self, product_name):
        result = await super().check_inventory(product_name)
        if product_name == "眼霜":
            result.update(in_stock=False, available_quantity=0)
        return result


def test_an_out_of_stock_product_lets_the_influencer_pick_another():
    handler = make_handler(PartlyOutOfStockERP())
    assert "没有库存" in say(handler, "想申请眼霜样品")
    assert "【面膜】目前有货" in say(handler, "那换成面膜吧")


def test_contacts_outside_the_whitelist_are_left_alone():
    handler = make_handler(influencers=())
    assert run(handler.process_incoming_message("wxid_stranger", SAMPLE_REQUEST)) is None


def test_the_environment_whitelist_is_merged_into_the_list_at_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("INFLUENCER_CHAT_IDS", "wxid_a, wxid_b ,")
    db_path = str(tmp_path / "messages.db")
    handler = build_influencer_handler(db_path)
    assert handler.is_influencer("wxid_a") and handler.is_influencer("wxid_b")
    assert not handler.is_influencer("wxid_c")
    handler.store.save_contact("wxid_a", enabled=False, source=SOURCE_CONSOLE)
    assert not handler.is_influencer("wxid_a")
    assert not build_influencer_handler(db_path).is_influencer("wxid_a"), "重启后，停用的达人不能被环境变量加回来（Q4）"


class BrokenERP(FakeERP):
    async def check_inventory(self, product_name):
        return {"in_stock": True, "available_quantity": 1}  # 缺字段，后面会出错


def test_an_unexpected_error_gets_a_neutral_reply_instead_of_the_chat_model():
    router, summarizer = router_with(make_handler(BrokenERP()))
    reply = router.handle(private_msg("m1", INFLUENCER, "小美", "想申请眼霜样品"))
    assert reply == HOLDING_REPLY
    assert summarizer.chat_calls == []


def test_a_router_built_without_the_handler_never_opens_the_database(tmp_path, monkeypatch):
    # 达人处理器由 bot.py 构造后传给 router。router 自己不建，用默认配置的测试就不会
    # 在 data/messages.db 里建表
    monkeypatch.chdir(tmp_path)
    router, summarizer = make_router()
    router.handle(private_msg("m1", INFLUENCER, "小美", SAMPLE_REQUEST))
    assert not (tmp_path / "data" / "messages.db").exists()
    assert summarizer.chat_calls, "没挂达人模块时，私聊走原来的聊天逻辑"
