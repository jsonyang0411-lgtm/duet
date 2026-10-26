"""达人模块的处理器（src/influencer/handler.py）：启动时并入名单、生成审批单前到聚水潭查商品。

原来这里测的是规则对话（多轮补齐收件信息等），2026-10-26 第 8 步和旧代码一起删了：
达人的私聊改由 PRM 起草、运营审核，收件信息在合作单上。"""

from src.influencer.handler import ERP_UNREACHABLE, build_influencer_handler
from src.influencer.store import SOURCE_CONSOLE
from src.influencer.types import SampleRequestDraft
from test_influencer_acceptance import FakeERP, make_handler, run


def test_the_environment_whitelist_is_merged_into_the_list_at_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("INFLUENCER_CHAT_IDS", "wxid_a, wxid_b ,")
    db_path = str(tmp_path / "messages.db")
    handler = build_influencer_handler(db_path)
    assert handler.is_influencer("wxid_a") and handler.is_influencer("wxid_b")
    assert not handler.is_influencer("wxid_c")
    handler.store.save_contact("wxid_a", enabled=False, source=SOURCE_CONSOLE)
    assert not handler.is_influencer("wxid_a")
    assert not build_influencer_handler(db_path).is_influencer("wxid_a"), "重启后，停用的达人不能被环境变量加回来（Q4）"


class PartlyMissingERP(FakeERP):
    """丹麦贝雷帽在 ERP 里查不到；北欧套头帽在 ERP 里有，但没有现货。"""

    async def check_inventory(self, product_name):
        result = await super().check_inventory(product_name)
        if product_name == "丹麦贝雷帽":
            result.update(found=False, in_stock=False, available_quantity=0)
        if product_name == "北欧套头帽":
            result.update(found=True, in_stock=False, available_quantity=0)
        return result


class DownERP(FakeERP):
    async def check_inventory(self, product_name):
        raise ConnectionError("聚水潭连不上")


def test_a_product_missing_from_the_erp_is_refused_and_put_in_front_of_the_operator():
    handler = make_handler(PartlyMissingERP())
    missing = SampleRequestDraft(chat_id="wxid_a", product="丹麦贝雷帽")
    assert "查不到" in run(handler.check_product(missing)) and missing.inventory is None
    [item] = handler.store.list_inbox()
    assert item["kind"] == "out_of_stock" and "丹麦贝雷帽" in item["content"]
    # D35：ERP 里有就能寄，没有现货也照常往下走
    no_stock = SampleRequestDraft(chat_id="wxid_a", product="北欧套头帽")
    assert run(handler.check_product(no_stock)) == "" and no_stock.inventory["available_quantity"] == 0


def test_an_erp_that_cannot_be_reached_is_said_plainly():
    handler = make_handler(DownERP())
    assert run(handler.check_product(SampleRequestDraft(chat_id="wxid_a", product="贝雷帽"))) == ERP_UNREACHABLE
    assert handler.store.list_inbox() == [], "连不上不是商品的问题，不记进待处理"
