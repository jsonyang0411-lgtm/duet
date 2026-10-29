#!/usr/bin/env python3
"""达人广场「邀约带货」的两段式批量工具（说明见同目录 README.md）。

plan（只读，什么都不发）：
  聚水潭命令行 ``jst find`` 按条件找达人（可以翻页）→ 去掉台账里邀约过的 → 抖音命令行
  ``dy buyin author-invite-status`` 一次查一批平台状态，只留「邀约带货」（状态 1）的 → 帽子占比
  ≥ 90% 的单列为「疑似同行自播号」、女性粉丝低于 60% 的，都不进清单 → 按三类配话术和商品（主推
  1 款 + 配置里其余的都附带，一张卡最多 5 款；默认不看库存，配置 min_stock 大于 0 才查、才去掉
  缺货的）→ 读出本周额度 → 写一份给人看的清单（Markdown + CSV）和一份给第二段用的 JSON。

run（要 ``--confirm <清单编号>`` 和清单文件）：只发清单里的人。每位先只读预览，再带
``--expect-status 1 --expect-popwindow -1 --execute`` 发送（预览绑定执行）。发一位记一位：本地台账
追加写、0600，执行之前先记「开始发」，同一个抖音号绝不发第二次，进程中途死掉重跑也不会重发。
两位之间随机等 15～30 秒；退出码 76 / 77 / 79 立刻停；STATE_CONFLICT 跳过这位、继续；平台本周
额度低于余量、或我们自己的每周上限（默认 800）到了也停；重跑就接着上次停下的地方。

两个命令行都经本目录的放慢包装（paced-dy.mjs / paced-jst.mjs）调用：任意两次平台请求之间至少
隔 gap_seconds（默认 3 秒，跨进程也算）。只用标准库，macOS 自带的 Python 3.9 能跑。
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import io
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

TOOL_DIR = Path(__file__).resolve().parent
BJT = timezone(timedelta(hours=8))
DEFAULT_HOME = Path.home() / ".claude-relay" / "platform" / "creator-invites"

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2
EXIT_WALL, EXIT_LOGIN, EXIT_ACCOUNT, EXIT_INTERRUPTED = 76, 77, 79, 130
STOP_EXITS = {EXIT_WALL: "验证码或限流（退出码 76）", EXIT_LOGIN: "登录失效（退出码 77）", EXIT_ACCOUNT: "登录的不是指定的账号（退出码 79）"}
# The douyin CLI could not tell which login the tab is (exit 1; 2026-10-29 live: getUser gave shop id 0 = a 百应 tab that
# had logged out). It checks before the command's own request, so nothing else went out — and nothing will, until a person logs in.
IDENTITY_UNVERIFIABLE = "[IDENTITY_UNVERIFIABLE]"
# A 抖音 search in the config may only filter and page: these the tool sets itself, or they would bypass the pacing, write a file or act.
DY_SEARCH_FORBIDDEN = ("--execute", "--raw", "--out", "--with-values", "--expect-shop-id", "--expect-user-id", "--expect-aavid",
                       "--transport", "--session-file", "--session-from", "--no-open", "--format", "--fields")
LOGIN_HINT = "核对不了调试 Chrome 里登录的是哪家店，多半是百应掉登录了：在调试 Chrome 里重新登录百应、开着百应的页面，再重跑"

AWEME_ID = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")          # what douyin-engineering's author-invite takes
WECHAT = re.compile(r"^[a-zA-Z_-][a-zA-Z\d_-]{5,19}$")      # the drawer's own patterns
PHONE = re.compile(r"^1[23456789]\d{9}$")
DIGITS = re.compile(r"^\d{1,20}$")
PRODUCT_ID = re.compile(r"^\d{6,25}$")
STYLE_CODE = re.compile(r"^[A-Za-z0-9_-]{2,40}$")
RIGHT_WORDS = ("high-commission", "free-sample", "video-material", "video-boost")
RIGHT_NAMES = {"high-commission": "专属高佣", "free-sample": "免费申样", "video-material": "视频素材支持", "video-boost": "优质视频投放"}
COMMISSION_CAP = 25                                        # the user's own ceiling (2026-10-28), also enforced by the CLI
CONTENT_MAX = 150                                          # the drawer's limit, counted as JS counts (UTF-16 units)
NOT_INVITED, GATE_OPEN = 1, -1                              # status 1 = 邀约带货 (enabled); popwindow -1 = no confirmation
SENT_OUTCOMES = ("VERIFIED_SUCCESS", "ACCEPTED_UNVERIFIED", "INDETERMINATE", "CHANGED_WITH_SIDE_EFFECT", "OBSERVED_DESIRED_STATE")

CATEGORY_NAMES = {"small": "小达人", "mid": "中大博主", "big": "大主播/专场型"}
CATEGORY_ORDER = ("small", "mid", "big")
# 三类话术（用户 2026-10-28 定的要点；{品牌} 换成配置里的 brand；只写「包邮」，不写快递公司；
# 2026-10-29 去掉了「视频素材支持」（卡上也不再勾这项权益）和「25%专属高佣」：佣金不在卡上，到微信里谈；
# 同天下午加上「合作细节加微信详聊」：卡就是为了让达人加卡上的微信）
DEFAULT_TEMPLATES = {
    "small": "您好！我们是原创设计师帽饰品牌{品牌}，主打英伦复古、老钱风女帽，客单价高、转化好。很喜欢您的穿搭内容，"
             "诚邀您带我们的帽子，免费寄样包邮。合作细节加微信详聊，期待合作～",
    "mid": "您好！我们是设计师女帽品牌{品牌}（英伦复古/老钱风），一直关注您的内容，诚邀您合作带货：现货样品任选3顶，"
           "免费包邮寄出。合作细节加微信详聊，期待您的回复～",
    "big": "您好！我们是设计师女帽品牌{品牌}，主打英伦复古、老钱风全系帽饰。诚邀您做一场品牌专场：专场样品看中多少款寄多少款，"
           "全系现货锁库存，优质视频投流支持。合作细节加微信详聊，期待合作！",
}
DEFAULT_FIND = {
    "goods_category": "1000003461",     # 帽子（服装 > 服装配饰 > 帽子）
    "price_min": 150, "price_max": 500,
    # 平台那边先筛掉一部分「可能骗样」的（等级 LV2 以上；小达人近 30 天销售额区间下限 ≥ 1 万。20 万粉以上不按销售额筛：
    # 销售额不公开的大主播平台会整个筛掉），翻的页就不浪费在这些人身上；本地照样按 DEFAULT_RULES 再查一遍
    "queries": [{"fans_min": 5000, "fans_max": 200000, "level_min": 2, "sales_min": 10000, "pages": 2},
                {"fans_min": 200000, "level_min": 2, "pages": 1}],
}
DEFAULT_RULES = {
    "small_fans_min": 5000, "mid_fans_min": 200000,        # 小达人 5000～20 万（15～20 万也算小达人：用户 2026-10-29），中大博主 ≥ 20 万
    "big_live_min": 5000000, "big_level_min": 4,          # 专场型：直播为主、近 30 天直播销售额区间下限 ≥ 500 万、LV4 及以上
    "suspect_hat_share": 90.0,
    "female_min": 60.0,                                    # 女性粉丝低于它的不进清单（我们卖女帽：用户 2026-10-29）
    "low_hat_share": 1.0,                                  # 帽子占比低于它的标出来（只是标，不去掉）
    # 专门带自己牌子的不进（用户 2026-10-29）：名字像店铺 / 品牌号，或这次找到的人里好几个号用同一个店名开头（店铺矩阵号）
    "own_brand_words": "女装|男装|童装|服饰|服装|羽绒|皮草|鹅绒|貂|大码|工厂|厂家|源头|档口|批发|尾货|仓储|仓库|老板娘|旗舰|官方|专卖|门店|实体|店|品牌|定制|高定|上新",
    "matrix_min": 2,
    # 可能骗样品自己用的不进（用户 2026-10-29）：带货等级低、卖得少、销售额不公开又等级不高、粉丝多卖得少
    "min_level": 2,                                        # 带货等级低于 LV2
    "min_sales30d": 10000,                                 # 近 30 天销售额区间下限低于 1 万元
    "hidden_sales_min_level": 3,                           # 销售额不公开（无数据）时，等级低于 LV3
    "min_sales_per_fan": 0.2, "sales_per_fan_from": 50000, # 粉丝 5 万以上时，销售额下限 ÷ 粉丝数不到 0.2 元
    # 只标出来给人看：帽子占比高（可能自己做帽子）、近 30 天几乎只卖一类别的货（可能只带一个牌子）
    "own_hat_flag": 50.0, "single_category_flag": 90.0,
    "drop_ids": [], "keep_ids": [],                        # 手动：一定不要的抖音号 / 不按「自己牌子」「骗样」两组规则去掉的抖音号
}


class ToolError(Exception):
    """A problem that ends the command with ``exit_code`` (the message is Chinese, for the operator)."""

    def __init__(self, message: str, exit_code: int = EXIT_FAILED):
        super().__init__(message)
        self.exit_code = exit_code


class StopRun(ToolError):
    """A platform said stop (76 / 77 / 79, or an interrupted write): nothing more is asked of it."""


def js_length(text: str) -> int:
    """The length JavaScript gives a string (UTF-16 units): what the CLI's 150-character check counts."""
    return len(text.encode("utf-16-le")) // 2


def now_bjt() -> datetime:
    return datetime.now(BJT)


def week_start(moment: datetime) -> datetime:
    """Monday 00:00 (Beijing time) of the week ``moment`` is in."""
    local = moment.astimezone(BJT)
    return (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def mask_contact(value: str) -> str:
    return value[:3] + "*" * max(len(value) - 3, 3) if value else ""


def private_write(path: Path, text: str) -> None:
    """Write a file only its owner can read (0600), whatever the umask."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(str(path), 0o600)


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(str(path), 0o700)
    return path


# ── configuration ────────────────────────────────────────────────────────────

def find_node(configured: str = "") -> Optional[str]:
    candidates = [configured] if configured else []
    candidates += [shutil.which("node") or "", str(Path.home() / ".local/bin/node"), "/opt/homebrew/bin/node", "/usr/local/bin/node"]
    for c in candidates:
        path = os.path.expanduser(c) if c else ""
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _number(raw: dict, key: str, default, low, high, *, integer: bool = False):
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (low <= value <= high) or (integer and int(value) != value):
        raise ToolError(f"配置 {key} 要是 {low} 到 {high} 之间的{'整数' if integer else '数'}（现在是 {value!r}）", EXIT_USAGE)
    return int(value) if integer else float(value)


def load_config(path: Optional[str]) -> dict:
    """The tool's settings (a JSON file; tools/creator_invite/config.example.json), checked. Ids and the contact live only there."""
    raw: dict = {}
    if path:
        try:
            raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ToolError(f"读不了配置文件 {path}：{e}", EXIT_USAGE)
        if not isinstance(raw, dict):
            raise ToolError("配置文件要是一个 JSON 对象", EXIT_USAGE)
    cfg: dict = {"config_path": str(path or "")}
    cfg["node"] = find_node(str(raw.get("node") or ""))
    cfg["dy_dir"] = Path(os.path.expanduser(str(raw.get("dy_dir") or "~/Desktop/douyin-engineering"))).resolve()
    cfg["jst_dir"] = Path(os.path.expanduser(str(raw.get("jst_dir") or "~/Desktop/jushuitan-engineering"))).resolve()
    cfg["cdp_endpoint"] = str(raw.get("cdp_endpoint") or "http://127.0.0.1:9222")
    if not re.match(r"^(https?|wss?)://[^\s]{3,200}$", cfg["cdp_endpoint"]):
        raise ToolError("配置 cdp_endpoint 要是调试 Chrome 的地址，比如 http://127.0.0.1:9222", EXIT_USAGE)
    for key in ("dy_expect_shop_id", "jst_expect_uid"):
        value = str(raw.get(key) or "").strip()
        if not DIGITS.match(value):
            raise ToolError(f"配置 {key} 必填（数字）：每条平台命令都先核对登录的是不是这个账号", EXIT_USAGE)
        cfg[key] = value
    cfg["invite_wechat"] = str(raw.get("invite_wechat") or "").strip()
    cfg["invite_phone"] = str(raw.get("invite_phone") or "").strip()
    if cfg["invite_wechat"] and not WECHAT.match(cfg["invite_wechat"]):
        raise ToolError("配置 invite_wechat 不是邀约抽屉认的微信号格式", EXIT_USAGE)
    if cfg["invite_phone"] and not PHONE.match(cfg["invite_phone"]):
        raise ToolError("配置 invite_phone 不是邀约抽屉认的手机号格式", EXIT_USAGE)
    if not cfg["invite_wechat"] and not cfg["invite_phone"]:
        raise ToolError("配置 invite_wechat / invite_phone 至少要一个：邀约卡上给达人看的联系方式", EXIT_USAGE)
    cfg["brand"] = str(raw.get("brand") or "").strip()
    # 卡上的专属佣金：0（默认）= 不开，卡只为让达人加微信，佣金到微信里谈（用户 2026-10-29 下午）；1–25 = 卡一发出去
    # 平台就给卡上的商品开这个比例的长期专属佣金
    cfg["commission_rate"] = _number(raw, "commission_rate", 0, 0, COMMISSION_CAP, integer=True)
    rights = raw.get("rights", ["free-sample"])               # 2026-10-29 下午：只勾免费申样
    if not isinstance(rights, list) or any(r not in RIGHT_WORDS for r in rights) or len(set(rights)) != len(rights):
        raise ToolError(f"配置 rights 要是 {', '.join(RIGHT_WORDS)} 里的几个（也可以一个都不勾）", EXIT_USAGE)
    if ("high-commission" in rights) != bool(cfg["commission_rate"]):
        raise ToolError("配置 rights 里的 high-commission（专属高佣）和 commission_rate 要一起有、一起没有：勾专属高佣就写比例，"
                        "不开专属佣金就别勾", EXIT_USAGE)
    cfg["rights"] = list(rights)
    # 0（默认）= 不看库存：能挂上卡的都配（老板 2026-10-29）；大于 0 才按款号查聚水潭的可用库存、不配缺货的
    cfg["min_stock"] = _number(raw, "min_stock", 0, 0, 100000, integer=True)
    products = raw.get("products")
    if not isinstance(products, list) or not 1 <= len(products) <= 5:
        raise ToolError("配置 products 要列 1 到 5 款商品（一张卡最多 5 款）：{id, name, price, styles}", EXIT_USAGE)
    cfg["products"] = []
    for p in products:
        if not isinstance(p, dict):
            raise ToolError("products 的每一项要是一个对象", EXIT_USAGE)
        pid, name, styles = str(p.get("id") or ""), str(p.get("name") or "").strip(), p.get("styles", [])
        price = p.get("price")
        if not PRODUCT_ID.match(pid) or not name or isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
            raise ToolError("products 的每一项要有 id（抖音商品 ID）、name、price（元）", EXIT_USAGE)
        if (not isinstance(styles, list) or (cfg["min_stock"] and not styles)
                or not all(isinstance(s, str) and STYLE_CODE.match(s) for s in styles)):
            raise ToolError(f"商品 {name} 的 styles 要是聚水潭的款号（min_stock 大于 0 时必填，查真实可用库存用）", EXIT_USAGE)
        cfg["products"].append({"id": pid, "name": name, "price": float(price), "styles": list(styles)})
    if len({p["id"] for p in cfg["products"]}) != len(cfg["products"]):
        raise ToolError("products 里有重复的商品", EXIT_USAGE)
    find = dict(DEFAULT_FIND)
    find.update(raw.get("find") or {})
    if not re.match(r"^\d{4,15}(,\d{4,15})*$", str(find.get("goods_category"))):
        raise ToolError("配置 find.goods_category 要是细分类目编号（帽子是 1000003461）", EXIT_USAGE)
    for key in ("price_min", "price_max"):
        _number(find, key, find.get(key), 0, 1000000)
    queries = find.get("queries")
    if not isinstance(queries, list) or not 1 <= len(queries) <= 10:
        raise ToolError("配置 find.queries 要是 1 到 10 组筛选", EXIT_USAGE)
    for q in queries:
        if not isinstance(q, dict):
            raise ToolError("find.queries 的每一组要是一个对象", EXIT_USAGE)
        for key in ("fans_min", "fans_max", "sales_min", "sales_max"):
            if key in q:
                _number(q, key, q[key], 0, 10 ** 10, integer=True)
        for key in ("level_min", "level_max"):
            if key in q:
                _number(q, key, q[key], 0, 7, integer=True)
        _number(q, "pages", q.get("pages", 1), 1, 5, integer=True)
        _number(q, "page", q.get("page", 1), 1, 500, integer=True)
    # 抖音那边也找（用户 2026-10-29）：百应达人广场的只读搜索，参数原样写在配置里（douyin-engineering 的 buyin authors-*）
    dy_queries = find.get("dy_queries") or []
    if not isinstance(dy_queries, list) or len(dy_queries) > 10:
        raise ToolError("配置 find.dy_queries 要是 0 到 10 组抖音达人广场的搜索", EXIT_USAGE)
    for q in dy_queries:
        args = q.get("args") if isinstance(q, dict) else None
        if (not isinstance(args, list) or not 2 <= len(args) <= 30 or not all(isinstance(a, str) for a in args)
                or args[0] != "buyin" or not re.match(r"^authors-[a-z-]{2,30}$", args[1])):
            raise ToolError("find.dy_queries 的每一组要写 args：[\"buyin\", \"authors-…\", \"--参数=值\", …]（只读的找达人命令）", EXIT_USAGE)
        for a in args[2:]:
            name = a.split("=", 1)[0]
            if not re.match(r"^--[a-z][a-z0-9-]{0,40}(=[^\s]{1,120})?$", a) or name in DY_SEARCH_FORBIDDEN:
                raise ToolError(f"find.dy_queries 里的参数 {a[:60]} 不行：只写筛选和页数（{'、'.join(DY_SEARCH_FORBIDDEN)} 由工具管或不许用）", EXIT_USAGE)
    find["dy_queries"] = dy_queries
    find["dy_lookup_max"] = _number(find, "dy_lookup_max", find.get("dy_lookup_max", 60), 0, 500, integer=True)
    cfg["find"] = find
    rules = dict(DEFAULT_RULES)
    rules.update(raw.get("rules") or {})
    try:
        re.compile(str(rules["own_brand_words"] or ""))
    except re.error as e:
        raise ToolError(f"配置 rules.own_brand_words 不是正则表达式：{e}", EXIT_USAGE)
    for key in ("drop_ids", "keep_ids"):
        ids = rules.get(key)
        if not isinstance(ids, list) or not all(isinstance(x, str) and AWEME_ID.match(x) for x in ids):
            raise ToolError(f"配置 rules.{key} 要是抖音号的列表", EXIT_USAGE)
    cfg["rules"] = rules
    templates = dict(DEFAULT_TEMPLATES)
    templates.update(raw.get("templates") or {})
    cfg["templates"] = {}
    for key in CATEGORY_ORDER:
        text = str(templates.get(key) or "")
        if "{品牌}" in text:
            if not cfg["brand"]:
                raise ToolError("话术里有 {品牌}：在配置里写 brand", EXIT_USAGE)
            text = text.replace("{品牌}", cfg["brand"])
        if not text.strip() or js_length(text) > CONTENT_MAX:
            raise ToolError(f"{CATEGORY_NAMES[key]}的话术要 1 到 {CONTENT_MAX} 字（现在 {js_length(text)} 字）", EXIT_USAGE)
        if re.search(r"顺丰|中通|圆通|申通|韵达|京东快递|邮政", text):
            raise ToolError(f"{CATEGORY_NAMES[key]}的话术里写了快递公司：只写「包邮」", EXIT_USAGE)
        cfg["templates"][key] = text
    cfg["weekly_cap"] = _number(raw, "weekly_cap", 800, 1, 5000, integer=True)
    # 每天最多发几张（2026-10-29：同店另一位 3 分钟发了 20 张，像是碰了平台风控——我们放慢、限量）
    cfg["daily_cap"] = _number(raw, "daily_cap", 20, 1, 1000, integer=True)
    cfg["quota_margin"] = _number(raw, "quota_margin", 50, 0, 5000, integer=True)
    cfg["gap_seconds"] = _number(raw, "gap_seconds", 3, 3, 60)
    wait = raw.get("wait_between", [15, 30])
    if (not isinstance(wait, list) or len(wait) != 2 or any(isinstance(w, bool) or not isinstance(w, (int, float)) for w in wait)
            or not 10 <= wait[0] <= wait[1] <= 600):
        raise ToolError("配置 wait_between 要是 [最少秒, 最多秒]，至少 10 秒", EXIT_USAGE)
    cfg["wait_between"] = [float(wait[0]), float(wait[1])]
    cfg["plan_max_age_days"] = _number(raw, "plan_max_age_days", 3, 0.01, 30)
    cfg["home"] = Path(os.path.expanduser(str(raw.get("home") or DEFAULT_HOME)))
    return cfg


def check_clis(cfg: dict, need: tuple = ("dy", "jst")) -> None:
    if not cfg["node"]:
        raise ToolError("找不到 node：在配置的 node 里写上它的路径", EXIT_USAGE)
    for which in need:
        folder = cfg["dy_dir"] if which == "dy" else cfg["jst_dir"]
        cli = "bin/dy.mjs" if which == "dy" else "bin/jst.mjs"
        if not (folder / cli).is_file():
            raise ToolError(f"{folder} 里没有 {cli}：配置 {which}_dir 写错了", EXIT_USAGE)


# ── the command lines ────────────────────────────────────────────────────────

class CliResult:
    def __init__(self, argv: List[str], exit_code: Optional[int], stdout: str, stderr: str, seconds: float, timed_out: bool = False):
        self.argv, self.exit_code, self.stdout, self.stderr, self.seconds, self.timed_out = argv, exit_code, stdout, stderr, seconds, timed_out
        self.data = None
        try:
            parsed = json.loads(stdout) if stdout.strip() else None
            self.data = parsed if isinstance(parsed, dict) else None
        except ValueError:
            self.data = None

    def said(self) -> str:
        """The CLI's own words on stderr, for the operator (one line, at most 300 characters): its last error line
        (「[CODE] …」, how both CLIs say what went wrong), else its last line (a trailing note says less)."""
        lines = [l.strip() for l in self.stderr.strip().splitlines() if l.strip()]
        tagged = [l for l in lines if re.match(r"^\[[A-Z_]+\]", l)]
        return ((tagged or lines or [""])[-1])[:300]

    def unverifiable(self) -> bool:
        return self.exit_code != 0 and IDENTITY_UNVERIFIABLE in self.stderr


class Clis:
    """Runs the two command lines through the pacing wrappers in one work folder (this run's request log). ``stamp`` is the
    shared request clock: the time of the last platform request of ANY run that uses it (the ledger folder's ``last-request``)."""

    def __init__(self, cfg: dict, workdir: Path, runner: Callable = subprocess.run, stamp: Optional[Path] = None):
        self.cfg, self.workdir, self.runner = cfg, private_dir(workdir), runner
        self.stamp = stamp or (self.workdir / "last-request")
        self.calls: List[List[str]] = []

    def env(self, which: str) -> dict:
        env = dict(os.environ)
        env.pop("DY_TRANSPORT", None)                    # always the page transport (the one the wrapper paces)
        env.update({
            "CREATOR_INVITE_CLI_ROOT": str(self.cfg["dy_dir"] if which == "dy" else self.cfg["jst_dir"]),
            "CREATOR_INVITE_GAP_MS": str(int(self.cfg["gap_seconds"] * 1000)),
            "CREATOR_INVITE_STAMP": str(self.stamp),
            "CREATOR_INVITE_REQUEST_LOG": str(self.workdir / "requests.jsonl"),
            "NO_COLOR": "1",
        })
        env["DY_CDP_ENDPOINT" if which == "dy" else "JST_CDP_ENDPOINT"] = self.cfg["cdp_endpoint"]
        return env

    def run(self, which: str, args: List[str], timeout: float) -> CliResult:
        wrapper = TOOL_DIR / ("paced-dy.mjs" if which == "dy" else "paced-jst.mjs")
        argv = [str(self.cfg["node"]), str(wrapper), *args]
        self.calls.append(list(args))
        started = time.monotonic()
        try:
            done = self.runner(argv, cwd=str(self.workdir), env=self.env(which), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")
            err = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode("utf-8", "replace")
            return CliResult(args, None, out, err, time.monotonic() - started, timed_out=True)
        return CliResult(args, done.returncode, done.stdout or "", done.stderr or "", time.monotonic() - started)

    def requests(self) -> Dict[str, int]:
        counts = {"dy": 0, "jst": 0}
        try:
            for line in (self.workdir / "requests.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    which = json.loads(line).get("cli", "?")
                except (ValueError, AttributeError):
                    continue
                counts[which] = counts.get(which, 0) + 1
        except OSError:
            pass
        return counts


def must_read(result: CliResult, what: str) -> dict:
    """A read's JSON, or the stop / failure it ended with."""
    if result.exit_code in STOP_EXITS:
        raise StopRun(f"{what}：{STOP_EXITS[result.exit_code]}，立刻停下。命令行说：{result.said()}", result.exit_code)
    if result.unverifiable():
        raise StopRun(f"{what}：{LOGIN_HINT}。命令行说：{result.said()}")
    if result.timed_out:
        raise ToolError(f"{what}：命令行超时")
    if result.exit_code != 0 or result.data is None:
        raise ToolError(f"{what}：命令行退出码 {result.exit_code}。{result.said()}")
    return result.data


# ── the ledger ───────────────────────────────────────────────────────────────

class Ledger:
    """invites.jsonl：一行一件事，只追加、0600。``attempt`` 在执行之前写（并 fsync）：有 attempt、没有说明「没发出去」
    的 result 的抖音号，永远不再发——哪怕进程在两行之间死掉。"""

    def __init__(self, folder: Path):
        self.folder = private_dir(folder)
        self.path = folder / "invites.jsonl"
        self._lock_fd: Optional[int] = None

    def lock(self) -> None:
        fd = os.open(str(self.folder / "invites.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise ToolError(f"另一个 run 正在用这个台账（{self.folder}）：等它结束", EXIT_USAGE)
        self._lock_fd = fd

    def unlock(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def records(self) -> List[dict]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        out = []
        for line in text.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue                                  # a line cut by a crash: the attempt before it still counts
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def append(self, record: dict) -> dict:
        rec = {"at": now_bjt().isoformat(timespec="seconds"), **record}
        fd = os.open(str(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(str(self.path), 0o600)
        return rec

    def _attempts(self) -> Dict[str, dict]:
        """attempt id → {aweme_id, at, sent (None = not known)}."""
        attempts: Dict[str, dict] = {}
        for rec in self.records():
            if rec.get("event") == "attempt" and rec.get("attempt"):
                attempts[rec["attempt"]] = {"aweme_id": rec.get("aweme_id"), "at": rec.get("at"), "sent": None}
            elif rec.get("event") == "result" and rec.get("attempt") in attempts:
                attempts[rec["attempt"]]["sent"] = rec.get("sent")
        return attempts

    def never_again(self) -> set:
        """抖音号：发出去了，或不知道发出去没有。"""
        return {a["aweme_id"] for a in self._attempts().values() if a["sent"] is not False}

    def unknown(self) -> List[str]:
        return [a["aweme_id"] for a in self._attempts().values() if a["sent"] is None]

    def sent_this_week(self, moment: datetime) -> int:
        return self.sent_since(week_start(moment))

    def sent_today(self, moment: datetime) -> int:
        return self.sent_since(moment.astimezone(BJT).replace(hour=0, minute=0, second=0, microsecond=0))

    def sent_since(self, start: datetime) -> int:
        """发出去了或不知道发出去没有的卡，从 ``start`` 起有几张（读不懂时间的也算上，宁多不少）。"""
        n = 0
        for a in self._attempts().values():
            if a["sent"] is False:
                continue
            try:
                at = datetime.fromisoformat(str(a["at"]))
            except ValueError:
                n += 1                                     # not readable: counted, never under-counted
                continue
            if at >= start:
                n += 1
        return n


# ── plan ─────────────────────────────────────────────────────────────────────

def percent(text) -> Optional[float]:
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*%?\s*$", str(text or ""))
    return float(m.group(1)) if m else None


def hat_share(row: dict) -> Optional[float]:
    for g in row.get("recentGoods") or []:
        if isinstance(g, dict) and g.get("name") == "帽子":
            return percent(g.get("ratio"))
    return 0.0 if row.get("recentGoods") else None


def female_share(row: dict) -> Optional[float]:
    """粉丝画像只给占比最大的性别：「女性 96%」→ 96；「男性 62%」→ 38；没有就 None。"""
    sex = str((row.get("fans") or {}).get("sex") or "")
    m = re.match(r"^\s*(女性|男性)\s+(\d+(?:\.\d+)?)%", sex)
    if not m:
        return None
    value = float(m.group(2))
    return round(value if m.group(1) == "女性" else 100.0 - value, 2)


def band_min(band) -> float:
    return float(band.get("min") or 0) if isinstance(band, dict) and band.get("available") else 0.0


def sales_floor(row: dict) -> Optional[float]:
    """近 30 天销售额区间的下限（元）；平台没给（达人不公开，「无数据」）是 None。"""
    band = row.get("sales30d")
    return float(band.get("min") or 0) if isinstance(band, dict) and band.get("available") else None


def top_category(row: dict):
    """近 30 天卖得最多的细分品类和它的占比（%）；没有就 (None, 0)。"""
    best, share = None, 0.0
    for g in row.get("recentGoods") or []:
        value = percent(g.get("ratio")) if isinstance(g, dict) else None
        if value is not None and value > share:
            best, share = g.get("name"), value
    return best, share


def name_stem(nickname: str) -> str:
    """昵称里店名的那一段：第一个括号 / 点 / 横线 / 竖线 / 空格前面的（「小鹿（日常穿搭）」→「小鹿」，「木子-琳琳」→「木子」）；
    没有这些分隔的，整个昵称就是它自己。英文不分大小写（「Mia(…)」和「MIA-…」算同一家：2026-10-29 真遇到过只差大小写的同一家店）。"""
    text = re.sub(r"^[\W_]+", "", nickname or "")
    m = re.match(r"^([^（(•·\-|｜【\[\s]{2,})[（(•·\-|｜【\[\s]", text)
    return (m.group(1) if m else text).casefold()


def own_brand_reason(view: dict, rules: dict, stems: Dict[str, int]) -> Optional[str]:
    """专门带自己牌子的多半不接别人的货：名字像店铺 / 品牌号，或店铺矩阵号（同一个店名开头的号这次找到 ≥ matrix_min 个）。"""
    words = str(rules["own_brand_words"] or "")
    hit = re.search(words, view["nickname"]) if words else None
    if hit:
        return f"名字像店铺 / 品牌号（「{hit.group(0)}」）：多半专门带自己的牌子"
    stem = name_stem(view["nickname"])
    if stem and stems.get(stem, 0) >= rules["matrix_min"]:
        return f"店铺矩阵号（这次找到 {stems[stem]} 个「{stem}」开头的号）：多半专门带自己的牌子"
    return None


def sample_risk_reason(row: dict, view: dict, rules: dict) -> Optional[str]:
    """可能骗样品自己用的：带货等级低、近 30 天卖得少、销售额不公开又等级不高、粉丝多卖得少。"""
    level = view["level"] or 0
    if level < rules["min_level"]:
        return f"带货等级 LV{level}（低于 LV{rules['min_level']}）：卖得少，骗样风险高"
    floor = sales_floor(row)
    if floor is None:
        if level < rules["hidden_sales_min_level"]:
            return f"近 30 天销售额不公开、带货等级只有 LV{level}：骗样风险高"
        return None
    if floor < rules["min_sales30d"]:
        return f"近 30 天销售额只有 {view['sales30d']}（低于 {rules['min_sales30d']:g} 元）：骗样风险高"
    fans = view["fans"] or 0
    if fans >= rules["sales_per_fan_from"] and floor / fans < rules["min_sales_per_fan"]:
        return (f"粉丝 {fans}、近 30 天销售额只有 {view['sales30d']}（每个粉丝不到 {rules['min_sales_per_fan']:g} 元）：粉多卖少，"
                "骗样风险高")
    return None


def classify(row: dict, rules: dict):
    """(类别, 不进清单的原因)：先看专场型，再按粉丝分中大博主（≥ 20 万）/ 小达人（5000 到 20 万，15～20 万也算）；太小的不进。"""
    fans = row.get("fansNum") or 0
    level = row.get("level") or 0
    if (row.get("channelBySales") == "直播" and band_min(row.get("sales30dLive")) >= rules["big_live_min"]
            and level >= rules["big_level_min"]):
        return "big", None
    if fans >= rules["mid_fans_min"]:
        return "mid", None
    if fans >= rules["small_fans_min"]:
        return "small", None
    return None, f"粉丝不到 {rules['small_fans_min']}"


def assign_products(avg_price, products: List[dict]) -> List[dict]:
    """主推：售价最接近达人件单价的那款（一样近取配置里靠前的）；附带：其余配得上的全部，按配置顺序（配置最多 5 款 = 一张卡的上限）。"""
    if not products:
        return []
    if isinstance(avg_price, (int, float)) and avg_price > 0:
        main = min(products, key=lambda p: (abs(p["price"] - avg_price), products.index(p)))
    else:
        main = products[0]
    return [main] + [p for p in products if p is not main]


def check_stock(cfg: dict, clis: Clis) -> List[dict]:
    if not cfg["min_stock"]:                              # 不看库存：一个请求都不发，每款都配
        return [{**p, "stock": None, "missing_styles": [], "in_stock": True} for p in cfg["products"]]
    out = []
    for p in cfg["products"]:
        stock, missing = 0, []
        for style in p["styles"]:
            data = must_read(clis.run("jst", ["goods-detail", f"--style-code={style}", f"--expect-uid={cfg['jst_expect_uid']}"], 300),
                             f"聚水潭查库存（{style}）")
            if not data.get("found"):
                missing.append(style)
            for sku in data.get("skus") or []:
                value = sku.get("availableStock")
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    stock += int(value)
        out.append({**p, "stock": stock, "missing_styles": missing, "in_stock": stock >= cfg["min_stock"]})
    return out


def find_creators(cfg: dict, clis: Clis) -> List[dict]:
    find, rows = cfg["find"], []
    for q in find["queries"]:
        args = ["find", "--platform=DY", f"--goods-category={find['goods_category']}", f"--price-min={find['price_min']}",
                f"--price-max={find['price_max']}", "--not-added"]
        for key, flag in (("fans_min", "--fans-min"), ("fans_max", "--fans-max"), ("level_min", "--level-min"), ("level_max", "--level-max"),
                          ("sales_min", "--sales-min"), ("sales_max", "--sales-max")):
            if key in q:
                args.append(f"{flag}={int(q[key])}")
        args += [f"--page={int(q.get('page', 1))}", f"--pages={int(q.get('pages', 1))}", f"--expect-uid={cfg['jst_expect_uid']}"]
        data = must_read(clis.run("jst", args, 600), "聚水潭找达人")
        if data.get("stopped"):
            # jst find hands back the pages it read before a later page was rate-limited, with exit 0: that is a wall all the same
            raise StopRun(f"聚水潭找达人：翻页时被限流，立刻停下。命令行说：{str(data['stopped'])[:200]}", EXIT_WALL)
        for r in data.get("rows") or []:
            if isinstance(r, dict):
                rows.append(r)
    return rows


def find_on_douyin(cfg: dict, clis: Clis, known: set) -> dict:
    """百应达人广场那边找到的人（find.dy_queries 每组一条只读搜索）：聚水潭那边已经有的只记一笔「两边都有」；行上的按钮已经不是
    「邀约带货」（imStatus 不是 1：本店沟通过、禁用）的、已经在本店百应达人库里（inCrm）的不查也不进；其余的每位到聚水潭按抖音号
    查一次（jst lookup，最多 dy_lookup_max 位），拿到和 jst find 一样的行（粉丝画像、近 30 天带货品类和销售额），这样两边的人走
    同一套规则；聚水潭查不到的、已经在聚水潭「我的达人」里的、件单价不在 find.price_min～price_max 的不进。"""
    out = {"rows": [], "both": set(), "excluded": [], "found": 0, "new": 0, "looked_up": 0, "not_looked_up": 0, "stopped": None}
    queries = cfg["find"].get("dy_queries") or []
    if not queries:
        return out
    order: List[str] = []
    names: Dict[str, dict] = {}
    for q in queries:
        data = must_read(clis.run("dy", [*q["args"], f"--expect-shop-id={cfg['dy_expect_shop_id']}", "--no-open"], 900), "抖音达人广场找达人")
        for r in data.get("rows") or []:
            aweme = str(r.get("awemeId") or "") if isinstance(r, dict) else ""
            if not aweme or aweme in names:
                continue
            names[aweme] = r
            out["found"] += 1
            if aweme in known:
                out["both"].add(aweme)
            elif r.get("imStatus") not in (None, NOT_INVITED):
                out["excluded"].append({"aweme_id": aweme, "nickname": str(r.get("nickname") or ""), "fans": r.get("fansNum"),
                                        "hat_share": None, "female_share": None, "source": "抖音",
                                        "reason": f"百应里的按钮已经不是「邀约带货」（状态 {r.get('imStatus')}：本店沟通过或不能邀约）"})
            elif r.get("inCrm") is True:
                out["excluded"].append({"aweme_id": aweme, "nickname": str(r.get("nickname") or ""), "fans": r.get("fansNum"),
                                        "hat_share": None, "female_share": None, "source": "抖音", "reason": "已经在本店百应达人库里"})
            else:
                order.append(aweme)
    out["new"] = len(order)
    for aweme in order[cfg["find"]["dy_lookup_max"]:]:
        out["not_looked_up"] += 1
    for index, aweme in enumerate(order[:cfg["find"]["dy_lookup_max"]]):
        r = names[aweme]
        stub = {"aweme_id": aweme, "nickname": str(r.get("nickname") or ""), "fans": r.get("fansNum"), "hat_share": None,
                "female_share": None, "source": "抖音"}
        if not AWEME_ID.match(aweme):
            out["excluded"].append({**stub, "reason": "抖音号格式命令行不收"})
            continue
        found = clis.run("jst", ["lookup", "--platform=DY", f"--no={aweme}", f"--expect-uid={cfg['jst_expect_uid']}"], 300)
        out["looked_up"] += 1
        if found.exit_code == EXIT_WALL:
            # 聚水潭限流：不再往下查（不绕过），已经查到的照常进清单，没查的记个数——一份清单不因为这个作废
            left = len(order[:cfg["find"]["dy_lookup_max"]]) - index
            out["looked_up"] -= 1
            out["not_looked_up"] += left
            out["stopped"] = f"聚水潭按抖音号查到第 {index + 1} 位时限流（{found.said()[:120]}）：后面 {left} 位没查，过一会儿重新出清单再查"
            break
        if found.exit_code in STOP_EXITS:
            raise StopRun(f"聚水潭按抖音号查达人：{STOP_EXITS[found.exit_code]}，立刻停下。命令行说：{found.said()}", found.exit_code)
        if found.exit_code != 0 or found.data is None:
            out["excluded"].append({**stub, "reason": f"聚水潭查这个号出错（退出码 {found.exit_code}）：{found.said()[:120]}"})
            continue
        daren = found.data.get("daren") if found.data.get("found") else None
        if not isinstance(daren, dict):
            out["excluded"].append({**stub, "reason": "聚水潭查不到这个号：没有粉丝画像和带货数据，规则筛不了，不进"})
            continue
        if daren.get("added"):
            out["excluded"].append({**stub, "reason": "已经在聚水潭「我的达人」里（有人在跟），不进"})
            continue
        price = daren.get("avgPrice")
        lo, hi = cfg["find"]["price_min"], cfg["find"]["price_max"]
        if isinstance(price, (int, float)) and not isinstance(price, bool) and not lo <= price <= hi:
            out["excluded"].append({**creator_view({**daren, "platformNo": aweme}), "source": "抖音",
                                    "reason": f"件单价 {price} 元不在 {lo:g}～{hi:g} 元（和聚水潭那边一样的条件）"})
            continue
        out["rows"].append({**daren, "platformNo": aweme, "_source": "抖音"})
    return out


def creator_view(row: dict) -> dict:
    return {
        "source": row.get("_source") or "聚水潭",
        "aweme_id": str(row.get("platformNo") or ""), "nickname": str(row.get("nickName") or ""),
        "fans": row.get("fansNum"), "sales30d": (row.get("sales30d") or {}).get("label") if isinstance(row.get("sales30d"), dict) else None,
        "avg_price": row.get("avgPrice"), "level": row.get("level"), "channel": row.get("channelBySales") or row.get("mainChannel"),
        "category_goods": row.get("category"), "hat_share": hat_share(row), "female_share": female_share(row),
    }


def mix_categories(candidates: List[dict]) -> List[dict]:
    """The order the platform's status is asked in: inside each category the ones without a flag first, then the higher hat share;
    then the categories taken in turn (大主播/专场型, 中大博主, 小达人, 大主播 …) so that a list cut at --count has all three."""
    pools = {k: sorted((c for c in candidates if c["category"] == k), key=lambda c: (bool(c["flags"]), -(c["hat_share"] or 0.0)))
             for k in ("big", "mid", "small")}
    out: List[dict] = []
    while any(pools.values()):
        for k in ("big", "mid", "small"):
            if pools[k]:
                out.append(pools[k].pop(0))
    return out


def make_plan_id(moment: datetime) -> str:
    return f"P{moment.strftime('%Y%m%d-%H%M')}-{secrets.token_hex(2)}"


def build_plan(cfg: dict, clis: Clis, ledger: Ledger, count: int, *, moment: Optional[datetime] = None) -> dict:
    moment = moment or now_bjt()
    rules = cfg["rules"]
    products = check_stock(cfg, clis)
    usable = [p for p in products if p["in_stock"]]
    if not usable:
        raise ToolError(f"配置的商品没有一款可用库存 ≥ {cfg['min_stock']}：不出清单")
    rows = find_creators(cfg, clis)
    douyin = find_on_douyin(cfg, clis, {str(r.get("platformNo") or "") for r in rows})
    for r in rows:
        if str(r.get("platformNo") or "") in douyin["both"]:
            r["_source"] = "两边都有"
    rows = rows + douyin["rows"]
    invited = ledger.never_again()
    excluded: List[dict] = list(douyin["excluded"])
    candidates: List[dict] = []
    stems: Dict[str, int] = {}
    for aweme, nick in {str(r.get("platformNo") or ""): str(r.get("nickName") or "") for r in rows}.items():
        stem = name_stem(nick)
        stems[stem] = stems.get(stem, 0) + 1
    seen = set()
    for row in rows:
        view = creator_view(row)
        aweme = view["aweme_id"]
        if not aweme or aweme in seen:
            continue
        seen.add(aweme)
        if not AWEME_ID.match(aweme):
            excluded.append({**view, "reason": "抖音号格式命令行不收"})
            continue
        if aweme in invited:
            excluded.append({**view, "reason": "台账里已经邀约过（或不知道上次发出去没有）"})
            continue
        if aweme in rules["drop_ids"]:
            excluded.append({**view, "reason": "手动排除（配置 rules.drop_ids）"})
            continue
        if view["hat_share"] is not None and view["hat_share"] >= rules["suspect_hat_share"]:
            excluded.append({**view, "reason": f"疑似同行自播号（帽子占比 {view['hat_share']}% ≥ {rules['suspect_hat_share']}%）", "suspect": True})
            continue
        if view["female_share"] is not None and view["female_share"] < rules["female_min"]:
            excluded.append({**view, "reason": f"女性粉丝 {view['female_share']}%（低于 {rules['female_min']}%）"})
            continue
        if aweme not in rules["keep_ids"]:
            why = own_brand_reason(view, rules, stems)
            if why:
                excluded.append({**view, "reason": why, "group": "own_brand"})
                continue
            why = sample_risk_reason(row, view, rules)
            if why:
                excluded.append({**view, "reason": why, "group": "sample_risk"})
                continue
        category, why = classify(row, rules)
        if category is None:
            excluded.append({**view, "reason": why})
            continue
        flags = []
        if view["hat_share"] is not None and view["hat_share"] < rules["low_hat_share"]:
            flags.append(f"帽子占比只有 {view['hat_share']}%")
        if view["female_share"] is None:
            flags.append("粉丝性别不知道")
        if view["hat_share"] is not None and view["hat_share"] >= rules["own_hat_flag"]:
            flags.append(f"帽子占比 {view['hat_share']}%：可能自己做帽子，发之前看一眼她的橱窗")
        cat, share = top_category(row)
        if cat and cat != "帽子" and share >= rules["single_category_flag"]:
            flags.append(f"近 30 天 {share:g}% 是{cat}：可能只带一个牌子，看一眼")
        candidates.append({**view, "category": category, "flags": flags})
    candidates = mix_categories(candidates)
    picked: List[dict] = []
    spare: List[dict] = []
    quotas: List[dict] = []
    checked = 0
    limit_checks = count + 20
    for start in range(0, len(candidates), 20):
        if len(picked) >= count or checked >= limit_checks:
            break
        chunk = candidates[start:start + 20]
        checked += len(chunk)
        data = must_read(clis.run("dy", ["buyin", "author-invite-status", f"--aweme-id={','.join(c['aweme_id'] for c in chunk)}",
                                         "--pause-ms=3000", f"--expect-shop-id={cfg['dy_expect_shop_id']}", "--no-open"], 900),
                         "抖音查邀约状态")
        by_id = {c.get("awemeId"): c for c in data.get("creators") or [] if isinstance(c, dict)}
        for q in (data.get("summary") or {}).get("weeklyQuotas") or []:
            if isinstance(q, dict) and q not in quotas:
                quotas.append(q)
        for c in chunk:
            s = by_id.get(c["aweme_id"])
            if not s or not s.get("found"):
                excluded.append({**c, "reason": f"百应搜不到这个抖音号（{(s or {}).get('why') or '没有回答'}）"})
            elif s.get("status") != NOT_INVITED:
                excluded.append({**c, "reason": f"平台状态不是「邀约带货」：{s.get('button') or s.get('why') or s.get('status')}", "status": s.get("status")})
            else:
                chosen = assign_products(c.get("avg_price"), usable)
                entry = {**c, "status": s.get("status"), "button": s.get("button"), "weekly_quota": s.get("weeklyQuota"),
                         "products": {"main": chosen[0]["id"], "extras": [p["id"] for p in chosen[1:]]},
                         "content": cfg["templates"][c["category"]]}
                (picked if len(picked) < count else spare).append(entry)
    not_checked = candidates[checked:]
    order = {k: i for i, k in enumerate(CATEGORY_ORDER)}
    picked.sort(key=lambda c: order[c["category"]])
    return {
        "kind": "creator-invite-plan", "version": 1, "plan_id": make_plan_id(moment), "created_at": moment.isoformat(timespec="seconds"),
        "settings": {
            "commission_rate": cfg["commission_rate"], "rights": cfg["rights"],
            "contact": "微信" if cfg["invite_wechat"] else "手机号", "contact_masked": mask_contact(cfg["invite_wechat"] or cfg["invite_phone"]),
            "weekly_cap": cfg["weekly_cap"], "quota_margin": cfg["quota_margin"], "find": cfg["find"], "rules": rules, "min_stock": cfg["min_stock"],
        },
        "products": [{k: p[k] for k in ("id", "name", "price", "styles", "stock", "missing_styles", "in_stock")} for p in products],
        "weekly_quotas": quotas, "ledger_sent_this_week": ledger.sent_this_week(moment),
        "templates": cfg["templates"], "creators": picked, "spare": spare, "excluded": excluded,
        "not_checked": len(not_checked), "requests": clis.requests(),
        "douyin": {k: douyin[k] for k in ("found", "new", "looked_up", "not_looked_up", "stopped")} | {"both": len(douyin["both"])},
    }


def fans_range(q: dict) -> str:
    lo, hi = q.get("fans_min"), q.get("fans_max")
    if lo is not None and hi is not None:
        text = f"{lo}～{hi}"
    elif lo is not None:
        text = f"{lo} 以上"
    else:
        text = f"{hi} 以下" if hi is not None else "不限"
    if q.get("level_min") is not None or q.get("level_max") is not None:
        text += f"、带货等级 LV{q.get('level_min', 0)}～LV{q.get('level_max', 7)}"
    if q.get("sales_min") is not None:
        text += f"、近 30 天销售额 ≥ {q['sales_min']} 元"
    return text


def plan_markdown(plan: dict, plan_file: str) -> str:
    names = {p["id"]: p["name"] for p in plan["products"]}
    s = plan["settings"]
    out = io.StringIO()
    w = out.write
    w(f"# 达人邀约清单 {plan['plan_id']}\n\n")
    w(f"只读生成于 {plan['created_at']}，**还没有发任何邀约**。确认这份清单以后，才由用户明确同意执行：\n\n")
    w(f"```\npython3 tools/creator_invite/invite.py run --config <配置> --plan {plan_file} --confirm {plan['plan_id']}\n```\n\n")
    card = f"专属佣金 {s['commission_rate']}%（长期）" if s["commission_rate"] else "不开专属佣金（佣金到微信里谈）"
    w(f"- 卡片：{card}，权益 {'、'.join(RIGHT_NAMES.get(r, r) for r in s['rights']) or '一项都不勾'}，联系方式 {s['contact']} {s['contact_masked']}\n")
    quotas = "；".join(f"{q.get('levels')} 已用 {q.get('used')} / 上限 {q.get('limit')}" for q in plan["weekly_quotas"]) or "没读到"
    w(f"- 平台本周邀约次数（按达人等级分档，按钮浮层的原话读出）：{quotas}\n")
    w(f"- 我们自己的每周上限 {s['weekly_cap']}，台账里本周已发 {plan['ledger_sent_this_week']}；平台额度低于余量 {s['quota_margin']} 就停\n")
    f = s["find"]
    w(f"- 筛选：类目 {f['goods_category']}，件单价 {f['price_min']}～{f['price_max']} 元，没加进达人库；"
      + "；".join(f"粉丝 {fans_range(q)}，第 {q.get('page', 1)} 页起读 {q.get('pages', 1)} 页" for q in f["queries"]) + "\n")
    dy = plan.get("douyin") or {}
    if f.get("dy_queries"):
        w(f"- 抖音达人广场也找了（{len(f['dy_queries'])} 组搜索）：找到 {dy.get('found', 0)} 位，其中 {dy.get('both', 0)} 位聚水潭那边也找到了；"
          f"新的 {dy.get('new', 0)} 位按抖音号到聚水潭查了 {dy.get('looked_up', 0)} 位"
          + (f"，还有 {dy['not_looked_up']} 位没查" + ("" if dy.get("stopped") else f"（超过一次最多查的 {f.get('dy_lookup_max')} 位）")
             if dy.get("not_looked_up") else "") + (f"。**{dy['stopped']}**" if dy.get("stopped") else "") + "\n")
    req = plan.get("requests") or {}
    w(f"- 生成这份清单用了平台请求：聚水潭 {req.get('jst', 0)} 次、抖音 {req.get('dy', 0)} 次\n\n")
    w(f"## 商品（主推 1 款 + 其余{'有货的' if s['min_stock'] else '的'}都附带；"
      + (f"可用库存低于 {s['min_stock']} 的不配" if s["min_stock"] else "不看库存，能挂上卡的都配") + "）\n\n")
    w("| 商品 ID | 名字 | 售价 | 聚水潭款号 | 可用库存 | 配不配 |\n|---|---|---|---|---|---|\n")
    for p in plan["products"]:
        w(f"| {p['id']} | {p['name']} | ¥{p['price']:g} | {'、'.join(p['styles']) or '-'} | {'没查' if p['stock'] is None else p['stock']} | "
          f"{'配' if p['in_stock'] else '不配'} |\n")
    w("\n")
    for key in CATEGORY_ORDER:
        rows = [c for c in plan["creators"] if c["category"] == key]
        w(f"## {CATEGORY_NAMES[key]}（{len(rows)} 位）\n\n")
        if not rows:
            continue
        w("| # | 来源 | 抖音号 | 昵称 | 粉丝 | 近 30 天销售额 | 件单价 | 等级 | 主播类型 | 帽子占比 | 女粉 | 主推 / 附带 | 状态 | 提醒 |\n"
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
        for i, c in enumerate(rows, 1):
            prods = names.get(c["products"]["main"], c["products"]["main"]) + " / " + "、".join(names.get(x, x) for x in c["products"]["extras"])
            w(f"| {i} | {c.get('source') or '聚水潭'} | {c['aweme_id']} | {c['nickname']} | {c['fans']} | {c['sales30d'] or '-'} | {c['avg_price']} | LV{c['level']} | {c['channel'] or '-'} | "
              f"{'-' if c['hat_share'] is None else str(c['hat_share']) + '%'} | {'-' if c['female_share'] is None else str(c['female_share']) + '%'} | {prods} | "
              f"{c.get('button') or c.get('status')} | {'；'.join(c['flags'])} |\n")
        w("\n")
    w("## 话术（按类别，字数按命令行的算法）\n\n")
    for key in CATEGORY_ORDER:
        w(f"- **{CATEGORY_NAMES[key]}**（{js_length(plan['templates'][key])} 字）：{plan['templates'][key]}\n")
    suspects = [c for c in plan["excluded"] if c.get("suspect")]
    own = [c for c in plan["excluded"] if c.get("group") == "own_brand"]
    risky = [c for c in plan["excluded"] if c.get("group") == "sample_risk"]
    others = [c for c in plan["excluded"] if not c.get("suspect") and c.get("group") not in ("own_brand", "sample_risk")]
    w(f"\n## 疑似同行自播号（帽子占比 ≥ {plan['settings']['rules']['suspect_hat_share']:g}%，默认不进清单，{len(suspects)} 位）\n\n")
    excluded_table(w, suspects)
    w(f"\n## 专门带自己牌子的（名字像店铺 / 品牌号、店铺矩阵号，不进清单，{len(own)} 位；要留下哪位，把抖音号写进配置 rules.keep_ids）\n\n")
    excluded_table(w, own)
    w(f"\n## 可能骗样品的（带货等级低、卖得少、销售额不公开、粉多卖少，不进清单，{len(risky)} 位；要留下哪位，写进 rules.keep_ids）\n\n")
    excluded_table(w, risky)
    w(f"\n## 其他没进清单的（{len(others)} 位）\n\n")
    excluded_table(w, others)
    if plan.get("spare"):
        w(f"\n## 备选（查过状态、可以邀约，清单已够数，{len(plan['spare'])} 位）\n\n")
        w("| 类别 | 抖音号 | 昵称 | 粉丝 | 件单价 | 帽子占比 | 提醒 |\n|---|---|---|---|---|---|---|\n")
        for c in plan["spare"]:
            w(f"| {CATEGORY_NAMES[c['category']]} | {c['aweme_id']} | {c['nickname']} | {c['fans']} | {c['avg_price']} | "
              f"{'-' if c['hat_share'] is None else str(c['hat_share']) + '%'} | {'；'.join(c['flags'])} |\n")
    if plan.get("not_checked"):
        w(f"\n另有 {plan['not_checked']} 位符合条件、还没查平台状态（清单已够数）。\n")
    return out.getvalue()


def excluded_table(w, rows: List[dict]) -> None:
    if not rows:
        w("没有。\n")
        return
    w("| 来源 | 抖音号 | 昵称 | 粉丝 | 帽子占比 | 女粉 | 原因 |\n|---|---|---|---|---|---|---|\n")
    for c in rows:
        w(f"| {c.get('source') or '聚水潭'} | {c['aweme_id']} | {c['nickname']} | {c['fans']} | {'-' if c.get('hat_share') is None else str(c['hat_share']) + '%'} | "
          f"{'-' if c.get('female_share') is None else str(c['female_share']) + '%'} | {c['reason']} |\n")


def plan_csv(plan: dict) -> str:
    names = {p["id"]: p["name"] for p in plan["products"]}
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["类别", "来源", "抖音号", "昵称", "粉丝", "近30天销售额", "件单价", "等级", "主播类型", "帽子占比%", "女粉%", "主推", "附带", "状态", "提醒", "话术"])
    for c in plan["creators"]:
        writer.writerow([CATEGORY_NAMES[c["category"]], c.get("source") or "聚水潭", c["aweme_id"], c["nickname"], c["fans"], c["sales30d"], c["avg_price"], c["level"], c["channel"],
                         c["hat_share"], c["female_share"], names.get(c["products"]["main"]), "、".join(names.get(x, x) for x in c["products"]["extras"]),
                         c.get("button"), "；".join(c["flags"]), c["content"]])
    return out.getvalue()


def write_plan(plan: dict, out_dir: Path) -> Dict[str, Path]:
    folder = private_dir(out_dir)
    base = folder / f"plan-{plan['plan_id']}"
    paths = {"json": base.with_suffix(".json"), "md": base.with_suffix(".md"), "csv": base.with_suffix(".csv")}
    private_write(paths["json"], json.dumps(plan, ensure_ascii=False, indent=1) + "\n")
    private_write(paths["md"], plan_markdown(plan, str(paths["json"])))
    private_write(paths["csv"], "﻿" + plan_csv(plan))
    return paths


# ── run ──────────────────────────────────────────────────────────────────────

def invite_args(cfg: dict, plan: dict, creator: dict) -> List[str]:
    products = [creator["products"]["main"], *creator["products"]["extras"]]
    args = ["buyin", "author-invite", f"--aweme-id={creator['aweme_id']}", f"--product={','.join(products)}", f"--content={creator['content']}",
            f"--rights={','.join(plan['settings']['rights']) or 'none'}"]
    if plan["settings"]["commission_rate"]:                 # 0 = 卡上不开专属佣金
        args.append(f"--commission-rate={plan['settings']['commission_rate']}")
    if cfg["invite_wechat"]:
        args.append(f"--wechat={cfg['invite_wechat']}")
    if cfg["invite_phone"]:
        args.append(f"--phone={cfg['invite_phone']}")
    return args + [f"--expect-shop-id={cfg['dy_expect_shop_id']}", "--no-open"]


def load_plan(path: str) -> dict:
    try:
        plan = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ToolError(f"读不了清单 {path}：{e}", EXIT_USAGE)
    if not isinstance(plan, dict) or plan.get("kind") != "creator-invite-plan" or not isinstance(plan.get("creators"), list):
        raise ToolError(f"{path} 不是 plan 写的清单", EXIT_USAGE)
    return plan


def run_plan(cfg: dict, plan: dict, confirm: str, clis: Clis, ledger: Ledger, *, limit: Optional[int] = None,
             sleep: Callable = time.sleep, uniform: Callable = random.uniform, clock: Callable = now_bjt) -> dict:
    if confirm != plan.get("plan_id"):
        raise ToolError(f"--confirm 要写这份清单的编号 {plan.get('plan_id')}（用户同意的是哪一份，就写哪一份）", EXIT_USAGE)
    try:
        created = datetime.fromisoformat(str(plan.get("created_at")))
    except ValueError:
        raise ToolError("清单没有生成时间", EXIT_USAGE)
    if clock() - created > timedelta(days=cfg["plan_max_age_days"]):
        raise ToolError(f"清单是 {plan.get('created_at')} 生成的，超过 {cfg['plan_max_age_days']:g} 天：重新 plan、重新确认", EXIT_USAGE)
    s = plan.get("settings") or {}
    if s.get("commission_rate") != cfg["commission_rate"] or s.get("rights") != cfg["rights"]:
        raise ToolError("清单里的专属佣金或权益和现在的配置不一样：重新 plan、重新确认", EXIT_USAGE)
    summary = {"plan_id": plan["plan_id"], "sent": [], "skipped": [], "conflicts": [], "unknown_before": ledger.unknown(), "stopped": None}
    ledger.append({"event": "run-start", "plan_id": plan["plan_id"]})
    previews_failed = 0
    first = True
    for index, creator in enumerate(plan["creators"]):
        aweme = creator.get("aweme_id")
        if limit is not None and len(summary["sent"]) >= limit:
            summary["stopped"] = {"why": f"这次只发 {limit} 位（--limit）", "at": index}
            break
        if not isinstance(aweme, str) or not AWEME_ID.match(aweme):
            summary["skipped"].append({"aweme_id": aweme, "why": "抖音号格式不对"})
            continue
        if aweme in ledger.never_again():
            summary["skipped"].append({"aweme_id": aweme, "why": "台账里已经发过（或不知道发出去没有）：绝不再发"})
            continue
        if aweme in cfg["rules"]["drop_ids"]:                # 清单出了以后用户又说不要的
            summary["skipped"].append({"aweme_id": aweme, "why": "手动排除（配置 rules.drop_ids）"})
            continue
        week = ledger.sent_this_week(clock())
        if week >= cfg["weekly_cap"]:
            summary["stopped"] = {"why": f"我们自己的每周上限 {cfg['weekly_cap']} 到了（本周已发 {week}）", "at": index}
            break
        today = ledger.sent_today(clock())
        if today >= cfg["daily_cap"]:
            summary["stopped"] = {"why": f"每天上限 {cfg['daily_cap']} 到了（今天已发 {today}）：明天接着发", "at": index}
            break
        if not first:
            sleep(uniform(*cfg["wait_between"]))
        first = False
        args = invite_args(cfg, plan, creator)
        preview = clis.run("dy", args, 600)
        if preview.exit_code in STOP_EXITS:
            summary["stopped"] = {"why": f"预览 {aweme}：{STOP_EXITS[preview.exit_code]}", "at": index, "exit": preview.exit_code}
            ledger.append({"event": "stop", "plan_id": plan["plan_id"], "aweme_id": aweme, "phase": "preview", "exit": preview.exit_code})
            break
        if preview.unverifiable():
            summary["stopped"] = {"why": f"预览 {aweme}：{LOGIN_HINT}（{preview.said()}）", "at": index, "exit": EXIT_FAILED}
            ledger.append({"event": "stop", "plan_id": plan["plan_id"], "aweme_id": aweme, "phase": "preview", "exit": preview.exit_code})
            break
        item = ((preview.data or {}).get("items") or [{}])[0] if preview.data else {}
        if preview.exit_code != 0 or not item:
            previews_failed += 1
            summary["skipped"].append({"aweme_id": aweme, "why": f"预览失败（退出码 {preview.exit_code}）：{preview.said()}"})
            ledger.append({"event": "skip", "plan_id": plan["plan_id"], "aweme_id": aweme, "why": "preview failed", "exit": preview.exit_code})
            if previews_failed >= 3:
                summary["stopped"] = {"why": "连续 3 位预览失败：停下查原因", "at": index}
                break
            continue
        previews_failed = 0
        status = (preview.data.get("statusBefore") or {}).get("status")
        gate = (preview.data.get("gate") or {}).get("popwindowType")
        if item.get("blocked") or item.get("outcome") != "PREVIEW" or status != NOT_INVITED or gate != GATE_OPEN:
            why = item.get("detail") if item.get("blocked") else f"状态 {status}、门槛 {gate}（只发 状态 1、门槛 -1 的）"
            summary["skipped"].append({"aweme_id": aweme, "why": str(why)[:300]})
            ledger.append({"event": "skip", "plan_id": plan["plan_id"], "aweme_id": aweme, "why": str(why)[:300]})
            continue
        quota = (preview.data.get("statusBefore") or {}).get("weeklyQuota")
        if isinstance(quota, dict) and isinstance(quota.get("used"), int) and isinstance(quota.get("limit"), int):
            left = quota["limit"] - quota["used"]
            if left < cfg["quota_margin"]:
                summary["stopped"] = {"why": f"平台本周 {quota.get('levels')} 只剩 {left} 次（余量 {cfg['quota_margin']}）", "at": index}
                ledger.append({"event": "stop", "plan_id": plan["plan_id"], "aweme_id": aweme, "phase": "quota", "left": left})
                break
        attempt = secrets.token_hex(6)
        # what the preview said of the card: each product's sample (免费申样 / 买样) and the CLI's warnings (免费申样 ticked for a 买样 product …)
        samples = {str(p.get("productId")): p.get("sample") for p in preview.data.get("products") or [] if isinstance(p, dict) and p.get("productId")}
        warnings = [str(x)[:300] for x in preview.data.get("warnings") or []][:5]
        ledger.append({"event": "attempt", "attempt": attempt, "plan_id": plan["plan_id"], "aweme_id": aweme,
                       "products": [creator["products"]["main"], *creator["products"]["extras"]], "commission_rate": plan["settings"]["commission_rate"],
                       "rights": plan["settings"]["rights"], "samples": samples, "warnings": warnings, "category": creator.get("category")})
        done = clis.run("dy", [*args, "--expect-status=1", "--expect-popwindow=-1", "--execute"], 900)
        outcome = (((done.data or {}).get("items") or [{}])[0] or {}).get("outcome") if done.data else None
        writes = (done.data or {}).get("platformWrites") if done.data else None
        if done.unverifiable() and not done.timed_out:
            sent = False                                  # the login check failed before the card's own request: nothing went out
        elif done.data is None or done.timed_out:
            sent = None                                   # no answer: not known — never sent again
        elif outcome in SENT_OUTCOMES or (isinstance(writes, int) and writes > 0):
            sent = True
        else:
            sent = False                                  # STATE_CONFLICT, FAILED before the send …: nothing went out
        ledger.append({"event": "result", "attempt": attempt, "plan_id": plan["plan_id"], "aweme_id": aweme, "exit": done.exit_code,
                       "outcome": outcome, "platform_writes": writes, "sent": sent, "timed_out": done.timed_out})
        entry = {"aweme_id": aweme, "outcome": outcome, "exit": done.exit_code, "sent": sent, **({"warnings": warnings} if warnings else {})}
        if done.exit_code in STOP_EXITS:
            (summary["sent"] if sent else summary["skipped"]).append(entry)
            summary["stopped"] = {"why": f"发送 {aweme}：{STOP_EXITS[done.exit_code]}", "at": index, "exit": done.exit_code}
            break
        if done.unverifiable() and not done.timed_out:
            summary["skipped"].append({**entry, "why": "没发出去：发送前核对登录没过"})
            summary["stopped"] = {"why": f"发送 {aweme}：{LOGIN_HINT}（{done.said()}）。这位没发出去，重跑会再发她", "at": index, "exit": EXIT_FAILED}
            break
        if done.exit_code == EXIT_INTERRUPTED or done.timed_out or sent is None:
            summary["stopped"] = {"why": f"发送 {aweme} 的结果不知道（退出码 {done.exit_code}{'，超时' if done.timed_out else ''}）：先到百应里看，不会重发", "at": index}
            break
        if outcome == "STATE_CONFLICT":
            summary["conflicts"].append(entry)
            continue
        (summary["sent"] if sent else summary["skipped"]).append(entry)
    ledger.append({"event": "run-end", "plan_id": plan["plan_id"], "sent": len(summary["sent"]), "stopped": (summary["stopped"] or {}).get("why")})
    summary["sent_this_week"] = ledger.sent_this_week(clock())
    summary["requests"] = clis.requests()
    return summary


def run_markdown(summary: dict) -> str:
    out = io.StringIO()
    w = out.write
    w(f"# 邀约执行记录 {summary['plan_id']}\n\n")
    w(f"- 发出去 {len(summary['sent'])} 位；跳过 {len(summary['skipped'])} 位；STATE_CONFLICT {len(summary['conflicts'])} 位\n")
    w(f"- 本周已发（台账）：{summary['sent_this_week']}\n")
    if summary["stopped"]:
        w(f"- **停在清单第 {summary['stopped'].get('at', 0) + 1} 位**：{summary['stopped']['why']}\n")
    if summary["unknown_before"]:
        w(f"- 上次有 {len(summary['unknown_before'])} 位不知道发出去没有（不会重发，到百应 › 消息里看）：{'、'.join(summary['unknown_before'])}\n")
    for title, key in (("发出去的", "sent"), ("跳过的", "skipped"), ("STATE_CONFLICT（没发，下次 plan 会重新查）", "conflicts")):
        if summary[key]:
            w(f"\n## {title}\n\n")
            for x in summary[key]:
                w(f"- {x.get('aweme_id')}：{x.get('outcome') or ''} {x.get('why') or ''}"
                  + (f"（提醒：{'；'.join(x['warnings'])}）" if x.get("warnings") else "") + "\n")
    return out.getvalue()


# ── command line ─────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None, *, runner: Callable = subprocess.run, sleep: Callable = time.sleep,
         uniform: Callable = random.uniform, clock: Callable = now_bjt, stdout=None) -> int:
    stdout = stdout or sys.stdout
    parser = argparse.ArgumentParser(prog="invite.py", description="达人广场「邀约带货」两段式批量：plan（只读出清单）→ 用户确认 → run（只发清单里的人）")
    sub = parser.add_subparsers(dest="command", required=True)
    p_plan = sub.add_parser("plan", help="只读：找达人、查状态、配话术和商品，写清单（不发任何邀约）")
    p_plan.add_argument("--config", required=True, help="配置文件（JSON，见 config.example.json）")
    p_plan.add_argument("--count", type=int, default=30, help="清单最多几位（30～100 之间常用；1 到 100）")
    p_plan.add_argument("--out-dir", help="清单写到哪（默认 <home>/plans）")
    p_plan.add_argument("--ledger-dir", help="台账目录（默认 <home>）")
    p_run = sub.add_parser("run", help="执行：只发清单里的人（要 --confirm 清单编号）")
    p_run.add_argument("--config", required=True)
    p_run.add_argument("--plan", required=True, help="plan 写的 JSON 清单")
    p_run.add_argument("--confirm", required=True, help="清单编号（用户同意的那一份的编号）")
    p_run.add_argument("--limit", type=int, help="这次最多发几位")
    p_run.add_argument("--ledger-dir", help="台账目录（默认 <home>）")
    p_status = sub.add_parser("status", help="看台账：本周发了几位、哪些不知道发出去没有")
    p_status.add_argument("--config", required=True)
    p_status.add_argument("--ledger-dir")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        ledger = Ledger(Path(args.ledger_dir).expanduser() if args.ledger_dir else cfg["home"])
        if args.command == "status":
            moment = clock()
            stdout.write(json.dumps({"ledger": str(ledger.path), "sent_this_week": ledger.sent_this_week(moment), "weekly_cap": cfg["weekly_cap"],
                                     "never_again": len(ledger.never_again()), "unknown": ledger.unknown()}, ensure_ascii=False, indent=1) + "\n")
            return EXIT_OK
        check_clis(cfg)
        if args.command == "plan":
            if not 1 <= args.count <= 100:
                raise ToolError("--count 在 1 到 100 之间", EXIT_USAGE)
            moment = clock()
            out_dir = Path(args.out_dir).expanduser() if args.out_dir else cfg["home"] / "plans"
            clis = Clis(cfg, private_dir(out_dir) / f"work-{moment.strftime('%Y%m%d-%H%M%S')}", runner=runner, stamp=ledger.folder / "last-request")
            plan = build_plan(cfg, clis, ledger, args.count, moment=moment)
            paths = write_plan(plan, out_dir)
            by_cat = {CATEGORY_NAMES[k]: sum(1 for c in plan["creators"] if c["category"] == k) for k in CATEGORY_ORDER}
            stdout.write(json.dumps({"plan_id": plan["plan_id"], "files": {k: str(v) for k, v in paths.items()}, "creators": len(plan["creators"]),
                                     "by_category": by_cat, "excluded": len(plan["excluded"]), "requests": plan["requests"],
                                     "next": f"用户确认后：python3 tools/creator_invite/invite.py run --config <配置> --plan {paths['json']} --confirm {plan['plan_id']}"},
                                    ensure_ascii=False, indent=1) + "\n")
            return EXIT_OK
        plan = load_plan(args.plan)
        if args.limit is not None and args.limit < 1:
            raise ToolError("--limit 至少 1", EXIT_USAGE)
        ledger.lock()
        try:
            clis = Clis(cfg, ledger.folder / f"run-{clock().strftime('%Y%m%d-%H%M%S')}", runner=runner, stamp=ledger.folder / "last-request")
            summary = run_plan(cfg, plan, args.confirm, clis, ledger, limit=args.limit, sleep=sleep, uniform=uniform, clock=clock)
        finally:
            ledger.unlock()
        private_write(clis.workdir / "report.md", run_markdown(summary))
        stdout.write(json.dumps(summary, ensure_ascii=False, indent=1) + "\n")
        exit_code = (summary["stopped"] or {}).get("exit")
        return exit_code if exit_code in STOP_EXITS or exit_code == EXIT_FAILED else EXIT_OK
    except ToolError as e:
        sys.stderr.write(f"[{'STOP' if isinstance(e, StopRun) else 'ERROR'}] {e}\n")
        return e.exit_code


if __name__ == "__main__":
    sys.exit(main())
