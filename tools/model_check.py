"""Check the model(s) MODEL_ROUTE uses before running the bot on them.

    python tools/model_check.py            # the local model (costs nothing)
    python tools/model_check.py --cloud    # the cloud model too (a few thousand tokens)

For each model it reports:

* reachable, and how long a short answer takes;
* whether the answer still contains a ``<think>`` part (thinking was not
  switched off: see LOCAL_EXTRA_BODY in .env.example; the bot removes it, but
  it costs time and tokens);
* whether the JSON mode works (the structured pipeline needs it);
* context: a long prompt (about as long as the bot's) with a code word at the
  very start; a model that cannot repeat it lost the start, and with it the
  persona and the rules.  On Ollama raise the context to 8192 or more.

Settings come from .env like the bot's.  Exit status 1 when a check failed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.config import load_config  # noqa: E402
from src.pipeline.envelope import EnvelopeError, extract_envelope_dict  # noqa: E402
from src.summarize import create_local_summarizer, create_summarizer  # noqa: E402

CODE_WORD = "蓝莓-4721"
FILLER = "这是一段用来占位的聊天记录，没有任何需要记住的内容。"   # 25 characters
FILLER_REPEAT = 340      # about 8500 characters, roughly 6000 tokens: the size of the bot's prompts


def _timed(fn):
    started = time.monotonic()
    result = fn()
    return result, time.monotonic() - started


def check_model(label: str, model, *, long_prompt: bool, out) -> bool:
    print(f"== {label}: {model.model} ({getattr(model, 'base_url', '')})", file=out, flush=True)
    ok = True
    strip = getattr(model, "strip_reasoning", False)
    hello = ("你在做连通性测试。只回复两个字：在的", [{"role": "user", "content": "在吗"}])
    reasoning = ""
    try:
        model.strip_reasoning = False                    # look at the raw answer
        if hasattr(model, "_create_chat_completion"):
            # The whole response: Ollama puts the thinking in message.reasoning,
            # not in the text, so the text alone cannot show it.
            data, seconds = _timed(lambda: model._create_chat_completion(
                [{"role": "system", "content": hello[0]}, *hello[1]], max_tokens=260))
            message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
            raw = str(message.get("content") or "")
            reasoning = str(message.get("reasoning") or message.get("reasoning_content") or "")
        else:
            raw, seconds = _timed(lambda: model._call_chat_api(*hello))
    except Exception as exc:                             # noqa: BLE001 - report, do not crash
        print(f"   FAIL  not reachable: {exc}", file=out)
        return False
    finally:
        model.strip_reasoning = strip
    thinks = bool(reasoning.strip()) or "<think>" in raw.lower() or "</think>" in raw.lower()
    print(f"   ok    answered in {seconds:.1f}s: {raw.strip()[-40:]!r}", file=out)
    if thinks:
        print(f"   WARN  the model thought before answering ({len(reasoning) or 'a <think> part in the'} "
              f"{'characters of reasoning' if reasoning else 'answer'}): thinking is still on "
              "(the bot drops it, but it is slow); see LOCAL_EXTRA_BODY", file=out)

    try:
        output, seconds = _timed(lambda: model._call_json_api(
            '只输出一个 JSON 对象：{"action":"reply","messages":["好的"]}',
            [{"role": "user", "content": "测试"}]))
        data = extract_envelope_dict(output.text)
        print(f"   ok    JSON in {seconds:.1f}s (native JSON mode: {'yes' if output.native_json else 'no'}): "
              f"{json.dumps(data, ensure_ascii=False)[:60]}", file=out)
    except (EnvelopeError, Exception) as exc:            # noqa: BLE001
        print(f"   FAIL  no usable JSON: {exc}", file=out)
        ok = False

    if long_prompt:
        prompt = f"开头的暗号是：{CODE_WORD}。\n" + FILLER * FILLER_REPEAT + "\n问题：最开头的暗号是什么？只回复暗号本身。"
        try:
            answer, seconds = _timed(lambda: model._call_chat_api(
                "你是测试助手。", [{"role": "user", "content": prompt}]))
        except Exception as exc:                         # noqa: BLE001
            print(f"   FAIL  the long prompt failed: {exc}", file=out)
            return False
        if "4721" in answer:
            print(f"   ok    long prompt (~6000+ tokens) kept whole, {seconds:.1f}s", file=out)
        else:
            print(f"   FAIL  the start of a long prompt was lost (answer {answer.strip()[:30]!r}): "
                  "raise the context window to 8192 or more (Ollama: OLLAMA_CONTEXT_LENGTH)", file=out)
            ok = False
    return ok


def main(argv=None, out=sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cloud", action="store_true", help="also check the cloud model (costs a little)")
    args = parser.parse_args(argv)
    config = load_config()
    results = []
    if config.local_model:
        results.append(check_model("local", create_local_summarizer(config), long_prompt=True, out=out))
    else:
        print("== local: no LOCAL_MODEL in .env, skipped", file=out)
    if args.cloud:
        if config.model_route == "local":
            print("== cloud: MODEL_ROUTE=local, skipped", file=out)
        else:
            results.append(check_model("cloud", create_summarizer(config), long_prompt=False, out=out))
    if not results:
        return 1
    print("== " + ("ALL OK" if all(results) else "SOMETHING FAILED (see above)"), file=out)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
