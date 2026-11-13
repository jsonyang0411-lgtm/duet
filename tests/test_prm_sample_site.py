"""选款网站 on the bot's side (src/prm/sample_site.py, PRM_SAMPLE_SITE_MIN / PRM_SAMPLE_SITE_AUTO; 2026-11-13).

A creator picks her sample hats on a web page (``sample_site/``) and types the receiver herself; the Mac takes the
submissions from the site and each becomes a sample order's draft the bot keeps for a person.  Here the Mac is played
by hand (the jobs' answers are written out); tests/test_sample_site_mac.py runs the real worker against the real site.
Names, numbers and addresses are made up.
"""

import json

import pytest

from platform_fakes import DY_ITEM2, DY_SKU2, DY_SKU2B, JST_ORDER
from test_prm import ADMIN, CREATOR, Bot, Clock, draft

from src.prm import platforms, sample_site
from src.prm.service import event_out, snapshot
from src.prm.store import EXECUTING, PENDING, SUPERSEDED

URL = "https://xuan.example.test/s/AbCdEfGhIjKlMnOpQrStUvWxYz012345"
LINK_LINE = f"老师，我们把在售的款式放在这个页面了，您看看喜欢哪几款，直接在上面选就可以：\n{URL}"
REPLY = "老师，您选的款我们收到啦，已经在安排了～"
PHONE = "13800000000"
RECEIVER = {"name": "小美", "phone": PHONE, "province": "浙江省", "city": "杭州市", "district": "西湖区",
            "address": "测试路 1 号 2 幢 301 室"}
ITEMS = [{"sku": "T1001A-F", "name": "测试渔夫帽", "colour": "黑色", "size": "均码", "image": "a" * 32},
         {"sku": "T1001B-F", "name": "测试渔夫帽", "colour": "米白", "size": "均码", "image": "b" * 32}]
ACCOUNT = "xiaomei_test01"


def make(clock, tmp_path, **prm):
    b = Bot(clock, tmp_path, **{"prm_sample_site_min": 5, **prm})
    b.prm.register(CREATOR, "小美", "测试达人")
    return b


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def bot(clock, tmp_path):
    b = make(clock, tmp_path)
    yield b
    b.prm.stop()
    b.relay.stop()


def party(b):
    return b.prm.store.party_by_wxid(CREATOR)


def deal_id(b):
    return b.prm.store.deal_of(party(b)["id"])["id"]


def link_her(b, account=ACCOUNT):
    assert b.prm.store.add_creator_link(party(b)["id"], account, source="console", now=b.clock.now)


def submission(site_id=7, **over):
    return {"id": site_id, "rev": 1, "ref": f"p{over.pop('party_id', 1)}", "kind": "personal", "account": "",
            "items": [dict(i) for i in ITEMS], "receiver": dict(RECEIVER), "purged": False, **over}


def pull_answer(*subs, acked=(), stopped=None):
    return {"version": 1, "op": "site_pull", "acked": list(acked), "submissions": list(subs), "stopped": stopped}


def round_(b, *subs, acked=(), stopped=None):
    """One round: the bot asks, the Mac answers with these submissions; returns the round's job as the worker saw it."""
    b.clock.now += 6 * 60
    assert b.prm.sample_site.sweep() is not None
    job = b.worker_takes()
    assert job["meta"]["op"] == "site_pull"
    b.finish(job["id"], pull_answer(*subs, acked=acked, stopped=stopped))
    return job


def action(receiver_phone="〈手机1〉", account=ACCOUNT):
    return {"action": "jst.sample_order", "shop": "dy", "creator_platform": "dy", "creator_account": account,
            "items": [{"item_id": DY_ITEM2, "skus": [{"sku_id": DY_SKU2, "qty": 1}, {"sku_id": DY_SKU2B, "qty": 1}]}],
            "receiver": {**RECEIVER, "phone": receiver_phone}}


def block(verdict="ready", problems=(), audit=True, **over):
    return {"version": 1, "action": action(), "requested": None, "verdict": verdict, "problems": list(problems),
            "facts": [{"label": "执行时还会做", "value": platforms.AUDIT_FACT}], "steps": [],
            "run": {"steps": ["create", "audit"] if audit else ["create"]} if verdict == "ready" else {},
            "catalog": {"synced_at": 1761400000.0, "title": "测试贝雷帽"}, "previewed_at": 1761400000.0, **over}


def order_answer(site_id=7, **over):
    return draft(**{"intent": "选款网站：她自己选了 2 顶", "slots": {}, "stage_proposal": "", "message_kind": "other",
                    "reply_draft": REPLY, "reason": "", "platform": block(),
                    "site_order": {"v": 1, "id": site_id, "photo_check": False}, **over})


def order_job(b):
    job = b.worker_takes()
    assert job["meta"]["op"] == "site_order", job["meta"]
    return job


def drafted(b, site_id=7, **over):
    """A submission taken in a round and its order's draft back from the Mac: the waiting packet."""
    link_her(b)
    round_(b, submission(site_id))
    job = order_job(b)
    b.finish(job["id"], order_answer(site_id, **over))
    row = b.prm.store.site_order(site_id)
    return b.prm.store.packet(row["packet_id"]) if row["packet_id"] else None


# ── off by default ─────────────────────────────────────────────────────────

def test_everything_is_off_by_default(clock, tmp_path):
    b = Bot(clock, tmp_path)
    b.prm.register(CREATOR, "小美")
    try:
        assert b.prm.sample_site.enabled is False
        assert b.prm.deal_action("site_link", {"deal_id": deal_id(b)}) == {"ok": False, "error": sample_site.OFF_TEXT}
        clock.now += 86400
        assert b.prm.sample_site.sweep() is None
        b.prm.tick()
        assert b.relay.store.list_jobs(10) == []
        assert b.prm.snapshot()["sample_site"] == {"min": 0, "auto": False, "last": None, "unmatched": [],
                                                    "failed": []}
    finally:
        b.prm.stop()
        b.relay.stop()


def test_the_config_takes_the_two_switches_and_refuses_nonsense(monkeypatch):
    from src.config import load_config

    for name in ("PRM_SAMPLE_SITE_MIN", "PRM_SAMPLE_SITE_AUTO"):
        monkeypatch.delenv(name, raising=False)
    cfg = load_config()
    assert cfg.prm_sample_site_min == 0 and cfg.prm_sample_site_auto == "off"
    monkeypatch.setenv("PRM_SAMPLE_SITE_MIN", "5")
    monkeypatch.setenv("PRM_SAMPLE_SITE_AUTO", "on")
    cfg = load_config()
    assert cfg.prm_sample_site_min == 5 and cfg.prm_sample_site_auto == "on"
    monkeypatch.setenv("PRM_SAMPLE_SITE_MIN", "1")
    with pytest.raises(Exception, match="PRM_SAMPLE_SITE_MIN"):
        load_config()


# ── 「选款链接」 ───────────────────────────────────────────────────────────

def test_a_link_is_one_job_and_its_fixed_sentence_waits_for_a_person(bot, clock):
    """The job has no line of hers and says what it is; the Mac's answer (a fixed sentence with the link) waits in
    待审批 tagged, the deal says a person must look first; no 「稍等」 goes for it; sent, the link goes out as it came."""
    answer = bot.prm.deal_action("site_link", {"deal_id": deal_id(bot), "max": 5})
    assert answer["ok"] is True
    job = bot.worker_takes()
    assert job["id"] == answer["job"] and job["text"] == ""
    assert job["meta"]["site_link"] == {"by": "console", "ask_account": True, "max": 5}      # (her 抖音号 is not known)
    assert "op" not in job["meta"]                                    # a draft job of her chat, like 补发货盘
    bot.finish(job["id"], draft(intent="发选款链接", slots={}, stage_proposal="", message_kind="other",
                                reply_draft=LINK_LINE, reason="", site_link={"id": 3, "max": 5}))
    packet = bot.prm.store.packet_for_job(job["id"])
    assert packet["status"] == PENDING and packet["route"] == "review" and packet["text"] == LINK_LINE
    assert packet["meta"]["site_link"] is True
    shown = snapshot(bot.prm.store, running=True)
    assert [p["site_link"] for p in shown["pending"] if p["id"] == packet["id"]] == [True]
    notices = " ".join(n["text"] for n in bot.prm.store.deal(deal_id(bot))["notices"])
    assert sample_site.LINK_NOTE in notices
    clock.now += 3600
    bot.prm.send_holding_replies()
    assert bot.texts == []                                            # we started it: nothing of hers to hold
    [ev] = bot.prm.store.events(deal_id(bot), kinds=("site_link",))
    assert ev["actor"] == "console" and event_out(ev)["name"] == "派发选款链接的任务"
    assert bot.prm.decide(packet["id"], "send")["ok"]
    bot.prm.send_approved()
    assert bot.texts and bot.texts[-1][0] == CREATOR and URL in " ".join(t for _, t in bot.texts)


def test_a_link_does_not_ask_for_her_account_when_exactly_one_is_known(bot):
    link_her(bot)
    assert bot.prm.deal_action("site_link", {"deal_id": deal_id(bot)})["ok"]
    assert bot.worker_takes()["meta"]["site_link"] == {"by": "console", "ask_account": False}


@pytest.mark.parametrize("most", [0, 11, "3", True])
def test_a_link_takes_only_a_sane_number_of_hats(bot, most):
    answer = bot.prm.deal_action("site_link", {"deal_id": deal_id(bot), "max": most})
    assert answer["ok"] is False and "1–10" in answer["error"]
    assert bot.relay.store.list_jobs(5) == []


def test_no_link_while_a_person_has_the_chat_or_a_draft_of_hers_waits(bot, clock):
    assert bot.prm.deal_action("mode", {"deal_id": deal_id(bot), "mode": "human"})["ok"]
    answer = bot.prm.deal_action("site_link", {"deal_id": deal_id(bot)})
    assert answer == {"ok": False, "error": "现在不在副驾驶模式（人工接管或暂停），不发选款链接"}
    assert bot.prm.deal_action("mode", {"deal_id": deal_id(bot), "mode": "copilot"})["ok"]
    bot.prm.handle(bot.msg("在吗"))
    answer = bot.prm.deal_action("site_link", {"deal_id": deal_id(bot)})
    assert answer["ok"] is False and "正在写" in answer["error"]


def test_the_admin_types_the_command_in_wechat(clock, tmp_path):
    from src.prm.admin_commands import HELP_TEXT, VERBS

    assert VERBS["选款链接"] == "site_link" and "选款链接 名字" in HELP_TEXT
    b = make(clock, tmp_path, prm_wechat_commands="on")
    try:
        run = b.prm.deal_commands.admin_command("选款链接 小美")
        assert run is not None and "已派发选款链接的任务" in run()
        assert b.worker_takes()["meta"]["site_link"]["by"] == "admin"
    finally:
        b.prm.stop()
        b.relay.stop()


# ── the rounds ─────────────────────────────────────────────────────────────

def test_a_round_is_asked_once_an_interval_and_one_at_a_time(bot, clock):
    assert bot.prm.sample_site.sweep() is None                        # (a bot just started waits a minute)
    clock.now += 61
    first = bot.prm.sample_site.sweep()
    assert first is not None and bot.prm.sample_site.sweep() is None  # one job at a time
    job = bot.worker_takes()
    assert job["meta"] == {"op": "site_pull", "acks": []}
    assert bot.relay.store.get(job["id"])["chat_id"] == sample_site.SYNC_CHAT
    bot.finish(job["id"], pull_answer())
    assert bot.prm.sample_site.sweep() is None                        # not before the interval is over
    clock.now += 5 * 60
    assert bot.prm.sample_site.sweep() is not None
    assert bot.prm.sample_site.status()["last"]["submissions"] == 0


def test_a_submission_becomes_one_order_job_with_the_phone_as_her_placeholder(bot):
    """The order's job carries her codes, the receiver she typed with the phone masked, her known 抖音号; the round's
    own job keeps only a summary (the receivers were in its answer); the same submission in the next round, its job
    still with the Mac, queues nothing again."""
    link_her(bot)
    pulled = round_(bot, submission())
    summary = {"waiting": 0, "orders": 0, "unmatched": 0, "failed": 0}
    [same] = sample_site.check_pull(pull_answer(submission()))["submissions"]
    bot.prm.sample_site._take(same, bot.clock.now, summary)           # (the site gives it again: its job still waits)
    assert summary == {"waiting": 1, "orders": 0, "unmatched": 0, "failed": 0}
    assert len([j for j in bot.relay.store.list_jobs(20) if '"site_order"' in str(j["meta"])]) == 1
    job = order_job(bot)
    order = job["meta"]["site_order"]
    assert order["id"] == 7 and order["account"] == ACCOUNT and [i["sku"] for i in order["items"]] == [
        "T1001A-F", "T1001B-F"]
    assert order["receiver"] == {**RECEIVER, "phone": "〈手机1〉"}
    assert PHONE not in json.dumps(job["meta"], ensure_ascii=False)
    assert job["text"] == "" and job["meta"]["party_id"] == party(bot)["id"]
    kept = bot.relay.store.get(pulled["id"])["result_text"]
    assert PHONE not in kept and "测试路" not in kept and json.loads(kept)["summary"]["orders"] == 1
    row = bot.prm.store.site_order(7)
    assert row["state"] == "queued" and row["job_id"] == job["id"] and row["hats"] == 2 and row["acked"] == 0
    assert "address" not in row and "phone" not in row                # (no receiver in the bot's own table)


def test_the_orders_draft_is_the_bots_own_and_she_cannot_put_it_out_of_date(bot, clock):
    packet = drafted(bot)
    assert packet["status"] == PENDING and packet["job_id"] is None and packet["tool"] == "jst.sample_order"
    assert packet["text"] == REPLY and packet["chat_id"] == CREATOR
    assert packet["meta"]["site_order"] == {"id": 7, "hats": 2, "photo_check": False}
    assert packet["platform"]["action"]["receiver"]["phone"] == "〈手机1〉"
    assert packet["platform"]["preview"]["run"] == {"steps": ["create", "audit"]}
    row = bot.prm.store.site_order(7)
    assert row["state"] == "drafted" and row["packet_id"] == packet["id"]
    [ev] = bot.prm.store.events(deal_id(bot), kinds=("site_order",))
    assert ev["detail"] == {"packet": packet["id"], "site": 7, "hats": 2} and "不用对图" in event_out(ev)["name"]
    shown = [p for p in snapshot(bot.prm.store, running=True)["pending"] if p["id"] == packet["id"]]
    assert shown[0]["site_order"]["photo_check"] is False and shown[0]["platform"]["can_execute"] is True
    bot.prm.handle(bot.msg("老师我选好啦"))                              # her next message: a reply draft of its own
    job = bot.worker_takes()
    bot.finish(job["id"], draft(reply_draft="收到～", message_kind="greeting", slots={}, stage_proposal=""))
    assert bot.prm.store.packet(packet["id"])["status"] == PENDING
    other = bot.prm.store.packet_for_job(job["id"])
    assert other["status"] != SUPERSEDED


def test_approving_makes_and_audits_the_order_with_the_number_put_back(bot):
    packet = drafted(bot)
    assert bot.prm.decide(packet["id"], "execute")["ok"]
    job = bot.worker_takes()
    meta = job["meta"]
    assert meta["op"] == "platform_execute" and meta["run"] == {"steps": ["create", "audit"]}
    assert meta["action"]["receiver"]["phone"] == PHONE and meta["action"]["creator_account"] == ACCOUNT
    assert bot.prm.store.packet(packet["id"])["status"] == EXECUTING
    record = platforms.record(platforms.VERIFIED)
    record.update(key=meta["key"], created={"sample_order_no": JST_ORDER},
                  steps=[{"name": "create", "label": "建", "execute": True, "command": "", "exit_code": 0, "seconds": 1,
                          "verdict": "", "outcome": platforms.VERIFIED, "problems": [], "facts": [], "raw": ""},
                         {"name": "audit", "label": "审", "execute": True, "command": "", "exit_code": 0, "seconds": 1,
                          "verdict": "", "outcome": platforms.VERIFIED, "problems": [], "facts": [], "raw": ""}])
    bot.finish(job["id"], record)
    bot.prm.send_approved()
    assert (CREATOR, REPLY) in bot.texts


def test_no_photo_check_stands_in_the_way_with_the_switch_on(clock, tmp_path):
    """PRM_DRAFT_CONTEXT on: any other sample order without the Mac's photo mark cannot be approved; this one can
    (she picked our own codes), its new preview asks for no photo check, and the answer is not held for one."""
    b = make(clock, tmp_path, prm_draft_context="on")
    try:
        packet = drafted(b)
        assert "draft_context" not in packet["meta"]
        shown = [p for p in snapshot(b.prm.store, running=True, photo_check=True)["pending"] if p["id"] == packet["id"]]
        assert shown[0]["platform"]["can_execute"] is True and shown[0]["platform"]["problems"] == []
        assert b.prm.wechat_review._photo_stop(packet) == ""
        assert b.prm.decide(packet["id"], "repreview")["ok"]
        job = b.worker_takes()
        assert job["meta"]["op"] == "platform_preview" and job["meta"]["site_order"] == {"id": 7}
        assert "draft_context" not in job["meta"]
        b.finish(job["id"], block())                                  # (the Mac's answer has no photo mark)
        fresh = b.prm.store.packet(packet["id"])
        assert fresh["platform"]["preview"]["verdict"] == "ready" and "draft_context" not in fresh["meta"]
        assert b.prm.decide(packet["id"], "execute")["ok"]
        assert b.worker_takes()["meta"]["run"] == {"steps": ["create", "audit"]}
    finally:
        b.prm.stop()
        b.relay.stop()


def test_an_ordinary_sample_order_is_still_held_for_its_photo_check(clock, tmp_path):
    """The exemption is the site's alone: a chat draft's sample order without the photo mark stays held."""
    b = make(clock, tmp_path, prm_draft_context="on")
    try:
        b.prm.handle(b.msg("寄这两顶"))
        job = b.worker_takes()
        b.finish(job["id"], draft(reply_draft="好的老师", message_kind="other", slots={}, stage_proposal="",
                                  platform=block(audit=False)))
        packet = b.prm.store.packet_for_job(job["id"])
        assert packet["platform"]["preview"]["verdict"] == "blocked"
        assert b.prm.decide(packet["id"], "execute")["ok"] is False
    finally:
        b.prm.stop()
        b.relay.stop()


def test_a_stored_submission_is_acked_in_the_next_round_and_only_once(bot):
    drafted(bot)
    job = round_(bot, submission(), acked=[])                         # (the site gives it again: not acked yet)
    assert job["meta"]["acks"] == [7]
    assert len(bot.prm.store.deal_packets(deal_id(bot), statuses=(PENDING,))) == 1      # no second draft
    assert bot.prm.store.site_order(7)["acked"] == 0                  # (the Mac did not say it got there)
    job = round_(bot, acked=[7])
    assert job["meta"]["acks"] == [7] and bot.prm.store.site_order(7)["acked"] == 1
    assert round_(bot)["meta"]["acks"] == []


def test_a_preview_that_does_not_pass_still_waits_for_a_person_and_says_why(bot):
    packet = drafted(bot, platform=block("blocked", ["这位达人归别的账号跟进"]))
    assert packet["status"] == PENDING and packet["needs_attention"] == 1
    assert "归别的账号跟进" in packet["reason"]
    assert bot.prm.decide(packet["id"], "execute")["ok"] is False
    assert "归别的账号跟进" in " ".join(n["text"] for n in bot.prm.store.deal(deal_id(bot))["notices"])


def test_an_order_the_mac_could_not_build_is_tried_again_and_then_only_hourly(bot, clock):
    link_her(bot)
    failed = {"version": 1, "action": None, "requested": None, "verdict": "invalid", "run": {}, "facts": [],
              "steps": [], "problems": ["她选的编码 T1001A-F 这台 Mac 的目录表里没有"], "previewed_at": 1.0}
    for n in range(1, sample_site.TRIES + 1):
        round_(bot, submission())
        job = order_job(bot)
        bot.finish(job["id"], order_answer(platform=failed))
        row = bot.prm.store.site_order(7)
        assert row["state"] == "failed" and row["tries"] == n and row["packet_id"] is None
    assert "目录表里没有" in " ".join(x["text"] for x in bot.prm.store.deal(deal_id(bot))["notices"])
    round_(bot, submission())
    assert bot.relay.next_job(0, ("prm",)) is None                    # not again within the hour
    clock.now += sample_site.RETRY_SEC
    round_(bot, submission())
    job = order_job(bot)
    bot.finish(job["id"], order_answer())
    assert bot.prm.store.site_order(7)["state"] == "drafted"
    assert "目录表里没有" not in " ".join(x["text"] for x in bot.prm.store.deal(deal_id(bot))["notices"])


def test_a_lost_mac_fails_the_try_and_the_site_still_has_it(bot):
    link_her(bot)
    round_(bot, submission())
    job = order_job(bot)
    bot.finish(job["id"], ok=False, text="")
    row = bot.prm.store.site_order(7)
    assert row["state"] == "failed" and row["acked"] == 0 and row["tries"] == 1
    assert round_(bot, submission())["meta"]["acks"] == []            # never acked: the site gives it again
    assert order_job(bot)["meta"]["site_order"]["id"] == 7


def test_a_general_links_submission_goes_to_the_creator_known_by_that_account(bot):
    link_her(bot)
    round_(bot, submission(9, ref="general", kind="general", account=ACCOUNT.upper()))
    job = order_job(bot)
    assert job["meta"]["party_id"] == party(bot)["id"] and job["meta"]["site_order"]["account"] == ACCOUNT.upper()


def test_nobody_on_the_list_waits_and_the_admin_hears_once(bot):
    sub = submission(9, ref="general", kind="general", account="somebody_else")
    round_(bot, sub)
    assert bot.relay.next_job(0, ("prm",)) is None
    row = bot.prm.store.site_order(9)
    assert row["state"] == "unmatched" and row["account"] == "somebody_else" and row["party_id"] is None
    assert bot.prm.sample_site.status()["unmatched"][0]["account"] == "somebody_else"
    job = round_(bot, sub)
    assert job["meta"]["acks"] == []                                  # it stays on the site until somebody is found
    link_her(bot, "somebody_else")                                    # an operator links the account to her
    round_(bot, sub)
    assert order_job(bot)["meta"]["site_order"]["id"] == 9


def test_a_link_of_a_creator_whose_account_is_not_known_is_not_guessed(bot):
    round_(bot, submission())                                         # personal link, no account typed, none known
    assert bot.relay.next_job(0, ("prm",)) is None
    row = bot.prm.store.site_order(7)
    assert row["state"] == "failed" and "抖音号" in row["why"]
    round_(bot, submission(account="typed_by_her"))                   # (a link that asked: what she typed is used)
    assert order_job(bot)["meta"]["site_order"]["account"] == "typed_by_her"


def test_a_submission_purged_on_the_site_is_given_up_and_acked(bot):
    link_her(bot)
    round_(bot, submission(purged=True))
    assert bot.relay.next_job(0, ("prm",)) is None
    assert bot.prm.store.site_order(7)["state"] == "dropped"
    assert round_(bot)["meta"]["acks"] == [7]


def test_a_round_that_did_not_go_through_is_said_and_changes_nothing(bot):
    round_(bot, stopped={"verdict": "unauthorized", "message": "选款网站不认这台 Mac 的管理密钥"})
    last = bot.prm.sample_site.status()["last"]
    assert last["stopped"]["verdict"] == "unauthorized" and last["submissions"] == 0
    b_job = round_(bot)
    assert b_job["meta"]["acks"] == []


def test_an_older_mac_worker_is_said_as_what_it_is(bot, clock):
    """A worker from before this feature: a round fails as a creator's job without a creator, and a 选款链接 job is
    drafted by the model without any link: neither is taken for the real thing."""
    clock.now += 6 * 60
    assert bot.prm.sample_site.sweep() is not None
    job = bot.worker_takes()
    assert bot.relay.finish(job["id"], {"ok": False, "text": "", "error": "任务里没有达人编号", "duration_s": 0})
    bot.relay.tick()
    last = bot.prm.sample_site.status()["last"]
    assert last["stopped"] == {"verdict": "old_worker", "message": sample_site.OLD_WORKER}
    assert bot.prm.deal_action("site_link", {"deal_id": deal_id(bot)})["ok"]
    job = bot.worker_takes()
    bot.finish(job["id"], draft(reply_draft="老师您好～想了解一下您对哪些款式感兴趣呢", message_kind="other", slots={},
                                stage_proposal=""))
    assert bot.prm.store.packet_for_job(job["id"]) is None
    assert sample_site.NO_LINK in " ".join(n["text"] for n in bot.prm.store.deal(deal_id(bot))["notices"])


def test_what_counts_as_a_link_of_the_site():
    assert sample_site.has_link(LINK_LINE) and sample_site.has_link("http://127.0.0.1:8080/g/" + "a" * 32)
    for text in ("", "老师您好", "https://xuan.example.test/", "https://xuan.example.test/s/short", "/s/" + "a" * 32):
        assert not sample_site.has_link(text)


def test_junk_in_the_macs_answer_is_left_out():
    clean = sample_site.check_pull(pull_answer(
        {"id": "7"}, {"id": True}, {"id": -1}, "x",
        {"id": 8, "ref": "p1", "account": "bad account!", "items": [{"sku": "x"}, {"sku": "T1001A-F", "image": "zz"}],
         "receiver": {"name": "小\x00美", "phone": 1}}, acked=[7, "8", True]))
    assert clean["acked"] == [7] and [s["id"] for s in clean["submissions"]] == [8]
    [sub] = clean["submissions"]
    assert sub["account"] == "" and sub["items"] == [{"sku": "T1001A-F", "name": "", "colour": "", "size": "",
                                                      "image": ""}]
    assert sub["receiver"]["name"] == "小 美" and sub["receiver"]["phone"] == ""
    with pytest.raises(ValueError):
        sample_site.check_pull({"op": "something_else"})


# ── PRM_SAMPLE_SITE_AUTO ───────────────────────────────────────────────────

def test_with_the_auto_switch_a_ready_order_is_made_without_anybody(clock, tmp_path):
    b = make(clock, tmp_path, prm_sample_site_auto="on")
    try:
        packet = drafted(b)
        assert packet["status"] == EXECUTING
        job = b.worker_takes()
        assert job["meta"]["op"] == "platform_execute" and job["meta"]["run"] == {"steps": ["create", "audit"]}
        [ev] = b.prm.store.events(deal_id(b), kinds=("site_auto",))
        assert ev["actor"] == "system" and ev["detail"]["packet"] == packet["id"]
    finally:
        b.prm.stop()
        b.relay.stop()


def test_the_auto_switch_never_runs_a_preview_that_did_not_pass(clock, tmp_path):
    b = make(clock, tmp_path, prm_sample_site_auto="on")
    try:
        packet = drafted(b, platform=block("blocked", ["寄过这个商品"]))
        assert packet["status"] == PENDING
        assert b.relay.next_job(0, ("prm",)) is None
    finally:
        b.prm.stop()
        b.relay.stop()


def test_the_auto_switch_refused_leaves_the_draft_for_a_person(clock, tmp_path):
    b = Bot(clock, tmp_path, prm_sample_site_min=5, prm_sample_site_auto="on")
    b.prm.register(CREATOR, "小美")
    b.prm._send = None                                                # (not connected to WeChat: nothing is started)
    try:
        packet = drafted(b)
        assert packet["status"] == PENDING
        assert "没能自动建" in " ".join(n["text"] for n in b.prm.store.deal(deal_id(b))["notices"])
    finally:
        b.prm.stop()
        b.relay.stop()
