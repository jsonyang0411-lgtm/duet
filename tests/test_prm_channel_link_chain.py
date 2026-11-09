"""专属链接 after 专属佣金 (2026-11-09): a 抖店 sample order's products get their 专属链接 (dy.channel_goods) as the step
after each one's 专属佣金, only when the deal asks for links.  Whether it does is the drafting Claude's call: the creator
or the boss asked for 「链接」「专属链接」「前缀」, and the draft says so in the slot 「专属链接前缀」 (the prefix, or 「要」
while she has not said one; empty or 「不要」: not wanted).  The bot keeps the chain's record (src/prm/service.py
``_chain``): per product its commission, then its link; a link only after its commission is open; each once a round;
one that cannot be made does not hold up the rest.  Without a chain (no verified sample order) the step after a
verified 专属佣金 asks for that product's link.  Names, ids and numbers are invented."""

import json

import pytest

from test_prm import draft
from test_prm_platform_flow import (CHANNEL, DESIGNATED, THREE, TWO, approve, block, bot, clock,  # noqa: F401
                                    drafted, mac_runs, record, system_jobs)

from platform_fakes import DY_ITEM, DY_ITEM2, DY_LINK, DY_SKU, DY_SKU2, DY_UID, JST_ORDER
from src.prm import platforms
from src.prm.service import LINK_SLOT, RESTART_HINT, link_wish, next_step_note

LINK2 = {**CHANNEL, "item_id": DY_ITEM2}
RUN2 = {"steps": ["create", "bind"], "skus": [{"id": DY_SKU2, "price": "198.00", "stock": 100, "main_price": "220.00"}]}


def wants_links(b, value="【小美专属】"):
    """She asks for links: the reply draft records the slot (the drafting Claude's call)."""
    b.prm.handle(b.msg("寄的帽子都要链接，前缀：小美专属"))
    job = b.worker_takes()
    b.finish(job["id"], draft(message_kind="faq", reply_draft="好的，签收以后就给您弄", slots={LINK_SLOT: value},
                              stage_proposal=""))
    packet = b.prm.store.packet_for_job(job["id"])
    if packet["status"] == "pending":
        assert b.prm.decide(packet["id"], "drop")["ok"]
    return packet


def two_signed_for(b, clock, links="【小美专属】"):
    """A two-product 抖店 sample order made and verified; she asked for links (``links``); signed for: the first
    step is asked for."""
    if links is not None:
        wants_links(b, links)
    order = drafted(b, preview=block(TWO, run={"steps": ["create"]}), reply="寄样单建好啦")
    b.prm.store.token_for(order["party_id"], "phone", "手机", "13900001111", clock.now)
    mac_runs(b, approve(b, order), record(sample_order_no=JST_ORDER, creator_uid=DY_UID))
    b.prm.send_approved()
    assert b.prm.deal_action("stage", {"deal_id": order["deal_id"], "stage": "delivered"})["ok"]
    return order


def step(b, raw=None, verdict="ready", reply="", run=None):
    """The Mac takes the step job and drafts ``raw`` (None: no action): (the job, its draft)."""
    job = b.worker_takes()
    assert job is not None and "system_note" in job["meta"], job
    d = draft(message_kind="money" if raw else "other", reply_draft=reply, slots={})
    if raw is not None:
        d["platform"] = block(raw, verdict=verdict, run=run)
    b.finish(job["id"], d)
    return job, b.prm.store.packet_for_job(job["id"])


def chain_attention(b):
    return next((n["text"] for n in b.prm.store.deal(1)["notices"] if n["key"] == "chain"), "")


def opens(b, packet, rec=None):
    mac_runs(b, approve(b, packet), rec or record())
    b.prm.send_approved()


# ── what asks for links ───────────────────────────────────────────────────

@pytest.mark.parametrize("value, wish", [
    ("【小美专属】", {"prefix": "【小美专属】", "said": "【小美专属】"}),
    ("小美专属", {"prefix": "【小美专属】", "said": "小美专属"}),
    ("要", {"prefix": "", "said": "要"}),                                  # she wants links, no prefix said yet
    ("【小美推荐】", {"prefix": "", "said": "【小美推荐】"}),                  # not the shop's 【某某专属】: the case file's call
    ("四个字的专属", {"prefix": "", "said": "四个字的专属"}),                 # too long for the page: the case file's call
    ("", None), ("  ", None), ("不要", None), ("不用", None), (None, None),
])
def test_the_slot_says_whether_the_deal_asks_for_links(value, wish):
    assert link_wish({LINK_SLOT: value} if value is not None else {}) == wish


# ── the chain: per product its commission, then its link ─────────────────

def test_each_product_gets_its_link_right_after_its_commission(bot, clock):
    """kb 2.1 with links: signed for → A's 专属佣金 (quiet) → A's link (her reply with the link) → B's 专属佣金 → B's
    link → nothing left: one step tells the case file, then no more."""
    order = two_signed_for(bot, clock)
    job, commission = step(bot, DESIGNATED)
    note = job["meta"]["system_note"]
    assert f"这一份草稿只填商品 {DY_ITEM} 的 dy.designated_commission" in note
    assert "这位达人要专属链接" in note and "系统会再派一步建它的链接" in note and "这一轮这之后还有 3 步" in note
    assert job["meta"]["step"]["item"] == DY_ITEM and "action" not in job["meta"]["step"]
    opens(bot, commission)
    newest = system_jobs(bot)[0]
    assert f"正在起草 {DY_ITEM}的专属链接（任务 #{newest['id']}）" in chain_attention(bot)
    job, link = step(bot, CHANNEL, reply="您的专属链接：{链接}")
    note = job["meta"]["system_note"]
    assert note.startswith(f"商品 {DY_ITEM} 的专属佣金（20%）已经在平台上开好了")
    assert f"这一份草稿只填商品 {DY_ITEM} 的 dy.channel_goods" in note and "title_prefix 填「【小美专属】」" in note
    assert "日常价 9 折取整、每个颜色 100 件" in note and "{链接}" in note and "这一轮这之后还有 2 步" in note
    assert "专属链接 · 还没建：" in note and job["meta"]["step"] == {"packet": commission["id"], "delivered": True,
                                                                  "item": DY_ITEM, "action": "dy.channel_goods"}
    assert link["status"] == "pending" and link["meta"]["lane"] == "system"
    assert f"专属链接：建好 0/2 个；在待审批 {DY_ITEM}（#{link['id']}）" in chain_attention(bot)
    opens(bot, link, record(link=DY_LINK, channel_id="7200000000000001"))
    assert bot.texts[-1] == (order["chat_id"], f"您的专属链接：{DY_LINK}")
    job, commission2 = step(bot, {**DESIGNATED, "item_id": DY_ITEM2})
    note = job["meta"]["system_note"]
    assert note.startswith(f"商品 {DY_ITEM} 的专属链接已经建好、绑好达人了（渠道品 7200000000000001）")
    assert f"这一份草稿只填商品 {DY_ITEM2} 的 dy.designated_commission" in note and "这一轮这之后还有 1 步" in note
    opens(bot, commission2)
    job, link2 = step(bot, LINK2, reply="这款的专属链接：{链接}", run=RUN2)
    assert f"这一份草稿只填商品 {DY_ITEM2} 的 dy.channel_goods" in job["meta"]["system_note"]
    assert "这是这一轮最后一步" in job["meta"]["system_note"]
    opens(bot, link2, record(link=DY_LINK, channel_id="7200000000000002"))
    final, nothing = step(bot)
    note = final["meta"]["system_note"]
    assert "这一轮没有要开的专属佣金、要建的专属链接了：这一份不要填平台动作" in note
    assert f"专属链接 · 已建好：{DY_ITEM}、{DY_ITEM2}" in note and nothing["status"] == "no_reply"
    assert bot.worker_takes() is None and chain_attention(bot) == ""
    assert len(system_jobs(bot)) == 5


def test_without_links_the_chain_is_as_before(bot, clock):
    """Nobody asked for links (no slot, or 「不要」): the 专属佣金 one after the other, no link step, the notes as before."""
    two_signed_for(bot, clock, links="不要")
    job, first = step(bot, DESIGNATED)
    assert "专属链接" not in job["meta"]["system_note"] and "这一轮这之后还要开 1 个" in job["meta"]["system_note"]
    opens(bot, first)
    job, second = step(bot, {**DESIGNATED, "item_id": DY_ITEM2})
    assert f"这一份草稿只填商品 {DY_ITEM2} 的 dy.designated_commission" in job["meta"]["system_note"]
    assert "这是这一轮最后一个要开的商品" in job["meta"]["system_note"]
    opens(bot, second)
    final, _ = step(bot)
    assert "这一轮没有要开的商品了：这一份不要填平台动作" in final["meta"]["system_note"]
    assert bot.worker_takes() is None


def test_a_product_whose_commission_is_not_open_gets_no_link_and_the_next_one_goes_on(bot, clock):
    """A's 专属佣金 dropped (「不发」): A gets no link this round (bind needs her in its strategy), B's 专属佣金 is next."""
    two_signed_for(bot, clock)
    _, first = step(bot, DESIGNATED)
    assert bot.prm.decide(first["id"], "drop")["ok"]
    job, _ = step(bot, {**DESIGNATED, "item_id": DY_ITEM2})
    note = job["meta"]["system_note"]
    assert f"这一份草稿只填商品 {DY_ITEM2} 的 dy.designated_commission" in note
    assert f"这一轮不开（运营点了「不发」）：{DY_ITEM}" in note
    assert f"专属链接 · 等它的专属佣金开好：{DY_ITEM}、{DY_ITEM2}" in note and "这一轮这之后还有 1 步" in note


@pytest.mark.parametrize("how", ["dropped", "uncertain", "asked"])
def test_a_link_that_is_not_made_does_not_hold_up_the_next_product(bot, clock, how):
    """A's link dropped, ended uncertain (the channel product made, binding her refused), or its step drafted nothing:
    B's 专属佣金 is next; A's link is not asked again this round; a red one is said until 「我已处理」."""
    two_signed_for(bot, clock)
    _, first = step(bot, DESIGNATED)
    opens(bot, first)
    if how == "asked":
        step(bot)                                                         # the link step drafted no action
    else:
        _, link = step(bot, CHANNEL, reply="您的专属链接：{链接}")
        if how == "dropped":
            assert bot.prm.decide(link["id"], "drop")["ok"]
        else:
            rec = record("FAILED", channel_id="7200000000000001")
            rec["steps"] = [{"name": "create", "label": "create", "execute": True, "command": "node bin/dy.mjs",
                             "exit_code": 0, "seconds": 1.0, "verdict": "", "outcome": "VERIFIED_SUCCESS", "problems": [],
                             "facts": [], "raw": "{}"},
                            {"name": "bind", "label": "bind", "execute": True, "command": "node bin/dy.mjs",
                             "exit_code": 1, "seconds": 1.0, "verdict": "", "outcome": "FAILED", "problems": [],
                             "facts": [], "raw": "{}"}]
            mac_runs(bot, approve(bot, link), rec)
    job, _ = step(bot, {**DESIGNATED, "item_id": DY_ITEM2})
    note = job["meta"]["system_note"]
    assert f"这一份草稿只填商品 {DY_ITEM2} 的 dy.designated_commission" in note
    label = {"dropped": "这一轮不建（运营点了「不发」）", "uncertain": "结果不确定（运营会先到平台后台核对）",
             "asked": "派过、草稿没建它"}[how]
    assert f"专属链接 · {label}：{DY_ITEM}" in note
    if how == "uncertain":
        assert "平台上可能已经开了" in bot.prm.store.packet(link["id"])["error"]
        assert "结果不确定" in chain_attention(bot) and "先到平台后台核对" in chain_attention(bot)


def test_a_long_order_with_links_keeps_the_note_and_the_notice_short(bot, clock):
    """A 专场 order of 50 products (32-digit ids) whose links are wanted: the note still starts with what to do and stays
    under the worker's 4000 characters; the deal's notice within its 300."""
    wants_links(bot)
    many = [str(30000000000000000000000000000000 + i) for i in range(50)]
    order = {**THREE, "items": [{"item_id": i, "skus": [{"sku_id": str(31000000000000000000000000000000 + n), "qty": 1}]}
                                for n, i in enumerate(many)]}
    action = platforms.check_action(order, limits=platforms.FORM_LIMITS)
    packet = drafted(bot, preview={**block(THREE, run={"steps": ["create"]}), "action": action}, reply="寄样单建好啦")
    bot.prm.store.token_for(packet["party_id"], "phone", "手机", "13900001111", clock.now)
    mac_runs(bot, approve(bot, packet), record(sample_order_no=JST_ORDER))
    bot.prm.send_approved()
    assert bot.prm.deal_action("stage", {"deal_id": 1, "stage": "delivered"})["ok"]
    [job] = system_jobs(bot)
    note = json.loads(job["meta"])["system_note"]
    assert note.index(f"这一份草稿只填商品 {many[0]}") < 300 and len(note) < 4000 and "这一轮这之后还有 99 步" in note
    text = chain_attention(bot)
    assert len(text) <= 300 and text.startswith(f"专属佣金（寄样单 {JST_ORDER}）：开好 0/50 个")


def test_a_link_step_whose_draft_forgot_the_action_says_so_in_its_own_words(bot, clock):
    two_signed_for(bot, clock)
    _, first = step(bot, DESIGNATED)
    opens(bot, first)
    job, stepless = step(bot, reply="您的专属链接马上就好")                  # a reply, no dy.channel_goods
    assert stepless["status"] == "pending" and stepless["needs_attention"]
    assert "建专属链接这一步的草稿没带平台动作，平台上什么都不会做；回复要是说建好了，发之前请核对" in stepless["reason"]
    assert f"#{job['id']} 建专属链接这一步的草稿没带平台动作" in bot.prm.store.deal(1)["attention"]
    assert f"正在起草 {DY_ITEM}的专属链接" not in chain_attention(bot)                # (its draft is written)


def test_links_asked_for_after_the_commissions_are_open_are_made_at_once(bot, clock):
    """The round was done (both 专属佣金 open) when she asked for links: the draft that records it starts A's link."""
    two_signed_for(bot, clock, links=None)
    for item in (DY_ITEM, DY_ITEM2):
        _, commission = step(bot, {**DESIGNATED, "item_id": item})
        opens(bot, commission)
    step(bot)                                                             # the round is done: nothing more
    assert bot.worker_takes() is None
    wants_links(bot, "小美专属")
    job = bot.worker_takes()
    assert job is not None and f"这一份草稿只填商品 {DY_ITEM} 的 dy.channel_goods" in job["meta"]["system_note"]
    assert job["meta"]["step"]["action"] == "dy.channel_goods"


def test_a_link_step_owed_while_a_person_has_the_deal_is_asked_for_when_it_ends(bot, clock):
    two_signed_for(bot, clock)
    _, first = step(bot, DESIGNATED)
    execution = approve(bot, first)
    assert bot.prm.deal_action("mode", {"deal_id": 1, "mode": "human"})["ok"]
    mac_runs(bot, execution, record())
    assert bot.worker_takes() is None
    assert f"#{first['id']} 之后该建专属链接了" in bot.prm.store.deal(1)["attention"]
    assert bot.prm.deal_action("mode", {"deal_id": 1, "mode": "copilot"})["ok"]
    assert bot.prm.sweep_owed_steps() == 1
    job = bot.worker_takes()
    assert f"这一份草稿只填商品 {DY_ITEM} 的 dy.channel_goods" in job["meta"]["system_note"]
    assert job["meta"]["step"]["action"] == "dy.channel_goods"


def test_a_link_drafted_for_a_product_without_its_commission_says_so(bot, clock):
    """A reply that carries a link for a product the bot never opened a 专属佣金 of: the draft waits flagged (binding her
    may be refused; one opened by hand in 百应 is fine)."""
    packet = drafted(bot, block(CHANNEL), reply="链接：{链接}")
    assert packet["needs_attention"] and "还没给这位达人开过专属佣金" in packet["reason"]
    assert "还没给这位达人开过专属佣金" in bot.prm.store.deal(packet["deal_id"])["attention"]
    commission = drafted(bot, reply="专属佣金设好啦")
    opens(bot, commission)
    for job in system_jobs(bot):
        bot.relay.store.cancel_if_pending(job["id"], clock.now)
    again = drafted(bot, block(CHANNEL), reply="链接：{链接}")
    assert "还没给这位达人开过专属佣金" not in again["reason"]


# ── without a chain (no verified sample order) ────────────────────────────

def test_without_a_chain_a_verified_commission_asks_for_that_products_link(bot, clock):
    wants_links(bot)
    commission = drafted(bot, reply="专属佣金设好啦")
    opens(bot, commission)
    assert len(system_jobs(bot)) == 1
    job, link = step(bot, CHANNEL, reply="链接：{链接}")
    note = job["meta"]["system_note"]
    assert f"商品 {DY_ITEM} 的专属佣金（20%）已经在平台上开好了" in note
    assert f"这一份草稿只填商品 {DY_ITEM} 的 dy.channel_goods" in note and "title_prefix 填「【小美专属】」" in note
    assert job["meta"]["step"]["action"] == "dy.channel_goods"
    opens(bot, link, record(link=DY_LINK, channel_id="7200000000000001"))
    nxt = bot.worker_takes()
    assert "专属链接已经建好" in nxt["meta"]["system_note"] and "都弄完了就不要填平台动作" in nxt["meta"]["system_note"]


def test_without_links_a_verified_link_asks_for_nothing_more(bot, clock):
    link = drafted(bot, block(CHANNEL), reply="链接：{链接}")
    opens(bot, link, record(link=DY_LINK, channel_id="7200000000000001"))
    assert bot.worker_takes() is None


def test_the_next_step_notes_for_links():
    commission = platforms.check_action(DESIGNATED)
    link = platforms.check_action(CHANNEL)
    wish = {"prefix": "【小美专属】", "said": "【小美专属】"}
    assert next_step_note(link, record(link=DY_LINK)) == ""                  # no links asked for: nothing follows
    assert "dy.channel_goods" not in next_step_note(commission, record())
    note = next_step_note(commission, record(), links=wish)
    assert f"这一份草稿只填商品 {DY_ITEM} 的 dy.channel_goods" in note and "title_prefix 填「【小美专属】」" in note
    assert "dy.channel_goods" not in next_step_note(commission, record(), links=wish, linked=True)
    note = next_step_note(link, record(link=DY_LINK), links={"prefix": "", "said": "要"})
    assert "专属链接已经建好" in note and "先开它的 dy.designated_commission" in note
