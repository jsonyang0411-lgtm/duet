"""What a WeChat message becomes in an edge event (edge-protocol.md 4.2): the ``text``, the
``quote`` of a reply, the line of a system notice.

Works on the standardized messages of the Windows backend (wcdb_backend._standardize):
``msg_type``, ``msg_subtype`` (an appmsg's kind from the high 32 bits of WeChat 4's
``local_type``), ``content`` (voice already as ``[语音] …``), ``file_name``, the ``quoted_*``
fields of a reply, and ``special`` (the title of a link, a mini program, a card, a location, and
a link's ``url`` when the backend gives one; the line of a system message).  Nothing is dropped:
an unknown type is still a line.  The PRM copilot writes its draft jobs' lines with this too
(src/prm/service.py ``message_text``), so both deployments tell the Mac the same.
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
SYSTEM_TYPES = (SYSTEM_TYPE, 10002)          # WeChat's own lines (10002: the sysmsg XML of older versions)
RECALL_MARK = "撤回了一条消息"
APPMSG_TYPE = 49
# 拍一拍: an appmsg of kind 62.  WeChat 4 writes it with local_type (62 << 32) | 49 = 266287972401
# (Windows, 4.1.15, 2026-10-28) and <type>62</type> in its XML; the backend keeps 49 as msg_type and
# the high 32 bits as msg_subtype.  Only this kind is WeChat's line: a reply (57), a link (5), a file
# (6), a transfer (2000) … carry their kind in the high bits too and are someone's message.
PAT_SUBTYPE = 62
PAT_LINE = "[拍一拍]"                          # its line when the XML gave no title


def msg_type_of(msg: dict) -> int:
    try:
        return int(msg.get("msg_type", 1) or 1)
    except (TypeError, ValueError):
        return 1


def is_reply(msg: dict) -> bool:
    return bool(str(msg.get("quoted_content") or "").strip() or str(msg.get("quoted_server_id") or "").strip())


def is_pat(msg: dict) -> bool:
    """拍一拍, whoever patted whom: an appmsg (49) of kind 62, by the high 32 bits of ``local_type``
    (``msg_subtype``) or by the XML's ``<type>`` (``special.type``)."""
    if msg_type_of(msg) != APPMSG_TYPE:
        return False
    try:
        if int(msg.get("msg_subtype") or 0) == PAT_SUBTYPE:
            return True
    except (TypeError, ValueError):
        pass
    special = msg.get("special") if isinstance(msg.get("special"), dict) else {}
    return str(special.get("type") or "").strip() == str(PAT_SUBTYPE)


def is_notice(msg: dict) -> bool:
    """One of WeChat's own lines in the chat (a recall, 拍一拍, a red packet notice …): nobody's words."""
    return msg_type_of(msg) in SYSTEM_TYPES or is_pat(msg)


def recall_by(text: str) -> str:
    """Who took a message back, by WeChat's line: "us" (「你撤回了一条消息」), "them" (「"小美" 撤回了
    一条消息」, 「对方撤回了一条消息」), or "" when the line is no recall."""
    line = " ".join(str(text or "").split())
    if RECALL_MARK not in line:
        return ""
    return "us" if line.startswith("你") else "them"


def _title(msg: dict) -> str:
    special = msg.get("special") if isinstance(msg.get("special"), dict) else {}
    return " ".join(str(special.get("title") or "").split())[:120]


def _url(msg: dict) -> str:
    """The web address of a link (``special.url``, http or https only), or ""."""
    special = msg.get("special") if isinstance(msg.get("special"), dict) else {}
    url = "".join(str(special.get("url") or "").split())
    return url[:500] if url.lower().startswith(("http://", "https://")) else ""


def message_text(msg: dict) -> str:
    """The line of a creator's (or an operator's) message; WeChat's own line (``is_notice``) is
    its notice text."""
    msg_type = msg_type_of(msg)
    content = _QUOTE_PREFIX.sub("", str(msg.get("content", "") or "")).strip()
    if is_notice(msg):
        return notice_text(msg)
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
        if kind in _NO_TITLE:
            return f"[{label}]"
        return " ".join(t for t in (f"[{label}]", _title(msg), _url(msg)) if t)
    return content if content and not content.startswith("[消息类型") else f"[消息类型:{msg_type}]"


def notice_text(msg: dict) -> str:
    """The line of a system message in the chat (「对方撤回了一条消息」, 「"小美" 拍了拍我」)."""
    title = _title(msg)
    if title:
        return title
    if is_pat(msg):
        return PAT_LINE
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
