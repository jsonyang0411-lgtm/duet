"""A forwarded 「聊天记录」 (WeChat's merged forward, an appmsg of type 19) read out of its XML (2026-11-06).

Before, the bot saw only its title (「XX的聊天记录」).  A boss who pulls a creator into a group often forwards his chat
with her first (what she wants, what he agreed to), and her draft could only ask her again.  WeChat writes the
record's messages into the appmsg's ``<recorditem>``: the text of a ``<recordinfo>`` document whose ``<datalist>`` has
one ``<dataitem datatype="…">`` per message forwarded, with ``<sourcename>`` (who wrote it, as the forwarder saw that
person), ``<datadesc>`` (a text's words), ``<datatitle>`` (a link's, a file's title) and ``<dataitemsource>`` (that
person's ids, as far as WeChat writes them: ``fromusr``, ``realchatname`` or a ``hashusername``).  The appmsg's
``<des>`` repeats the first few lines (「名字: 话」) as a preview: read only when ``<recorditem>`` is not there or
cannot be read (``partial``).

``parse`` gives ``{"title", "count", "items": [{"name", "text", "ids"}], "partial"}``, or None (no record, nothing
readable: the message stays what it was, 「[聊天记录] <title>」).  src/edge/text.py makes it one line
(``record_line``); PRM hides the bosses' names in it where they never go to the Mac (src/prm/boss_groups.py
``record_msg``).  Never raises; reads at most RECORD_XML_MAX characters of XML.
"""

from __future__ import annotations

import hashlib
import logging
import xml.etree.ElementTree as ET
from typing import Optional

logger = logging.getLogger(__name__)

RECORD_APPMSG = "19"            # the appmsg <type> of a forwarded 聊天记录
NESTED = "17"                   # an item that is a 聊天记录 itself
RECORD_ITEMS = 50               # the items kept of one record (the first ones)
RECORD_XML_MAX = 400_000        # a longer <recorditem> is not parsed (its <des> preview is read)
TITLE_CHARS = 60
NAME_CHARS = 30
ITEM_CHARS = 300                # one item's words
ID_CHARS = 100
IDS_KEPT = 4
# what an item that is not text is told as (WeChat's favourite types; the others: 「[消息] <its title>」)
ITEM_LABELS = {"2": "图片", "3": "语音", "4": "视频", "5": "链接", "6": "位置", "7": "音乐", "8": "文件",
               "15": "视频", NESTED: "聊天记录"}
_TITLED = {"链接", "位置", "音乐", "聊天记录"}       # told with their title after the label
_LOGGED: set = set()                                # records whose trouble was logged (each once: they are read again)
LOGGED_KEEP = 500


def _clean(text, limit: int) -> str:
    """One line: each run of white space one space, at most ``limit`` characters."""
    return " ".join(str(text or "").split())[:limit]


def _text(node: Optional[ET.Element], path: str) -> str:
    if node is None:
        return ""
    return str(node.findtext(path, default="") or "").strip()


def _item_words(item: ET.Element) -> str:
    """What one forwarded message says: a text's words; 「[图片]」, 「[链接] 标题」, 「[文件：名字]」 …; 「[消息]」 for a
    message with nothing to tell."""
    kind = str(item.get("datatype") or "").strip()
    desc = _clean(_text(item, "datadesc"), ITEM_CHARS)
    title = _clean(_text(item, "datatitle"), ITEM_CHARS)
    if kind == "1":
        return desc or "[消息]"
    label = ITEM_LABELS.get(kind)
    if label == "文件":
        return f"[文件：{title}]" if title else "[文件]"
    if label in _TITLED:
        said = title or desc
        return f"[{label}] {said}" if said else f"[{label}]"
    if label:
        return f"[{label}]"
    said = title or desc
    return f"[消息] {said}" if said else "[消息]"


def _ids(item: ET.Element) -> list[str]:
    """The ids WeChat wrote of an item's person (``<dataitemsource>``'s children: a wxid, a hash of one), lower case,
    each once."""
    source = item.find("dataitemsource")
    found: list[str] = []
    for child in list(source) if source is not None else []:
        value = "".join(str(child.text or "").split()).lower()
        if 4 <= len(value) <= ID_CHARS and value not in found:
            found.append(value)
    return found[:IDS_KEPT]


def _recordinfo(appmsg: ET.Element) -> Optional[ET.Element]:
    """The ``<recordinfo>`` the appmsg's ``<recorditem>`` holds (its text is a document of its own), or None."""
    raw = str(appmsg.findtext("recorditem", default="") or "").strip()
    if not raw:
        inline = appmsg.find("recorditem/recordinfo")           # (written as XML, not as text)
        return inline
    if len(raw) > RECORD_XML_MAX:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        logger.debug("A forwarded 聊天记录's <recorditem> could not be parsed", exc_info=True)
        return None
    return root if root.tag == "recordinfo" else root.find(".//recordinfo")


def _preview(appmsg: ET.Element) -> list[dict]:
    """The items of the appmsg's ``<des>`` preview, 「名字: 话」 a line (a line without a name: the words only)."""
    items = []
    for line in str(appmsg.findtext("des", default="") or "").splitlines():
        line = line.strip()
        if not line:
            continue
        name, words = "", line
        for mark in (": ", "：", ":"):
            if mark in line:
                head, tail = line.split(mark, 1)
                if head.strip() and len(head.strip()) <= NAME_CHARS:
                    name, words = head, tail
                break
        items.append({"name": _clean(name, NAME_CHARS), "text": _clean(words, ITEM_CHARS) or "[消息]", "ids": []})
    return items[:RECORD_ITEMS]


def _shape(appmsg: ET.Element) -> str:
    """What a record's XML looks like, for the log when it could not be read whole: the appmsg's tags and how long its
    ``<recorditem>`` is, the tag it starts with; never a name or a word of it."""
    raw = str(appmsg.findtext("recorditem", default="") or "").strip()
    start = raw[:raw.find(">") + 1] if raw.startswith("<") and 0 < raw.find(">") <= 40 else ""
    start = start if start and all(ch.isalnum() or ch in "<>/?=\"'._- " for ch in start) else ("text" if raw else "none")
    return (f"tags {','.join(sorted({child.tag for child in appmsg})) or '-'}; recorditem {len(raw)} chars, starts "
            f"{start}; inline recordinfo {appmsg.find('recorditem/recordinfo') is not None}")


def _log_once(appmsg: ET.Element, said: str) -> None:
    """One INFO line for a record that could not be read whole (``_shape``: no name, no word of it), once per record
    (the same rows are read again at each poll)."""
    raw = str(appmsg.findtext("recorditem", default="") or "")
    key = hashlib.md5(f"{len(raw)}|{raw[:200]}|{appmsg.findtext('des', default='')}".encode("utf-8")).hexdigest()
    if key in _LOGGED:
        return
    if len(_LOGGED) >= LOGGED_KEEP:
        _LOGGED.clear()
    _LOGGED.add(key)
    logger.info("A forwarded 聊天记录 could not be read whole (%s): %s", _shape(appmsg), said)


def parse(appmsg: Optional[ET.Element]) -> Optional[dict]:
    """A forwarded 聊天记录's contents from its ``<appmsg>`` (of type RECORD_APPMSG): ``{"title", "count", "items":
    [{"name", "text", "ids"}], "partial"}`` (``count``: how many messages it holds, ``items`` the first RECORD_ITEMS;
    ``partial``: only the ``<des>`` preview was readable).  None for another appmsg, or a record with nothing readable.
    Never raises."""
    try:
        if appmsg is None or _text(appmsg, "type") != RECORD_APPMSG:
            return None
        info = _recordinfo(appmsg)
        title = _clean(_text(info, "title") or _text(appmsg, "title"), TITLE_CHARS)
        if info is not None:
            data = info.find("datalist")
            nodes = [n for n in (list(data) if data is not None else []) if n.tag == "dataitem"]
            items = [{"name": _clean(_text(n, "sourcename"), NAME_CHARS), "text": _item_words(n), "ids": _ids(n)}
                     for n in nodes[:RECORD_ITEMS]]
            try:
                count = int(str(data.get("count") or "").strip()) if data is not None else 0
            except ValueError:
                count = 0
            if items:
                return {"title": title, "count": max(count, len(nodes)), "items": items, "partial": False}
        items = _preview(appmsg)
        _log_once(appmsg, f"its preview gives {len(items)} line(s)" if items else "only its title is told")
        if items:
            return {"title": title, "count": len(items), "items": items, "partial": True}
        return None
    except Exception:
        logger.debug("A forwarded 聊天记录 could not be read", exc_info=True)
        return None
