"""选款网站（仓库里的 ``sample_site/``，老板部署在公网）在 Mac 这一侧的全部代码（2026-11-13）。

网站够不到这台 Mac（Mac 在 NAT 后面，只有 Tailscale 能到），所以只有 Mac 去找网站：

- ``SiteClient``：网站的管理接口（``/admin/api/*``，Bearer 密钥；只走 https，本机地址除外）。
- 目录（``build_catalog`` / ``upload_catalog``，命令 ``catalog``）：按规则挑款（``RANKERS``：聚水潭近 30 天热卖、手写的
  款号单、淘宝生意参谋——最后一个现在读不到，见 ``rank_tb_sycm``），每款用聚水潭 ``goods-detail`` 读颜色、尺码、库存，
  真实库存 = 库存 − 虚拟库存，不够 ``min_stock`` 的颜色 / 尺码不上架；每个编码要在抖店商品目录（kb/products.json）里
  有对应的商品和 SKU（寄样单按店铺商品建），对不上的不上架。图片下载后缩小再传到网站，网站不外链。
  传成功以后把「编码 → 抖店商品 / SKU」写进 ``catalog-map.json``（仓库外，0600）：建单时按它换。
- 链接（``make_link`` / ``link_draft``）：给一位达人建专属链接，写成一句现成的话（``LINK_TEXT``，不经过模型）。
- 取单（``pull``）：先确认（ack）机器人已经存好的，再把新提交锁住取回来；没确认的每次都再给，所以不丢也不重。
- 寄样单（``order_action`` / ``pick_sheet``）：把她选的编码换成 ``jst.sample_order`` 的参数（抖店商品 ID + SKU ID、
  她自己填的收货信息）。**这些单不用对图**：帽子是她自己在我们的目录里点的，编码就是我们的编码，没有「从截图猜是哪顶」
  这一步；给运营看的是一张「选款单」（她选的每一顶的图和名字）。

设置在 ``~/.claude-relay/config.json`` 的 ``sample_site``（见 ``config.example.json``）；密钥放在单独的文件里
（``token_file``，权限 600），不进仓库、不进日志。这里从不打印收货人、电话、地址、抖音号。

用法（Mac 自带的 python3 就行）::

    python3 tools/claude_relay/sample_site.py status
    python3 tools/claude_relay/sample_site.py catalog --dry-run     # 只读聚水潭、只在本机出结果，不传
    python3 tools/claude_relay/sample_site.py catalog               # 生成并传到网站
    python3 tools/claude_relay/sample_site.py link --general        # 老板发群里用的通用链接
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("claude_relay.sample_site")

KEY = "sample_site"                     # the block of config.json
HOME_DIR = Path(os.environ.get("CLAUDE_RELAY_HOME", Path.home() / ".claude-relay"))
STATE_DIR = "platform/sample-site"      # under HOME_DIR: catalog-map.json, the pictures' cache
MAP_FILE = "catalog-map.json"
PLATFORMS_FILE = Path(__file__).resolve().parents[2] / "src" / "prm" / "platforms.py"

_OWNER = re.compile(r"^[0-9A-Za-z_-]{1,32}$")
_CODE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{2,63}$")          # a style code / an item code of 聚水潭 (the site's too)
_REF = re.compile(r"^[0-9A-Za-z:_-]{1,64}$")
_ACCOUNT = re.compile(r"^[0-9A-Za-z_.][0-9A-Za-z_.-]{1,39}$")    # an 抖音号 as the site takes it
_KEY = re.compile(r"^[0-9a-f]{32}$")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
PHONE_TOKEN = re.compile(r"^〈手机\d+〉$")

# What a creator reads (kb's wording rules: 老师 / 您, no 送 / 免费 / 包邮, no count promised, no dates).
LINK_TEXT = "老师，我们把在售的款式放在这个页面了，您看看喜欢哪几款，直接在上面选就可以：\n{url}"
ORDER_REPLY = "老师，您选的款我们收到啦，已经在安排了～"
SHEET_PREFIX = "选款单-"
SHEET_WHY = "她在选款网站上自己选的款（每一顶的图和名字）；只给运营看，不发给达人"
NO_PHOTO_CHECK = "她在选款网站上自己点的款，编码是我们目录里的：不用对图"

IMAGE_URL = ("https://newmagic.oss-accelerate.aliyuncs.com/magic/ACE7CN/{style}/sku_squares/"
             "square_{style}_{colour}.jpg")
IMAGE_MAX_BYTES = 400 * 1024            # the site's own cap
IMAGE_SOURCE_MAX = 12 * 1024 * 1024     # what is downloaded at most
IMAGE_EDGE = 640                        # the longer edge after shrinking
JST_GAP_SEC = 3.0                       # between two read-only 聚水潭 calls
MAX_STYLES = 60

DEFAULTS = {
    "url": "", "token_file": "", "token": "", "owner": "main", "timeout_sec": 20,
    "max_items": 3, "expires_days": 14, "link_text": LINK_TEXT, "order_reply": ORDER_REPLY,
    "audit": True, "shop": "dy", "logistics": "",
    "catalog": {"source": "jst_hot30", "top_n": 20, "pinned": [], "excluded": [], "styles": [], "names": {},
                "descs": {}, "min_stock": 1, "image_url": IMAGE_URL, "image_hosts": [], "prefer_items": []},
}


class SiteError(Exception):
    """Something the site (or this Mac's settings for it) refused; ``verdict`` says which kind: ``unconfigured``,
    ``unauthorized``, ``unreachable``, ``refused`` (a 4xx with the site's own message), ``error``."""

    def __init__(self, verdict: str, message: str):
        super().__init__(message)
        self.verdict = verdict


# ── settings ────────────────────────────────────────────────────────────────

def settings_of(config: dict) -> dict:
    """The ``sample_site`` block of config.json with its defaults, checked (``SiteError("unconfigured")`` says what is
    wrong, in words an operator can act on).  The token is read later (``read_token``): it is never kept here."""
    block = (config or {}).get(KEY)
    if not isinstance(block, dict) or not str(block.get("url") or "").strip():
        raise SiteError("unconfigured", "这台 Mac 还没配选款网站：在 ~/.claude-relay/config.json 里加 sample_site"
                                        "（url、token_file），见 tools/claude_relay/config.example.json")
    out = {**DEFAULTS, **{k: v for k, v in block.items() if k in DEFAULTS and k != "catalog"}}
    catalog = block.get("catalog") if isinstance(block.get("catalog"), dict) else {}
    out["catalog"] = {**DEFAULTS["catalog"], **{k: v for k, v in catalog.items() if k in DEFAULTS["catalog"]}}
    out["url"] = str(out["url"]).strip().rstrip("/")
    parts = urllib.parse.urlsplit(out["url"])
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise SiteError("unconfigured", "sample_site.url 要是网站的地址，比如 https://xuan.example.com")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK:
        raise SiteError("unconfigured", "sample_site.url 要用 https：管理密钥和收货信息不走明文（只有本机地址可以用 http）")
    if not _OWNER.match(str(out["owner"])):
        raise SiteError("unconfigured", "sample_site.owner 只能是字母、数字、_、-，最多 32 个")
    for key, low, high in (("max_items", 1, 10), ("expires_days", 1, 90), ("timeout_sec", 3, 120)):
        value = out[key]
        if isinstance(value, bool) or not isinstance(value, int) or not (low <= value <= high):
            raise SiteError("unconfigured", f"sample_site.{key} 要是 {low}–{high} 的整数")
    for key in ("link_text", "order_reply"):
        if not isinstance(out[key], str) or not out[key].strip() or len(out[key]) > 400:
            raise SiteError("unconfigured", f"sample_site.{key} 要是一句话（最多 400 个字）")
    if "{url}" not in out["link_text"]:
        raise SiteError("unconfigured", "sample_site.link_text 里要有 {url}（链接放在那儿）")
    if "{" in out["order_reply"] or "}" in out["order_reply"]:
        raise SiteError("unconfigured", "sample_site.order_reply 里不能有花括号（寄样单的回复没有占位符）")
    out["audit"] = out["audit"] is not False
    if out["shop"] != "dy":
        raise SiteError("unconfigured", "sample_site.shop 现在只支持 dy（抖店的商品）")
    return out


def read_token(conf: dict) -> str:
    """The site's admin token: the first line of ``token_file`` (kept 0600 outside the repository), else ``token``."""
    path = str(conf.get("token_file") or "").strip()
    token = ""
    if path:
        try:
            token = Path(path).expanduser().read_text(encoding="utf-8").strip().splitlines()[0].strip()
        except (OSError, IndexError):
            raise SiteError("unconfigured", f"读不到选款网站的管理密钥文件：{path}") from None
    token = token or str(conf.get("token") or "").strip()
    if len(token) < 32:
        raise SiteError("unconfigured", "选款网站的管理密钥没配（sample_site.token_file，文件里一行、至少 32 个字符）")
    return token


def state_dir(home: Optional[Path] = None) -> Path:
    folder = Path(home or HOME_DIR) / STATE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    return folder


# ── the site's admin API ────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect (security review 2026-11-13 #9): urllib would send the request again, its
    ``Authorization`` header with it, to wherever a 3xx points, another host over plain http too."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                                      # (urllib then raises the 3xx as an HTTPError)


_OPENER = urllib.request.build_opener(_NoRedirect)


def open_no_redirect(request, timeout):
    """``urlopen`` that answers a redirect with its HTTPError instead of following it."""
    return _OPENER.open(request, timeout=timeout)


class SiteClient:
    """The admin API of the site.  ``opener(request, timeout)`` is ``open_no_redirect`` by default (tests hand in
    their own): a redirect is an error (``refused``), never followed."""

    def __init__(self, url: str, token: str, timeout: float = 20, opener: Optional[Callable] = None):
        self.url, self._token, self.timeout = url.rstrip("/"), token, timeout
        self._open = opener or open_no_redirect

    def _call(self, method: str, path: str, body=None, raw: Optional[bytes] = None, content_type: str = ""):
        data, headers = None, {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if raw is not None:
            data, headers["Content-Type"] = raw, content_type or "application/octet-stream"
        elif body is not None:
            data, headers["Content-Type"] = json.dumps(body, ensure_ascii=False).encode("utf-8"), "application/json"
        request = urllib.request.Request(f"{self.url}/admin/api/{path}", data=data, method=method, headers=headers)
        try:
            with self._open(request, self.timeout) as answer:
                text = answer.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as e:
            code, said = e.code, ""
            try:
                value = json.loads(e.read(65536).decode("utf-8", "replace"))
                said = str(value.get("message") or value.get("error") or "")[:200] if isinstance(value, dict) else ""
            except Exception:
                pass
            if 300 <= code < 400:                         # (never followed: the key would go along)
                raise SiteError("refused", f"选款网站答了一个跳转（{code}）：sample_site.url 要写最终的 https 地址，"
                                           "机器人不跟着跳（管理密钥不能带去别处）") from None
            if code in (401, 403):
                raise SiteError("unauthorized", "选款网站不认这台 Mac 的管理密钥（换过密钥？问老板要新的）") from None
            verdict = "refused" if 400 <= code < 500 and code != 429 else "error"
            raise SiteError(verdict, f"选款网站答 {code}" + (f"：{said}" if said else "")) from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            raise SiteError("unreachable", f"连不上选款网站（{type(reason).__name__}）") from None
        try:
            value = json.loads(text.decode("utf-8"))
        except ValueError:
            raise SiteError("error", "选款网站的回答读不出来（不是 JSON：地址对吗？）") from None
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise SiteError("error", "选款网站的回答不对（没有 ok）")
        return value

    def status(self) -> dict:
        return self._call("GET", "status")

    def image_keys(self) -> set:
        return {k for k in self._call("GET", "images").get("keys") or [] if isinstance(k, str)}

    def put_image(self, key: str, data: bytes, content_type: str = "image/jpeg") -> dict:
        return self._call("PUT", f"images/{key}", raw=data, content_type=content_type)

    def put_catalog(self, doc: dict) -> dict:
        return self._call("PUT", "catalog", body=doc)

    def create_link(self, *, ref: str, owner: str, max_items: int, expires_days: int, ask_account: bool = False,
                    general: bool = False, max_uses: Optional[int] = None) -> dict:
        body = {"ref": ref, "owner": owner, "kind": "general" if general else "personal", "max_items": max_items,
                "expires_days": expires_days, "ask_account": bool(ask_account)}
        if general and max_uses:
            body["max_uses"] = max_uses
        value = self._call("POST", "links", body=body)
        if not str(value.get("url") or "").startswith(("https://", "http://")):
            raise SiteError("error", "选款网站建了链接，可回答里没有地址")
        return value

    def pull(self, owner: str) -> list:
        value = self._call("POST", "submissions/pull", body={"owner": owner}).get("submissions")
        return [s for s in value if isinstance(s, dict)] if isinstance(value, list) else []

    def ack(self, owner: str, ids: list) -> dict:
        return self._call("POST", "submissions/ack", body={"owner": owner, "ids": list(ids)})

    def picture(self, key: str) -> bytes:
        """A catalogue picture as the site serves it to everybody (``/img/<key>``; for the 选款单)."""
        if not _KEY.match(str(key or "")):
            raise SiteError("error", "图片的编号不对")
        request = urllib.request.Request(f"{self.url}/img/{key}", method="GET")
        try:
            with self._open(request, self.timeout) as answer:
                return answer.read(IMAGE_MAX_BYTES + 1)[:IMAGE_MAX_BYTES]
        except (urllib.error.URLError, OSError, TimeoutError):   # (a redirect too: an HTTPError)
            raise SiteError("unreachable", "选款网站的图片没取到") from None


def client_of(config: dict, opener: Optional[Callable] = None) -> tuple:
    """``(SiteClient, settings)`` of this Mac's config.json, or ``SiteError``."""
    conf = settings_of(config)
    return SiteClient(conf["url"], read_token(conf), timeout=conf["timeout_sec"], opener=opener), conf


# ── which styles (the ranking is pluggable) ─────────────────────────────────

def _codes(value) -> list:
    out = []
    for code in value if isinstance(value, list) else []:
        code = str(code).strip()
        if _CODE.match(code) and code not in out:
            out.append(code)
    return out


def rank_manual(conf: dict, jst: Callable) -> list:
    """The hand-kept list (``catalog.styles``: style codes in the order to show)."""
    return [{"code": code, "name": "", "sales": None} for code in _codes(conf.get("styles"))]


def rank_jst_hot30(conf: dict, jst: Callable) -> list:
    """聚水潭「近 30 天热卖」（`jst goods --sort hot30`，平台给前 50 款）：ERP 里所有店铺合起来的销量，不是只算淘宝店。"""
    data = jst(["goods", "--sort=hot30", "--page-size=50"])
    rows = [r for r in (data.get("rows") or []) if isinstance(r, dict) and _CODE.match(str(r.get("styleCode") or ""))]
    for index, row in enumerate(rows):
        row["_at"] = index

    def sold(row):
        value = row.get("sale30d")
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else -1

    rows.sort(key=lambda r: (-sold(r), r["_at"]))
    return [{"code": str(r["styleCode"]), "name": str(r.get("name") or "").strip(),
             "sales": sold(r) if sold(r) >= 0 else None} for r in rows]


def rank_tb_sycm(conf: dict, jst: Callable) -> list:
    """淘宝店自己的销量排行（生意参谋）。2026-11-13 查过：campaign-engineering 有 `oc sycm item-list`（商品排行，认日期），
    但这台 Mac 的调试 Chrome 里没有淘宝登录（`sycm doctor`：没有 unb Cookie），而且淘宝商品 → 聚水潭款号的对应还没人
    验证过（要看 `sycm item-search` 的「货号」是不是款号）。所以现在不读：换成 jst_hot30 或 manual。"""
    raise SiteError("unconfigured", "catalog.source = tb_sycm 现在读不到：这台 Mac 的调试 Chrome 没登录淘宝（生意参谋），"
                                    "淘宝商品和聚水潭款号的对应也还没接；先用 jst_hot30（聚水潭近 30 天热卖）或 manual（手写款号单）")


RANKERS = {"manual": rank_manual, "jst_hot30": rank_jst_hot30, "tb_sycm": rank_tb_sycm}


def choose_styles(ranked: list, conf: dict) -> list:
    """The styles to show, in order: the pinned ones first (``catalog.pinned``), then the first ``top_n`` of the
    ranking that are neither pinned nor excluded (``catalog.excluded``); at most MAX_STYLES."""
    pinned, excluded = _codes(conf.get("pinned")), set(_codes(conf.get("excluded")))
    names = {r["code"]: r for r in ranked}
    out = [names.get(code) or {"code": code, "name": "", "sales": None} for code in pinned if code not in excluded]
    top_n = conf.get("top_n")
    top_n = top_n if isinstance(top_n, int) and not isinstance(top_n, bool) and 0 <= top_n <= MAX_STYLES else 20
    rest = [r for r in ranked if r["code"] not in excluded and r["code"] not in pinned]
    return (out + rest[:top_n])[:MAX_STYLES]


# ── one style: colours, sizes, real stock, the shop's product ───────────────

def real_stock(sku: dict) -> Optional[int]:
    """What is really on the shelf: 库存 − 虚拟库存 (the platforms' ~1000 a colour is virtual); without both
    numbers, 可用库存; None when 聚水潭 gave nothing to go by."""
    stock, virtual, available = sku.get("stock"), sku.get("virtualQty"), sku.get("availableStock")

    def number(value):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    if number(stock) is not None:
        return int(number(stock) - (number(virtual) or 0))
    return int(number(available)) if number(available) is not None else None


_SPEC_HEAD = re.compile(r"^\s*(?:尺码|尺寸|规格)\s*[:：]\s*")


def size_label(spec) -> str:
    """A size as the page shows it, from 聚水潭's ``spec`` (「尺码:F(52-60cm)」, 「尺码:M(56-58cm)」): the label's head off,
    a lone F written 均码; nothing there: 均码."""
    text = _SPEC_HEAD.sub("", str(spec or "").strip())
    text = re.sub(r"^F(?=$|[\s(（])", "均码", text)
    return text[:60] or "均码"


def shop_index(products: Optional[dict], shop: str = "dy") -> dict:
    """聚水潭 item code -> the shop's products that sell it, from kb/products.json (the SKUs' ``code``):
    ``{code: [{"item_id", "sku_id", "title", "image_file"}]}``."""
    index: dict = {}
    part = ((products or {}).get("platforms") or {}).get(shop) or {}
    for item in part.get("items") or []:
        if not isinstance(item, dict):
            continue
        for sku in item.get("skus") or []:
            code = str((sku or {}).get("code") or "")
            if _CODE.match(code) and sku.get("id"):
                index.setdefault(code, []).append({"item_id": str(item.get("id") or ""), "sku_id": str(sku["id"]),
                                                   "title": str(item.get("title") or ""),
                                                   "image_file": str(item.get("image_file") or "")})
    return index


def pick_products(codes: list, index: dict, prefer=()) -> dict:
    """Which shop product each code of ONE style is ordered from (``{code: entry}``; a code no product sells is left
    out).  A code sold by several products goes to: one named in ``prefer`` (``catalog`` has none by default), else
    the product that sells the most codes of this style (so a style's hats come from one product where they can), not
    a 专属 one when there is another, the smaller product id last (always the same choice)."""
    cover: dict = {}
    for code in codes:
        for entry in index.get(code) or []:
            cover[entry["item_id"]] = cover.get(entry["item_id"], 0) + 1
    prefer = [str(p) for p in prefer or ()]
    out = {}
    for code in codes:
        entries = index.get(code) or []
        if not entries:
            continue
        out[code] = min(entries, key=lambda e: (e["item_id"] not in prefer, "专属" in e["title"],
                                                -cover.get(e["item_id"], 0), e["item_id"]))
    return out


def style_entry(style: dict, detail: dict, index: dict, conf: dict) -> tuple:
    """One style for the site and for the map: ``(entry | None, mapping, notes)``.  ``entry``: ``{"code", "name",
    "desc"?, "colours": [{"name", "sizes": [{"label", "sku"}]}]}`` (pictures are added by ``build_catalog``); only
    SKUs really in stock (``real_stock`` ≥ ``min_stock``) that a shop product sells.  ``mapping``: ``{sku: {"shop",
    "item_id", "sku_id", "style", "name", "colour", "size"}}``.  ``notes``: what was left out and why (for the
    operator; nothing of it reaches the site)."""
    code, notes = style["code"], []
    least = conf.get("min_stock")
    least = least if isinstance(least, int) and not isinstance(least, bool) and least >= 1 else 1
    skus = [s for s in (detail.get("skus") or []) if isinstance(s, dict) and _CODE.match(str(s.get("itemCode") or ""))]
    if not skus:
        return None, {}, [f"{code}：聚水潭里没读到这一款的 SKU"]
    stocked = []
    for sku in skus:
        have = real_stock(sku)
        if have is None or have < least:
            continue
        stocked.append(sku)
    chosen = pick_products([s["itemCode"] for s in stocked], index, prefer=conf.get("prefer_items") or ())
    unsold = [s["itemCode"] for s in stocked if s["itemCode"] not in chosen]
    if unsold:
        notes.append(f"{code}：{len(unsold)} 个编码抖店商品目录里没有（没上架，或者目录该同步了），没放上网站")
    names = conf.get("names") if isinstance(conf.get("names"), dict) else {}
    descs = conf.get("descs") if isinstance(conf.get("descs"), dict) else {}
    name = str(names.get(code) or style.get("name") or skus[0].get("name") or "").strip()[:60]
    if not name:
        return None, {}, [f"{code}：没有款名（在 catalog.names 里给它起一个）"]
    colours, mapping = [], {}
    for sku in stocked:
        item_code = sku["itemCode"]
        if item_code not in chosen:
            continue
        colour = str(sku.get("colour") or "").strip()[:60] or "默认"
        size = size_label(sku.get("spec"))
        entry = next((c for c in colours if c["name"] == colour), None)
        if entry is None:
            entry = {"name": colour, "sizes": []}
            colours.append(entry)
        if any(s["label"] == size for s in entry["sizes"]):
            notes.append(f"{code}：{colour} {size} 有两个编码，只放了第一个")
            continue
        entry["sizes"].append({"label": size, "sku": item_code})
        mapping[item_code] = {"shop": conf.get("shop") or "dy", "item_id": chosen[item_code]["item_id"],
                              "sku_id": chosen[item_code]["sku_id"], "style": code, "name": name, "colour": colour,
                              "size": size}
    if not colours:
        return None, {}, notes + [f"{code}（{name}）：现在没有能放上网站的颜色，这一款没放"]
    entry = {"code": code, "name": name, "colours": colours}
    desc = str(descs.get(code) or "").strip()[:120]
    if desc:
        entry["desc"] = desc
    return entry, mapping, notes


# ── pictures ────────────────────────────────────────────────────────────────

def image_hosts(conf: dict) -> tuple:
    """The hosts a picture may be downloaded from: the pattern's own and ``catalog.image_hosts``."""
    hosts = {str(h).lower() for h in conf.get("image_hosts") or [] if isinstance(h, str)}
    host = urllib.parse.urlsplit(str(conf.get("image_url") or IMAGE_URL)).hostname
    if host:
        hosts.add(host.lower())
    return tuple(sorted(hosts))


def picture_url(conf: dict, style: str, colour: str) -> str:
    pattern = str(conf.get("image_url") or IMAGE_URL)
    return pattern.format(style=urllib.parse.quote(style, safe=""), colour=urllib.parse.quote(colour, safe=""))


def fetch_picture(url: str, hosts: tuple, timeout: float = 20) -> bytes:
    """Download one picture (https, one of ``hosts``, at most IMAGE_SOURCE_MAX); b"" when it is not there."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in hosts:
        return b""
    try:
        # (no redirect followed here either: the picture comes from one of ``hosts`` or not at all)
        with open_no_redirect(urllib.request.Request(url, headers={"User-Agent": "sample-catalog"}), timeout) as answer:
            data = answer.read(IMAGE_SOURCE_MAX + 1)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return b""
    return data if 0 < len(data) <= IMAGE_SOURCE_MAX else b""


def _kind(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def shrink(data: bytes) -> tuple:
    """``(bytes, content type)`` small enough for the site (a JPEG of at most IMAGE_EDGE on its longer edge, made
    with Pillow when this Python has it), or ``(b"", "")``: not a picture, or too big and no Pillow to shrink it."""
    kind = _kind(data)
    if not kind:
        return b"", ""
    try:
        from PIL import Image
    except Exception:                                    # no Pillow: only a picture that is small already goes
        return (data, kind) if len(data) <= IMAGE_MAX_BYTES else (b"", "")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            image.thumbnail((IMAGE_EDGE, IMAGE_EDGE))
            for quality in (82, 70, 55):
                out = io.BytesIO()
                image.save(out, "JPEG", quality=quality, optimize=True)
                if out.tell() <= IMAGE_MAX_BYTES:
                    return out.getvalue(), "image/jpeg"
    except Exception:
        return b"", ""
    return b"", ""


def image_key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


# ── the catalogue ───────────────────────────────────────────────────────────

def build_catalog(conf: dict, *, jst: Callable, products: Optional[dict], fetch: Optional[Callable] = None,
                  local_image: Optional[Callable] = None, sleep: Callable = time.sleep, say: Callable = None) -> dict:
    """The catalogue by this Mac's rule: ``{"doc", "images": {key: (bytes, type)}, "map", "notes", "styles":
    [{"code", "name", "sales", "colours"}]}``.  ``jst(words) -> dict`` runs one read-only 聚水潭 command (``goods``,
    ``goods-detail``; ``JST_GAP_SEC`` apart); ``products``: kb/products.json; ``fetch(url) -> bytes`` downloads a
    picture; ``local_image(entry) -> bytes`` gives the shop product's main picture when a colour has none of its own.
    Nothing is written and nothing is sent to the site here."""
    say = say or (lambda text: None)
    catalog_conf = conf["catalog"]
    source = str(catalog_conf.get("source") or "")
    if source not in RANKERS:
        raise SiteError("unconfigured", f"catalog.source 只能是 {'、'.join(RANKERS)}")
    ranked = RANKERS[source](catalog_conf, jst)
    styles = choose_styles(ranked, catalog_conf)
    if not styles:
        raise SiteError("unconfigured", "一款都没挑出来：catalog.source 是 manual 时在 catalog.styles 里写款号，"
                                        "别的来源看排行有没有读到")
    say(f"按 {source} 挑了 {len(styles)} 款")
    index = shop_index(products, conf.get("shop") or "dy")
    hosts = image_hosts(catalog_conf)
    fetch = fetch or (lambda url: fetch_picture(url, hosts))
    doc, images, mapping, notes, shown = {"styles": []}, {}, {}, [], []
    for n, style in enumerate(styles):
        if n or source != "manual":
            sleep(JST_GAP_SEC)
        detail = jst(["goods-detail", f"--style-code={style['code']}"])
        entry, style_map, style_notes = style_entry(style, detail, index, {**catalog_conf, "shop": conf.get("shop")})
        notes += style_notes
        if entry is None:
            continue
        colours = []
        for colour in entry["colours"]:
            data, kind = shrink(fetch(picture_url(catalog_conf, style["code"], colour["name"])))
            if not data and local_image is not None:
                first = style_map[colour["sizes"][0]["sku"]]
                data, kind = shrink(local_image(next((e for e in index.get(colour["sizes"][0]["sku"]) or []
                                                      if e["item_id"] == first["item_id"]), {})) or b"")
            if not data:
                notes.append(f"{style['code']}（{entry['name']}）{colour['name']}：没有图，这个颜色没放")
                for size in colour["sizes"]:
                    style_map.pop(size["sku"], None)
                continue
            key = image_key(data)
            images[key] = (data, kind)
            colours.append({**colour, "image": key})
        if not colours:
            notes.append(f"{style['code']}（{entry['name']}）：一个有图的颜色都没有，这一款没放")
            continue
        doc["styles"].append({**entry, "colours": colours})
        mapping.update(style_map)
        shown.append({"code": style["code"], "name": entry["name"], "sales": style.get("sales"),
                      "colours": [f"{c['name']}（{'/'.join(s['label'] for s in c['sizes'])}）" for c in colours]})
        say(f"{len(doc['styles'])}. {entry['name']}：{len(colours)} 个颜色")
    if not doc["styles"]:
        raise SiteError("error", "挑出来的款都放不上网站（没有现货、抖店目录里没有、或者没有图）：" + "；".join(notes[:5]))
    return {"doc": doc, "images": images, "map": mapping, "notes": notes, "styles": shown}


def save_map(mapping: dict, home: Optional[Path] = None, version=None) -> Path:
    """Write catalog-map.json (0600, atomically): what an order's codes are changed by (``order_action``)."""
    path = state_dir(home) / MAP_FILE
    part = path.with_name(path.name + ".part")
    fd = os.open(str(part), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "saved_at": time.time(), "site_version": version, "skus": mapping}, f,
                  ensure_ascii=False, indent=1)
    os.replace(str(part), str(path))
    return path


def load_map(home: Optional[Path] = None) -> dict:
    """``{sku: {...}}`` of catalog-map.json; {} when there is none yet (no catalogue was uploaded from this Mac)."""
    try:
        value = json.loads((Path(home or HOME_DIR) / STATE_DIR / MAP_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    skus = value.get("skus") if isinstance(value, dict) else None
    return {k: v for k, v in skus.items() if isinstance(v, dict)} if isinstance(skus, dict) else {}


def upload_catalog(client: SiteClient, built: dict, home: Optional[Path] = None, keep: Optional[dict] = None) -> dict:
    """Send the pictures the site does not have yet, then the catalogue; then write the map (the codes of the map
    before, ``keep``, stay in it: a creator may have picked one a minute ago).  Returns the site's answer."""
    have = client.image_keys()
    sent = 0
    for key, (data, kind) in built["images"].items():
        if key not in have:
            client.put_image(key, data, kind)
            sent += 1
    answer = client.put_catalog(built["doc"])
    save_map({**(keep or {}), **built["map"]}, home, version=answer.get("version"))
    return {**answer, "images_sent": sent}


# ── a link for one creator, as a draft ──────────────────────────────────────

def party_ref(party_id) -> str:
    """How a link says whose it is (``ref``): the bot's party number, nothing of hers."""
    return f"p{int(party_id)}"


def link_draft(client: SiteClient, conf: dict, asked: dict, party_id: int) -> dict:
    """「发选款链接」 (the bot's ``meta.site_link``: ``{"by", "max"?, "ask_account"?}``): one link for this creator and
    the draft that sends it, a fixed sentence (``link_text``), never the model's: the link must go out exactly as the
    site gave it.  The draft's shape is the worker's PRM_SCHEMA (the bot's ``parse_draft``)."""
    wanted = asked.get("max")
    most = wanted if isinstance(wanted, int) and not isinstance(wanted, bool) and 1 <= wanted <= 10 else conf["max_items"]
    link = client.create_link(ref=party_ref(party_id), owner=conf["owner"], max_items=most,
                              expires_days=conf["expires_days"], ask_account=asked.get("ask_account") is True)
    text = conf["link_text"].replace("{url}", str(link["url"]))
    return {"intent": f"发选款链接（最多选 {most} 顶，{conf['expires_days']} 天内有效）", "slots": {}, "stage_proposal": "",
            "message_kind": "other", "reply_draft": text, "attachments_to_send": [], "attachments_named": [],
            "needs_attention": False, "reason": "",
            "site_link": {"id": link.get("id"), "max": most, "expires_at": link.get("expires_at")}}


# ── what came in ────────────────────────────────────────────────────────────

def _text(value, limit: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value if isinstance(value, (str, int)) else "")).strip()[:limit]


def clean_submission(raw: dict) -> Optional[dict]:
    """One submission as the bot gets it (None for one that is not one): only what an order needs."""
    if isinstance(raw.get("id"), bool) or not isinstance(raw.get("id"), int) or raw["id"] <= 0:
        return None
    items = []
    for item in raw.get("items") if isinstance(raw.get("items"), list) else []:
        if isinstance(item, dict) and _CODE.match(str(item.get("sku") or "")):
            items.append({"sku": str(item["sku"]), "name": _text(item.get("style_name"), 60),
                          "colour": _text(item.get("colour"), 60), "size": _text(item.get("size"), 60),
                          "image": str(item.get("image") or "") if _KEY.match(str(item.get("image") or "")) else ""})
    receiver = raw.get("receiver") if isinstance(raw.get("receiver"), dict) else {}
    account = str(raw.get("account") or "").strip()
    return {"id": raw["id"], "rev": raw.get("rev") if isinstance(raw.get("rev"), int) else 1,
            "ref": str(raw.get("ref") or "") if _REF.match(str(raw.get("ref") or "")) else "",
            "kind": "general" if raw.get("kind") == "general" else "personal",
            "account": account if _ACCOUNT.match(account) else "", "items": items[:10],
            "receiver": {key: _text(receiver.get(key), limit) for key, limit in (
                ("name", 30), ("phone", 20), ("province", 20), ("city", 20), ("district", 20), ("address", 120))},
            "purged": raw.get("purged") is True,
            "submitted_at": raw.get("submitted_at") if isinstance(raw.get("submitted_at"), (int, float)) else None}


def pull(client: SiteClient, conf: dict, acks: list) -> dict:
    """One round (the bot's ``meta.op = "site_pull"``): the bot's ``acks`` (the submissions it has stored: the site
    then keeps only masked remnants of their receivers) first, then every submission not acked yet (new ones are
    locked by this very call).  ``{"version", "op", "acked", "submissions", "stopped"}``."""
    ids = sorted({i for i in acks or [] if isinstance(i, int) and not isinstance(i, bool) and i > 0})[:500]
    acked = []
    if ids:
        answer = client.ack(conf["owner"], ids)
        acked = [i for i in answer.get("acked") or [] if isinstance(i, int)]
    rows = [clean_submission(raw) for raw in client.pull(conf["owner"])]
    return {"version": 1, "op": "site_pull", "acked": acked, "submissions": [r for r in rows if r is not None],
            "stopped": None}


def stopped_result(error: SiteError) -> dict:
    return {"version": 1, "op": "site_pull", "acked": [], "submissions": [],
            "stopped": {"verdict": error.verdict, "message": str(error)[:300]}}


# ── her choice as a sample order ────────────────────────────────────────────

def order_action(order: dict, mapping: dict, conf: dict) -> dict:
    """The ``jst.sample_order`` of one submission (the bot's ``meta.site_order``: ``{"id", "account", "items":
    [{"sku", …}], "receiver": {…, "phone": 〈手机N〉}}``): each code changed by the map to the shop's product and SKU
    id, one of each, the receiver as she typed it.  ``SiteError("refused")`` says what stands in the way (a code the
    map does not know: the catalogue was rebuilt on another Mac, say)."""
    account = str(order.get("account") or "").strip()
    if not _ACCOUNT.match(account):
        raise SiteError("refused", "不知道她的抖音号（链接没让她填，合作单上也没有）：在达人名单里补上抖音号，机器人下一轮会自己再试")
    items: dict = {}
    for item in order.get("items") or []:
        code = str((item or {}).get("sku") or "")
        entry = mapping.get(code)
        if entry is None or entry.get("shop") != conf.get("shop"):
            raise SiteError("refused", f"她选的编码 {code} 这台 Mac 的目录表里没有（目录是别的电脑传的？）："
                                       "在这台 Mac 上重新生成并上传一次目录，机器人下一轮会自己再试")
        skus = items.setdefault(str(entry["item_id"]), [])
        if not any(s["sku_id"] == str(entry["sku_id"]) for s in skus):
            skus.append({"sku_id": str(entry["sku_id"]), "qty": 1})
    if not items:
        raise SiteError("refused", "这份提交里没有选款")
    action = {"action": "jst.sample_order", "shop": conf.get("shop") or "dy", "creator_platform": "dy",
              "creator_account": account, "items": [{"item_id": k, "skus": v} for k, v in items.items()]}
    receiver = order.get("receiver") if isinstance(order.get("receiver"), dict) else {}
    action["receiver"] = {key: str(receiver.get(key) or "") for key in
                          ("name", "phone", "province", "city", "district", "address")}
    if conf.get("logistics"):
        action["logistics"] = str(conf["logistics"])
    return action


def order_draft(conf: dict, order: dict, block: dict, files: list) -> dict:
    """The answer of a site order's job: the fixed reply (``order_reply``, sent only after the order is made and
    verified), the preview, the 选款单, and the mark that says this order needs no photo check."""
    count = len(order.get("items") or [])
    return {"intent": f"选款网站：她自己选了 {count} 顶", "slots": {}, "stage_proposal": "", "message_kind": "other",
            "reply_draft": conf["order_reply"], "attachments_to_send": [], "attachments_named": [],
            "needs_attention": False, "reason": "", "platform": block,
            **({"operator_files": files} if files else {}),
            "site_order": {"v": 1, "id": order.get("id"), "photo_check": False, "why": NO_PHOTO_CHECK}}


def failed_block(action, why: str) -> dict:
    """A preview that could not be made (``order_action`` refused): shown on the card, never executable."""
    return {"version": 1, "action": None, "requested": None, "verdict": "invalid", "problems": [str(why)[:400]],
            "facts": [], "steps": [], "run": {}, "previewed_at": time.time()}


def pick_sheet(items: list, picture: Callable, path: Path) -> Optional[Path]:
    """The 选款单: one row a hat she picked (its picture as the site showed it to her, its name, colour, size), a JPEG
    at ``path``; None without Pillow or without any row.  Nothing of the receiver is on it."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    rows = []
    for item in items or []:
        try:
            with Image.open(io.BytesIO(picture(item.get("image")))) as image:
                thumb = image.convert("RGB")
                thumb.thumbnail((200, 200))
        except Exception:
            thumb = None
        rows.append((thumb, f"{item.get('name') or ''}", f"{item.get('colour') or ''} · {item.get('size') or ''}",
                     str(item.get("sku") or "")))
    if not rows:
        return None

    def font(size: int):
        for name in ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
                     "/System/Library/Fonts/Hiragino Sans GB.ttc"):
            try:
                return ImageFont.truetype(name, size)
            except Exception:
                continue
        return ImageFont.load_default()

    big, small = font(30), font(22)
    width, row_h, top = 760, 220, 70
    sheet = Image.new("RGB", (width, top + row_h * len(rows) + 20), "white")
    draw = ImageDraw.Draw(sheet)
    draw.text((20, 18), f"选款单：她在网站上自己选的 {len(rows)} 顶", fill="black", font=big)
    for n, (thumb, name, spec, code) in enumerate(rows):
        y = top + n * row_h
        draw.line([(20, y), (width - 20, y)], fill=(210, 210, 210))
        if thumb is not None:
            sheet.paste(thumb, (20, y + 10))
        draw.text((240, y + 30), name, fill="black", font=big)
        draw.text((240, y + 80), spec, fill=(60, 60, 60), font=small)
        draw.text((240, y + 120), f"编码 {code}", fill=(120, 120, 120), font=small)
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(str(path), "JPEG", quality=85)
    return path


# ── the command line ────────────────────────────────────────────────────────

def _load_platforms():
    spec = importlib.util.spec_from_file_location("webot_prm_platforms", str(PLATFORMS_FILE))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def jst_runner(platforms, cfg: dict, say: Callable = None) -> Callable:
    """``jst(words) -> dict``: one read-only 聚水潭 command (never ``--execute``) under the Mac's one-CLI-at-a-time lock,
    with ``--expect-uid`` when this Mac names its sub-account."""
    if "jst" not in cfg.get("dirs", {}):
        raise SiteError("unconfigured", cfg.get("missing", {}).get("jst") or "这台 Mac 没配聚水潭的命令行（platforms.jst）")
    who = [f"--expect-uid={cfg['jst_expect_uid']}"] if cfg.get("jst_expect_uid") else []

    def jst(words: list) -> dict:
        if any(str(w).startswith("--execute") for w in words) or words[0] not in ("goods", "goods-detail"):
            raise SiteError("error", "目录只跑聚水潭的只读命令")
        with platforms.CliLock(HOME_DIR / "platform" / "cli.lock"):
            run = platforms.run_cli([cfg["node"], platforms.CLI_FILES["jst"], *words, *who], cfg["dirs"]["jst"],
                                    timeout=120, env=platforms.cli_env(cfg), interruptible=True)
        data = platforms.parse_json(run.stdout)
        if say:
            say(f"jst {words[0]} -> 退出码 {run.exit_code}")
        if run.exit_code != 0 or not isinstance(data, dict):
            raise SiteError("error", f"聚水潭 {words[0]} 没读成（退出码 {run.exit_code}："
                                     f"{platforms.EXIT_CODES.get(run.exit_code, '')}）")
        return data
    return jst


def _local_image(workspace: Optional[Path]) -> Callable:
    def read(entry: dict) -> bytes:
        rel = str((entry or {}).get("image_file") or "")
        if not workspace or not rel or ".." in rel:
            return b""
        try:
            return (workspace / rel).read_bytes()
        except OSError:
            return b""
    return read


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="选款网站：目录、链接、状态（Mac 这一侧）")
    parser.add_argument("--config", default=str(HOME_DIR / "config.json"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="网站连不连得上、目录是哪一版、有几份没取的提交")
    cat = sub.add_parser("catalog", help="按 sample_site.catalog 的规则生成目录并传到网站（只读聚水潭）")
    cat.add_argument("--dry-run", action="store_true", help="只生成、不传（也不写目录表）")
    cat.add_argument("--out", help="把生成的目录（不含图片）另存一份 JSON")
    link = sub.add_parser("link", help="手动建一个链接（通用链接给老板发群里；专属链接平时由机器人建）")
    link.add_argument("--general", action="store_true", help="通用链接：达人自己填抖音号")
    link.add_argument("--ref", default="", help="专属链接是谁的（机器人里的达人编号，如 p12）")
    link.add_argument("--max", type=int, default=0, help="最多选几顶（默认照设置）")
    link.add_argument("--days", type=int, default=0, help="几天内有效（默认照设置）")
    args = parser.parse_args(argv)
    try:
        config = json.loads(Path(args.config).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"读不了设置 {args.config}：{e}", file=sys.stderr)
        return 2
    try:
        client, conf = client_of(config)
        if args.command == "status":
            status = client.status()
            print(json.dumps({k: status.get(k) for k in ("catalog", "links", "submissions")}, ensure_ascii=False))
            print(f"目录表：{len(load_map())} 个编码（{state_dir() / MAP_FILE}）")
            return 0
        if args.command == "link":
            most, days = args.max or conf["max_items"], args.days or conf["expires_days"]
            if not args.general and not _REF.match(args.ref):
                print("专属链接要写 --ref（如 p12）；要给老板发群里的用 --general", file=sys.stderr)
                return 2
            made = client.create_link(ref=args.ref or "general", owner=conf["owner"], max_items=most,
                                      expires_days=days, general=args.general, ask_account=True)
            print(made["url"])
            return 0
        platforms = _load_platforms()
        cfg = platforms.config(config)
        workspace = Path(str(config.get("prm_workspace") or "")).expanduser() if config.get("prm_workspace") else None
        products = platforms.load_catalog(workspace) if workspace else None
        if products is None:
            raise SiteError("unconfigured", "没有抖店商品目录（kb/products.json）：先跑 worker.py --sync-products")
        built = build_catalog(conf, jst=jst_runner(platforms, cfg, say=print), products=products,
                              local_image=_local_image(workspace), say=print)
        for note in built["notes"]:
            print(f"  · {note}")
        if args.out:
            Path(args.out).write_text(json.dumps({"doc": built["doc"], "styles": built["styles"]}, ensure_ascii=False,
                                                 indent=1), encoding="utf-8")
        skus = sum(len(c["sizes"]) for s in built["doc"]["styles"] for c in s["colours"])
        if args.dry_run:
            print(f"只是试生成：{len(built['doc']['styles'])} 款、{skus} 个编码、{len(built['images'])} 张图，没有传")
            return 0
        answer = upload_catalog(client, built, keep=load_map())
        print(f"已上传：第 {answer.get('version')} 版，{answer.get('styles')} 款、{answer.get('skus')} 个编码，"
              f"新传 {answer.get('images_sent')} 张图")
        return 0
    except SiteError as e:
        print(f"没做成（{e.verdict}）：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
