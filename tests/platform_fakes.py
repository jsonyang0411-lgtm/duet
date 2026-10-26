"""Fakes of the three platform CLIs for the platform-action tests (src/prm/platforms.py).

``FakeNode`` is a ``node`` executable (a Python script) that answers the way
xiaohongshu-engineering, douyin-engineering and campaign-engineering do: the rules a
test gives say what a command prints (stdout, stderr, exit code); every call is logged
(argv, cwd).  The answers below are built from the CLIs' code and tests (their key names
are exact; the values are invented, like the CLIs' own fixtures).
"""

import json
import sys
import textwrap
from pathlib import Path

XHS_ITEM = "64f1a2b3c4d5e6f7a8b9c0d1"
XHS_SKU = "64f1a2b3c4d5e6f7a8b9c0d2"
XHS_BUYER = "5e9a8b7c6d5e4f3a2b1c0d9e"
DY_ITEM = "3000000000000000101"
DY_SKU = "3100000000000000201"
DY_UID = "5100000000000002"
DY_CHANNEL = "7200000000000001"
DY_LINK = (f"https://haohuo.jinritemai.com/ecommerce/trade/detail/index.html?id={DY_ITEM}"
           f"&origin_type=604&channel_id={DY_CHANNEL}&channel_type=4")
TB_ITEM = "9000123456789"
TB_CROWD = "90000400001"
TB_CAMPAIGN = "90000000555"

FAKE_NODE = textwrap.dedent("""\
    import json, os, sys, time
    argv = sys.argv[1:]
    with open(os.environ["FAKE_NODE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv, "cwd": os.getcwd()}, ensure_ascii=False) + "\\n")
    rules = json.load(open(os.environ["FAKE_NODE_RULES"], encoding="utf-8"))
    executing = any(a in ("--execute", "--execute=true") for a in argv)
    for rule in rules:
        if all(token in argv for token in rule["match"]) and rule.get("execute", executing) == executing:
            time.sleep(rule.get("sleep", 0))
            out = rule.get("stdout")
            if out is not None:
                sys.stdout.write(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=2) + "\\n")
            sys.stderr.write(rule.get("stderr", ""))
            sys.exit(rule.get("exit", 0))
    sys.stderr.write("[FAKE] no rule for " + " ".join(argv) + "\\n")
    sys.exit(99)
""")


class FakeNode:
    """A fake ``node`` and the three CLI folders it answers for."""

    def __init__(self, root: Path, monkeypatch):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "node"
        self.path.write_text(f"#!{sys.executable}\n" + FAKE_NODE, encoding="utf-8")
        self.path.chmod(0o755)
        self.dirs = {}
        for platform, cli in (("xhs", "bin/xhs.mjs"), ("dy", "bin/dy.mjs"), ("tb", "bin/oc.mjs")):
            folder = self.root / f"{platform}-engineering"
            (folder / "bin").mkdir(parents=True)
            (folder / cli).write_text("// fake\n", encoding="utf-8")
            self.dirs[platform] = folder
        (self.dirs["xhs"] / "tools").mkdir()
        (self.dirs["xhs"] / "tools/dry-execute.mjs").write_text("// fake\n", encoding="utf-8")
        self.log = self.root / "node-calls.jsonl"
        self.rules_file = self.root / "node-rules.json"
        self.rules([])
        monkeypatch.setenv("FAKE_NODE_LOG", str(self.log))
        monkeypatch.setenv("FAKE_NODE_RULES", str(self.rules_file))

    def rules(self, rules: list) -> None:
        self.rules_file.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")

    def calls(self) -> list:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def settings(self, **extra) -> dict:
        """config.json's "platforms" for these folders."""
        values = {"node_bin": str(self.path), **{k: str(v) for k, v in self.dirs.items()}}
        values.update(extra)
        return {"platforms": values}


def rule(match, stdout=None, *, execute=None, stderr="", exit=0, sleep=0) -> dict:
    out = {"match": list(match), "stdout": stdout, "stderr": stderr, "exit": exit, "sleep": sleep}
    if execute is not None:
        out["execute"] = execute
    return out


def catalog() -> dict:
    """A catalogue (kb/products.json) with one product of each platform."""
    return {"version": 1, "synced_at": 1761400000.0, "platforms": {
        "xhs": {"ok": True, "items": [{"id": XHS_ITEM, "title": "防晒霜", "price": "99.00",
                                       "skus": [{"id": XHS_SKU, "name": "50ml", "price": "99.00"}]}]},
        "dy": {"ok": True, "items": [{"id": DY_ITEM, "title": "测试主品", "price": "19.99",
                                      "skus": [{"id": DY_SKU, "name": "红色/M", "price": "19.99"}]}]},
        "tb": {"ok": True, "items": [{"id": TB_ITEM, "title": "连衣裙 夏季", "price": "199.00", "skus": []}]},
    }}


def _summary(**counts) -> dict:
    base = {k: 0 for k in ("PREVIEW", "VERIFIED_SUCCESS", "FAILED", "ACCEPTED_UNVERIFIED", "CHANGED_WITH_SIDE_EFFECT",
                           "OBSERVED_DESIRED_STATE", "INDETERMINATE", "STATE_CONFLICT", "NO_CHANGE_NEEDED")}
    base.update(counts)
    return {"overall": "PREVIEW_ONLY", "counts": base, "wroteToPlatform": False}


# ── 抖音 ──────────────────────────────────────────────────────────────────

def dy_row(target, outcome="PREVIEW", detail="", **extra) -> dict:
    row = {"targetKind": "product_id", "targetId": target, "outcome": outcome, "detail": detail,
           "currentValue": "(status=2; cos_ratio=5)", "desiredValue": "created", "unlocated": False, "blocked": False}
    row.update(extra)
    return row


def dy_result(command, rows, *, executed=False, **fields) -> dict:
    return {"site": "buyin" if command.startswith("promotion") else "fxg", "command": command, "executed": executed,
            "overall": "VERIFIED" if executed else "PREVIEW_ONLY", "platformWrites": int(executed),
            "summary": _summary(), "items": rows, "restore": [],
            "note": "nothing was written; add --execute to write." if not executed else "", **fields,
            "transport": {"kind": "page", "pageSignature": True, "platformRequests": 4}}


def dy_designated_preview(*, blocked="", unlocated=False, addable=True) -> dict:
    row = dy_row(DY_ITEM, detail=f"product {DY_ITEM}, status 2 (推广中) -> create ONE designated-creator strategy: "
                                 "fixed commission 20%; ends never (长期有效, 3000-01-01 00:00:00); 1 creator(s) of 抖音火山")
    if unlocated:
        row.update(unlocated=True, currentValue=None, desiredValue=None,
                   detail=f"no row for product {DY_ITEM} in 已推广商品 (create its promotion first)")
    if blocked:
        row.update(blocked=True, detail=f"blocked — {blocked}; nothing is sent for it")
    return dy_result("promotion-strategy-create-designated", [row], creators={"check": {"verdicts": [
        {"uid": DY_UID, "addable": addable, "reminder": "", "reason": "" if addable else "达人已在其他策略中", "errors": []}]}},
        lowCommission={"status": 2, "floor": 5, "rule": "effect period: a value below the floor is refused"})


def dy_designated_done(outcome="VERIFIED_SUCCESS") -> dict:
    row = dy_row(DY_ITEM, outcome, "the platform acknowledged this target with no reported error, and a re-read "
                                   "shows the requested value", observedValue="created")
    return dy_result("promotion-strategy-create-designated", [row], executed=True, createdStrategies=[
        {"productId": DY_ITEM, "strategyId": "7600000000000000001", "kind": 2, "status": 1, "type": 1, "ratio": 20}])


def dy_promotion(outcome="PREVIEW") -> dict:
    row = dy_row(DY_ITEM, outcome, f"product {DY_ITEM} has no row in 已推广商品 (the answer is complete) -> send the "
                                   "page's StrategyPromotionCreate for it", currentValue="(no promotion row)")
    return dy_result("promotion-create", [row], executed=outcome != "PREVIEW")


def dy_create_preview(*, blockers=(), price_kind="pass") -> dict:
    row = dy_row(DY_ITEM, detail="create a 达人带货 channel product of the main product with 1 SKU(s) "
                                 "(price 15.00; stock shared with the main product; 长期有效)",
                 currentValue="(no channel product yet)", blocked=bool(blockers))
    return dy_result("channel-goods-create", [row], channelGoods={
        "saleChannel": {"option": "kol", "card": 2, "label": "达人带货", "channelType": 4, "isKolExclusive": False},
        "skus": [{"skuId": DY_SKU, "name": "红色/M", "mainPrice": "19.99", "channelPrice": "15.00",
                  "channelStock": "shared with the main product", "assignableStock": 40}],
        "period": "长期有效", "stockDefaultedToZero": False,
        "priceCheck": {"verdict": price_kind, "kind": price_kind, "reasons": [] if price_kind == "pass" else ["渠道价低于近30天最低价"]},
        "blockers": list(blockers), "warnings": ["渠道品链接（…）仅支持预览查看，下单可能被拦截"],
        "created": None, "links": {"channelLink": None, "mainProductLink": None}, "next": None})


def dy_create_done(outcome="VERIFIED_SUCCESS", link=DY_LINK) -> dict:
    row = dy_row(DY_ITEM, outcome, "the platform acknowledged this target with no reported error, and a re-read "
                                   "shows the requested value", observedValue="created")
    return dy_result("channel-goods-create", [row], executed=True, channelGoods={
        "created": {"channelId": DY_CHANNEL, "productId": DY_ITEM},
        "links": {"channelLink": link, "mainProductLink": link.split("&channel_id")[0] if link else None},
        "review": "渠道品已新建成功，审核通过后生效",
        "next": f"dy fxg channel-goods-bind --product-id {DY_ITEM} --channel-id {DY_CHANNEL} --uid <达人 UID,…> --execute"})


def dy_bind_preview(*, blocked=False, already=False) -> dict:
    if already:
        row = dy_row(f"already-bound:{DY_UID}", "NO_CHANGE_NEEDED", targetKind="author_id",
                     currentValue="bound", desiredValue="bound")
    else:
        row = dy_row(DY_UID, targetKind="author_id", currentValue="not-bound", desiredValue="bound", blocked=blocked,
                     detail="blocked (the platform refuses this UID (authors/byIds): 达人未开通带货权限); nothing would be "
                            "sent for it" if blocked else "")
    return dy_result("channel-goods-bind", [row], channelGoods={
        "channel": {"productId": DY_ITEM, "channelId": DY_CHANNEL, "channelType": 4, "kolExclusive": False,
                    "status": 2, "statusName": "审核中"}, "warnings": [], "failedTalents": []})


def dy_bind_done(outcome="VERIFIED_SUCCESS") -> dict:
    row = dy_row(DY_UID, outcome, "bound", targetKind="author_id", currentValue="not-bound", desiredValue="bound",
                 observedValue="bound")
    return dy_result("channel-goods-bind", [row], executed=True, channelGoods={"failedTalents": [], "boundAfter": 1})


# ── 小红书 ────────────────────────────────────────────────────────────────

def xhs_buyers(*buyers) -> dict:
    return {"site": "ark", "view": "find-buyer", "matches": len(buyers), "buyers": [
        {"note_cnt": 3, "user_id": uid, "nick_name": nick, "avatar": "https://example.com/a.jpg", "fan_cnt": 12000}
        for uid, nick in buyers], **({} if buyers else {"refusal": "未找到该主播，请检查姓名是否正确"})}


def xhs_plan(outcome="INDETERMINATE", *, detail="", target=None, stopped=None, executed=True) -> dict:
    return {"site": "ark", "object": "direct-plan", "action": "create", "name": "达人合作7-1026101500", "buyers": 1,
            "window": {"start": "2026-10-26 10:15:00", "end": "长期有效 (3000-01-01 00:00:00)", "longTerm": True},
            "items": [{"targetKind": "direct-plan", "targetId": target, "outcome": outcome, "detail": detail,
                       "request": {"method": "POST", "url": "https://ark.xiaohongshu.com/api/edith/distribution/plans/create_group_with_plan",
                                   "body": {"group_info": {"name": "达人合作7-1026101500", "distributor_ids": [XHS_BUYER]},
                                            "plan_infos": [{"distributor_rate": "20", "item_id": XHS_ITEM}]}},
                       **({"readBack": {"matched": ["name"], "mismatched": [], "unverified": [],
                                        "info": {"taskId": "30000001", "task": {"status": "ALL_SUCCESS", "reads": 1}}}}
                          if outcome == "VERIFIED_SUCCESS" else {})}],
            "executed": executed, "write": {"id": "direct.plan.create", "risk": "buyer-visible"},
            "summary": _summary(), "stopped": stopped}


DRY_STDERR = ("[DRY-EXECUTE] reads sent: 6; writes refused: 1 "
              "(POST /api/edith/distribution/plans/create_group_with_plan)\n")


def xhs_channel_preview(*, blocked=(), problem="") -> dict:
    out = {"site": "ark", "object": "channel-item", "action": "create", "mainItemId": XHS_ITEM,
           "items": [{"targetKind": "channel-item", "targetId": None, "outcome": "PREVIEW", "detail": "",
                      "request": {"method": "POST", "url": "https://ark.xiaohongshu.com/api/edith/product/batch_create_item"}}],
           "executed": False, "summary": _summary(PREVIEW=1), "stopped": None,
           "current": {"blocked": list(blocked), "unsupported": [], "channelItemsOfThisMainItem": 0,
                       "rules": {"title": {"min": 16, "max": 60, "weighted": True}},
                       "skus": [{"skuId": XHS_SKU, "mainSkuId": XHS_SKU, "price": "99.00", "available": True, "stock": 40,
                                 "newPrice": "79.00"}]}}
    if problem:
        out["adjustmentProblem"] = problem
    else:
        out["wouldSend"] = {"title": "防晒霜", "shortTitle": "防晒霜 达人专享", "customFields": ["PRICE"],
                            "available": True, "skuCount": 1, "bodyChars": 2000}
    return out


def xhs_channel_done() -> dict:
    new = "6a0000000000000000000b02"
    return {"site": "ark", "object": "channel-item", "action": "create", "executed": True,
            "items": [{"targetKind": "channel-item", "targetId": new, "outcome": "ACCEPTED_UNVERIFIED",
                       "detail": "sent but not verifiable: form constants, copied form",
                       "readBack": {"matched": ["price"], "mismatched": [], "unverified": [],
                                    "cannotVerify": ["form constants", "copied form"],
                                    "info": {"taskId": "T1", "checkStatus": 2, "itemId": new}}}],
            "summary": _summary(ACCEPTED_UNVERIFIED=1), "stopped": None}


# ── 淘宝 ──────────────────────────────────────────────────────────────────

def tb_row(outcome="PREVIEW", **extra) -> dict:
    row = {"targetKind": "itemId", "targetId": TB_ITEM, "outcome": outcome, "currentValue": "(no campaign yet)",
           "desiredValue": "new onebpDisplay 自定义推广 campaign name=达人合作7_10261015 bidTargetV2=display_pay "
                           "dmcType=normal dayBudget=50 crowds=1", "observedValue": None, "platformWrites": 0,
           "detail": "人群 90000400001@0.80(建议价) | creation starts delivery immediately | --pause-after-create: …",
           "phase": "PREVIEW", "requestState": "NOT_SENT", "errorCode": None, "platformCodes": [],
           "evidence": {"schemaVersion": 1, "write": None},
           "risk": {"schemaVersion": 1, "stopRequired": False, "codes": [], "signals": []}}
    row.update(extra)
    return row


def tb_done(create="VERIFIED_SUCCESS", pause="VERIFIED_SUCCESS") -> list:
    return [tb_row(create, targetKind="campaignId", targetId=TB_CAMPAIGN, platformWrites=1, phase="READBACK",
                   currentValue="status=paused onlineStatus=0 dayBudget=50"),
            tb_row(pause, targetKind="campaignId", targetId=TB_CAMPAIGN, currentValue="start", desiredValue="pause",
                   observedValue="pause", platformWrites=1, detail="--pause-after-create, before the read-back")]


TB_PRECHECK = ("note: 互斥前检……\nok: false\nerror:\n  code: CREATE_PRECHECK_NOT_ALLOWED\n"
               "  message: '[NOT_ALLOWED] the platform reports item 9000123456789 as not promotable in onebpDisplay "
               "(accessAllowed=false, reason: 商品已下架). Nothing was created.'\n  exitCode: 2\n")
