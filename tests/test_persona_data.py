"""Personas and alias rules as data files."""

import hashlib
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

import pytest

import src.persona as P
from src.memory.facts import AliasRules, GroupFactExtractor, load_alias_rules

ROOT = Path(__file__).resolve().parents[1]


def digest(text):
    return hashlib.sha256(str(text).encode()).hexdigest()[:16]


# ── the shipped personas are byte-for-byte what the code used to hold ────────

def test_the_shipped_persona_reads_exactly_as_the_old_constants_did():
    assert list(P.builtin_personas()) == ["yang_yujing"]
    yang = P.builtin_personas()["yang_yujing"]
    assert (digest(yang.name), digest(yang.prompt), digest(yang.reply_rules)) == (
        "c45bef0d7a3ff8f4", "b0bfd44e87d7b402", "b6110f1fadf37d7f")
    assert (yang.max_chars, yang.context_window_sec, yang.context_count) == (30, 900, 14)
    assert sorted(p.name for p in (ROOT / "src" / "personas").glob("*.json")) == [
        "alias_rules.json", "yang_yujing.json"]


def test_the_old_module_level_names_still_exist_with_the_same_values():
    assert P.DEFAULT_CHAT_PERSONA_NAME == "杨宇靖" and P.DEFAULT_YANG_YUJING_PERSONA_NAME == "杨宇靖"
    assert P.DEFAULT_CHAT_PERSONA_PROMPT == P.DEFAULT_YANG_YUJING_PERSONA_PROMPT
    assert P.DEFAULT_CHAT_REPLY_RULES == P.DEFAULT_YANG_YUJING_REPLY_RULES
    assert P.DEFAULT_CHAT_MAX_CHARS == 30 and P.DEFAULT_CHAT_CONTEXT_COUNT == 14
    assert P.DEFAULT_YANG_YUJING_CONTEXT_COUNT == 14 and P.DEFAULT_CHAT_CONTEXT_WINDOW_SEC == 900
    assert P.DEFAULT_YANG_YUJING_GROUP_NAME and P.DEFAULT_YANG_YUJING_ALWAYS_REPLY_GROUP_NAME == ""
    assert P.PERSONA_PROFILES == ("yang_yujing", "custom")
    assert P.persona_defaults_for_profile("custom") is None
    assert P.persona_defaults_for_profile("nonsense") == P.persona_defaults_for_profile("yang_yujing")
    assert P.normalize_persona_profile(" YANG_YUJING ") == "yang_yujing"
    assert P.normalize_persona_profile("other") == "yang_yujing"
    assert P.persona_defaults_for_profile("yang_yujing") == {
        "name": "杨宇靖", "prompt": P.DEFAULT_YANG_YUJING_PERSONA_PROMPT,
        "reply_rules": P.DEFAULT_YANG_YUJING_REPLY_RULES, "max_chars": 30,
        "context_window_sec": 900, "context_count": 14,
    }


def test_the_full_feed_block_reproduces_the_pacing_the_router_used_to_hard_code():
    import src.router as router
    feed = P.builtin_personas()["yang_yujing"].full_feed
    assert feed.groups == (P.DEFAULT_YANG_YUJING_GROUP_NAME,) and feed.always_reply_groups == ()
    assert feed.ambient_cooldown_sec == 45 and feed.min_chars == 3
    assert feed.skip_reactions == frozenset({
        "嗯", "啊", "哦", "噢", "对", "是", "不是", "确实", "哈哈", "呵呵", "笑死", "牛", "6", "666",
        "好", "行", "可以", "没事", "寄",
    })
    assert feed.skip_words == ("ai", "机器人", "人机", "chatgpt", "提示词", "上下文", "人设")
    # the router's old constants are now read from the same data
    assert router.AMBIENT_COOLDOWN_SEC == 45
    assert router._FEED_SHORT_REACTIONS == set(feed.skip_reactions)
    assert router._FEED_META_RE.search("聊聊AI") and not router._FEED_META_RE.search("爬山")


def test_a_persona_hands_the_prompt_builders_their_settings_dict():
    settings = P.builtin_personas()["yang_yujing"].settings()
    assert settings == {"persona_name": "杨宇靖", "persona_prompt": P.DEFAULT_YANG_YUJING_PERSONA_PROMPT,
                        "reply_rules": P.DEFAULT_YANG_YUJING_REPLY_RULES, "max_chars": 30, "context_count": 14}


# ── reading and validating files ────────────────────────────────────────────

GOOD = {"id": "me", "name": "小助手", "prompt": "你是群友", "reply_rules": "简短"}


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_a_minimal_file_gets_sensible_defaults(tmp_path):
    persona = P.load_persona_file(write(tmp_path, "me.json", GOOD))
    assert (persona.max_chars, persona.context_window_sec, persona.context_count) == (36, 900, 40)
    assert persona.full_feed is None and persona.source.endswith("me.json")


def test_a_full_feed_block_is_read(tmp_path):
    persona = P.load_persona_file(write(tmp_path, "me.json", {**GOOD, "full_feed": {
        "groups": [" 群A ", ""], "always_reply_groups": ["群B"], "ambient_cooldown_sec": 10,
        "skip_reactions": ["嗯", "好"], "skip_words": [" AI ", "机器人", ""], "min_chars": 5}}))
    feed = persona.full_feed
    assert feed.groups == ("群A",) and feed.always_reply_groups == ("群B",)
    assert feed.skip_reactions == frozenset({"嗯", "好"}) and feed.min_chars == 5
    assert feed.skip_words == ("AI", "机器人") and feed.ambient_cooldown_sec == 10


@pytest.mark.parametrize("change", [
    {"id": ""}, {"id": "bad id!"}, {"id": 5}, {"name": ""}, {"name": ["x"]}, {"prompt": " "},
    {"reply_rules": None}, {"max_chars": 3}, {"max_chars": "36"}, {"max_chars": True},
    {"context_window_sec": 10}, {"context_count": 0}, {"context_count": 500},
    {"prompt": "字" * 20001},
    {"full_feed": "yes"}, {"full_feed": {"groups": "群A"}}, {"full_feed": {"groups": [1]}},
    {"full_feed": {"skip_pattern": "ai|机器人"}},                       # the old regex form is refused, not ignored
    {"full_feed": {"skip_words": "ai"}}, {"full_feed": {"skip_words": [1]}},
    {"full_feed": {"skip_words": ["字" * 41]}}, {"full_feed": {"skip_words": ["x"] * 201}},
    {"full_feed": {"skip_reactions": "嗯"}}, {"full_feed": {"ambient_cooldown_sec": -1}},
])
def test_unusable_values_are_refused_with_a_reason(tmp_path, change):
    with pytest.raises(P.PersonaError):
        P.load_persona_file(write(tmp_path, "me.json", {**GOOD, **change}))


@pytest.mark.parametrize("content", ["not json", "[1, 2]", "", "null", '{"id": "x"}'])
def test_broken_files_are_refused(tmp_path, content):
    with pytest.raises(P.PersonaError):
        P.load_persona_file(write(tmp_path, "me.json", content))
    with pytest.raises(P.PersonaError):
        P.load_persona_file(tmp_path / "missing.json")


def test_a_later_directory_replaces_an_earlier_persona_and_bad_files_are_skipped(tmp_path, caplog):
    builtin, user = tmp_path / "builtin", tmp_path / "user"
    builtin.mkdir(), user.mkdir()
    write(builtin, "a.json", {**GOOD, "id": "a", "name": "原版"})
    write(builtin, "b.json", {**GOOD, "id": "b"})
    write(user, "a.json", {**GOOD, "id": "a", "name": "我改的"})
    write(user, "broken.json", "{oops")
    write(user, "alias_rules.json", {"alias_pattern": "x"})               # not a persona
    found = P.load_personas(builtin, user, tmp_path / "does-not-exist")
    assert {k: v.name for k, v in found.items()} == {"a": "我改的", "b": "小助手"}
    assert "broken.json" in caplog.text


def load_copy(tmp_path, builtin=None):
    """The persona module as if installed in tmp_path (optionally with its own personas/ folder)."""
    copy = tmp_path / "persona.py"
    copy.write_text((ROOT / "src" / "persona.py").read_text(encoding="utf-8").replace(
        "from .datafile import", "from src.datafile import"), encoding="utf-8")
    if builtin is not None:
        (tmp_path / "personas").mkdir(exist_ok=True)
        for name, data in builtin.items():
            (tmp_path / "personas" / name).write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
                                                       encoding="utf-8")
    spec = importlib.util.spec_from_file_location("persona_copy", copy)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module                       # dataclasses look their module up here
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    return module


def test_a_missing_built_in_persona_stops_the_program_with_a_message_that_names_the_fix(tmp_path):
    with pytest.raises(Exception) as caught:
        load_copy(tmp_path)                               # no personas/ folder next to this copy
    assert "built-in persona 'yang_yujing' cannot be loaded" in str(caught.value)
    assert "src/personas" in str(caught.value) and "PyInstaller" in str(caught.value)


def test_a_broken_built_in_file_is_an_error_not_a_swap_for_another_persona(tmp_path):
    yang = json.loads((ROOT / "src" / "personas" / "yang_yujing.json").read_text(encoding="utf-8"))
    yang["full_feed"]["skip_words"] = "not a list"
    with pytest.raises(Exception) as caught:
        load_copy(tmp_path, {"yang_yujing.json": yang})
    assert "yang_yujing" in str(caught.value) and "skip_words" in str(caught.value)


def test_a_built_in_file_must_carry_its_own_id(tmp_path):
    yang = json.loads((ROOT / "src" / "personas" / "yang_yujing.json").read_text(encoding="utf-8"))
    with pytest.raises(Exception, match="must have id 'yang_yujing'"):
        load_copy(tmp_path, {"yang_yujing.json": {**yang, "id": "other"}})


def test_both_packaging_specs_ship_the_persona_folder():
    for spec in ("build.spec", "build-macos.spec"):
        text = (ROOT / spec).read_text(encoding="utf-8")
        assert re.search(r"""\(\s*['"]src/personas['"]\s*,\s*['"]src/personas['"]\s*\)""", text), spec


def _string_literals(tree):
    """Every string constant that is not a docstring."""
    import ast
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


def test_no_real_persona_or_group_name_is_left_in_the_code():
    """Names live in data files; docstrings and comments may still use them as examples."""
    import ast
    offenders = []
    for path in (ROOT / "src").rglob("*.py"):
        for number, value in _string_literals(ast.parse(path.read_text(encoding="utf-8"))):
            for name in ("杨宇靖", "林小满", "群主应该是pyc", "次元", "牛牛哥哥"):
                if name in value:
                    offenders.append(f"{path.relative_to(ROOT)}:{number}: {name}")
    assert offenders == [], offenders


# ── alias rules ─────────────────────────────────────────────────────────────

def test_the_shipped_alias_rules_build_the_old_hard_coded_patterns():
    rules = load_alias_rules(user_path=Path("/nonexistent/alias_rules.json"))
    assert rules.alias_pattern == "(?:次元(?:哥哥|大哥|哥)?|牛牛哥哥)"
    assert rules.bare_alias_pattern == "(?:次元(?:哥哥|哥|大哥)?)"
    assert rules.derived == (("次元", "cy"),)


def test_other_nicknames_need_only_a_data_file(tmp_path):
    rules_file = write(tmp_path, "alias_rules.json", {
        "aliases": [{"stem": "小胖", "suffixes": []}, {"stem": "胖虎", "suffixes": ["哥"]}],
        "bare_aliases": [], "derived": [{"prefix": "小胖", "alias": "xp"}]})
    extractor = GroupFactExtractor(rules=load_alias_rules(user_path=rules_file))
    facts = extractor.extract({"content": "叫阿强小胖"})
    assert [(f.subject, f.object) for f in facts] == [("阿强", "小胖"), ("阿强", "xp")]
    assert [f.object for f in extractor.extract({"content": "叫阿强胖虎哥"})] == ["胖虎哥"]
    assert extractor.extract({"content": "叫阿强次元哥哥"}) == []              # the old nickname is gone
    assert extractor.extract({"content": "阿强小胖"}) == []                     # no bare form configured


def test_alias_words_are_literal_so_a_pattern_typed_into_the_file_cannot_run():
    rules = AliasRules.from_dict({"aliases": [{"stem": "(?:a+)+$", "suffixes": ["(x+)+y"]}]})
    import re as regex
    assert regex.search(rules.alias_pattern, "(?:a+)+$(x+)+y") and not regex.search(rules.alias_pattern, "aaaa")
    started = time.perf_counter()
    GroupFactExtractor(rules=rules).extract({"content": "叫宇肆" + "a" * 5000 + "!"})
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize("bad", [
    [1], {"aliases": "x"}, {"aliases": [{"stem": ""}]}, {"aliases": [{"stem": "字" * 21}]},
    {"aliases": [{"stem": "a", "suffixes": "b"}]}, {"aliases": [{"stem": "a", "suffixes": [""]}]},
    {"aliases": [{"stem": f"s{i}"} for i in range(51)]}, {"derived": "x"}, {"derived": [1]},
    {"derived": [{"prefix": "a"}]},
])
def test_unusable_alias_rules_are_refused(bad):
    with pytest.raises(ValueError):
        AliasRules.from_dict(bad)


def test_a_broken_user_file_falls_back_to_the_shipped_rules(tmp_path, caplog):
    for content in ("{oops", json.dumps({"aliases": "x"}), json.dumps({"derived": "x"}), json.dumps([1])):
        bad = write(tmp_path, "alias_rules.json", content)
        assert load_alias_rules(user_path=bad).derived == (("次元", "cy"),)
    assert "ignored" in caplog.text


def test_with_no_rules_at_all_nothing_is_extracted(tmp_path):
    nothing = load_alias_rules(user_path=tmp_path / "a.json", shipped_path=tmp_path / "b.json")
    assert nothing == AliasRules()
    assert GroupFactExtractor(rules=nothing).extract({"content": "叫宇肆次元哥哥"}) == []


def test_the_default_extractor_still_finds_the_original_nickname():
    facts = GroupFactExtractor().extract({"content": "叫宇肆次元哥哥"})
    assert [(f.subject, f.object) for f in facts] == [("宇肆", "次元哥哥"), ("宇肆", "cy")]
