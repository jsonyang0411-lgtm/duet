"""直播手卡 (PRM_HANDCARDS, 2026-11-09; the user: the bot makes the cards itself, a person still looks before anything
goes to the creator).

Creators who will stream the shop's hats ask for 直播手卡: one card per product with the price, the commission, the
stock, the specs, the selling points and the shipping.  The kb answered 「好的老师，手卡我这边整理好发您～」 and an operator
had Claude make them on the Mac with the skill ``live-handcard`` (~/.claude/skills/live-handcard: its
make_cards_local.py takes one 单子.json and renders a 1600×1200 PNG per product).  Now a draft makes them:

1. The request.  The drafting Claude names the products in its answer, ``handcards`` (``REQUEST_SCHEMA``): the 抖店
   item ids of kb/products.md, the colours she picked, the cell of a 2×2 main image and whether its big brand letters
   at the bottom go (it looks at the picture), and the words it writes from the listing title (short name, brand
   line, audience, selling points).  It may when the bot's job says the bot takes cards (``meta.handcards_ok``,
   PRM_HANDCARDS on Windows) and she asks for them, or when an operator clicked 「做手卡」 in her deal
   (``meta.handcards`` {"by": "console", "items": the products of her latest sample order or the ticked ones}).
2. The cards, on the Mac (tools/claude_relay/worker.py ``do_prm_job`` → ``make``).  ``build_sheet`` turns the request
   into the skill's 单子.json from data only: the title, the price and the SKUs of kb/products.json, the sizes and
   colours of the hat index's skus.json (the SKU names for a code it does not have), the material from the title, the
   house 口径 (``globals``: the 老板模板) and the 双11 line (``promo``, until its end date) of ~/.claude-relay/handcards.json.
   Stock is always 「充足」 (the user, 2026-11-06: the real stock is the operator's); a field without a source is null
   (a red 待定 on the card); 直播价 = 日常价 × discount floored to the yuan (315 → 283), the script's own rule.  The main
   image is cropped (``crop_box``: its white side margins off, the cell asked for, its brand letters) with the skill's
   Python (``crop_photos``: Pillow), then the skill's script renders the cards.  The 1x PNGs go back as the draft's
   files and ``handcards_made`` (``made_block``) says which, with the operator's note: every 待定, what was inferred,
   which 口径 were reused, the crops.  All or nothing: a product without data, the script failing, a card missing:
   no card at all, and the verdict says why.
3. The bot (src/prm/service.py ``on_result``): a draft with a verdict always waits for a person (never by its kind's
   gate, the 下班时段, 自动模式 or PRM_OPENING_AUTO_MIN); a draft whose cards did not all arrive carries none of them
   (``keep_cards``), and its words cannot go out as they are (``PrmService.decide``).

Standard library only, Python 3.9: the Mac worker loads this file by its path with macOS's /usr/bin/python3 (like
platforms.py); the bot imports it as src.prm.handcards.  Only ``crop_photos`` needs Pillow: the worker runs it with
the skill's own Python (``python -I handcards.py crop <plan.json>``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Optional

REQUEST = "handcards"            # the drafting Claude's request, in its answer
MADE = "handcards_made"          # the Mac's verdict in its place, in the draft that goes back to the bot
OK_KEY = "handcards_ok"          # a draft job's meta: the bot takes cards (PRM_HANDCARDS on)
MAX_CARDS = 9                    # one draft carries at most 9 files (result.MAX_ATTACHMENTS, the relay's MAX_OUTFILES)
SHORT_MAX = 6                    # the card's 产品名称 holds about 6 Han characters (the skill: a longer one is cut off)
SELLS_MAX = 4                    # selling points on a card
# How long a written field may be on the card (its box does not wrap): longer is cut and the note says so.
TEXT_MAX = {"brand_line": 30, "audience": 24, "head": 14, "rest": 26}
CALL_MAX = 16
CELLS = {"左上": (0, 0), "右上": (1, 0), "左下": (0, 1), "右下": (1, 1)}
CUT_BOTTOM = 0.18                # a main image with big brand letters at its bottom loses its lowest 18% (as by hand)
CELL_INSET = 0.01                # a cell of a 2×2 image is cut 1% inside its edges (no sliver of the next one)
STOCK = "充足"                    # the user, 2026-11-06: the cards say 「充足」, the real stock is the operator's
NOTE_CHARS = 1500
PROBLEMS = 10
CARD_SIZE = (1600, 1200)         # the skill's 1x card
KEEP_DAYS = 7                    # a session's folder on the Mac (单子.json, photos, cards) is kept this long
GLOBAL_KEYS = ("commission", "pack", "ship_time", "aftersale", "ship_from", "no_ship", "express")
GLOBAL_LABELS = {"commission": "佣金", "pack": "包装", "ship_time": "发货", "aftersale": "售后", "ship_from": "发货地",
                 "no_ship": "不发货地", "express": "快递"}
TODO_LABELS = {"daily": "日常价", "material": "材质", "size": "尺码", "colors": "颜色"}

# The drafting Claude's ``handcards`` (result.SCHEMA and the worker's PRM_SCHEMA carry it; basic keywords only, as the
# rest of the schema: lengths and counts are this module's to check).
REQUEST_SCHEMA = {
    "type": "object",
    "properties": {
        "call": {"type": "string"},
        "products": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "colors": {"type": "array", "items": {"type": "string"}},
                "photo_cell": {"type": "string", "enum": ["", *CELLS]},
                "cut_bottom": {"type": "boolean"},
                "short": {"type": "string"},
                "brand_line": {"type": "string"},
                "material": {"type": "string"},
                "audience": {"type": "string"},
                "sells": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"head": {"type": "string"}, "rest": {"type": "string"}},
                    "required": ["head", "rest"], "additionalProperties": False}},
            },
            "required": ["item_id", "short", "brand_line", "audience", "sells"],
            "additionalProperties": False}},
    },
    "required": ["products"],
    "additionalProperties": False,
}


# ── the Mac's verdict (what the bot reads) ───────────────────────────────

def made_block(ok: bool, *, cards=(), items=(), note: str = "", problems=()) -> dict:
    """The Mac's verdict on a draft's request (``handcards_made``; the bot reads it with ``check_made``): ``ok`` with
    the cards' file names (the draft's files of those names), else ``problems`` (why no card was made)."""
    problems = [" ".join(str(p).split())[:300] for p in problems if str(p).strip()][:PROBLEMS]
    return {"version": 1, "ok": bool(ok) and bool(cards) and not problems, "cards": [str(c) for c in cards],
            "items": [dict(i) for i in items][:MAX_CARDS], "note": str(note or "")[:NOTE_CHARS], "problems": problems}


def check_made(raw) -> dict:
    """The Mac's verdict as the bot keeps it, ``{"ok", "cards", "items", "note", "problems"}`` (``ok`` only with cards
    and no problem); a ValueError for one it cannot use (``result.parse_draft`` refuses the draft then, as it does a
    malformed platform verdict)."""
    if not isinstance(raw, dict):
        raise ValueError("handcards_made is not an object")
    cards = raw.get("cards") if raw.get("cards") is not None else []
    if not isinstance(cards, list) or len(cards) > MAX_CARDS:
        raise ValueError("handcards_made.cards is not a list of at most 9 names")
    names: list = []
    for card in cards:
        if not isinstance(card, str) or not card.strip() or re.search(r"[\\/\x00-\x1f]", card):
            raise ValueError("a card name is not a file name")
        names.append(card.strip()[:120])
    problems = raw.get("problems") if raw.get("problems") is not None else []
    items = raw.get("items") if raw.get("items") is not None else []
    if not isinstance(problems, list) or not isinstance(items, list):
        raise ValueError("handcards_made.problems / items are not lists")
    problems = [" ".join(str(p).split())[:300] for p in problems if str(p).strip()][:PROBLEMS]
    kept = []
    for item in items[:MAX_CARDS]:
        if isinstance(item, dict) and str(item.get("item_id") or "").strip():
            todo = item.get("todo") if isinstance(item.get("todo"), list) else []
            kept.append({"item_id": str(item["item_id"]).strip()[:40], "short": str(item.get("short") or "")[:20],
                         "todo": [str(t)[:20] for t in todo if str(t).strip()][:8]})
    return {"ok": raw.get("ok") is True and bool(names) and not problems, "cards": names, "items": kept,
            "note": str(raw.get("note") or "")[:NOTE_CHARS], "problems": problems}


def keep_cards(made: dict, attachments: list) -> tuple:
    """All or nothing (the bot's side): ``(attachments, made)``, the draft's files without any card when not every card
    of a good verdict arrived (a failed verdict: none of its cards either, whatever came), and the verdict as it stands
    then (``ok`` False, ``problems`` saying how many came)."""
    names = set(made.get("cards") or [])
    if not names:
        return attachments, made
    arrived = {a.get("name") for a in attachments if a.get("name") in names}
    if made.get("ok") and arrived == names:
        return attachments, made
    if made.get("ok"):
        made = {**made, "ok": False, "problems": [f"手卡只传上来 {len(arrived)} / {len(names)} 张"]}
    return [a for a in attachments if a.get("name") not in names], made


def attention(made: dict) -> str:
    """The deal's line for a draft with cards (a person always looks first) or with none made (why)."""
    if made.get("ok"):
        todo = [f"第 {n} 款 {'、'.join(i['todo'])}" for n, i in enumerate(made.get("items") or [], 1) if i.get("todo")]
        return (f"手卡 {len(made['cards'])} 张附在草稿里，要人看过再发"
                + (f"（卡上待定：{'；'.join(todo)}）" if todo else ""))
    why = "；".join(made.get("problems") or []) or "Mac 没说为什么"
    return f"手卡没做成：{why}；草稿里没附手卡"


def packet_meta(made: dict) -> dict:
    """What a packet keeps of the verdict for the console (``meta.handcards``)."""
    return {"ok": bool(made.get("ok")), "count": len(made.get("cards") or []) if made.get("ok") else 0,
            "note": str(made.get("note") or "")[:NOTE_CHARS], "problems": list(made.get("problems") or [])}


# ── the Mac: settings (~/.claude-relay/handcards.json) ──────────────────

CONFIG_FILE = "handcards.json"   # in the worker's home: absent = this Mac makes no cards
SKILL_SCRIPT = "~/.claude/skills/live-handcard/scripts/make_cards_local.py"
SKILL_PYTHON = "~/.local/share/live-handcard/venv/bin/python"      # the skill's own (Playwright, Pillow)
HAT_SKUS = "~/.claude-relay/platform/hat-index/skus.json"


def _date(value) -> Optional[date]:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def load_config(path) -> tuple:
    """``(settings, "")`` of the Mac's handcards.json, or ``(None, why not)``: ``script`` (the skill's
    make_cards_local.py), ``python`` (the skill's Python: Pillow for the photos, Playwright for the cards; empty:
    $LIVE_HANDCARD_PYTHON, then SKILL_PYTHON), ``skus`` (the hat index's skus.json, read only), ``source`` (which 口径
    the house globals are: the note says it), ``globals`` (commission, pack, ship_time, aftersale, ship_from, no_ship,
    express: a missing one is a red 待定; discount, 0.9 by default), ``caveats`` (lines every note repeats: 「运费险是推断的」),
    ``promo`` ({"text" with {N}, "factor", "until" YYYY-MM-DD, "from"?}: the 双11 line, its N = 直播价 × factor
    floored, only from–until), ``send_zip`` (also the skill's zip, when it fits) and ``timeout_sec``."""
    path = Path(os.path.expanduser(str(path)))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        shown = str(path).replace(str(Path.home()), "~", 1)
        return None, f"这台 Mac 没有 {shown}（照 tools/claude_relay/handcards.example.json 建一个）"
    except (OSError, ValueError) as e:
        return None, f"{path.name} 读不了：{e}"
    if not isinstance(raw, dict):
        return None, f"{path.name} 不是一个 JSON 对象"
    if raw.get("enabled", True) is not True:
        return None, f"{path.name} 里 enabled 不是 true"
    cfg = {"script": os.path.expanduser(str(raw.get("script") or SKILL_SCRIPT)),
           "python": os.path.expanduser(str(raw.get("python") or os.environ.get("LIVE_HANDCARD_PYTHON")
                                             or SKILL_PYTHON)),
           "skus": os.path.expanduser(str(raw.get("skus") or HAT_SKUS)),
           "source": " ".join(str(raw.get("source") or "").split())[:60],
           "caveats": [" ".join(str(c).split())[:120] for c in raw.get("caveats") or [] if str(c).strip()][:5],
           "send_zip": raw.get("send_zip") is True, "promo": None}
    try:
        cfg["timeout_sec"] = max(30, min(int(raw.get("timeout_sec") or 0), 1800)) if raw.get("timeout_sec") else 0
    except (TypeError, ValueError):
        return None, f"{path.name} 的 timeout_sec 要是秒数"
    house = raw.get("globals") if raw.get("globals") is not None else {}
    if not isinstance(house, dict):
        return None, f"{path.name} 的 globals 要是一个对象"
    cfg["globals"] = {k: (" ".join(str(house[k]).split())[:40] if house.get(k) not in (None, "") else None)
                      for k in GLOBAL_KEYS}
    try:
        discount = float(house.get("discount", 0.9))
    except (TypeError, ValueError):
        discount = -1.0
    if not 0.5 <= discount <= 1:
        return None, f"{path.name} 的 globals.discount 要在 0.5 到 1 之间（9 折是 0.9）"
    cfg["globals"]["discount"] = discount
    promo = raw.get("promo")
    if promo not in (None, {}):
        if not isinstance(promo, dict):
            return None, f"{path.name} 的 promo 要是一个对象"
        text = " ".join(str(promo.get("text") or "").split())
        try:
            factor = float(promo.get("factor"))
        except (TypeError, ValueError):
            factor = -1.0
        until, since = _date(promo.get("until")), _date(promo.get("from")) if promo.get("from") else None
        if "{N}" not in text or len(text) > 30 or not 0.5 <= factor <= 1 or until is None or (
                promo.get("from") and since is None):
            return None, (f"{path.name} 的 promo 要写成 {{\"text\": \"双11再减15%实付约{{N}}\", \"factor\": 0.85, "
                          "\"until\": \"2026-11-11\"}（from 可省）")
        cfg["promo"] = {"text": text, "factor": factor, "until": until, "from": since}
    for key, what in (("script", "手卡脚本"), ("python", "手卡用的 Python（技能自己的环境）")):
        if not os.path.isfile(cfg[key]):
            return None, f"找不到{what}：{cfg[key]}"
    return cfg, ""


# ── the Mac: products, SKUs ──────────────────────────────────────────────

def load_catalog(workspace) -> dict:
    """The 抖店 products of the workspace's kb/products.json (``--sync-products``) by id; {} without one."""
    try:
        data = json.loads((Path(workspace) / "kb" / "products.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = (((data.get("platforms") or {}).get("dy") or {}).get("items") or []) if isinstance(data, dict) else []
    return {str(i["id"]): i for i in items if isinstance(i, dict) and str(i.get("id") or "").strip()}


def sku_index(path) -> dict:
    """The hat index's skus.json (per style: code, colour, spec; read only) by item code: {code: {"colour", "spec"}};
    {} when it cannot be read."""
    try:
        data = json.loads(Path(os.path.expanduser(str(path))).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict = {}
    for rows in (data.values() if isinstance(data, dict) else ()):
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, dict) and str(row.get("code") or "").strip():
                out.setdefault(str(row["code"]).strip(), {"colour": str(row.get("colour") or ""),
                                                          "spec": str(row.get("spec") or "")})
    return out


_RANGE = re.compile(r"(XXL|XL|XS|S|M|L|F)?\s*[(（]?\s*(\d{2})\s*[-~～－—]\s*(\d{2})\s*(?:cm|CM|厘米)?\s*[)）]?")
_LABEL = re.compile(r"(?<![A-Za-z])(XXL|XL|XS|S|M|L|F)(?![A-Za-z])|均码|可调节")
_SPEC_HEAD = re.compile(r"^\s*(?:尺码|尺寸|规格)\s*[:：]\s*")
_COLOUR_HEAD = re.compile(r"^\s*颜色\s*[:：]\s*")
_SEPARATORS = " /／,，;；-－_·|"


def parse_spec(text: str) -> list:
    """The sizes in a SKU's spec or name, ``[(label, "56-58")]`` (label "" for a bare range, range "" for a bare label:
    「均码」, 「可调节」, 「M」); [] when it names none (「无规格」, a remark)."""
    text = _SPEC_HEAD.sub("", str(text or ""))
    ranged = [(m.group(1) or "", f"{m.group(2)}-{m.group(3)}") for m in _RANGE.finditer(text)]
    if ranged:
        return ranged
    return [(m.group(1) or m.group(0), "") for m in _LABEL.finditer(text)]


def parse_sku_name(name: str) -> tuple:
    """``(colour, sizes)`` of a 抖店 SKU name the hat index does not know (「M驼色」, 「均码黑色」, 「白色/M（56-58cm）」)."""
    name = str(name or "")
    sizes = parse_spec(name)
    rest = _RANGE.sub(" ", name) if any(r for _, r in sizes) else _LABEL.sub(" ", name)
    return " ".join(rest.strip(_SEPARATORS).split()), sizes


def item_facts(item: dict, index: dict) -> dict:
    """What the SKUs of a catalogue item say: ``colours`` (in the shop's order), ``sizes``, ``prices`` (each SKU's) and
    ``named`` (SKUs read from their names: the hat index has no such code)."""
    colours: list = []
    sizes: list = []
    prices: list = []
    named = 0
    for sku in item.get("skus") or []:
        if not isinstance(sku, dict):
            continue
        entry = index.get(str(sku.get("code") or "").strip())
        if entry is not None:
            colour, found = _COLOUR_HEAD.sub("", entry["colour"]).strip(), parse_spec(entry["spec"])
        else:
            colour, found = parse_sku_name(str(sku.get("name") or ""))
            named += 1
        if colour and colour not in colours:
            colours.append(colour)
        for size in found:
            if size not in sizes:
                sizes.append(size)
        if sku.get("price") not in (None, ""):
            prices.append(str(sku["price"]))
    return {"colours": colours, "sizes": sizes, "prices": prices, "named": named}


def size_text(sizes: list) -> Optional[str]:
    """The card's 尺码: 「F 52-60cm」, 「M 56-58cm」, two of them 「M56-58/L58-60cm」 (the 老板模板's way), a bare label
    「M 码」 / 「均码」; None (待定) without any."""
    if not sizes:
        return None
    if len(sizes) == 1:
        label, span = sizes[0]
        if span:
            return f"{label} {span}cm" if label else f"{span}cm"
        return label if label in ("均码", "可调节") else f"{label} 码"
    if all(span for _, span in sizes):
        return "/".join(f"{label}{span}" for label, span in sizes) + "cm"
    return " / ".join(label or span for label, span in sizes)


def size_note(sizes: list) -> str:
    """The 注意事项's words on the head size: 「均码 F 头围 52-60cm」, 「M 码头围 56-58cm、L 码头围 58-60cm」 ("" without one)."""
    out = []
    for label, span in sizes:
        if not span:
            continue
        out.append(f"均码 F 头围 {span}cm" if label == "F" else f"{label} 码头围 {span}cm" if label else f"头围 {span}cm")
    return "、".join(out)


# The material as the title says it, the first that fits (a longer word before the word it holds).
MATERIALS = ("100%羊毛", "纯羊毛", "羊毛混纺", "羊毛毡", "羊毛针织", "羊毛呢", "羊绒", "羊毛", "麂皮绒", "灯芯绒", "丝绒",
             "毛呢", "花呢", "针织", "毛线", "拉菲草", "麦秆", "咸草", "纸草", "草编", "编织", "棉麻", "亚麻", "牛仔", "丹宁",
             "天丝", "皮质", "皮革", "布艺", "毛绒")


def material_of(title: str, said: str = "") -> tuple:
    """``(material, how)`` from the listing title: the first of MATERIALS in it (``how`` "title"), else what the draft
    said when the title has those very words (``how`` "said"), else ``(None, "")`` (待定)."""
    title = re.sub(r"100\s*%\s*羊毛", "100%羊毛", str(title or ""))
    hit = next((m for m in MATERIALS if m in title), None)
    if hit:
        return hit, "title"
    said = " ".join(str(said or "").split())
    if 1 <= len(said) <= 10 and said in title:
        return said, "said"
    return None, ""


def care_of(material: Optional[str]) -> str:
    """The 注意事项's general care words for the material (the detail pages have none: the note says it is general)."""
    m = material or ""
    if "麂皮" in m:
        return "麂皮绒勿水洗，可用软毛刷顺毛清理"
    if "针织" in m or "毛线" in m:
        return ""
    if any(k in m for k in ("羊毛", "羊绒", "毛呢", "花呢")):
        return "羊毛帽勿水洗、勿重压，可用软毛刷清灰"
    if any(k in m for k in ("草", "麦秆", "编织")):
        return "草编帽勿水洗、勿重压"
    return "勿水洗、勿重压"


def floor_price(price: int, factor: float) -> int:
    """直播价 and the 双11 实付: ``price`` × ``factor`` floored to the yuan, as the skill's script does (315 × 0.9 → 283)."""
    return int(price * factor + 1e-9)


def daily_of(item: dict, prices: list) -> tuple:
    """``(日常价, "")`` (whole yuan) of a catalogue item, or ``(None, why)`` when its SKUs differ or it is not whole."""
    try:
        values = {Decimal(str(p)) for p in [item.get("price"), *prices] if p not in (None, "")}
    except InvalidOperation:
        return None, "价格读不懂"
    if not values:
        return None, "目录里没有价格"
    if len(values) > 1:
        return None, "SKU 价格不一样（" + "、".join(f"{v.normalize():f}" for v in sorted(values)) + "）"
    value = values.pop()
    if value != value.to_integral_value() or value <= 0:
        return None, f"价格 {value.normalize():f} 不是整元"
    return int(value), ""


def promo_line(live: Optional[int], promo: Optional[dict], today: date) -> str:
    """The 双11 line of a card (``promo``: from–until, both days in) for this 直播价; "" outside it or without one."""
    if not promo or live is None or today > promo["until"] or (promo.get("from") and today < promo["from"]):
        return ""
    return promo["text"].replace("{N}", str(floor_price(live, promo["factor"])))


def _width(text: str) -> float:
    return sum(0.5 if ord(c) < 128 else 1.0 for c in text)


def short_name(text: str) -> tuple:
    """``(short, cut)``: the card's 产品名称 as a file name can carry it (no / \\ : * ? " < > | or spaces), at most
    SHORT_MAX Han characters wide (a letter counts half); ``cut`` True when it had to be shortened."""
    clean = re.sub(r'[\\/:*?"<>|\s]', "", str(text or ""))
    out = ""
    for c in clean:
        if _width(out + c) > SHORT_MAX:
            return out, True
        out += c
    return out, False


def _text(value, limit: int) -> tuple:
    text = " ".join(str(value or "").split())
    return (text[:limit], len(text) > limit)


_PREFIX = re.compile(r"^(?:抖音|小红书|淘宝|快手|视频号)\s*[-－_—:：·]+\s*")   # (the friend add's remark: 抖音-…)


def channel_of(call: str, name: str) -> str:
    """The cards' 场次: 「<称呼>专属」, her 称呼 as the draft gave it, else her deal's name without a 「抖音-」 prefix (the
    WeChat remark the friend add sets); runtime data only, never in a file of the repository."""
    raw = " ".join(str(call or "").split()) or " ".join(str(name or "").split())
    raw = re.sub(r'[\\/:*?"<>|]', "", _PREFIX.sub("", raw)).strip()[:CALL_MAX]
    return f"{raw}专属" if raw else "达人专属"


def _norm_colour(text: str) -> str:
    text = _COLOUR_HEAD.sub("", " ".join(str(text or "").split()))
    return text[:-1] if len(text) > 1 and text.endswith("色") else text


def colours_text(colours: list) -> str:
    """The card's colours: 「棕咖色 / 驼色」; five or more short (a trailing 色 off) with the count: 「黑 / 墨绿 / … 共 10 色」."""
    if len(colours) > 4:
        return " / ".join(_norm_colour(c) for c in colours) + f" 共 {len(colours)} 色"
    return " / ".join(colours)


def _sells(raw) -> tuple:
    """The card's selling points ``[[bold, rest]]`` (at most SELLS_MAX; a rest starts with 「，」 unless it starts with
    punctuation already) and whether any was cut."""
    out, cut = [], False
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        head, cut_head = _text(entry.get("head"), TEXT_MAX["head"])
        rest, cut_rest = _text(entry.get("rest"), TEXT_MAX["rest"])
        if not head:
            continue
        if rest and rest[0] not in "，,：:；;、。（(":
            rest = "，" + rest
        out.append([head, rest])
        cut = cut or cut_head or cut_rest
    return out[:SELLS_MAX], cut or len(out) > SELLS_MAX


def build_sheet(request, *, catalog: dict, skus: dict, config: dict, name: str, today: date, workspace,
                asked: Optional[list] = None, others: int = 0) -> tuple:
    """The skill's 单子.json for a draft's request: ``(sheet, photos, report)``; its 场次 「<称呼>专属」 (``channel_of``:
    the request's ``call``, else ``name``, her deal's).  ``photos``: per card the main image
    and its crop (``crop_photos``'s plan).  ``report``: ``problems`` (any: no card at all), ``todo`` per card (its null
    fields, a red 待定), ``notes`` (what the operator should know: a name cut, a colour that does not fit, a price
    left 待定) and ``items``.  ``asked``: the products an operator's 「做手卡」 named (``[{"item_id", "sku_ids"}]``; None or
    [] when none): those, in that order (one the draft left out is a problem, one it added is left out); ``others``:
    the files the draft sends besides (one draft carries at most MAX_CARDS)."""
    report: dict = {"problems": [], "notes": [], "todo": {}, "items": [], "crops": []}
    products = request.get("products") if isinstance(request, dict) else None
    if not isinstance(products, list) or not products:
        report["problems"].append("草稿要做手卡，可没写是哪几款")
        return None, [], report
    entries, seen = [], set()
    for entry in products:
        item_id = str((entry or {}).get("item_id") or "").strip() if isinstance(entry, dict) else ""
        if item_id and item_id in seen:
            report["notes"].append(f"商品 {item_id} 写了两次，只做一张")
            continue
        seen.add(item_id)
        entries.append(entry if isinstance(entry, dict) else {})
    if asked:
        wanted = [str(a.get("item_id") or "") for a in asked if isinstance(a, dict)]
        by_id = {str(e.get("item_id") or "").strip(): e for e in entries}
        extra = [i for i in by_id if i not in wanted]
        if extra:
            report["notes"].append(f"运营点的是 {len(wanted)} 款，草稿多写的 {'、'.join(extra)} 没做")
        for item_id in wanted:
            if item_id not in by_id:
                report["problems"].append(f"运营点的商品 {item_id}，草稿没给它写卖点")
        entries = [by_id[i] for i in wanted if i in by_id]
    if len(entries) + others > MAX_CARDS:
        report["problems"].append(f"一份草稿最多附 {MAX_CARDS} 个文件：这次 {len(entries)} 张手卡"
                                  + (f"加 {others} 个别的附件" if others else "") + "，放不下，分两次做")
    house = dict(config["globals"])
    discount = float(house.pop("discount", 0.9))
    assets = os.path.realpath(Path(workspace) / "assets")
    out, photos = [], []
    for no, entry in enumerate(entries, 1):
        item_id = str(entry.get("item_id") or "").strip()
        item = catalog.get(item_id)
        if item is None:
            report["problems"].append(f"第 {no} 款：商品 {item_id or '（没写 ID）'} 不在 kb/products.md 的抖店目录里")
            continue
        title = str(item.get("title") or "")
        short, cut = short_name(entry.get("short"))
        label = short or item_id
        if not short:
            report["problems"].append(f"第 {no} 款（商品 {item_id}）：草稿没写短名")
        elif cut:
            report["notes"].append(f"第 {no} 款短名「{' '.join(str(entry.get('short')).split())}」超过 {SHORT_MAX} 个字，"
                                   f"卡上用了「{short}」")
        image = str(item.get("image_file") or "")
        real = os.path.realpath(Path(workspace) / image) if image else ""
        if not image or not real.startswith(assets + os.sep) or not os.path.isfile(real):
            report["problems"].append(f"第 {no} 款（{label}）：没有主图（{image or f'assets/products/dy-{item_id}.jpg'}）")
            continue
        facts = item_facts(item, skus)
        if facts["named"]:
            report["notes"].append(f"第 {no} 款有 {facts['named']} 个 SKU 帽子图库里没有，颜色尺码按 SKU 名字拆的")
        colours = facts["colours"]
        asked_colours = ([str(c) for c in entry.get("colors") or [] if str(c).strip()]
                         if isinstance(entry.get("colors"), list) else [])
        if asked_colours:
            have = {_norm_colour(c): c for c in colours}
            picked = [have[_norm_colour(c)] for c in asked_colours if _norm_colour(c) in have]
            missing = [c for c in asked_colours if _norm_colour(c) not in have]
            if missing:
                report["notes"].append(f"第 {no} 款要的颜色「{'、'.join(missing)}」这款没有（有 {' / '.join(colours) or '—'}）"
                                       + ("，卡上写了全部颜色" if not picked else ""))
            colours = list(dict.fromkeys(picked)) or colours
        todo = []
        daily, why = daily_of(item, facts["prices"])
        if daily is None:
            todo.append(TODO_LABELS["daily"])
            report["notes"].append(f"第 {no} 款日常价待定：{why}")
        size = size_text(facts["sizes"])
        if size is None:
            todo.append(TODO_LABELS["size"])
        material, how = material_of(title, entry.get("material") or "")
        if material is None:
            todo.append(TODO_LABELS["material"])
        if not colours:
            todo.append(TODO_LABELS["colors"])
        brand, cut_brand = _text(entry.get("brand_line"), TEXT_MAX["brand_line"])
        audience, cut_audience = _text(entry.get("audience"), TEXT_MAX["audience"])
        sells, cut_sells = _sells(entry.get("sells"))
        lacking = [w for w, v in (("品牌句", brand), ("适用人群", audience), ("卖点", sells)) if not v]
        if lacking:
            report["problems"].append(f"第 {no} 款（{label}）：草稿没写{'、'.join(lacking)}")
        if cut_brand or cut_audience or cut_sells:
            report["notes"].append(f"第 {no} 款的字太长，卡上截短了（品牌句、适用人群、卖点格子不换行）")
        live = floor_price(daily, discount) if daily is not None else None
        cell = CELLS.get(str(entry.get("photo_cell") or ""))
        cut_bottom = entry.get("cut_bottom") is True
        photos.append({"src": real, "dst": f"产品图/{no:02d}.jpg", "cell": list(cell) if cell else None,
                       "cut_bottom": cut_bottom})
        crop = "、".join(w for w in ((f"主图{entry.get('photo_cell')}那一格" if cell else ""),
                                      ("去掉了底下的品牌字" if cut_bottom else "")) if w)
        if crop:
            report["crops"].append(f"第 {no} 款{crop}")
        product = {
            "short": short, "name": title, "brand_line": brand, "material": material, "size": size, "daily": daily,
            "stock": STOCK, "colors": colours_text(colours) if colours else "颜色待定", "audience": audience,
            "img": f"产品图/{no:02d}.jpg", "id": item_id, "sells": sells,
            "note": "；".join(w for w in (size_note(facts["sizes"]), care_of(material), "拍摄光线略有色差属正常") if w),
        }
        promo = promo_line(live, config.get("promo"), today)
        if promo:
            product["promo"] = promo
        out.append(product)
        report["todo"][no] = todo
        report["items"].append({"item_id": item_id, "short": short, "daily": daily, "live": live, "todo": todo,
                                "material_from": how})
    channel = channel_of(request.get("call") or "", name)
    sheet = {"channel": channel, "platform": "douyin", "globals": {**house, "discount": discount}, "products": out}
    return sheet, photos, report


def note_text(sheet: dict, report: dict, config: dict, today: date, folder: str = "") -> str:
    """The operator's note on the cards (the skill's report, §6): the cards, every 待定, what was inferred, the 口径
    reused (``source``, ``caveats``), the 双11 line, the stock, the crops, where the session is kept on the Mac."""
    products = sheet["products"]
    lines = ["做了 " + str(len(products)) + f" 张手卡（{sheet['channel']}，抖音）："
             + "；".join(f"第 {n} 款 {p['short']}" + (f" {p['daily']} → 直播 {floor_price(p['daily'], sheet['globals']['discount'])}"
                                                     if p.get("daily") is not None else "")
                        for n, p in enumerate(products, 1))]
    todo = [f"第 {n} 款 {'、'.join(t)}" for n, t in report["todo"].items() if t]
    house = [k for k in GLOBAL_KEYS if sheet["globals"].get(k) is None]
    if house:
        todo.append("每张的 " + "、".join(GLOBAL_LABELS[k] for k in house))
    lines.append("待定（卡上红字）：" + ("；".join(todo) if todo else "没有"))
    said = [f"第 {i + 1} 款" for i, item in enumerate(report["items"]) if item.get("material_from") == "said"]
    lines.append("推断的：短名、品牌句、适用人群、卖点是按商品标题写的；注意事项里的洗护是通用写法"
                 + (f"；{'、'.join(said)}的材质是草稿从标题里挑的" if said else ""))
    source = config.get("source") or "handcards.json 里"
    words = "，".join(f"{GLOBAL_LABELS[k]} {sheet['globals'][k]}" for k in GLOBAL_KEYS if sheet["globals"].get(k))
    discount = sheet["globals"]["discount"]
    promo = config.get("promo")
    promo_said = ""
    if promo and products and any(p.get("promo") for p in products):
        line = promo["text"].replace("{N}", "N")
        promo_said = f"；{line}（N = 直播价 × {promo['factor']:g} 取整，到 {promo['until']:%m-%d}）"
    lines.append(f"沿用了{source}的口径：{words}；直播价 = 日常价 × {discount:g} 取整{promo_said}"
                 + "".join(f"；{c}" for c in config.get("caveats") or []))
    lines.append(f"库存：卡上一律写「{STOCK}」；真实库存没查（要聚水潭命令行），少的颜色开播前看一眼")
    if report["crops"]:
        lines.append("产品图：" + "；".join(report["crops"]))
    lines.extend(report["notes"])
    if folder:
        lines.append(f"单子和图在 Mac 的 {folder}（留 {KEEP_DAYS} 天）")
    return "\n".join(lines)[:NOTE_CHARS]


# ── the Mac: photos (Pillow, under the skill's Python) ───────────────────

def side_margin(white: list) -> int:
    """How many columns of a picture's left and right edges are blank canvas (``white``: per column, is it near white
    top to bottom), the smaller of the two: the shop's main images put a 3:4 photo on a white square, as wide on
    both sides."""
    left = next((i for i, w in enumerate(white) if not w), len(white))
    right = next((i for i, w in enumerate(reversed(white)) if not w), len(white))
    return min(left, right)


def crop_box(width: int, height: int, margin: int = 0, cell=None, cut_bottom: bool = False) -> tuple:
    """The part of a main image a card shows, ``(left, top, right, bottom)``: without its blank side margins (only a
    real one: at least 1.5% and less than 30% of the width on each side), the cell of a 2×2 image asked for
    (``cell`` (column, row), 1% inside its edges) and without its lowest CUT_BOTTOM (big brand letters)."""
    m = margin if width * 0.015 <= margin < width * 0.3 else 0
    x0, x1, y0, y1 = float(m), float(width - m), 0.0, float(height)
    if cell is not None:
        col, row = int(cell[0]), int(cell[1])
        w2, h2 = (x1 - x0) / 2, (y1 - y0) / 2
        x0, x1 = x0 + col * w2 + w2 * CELL_INSET, x0 + (col + 1) * w2 - w2 * CELL_INSET
        y0, y1 = y0 + row * h2 + h2 * CELL_INSET, y0 + (row + 1) * h2 - h2 * CELL_INSET
    if cut_bottom:
        y1 = y0 + (y1 - y0) * (1 - CUT_BOTTOM)
    return int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))


def crop_photos(plan_path) -> int:
    """``python -I handcards.py crop <plan.json>`` (the skill's Python, Pillow): each photo of the plan
    (``{"base", "photos": [{"src", "dst", "cell", "cut_bottom"}]}``) cut by ``crop_box`` and written as a JPEG; prints
    ``[{"dst", "box", "size"}]``.  Exit 0, or 1 with what went wrong."""
    from PIL import Image                             # (the skill's environment has it; the worker's need not)

    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    base = Path(plan["base"])
    done = []
    for photo in plan["photos"]:
        with Image.open(photo["src"]) as source:
            image = source.convert("RGB")
        small = image.convert("L")
        small.thumbnail((360, 360))
        pixels = small.load()
        white = []
        for x in range(small.width):
            light = sum(1 for y in range(small.height) if pixels[x, y] >= 242)
            white.append(light >= small.height * 0.985)
        margin = int(round(side_margin(white) * image.width / small.width))
        box = crop_box(image.width, image.height, margin, photo.get("cell"), bool(photo.get("cut_bottom")))
        dst = base / photo["dst"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        image.crop(box).save(dst, "JPEG", quality=92)
        done.append({"dst": photo["dst"], "box": list(box), "size": [image.width, image.height]})
    print(json.dumps(done, ensure_ascii=False))
    return 0


# ── the Mac: making the cards ────────────────────────────────────────────

def png_size(path) -> Optional[tuple]:
    """``(width, height)`` of a PNG file by its header; None for anything else."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", head[16:24])


def run(argv: list, *, cwd, timeout: float, heartbeat: Optional[Callable[[], bool]] = None, every: float = 30.0,
        popen=subprocess.Popen) -> tuple:
    """One command (an argv list, no shell) to its end: ``(exit code or None, its output's tail, stopped)``;
    ``heartbeat()`` every ``every`` seconds meanwhile (the relay job stays alive), True from it (the job was
    cancelled) or ``timeout`` stops it (``stopped`` "cancelled" / "timeout")."""
    started = time.monotonic()
    log = Path(cwd) / "run.log"
    try:
        with open(log, "ab") as out:
            proc = popen([str(a) for a in argv], cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=out,
                         stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        return None, f"启动不了：{e}", ""
    stopped, beat = "", time.monotonic()
    while True:
        try:
            proc.wait(timeout=1.0)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if now - started >= timeout:
            stopped = "timeout"
        elif heartbeat is not None and now - beat >= every:
            beat = now
            try:
                if heartbeat():
                    stopped = "cancelled"
            except Exception:
                pass
        if stopped:
            try:
                os.killpg(proc.pid, signal.SIGKILL)     # (Chromium too: the script's browser is in its group)
            except OSError:
                proc.kill()
            proc.wait()
            break
    try:
        tail = log.read_text(encoding="utf-8", errors="replace").strip()[-400:]
    except OSError:
        tail = ""
    return proc.returncode, tail, stopped


def session_folder(home, job_id: int, now: Optional[float] = None) -> Path:
    """A new folder for one draft's cards under ``home`` (the worker's platform/handcards), the ones older than
    KEEP_DAYS gone first."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    for old in home.iterdir():
        try:
            if old.is_dir() and now - old.stat().st_mtime > KEEP_DAYS * 86400:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    folder = home / f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-job{int(job_id)}"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    return folder


def make(request, *, workspace, config: dict, name: str, job_id: int, home, asked: Optional[list] = None,
         others: int = 0, today: Optional[date] = None, heartbeat: Optional[Callable[[], bool]] = None,
         popen=subprocess.Popen) -> tuple:
    """The cards of one draft's request, ``(verdict, files)``: ``made_block`` and the 1x PNGs to send (the skill's zip
    after them when ``send_zip`` and it fits), or a verdict saying why there are none (and no file).  In a folder of
    its own under ``home`` (kept KEEP_DAYS: 单子.json and the photos for a person who fixes a 待定 by hand)."""
    today = today or date.today()
    catalog = load_catalog(workspace)
    if not catalog:
        return made_block(False, problems=["这台 Mac 的 kb/products.json 里没有抖店商品（先在 Mac 上 --sync-products）"]), []
    sheet, photos, report = build_sheet(request, catalog=catalog, skus=sku_index(config["skus"]), config=config,
                                        name=name, today=today, workspace=workspace, asked=asked, others=others)
    if report["problems"]:
        return made_block(False, problems=report["problems"], items=report["items"]), []
    folder = session_folder(home, job_id)
    plan = folder / "crop.json"
    plan.write_text(json.dumps({"base": str(folder), "photos": photos}, ensure_ascii=False), encoding="utf-8")
    timeout = config.get("timeout_sec") or 60 + 30 * len(sheet["products"])
    code, tail, stopped = run([config["python"], "-I", str(Path(__file__).resolve()), "crop", str(plan)], cwd=folder,
                              timeout=timeout, heartbeat=heartbeat, popen=popen)
    if stopped or code != 0 or not all((folder / p["dst"]).is_file() for p in photos):
        return made_block(False, problems=[_failed("裁产品图", code, tail, stopped, timeout)], items=report["items"]), []
    sheet_path = folder / "单子.json"
    sheet_path.write_text(json.dumps(sheet, ensure_ascii=False, indent=1), encoding="utf-8")
    cards_dir = folder / "手卡"
    argv = [config["python"], "-I", config["script"], str(sheet_path), "--out-dir", str(cards_dir)]
    code, tail, stopped = run(argv + (["--zip"] if config.get("send_zip") else ["--no-pdf"]), cwd=folder,
                              timeout=timeout, heartbeat=heartbeat, popen=popen)
    if stopped or code != 0:
        return made_block(False, problems=[_failed("出图", code, tail, stopped, timeout)], items=report["items"]), []
    files, problems = [], []
    for no, product in enumerate(sheet["products"], 1):
        card = cards_dir / f"手卡_{no:02d}_{product['short']}.png"
        size = png_size(card)
        if size is None:
            problems.append(f"第 {no} 张（{product['short']}）没出来")
        elif tuple(size) != CARD_SIZE:
            problems.append(f"第 {no} 张（{product['short']}）尺寸不对（{size[0]}×{size[1]}）")
        files.append(card)
    if problems:
        return made_block(False, problems=problems, items=report["items"]), []
    if config.get("send_zip"):
        bundle = folder / f"{sheet['channel']}直播手卡.zip"
        if not bundle.is_file():
            report["notes"].append("压缩包没出来，只附了图")
        elif len(files) + others + 1 > MAX_CARDS:
            report["notes"].append(f"压缩包放不下（一份草稿最多 {MAX_CARDS} 个文件），只附了图")
        else:
            files.append(bundle)
    for extra in list(cards_dir.glob("*@2x.png")) + list(cards_dir.glob("*.html")):
        extra.unlink()                                 # (the 1x went back; the zip keeps its own copies)
    note = note_text(sheet, report, config, today, folder=str(folder).replace(str(Path.home()), "~", 1))
    return made_block(True, cards=[f.name for f in files], items=report["items"], note=note), files


def _failed(step: str, code, tail: str, stopped: str, timeout: float) -> str:
    if stopped == "timeout":
        return f"{step}超过 {int(timeout)} 秒没做完"
    if stopped == "cancelled":
        return f"{step}时任务被取消了"
    last = tail.splitlines()[-1] if tail else ""
    return f"{step}出错了（退出码 {code}）" + (f"：{last[:200]}" if last else "")


def asked_items(meta: dict) -> Optional[list]:
    """The products of an operator's 「做手卡」 (``meta.handcards.items``: ``[{"item_id", "sku_ids"}]``, ids of digits);
    None when the job is no such click, [] when it named none (the draft finds them in the case file)."""
    ask = meta.get(REQUEST) if isinstance(meta, dict) else None
    if not isinstance(ask, dict):
        return None
    out = []
    for item in ask.get("items") or []:
        item_id = str((item or {}).get("item_id") or "").strip() if isinstance(item, dict) else ""
        if re.fullmatch(r"\d{6,25}", item_id) and all(i["item_id"] != item_id for i in out):
            skus = [str(s) for s in item.get("sku_ids") or [] if re.fullmatch(r"\d{6,25}", str(s))]
            out.append({"item_id": item_id, "sku_ids": skus[:20]})
    return out[:MAX_CARDS]


def describe_asked(asked: list, catalog: dict) -> list:
    """The products of a 「做手卡」 as the prompt names them: 「商品 3843…（标题；寄样拿到的：M驼色）」."""
    out = []
    for item in asked:
        entry = catalog.get(item["item_id"])
        if entry is None:
            out.append(f"商品 {item['item_id']}（不在 kb/products.md 的抖店目录里）")
            continue
        names = {str(s.get("id")): str(s.get("name") or "") for s in entry.get("skus") or [] if isinstance(s, dict)}
        got = [names[s] for s in item.get("sku_ids") or [] if names.get(s)]
        out.append(f"商品 {item['item_id']}（{' '.join(str(entry.get('title') or '').split())[:40]}"
                   + (f"；寄样拿到的：{'、'.join(got)}" if got else "") + "）")
    return out


if __name__ == "__main__":                            # the worker runs ``crop`` with the skill's Python
    if len(sys.argv) == 3 and sys.argv[1] == "crop":
        sys.exit(crop_photos(sys.argv[2]))
    sys.exit("用法：python -I handcards.py crop <plan.json>")
