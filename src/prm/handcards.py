"""直播手卡 (PRM_HANDCARDS, 2026-11-09; the user: the bot makes the cards itself, a person still looks before anything
goes to the creator).

Creators who will stream the shop's hats ask for 直播手卡: one card per product with the price, the commission, the
stock, the specs, the selling points and the shipping.  The kb answered 「好的老师，手卡我这边整理好发您～」 and an operator
had Claude make them on the Mac with the skill ``live-handcard`` (~/.claude/skills/live-handcard: its
make_cards_local.py takes one 单子.json and renders a 1600×1200 PNG per product).  Now a draft makes them:

1. The request.  The drafting Claude names the products in its answer, ``handcards`` (``REQUEST_SCHEMA``): the 抖店
   item ids of kb/products.md, the colours she picked, the cell of a 2×2 main image and whether its big brand letters
   at the bottom go (it looks at the picture), and the words it writes from the listing title (short name, brand
   line, audience, selling points).  The field is in a job's schema only when its prompt has the 【直播手卡】 rule: the
   bot's job says the bot takes cards (``meta.handcards_ok``, PRM_HANDCARDS on Windows) or an operator clicked
   「做手卡」 in her deal (``meta.handcards`` {"by": "console", "items": her latest sample order's or the ticked ones}),
   and this Mac has its settings.  A short name the card cannot hold is asked again of the model once (the worker).
2. The cards, on the Mac (tools/claude_relay/worker.py ``do_prm_job`` → ``make``).  A catalogue older than
   ``max_catalogue_days`` (or marked stale) makes none: the prices may have changed.  ``build_sheet`` turns the request
   into the skill's 单子.json from data only: the title, the price and the SKUs of kb/products.json (the colours from
   the 抖店 SKU names), the sizes of the hat index's skus.json (the SKU names for a code it does not have), the
   material from the title, the house 口径 (``globals``: the 老板模板) and the 双11 line (``promo``, until its end date:
   N = 日常价 × discount × factor floored once) of ~/.claude-relay/handcards.json.  Stock is always 「充足」 (the user,
   2026-11-06: the real stock is the operator's); a field without a source is null (a red 待定 on the card); 直播价 =
   日常价 × discount floored to the yuan (315 → 283), the script's own rule; words the card cannot hold are cut at a
   word boundary and listed (``cuts``).  The main image is cropped (``crop_box``: each white side margin as measured,
   the cell asked for at the seam between the photos, its brand letters) with the skill's Python (``crop_photos``:
   Pillow), then the skill's script renders the cards.  The 1x PNGs go back as the draft's files and
   ``handcards_made`` (``made_block``) says which, with the operator's note: the catalogue's date, every 待定, what
   was inferred, which 口径 were reused, the crops, the cuts.  All or nothing: a product without data, the script
   failing, a card missing or not uploaded: the verdict names every planned file and says why.
3. The bot (src/prm/service.py ``on_result``): a draft with a verdict always waits for a person (never by its kind's
   gate, the 下班时段, 自动模式 or PRM_OPENING_AUTO_MIN); a draft whose cards did not all arrive carries none of them,
   nor any other card-like file (``keep_cards``); its words cannot go out as they are (``PrmService.decide``), nor can
   cards whose 双11 line is past its end date by Beijing time (``promo_expired``).

Standard library only, Python 3.9: the Mac worker loads this file by its path with macOS's /usr/bin/python3 (like
platforms.py); the bot imports it as src.prm.handcards.  Only ``crop_photos`` needs Pillow: the worker runs it with
the skill's own Python (``python -I handcards.py crop <plan.json>``).
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import signal
import statistics
import struct
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Optional

REQUEST = "handcards"            # the drafting Claude's request, in its answer
MADE = "handcards_made"          # the Mac's verdict in its place, in the draft that goes back to the bot
OK_KEY = "handcards_ok"          # a draft job's meta: the bot takes cards (PRM_HANDCARDS on)
MAX_CARDS = 9                    # one draft carries at most 9 files (result.MAX_ATTACHMENTS, the relay's MAX_OUTFILES)
SHORT_MAX = 6                    # the card's 产品名称 holds about 6 Han characters (the skill: a longer one is cut off)
SELLS_MAX = 4                    # selling points on a card
# How long a written field may be on the card (its box does not wrap): longer is cut at a word boundary and listed.
TEXT_MAX = {"brand_line": 30, "audience": 24, "head": 14, "rest": 26}
CALL_MAX = 16
CELLS = {"左上": (0, 0), "右上": (1, 0), "左下": (0, 1), "右下": (1, 1)}
CUT_BOTTOM = 0.18                # a main image with big brand letters at its bottom loses its lowest 18% (as by hand)
CELL_INSET = 0.01                # a cell of a 2×2 image is cut 1% inside its edges (no sliver of the next one)
SEAM_WINDOW = 0.03               # the seam between two photos is looked for this far (of the content) around its middle
STOCK = "充足"                    # the user, 2026-11-06: the cards say 「充足」, the real stock is the operator's
NOTE_CHARS = 1500
PROBLEMS = 10
CUTS_MAX = 12
CARD_SIZE = (1600, 1200)         # the skill's 1x card
KEEP_DAYS = 7                    # a session's folder on the Mac (单子.json, photos, cards) is kept this long
CATALOGUE_DAYS = 7               # handcards.json ``max_catalogue_days``: an older catalogue makes no card
ZIP_NAME = "ACE7CN直播手卡"       # the skill's zip and pdf names (her 称呼 is in no file name: names reach logs)
EXPIRED_TEXT = "手卡上的双11价已过期，请重新做手卡"
BEIJING = timezone(timedelta(hours=8))
TODO_HTML = '<span class="todo">待定</span>'   # the skill's red 待定, for the colours (its tag line is raw HTML)
GLOBAL_KEYS = ("commission", "pack", "ship_time", "aftersale", "ship_from", "no_ship", "express")
GLOBAL_LABELS = {"commission": "佣金", "pack": "包装", "ship_time": "发货", "aftersale": "售后", "ship_from": "发货地",
                 "no_ship": "不发货地", "express": "快递"}
TODO_LABELS = {"daily": "日常价", "material": "材质", "size": "尺码", "colors": "颜色"}
# A file of the cards: 手卡_01_<短名>.png (the relay may add 「(2)」 to a name it has already), the skill's zip.
_CARD_FILE = re.compile(r"^手卡_\d{2}_.*\.png$")
_ZIP_FILE = re.compile(r"直播手卡(?:\(\d+\))?\.zip$")

# The drafting Claude's ``handcards`` (the worker adds it to a job's --json-schema only with the 【直播手卡】 rule; basic
# keywords only, as the rest of the schema: lengths and counts are this module's to check; ``minItems`` 1: a request
# names a product, and one that names none is no request, ``has_products``).
REQUEST_SCHEMA = {
    "type": "object",
    "properties": {
        "call": {"type": "string"},
        "products": {"type": "array", "minItems": 1, "items": {
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


def beijing_today(now: Optional[float] = None) -> date:
    """Today in Beijing (the bot's and the Mac's day: a promo's end date is one of its days)."""
    return datetime.fromtimestamp(time.time() if now is None else now, BEIJING).date()


def _day(at) -> str:
    """A time as 「10-28」 (its year too when it is not this one's), Beijing time; "" for none."""
    try:
        when = datetime.fromtimestamp(float(at), BEIJING)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return when.strftime("%m-%d" if when.year == beijing_today().year else "%Y-%m-%d")


# ── the Mac's verdict (what the bot reads) ───────────────────────────────

def made_block(ok: bool, *, cards=(), items=(), note: str = "", problems=(), cuts=(), promo_until: str = "",
               catalog_at: Optional[float] = None, house_todo=()) -> dict:
    """The Mac's verdict on a draft's request (``handcards_made``; the bot reads it with ``check_made``): ``ok`` with
    the cards' file names (the draft's files of those names), else ``problems`` (why no card was made) and every file
    that was planned (the bot leaves out any of them that came).  ``cuts``: the words cut to fit the card;
    ``promo_until``: the end date (YYYY-MM-DD) of the 双11 line on the cards; ``catalog_at``: the catalogue's sync time;
    ``house_todo``: the house 口径 handcards.json lacks (a red 待定 on every card: 「快递」)."""
    problems = [" ".join(str(p).split())[:300] for p in problems if str(p).strip()][:PROBLEMS]
    return {"version": 1, "ok": bool(ok) and bool(cards) and not problems, "cards": [str(c) for c in cards],
            "items": [dict(i) for i in items][:MAX_CARDS], "note": str(note or "")[:NOTE_CHARS], "problems": problems,
            "cuts": [" ".join(str(c).split())[:160] for c in cuts if str(c).strip()][:CUTS_MAX],
            "promo_until": str(promo_until or ""),
            "catalog_at": float(catalog_at) if isinstance(catalog_at, (int, float)) else None,
            "house_todo": [str(t)[:20] for t in house_todo if str(t).strip()][:len(GLOBAL_KEYS)]}


def check_made(raw) -> dict:
    """The Mac's verdict as the bot keeps it, ``{"ok", "cards", "items", "note", "problems", "cuts", "promo_until",
    "catalog_at", "house_todo"}`` (``ok`` only with cards and no problem); a ValueError for one it cannot use
    (``result.parse_draft`` refuses the draft then, as it does a malformed platform verdict).  An older worker's verdict
    has no cuts, end date, catalogue time or house 待定."""
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
    cuts = raw.get("cuts") if raw.get("cuts") is not None else []
    house = raw.get("house_todo") if raw.get("house_todo") is not None else []
    if not all(isinstance(v, list) for v in (problems, items, cuts, house)):
        raise ValueError("handcards_made.problems / items / cuts / house_todo are not lists")
    problems = [" ".join(str(p).split())[:300] for p in problems if str(p).strip()][:PROBLEMS]
    kept = []
    for item in items[:MAX_CARDS]:
        if isinstance(item, dict) and str(item.get("item_id") or "").strip():
            todo = item.get("todo") if isinstance(item.get("todo"), list) else []
            kept.append({"item_id": str(item["item_id"]).strip()[:40], "short": str(item.get("short") or "")[:20],
                         "todo": [str(t)[:20] for t in todo if str(t).strip()][:8]})
    until = str(raw.get("promo_until") or "").strip()
    if until and _date(until) is None:
        raise ValueError("handcards_made.promo_until is not YYYY-MM-DD")
    at = raw.get("catalog_at")
    return {"ok": raw.get("ok") is True and bool(names) and not problems, "cards": names, "items": kept,
            "note": str(raw.get("note") or "")[:NOTE_CHARS], "problems": problems,
            "cuts": [" ".join(str(c).split())[:160] for c in cuts if str(c).strip()][:CUTS_MAX], "promo_until": until,
            "catalog_at": float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None,
            "house_todo": [str(t)[:20] for t in house if str(t).strip()][:len(GLOBAL_KEYS)]}


def has_products(request) -> bool:
    """A draft's ``handcards`` that names at least one product: anything else is no request (the worker leaves it
    out, as a request a job did not offer)."""
    return isinstance(request, dict) and isinstance(request.get("products"), list) and bool(request["products"])


def is_card_file(name) -> bool:
    """A file of the cards by its name: 「手卡_01_….png」, the skill's 「…直播手卡.zip」."""
    name = str(name or "")
    return bool(_CARD_FILE.match(name) or _ZIP_FILE.search(name))


def keep_cards(made: dict, attachments: list) -> tuple:
    """All or nothing (the bot's side): ``(attachments, made)``.  A good verdict whose every card arrived keeps them;
    otherwise none of the cards goes, nor any file that looks like one (``is_card_file``: a card whose upload the Mac
    took for failed may have arrived), and the verdict is a failure (「手卡只传上来 a / b 张」 for a good one)."""
    names = set(made.get("cards") or [])
    arrived = {a.get("name") for a in attachments if a.get("name") in names}
    if made.get("ok") and names and arrived == names:
        return attachments, made
    if made.get("ok"):
        made = {**made, "ok": False, "problems": [f"手卡只传上来 {len(arrived)} / {len(names)} 张"]}
    return [a for a in attachments if a.get("name") not in names and not is_card_file(a.get("name"))], made


def _todo_lines(made: dict) -> list:
    lines = [f"第 {n} 款 {'、'.join(i['todo'])}" for n, i in enumerate(made.get("items") or [], 1) if i.get("todo")]
    house = list(made.get("house_todo") or [])
    return lines + ([f"每张 {'、'.join(house)}"] if house else [])


def attention(made: dict) -> str:
    """The deal's line for a draft with cards (a person always looks first: the catalogue's date, the 待定, the cuts)
    or with none made (why)."""
    when = _day(made.get("catalog_at"))
    if made.get("ok"):
        parts = [f"价格按 {when} 同步的商品目录" if when else ""]
        todo, cuts = _todo_lines(made), list(made.get("cuts") or [])
        parts += [f"卡上待定：{'；'.join(todo)}" if todo else "", f"截短了：{'；'.join(cuts)}" if cuts else ""]
        said = "；".join(p for p in parts if p)
        return f"手卡 {len(made['cards'])} 张附在草稿里，要人看过再发" + (f"（{said}）" if said else "")
    why = "；".join(made.get("problems") or []) or "Mac 没说为什么"
    return f"手卡没做成：{why}；草稿里没附手卡"


def flagged(made: dict) -> bool:
    """Does a draft with these cards need a person's special care (``needs_attention``): none made, words cut, 待定 (a
    card's, or the house 口径's on every card)."""
    return not made.get("ok") or bool(made.get("cuts")) or bool(_todo_lines(made))


def packet_meta(made: dict) -> dict:
    """What a packet keeps of the verdict for the console and the checks before sending (``meta.handcards``)."""
    return {"ok": bool(made.get("ok")), "count": len(made.get("cards") or []) if made.get("ok") else 0,
            "note": str(made.get("note") or "")[:NOTE_CHARS], "problems": list(made.get("problems") or []),
            "cuts": list(made.get("cuts") or []), "promo_until": str(made.get("promo_until") or ""),
            "catalog_at": made.get("catalog_at")}


def promo_expired(cards, today: date) -> bool:
    """Cards (``meta.handcards``) whose 双11 line is past its end date (``today``: Beijing's): they cannot go out."""
    until = _date((cards or {}).get("promo_until")) if isinstance(cards, dict) else None
    return until is not None and today > until


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
    $LIVE_HANDCARD_PYTHON, then SKILL_PYTHON), ``skus`` (the hat index's skus.json, read only: sizes), ``source``
    (which 口径 the house globals are: the note says it), ``globals`` (commission, pack, ship_time, aftersale, ship_from,
    no_ship, express: a missing one is a red 待定; discount, 0.9 by default), ``caveats`` (lines every note repeats:
    「运费险是推断的」), ``promo`` ({"text" with {N}, "factor", "until" YYYY-MM-DD, "from"?}: the 双11 line, its N =
    日常价 × discount × factor floored, only from–until), ``max_catalogue_days`` (1–60, 7 by default: an older
    catalogue makes no card), ``send_zip`` (also the skill's zip, when it fits) and ``timeout_sec``."""
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
        days = raw.get("max_catalogue_days", CATALOGUE_DAYS)
        if isinstance(days, bool) or not 1 <= int(days) <= 60:
            raise ValueError(days)
        cfg["max_catalogue_days"] = int(days)
    except (TypeError, ValueError):
        return None, f"{path.name} 的 timeout_sec 要是秒数、max_catalogue_days 要是 1 到 60 天"
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

def catalog_info(workspace) -> dict:
    """The workspace's kb/products.json (``--sync-products``) as cards need it: ``items`` (its 抖店 products by id),
    ``synced_at`` (when the 抖店 part was synced; the file's own time for an older one; None when unknown) and
    ``stale`` (its last sync failed and kept the earlier products)."""
    try:
        data = json.loads((Path(workspace) / "kb" / "products.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    data = data if isinstance(data, dict) else {}
    dy = (data.get("platforms") or {}).get("dy") if isinstance(data.get("platforms"), dict) else None
    dy = dy if isinstance(dy, dict) else {}
    items = dy.get("items") if isinstance(dy.get("items"), list) else []
    at = dy.get("synced_at") if isinstance(dy.get("synced_at"), (int, float)) else data.get("synced_at")
    return {"items": {str(i["id"]): i for i in items if isinstance(i, dict) and str(i.get("id") or "").strip()},
            "synced_at": float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None,
            "stale": dy.get("stale") is True}


def catalogue_problem(info: dict, config: dict, now: Optional[float] = None) -> str:
    """Why the catalogue (``catalog_info``) makes no card now; "" when it does: no 抖店 product, its last sync failed
    (``stale``), it was synced longer ago than ``max_catalogue_days`` (the prices may have changed), or nobody knows
    when.  ``make`` checks it first; the worker before it runs a 「做手卡」 at all."""
    now = time.time() if now is None else now
    if not info.get("items"):
        return "这台 Mac 的 kb/products.json 里没有抖店商品（先在 Mac 上 --sync-products）"
    synced = info.get("synced_at")
    days = int(config.get("max_catalogue_days") or CATALOGUE_DAYS)
    if info.get("stale") or synced is None or now - synced > days * 86400:
        return f"商品目录太旧（上次同步 {_day(synced) or '不知道哪天'}），先同步再做手卡"
    return ""


def load_catalog(workspace) -> dict:
    """The 抖店 products of the workspace's kb/products.json by id; {} without one."""
    return catalog_info(workspace)["items"]


def sku_index(path) -> dict:
    """The hat index's skus.json (per style: code, colour, spec; read only) by item code: {code: {"colour", "spec"}};
    {} when it cannot be read.  Only its specs are used (the colours are the 抖店 SKU names')."""
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
_COLOUR_HEAD = re.compile(r"^\s*颜色(?:分类)?\s*[:：]\s*")
_NO_SPEC = re.compile(r"无规格")
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
    """``(colour, sizes)`` of a 抖店 SKU name (「M驼色」, 「均码黑色」, 「白色/M（56-58cm）」, 「无规格M（56-58cm）黑色」):
    the colour is what is left without the size and 「无规格」."""
    name = _COLOUR_HEAD.sub("", _NO_SPEC.sub(" ", str(name or "")))
    sizes = parse_spec(name)
    rest = _RANGE.sub(" ", name) if any(r for _, r in sizes) else _LABEL.sub(" ", name)
    return " ".join(rest.strip(_SEPARATORS).split()), sizes


def bad_colour(colour: str) -> bool:
    """A colour value that is none (empty, only digits: 「均码1」, or holding 尺码): the card says 待定 instead."""
    colour = str(colour or "").strip()
    return not colour or "尺码" in colour or bool(re.fullmatch(r"\d+", colour))


def item_facts(item: dict, index: dict) -> dict:
    """What the SKUs of a catalogue item say: ``colours`` (from the 抖店 SKU names, in the shop's order; the hat index
    disagrees with them for some 5% of the products), ``sizes`` (the hat index's spec by item code, else the SKU
    name's), ``prices`` (each SKU's) and ``named`` (SKUs whose size came from their names: no such code in the
    index)."""
    colours: list = []
    sizes: list = []
    prices: list = []
    named = 0
    for sku in item.get("skus") or []:
        if not isinstance(sku, dict):
            continue
        colour, from_name = parse_sku_name(str(sku.get("name") or ""))
        entry = index.get(str(sku.get("code") or "").strip())
        if entry is not None:
            found = parse_spec(entry["spec"])
        else:
            found = from_name
            named += 1
        if colour not in colours:
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
    """直播价: ``price`` × ``factor`` floored to the yuan, in exact decimals (315 × 0.9 → 283; the skill's script rounds
    the same way)."""
    return int((Decimal(str(price)) * Decimal(str(factor))).to_integral_value(rounding=ROUND_FLOOR))


def promo_price(daily: int, discount: float, factor: float) -> int:
    """The 双11 实付: 日常价 × discount × factor floored once to the yuan, in exact decimals (kb 第 6 节: 售价 × 0.9 × 0.85
    取整; flooring the 直播价 first differs by a yuan for one price in seven)."""
    exact = Decimal(str(daily)) * Decimal(str(discount)) * Decimal(str(factor))
    return int(exact.to_integral_value(rounding=ROUND_FLOOR))


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


def promo_line(daily: Optional[int], discount: float, promo: Optional[dict], today: date) -> str:
    """The 双11 line of a card (``promo``: from–until, both days in) for this 日常价; "" outside it or without one."""
    if not promo or daily is None or today > promo["until"] or (promo.get("from") and today < promo["from"]):
        return ""
    return promo["text"].replace("{N}", str(promo_price(daily, discount, promo["factor"])))


def _width(text: str) -> float:
    return sum(0.5 if ord(c) < 128 else 1.0 for c in text)


# Not in a card's file name: what a file system or the skill's file:// URL cannot carry (# % & ?), nor HTML.
_UNSAFE = re.compile(r'[\\/:*?"<>|#%&\s]')


def short_name(text: str) -> tuple:
    """``(short, fits)``: the card's 产品名称 as a file name and the skill's file:// URL can carry it (no / \\ : * ? " < >
    | # % & or blanks), and whether it is at most SHORT_MAX Han characters wide (a letter counts half).  One that does
    not fit is never cut (「复古西部巴拿」 says nothing): the model is asked again (the worker), else no card."""
    clean = _UNSAFE.sub("", str(text or ""))
    return clean, _width(clean) <= SHORT_MAX


def shorts_too_long(request) -> list:
    """The short names of a draft's request that do not fit the card (the worker asks the model once for others)."""
    products = request.get("products") if isinstance(request, dict) else None
    out = []
    for entry in products if isinstance(products, list) else []:
        short, fits = short_name((entry or {}).get("short") if isinstance(entry, dict) else "")
        if short and not fits:
            out.append(short)
    return out


_BOUNDARIES = (" · ", " / ", "／", "、", "，", ",", "；", ";", "。", " ")
_TRAILING = " ·/／、，,；;。"


def cut_words(text, limit: int) -> tuple:
    """``(text, cut)``: ``text`` with its blanks collapsed when it fits ``limit`` characters; else cut at its last word
    boundary (「 · 」, 「 / 」, 「、」, 「，」, a blank …) that keeps at least half of it, else at ``limit``."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text, False
    head = text[:limit + 1]                           # (a boundary right after the limit still counts)
    best = max(head.rfind(sep) for sep in _BOUNDARIES)
    out = text[:best] if best >= limit // 2 else text[:limit]
    return out.rstrip(_TRAILING) or text[:limit], True


_PREFIX = re.compile(r"^(?:抖音|小红书|淘宝|快手|视频号)\s*[-－_—:：·]+\s*")   # (the friend add's remark: 抖音-…)
_TAIL = re.compile(r"-[0-9A-Za-z_.]{4}$")      # the friend add's remark made unique: 「-」 and her 抖音号's last 4


def channel_of(call: str, name: str) -> tuple:
    """``(channel, cut)``: the cards' 场次 「<称呼>专属」, her 称呼 as the draft gave it, else her deal's name without a
    「抖音-」 prefix and the friend add's 「-ab12」 tail (the WeChat remark it sets; a 称呼 the draft copied from that
    remark loses the deal name's very tail too); nothing a file name or HTML cannot carry; cut at a word boundary to
    CALL_MAX.  Runtime data only, never in a file of the repository nor in a log."""
    name = " ".join(str(name or "").split())
    call = " ".join(str(call or "").split())
    tail = _TAIL.search(name)
    if call and tail and call != tail.group(0) and call.endswith(tail.group(0)):
        call = call[:-len(tail.group(0))].strip()
    raw = call or name
    stripped = _PREFIX.sub("", raw)
    if stripped != raw:
        stripped = _TAIL.sub("", stripped)
    clean = re.sub(r'[\\/:*?"<>|#%&]', "", stripped).strip()
    clean, cut = cut_words(clean, CALL_MAX)
    return (f"{clean}专属" if clean else "达人专属"), cut


def _norm_colour(text: str) -> str:
    text = _COLOUR_HEAD.sub("", " ".join(str(text or "").split()))
    return text[:-1] if len(text) > 1 and text.endswith("色") else text


def colours_text(colours: list) -> str:
    """The card's colours, HTML-escaped (the skill writes them into its markup as they are): 「棕咖色 / 驼色」; five or
    more short (a trailing 色 off) with the count: 「黑 / 墨绿 / … 共 10 色」."""
    if len(colours) > 4:
        return html.escape(" / ".join(_norm_colour(c) for c in colours) + f" 共 {len(colours)} 色")
    return html.escape(" / ".join(colours))


def _sells(raw, no: int) -> tuple:
    """The card's selling points ``[[bold, rest]]`` (at most SELLS_MAX; a rest starts with 「，」 unless it starts with
    punctuation already) and what was cut to fit (``cuts`` lines)."""
    out, cuts = [], []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        head, cut_head = cut_words(entry.get("head"), TEXT_MAX["head"])
        rest, cut_rest = cut_words(entry.get("rest"), TEXT_MAX["rest"])
        if not head:
            continue
        if rest and rest[0] not in "，,：:；;、。（(":
            rest = "，" + rest
        if cut_head or cut_rest:
            cuts.append(f"第 {no} 款卖点截成「{head}{rest}」")
        out.append([head, rest])
    if len(out) > SELLS_MAX:
        cuts.append(f"第 {no} 款卖点只用了前 {SELLS_MAX} 条")
    return out[:SELLS_MAX], cuts


def build_sheet(request, *, catalog: dict, skus: dict, config: dict, name: str, today: date, workspace,
                asked: Optional[list] = None, others: int = 0, reasked: bool = False) -> tuple:
    """The skill's 单子.json for a draft's request: ``(sheet, photos, report)``; its 场次 「<称呼>专属」 (``channel_of``:
    the request's ``call``, else ``name``, her deal's).  ``photos``: per card the main image
    and its crop (``crop_photos``'s plan).  ``report``: ``problems`` (any: no card at all), ``todo`` per card (its null
    fields, a red 待定), ``notes`` (what the operator should know: a colour that does not fit, a price left 待定),
    ``cuts`` (words cut to fit the card) and ``items``.  ``asked``: the products an operator's 「做手卡」 named
    (``[{"item_id", "sku_ids"}]``; None or [] when none): those, in that order (one the draft left out is a problem, one
    it added is left out); ``others``: the files the draft sends besides (one draft carries at most MAX_CARDS);
    ``reasked``: the model was asked once more about its short names (a short name still too long says so)."""
    report: dict = {"problems": [], "notes": [], "todo": {}, "items": [], "crops": [], "cuts": []}
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
        short, fits = short_name(entry.get("short"))
        label = short or item_id
        if not short:
            report["problems"].append(f"第 {no} 款（商品 {item_id}）：草稿没写短名")
        elif not fits:
            report["problems"].append(f"第 {no} 款短名「{short}」超过 {SHORT_MAX} 个字，卡上放不下"
                                      + ("（再问过一次也没改短）" if reasked else "") + "：再点一次「做手卡」")
        image = str(item.get("image_file") or "")
        real = os.path.realpath(Path(workspace) / image) if image else ""
        if not image or not real.startswith(assets + os.sep) or not os.path.isfile(real):
            report["problems"].append(f"第 {no} 款（{label}）：没有主图（{image or 'assets/products/dy-' + item_id + '.jpg'}）")
            continue
        facts = item_facts(item, skus)
        if facts["named"]:
            report["notes"].append(f"第 {no} 款有 {facts['named']} 个 SKU 帽子图库里没有，尺码按 SKU 名字拆的")
        colours = facts["colours"]
        odd = [c for c in colours if bad_colour(c)]
        asked_colours = ([str(c) for c in entry.get("colors") or [] if str(c).strip()]
                         if isinstance(entry.get("colors"), list) else [])
        if asked_colours and not odd:
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
        if odd or not colours:
            todo.append(TODO_LABELS["colors"])
            if odd:
                report["notes"].append(f"第 {no} 款抖店 SKU 的颜色名（{'、'.join(c or '空' for c in odd)}）不像颜色，"
                                       "卡上颜色写待定")
        brand, cut_brand = cut_words(entry.get("brand_line"), TEXT_MAX["brand_line"])
        audience, cut_audience = cut_words(entry.get("audience"), TEXT_MAX["audience"])
        sells, sell_cuts = _sells(entry.get("sells"), no)
        lacking = [w for w, v in (("品牌句", brand), ("适用人群", audience), ("卖点", sells)) if not v]
        if lacking:
            report["problems"].append(f"第 {no} 款（{label}）：草稿没写{'、'.join(lacking)}")
        if cut_brand:
            report["cuts"].append(f"第 {no} 款品牌句截成「{brand}」")
        if cut_audience:
            report["cuts"].append(f"第 {no} 款适用人群截成「{audience}」")
        report["cuts"].extend(sell_cuts)
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
            "short": short, "name": title, "brand_line": brand,
            "material": html.escape(material) if material else None, "size": size, "daily": daily,
            "stock": STOCK, "colors": TODO_HTML if odd or not colours else colours_text(colours),
            "audience": audience, "img": f"产品图/{no:02d}.jpg", "id": item_id, "sells": sells,
            "note": "；".join(w for w in (size_note(facts["sizes"]), care_of(material), "拍摄光线略有色差属正常") if w),
        }
        promo = promo_line(daily, discount, config.get("promo"), today)
        if promo:
            product["promo"] = promo
        out.append(product)
        report["todo"][no] = todo
        report["items"].append({"item_id": item_id, "short": short, "daily": daily, "live": live, "todo": todo,
                                "material_from": how})
    channel, cut_call = channel_of(request.get("call") or "", name)
    if cut_call:
        report["cuts"].append(f"称呼截成「{channel}」")
    sheet = {"channel": channel, "platform": "douyin", "pdf_name": f"{ZIP_NAME}_合集.pdf", "zip_name": ZIP_NAME,
             "globals": {**house, "discount": discount}, "products": out}
    return sheet, photos, report


def note_text(sheet: dict, report: dict, config: dict, today: date, folder: str = "",
              catalog_at: Optional[float] = None) -> str:
    """The operator's note on the cards (the skill's report, §6): the cards, the catalogue's date, every 待定, what was
    inferred, the 口径 reused (``source``, ``caveats``), the 双11 line, the stock, the crops, the cuts, where the session
    is kept on the Mac."""
    products = sheet["products"]
    discount = sheet["globals"]["discount"]
    lines = ["做了 " + str(len(products)) + f" 张手卡（{sheet['channel']}，抖音）："
             + "；".join(f"第 {n} 款 {p['short']}" + (f" {p['daily']} → 直播 {floor_price(p['daily'], discount)}"
                                                     if p.get("daily") is not None else "")
                        for n, p in enumerate(products, 1))]
    when = _day(catalog_at)
    if when:
        lines.append(f"价格按 {when} 同步的商品目录（kb/products.md）")
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
    promo = config.get("promo")
    promo_said = ""
    if promo and products and any(p.get("promo") for p in products):
        line = promo["text"].replace("{N}", "N")
        promo_said = (f"；{line}（N = 日常价 × {discount:g} × {promo['factor']:g} 取整，到 {promo['until']:%m-%d}，"
                      "过了这天草稿就不能发）")
    lines.append(f"沿用了{source}的口径：{words}；直播价 = 日常价 × {discount:g} 取整{promo_said}"
                 + "".join(f"；{c}" for c in config.get("caveats") or []))
    lines.append(f"库存：卡上一律写「{STOCK}」；真实库存没查（要聚水潭命令行），少的颜色开播前看一眼")
    if report["crops"]:
        lines.append("产品图：" + "；".join(report["crops"]))
    if report["cuts"]:
        lines.append("截短了：" + "；".join(report["cuts"]))
    lines.extend(report["notes"])
    if folder:
        lines.append(f"单子和图在 Mac 的 {folder}（留 {KEEP_DAYS} 天）")
    return "\n".join(lines)[:NOTE_CHARS]


# ── the Mac: photos (Pillow, under the skill's Python) ───────────────────

def side_margins(white: list) -> tuple:
    """``(left, right)``: how many columns (or rows) at each edge of a picture are blank canvas, each side on its own
    (``white``: per column, is it near white top to bottom).  The shop's collages put their photos on a white square,
    not always as wide on both sides (one in twelve of them)."""
    left = next((i for i, w in enumerate(white) if not w), len(white))
    right = next((i for i, w in enumerate(reversed(white)) if not w), len(white))
    return left, right


def seam_near(profile: list, centre: float, window: float) -> Optional[int]:
    """Where the seam between two photos is, near ``centre``: the index ``i + 1`` after the strongest step of
    ``profile`` (per column, the mean difference to the next one) within ``window`` of ``centre`` when it stands out
    (at least 3 times the profile's median and 12 grey levels); None (take the centre) otherwise.  Only a narrow
    window: on real collages the strongest step anywhere was a hat brim or hair as often as the seam."""
    if not profile:
        return None
    lo, hi = max(0, int(centre - window)), min(len(profile) - 1, int(centre + window))
    if lo > hi:
        return None
    best = max(range(lo, hi + 1), key=lambda i: profile[i])
    floor = max(12.0, 3 * statistics.median(profile))
    return best + 1 if profile[best] >= floor else None


def _margin(size: int, margin: int) -> int:
    return margin if size * 0.015 <= margin < size * 0.3 else 0


def crop_box(width: int, height: int, left: int = 0, right: int = 0, cell=None, cut_bottom: bool = False,
             seam_x=None, top: int = 0, bottom: int = 0, seam_y=None) -> tuple:
    """The part of a main image a card shows, ``(left, top, right, bottom)``: without its blank margins (each as
    measured, a real one only: at least 1.5% and less than 30% of that side), the cell of a 2×2 image asked for
    (``cell`` (column, row)) between the content's edges and its seams (``seam_x`` / ``seam_y``; the content's middle
    without one), 1% inside the cell's edges, and without its lowest CUT_BOTTOM (big brand letters)."""
    x0, x1 = float(_margin(width, left)), float(width - _margin(width, right))
    y0, y1 = float(_margin(height, top)), float(height - _margin(height, bottom))
    if cell is not None:
        col, row = int(cell[0]), int(cell[1])
        sx = float(seam_x) if seam_x is not None and x0 < float(seam_x) < x1 else (x0 + x1) / 2
        sy = float(seam_y) if seam_y is not None and y0 < float(seam_y) < y1 else (y0 + y1) / 2
        x0, x1 = (x0, sx) if col == 0 else (sx, x1)
        y0, y1 = (y0, sy) if row == 0 else (sy, y1)
        dx, dy = (x1 - x0) * CELL_INSET, (y1 - y0) * CELL_INSET
        x0, x1, y0, y1 = x0 + dx, x1 - dx, y0 + dy, y1 - dy
    if cut_bottom:
        y1 = y0 + (y1 - y0) * (1 - CUT_BOTTOM)
    return int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))


def _white_lines(px, length: int, span: range, columns: bool) -> list:
    """Per column (``columns``) or row: is it near white across ``span``."""
    need = len(span) * 0.985
    if columns:
        return [sum(1 for y in span if px[x, y] >= 242) >= need for x in range(length)]
    return [sum(1 for x in span if px[x, y] >= 242) >= need for y in range(length)]  # (rows: kept for symmetry)


def _steps(px, along: range, across: range, columns: bool) -> list:
    """Per column (``columns``) or row of ``along``: its mean grey difference to the next one, over ``across``."""
    out = []
    for a in list(along)[:-1]:
        if columns:
            total = sum(abs(px[a, b] - px[a + 1, b]) for b in across)
        else:
            total = sum(abs(px[b, a] - px[b, a + 1]) for b in across)
        out.append(total / max(1, len(across)))
    return out


def crop_photos(plan_path) -> int:
    """``python -I handcards.py crop <plan.json>`` (the skill's Python, Pillow): each photo of the plan
    (``{"base", "photos": [{"src", "dst", "cell", "cut_bottom"}]}``) cut by ``crop_box`` (its margins measured on each
    side; for a cell, the seams found near the content's middle, ``seam_near``) and written as a JPEG; prints
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
        px, w, h = small.load(), small.width, small.height
        sx_scale, sy_scale = image.width / w, image.height / h
        left, right = side_margins(_white_lines(px, w, range(h), columns=True))
        cell = photo.get("cell")
        seam_x = seam_y = None
        if cell is not None and w - left - right > 20:
            # (no top / bottom margin: 95 of 108 shop collages have their rows meet right at the middle)
            x0, x1, y0, y1 = left, w - right, 0, h
            col, row = int(cell[0]), int(cell[1])
            band = range(y0, (y0 + y1) // 2) if row == 0 else range((y0 + y1) // 2, y1)
            found = seam_near(_steps(px, range(x0, x1), band, columns=True), (x1 - x0) / 2,
                              (x1 - x0) * SEAM_WINDOW)
            sx = x0 + found if found is not None else (x0 + x1) / 2
            seam_x = sx * sx_scale if found is not None else None
            across = range(x0, int(sx)) if col == 0 else range(int(sx), x1)
            found = seam_near(_steps(px, range(y0, y1), across, columns=False), (y1 - y0) / 2,
                              (y1 - y0) * SEAM_WINDOW)
            seam_y = (y0 + found) * sy_scale if found is not None else None
        box = crop_box(image.width, image.height, int(round(left * sx_scale)), int(round(right * sx_scale)), cell,
                       bool(photo.get("cut_bottom")), seam_x=seam_x, seam_y=seam_y)
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
    cancelled) or ``timeout`` stops it (``stopped`` "cancelled" / "timeout"), with what it started (its browser)."""
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


def prune_sessions(home, now: Optional[float] = None, keep_days: int = KEEP_DAYS) -> int:
    """The folders of earlier cards under ``home`` (the worker's platform/handcards) older than ``keep_days`` go (the
    worker calls this with every prm job, the hourly sample-status rounds too); how many went."""
    home = Path(home)
    now = time.time() if now is None else now
    gone = 0
    try:
        folders = list(home.iterdir()) if home.is_dir() else []
    except OSError:
        return 0
    for old in folders:
        try:
            if old.is_dir() and now - old.stat().st_mtime > keep_days * 86400:
                shutil.rmtree(old, ignore_errors=True)
                gone += 1
        except OSError:
            pass
    return gone


def session_folder(home, job_id: int, now: Optional[float] = None) -> Path:
    """A new folder for one draft's cards under ``home``, the ones older than KEEP_DAYS gone first."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    prune_sessions(home, now)
    folder = home / f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}-job{int(job_id)}"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    return folder


def make(request, *, workspace, config: dict, name: str, job_id: int, home, asked: Optional[list] = None,
         others: int = 0, today: Optional[date] = None, heartbeat: Optional[Callable[[], bool]] = None,
         popen=subprocess.Popen, now: Optional[float] = None, reasked: bool = False) -> tuple:
    """The cards of one draft's request, ``(verdict, files)``: ``made_block`` and the 1x PNGs to send (the skill's zip
    after them when ``send_zip`` and it fits), or a verdict saying why there are none (and no file).  A catalogue
    synced longer than ``max_catalogue_days`` ago, or whose last sync failed, makes none (``catalogue_problem``).
    ``reasked``: the worker asked the model once more about its short names (a problem says so).  In a folder of its
    own under ``home`` (kept KEEP_DAYS: 单子.json and the photos for a person who fixes a 待定 by hand)."""
    now = time.time() if now is None else now
    today = today or beijing_today(now)
    info = catalog_info(workspace)
    synced = info["synced_at"]
    stale = catalogue_problem(info, config, now)
    if stale:
        return made_block(False, problems=[stale], catalog_at=synced), []
    sheet, photos, report = build_sheet(request, catalog=info["items"], skus=sku_index(config["skus"]), config=config,
                                        name=name, today=today, workspace=workspace, asked=asked, others=others,
                                        reasked=reasked)
    if report["problems"]:
        return made_block(False, problems=report["problems"], items=report["items"], catalog_at=synced), []
    hide = (sheet["channel"], sheet["channel"][:-2])        # her 称呼: kept out of what goes back (a script's output)
    folder = session_folder(home, job_id, now)
    plan = folder / "crop.json"
    plan.write_text(json.dumps({"base": str(folder), "photos": photos}, ensure_ascii=False), encoding="utf-8")
    timeout = config.get("timeout_sec") or 60 + 30 * len(sheet["products"])
    code, tail, stopped = run([config["python"], "-I", str(Path(__file__).resolve()), "crop", str(plan)], cwd=folder,
                              timeout=timeout, heartbeat=heartbeat, popen=popen)
    if stopped or code != 0 or not all((folder / p["dst"]).is_file() for p in photos):
        return made_block(False, problems=[_failed("裁产品图", code, tail, stopped, timeout, hide)],
                          items=report["items"], catalog_at=synced), []
    sheet_path = folder / "单子.json"
    sheet_path.write_text(json.dumps(sheet, ensure_ascii=False, indent=1), encoding="utf-8")
    cards_dir = folder / "手卡"
    argv = [config["python"], "-I", config["script"], str(sheet_path), "--out-dir", str(cards_dir)]
    code, tail, stopped = run(argv + (["--zip"] if config.get("send_zip") else ["--no-pdf"]), cwd=folder,
                              timeout=timeout, heartbeat=heartbeat, popen=popen)
    if stopped or code != 0:
        return made_block(False, problems=[_failed("出图", code, tail, stopped, timeout, hide)],
                          items=report["items"], catalog_at=synced), []
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
        return made_block(False, problems=problems, items=report["items"], catalog_at=synced), []
    if config.get("send_zip"):
        bundle = folder / f"{ZIP_NAME}.zip"
        if not bundle.is_file():
            report["notes"].append("压缩包没出来，只附了图")
        elif len(files) + others + 1 > MAX_CARDS:
            report["notes"].append(f"压缩包放不下（一份草稿最多 {MAX_CARDS} 个文件），只附了图")
        else:
            files.append(bundle)
    for extra in list(cards_dir.glob("*@2x.png")) + list(cards_dir.glob("*.html")):
        extra.unlink()                                 # (the 1x went back; the zip keeps its own copies)
    promo = config.get("promo")
    until = promo["until"].isoformat() if promo and any(p.get("promo") for p in sheet["products"]) else ""
    note = note_text(sheet, report, config, today, folder=str(folder).replace(str(Path.home()), "~", 1),
                     catalog_at=synced)
    house = [GLOBAL_LABELS[k] for k in GLOBAL_KEYS if sheet["globals"].get(k) is None]
    return made_block(True, cards=[f.name for f in files], items=report["items"], note=note, cuts=report["cuts"],
                      promo_until=until, catalog_at=synced, house_todo=house), files


def _failed(step: str, code, tail: str, stopped: str, timeout: float, hide=()) -> str:
    """Why a step did not make the cards: its timeout, a cancel, or its exit code and last line (her 称呼 taken out:
    the script prints the 场次)."""
    if stopped == "timeout":
        return f"{step}超过 {int(timeout)} 秒没做完"
    if stopped == "cancelled":
        return f"{step}时任务被取消了"
    last = tail.splitlines()[-1] if tail else ""
    for words in hide:
        if words:
            last = last.replace(words, "（称呼）")
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
