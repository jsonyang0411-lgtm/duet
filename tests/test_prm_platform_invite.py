"""The platform action dy.creator_invite (src/prm/platforms.py): the 邀约带货 card of 百应
达人广场, sent by douyin-engineering's ``buyin author-invite``.  Its checks, the Mac's contact
settings, the preview and the execution against the fake ``node`` (tests/platform_fakes.py),
whose answers here are the command's own JSON shapes (taken from its offline run against its
fake platform; every id, name and number is made up), and what the bot accepts back."""

import json

import pytest

from src.prm import platforms
from src.prm.platforms import ActionError

from platform_fakes import DY_ITEM, FakeNode, catalog, rule

KEY = "e" * 32
DY_ITEM_2 = "3000000000000000102"
DOUYIN = "dy_test_creator"
PHONE = "13800000000"
WECHAT = "shop_test_wx"
TEXT = "你好，我们是测试店铺，想邀请你带货我们的测试商品。"

INVITE = {"action": "dy.creator_invite", "item_id": DY_ITEM, "creator_douyin_id": DOUYIN, "invite_text": TEXT,
          "rights": ["free_sample", "high_commission"], "note": "kb/合作政策.md：先发邀约卡片"}


def two_items() -> dict:
    cat = catalog()
    cat["platforms"]["dy"]["items"].append({"id": DY_ITEM_2, "title": "测试副品", "price": "29.90", "skus": []})
    return cat


def invite_result(outcome="PREVIEW", *, status=1, blocked="", unlocated=False, executed=False, remaining=None) -> dict:
    """``buyin author-invite``'s JSON (preview or run), as the command prints it."""
    detail = blocked and f"blocked ({blocked}); nothing would be sent for it. " or ""
    if unlocated:
        detail = f"the creator search answered 0 row(s) and none carries exactly the Douyin id {DOUYIN}; nothing would be sent"
    elif outcome == "PREVIEW":
        detail += (f"creator {DOUYIN} would get a 邀约带货 card in 精选联盟 IM: the 邀约信息 (25 characters), 专属权益 专属高佣、免费申样, "
                   "contact phone and WeChat id, 1 product(s) (<digits> 免费申样 (the platform reviews the application)); "
                   "the creator receives it at once and it cannot be taken back")
    else:
        detail = "the platform acknowledged this target with no reported error, and a re-read shows the requested value"
    button = {1: "邀约带货", 2: "继续沟通 · 待回复", 3: "继续沟通 · 达人已拒绝", 99: "邀约带货 (disabled)"}[status]
    gate_type = 4 if remaining is not None else -1
    row = {"targetKind": "aweme_id", "targetId": DOUYIN, "outcome": outcome, "detail": detail,
           "currentValue": f"{button} (status {status}); gate {gate_type}", "desiredValue": "invited", "unlocated": unlocated,
           "blocked": bool(blocked)}
    if outcome == "VERIFIED_SUCCESS":
        row["observedValue"] = "invited (继续沟通 · 待回复 (status 2))"
    elif executed:
        row["observedValue"] = "not shown as invited: 继续沟通 · 待回复 (status 2)"
    out = {"site": "buyin", "command": "author-invite", "executed": executed,
           "overall": "VERIFIED" if outcome == "VERIFIED_SUCCESS" else "UNCERTAIN" if executed else "PREVIEW_ONLY",
           "platformWrites": int(executed), "items": [row], "restore": [], "creator": {"awemeId": DOUYIN, "nickname": "测试达人昵称"},
           "transport": {"kind": "page", "pageSignature": True, "platformRequests": 7}}
    if not unlocated:
        out["gate"] = {"popwindowType": gate_type,
                       "popwindow": "PopRemainTimes" if remaining is not None else "NoPopWindow",
                       "connectionType": 1, "isPassed": True, "needStrongTemplate": False, "remaining": remaining}
        out["statusBefore"] = {"status": status, "button": f"{button} (status {status})"}
        out["products"] = [{"productId": DY_ITEM, "sample": "免费申样 (the platform reviews the application)"}]
    if executed:
        out["notes"] = ["the card went to the creator in 精选联盟 IM and cannot be recalled by this tool"]
    return out


@pytest.fixture
def node(tmp_path, monkeypatch):
    return FakeNode(tmp_path / "mac", monkeypatch)


@pytest.fixture
def cfg(node):
    return platforms.config(node.settings(dy_invite_phone=PHONE, dy_invite_wechat=WECHAT, dy_expect_shop_id="1234567"))


def argvs(node, *needles):
    return [c["argv"] for c in node.calls() if all(n in c["argv"] for n in needles)]


PLAN = {"steps": ["invite"], "status": 1, "popwindow": -1}


def execute(cfg, raw, tmp_path, key=KEY, cat=None, plan=None):
    return platforms.execute(raw, cfg, plan=PLAN if plan is None else plan, key=key, ledger=tmp_path / "ledger",
                             catalog=catalog() if cat is None else cat, context={"packet_id": 5})


# ── the action and its checks ─────────────────────────────────────────────

def test_the_action_is_normalised_rights_in_the_pages_order():
    assert platforms.check_action(INVITE, catalog=catalog()) == {
        "action": "dy.creator_invite", "item_id": DY_ITEM, "creator_douyin_id": DOUYIN, "invite_text": TEXT,
        "rights": ["high_commission", "free_sample"], "note": "kb/合作政策.md：先发邀约卡片"}
    more = platforms.check_action({**INVITE, "extra_item_ids": [DY_ITEM_2], "rights": [], "invite_text": f"  {TEXT}  "},
                                  catalog=two_items())
    assert more["extra_item_ids"] == [DY_ITEM_2] and "rights" not in more and more["invite_text"] == TEXT
    spec = platforms.ACTIONS["dy.creator_invite"]
    assert spec["platform"] == "dy" and spec["link"] is False and platforms.REPLY_FIELDS["dy.creator_invite"] == {}
    assert platforms.PLANS["dy.creator_invite"] == (("invite",),)


@pytest.mark.parametrize("change, code", [
    ({"creator_douyin_id": "-dy_test"}, "bad_creator"),
    ({"creator_douyin_id": "a"}, "bad_creator"),
    ({"creator_douyin_id": "dy test"}, "bad_creator"),
    ({"creator_douyin_id": True}, "bad_creator"),
    ({"invite_text": ""}, "missing_param"),
    ({"invite_text": "x" * 151}, "bad_text"),
    ({"invite_text": "第一行\n第二行"}, "bad_text"),
    ({"invite_text": "领 {链接}"}, "bad_text"),
    ({"invite_text": "-开头"}, "bad_text"),
    ({"extra_item_ids": DY_ITEM_2}, "bad_item"),
    ({"extra_item_ids": [DY_ITEM_2] * 2}, "bad_item"),
    ({"extra_item_ids": [DY_ITEM]}, "bad_item"),
    ({"extra_item_ids": ["3000000000000000103", "3000000000000000104", "3000000000000000105", "3000000000000000106",
                         "3000000000000000107"]}, "bad_item"),
    ({"extra_item_ids": ["3000000000000000199"]}, "not_in_catalog"),
    ({"extra_item_ids": ["0123"]}, "bad_item"),
    ({"rights": ["free_sample", "free_sample"]}, "bad_rights"),
    ({"rights": ["cash"]}, "bad_rights"),
    ({"rights": "free_sample"}, "bad_rights"),
    ({"creator_platform_id": "5100000000000002"}, "extra_param"),
    ({"item_id": "3000000000000000999"}, "not_in_catalog"),
])
def test_what_does_not_fit_is_refused(change, code):
    with pytest.raises(ActionError) as e:
        platforms.check_action({**INVITE, **change}, catalog=two_items())
    assert e.value.code == code


def test_the_schema_and_the_console_know_the_new_parameters():
    props = platforms.ACTION_SCHEMA["properties"]
    assert props["rights"]["items"]["enum"] == ["high_commission", "free_sample", "video_material", "video_boost"]
    assert props["creator_douyin_id"] == {"type": "string"} and props["invite_text"] == {"type": "string"}
    shown = platforms.describe(platforms.check_action({**INVITE, "extra_item_ids": [DY_ITEM_2]}, catalog=two_items()),
                               {"title": "测试主品"})
    assert {"label": "专属权益", "value": "专属高佣、免费申样"} in shown
    assert {"label": "其他推荐商品", "value": DY_ITEM_2} in shown
    assert {"label": "达人抖音号", "value": DOUYIN} in shown and shown[0] == {"label": "商品", "value": "测试主品"}
    assert platforms.reply_problems("已经在抖音给你发了邀约，记得看私信～", INVITE) == []
    assert platforms.reply_problems("链接 {链接}", INVITE)


def test_the_contact_is_the_macs_own_and_must_fit_the_drawer(node):
    good = platforms.config(node.settings(dy_invite_phone=PHONE, dy_invite_wechat=WECHAT))
    assert (good["dy_invite_phone"], good["dy_invite_wechat"]) == (PHONE, WECHAT)
    bad = platforms.config(node.settings(dy_invite_phone="10800000000", dy_invite_wechat="1abcdef"))
    assert (bad["dy_invite_phone"], bad["dy_invite_wechat"]) == ("", "")
    assert platforms.config(node.settings())["dy_invite_phone"] == ""


# ── the preview ───────────────────────────────────────────────────────────

def test_the_preview_reads_and_never_executes(node, cfg):
    node.rules([rule(["author-invite"], invite_result(), execute=False)])
    block = platforms.preview({**INVITE, "extra_item_ids": [DY_ITEM_2]}, cfg, catalog=two_items())
    assert block["verdict"] == "ready", block
    assert block["run"] == {"steps": ["invite"], "status": 1, "popwindow": -1}, "the approval holds the creator to what this preview saw"
    (argv,) = argvs(node, "author-invite")
    assert argv[0] == "bin/dy.mjs" and "--execute" not in argv
    assert argv[1:] == ["buyin", "author-invite", f"--aweme-id={DOUYIN}", f"--product={DY_ITEM},{DY_ITEM_2}", f"--content={TEXT}",
                        "--rights=high-commission,free-sample", f"--phone={PHONE}", f"--wechat={WECHAT}", "--names",
                        "--expect-shop-id=1234567"]
    assert "--phone=138****0000" in block["steps"][0]["command"] and PHONE not in block["steps"][0]["command"]
    facts = {f["label"]: f["value"] for f in block["steps"][0]["facts"]}
    assert facts["达人昵称（请核对是不是聊天里这位）"] == "测试达人昵称"
    assert facts["达人现在的按钮"] == "邀约带货 (status 1)"
    assert facts["邀约门槛"] == "NoPopWindow（popwindow_type -1）"
    assert facts[f"商品 {DY_ITEM}"].startswith("免费申样")
    assert "读回" not in facts
    assert "收不回" in " ".join(f["value"] for f in block["facts"])


def test_only_the_contact_that_is_set_goes(node):
    cfg = platforms.config(node.settings(dy_invite_wechat=WECHAT))
    node.rules([rule(["author-invite"], invite_result(), execute=False)])
    platforms.preview({**INVITE, "rights": []}, cfg, catalog=catalog())
    (argv,) = argvs(node, "author-invite")
    assert f"--wechat={WECHAT}" in argv and not any(a.startswith(("--phone", "--rights")) for a in argv)


def test_no_contact_on_the_mac_means_no_preview_and_no_command(node):
    cfg = platforms.config(node.settings())
    block = platforms.preview(INVITE, cfg, catalog=catalog())
    assert block["verdict"] == "unavailable"
    assert "dy_invite_phone" in " ".join(block["problems"])
    assert node.calls() == []
    clean = platforms.check_block(json.loads(json.dumps(block)))
    assert clean["verdict"] == "unavailable" and clean["run"] == {}


@pytest.mark.parametrize("answer, verdict", [
    (dict(stdout=invite_result(blocked="the shop has no authority to contact creators")), "blocked"),
    (dict(stdout=invite_result(unlocated=True)), "blocked"),
    (dict(stderr="[NOT_LOGGED_IN] the 百应 (buyin) tab is on a login page; nothing was requested\n", exit=77), "login"),
    (dict(stderr="[RISK_CONTROL] verification wall\n", exit=76), "wall"),
    (dict(stderr="[IDENTITY_MISMATCH] this login is not the one expected\n", exit=79), "wrong_account"),
    (dict(stderr="[SCHEMA_MISMATCH] the answer changed shape\n", exit=1), "error"),
])
def test_what_stops_a_preview_is_its_verdict(node, cfg, answer, verdict):
    node.rules([rule(["author-invite"], execute=False, **answer)])
    block = platforms.preview(INVITE, cfg, catalog=catalog())
    assert block["verdict"] == verdict
    assert block["problems"]


def test_a_creator_already_invited_or_in_a_conversation_is_blocked(node, cfg):
    node.rules([rule(["author-invite"], invite_result(status=3, remaining=17), execute=False)])
    block = platforms.preview(INVITE, cfg, catalog=catalog())
    assert block["verdict"] == "blocked" and block["run"] == {"steps": ["invite"]}
    assert "再发一张卡片收不回" in " ".join(block["problems"])
    facts = {f["label"]: f["value"] for f in block["steps"][0]["facts"]}
    assert facts["邀约门槛"] == "PopRemainTimes（popwindow_type 4）；本周还剩 17 次"
    with pytest.raises(ActionError):
        platforms.check_plan(platforms.check_action(INVITE), {"steps": ["invite"]})


def test_a_preview_without_the_creators_status_cannot_be_approved(node, cfg):
    answer = invite_result()
    del answer["statusBefore"]
    node.rules([rule(["author-invite"], answer, execute=False)])
    block = platforms.preview(INVITE, cfg, catalog=catalog())
    assert block["verdict"] == "error" and "没有达人现在的状态" in " ".join(block["problems"])


def test_the_bot_accepts_the_macs_preview_and_holds_it_to_the_form(node, cfg):
    node.rules([rule(["author-invite"], invite_result(), execute=False)])
    block = platforms.preview(INVITE, cfg, catalog=catalog())
    clean = platforms.check_block(json.loads(json.dumps(block)))
    assert clean["verdict"] == "ready" and clean["run"] == {"steps": ["invite"], "status": 1, "popwindow": -1}
    for bad in ({"steps": ["invite"]}, {"steps": ["invite"], "status": "1", "popwindow": -1}, {"steps": ["invite"], "status": True, "popwindow": -1},
                {"steps": ["invite"], "status": 1, "popwindow": -2}):
        with pytest.raises(ActionError):
            platforms.check_block({**block, "run": bad})
    assert clean["action"]["creator_douyin_id"] == DOUYIN
    with pytest.raises(ActionError):
        platforms.check_block({**block, "run": {"steps": ["invite", "invite"]}})
    with pytest.raises(ActionError):
        platforms.check_block({**block, "action": {**INVITE, "invite_text": "x" * 200}})


# ── the execution ─────────────────────────────────────────────────────────

def test_a_verified_invitation_lets_the_reply_go_and_runs_once(node, cfg, tmp_path):
    node.rules([rule(["author-invite"], invite_result("VERIFIED_SUCCESS", executed=True), execute=True)])
    out = execute(cfg, INVITE, tmp_path)
    assert out["outcome"] == "VERIFIED_SUCCESS", out
    (argv,) = argvs(node, "author-invite", "--execute")
    assert argv[-1] == "--execute" and f"--aweme-id={DOUYIN}" in argv and f"--phone={PHONE}" in argv
    assert argv[-4:-1] == ["--expect-status=1", "--expect-popwindow=-1", "--expect-shop-id=1234567"], "the preview's baseline goes with the write"
    assert PHONE not in out["steps"][0]["command"]
    assert [(s["name"], s["execute"]) for s in out["steps"]] == [("invite", True)]
    again = execute(cfg, INVITE, tmp_path)
    assert again["outcome"] == "ALREADY_RUN" and len(argvs(node, "--execute")) == 1


@pytest.mark.parametrize("answer, outcome", [
    (dict(stdout=invite_result("ACCEPTED_UNVERIFIED", status=3, executed=True), exit=1), "ACCEPTED_UNVERIFIED"),
    (dict(stdout=invite_result("STATE_CONFLICT", executed=False), exit=1), "STATE_CONFLICT"),
    (dict(stdout=invite_result("INDETERMINATE", executed=True), exit=1), "INDETERMINATE"),
    (dict(stderr="[NOT_LOGGED_IN] the 百应 tab is on a login page; nothing was requested\n", exit=77), "FAILED"),
    (dict(stderr="[INTERRUPTED] stopped; a write request of this run was in flight\n", exit=130), "INDETERMINATE"),
])
def test_an_invitation_that_is_not_verified_says_so(node, cfg, tmp_path, answer, outcome):
    node.rules([rule(["author-invite"], execute=True, **answer)])
    out = execute(cfg, INVITE, tmp_path)
    assert out["outcome"] == outcome
    assert out["problems"] or any(s["problems"] for s in out["steps"])


def test_without_a_contact_on_the_mac_nothing_is_run(node, tmp_path):
    cfg = platforms.config(node.settings())
    out = execute(cfg, INVITE, tmp_path)
    assert out["outcome"] == "NOT_RUN" and node.calls() == []
    assert "dy_invite_phone" in " ".join(out["problems"])


def test_a_contact_in_the_wrong_format_is_said_not_silently_dropped(node):
    cfg = platforms.config(node.settings(dy_invite_phone="1234", dy_invite_wechat="1abc"))
    assert cfg["dy_invite_invalid"] == ["dy_invite_phone", "dy_invite_wechat"]
    block = platforms.preview(INVITE, cfg, catalog=catalog())
    assert block["verdict"] == "unavailable" and "填了但格式不对：dy_invite_phone、dy_invite_wechat" in " ".join(block["problems"])
    # one of the two in the wrong format is not dropped silently: the preview and the execution stop until it is fixed
    half = platforms.config(node.settings(dy_invite_phone="1234", dy_invite_wechat=WECHAT))
    assert half["dy_invite_wechat"] == WECHAT and half["dy_invite_invalid"] == ["dy_invite_phone"]
    stopped = platforms.preview(INVITE, half, catalog=catalog())
    assert stopped["verdict"] == "unavailable" and "dy_invite_phone 填了但格式不对" in " ".join(stopped["problems"])
    out = platforms.execute(INVITE, half, plan=PLAN, key="9" * 32, ledger=node.root / "ledger", catalog=catalog())
    assert out["outcome"] == "NOT_RUN" and node.calls() == []


def test_the_text_is_counted_as_the_page_counts_it():
    emoji = "好" * 149 + "😀"                  # 150 code points, 151 UTF-16 units
    with pytest.raises(ActionError) as e:
        platforms.check_action({**INVITE, "invite_text": emoji}, catalog=catalog())
    assert e.value.code == "bad_text"
    assert platforms.check_action({**INVITE, "invite_text": "好" * 148 + "😀"}, catalog=catalog())["invite_text"].endswith("😀")


def test_only_the_invite_plan_with_its_baseline_runs(node, cfg, tmp_path):
    for n, plan in enumerate(({"steps": ["designated"]}, {"steps": ["invite"]}, {"steps": ["invite"], "status": 1})):
        out = platforms.execute(INVITE, cfg, plan=plan, key=f"{n}" * 32, ledger=tmp_path / "ledger", catalog=catalog())
        assert out["outcome"] == "NOT_RUN", plan
    assert node.calls() == []
