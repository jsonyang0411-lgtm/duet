"""达人索样的多轮对话：信息分几条消息发来，逐步补齐（src/influencer/handler.py）。"""

import time

from test_influencer_acceptance import INFLUENCER, FakeERP, make_handler, pending_packets, run


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
    handler.drafts[INFLUENCER].updated_at = time.time() - 2 * 24 * 3600
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
