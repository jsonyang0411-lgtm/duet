"""日志和追踪记录里的个人信息打码（B14）。

只用在"写进日志、追踪文件"的地方，不改消息本身：数据库、回复、发给模型的内容都不经过这里。
规则是宁可多遮一点：手机号留前 3 位和后 4 位，身份证号只留前 3 位和后 2 位，
像地址的片段（"xx省/市/区/县"开头的一串）整段换成〔地址〕，"收件人：张三"和紧挨着手机号的姓名换成〔姓名〕。
"""

from __future__ import annotations

import re

_ID_CARD_RE = re.compile(r"(?<!\d)(\d{3})\d{12}(\d{2}[\dXx])(?![\dXx])")
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_ADDRESS_RE = re.compile(r"[一-龥]{2,}(?:省|市|自治区|区|县)[^\s，,。；;！？!?]{2,}")
_LABELED_NAME_RE = re.compile(r"((?:收件人|收货人|联系人|姓名)\s*[:：]?\s*)[一-龥·]{2,4}")
# 紧挨着手机号的两到四个汉字，多半是姓名："张三 13812345678"
_NAME_BY_PHONE_RE = re.compile(r"(?<![一-龥])[一-龥·]{2,4}(?=\s*[:：]?\s*1[3-9]\d{9}(?!\d))")
_PHONE_LABELS = ("电话", "手机", "手机号", "联系电话", "号码", "电话号码", "联系方式", "微信")


def mask_pii(text: object) -> str:
    """把一段文字里的手机号、身份证号、地址和带标签的姓名遮住。"""
    masked = "" if text is None else str(text)
    masked = _ID_CARD_RE.sub(lambda m: f"{m.group(1)}{'*' * 13}{m.group(2)[-2:]}", masked)
    masked = _NAME_BY_PHONE_RE.sub(lambda m: m.group(0) if m.group(0) in _PHONE_LABELS else "〔姓名〕", masked)
    masked = _LABELED_NAME_RE.sub(r"\1〔姓名〕", masked)
    masked = _PHONE_RE.sub(r"\1****\2", masked)
    return _ADDRESS_RE.sub("〔地址〕", masked)
