"""直播手卡 (PRM_HANDCARDS, 2026-11-09; src/prm/handcards.py): the bot makes the cards itself, a person still looks first.

The drafting Claude names the products (``handcards``) when she asks for 手卡 or an operator clicks 「做手卡」; the Mac
turns them into the skill live-handcard's 单子.json from data only (``build_sheet``: the 9 折 price floored, the 双11 line
until its end date, 「充足」, a null 待定 where no source says it, the main image's crop, a short name the card can hold),
crops the photos and runs the skill's script (here a fake that writes PNG headers), and the draft comes back with the
1x PNGs and a verdict.  The bot keeps such a draft for a person whatever would send it by itself, all of its cards or
none, and words whose cards were not made cannot go out as they are.  Products, ids, names and photos are made up.
"""

import io
import json
import struct
import sys
import textwrap
import time
import zlib
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from test_prm import CREATOR, Bot, Clock, draft
from test_prm_sample_sync import verified_order

from platform_fakes import DY_ITEM, DY_SKU, JST_ORDER
from src.prm import handcards as hc
from src.prm.result import DraftError, parse_draft
from src.prm.service import HANDCARDS_NOTE, apply_deal_action, card_ids, snapshot

ITEM, ITEM2, ITEM3, ITEM4 = "3000000000000000901", "3000000000000000902", "3000000000000000903", "3000000000000000904"
TITLE = "ACE7CN 测试巴拿马帽 100%羊毛英伦复古皮带扣平顶帽出游造型帽子"
GLOBALS = {"commission": "25%", "pack": "盒装 · 内置帽撑防压", "ship_time": "付款后 7 天内发货",
           "aftersale": "7天无理由 · 运费险", "ship_from": "广东东莞 · 包邮", "no_ship": "无 · 全国均发",
           "express": "中通快递", "discount": 0.9}
PROMO = {"text": "双11再减15%实付约{N}", "factor": 0.85, "until": "2026-11-11"}
TODAY = date(2026, 11, 9)
CELLS = {"左上": (200, 30, 30), "右上": (30, 160, 60), "左下": (40, 60, 200), "右下": (220, 200, 40)}


def catalog():
    """kb/products.json: four 抖店 hats (a 2×2 main image; a single one with brand letters; one the hat index does not
    know and whose title names no material; one whose SKUs differ in price) and a 小红书 one."""
    return {"version": 1, "platforms": {
        "xhs": {"items": [{"id": "6ad0000000000000000000a1", "title": "测试小红书帽", "price": "100.00", "skus": []}]},
        "dy": {"items": [
            {"id": ITEM, "title": TITLE, "price": "315.00", "image_file": f"assets/products/dy-{ITEM}.jpg",
             "skus": [{"id": "3100000000000000911", "name": "M驼色", "price": "315.00", "code": "A7269901001A-M"},
                      {"id": "3100000000000000912", "name": "M棕咖色", "price": "315.00", "code": "A7269901001B-M"}]},
            {"id": ITEM2, "title": "ACE7CN 测试药盒帽 pillboxhat 100%羊毛 英伦复古无檐圆顶小礼帽", "price": "269.00",
             "image_file": f"assets/products/dy-{ITEM2}.jpg",
             "skus": [{"id": "3100000000000000921", "name": "白色/M（56-58cm）", "price": "269.00", "code": "A72599010B-M"},
                      {"id": "3100000000000000922", "name": "白色/L（58-60cm）", "price": "269.00", "code": "A72599010B-L"},
                      {"id": "3100000000000000923", "name": "黑色/M（56-58cm）", "price": "269.00", "code": "A72599010A-M"}]},
            {"id": ITEM3, "title": "ACE7CN 测试度假遮阳帽 出游百搭帽子女", "price": "199.00",
             "image_file": f"assets/products/dy-{ITEM3}.jpg",
             "skus": [{"id": "3100000000000000931", "name": "均码米色", "price": "199.00", "code": "local_x1"}]},
            {"id": ITEM4, "title": "ACE7CN 测试贝雷帽 羊毛混纺画家帽", "price": "290.00",
             "image_file": f"assets/products/dy-{ITEM4}.jpg",
             "skus": [{"id": "3100000000000000941", "name": "M黑色", "price": "290.00", "code": "A7269904001A-M"},
                      {"id": "3100000000000000942", "name": "L黑色", "price": "320.00", "code": "A7269904001A-L"}]},
        ]}}}


def hat_skus():
    """The hat index's skus.json (per style: code, colour, spec)."""
    return {"ACE7CN202699010001": [{"code": "A7269901001A-M", "colour": "驼色", "spec": "尺码:M(56-58cm)"},
                                   {"code": "A7269901001B-M", "colour": "颜色:棕咖色", "spec": "尺码:M(56-58cm)"}],
            "A72599010B": [{"code": "A72599010B-M", "colour": "白色", "spec": "尺码:M(56-58cm)"},
                           {"code": "A72599010B-L", "colour": "白色", "spec": "尺码:L(58-60cm)"}],
            "A72599010A": [{"code": "A72599010A-M", "colour": "黑色", "spec": "尺码:M(56-58cm)"}],
            "ACE7CN202699040001": [{"code": "A7269904001A-M", "colour": "黑色", "spec": "尺码:M(56-58cm)"},
                                   {"code": "A7269904001A-L", "colour": "黑色", "spec": "尺码:L(58-60cm)"}]}


def grid_image(path):
    """A shop main image: a 2×2 collage of 540×720 photos on a white 1440×1440 square (180 px white on each side)."""
    image = Image.new("RGB", (1440, 1440), (255, 255, 255))
    for (col, row), colour in zip(((0, 0), (1, 0), (0, 1), (1, 1)), CELLS.values()):
        image.paste(colour, (180 + col * 540, row * 720, 180 + (col + 1) * 540, (row + 1) * 720))
    image.save(path, "JPEG", quality=95)


def single_image(path, colour=(90, 90, 90)):
    """A single 3:4 photo on the white square, its lowest part white letters (the brand) on it."""
    image = Image.new("RGB", (1440, 1440), (255, 255, 255))
    image.paste(colour, (180, 0, 1260, 1440))
    image.paste((250, 250, 250), (300, 1220, 1140, 1320))
    image.save(path, "JPEG", quality=95)


@pytest.fixture
def ws(tmp_path):
    """The Mac's workspace: kb/products.json and the main images."""
    root = tmp_path / "biz"
    (root / "kb").mkdir(parents=True)
    (root / "assets" / "products").mkdir(parents=True)
    (root / "kb" / "products.json").write_text(json.dumps(catalog(), ensure_ascii=False), encoding="utf-8")
    grid_image(root / "assets" / "products" / f"dy-{ITEM}.jpg")
    for item in (ITEM2, ITEM3, ITEM4):
        single_image(root / "assets" / "products" / f"dy-{item}.jpg")
    return root


def config(**more):
    """The Mac's handcards settings as ``load_config`` gives them (no files checked: the fake script and Python)."""
    promo = more.pop("promo", PROMO)
    return {"script": more.pop("script", ""), "python": sys.executable, "skus": more.pop("skus", ""),
            "source": "老板模板（测试）", "caveats": ["运费险是推断的"], "send_zip": False, "timeout_sec": 0,
            "globals": dict(GLOBALS), "promo": None if promo is None else {
                **promo, "until": date.fromisoformat(promo["until"]),
                "from": date.fromisoformat(promo["from"]) if promo.get("from") else None}, **more}


def product(item=ITEM, **more):
    """One product of the drafting Claude's request, its words written from the title."""
    return {"item_id": item, "colors": [], "photo_cell": "", "cut_bottom": False, "short": "测试巴拿马帽",
            "brand_line": "测试巴拿马帽 · 100%羊毛 · 英伦复古平顶帽", "audience": "女性 · 秋冬出游 / 通勤 · 英伦复古风",
            "sells": [{"head": "平顶宽檐", "rest": "，英伦复古"}, {"head": "100%羊毛", "rest": "秋冬保暖"}], **more}


def build(ws, *products, asked=None, others=0, today=TODAY, cfg=None, name="小美", call=""):
    request = {"products": list(products), **({"call": call} if call else {})}
    return hc.build_sheet(request, catalog=hc.load_catalog(ws), skus=hc_index(), config=cfg or config(), name=name,
                          today=today, workspace=ws, asked=asked, others=others)


def hc_index():
    index = {}
    for rows in hat_skus().values():
        for row in rows:
            index[row["code"]] = {"colour": row["colour"], "spec": row["spec"]}
    return index


# ── the 单子.json: prices, the 双11 line, stock, 待定, names ────────────────

@pytest.mark.parametrize("daily, live", [(315, 283), (299, 269), (279, 251), (570, 513), (300, 270), (188, 169)])
def test_the_live_price_is_nine_tenths_floored_to_the_yuan(daily, live):
    assert hc.floor_price(daily, 0.9) == live


def test_a_sheet_takes_its_facts_from_data_and_its_words_from_the_draft(ws):
    sheet, photos, report = build(ws, product(), call="小美")
    assert report["problems"] == []
    assert sheet["channel"] == "小美专属" and sheet["platform"] == "douyin"
    assert sheet["globals"] == GLOBALS
    [p] = sheet["products"]
    assert (p["daily"], p["stock"], p["material"], p["size"], p["id"]) == (315, "充足", "100%羊毛", "M 56-58cm", ITEM)
    assert p["colors"] == "驼色 / 棕咖色" and p["name"] == TITLE and p["img"] == "产品图/01.jpg"
    assert p["sells"] == [["平顶宽檐", "，英伦复古"], ["100%羊毛", "，秋冬保暖"]]        # (a rest gets its 「，」)
    assert p["note"] == "M 码头围 56-58cm；羊毛帽勿水洗、勿重压，可用软毛刷清灰；拍摄光线略有色差属正常"
    assert p["promo"] == "双11再减15%实付约240"                                      # 315 → 283 → 283 × 0.85 = 240
    assert "live" not in p                                                            # (the script works it out)
    assert photos == [{"src": str((ws / "assets" / "products" / f"dy-{ITEM}.jpg").resolve()), "dst": "产品图/01.jpg",
                       "cell": None, "cut_bottom": False}]


@pytest.mark.parametrize("today, promo, line", [
    (date(2026, 11, 11), PROMO, "双11再减15%实付约240"),                       # the end date is still in
    (date(2026, 11, 12), PROMO, None),                                           # after it: no line
    (date(2026, 11, 9), {**PROMO, "from": "2026-11-10"}, None),                  # before its start: none either
    (date(2026, 11, 10), {**PROMO, "from": "2026-11-10"}, "双11再减15%实付约240"),
    (date(2026, 11, 9), None, None),                                             # no promo set
    (date(2026, 11, 9), {"text": "立减{N}元起", "factor": 0.8, "until": "2026-12-01"}, "立减226元起"),
])
def test_the_promo_line_follows_its_setting_and_end_date(ws, today, promo, line):
    sheet, _, _ = build(ws, product(), today=today, cfg=config(promo=promo))
    assert sheet["products"][0].get("promo") == line


def test_stock_is_always_sufficient_and_the_note_says_the_real_stock_was_not_read(ws):
    sheet, _, report = build(ws, product(), product(ITEM2, short="无檐小礼帽"))
    assert [p["stock"] for p in sheet["products"]] == ["充足", "充足"]
    note = hc.note_text(sheet, report, config(), TODAY)
    assert "卡上一律写「充足」" in note and "真实库存没查" in note


def test_a_field_without_a_source_is_null_and_the_note_lists_it(ws):
    """No material in the title (and none the draft named from it), SKU prices that differ, a house value missing: null
    (a red 待定 on the card), and the note says which card lacks what."""
    cfg = config()
    cfg["globals"]["express"] = None
    sheet, _, report = build(ws, product(ITEM3, short="度假遮阳帽"), product(ITEM4, short="贝雷帽"), cfg=cfg)
    third, fourth = sheet["products"]
    assert third["material"] is None and third["size"] == "均码" and third["colors"] == "米色"
    assert fourth["daily"] is None and fourth["material"] == "羊毛混纺" and "promo" not in fourth
    assert sheet["globals"]["express"] is None
    assert report["todo"] == {1: ["材质"], 2: ["日常价"]}
    note = hc.note_text(sheet, report, cfg, TODAY)
    assert "待定（卡上红字）：第 1 款 材质；第 2 款 日常价；每张的 快递" in note
    assert "第 2 款日常价待定：SKU 价格不一样（290、320）" in note
    assert "第 1 款有 1 个 SKU 帽子图库里没有，颜色尺码按 SKU 名字拆的" in note


def test_a_material_the_draft_names_counts_only_when_the_title_has_those_words(ws):
    sheet, _, report = build(ws, product(ITEM3, short="度假遮阳帽", material="出游百搭"))
    assert sheet["products"][0]["material"] == "出游百搭" and report["items"][0]["material_from"] == "said"
    sheet, _, _ = build(ws, product(ITEM3, short="度假遮阳帽", material="100%羊毛"))         # not in the title: 待定
    assert sheet["products"][0]["material"] is None


@pytest.mark.parametrize("text, short, cut", [
    ("西部巴拿马帽", "西部巴拿马帽", False),
    ("一体成型马术帽", "一体成型马术", True),
    ("pillbox礼帽", "pillbox礼帽", False),            # letters count half: 3.5 + 2
    ("pillbox小礼帽", "pillbox小礼", True),           # 3.5 + 3: one too many
    ("翻边/小礼帽", "翻边小礼帽", False),               # nothing a file name cannot carry
    ("  贝雷 帽 ", "贝雷帽", False),
])
def test_a_short_name_fits_the_card_and_a_file_name(text, short, cut):
    assert hc.short_name(text) == (short, cut)


def test_a_short_name_cut_says_so_and_a_missing_one_makes_no_card(ws):
    sheet, _, report = build(ws, product(short="一体成型马术帽"))
    assert sheet["products"][0]["short"] == "一体成型马术"
    assert "第 1 款短名「一体成型马术帽」超过 6 个字，卡上用了「一体成型马术」" in report["notes"]
    _, _, report = build(ws, product(short=" "))
    assert report["problems"] == [f"第 1 款（商品 {ITEM}）：草稿没写短名"]


def test_words_the_card_cannot_hold_are_cut_and_missing_words_make_no_card(ws):
    sheet, _, report = build(ws, product(brand_line="长" * 40, sells=[{"head": "a", "rest": "b"}] * 6))
    assert len(sheet["products"][0]["brand_line"]) == 30 and len(sheet["products"][0]["sells"]) == 4
    assert any("字太长，卡上截短了" in n for n in report["notes"])
    _, _, report = build(ws, product(audience="", sells=[]))
    assert report["problems"] == ["第 1 款（测试巴拿马帽）：草稿没写适用人群、卖点"]


def test_colours_follow_what_she_picked_and_the_card_says_when_one_does_not_fit(ws):
    sheet, _, report = build(ws, product(colors=["棕咖"]))
    assert sheet["products"][0]["colors"] == "棕咖色" and report["notes"] == []
    sheet, _, report = build(ws, product(colors=["棕色"]))
    assert sheet["products"][0]["colors"] == "驼色 / 棕咖色"
    assert report["notes"] == ["第 1 款要的颜色「棕色」这款没有（有 驼色 / 棕咖色），卡上写了全部颜色"]
    many = [f"颜{n}色" for n in range(1, 11)]
    assert hc.colours_text(many) == " / ".join(f"颜{n}" for n in range(1, 11)) + " 共 10 色"
    assert hc.colours_text(["驼色", "黑色"]) == "驼色 / 黑色"


@pytest.mark.parametrize("spec, sizes, text", [
    ("尺码:M(56-58cm)", [("M", "56-58")], "M 56-58cm"),
    ("尺码:F(52~60cm)", [("F", "52-60")], "F 52-60cm"),
    ("尺码:F (52-60cm)", [("F", "52-60")], "F 52-60cm"),
    ("尺码:M(56-58cm) L(58-60cm)", [("M", "56-58"), ("L", "58-60")], "M56-58/L58-60cm"),
    ("尺码:均码", [("均码", "")], "均码"),
    ("尺码:M", [("M", "")], "M 码"),
    ("尺码:可调节", [("可调节", "")], "可调节"),
    ("尺码:(56-58cm)", [("", "56-58")], "56-58cm"),
    ("无规格", [], None),
    ("尺码:喜欢佩戴深一点的可选大一码", [], None),
])
def test_sizes_are_read_from_the_hat_index_spec(spec, sizes, text):
    assert hc.parse_spec(spec) == sizes and hc.size_text(sizes) == text


@pytest.mark.parametrize("name, colour, sizes", [
    ("M驼色", "驼色", [("M", "")]),
    ("均码黑色", "黑色", [("均码", "")]),
    ("白色/M（56-58cm）", "白色", [("M", "56-58")]),
    ("M（56-58cm）黑色", "黑色", [("M", "56-58")]),
    ("黑色", "黑色", []),
])
def test_a_sku_name_is_read_when_the_hat_index_has_no_such_code(name, colour, sizes):
    assert hc.parse_sku_name(name) == (colour, sizes)


def test_two_sizes_make_the_compact_spec_and_note(ws):
    sheet, _, _ = build(ws, product(ITEM2, short="无檐小礼帽"))
    p = sheet["products"][0]
    assert p["size"] == "M56-58/L58-60cm" and p["colors"] == "白色 / 黑色"
    assert p["note"].startswith("M 码头围 56-58cm、L 码头围 58-60cm；")


@pytest.mark.parametrize("call, name, channel", [
    ("小美", "抖音-另一个名字", "小美专属"), ("", "抖音-小美", "小美专属"), ("", "小红书－小美 ", "小美专属"),
    ("", "dy_小美", "dy_小美专属"), ("", "", "达人专属"), ("小/美", "", "小美专属"), ("一" * 20, "", "一" * 16 + "专属"),
])
def test_the_channel_is_her_call_or_her_deals_name_without_the_douyin_prefix(call, name, channel):
    assert hc.channel_of(call, name) == channel


def test_products_not_in_the_catalogue_or_too_many_make_no_card(ws):
    _, _, report = build(ws, product("6ad0000000000000000000a1"))           # a 小红书 product is no 抖店 one
    assert report["problems"] == ["第 1 款：商品 6ad0000000000000000000a1 不在 kb/products.md 的抖店目录里"]
    _, _, report = build(ws, product(), product(ITEM2, short="小礼帽"), others=8)
    assert report["problems"] == ["一份草稿最多附 9 个文件：这次 2 张手卡加 8 个别的附件，放不下，分两次做"]
    (ws / "assets" / "products" / f"dy-{ITEM}.jpg").unlink()
    _, _, report = build(ws, product())
    assert report["problems"] == [f"第 1 款（测试巴拿马帽）：没有主图（assets/products/dy-{ITEM}.jpg）"]


def test_an_operators_products_are_the_ones_made(ws):
    """「做手卡」 named two products: those, in its order; one the draft added is left out, one it left out is a problem."""
    asked = [{"item_id": ITEM2, "sku_ids": []}, {"item_id": ITEM, "sku_ids": []}]
    sheet, _, report = build(ws, product(), product(ITEM2, short="小礼帽"), product(ITEM4, short="贝雷帽"), asked=asked)
    assert [p["id"] for p in sheet["products"]] == [ITEM2, ITEM] and report["problems"] == []
    assert f"运营点的是 2 款，草稿多写的 {ITEM4} 没做" in report["notes"]
    _, _, report = build(ws, product(), asked=asked)
    assert report["problems"] == [f"运营点的商品 {ITEM2}，草稿没给它写卖点"]


# ── the photos ───────────────────────────────────────────────────────────

def test_the_crop_box_takes_the_side_margins_the_cell_and_the_brand_letters_off():
    assert hc.crop_box(1440, 1440) == (0, 0, 1440, 1440)
    assert hc.crop_box(1440, 1440, 180) == (180, 0, 1260, 1440)
    assert hc.crop_box(1440, 1440, 180, cut_bottom=True) == (180, 0, 1260, 1181)
    assert hc.crop_box(1440, 1440, 180, cell=(0, 1)) == (185, 727, 715, 1433)             # 左下, 1% inside
    assert hc.crop_box(1440, 1440, 10) == (0, 0, 1440, 1440)           # under 1.5%: the picture, not a margin
    assert hc.crop_box(1440, 1440, 500) == (0, 0, 1440, 1440)          # 30% or more on a side: no canvas
    assert hc.side_margin([True] * 3 + [False] * 4 + [True] * 5) == 3  # the smaller side (the canvas is even)
    assert hc.side_margin([True] * 4) == 4 and hc.side_margin([False, True]) == 0


def test_the_sheet_asks_for_the_cell_and_the_cut_the_draft_chose(ws):
    _, photos, report = build(ws, product(photo_cell="左下"), product(ITEM2, short="小礼帽", cut_bottom=True))
    assert [(p["cell"], p["cut_bottom"]) for p in photos] == [([0, 1], False), (None, True)]
    assert report["crops"] == ["第 1 款主图左下那一格", "第 2 款去掉了底下的品牌字"]


@pytest.mark.parametrize("cell", list(CELLS))
def test_the_photos_are_cropped_to_the_cell_asked_for(ws, tmp_path, cell):
    """Pillow under the skill's Python (here this one): the 2×2 image's cell is the colour of that cell, its white side
    margins gone; a single photo loses its brand letters."""
    plan = tmp_path / "crop.json"
    plan.write_text(json.dumps({"base": str(tmp_path), "photos": [
        {"src": str(ws / "assets" / "products" / f"dy-{ITEM}.jpg"), "dst": "产品图/01.jpg", "cell": list(hc.CELLS[cell]),
         "cut_bottom": False},
        {"src": str(ws / "assets" / "products" / f"dy-{ITEM2}.jpg"), "dst": "产品图/02.jpg", "cell": None,
         "cut_bottom": True}]}), encoding="utf-8")
    assert hc.crop_photos(plan) == 0
    with Image.open(tmp_path / "产品图" / "01.jpg") as one:
        assert abs(one.width - 530) <= 6 and abs(one.height - 706) <= 6
        assert all(abs(a - b) < 20 for a, b in zip(one.getpixel((one.width // 2, one.height // 2)), CELLS[cell]))
        assert all(abs(a - b) < 20 for a, b in zip(one.getpixel((2, 2)), CELLS[cell]))        # no white margin left
    with Image.open(tmp_path / "产品图" / "02.jpg") as two:
        assert abs(two.width - 1080) <= 6 and abs(two.height - 1181) <= 2
        assert max(two.getpixel((540, two.height - 5))) < 120                             # the letters' band is gone


# ── making the cards (the skill's script faked) ──────────────────────────

FAKE_CARDS = textwrap.dedent("""\
    import json, os, struct, sys, time, zlib, zipfile
    args = sys.argv[1:]
    spec_path, out = args[0], args[args.index("--out-dir") + 1]
    sheet = json.load(open(spec_path, encoding="utf-8"))
    with open(os.environ["FAKE_CARDS_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args, "sheet": sheet}, ensure_ascii=False) + "\\n")
    mode = os.environ.get("FAKE_CARDS_MODE", "")
    if mode == "fail":
        print("Traceback: boom")
        sys.exit(1)
    if mode == "slow":
        time.sleep(60)
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    w, h = (800, 600) if mode == "small" else (1600, 1200)
    png = (b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IEND", b""))
    os.makedirs(out, exist_ok=True)
    base_dir = os.path.dirname(os.path.abspath(spec_path))
    for n, p in enumerate(sheet["products"], 1):
        assert os.path.exists(os.path.join(base_dir, p["img"])), p["img"]
        if mode == "skip" and n == 2:
            continue
        base = os.path.join(out, "手卡_%02d_%s" % (n, p["short"]))
        for suffix in (".png", "@2x.png"):
            open(base + suffix, "wb").write(png)
        open(base + ".html", "w").write("<html></html>")
    if "--zip" in args:
        with zipfile.ZipFile(os.path.join(os.path.dirname(out), sheet["channel"] + "直播手卡.zip"), "w") as z:
            z.writestr("x.txt", "x")
""")


@pytest.fixture
def cards(tmp_path, monkeypatch):
    script = tmp_path / "make_cards_local.py"
    script.write_text(FAKE_CARDS, encoding="utf-8")
    log = tmp_path / "cards.jsonl"
    monkeypatch.setenv("FAKE_CARDS_LOG", str(log))
    skus = tmp_path / "skus.json"
    skus.write_text(json.dumps(hat_skus(), ensure_ascii=False), encoding="utf-8")
    return type("Cards", (), {"script": str(script), "skus": str(skus), "home": tmp_path / "home",
                              "calls": staticmethod(lambda: [json.loads(line) for line in
                                                             log.read_text(encoding="utf-8").splitlines()]
                                                    if log.exists() else [])})


def make(ws, cards, *products, asked=None, others=0, beat=None, **cfg):
    return hc.make({"products": list(products), "call": "小美"}, workspace=ws,
                   config=config(script=cards.script, skus=cards.skus, **cfg), name="抖音-小美", job_id=41,
                   home=cards.home, asked=asked, others=others, today=TODAY, heartbeat=beat)


def test_the_cards_are_made_in_a_folder_of_their_own_and_only_the_1x_pngs_go(ws, cards):
    made, files = make(ws, cards, product(photo_cell="左下"), product(ITEM2, short="无檐小礼帽", cut_bottom=True))
    assert made["ok"] is True and made["problems"] == []
    assert made["cards"] == ["手卡_01_测试巴拿马帽.png", "手卡_02_无檐小礼帽.png"] == [f.name for f in files]
    assert all(hc.png_size(f) == (1600, 1200) for f in files)
    [call] = cards.calls()
    assert "--no-pdf" in call["argv"] and "--zip" not in call["argv"]
    assert call["sheet"]["channel"] == "小美专属" and [p["daily"] for p in call["sheet"]["products"]] == [315, 269]
    folder = files[0].parent.parent
    assert folder.parent == cards.home and folder.name.endswith("-job41")
    assert (folder / "单子.json").is_file() and (folder / "产品图" / "01.jpg").is_file()
    assert not list(folder.glob("手卡/*@2x.png")) and not list(folder.glob("手卡/*.html"))     # (tidied)
    with Image.open(folder / "产品图" / "01.jpg") as photo:
        assert all(abs(a - b) < 20 for a, b in zip(photo.getpixel((photo.width // 2, photo.height // 2)), CELLS["左下"]))
    note = made["note"]
    for words in ("做了 2 张手卡（小美专属，抖音）：第 1 款 测试巴拿马帽 315 → 直播 283；第 2 款 无檐小礼帽 269 → 直播 242",
                  "待定（卡上红字）：没有", "推断的：短名、品牌句、适用人群、卖点是按商品标题写的",
                  "沿用了老板模板（测试）的口径：佣金 25%，包装 盒装 · 内置帽撑防压",
                  "直播价 = 日常价 × 0.9 取整；双11再减15%实付约N（N = 直播价 × 0.85 取整，到 11-11）；运费险是推断的",
                  "产品图：第 1 款主图左下那一格；第 2 款去掉了底下的品牌字", "（留 7 天）"):
        assert words in note, words


def test_a_zip_goes_too_only_when_asked_and_when_it_fits(ws, cards):
    made, files = make(ws, cards, product(), send_zip=True)
    assert [f.name for f in files] == ["手卡_01_测试巴拿马帽.png", "小美专属直播手卡.zip"] and made["ok"]
    assert "--zip" in cards.calls()[-1]["argv"]
    made, files = make(ws, cards, product(), send_zip=True, others=8)
    assert [f.name for f in files] == ["手卡_01_测试巴拿马帽.png"] and "压缩包放不下" in made["note"]


@pytest.mark.parametrize("mode, problem", [
    ("fail", "出图出错了（退出码 1）：Traceback: boom"),
    ("skip", "第 2 张（无檐小礼帽）没出来"),
    ("small", "第 1 张（测试巴拿马帽）尺寸不对（800×600）"),
])
def test_a_card_not_made_makes_no_card_at_all(ws, cards, monkeypatch, mode, problem):
    monkeypatch.setenv("FAKE_CARDS_MODE", mode)
    made, files = make(ws, cards, product(), product(ITEM2, short="无檐小礼帽"))
    assert (made["ok"], files, made["cards"]) == (False, [], []) and problem in made["problems"]


def test_a_request_without_data_never_runs_the_script(ws, cards):
    made, files = make(ws, cards, product("3000000000000000999"))
    assert (made["ok"], files) == (False, []) and cards.calls() == []
    assert made["problems"] == ["第 1 款：商品 3000000000000000999 不在 kb/products.md 的抖店目录里"]
    (ws / "kb" / "products.json").unlink()
    made, _ = make(ws, cards, product())
    assert made["problems"] == ["这台 Mac 的 kb/products.json 里没有抖店商品（先在 Mac 上 --sync-products）"]


def test_a_run_is_stopped_by_its_timeout_or_a_cancel(tmp_path):
    started = time.monotonic()
    code, _, stopped = hc.run([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, timeout=1)
    assert stopped == "timeout" and time.monotonic() - started < 10
    beats = []
    code, _, stopped = hc.run([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, timeout=60,
                              heartbeat=lambda: beats.append(1) or True, every=0.2)
    assert stopped == "cancelled" and beats
    code, tail, stopped = hc.run([sys.executable, "-c", "print('done')"], cwd=tmp_path, timeout=30)
    assert (code, tail, stopped) == (0, "done", "")


def test_old_session_folders_go(tmp_path):
    home = tmp_path / "home"
    old = home / "20261001-090000-job1"
    old.mkdir(parents=True)
    import os
    os.utime(old, (time.time() - 8 * 86400, time.time() - 8 * 86400))
    folder = hc.session_folder(home, 2)
    assert folder.is_dir() and not old.exists()


# ── the Mac's settings ───────────────────────────────────────────────────

def test_the_settings_say_what_is_missing(tmp_path, cards):
    path = tmp_path / "handcards.json"
    assert hc.load_config(path)[0] is None and "handcards.example.json" in hc.load_config(path)[1]
    good = {"script": cards.script, "python": sys.executable, "globals": GLOBALS, "promo": PROMO, "source": "老板模板"}
    path.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
    cfg, why = hc.load_config(path)
    assert why == "" and cfg["promo"]["until"] == date(2026, 11, 11) and cfg["globals"]["discount"] == 0.9
    assert cfg["globals"]["commission"] == "25%" and cfg["send_zip"] is False
    for bad, words in ((dict(good, enabled=False), "enabled 不是 true"),
                       (dict(good, promo={"text": "双11", "factor": 0.85, "until": "2026-11-11"}), "promo 要写成"),
                       (dict(good, promo={**PROMO, "until": "11-11"}), "promo 要写成"),
                       (dict(good, globals={**GLOBALS, "discount": 9}), "discount 要在 0.5 到 1 之间"),
                       (dict(good, script=str(tmp_path / "none.py")), "找不到手卡脚本"),
                       (dict(good, python=str(tmp_path / "none")), "找不到手卡用的 Python")):
        path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        cfg, why = hc.load_config(path)
        assert cfg is None and words in why, (bad, why)
    path.write_text("[1]", encoding="utf-8")
    assert "不是一个 JSON 对象" in hc.load_config(path)[1]


def test_the_example_settings_are_the_house_template(tmp_path):
    example = json.loads((Path(__file__).resolve().parents[1] / "tools" / "claude_relay" / "handcards.example.json")
                         .read_text(encoding="utf-8"))
    assert example["globals"] == GLOBALS and example["promo"] == PROMO and example["send_zip"] is False


# ── the verdict, as the bot reads it ─────────────────────────────────────

def test_the_verdict_is_checked_and_its_cards_come_all_or_none():
    made = hc.check_made(hc.made_block(True, cards=["手卡_01_a.png", "手卡_02_b.png"], note="n",
                                       items=[{"item_id": ITEM, "short": "a", "todo": ["材质"]}]))
    assert made == {"ok": True, "cards": ["手卡_01_a.png", "手卡_02_b.png"], "note": "n", "problems": [],
                    "items": [{"item_id": ITEM, "short": "a", "todo": ["材质"]}]}
    files = [{"name": "手卡_01_a.png"}, {"name": "x.jpg"}, {"name": "手卡_02_b.png"}]
    assert hc.keep_cards(made, files) == (files, made)
    kept, short = hc.keep_cards(made, files[:2])
    assert kept == [{"name": "x.jpg"}] and short["ok"] is False and short["problems"] == ["手卡只传上来 1 / 2 张"]
    failed = hc.check_made(hc.made_block(False, cards=["手卡_01_a.png"], problems=["没传上去"]))
    assert failed["ok"] is False and hc.keep_cards(failed, files)[0] == [{"name": "x.jpg"}, {"name": "手卡_02_b.png"}]
    for bad in ([], {"cards": "a.png"}, {"cards": ["../a.png"]}, {"cards": ["a"] * 10}, {"problems": "x"}):
        with pytest.raises(ValueError):
            hc.check_made(bad)
    assert hc.attention(made) == "手卡 2 张附在草稿里，要人看过再发（卡上待定：第 1 款 材质）"
    assert hc.attention(failed) == "手卡没做成：没传上去；草稿里没附手卡"


def test_a_draft_with_a_malformed_verdict_is_refused():
    with pytest.raises(DraftError) as e:
        parse_draft(json.dumps(draft(handcards_made={"cards": "x"}), ensure_ascii=False))
    assert e.value.code == "bad_handcards"
    assert parse_draft(json.dumps(draft(), ensure_ascii=False)).handcards is None


# ── the bot: drafts with cards wait for a person ─────────────────────────

LINE = "老师，手卡整理好啦～"
CARDS = (("手卡_01_测试巴拿马帽.png", b"png-1"), ("手卡_02_无檐小礼帽.png", b"png-2"))
HOURS = "18:00-09:00"


def at(hour, minute=0, day=9):
    """A time of day on this machine's clock (a Monday, 2026-11-09)."""
    return time.mktime((2026, 11, day, hour, minute, 0, 0, 0, -1))


@pytest.fixture
def clock():
    return Clock(at(14, 0))


def new_bot(clock, tmp_path, **prm):
    b = Bot(clock, tmp_path, **{"prm_handcards": "on", **prm})
    b.prm.register(CREATOR, "小美", "测试达人")
    return b


@pytest.fixture
def bot(clock, tmp_path):
    b = new_bot(clock, tmp_path)
    yield b
    b.prm.stop()
    b.relay.stop()


def deal_id(b):
    return b.prm.store.deal_of(b.prm.store.party_by_wxid(CREATOR)["id"])["id"]


def card_draft(text=LINE, made=None, **more):
    made = made if made is not None else hc.made_block(True, cards=[n for n, _ in CARDS], note="做了 2 张手卡",
                                                         items=[{"item_id": ITEM, "short": "测试巴拿马帽", "todo": []}])
    return draft(**{"message_kind": "material", "reply_draft": text, "slots": {}, "stage_proposal": "",
                    "intent": "要手卡", "reason": "kb 手卡那一节", "attachments_to_send": [n for n, _ in CARDS],
                    "attachments_named": [n for n, _ in CARDS], "handcards_made": made, **more})


def drafted(b, job, d=None, files=CARDS):
    for name, body in files:
        assert b.relay.outfile(job["id"], name, io.BytesIO(body).read, len(body))
    b.finish(job["id"], d or card_draft())
    return b.prm.store.packet_for_job(job["id"])


def asks(b, text="老师手卡发我一下"):
    b.prm.handle(b.msg(text))
    return b.worker_takes()


def test_her_draft_jobs_say_the_mac_may_make_cards_only_with_the_switch(bot, clock, tmp_path):
    assert asks(bot)["meta"][hc.OK_KEY] is True
    off = new_bot(clock, tmp_path / "off", prm_handcards="off")
    try:
        assert hc.OK_KEY not in asks(off)["meta"]
        assert off.prm.deal_action("handcards", {"deal_id": deal_id(off)}) == {
            "ok": False, "error": "手卡没打开（Windows 的 PRM_HANDCARDS），不能做手卡"}
    finally:
        off.prm.stop()
        off.relay.stop()


def test_a_draft_with_cards_waits_for_a_person_with_the_cards_and_the_note(bot):
    packet = drafted(bot, asks(bot))
    assert (packet["status"], packet["route"]) == ("pending", "review")
    assert [a["name"] for a in packet["attachments"]] == [n for n, _ in CARDS]
    assert packet["meta"]["handcards"] == {"ok": True, "count": 2, "note": "做了 2 张手卡", "problems": [],
                                           "console": False}
    assert bot.prm.store.deal(deal_id(bot))["attention"] == "手卡 2 张附在草稿里，要人看过再发"
    [card] = [p for p in snapshot(bot.prm.store, running=True)["pending"] if p["id"] == packet["id"]]
    assert card["handcards"]["ok"] is True and card["handcards"]["count"] == 2
    assert bot.prm.decide(packet["id"], "send")["ok"]
    bot.prm.send_approved()
    assert bot.texts == [(CREATOR, LINE)] and len(bot.files) == 2


@pytest.mark.parametrize("how", ["gate", "night", "auto_mode", "opening_auto"])
def test_a_draft_with_cards_never_goes_out_by_itself(clock, tmp_path, how):
    """Its kind's gate at auto, the 下班时段, 自动模式, a 开场白 that goes by itself: none of them sends the cards."""
    prm = {"night": {"prm_night_auto_hours": HOURS}, "opening_auto": {"prm_opening_auto_min": 30}}.get(how, {})
    b = new_bot(clock, tmp_path, **prm)
    try:
        if how == "gate":
            b.prm.store.set_gate("material", "auto", clock.now)
        elif how == "night":
            clock.now = at(21, 30)
        elif how == "auto_mode":
            assert b.prm.set_auto_mode(True)["ok"]
        if how == "opening_auto":
            assert b.prm.ask_opening(deal_id(b))["ok"]
            job = b.worker_takes()
        else:
            job = asks(b)
        packet = drafted(b, job)
        assert (packet["status"], packet["route"]) == ("pending", "review")
        assert "auto_at" not in packet["meta"] and "night_held" not in packet["meta"]
        clock.now += 3 * 3600
        assert b.prm.send_due_openings() == 0
        b.prm.send_approved()
        assert b.files == [] and not any(t == LINE for _, t in b.texts)
        assert b.prm.store.packet(packet["id"])["status"] == "pending"
    finally:
        b.prm.stop()
        b.relay.stop()


def test_cards_not_made_are_never_attached_and_the_words_cannot_go_as_they_are(bot):
    """The Mac failed after one card went up: the draft carries none of them, says why, and only an edited text goes."""
    made = hc.made_block(False, cards=[CARDS[0][0]], problems=["手卡_02 没传上去：网络断了"])
    packet = drafted(bot, asks(bot), card_draft(made=made, attachments_to_send=[], attachments_named=[]),
                     files=CARDS[:1])
    assert packet["attachments"] == [] and packet["needs_attention"] and packet["status"] == "pending"
    assert packet["reason"].startswith("手卡没做成：手卡_02 没传上去：网络断了；草稿里没附手卡")
    assert packet["meta"]["handcards"]["ok"] is False
    assert bot.prm.decide(packet["id"], "send") == {
        "ok": False, "error": "手卡没做成，草稿里没附手卡：不能原样发，改了再发（别说手卡做好了），或者不发"}
    assert bot.prm.decide(packet["id"], "edit", LINE)["ok"] is False                 # the same words: refused too
    assert bot.prm.decide(packet["id"], "edit", "老师，手卡我晚点整理好发您～")["ok"]
    bot.prm.send_approved()
    assert bot.texts == [(CREATOR, "老师，手卡我晚点整理好发您～")] and bot.files == []


def test_a_good_verdict_whose_cards_did_not_all_arrive_carries_none(bot):
    packet = drafted(bot, asks(bot), files=CARDS[:1])
    assert packet["attachments"] == [] and packet["needs_attention"]
    assert packet["meta"]["handcards"]["problems"] == ["手卡只传上来 1 / 2 张"]
    assert "手卡没做成：手卡只传上来 1 / 2 张" in bot.prm.store.deal(deal_id(bot))["attention"]


def test_a_plain_draft_is_as_before(bot):
    job = asks(bot, "你好")
    bot.finish(job["id"], draft(message_kind="greeting", reply_draft="在的～", slots={}, stage_proposal=""))
    packet = bot.prm.store.packet_for_job(job["id"])
    assert "handcards" not in packet["meta"]
    assert [p["handcards"] for p in snapshot(bot.prm.store, running=True)["pending"]] == [None]


# ── 「做手卡」 in the console ─────────────────────────────────────────────

def test_the_click_asks_for_her_latest_sample_orders_products_and_its_draft_waits(bot, clock):
    verified_order(bot.prm)
    shown = snapshot(bot.prm.store, running=True, extra={"handcards": True})
    assert shown["handcards"] is True
    assert shown["parties"][0]["deal"]["card_items"] == {"order": JST_ORDER, "at": clock.now, "shop": "dy",
                                                        "items": [{"item_id": DY_ITEM, "skus": 1}]}
    answer = bot.prm.deal_action("handcards", {"deal_id": deal_id(bot)})
    assert answer["ok"] is True
    job = bot.worker_takes()
    assert job["id"] == answer["job"] and job["text"] == ""
    assert job["meta"]["handcards"] == {"by": "console", "items": [{"item_id": DY_ITEM, "sku_ids": [DY_SKU]}],
                                       "order": JST_ORDER}
    assert job["meta"][hc.OK_KEY] is True and "night" not in job["meta"]
    names = [e["name"] for e in shown_events(bot)]
    assert f"派做手卡的任务 #{job['id']}（运营点的，1 款）" in names
    packet = drafted(bot, job)
    assert packet["status"] == "pending" and packet["meta"]["handcards"]["console"] is True
    assert bot.prm.store.deal(deal_id(bot))["attention"] == "手卡 2 张附在草稿里，要人看过再发"
    clock.now += 3600
    assert bot.prm.send_holding_replies() == 0 and bot.texts == []               # she said nothing: no 「稍等」


def shown_events(b):
    return snapshot(b.prm.store, running=True)["parties"][0]["deal"]["events"]


def test_ticked_products_and_none_at_all(bot):
    verified_order(bot.prm)
    assert bot.prm.deal_action("handcards", {"deal_id": deal_id(bot), "items": [ITEM, DY_ITEM, ITEM]})["ok"]
    job = bot.worker_takes()
    assert job["meta"]["handcards"]["items"] == [{"item_id": ITEM, "sku_ids": []},
                                                 {"item_id": DY_ITEM, "sku_ids": [DY_SKU]}]
    bot.finish(job["id"], draft(message_kind="material", reply_draft="", slots={}, stage_proposal="", reason="没写"))
    assert bot.prm.deal_action("handcards", {"deal_id": deal_id(bot), "items": []})["ok"]
    job = bot.worker_takes()
    assert job["meta"]["handcards"] == {"by": "console", "items": []}
    assert f"派做手卡的任务 #{job['id']}（运营点的，按案卷里她选的款）" in [e["name"] for e in shown_events(bot)]


@pytest.mark.parametrize("items, error", [
    ("3000000000000000901", "要做手卡的商品要写成商品 ID 的列表"),
    (["abc"], "「abc」不是抖店的商品 ID（一串数字）"),
    ([True], "「True」不是抖店的商品 ID（一串数字）"),
    ([str(3000000000000000001 + n) for n in range(10)], "一次最多做 9 款（一份草稿最多 9 个文件）"),
])
def test_ticked_products_that_are_not_ids_are_refused(bot, items, error):
    assert bot.prm.deal_action("handcards", {"deal_id": deal_id(bot), "items": items}) == {"ok": False, "error": error}
    assert card_ids(None) == (None, "") and card_ids([1234567, "1234567"]) == (["1234567"], "")


def test_why_no_cards_are_asked_for_now(bot, clock):
    deal = deal_id(bot)
    for odd in (999, "x", None):
        assert bot.prm.deal_action("handcards", {"deal_id": odd}) == {"ok": False, "error": "没有找到这个合作单"}
    bot.prm.handle(bot.msg("在吗"))
    assert bot.prm.deal_action("handcards", {"deal_id": deal}) == {
        "ok": False, "error": "她的草稿正在写，写好会出现在「待审批」里"}
    job = bot.worker_takes()
    bot.finish(job["id"], draft(message_kind="greeting", reply_draft="在的～", slots={}, stage_proposal=""))
    assert bot.prm.deal_action("handcards", {"deal_id": deal}) == {
        "ok": False, "error": "「待审批」里已经有她的草稿：先处理那一份"}
    assert bot.prm.decide(bot.prm.store.packet_for_job(job["id"])["id"], "drop")["ok"]
    assert bot.prm.deal_action("mode", {"deal_id": deal, "mode": "human"})["ok"]
    assert bot.prm.deal_action("handcards", {"deal_id": deal}) == {
        "ok": False, "error": "现在不在副驾驶模式（人工接管或暂停），不做手卡"}
    assert bot.prm.deal_action("mode", {"deal_id": deal, "mode": "copilot"})["ok"]
    bot.prm.set_active(bot.prm.store.party_by_wxid(CREATOR)["id"], False)
    assert bot.prm.deal_action("handcards", {"deal_id": deal}) == {
        "ok": False, "error": "这位达人已经停用（不在达人名单里），不做手卡"}
    assert apply_deal_action(bot.prm.store, "handcards", {"deal_id": deal}, now=clock.now, takeover_sec=7200) == {
        "ok": False, "error": "机器人没有运行（或没打开达人副驾驶），现在不能做手卡"}


def test_a_click_the_mac_made_nothing_of_says_why_on_the_deal(bot):
    """The Mac could not tell which products (no draft): the deal says why.  It wrote words but asked for no cards:
    those cannot go as they are.  The Mac cannot make cards at all (no handcards.json): a notice says how to go on,
    until the next click."""
    assert bot.prm.deal_action("handcards", {"deal_id": deal_id(bot), "items": []})["ok"]
    job = bot.worker_takes()
    bot.finish(job["id"], draft(message_kind="material", reply_draft="", slots={}, stage_proposal="",
                                reason="案卷里看不出她选了哪几款"))
    assert bot.prm.store.packet_for_job(job["id"])["status"] == "no_reply"
    assert bot.prm.store.deal(deal_id(bot))["attention"] == "「做手卡」这次没做：案卷里看不出她选了哪几款"
    assert bot.prm.deal_action("handcards", {"deal_id": deal_id(bot), "items": [ITEM]})["ok"]
    job = bot.worker_takes()
    bot.finish(job["id"], draft(message_kind="material", reply_draft=LINE, slots={}, stage_proposal=""))
    packet = bot.prm.store.packet_for_job(job["id"])
    assert packet["status"] == "pending" and packet["meta"]["handcards"]["ok"] is False
    assert bot.prm.decide(packet["id"], "send")["ok"] is False
    assert bot.prm.decide(packet["id"], "drop")["ok"]
    assert bot.prm.deal_action("handcards", {"deal_id": deal_id(bot), "items": [ITEM]})["ok"]
    failed = bot.worker_takes()
    bot.relay.finish(failed["id"], {"ok": False, "text": "", "error": "这台 Mac 做不了手卡：这台 Mac 没有 handcards.json"})
    bot.relay.tick()
    attention = bot.prm.store.deal(deal_id(bot))["attention"]
    assert "做手卡的草稿没写成：这台 Mac 做不了手卡" in attention and "再点一次「做手卡」" in attention
    assert bot.prm.deal_action("handcards", {"deal_id": deal_id(bot), "items": [ITEM]})["ok"]
    assert not any(n["key"] == f"failed:{failed['id']}" for n in bot.prm.store.deal(deal_id(bot))["notices"])


def test_her_message_outdates_the_waiting_cards_and_her_job_hears_they_never_went(bot, clock):
    assert bot.prm.deal_action("handcards", {"deal_id": deal_id(bot), "items": [ITEM]})["ok"]
    packet = drafted(bot, bot.worker_takes())
    clock.now += 600
    bot.prm.handle(bot.msg("老师还有别的颜色吗"))
    assert bot.prm.store.packet(packet["id"])["status"] == "superseded"
    job = bot.worker_takes()
    assert "handcards" not in job["meta"] and [s["text"] for s in job["meta"]["sent"] if s["what"] == "failed"] == [LINE]


def test_the_note_constant_goes_once(bot):
    """A 做手卡's deal line is the cards' own, not the click's note as well."""
    assert bot.prm.deal_action("handcards", {"deal_id": deal_id(bot), "items": [ITEM]})["ok"]
    drafted(bot, bot.worker_takes())
    assert HANDCARDS_NOTE not in bot.prm.store.deal(deal_id(bot))["attention"]


# ── the setting ──────────────────────────────────────────────────────────

def test_the_switch_defaults_off_and_is_checked():
    import os
    from types import SimpleNamespace
    from unittest.mock import patch

    from test_prm import config as prm_config
    from src.config import BotConfig, _validate_config, load_config
    from src.prm.service import PrmService
    from src.prm.store import PrmStore
    from src.relay.service import ClaudeRelay
    from src.relay.store import RelayStore
    from src.web import advanced_config as adv

    assert BotConfig().prm_handcards == "off"
    _validate_config({"prm_handcards": "on"})
    with pytest.raises(RuntimeError) as e:
        _validate_config({"prm_handcards": "maybe"})
    assert "PRM_HANDCARDS must be off or on" in str(e.value)
    with patch.dict(os.environ, {"PRM_HANDCARDS": "yes"}, clear=True):
        assert load_config().prm_handcards == "on"
    with patch.dict(os.environ, {}, clear=True):
        assert load_config().prm_handcards == "off"
    relay = ClaudeRelay(prm_config(), store=RelayStore(":memory:"))
    assert PrmService(SimpleNamespace(**vars(prm_config()), prm_handcards="on"), relay,
                      store=PrmStore(":memory:"))._handcards is True
    assert PrmService(prm_config(), relay, store=PrmStore(":memory:"))._handcards is False   # a config without it
    field = adv.FIELDS["PRM_HANDCARDS"]
    assert (field["type"], field["default"]) == ("onoff", "off")
    keys = [f["key"] for g in adv.GROUPS for f in g["fields"]]
    assert keys.index("PRM_HANDCARDS") == keys.index("PRM_SEND_PACE") + 1
    assert adv.validate_updates({"PRM_HANDCARDS": "maybe"}, {}) and adv.validate_updates({"PRM_HANDCARDS": "on"}, {}) == {}


def test_a_platform_draft_whose_cards_failed_cannot_be_executed_as_it_is(bot):
    """批准并执行 has the same guard as 发送: the reply goes out after the platform's read-back, never claiming cards it
    does not carry."""
    from src.prm.service import CARDS_FAILED_TEXT

    deal = bot.prm.store.deal_of(bot.prm.store.party_by_wxid(CREATOR)["id"])
    action = {"action": "dy.designated_commission", "item_id": DY_ITEM, "creator_platform_id": "1234567890",
              "commission_rate": 20}
    packet_id = bot.prm.store.add_packet(
        deal_id=deal["id"], party_id=deal["party_id"], job_id=None, chat_id=CREATOR, kind="money",
        text="佣金开好啦，手卡也整理好啦～", attachments=[], intent="", slots={}, stage_proposal="", needs_attention=True,
        reason="", status="pending", tool="dy.designated_commission",
        platform={"action": action, "preview": {"verdict": "ready", "problems": []}},
        meta={"handcards": {"ok": False, "count": 0, "note": "", "problems": ["出图出错了"], "console": False}},
        now=bot.clock.now)
    assert bot.prm.decide(packet_id, "execute") == {"ok": False, "error": CARDS_FAILED_TEXT}
    assert bot.prm.decide(packet_id, "execute", "佣金开好啦～") != {"ok": False, "error": CARDS_FAILED_TEXT}
