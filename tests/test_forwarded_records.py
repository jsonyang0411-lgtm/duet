"""A forwarded 「聊天记录」 (WeChat's merged forward, appmsg type 19) read out of its XML (2026-11-06).

Before, the bot saw only its title.  In two 老板拉的群 the boss forwarded his chat with the creator right after pulling
the group, and her draft could only ask her again.  Now the WCDB backend reads the record's messages
(src/wechat/chat_record.py ``parse``: ``special.record``), the edge's line says what is in it
(src/edge/text.py ``record_line``: 「[聊天记录：标题] 小美：想要白色；老板测试：好的」), the draft jobs and the Mac's events
get that line, and where the bosses' names never go to the Mac (a 老板拉的群, her 三方群, a creator he introduced)
PRM calls the people in it 老板 / 她 / 我们 / 甲 … (src/prm/boss_groups.py ``record_msg``).  The message's ``content``
stays 「[文件]」 as before, so the route treats it exactly as before: a record is never a cc request, an admin command or
「同意好友」, whatever its lines say.

Every name and id here is made up.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from router_support import ADMIN
from test_boss_groups_e2e import (BOSS, BOSS_NAME, GROUP, HER, HER_NAME, INTRO, boss_makes, opening_asked)
from test_boss_groups_e2e import World as BossWorld
from test_prm import CREATOR
from test_prm_catchup import World, text_lines

from src.edge import text as T
from src.prm import boss_groups as bg
from src.wechat import chat_record, wcdb_backend
from src.wechat.wcdb_backend import _special_info

ROOT = Path(__file__).resolve().parents[1]
TITLE = "小美和老板测试的聊天记录"
LONG_URL = "https://support.weixin.qq.com/cgi-bin/mmsupport-bin/readtemplate?t=page/favorite_record__w_unsupport"


def record_xml(items, title=TITLE, count=None, des="", how="cdata"):
    """A 聊天记录 as WeChat writes it: ``items`` = (datatype, name or None, words / title, [(tag, id), …]);
    ``how``: the <recordinfo> as CDATA text (WeChat's way), as escaped text, inline XML, or ``broken``."""
    rows = []
    for n, (kind, name, said, ids) in enumerate(items):
        body = (f"<datadesc>{escape(said)}</datadesc>" if kind == "1" else
                f"<datatitle>{escape(said)}</datatitle>" if said else "")
        who = f"<sourcename>{escape(name)}</sourcename>" if name is not None else ""
        source = "".join(f"<{tag}>{value}</{tag}>" for tag, value in ids)
        rows.append(f'<dataitem datatype="{kind}" dataid="id{n}">{body}{who}<sourcetime>2026-11-6 10:2{n % 10}'
                    f"</sourcetime>{f'<dataitemsource>{source}</dataitemsource>' if source else ''}</dataitem>")
    counted = f' count="{count}"' if count is not None else ""
    info = (f"<recordinfo><title>{escape(title)}</title><desc>{escape(des)}</desc>"
            f"<datalist{counted}>{''.join(rows)}</datalist></recordinfo>")
    item = {"cdata": f"<recorditem><![CDATA[{info}]]></recorditem>",
            "escaped": f"<recorditem>{escape(info)}</recorditem>",
            "inline": f"<recorditem>{info}</recorditem>",
            "broken": f"<recorditem><![CDATA[{info[:-20]}]]></recorditem>",
            "none": ""}[how]
    return (f'<?xml version="1.0"?><msg><appmsg appid="" sdkver="0"><title>{escape(title)}</title>'
            f"<des>{escape(des)}</des><type>19</type><url>{escape(LONG_URL)}</url>{item}</appmsg></msg>")


SHE_SAYS = [("1", "小美", "想要白色那顶，电话 13800138000", [("hashusername", "ABC123def456")]),
            ("2", "小美", "", []),
            ("1", "老板测试", "好的，发七退四", [])]


def as_message(xml, **more):
    """A standardized message of the WCDB backend for it (``_special_info`` as the backend calls it)."""
    return {"msg_type": 49, "content": "[文件]", "special": _special_info(xml, 49), **more}


# ── the backend reads it ──────────────────────────────────────────────────

@pytest.mark.parametrize("how", ["cdata", "escaped", "inline"])
def test_the_backend_reads_what_a_record_holds(how):
    info = _special_info(record_xml(SHE_SAYS, how=how), 49)
    assert (info["kind"], info["type"], info["title"], info["url"]) == ("appmsg", "19", TITLE, LONG_URL)
    assert info["record"] == {"title": TITLE, "count": 3, "partial": False, "items": [
        {"name": "小美", "text": "想要白色那顶，电话 13800138000", "ids": ["abc123def456"]},
        {"name": "小美", "text": "[图片]", "ids": []},
        {"name": "老板测试", "text": "好的，发七退四", "ids": []}]}


def test_every_kind_of_message_in_it_has_words():
    items = [("1", "甲方", "多行\n  文字", []), ("2", "甲方", "", []), ("3", "甲方", "", []), ("4", "甲方", "", []),
             ("5", "甲方", "新品测评", []), ("6", "甲方", "某某大厦", []), ("7", "甲方", "一首歌", []),
             ("8", "甲方", "报价单.xlsx", []), ("8", "甲方", "", []), ("15", "甲方", "", []),
             ("17", "甲方", "群聊的聊天记录", []), ("99", "甲方", "新东西", []), ("98", "甲方", "", []),
             ("1", "甲方", "", [])]
    record = _special_info(record_xml(items), 49)["record"]
    assert [i["text"] for i in record["items"]] == [
        "多行 文字", "[图片]", "[语音]", "[视频]", "[链接] 新品测评", "[位置] 某某大厦", "[音乐] 一首歌", "[文件：报价单.xlsx]",
        "[文件]", "[视频]", "[聊天记录] 群聊的聊天记录", "[消息] 新东西", "[消息]", "[消息]"]


def test_missing_fields_are_tolerated():
    # no names, no count, no title in <recordinfo> (the appmsg's), ids of every kind WeChat writes (lower case, once)
    items = [("1", None, "只有话", [("fromusr", "wxid_SomeOne"), ("realchatname", "wxid_someone")]),
             ("1", "", "名字是空的", [])]
    xml = record_xml(items, title="").replace("<title></title>", "<title>群聊的聊天记录</title>", 1)
    record = _special_info(xml, 49)["record"]
    assert record["title"] == "群聊的聊天记录" and record["count"] == 2
    assert [(i["name"], i["text"], i["ids"]) for i in record["items"]] == [
        ("", "只有话", ["wxid_someone"]), ("", "名字是空的", [])]
    assert T.message_text(as_message(xml)) == "[聊天记录：群聊的聊天记录] 只有话；名字是空的"


@pytest.mark.parametrize("how", ["broken", "none"])
def test_without_a_readable_recordinfo_the_preview_is_read_and_else_nothing_changes(how):
    des = "小美: 想要白色\n老板测试：好的\n没有名字的一行"
    info = _special_info(record_xml(SHE_SAYS, des=des, how=how), 49)
    assert info["record"] == {"title": TITLE, "count": 3, "partial": True, "items": [
        {"name": "小美", "text": "想要白色", "ids": []}, {"name": "老板测试", "text": "好的", "ids": []},
        {"name": "", "text": "没有名字的一行", "ids": []}]}
    assert T.message_text(as_message(record_xml(SHE_SAYS, des=des, how=how))) == (
        f"[聊天记录：{TITLE}] 小美：想要白色；老板测试：好的；没有名字的一行（只读到前几条）")
    # nothing readable at all: the line it always was
    info = _special_info(record_xml(SHE_SAYS, how=how), 49)
    assert "record" not in info and info["title"] == TITLE
    assert T.message_text(as_message(record_xml(SHE_SAYS, how=how))) == f"[聊天记录] {TITLE} {LONG_URL}"   # (as before)


def test_a_record_not_read_whole_is_logged_once_without_its_contents(caplog):
    """What to send back from Windows when a record shows only its title: one INFO line with its XML's shape (the tags,
    how long, the tag it starts with), never a name or a word of it, once per record (each poll reads it again)."""
    chat_record._LOGGED.clear()
    xml = record_xml(SHE_SAYS, how="broken")
    with caplog.at_level("INFO", logger="src.wechat.chat_record"):
        for _ in range(3):
            _special_info(xml, 49)
    [line] = [r.getMessage() for r in caplog.records if r.name == "src.wechat.chat_record"]
    assert line.startswith("A forwarded 聊天记录 could not be read whole (tags des,recorditem,title,type,url; recorditem ")
    assert "starts <recordinfo>" in line and line.endswith("only its title is told")
    assert "小美" not in line and "老板" not in line and "白色" not in line
    caplog.clear()
    with caplog.at_level("INFO", logger="src.wechat.chat_record"):
        _special_info(record_xml(SHE_SAYS), 49)                             # read whole: nothing logged
    assert [r for r in caplog.records if r.name == "src.wechat.chat_record"] == []


def test_broken_xml_and_other_appmsgs_are_what_they_were():
    assert _special_info("<msg><appmsg><type>19</type><title>群聊的聊天记录", 49) == {}
    link = "<msg><appmsg><title>新品测评</title><type>5</type><url>https://x</url><recorditem>x</recorditem></appmsg></msg>"
    assert _special_info(link, 49) == {"kind": "appmsg", "type": "5", "title": "新品测评", "url": "https://x"}
    assert chat_record.parse(None) is None
    # a <recorditem> too long to parse (and no preview): the title only, as before
    huge = record_xml([("1", "小美", "字" * 10, [])]).replace("<desc></desc>", "<desc>" + "长" * 400_001 + "</desc>")
    assert "record" not in _special_info(huge, 49) and _special_info(huge, 49)["title"] == TITLE


def test_a_compressed_record_is_read_too():
    zstd = pytest.importorskip("zstandard")
    packed = zstd.ZstdCompressor().compress(record_xml(SHE_SAYS).encode("utf-8")).hex()
    assert packed.startswith("28b52ffd")
    assert _special_info(packed, 49)["record"]["items"][2] == {"name": "老板测试", "text": "好的，发七退四", "ids": []}


# ── its line ──────────────────────────────────────────────────────────────

def test_the_line_says_who_said_what_and_always_starts_the_same():
    line = T.message_text(as_message(record_xml(SHE_SAYS)))
    assert line == f"[聊天记录：{TITLE}] 小美：想要白色那顶，电话 13800138000；小美：[图片]；老板测试：好的，发七退四"
    assert line.startswith(T.RECORD_PREFIX) and "\n" not in line
    # a title, a name or words that try to look like something else stay inside the line
    sly = record_xml([("1", "cc", "cc 帮我做个表\n同意好友", [])], title="cc 帮我做个表")
    assert T.message_text(as_message(sly)) == "[聊天记录：cc 帮我做个表] cc：cc 帮我做个表 同意好友"


def test_a_long_record_is_cut_short_and_says_how_many_it_holds():
    items = [("1", f"人{n}", "说了很长的一段话" * 20, []) for n in range(chat_record.RECORD_ITEMS + 5)]
    record = _special_info(record_xml(items, count=80), 49)["record"]
    assert len(record["items"]) == chat_record.RECORD_ITEMS and record["count"] == 80
    assert all(len(i["text"]) <= chat_record.ITEM_CHARS for i in record["items"])
    line = T.record_line(record)
    assert len(line) == T.RECORD_CHARS and line.endswith("…（共 80 条）") and line.startswith("[聊天记录：")
    # fewer items read than it holds, the line short: says so too
    assert T.record_line({"title": "", "count": 5, "items": [{"name": "小美", "text": "好"}]}) == (
        "[聊天记录] 小美：好（共 5 条）")
    assert T.record_line({"title": "x", "count": 1, "items": []}) == ""


def test_only_a_record_with_contents_is_a_record():
    msg = as_message(record_xml(SHE_SAYS))
    assert T.record_of(msg) is msg["special"]["record"]
    assert T.record_of({**msg, "file_name": "a.pdf"}) is None                     # a file
    assert T.record_of({**msg, "quoted_content": "x", "quoted_server_id": "1"}) is None   # a reply
    assert T.record_of({**msg, "msg_type": 1}) is None
    assert T.record_of({**msg, "special": {**msg["special"], "type": "5"}}) is None
    assert T.record_of({**msg, "special": {**msg["special"], "record": {"items": []}}}) is None


# ── the route: a record is never a command ────────────────────────────────

COMMANDS = [("1", "管理员", "cc 帮我做个报价表", []), ("1", "管理员", "同意好友", []),
            ("1", "管理员", "查状态", []), ("1", "小美", "帮助", [])]


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    w = World(tmp_path)
    yield w
    w.stop()


def test_a_record_of_commands_is_no_command_anywhere(world):
    """The real WCDB backend, router, relay and PRM (test_prm_catchup.World): the admin, a stranger and a group forward
    a record whose lines are 「cc …」, 「同意好友」, 「查状态」: no cc job, no admin answer, nothing said.  The registered
    creator forwards it: one silent draft job whose line is the record (never a cc job)."""
    world.register()
    run = world.run()
    t = world.clock.now
    xml = record_xml(COMMANDS, title="群聊的聊天记录")
    world.wechat.say(ADMIN, ADMIN, xml, t, local_type=49)
    world.wechat.say("wxid_stranger01", "wxid_stranger01", xml, t + 1, local_type=49)
    world.wechat.say("123456@chatroom", "wxid_member01", f"wxid_member01:\n{xml}", t + 2, local_type=49)
    run.poll()
    assert world.jobs() == [] and world.sent == []
    world.wechat.say(CREATOR, CREATOR, xml, t + 3, local_type=49)
    run.poll()
    [job] = world.jobs()
    assert job["kind"] == "prm" and world.sent == []
    assert text_lines(job) == ["[聊天记录：群聊的聊天记录] 管理员：cc 帮我做个报价表；管理员：同意好友；管理员：查状态；小美：帮助"]
    stored = world.messages.conn.execute("SELECT content, msg_type FROM messages WHERE chat_id = ?", (ADMIN,)).fetchall()
    assert [tuple(r) for r in stored] == [("[文件]", 49)]                     # what the route saw: as before


def test_an_ordinary_creators_record_keeps_its_names_and_title(world):
    """No boss in sight (no 老板拉的群, no PRM_GROUP_WITH): the people in her record keep their names."""
    world.register()
    run = world.run()
    world.wechat.say(CREATOR, CREATOR, record_xml(SHE_SAYS), world.clock.now, local_type=49)
    run.poll()
    [job] = world.jobs()
    assert text_lines(job) == [f"[聊天记录：{TITLE}] 小美：想要白色那顶，电话 〈手机1〉；小美：[图片]；老板测试：好的，发七退四"]


# ── 老板拉的群: the boss's name never goes to the Mac ──────────────────────

@pytest.fixture
def boss_world(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wcdb_backend, "VERIFY_PAUSE_SEC", 0)
    w = BossWorld(tmp_path)
    yield w
    w.stop()


def forwards(world, sender, items, at, title=TITLE):
    """A member forwards a 聊天记录 into the group (WeChat 4 writes ``<sender>:\\n`` before a member's XML)."""
    world.db.say(GROUP, sender, f"{sender}:\n{record_xml(items, title=title)}", at, local_type=49)


def test_the_bosss_record_reaches_her_group_opening_with_him_as_laoban(boss_world):
    """The boss pulls her into his group and forwards his chat with her before the bot takes over (the real case): the
    group 开场白's job is told the record, the boss under the nickname he has in it (not PRM_GROUP_WITH's) found as the one
    who forwarded it, her as 她, the phone number masked; neither of his names nor the record's title goes to the Mac."""
    world = boss_world
    run = world.run()
    assert run.prm.boss_groups.scan() == 0
    boss_makes(world)
    forwards(world, BOSS, [("1", HER_NAME, "想要白色那顶，电话 13800138000", []), ("2", HER_NAME, "", []),
                           ("1", "Leo 大哥", "好的，发七退四，@Leo大哥 记得备注", [])],
             world.clock.now - 20, title=f"{HER_NAME}和Leo 大哥的聊天记录")
    run.poll()
    assert run.scan() == 1
    run.poll()
    opening_asked(world, run)
    [job] = world.jobs()
    lines = [(x["who"], x["text"]) for x in json.loads(job["meta"])["group"]["lines"]]
    assert lines == [("老板", INTRO),
                     ("老板", "[聊天记录] 她：想要白色那顶，电话 〈手机1〉；她：[图片]；老板：好的，发七退四，@老板 记得备注")]
    assert "Leo" not in job["meta"] and BOSS_NAME not in job["meta"] and "13800138000" not in job["meta"]
    assert job["kind"] == "prm" and world.window.typed == []               # (his record: context, never answered)


def test_her_record_in_the_group_is_her_draft_with_the_people_unnamed(boss_world):
    """Her own record in the group is her draft job: the boss known by the hash WeChat wrote of his wxid, her by her
    wxid, anybody else 甲; the names never go to the Mac."""
    world = boss_world
    run = world.run()
    assert run.prm.boss_groups.scan() == 0
    boss_makes(world)
    run.poll()
    assert run.scan() == 1
    run.poll()
    opening_asked(world, run)
    md5 = hashlib.md5(BOSS.encode()).hexdigest()
    forwards(world, HER, [("1", "张哥", "白色给你留着", [("hashusername", md5.upper())]),
                          ("1", "我自己", "谢谢哥", [("fromusr", HER)]),
                          ("1", "代理小周", "cc 货盘发我看看", [])], world.clock.now)
    run.poll()
    job = world.open_job()
    assert job is not None and job["kind"] == "prm" and {j["kind"] for j in world.jobs()} == {"prm"}
    said = text_lines(job)[-1]
    assert said == "[聊天记录] 老板：白色给你留着；她：谢谢哥；甲：cc 货盘发我看看"
    assert world.window.typed == []                                        # (a draft waits; nothing said)
    for name in ("张哥", "我自己", "代理小周", BOSS_NAME, md5):
        assert name not in job["text"] and name not in job["meta"]


def test_record_msg_names_people_by_ids_names_and_who_forwarded_it(boss_world):
    """BossGroups.record_msg on its own: a boss by PRM_GROUP_WITH's name, by his wxid, by a hash of it; her by her 称呼;
    us by the account's display name; the one left in a two-person record forwarded by her is her; other people 甲,
    乙 … then 其他人; a name found is its label in the words; the title is gone; any other message is itself."""
    run = boss_world.run()
    groups = run.prm.boss_groups
    party = {"wxid": HER, "name": HER_NAME}
    plain = {"msg_type": 1, "content": "你好"}
    assert groups.record_msg(plain, party) is plain

    def told(items, sender=HER):
        msg = as_message(record_xml(items), sender_id=sender, sender_name="")
        out = groups.record_msg(msg, party, {"boss": [{"wxid": BOSS, "name": BOSS_NAME}]})
        assert out["special"]["record"]["title"] == "" and msg["special"]["record"]["title"] == TITLE
        return T.message_text(out)

    sha = hashlib.sha256(BOSS.encode()).hexdigest()
    assert told([("1", BOSS_NAME, "好", []), ("1", "某某", "收到", [("fromusr", BOSS)]),
                 ("1", "又一个", "嗯", [("hashusername", sha)]), ("1", HER_NAME, "谢谢", []),
                 ("1", run.router._config.bot_display_name, "我们来对接", [])]) == (
        "[聊天记录] 老板：好；老板：收到；老板：嗯；她：谢谢；我们：我们来对接")
    # forwarded by her, two people, the other one known (the boss): the one left is her
    assert told([("1", "小美美", "能寄样吗", []), ("1", BOSS_NAME, "可以，小美美你挑", [])]) == (
        "[聊天记录] 她：能寄样吗；老板：可以，她你挑")
    # forwarded by her, two people, she is known: the other one stays nobody's (not taken for the boss)
    assert told([("1", HER_NAME, "能寄样吗", []), ("1", "小周", "我问问", [])]) == "[聊天记录] 她：能寄样吗；甲：我问问"
    many = [("1", f"路人{n}", "在", []) for n in range(12)]
    assert told(many).split("；")[-3:] == ["癸：在", "其他人：在", "其他人：在"]


def test_the_kept_lines_of_a_record_may_be_longer_than_the_others(boss_world):
    """The boss's record of about 900 characters is kept and told whole; his ordinary line is cut at LINE_CHARS."""
    world = boss_world
    run = world.run()
    assert run.prm.boss_groups.scan() == 0
    boss_makes(world, intro="")
    run.poll()
    assert run.scan() == 1
    run.poll()
    forwards(world, BOSS, [("1", HER_NAME, f"第{n}款想要白色，麻烦帮我留着" * 6, []) for n in range(12)], world.clock.now)
    world.db.line(GROUP, BOSS, "好" * 500, world.clock.now + 1)
    run.poll()
    record, plain = [x["text"] for x in run.prm.boss_groups.job_group(GROUP)["lines"]]
    assert bg.LINE_CHARS < len(record) <= bg.RECORD_LINE_CHARS and record.startswith("[聊天记录] 她：第0款")
    assert record.endswith("…（共 12 条）") and plain == "好" * bg.LINE_CHARS
    assert bg.line_chars(record) == bg.RECORD_LINE_CHARS and bg.line_chars(plain) == bg.LINE_CHARS


# ── the Mac's prompt keeps a record's line longer ─────────────────────────

def _worker():
    import importlib.util
    spec = importlib.util.spec_from_file_location("worker_records", ROOT / "tools/claude_relay/worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_mac_tells_a_record_whole_and_other_lines_cut_as_before():
    w = _worker()
    record = "[聊天记录] " + "她：想要白色；" * 100                      # about 800 characters
    other = "好" * 500
    group = {"channel": "group", "group": {"name": "群", "boss": ["老板"],
                                           "lines": [{"who": "老板", "at": "11-06 10:20", "text": record},
                                                     {"who": "老板", "at": "11-06 10:21", "text": other}]}}
    rows = w.group_context_block(group).split("\n")
    assert f"老板 11-06 10:20：{record}" in rows and f"老板 11-06 10:21：{'好' * w.GROUP_ROW_CHARS}…" in rows
    chat = w.three_party_chat_block({"three_party_chat": [{"who": "老板", "at": "11-06 10:20", "text": record},
                                                          {"who": "她", "at": "11-06 10:21", "text": other}]})
    assert f"[11-06 10:20] 老板：{record}" in chat.split("\n")
    assert f"[11-06 10:21] 她：{'好' * w.THREE_PARTY_CHAT_CHARS}" in chat.split("\n")
    huge = "[聊天记录] " + "长" * 2000
    assert len(w.three_party_chat_block({"three_party_chat": [{"who": "老板", "at": "", "text": huge}]})
               .split("\n")[2]) == len("老板：") + w.RECORD_ROW_CHARS


def test_the_record_rows_work_on_the_macs_own_python(tmp_path):
    python = "/usr/bin/python3"
    if not os.path.exists(python):
        pytest.skip("no system python3")
    record = "[聊天记录] " + "她：想要白色；" * 100
    script = tmp_path / "smoke.py"
    script.write_text(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('w', {str(ROOT / 'tools/claude_relay/worker.py')!a})\n"
        "w = importlib.util.module_from_spec(spec); spec.loader.exec_module(w)\n"
        f"record = {record!a}\n"
        f"meta = {{'channel': 'group', 'group': {{'lines': [{{'who': {'老板'!a}, 'at': '11-06 10:20', 'text': record}}]}}}}\n"
        f"assert {'老板 11-06 10:20：'!a} + record in w.group_context_block(meta).split('\\n')\n"
        "chat = w.three_party_chat_block({'three_party_chat': [{'who': 'x', 'at': '11-06 10:20', 'text': record}]})\n"
        "assert '[11-06 10:20] x\\uff1a' + record in chat.split('\\n')\n"
        "print('ok', sys.version_info[:2])\n", encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "CLAUDE_RELAY_HOME": str(tmp_path / "home")}
    run = subprocess.run([python, "-I", str(script)], capture_output=True, text=True, timeout=60, cwd=tmp_path, env=env)
    assert run.returncode == 0, run.stderr
    assert run.stdout.startswith("ok")
