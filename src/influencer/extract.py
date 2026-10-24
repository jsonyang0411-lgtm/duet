"""从达人的微信消息里提取索样信息：商品、收件人姓名、电话、地址、小红书账号。

都是规则匹配，覆盖常见写法。提取不到的字段靠多轮对话追问，最后由运营在审批单上核对。
第 4 阶段计划改用大模型抽取（handoff.md 第 5 节）。
"""

from __future__ import annotations

import re

from .settings import PRODUCT_KEYWORDS, SAMPLE_KEYWORDS
from .types import SampleRequestIntent

_XHS_RE = re.compile(
    r"(?:小红书(?:主页|号|账号|链接)?\s*[:：]?\s*)?"
    r"(https?://(?:www\.)?xiaohongshu\.com/user/profile/[0-9A-Za-z]+|https?://xhslink\.com/[0-9A-Za-z/]+)"
    r"|小红书号\s*[:：]?\s*([0-9A-Za-z_]{5,})"
)
_URL_RE = re.compile(r"https?://\S+")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ADDRESS_RE = re.compile(
    r"(?:收件地址|收货地址|地址|寄到|寄送至)?[:：\s]*"
    r"((?:[一-龥]{2,}(?:省|市|区|县|街道|路|镇|村))[^，,。；;\n\r]+)"
)
_NAME_LABEL_RE = re.compile(
    r"(?:收件人|收货人|姓名)[:：\s]*([一-龥·]{2,4}?)"
    r"(?=[\s,，;；:：。]|电话|手机|联系|地址|1[3-9]\d{9}|$)"
)
# 地址末尾空格后跟着的两三个汉字，多半是收件人姓名："……100号 张三"
_TRAILING_NAME_RE = re.compile(r"^(.*\S)\s+([一-龥·]{2,4})$")
_NAME_RE = re.compile(r"[一-龥·]{2,4}")
_ADDRESS_CHARS = set("省市区县路街道号镇村巷弄栋室楼")
_NOT_NAMES = (
    "收件", "收货", "姓名", "电话", "手机", "地址", "联系", "老师", "你好", "您好",
    "谢谢", "感谢", "好的", "收到", "麻烦", "辛苦", "可以", "稍等", "没问题",
)


def mentions_sample(text: str) -> bool:
    return any(kw in text for kw in SAMPLE_KEYWORDS)


def extract_product(text: str) -> str:
    for name in PRODUCT_KEYWORDS:
        if name in text:
            return name
    return ""


def extract_xhs_account(text: str) -> str:
    """小红书主页链接（含 xhslink 短链），或"小红书号：xxx"里的号。"""
    match = _XHS_RE.search(text)
    if not match:
        return ""
    return match.group(1) or match.group(2) or ""


def _looks_like_name(token: str) -> bool:
    return (
        bool(_NAME_RE.fullmatch(token))
        and not any(word in token for word in _NOT_NAMES)
        and not set(token) & _ADDRESS_CHARS
    )


def extract_request_info(text: str, *, expecting_name: bool = False) -> SampleRequestIntent:
    """提取一条消息里的索样信息。

    expecting_name：上一轮只追问了姓名。只有这种情况，或者同一条消息里还带了电话或地址时，
    才把单独的两到四个汉字当成姓名，避免把"好的""稍等"之类的回复误认成名字。
    """
    xhs_account = extract_xhs_account(text)
    # 去掉小红书链接和其他网址，免得里面的数字和字样干扰电话、地址、姓名的识别
    cleaned = _URL_RE.sub("，", _XHS_RE.sub("，", text))

    phone_match = _PHONE_RE.search(cleaned)
    phone = phone_match.group(0) if phone_match else ""
    rest = _PHONE_RE.sub("，", cleaned)  # 去掉电话，免得地址把电话一起截进去

    name = ""
    address = ""
    address_match = _ADDRESS_RE.search(rest)
    if address_match:
        address = address_match.group(1).strip()
        trailing = _TRAILING_NAME_RE.match(address)
        if trailing and _looks_like_name(trailing.group(2)):
            address, name = trailing.group(1), trailing.group(2)
        rest = rest.replace(address_match.group(0), "，")

    label = _NAME_LABEL_RE.search(cleaned)
    if label:
        name = label.group(1)
    elif not name and (phone or address or expecting_name):
        for token in re.split(r"[\s,，;；:：。/]+", rest):
            if _looks_like_name(token):
                name = token
                break

    return SampleRequestIntent(
        is_sample_request=mentions_sample(text),
        requested_product=extract_product(text),
        recipient_name=name,
        recipient_phone=phone,
        shipping_address=address,
        xhs_account=xhs_account,
        raw_evidence=text,
    )
