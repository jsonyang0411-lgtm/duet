"""Tests for the offline evaluation harness in ``evals/``.

Two kinds of checks live here:

* data / metric / hygiene checks that must hold forever (schema validity,
  determinism, no files outside a temp dir, metric definitions);
* a few *wiring* checks that build tiny cases and pin today's router behaviour
  (explicit @ engages, stale @ is ignored, priming registers the real
  follow-up tracker, ...).  They prove the fake clock / priming plumbing works
  and must be revisited when routing is changed on purpose.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from pathlib import Path

import pytest

import src.router as router_module
from evals import (
    ACTIONS,
    ADDRESSEES,
    SCHEMA,
    STRENGTHS,
    load_jsonl,
    normalize_case,
    validate_case,
)
from evals import metrics, run_offline

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "evals" / "cases" / "scenarios_v1.jsonl"
RAW_OUTPUTS = ROOT / "evals" / "cases" / "raw_outputs_v1.jsonl"

REQUIRED_CATEGORIES = {
    "direct_at", "quote_bot", "name_call", "keyword", "followup_to_bot",
    "sticky_after_empty_at", "addressed_other_by_at", "quote_other_member",
    "between_others", "ambient_open", "ambient_closed_topic", "media_only",
    "stale_at", "private", "todo_command", "lots_command", "help_command",
    "slang_alias", "meta_about_bot", "short_reaction", "busy_group",
}
YANG = {"chat_persona_profile": "yang_yujing", "yang_yujing_group_name": "示例群"}


# ── helpers ──────────────────────────────────────────────────────────────

def _case(cid="t-1", *, category="direct_at", content="@小助手 你好", context=(),
          at=None, off=0, bot_state=None, group=True, sender=("u_b", "小红"),
          expect=("speak", "must", "bot"), bot="小助手", overrides=None,
          quoted="", quoted_name="", reply_to_bot=False, msg_type=1):
    if at is None:
        at = bool(group and f"@{bot}" in content) or reply_to_bot
    return {
        "schema": SCHEMA, "id": cid, "category": category,
        "chat": {"chat_id": "g_t@chatroom" if group else "wxid_t",
                 "group_name": "示例群", "is_group": group},
        "bot_name": bot, "now": 1_760_000_000,
        "context": [
            {"offset_sec": x[0], "sender_id": x[1], "sender_name": x[2],
             "content": x[3], "msg_type": x[4] if len(x) > 4 else 1}
            for x in context
        ],
        "trigger": {
            "sender_id": sender[0], "sender_name": sender[1], "content": content,
            "msg_type": msg_type, "is_at_mentioned": at,
            "is_reply_to_bot": reply_to_bot, "quoted_content": quoted,
            "quoted_sender_name": quoted_name, "offset_sec": off,
        },
        "next_messages": [], "bot_state": bot_state,
        "expect": None if expect is None else {
            "action": expect[0], "strength": expect[1], "addressee": expect[2],
            "note": "test",
        },
        "config_overrides": overrides or {}, "source": "test",
    }


def _bot_state(to="u_b", text="嗯，在的", off=-10, sticky=True):
    return {
        "last_bot_reply": {"to_sender_id": to, "text": text, "offset_sec": off},
        "sticky_for": [to] if sticky else [],
    }


def _replay(*cases, seed=7):
    out = run_offline.run_routing(list(cases), seed=seed)
    return {c["id"]: c for c in out["cases"]}


@pytest.fixture(scope="module")
def scenarios():
    return load_jsonl(SCENARIOS)


@pytest.fixture(scope="module")
def routing_report():
    cases = run_offline.load_cases([SCENARIOS])
    return run_offline.run_routing(cases, seed=7)


# ── schema and data hygiene ──────────────────────────────────────────────

def test_scenarios_validate_strictly(scenarios):
    for case in scenarios:
        assert validate_case(case) == [], case.get("id")
        assert case["schema"] == SCHEMA


def test_scenarios_are_numerous_unique_and_cover_the_categories(scenarios):
    ids = [c["id"] for c in scenarios]
    categories = {c["category"] for c in scenarios}
    assert len(scenarios) >= 90
    assert len(set(ids)) == len(ids)
    assert len(categories) >= 20
    assert REQUIRED_CATEGORIES <= categories
    # every category has several cases, and every kind of expectation occurs
    per_category = {c: 0 for c in categories}
    for case in scenarios:
        per_category[case["category"]] += 1
    assert min(per_category.values()) >= 3
    assert {c["expect"]["action"] for c in scenarios} == set(ACTIONS)
    assert {c["expect"]["strength"] for c in scenarios} == set(STRENGTHS)
    assert {c["expect"]["addressee"] for c in scenarios} == set(ADDRESSEES)
    assert all(c["source"] == "handwritten" for c in scenarios)


def test_scenarios_use_the_full_feed_overrides_and_private_chats(scenarios):
    overridden = [c for c in scenarios if c["config_overrides"]]
    assert len(overridden) >= 3
    assert all(c["config_overrides"] == YANG for c in overridden)
    assert all(c["chat"]["group_name"] == "示例群" for c in overridden)
    private = [c for c in scenarios if not c["chat"]["is_group"]]
    assert len(private) >= 4


def test_scenarios_are_internally_consistent(scenarios):
    for case in scenarios:
        cid = case["id"]
        trig, chat, bot = case["trigger"], case["chat"], case["bot_name"]
        # backend rule: a mention is a quote of the bot or the literal @name
        expected_at = bool(
            trig["is_reply_to_bot"]
            or (chat["is_group"] and f"@{bot}" in trig["content"])
        )
        assert trig["is_at_mentioned"] == expected_at, cid
        if trig["is_reply_to_bot"]:
            assert trig["quoted_sender_name"] == bot, cid
        assert trig["sender_name"] != bot, cid
        offsets = [m["offset_sec"] for m in case["context"]]
        assert offsets == sorted(offsets), cid
        assert all(o <= trig["offset_sec"] for o in offsets), cid
        assert all(
            m["offset_sec"] >= trig["offset_sec"] for m in case["next_messages"]
        ), cid
        state = case["bot_state"]
        if state and state["last_bot_reply"]:
            last = state["last_bot_reply"]
            assert last["offset_sec"] <= trig["offset_sec"], cid
            assert any(
                m["sender_name"] == bot and m["content"] == last["text"]
                and m["offset_sec"] == last["offset_sec"]
                for m in case["context"]
            ), f"{cid}: last_bot_reply is not mirrored in the context"
        expect = case["expect"]
        assert (expect["action"] == "either") == (expect["strength"] == "free"), cid
        assert expect["note"].strip(), cid
        if not chat["is_group"]:
            assert not trig["is_at_mentioned"], cid


def test_scenarios_contain_no_obvious_personal_data(scenarios):
    blob = SCENARIOS.read_text(encoding="utf-8")
    assert not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", blob)          # phone
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", blob)           # e-mail
    assert "wxid_" not in blob and "http" not in blob


def test_validate_case_rejects_broken_cases(scenarios):
    good = scenarios[0]
    assert validate_case(good) == []

    def broken(mutate):
        case = copy.deepcopy(good)
        mutate(case)
        return validate_case(case)

    assert broken(lambda c: c.update(schema="replay-case/v0"))
    assert broken(lambda c: c.pop("trigger"))
    assert broken(lambda c: c.update(typo_key=1))
    assert broken(lambda c: c["expect"].update(action="reply"))
    assert broken(lambda c: c["expect"].update(strength="maybe"))
    assert broken(lambda c: c["expect"].update(addressee="everyone"))
    assert broken(lambda c: c["trigger"].update(is_at_mentioned="yes"))
    assert broken(lambda c: c["chat"].update(is_group="true"))
    assert broken(lambda c: c.update(now="1760000000"))
    assert broken(lambda c: c["context"].append({"offset_sec": -1}))
    assert broken(lambda c: c.update(bot_state={"last_bot_reply": None}))
    assert validate_case("not an object")
    # non-strict mode tolerates unknown keys
    extra = copy.deepcopy(good)
    extra["typo_key"] = 1
    assert validate_case(extra, strict=False) == []


def test_exporter_style_cases_normalize_and_validate():
    exported = {
        "schema": SCHEMA, "id": "exp-000001", "category": "unlabeled",
        "chat": {"chat_id": "g_ab@chatroom", "group_name": "示例群1", "is_group": True},
        "bot_name": "小助手", "now": 1_700_000_000,
        "context": [{"offset_sec": -5, "sender_id": "u_aa", "sender_name": "用户A",
                     "content": "今晚吃啥", "msg_type": 1}],
        "trigger": {"sender_id": "u_bb", "sender_name": "用户B", "content": "火锅吧",
                    "msg_type": 1, "is_at_mentioned": False,
                    "is_reply_to_bot": None, "quoted_content": "",
                    "quoted_sender_name": "", "offset_sec": 0},
        "next_messages": [{"offset_sec": 3, "sender_id": "u_aa",
                           "sender_name": "用户A", "content": "好"}],
        "bot_state": None, "expect": None, "source": "export",
    }
    assert validate_case(exported)  # strict fixture rules reject it
    case = normalize_case(exported)
    assert validate_case(case, allow_unlabeled=True) == []
    assert case["trigger"]["is_reply_to_bot"] is False
    assert case["config_overrides"] == {}
    assert case["next_messages"][0]["msg_type"] == 1
    assert exported["trigger"]["is_reply_to_bot"] is None  # input untouched

    labeled = copy.deepcopy(exported)
    labeled["expect"] = {"action": "speak", "strength": "should",
                         "addressee": None, "note": ""}
    assert validate_case(normalize_case(labeled), allow_unlabeled=True) == []

    result = _replay(case)["exp-000001"]
    assert result["expect"] is None
    assert result["verdict"] == "unlabeled"
    assert result["outcome"] in metrics.OUTCOMES


def test_load_cases_skips_bad_and_rejects_duplicates(tmp_path):
    good = _case("ok-1")
    bad = _case("bad-1", category="bad", expect=None)
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in (good, bad)) + "\n",
        encoding="utf-8",
    )
    loaded = run_offline.load_cases([path])
    assert [c["id"] for c in loaded] == ["ok-1"]

    dup = tmp_path / "dup.jsonl"
    dup.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in (good, good)) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate"):
        run_offline.load_cases([dup])

    invalid = tmp_path / "invalid.jsonl"
    broken = copy.deepcopy(good)
    broken["expect"]["action"] = "shout"
    invalid.write_text(json.dumps(broken, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid"):
        run_offline.load_cases([invalid])


def test_raw_output_rows_are_well_formed():
    rows = load_jsonl(RAW_OUTPUTS)
    assert 50 <= len(rows) <= 80
    assert len({r["id"] for r in rows}) == len(rows)
    kinds = {r["kind"] for r in rows}
    assert {"clean", "leak_meta", "paren_aside", "too_long", "prefix",
            "punct_only", "marker_false_kill"} <= kinds
    for row in rows:
        assert isinstance(row["raw"], str) and row["raw"]
        assert isinstance(row["expect_silent"], bool)
        assert (row["expect_text"] is None) == row["expect_silent"]
        assert isinstance(row["max_chars"], int) and row["max_chars"] > 0
    assert any(r["expect_silent"] for r in rows)
    assert any(not r["expect_silent"] for r in rows)


# ── determinism, speed, hygiene ──────────────────────────────────────────

def test_routing_replay_is_deterministic(scenarios, routing_report):
    cases = run_offline.load_cases([SCENARIOS])
    again = run_offline.run_routing(cases, seed=7)
    assert json.dumps(again, sort_keys=True, ensure_ascii=False) == json.dumps(
        routing_report, sort_keys=True, ensure_ascii=False,
    )
    assert len(routing_report["cases"]) == len(scenarios)
    assert routing_report["summary"]["outcomes"]["error"] == 0
    assert all("warnings" not in c for c in routing_report["cases"])


def test_full_report_is_identical_across_runs_and_fast():
    started = time.perf_counter()
    first = run_offline.build_report(seed=7)
    second = run_offline.build_report(seed=7)
    elapsed = time.perf_counter() - started
    assert run_offline.canonical_json(first) == run_offline.canonical_json(second)
    assert elapsed < 15.0
    assert {"routing", "cleaning", "gate", "not_measured_offline"} <= set(first)
    assert first["inputs"]["n_cases"] >= 90


def test_cli_writes_json_and_markdown(tmp_path, capsys):
    code = run_offline.main(["--out", str(tmp_path), "--seed", "7"])
    assert code == 0
    report = json.loads((tmp_path / "current.json").read_text("utf-8"))
    assert report["determinism"]["identical"] is True
    assert report["schema"] == run_offline.BASELINE_SCHEMA
    markdown = (tmp_path / "current.md").read_text("utf-8")
    assert "Headline numbers" in markdown
    assert "Not measured offline" in markdown
    out = capsys.readouterr().out
    assert "addressing recall" in out and "determinism" in out


def test_replay_leaves_no_files_and_restores_global_state(tmp_path, monkeypatch):
    repo_data = ROOT / "data"
    before = set(os.listdir(repo_data)) if repo_data.is_dir() else set()
    real_time = time.time
    router_time = router_module.time
    monkeypatch.chdir(tmp_path)

    cases = run_offline.load_cases([SCENARIOS])[:25]
    run_offline.run_routing(cases, seed=7)

    assert list(tmp_path.iterdir()) == []                 # cwd untouched
    assert Path.cwd() == tmp_path
    after = set(os.listdir(repo_data)) if repo_data.is_dir() else set()
    artefacts = {"decision_trace.jsonl", "llm_calls.jsonl", "vision_cache",
                 "messages.db", "lots.json", "group_names.json"}
    assert not ((after - before) & artefacts)
    assert time.time is real_time                          # no global patch
    assert router_module.time is router_time               # module swap undone


def test_unknown_config_override_is_an_error():
    case = _case("t-bad", overrides={"no_such_option": 1})
    with pytest.raises(ValueError, match="no_such_option"):
        run_offline.run_routing([case], seed=7)


# ── routing wiring (pins today's router; see module docstring) ───────────

def test_explicit_mention_engages_and_unrelated_chatter_does_not():
    res = _replay(
        _case("at", content="@小助手 你好", context=[(-30, "u_a", "阿明", "吃了吗")]),
        _case("plain", content="今天好热啊", context=[(-30, "u_a", "阿明", "吃了吗")],
              expect=("silent", "should", "group")),
    )
    assert res["at"]["outcome"] == "engaged_chat"
    assert res["at"]["route"] == "at_mention"
    assert res["at"]["model_calls"] == ["chat"]
    assert res["at"]["verdict"] == "ok"
    assert res["plain"]["outcome"] == "none"
    assert res["plain"]["verdict"] == "ok"


def test_fake_clock_drives_the_stale_mention_guard():
    res = _replay(
        _case("fresh", content="@小助手 讲个笑话", off=-299),
        _case("stale", content="@小助手 讲个笑话", off=-301,
              expect=("silent", "must", "bot")),
    )
    assert res["fresh"]["outcome"] == "engaged_chat"
    assert res["stale"]["outcome"] == "none"


def test_priming_registers_the_real_followup_and_sticky_trackers():
    question = "《晴天》《七里香》《稻香》，你想听哪首？"
    ctx = [(-20, "u_bot", "小助手", question)]
    primed = _case(
        "primed", content="稻香", sender=("u_c", "大伟"), context=ctx,
        bot_state=_bot_state("u_b", question, -20, sticky=True),
        expect=("speak", "should", "bot"),
    )
    unprimed = _case(
        "unprimed", content="稻香", sender=("u_c", "大伟"), context=[],
        bot_state=None, expect=("speak", "should", "bot"),
    )
    late = _case(
        "late", content="稻香", sender=("u_c", "大伟"),
        context=[(-150, "u_bot", "小助手", question)],
        bot_state=_bot_state("u_b", question, -150, sticky=False),
        expect=("speak", "should", "bot"),
    )
    res = _replay(primed, unprimed, late)
    assert res["primed"]["outcome"] == "engaged_chat"
    assert res["primed"]["route"] == "followup"
    assert res["unprimed"]["outcome"] == "none"
    assert res["late"]["outcome"] == "none"      # follow-up window (60 s) expired


def test_sticky_after_bare_mention_and_its_expiry():
    near = _case(
        "near", content="帮我想个周末活动", sender=("u_b", "小红"),
        context=[(-12, "u_b", "小红", "@小助手")],
    )
    far = _case(
        "far", content="帮我想个周末活动", sender=("u_b", "小红"),
        context=[(-100, "u_b", "小红", "@小助手")],
        expect=("speak", "should", "bot"),
    )
    other = _case(
        "other", content="帮我想个周末活动", sender=("u_c", "大伟"),
        context=[(-12, "u_b", "小红", "@小助手")],
        expect=("silent", "should", "group"),
    )
    res = _replay(near, far, other)
    assert res["near"]["outcome"] == "engaged_chat" and res["near"]["route"] == "sticky"
    assert res["far"]["outcome"] == "none"
    assert res["other"]["outcome"] == "none"      # sticky is per sender


def test_keyword_private_and_local_routes():
    res = _replay(
        _case("kw", content="总结一下", expect=("speak", "must", "bot")),
        _case("private", content="在吗", group=False, sender=("wxid_t", "小明")),
        _case("lots", content="@小助手 抽签", category="lots_command"),
        _case("help", content="@小助手 帮助", category="help_command"),
    )
    assert (res["kw"]["outcome"], res["kw"]["route"]) == ("engaged_chat", "keyword")
    assert (res["private"]["outcome"], res["private"]["route"]) == ("engaged_chat", "private")
    assert (res["lots"]["outcome"], res["lots"]["route"]) == ("handled_locally", "local:lots")
    assert (res["help"]["outcome"], res["help"]["route"]) == ("handled_locally", "local:help")
    assert res["lots"]["model_calls"] == []


def test_proactive_path_is_evaluated_only_for_the_trigger():
    dense = [(-90 + 15 * i, f"u_{i % 3}", f"用户{i % 3}", f"聊天内容{i}") for i in range(6)]
    res = _replay(
        _case("dense", content="哈哈哈哈", context=dense,
              expect=("either", "free", "group")),
        _case("sparse", content="哈哈哈哈", context=[],
              expect=("either", "free", "group")),
    )
    assert res["dense"]["outcome"] == "engaged_proactive"
    assert res["dense"]["model_calls"] == ["proactive_chat"]
    assert res["dense"]["gate"]["mode"] in {"QUIET", "CASUAL", "LIVELY", "BURST"}
    assert 0 < res["dense"]["gate"]["reply_probability"] <= 0.7
    assert res["sparse"]["outcome"] == "none"
    assert "SLEEP" in res["sparse"]["gate_reason"]


def test_full_feed_persona_override_changes_the_route():
    topic = "鸣潮这版本的新角色你们抽了吗"
    default = _case("default", content=topic, expect=("either", "free", "group"))
    yang = _case("yang", content=topic, expect=("either", "free", "group"),
                 overrides=YANG)
    reaction = _case("short", content="哈哈", expect=("silent", "should", "nobody"),
                     overrides=YANG)
    res = _replay(default, yang, reaction)
    assert res["default"]["outcome"] == "none"
    assert (res["yang"]["outcome"], res["yang"]["route"]) == ("engaged_chat", "full_feed")
    assert res["short"]["outcome"] == "none"


def test_media_message_uses_the_vision_route_when_asked():
    res = _replay(
        _case("ask", content="@小助手 这张图里写的什么",
              context=[(-40, "u_a", "阿明", "[图片]", 3)]),
    )
    assert res["ask"]["outcome"] == "engaged_vision"
    assert res["ask"]["route"] == "image_question"


def test_busy_group_context_ends_with_the_message_being_answered():
    # 80 lines in the last 10 minutes: more than the router's context window.
    chatter = [(-600 + 7 * i, "u_a", "阿明", f"闲聊第{i}句") for i in range(80)]
    res = _replay(
        _case("busy", content="@小助手 在吗", context=chatter),
        _case("quiet", content="@小助手 在吗", context=chatter[:3]),
        _case("local", category="todo_command", content="查看待办", context=chatter[:3]),
    )
    assert res["busy"]["context_has_trigger"] is True
    assert res["quiet"]["context_has_trigger"] is True
    assert res["local"]["context_has_trigger"] is None        # model never called


def test_shipped_busy_group_scenarios_keep_the_trigger_in_the_model_context(
        scenarios, routing_report):
    busy = [c for c in scenarios if c["category"] == "busy_group"]
    assert len(busy) >= 4
    assert all(len(c["context"]) >= 50 for c in busy)
    context = routing_report["summary"]["context"]
    assert context["missing_ids"] == []
    assert context["latest_in_context"]["n"] >= len(busy)


# ── metrics on hand-made input ───────────────────────────────────────────

def _res(cid, category, action, strength, outcome, *, route="at_mention",
         addressing="explicit", local_empty=False, labeled=True,
         context_has_trigger=None):
    return {
        "id": cid, "category": category, "outcome": outcome, "route": route,
        "addressing": addressing, "local_empty": local_empty,
        "context_has_trigger": context_has_trigger,
        "expect": {"action": action, "strength": strength, "addressee": "bot"}
        if labeled else None,
    }


def test_routing_summary_on_a_tiny_hand_made_set():
    results = [
        _res("c1", "direct_at", "speak", "must", "engaged_chat"),
        _res("c2", "name_call", "speak", "must", "none", route="none",
             addressing="implicit"),
        _res("c3", "todo_command", "speak", "must", "none", route="local:empty_reply",
             local_empty=True),
        _res("c4", "keyword", "silent", "should", "engaged_chat", route="keyword",
             addressing="implicit"),
        _res("c5", "between_others", "silent", "must", "engaged_proactive",
             route="proactive", addressing="implicit"),
        _res("c6", "between_others", "silent", "must", "none", route="none",
             addressing="implicit"),
        _res("c7", "direct_at", "speak", "should", "handled_locally",
             route="local:tarot"),
        _res("c8", "media_only", "either", "free", "engaged_vision",
             route="image_followup"),
        _res("c9", "unlabeled", "x", "x", "engaged_chat", labeled=False),
    ]
    s = metrics.routing_summary(results)
    assert s["n_cases"] == 9
    assert s["outcomes"]["engaged_chat"] == 3 and s["outcomes"]["none"] == 3
    rec = s["addressing_recall"]
    assert (rec["speak_must"]["k"], rec["speak_must"]["n"]) == (1, 3)
    assert (rec["speak_any"]["k"], rec["speak_any"]["n"]) == (2, 4)
    fe = s["false_engagement"]
    assert (fe["silent_any"]["deterministic"]["k"], fe["silent_any"]["deterministic"]["n"]) == (1, 3)
    assert fe["silent_any"]["incl_proactive"]["k"] == 2
    assert fe["silent_any"]["proactive_open_only"] == 1
    assert (fe["silent_must"]["deterministic"]["k"], fe["silent_must"]["deterministic"]["n"]) == (0, 2)
    assert fe["silent_must"]["incl_proactive"]["k"] == 1
    assert fe["either_engaged"]["k"] == 1
    ce = s["cannot_engage"]
    assert ce["ids"] == ["c2"]                       # c3 matched a local route
    assert (ce["speak_must"]["k"], ce["speak_must"]["n"]) == (1, 3)
    assert s["local_empty_reply"] == {"n": 1, "ids": ["c3"]}
    assert s["unexpected_local"] == {"n": 1, "ids": ["c7"]}
    assert s["unlabeled"]["n"] == 1
    assert s["unlabeled"]["engaged_always_on"]["k"] == 1
    assert s["false_engagement_ids"] == ["c4", "c5"]
    assert s["missed_must_ids"] == ["c2", "c3"]
    assert s["by_addressing"]["explicit"]["n"] == 5      # incl. the unlabeled row
    assert s["by_route"]["keyword"]["unwanted"] == 1
    assert s["by_route"]["proactive"]["unwanted"] == 1
    assert s["by_category"]["between_others"]["silent_engaged"]["k"] == 1
    assert s["expect_vs_outcome"]["unlabeled"]["engaged_chat"] == 1


def test_routing_summary_lists_cases_whose_model_context_misses_the_trigger():
    results = [
        _res("c1", "busy_group", "speak", "must", "engaged_chat",
             context_has_trigger=True),
        _res("c2", "busy_group", "speak", "must", "engaged_chat",
             context_has_trigger=False),
        _res("c3", "todo_command", "speak", "must", "handled_locally",
             route="local:todo"),                      # no model call: not counted
    ]
    context = metrics.routing_summary(results)["context"]
    assert (context["latest_in_context"]["k"], context["latest_in_context"]["n"]) == (1, 2)
    assert context["missing_ids"] == ["c2"]


def test_routing_summary_handles_empty_input():
    s = metrics.routing_summary([])
    assert s["n_cases"] == 0
    assert s["addressing_recall"]["speak_must"]["rate"] is None
    assert metrics.fmt_ratio(s["addressing_recall"]["speak_must"]) == "0/0 (n/a)"


def test_ratio_and_distribution_helpers():
    assert metrics.ratio(1, 4) == {"k": 1, "n": 4, "rate": 0.25}
    assert metrics.ratio(0, 0)["rate"] is None
    assert metrics.fmt_ratio(metrics.ratio(1, 3)) == "1/3 (33.3%)"
    dist = metrics.distribution({"A": 1, "B": 3}, ["A", "B", "C"])
    assert dist == {"A": 0.25, "B": 0.75, "C": 0.0}
    assert metrics.distribution({}, ["A"]) == {"A": 0.0}


def test_clean_row_classification_and_summary():
    classify = metrics.classify_clean_row
    ok = classify(expect_silent=False, expect_text="好的", out="好的", full="好的", max_chars=30)
    leak = classify(expect_silent=True, expect_text=None, out="沉默。", full="沉默。", max_chars=30)
    kill = classify(expect_silent=False, expect_text="不用回复我", out="", full="", max_chars=30)
    mangled = classify(expect_silent=False, expect_text="晚上7:30见", out="30见",
                       full="30见", max_chars=30)
    cut_text = "一二三四五六七八九十，一二三四五六七八九十，一二三四五六七八九十，尾"
    cut = classify(expect_silent=False, expect_text="一二三四五六七八九十",
                   out=cut_text[:30], full=cut_text, max_chars=30)
    boundary = classify(expect_silent=False, expect_text=cut_text[:10],
                        out=cut_text[:10], full=cut_text, max_chars=10)
    assert ok["status"] == "ok" and not ok["truncated"]
    assert leak["status"] == "leak"
    assert kill["status"] == "false_kill"
    assert mangled["status"] == "mangled"
    assert cut["status"] == "truncated" and cut["mid_sentence_truncation"]
    assert boundary["status"] == "ok" and boundary["truncated"]

    rows = [
        {"id": "a", "kind": "clean", "expect_silent": False, **ok},
        {"id": "b", "kind": "leak_meta", "expect_silent": True, **leak},
        {"id": "c", "kind": "marker_false_kill", "expect_silent": False, **kill},
        {"id": "d", "kind": "prefix", "expect_silent": False, **mangled},
        {"id": "e", "kind": "too_long", "expect_silent": False, **cut},
        {"id": "f", "kind": "punct_only", "expect_silent": True,
         **classify(expect_silent=True, expect_text=None, out="", full="", max_chars=30)},
    ]
    s = metrics.clean_summary(rows)
    assert (s["leak_rate"]["k"], s["leak_rate"]["n"]) == (1, 2)
    assert (s["false_kill_rate"]["k"], s["false_kill_rate"]["n"]) == (1, 4)
    assert (s["mangled_rate"]["k"], s["mangled_rate"]["n"]) == (2, 4)
    assert (s["exact_match_rate"]["k"], s["exact_match_rate"]["n"]) == (2, 6)
    assert s["truncated_rows"] == 1 and s["mid_sentence_truncations"] == 1
    assert s["leak_ids"] == ["b"] and s["false_kill_ids"] == ["c"]
    assert s["by_kind"]["clean"] == {"n": 1, "ok": 1}


def test_cleaning_replay_uses_the_production_cleaner():
    rows = [
        {"id": "r1", "raw": "嗯，在的", "expect_text": "嗯，在的",
         "expect_silent": False, "kind": "clean", "max_chars": 30},
        {"id": "r2", "raw": "（沉默，不需要回复）", "expect_text": None,
         "expect_silent": True, "kind": "leak_meta", "max_chars": 30},
        {"id": "r3", "raw": "林小满：好呀", "expect_text": "好呀",
         "expect_silent": False, "kind": "prefix", "max_chars": 30,
         "names": ["林小满"]},
    ]
    out = run_offline.run_cleaning(rows)
    assert [r["status"] for r in out["rows"]] == ["ok", "ok", "ok"]
    assert out["rows"][1]["silence_flag"] is True
    assert out["summary"]["exact_match_rate"]["k"] == 3


def test_baseline_cleaning_numbers_match_the_summary_rows():
    report = run_offline.run_cleaning(load_jsonl(RAW_OUTPUTS))
    rows, summary = report["rows"], report["summary"]
    assert summary["n_rows"] == len(rows)
    assert summary["leak_rate"]["k"] == sum(r["status"] == "leak" for r in rows)
    assert summary["false_kill_rate"]["k"] == sum(r["status"] == "false_kill" for r in rows)
    assert summary["exact_match_rate"]["k"] == sum(r["status"] == "ok" for r in rows)
    assert all(r["max_chars"] > 0 for r in rows)


# ── gate streams ─────────────────────────────────────────────────────────

def test_gate_streams_shape_determinism_and_mode_mix():
    first = run_offline.run_gate_streams(seed=7, rates=(1.0, 12.0), minutes=30)
    second = run_offline.run_gate_streams(seed=7, rates=(1.0, 12.0), minutes=30)
    assert first == second
    assert first["mode_order"] == ["SLEEP", "QUIET", "CASUAL", "LIVELY", "BURST"]
    slow, fast = first["rates"]
    assert fast["messages"] > slow["messages"]
    for row in first["rates"]:
        assert sum(row["mode_share_of_messages"].values()) == pytest.approx(1.0, abs=1e-3)
        for key in ("always_silent_model", "always_speaking_model"):
            model = row[key]
            assert sum(model["pass_mode_counts"].values()) == model["passes"]
            assert model["passes"] >= 0
        silent = row["always_silent_model"]
        assert silent["wasted_model_calls"] == silent["passes"]
    assert slow["mode_share_of_messages"]["BURST"] == 0.0
    assert fast["mode_share_of_messages"]["BURST"] > 0.5
    # a model that always speaks is called far more often in a busy group
    assert fast["always_speaking_model"]["passes"] > slow["always_speaking_model"]["passes"]


def test_gate_streams_depend_on_the_seed():
    a = run_offline.run_gate_streams(seed=7, rates=(4.0,), minutes=30)
    b = run_offline.run_gate_streams(seed=8, rates=(4.0,), minutes=30)
    assert a["rates"][0]["messages"] != b["rates"][0]["messages"]


# ── structured pipeline profile ──────────────────────────────────────────

ENVELOPES = ROOT / "evals" / "cases" / "envelopes_v1.jsonl"
SINGLE = run_offline.make_profile("single", "score")


def _replay_v2(*cases, seed=7):
    out = run_offline.run_routing(list(cases), seed=seed, profile=SINGLE)
    return {c["id"]: c for c in out["cases"]}


def test_the_default_profile_is_empty_and_unknown_choices_are_rejected():
    assert run_offline.make_profile() == {}
    assert run_offline.make_profile("single", "score") == {
        "pipeline_v2": "single", "proactive_gate_mode": "score",
    }
    assert run_offline.make_profile("two_step") == {"pipeline_v2": "two_step"}
    with pytest.raises(ValueError):
        run_offline.make_profile("bogus")
    with pytest.raises(ValueError):
        run_offline.make_profile("off", "bogus")


def test_legacy_reports_carry_no_pipeline_keys():
    report = run_offline.build_report(seed=7)
    assert "profile" not in report and "envelopes" not in report
    assert "pipeline" not in report["routing"]["summary"]
    assert all("route_level" not in c for c in report["routing"]["cases"])


def test_a_name_call_reaches_the_model_only_in_the_structured_pipeline():
    case = _case("nc", category="name_call", content="小助手在吗", at=False,
                 expect=("speak", "must", "bot"))
    legacy = _replay(case)["nc"]
    v2 = _replay_v2(case)["nc"]
    assert legacy["outcome"] == "none"
    assert (v2["outcome"], v2["route"]) == ("engaged_chat", "name_call")
    assert v2["route_level"] == "strong" and v2["route_signals"] == ["name_call"]
    assert v2["strategy"] == "single"


def test_an_explicit_at_is_the_explicit_level():
    res = _replay_v2(_case("at", content="@小助手 你好"))["at"]
    assert (res["outcome"], res["route"]) == ("engaged_chat", "at_mention")
    assert res["route_level"] == "explicit" and res["route_signals"] == ["at"]


def test_commands_and_unrouted_messages_are_labelled_in_the_pipeline_report():
    res = _replay_v2(
        _case("cmd", category="lots_command", content="@小助手 抽签"),
        _case("idle", content="今天好热啊", at=False, expect=("silent", "should", "group"),
              context=[(-30, "u_a", "阿明", "吃了吗")]),
    )
    assert res["cmd"]["route_level"] == "local"
    assert res["idle"]["route_level"] in ("none", "ambient")


def test_the_score_gate_decides_the_proactive_cases():
    chatter = [(-90 + 10 * i, f"u_{i % 4}", f"人{i % 4}", f"闲聊{i}") for i in range(10)]
    question = _case("q", category="ambient_open", content="各位 Python 里怎么把列表去重啊",
                     at=False, context=chatter, expect=("speak", "should", "group"))
    reaction = _case("r", category="short_reaction", content="哈哈", at=False,
                     context=chatter, expect=("silent", "should", "group"))
    res = _replay_v2(question, reaction)
    assert res["q"]["outcome"] == "engaged_proactive" and res["q"]["gate"]["score"] > 0.35
    assert res["r"]["outcome"] == "none" and res["r"]["gate_score"] < 0.35


def test_pipeline_summary_on_hand_made_results():
    def row(cid, action, strength, outcome, level, addressee="bot", **extra):
        return {"id": cid, "category": extra.pop("category", "x"), "outcome": outcome,
                "route": "r", "route_level": level, "strategy": "single",
                "expect": {"action": action, "strength": strength, "addressee": addressee},
                **extra}

    results = [
        row("a", "speak", "must", "engaged_chat", "explicit"),
        row("b", "speak", "must", "engaged_chat", "strong"),
        row("c", "speak", "must", "handled_locally", "local"),
        row("d", "speak", "must", "none", "none"),
        row("e", "speak", "must", "engaged_vision", "none"),
        row("f", "silent", "must", "engaged_chat", "explicit", "other_member"),
        row("g", "silent", "should", "engaged_chat", "weak", "group"),
        row("h", "silent", "should", "none", "none", "nobody"),
    ]
    pipe = metrics.pipeline_summary(results)
    assert pipe["strategy"] == "single"
    assert pipe["levels"] == {"explicit": 2, "strong": 1, "weak": 1, "ambient": 0}
    assert pipe["must_speak"] == {
        "n": 5, "guaranteed": 1, "model_decides": 1, "vision": 1, "local": 1, "none": 1,
    }
    assert (pipe["silent_guaranteed"]["k"], pipe["silent_guaranteed"]["n"]) == (1, 3)
    assert pipe["silent_guaranteed_ids"] == ["f"]
    assert (pipe["silent_model_asked"]["k"], pipe["silent_model_asked"]["n"]) == (1, 3)
    assert pipe["addressee"]["other_member"] == {"explicit": 1}
    assert metrics.pipeline_summary([{"id": "x", "category": "c", "outcome": "none"}]) is None


def test_the_separation_auc_needs_enough_cases_on_both_sides():
    assert metrics.auc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert metrics.auc([0.5], [0.5]) == 0.5
    assert metrics.auc([], [0.5]) is None

    def scored(cid, action, score):
        return {"id": cid, "category": "ambient_open", "outcome": "none", "route": "none",
                "route_level": "none", "gate_score": score,
                "expect": {"action": action, "strength": "should", "addressee": "group"}}

    few = [scored("a", "speak", 0.9), scored("b", "silent", 0.1)]
    assert metrics.pipeline_summary(few)["gate_scores"]["auc"] is None
    many = [scored(f"s{i}", "speak", 0.8 + i / 100) for i in range(5)]
    many += [scored(f"n{i}", "silent", 0.1 + i / 100) for i in range(5)]
    summary = metrics.pipeline_summary(many)["gate_scores"]
    assert summary["auc"] == 1.0 and summary["speak_n"] == summary["silent_n"] == 5


# ── envelope replay ─────────────────────────────────────────────────────

def test_envelope_row_verdicts():
    classify = metrics.classify_envelope_row
    reply = {"action": "reply", "text": "好"}
    silent = {"action": "silent"}
    assert classify(reply, reply) == "ok"
    assert classify(reply, {"action": "reply", "text": "好吧"}) == "mangled"
    assert classify(reply, silent) == "false_kill"
    assert classify(reply, {"action": "fail", "code": "no_json"}) == "false_kill"
    assert classify(silent, reply) == "leak"
    assert classify(silent, silent) == "ok"
    assert classify(silent, {"action": "fail", "code": "x"}) == "wrong_failure"
    assert classify({"action": "fail", "code": "no_json"}, reply) == "leak"
    assert classify({"action": "fail", "code": "no_json"}, {"action": "fail", "code": "no_json"}) == "ok"
    assert classify({"action": "fail", "code": "no_json"}, {"action": "fail", "code": "bad_json"}) == "wrong_failure"
    assert classify({"action": "fail"}, {"action": "fail", "code": "anything"}) == "ok"


def test_the_shipped_envelope_fixture_is_well_formed():
    rows = load_jsonl(ENVELOPES)
    assert len(rows) >= 100
    assert len({r["id"] for r in rows}) == len(rows)
    for row in rows:
        assert set(row) >= {"id", "kind", "raw", "expect", "max_chars", "names"}
        assert row["expect"]["action"] in ("reply", "silent", "fail")
        assert (row["expect"]["action"] == "reply") == ("text" in row["expect"])
    assert {r["kind"] for r in rows} >= {
        "valid_reply", "valid_silent", "silent_with_text", "wrapped", "leak_in_reply",
        "leak_plain_text", "broken_json", "bad_schema", "format_noise", "known_gap",
    }


def test_the_envelope_replay_only_misses_the_documented_gaps():
    result = run_offline.run_envelopes(load_jsonl(ENVELOPES))
    summary = result["summary"]
    assert summary["known_gaps"]
    assert summary["mismatches"] == summary["known_gaps"]
    assert summary["false_kill_rate"]["k"] == 0
    assert summary["wrong_failure"] == 0
    # every legacy leak string is caught when it sits inside a reply or replaces the JSON
    by_kind = summary["by_kind"]
    for kind in ("leak_in_reply", "leak_plain_text", "valid_reply_mentions_silence", "silent_with_text"):
        assert by_kind[kind]["ok"] == by_kind[kind]["n"], kind


def test_text_on_a_silent_envelope_never_leaks_in_the_replay():
    rows = [
        {"id": f"s{i}", "kind": "silent_with_text", "max_chars": 30, "names": [],
         "raw": json.dumps({"action": "silent", "messages": [text]}, ensure_ascii=False),
         "expect": {"action": "silent"}}
        for i, text in enumerate(["好的", "NO_REPLY", "沉默，因为他们在聊", "你好呀"])
    ]
    result = run_offline.run_envelopes(rows)
    assert result["summary"]["leak_rate"]["k"] == 0 and result["summary"]["exact"]["k"] == 4


# ── report assembly with a profile ───────────────────────────────────────

@pytest.fixture(scope="module")
def v2_report():
    return run_offline.build_report(seed=7, pipeline="single", gate="score")


def test_a_pipeline_report_is_deterministic_and_complete(v2_report):
    again = run_offline.build_report(seed=7, pipeline="single", gate="score")
    assert run_offline.canonical_json(v2_report) == run_offline.canonical_json(again)
    assert v2_report["profile"] == {"pipeline": "single", "gate": "score"}
    assert "pipeline" in v2_report["routing"]["summary"]
    assert v2_report["inputs"]["envelopes"]["n_rows"] >= 100
    assert v2_report["gate"]["config"]["gate_mode"] == "score"


def test_the_pipeline_never_forces_a_reply_on_a_message_labelled_silent(v2_report):
    pipe = v2_report["routing"]["summary"]["pipeline"]
    assert pipe["silent_guaranteed_ids"] == []


def test_the_pipeline_answers_more_of_the_must_speak_cases_than_the_legacy_router(v2_report):
    legacy = run_offline.build_report(seed=7)
    old = legacy["routing"]["summary"]["addressing_recall"]["speak_must"]
    new = v2_report["routing"]["summary"]["addressing_recall"]["speak_must"]
    assert new["k"] > old["k"] and new["n"] == old["n"]


def test_the_score_gate_spends_calls_on_better_moments_than_the_dice(v2_report):
    rows = v2_report["gate"]["rates"]
    assert all("dice_reference" in row for row in rows)
    better = 0
    for row in rows:
        score = row["always_silent_model"]["worthy_share_of_passes"]
        dice = row["dice_reference"]["worthy_share_of_passes"]
        if score is not None and (dice is None or score > dice):
            better += 1
    assert better >= len(rows) - 1


def test_the_markdown_and_console_show_the_pipeline_sections(v2_report):
    text = run_offline.render_markdown(v2_report)
    for needle in ("structured pipeline: strategy / gate", "## 1b. Structured pipeline",
                   "## 4. Envelope replay", "worthy share of calls", "known gap"):
        assert needle in text, needle
    console = run_offline.render_console(v2_report)
    assert "-- structured pipeline (single, gate score) --" in console
    assert "-- envelope parsing --" in console


def test_cli_pipeline_flags_write_a_profile_report(tmp_path, capsys):
    code = run_offline.main(["--out", str(tmp_path), "--seed", "7", "--tag", "v2",
                             "--pipeline", "single", "--gate", "score"])
    assert code == 0
    report = json.loads((tmp_path / "v2.json").read_text("utf-8"))
    assert report["profile"] == {"pipeline": "single", "gate": "score"}
    assert report["determinism"]["identical"] is True
    assert "envelope parsing" in capsys.readouterr().out



def test_a_temp_dir_that_cannot_be_deleted_does_not_fail_the_run(monkeypatch):
    """Windows: a SQLite file still open in the temp dir raises WinError 32 on cleanup."""
    import tempfile
    from evals import run_offline

    created = {}
    real = tempfile.TemporaryDirectory

    def spy(*args, **kwargs):
        created.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(run_offline.tempfile, "TemporaryDirectory", spy)
    with run_offline._hermetic_cwd() as tmp:
        (tmp / "messages.db").write_bytes(b"x")
    assert created.get("ignore_cleanup_errors") is True


def test_inputs_hash_the_same_with_windows_line_endings(tmp_path):
    """A Windows checkout (core.autocrlf) must report the same input hashes as macOS."""
    from evals import run_offline

    unix, windows = tmp_path / "unix.jsonl", tmp_path / "windows.jsonl"
    unix.write_bytes(b'{"id": 1}\n{"id": 2}\n')
    windows.write_bytes(b'{"id": 1}\r\n{"id": 2}\r\n')
    assert run_offline._sha256(unix) == run_offline._sha256(windows)
