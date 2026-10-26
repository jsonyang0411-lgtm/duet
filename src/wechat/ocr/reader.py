"""Reading a WeChat window from OCR rows (the OCR fallback's platform-neutral layer).

Input is what an OCR engine returns for a screenshot of the WeChat window: one row
per text block, ``{"t": text, "x0", "x1", "y0", "y1": 0..1 from the top left,
"bg": [r, g, b] of the pixels left of the text}``.  From the rows this module finds
the chat area, tells the other side's bubbles from one's own by their colour, merges
lines into messages and works out which messages are new.  No screenshots, no
engine: ``mac_vision.py`` is the macOS engine, another platform only has to produce
the same rows.

``bubble_kind`` ... ``MessageDeduplicator`` come from the user's wechat-ocr-kit
(extracted from wechat-ai-agent) unchanged; ``NewMessageTracker`` and ``read_rows``
are new.
"""
from __future__ import annotations

import re
import time
from collections import deque
from difflib import SequenceMatcher
from typing import NamedTuple


# ---------------- bubbles, geometry, titles, messages (from the kit) ----------------

PLAIN_TOLERANCE = 4      # colour distance up to which two backgrounds are "the same"


def bubble_kind(bg: list[int], plain: tuple | list = ()) -> str:
    """``self`` (green bubble), ``other`` (white / dark grey bubble) or ``none``.

    ``plain`` are the colours of the chat's own background (see ``plain_backgrounds``): text drawn
    directly on it (a time line, the name above a bubble, a system notice) is not a bubble.  The kit
    called every near-white colour a bubble, which is wrong where the chat background itself is
    near-white (WeChat 4.x light mode: 251,251,251).
    """
    r, g, b = bg
    if any(max(abs(r - p[0]), abs(g - p[1]), abs(b - p[2])) <= PLAIN_TOLERANCE for p in plain):
        return "none"
    if g > 150 and g - r > 50:
        return "self"      # 绿色气泡：自己发的
    if 50 <= r <= 80 and abs(r - g) < 8 and abs(g - b) < 8:
        return "other"     # 深灰气泡：别人发的（深色模式）
    if r > 235 and g > 235 and b > 235:
        return "other"     # 白色气泡：别人发的（浅色模式）
    return "none"


class ChatGeometry(NamedTuple):
    chat_x0: float   # 聊天区域左边界 (0.0 ~ 1.0)
    chat_x1: float   # 聊天区域右边界 (0.0 ~ 1.0)
    chat_y0: float   # 聊天区域上边界 (0.0 ~ 1.0)
    chat_y1: float   # 聊天区域下边界 (0.0 ~ 1.0)


def clean_group_name(name: str) -> str:
    """清洗会话/群聊名称，自动去除末尾的人数统计后缀（如 `（2）`, `(100)`, `(32人)` 等）。"""
    if not name:
        return ""
    return re.sub(r"[\(（]\s*\d+\s*(?:人)?\s*[\)）]\s*$", "", name).strip()


class CleanedTitle(str):
    """自适应会话标题字符串：既能保留原始带后缀标题，又能与清洗后的群名或原始群名双向兼容匹配。"""
    @property
    def cleaned(self) -> str:
        return clean_group_name(self)

    def __eq__(self, other: object) -> bool:
        if super().__eq__(other):
            return True
        if isinstance(other, str):
            return clean_group_name(self) == clean_group_name(other) or self == clean_group_name(other)
        return False

    def __hash__(self) -> int:
        return super().__hash__()

    def startswith(self, prefix: str | tuple[str, ...], start: int | None = None, end: int | None = None) -> bool:
        if super().startswith(prefix, start, end):
            return True
        if isinstance(prefix, str):
            c_prefix = clean_group_name(prefix)
            c_self = clean_group_name(self)
            if start is None and end is None:
                return c_self.startswith(c_prefix)
        return False


def _is_search_box(text: str) -> bool:
    """The search field's placeholder: "搜索" / "Search", also when the magnifier is read as a letter ("Q 搜索")."""
    t = text.strip().lower()
    if t.startswith(("search", "搜索")):
        return True
    return t.lstrip("0123456789q⌕🔍oO ").strip() in ("搜索", "search")


FUNC_BUTTONS = (
    "搜索", "Search", "search", "*", "•", "X", "x", "+", "十", "＋", "...", "…",
    "取消", "Cancel", "cancel", "O", "o", "0"
)


def resolve_chat_geometry(rows: list[dict], img_size: tuple[int, int] | None = None) -> ChatGeometry:
    """自适应几何解析器：动态推导微信聊天区域边界。
    适配超高大屏 (H >= 1200)、超宽屏 (W >= 1600)、超窄窗 (W <= 650) 与侧边抽屉展开/折叠。
    彻底废除固定相对百分比硬编码，精准防护浅色模式输入框文字防误吞。
    """
    if not rows:
        return ChatGeometry(0.08, 0.98, 0.065, 0.88)

    # 0. 估算正文单行文本相对高度，用于输入区域物理尺寸无量纲推导
    line_heights = [r["y1"] - r["y0"] for r in rows if 0.002 < (r["y1"] - r["y0"]) < 0.10]
    if line_heights:
        sorted_h = sorted(line_heights)
        median_line_h = sorted_h[len(sorted_h) // 2]
    else:
        median_line_h = 0.025

    # 1. 动态确定右侧边界 chat_x1（检测是否展开搜一搜、小程序、聊天记录等侧边抽屉面板）
    panel_keywords = ("搜一搜", "小程序", "相关聊天记录", "微信文件传输助手")
    has_right_panel = any(
        r["x0"] > 0.50 and (
            (r["y0"] < 0.08 and any(k in r["t"] for k in panel_keywords))
            or (r["y0"] < 0.08 and "搜一搜" in r["t"])
        )
        for r in rows
    )
    if has_right_panel:
        panel_rows = [
            r for r in rows
            if r["x0"] > 0.50 and (
                any(k in r["t"] for k in panel_keywords)
                or (r["y0"] < 0.08 and r["t"] in ("*", "X", "x", "关闭"))
            )
        ]
        min_panel_x0 = min((r["x0"] for r in panel_rows), default=0.62)
        chat_x1 = max(0.50, min(0.98, min_panel_x0 - 0.01))
    else:
        chat_x1 = 0.98

    # 2. 动态确定左侧边界 chat_x0（适配超宽屏 W>=3000px，放宽左边界下限至 0.04）
    search_rows = [
        r for r in rows
        if r["y0"] < 0.12 and r["x0"] < 0.40 and ("搜索" in r["t"] or "search" in r["t"].lower())
    ]
    search_x1 = max((r["x1"] for r in search_rows), default=0.0)
    # Without the search box (an independent chat window) there is no session list: the title
    # starts near the left edge, so the left limits below drop from 0.04 to 0.
    left_limit = 0.04 if search_rows else 0.0
    title_cands = [
        r for r in rows
        if r["y0"] < 0.08 and (r["x0"] > search_x1 if search_rows else r["x0"] >= 0.0) and r["x0"] < chat_x1
        and r["t"] not in FUNC_BUTTONS
        and not _is_search_box(r["t"])
        and not any(k in r["t"] for k in ("搜一搜", "小程序", "相关聊天记录"))
    ]

    if search_rows:
        if title_cands:
            first_title_x0 = min(r["x0"] for r in title_cands)
            session_items = [
                r for r in rows
                if r["y0"] >= 0.06 and r["x0"] < first_title_x0 and r["x1"] <= first_title_x0 + 0.005
                and bubble_kind(r["bg"]) == "none"
            ]
            left_bound = max(search_x1, max((r["x1"] for r in session_items), default=search_x1))
            chat_x0 = (left_bound + first_title_x0) / 2.0
            chat_x0 = max(0.04, min(first_title_x0 - 0.005, chat_x0))
        else:
            bubble_x0s = [r["x0"] for r in rows if bubble_kind(r["bg"]) in ("self", "other") and r["y0"] > 0.06]
            if bubble_x0s:
                min_bx = min(bubble_x0s)
                chat_x0 = max(0.04, min(search_x1 * 1.01, min_bx - 0.01))
            else:
                chat_x0 = max(0.05, min(0.35, search_x1 * 1.15))
    else:
        # 无搜索框时的超宽屏/窄窗推导
        bubble_x0s = [r["x0"] for r in rows if bubble_kind(r["bg"]) in ("self", "other") and r["y0"] > 0.06]
        if title_cands and bubble_x0s:
            min_ref = min(min(r["x0"] for r in title_cands), min(bubble_x0s))
            chat_x0 = max(left_limit, min_ref - 0.015)
        elif bubble_x0s:
            chat_x0 = max(left_limit, min(bubble_x0s) - 0.015)
        elif title_cands:
            chat_x0 = max(left_limit, min(r["x0"] for r in title_cands) - 0.015)
        else:
            chat_x0 = 0.08

    # 3. 动态确定顶部边界 chat_y0
    chat_y0 = 0.065

    # 4. 动态确定底部边界 chat_y1（结合文本行高自适应模型与视觉锚点）
    # 基于行高模型推导输入框理论安全顶部（微信输入框约占 7.2 个单行文本高度）
    est_input_top = min(0.96, max(0.68, 1.0 - 7.2 * median_line_h))

    # 4.1 查找工具栏分割标记（基于明确视觉特征词，严禁包含发送按钮或无底色时间戳）
    toolbar_keywords = ("表情", "发送文件", "截图", "聊天记录")
    toolbar_rows = [
        r for r in rows
        if chat_x0 < r["x0"] < chat_x1
        and 0.50 <= r["y0"] <= 0.97
        and any(k in r["t"] for k in toolbar_keywords)
    ]

    # 4.2 查找右下角发送按钮锚点
    send_buttons = [
        r for r in rows
        if chat_x0 < r["x0"] < chat_x1
        and r["y0"] >= 0.65
        and any(k in r["t"] for k in ("发送", "发送(S)", "发送（S）", "Enter", "Send"))
    ]

    if toolbar_rows:
        toolbar_top = min(r["y0"] for r in toolbar_rows)
        chat_y1 = toolbar_top
    elif send_buttons:
        send_y0 = min(r["y0"] for r in send_buttons)
        # 发送按钮上方约 5.0 个行高处为输入框顶部
        chat_y1 = min(est_input_top, send_y0 - max(0.04, 5.0 * median_line_h))
    else:
        chat_y1 = est_input_top

    # 4.3 检查有效消息气泡（严格物理隔离：气泡必须位于 chat_y1 之上）
    valid_bubbles = [
        r for r in rows
        if chat_x0 < r["x0"] < chat_x1
        and bubble_kind(r["bg"]) in ("self", "other")
        and r["y1"] <= (chat_y1 + 0.015)
    ]
    if valid_bubbles:
        highest_bubble_bottom = max(r["y1"] for r in valid_bubbles)
        if highest_bubble_bottom > chat_y1:
            if not toolbar_rows or (toolbar_top - highest_bubble_bottom >= -0.01):
                chat_y1 = min(0.97, highest_bubble_bottom + 0.005)

    return ChatGeometry(chat_x0, chat_x1, chat_y0, chat_y1)


def get_chat_bounds(rows: list[dict], img_size: tuple[int, int] | None = None) -> tuple[float, float, float, float]:
    """向后兼容接口：获取聊天区域边界 (chat_x0, chat_x1, chat_y0, chat_y1)。"""
    return resolve_chat_geometry(rows, img_size=img_size)


def _title_rows(rows: list[dict], geom: ChatGeometry) -> list[dict]:
    """The text blocks of the title bar, left to right."""
    chat_x0, chat_x1 = geom.chat_x0, geom.chat_x1
    # 顶部右侧按钮右边界保护：群名绝不能跨越右侧功能按钮
    right_btns = [
        r["x0"] for r in rows
        if r["y0"] < 0.08 and r["x0"] > chat_x0 and r["t"] in ("*", "•", "X", "x", "...", "…", "0", "O", "o")
    ]
    title_x1 = min(right_btns) if right_btns else chat_x1

    # 标题通常位于顶部栏（y0 < 0.08），水平位于会话列表以右、右侧搜索/面板以左（chat_x0 <= x0 < title_x1）
    top = [
        r for r in rows
        if chat_x0 <= r["x0"] < title_x1
        and r["y0"] < 0.08
        and r["t"] not in FUNC_BUTTONS
        and not _is_search_box(r["t"])
        and not any(k in r["t"] for k in ("搜一搜", "小程序", "相关聊天记录"))
    ]
    top.sort(key=lambda r: r["x0"])
    return top


def _title_parts(rows: list[dict], geom: ChatGeometry) -> list[dict]:
    """The text blocks that make up the title: the leftmost block of the title bar and the neighbours on
    the same line (处理群名包含空格、人数括号或被 OCR 切分为多个词块的情况).  A message scrolled up under the
    title bar is in the band too, but it is not on the title's line."""
    top = _title_rows(rows, geom)
    if not top:
        return []
    parts = [top[0]]
    for r in top[1:]:
        last = parts[-1]
        if r["x0"] - last["x1"] < 0.05 and abs(r["y0"] - last["y0"]) < 0.03:
            parts.append(r)
        else:
            break
    return parts


def chat_title(rows: list[dict], geometry: ChatGeometry | None = None) -> str:
    """提取当前微信主聊天窗口的会话标题（群名或联系人名），自适应任意窗口宽度并支持群名称清洗与相邻切片合并。"""
    if not rows:
        return ""
    geom = geometry or resolve_chat_geometry(rows)
    parts = _title_parts(rows, geom)
    if not parts:
        return CleanedTitle("")
    return CleanedTitle("".join(r["t"] for r in parts))


_CLOCK = re.compile(
    r"^(?:(?:昨天|今天|前天|星期[一二三四五六日天]|周[一二三四五六日天]|(?:\d{4}年)?\d{1,2}月\d{1,2}日)\s*)?"
    r"\d{1,2}:\d{2}(?::\d{2})?$"
)


def is_clock_text(text: str) -> bool:
    """A time line between messages ("13:01", "昨天 12:30", "10月21日 08:00") or a video's length ("0:25")."""
    return bool(_CLOCK.match(text.strip().rstrip(".。·,，")))      # OCR adds a dot now and then: "15:08."


def plain_backgrounds(rows: list[dict], geometry: ChatGeometry | None = None) -> list[list[int]]:
    """The colours text is drawn on where it is not in a bubble: under the title and under the time lines."""
    if not rows:
        return []
    geom = geometry or resolve_chat_geometry(rows)
    chat_x0, chat_x1, chat_y0, chat_y1 = geom
    colours = [r["bg"] for r in _title_parts(rows, geom)]
    colours += [
        r["bg"] for r in rows
        if chat_x0 < r["x0"] < chat_x1 and chat_y0 < r["y0"] < chat_y1 and is_clock_text(r["t"])
    ]
    unique: list[list[int]] = []
    for colour in colours:
        if colour not in unique:
            unique.append(list(colour))
    return unique


def other_messages(rows: list[dict], want: str = "other", geometry: ChatGeometry | None = None) -> list[str]:
    """把聊天区里某一方（other=别人，self=自己）发的行按气泡合并成消息（同一气泡的相邻行合并）。"""
    if not rows:
        return []
    geom = geometry or resolve_chat_geometry(rows)
    chat_x0, chat_x1, chat_y0, chat_y1 = geom
    plain = plain_backgrounds(rows, geom)

    lines = [r for r in rows if chat_x0 < r["x0"] < chat_x1 and chat_y0 < r["y0"] < chat_y1]
    lines.sort(key=lambda r: r["y0"])
    msgs, cur, last = [], None, None
    for r in lines:
        kind = "none" if is_clock_text(r["t"]) else bubble_kind(r["bg"], plain)
        if kind != want:
            if cur:
                msgs.append(cur)
                cur = None
            continue
        if cur and last and (r["y0"] - last["y1"] < 0.035) and (abs(r["x0"] - last["x0"]) < 0.05):
            cur += r["t"]
        else:
            if cur:
                msgs.append(cur)
            cur = r["t"]
        last = r
    if cur:
        msgs.append(cur)
    return msgs


def messages_in_order(rows: list[dict], geometry: ChatGeometry | None = None) -> list[tuple[str, str]]:
    """Every bubble and time line of the chat area, top to bottom: ``[(kind, text)]`` with kind
    ``other``, ``self`` or ``time``.  Bubbles are merged line by line the way ``other_messages`` does."""
    if not rows:
        return []
    geom = geometry or resolve_chat_geometry(rows)
    chat_x0, chat_x1, chat_y0, chat_y1 = geom
    plain = plain_backgrounds(rows, geom)
    lines = [r for r in rows if chat_x0 < r["x0"] < chat_x1 and chat_y0 < r["y0"] < chat_y1]
    lines.sort(key=lambda r: r["y0"])
    out: list[tuple[str, str]] = []
    cur_kind, cur_text, last = None, "", None
    for r in lines:
        kind = "time" if is_clock_text(r["t"]) else bubble_kind(r["bg"], plain)
        if kind == "none":
            if cur_kind:
                out.append((cur_kind, cur_text))
            cur_kind, cur_text, last = None, "", None
            continue
        if (kind == cur_kind and kind != "time" and last is not None
                and r["y0"] - last["y1"] < 0.035 and abs(r["x0"] - last["x0"]) < 0.05):
            cur_text += r["t"]
        else:
            if cur_kind:
                out.append((cur_kind, cur_text))
            cur_kind, cur_text = kind, r["t"]
        last = r
    if cur_kind:
        out.append((cur_kind, cur_text))
    return out


# ---------------- normalizing and de-duplicating (from the kit) ----------------

def normalize_text(text: str) -> str:
    """去除所有标点、空白，统一转小写，消除 Vision OCR 在中英文间随机插入空格或中英标点差异的影响。"""
    t = text.lower()
    return re.sub(r'[\s\W_]+', '', t, flags=re.UNICODE)


class MessageDeduplicator:
    """消息去重与防抖控制器：
    1. 维护最近 2000 个处理过的消息指纹（MD5 + 归一化文本 MD5 + 提取问题 MD5），采用滑动窗口淘汰，绝不盲目 clear()
    2. 维护最近问答缓存（归一化问题 -> 时间戳），对短时间内重复出现的相同提问进行防抖拦截
    3. 自动识别并过滤机器人自身回复，防止自触发
    """
    def __init__(self, max_size: int = 2000, cooldown_seconds: float = 60.0):
        self.max_size = max_size
        self.cooldown_seconds = cooldown_seconds
        self._hashes: set[str] = set()
        self._queue: deque[str] = deque()
        self._recent_questions: dict[str, float] = {}

    def is_handled(self, *hash_keys: str) -> bool:
        for k in hash_keys:
            if k and k in self._hashes:
                return True
        return False

    def add(self, *hash_keys: str):
        for k in hash_keys:
            if not k:
                continue
            if k not in self._hashes:
                self._hashes.add(k)
                self._queue.append(k)
                if len(self._queue) > self.max_size:
                    old = self._queue.popleft()
                    self._hashes.discard(old)

    def is_recent_question(self, norm_q: str) -> bool:
        if not norm_q:
            return False
        now = time.time()
        # 清理超过 15 分钟的过期记录
        self._recent_questions = {k: ts for k, ts in self._recent_questions.items() if now - ts < 900}
        last_ts = self._recent_questions.get(norm_q)
        if last_ts and (now - last_ts < self.cooldown_seconds):
            return True
        return False

    def record_question(self, norm_q: str):
        if norm_q:
            self._recent_questions[norm_q] = time.time()


# ---------------- new messages ----------------

def _same(a: str, b: str) -> bool:
    """Two normalized texts that OCR may have read slightly differently."""
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.85


def _cut_off(a: str, b: str) -> bool:
    """One text is the end of the other: the window's top edge cut the beginning off a message."""
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= 3 and longer.endswith(shorter)


def _same_item(a: tuple[str, str], b: tuple[str, str]) -> bool:
    return a[0] == b[0] and _same(a[1], b[1])


def _cut_off_item(a: tuple[str, str], b: tuple[str, str]) -> bool:
    return a[0] == b[0] and _cut_off(a[1], b[1])


def _overlap(prev: list, cur: list) -> int:
    """The longest k such that the last k of ``prev`` are the first k of ``cur``.

    The screen is a window onto the chat: a new message pushes the old ones up, so
    what was at the bottom last time is at the top now.  The topmost message may be
    cut off by the window's edge (a file card scrolled up shows only its last line),
    so it only has to be the end of the remembered one.
    """
    for k in range(min(len(prev), len(cur)), 0, -1):
        tail = prev[len(prev) - k:]
        head = cur[:k]
        if not (_same_item(tail[0], head[0]) or _cut_off_item(tail[0], head[0])):
            continue
        if all(_same_item(p, c) for p, c in zip(tail[1:], head[1:])):
            return k
    return 0


class NewMessageTracker:
    """Which of the other side's messages on screen are new.

    It remembers what it has seen in the order it came (``HISTORY`` items): the other
    side's bubbles, the account's own bubbles and the time lines (``messages_in_order``).
    Own bubbles and time lines are only landmarks, never reported, but without them a
    run of identical messages ("@bot hi" five times) looks the same before and after a
    new one arrives (found in a live test).  Plain strings count as the other side's.  The screen is a window onto the chat: a new message pushes the old ones
    up, so the end of the remembered messages is the start of the screen, and what
    follows that overlap is new.  Scrolling does not change what is remembered, so
    scrolling up to read and back down reports nothing twice.

    The first look at a chat, and a look at a different chat, only fill the memory
    (what is on screen is history).  A screen that does not continue the memory is
    not reported at all: somebody scrolled up, or more than a screenful arrived at
    once, and reporting old messages that scrolled into view would make the bot
    answer them.  If the same unrelated screen stays for ``REBASE_AFTER`` looks it
    becomes the new starting point, so the tracker cannot get stuck.
    """

    HISTORY = 100
    REBASE_AFTER = 3

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._title: str | None = None
        self._history: list[str] | None = None
        self._lost = 0
        self._lost_screen: list[str] | None = None

    def feed(self, title: str, items: list) -> list[str]:
        """``items``: ``messages_in_order`` output, or plain strings (the other side's messages)."""
        key = clean_group_name(title)
        pairs = []
        for item in items:
            kind, text = ("other", item) if isinstance(item, str) else item
            text = str(text or "").strip()
            norm = normalize_text(text)
            if norm:
                pairs.append((text, (kind, norm)))
        norm_cur = [n for _, n in pairs]
        if key != self._title or self._history is None:
            self._title, self._history, self._lost = key, norm_cur[-self.HISTORY:], 0
            return []
        k = _overlap(self._history, norm_cur)
        if k == 0 and self._history and norm_cur:
            self._lost = self._lost + 1 if norm_cur == self._lost_screen else 1
            self._lost_screen = norm_cur
            if self._lost >= self.REBASE_AFTER:
                self._history, self._lost = norm_cur[-self.HISTORY:], 0
            return []
        self._lost = 0
        fresh = pairs[k:]
        self._history = (self._history + [n for _, n in fresh])[-self.HISTORY:]
        return [t for t, n in fresh if n[0] == "other"]


def read_rows(rows: list[dict]) -> dict:
    """What one look at the window shows: ``{"title", "title_clean", "other", "self", "sequence"}``.

    ``other`` and ``self`` are the messages of the other side and of the account itself,
    each from top to bottom of the screen; ``sequence`` is both sides and the time lines
    in screen order (what ``NewMessageTracker`` needs).
    """
    geom = resolve_chat_geometry(rows)
    title = chat_title(rows, geometry=geom)
    return {
        "title": str(title),
        "title_clean": clean_group_name(title),
        "other": other_messages(rows, "other", geometry=geom),
        "self": other_messages(rows, "self", geometry=geom),
        "sequence": messages_in_order(rows, geometry=geom),
    }
