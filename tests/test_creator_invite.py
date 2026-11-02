"""tools/creator_invite/invite.py — the two-stage batch of 达人广场「邀约带货」, against a fake ``node``.

The fake answers the way douyin-engineering (``buyin author-invite-status``, ``buyin author-invite``) and
jushuitan-engineering (``find``, ``goods-detail``) do — their JSON key names are exact, every value is made up —
and logs every call. Nothing here reaches a platform, a browser or ~/.claude-relay: the ledger and the plans
live in tmp_path. The pacing wrappers (paced-dy.mjs / paced-jst.mjs) are run with real node against tiny fake
CLI folders when node is installed.
"""

import csv
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "creator_invite" / "invite.py"
BJT = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 28, 15, 0, tzinfo=BJT)          # a Wednesday


def load_tool():
    spec = importlib.util.spec_from_file_location("creator_invite_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tool = load_tool()

P_BERET, P_PILLBOX, P_EQUESTRIAN = "3000000000000000101", "3000000000000000102", "3000000000000000103"
HOVER = "本周向 LV2-LV3 的达人发起邀约带货次数上限为 {used}/2500，各等级达人建联上限可"

FAKE_NODE = textwrap.dedent("""\
    import json, os, sys
    args = sys.argv[2:]                       # sys.argv[1] is the pacing wrapper
    scenario = json.load(open(os.environ["FAKE_SCENARIO"], encoding="utf-8"))
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"wrapper": os.path.basename(sys.argv[1]), "args": args, "gap": os.environ.get("CREATOR_INVITE_GAP_MS"),
                            "root": os.environ.get("CREATOR_INVITE_CLI_ROOT"), "dy_transport": os.environ.get("DY_TRANSPORT"), "stamp": os.environ.get("CREATOR_INVITE_STAMP")}, ensure_ascii=False) + "\\n")
    opt = {}
    for a in args:
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            opt[k] = v
        elif a.startswith("--"):
            opt[a[2:]] = True
    def out(data, code=0):
        sys.stdout.write(json.dumps(data, ensure_ascii=False))
        sys.exit(code)
    exits = scenario.get("exits", {})
    if args[:1] == ["goods-detail"]:
        code = exits.get("stock")
        if code: sys.stderr.write("[RISK] 被限流\\n"); sys.exit(code)
        style = opt["style-code"]
        skus = scenario["stock"].get(style)
        out({"command": "goods-detail", "styleCode": style, "found": skus is not None,
             "skus": [{"itemCode": style + "-" + str(i), "availableStock": n} for i, n in enumerate(skus or [])]})
    if args[:1] == ["lookup"]:
        code = exits.get("lookup")
        if code: sys.stderr.write("[NOT_LOGGED_IN] 登录失效\\n"); sys.exit(code)
        daren = scenario.get("lookup", {}).get(opt["no"])
        out({"platform": "DY", "no": opt["no"], "found": daren is not None, **({"daren": daren} if daren is not None else {})})
    if args[:2] == ["buyin", "authors-square"]:
        code = exits.get("square")
        if code: sys.stderr.write("[WALL] 验证码\\n"); sys.exit(code)
        out({"site": "buyin", "command": "authors-square", "rows": scenario.get("square", [])})
    if args[:1] == ["find"]:
        code = exits.get("find")
        if code: sys.stderr.write("[NOT_LOGGED_IN] 登录失效\\n"); sys.exit(code)
        lo, hi = int(opt.get("fans-min", 0)), int(opt.get("fans-max", 10 ** 12))
        rows = [r for r in scenario["creators"] if lo <= r["fansNum"] <= hi]
        start = (int(opt.get("page", 1)) - 1) * 20
        rows = rows[start:start + 20 * int(opt.get("pages", 1))]
        out({"command": "find", "platform": "DY", "total": len(rows), "rows": rows, "stopped": scenario.get("find_stopped")})
    if args[:2] == ["buyin", "author-invite-status"]:
        code = exits.get("status")
        if code: sys.stderr.write(scenario.get("said", {}).get("status", "[WALL] 验证码") + "\\n"); sys.exit(code)
        creators = []
        for a in opt["aweme-id"].split(","):
            if a in scenario.get("missing", []):
                creators.append({"awemeId": a, "found": False, "why": "the creator search answered 0 row(s)"})
                continue
            s = scenario.get("status", {}).get(a, 1)
            creators.append({"awemeId": a, "found": True, "status": s, "nickname": "达人" + a[-3:], "button": "邀约带货 (status 1)" if s == 1 else "继续沟通 · 待回复 (status 2)",
                             "invitable": s == 1, "hoverText": scenario["hover"], "weeklyQuota": scenario["quota"]})
        out({"site": "buyin", "command": "author-invite-status", "creators": creators,
             "summary": {"weeklyQuotas": [scenario["quota"]]}})
    if args[:2] == ["buyin", "author-invite"]:
        a = opt["aweme-id"]
        phase = "execute" if opt.get("execute") else "preview"
        code = exits.get(a + ":" + phase)
        said = scenario.get("said", {}).get(a + ":" + phase)
        if code and (phase == "preview" or said): sys.stderr.write((said or "[STOP]") + "\\n"); sys.exit(code)
        status = scenario.get("preview_status", {}).get(a, 1)
        gate = scenario.get("preview_gate", {}).get(a, -1)
        quota = scenario.get("preview_quota", {}).get(a, scenario["quota"])
        if phase == "preview":
            blocked = a in scenario.get("blocked", [])
            out({"executed": False, "platformWrites": 0, "items": [{"targetId": a, "outcome": "PREVIEW", "blocked": blocked,
                 "detail": "blocked (the square shows this creator's button disabled)" if blocked else "creator would get a card",
                 "currentValue": "邀约带货 (status %d); gate %d" % (status, gate)}],
                 "gate": {"popwindowType": gate}, "statusBefore": {"status": status, "weeklyQuota": quota},
                 "products": [{"productId": x, "sample": scenario.get("sample", {}).get(x, "买样")} for x in opt["product"].split(",")],
                 **({"warnings": scenario["warnings"]} if scenario.get("warnings") else {})})
        outcome = scenario.get("outcome", {}).get(a, "VERIFIED_SUCCESS")
        writes = 0 if outcome in ("STATE_CONFLICT", "FAILED") else 1
        detail = scenario.get("detail", {}).get(a)
        out({"executed": True, "platformWrites": writes, "items": [{"targetId": a, "outcome": outcome, **({"detail": detail} if detail else {})}]},
            code or (0 if outcome == "VERIFIED_SUCCESS" else 1))
    sys.stderr.write("[FAKE] no answer for " + " ".join(args) + "\\n")
    sys.exit(99)
""")


def row(aweme, fans, *, nick=None, level=3, hat="12.0%", sex="女性 90.00%", live_min=1000000, channel="直播", avg=300.0,
        hidden=False, goods=None, price=None, crowd=None):
    sales = ({"available": False, "min": None, "max": None, "label": "无数据"} if hidden
             else {"available": True, "min": live_min, "max": None, "label": f"{live_min}起"})
    return {"platformNo": aweme, "nickName": nick or f"测试达人{aweme[-3:]}", "fansNum": fans, "level": level,
            "sales30d": sales, "sales30dLive": {**sales, "label": "x"}, "channelBySales": channel, "mainChannel": channel,
            "avgPrice": avg, "category": "服饰内衣",
            "recentGoods": [{"name": n, "ratio": r} for n, r in goods] if goods else [{"name": "大衣", "ratio": "50%"}, {"name": "帽子", "ratio": hat}],
            "fans": {"sex": sex, **({"price": price} if price else {}), **({"crowd": crowd} if crowd else {})}}


def creators():
    return [
        row("dy_small_a", 30000, avg=190.0),
        row("dy_small_b", 60000, avg=380.0, sex="女性 75.00%"),
        row("dy_suspect", 28000, hat="99.80%"),                           # 疑似同行自播号
        row("dy_gap", 170000),                                            # 15万～20万: a small one (the user, 2026-10-29)
        row("dy_tiny", 3000),                                             # 粉丝不到 5000
        row("bad id!", 40000),                                            # not a Douyin id the CLI takes
        row("dy_invited", 45000),                                         # in the ledger already
        row("dy_talking", 50000),                                         # status 2 on the platform
        row("dy_ghost", 52000),                                           # the search does not find it
        row("dy_mid_a", 250000, avg=230.0, live_min=200000),
        row("dy_big_a", 760000, level=5, live_min=10000000, avg=229.0),   # 专场型
        row("dy_big_b", 250000, level=4, live_min=5000000, avg=288.0),    # 专场型 by the rule, though under 300k fans
        row("dy_video_big", 13000, level=4, live_min=20000, channel="视频", hat="0.5%"),  # video seller: by fans, a small one; hardly any hats
        row("dy_male_fans", 70000, sex="男性 61.00%"),                    # female 39%: not on the list (we sell women's hats)
    ]


def scenario(**over):
    s = {"creators": creators(), "stock": {"S-BERET": [20, 8], "S-PILL-A": [13], "S-PILL-B": [12], "S-EQ": [11]},
         "status": {"dy_talking": 2}, "missing": ["dy_ghost"], "hover": HOVER.format(used=7),
         "quota": {"levels": "LV2-LV3", "used": 7, "limit": 2500}, "exits": {}}
    s.update(over)
    return s


@pytest.fixture
def env(tmp_path, monkeypatch):
    node = tmp_path / "node"
    node.write_text(f"#!{sys.executable}\n" + FAKE_NODE, encoding="utf-8")
    node.chmod(0o755)
    for name, cli in (("dy", "bin/dy.mjs"), ("jst", "bin/jst.mjs")):
        (tmp_path / f"{name}-cli" / "bin").mkdir(parents=True)
        (tmp_path / f"{name}-cli" / cli).write_text("// fake\n", encoding="utf-8")
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_SCENARIO", str(tmp_path / "scenario.json"))
    monkeypatch.setenv("DY_TRANSPORT", "http")                         # the tool must take it away
    config = {
        "node": str(node), "dy_dir": str(tmp_path / "dy-cli"), "jst_dir": str(tmp_path / "jst-cli"),
        "dy_expect_shop_id": "9100000000000001", "jst_expect_uid": "900001", "invite_wechat": "shop_test_wx", "brand": "测试品牌",
        "products": [
            {"id": P_BERET, "name": "测试贝雷帽", "price": 339, "styles": ["S-BERET"]},
            {"id": P_PILLBOX, "name": "测试药盒帽", "price": 228, "styles": ["S-PILL-A", "S-PILL-B"]},
            {"id": P_EQUESTRIAN, "name": "测试马术帽", "price": 229, "styles": ["S-EQ"]},
        ],
        "home": str(tmp_path / "home"),
    }

    class Env:
        def __init__(self):
            self.tmp, self.log, self.config = tmp_path, log, config
            self.scenario(scenario())

        def scenario(self, s):
            (tmp_path / "scenario.json").write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")

        def write_config(self, **over):
            cfg = {**config, **over}
            path = tmp_path / "config.json"
            path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            return str(path)

        def calls(self):
            return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

        def main(self, argv, **kw):
            out = []
            sleeps = []

            class W:
                def write(self, s):
                    out.append(s)
            code = tool.main(argv, sleep=lambda s: sleeps.append(s), uniform=kw.get("uniform", lambda a, b: (a + b) / 2),
                             clock=kw.get("clock", lambda: NOW), stdout=W(), runner=kw.get("runner", subprocess.run))
            text = "".join(out)
            return code, (json.loads(text) if text.strip().startswith("{") else text), sleeps

    return Env()


def plan_now(env, *extra, count=30, ledger="ledger", config=None):
    cfg = env.write_config(**(config or {}))
    out_dir = env.tmp / "plans"
    code, out, _ = env.main(["plan", "--config", cfg, "--count", str(count), "--out-dir", str(out_dir), "--ledger-dir", str(env.tmp / ledger), *extra])
    return code, out


def ledger_lines(env, ledger="ledger"):
    path = env.tmp / ledger / "invites.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# ── plan ────────────────────────────────────────────────────────────────────

def test_plan_is_read_only_and_builds_the_list_by_the_users_rules(env):
    led = tool.Ledger(env.tmp / "ledger")
    led.append({"event": "attempt", "attempt": "a1", "aweme_id": "dy_invited", "plan_id": "Pold"})
    led.append({"event": "result", "attempt": "a1", "aweme_id": "dy_invited", "sent": True, "outcome": "VERIFIED_SUCCESS"})
    code, out = plan_now(env)
    assert code == 0, out
    calls = env.calls()
    assert not any("--execute" in c["args"] for c in calls), "plan never executes anything"
    assert all(c["dy_transport"] is None for c in calls), "DY_TRANSPORT is taken away: the page transport, which the wrapper paces"
    assert all(c["gap"] == "3000" for c in calls)
    assert {c["stamp"] for c in calls} == {str(env.tmp / "ledger" / "last-request")}, "one request clock, in the ledger folder, for every run that uses it"
    assert not any(c["args"][0] == "goods-detail" for c in calls), "stock is not looked at by default (the boss, 2026-10-29)"
    assert all(c["wrapper"] == "paced-jst.mjs" for c in calls if c["args"][0] == "find")
    finds = [c["args"] for c in calls if c["args"][0] == "find"]
    assert finds[0][:6] == ["find", "--platform=DY", "--goods-category=1000003461", "--price-min=150", "--price-max=500", "--not-added"]
    assert "--fans-min=5000" in finds[0] and "--fans-max=200000" in finds[0] and "--pages=2" in finds[0] and "--expect-uid=900001" in finds[0]
    assert "--level-min=2" in finds[0] and "--sales-min=10000" in finds[0], "the platform drops the likely sample-takers first"
    assert "--fans-min=200000" in finds[1] and "--level-min=2" in finds[1]
    assert not any(a.startswith("--sales-min") for a in finds[1]), "a big creator who hides her sales is not dropped by the search"
    status = [c["args"] for c in calls if c["args"][:2] == ["buyin", "author-invite-status"]]
    assert len(status) == 1 and "--expect-shop-id=9100000000000001" in status[0] and "--no-open" in status[0]
    plan = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
    by = {c["aweme_id"]: c for c in plan["creators"]}
    assert set(by) == {"dy_small_a", "dy_small_b", "dy_gap", "dy_mid_a", "dy_big_a", "dy_big_b", "dy_video_big"}
    assert [c["category"] for c in plan["creators"]] == ["small"] * 4 + ["mid", "big", "big"], "grouped by category; inside one, no hat seller first, then the fewer flags"
    assert [c["aweme_id"] for c in plan["creators"]][:4] == ["dy_small_a", "dy_small_b", "dy_gap", "dy_video_big"]
    assert by["dy_big_b"]["category"] == "big" and by["dy_video_big"]["category"] == "small" and by["dy_gap"]["category"] == "small"
    assert by["dy_small_b"]["flags"] == []
    assert by["dy_video_big"]["flags"] == ["帽子占比只有 0.5%"], "flagged for a person to look at, not taken out"
    # the products: the one closest to the creator's 件单价 leads, all the others follow; stock is not asked
    assert by["dy_small_a"]["products"] == {"main": P_PILLBOX, "extras": [P_BERET, P_EQUESTRIAN]}
    assert by["dy_small_b"]["products"] == {"main": P_BERET, "extras": [P_PILLBOX, P_EQUESTRIAN]}
    assert [(p["stock"], p["in_stock"]) for p in plan["products"]] == [(None, True)] * 3
    assert by["dy_big_a"]["content"].startswith("您好！我们是设计师女帽品牌测试品牌，主打英伦复古")
    assert "{品牌}" not in json.dumps(plan, ensure_ascii=False)
    reasons = {c["aweme_id"]: c["reason"] for c in plan["excluded"]}
    assert reasons["dy_suspect"].startswith("疑似同行自播号（帽子占比 99.8%")
    assert reasons["dy_invited"].startswith("台账里已经邀约过")
    assert reasons["dy_talking"].startswith("平台状态不是「邀约带货」")
    assert reasons["dy_ghost"].startswith("百应搜不到这个抖音号")
    assert reasons["dy_male_fans"] == "女性粉丝 39.0%（低于 60.0%）", "under 60% women: off the list, with the reason"
    assert "dy_tiny" not in reasons, "the default searches do not even ask for it"
    rules = tool.DEFAULT_RULES
    assert tool.classify(row("dy_gap", 170000), rules) == ("small", None), "15万～20万 is a small one (the user, 2026-10-29)"
    assert tool.classify(row("x", 199999, live_min=0), rules) == ("small", None)
    assert tool.classify(row("x", 200000, live_min=0), rules) == ("mid", None)
    assert tool.classify(row("dy_tiny", 3000), rules) == (None, "粉丝不到 5000")
    assert tool.classify(row("x", 300000, level=3, live_min=10000000), rules) == ("mid", None), "LV3: not 专场型 however big the sales"
    assert tool.classify(row("x", 9000, level=4, live_min=5000000), rules) == ("big", None), "专场型 goes by the live sales and the level, not the fans"
    assert reasons["bad id!"] == "抖音号格式命令行不收"
    assert plan["weekly_quotas"] == [{"levels": "LV2-LV3", "used": 7, "limit": 2500}]
    assert plan["ledger_sent_this_week"] == 1
    md = Path(out["files"]["md"]).read_text(encoding="utf-8")
    assert f"run --config <配置> --plan {out['files']['json']} --confirm {plan['plan_id']}" in md and "还没有发任何邀约" in md
    assert "粉丝 5000～200000、带货等级 LV2～LV7、近 30 天销售额 ≥ 10000 元，第 1 页起读 2 页；粉丝 200000 以上、带货等级 LV2～LV7，第 1 页起读 1 页" in md
    assert "不看库存，能挂上卡的都配" in md and "| 没查 | 配 |" in md
    assert "## 疑似同行自播号" in md and "dy_suspect" in md.split("## 疑似同行自播号")[1].split("## 其他没进清单的")[0]
    assert "LV2-LV3 已用 7 / 上限 2500" in md
    assert "shop_test_wx" not in md and "shop_test_wx" not in Path(out["files"]["json"]).read_text(encoding="utf-8"), "the contact is masked in the plan"
    for f in out["files"].values():
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert out["by_category"] == {"小达人": 4, "中大博主": 1, "大主播/专场型": 2}


def test_plan_counts_stops_and_stock(env):
    stocked = {"min_stock": 10}                           # stock is looked at only when the config asks for it
    env.scenario(scenario(stock={"S-BERET": [5], "S-PILL-A": [13], "S-PILL-B": [12], "S-EQ": [11]}))
    code, out = plan_now(env, count=2, config=stocked)
    assert code == 0, out
    assert [c["wrapper"] for c in env.calls() if c["args"][0] == "goods-detail"] == ["paced-jst.mjs"] * 4, "one read a style code"
    plan = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
    assert len(plan["creators"]) == 2
    assert [p["stock"] for p in plan["products"]] == [5, 25, 11]
    assert [p["in_stock"] for p in plan["products"]] == [False, True, True], "below 10: not offered"
    assert all(P_BERET not in [c["products"]["main"], *c["products"]["extras"]] for c in plan["creators"])
    assert all(len(c["products"]["extras"]) == 1 for c in plan["creators"]), "two products in stock: a main one and one more"
    assert "可用库存低于 10 的不配" in Path(out["files"]["md"]).read_text(encoding="utf-8")
    for where, code_expected in (("find", 77), ("status", 76), ("stock", 79)):
        env.scenario(scenario(exits={where: code_expected}))
        code, out = plan_now(env, config=stocked)
        assert code == code_expected, where
        assert "立刻停下" in out or out == "", where
    env.scenario(scenario(stock={"S-BERET": [1], "S-PILL-A": [1], "S-PILL-B": [1], "S-EQ": [1]}))
    code, _ = plan_now(env, config=stocked)
    assert code == 1, "nothing in stock: no list"
    code, out = plan_now(env)
    assert code == 0, "the default does not look at stock: the same products are all offered"
    env.scenario(scenario(find_stopped="第 2 页被限流，已停下：[10001]请求太频繁"))
    env.log.unlink()
    code, _ = plan_now(env)
    assert code == 76, "a later page rate-limited (jst find says so with exit 0): a wall all the same"
    assert [c["args"][0] for c in env.calls()].count("find") == 1 and not any(c["args"][:1] == ["buyin"] for c in env.calls())


def test_plan_drops_creators_who_sell_their_own_brand_and_likely_sample_takers(env):
    env.scenario(scenario(missing=[], status={}, creators=[
        row("dy_ok", 80000),
        row("dy_shop", 60000, nick="小美女装（官方）"),                       # a shop's own account by its name
        row("dy_mx_a", 40000, nick="小鹿（日常穿搭）"), row("dy_mx_b", 30000, nick="小鹿·通勤"),  # one shop's accounts
        row("dy_mx_c", 40000, nick="Mia(轻奢极简)"), row("dy_mx_d", 30000, nick="MIA-精选", sex="女性 40.00%"),  # the same, any case
        row("dy_lv1", 40000, level=1),                                    # sells little: 带货等级 LV1
        row("dy_low", 40000, live_min=5000),                              # under 10 000 yuan in 30 days
        row("dy_hidden2", 40000, level=2, hidden=True),                   # sales not shown and only LV2
        row("dy_hidden3", 40000, level=3, hidden=True),                   # not shown, but LV3: kept
        row("dy_fanmany", 300000, live_min=50000),                        # 300k fans, from 50k yuan: 0.17 a fan
        row("dy_fanok", 300000, live_min=100000),                         # 0.33 a fan: kept
        row("dy_kept", 50000, level=1),                                   # LV1, but a person said keep her
        row("dy_dropped", 50000),                                         # a person said never
        row("dy_ownhat", 50000, hat="57.0%"),                             # flagged: may make hats herself
        row("dy_onecat", 50000, goods=[("连衣裙", "99%"), ("帽子", "1.0%")]),   # flagged: one kind of goods only
    ]))
    code, out = plan_now(env, config={"rules": {"keep_ids": ["dy_kept"], "drop_ids": ["dy_dropped"]}})
    assert code == 0, out
    plan = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
    by = {c["aweme_id"]: c for c in plan["creators"]}
    assert set(by) == {"dy_ok", "dy_hidden3", "dy_fanok", "dy_kept", "dy_ownhat", "dy_onecat"}
    gone = {c["aweme_id"]: c for c in plan["excluded"]}
    assert {k for k, c in gone.items() if c.get("group") == "own_brand"} == {"dy_shop", "dy_mx_a", "dy_mx_b", "dy_mx_c"}
    assert gone["dy_mx_d"]["reason"].startswith("女性粉丝 40.0%"), "out for its fans, but still counted for the shop's name"
    assert gone["dy_shop"]["reason"].startswith("名字像店铺 / 品牌号（「女装」）")
    assert gone["dy_mx_a"]["reason"].startswith("店铺矩阵号（这次找到 2 个「小鹿」开头的号）")
    assert {k for k, c in gone.items() if c.get("group") == "sample_risk"} == {"dy_lv1", "dy_low", "dy_hidden2", "dy_fanmany"}
    assert gone["dy_lv1"]["reason"].startswith("带货等级 LV1（低于 LV2）")
    assert gone["dy_low"]["reason"].startswith("近 30 天销售额只有 5000起（低于 10000 元）")
    assert gone["dy_hidden2"]["reason"].startswith("近 30 天销售额不公开、带货等级只有 LV2")
    assert "粉多卖少" in gone["dy_fanmany"]["reason"]
    assert gone["dy_dropped"]["reason"] == "手动排除（配置 rules.drop_ids）"
    assert by["dy_ownhat"]["flags"] == ["帽子占比 57.0%：可能自己做帽子，发之前看一眼她的橱窗"]
    assert by["dy_onecat"]["flags"] == ["近 30 天 99% 是连衣裙：可能只带一个牌子，看一眼"]
    assert [c["aweme_id"] for c in plan["creators"] if c["category"] == "small"][-2:] == ["dy_ownhat", "dy_onecat"], "flagged ones go last"
    md = Path(out["files"]["md"]).read_text(encoding="utf-8")
    own = md.split("## 专门带自己牌子的")[1].split("## 可能骗样品的")[0]
    risky = md.split("## 可能骗样品的")[1].split("## 其他没进清单的")[0]
    assert "dy_shop" in own and "dy_mx_b" in own and "dy_lv1" not in own
    assert "dy_lv1" in risky and "dy_fanmany" in risky and "dy_shop" not in risky
    texts = json.dumps(plan["templates"], ensure_ascii=False)
    assert "视频素材" not in texts and "25%" not in texts and "专属高佣" not in texts, "no commission in the texts (2026-10-29)"
    assert all("合作细节加微信详聊" in plan["templates"][k] for k in ("small", "mid", "big")), "the card is there to bring her to WeChat"
    assert plan["settings"]["rights"] == ["free-sample"] and plan["settings"]["commission_rate"] == 0
    assert "- 卡片：不开专属佣金（佣金到微信里谈），权益 免费申样，联系方式 微信" in md
    rules = tool.DEFAULT_RULES
    assert tool.name_stem("小鹿（时尚穿搭）") == tool.name_stem("小鹿•精选") == "小鹿" and tool.name_stem("木子-琳琳") == "木子"
    assert tool.name_stem("🌟港风男装") == "港风男装" and tool.name_stem("Amy小米") == "amy小米"
    assert tool.name_stem("Mia(轻奢极简每日上新)") == tool.name_stem("MIA-轻奢精选女装") == "mia"
    assert tool.sample_risk_reason(row("x", 9000, level=2, live_min=10000), tool.creator_view(row("x", 9000, level=2, live_min=10000)), rules) is None


SQUARE = {"args": ["buyin", "authors-square", "--main-cate=服饰内衣", "--fans-min=5000", "--pages=2"]}


def test_plan_also_finds_creators_on_douyin_and_looks_each_new_one_up_in_jushuitan(env):
    sq_new = {**row("dy_sq_new", 40000, avg=228.0), "added": False}
    env.scenario(scenario(
        square=[{"awemeId": "dy_sq_new", "nickname": "广场新人", "fansNum": 40000},
                {"awemeId": "dy_small_a", "nickname": "两边都有", "fansNum": 30000},          # 聚水潭 found her too
                {"awemeId": "dy_sq_missing", "nickname": "查不到", "fansNum": 9000},
                {"awemeId": "dy_sq_added", "nickname": "有人跟", "fansNum": 20000},
                {"awemeId": "dy_sq_male", "nickname": "男粉多", "fansNum": 50000},
                {"awemeId": "dy_sq_new", "nickname": "广场新人", "fansNum": 40000},          # the same one twice
                {"awemeId": "dy_sq_talked", "nickname": "沟通过", "fansNum": 30000, "imStatus": 2},
                {"awemeId": "dy_sq_crm", "nickname": "在库里", "fansNum": 30000, "imStatus": 1, "inCrm": True},
                {"awemeId": "dy_sq_cheap", "nickname": "便宜货", "fansNum": 30000, "imStatus": 1, "inCrm": False}],
        lookup={"dy_sq_new": sq_new, "dy_sq_added": {**row("dy_sq_added", 20000), "added": True},
                "dy_sq_male": {**row("dy_sq_male", 50000, sex="男性 70.00%"), "added": False},
                "dy_sq_cheap": {**row("dy_sq_cheap", 30000, avg=59.9), "added": False}}))
    code, out = plan_now(env, config={"find": {**tool.DEFAULT_FIND, "dy_queries": [SQUARE]}})
    assert code == 0, out
    calls = env.calls()
    square = [c["args"] for c in calls if c["args"][:2] == ["buyin", "authors-square"]]
    assert square == [[*SQUARE["args"], "--expect-shop-id=9100000000000001", "--no-open"]]
    assert all(c["wrapper"] == "paced-dy.mjs" for c in calls if c["args"][:2] == ["buyin", "authors-square"])
    looked = [c["args"] for c in calls if c["args"][:1] == ["lookup"]]
    assert [a[2] for a in looked] == ["--no=dy_sq_new", "--no=dy_sq_missing", "--no=dy_sq_added", "--no=dy_sq_male", "--no=dy_sq_cheap"], \
        "only the new ones, once each, in the order the square gave them; not those the square already says are taken"
    assert all("--expect-uid=900001" in a for a in looked)
    plan = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
    by = {c["aweme_id"]: c for c in plan["creators"]}
    assert by["dy_sq_new"]["source"] == "抖音" and by["dy_small_a"]["source"] == "两边都有" and by["dy_mid_a"]["source"] == "聚水潭"
    assert by["dy_sq_new"]["female_share"] == 90.0 and by["dy_sq_new"]["products"]["main"] == P_PILLBOX, "the 聚水潭 row's data, our rules"
    gone = {c["aweme_id"]: c for c in plan["excluded"]}
    assert gone["dy_sq_missing"]["reason"].startswith("聚水潭查不到这个号") and gone["dy_sq_missing"]["source"] == "抖音"
    assert gone["dy_sq_added"]["reason"].startswith("已经在聚水潭「我的达人」里")
    assert gone["dy_sq_male"]["reason"].startswith("女性粉丝 30.0%"), "a 抖音 one goes through the same rules"
    assert gone["dy_sq_talked"]["reason"].startswith("百应里的按钮已经不是「邀约带货」（状态 2")
    assert gone["dy_sq_crm"]["reason"] == "已经在本店百应达人库里"
    assert gone["dy_sq_cheap"]["reason"].startswith("件单价 59.9 元不在 150～500 元")
    assert plan["douyin"] == {"found": 8, "new": 5, "looked_up": 5, "not_looked_up": 0, "both": 1, "stopped": None}
    md = Path(out["files"]["md"]).read_text(encoding="utf-8")
    assert "抖音达人广场也找了（1 组搜索）：找到 8 位，其中 1 位聚水潭那边也找到了；新的 5 位按抖音号到聚水潭查了 5 位" in md
    assert "| 来源 | 抖音号 |" in md and "| 抖音 | dy_sq_new |" in md
    # at most dy_lookup_max look-ups a plan; a lost 聚水潭 login stops it
    env.log.unlink()
    code, out = plan_now(env, config={"find": {**tool.DEFAULT_FIND, "dy_queries": [SQUARE], "dy_lookup_max": 2}})
    plan = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
    assert len([c for c in env.calls() if c["args"][:1] == ["lookup"]]) == 2 and plan["douyin"]["not_looked_up"] == 3
    assert "还有 3 位没查（超过一次最多查的 2 位）" in Path(out["files"]["md"]).read_text(encoding="utf-8")
    # 聚水潭 rate-limits a look-up: no more look-ups, the list is still made from what was found
    env.scenario(scenario(square=[{"awemeId": "dy_sq_new", "nickname": "x", "fansNum": 1}, {"awemeId": "dy_sq_two", "nickname": "y", "fansNum": 1}],
                          exits={"lookup": 76}))
    env.log.unlink()
    code, out = plan_now(env, config={"find": {**tool.DEFAULT_FIND, "dy_queries": [SQUARE]}})
    assert code == 0, out
    plan = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
    assert plan["douyin"]["looked_up"] == 0 and plan["douyin"]["not_looked_up"] == 2 and "限流" in plan["douyin"]["stopped"]
    assert len([c for c in env.calls() if c["args"][:1] == ["lookup"]]) == 1, "not one more look-up after the wall"
    assert {c["aweme_id"] for c in plan["creators"]} >= {"dy_small_a", "dy_mid_a"}, "the 聚水潭 side is still on the list"
    env.scenario(scenario(square=[{"awemeId": "dy_sq_new", "nickname": "x", "fansNum": 1}], exits={"lookup": 77}))
    code, _ = plan_now(env, config={"find": {**tool.DEFAULT_FIND, "dy_queries": [SQUARE]}})
    assert code == 77
    # the config may only filter and page
    for bad in (["buyin", "author-invite", "--aweme-id=x"], ["buyin", "authors-square", "--execute"],
                ["buyin", "authors-square", "--transport=http"], ["fxg", "products"], ["buyin", "authors-square", "--x y"]):
        cfg = env.write_config(find={**tool.DEFAULT_FIND, "dy_queries": [{"args": bad}]})
        code, _, _ = env.main(["plan", "--config", cfg, "--out-dir", str(env.tmp / "p"), "--ledger-dir", str(env.tmp / "l")])
        assert code == 2, bad


def test_plan_can_leave_out_creators_whose_fans_spend_little(env):
    fans = [row("dy_fp_low", 30000, price="客单价50-100元 28.39%"),
            row("dy_fp_tiny", 31000, price="客单价25元以下 30.00%"),
            row("dy_fp_ok", 32000, price="客单价100-200元 30.88%", crowd="genz 34.22%"),
            row("dy_fp_high", 33000, price="客单价500元以上 34.05%", crowd="资深中产 20.10%"),
            row("dy_fp_none", 34000)]                                         # 聚水潭 gives no 客单价 for her fans
    env.scenario(scenario(creators=fans))
    # off by default: nobody is left out or flagged for it, but the list shows the fans' 客单价 and 人群
    code, out = plan_now(env)
    assert code == 0, out
    plan = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
    by = {c["aweme_id"]: c for c in plan["creators"]}
    assert set(by) == {"dy_fp_low", "dy_fp_tiny", "dy_fp_ok", "dy_fp_high", "dy_fp_none"}
    assert all(c["flags"] == [] for c in by.values())
    assert (by["dy_fp_ok"]["fans_price"], by["dy_fp_ok"]["fans_price_low"], by["dy_fp_ok"]["fans_crowd"]) == ("100-200元", 100.0, "Z世代")
    assert (by["dy_fp_tiny"]["fans_price"], by["dy_fp_tiny"]["fans_price_low"]) == ("25元以下", 0.0)
    assert (by["dy_fp_high"]["fans_price"], by["dy_fp_high"]["fans_price_low"], by["dy_fp_high"]["fans_crowd"]) == ("500元以上", 500.0, "资深中产")
    assert (by["dy_fp_none"]["fans_price"], by["dy_fp_none"]["fans_price_low"], by["dy_fp_none"]["fans_crowd"]) == (None, None, None)
    md = Path(out["files"]["md"]).read_text(encoding="utf-8")
    assert "| 件单价 | 粉丝客单价 | 等级 |" in md and "| 女粉 | 粉丝人群 |" in md
    assert "| 300.0 | 100-200元 | LV3 |" in md and "| Z世代 |" in md and "粉丝客单价 ≥" not in md
    # 100 (the user, 2026-11-02): a band under it is off the list with the reason; a creator without one is only flagged
    code, out = plan_now(env, config={"rules": {"fans_price_min": 100}})
    assert code == 0, out
    plan = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
    by = {c["aweme_id"]: c for c in plan["creators"]}
    assert set(by) == {"dy_fp_ok", "dy_fp_high", "dy_fp_none"}
    assert by["dy_fp_none"]["flags"] == ["粉丝客单价不知道"] and by["dy_fp_ok"]["flags"] == []
    assert [c["aweme_id"] for c in plan["creators"]][-1] == "dy_fp_none", "the flagged one after the others"
    gone = {c["aweme_id"]: c["reason"] for c in plan["excluded"]}
    assert gone["dy_fp_low"] == "粉丝客单价 50-100元（低于 100 元）：她的粉丝平时一单花得少"
    assert gone["dy_fp_tiny"] == "粉丝客单价 25元以下（低于 100 元）：她的粉丝平时一单花得少"
    md = Path(out["files"]["md"]).read_text(encoding="utf-8")
    assert "件单价 150～500 元，粉丝客单价 ≥ 100 元（粉丝画像里占比最大的那一档，按它的下限比），没加进达人库；" in md
    table = list(csv.reader(io.StringIO(Path(out["files"]["csv"]).read_text(encoding="utf-8-sig"))))
    assert table[0][6:9] == ["件单价", "粉丝客单价", "等级"] and table[0][12] == "粉丝人群"
    ok = next(r for r in table if "dy_fp_ok" in r)
    assert ok[7] == "100-200元" and ok[12] == "Z世代"
    # bands are compared by their lower bound: 200 leaves out the 100-200 one too
    code, out = plan_now(env, config={"rules": {"fans_price_min": 200}})
    plan = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
    assert {c["aweme_id"] for c in plan["creators"]} == {"dy_fp_high", "dy_fp_none"}
    # one found on 抖音 goes by the same rule, with her 聚水潭 look-up's portrait
    env.scenario(scenario(creators=fans, square=[{"awemeId": "dy_sq_cheapfans", "nickname": "x", "fansNum": 40000}],
                          lookup={"dy_sq_cheapfans": {**row("dy_sq_cheapfans", 40000, avg=228.0, price="客单价50-100元 31.00%"), "added": False}}))
    code, out = plan_now(env, config={"rules": {"fans_price_min": 100}, "find": {**tool.DEFAULT_FIND, "dy_queries": [SQUARE]}})
    assert code == 0, out
    gone = {c["aweme_id"]: c for c in json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))["excluded"]}
    assert gone["dy_sq_cheapfans"]["reason"].startswith("粉丝客单价 50-100元（低于 100 元）") and gone["dy_sq_cheapfans"]["source"] == "抖音"
    # the portrait's words as 聚水潭 writes them; anything else is not known
    assert tool.fans_price({"fans": {"price": "客单价 200 - 500 元 28.67%"}}) == ("200-500元", 200.0)
    assert tool.fans_price({"fans": {"price": "客单价0-25元"}}) == ("0-25元", 0.0)
    assert tool.fans_price({"fans": {"price": "高消费 30%"}}) == tool.fans_price({"fans": None}) == tool.fans_price({}) == (None, None)
    assert tool.fans_crowd({"fans": {"crowd": "精致妈妈"}}) == "精致妈妈" and tool.fans_crowd({"fans": {"crowd": "GENZ 12.5%"}}) == "Z世代"
    assert tool.fans_crowd({"fans": {}}) is None
    # the config: a number from 0 to 500 (the highest band starts at 500)
    for bad in (-1, 501, "100", True):
        cfg = env.write_config(rules={"fans_price_min": bad})
        code, _, _ = env.main(["plan", "--config", cfg, "--out-dir", str(env.tmp / "p"), "--ledger-dir", str(env.tmp / "l")])
        assert code == 2, bad


def test_plan_mixes_the_three_categories_and_puts_the_better_fits_first(env):
    code, out = plan_now(env, count=3)
    assert code == 0, out
    plan = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
    assert [c["category"] for c in plan["creators"]] == ["small", "mid", "big"], "one of each, not the first three the search gave"
    assert plan["creators"][0]["aweme_id"] == "dy_small_a", "inside a category: no hat seller first, then the fewer flags (a tie: the search's order)"
    assert {c["aweme_id"] for c in plan["spare"]} >= {"dy_big_b"}, "checked and invitable but over the count: kept as spare, not dropped"
    md = Path(out["files"]["md"]).read_text(encoding="utf-8")
    assert "## 备选（查过状态、可以邀约" in md
    order = tool.mix_categories([{"category": k, "flags": f, "hat_share": h, "id": i} for i, (k, f, h) in enumerate(
        [("small", [], 5.0), ("small", ["x"], 50.0), ("small", [], 20.0), ("mid", [], 1.0), ("big", ["y"], 3.0)])])
    assert [o["id"] for o in order] == [4, 3, 0, 2, 1], "a higher hat share no longer goes first (2026-11-02)"
    # a tie (same flags, same hat share) is taken from the two routes in turn, so a list cut short still holds both
    rows = [{"category": "small", "flags": ["帽子占比只有 0.0%"], "hat_share": 0.0, "source": s, "id": i}
            for i, s in enumerate(["聚水潭", "聚水潭", "聚水潭", "抖音", "抖音", "两边都有"])]
    assert [o["id"] for o in tool.mix_categories(rows)] == [0, 3, 1, 4, 2, 5]
    assert [o["id"] for o in tool.mix_categories(rows + [{"category": "small", "flags": [], "hat_share": 0.0, "source": "抖音", "id": 9}])][0] == 9, \
        "a better fit still goes first, whichever route found her"
    # the ones who already sell hats go last, whatever their flags (2026-11-02: of 19 invited none answered)
    sellers = [{"category": "small", "flags": [], "hat_share": h, "source": "抖音", "id": i} for i, h in ((7, 0.3), (8, 40.0))]
    assert [o["id"] for o in tool.mix_categories(sellers + rows)] == [0, 3, 1, 4, 2, 5, 7, 8]
    assert [o["id"] for o in tool.mix_categories([{**sellers[1], "category": "mid"}, *rows])][:2] == [8, 0], \
        "the categories still take turns: a seller is last only inside her own category"


def test_plan_checks_status_twenty_at_a_time(env):
    many = [row(f"dy_many_{i:02d}", 10000 + i) for i in range(45)]
    env.scenario(scenario(creators=many, missing=[], status={"dy_many_03": 2}))
    code, out = plan_now(env, count=30)
    assert code == 0, out
    batches = [c["args"] for c in env.calls() if c["args"][:2] == ["buyin", "author-invite-status"]]
    assert [len(a[2].split("=", 1)[1].split(",")) for a in batches] == [20, 20], "the two pages the search read (40 creators), 20 a request"
    plan = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
    assert len(plan["creators"]) == 30 and plan["not_checked"] == 0
    env.log.unlink()
    code, out = plan_now(env, count=15)
    batches = [c["args"] for c in env.calls() if c["args"][:2] == ["buyin", "author-invite-status"]]
    assert len(batches) == 1, "15 wanted: one batch of 20 is enough, the rest is not asked about"
    plan = json.loads(Path(out["files"]["json"]).read_text(encoding="utf-8"))
    assert len(plan["creators"]) == 15 and plan["not_checked"] == 20


def test_config_is_checked_before_anything_runs(env):
    for over, why in (({"commission_rate": 26, "rights": ["high-commission"]}, "commission_rate"), ({"invite_wechat": "", "invite_phone": ""}, "invite_wechat"),
                      ({"rights": ["high-commission"]}, "专属高佣 needs a rate"), ({"commission_rate": 25}, "a rate needs 专属高佣"),
                      ({"dy_expect_shop_id": ""}, "dy_expect_shop_id"), ({"templates": {"small": "x" * 151}}, "150"),
                      ({"templates": {"mid": "顺丰包邮"}}, "快递公司"), ({"rights": ["free-sample", "nope"]}, "rights"),
                      ({"wait_between": [5, 8]}, "wait_between"), ({"gap_seconds": 1}, "gap_seconds"), ({"brand": ""}, "brand"),
                      ({"min_stock": 10, "products": [{"id": P_BERET, "name": "测试贝雷帽", "price": 339}]}, "styles"),
                      ({"rules": {"own_brand_words": "女装|(["}}, "own_brand_words"), ({"rules": {"drop_ids": ["bad id!"]}}, "drop_ids"),
                      ({"find": {**tool.DEFAULT_FIND, "queries": [{"fans_min": 5000, "level_min": 9}]}}, "level_min"),
                      ({"products": [{"id": f"30000000000000002{i:02d}", "name": f"款{i}", "price": 200} for i in range(6)]}, "5 款")):
        cfg = env.write_config(**over)
        code, _, _ = env.main(["plan", "--config", cfg, "--out-dir", str(env.tmp / "p"), "--ledger-dir", str(env.tmp / "l")])
        assert code == 2, over
    assert env.calls() == []
    assert tool.js_length("😀a") == 3
    # without min_stock a product needs no style codes; a card takes the main one and all the others (at most 5 in all)
    assert tool.load_config(env.write_config(products=[{"id": P_BERET, "name": "测试贝雷帽", "price": 339}]))["products"][0]["styles"] == []
    five = [{"id": f"30000000000000003{i:02d}", "price": price} for i, price in enumerate((339, 398, 229, 228, 469))]
    assert [p["id"] for p in tool.assign_products(400.0, five)] == [five[1]["id"], five[0]["id"], five[2]["id"], five[3]["id"], five[4]["id"]]


# ── run ─────────────────────────────────────────────────────────────────────

def invited_before(env, ledger="ledger"):
    led = tool.Ledger(env.tmp / ledger)
    if "dy_invited" not in led.never_again():
        led.append({"event": "attempt", "attempt": "old1", "aweme_id": "dy_invited", "plan_id": "Pold"})
        led.append({"event": "result", "attempt": "old1", "aweme_id": "dy_invited", "sent": True, "outcome": "VERIFIED_SUCCESS"})


def make_plan(env, ledger="ledger", **scenario_over):
    invited_before(env, ledger)
    env.scenario(scenario(**scenario_over))
    code, out = plan_now(env, ledger=ledger)
    assert code == 0, out
    return out["files"]["json"], out["plan_id"]


def run_now(env, plan_file, confirm, *extra, ledger="ledger", **kw):
    cfg = env.write_config()
    return env.main(["run", "--config", cfg, "--plan", plan_file, "--confirm", confirm, "--ledger-dir", str(env.tmp / ledger), *extra], **kw)


def test_run_previews_then_executes_bound_to_the_preview_and_records_each_one(env):
    plan_file, plan_id = make_plan(env)
    env.log.unlink()
    code, out, sleeps = run_now(env, plan_file, plan_id)
    assert code == 0, out
    calls = [c["args"] for c in env.calls()]
    assert len(calls) == 14, "seven creators: a preview and an execution each"
    for preview, execute in zip(calls[0::2], calls[1::2]):
        assert "--execute" not in preview
        assert execute[:len(preview)] == preview, "the execution is the preview's command plus the binding"
        assert execute[len(preview):] == ["--expect-status=1", "--expect-popwindow=-1", "--execute"]
    first = calls[0]
    assert "--rights=free-sample" in first and "--wechat=shop_test_wx" in first, "only 免费申样 on the card (2026-10-29 afternoon)"
    assert not any(a.startswith("--commission-rate") for a in first), "no 专属佣金 on the card: the rate is talked over on WeChat"
    assert "--expect-shop-id=9100000000000001" in first and "--no-open" in first
    assert f"--product={P_PILLBOX},{P_BERET},{P_EQUESTRIAN}" in first, "the main product first, then all the others"
    assert len(sleeps) == 6 and all(s == 22.5 for s in sleeps), "15–30 s between two creators (the middle, with this uniform)"
    assert [x["aweme_id"] for x in out["sent"]] == ["dy_small_a", "dy_small_b", "dy_gap", "dy_video_big", "dy_mid_a", "dy_big_a", "dy_big_b"]
    events = [(r["event"], r.get("aweme_id")) for r in ledger_lines(env)]
    run = events[events.index(("run-start", None)):]
    assert run[1:3] == [("attempt", "dy_small_a"), ("result", "dy_small_a")], "the attempt is written BEFORE the execution, the result after"
    assert stat.S_IMODE(os.stat(env.tmp / "ledger" / "invites.jsonl").st_mode) == 0o600
    assert "shop_test_wx" not in (env.tmp / "ledger" / "invites.jsonl").read_text(encoding="utf-8")
    # the same list again: nobody is sent twice
    env.log.unlink()
    code, out, _ = run_now(env, plan_file, plan_id)
    assert code == 0 and out["sent"] == [] and env.calls() == []
    assert all("绝不再发" in x["why"] for x in out["skipped"])


def test_run_records_what_the_preview_said_of_each_products_sample(env):
    warn = "免费申样 is ticked, but 2 of the 3 product(s) have no free sample and go as 买样"
    plan_file, plan_id = make_plan(env, sample={P_PILLBOX: "免费申样"}, warnings=[warn])
    code, out, _ = run_now(env, plan_file, plan_id, "--limit", "1")
    assert code == 0 and out["sent"][0]["warnings"] == [warn]
    attempt = [r for r in ledger_lines(env) if r.get("event") == "attempt"][-1]
    assert attempt["samples"] == {P_PILLBOX: "免费申样", P_BERET: "买样", P_EQUESTRIAN: "买样"}
    assert attempt["warnings"] == [warn] and attempt["rights"] == ["free-sample"] and attempt["commission_rate"] == 0
    report = sorted((env.tmp / "ledger").glob("run-*/report.md"))[-1].read_text(encoding="utf-8")
    assert "（提醒：免费申样 is ticked" in report
    # a 专属佣金 on the card still works when the config asks for it
    cfg = env.write_config(commission_rate=25, rights=["high-commission", "free-sample"])
    assert tool.invite_args(tool.load_config(cfg), {"settings": {"rights": ["high-commission", "free-sample"], "commission_rate": 25}},
                            {"aweme_id": "dy_x", "content": "x", "products": {"main": P_BERET, "extras": []}})[5:7] == \
        ["--rights=high-commission,free-sample", "--commission-rate=25"]
    assert "--rights=none" in tool.invite_args(tool.load_config(env.write_config(rights=[])), {"settings": {"rights": [], "commission_rate": 0}},
                                               {"aweme_id": "dy_x", "content": "x", "products": {"main": P_BERET, "extras": []}})


def test_a_mixed_send_order_takes_the_three_categories_in_turn_best_first(env):
    plan_file, plan_id = make_plan(env)
    env.log.unlink()
    code, out, _ = env.main(["run", "--config", env.write_config(send_order="mixed"), "--plan", plan_file, "--confirm", plan_id,
                             "--ledger-dir", str(env.tmp / "ledger"), "--limit", "4"])
    assert code == 0
    assert [x["aweme_id"] for x in out["sent"]] == ["dy_big_a", "dy_mid_a", "dy_small_a", "dy_big_b"], \
        "a day's batch is the best of what is left in each category, not all the small ones first"
    code, _, _ = env.main(["status", "--config", env.write_config(send_order="sideways")])
    assert code == 2


def test_run_sends_only_the_ones_the_user_picked_from_the_list(env, tmp_path):
    plan_file, plan_id = make_plan(env)
    env.log.unlink()
    pick = tmp_path / "pick.txt"
    pick.write_text("# 用户挑的\ndy_big_b\ndy_small_a\n", encoding="utf-8")
    code, out, _ = env.main(["run", "--config", env.write_config(send_order="mixed"), "--plan", plan_file, "--confirm", plan_id,
                             "--ledger-dir", str(env.tmp / "ledger"), "--only", f"@{pick}"])
    assert code == 0 and [x["aweme_id"] for x in out["sent"]] == ["dy_small_a", "dy_big_b"], "only those two, in the send order"
    assert {a.split("=", 1)[1] for c in env.calls() for a in c["args"] if a.startswith("--aweme-id=")} == {"dy_small_a", "dy_big_b"}
    for bad in ("dy_not_on_the_list", "bad id!"):
        code, _, _ = run_now(env, plan_file, plan_id, "--only", bad)
        assert code == 2, bad


def test_run_needs_the_plan_id_the_user_agreed_to_and_a_fresh_list(env):
    plan_file, plan_id = make_plan(env)
    env.log.unlink()
    code, _, _ = run_now(env, plan_file, "P20261028-0000-zzzz")
    assert code == 2 and env.calls() == []
    code, _, _ = run_now(env, plan_file, plan_id, clock=lambda: NOW + timedelta(days=4))
    assert code == 2 and env.calls() == [], "older than plan_max_age_days (3)"
    cfg = env.write_config(commission_rate=20, rights=["high-commission"])
    code, _, _ = env.main(["run", "--config", cfg, "--plan", plan_file, "--confirm", plan_id, "--ledger-dir", str(env.tmp / "ledger")])
    assert code == 2 and env.calls() == [], "the card would differ from the list that was agreed to"


@pytest.mark.parametrize("phase,exit_code", [("preview", 76), ("preview", 77), ("preview", 79), ("execute", 76), ("execute", 77)])
def test_run_stops_at_once_on_76_77_79_and_says_where(env, phase, exit_code):
    plan_file, plan_id = make_plan(env, exits={f"dy_small_b:{phase}": exit_code})
    env.log.unlink()
    code, out, _ = run_now(env, plan_file, plan_id)
    assert code == exit_code
    assert out["stopped"]["at"] == 1 and "dy_small_b" in out["stopped"]["why"]
    assert [x["aweme_id"] for x in out["sent"]][:1] == ["dy_small_a"]
    later = [c["args"] for c in env.calls() if any(a.startswith("--aweme-id=dy_video_big") for a in c["args"])]
    assert later == [], "nothing after the stop"
    if phase == "execute":
        assert out["sent"][-1]["aweme_id"] == "dy_small_b", "a card that may have gone out is counted as sent"
        assert "dy_small_b" in tool.Ledger(env.tmp / "ledger").never_again()


GONE = ("[IDENTITY_UNVERIFIABLE] the expected identity could not be checked on buyin (the answer carried no usable identity field "
        "(shopId: an id of zero (an anonymous or unset session), not an identity)); the request itself was not sent\n"
        "hint: An expectation that cannot be checked is not a pass. Only the identity request was sent.\n"
        "note: this run had sent 1 platform request when it stopped")


def test_a_logged_out_buyin_tab_stops_the_plan_and_says_so(env, capsys):
    env.scenario(scenario(exits={"status": 1}, said={"status": GONE}))
    code, _ = plan_now(env)
    assert code == 1
    err = capsys.readouterr().err
    assert "百应掉登录了" in err and "[IDENTITY_UNVERIFIABLE]" in err, "the CLI's own error line, not its trailing note"
    assert "note: this run" not in err


@pytest.mark.parametrize("phase", ["preview", "execute"])
def test_a_logged_out_buyin_tab_stops_the_run_at_once_and_nothing_counts_as_sent(env, phase):
    plan_file, plan_id = make_plan(env, exits={f"dy_small_b:{phase}": 1}, said={f"dy_small_b:{phase}": GONE})
    env.log.unlink()
    code, out, _ = run_now(env, plan_file, plan_id)
    assert code == 1
    assert out["stopped"]["at"] == 1 and "百应掉登录了" in out["stopped"]["why"]
    assert [x["aweme_id"] for x in out["sent"]] == ["dy_small_a"]
    assert not any(a.startswith("--aweme-id=dy_gap") for c in env.calls() for a in c["args"]), "nothing after the stop"
    assert "dy_small_b" not in tool.Ledger(env.tmp / "ledger").never_again(), "the check failed before the card's request: a rerun sends her"
    results = [r for r in ledger_lines(env) if r.get("event") == "result" and r.get("aweme_id") == "dy_small_b"]
    assert results == [] if phase == "preview" else [r["sent"] for r in results] == [False]


def test_run_skips_a_state_conflict_and_goes_on_and_a_later_plan_may_ask_again(env):
    plan_file, plan_id = make_plan(env, outcome={"dy_small_b": "STATE_CONFLICT"})
    code, out, _ = run_now(env, plan_file, plan_id)
    assert code == 0
    assert [x["aweme_id"] for x in out["conflicts"]] == ["dy_small_b"]
    assert "dy_video_big" in [x["aweme_id"] for x in out["sent"]], "the run goes on after a conflict"
    assert "dy_small_b" not in tool.Ledger(env.tmp / "ledger").never_again(), "nothing went out: a later plan may ask again"


def test_a_card_that_never_arrived_is_written_back_as_not_sent_and_the_run_stops(env):
    """2026-10-31 live: from the 50th card in 24 hours the platform took the request and sent no card (INDETERMINATE, her
    button still 邀约带货), and the run went on through nine more. Now one more look at her button, a little later: still
    1 → not sent (a rerun sends her) and the run stops, saying how many went out in the last 24 hours."""
    said = "the platform reported an error it did not attribute to any target (errorCount=n/a, …)"
    plan_file, plan_id = make_plan(env, outcome={"dy_small_b": "INDETERMINATE"}, detail={"dy_small_b": said})
    env.log.unlink()
    code, out, sleeps = run_now(env, plan_file, plan_id)
    assert code == 0
    assert [x["aweme_id"] for x in out["sent"]] == ["dy_small_a"]
    assert out["skipped"] == [{"aweme_id": "dy_small_b", "outcome": "NOT_DELIVERED", "exit": 1, "sent": False, "checked_status": 1, "detail": said,
                               "why": "命令行说 INDETERMINATE，再查她的按钮还是「邀约带货」：卡没送到，重跑会再发她"}]
    first = [r for r in ledger_lines(env) if r.get("event") == "result" and r.get("aweme_id") == "dy_small_b"][0]
    assert first["detail"] == said, "what the CLI said of the send is kept in the ledger (the platform's own words, when it gave any)"
    assert not any("detail" in r for r in ledger_lines(env) if r.get("event") == "result" and r.get("aweme_id") == "dy_small_a"), \
        "nothing kept for a card the CLI saw arrive"
    assert out["stopped"]["at"] == 1 and "没把卡发出去" in out["stopped"]["why"]
    assert "台账里这 24 小时发出去 2 张" in out["stopped"]["why"], "dy_invited earlier and dy_small_a now; not the one that never arrived"
    calls = [c["args"] for c in env.calls()]
    assert calls[-1] == ["buyin", "author-invite-status", "--aweme-id=dy_small_b", "--pause-ms=4000",
                         "--expect-shop-id=9100000000000001", "--no-open"], "a read-only look, right after her execution"
    assert "--execute" in calls[-2] and "--aweme-id=dy_small_b" in calls[-2]
    assert sleeps == [22.5, 20.0], "the usual wait before her, then recheck_after_seconds before the look"
    results = [(r["outcome"], r["sent"]) for r in ledger_lines(env) if r.get("event") == "result" and r.get("aweme_id") == "dy_small_b"]
    assert results == [("INDETERMINATE", True), ("NOT_DELIVERED", False)], "the CLI's answer first, then what her button said"
    led = tool.Ledger(env.tmp / "ledger")
    assert "dy_small_b" not in led.never_again() and led.unknown() == []
    report = sorted((env.tmp / "ledger").glob("run-*/report.md"))[-1].read_text(encoding="utf-8")
    assert "停在清单第 2 位" in report and "卡没送到" in report
    # hours later the platform takes cards again: the same command sends her and goes on
    env.scenario(scenario())
    code, out, _ = run_now(env, plan_file, plan_id)
    assert code == 0 and out["stopped"] is None
    assert [x["aweme_id"] for x in out["sent"]][:2] == ["dy_small_b", "dy_gap"] and len(out["sent"]) == 6
    assert tool.load_config(env.write_config())["recheck_after_seconds"] == 20
    code, _, _ = env.main(["status", "--config", env.write_config(recheck_after_seconds=-1)])
    assert code == 2


def test_a_card_the_cli_could_not_confirm_that_did_arrive_counts_and_the_run_goes_on(env):
    plan_file, plan_id = make_plan(env)
    env.scenario(scenario(outcome={"dy_small_b": "ACCEPTED_UNVERIFIED"}, status={"dy_talking": 2, "dy_small_b": 2}))
    code, out, _ = run_now(env, plan_file, plan_id)
    assert code == 0 and out["stopped"] is None and len(out["sent"]) == 7
    b = next(x for x in out["sent"] if x["aweme_id"] == "dy_small_b")
    assert (b["outcome"], b["sent"], b["checked_status"]) == ("ACCEPTED_UNVERIFIED", True, 2), "her button: 继续沟通 · 待回复"
    assert [r["sent"] for r in ledger_lines(env) if r.get("event") == "result" and r.get("aweme_id") == "dy_small_b"] == [True]
    assert "dy_small_b" in tool.Ledger(env.tmp / "ledger").never_again()
    assert "checked_status" not in next(x for x in out["sent"] if x["aweme_id"] == "dy_small_a"), "a verified card is not looked at again"


@pytest.mark.parametrize("why", ["the read fails", "the search does not find her"])
def test_a_card_the_cli_could_not_confirm_whose_button_cannot_be_read_stops_the_run_and_is_never_sent_again(env, why):
    plan_file, plan_id = make_plan(env)
    broken = {"exits": {"status": 1}} if why == "the read fails" else {"missing": ["dy_ghost", "dy_small_b"]}
    env.scenario(scenario(outcome={"dy_small_b": "INDETERMINATE"}, **broken))
    env.log.unlink()
    code, out, _ = run_now(env, plan_file, plan_id)
    assert code == 0 and out["stopped"]["at"] == 1 and "再查她的按钮也没查到" in out["stopped"]["why"]
    assert [(x["aweme_id"], x["sent"], x.get("checked_status", "-")) for x in out["sent"]] == [("dy_small_a", True, "-"), ("dy_small_b", True, None)]
    assert "dy_small_b" in tool.Ledger(env.tmp / "ledger").never_again(), "it may have gone out: never sent again"
    assert not any(a.startswith("--aweme-id=dy_gap") for c in env.calls() for a in c["args"]), "nothing after the stop"


def test_run_skips_what_the_preview_does_not_bear_out(env):
    plan_file, plan_id = make_plan(env, preview_status={"dy_small_a": 2}, preview_gate={"dy_small_b": 4}, blocked=["dy_video_big"])
    code, out, _ = run_now(env, plan_file, plan_id)
    assert code == 0
    skipped = {x["aweme_id"]: x["why"] for x in out["skipped"]}
    assert set(skipped) == {"dy_small_a", "dy_small_b", "dy_video_big"}
    assert "状态 2" in skipped["dy_small_a"] and "门槛 4" in skipped["dy_small_b"] and "disabled" in skipped["dy_video_big"]
    executed = [c["args"] for c in env.calls() if "--execute" in c["args"]]
    assert all(not any(a.endswith(x) for x in ("dy_small_a", "dy_small_b", "dy_video_big")) for args in executed for a in args if a.startswith("--aweme-id="))


def test_run_stops_when_the_platform_quota_runs_low_or_our_own_weekly_cap_is_reached(env):
    plan_file, plan_id = make_plan(env, preview_quota={"dy_video_big": {"levels": "LV2-LV3", "used": 2460, "limit": 2500}})
    code, out, _ = run_now(env, plan_file, plan_id)
    assert code == 0 and "只剩 40 次" in out["stopped"]["why"] and len(out["sent"]) == 3
    # our own cap: 800 a week by default; here 3, and 2 were sent this week already
    led = tool.Ledger(env.tmp / "ledger2")
    plan_file, plan_id = make_plan(env)
    code, out, _ = env.main(["run", "--config", env.write_config(weekly_cap=3), "--plan", plan_file, "--confirm", plan_id,
                             "--ledger-dir", str(env.tmp / "ledger2")])
    assert code == 0 and len(out["sent"]) == 3 and "每周上限 3" in out["stopped"]["why"]
    # a new week starts on Monday (Beijing time).  The cards above were written at the real time, not at NOW: the week
    # after NOW's stopped being a later week the day the suite was run on it (2026-11-02, a Monday)
    monday = tool.week_start(tool.now_bjt()) + timedelta(days=7)
    assert led.sent_this_week(monday - timedelta(seconds=1)) == 3 and led.sent_this_week(monday) == 0


def test_run_leaves_out_who_was_dropped_after_the_list_and_keeps_a_daily_cap(env):
    plan_file, plan_id = make_plan(env)
    cfg = env.write_config(rules={"drop_ids": ["dy_small_b", "dy_mid_a"]}, daily_cap=3, wait_between=[180, 300])
    env.log.unlink()
    code, out, sleeps = env.main(["run", "--config", cfg, "--plan", plan_file, "--confirm", plan_id, "--ledger-dir", str(env.tmp / "ledger")])
    assert code == 0
    skipped = {x["aweme_id"]: x["why"] for x in out["skipped"]}
    assert skipped["dy_small_b"].startswith("手动排除") and not any(a == "--aweme-id=dy_small_b" for c in env.calls() for a in c["args"])
    assert len(out["sent"]) == 2 and "每天上限 3 到了（今天已发 3）" in out["stopped"]["why"], "one was sent earlier today (invited_before)"
    assert sleeps == [240.0], "3 to 5 minutes between two creators (the middle, with this uniform)"
    led = tool.Ledger(env.tmp / "ledger")
    assert led.sent_today(NOW) == 3 and led.sent_today(NOW + timedelta(days=3650)) == 0, "the ledger writes the real time: a day far ahead has none"
    assert tool.load_config(env.write_config())["daily_cap"] == 0, "no daily cap unless the config sets one (the user, 2026-10-29)"


def test_a_crash_between_the_attempt_and_its_result_never_leads_to_a_second_send(env):
    plan_file, plan_id = make_plan(env)
    led = tool.Ledger(env.tmp / "ledger")
    led.append({"event": "attempt", "attempt": "x1", "aweme_id": "dy_small_a", "plan_id": plan_id})     # the process died here
    env.log.unlink()
    code, out, _ = run_now(env, plan_file, plan_id)
    assert code == 0
    assert out["unknown_before"] == ["dy_small_a"]
    assert not any(a == "--aweme-id=dy_small_a" for c in env.calls() for a in c["args"])
    # a write that timed out: not known, never again, and the run stops there
    plan_file, plan_id = make_plan(env, ledger="ledger3")

    def runner(argv, **kw):
        if "--execute" in argv and "--aweme-id=dy_small_b" in argv:
            raise subprocess.TimeoutExpired(argv, 900)
        return subprocess.run(argv, **kw)
    code, out, _ = run_now(env, plan_file, plan_id, ledger="ledger3", runner=runner)
    assert "结果不知道" in out["stopped"]["why"] and out["stopped"]["at"] == 1
    assert tool.Ledger(env.tmp / "ledger3").unknown() == ["dy_small_b"]
    code, out, _ = run_now(env, plan_file, plan_id, ledger="ledger3")
    assert "dy_small_b" not in [x["aweme_id"] for x in out["sent"]] and out["unknown_before"] == ["dy_small_b"]


def test_resume_goes_on_where_the_last_run_stopped(env):
    plan_file, plan_id = make_plan(env)
    code, out, _ = run_now(env, plan_file, plan_id, "--limit", "2")
    assert [x["aweme_id"] for x in out["sent"]] == ["dy_small_a", "dy_small_b"] and "--limit" in out["stopped"]["why"]
    code, out, _ = run_now(env, plan_file, plan_id)
    assert [x["aweme_id"] for x in out["sent"]] == ["dy_gap", "dy_video_big", "dy_mid_a", "dy_big_a", "dy_big_b"]
    code, out, _ = env.main(["status", "--config", env.write_config(), "--ledger-dir", str(env.tmp / "ledger")], clock=lambda: NOW)
    assert out["sent_this_week"] == 8 and out["unknown"] == [], "the seven of this list and the one invited before"


def test_one_run_at_a_time(env):
    plan_file, plan_id = make_plan(env)
    held = tool.Ledger(env.tmp / "ledger")
    held.lock()
    try:
        code, _, _ = run_now(env, plan_file, plan_id)
        assert code == 2
    finally:
        held.unlock()


# ── follow: who answered a card ─────────────────────────────────────────────

def sent_cards(env, *cards, ledger="ledger"):
    led = tool.Ledger(env.tmp / ledger)
    for i, (aweme, at) in enumerate(cards):
        stamp = at.isoformat(timespec="seconds")
        led.append({"event": "attempt", "attempt": f"f{i}", "aweme_id": aweme, "plan_id": "Pf", "at": stamp})
        led.append({"event": "result", "attempt": f"f{i}", "aweme_id": aweme, "sent": True, "outcome": "VERIFIED_SUCCESS", "at": stamp})
    return led


def follow_now(env, at, ledger="ledger", **config):
    notes = []

    def runner(argv, **kw):
        if argv[0] == "/usr/bin/osascript":
            notes.append((argv[-2], argv[-1]))
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.run(argv, **kw)
    code, out, _ = env.main(["follow", "--config", env.write_config(**config), "--ledger-dir", str(env.tmp / ledger)], clock=lambda: at, runner=runner)
    return code, out, notes


def test_follow_tells_this_mac_when_a_creator_answers_a_card(env):
    sent_cards(env, ("dy_fa", NOW - timedelta(hours=1)), ("dy_fb", NOW - timedelta(hours=1)), ("dy_fc", NOW - timedelta(days=3)),
               ("dy_old", NOW - timedelta(days=20)))
    env.scenario(scenario(status={"dy_fa": 4, "dy_fb": 2, "dy_fc": 3}))
    code, out, notes = follow_now(env, NOW)
    assert code == 0 and out["checked"] == 3 and out["waiting"] == 2, "dy_old is past follow_days (14): not asked about, still counted as waiting"
    [ask] = [c["args"] for c in env.calls()]
    assert ask[:3] == ["buyin", "author-invite-status", "--aweme-id=dy_fa,dy_fb,dy_fc"] and "--names" in ask and "--pause-ms=4000" in ask
    assert "--expect-shop-id=9100000000000001" in ask and not any("--execute" in a for a in ask)
    assert [(c["aweme_id"], c["to"]) for c in out["changes"]] == [("dy_fa", 4), ("dy_fc", 3)]
    assert notes == [("百应：达人回复了邀约", "达人_fa（dy_fa） 回复了：去百应「合作消息」看她说什么，要加微信就加"),
                     ("百应：达人拒绝了邀约", "达人_fc（dy_fc） 拒绝了")]
    log = [json.loads(l) for l in (env.tmp / "ledger" / "follow.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(r["aweme_id"], r["from"], r["to"]) for r in log] == [("dy_fa", None, 4), ("dy_fc", None, 3)]
    for name in ("follow.jsonl", "follow-state.json"):
        assert stat.S_IMODE(os.stat(env.tmp / "ledger" / name).st_mode) == 0o600
    # 10 minutes later: the answered ones are done, the waiting one was just asked about
    env.log.unlink()
    code, out, notes = follow_now(env, NOW + timedelta(minutes=10))
    assert out["checked"] == 0 and env.calls() == [] and notes == []
    # 30 minutes later the new one is asked again; one sent 3 days ago waits two hours between two looks
    env.scenario(scenario(status={"dy_fb": 4}))
    code, out, notes = follow_now(env, NOW + timedelta(minutes=30))
    assert out["checked"] == 1 and [c["aweme_id"] for c in out["changes"]] == ["dy_fb"] and len(notes) == 1
    sent_cards(env, ("dy_fd", NOW - timedelta(days=3)), ledger="ledger2")
    env.scenario(scenario(status={"dy_fd": 2}))
    follow_now(env, NOW, ledger="ledger2")
    env.log.unlink()
    follow_now(env, NOW + timedelta(minutes=60), ledger="ledger2")
    assert env.calls() == [], "sent 3 days ago: not again within 115 minutes"
    follow_now(env, NOW + timedelta(minutes=120), ledger="ledger2")
    assert len(env.calls()) == 1


def test_follow_keeps_quiet_at_night_and_says_once_when_it_cannot_look(env, tmp_path):
    sent_cards(env, ("dy_fa", NOW - timedelta(hours=1)))
    code, out, notes = follow_now(env, NOW.replace(hour=2))
    assert out["skipped"].startswith("北京时间 9～23 点之外不查") and env.calls() == [] and notes == []
    env.scenario(scenario(exits={"status": 77}))
    code, out, notes = follow_now(env, NOW)
    assert out["problem"] == "登录失效（退出码 77）" and notes == [("百应：这次查不了达人回复", "登录失效（退出码 77）。处理好以后会接着查")]
    code, out, notes = follow_now(env, NOW + timedelta(minutes=30))
    assert out["problem"] and notes == [], "at most once in two hours"
    code, out, notes = follow_now(env, NOW + timedelta(hours=2, minutes=1))
    assert len(notes) == 1
    # while a run sends cards, follow does not look
    held = tool.Ledger(env.tmp / "ledger")
    held.lock()
    try:
        env.log.unlink()
        code, out, notes = follow_now(env, NOW + timedelta(hours=3))
        assert code == 0 and out == {"skipped": "正在发邀约卡，这次不查"} and env.calls() == []
        runs = [json.loads(l) for l in (env.tmp / "ledger" / "follow.log").read_text(encoding="utf-8").splitlines()]
        assert [r.get("skipped") or r.get("problem") for r in runs][-2:] == ["登录失效（退出码 77）", "正在发邀约卡，这次不查"], "one line a run"
    finally:
        held.unlock()
    calls = []
    tool.mac_notify("标题", '她说"加我"', runner=lambda argv, **kw: calls.append(argv))
    assert calls[0][0] == "/usr/bin/osascript" and calls[0][-2:] == ["标题", '她说"加我"'], "the texts are arguments, never part of the script"


# ── the pacing wrappers, with real node ─────────────────────────────────────

NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_wrappers_space_every_request_out_also_across_processes(tmp_path):
    dy = tmp_path / "dy" / "src"
    dy.mkdir(parents=True)
    (dy / "transport.mjs").write_text(textwrap.dedent("""\
        import { appendFileSync } from 'node:fs';
        export class PageTransport { async request(req) { appendFileSync(process.env.TIMES, `${Date.now()}\\n`); return { sent: true, status: 200 }; } }
    """), encoding="utf-8")
    (dy / "cli.mjs").write_text(textwrap.dedent("""\
        import { PageTransport } from './transport.mjs';
        export async function main(argv) {
            const t = new PageTransport();
            await t.request({ method: 'GET', url: 'https://buyin.example/a?x=1' });
            await Promise.all([t.request({ method: 'POST', url: 'https://buyin.example/b' }), t.request({ method: 'GET', url: 'https://buyin.example/c' })]);
            return argv.includes('--exit-3') ? 3 : 0;
        }
    """), encoding="utf-8")
    jst = tmp_path / "jst" / "src"
    jst.mkdir(parents=True)
    (jst / "cli.mjs").write_text(textwrap.dedent("""\
        import { appendFileSync } from 'node:fs';
        export async function main(argv, { fetchImpl }) {
            for (let i = 0; i < 2; i++) { const r = await fetchImpl('data:,hello', { method: 'GET' }); await r.text(); appendFileSync(process.env.TIMES, `${Date.now()}\\n`); }
            return 0;
        }
    """), encoding="utf-8")
    times, stamp, log = tmp_path / "times", tmp_path / "stamp", tmp_path / "requests.jsonl"
    base = {**os.environ, "TIMES": str(times), "CREATOR_INVITE_STAMP": str(stamp), "CREATOR_INVITE_REQUEST_LOG": str(log), "CREATOR_INVITE_GAP_MS": "250"}
    wrappers = ROOT / "tools" / "creator_invite"
    r1 = subprocess.run([NODE, str(wrappers / "paced-dy.mjs"), "--exit-3"], env={**base, "CREATOR_INVITE_CLI_ROOT": str(tmp_path / "dy")}, capture_output=True, text=True, timeout=60)
    assert r1.returncode == 3, r1.stderr
    r2 = subprocess.run([NODE, str(wrappers / "paced-jst.mjs")], env={**base, "CREATOR_INVITE_CLI_ROOT": str(tmp_path / "jst")}, capture_output=True, text=True, timeout=60)
    assert r2.returncode == 0, r2.stderr
    stamps = [int(x) for x in times.read_text().split()]
    assert len(stamps) == 5
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 240 for g in gaps), gaps
    entries = [json.loads(l) for l in log.read_text().splitlines()]
    assert [e["cli"] for e in entries] == ["dy", "dy", "dy", "jst", "jst"]
    assert entries[0]["path"] == "/a" and "x=1" not in log.read_text(), "no query in the request log"
    assert stat.S_IMODE(os.stat(log).st_mode) == 0o600
    bad = subprocess.run([NODE, str(wrappers / "paced-dy.mjs")], env={**os.environ, "CREATOR_INVITE_CLI_ROOT": str(tmp_path / "dy")}, capture_output=True, text=True, timeout=60)
    assert bad.returncode == 2 and "CREATOR_INVITE_STAMP" in bad.stderr


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system Python")
def test_the_tool_runs_on_the_macs_python_3_9(env):
    cfg = env.write_config()
    done = subprocess.run(["/usr/bin/python3", str(TOOL), "plan", "--config", cfg, "--count", "3", "--out-dir", str(env.tmp / "p39"),
                           "--ledger-dir", str(env.tmp / "l39")], capture_output=True, text=True, timeout=120,
                          env={**os.environ})
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["creators"] == 3
