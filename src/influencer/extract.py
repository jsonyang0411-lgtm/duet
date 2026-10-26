"""从达人的话里认两件事（prm_bridge 用）：合作单上写的小红书账号，以及达人是不是在要求提高佣金。

收件信息、商品由 PRM 起草的 Claude 整理在合作单上，不在这里提取。
"""

from __future__ import annotations

import re

from .settings import (
    COMMISSION_RAISE_WORDS,
    COMMISSION_WORDS,
    DEFAULT_COMMISSION_RATE,
)

_XHS_RE = re.compile(
    r"(?:小红书(?:主页|号|账号|链接)?\s*[:：]?\s*)?"
    r"(https?://(?:www\.)?xiaohongshu\.com/user/profile/[0-9A-Za-z]+|https?://xhslink\.com/[0-9A-Za-z/]+)"
    r"|小红书号\s*[:：]?\s*([0-9A-Za-z_]{5,})"
)

# "30%""30个点"这种明确的数；单独的"30点"可能是时间，要和"佣金"之类的词一起出现才算
_RATE_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(%|％|个点|点)")


def asks_for_higher_commission(text: str) -> bool:
    """达人是不是在要求提高佣金：提到佣金并且说要高一点，或者开出了超过起步佣金的数。
    起步是 20%（D37），所以达人要 25% 也算要求加佣金，转运营，不由模型答应。"""
    mentions_commission = any(word in text for word in COMMISSION_WORDS)
    if mentions_commission and any(word in text for word in COMMISSION_RAISE_WORDS):
        return True
    for match in _RATE_RE.finditer(text):
        if match.group(2) == "点" and not mentions_commission:
            continue
        if float(match.group(1)) > DEFAULT_COMMISSION_RATE * 100:
            return True
    return False


def extract_xhs_account(text: str) -> str:
    """小红书主页链接（含 xhslink 短链），或"小红书号：xxx"里的号。"""
    match = _XHS_RE.search(text)
    if not match:
        return ""
    return match.group(1) or match.group(2) or ""
