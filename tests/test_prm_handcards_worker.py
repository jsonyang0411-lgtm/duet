"""直播手卡 on the Mac (tools/claude_relay/worker.py with src/prm/handcards.py), against the real relay and PRM service.

A fake ``claude`` answers with a draft that asks for cards; a fake skill script writes the PNGs; the cards go back as
the draft's files and the draft waits in 待审批.  Without handcards.json on the Mac, or without the bot's switch, no
draft is told it may ask for cards and the prompt is as before; a 「做手卡」 on such a Mac fails at once without a model
run.  The prompt lines also run on macOS's own Python 3.9.  Products, ids and names are made up.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_prm_handcards import FAKE_CARDS, ITEM, ITEM2, catalog, grid_image, hat_skus, product, single_image
from test_prm_worker import TOKEN, creator_says, run_one, settings, worker  # noqa: F401  (the worker fixture)

from src.prm import handcards as hc
from src.prm.result import SCHEMA
from src.prm.service import PrmService
from src.prm.store import PrmStore
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore

ROOT = Path(__file__).resolve().parents[1]
CREATOR = "wxid_creator01"
LINE = "老师，手卡整理好啦～"

FAKE_CLAUDE = textwrap.dedent("""\
    import json, os, sys
    args = sys.argv[1:]
    prompt = json.loads(sys.stdin.readline())["message"]["content"]
    with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"args": args, "prompt": prompt}, ensure_ascii=False) + "\\n")
    draft = json.load(open(os.environ["FAKE_CLAUDE_DRAFT"], encoding="utf-8"))
    print(json.dumps({"type": "result", "result": json.dumps(draft, ensure_ascii=False), "structured_output": draft,
                      "is_error": False, "total_cost_usd": 0.02, "permission_denials": []}, ensure_ascii=False))
""")


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE, encoding="utf-8")
    script.chmod(0o755)
    log, answer = tmp_path / "calls.jsonl", tmp_path / "draft.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_DRAFT", str(answer))

    def answers(**draft):
        base = {"intent": "要手卡", "slots": {}, "stage_proposal": "", "message_kind": "material",
                "reply_draft": LINE, "attachments_to_send": [], "needs_attention": False, "reason": "kb 手卡那一节"}
        answer.write_text(json.dumps({**base, **draft}, ensure_ascii=False), encoding="utf-8")

    answers(handcards={"call": "小美", "products": [product(photo_cell="左下"),
                                                    product(ITEM2, short="无檐小礼帽", cut_bottom=True)]})
    return SimpleNamespace(path=str(script), answers=answers, calls=lambda: [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else [])


@pytest.fixture
def bot(tmp_path):
    """The relay over HTTP and the PRM service with PRM_HANDCARDS on, 小美 registered."""
    cfg = SimpleNamespace(
        claude_relay="on", claude_relay_token=TOKEN, claude_relay_host="127.0.0.1", claude_relay_port=0,
        claude_relay_prefix="cc", claude_relay_wxids=[], claude_relay_db=":memory:", claude_relay_max_chars=1500,
        admin_wxid="wxid_admin", prm_copilot="on", prm_db=":memory:", prm_handcards="on",
    )
    relay = ClaudeRelay(cfg, store=RelayStore(":memory:"), files_dir=tmp_path / "relay", send=lambda c, t: True)
    prm = PrmService(cfg, relay, store=PrmStore(":memory:"), send=lambda c, t: True, send_file=lambda c, p: True)
    prm.register(CREATOR, "抖音-小美")
    relay.start()
    host, port = relay.address
    yield SimpleNamespace(relay=relay, prm=prm, url=f"http://{host}:{port}")
    relay.stop()


@pytest.fixture
def mac(tmp_path, monkeypatch, worker):  # noqa: F811
    """The workspace (catalogue, main images), the fake skill script, and this Mac's handcards.json."""
    ws = tmp_path / "biz"
    for sub in ("kb", "assets/products", "deals"):
        (ws / sub).mkdir(parents=True)
    (ws / "kb" / "products.json").write_text(json.dumps(catalog(), ensure_ascii=False), encoding="utf-8")
    grid_image(ws / "assets" / "products" / f"dy-{ITEM}.jpg")
    single_image(ws / "assets" / "products" / f"dy-{ITEM2}.jpg")
    script = tmp_path / "make_cards_local.py"
    script.write_text(FAKE_CARDS, encoding="utf-8")
    skus = tmp_path / "skus.json"
    skus.write_text(json.dumps(hat_skus(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("FAKE_CARDS_LOG", str(tmp_path / "cards.jsonl"))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)

    def set_up(**more):
        cfg = {"script": str(script), "python": sys.executable, "skus": str(skus), "source": "老板模板（测试）",
               "globals": {"commission": "25%", "pack": "盒装", "ship_time": "付款后 7 天内发货", "aftersale": "7天无理由",
                           "ship_from": "广东东莞 · 包邮", "no_ship": "无 · 全国均发", "express": "中通快递"},
               "promo": {"text": "双11再减15%实付约{N}", "factor": 0.85, "until": "2099-12-31"}, **more}
        (home / "handcards.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    return SimpleNamespace(ws=ws, home=home, set_up=set_up, cards=lambda: [
        json.loads(line) for line in (tmp_path / "cards.jsonl").read_text(encoding="utf-8").splitlines()]
        if (tmp_path / "cards.jsonl").exists() else [])


def test_the_worker_offers_the_same_request_field_as_the_bot(worker):  # noqa: F811
    assert worker.handcards is not None and worker.PRM_SCHEMA == SCHEMA
    assert worker.PRM_SCHEMA["properties"]["handcards"] == hc.REQUEST_SCHEMA
    assert "handcards" not in worker.PRM_SCHEMA["required"]


def test_her_request_for_cards_comes_back_with_the_cards_and_waits_for_a_person(worker, bot, fake_claude, mac,  # noqa: F811
                                                                                tmp_path):
    mac.set_up()
    creator_says(bot, "老师手卡发我一下")
    run_one(worker, bot, settings(bot, fake_claude, tmp_path, prm_workspace=str(mac.ws)))
    [call] = fake_claude.calls()
    assert worker.HANDCARDS_RULE in call["prompt"].split("\n")
    [made] = mac.cards()
    assert made["sheet"]["channel"] == "小美专属" and [p["id"] for p in made["sheet"]["products"]] == [ITEM, ITEM2]
    [packet] = bot.prm.store.packets()
    assert (packet["status"], packet["text"]) == ("pending", LINE)
    assert [a["name"] for a in packet["attachments"]] == ["手卡_01_测试巴拿马帽.png", "手卡_02_无檐小礼帽.png"]
    assert all(Path(a["path"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" for a in packet["attachments"])
    cards = packet["meta"]["handcards"]
    assert cards["ok"] is True and cards["count"] == 2 and "沿用了老板模板（测试）的口径" in cards["note"]
    assert f"{mac.home}/platform/handcards/" in cards["note"] or "~/" in cards["note"]   # where the session is kept


def test_without_the_macs_settings_no_draft_is_told_and_a_click_fails_at_once(worker, bot, fake_claude, mac,  # noqa: F811
                                                                             tmp_path):
    s = settings(bot, fake_claude, tmp_path, prm_workspace=str(mac.ws))
    fake_claude.answers()
    creator_says(bot, "老师手卡发我一下")
    run_one(worker, bot, s)
    [call] = fake_claude.calls()
    assert "【直播手卡】" not in call["prompt"]
    [packet] = bot.prm.store.packets()
    assert bot.prm.decide(packet["id"], "drop")["ok"]
    deal = bot.prm.store.deal_of(bot.prm.store.party_by_wxid(CREATOR)["id"])
    assert bot.prm.deal_action("handcards", {"deal_id": deal["id"], "items": [ITEM]})["ok"]
    run_one(worker, bot, s)
    assert len(fake_claude.calls()) == 1                                              # no model run for it
    attention = bot.prm.store.deal(deal["id"])["attention"]
    assert "做手卡的草稿没写成：这台 Mac 做不了手卡：这台 Mac 没有" in attention and "再点一次「做手卡」" in attention


def test_a_request_the_bot_did_not_allow_makes_no_card(worker, bot, fake_claude, mac, tmp_path):  # noqa: F811
    """PRM_HANDCARDS off on Windows (the job has no ``handcards_ok``): a Mac with settings tells no draft, and a draft
    that asks all the same gets no card, and waits for a person saying why."""
    mac.set_up()
    bot.prm._handcards = False
    creator_says(bot, "老师手卡发我一下")
    run_one(worker, bot, settings(bot, fake_claude, tmp_path, prm_workspace=str(mac.ws)))
    assert "【直播手卡】" not in fake_claude.calls()[0]["prompt"] and mac.cards() == []
    [packet] = bot.prm.store.packets()
    assert packet["attachments"] == [] and packet["status"] == "pending" and packet["needs_attention"]
    assert "机器人没打开手卡" in packet["reason"]


def test_the_consoles_click_names_the_products_and_makes_up_no_message_of_hers(worker, bot, fake_claude, mac,  # noqa: F811
                                                                              tmp_path):
    from test_prm_sample_sync import verified_order
    mac.set_up()
    verified_order(bot.prm)                                       # (a 抖店 product not in this workspace's catalogue)
    deal = bot.prm.store.deal_of(bot.prm.store.party_by_wxid(CREATOR)["id"])
    assert bot.prm.deal_action("handcards", {"deal_id": deal["id"], "items": [ITEM]})["ok"]
    fake_claude.answers(handcards={"products": [product()]})
    run_one(worker, bot, settings(bot, fake_claude, tmp_path, prm_workspace=str(mac.ws)))
    [call] = fake_claude.calls()
    line = next(x for x in call["prompt"].split("\n") if x.startswith("运营在控制台点了「做手卡」"))
    assert f"商品 {ITEM}（ACE7CN 测试巴拿马帽 100%羊毛英伦复古皮带扣平顶帽出游造型帽子）" in line
    assert "<creator_message>" not in call["prompt"] and worker.HANDCARDS_RULE in call["prompt"]
    [packet] = bot.prm.store.packets(("pending",))
    assert packet["meta"]["handcards"]["console"] is True and len(packet["attachments"]) == 1


def test_the_lines_and_a_prompt_without_them(worker):  # noqa: F811
    job = {"id": 3, "text": "[11-09 14:00] 手卡发一下", "meta": {"handcards_ok": True}}
    assert worker.handcards_rule(job["meta"], True) == worker.HANDCARDS_RULE
    assert worker.handcards_rule(job["meta"], False) == ""
    assert worker.handcards_rule({}, True) == "" and worker.handcards_rule({"handcards_ok": "yes"}, True) == ""
    assert worker.handcards_rule({"handcards": {"by": "console"}}, True) == worker.HANDCARDS_RULE
    plain = worker.prm_prompt({"id": 3, "text": job["text"], "meta": {}}, 1, "小美", "deals/1.md", [], [])
    assert worker.prm_prompt(job, 1, "小美", "deals/1.md", [], []) == plain           # (no lines given: as before)
    told = worker.prm_prompt(job, 1, "小美", "deals/1.md", [], [], card_rule=worker.HANDCARDS_RULE)
    assert told.index(worker.HANDCARDS_RULE) < told.index("<creator_message>")
    for words in ("item_id 只用 kb/products.md 抖店（dy）那张表里的商品 ID", "photo_cell", "cut_bottom", "最多 6 个字",
                  "sells 写 4 条卖点", "不用为手卡设 needs_attention", "看不出是哪几款就问她"):
        assert words in worker.HANDCARDS_RULE
    ask = {"handcards": {"by": "console", "items": []}}
    line = worker.handcards_line(ask, [])
    assert line.startswith("运营在控制台点了「做手卡」：这一次没有达人的新消息") and "按案卷里她选的款做" in line
    joined = worker.handcards_line(ask, ["商品 1"], joined=True)
    assert "她刚好又发来了消息" in joined and "商品 1" in joined
    assert worker.handcards_line({}, []) == "" and worker.handcards_line({"handcards": True}, []) == ""
    prompt = worker.prm_prompt({"id": 4, "text": "", "meta": ask}, 1, "小美", "deals/1.md", [], [], card_ask=line)
    assert line in prompt.split("\n") and "<creator_message>" not in prompt


def test_the_handcards_lines_work_on_the_macs_own_python(tmp_path):
    """The worker stays Python 3.9 (macOS's /usr/bin/python3): it loads src/prm/handcards.py by its path, tells the
    rule and a click's line, builds a 单子.json from a small catalogue and reads its settings."""
    python = "/usr/bin/python3"
    if not os.path.exists(python):
        pytest.skip("no system python3")
    ws = tmp_path / "ws"
    (ws / "kb").mkdir(parents=True)
    (ws / "assets" / "products").mkdir(parents=True)
    (ws / "kb" / "products.json").write_text(json.dumps(catalog(), ensure_ascii=False), encoding="utf-8")
    (ws / "assets" / "products" / f"dy-{ITEM}.jpg").write_bytes(b"jpg")
    script = tmp_path / "smoke.py"
    script.write_text(
        "import importlib.util, json, sys\n"
        "from datetime import date\n"
        f"spec = importlib.util.spec_from_file_location('w', {str(ROOT / 'tools/claude_relay/worker.py')!r})\n"
        "w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)\n"
        "assert w.handcards is not None and 'handcards' in w.PRM_SCHEMA['properties']\n"
        "h = w.handcards\n"
        "job = {'id': 1, 'text': '', 'meta': {'handcards': {'by': 'console', 'items': []}, 'handcards_ok': True}}\n"
        "rule = w.handcards_rule(job['meta'], True)\n"
        "line = w.handcards_line(job['meta'], h.describe_asked([{'item_id': '1', 'sku_ids': []}], {}))\n"
        "p = w.prm_prompt(job, 1, 'x', 'deals/1.md', [], [], card_rule=rule, card_ask=line)\n"
        "assert rule in p and line in p and '<creator_message>' not in p\n"
        f"ws = {str(ws)!r}\n"
        "req = {'products': [{'item_id': " + repr(ITEM) + ", 'short': 'abc', 'brand_line': 'b', 'audience': 'a',"
        " 'sells': [{'head': 'h', 'rest': 'r'}], 'photo_cell': '\\u5de6\\u4e0b'}]}\n"
        "cfg = {'globals': {'commission': '25%', 'discount': 0.9}, 'promo': {'text': 'N={N}', 'factor': 0.85,"
        " 'until': date(2099, 1, 1), 'from': None}, 'source': '', 'caveats': []}\n"
        "sheet, photos, report = h.build_sheet(req, catalog=h.load_catalog(ws), skus={}, config=cfg, name='x',"
        " today=date(2026, 11, 9), workspace=ws)\n"
        "assert report['problems'] == [], report\n"
        "assert sheet['products'][0]['daily'] == 315 and sheet['products'][0]['promo'] == 'N=240'\n"
        "assert photos[0]['cell'] == [0, 1] and h.crop_box(1440, 1440, 180, cell=(0, 1)) == (185, 727, 715, 1433)\n"
        "assert '\\u5f85\\u5b9a' in h.note_text(sheet, report, cfg, date(2026, 11, 9))\n"
        "print('ok', sys.version_info[:2])\n", encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "CLAUDE_RELAY_HOME": str(tmp_path / "home")}
    run = subprocess.run([python, "-I", str(script)], capture_output=True, text=True, timeout=60, cwd=tmp_path, env=env)
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("ok")
