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
from src.prm.service import LINK_SLOT, RESTART_HINT, link_wish, next_step_note, unfilled_placeholders

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
    ("四个字的专属", {"prefix": "", "said": "四个字的专属", "long": "【四个字的专属】"}),   # 14: never dictated (F1)
    ("这是一个非常非常长的名字的专属", {"prefix": "", "said": "这是一个非常非常长的名字的专属"}),   # too long: the case file's call
    ("【不二专属】", {"prefix": "【不二专属】", "said": "【不二专属】"}),          # a bracketed name that starts with 不
    ("", None), ("  ", None), ("不要", None), ("不用", None), (None, None),
    ("不需要了", None), ("不用了", None), ("暂时不要", None), ("不要专属链接", None), ("别弄了", None), ("没有", None),
    ("无", None), ("取消吧", None), ("先不用", None), ("\u200b不要", None),        # review 2026-11-09 #7
    # an explicit 要 says yes whatever comes before it, a prefix too; anything else says no (review 2026-11-09 F6)
    ("没问题，要", {"prefix": "", "said": "没问题，要"}), ("不错，要链接", {"prefix": "", "said": "不错，要链接"}),
    ("无所谓，要", {"prefix": "", "said": "无所谓，要"}), ("要，不用前缀", {"prefix": "", "said": "要，不用前缀"}),
    ("要（前缀不用写）", {"prefix": "", "said": "要（前缀不用写）"}), ("没说前缀，要链接", {"prefix": "", "said": "没说前缀，要链接"}),
    ("别人推荐的，要", {"prefix": "", "said": "别人推荐的，要"}), ("需要", {"prefix": "", "said": "需要"}),
    ("【不要停专属】", {"prefix": "【不要停专属】", "said": "【不要停专属】"}),        # a bracketed prefix says yes
    ("要，前缀写小美专属", {"prefix": "【小美专属】", "said": "要，前缀写小美专属"}),
    ("前缀：小美专属", {"prefix": "【小美专属】", "said": "前缀：小美专属"}),
    ("“小美专属”", {"prefix": "【小美专属】", "said": "“小美专属”"}),
    ("算了", None), ("先不弄了", None), ("以后再说", None), ("No", None), ("no", None), ("好的", None),
    ("不想要", None), ("主要看佣金", None), ("【小美专属】不要了", None), ("专属", None),
])
def test_the_slot_says_whether_the_deal_asks_for_links(value, wish):
    assert link_wish({LINK_SLOT: value} if value is not None else {}) == wish


@pytest.mark.parametrize("text, found", [
    ("老师您好～样品已经寄出啦：中通 78912345678901。签收之后跟我说一声哦～", []),
    ("已经在千帆给您开好了专属定向计划「{计划名}」～", ["{计划名}"]),
    ("您的专属推广链接：{链接}，审核通过后就能用～", ["{链接}"]),
    ("渠道品 {渠道品ID} 建好了，链接：{链接}", ["{链接}", "{渠道品ID}"]),
])
def test_a_plain_reply_is_held_for_the_platforms_placeholders(text, found):
    """The verifier's probe P7: only the platforms' placeholders (``platforms.PLACEHOLDER_LABELS``) hold a plain draft."""
    assert sorted(unfilled_placeholders(text)) == sorted(found)


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
                                                                  "item": DY_ITEM, "action": "dy.channel_goods",
                                                                  "prefix": "【小美专属】", "uid": DY_UID}
    assert link["status"] == "pending" and link["meta"]["lane"] == "system" and link["meta"]["chain_uid"] == DY_UID
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
    if how == "uncertain":                     # the channel product, and the two ways back (review 2026-11-09 F3)
        error = bot.prm.store.packet(link["id"])["error"]
        assert error.startswith("渠道品 7200000000000001 建好了、没绑上她（FAILED") and "② 把它下架、删掉" in error
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
    """A reply that carries a link for a product the bot never opened a 专属佣金 of for that UID: the draft waits
    flagged (binding her may be refused; one opened by hand in 百应 is fine); so does one whose UID the bot cannot
    hold to hers (no verified 抖店 sample order: her UID is not known; review 2026-11-09 #2)."""
    packet = drafted(bot, block(CHANNEL), reply="链接：{链接}")
    unopened = f"商品 {DY_ITEM} 在系统里还没给 UID {DY_UID} 开过专属佣金"
    unknown = f"系统不知道这位达人的抖音 UID（她没有核验成功的抖店寄样单）：批准前核对链接要绑的 UID {DY_UID} 是她的"
    assert packet["needs_attention"] and unopened in packet["reason"] and unknown in packet["reason"]
    assert unopened in bot.prm.store.deal(packet["deal_id"])["attention"]
    commission = drafted(bot, reply="专属佣金设好啦")
    opens(bot, commission)
    for job in system_jobs(bot):
        bot.relay.store.cancel_if_pending(job["id"], clock.now)
    again = drafted(bot, block(CHANNEL), reply="链接：{链接}")
    assert unopened not in again["reason"] and unknown in again["reason"]
    other = drafted(bot, block({**CHANNEL, "creator_platform_id": "5100000000000999"}), reply="链接：{链接}")
    assert other["needs_attention"] and "还没给 UID 5100000000000999 开过专属佣金" in other["reason"]   # (the probe)


def test_a_sample_order_without_her_uid_is_said_so(bot, clock):
    """review 2026-11-09 F7: she has a verified 抖店 sample order, but it recorded no UID: the flag says that, not that
    she has no such order."""
    by_account = {**{k: v for k, v in TWO.items() if k != "creator_uid"}, "creator_account": "test_daren_01"}
    order = drafted(bot, preview=block(by_account, run={"steps": ["create"]}), reply="寄样单建好啦")
    bot.prm.store.token_for(order["party_id"], "phone", "手机", "13900001111", clock.now)
    mac_runs(bot, approve(bot, order), record(sample_order_no=JST_ORDER))         # (no creator_uid anywhere)
    bot.prm.send_approved()
    link = drafted(bot, block(CHANNEL), reply="链接：{链接}")
    assert f"她核验成功的抖店寄样单上没记下抖音 UID：批准前核对链接要绑的 UID {DY_UID} 是她的" in link["reason"]
    assert "她没有核验成功的抖店寄样单" not in link["reason"]


def test_a_link_for_a_uid_none_of_her_orders_has_is_flagged_not_refused(bot, clock):
    """Her UIDs are known (her verified 抖店 sample orders: 聚水潭's platformUid).  A link draft outside the chain that
    would bind another one is flagged when it comes, and goes if a person approves it: she may have two 抖音 accounts
    (review 2026-11-09 F4: refusing it was wrong); hers is not flagged."""
    two_signed_for(bot, clock, links=None)
    for item in (DY_ITEM, DY_ITEM2):
        _, commission = step(bot, {**DESIGNATED, "item_id": item})
        opens(bot, commission)
    step(bot)                                                             # the round is done
    other = drafted(bot, block({**CHANNEL, "creator_platform_id": "5100000000000999"}), reply="链接：{链接}")
    doubt = f"链接要绑的 UID 5100000000000999 不在她核验成功的抖店寄样单上（寄样单上的 UID：{DY_UID}）：批准前核对是不是她的"
    assert other["needs_attention"] and doubt in other["reason"] and "chain_uid" not in other["meta"]
    mac_runs(bot, approve(bot, other), record(link=DY_LINK, channel_id="7200000000000005"))   # a person's call: it goes
    hers = drafted(bot, block(CHANNEL), reply="链接：{链接}")
    assert not hers["needs_attention"] and hers["reason"] == "收件信息齐了"           # (draft()'s own reason)
    assert bot.prm.decide(hers["id"], "execute")["ok"]


def test_a_step_of_the_chain_binds_its_orders_uid_whatever_her_newest_order_says(bot, clock):
    """A link step of the chain carries its round's order's UID (``meta.step.uid`` → the draft's ``meta.chain_uid``):
    its draft is held to that one, not to her newest order's (a second order for her other account came in between:
    the verifier's probe P2, refused before); a step draft that binds another UID is flagged and refused."""
    two_signed_for(bot, clock)
    _, commission = step(bot, DESIGNATED)
    opens(bot, commission)
    job, link = step(bot, CHANNEL, reply="链接：{链接}")
    assert job["meta"]["step"]["uid"] == DY_UID and link["meta"]["chain_uid"] == DY_UID and not link["needs_attention"]
    other = "5100000000000999"
    order2 = drafted(bot, preview=block({**TWO, "creator_uid": other}, run={"steps": ["create"]}), reply="另一个号的寄样单建好啦")
    mac_runs(bot, approve(bot, order2), record(sample_order_no="YP20261109999", creator_uid=other))
    bot.prm.send_approved()
    opens(bot, link, record(link=DY_LINK, channel_id="7200000000000001"))   # the chain's own UID: it goes
    _, commission2 = step(bot, {**DESIGNATED, "item_id": DY_ITEM2})
    opens(bot, commission2)
    job, wrong = step(bot, {**LINK2, "creator_platform_id": other}, reply="链接：{链接}", run=RUN2)
    assert job["meta"]["step"]["item"] == DY_ITEM2 and job["meta"]["step"]["uid"] == DY_UID
    refused = f"链接要绑的 UID 不是这一轮寄样单上的（寄样单上的 UID：{DY_UID}；草稿写的是 {other}）"
    assert wrong["needs_attention"] and refused in wrong["reason"]
    jobs = len(bot.relay.store.list_jobs(100))
    out = bot.prm.decide(wrong["id"], "execute")
    assert not out["ok"] and out["error"].startswith(refused) and len(bot.relay.store.list_jobs(100)) == jobs


def answer_steps(b, rounds=8):
    """Answer every job the way a well-behaved Mac would (the review's probe): a link step with the link, a 专属佣金
    step with its commission (both approved and verified), anything else with nothing; returns the steps asked."""
    seen = []
    for _ in range(rounds):
        job = b.worker_takes()
        if job is None:
            break
        meta = job["meta"]
        st = meta.get("step") or {}
        seen.append(st)
        if "system_note" not in meta:
            b.finish(job["id"], draft(message_kind="other", reply_draft="", slots={}))
            continue
        if st.get("action") == "dy.channel_goods":
            b.finish(job["id"], draft(message_kind="money", reply_draft="链接：{链接}", slots={}, platform=block(
                {**CHANNEL, "item_id": st["item"]}, run=RUN2 if st["item"] == DY_ITEM2 else None)))
        elif st.get("item"):
            b.finish(job["id"], draft(message_kind="money", reply_draft="", slots={},
                                      platform=block({**DESIGNATED, "item_id": st["item"]})))
        else:
            b.finish(job["id"], draft(message_kind="other", reply_draft="", slots={}))
        packet = b.prm.store.packet_for_job(job["id"])
        if packet and packet["status"] == "pending" and packet.get("tool") != "message.send":
            opens(b, packet, record(link=DY_LINK, channel_id="7200000000000009")
                  if packet["tool"] == "dy.channel_goods" else record())
    return seen


def says_prefix(b, value="【小美专属】"):
    """She tells the prefix: her reply draft records it (no action), and is sent."""
    b.prm.handle(b.msg("前缀写小美专属"))
    job = b.worker_takes()
    assert "system_note" not in job["meta"], job["meta"]
    b.finish(job["id"], draft(message_kind="faq", reply_draft="好的，马上弄", slots={LINK_SLOT: value}, stage_proposal=""))
    packet = b.prm.store.packet_for_job(job["id"])
    if packet and packet["status"] == "pending":
        assert b.prm.decide(packet["id"], "send")["ok"]
        b.prm.send_approved()


def test_a_prefix_said_after_the_link_steps_asked_her_one_makes_the_links(bot, clock):
    """She wanted links (「要」) but said no prefix: each link step asked her one (no action) and the round went on; when
    she tells the prefix, the links are asked for again, now with it, and made (the review's probe, #3)."""
    order = two_signed_for(bot, clock, links="要")
    job, commission = step(bot, DESIGNATED)
    opens(bot, commission)
    job, asked = step(bot, reply="想问一下专属链接的前缀写什么呢？")
    assert job["meta"]["step"]["action"] == "dy.channel_goods" and job["meta"]["step"]["prefix"] == ""
    assert "还不知道前缀就不填平台动作，在回复里问她前缀写什么" in job["meta"]["system_note"]
    assert bot.prm.decide(asked["id"], "send")["ok"]
    bot.prm.send_approved()
    rest = answer_steps(bot)
    assert [(st.get("item") or None, st.get("action")) for st in rest] == [
        (DY_ITEM2, None), (DY_ITEM2, "dy.channel_goods"), (None, None)]     # B's 专属佣金, B's link, the round's end
    links = bot.prm.store.deal_packets(order["deal_id"], tools=("dy.channel_goods",))
    assert {p["platform"]["action"]["item_id"] for p in links if p["status"] == "sent"} == {DY_ITEM2}   # (the Mac filled B's)
    says_prefix(bot)
    after = answer_steps(bot)
    assert [(st.get("item"), st.get("action"), st.get("prefix")) for st in after][:1] == [
        (DY_ITEM, "dy.channel_goods", "【小美专属】")]
    links = bot.prm.store.deal_packets(order["deal_id"], tools=("dy.channel_goods",))
    assert {p["platform"]["action"]["item_id"] for p in links if p["status"] == "sent"} == {DY_ITEM, DY_ITEM2}
    says_prefix(bot, "【小美专属】")                                      # said again: nothing more to ask
    assert bot.worker_takes() is None


def test_a_prefix_too_long_is_never_dictated_and_a_link_dropped_at_it_is_asked_again(bot, clock):
    """The verifier's probe P3 (review 2026-11-09 F1): she asks for 「【小美美呀专属】」 (14 characters; the usual limit
    is 12): the link step's note does not dictate it, it says why and how the shop cuts it short (「【小美美专属】」), or
    to ask her.  The Mac wrote it anyway, the preview stopped at it, the operator dropped it; B goes on; once she says
    a shorter one, A's link is asked for again with it, then B's, and the note says why it comes now."""
    two_signed_for(bot, clock, links="【小美美呀专属】")
    _, commission = step(bot, DESIGNATED)
    opens(bot, commission)
    long = {**CHANNEL, "title_prefix": "【小美美呀专属】"}
    job, link = step(bot, long, verdict="blocked", reply="链接：{链接}")
    note = job["meta"]["system_note"]
    assert "title_prefix 填「【小美美呀专属】」" not in note and job["meta"]["step"]["prefix"] == ""
    assert "她要的前缀「【小美美呀专属】」是 14 个字符，超过一般商品的上限 12 个" in note
    assert "（比如「【小美美专属】」）；拿不准就不填平台动作，在回复里问她前缀怎么写" in note
    assert bot.prm.decide(link["id"], "drop")["ok"]
    _, commission2 = step(bot, {**DESIGNATED, "item_id": DY_ITEM2})
    says_prefix(bot, "【小美专属】")                                     # (B's 专属佣金 waits: nothing asked yet)
    assert bot.worker_takes() is None
    opens(bot, commission2)
    job = bot.worker_takes()
    assert job["meta"]["step"] == {"packet": commission2["id"], "delivered": True, "item": DY_ITEM,
                                   "action": "dy.channel_goods", "prefix": "【小美专属】", "uid": DY_UID}
    assert "title_prefix 填「【小美专属】」" in job["meta"]["system_note"]
    bot.finish(job["id"], draft(message_kind="money", reply_draft="链接：{链接}", slots={},
                                platform=block(CHANNEL)))
    opens(bot, bot.prm.store.packet_for_job(job["id"]), record(link=DY_LINK, channel_id="7200000000000001"))
    rest = answer_steps(bot)
    assert [(st.get("item") or None, st.get("action")) for st in rest][:1] == [(DY_ITEM2, "dy.channel_goods")]


def test_a_prefix_said_when_the_round_is_idle_says_so_first(bot, clock):
    """review 2026-11-09 F7: a link step asked because her prefix came starts with that, not with the signature."""
    two_signed_for(bot, clock, links="要")
    _, commission = step(bot, DESIGNATED)
    opens(bot, commission)
    _, asked = step(bot, reply="前缀写什么呢？")
    assert bot.prm.decide(asked["id"], "send")["ok"]
    bot.prm.send_approved()
    answer_steps(bot)                                                     # B's 专属佣金, its link step, the end
    says_prefix(bot)
    job = bot.worker_takes()
    note = job["meta"]["system_note"]
    assert note.startswith("合作单刚记下她要的专属链接前缀「【小美专属】」。接着开寄样单")
    assert "达人已经签收了寄样单" not in note and job["meta"]["step"]["prefix"] == "【小美专属】"


def test_a_link_step_asked_with_a_prefix_is_not_asked_again(bot, clock):
    two_signed_for(bot, clock)
    _, commission = step(bot, DESIGNATED)
    opens(bot, commission)
    job, _ = step(bot)                                                    # A's link step drafted nothing
    assert job["meta"]["step"]["prefix"] == "【小美专属】"
    job, _ = step(bot, {**DESIGNATED, "item_id": DY_ITEM2})               # the chain went on to B
    says_prefix(bot, "【美美专属】")                                      # another prefix: the chain goes on, A stays asked
    assert all(st.get("item") != DY_ITEM for st in answer_steps(bot))


def test_a_prefix_that_came_while_the_steps_draft_waited_is_taken_when_it_is_sent(bot, clock):
    """The verifier's probe P1b: the chain's link step asked her the prefix; she says it before that draft went out:
    sent, the link is asked for again with it."""
    two_signed_for(bot, clock, links="要")
    _, commission = step(bot, DESIGNATED)
    opens(bot, commission)
    _, asked = step(bot, reply="想问一下专属链接的前缀写什么呢？")
    says_prefix(bot)
    assert bot.worker_takes() is None
    assert bot.prm.decide(asked["id"], "send")["ok"]
    bot.prm.send_approved()
    job = bot.worker_takes()
    assert job["meta"]["step"]["item"] == DY_ITEM and job["meta"]["step"]["prefix"] == "【小美专属】"


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


def test_without_a_chain_a_prefix_said_later_asks_for_the_link_again(bot, clock):
    """No sample order: her 专属佣金 opened, links wanted with no prefix: the link step asked her one (no action); she
    tells it: that product's link is asked for again, now with the prefix, once (review 2026-11-09 #3)."""
    wants_links(bot, "要")
    commission = drafted(bot, reply="专属佣金设好啦")
    opens(bot, commission)
    job, asked = step(bot, reply="前缀写什么呢？")
    assert job["meta"]["step"] == {"packet": commission["id"], "delivered": False, "item": DY_ITEM,
                                   "action": "dy.channel_goods", "prefix": ""}
    assert bot.prm.decide(asked["id"], "send")["ok"]
    bot.prm.send_approved()
    assert bot.worker_takes() is None
    says_prefix(bot)
    job, link = step(bot, CHANNEL, reply="链接：{链接}")
    assert job["meta"]["step"]["prefix"] == "【小美专属】" and "title_prefix 填「【小美专属】」" in job["meta"]["system_note"]
    assert link["status"] == "pending"
    assert bot.prm.decide(link["id"], "drop")["ok"]                     # 「不发」: a person's decision
    says_prefix(bot, "【美美专属】")
    assert bot.worker_takes() is None


def test_without_a_chain_a_prefix_that_came_while_the_steps_draft_waited_is_not_lost(bot, clock):
    """The verifier's probe P1 (review 2026-11-09 F2): the link step asked her the prefix (its draft waits in 待审批);
    she says it meanwhile: nothing can be asked yet, the deal says the link waits; the step's draft sent, the link is
    asked for with the prefix, and the notice goes."""
    wants_links(bot, "要")
    commission = drafted(bot, reply="专属佣金设好啦")
    opens(bot, commission)
    job, asked = step(bot, reply="前缀写什么呢？")
    assert asked["status"] == "pending" and job["meta"]["step"]["prefix"] == ""
    says_prefix(bot)
    assert bot.worker_takes() is None
    waits = next(n["text"] for n in bot.prm.store.deal(1)["notices"] if n["key"] == "links")
    assert waits == f"专属链接（她要：【小美专属】）：商品 {DY_ITEM} 的还没建，手头这一步办完系统就接着派，一次一个商品"
    assert bot.prm.decide(asked["id"], "send")["ok"]
    bot.prm.send_approved()
    job, link = step(bot, CHANNEL, reply="链接：{链接}")
    assert job["meta"]["step"]["prefix"] == "【小美专属】" and job["meta"]["step"]["item"] == DY_ITEM
    assert job["meta"]["system_note"].startswith(f"商品 {DY_ITEM} 的专属佣金（20%）已经在平台上开好了")
    assert all(n["key"] != "links" for n in bot.prm.store.deal(1)["notices"])


def test_without_a_chain_a_link_wished_before_its_prefix_says_it_waits_for_one(bot, clock):
    """Links wanted after her 专属佣金 opened, no prefix said yet (or one too long): nothing to ask the Mac, the deal says
    which product waits and for what (review 2026-11-09 F2)."""
    commission = drafted(bot, reply="专属佣金设好啦")
    opens(bot, commission)
    step(bot)                                                             # the step after it: nothing more
    wants_links(bot, "要")
    assert bot.worker_takes() is None
    text = next(n["text"] for n in bot.prm.store.deal(1)["notices"] if n["key"] == "links")
    assert text == f"专属链接（她要：要）：商品 {DY_ITEM} 的还没建，还不知道前缀：问到她要的前缀、记进合作单以后系统就派"
    wants_links(bot, "【小美美呀专属】")
    text = next(n["text"] for n in bot.prm.store.deal(1)["notices"] if n["key"] == "links")
    assert "她要的前缀「【小美美呀专属】」超过 12 个字符，要她给个短一点的" in text
    wants_links(bot, "不要")
    assert all(n["key"] != "links" for n in bot.prm.store.deal(1)["notices"])


def test_an_owed_link_step_is_asked_afresh_when_her_prefix_came_meanwhile(bot, clock):
    """review 2026-11-09 F7: a link step owed (a person had the deal) with a note that asks her the prefix; the deal
    learned it meanwhile (a 百应 draft, say): the step goes out with a new note that gives it."""
    wants_links(bot, "要")
    commission = drafted(bot, reply="专属佣金设好啦")
    execution = approve(bot, commission)
    assert bot.prm.deal_action("mode", {"deal_id": 1, "mode": "human"})["ok"]
    mac_runs(bot, execution, record())
    assert bot.worker_takes() is None
    [owed] = [n for n in bot.prm.store.deal(1)["notices"] if n["key"] == "step"]
    assert owed["action"] == "dy.channel_goods" and owed["prefix"] == "" and "问她前缀写什么" in owed["note"]
    bot.prm.store.update_deal(1, clock.now, merge_slots={LINK_SLOT: "【小美专属】"})
    assert bot.prm.deal_action("mode", {"deal_id": 1, "mode": "copilot"})["ok"]
    bot.prm.sweep_owed_steps()
    job = bot.worker_takes()
    note = job["meta"]["system_note"]
    assert note.startswith("这一步欠着的时候，合作单记下了她要的专属链接前缀「【小美专属】」。")
    assert "title_prefix 填「【小美专属】」" in note and "问她前缀写什么" not in note
    assert job["meta"]["step"]["prefix"] == "【小美专属】" and job["meta"]["step"]["item"] == DY_ITEM
    assert bot.worker_takes() is None and all(n["key"] != "step" for n in bot.prm.store.deal(1)["notices"])


def test_without_a_chain_a_link_dropped_at_a_prefix_too_long_is_asked_again_with_a_shorter_one(bot, clock):
    """review 2026-11-09 F1 without a sample order: the link step does not dictate her 14-character prefix; the Mac
    wrote it anyway, the preview stopped at it, the operator dropped it; she gives a shorter one: asked again, once."""
    wants_links(bot, "【小美美呀专属】")
    commission = drafted(bot, reply="专属佣金设好啦")
    opens(bot, commission)
    job, link = step(bot, {**CHANNEL, "title_prefix": "【小美美呀专属】"}, verdict="blocked", reply="链接：{链接}")
    assert job["meta"]["step"]["prefix"] == "" and "不能照写" in job["meta"]["system_note"]
    assert bot.prm.decide(link["id"], "drop")["ok"]
    assert bot.worker_takes() is None
    says_prefix(bot, "【小美专属】")
    job = bot.worker_takes()
    assert job["meta"]["step"]["item"] == DY_ITEM and job["meta"]["step"]["prefix"] == "【小美专属】"
    assert job["meta"]["system_note"].startswith("合作单刚记下她要的专属链接前缀「【小美专属】」。")
    bot.finish(job["id"], draft(message_kind="other", reply_draft="", slots={}))     # (drafted nothing this time)
    says_prefix(bot, "【小美专属】")
    assert bot.worker_takes() is None                                    # asked with it once: not again


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
