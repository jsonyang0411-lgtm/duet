"""Make phone-width screenshots of every creator page with a fake catalogue.

Development tool (not needed on the server). Needs Playwright with chromium;
Pillow draws the placeholder hats (solid squares without it). Uses only a
temp directory and a server on 127.0.0.1.

    python sample_site/tools/make_screenshots.py OUT_DIR
"""

import http.client
import io
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sample_site.app import create_app, make_wsgi_server  # noqa: E402
from sample_site.catalog import image_key  # noqa: E402
from sample_site.config import Config  # noqa: E402

# (code, name, desc, kind, sized, [(colour name, rgb), ...]) - all invented.
STYLES = [
    ("T1001", "测试渔夫帽", "软顶可折叠，出门好收纳", "bucket", False,
     [("燕麦", (214, 196, 168)), ("黑色", (38, 36, 34)), ("雾蓝", (128, 152, 178))]),
    ("T1002", "测试棒球帽", "弯檐六片，后扣可调", "cap", True,
     [("藏青", (36, 48, 82)), ("卡其", (186, 160, 118)), ("奶白", (238, 232, 218)), ("墨绿", (46, 74, 58))]),
    ("T1003", "测试贝雷帽", "羊毛混纺，复古百搭", "beret", False,
     [("酒红", (128, 30, 44)), ("驼色", (176, 132, 86))]),
    ("T1004", "测试礼帽", "宽檐挺括，配丝带", "fedora", True,
     [("黑色", (34, 32, 31)), ("咖啡", (102, 70, 48)), ("灰色", (128, 126, 122))]),
    ("T1005", "测试针织帽", "加厚毛线，带毛球", "beanie", False,
     [("米白", (232, 226, 210)), ("焦糖", (184, 112, 52)), ("雾粉", (214, 168, 166))]),
    ("T1006", "测试草帽", "大檐遮阳，度假风", "straw", False,
     [("原色", (222, 194, 140)), ("浅棕", (176, 136, 86))]),
    ("T1007", "测试报童帽", "灯芯绒面料", "flat", True,
     [("棕色", (110, 76, 50)), ("灰色", (112, 112, 108)), ("黑色", (36, 34, 32))]),
    ("T1008", "测试牛仔帽", "卷边宽檐，西部风", "cowboy", False,
     [("沙色", (200, 170, 126)), ("黑色", (40, 36, 34)), ("白色", (238, 234, 226)), ("棕色", (122, 82, 50))]),
]


def _font(size):
    from PIL import ImageFont
    for path in ("/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/Helvetica.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    return ImageFont.load_default()


def _shade(rgb, f):
    return tuple(max(0, min(255, int(c * f))) for c in rgb)


def _band(rgb):
    light = sum(rgb) / 3 > 140
    return (60, 46, 36) if light else (214, 196, 160)


def draw_hat(kind, rgb, label):
    """A 600x600 JPEG of a simple hat drawing (bytes)."""
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        return _plain_png(rgb)
    s = 2
    W = 600 * s
    img = Image.new("RGB", (W, W), (246, 243, 238))
    bg = ImageDraw.Draw(img)
    for y in range(W):  # soft vertical gradient
        t = y / W
        bg.line([(0, y), (W, y)], fill=(int(248 - 10 * t), int(246 - 12 * t), int(242 - 16 * t)))
    shadow = Image.new("L", (W, W), 0)
    ImageDraw.Draw(shadow).ellipse([260, 880, 940, 960], fill=90)
    shadow = shadow.filter(ImageFilter.GaussianBlur(28))
    img.paste((150, 140, 128), None, shadow)
    d = ImageDraw.Draw(img)
    main = rgb
    dark = _shade(rgb, 0.78)
    darker = _shade(rgb, 0.62)
    light = _shade(rgb, 1.12)
    band = _band(rgb)

    if kind == "bucket":
        d.ellipse([230, 640, 970, 880], fill=dark)
        d.ellipse([270, 650, 930, 850], fill=main)
        d.polygon([(420, 440), (780, 440), (850, 760), (350, 760)], fill=main)
        d.ellipse([420, 380, 780, 500], fill=light)
        d.polygon([(362, 690), (838, 690), (850, 760), (350, 760)], fill=band)
        for i in range(6):
            d.arc([250 + i * 4, 660 + i * 4, 950 - i * 4, 870 - i * 4], 10, 170, fill=darker, width=2)
    elif kind == "cap":
        d.ellipse([600, 600, 1040, 700], fill=darker)
        d.chord([340, 380, 860, 920], 180, 360, fill=main)
        d.chord([400, 420, 800, 900], 200, 340, fill=light)
        d.chord([340, 380, 860, 920], 180, 360, outline=dark, width=4)
        d.line([(600, 384), (600, 650)], fill=dark, width=4)
        d.arc([440, 384, 760, 920], 200, 340, fill=dark, width=4)
        d.ellipse([580, 360, 620, 400], fill=dark)
        d.rectangle([344, 632, 856, 652], fill=dark)
    elif kind == "beret":
        d.ellipse([260, 470, 940, 770], fill=main)
        d.ellipse([300, 480, 900, 700], fill=light)
        d.ellipse([400, 690, 800, 790], fill=dark)
        d.rounded_rectangle([586, 420, 614, 500], radius=12, fill=darker)
    elif kind == "fedora":
        d.ellipse([190, 650, 1010, 880], fill=dark)
        d.ellipse([230, 660, 970, 850], fill=main)
        d.polygon([(430, 430), (770, 430), (810, 760), (390, 760)], fill=main)
        d.ellipse([430, 390, 770, 470], fill=light)
        d.line([(600, 400), (600, 470)], fill=darker, width=10)
        d.polygon([(400, 660), (800, 660), (812, 740), (388, 740)], fill=band)
        d.polygon([(760, 664), (820, 640), (812, 690)], fill=band)
    elif kind == "beanie":
        d.chord([370, 330, 830, 1000], 180, 360, fill=main)
        d.rectangle([370, 660, 830, 665], fill=main)
        d.rounded_rectangle([350, 640, 850, 820], radius=40, fill=dark)
        for x in range(380, 840, 34):
            d.line([(x, 650), (x, 810)], fill=darker, width=6)
        d.ellipse([530, 230, 670, 370], fill=light)
        d.ellipse([548, 248, 652, 352], fill=main)
    elif kind == "straw":
        d.ellipse([130, 590, 1070, 910], fill=dark)
        d.ellipse([170, 600, 1030, 880], fill=main)
        for i in range(0, 360, 12):
            d.arc([180, 610, 1020, 870], i, i + 4, fill=darker, width=3)
        d.chord([410, 410, 790, 1000], 180, 360, fill=light)
        d.polygon([(412, 650), (788, 650), (792, 720), (408, 720)], fill=(120, 40, 36))
    elif kind == "flat":
        d.ellipse([320, 470, 880, 770], fill=main)
        d.ellipse([360, 480, 860, 690], fill=light)
        d.pieslice([520, 660, 940, 820], 180, 360, fill=darker)
        for i in range(8):
            d.arc([340 + i * 6, 490 + i * 6, 860 - i * 6, 760 - i * 6], 200, 340, fill=dark, width=2)
    else:  # cowboy
        d.polygon([(180, 640), (330, 720), (600, 760), (870, 720), (1020, 640), (980, 760),
                   (820, 840), (600, 860), (380, 840), (220, 760)], fill=dark)
        d.polygon([(220, 660), (340, 730), (600, 770), (860, 730), (980, 660), (950, 750),
                   (810, 820), (600, 840), (390, 820), (250, 750)], fill=main)
        d.polygon([(440, 440), (760, 440), (800, 760), (400, 760)], fill=main)
        d.polygon([(440, 440), (600, 500), (760, 440), (740, 410), (600, 450), (460, 410)], fill=light)
        d.polygon([(404, 690), (796, 690), (802, 750), (398, 750)], fill=band)

    font = _font(30)
    d.text((48, W - 82), label, fill=(150, 142, 132), font=font)
    img = img.resize((600, 600), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=86, optimize=True, progressive=True)
    return out.getvalue()


def _plain_png(rgb):
    import struct
    import zlib
    raw = b"".join(b"\x00" + bytes(rgb) * 600 for _ in range(600))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 600, 600, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


class Api(object):
    def __init__(self, port, token):
        self.port = port
        self.token = token

    def call(self, method, path, payload=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else b"")
        conn.request(method, "/admin/api/" + path, body=body,
                     headers={"Authorization": "Bearer " + self.token,
                              "Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert data.get("ok"), data
        return data


def build_catalogue(api):
    styles = []
    for code, name, desc, kind, sized, colours in STYLES:
        cols = []
        for ci, (cname, rgb) in enumerate(colours):
            data = draw_hat(kind, rgb, "ACE7CN  %s" % code)
            key = image_key(data)
            api.call("PUT", "images/" + key, raw=data)
            letter = "ABCDEFGH"[ci]
            sizes = ([{"label": "M", "sku": "%s%s-M" % (code, letter)},
                      {"label": "L", "sku": "%s%s-L" % (code, letter)}] if sized
                     else [{"label": "均码", "sku": "%s%s-F" % (code, letter)}])
            cols.append({"name": cname, "image": key, "sizes": sizes})
        styles.append({"code": code, "name": name, "desc": desc, "colours": cols})
    return api.call("PUT", "catalog", {"styles": styles})


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    out_dir = os.path.abspath(argv[0] if argv else "screenshots")
    os.makedirs(out_dir, exist_ok=True)
    from playwright.sync_api import sync_playwright

    tmp = tempfile.mkdtemp(prefix="sample-site-shots-")
    token = secrets.token_urlsafe(32)
    app = create_app(Config(admin_token=token, db_path=os.path.join(tmp, "site.db")))
    server = make_wsgi_server(app, "127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    try:
        api = Api(port, token)
        build_catalogue(api)
        personal = api.call("POST", "links", {"ref": "demo:1", "max_items": 3})
        general = api.call("POST", "links", {"ref": "demo:2", "kind": "general"})
        dead = api.call("POST", "links", {"ref": "demo:3"})
        api.call("POST", "links/%d/revoke" % dead["id"])
        s_url = base + "/s/" + personal["token"]
        g_url = base + "/g/" + general["token"]

        def shot(page, name, full=False):
            page.wait_for_timeout(250)
            if full:
                # Grow the viewport to the page so the fixed bottom bar lands at the bottom.
                height = page.evaluate("document.documentElement.scrollHeight")
                page.set_viewport_size({"width": 390, "height": max(844, height)})
                page.wait_for_timeout(300)
                page.screenshot(path=os.path.join(out_dir, name))
                page.set_viewport_size({"width": 390, "height": 844})
            else:
                page.screenshot(path=os.path.join(out_dir, name))
            print("saved", name)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                      is_mobile=True, has_touch=True, locale="zh-CN")
            page = ctx.new_page()
            page.goto(s_url)
            page.wait_for_load_state("networkidle")
            shot(page, "01-pick.png")
            shot(page, "10-pick-full.png", full=True)

            cards = page.locator("[data-card]")
            first = cards.nth(0)
            first.locator("[data-colours] .chip").nth(1).click()
            first.locator("[data-pick]").click()
            second = cards.nth(1)
            second.locator("[data-colours] .chip").nth(0).click()
            second.locator("[data-sizes] .chip").nth(0).click()
            second.locator("[data-pick]").click()
            page.evaluate("window.scrollTo(0, 220)")
            shot(page, "02-picked.png")

            page.click("[data-next]")
            page.fill("#f-name", "小美")
            page.fill("#f-phone", "13800000000")
            page.select_option("#f-province", "浙江省")
            page.fill("#f-city", "杭州市")
            page.fill("#f-district", "西湖区")
            page.fill("#f-address", "测试路 1 号 2 幢 301 室")
            shot(page, "03-details.png", full=True)

            page.click("[data-submit]")
            page.wait_for_load_state("networkidle")
            shot(page, "04-confirm.png", full=True)

            page.click("button[value=submit]")
            page.wait_for_load_state("networkidle")
            shot(page, "05-thanks.png", full=True)

            page2 = ctx.new_page()
            page2.goto(s_url)
            page2.wait_for_load_state("networkidle")
            shot(page2, "06-return-editable.png", full=True)
            page2.click(".actions a.btn")
            page2.wait_for_load_state("networkidle")
            shot(page2, "06b-edit.png")
            page2.click("[data-next]")
            page2.wait_for_selector("#details", state="visible")
            shot(page2, "06c-edit-details.png", full=True)

            api.call("POST", "submissions/pull", {"owner": "main"})
            page2.goto(s_url)
            page2.wait_for_load_state("networkidle")
            shot(page2, "07-locked.png", full=True)

            page2.goto(base + "/s/" + dead["token"])
            shot(page2, "08-expired.png")

            g = ctx.new_page()
            g.goto(g_url)
            g.wait_for_load_state("networkidle")
            shot(g, "09-general-link.png")
            g.locator("[data-card]").nth(2).locator("[data-pick]").click()
            g.click("[data-next]")
            g.fill("#paste", "小美 13800000000 上海市浦东新区测试路 8 号 5 楼")
            g.click("[data-paste]")
            g.fill("#f-account", "xiaomei.test")
            shot(g, "09b-general-details.png", full=True)
            g.click("[data-submit]")
            g.wait_for_load_state("networkidle")
            g.click("button[value=submit]")
            g.wait_for_load_state("networkidle")
            shot(g, "09c-general-done.png", full=True)
            g.goto(g_url)  # the same phone opens the general link again
            g.wait_for_load_state("networkidle")
            shot(g, "09d-general-again.png")
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        app.close()
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
