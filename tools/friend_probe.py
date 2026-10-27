#!/usr/bin/env python3
"""What does a new friend look like in WeChat 4's database?  Read-only, for friend_added (edge-friends-reply.md).

Run it on the Windows computer, from the project folder, after these three on the bot's WeChat account:
  1. a test account adds the bot's account, and you accept the request on THIS computer;
  2. another (or the same, after deleting it) adds the bot, and you accept it on the PHONE;
  3. the bot's account sends a request to a test account, and the test account accepts it.
WeChat must be running and logged in; the bot may run or not.  Nothing is clicked, typed or sent.

    python tools/friend_probe.py                     # the 8 newest private chats, 12 messages each
    python tools/friend_probe.py --chats 15 --messages 20

It prints, and writes to probe-friends.txt in the project folder:
  - which fields a session and a message carry (their names only);
  - the newest private chats: the shape of their id (wxid_ or a custom one, and its length, never the id),
    the name masked, when the last message came;
  - each chat's first messages, oldest first: time, type, from whom (我 / 对方 / 系统), and the text with
    phone numbers and the friend's name masked; the system lines ("你已添加了…", "…通过了你的朋友验证…",
    "以上是打招呼的内容"…) are marked with >>>.
Look at the file before you send it on.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FRIEND_MARKERS = ("你已添加了", "现在可以开始聊天了", "通过了你的朋友验证", "朋友验证请求", "以上是打招呼的内容",
                  "打招呼", "已添加", "added", "accepted your friend request")
PHONE = re.compile(r"(?<!\d)(1[3-9]\d)[\s-]?\d{4}[\s-]?(\d{4})(?!\d)")
ADDED_NAME = re.compile(r"(你已添加了|你已添加)(.+?)(，|,|。|$)")


def shape(username: str) -> str:
    """The form of a chat id, not the id: wxid_ or custom, and its length."""
    if username.startswith("wxid_"):
        return f"wxid_…（{len(username)} 位）"
    return f"自定义微信号（首字符 {'字母' if username[:1].isalpha() else '其他'}，{len(username)} 位）"


def masked_name(name: str) -> str:
    name = str(name or "").strip()
    return (name[:1] + "*" * min(4, max(1, len(name) - 1))) if name else "（空）"


def masked_text(text: str, name: str = "") -> str:
    text = " ".join(str(text or "").split())
    text = ADDED_NAME.sub(lambda m: f"{m.group(1)}〈名字〉{m.group(3)}", text)
    if name and len(name) >= 2:
        text = text.replace(name, "〈名字〉")
    text = PHONE.sub(lambda m: f"{m.group(1)}****{m.group(2)}", text)
    return text[:120] + ("…" if len(text) > 120 else "")


def first(d: dict, *keys, default=None):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return default


def is_me(sender: str, me: str) -> bool:
    """Is this message the account's own?  ``myWxid`` comes from WeChat 4's data folder, which
    carries a suffix (wxid_abc_b6ce) the messages' sender does not (wxid_abc): the bot's backend
    matches the same way."""
    return bool(sender and me) and (sender == me or me.startswith(sender + "_") or sender.startswith(me + "_"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chats", type=int, default=8, help="how many of the newest private chats (default 8)")
    parser.add_argument("--messages", type=int, default=12, help="how many messages of each, oldest first (default 12)")
    parser.add_argument("--out", default=str(ROOT / "probe-friends.txt"), help="where the copy goes (default: project folder)")
    args = parser.parse_args(argv)

    from src.wechat.wcdb_backend import _decompress_zstd_hex, _is_private_talker, _session_username, _special_info
    from src.wechat.wcdb_client import WcdbNativeClient

    out: list[str] = []

    def say(line: str = "") -> None:
        print(line)
        out.append(line)

    client = WcdbNativeClient()
    client.init()
    client.open()
    me = str((getattr(client, "_config", {}) or {}).get("myWxid", "") or "")
    sessions = client.get_sessions() or []
    say(f"friend_probe {time.strftime('%Y-%m-%d %H:%M:%S')}：会话 {len(sessions)} 个")
    if sessions:
        say("会话带的字段：" + "、".join(sorted(str(k) for k in sessions[0].keys())))

    def when(s: dict) -> float:
        value = first(s, "sort_timestamp", "sortTimestamp", "last_timestamp", "lastTimestamp", "timestamp",
                      "last_msg_time", "lastMsgTime", default=0)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 0.0
        return value / 1000 if value > 1e11 else value

    private = []
    for s in sessions:
        username = _session_username(s)
        if not username or not _is_private_talker(username) or "@openim" in username:
            continue
        if me and (username == me or username.startswith(me) or me.startswith(username)):
            continue
        private.append(s)
    private.sort(key=when, reverse=True)
    say(f"个人私聊 {len(private)} 个，看最新的 {min(args.chats, len(private))} 个")

    message_fields_shown = False
    for n, s in enumerate(private[:args.chats], 1):
        username = _session_username(s)
        name = str(first(s, "displayName", "displayname", "nickname", "nickName", "remark", "remarkName", default="") or "")
        last = when(s)
        say("")
        say(f"[{n}] 私聊编号：{shape(username)}；名字：{masked_name(name)}；"
            f"有备注字段：{'是' if first(s, 'remark', 'remarkName') else '否'}；"
            f"最后一条：{time.strftime('%m-%d %H:%M', time.localtime(last)) if last else '（看不出）'}")
        messages = client.get_messages(username, limit=max(1, args.messages)) or []
        if messages and not message_fields_shown:
            say("    消息带的字段：" + "、".join(sorted(str(k) for k in messages[0].keys())))
            message_fields_shown = True

        def ts(m: dict) -> float:
            try:
                return float(first(m, "create_time", "createTime", "timestamp", default=0) or 0)
            except (TypeError, ValueError):
                return 0.0

        for m in sorted(messages, key=ts)[:args.messages]:
            local_type = int(first(m, "local_type", "localType", "msg_type", default=0) or 0)
            sender = str(first(m, "sender_username", "senderUsername", "sender", default="") or "")
            content = str(first(m, "message_content", "content", default="") or "")
            decoded = _decompress_zstd_hex(content) or content
            mine = "我" if is_me(sender, me) else "对方"
            if local_type == 10000:
                who, text = "系统", _special_info(content, local_type).get("title", "")
            elif local_type == 1:
                who, text = mine, decoded
            else:
                who, text = mine, f"（类型 {local_type}，不看内容）"
            real = first(m, "real_sender_id", "realSenderId")
            if real is not None:
                who += f"（real_sender_id {real}）"
            line = masked_text(text, name)
            mark = ">>>" if any(k in str(text) for k in FRIEND_MARKERS) else "   "
            stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(ts(m))) if ts(m) else "--"
            say(f"  {mark} {stamp} 类型 {local_type:<5} {who}：{line}")
    path = Path(args.out)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"\n写好了：{path}（发给我之前先看一眼）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
