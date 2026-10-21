"""tools/make_training_data.py: invented chats, the teacher's answers, the overlap check."""

import json
from types import SimpleNamespace

import httpx
import pytest

from src.summarize.openai_backend import OpenAISummarizer
from tools import make_training_data as M

TEACHER_REPLY = '{"addressee":"me","action":"reply","messages":["好呀"],"reason":"被问"}'
TEACHER_SILENT = '{"addressee":"others","action":"silent","messages":[],"reason":"在互相聊"}'


def chat(kind="at_request", latest="讲个笑话", at=True, context=(("阿明", "好无聊"),), name="小红"):
    return {"kind": kind, "group_name": "宿舍", "context": [{"name": n, "text": t} for n, t in context],
            "latest": {"name": name, "text": latest, "at_bot": at}}


def test_a_written_chat_becomes_a_valid_scenario():
    case = M.to_case(chat(context=(("阿明", "好无聊"), ("小助手", "要不要听歌"))), index=1, bot_name="小助手")
    assert case["trigger"]["content"] == "@小助手 讲个笑话" and case["trigger"]["is_at_mentioned"]
    assert case["context"][1]["sender_id"] == "u_bot" and case["bot_state"]["last_bot_reply"]["text"] == "要不要听歌"
    assert case["source"] == "synthetic" and case["category"] == "at_request" and case["id"].startswith("syn-")
    plain = M.to_case(chat(kind="weird", latest="哈哈", at=False), index=2, bot_name="小助手")
    assert plain["trigger"]["content"] == "哈哈" and plain["bot_state"] is None and plain["category"] == "synthetic"


@pytest.mark.parametrize("bad", [{"kind": "x"}, chat(latest=""), chat(name="小助手"), {"latest": "文字"}])
def test_unusable_chats_are_dropped(bad):
    assert M.to_case(bad, index=0, bot_name="小助手") is None


class FakeCloud:
    model = "teacher"

    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def _chat_persona_settings(self):
        return {"persona_name": "林小满", "persona_prompt": "P", "reply_rules": "R", "max_chars": 30,
                "context_count": 10}

    def _call_json_api(self, system, messages, **kwargs):
        self.prompts.append((system, messages))
        text = self.answer(system, messages) if callable(self.answer) else self.answer
        return SimpleNamespace(text=text)


def test_synth_asks_for_the_kinds_and_keeps_new_chats():
    cloud = FakeCloud(lambda s, m: json.dumps({"chats": [chat(latest=f"讲个笑话{len(m[0]['content'])}"),
                                                         chat(latest="帮我选"), {"broken": 1}]},
                                                ensure_ascii=False))
    cases = M.synth(cloud, 3, seed=1, batch=2)
    assert len(cases) == 3 and len({c["id"] for c in cases}) == 3
    prompt = cloud.prompts[0][1][0]["content"]
    assert "类型" in prompt and "@" in prompt and '"chats"' in prompt


def test_synth_survives_a_broken_batch():
    answers = iter(["不是 JSON", json.dumps({"chats": [chat(), chat(latest="另一个")]}, ensure_ascii=False)])
    cases = M.synth(FakeCloud(lambda s, m: next(answers)), 2, seed=1, batch=2)
    assert len(cases) == 2


def test_rows_keep_first_asks_with_a_canonical_answer():
    calls = [
        ("你是林小满的“要不要发言”判断器", [{"role": "user", "content": "u1"}], '```json\n{"action":"reply","messages":[]}\n```'),
        ("你是林小满", [{"role": "user", "content": "u2"}], TEACHER_REPLY),
        ("你是林小满", [{"role": "user", "content": "u2"}, {"role": "assistant", "content": "x"},
                       {"role": "user", "content": "改短"}], TEACHER_REPLY),          # a rewrite: left out
        ("你是林小满", [{"role": "user", "content": "u3"}], "没有 JSON"),
    ]
    rows = M.training_rows("c1", "synthetic", calls)
    assert [r["meta"]["stage"] for r in rows] == ["decide", "reply"]
    assert rows[0]["messages"][-1] == {"role": "assistant", "content": '{"action": "reply", "messages": []}'}
    assert rows[1]["messages"][1] == {"role": "user", "content": "u2"} and rows[1]["meta"]["action"] == "reply"


def openai_teacher():
    """A real OpenAI-compatible model object behind a fake network: silent at the decide step."""
    teacher = OpenAISummarizer(api_key="k", model="teacher", base_url="https://relay.example/v1")

    def handler(request):
        system = json.loads(request.content)["messages"][0]["content"]
        answer = TEACHER_SILENT if "判断器" in system else TEACHER_REPLY
        return httpx.Response(200, json={"choices": [{"message": {"content": answer}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 2}})

    teacher.client = httpx.Client(transport=httpx.MockTransport(handler))
    return teacher


def test_teach_records_the_compact_prompts_of_the_real_pipeline():
    cases = [M.to_case(chat(), index=1, bot_name="小助手"),
             M.to_case(chat(kind="name_call", latest="小助手今天怎么不说话", at=False,
                            context=(("阿明", "周末爬山"), ("小红", "我去"))), index=2, bot_name="小助手")]
    teacher = openai_teacher()
    rows = M.teach(teacher, cases, seed=7)
    assert {r["meta"]["stage"] for r in rows} == {"decide", "reply"}
    assert teacher.prompt_profile == "compact"
    reply_rows = [r for r in rows if r["meta"]["stage"] == "reply"]
    assert reply_rows and "## 别人找你做事时" in reply_rows[0]["messages"][0]["content"]
    assert "讲个笑话" in reply_rows[0]["messages"][1]["content"]
    assert {r["meta"]["case"] for r in rows} <= {c["id"] for c in cases}


def test_scenarios_like_the_evaluation_sets_are_found():
    evals = M.load_eval_cases()
    copy = dict(evals[0], id="syn-copy")
    fresh = M.to_case(chat(latest="今天食堂的麻辣香锅好咸", context=(("老周", "打完球饿死"),)), index=1, bot_name="小助手")
    found = M.similar_to_evals([copy, fresh], evals)
    assert [f[0] for f in found] == ["syn-copy"] and found[0][1] == evals[0]["id"]


def test_teach_refuses_evaluation_scenarios(tmp_path, capsys):
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(M.load_eval_cases()[0], ensure_ascii=False) + "\n", encoding="utf-8")

    def no_cloud():
        raise AssertionError("must not reach the cloud")

    assert M.main(["teach", "--cases", str(path), "--out", str(tmp_path / "t.jsonl"), "--yes"],
                  cloud_factory=no_cloud) == 2
    assert "resemble the evaluation sets" in capsys.readouterr().err


def test_dry_runs_call_nothing(tmp_path, capsys):
    def no_cloud():
        raise AssertionError("must not reach the cloud")

    assert M.main(["synth", "--n", "20", "--out", str(tmp_path / "s.jsonl")], cloud_factory=no_cloud) == 0
    assert "about 3 calls" in capsys.readouterr().out and not (tmp_path / "s.jsonl").exists()


def test_stats_count_stages_actions_and_duplicates():
    row = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "uu"},
                        {"role": "assistant", "content": "{}"}],
           "meta": {"case": "c1", "stage": "reply", "action": "reply"}}
    result = M.stats([row, dict(row), dict(row, meta={"case": "c2", "stage": "decide", "action": "silent"})])
    assert result["rows"] == 3 and result["scenarios"] == 2 and result["duplicates"] == 2
    assert result["actions"] == {"decide/silent": 1, "reply/reply": 2} and result["prompt_chars_max"] == 3


def test_the_persona_line_names_the_persona_without_its_text(capsys):
    assert M.main(["persona"], cloud_factory=lambda: FakeCloud("{}")) == 0
    out = capsys.readouterr().out
    assert out.startswith("persona 林小满 (prompt sha256 ") and "model teacher" in out and "P" not in out.split("sha256")[1][:12]


def test_check_can_look_at_scenarios_alone(tmp_path, capsys):
    path = tmp_path / "export.jsonl"
    path.write_text(json.dumps(M.load_eval_cases()[0], ensure_ascii=False) + "\n", encoding="utf-8")
    assert M.main(["check", "--cases", str(path)]) == 1
    assert "too close to the evaluation sets: 1" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        M.main(["check"])


def test_a_shortened_answer_replaces_the_long_one_and_long_ones_are_dropped():
    system = "你是林小满……messages：要发到微信里的话，只写 1 条，不超过 10 个字。"
    long_reply = '{"action":"reply","messages":["这是一条明显超过十个字的很长的回复"]}'
    short_reply = '{"action":"reply","messages":["好呀，去吧"]}'
    calls = [
        (system, [{"role": "user", "content": "u1"}], long_reply),
        (system, [{"role": "user", "content": "u1"}, {"role": "assistant", "content": long_reply},
                  {"role": "user", "content": "你上一条回复太长了，超过 10 个字"}], short_reply),
        (system, [{"role": "user", "content": "u2"}], long_reply),               # never shortened
        (system, [{"role": "user", "content": "u3"}], short_reply),
    ]
    rows = M.training_rows("c1", "synthetic", calls)
    assert [r["messages"][1]["content"] for r in rows] == ["u1", "u3"]
    assert rows[0]["messages"][-1]["content"] == '{"action": "reply", "messages": ["好呀，去吧"]}'
    assert rows[0]["meta"]["rewritten"] and "rewritten" not in rows[1]["meta"]
    assert all("_data" not in r for r in rows)


# ── many personas ───────────────────────────────────────────────────────

def test_written_personas_are_checked_and_never_use_an_evaluation_name():
    from evals.persona_cards import held_out_names
    banned = sorted(held_out_names())[0]

    def answer(system, messages):
        prompt = messages[0]["content"]
        assert banned in prompt                                    # the writer is told
        return json.dumps({"personas": [
            {"persona_name": "阿芳", "persona_prompt": "你是阿芳，开花店的。", "reply_rules": "每条回复不超过20字。", "max_chars": 20},
            {"persona_name": banned, "persona_prompt": "你是别人。", "reply_rules": "规则", "max_chars": 20},
            {"persona_name": "坏卡", "persona_prompt": "你是坏卡。", "reply_rules": "规则", "max_chars": 900},
            {"persona_name": "阿芳", "persona_prompt": "你是另一个阿芳。", "reply_rules": "规则", "max_chars": 30},
        ]}, ensure_ascii=False)

    cards = M.synth_personas(FakeCloud(answer), 3, seed=1, batch=4)
    assert [c["persona_name"] for c in cards] == ["阿芳"] and cards[0]["id"].startswith("p-")


def test_a_scenario_can_be_played_under_the_cards_name():
    case = M.to_case(chat(context=(("阿明", "好无聊"), ("小助手", "要不要听歌"))), index=1, bot_name="小助手")
    card = {"id": "p-1", "persona_name": "老王", "persona_prompt": "你是老王。", "reply_rules": "规则", "max_chars": 20}
    renamed = M.as_card(case, card, rename=True)
    assert renamed["bot_name"] == "老王" and renamed["trigger"]["content"] == "@老王 讲个笑话"
    assert renamed["context"][1]["sender_name"] == "老王" and case["bot_name"] == "小助手"   # the original is untouched
    assert M.as_card(case, card, rename=False) == case


def test_teach_gives_each_scenario_a_card():
    cards = [{"id": f"p-{i}", "persona_name": name, "persona_prompt": f"你是{name}，很有个性。",
              "reply_rules": "每条回复不超过20字。", "max_chars": 20} for i, name in enumerate(("老王", "阿芳"))]
    cases = [M.to_case(chat(latest=f"讲个笑话{i}"), index=i, bot_name="小助手") for i in range(6)]
    rows = M.teach(openai_teacher(), cases, seed=3, personas=cards)
    used = {r["meta"]["persona"] for r in rows}
    assert used <= {"p-0", "p-1"} and len(used) == 2
    for row in rows:
        name = next(c["persona_name"] for c in cards if c["id"] == row["meta"]["persona"])
        assert f"你是{name}" in row["messages"][0]["content"] and "不超过 20 个字" in row["messages"][0]["content"]


def test_teach_refuses_evaluation_personas(tmp_path, capsys):
    from evals.persona_cards import HELD_OUT
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps(M.to_case(chat(), index=1, bot_name="小助手"), ensure_ascii=False) + "\n",
                     encoding="utf-8")

    def no_cloud():
        raise AssertionError("must not reach the cloud")

    assert M.main(["teach", "--cases", str(cases), "--personas", str(HELD_OUT), "--out",
                   str(tmp_path / "t.jsonl"), "--yes"], cloud_factory=no_cloud) == 2
    assert "evaluation personas" in capsys.readouterr().err


def test_the_configured_persona_prints_as_a_card(capsys):
    assert M.main(["persona", "--card", "--id", "mine"], cloud_factory=lambda: FakeCloud("{}")) == 0
    card = json.loads(capsys.readouterr().out)
    assert card == {"id": "mine", "persona_name": "林小满", "persona_prompt": "P", "reply_rules": "R", "max_chars": 30}
