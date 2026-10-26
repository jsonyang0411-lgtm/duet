"""What a WeChat message becomes in an edge event (edge-protocol.md 4.2): the ``text``, the
``quote`` of a reply, the line of a system notice.

Works on the standardized messages of the Windows backend (wcdb_backend._standardize):
``msg_type``, ``content`` (voice already as ``[语音] …``), ``file_name``, the ``quoted_*``
fields of a reply, and ``special`` (the title of a link, a mini program, a card, a location;
the line of a system message).  Nothing is dropped: an unknown type is still a line.
"""

from __future__ import annotations

import re
from typing import Optional

# appmsg (type 49) kinds that are neither a file (6) nor a reply (57)
APPMSG_LABELS = {
    "1": "消息", "3": "音乐", "4": "链接", "5": "链接", "8": "表情", "19": "聊天记录", "24": "笔记",
    "33": "小程序", "36": "小程序", "51": "视频号", "63": "直播", "2000": "转账", "2001": "红包",
}
_NO_TITLE = {"2000", "2001", "8"}             # 转账 / 红包 / 表情: the title says nothing useful
_QUOTE_PREFIX = re.compile(r"^\s*wxid_[a-zA-Z0-9]+:\s*")
SYSTEM_TYPE = 10000


def msg_type_of(msg: dict) -> int:
    try:
        return int(msg.get("msg_type", 1) or 1)
    except (TypeError, ValueError):
        return 1


def is_reply(msg: dict) -> bool:
    return bool(str(msg.get("quoted_content") or "").strip() or str(msg.get("quoted_server_id") or "").strip())


def is_notice(msg: dict) -> bool:
    return msg_type_of(msg) == SYSTEM_TYPE


def _title(msg: dict) -> str:
    special = msg.get("special") if isinstance(msg.get("special"), dict) else {}
    return " ".join(str(special.get("title") or "").split())[:120]


def message_text(msg: dict) -> str:
    """The line of a creator's (or an operator's) message."""
    msg_type = msg_type_of(msg)
    content = _QUOTE_PREFIX.sub("", str(msg.get("content", "") or "")).strip()
    if is_reply(msg):
        return content or "[引用消息]"
    if msg_type == 1:
        return content or "[消息]"
    if msg_type == 3:
        return "[图片]"
    if msg_type == 34:
        heard = content[len("[语音]"):].strip() if content.startswith("[语音]") else ""
        return f"[语音] {heard}" if heard else "[语音]（没转出文字）"
    if msg_type == 43:
        return "[视频]"
    if msg_type == 47:
        return "[表情]"
    if msg_type == 42:
        return f"[名片] {_title(msg)}".strip()
    if msg_type == 48:
        return f"[位置] {_title(msg)}".strip()
    if msg_type == 49:
        name = str(msg.get("file_name") or "").strip()
        if name:
            return f"[文件] {name}"
        special = msg.get("special") if isinstance(msg.get("special"), dict) else {}
        kind = str(special.get("type") or "")
        label = APPMSG_LABELS.get(kind, "消息")
        title = "" if kind in _NO_TITLE else _title(msg)
        return f"[{label}] {title}".strip()
    if msg_type == SYSTEM_TYPE:
        return notice_text(msg)
    return content if content and not content.startswith("[消息类型") else f"[消息类型:{msg_type}]"


def notice_text(msg: dict) -> str:
    """The line of a system message in the chat (「对方撤回了一条消息」)."""
    title = _title(msg)
    if title:
        return title
    content = re.sub(r"<[^>]+>", " ", str(msg.get("content", "") or ""))
    content = " ".join(content.split())
    return content if content and not content.startswith("[消息类型") else "[系统消息]"


def quote_of(msg: dict, own_wxid: str, chat_id: str) -> Optional[dict]:
    """``{"text", "from", "server_id"}`` of a reply, ``from`` = us / creator / other; None otherwise."""
    if not is_reply(msg):
        return None
    sender = str(msg.get("quoted_sender_id") or "").strip()
    if own_wxid and sender and (sender == own_wxid or own_wxid.startswith(sender) or sender.startswith(own_wxid)):
        whose = "us"
    elif sender and sender == chat_id:
        whose = "creator"
    else:
        whose = "other"
    return {"text": str(msg.get("quoted_content") or "").strip(), "from": whose,
            "server_id": str(msg.get("quoted_server_id") or "").strip()}


def normalized(text: str) -> str:
    """Text as compared for the read-back: runs of white space are one space."""
    return " ".join(str(text or "").split())
