"""WeChat's built-in emoji ("小黄脸"): text codes such as ``[捂脸]``.

WeChat draws a known code as a picture; an unknown one (``[开心]``) stays plain
text and gives the bot away.  With ``WECHAT_EMOJI=on`` the structured pipeline
offers the model a short list of codes, and :func:`keep_known` removes every
other bracket code from its reply.  The list differs between WeChat versions:
check it once with ``python tools/sticker_check.py --emoji``.
"""

from __future__ import annotations

import re

# Codes in WeChat for years and common in group chats; WECHAT_EMOJI_CODES replaces the list.
DEFAULT_CODES = (
    "[微笑]", "[捂脸]", "[旺柴]", "[吃瓜]", "[裂开]", "[苦涩]", "[叹气]", "[让我看看]",
    "[翻白眼]", "[666]", "[好的]", "[耶]", "[加油]", "[害羞]", "[偷笑]", "[呲牙]",
    "[委屈]", "[疑问]", "[尴尬]", "[汗]", "[Emm]", "[奸笑]", "[机智]", "[皱眉]",
    "[哇]", "[天啊]", "[破涕为笑]", "[愉快]", "[强]", "[抱拳]",
)
MAX_CODES = 60
MAX_CODE_CHARS = 8          # inside the brackets
MAX_PER_MESSAGE = 2

_CODE_RE = re.compile(
    r"(\[)([^\[\]\r\n]{1,%d})\]|([［【])([^［］【】\[\]\r\n]{1,%d})[］】]" % (MAX_CODE_CHARS, MAX_CODE_CHARS)
)
_DANGLING_RE = re.compile(r"\[([^\[\]\r\n]{0,%d})$" % MAX_CODE_CHARS)   # cut by the length limit
_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def parse_codes(raw) -> tuple[str, ...]:
    """Codes from ``[捂脸][旺柴]``, ``捂脸,旺柴`` or a list; empty when nothing usable."""
    if isinstance(raw, (list, tuple)):
        raw = ",".join(str(item) for item in raw)
    names: list[str] = []
    for piece in re.split(r"[,，\s]+", str(raw or "")):
        names += re.findall(r"\[([^\[\]\r\n]+)\]", piece) if "[" in piece else [piece]
    codes: list[str] = []
    for name in names:
        name = name.strip()
        if not name or len(name) > MAX_CODE_CHARS or any(c in name for c in "[]［］【】"):
            continue
        code = f"[{name}]"
        if code not in codes:
            codes.append(code)
    return tuple(codes[:MAX_CODES])


def keep_known(text: str, allowed, *, limit: int = MAX_PER_MESSAGE) -> str:
    """The text with only allowed codes left, at most ``limit`` of them.

    ``[emm]`` becomes ``[Emm]`` and ``【捂脸】`` becomes ``[捂脸]`` when the code
    is allowed.  An unknown ``[...]`` with Chinese in it (``[开心]``, which
    WeChat would show as text) is removed, as is one cut open at the end; any
    other bracket (``[A]``, ``【通知】``) is ordinary text and stays.
    """
    canonical = {code[1:-1].casefold(): code for code in allowed}
    count = 0

    def take(match) -> str:
        nonlocal count
        ascii_bracket = match.group(1) is not None
        name = match.group(2) if ascii_bracket else match.group(4)
        code = canonical.get(name.strip().casefold())
        if code is None:
            return "" if ascii_bracket and _CJK_RE.search(name) else match.group(0)
        if count >= limit:
            return ""
        count += 1
        return code

    text = _CODE_RE.sub(take, str(text or ""))
    text = _DANGLING_RE.sub(lambda m: "" if not m.group(1) or _CJK_RE.search(m.group(1)) else m.group(0), text)
    return re.sub(r"\s{2,}", " ", text).strip()


def prompt_line(codes) -> str:
    return (
        "- 微信自带表情（可选）：可以偶尔在句子里放一个，写成方括号代码，只能从这些里选："
        + "".join(codes)
        + "。它们不算颜文字或 emoji，人设里“不用表情”的要求不包括它们；大多数句子不需要，一句最多一个。"
    )
