"""Persona example dialogues: data/examples/<persona>.txt shown to the model as style reference."""

import io
import os
import sqlite3
import time
from unittest.mock import patch

import pytest

from test_router_pipeline import PipelineSummarizer, TraceSpy
from test_router_priority import BOT, RouterTestCase, StubWebSearch

from src.config import load_config
from src.persona_examples import (
    MAX_EXAMPLE_CHARS, ExampleLibrary, parse_examples, render_examples,
)
from src.pipeline.addressing import LEVEL_EXPLICIT, Addressing
from src.pipeline.prompts import STAGE_DECIDE, build_prompts
from src.pipeline.types import ChatRequest, Decision

SAMPLE = """﻿# 杨宇靖的示例
群友：你今天好可爱
杨宇靖：才、才没有……
---
小明：晚饭吃啥
杨宇靖：食堂的番茄炒蛋，便宜
---
阿强: 周末去爬山吗
小红：我去
我：我也想去，可以吗
---
群友：随便聊聊
---
这一行没有说话人
"""


def book(text=SAMPLE):
    return parse_examples(text, ["杨宇靖", "yang_yujing"])


# ── the file format ─────────────────────────────────────────────────────────

def test_blocks_lines_and_problems():
    parsed = book()
    assert len(parsed) == 3
    first = parsed.examples[0]
    assert first.lines == ((False, "群友", "你今天好可爱"), (True, "杨宇靖", "才、才没有……"))
    assert parsed.examples[2].lines[-1] == (True, "我", "我也想去，可以吗")         # 我 counts as the persona
    assert any("no line by the persona" in p for p in parsed.problems)
    assert any("expected 'speaker：text'" in p for p in parsed.problems)


def test_an_over_long_block_is_skipped():
    parsed = book("群友：" + "啊" * MAX_EXAMPLE_CHARS + "\n杨宇靖：嗯\n")
    assert len(parsed) == 0 and "longer than" in parsed.problems[0]


def test_the_closest_examples_come_first():
    parsed = book()
    for seed in range(20):                                     # not the luck of the tie-break
        assert parsed.pick("今晚食堂吃啥呀", 1, seed=str(seed))[0].lines[0][2] == "晚饭吃啥"
    assert len(parsed.pick("x", 2, seed="m1")) == 2 and parsed.pick("x", 0) == []
    assert len(parsed.pick("x", 99)) == 3


def test_ties_vary_with_the_message_but_are_stable():
    parsed = book()
    first = [e.lines[0][2] for e in parsed.pick("完全无关", 1, seed="a")]
    assert first == [e.lines[0][2] for e in parsed.pick("完全无关", 1, seed="a")]
    seen = {parsed.pick("完全无关", 1, seed=str(i))[0].lines[0][2] for i in range(30)}
    assert len(seen) > 1


def test_other_speakers_never_appear_by_name():
    parsed = book()
    text = render_examples(parsed.examples, "杨宇靖")
    assert "小明" not in text and "阿强" not in text and "小红" not in text
    assert "例1\n群友：你今天好可爱\n杨宇靖：才、才没有……" in text
    assert "群友A：周末去爬山吗\n群友B：我去\n杨宇靖：我也想去，可以吗" in text


# ── the library ─────────────────────────────────────────────────────────────

def test_the_file_is_found_by_id_or_name_and_reread_when_it_changes(tmp_path):
    library = ExampleLibrary(tmp_path)
    assert library.book_for("yang_yujing", "杨宇靖") is None
    path = tmp_path / "杨宇靖.txt"
    path.write_text("群友：在吗\n杨宇靖：在的\n", encoding="utf-8")
    assert len(library.book_for("yang_yujing", "杨宇靖")) == 1
    path.write_text("群友：在吗\n杨宇靖：在的\n---\n群友：好\n杨宇靖：嗯\n", encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert len(library.book_for("yang_yujing", "杨宇靖")) == 2
    (tmp_path / "yang_yujing.txt").write_text("群友：a\n我：b\n", encoding="utf-8")
    assert len(library.book_for("yang_yujing", "杨宇靖")) == 1                 # the id comes first


def test_unsafe_names_and_bad_files_are_ignored(tmp_path):
    (tmp_path / "examples").mkdir()
    library = ExampleLibrary(tmp_path / "examples")
    (tmp_path / "secret.txt").write_text("群友：a\n我：b\n", encoding="utf-8")
    (tmp_path / "examples" / ".hidden.txt").write_text("群友：a\n我：b\n", encoding="utf-8")
    assert library.book_for("../secret", ".hidden") is None
    (tmp_path / "examples" / "gbk.txt").write_bytes("群友：你好\n我：嗯\n".encode("gbk"))
    bad = library.book_for("gbk")
    assert len(bad) == 0 and bad.problems


# ── the prompt ──────────────────────────────────────────────────────────────

SETTINGS = {"persona_name": "杨宇靖", "persona_prompt": "人设", "reply_rules": "规则",
            "max_chars": 30, "context_count": 10}


def test_the_prompt_shows_them_only_when_given_and_not_to_the_judge():
    request = ChatRequest(message="吃啥", bot_name=BOT, addressing=Addressing(LEVEL_EXPLICIT))
    _, plain = build_prompts(SETTINGS, request)
    assert "平时说话的样子" not in plain
    with_examples = ChatRequest(message="吃啥", bot_name=BOT, addressing=Addressing(LEVEL_EXPLICIT),
                                examples="例1\n群友：晚饭吃啥\n杨宇靖：番茄炒蛋")
    _, user = build_prompts(SETTINGS, with_examples)
    assert "杨宇靖平时说话的样子" in user and "例1\n群友：晚饭吃啥" in user
    assert user.index("平时说话的样子") < user.index("最近聊天记录")
    _, judge = build_prompts(SETTINGS, with_examples, stage=STAGE_DECIDE)
    assert "平时说话的样子" not in judge


# ── the router ──────────────────────────────────────────────────────────────

class RouterExamples(RouterTestCase):
    def build(self, **config):
        model = PipelineSummarizer(Decision(action="reply", messages=("嗯",), addressee="me"))
        router, _ = self.make_router(summarizer=model, pipeline_v2="single", web=StubWebSearch(),
                                     persona_examples_dir=str(self.tmp / "examples"), **config)
        router._tracer = self.trace = TraceSpy()
        return router, model

    def write(self, name, text):
        folder = self.tmp / "examples"
        folder.mkdir(exist_ok=True)
        (folder / name).write_text(text, encoding="utf-8")

    def test_the_closest_examples_reach_the_model(self):
        self.write("yang_yujing.txt", SAMPLE)
        router, model = self.build()
        router.handle(self.group_msg("m1", f"@{BOT} 晚饭吃啥"))
        examples = model.decide_requests[0].examples
        assert examples.startswith("例1\n群友：晚饭吃啥") and "小明" not in examples
        assert self.trace.notes["persona_examples"] == 3

    def test_without_a_file_or_with_zero_nothing_is_added(self):
        router, model = self.build()
        router.handle(self.group_msg("m1", f"@{BOT} 晚饭吃啥"))
        assert model.decide_requests[0].examples == "" and "persona_examples" not in self.trace.notes
        self.write("yang_yujing.txt", SAMPLE)
        router, model = self.build(persona_examples=0)
        router.handle(self.group_msg("m2", f"@{BOT} 晚饭吃啥"))
        assert model.decide_requests[0].examples == ""

    def test_the_count_is_respected(self):
        self.write("yang_yujing.txt", SAMPLE)
        router, model = self.build(persona_examples=1)
        router.handle(self.group_msg("m1", f"@{BOT} 晚饭吃啥"))
        assert model.decide_requests[0].examples.count("例") == 1

    def test_the_proactive_path_gets_them_too(self):
        from test_router_pipeline import MODE
        self.write("yang_yujing.txt", SAMPLE)
        router, model = self.build(proactive_enabled=True)
        self.gate(router, allow=True, mode=MODE)
        router.handle(self.group_msg("m1", "晚饭吃啥", at=False))
        request = model.decide_requests[0]
        assert request.call_name == "proactive_v2" and request.examples.startswith("例1")

    def test_a_broken_library_never_stops_a_reply(self):
        router, model = self.build()
        with patch.object(router._examples, "book_for", side_effect=OSError("disk")):
            assert router.handle(self.group_msg("m1", f"@{BOT} 在吗")) == "嗯"


def test_the_settings():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=True):
        config = load_config()
    assert (config.persona_examples, config.persona_examples_dir) == (4, "data/examples")
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", "PERSONA_EXAMPLES": "9"}, clear=True):
        with pytest.raises(RuntimeError, match="PERSONA_EXAMPLES"):
            load_config()


# ── the tool ────────────────────────────────────────────────────────────────

def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE messages (chat_id TEXT, sender_name TEXT, content TEXT, msg_type INT, timestamp INT)")
    conn.execute("CREATE TABLE history_messages (chat_id TEXT, sender_name TEXT, content TEXT, msg_type INT, "
                 "timestamp INT, source TEXT)")
    rows = [
        ("g1", "小明", "周末打王者吗", 1, 100), ("g1", "阿强", "打啊，晚上八点", 1, 130),
        ("g1", "小明", "好", 1, 140), ("g1", "小红", "我也来", 1, 150), ("g1", "阿强", "行啊", 1, 160),
        ("g1", "阿强", "http://x.cn 看这个", 1, 170), ("g2", "小李", "在吗", 1, 100),
        ("g2", "阿强", "在呢在呢", 1, 900),                            # too long after: no context
    ]
    conn.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?)", rows)
    conn.execute("INSERT INTO history_messages VALUES ('g1', '小明', '昨天那局太菜了', 1, 50, 'import')")
    conn.execute("INSERT INTO history_messages VALUES ('g1', '阿强', '确实，我背锅', 1, 60, 'import')")
    conn.commit()
    conn.close()


def test_extract_drafts_blocks_with_context_and_hides_names(tmp_path):
    from tools import persona_examples as tool
    db = tmp_path / "messages.db"
    make_db(db)
    out_file = tmp_path / "examples" / "yang.txt"
    out = io.StringIO()
    code = tool.main(["extract", "--speaker", "阿强", "--as", "杨宇靖", "--out", str(out_file), "--db", str(db)], out)
    assert code == 0, out.getvalue()
    text = out_file.read_text(encoding="utf-8")
    assert "群友：昨天那局太菜了\n杨宇靖：确实，我背锅" in text
    assert "群友：周末打王者吗\n杨宇靖：打啊，晚上八点" in text
    assert "群友A：好\n群友B：我也来\n杨宇靖：行啊" in text
    assert "http" not in text and "小明" not in text and "征得本人同意" in text
    assert "在呢在呢" not in text                                              # 800 s later: no context
    assert len(parse_examples(text, ["杨宇靖"])) == 3
    assert tool.main(["extract", "--speaker", "阿强", "--as", "杨宇靖", "--out", str(out_file),
                      "--db", str(db)], io.StringIO()) == 1                      # no overwrite
    assert tool.main(["extract", "--speaker", "阿强", "--as", "杨宇靖", "--out", str(out_file), "--db", str(db),
                      "--force", "--limit", "1", "--chat", "g1"], io.StringIO()) == 0
    assert len(parse_examples(out_file.read_text(encoding="utf-8"), ["杨宇靖"])) == 1


def test_check_reports_problems(tmp_path):
    from tools import persona_examples as tool
    path = tmp_path / "yang_yujing.txt"
    path.write_text(SAMPLE, encoding="utf-8")
    out = io.StringIO()
    assert tool.main(["check", str(path), "--persona", "杨宇靖"], out) == 1
    assert "3 example(s) usable, 2 skipped" in out.getvalue()
    path.write_text("群友：在吗\n杨宇靖：在的\n", encoding="utf-8")
    assert tool.main(["check", str(path), "--persona", "杨宇靖"], io.StringIO()) == 0
