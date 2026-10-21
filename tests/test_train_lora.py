"""tools/finetune/train_lora.py: the parts that run without a GPU."""

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "train_lora", Path(__file__).resolve().parents[1] / "tools" / "finetune" / "train_lora.py")
T = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(T)


class FakeTokenizer:
    """One token per character; a ChatML-like template that knows enable_thinking."""

    def __init__(self):
        self.thinking = []

    def apply_chat_template(self, messages, tokenize=False, enable_thinking=True):
        self.thinking.append(enable_thinking)
        return "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in messages)

    def __call__(self, text):
        return {"input_ids": list(text)}


def test_rows_must_end_with_the_answer(tmp_path):
    good = {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
                         {"role": "assistant", "content": "{}"}]}
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(good) + "\n\n", encoding="utf-8")
    assert T.load_rows(path) == [good]
    path.write_text(json.dumps({"messages": good["messages"][:2]}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=":1: the last message"):
        T.load_rows(path)
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="no rows"):
        T.load_rows(path)


def test_training_text_has_thinking_off_and_the_markers():
    tokenizer = FakeTokenizer()
    text = T.render(tokenizer, [{"role": "user", "content": "在吗"}, {"role": "assistant", "content": "{}"}])
    assert tokenizer.thinking == [False]
    assert T.INSTRUCTION_PART in text and T.RESPONSE_PART in text


def test_smoke_rows_are_as_long_as_allowed():
    tokenizer = FakeTokenizer()
    rows = T.smoke_rows(tokenizer, 2048, 3)
    assert len(rows) == 3 and rows[0] == rows[2] and rows[0] is not rows[2]
    length = len(T.render(tokenizer, rows[0]["messages"]))
    assert 2048 - 8 - len(T.SMOKE_LINE) <= length <= 2048 - 8
    assert rows[0]["messages"][-1]["content"] == T.SMOKE_ANSWER


def test_only_accepted_keywords_are_passed():
    def config(dataset_text_field=None, max_length=None):
        return None
    assert T._accepted(config, dataset_text_field="text", max_seq_length=1, max_length=2) == {
        "dataset_text_field": "text", "max_length": 2}


def test_without_data_or_smoke_it_stops_before_importing_anything(capsys):
    with pytest.raises(SystemExit):
        T.main([])
    assert "give --data, or --smoke" in capsys.readouterr().err



class FakeProcessor:
    """Like Qwen3.5's processor: the first argument is images, the tokenizer is inside."""

    def __init__(self):
        self.tokenizer = FakeTokenizer()

    def apply_chat_template(self, messages, tokenize=False, enable_thinking=True):
        return self.tokenizer.apply_chat_template(messages, tokenize=tokenize, enable_thinking=enable_thinking)

    def __call__(self, images=None, text=None):
        if images is not None:
            raise ValueError("Incorrect image source")              # what round 4 hit
        return self.tokenizer(text)


def test_smoke_rows_work_with_a_processor():
    processor = FakeProcessor()
    rows = T.smoke_rows(processor, 512, 2)
    assert len(T.render(processor, rows[0]["messages"])) <= 512 - 8
    assert T.text_tokenizer(processor) is processor.tokenizer
    plain = FakeTokenizer()
    assert T.text_tokenizer(plain) is plain



def test_rows_longer_than_the_limit_are_left_out_not_cut():
    processor = FakeProcessor()
    short = {"messages": [{"role": "user", "content": "在吗"}, {"role": "assistant", "content": "{}"}]}
    long = {"messages": [{"role": "user", "content": "长" * 600}, {"role": "assistant", "content": "{}"}]}
    kept, dropped = T.fitting_rows(processor, [short, long, short], 200)
    assert kept == [short, short] and dropped == 1



def test_memory_settings_are_set_before_the_imports():
    env = {}
    T.configure_memory(128, env)
    assert env == {"UNSLOTH_CE_LOSS_N_CHUNKS": "128", "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"}
    env = {"PYTORCH_CUDA_ALLOC_CONF": "mine"}
    T.configure_memory(0, env)
    assert env == {"PYTORCH_CUDA_ALLOC_CONF": "mine"}                # a user's own setting stays
