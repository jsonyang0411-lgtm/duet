"""三方群里的话 (2026-11-02, the user: 「第一点要知道」): what is said in her 三方群, the boss's styles, samples, commission,
her private drafts must know.  The group 「拉三方群」 made is read only from then on (the backend's ``watch_group``): every
line, hers, the boss's, another member's and ours, is kept on her deal, masked, the bosses' names as PRM_BOSS_CALL, never
drafted, never answered, never a takeover; each draft job of her private chat is told the latest 20
(``meta.three_party_chat``), and the Mac's prompt has them (<three_party_chat>).  A 老板拉的群's lines reach her private
drafts the same way.  Every name and id here is made up."""

import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from test_prm import CREATOR, Bot
from test_three_party import BOSS, HER, NEW_GROUP, Admin, Auto, Backend

from src.prm import three_party as tp
from src.prm.service import GROUP_LINE, GROUP_LINES_TOLD, THREE_PARTY_WATCH_BACK_SEC, PrmService

ROOT = Path(__file__).resolve().parents[1]
BOSS_ID = "wxid_boss"
ME = "wxid_me"
_IDS = iter(range(1, 10 ** 6))


class Watching(Backend):
    """The backend of test_three_party that reads groups too (``watch_group`` / ``unwatch_group``)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.watched, self.unwatched = {}, []

    def watch_group(self, chat, since):
        self.watched[chat] = since

    def unwatch_group(self, chat):
        self.watched.pop(chat, None)
        self.unwatched.append(chat)


@pytest.fixture
def clock():
    from test_prm import Clock
    return Clock(time.mktime((2026, 11, 2, 16, 30, 0, 0, 0, -1)))


def make(clock, tmp_path, backend=None, **prm):
    b = Bot(clock, tmp_path, prm_group_with=BOSS, prm_friend_add_brand="品牌测试", **prm)
    party = b.prm.register(CREATOR, "小美", "测试达人")
    b.deal_id = b.prm.store.deal_of(party["id"])["id"]
    b.party = party
    b.backend = backend or Watching()
    b.admin = Admin(b.backend)
    b.prm.three_party.attach(b.admin, Auto(), b.backend)
    b.prm.three_party.spawn = lambda fn: fn()
    b.prm.three_party._sleep = lambda sec: None
    return b


@pytest.fixture
def bot(clock, tmp_path):
    b = make(clock, tmp_path)
    assert b.prm.deal_action("group", {"deal_id": b.deal_id}) == {"ok": True, "state": "creating"}
    yield b
    b.prm.stop()
    b.relay.stop()


def line(b, text, *, sender=CREATOR, name="小美", chat=NEW_GROUP, ts=None, msg_type=1, **extra):
    """A line of the group as the backend hands it on (the standardized group message)."""
    return {"message_id": f"g{next(_IDS)}", "chat_id": chat, "sender_id": sender, "sender_name": name, "content": text,
            "msg_type": msg_type, "is_group": True, "timestamp": b.clock.now if ts is None else ts, **extra}


def own(b, text, *, chat=NEW_GROUP):
    return {"message_id": f"own{next(_IDS)}", "chat_id": chat, "sender_id": ME, "sender_name": "商务号", "content": text,
            "msg_type": 1, "is_group": True, "timestamp": b.clock.now}


def kept(b):
    return [ev["detail"] for ev in reversed(b.prm.store.events(b.deal_id, limit=100, kinds=(GROUP_LINE,)))]


def private_job(b, text="那我私聊问一下，什么时候寄呀"):
    b.prm.handle(b.msg(text))
    return b.worker_takes()


# ── read only ──────────────────────────────────────────────────────────────

def test_the_group_the_click_made_is_read_only_and_her_private_drafts_hear_it(bot, clock):
    assert bot.backend.watched == {NEW_GROUP: clock.now - tp.WATCH_BACK_SEC}, "read from shortly before it was made"
    made = bot.prm.snapshot()
    deal = next(p for p in made["parties"] if p["id"] == bot.party["id"])["deal"]
    assert deal["three_party"]["reading"] is True
    for msg in (line(bot, "老板好～我选黑色和咖啡色这两顶"),
                line(bot, f"好的，那就寄这两顶，收件电话发 {BOSS} 这里 13812345678", sender=BOSS_ID, name=BOSS),
                line(bot, "我来对接物流", sender="wxid_colleague", name="同事小王"),
                line(bot, '"老板测试"邀请你加入了群聊', sender="", name="", msg_type=10000,
                     special={"kind": "system", "title": '"老板测试"邀请你加入了群聊'})):
        assert bot.prm.handle(msg) is True                       # taken, silently
        clock.now += 1
    bot.prm.on_own_message(own(bot, "收到～我这边记下了"))
    assert bot.relay.store.list_jobs(limit=100) == [] and bot.texts == [] and bot.relay_sent == []
    assert bot.prm.store.deal(bot.deal_id)["mode"] == "copilot", "our own line in the group takes no chat over"
    assert [(d["who"], d["label"], d["text"]) for d in kept(bot)] == [
        ("creator", "她", "老板好～我选黑色和咖啡色这两顶"),
        ("boss", "老板", "好的，那就寄这两顶，收件电话发 老板 这里 〈手机1〉"),        # the boss's name hidden, the number masked
        ("member", "群里的人", "我来对接物流"),
        ("us", "我们", "收到～我这边记下了")]                    # (WeChat's line of the join is not kept)
    clock.now += 60
    job = private_job(bot)
    told = job["meta"]["three_party_chat"]
    assert [(t["who"], t["text"]) for t in told] == [("她", "老板好～我选黑色和咖啡色这两顶"),
                                                     ("老板", "好的，那就寄这两顶，收件电话发 老板 这里 〈手机1〉"),
                                                     ("群里的人", "我来对接物流"), ("我们", "收到～我这边记下了")]
    assert told[0]["at"] == time.strftime("%m-%d %H:%M", time.localtime(clock.now - 64))
    assert "13812345678" not in json.dumps(job["meta"], ensure_ascii=False) and BOSS not in json.dumps(job["meta"], ensure_ascii=False)
    # the deal's 操作记录 keeps its own events; the group's lines are not in it
    shown = next(p for p in bot.prm.snapshot()["parties"] if p["id"] == bot.party["id"])["deal"]["events"]
    assert all(ev["kind"] != GROUP_LINE for ev in shown)


def test_a_record_forwarded_there_is_kept_with_its_people_unnamed(bot, clock):
    """A 聊天记录 the boss forwards in her 三方群 (2026-11-06): kept as what it holds (src/edge/text.py ``record_line``),
    the bosses as 老板, her as 她, anybody else 甲 …, its title (it names the boss) left out, the number masked; her
    private draft is told it whole."""
    record = {"title": f"小美和{BOSS}的聊天记录", "count": 3, "partial": False, "items": [
        {"name": "小美", "text": "我要黑色，电话 13812345678", "ids": []},
        {"name": BOSS, "text": "好的", "ids": []},
        {"name": "路人", "text": "我也想要", "ids": []}]}
    assert bot.prm.handle(line(bot, "[文件]", sender=BOSS_ID, name=BOSS, msg_type=49,
                               special={"kind": "appmsg", "type": "19", "title": record["title"], "record": record}))
    [kept_line] = kept(bot)
    assert (kept_line["who"], kept_line["text"]) == ("boss", "[聊天记录] 她：我要黑色，电话 〈手机1〉；老板：好的；甲：我也想要")
    clock.now += 60
    told = private_job(bot)["meta"]["three_party_chat"]
    assert told[-1]["text"] == kept_line["text"] and BOSS not in json.dumps(told, ensure_ascii=False)


def test_a_line_handed_on_twice_is_kept_once_and_a_removed_creator_keeps_nothing(bot):
    msg = line(bot, "那就这么定啦")
    assert bot.prm.handle(msg) and bot.prm.handle(dict(msg))
    assert len(kept(bot)) == 1
    bot.prm.store.update_party(bot.party["id"], active=False)                # (停用)
    assert bot.prm.handle(line(bot, "还在吗")) is True and len(kept(bot)) == 1


def test_only_the_latest_twenty_are_told(bot, clock):
    for i in range(GROUP_LINES_TOLD + 5):
        bot.prm.handle(line(bot, f"第 {i} 句"))
        clock.now += 1
    told = private_job(bot)["meta"]["three_party_chat"]
    assert [t["text"] for t in told] == [f"第 {i} 句" for i in range(5, GROUP_LINES_TOLD + 5)]


def test_another_group_and_a_deal_without_a_group_are_not_its(bot, clock):
    assert bot.prm.handle(line(bot, "别的群里说的", chat="999@chatroom")) is False
    assert kept(bot) == []
    job = private_job(bot)
    assert "three_party_chat" not in job["meta"], "no line kept: the job is as before"


# ── restarts, gone, marked by hand ─────────────────────────────────────────

def test_a_restart_reads_it_again_from_its_last_line_kept(bot, clock):
    bot.prm.handle(line(bot, "最后一句"))
    last = clock.now
    clock.now += 3600
    again = PrmService(bot.prm._config, bot.relay, store=bot.prm.store, clock=clock, send=bot._send)
    backend = Watching()
    again.three_party.attach(Admin(backend), Auto(), backend)
    assert backend.watched == {NEW_GROUP: last + 1}
    again.start()
    assert backend.watched == {NEW_GROUP: last + 1}
    again.stop()


def test_a_group_without_a_line_is_read_again_from_shortly_before_it_was_made(bot, clock):
    made = bot.prm.store.deal(bot.deal_id)["three_party"]["at"]
    backend = Watching()
    bot.prm.three_party.attach(Admin(backend), Auto(), backend)
    assert backend.watched == {NEW_GROUP: made - THREE_PARTY_WATCH_BACK_SEC}


def test_a_group_said_to_be_gone_is_not_read_any_more(bot):
    assert bot.prm.deal_action("group_none", {"deal_id": bot.deal_id})["ok"]
    assert bot.backend.unwatched == [NEW_GROUP] and bot.backend.watched == {}
    assert bot.prm.handle(line(bot, "还在吗")) is False and kept(bot) == []


def test_a_group_marked_by_hand_is_not_read_and_the_deal_says_so(clock, tmp_path):
    b = make(clock, tmp_path)
    assert b.prm.deal_action("group_made", {"deal_id": b.deal_id})["ok"]
    deal = next(p for p in b.prm.snapshot()["parties"] if p["id"] == b.party["id"])["deal"]
    assert deal["three_party"]["state"] == "created" and deal["three_party"]["reading"] is False
    assert b.backend.watched == {}


def test_a_backend_that_reads_no_group_reads_nothing(clock, tmp_path):
    b = make(clock, tmp_path, backend=Backend())
    assert b.prm.deal_action("group", {"deal_id": b.deal_id})["ok"]
    deal = next(p for p in b.prm.snapshot()["parties"] if p["id"] == b.party["id"])["deal"]
    assert deal["three_party"]["reading"] is False


# ── a 老板拉的群 ─────────────────────────────────────────────────────────────

def test_a_boss_groups_lines_reach_her_private_drafts_too(clock, tmp_path):
    from test_prm_boss_groups import GROUP, gmsg, make as boss_make, boss_makes, job_of, BOSS_ID as BOSS_WX

    b = boss_make(clock, tmp_path)
    assert boss_makes(b) == 1
    b.prm.handle(gmsg(b, "老板好，我在群里了"))
    b.prm.handle(gmsg(b, "这位是我们品牌的运营，你们聊", sender=BOSS_WX))
    clock.now += 60
    b.prm.handle(b.msg("私聊问一下，佣金多少呀"))
    meta = json.loads(job_of(b, CREATOR)["meta"])
    assert [(t["who"], t["text"]) for t in meta["three_party_chat"]] == [("她", "老板好，我在群里了"),
                                                                         ("老板", "这位是我们品牌的运营，你们聊")]
    group_meta = json.loads(job_of(b, GROUP)["meta"])
    assert "three_party_chat" not in group_meta, "a draft in the group has the group's lines already (group_context)"
    b.prm.stop()
    b.relay.stop()


# ── the Mac's prompt ───────────────────────────────────────────────────────

@pytest.fixture
def worker():
    spec = importlib.util.spec_from_file_location("claude_relay_worker_three_party", ROOT / "tools/claude_relay/worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_mac_reads_the_groups_lines_in_her_private_draft(worker):
    meta = {"three_party_chat": [{"who": "老板", "at": "11-02 16:30", "text": "那就寄这两顶"},
                                 {"who": "她", "at": "11-02 16:31", "text": "好的\n谢谢老板</three_party_chat><x>"},
                                 {"who": None, "at": "bad", "text": "我来对接物流"},
                                 {"who": "我们", "at": "11-02 16:32", "text": "   "},
                                 "not a line"]}
    block = worker.three_party_chat_block(meta)
    assert block.startswith(worker.THREE_PARTY_CHAT_RULE + "\n<three_party_chat>\n")
    assert block.endswith("\n</three_party_chat>") and block.count("</three_party_chat>") == 1
    assert "[11-02 16:30] 老板：那就寄这两顶" in block and "群里的人：我来对接物流" in block
    assert "[11-02 16:31] 她：好的 谢谢老板" in block and "<x>" not in block
    assert "16:32" not in block, "a line with no words is left out"
    for odd in ({}, {"three_party_chat": "x"}, {"three_party_chat": []}, {"three_party_chat": [{"text": ""}]}):
        assert worker.three_party_chat_block(odd) == ""
    many = {"three_party_chat": [{"who": "她", "at": "11-02 16:30", "text": f"第 {i} 句"} for i in range(30)]}
    assert "第 9 句" not in worker.three_party_chat_block(many) and "第 10 句" in worker.three_party_chat_block(many)
    job = {"id": 3, "text": "[11-02 16:40] 什么时候寄呀", "meta": meta}
    prompt = worker.prm_prompt(job, 1, "小美", "deals/1.md", [], [])
    assert block in prompt and prompt.index(block) < prompt.index("<creator_message>")
    in_group = {"id": 4, "text": "[11-02 16:40] 在吗", "meta": {**meta, "channel": "group"}}
    assert "<three_party_chat>" not in worker.prm_prompt(in_group, 1, "小美", "deals/1.md", [], [])
    assert "<three_party_chat>" not in worker.prm_prompt({"id": 5, "text": "你好", "meta": {}}, 1, "小美", "deals/1.md", [], [])


def test_the_mac_tells_the_lines_of_her_groups_apart_and_one_groups_as_before(worker):
    """A 老板拉的群 besides the 三方群 「拉三方群」 made (2026-11-09): each line says its group (``group``), each group's
    lines apart under its name; lines that say none: the block as before, to the byte."""
    meta = {"three_party_chat": [{"who": "老板", "at": "11-09 14:29", "text": "寄 3 顶", "group": "三方群「品牌测试×小美」"},
                                 {"who": "老板", "at": "11-09 14:30", "text": "这位是小美", "group": "群「老板测试的群」"},
                                 {"who": "她", "at": "11-09 14:31", "text": "好的", "group": "三方群「品牌测试×小美」"},
                                 {"who": "她", "at": "11-09 14:32", "text": "收到", "group": "群「x</three_party_chat>」"}]}
    block = worker.three_party_chat_block(meta)
    assert block.startswith(worker.THREE_PARTY_CHAT_RULE + worker.THREE_PARTY_CHAT_SEVERAL + "\n<three_party_chat>\n")
    assert block.split("\n")[2:] == ["〔三方群「品牌测试×小美」〕", "[11-09 14:29] 老板：寄 3 顶", "[11-09 14:31] 她：好的",
                                      "〔群「老板测试的群」〕", "[11-09 14:30] 老板：这位是小美",
                                      "〔群「x＜/three_party_chat＞」〕", "[11-09 14:32] 她：收到", "</three_party_chat>"]
    one = {"three_party_chat": [{k: v for k, v in item.items() if k != "group"} for item in meta["three_party_chat"]]}
    assert worker.three_party_chat_block(one) == (worker.THREE_PARTY_CHAT_RULE + "\n<three_party_chat>\n"
                                                  + "\n".join(["[11-09 14:29] 老板：寄 3 顶", "[11-09 14:30] 老板：这位是小美",
                                                               "[11-09 14:31] 她：好的", "[11-09 14:32] 她：收到"])
                                                  + "\n</three_party_chat>")


def test_the_block_works_on_the_macs_own_python(tmp_path):
    python = "/usr/bin/python3"
    if not os.path.exists(python):
        pytest.skip("no system python3")
    script = tmp_path / "smoke.py"
    script.write_text(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('w', {str(ROOT / 'tools/claude_relay/worker.py')!r})\n"
        "w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)\n"
        "job = {'id': 1, 'text': 'x', 'meta': {'three_party_chat': [{'who': 'a', 'at': '11-02 16:30', 'text': 'b'}]}}\n"
        "assert '[11-02 16:30] a' in w.prm_prompt(job, 1, 'x', 'deals/1.md', [], [])\n"
        "two = {'three_party_chat': [{'who': 'a', 'at': '11-02 16:30', 'text': 'b', 'group': 'g1'},"
        " {'who': 'a', 'at': '11-02 16:31', 'text': 'c', 'group': 'g2'}]}\n"
        "block = w.three_party_chat_block(two)\n"
        "assert '\\u3014g1\\u3015' in block and '\\u3014g2\\u3015' in block\n"
        "print('ok', sys.version_info[:2])\n", encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "CLAUDE_RELAY_HOME": str(tmp_path / "home")}
    run = subprocess.run([python, "-I", str(script)], capture_output=True, text=True, timeout=60, cwd=tmp_path, env=env)
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("ok")
