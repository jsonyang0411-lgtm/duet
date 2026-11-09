"""专属链接 (dy.channel_goods), the reviews of 2026-11-10: a draft not made the shop's way (not 专属达人带货, a channel price
that is not each colour's 9 折, a stock other than 100 a colour) waits with a line naming each way it is not (review A
D4); the 专属链接 steps' record (``next_step`` events) keeps her UID only by its last 4 characters, and the 8.2 clean-up
cuts its prefix (her name in it) and an older event's whole UID (review B).  Names, ids and numbers are invented."""

import pytest

from test_prm_channel_link_chain import opens, step, two_signed_for
from test_prm_platform_flow import CHANNEL, DESIGNATED, block, bot, clock, drafted  # noqa: F401

from platform_fakes import DY_ITEM, DY_SKU, DY_UID
from src.prm.service import PREFIX_CLEARED, UID_TAIL, convention_text


# ── A-D4: the shop's way ───────────────────────────────────────────────────

@pytest.mark.parametrize("extra, said", [
    ({}, ""),                                                              # the shop's way: nothing to say
    ({"price": "17"}, ""),                                                 # (17 is 19.99's 9 折 floored)
    ({"stock_per_sku": 100}, ""),
    ({"exclusive": False}, "没按店里的规矩：不是专属达人带货（写的是达人带货，共用主商品库存）"),
    ({"exclusive": False, "stock_per_sku": 100}, "没按店里的规矩：不是专属达人带货（写的是达人带货）"),
    ({"price": "15"}, f"没按店里的规矩：渠道价不是 9 折（统一写的 15.00 元；SKU {DY_SKU} 日常价 19.99 元的 9 折是 17.00 元）"),
    ({"stock_per_sku": 50}, "没按店里的规矩：每个颜色的库存写的是 50 件，不是 100 件"),
    ({"exclusive": False, "price": "15", "stock_per_sku": 50},
     f"没按店里的规矩：不是专属达人带货（写的是达人带货）；渠道价不是 9 折（统一写的 15.00 元；SKU {DY_SKU} 日常价 19.99 元"
     "的 9 折是 17.00 元）；每个颜色的库存写的是 50 件，不是 100 件"),
])
def test_a_link_draft_not_made_the_shops_way_says_how_and_waits(bot, extra, said):
    # (the preview's agreement as the Mac works it out for that action: its price, its stock or none when shared)
    per = extra.get("stock_per_sku") or (100 if extra.get("exclusive", True) else None)
    run = {"steps": ["create", "bind"], "skus": [{"id": DY_SKU, "price": f"{float(extra.get('price', 17)):.2f}",
                                                  "stock": None if per is None else min(per, 40),
                                                  "main_price": "19.99"}]}
    packet = drafted(bot, preview=block({**CHANNEL, **extra}, run=run), reply="您的专属链接：{链接}", kind="other")
    assert packet["status"] == "pending"
    if said:
        assert packet["needs_attention"] and said in packet["reason"]
        assert said in bot.prm.store.deal(packet["deal_id"])["attention"]
    else:
        assert "没按店里的规矩" not in packet["reason"]


def test_without_daily_prices_the_price_the_draft_wrote_is_named():
    action = {**CHANNEL, "price": "15.00"}
    assert convention_text(action, {"run": {}}) == "没按店里的规矩：渠道价不是 9 折（统一写的 15.00 元，不是每个颜色日常价的 9 折）"
    assert convention_text(action, None) == convention_text(action, {"run": {}})
    skus = [{"id": str(n), "price": "15.00", "main_price": "100.00"} for n in range(5)]
    said = convention_text(action, {"run": {"skus": skus}})
    assert said.count("日常价 100.00 元的 9 折是 90.00 元") == 3 and said.endswith(" 等 5 个颜色）")
    assert convention_text({**CHANNEL, "price": "90"}, {"run": {"skus": skus}}) == ""     # (each colour's 9 折)
    assert convention_text({**CHANNEL, "stock_per_sku": True}, None) == ""                # (no number: nothing to say)


# ── the 专属链接 steps' record ───────────────────────────────────────────────

def link_step_asked(bot, clock):
    """A 抖店 sample order signed for, its first 专属佣金 opened: the step of its 专属链接 is asked for."""
    order = two_signed_for(bot, clock)
    _, commission = step(bot, DESIGNATED)
    opens(bot, commission)
    return order


def test_a_link_steps_record_keeps_her_uid_by_its_last_characters_and_the_purge_cuts_the_prefix(bot, clock):
    order = link_step_asked(bot, clock)
    deal_id = order["deal_id"]
    [asked] = [e for e in bot.prm.store.events(deal_id, kinds=("next_step",)) if e["detail"].get("action")]
    assert asked["detail"]["uid"] == DY_UID[-UID_TAIL:] and DY_UID not in str(asked["detail"])
    assert asked["detail"]["prefix"] == "【小美专属】" and asked["detail"]["item"] == DY_ITEM
    job, link = step(bot, CHANNEL, reply="您的专属链接：{链接}")
    assert job["meta"]["step"]["uid"] == DY_UID and link["meta"]["chain_uid"] == DY_UID   # (the job's: whole)
    # an event an older bot wrote (her UID whole)
    bot.prm.store.add_event(deal_id, order["party_id"], "next_step", {"job": 999, "item": DY_ITEM, "action": CHANNEL["action"],
                                                                      "prefix": "【小美专属】", "uid": DY_UID},
                            actor="system", now=clock.now)
    counts = bot.prm.purge(bot.prm.store.deal(deal_id), clock.now)
    assert counts["steps"] == 2
    for ev in bot.prm.store.events(deal_id, kinds=("next_step",)):
        assert "小美" not in str(ev["detail"]) and DY_UID not in str(ev["detail"])
        if ev["detail"].get("action"):
            assert ev["detail"]["prefix"] == PREFIX_CLEARED and ev["detail"]["uid"] == DY_UID[-UID_TAIL:]
    assert bot.prm.purge(bot.prm.store.deal(deal_id), clock.now)["steps"] == 0         # (once)
