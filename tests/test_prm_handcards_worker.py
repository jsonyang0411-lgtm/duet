"""直播手卡 on the Mac (tools/claude_relay/worker.py with src/prm/handcards.py), against the real relay and PRM service.

A fake ``claude`` answers with a draft that asks for cards (one answer per turn: a short name the card cannot hold is
asked about once more); a fake skill script writes the PNGs; the cards go back as the draft's files and the draft waits
in 待审批.  Without handcards.json on the Mac, or without the bot's switch, no job's prompt has the rule nor its schema
the request: a draft that names products all the same goes its usual way, without cards or a verdict; a 「做手卡」 on a
Mac without settings fails at once without a model run.  A card that does not go up, or that would not fit the job's
9 files, fails the whole set.  The prompt lines also run on macOS's own Python 3.9.  Products, ids and names are made
up.
"""

import json
import logging
import os
import subprocess
import sys
import textwrap
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_prm_handcards import FAKE_CARDS, ITEM, ITEM2, catalog, grid_image, hat_skus, product, single_image
from test_prm_worker import TOKEN, creator_says, run_one, settings, worker  # noqa: F401  (the worker fixture)

from src.prm import handcards as hc
from src.prm.result import SCHEMA
from src.prm.service import CARDS_FAILED_TEXT, PrmService
from src.prm.store import PrmStore
from src.relay.service import ClaudeRelay
from src.relay.store import RelayStore

ROOT = Path(__file__).resolve().parents[1]
CREATOR = "wxid_creator01"
LINE = "老师，手卡整理好啦～"
TOO_LONG = "第 1 款短名「复古西部巴拿马帽」超过 6 个字，卡上放不下（问过 Mac 也没改短）：再点一次「做手卡」"

# One answer per turn of the conversation (the task, then what the worker asks again), the last one repeated; a null
# answer is a turn that ends without the structured answer.
FAKE_CLAUDE = textwrap.dedent("""\
    import json, os, sys
    args = sys.argv[1:]
    answers = json.load(open(os.environ["FAKE_CLAUDE_DRAFT"], encoding="utf-8"))
    turn = 0
    while True:
        line = sys.stdin.readline()                     # stream-json input, until the worker closes it
        if not line:
            break
        if not line.strip():
            continue
        prompt = json.loads(line)["message"]["content"]
        with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"args": args, "prompt": prompt, "turn": turn}, ensure_ascii=False) + "\\n")
        draft = answers[min(turn, len(answers) - 1)]
        turn += 1
        event = {"type": "result", "is_error": False, "total_cost_usd": 0.02, "permission_denials": []}
        if draft is None:
            event["result"] = "好的"
        else:
            event.update(result=json.dumps(draft, ensure_ascii=False), structured_output=draft)
        print(json.dumps(event, ensure_ascii=False), flush=True)
""")

BASE = {"intent": "要手卡", "slots": {}, "stage_proposal": "", "message_kind": "material", "reply_draft": LINE,
        "attachments_to_send": [], "needs_attention": False, "reason": "kb 手卡那一节"}


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE, encoding="utf-8")
    script.chmod(0o755)
    log, answer = tmp_path / "calls.jsonl", tmp_path / "draft.json"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_DRAFT", str(answer))

    def turns(*changes):
        """The answer of each turn: the base draft with these changes (None: no structured answer)."""
        answer.write_text(json.dumps([None if c is None else {**BASE, **c} for c in changes], ensure_ascii=False),
                          encoding="utf-8")

    turns({"handcards": {"call": "小美", "products": [product(photo_cell="左下"),
                                                      product(ITEM2, short="无檐小礼帽", cut_bottom=True)]}})
    return SimpleNamespace(path=str(script), answers=lambda **draft: turns(draft), turns=turns, calls=lambda: [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else [])


def schema_of(args):
    """The --json-schema a claude command line gives."""
    return json.loads(args[args.index("--json-schema") + 1])


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
    """The workspace (catalogue synced now, main images), the fake skill script, and this Mac's handcards.json."""
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


def her_job(worker, bot, fake_claude, mac, tmp_path, text="老师手卡发我一下"):  # noqa: F811
    job_id = creator_says(bot, text)
    run_one(worker, bot, settings(bot, fake_claude, tmp_path, prm_workspace=str(mac.ws)))
    return job_id


def test_the_request_field_is_in_a_jobs_schema_only_with_the_rule(worker, fake_claude):  # noqa: F811
    """(Review 2026-11-09, item 5.)  Every other job's answer is exactly as before PRM_HANDCARDS."""
    assert worker.handcards is not None and worker.PRM_SCHEMA == SCHEMA and "handcards" not in SCHEMA["properties"]
    assert worker.prm_schema() == worker.prm_schema(False) == SCHEMA
    told = worker.prm_schema(True)
    assert told["properties"]["handcards"] == hc.REQUEST_SCHEMA and told["required"] == SCHEMA["required"]
    assert {k: v for k, v in told["properties"].items() if k != "handcards"} == SCHEMA["properties"]
    s = {"claude_bin": fake_claude.path}
    assert schema_of(worker.prm_claude_command(s)) == SCHEMA
    assert schema_of(worker.prm_claude_command(s, cards=True)) == told


def test_her_request_for_cards_comes_back_with_the_cards_and_waits_for_a_person(worker, bot, fake_claude, mac,  # noqa: F811
                                                                                tmp_path, caplog):
    mac.set_up()
    with caplog.at_level(logging.INFO, logger="claude_relay.worker"):
        her_job(worker, bot, fake_claude, mac, tmp_path)
    [call] = fake_claude.calls()
    assert worker.HANDCARDS_RULE in call["prompt"].split("\n")
    assert schema_of(call["args"])["properties"]["handcards"] == hc.REQUEST_SCHEMA
    [made] = mac.cards()
    assert made["sheet"]["channel"] == "小美专属" and [p["id"] for p in made["sheet"]["products"]] == [ITEM, ITEM2]
    [packet] = bot.prm.store.packets()
    assert (packet["status"], packet["text"]) == ("pending", LINE)
    assert [a["name"] for a in packet["attachments"]] == ["手卡_01_测试巴拿马帽.png", "手卡_02_无檐小礼帽.png"]
    assert all(Path(a["path"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" for a in packet["attachments"])
    cards = packet["meta"]["handcards"]
    assert cards["ok"] is True and cards["count"] == 2 and "沿用了老板模板（测试）的口径" in cards["note"]
    assert f"{mac.home}/platform/handcards/" in cards["note"] or "~/" in cards["note"]   # where the session is kept
    assert cards["promo_until"] == "2099-12-31" and abs(cards["catalog_at"] - time.time()) < 120
    # (review item 12) her 称呼 is in no file name and in no line of the worker's log
    logged = [r.getMessage() for r in caplog.records if r.name == "claude_relay.worker"]
    assert logged and not any("小美" in line for line in logged)
    assert not any("小美" in p.name for p in (mac.home / "platform" / "handcards").rglob("*"))


def test_without_the_macs_settings_no_draft_is_told_and_a_click_fails_at_once(worker, bot, fake_claude, mac,  # noqa: F811
                                                                             tmp_path):
    s = settings(bot, fake_claude, tmp_path, prm_workspace=str(mac.ws))
    fake_claude.answers()
    creator_says(bot, "老师手卡发我一下")
    run_one(worker, bot, s)
    [call] = fake_claude.calls()
    assert "【直播手卡】" not in call["prompt"] and schema_of(call["args"]) == SCHEMA
    [packet] = bot.prm.store.packets()
    assert bot.prm.decide(packet["id"], "drop")["ok"]
    deal = bot.prm.store.deal_of(bot.prm.store.party_by_wxid(CREATOR)["id"])
    assert bot.prm.deal_action("handcards", {"deal_id": deal["id"], "items": [ITEM]})["ok"]
    run_one(worker, bot, s)
    assert len(fake_claude.calls()) == 1                                              # no model run for it
    attention = bot.prm.store.deal(deal["id"])["attention"]
    assert "做手卡的草稿没写成：这台 Mac 做不了手卡：这台 Mac 没有" in attention and "再点一次「做手卡」" in attention


def test_a_request_the_job_did_not_offer_is_left_out_and_the_draft_goes_its_usual_way(worker, bot, fake_claude,  # noqa: F811
                                                                                     mac, tmp_path, caplog):
    """(Review 2026-11-09, item 5.)  PRM_HANDCARDS off on Windows (the job has no ``handcards_ok``): a Mac with
    settings tells no draft, its schema has no ``handcards``; a model that names products all the same gets no card
    and no verdict (one log line): the draft goes as it would have gone without them, here by its kind's gate."""
    mac.set_up()
    bot.prm._handcards = False
    bot.prm.store.set_gate("faq", "auto", time.time())
    fake_claude.answers(message_kind="faq", reply_draft="好的老师，手卡我这边整理好发您～", reason="kb/合作政策.md 手卡",
                        handcards={"products": [product()]})
    with caplog.at_level(logging.INFO, logger="claude_relay.worker"):
        her_job(worker, bot, fake_claude, mac, tmp_path)
    [call] = fake_claude.calls()
    assert "【直播手卡】" not in call["prompt"] and schema_of(call["args"]) == SCHEMA and mac.cards() == []
    [packet] = bot.prm.store.packets()
    assert packet["status"] == "approved" and packet["attachments"] == [] and "handcards" not in packet["meta"]
    assert sum("the job did not offer: left out" in r.getMessage() for r in caplog.records) == 1


def test_a_short_name_too_long_is_asked_about_once_and_the_shorter_one_is_used(worker, bot, fake_claude, mac,  # noqa: F811
                                                                              tmp_path):
    """(Review 2026-11-09, item 6.)  Never cut to 「复古西部巴拿」: the worker asks the model once more, in the same
    conversation, and the card takes the name it gives."""
    mac.set_up()
    fake_claude.turns({"handcards": {"call": "小美", "products": [product(short="复古西部巴拿马帽")]}},
                      {"handcards": {"call": "小美", "products": [product(short="西部巴拿马帽")]}})
    her_job(worker, bot, fake_claude, mac, tmp_path)
    first, again = fake_claude.calls()
    assert again["prompt"] == worker.HANDCARDS_SHORTER.format(names="「复古西部巴拿马帽」")
    assert "最多 6 个字" in again["prompt"] and "别的都照原样" in again["prompt"]
    [made] = mac.cards()
    assert [p["short"] for p in made["sheet"]["products"]] == ["西部巴拿马帽"]
    [packet] = bot.prm.store.packets()
    assert [a["name"] for a in packet["attachments"]] == ["手卡_01_西部巴拿马帽.png"]
    assert packet["meta"]["handcards"]["ok"] is True


@pytest.mark.parametrize("second", ["still_too_long", "no_answer", "no_request"])
def test_a_short_name_that_stays_too_long_makes_no_card_and_the_words_wait(worker, bot, fake_claude, mac,  # noqa: F811
                                                                          tmp_path, second):
    """Asked once, never twice: the name stays too long, the turn asked again ends without an answer (the first one
    stands), or it answers without the request (the first request stands: no card, never words without cards)."""
    mac.set_up()
    long = {"handcards": {"call": "小美", "products": [product(short="复古西部巴拿马帽")]}}
    fake_claude.turns(long, {"still_too_long": long, "no_answer": None, "no_request": {}}[second])
    her_job(worker, bot, fake_claude, mac, tmp_path)
    assert len(fake_claude.calls()) == 2 and mac.cards() == []
    [packet] = bot.prm.store.packets()
    assert (packet["status"], packet["text"], packet["attachments"]) == ("pending", LINE, [])
    assert packet["needs_attention"] and packet["meta"]["handcards"]["problems"] == [TOO_LONG]
    assert bot.prm.decide(packet["id"], "send") == {"ok": False, "error": CARDS_FAILED_TEXT}


@pytest.mark.parametrize("how, why", [
    ("lost", "第 2 个文件没传上去：timed out"),
    ("refused", "中继不收第 2 个文件（这个任务的文件满了：一份草稿最多 9 个文件；或者任务已经结束）"),
])
def test_a_card_that_does_not_go_up_fails_the_whole_set(worker, bot, fake_claude, mac, tmp_path, monkeypatch,  # noqa: F811
                                                        how, why):
    """(Review 2026-11-09, items 1 and 11.)  The 2nd card's upload raised after the relay stored it (its answer
    lost), or the relay refused it: the verdict names every card planned, and the bot carries none of them."""
    mac.set_up()
    real = worker.RelayClient.upload

    def upload(self, job_id, path, name):
        if name.startswith("手卡_02"):
            if how == "lost":
                real(self, job_id, path, name)
                raise TimeoutError("timed out")
            raise urllib.error.HTTPError("http://relay/outfiles", 404, "Not Found", {}, None)
        return real(self, job_id, path, name)

    monkeypatch.setattr(worker.RelayClient, "upload", upload)
    job_id = her_job(worker, bot, fake_claude, mac, tmp_path)
    assert len(bot.relay.store.outfiles(job_id)) == (2 if how == "lost" else 1)
    [packet] = bot.prm.store.packets()
    assert (packet["status"], packet["attachments"]) == ("pending", []) and packet["needs_attention"]
    assert packet["meta"]["handcards"]["ok"] is False and packet["meta"]["handcards"]["problems"] == [why]
    assert packet["reason"].startswith(f"手卡没做成：{why}；草稿里没附手卡")


def test_cards_that_would_not_fit_the_jobs_files_are_not_made_at_all(worker, bot, fake_claude, mac, tmp_path):  # noqa: F811
    """(Review 2026-11-09, item 11.)  The relay takes 9 files per job: a draft that sends 8 of its own leaves room
    for one card, so its two are not made (all or nothing); its own files still go to the person who looks."""
    mac.set_up()
    named = []
    for n in range(8):
        (mac.ws / "assets" / f"款式{n}.jpg").write_bytes(b"jpg-%d" % n)
        named.append(f"assets/款式{n}.jpg")
    fake_claude.answers(attachments_to_send=named, handcards={"call": "小美", "products": [
        product(), product(ITEM2, short="无檐小礼帽")]})
    her_job(worker, bot, fake_claude, mac, tmp_path)
    assert mac.cards() == []
    [packet] = bot.prm.store.packets()
    assert [a["name"] for a in packet["attachments"]] == [f"款式{n}.jpg" for n in range(8)]
    assert packet["meta"]["handcards"]["problems"] == ["一份草稿最多附 9 个文件：这次 2 张手卡加 8 个别的附件，放不下，分两次做"]


def test_a_job_full_of_files_says_so(worker, tmp_path, monkeypatch):  # noqa: F811
    """The worker counts every file it sent back for a job: a card refused once the job holds 9 says the job is
    full."""
    made = hc.made_block(True, cards=["手卡_01_a.png", "手卡_02_b.png"], catalog_at=1.5e9)
    files = [tmp_path / name for name in made["cards"]]
    for path in files:
        path.write_bytes(b"png")
    monkeypatch.setattr(worker.handcards, "make", lambda *a, **k: (made, files))

    class Full:
        def uploaded(self, job_id):
            return 9

        def upload(self, job_id, path, name):
            raise RuntimeError("HTTP Error 404: Not Found")

    verdict, sent = worker.make_handcards({}, {"id": 7}, {"handcards_ok": True}, {"products": []}, tmp_path, Full(),
                                          {"timeout_sec": 0}, "")
    assert sent == [] and verdict["cards"] == made["cards"] and verdict["catalog_at"] == 1.5e9
    assert verdict["ok"] is False and verdict["problems"] == ["这个任务的文件满了（一份草稿最多 9 个文件）"]


def test_old_card_folders_go_with_every_prm_job(worker, monkeypatch):  # noqa: F811
    """(Review 2026-11-09, item 12.)  On the worker's own schedule: every prm job, the hourly sample-status rounds
    too, before anything else."""
    folders = worker.HOME_DIR / "platform" / "handcards"
    old, new = folders / "20261001-090000-job1", folders / "20261108-090000-job2"
    for folder in (old, new):
        folder.mkdir(parents=True)
    os.utime(old, (time.time() - 8 * 86400, time.time() - 8 * 86400))
    monkeypatch.setattr(worker, "do_sample_status_job", lambda *a: {"ok": True})
    assert worker.do_prm_job({}, {"id": 9, "meta": {"op": "sample_status"}}, None, None) == {"ok": True}
    assert not old.exists() and new.exists()


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
    assert "handcards" in schema_of(call["args"])["properties"]
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
                  "字母算半个；放不下的会再问你一次", "sells 写 4 条卖点", "不用为手卡设 needs_attention",
                  "看不出是哪几款就问她"):
        assert words in worker.HANDCARDS_RULE
    ask = {"handcards": {"by": "console", "items": []}}
    line = worker.handcards_line(ask, [])
    assert line.startswith("运营在控制台点了「做手卡」：这一次没有达人的新消息") and "按案卷里她选的款做" in line
    joined = worker.handcards_line(ask, ["商品 1"], joined=True)
    assert "她刚好又发来了消息" in joined and "商品 1" in joined
    assert worker.handcards_line({}, []) == "" and worker.handcards_line({"handcards": True}, []) == ""
    prompt = worker.prm_prompt({"id": 4, "text": "", "meta": ask}, 1, "小美", "deals/1.md", [], [], card_ask=line)
    assert line in prompt.split("\n") and "<creator_message>" not in prompt


def test_what_is_asked_again_and_which_answer_stands(worker):  # noqa: F811
    asked = []
    long = {"structured_output": {"handcards": {"products": [{"short": "复古西部巴拿马帽"}, {"short": "小礼帽"}]}}}
    assert worker.handcards_review(long, asked) == worker.HANDCARDS_SHORTER.format(names="「复古西部巴拿马帽」")
    assert asked == [1]
    as_text = {"result": json.dumps({"handcards": {"products": [{"short": "pillbox小礼帽"}]}}, ensure_ascii=False)}
    assert "「pillbox小礼帽」" in worker.handcards_review(as_text, asked)
    for fine in ({"structured_output": {"handcards": {"products": [{"short": "小礼帽"}]}}}, {"result": "好的"},
                 {"structured_output": {}}, None, {"result": "[1]"}):
        assert worker.handcards_review(fine, []) == ""
    first = {"type": "result", "structured_output": {"reply_draft": "a"}}
    lines = [json.dumps(first), "x", json.dumps({"type": "assistant"}), json.dumps({"type": "result", "result": "好的"})]
    assert worker.structured_events("\n".join(lines)) == [first]


def test_the_handcards_lines_work_on_the_macs_own_python(tmp_path):
    """The worker stays Python 3.9 (macOS's /usr/bin/python3): it loads src/prm/handcards.py by its path, tells the
    rule and a click's line, offers the request in the schema only with it, asks again about a long short name,
    builds a 单子.json from a small catalogue and reads its settings."""
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
        "assert w.handcards is not None and 'handcards' not in w.PRM_SCHEMA['properties']\n"
        "assert 'handcards' in w.prm_schema(True)['properties'] and w.prm_schema(False) is w.PRM_SCHEMA\n"
        "h = w.handcards\n"
        "job = {'id': 1, 'text': '', 'meta': {'handcards': {'by': 'console', 'items': []}, 'handcards_ok': True}}\n"
        "rule = w.handcards_rule(job['meta'], True)\n"
        "line = w.handcards_line(job['meta'], h.describe_asked([{'item_id': '1', 'sku_ids': []}], {}))\n"
        "p = w.prm_prompt(job, 1, 'x', 'deals/1.md', [], [], card_rule=rule, card_ask=line)\n"
        "assert rule in p and line in p and '<creator_message>' not in p\n"
        "asked = []\n"
        "assert w.handcards_review({'structured_output': {'handcards': {'products': [{'short': 'x' * 13}]}}}, asked)\n"
        "assert asked == [1] and len(w.structured_events(json.dumps({'type': 'result', 'result': '{}'}))) == 1\n"
        f"ws = {str(ws)!r}\n"
        "req = {'products': [{'item_id': " + repr(ITEM) + ", 'short': 'abc', 'brand_line': 'b', 'audience': 'a',"
        " 'sells': [{'head': 'h', 'rest': 'r'}], 'photo_cell': '\\u5de6\\u4e0b'}]}\n"
        "cfg = {'globals': {'commission': '25%', 'discount': 0.9}, 'promo': {'text': 'N={N}', 'factor': 0.85,"
        " 'until': date(2099, 1, 1), 'from': None}, 'source': '', 'caveats': []}\n"
        "sheet, photos, report = h.build_sheet(req, catalog=h.load_catalog(ws), skus={}, config=cfg, name='x',"
        " today=date(2026, 11, 9), workspace=ws)\n"
        "assert report['problems'] == [], report\n"
        "assert sheet['products'][0]['daily'] == 315 and sheet['products'][0]['promo'] == 'N=240'\n"
        "assert photos[0]['cell'] == [0, 1] and h.crop_box(1440, 1440, 180, 180, cell=(0, 1)) == (185, 727, 715, 1433)\n"
        "assert h.promo_price(101, 0.9, 0.85) == 77 and h.short_name('abc') == ('abc', True)\n"
        "assert h.catalog_info(ws)['synced_at'] and h.beijing_today(0) == date(1970, 1, 1)\n"
        "assert h.check_made(h.made_block(True, cards=['a.png'], promo_until='2099-01-01'))['promo_until']\n"
        "assert '\\u5f85\\u5b9a' in h.note_text(sheet, report, cfg, date(2026, 11, 9))\n"
        "print('ok', sys.version_info[:2])\n", encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "CLAUDE_RELAY_HOME": str(tmp_path / "home")}
    run = subprocess.run([python, "-I", str(script)], capture_output=True, text=True, timeout=60, cwd=tmp_path, env=env)
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("ok")
