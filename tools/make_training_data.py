"""Training data for fine-tuning the local model on the cloud model's answers.

    python tools/make_training_data.py synth --n 200 --out data/train/synth.jsonl          # plan
    python tools/make_training_data.py synth --n 200 --out data/train/synth.jsonl --yes    # cloud writes chats
    python tools/make_training_data.py teach --cases data/train/synth.jsonl --out data/train/train.jsonl --yes
    python tools/make_training_data.py check --data data/train/train.jsonl --cases data/train/synth.jsonl
    python tools/make_training_data.py persona      # which persona the teacher would answer as
    python tools/make_training_data.py persona --card --id mine >> data/train/personas.jsonl
    python tools/make_training_data.py personas --n 60 --out data/train/personas.jsonl --yes
    python tools/make_training_data.py teach --cases ... --personas data/train/personas.jsonl --out ... --yes

The goal is a model that plays ANY persona it is given, not one persona: so
``personas`` has the cloud model write many different persona cards
(evals/persona_cards.py), and ``teach --personas`` gives every scenario one of
them at random (half the time the group also calls the bot by the persona's
name).  The evaluation personas (evals/cases/personas_eval_v1.jsonl) are never
used for training.

``synth`` asks the cloud model in .env to invent group chats of the kinds the
bot meets (requests, banter, follow-ups, chats it should stay out of, open
topics it may join), as ``replay-case/v1`` scenarios.

``teach`` replays every scenario through the real router and pipeline with
the two-step strategy and the compact prompt (LOCAL_PROMPT=compact), with the
cloud model behind it: every prompt the pipeline sends (the "should I speak?"
step, the reply) becomes one training chat, the cloud model's answer its
target.  So the local model is trained on exactly the prompts it will see.
Scenarios exported from your own chats (tools/export_replay_cases.py, de-
identified) go in the same way; their text reaches the cloud model, as it
does when the bot replies with it.

``check`` counts what is there and refuses scenarios that resemble the
evaluation sets (evals/cases), which must never be trained on.

Like the evaluations, ``synth`` and ``teach`` only print a plan without --yes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals import normalize_case, validate_case  # noqa: E402
from evals.persona_cards import apply_card, card_from_settings, check_card, held_out_names, load_cards  # noqa: E402
from evals import run_offline  # noqa: E402
from src.pipeline.envelope import EnvelopeError, extract_envelope_dict  # noqa: E402

EVAL_CASES = sorted((REPO_ROOT / "evals" / "cases").glob("*.jsonl"))
SYNTH_BATCH = 8
SYNTH_TOKENS_PER_CHAT = 450
#: Seconds the cloud model may take: writing a batch of chats is long, and the
#: relay is sometimes slow (the bot's own 25 s would cut both off).
SYNTH_TIMEOUT_SEC = 240
TEACH_TIMEOUT_SEC = 120
TEACH_STRATEGY = "two_step"
#: A scenario this similar to an evaluation scenario (character 4-grams) is not trained on.
MAX_EVAL_SIMILARITY = 0.5
BOT_NAMES = ("小助手", "宇靖", "小杨")

# What the bot meets in a group, and how often (weights).  Each kind tells the
# writer what the latest message must be like; whether to answer is left to
# the teacher.
KINDS: dict[str, tuple[int, str]] = {
    "at_request": (5, "有人 @机器人 提一个具体请求：讲笑话、帮忙在两样里选一个、解释一个词或梗、出主意、推荐、算个简单的数、安排计划"),
    "at_self": (2, "有人 @机器人 问它自己的事：多大、学什么、喜欢什么、在干嘛、是不是机器人"),
    "at_feeling": (2, "有人 @机器人 倾诉烦恼、难过，或者分享好消息"),
    "at_banter": (2, "有人 @机器人 开玩笑、调侃它、让它夸人"),
    "at_unclear": (1, "有人 @机器人 但说得含糊，看不出要什么"),
    "followup": (3, "机器人刚在群里说过一句（写在 context 里，name 用机器人的名字），对方接着追问或反驳"),
    "name_call": (2, "有人提到机器人的名字但没有 @：有时是在叫它，有时只是在跟别人谈论它"),
    "ambient_open": (3, "群里在聊一个开放的话题，没人叫机器人，但它自然地插一句也合适"),
    "ambient_closed": (2, "群友之间一问一答，话题已经说完整了，机器人插话会很突兀"),
    "between_others": (2, "两个群友在互相说话，最新消息 @ 的是别人"),
    "short_reaction": (2, "最新消息只是“哈哈”“？”“笑死”“好”这样的短反应"),
    "meta_bot": (1, "群友在跟别人评价机器人刚才的回复，并不是在对它说话"),
}
THEMES = ("吃饭点外卖", "考试复习", "追剧综艺", "打游戏", "周末出游", "工作吐槽", "宠物", "天气穿搭",
          "考研考公", "租房搬家", "健身减肥", "数码产品", "音乐演唱会", "八卦明星", "做饭", "生日聚会",
          "实习找工作", "宿舍生活", "旅行攻略", "读书", "看球", "理财省钱", "失眠熬夜", "猫狗趣事")
SPEAKERS = ("阿明", "小红", "大伟", "小李", "婷婷", "老周", "佳佳", "阿杰", "小雨", "浩子")


# ── synth: the cloud model writes chats ───────────────────────────────────

def synth_prompt(kinds: Sequence[str], themes: Sequence[str], bot_name: str) -> str:
    wanted = "\n".join(f"{i}. 类型 {kind}：{KINDS[kind][1]}；话题：{theme}"
                       for i, (kind, theme) in enumerate(zip(kinds, themes), 1))
    return f"""请编写 {len(kinds)} 段中文微信群聊，用来测试一个叫「{bot_name}」的群聊机器人。每段对应下面一条要求：
{wanted}

要求：
- 每段 3 到 8 条更早的消息（context），加 1 条最新消息（latest）。像真实的年轻人群聊：口语、短句、有错别字和省略也行。
- 群友名字从这些里选：{"、".join(SPEAKERS)}。机器人自己说过的话 name 写「{bot_name}」。
- @机器人 时，latest.text 里要写 “@{bot_name}”，并把 at_bot 设为 true；没有 @ 机器人就是 false。
- 只写聊天，不要写机器人对 latest 的回复，不要解释。

只输出一个 JSON 对象：{{"chats": [{{"kind": "类型", "group_name": "群名", "context": [{{"name": "名字", "text": "消息"}}], "latest": {{"name": "名字", "text": "消息", "at_bot": true}}}}]}}"""


def to_case(chat: Mapping[str, Any], *, index: int, bot_name: str) -> dict | None:
    """A written chat as a replay-case/v1 scenario; None when it is unusable."""
    try:
        latest = chat["latest"]
        context = [m for m in chat.get("context") or [] if str(m.get("text") or "").strip()]
        text, name = str(latest["text"]).strip(), str(latest["name"]).strip()
    except (KeyError, TypeError, AttributeError):
        return None
    if not text or not name or name == bot_name:
        return None
    at_bot = bool(latest.get("at_bot"))
    if at_bot and f"@{bot_name}" not in text:
        text = f"@{bot_name} {text}"
    ids: dict[str, str] = {bot_name: "u_bot"}

    def sender(person: str) -> str:
        return ids.setdefault(person, f"u_{len(ids)}")

    offsets = [-45 * (len(context) - i) for i in range(len(context))]
    messages = [{"offset_sec": offset, "sender_id": sender(str(m.get("name") or "某人")),
                 "sender_name": str(m.get("name") or "某人"), "content": str(m["text"]).strip(), "msg_type": 1}
                for m, offset in zip(context, offsets)]
    bot_lines = [m for m in messages if m["sender_id"] == "u_bot"]
    kind = str(chat.get("kind") or "synthetic")
    case = {
        "schema": "replay-case/v1",
        "id": "syn-" + hashlib.sha256(json.dumps(chat, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:10],
        "category": kind if kind in KINDS else "synthetic",
        "chat": {"chat_id": f"g_syn{index % 50}@chatroom", "group_name": str(chat.get("group_name") or "群聊"),
                 "is_group": True},
        "bot_name": bot_name, "now": 1765000000 + index * 1000,
        "context": messages,
        "trigger": {"sender_id": sender(name), "sender_name": name, "content": text, "msg_type": 1,
                    "is_at_mentioned": at_bot, "is_reply_to_bot": False, "quoted_content": "",
                    "quoted_sender_name": "", "offset_sec": 0},
        "next_messages": [],
        "bot_state": None if not bot_lines else {
            "last_bot_reply": {"to_sender_id": sender(name), "text": bot_lines[-1]["content"],
                               "offset_sec": bot_lines[-1]["offset_sec"]},
            "sticky_for": []},
        "expect": None, "config_overrides": {}, "source": "synthetic",
    }
    case = normalize_case(case)
    return None if validate_case(case, allow_unlabeled=True) else case


def synth(cloud, n: int, *, seed: int, batch: int = SYNTH_BATCH,
          progress: Callable[[str], None] | None = None) -> list[dict]:
    rng = random.Random(seed)
    names, weights = list(KINDS), [KINDS[k][0] for k in KINDS]
    cases: list[dict] = []
    seen: set[str] = set()
    calls = 0
    while len(cases) < n and calls < 3 * (n // batch + 1):
        calls += 1
        size = min(batch, n - len(cases))
        kinds = rng.choices(names, weights=weights, k=size)
        themes = rng.sample(THEMES, k=size)
        bot_name = rng.choice(BOT_NAMES)
        try:
            output = cloud._call_json_api("你是编剧，只输出 JSON。",
                                          [{"role": "user", "content": synth_prompt(kinds, themes, bot_name)}],
                                          max_tokens=SYNTH_TOKENS_PER_CHAT * size + 200)
            chats = extract_envelope_dict(getattr(output, "text", output)).get("chats") or []
        except Exception as exc:                          # noqa: BLE001 - the next batch tries again
            if progress:
                progress(f"batch {calls} failed: {type(exc).__name__}")
            continue
        for chat in chats if isinstance(chats, list) else []:
            case = to_case(chat, index=len(cases), bot_name=bot_name) if isinstance(chat, dict) else None
            if case and case["id"] not in seen:
                seen.add(case["id"])
                cases.append(case)
        if progress:
            progress(f"{len(cases)}/{n} chats after {calls} calls")
    return cases[:n]


# ── personas: the cloud model writes persona cards ───────────────────────

PERSONA_BATCH = 5
PERSONA_TOKENS_PER_CARD = 500
AGES = ("十几岁的中学生", "二十出头的大学生", "二十多岁刚工作", "三十多岁", "四五十岁", "六十岁以上")
ROLES = ("程序员", "护士", "外卖骑手", "小学老师", "厨师", "健身教练", "主播", "销售", "公务员", "研究生",
         "退休工人", "宝妈", "设计师", "出租车司机", "医生", "律师", "大学辅导员", "店主", "农民", "画家")
ORIGINS = ("现实里的普通人", "现实里的普通人", "网络小说里的角色", "动漫或游戏里的角色", "影视剧里的角色")
TEMPERS = ("外向话多", "内向慢热", "毒舌但心软", "温柔耐心", "严肃认真", "搞笑沙雕", "高冷寡言", "憨厚老实",
           "中二", "傲娇", "暴躁直率", "佛系")
STYLES = ("带方言口音", "爱用网络流行语", "说话文绉绉", "极简短", "爱用比喻", "爱反问", "口头禅很多", "说话很正式")
LIMITS = (12, 20, 30, 40, 60, 100)


def personas_prompt(specs: Sequence[dict]) -> str:
    wanted = "\n".join(
        f"{i}. {s['age']}，{s['role']}，{s['origin']}，性格{s['temper']}，说话{s['style']}；每条回复不超过 {s['limit']} 字"
        for i, s in enumerate(specs, 1))
    return f"""请设计 {len(specs)} 个中文微信群聊里的人设，每个对应下面一条要求：
{wanted}

每个人设写：
- persona_name：名字或外号（不要用：{"、".join(sorted(held_out_names()))}）
- persona_prompt：150 到 400 字，用“你是……”开头，写清身份、经历、性格、说话方式、口头禅、和群友的关系分寸
- reply_rules：2 到 4 句说话规则，第一句必须是“每条回复不超过 N 字”（N 用上面给的数）；再写清能不能用 emoji、颜文字、感叹号等，各人设要不一样
- max_chars：上面给的数字

只输出一个 JSON 对象：{{"personas": [{{"persona_name": "", "persona_prompt": "", "reply_rules": "", "max_chars": 30}}]}}"""


def synth_personas(cloud, n: int, *, seed: int, batch: int = PERSONA_BATCH,
                   progress: Callable[[str], None] | None = None) -> list[dict]:
    rng = random.Random(seed)
    banned = held_out_names()
    cards: list[dict] = []
    calls = 0
    while len(cards) < n and calls < 3 * (n // batch + 1):
        calls += 1
        specs = [{"age": rng.choice(AGES), "role": rng.choice(ROLES), "origin": rng.choice(ORIGINS),
                  "temper": rng.choice(TEMPERS), "style": rng.choice(STYLES), "limit": rng.choice(LIMITS)}
                 for _ in range(min(batch, n - len(cards)))]
        try:
            output = cloud._call_json_api("你是编剧，只输出 JSON。",
                                          [{"role": "user", "content": personas_prompt(specs)}],
                                          max_tokens=PERSONA_TOKENS_PER_CARD * len(specs) + 200)
            written = extract_envelope_dict(getattr(output, "text", output)).get("personas") or []
        except Exception as exc:                          # noqa: BLE001 - the next batch tries again
            if progress:
                progress(f"batch {calls} failed: {type(exc).__name__}")
            continue
        for item in written if isinstance(written, list) else []:
            if not isinstance(item, dict):
                continue
            card = {"id": "", "persona_name": str(item.get("persona_name") or "").strip(),
                    "persona_prompt": str(item.get("persona_prompt") or "").strip(),
                    "reply_rules": str(item.get("reply_rules") or "").strip(),
                    "max_chars": item.get("max_chars")}
            card["id"] = "p-" + hashlib.sha256(card["persona_prompt"].encode("utf-8")).hexdigest()[:8]
            if (check_card(card) or card["persona_name"] in banned
                    or any(c["persona_name"] == card["persona_name"] or c["id"] == card["id"] for c in cards)):
                continue
            cards.append(card)
        if progress:
            progress(f"{len(cards)}/{n} personas after {calls} calls")
    return cards[:n]


def as_card(case: dict, card: Mapping[str, Any], *, rename: bool) -> dict:
    """``case`` for a bot playing ``card``; ``rename``: the group also calls it by the card's name."""
    case = json.loads(json.dumps(case, ensure_ascii=False))
    if not rename or case["bot_name"] == card["persona_name"]:
        return case
    old, new = case["bot_name"], card["persona_name"]
    for message in [*case.get("context", []), case["trigger"]]:
        message["content"] = str(message.get("content", "")).replace(f"@{old}", f"@{new}")
        if message.get("sender_name") == old:
            message["sender_name"] = new
    case["bot_name"] = new
    return case


# ── teach: the cloud model answers what the pipeline asks ─────────────────

class Recorder:
    """Wraps a model's JSON call and keeps every (system, messages, answer)."""

    def __init__(self, model):
        self.model = model
        self.calls: list[tuple[str, list, str]] = []
        self._call = model._call_json_api
        model._call_json_api = self.call

    def call(self, system, messages, **kwargs):
        output = self._call(system, messages, **kwargs)
        self.calls.append((system, [dict(m) for m in messages], str(getattr(output, "text", output) or "")))
        return output


_LIMIT_RE = re.compile(r"不超过 (\d+) 个字")
_REWRITE_MARK = "你上一条回复太长了"


def _answer(text: str) -> dict | None:
    try:
        data = extract_envelope_dict(text)
    except EnvelopeError:
        return None
    return data if "action" in data else None


def _too_long(system: str, data: Mapping[str, Any]) -> bool:
    """Whether the reply would be cut: longer than the prompt's character limit."""
    limit = _LIMIT_RE.search(system)
    messages = data.get("messages") or []
    if not limit or not isinstance(messages, list):
        return False
    return sum(len(str(m)) for m in messages) > int(limit.group(1))


def training_rows(case_id: str, source: str, calls: Iterable[tuple[str, list, str]]) -> list[dict]:
    """The first-ask calls of one scenario as training chats.

    A reply the pipeline asked to shorten (PIPELINE_REWRITE_LONG) takes the
    shortened answer as its target; a reply still over the prompt's character
    limit is left out, so the model is not taught answers the bot would cut.
    Repair calls are left out.
    """
    rows: list[dict] = []
    by_prompt: dict[tuple[str, str], dict] = {}
    for system, messages, answer in calls:
        if len(messages) == 3 and _REWRITE_MARK in str(messages[2].get("content", "")):
            row = by_prompt.get((system, messages[0].get("content", "")))
            data = _answer(answer)
            if row is not None and data is not None:
                row["messages"][-1]["content"] = json.dumps(data, ensure_ascii=False)
                row["meta"]["rewritten"] = True
                row["_data"] = data
            continue
        if len(messages) != 1:
            continue
        data = _answer(answer)
        if data is None:
            continue
        stage = "decide" if "判断器" in system else "reply"
        row = {
            "messages": [{"role": "system", "content": system}, dict(messages[0]),
                         {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)}],
            "meta": {"case": case_id, "source": source, "stage": stage, "action": data.get("action")},
            "_data": data,
        }
        by_prompt[(system, messages[0].get("content", ""))] = row
        rows.append(row)
    kept = []
    for row in rows:
        data = row.pop("_data")
        if row["meta"]["stage"] == "reply" and _too_long(row["messages"][0]["content"], data):
            continue
        kept.append(row)
    return kept


def teach(teacher, cases: Sequence[dict], *, seed: int, personas: Sequence[dict] = (),
          progress: Callable[[str], None] | None = None) -> list[dict]:
    from evals.run_live import LiveSummarizer

    teacher.prompt_profile = "compact"
    teacher.pipeline_rewrite_long = True                  # a too long answer is shortened, not cut
    recorder = Recorder(teacher)
    profile = run_offline.make_profile(TEACH_STRATEGY, "dice")
    rows: list[dict] = []
    with run_offline._hermetic_cwd() as workdir, run_offline._quiet_logging():
        image_path = workdir / "img.png"
        image_path.write_bytes(run_offline._PNG_1X1)
        rng = random.Random(seed)
        for index, case in enumerate(cases, 1):
            recorder.calls.clear()
            card = rng.choice(list(personas)) if personas else None
            if card is not None:
                apply_card(teacher, card)
                case = as_card(case, card, rename=rng.random() < 0.5)
            try:
                run_offline.replay_case(case, workdir=workdir, image_path=str(image_path), seed=seed,
                                        profile=profile, summarizer=LiveSummarizer(teacher), keep_reply=True)
            except Exception as exc:                      # noqa: BLE001 - one bad scenario, not the run
                if progress:
                    progress(f"{case['id']} failed: {type(exc).__name__}")
            new_rows = training_rows(case["id"], case.get("source", ""), recorder.calls)
            for row in new_rows:
                row["meta"]["persona"] = card["id"] if card is not None else "config"
            rows += new_rows
            if progress:
                progress(f"{index}/{len(cases)} scenarios, {len(rows)} rows")
    return rows


# ── check ────────────────────────────────────────────────────────────────

def _grams(text: str, n: int = 4) -> set[str]:
    text = "".join(str(text).split())
    return {text[i:i + n] for i in range(max(0, len(text) - n + 1))}


def case_text(case: Mapping[str, Any]) -> str:
    lines = [m.get("content", "") for m in (case.get("context") or [])[-3:]]
    return "".join(lines) + str((case.get("trigger") or {}).get("content", ""))


def similar_to_evals(cases: Sequence[Mapping], eval_cases: Sequence[Mapping],
                     limit: float = MAX_EVAL_SIMILARITY) -> list[tuple[str, str, float]]:
    """(case id, evaluation id, similarity) for every case too close to an evaluation scenario."""
    eval_grams = [(c["id"], _grams(case_text(c))) for c in eval_cases]
    found = []
    for case in cases:
        grams = _grams(case_text(case))
        if not grams:
            continue
        best = max(((eid, len(grams & g) / len(grams | g)) for eid, g in eval_grams if g),
                   key=lambda item: item[1], default=("", 0.0))
        if best[1] >= limit:
            found.append((case["id"], best[0], round(best[1], 3)))
    return found


def load_eval_cases() -> list[dict]:
    cases = []
    for path in EVAL_CASES:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("schema") == "replay-case/v1":
                    cases.append(row)
    return cases


def stats(rows: Sequence[Mapping]) -> dict:
    stages = Counter(r["meta"]["stage"] for r in rows)
    actions = Counter((r["meta"]["stage"], r["meta"]["action"]) for r in rows)
    prompt_chars = [sum(len(m["content"]) for m in r["messages"][:-1]) for r in rows]
    duplicates = len(rows) - len({json.dumps(r["messages"], ensure_ascii=False) for r in rows})
    return {
        "rows": len(rows), "scenarios": len({r["meta"]["case"] for r in rows}),
        "stages": dict(stages), "actions": {f"{s}/{a}": n for (s, a), n in sorted(actions.items())},
        "prompt_chars_max": max(prompt_chars, default=0),
        "prompt_chars_mean": round(sum(prompt_chars) / len(prompt_chars)) if prompt_chars else 0,
        "duplicates": duplicates,
    }


# ── command line ─────────────────────────────────────────────────────────

def _read_jsonl(path: str | Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: str | Path, rows: Iterable[Mapping]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _cloud():
    from src.config import load_config
    from src.summarize import create_summarizer
    return create_summarizer(load_config())


def _persona_line(model) -> str:
    persona = model._chat_persona_settings()
    digest = hashlib.sha256(str(persona.get("persona_prompt", "")).encode("utf-8")).hexdigest()[:8]
    return f"persona {persona.get('persona_name')} (prompt sha256 {digest}), model {getattr(model, 'model', '?')}"


def main(argv=None, cloud_factory: Callable[[], Any] = _cloud) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_synth = sub.add_parser("synth", help="the cloud model invents scenarios")
    p_synth.add_argument("--n", type=int, required=True)
    p_synth.add_argument("--batch", type=int, default=SYNTH_BATCH)
    p_synth.add_argument("--seed", type=int, default=7)
    p_synth.add_argument("--out", required=True)
    p_synth.add_argument("--yes", action="store_true")
    p_teach = sub.add_parser("teach", help="the cloud model answers the pipeline's prompts")
    p_teach.add_argument("--cases", nargs="+", required=True)
    p_teach.add_argument("--limit", type=int, default=0)
    p_teach.add_argument("--personas", help="persona cards: each scenario gets one at random "
                                           "(without it the persona in .env answers everything)")
    p_teach.add_argument("--seed", type=int, default=7)
    p_teach.add_argument("--out", required=True)
    p_teach.add_argument("--yes", action="store_true")
    p_check = sub.add_parser("check", help="statistics and the overlap with the evaluation sets")
    p_check.add_argument("--data", help="training JSONL to count")
    p_check.add_argument("--cases", nargs="*", default=[])
    p_persona = sub.add_parser("persona", help="the persona in .env (name and a fingerprint of its text)")
    p_persona.add_argument("--card", action="store_true", help="print it as a persona card (JSON) instead")
    p_persona.add_argument("--id", default="config", help="the card's id with --card")
    p_personas = sub.add_parser("personas", help="the cloud model writes persona cards")
    p_personas.add_argument("--n", type=int, required=True)
    p_personas.add_argument("--batch", type=int, default=PERSONA_BATCH)
    p_personas.add_argument("--seed", type=int, default=7)
    p_personas.add_argument("--out", required=True)
    p_personas.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    say = lambda text: print(f"\r{text}    ", end="", file=sys.stderr)  # noqa: E731

    if args.command == "persona":
        model = cloud_factory()
        if args.card:
            print(json.dumps(card_from_settings(model._chat_persona_settings(), args.id), ensure_ascii=False))
        else:
            print(_persona_line(model))
        return 0

    if args.command == "personas":
        print(f"personas: {args.n} cards in about {-(-args.n // args.batch)} calls of {args.batch}")
        if not args.yes:
            print("Dry run: nothing was sent. Add --yes to call the cloud model in .env.")
            return 0
        cloud = cloud_factory()
        cloud.timeout_floor = SYNTH_TIMEOUT_SEC
        cards = synth_personas(cloud, args.n, seed=args.seed, batch=args.batch, progress=say)
        print(file=sys.stderr)
        _write_jsonl(args.out, cards)
        print(f"wrote {len(cards)} persona cards to {args.out}; reply limits "
              f"{dict(Counter(c['max_chars'] for c in cards))}")
        return 0

    if args.command == "synth":
        calls = -(-args.n // args.batch)
        print(f"synth: {args.n} scenarios in about {calls} calls of {args.batch} "
              f"(plus retries for broken batches)")
        if not args.yes:
            print("Dry run: nothing was sent. Add --yes to call the cloud model in .env.")
            return 0
        cloud = cloud_factory()
        cloud.timeout_floor = SYNTH_TIMEOUT_SEC
        cases = synth(cloud, args.n, seed=args.seed, batch=args.batch, progress=say)
        print(file=sys.stderr)
        close = similar_to_evals(cases, load_eval_cases())
        cases = [c for c in cases if c["id"] not in {cid for cid, _, _ in close}]
        _write_jsonl(args.out, cases)
        print(f"wrote {len(cases)} scenarios to {args.out} ({len(close)} too close to the evaluation sets left out); "
              f"kinds: {dict(Counter(c['category'] for c in cases))}")
        return 0

    if args.command == "teach":
        cases, _ = run_offline._load_cases([Path(p).resolve() for p in args.cases])
        if args.limit:
            cases = cases[: args.limit]
        close = similar_to_evals(cases, load_eval_cases())
        if close:
            print(f"refused: {len(close)} scenarios resemble the evaluation sets, e.g. {close[:3]}; "
                  "remove them first", file=sys.stderr)
            return 2
        personas = load_cards(args.personas) if args.personas else []
        overlap = {c["persona_name"] for c in personas} & held_out_names()
        if overlap:
            print(f"refused: evaluation personas in --personas: {sorted(overlap)}", file=sys.stderr)
            return 2
        teacher = cloud_factory()
        teacher.timeout_floor = TEACH_TIMEOUT_SEC
        who = f"{len(personas)} persona cards at random" if personas else _persona_line(teacher)
        print(f"teach: {len(cases)} scenarios, strategy {TEACH_STRATEGY}, compact prompt; teacher {who}")
        print("  about 1-2 cloud calls per scenario that reaches the model")
        if not args.yes:
            print("Dry run: nothing was sent. Add --yes to call the cloud model in .env.")
            return 0
        rows = teach(teacher, cases, seed=args.seed, personas=personas, progress=say)
        print(file=sys.stderr)
        _write_jsonl(args.out, rows)
        print(f"wrote {len(rows)} training chats to {args.out}: {json.dumps(stats(rows), ensure_ascii=False)}")
        return 0

    if not args.data and not args.cases:
        parser.error("check needs --data, --cases or both")
    if args.data:
        print(json.dumps(stats(_read_jsonl(args.data)), ensure_ascii=False, indent=2))
    if args.cases:
        cases = [c for path in args.cases for c in _read_jsonl(path)]
        close = similar_to_evals(cases, load_eval_cases())
        print(f"scenarios too close to the evaluation sets: {len(close)} {close[:5]}")
        return 1 if close else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
