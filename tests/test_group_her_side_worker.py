"""她那边的人 (the bot's PRM_GROUP_HER_SIDE, 2026-11-11), the Mac side (tools/claude_relay/worker.py): a draft job of a
linked group whose ``meta.group.her_side`` is True is told, right after the group's own line, that the lines of her
message marked 「〔她那边的人〕」 come from her side's other people (her assistant, her 商务) and how to answer them (still
「老师」/「您」, never that person's name, which the Mac is not given).  A line of theirs from before the link comes in
<group_context> as 「她那边」.  A job without the key (an older bot, the switch off) is word for word as before (the
tests of tests/test_prm_worker.py and tests/test_group_link_worker.py keep those), an older worker only ignores the
key, and the worker stays Python 3.9.  Every name is made up."""

import json

from test_group_link_worker import MANUAL, MANUAL_LINE, manual_job
from test_prm_worker import BOSS_GROUP, BOSS_GROUP_LINE, bot, fake_claude, group_job, run_one, settings, worker, workspace  # noqa: F401

from src.prm import boss_groups as bg

MARK = "〔她那边的人〕"
SIDE_TEXT = f"[11-11 14:20] 样品收到啦\n[11-11 14:21] {MARK}帽子到了，上一下链接"


def test_the_bot_and_the_worker_mark_her_sides_lines_with_the_same_words(worker):
    assert worker.HER_SIDE_MARK == bg.HER_SIDE_MARK == MARK
    assert bg.HER_SIDE in worker.GROUP_WHO_MANUAL and MARK in worker.HER_SIDE_LINE


def test_a_job_that_says_her_side_is_drafted_too_gets_one_line_after_the_groups(worker):
    for group, line in (({**MANUAL, "her_side": True}, MANUAL_LINE), ({**BOSS_GROUP, "her_side": True}, BOSS_GROUP_LINE)):
        lines = worker.prm_prompt(group_job(text=SIDE_TEXT, group=group), 1, "小美", "deals/1.md", [], []).split("\n")
        assert lines[1] == line and lines[2] == worker.HER_SIDE_LINE
        assert lines.count(worker.HER_SIDE_LINE) == 1
        said = lines[lines.index("<creator_message>") + 1:lines.index("</creator_message>")]
        assert said == SIDE_TEXT.split("\n")                          # her message as it is, the mark kept
    for word in ("「老师」「您」", "不要猜这个人是谁", "needs_attention 设 true", "她那边的人说的"):
        assert word in worker.HER_SIDE_LINE, word
    assert "\n" not in worker.HER_SIDE_LINE


def test_without_the_key_or_with_another_value_the_prompt_is_as_before(worker):
    """An older bot (no key), the switch off (no key), or anything that is not True: no line, the prompt word for word
    as it was; a mark a creator typed herself changes nothing either."""
    plain = worker.prm_prompt(manual_job(text=SIDE_TEXT), 1, "小美", "deals/1.md", [], [])
    assert worker.HER_SIDE_LINE not in plain and plain.split("\n")[1] == MANUAL_LINE
    for odd in (False, None, 1, "true", "on", {"count": 1}):
        told = worker.prm_prompt(manual_job(text=SIDE_TEXT, group={**MANUAL, "her_side": odd}), 1, "小美", "deals/1.md",
                                 [], [])
        assert told == plain, odd
    assert worker.her_side_line({"channel": "group", "group": {"her_side": True}}) == worker.HER_SIDE_LINE
    assert worker.her_side_line({"group": {"her_side": True}}) == ""              # (not a group job)
    assert worker.her_side_line({"channel": "buyin", "group": {"her_side": True}}) == ""
    assert worker.her_side_line({"channel": "group", "group": "her_side"}) == ""
    private = {"id": 7, "text": SIDE_TEXT, "meta": {"party_id": 1, "deal_id": 1, "name": "小美"}}
    assert worker.HER_SIDE_LINE not in worker.prm_prompt(private, 1, "小美", "deals/1.md", [], [])


def test_her_sides_lines_from_before_the_link_are_told_as_such_in_the_context(worker):
    group = {**MANUAL, "her_side": True, "lines": [
        {"who": "她那边", "at": "11-11 13:40", "text": "我是小美的助理，以后对接找我"},
        {"who": "我们", "at": "11-11 13:42", "text": "好的～"}]}
    block = worker.group_context_block(manual_job(group=group)["meta"]).split("\n")
    assert block[1:] == ["<group_context>", "她那边 11-11 13:40：我是小美的助理，以后对接找我", "我们 11-11 13:42：好的～",
                         "</group_context>"]
    # a 老板拉的群 has no such rows (no catch-up there): told as 群成员, as any label it does not know
    boss = worker.group_context_block(group_job(group={**BOSS_GROUP, "lines": group["lines"]})["meta"]).split("\n")
    assert boss[2] == "群成员 11-11 13:40：我是小美的助理，以后对接找我"


def test_the_line_reaches_claude_through_a_real_job(worker, bot, fake_claude, tmp_path, workspace):  # noqa: F811
    """End to end on the Mac's side: the bot's job (its meta as PrmService makes it) through the worker to the fake
    claude: the prompt it got has the line once, and the mark in her message."""
    meta = {"party_id": 1, "deal_id": bot.prm.store.deal_of(1)["id"], "name": "小美", "channel": "group",
            "group": {**MANUAL, "her_side": True}}
    bot.relay.submit("40000000001@chatroom", "wxid_creator01", SIDE_TEXT, kind="prm", meta=meta, fresh=True)
    run_one(worker, bot, settings(bot, fake_claude, tmp_path, prm_workspace=str(workspace)))
    prompt = json.dumps(fake_claude.calls(), ensure_ascii=False)
    assert prompt.count("都当成她这一方在问") == 1 and MARK + "帽子到了" in prompt
