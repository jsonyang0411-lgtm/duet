"""Tests for WCDB message standardization."""

from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import zstandard as zstd

from src.config import BotConfig
from src.wechat.wcdb_backend import WcdbBackend
from src.wechat.wcdb_client import WcdbNativeClient


class FakeClient:
    _config = {"myWxid": "wxid_me"}

    def __init__(self, sessions=None):
        self.sessions = sessions or []

    def resolve_nickname(self, wxid):
        return {
            "wxid_user": "测试用户",
            "wxid_friend": "私聊好友",
            "room1@chatroom": "测试群",
        }.get(wxid, wxid)

    def get_sessions(self):
        return self.sessions

    def get_group_members(self, username):
        if username.endswith("@chatroom"):
            return [{"username": "wxid_user"}]
        return []

    def get_display_names(self, usernames):
        return {u: self.resolve_nickname(u) for u in usernames}


class WcdbBackendStandardizeTests(unittest.TestCase):
    def make_backend(self):
        backend = WcdbBackend(bot_display_name="林小满", groups=["测试群"])
        backend._client = FakeClient()
        return backend

    def test_standardize_uses_snake_case_local_type_for_images(self):
        msg = {
            "sender_username": "wxid_user",
            "message_content": "raw-media-payload",
            "local_type": 3,
            "create_time": int(time.time()),
            "local_id": "m1",
        }

        standardized = self.make_backend()._standardize(
            msg, "测试群", "chat1@chatroom",
        )

        self.assertEqual(standardized["content"], "[图片]")
        self.assertEqual(standardized["msg_type"], 3)
        self.assertEqual(standardized["server_id"], "")

    def test_a_long_text_wechat_keeps_compressed_is_text_not_a_picture(self):
        # WeChat 4 stores a longer text zstd compressed and hex encoded (seen on Windows,
        # 2026-10-26: a 124-character line read back as "[图片]").
        text = "好的～帮你核对一下收件信息：收件人测试，电话 13800001234，地址上海市浦东新区测试路1号2栋301。没问题回我一下哦～" * 2
        backend = self.make_backend()
        for talker, raw in (("wxid_friend", text), ("room1@chatroom", "wxid_user:\n" + text)):
            packed = zstd.ZstdCompressor().compress(raw.encode("utf-8")).hex()
            self.assertTrue(packed.startswith("28b52ffd"))
            standardized = backend._standardize(
                {"sender_username": "wxid_user", "message_content": packed, "local_type": 1,
                 "create_time": int(time.time()), "local_id": f"z-{talker}"},
                "测试群", talker, is_group=talker.endswith("@chatroom"))
            self.assertEqual(standardized["content"], text)
            self.assertEqual(standardized["msg_type"], 1)
            self.assertEqual(standardized["media_kind"], "")
        blob = "0a1b" * 30                                     # a raw media payload is still a picture
        standardized = backend._standardize(
            {"sender_username": "wxid_user", "message_content": blob, "local_type": 1,
             "create_time": int(time.time()), "local_id": "b1"}, "测试群", "wxid_friend", is_group=False)
        self.assertEqual((standardized["content"], standardized["msg_type"]), ("[图片]", 3))

    def test_yang_yujing_target_group_uses_full_feed_burst_rules(self):
        backend = WcdbBackend(
            bot_display_name="林小满",
            groups=["群主应该是pyc"],
            config=BotConfig(
                chat_persona_profile="yang_yujing",
                yang_yujing_group_name="群主应该是pyc",
            ),
        )

        self.assertTrue(backend._uses_yang_yujing_full_feed({
            "group_name": "群主应该是pyc",
            "chat_id": "chat1@chatroom",
        }))
        self.assertFalse(backend._uses_yang_yujing_full_feed({
            "group_name": "测试群",
            "chat_id": "chat2@chatroom",
        }))

    def test_yang_yujing_poll_merges_rapid_same_sender_messages(self):
        backend = WcdbBackend(
            bot_display_name="林小满",
            groups=["群主应该是pyc"],
            config=BotConfig(
                chat_persona_profile="yang_yujing",
                yang_yujing_group_name="群主应该是pyc",
            ),
        )
        backend._client = FakeClient()
        backend._client.get_messages = Mock(return_value=[
            {
                "sender_username": "wxid_user",
                "message_content": "第二句",
                "local_type": 1,
                "create_time": int(time.time()),
                "local_id": "m2",
            },
            {
                "sender_username": "wxid_user",
                "message_content": "第一句",
                "local_type": 1,
                "create_time": int(time.time()),
                "local_id": "m1",
            },
        ])
        backend._chat_is_group["chat1@chatroom"] = True
        backend._running = True
        callback = Mock()

        backend._poll_group("群主应该是pyc", "chat1@chatroom", callback)
        ready = backend._burst.flush_all()

        callback.assert_not_called()
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["content"], "第一句\n第二句")
        self.assertEqual(ready[0]["_burst_count"], 2)

    def _poll_senders(self, my_wxid):
        backend = WcdbBackend(bot_display_name="杨宇靖", groups=["测试群"])
        backend._client = FakeClient()
        backend._client._config = {"myWxid": my_wxid} if my_wxid else {}
        names = {"wxid_me": "杨宇靖", "wxid_yang": "杨宇靖", "wxid_user": "测试用户"}
        backend._client.resolve_nickname = lambda wxid: names.get(wxid, wxid)
        backend._client.get_display_names = lambda usernames: {u: names.get(u, u) for u in usernames}
        now = int(time.time())
        backend._client.get_messages = Mock(return_value=[
            {"sender_username": who, "message_content": f"{who} 说的话", "local_type": 1,
             "create_time": now, "local_id": f"m-{who}"}
            for who in ("wxid_user", "wxid_yang", "wxid_me")
        ])
        backend._chat_is_group["room1@chatroom"] = True
        backend._running = True
        seen = []
        backend._dispatch_message = lambda msg, callback: seen.append(msg["sender_id"])
        backend._poll_group("测试群", "room1@chatroom", Mock())
        for msg in backend._burst.flush_all():
            seen.append(msg["sender_id"])
        return sorted(seen)

    def test_a_member_named_like_the_bot_is_still_heard(self):
        self.assertEqual(self._poll_senders("wxid_me"), ["wxid_user", "wxid_yang"])

    def test_quoting_a_member_named_like_the_bot_is_not_a_reply_to_it(self):
        xml = """wxid_user:
<?xml version="1.0"?>
<msg><appmsg><title>哈哈</title><type>57</type>
<refermsg><type>1</type><svrid>124</svrid><fromusr>chat1@chatroom</fromusr>
<chatusr>wxid_yang</chatusr><displayname>林小满</displayname><content>我说的</content></refermsg>
</appmsg></msg>"""
        msg = {"sender_username": "wxid_user", "message_content": self._zstd_hex(xml),
               "local_type": str((57 << 32) | 49), "create_time": int(time.time()), "local_id": "m_quote2"}
        standardized = self.make_backend()._standardize(msg, "测试群", "chat1@chatroom")
        self.assertFalse(standardized["is_reply_to_bot"])

    def test_without_the_accounts_wxid_the_name_still_marks_own_messages(self):
        self.assertEqual(self._poll_senders(""), ["wxid_user"])

    def test_yang_yujing_monitors_only_its_target_group(self):
        backend = WcdbBackend(
            bot_display_name="林小满",
            groups=["*"],
            config=BotConfig(
                chat_persona_profile="yang_yujing",
                yang_yujing_group_name="群主应该是pyc",
            ),
        )

        self.assertEqual(backend._groups, ["群主应该是pyc"])

    def test_yang_yujing_also_monitors_its_forced_reply_group(self):
        backend = WcdbBackend(
            bot_display_name="林小满",
            groups=["*"],
            config=BotConfig(
                chat_persona_profile="yang_yujing",
                yang_yujing_group_name="群主应该是pyc",
                yang_yujing_always_reply_group_name="shit bro 和emperor 还有maybe哥",
            ),
        )

        self.assertEqual(
            backend._groups,
            ["群主应该是pyc", "shit bro 和emperor 还有maybe哥"],
        )
        self.assertTrue(backend._uses_yang_yujing_full_feed({
            "group_name": "shit bro 和emperor 还有maybe哥",
            "chat_id": "chat2@chatroom",
        }))

    def test_custom_profile_keeps_configured_groups(self):
        backend = WcdbBackend(
            bot_display_name="林小满",
            groups=["*"],
            config=BotConfig(chat_persona_profile="custom"),
        )

        self.assertEqual(backend._groups, ["*"])

    def test_nickname_cache_does_not_call_optional_contacts_endpoint(self):
        client = object.__new__(WcdbNativeClient)
        client._nicknames = {}
        client.get_sessions = Mock(return_value=[{
            "username": "room1@chatroom", "displayName": "测试群",
        }])
        client.get_contacts = Mock(side_effect=AssertionError("must not be called"))

        with patch("src.wechat.wcdb_client.Path.exists", return_value=False):
            client._load_nickname_cache()

        self.assertEqual(client._nicknames, {"room1@chatroom": "测试群"})
        client.get_contacts.assert_not_called()

    def test_save_key_to_env_atomically_replaces_existing_file(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
            env_path = Path(tmp_dir) / ".env.local"
            env_path.write_text("WCDB_KEY=old\nOTHER=value\n", encoding="utf-8")
            fresh_key = "a" * 64

            with patch(
                "src.config.find_local_env_file", return_value=env_path,
            ):
                WcdbNativeClient._save_key_to_env(fresh_key)

            self.assertIn(f"WCDB_KEY={fresh_key}", env_path.read_text(encoding="utf-8"))
            self.assertFalse(env_path.with_suffix(".tmp").exists())

    def test_standardize_treats_long_hex_text_as_image_blob(self):
        msg = {
            "sender_username": "wxid_user",
            "message_content": "28b52ffd" * 20,
            "local_type": 1,
            "create_time": int(time.time()),
            "local_id": "m2",
        }

        standardized = self.make_backend()._standardize(
            msg, "测试群", "chat1@chatroom",
        )

        self.assertEqual(standardized["content"], "[图片]")
        self.assertEqual(standardized["msg_type"], 3)
        self.assertEqual(standardized["media_kind"], "image")

    def test_standardize_image_records_media_path_when_present(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
            tmp.write(b"\xff\xd8\xff" + b"x" * 200)
            image_path = tmp.name
        try:
            msg = {
                "sender_username": "wxid_user",
                "message_content": image_path,
                "local_type": 3,
                "create_time": int(time.time()),
                "local_id": "m_img_path",
            }

            standardized = self.make_backend()._standardize(
                msg, "测试群", "chat1@chatroom",
            )

            self.assertEqual(standardized["content"], "[图片]")
            self.assertEqual(standardized["msg_type"], 3)
            self.assertEqual(standardized["media_kind"], "image")
            self.assertEqual(standardized["media_path"], image_path)
        finally:
            Path(image_path).unlink(missing_ok=True)

    def test_standardize_strips_group_sender_prefix_from_text(self):
        msg = {
            "sender_username": "wxid_user",
            "message_content": "wxid_user:\n@林小满 推荐几首英文歌",
            "local_type": 1,
            "create_time": int(time.time()),
            "local_id": "m_prefix",
        }

        standardized = self.make_backend()._standardize(
            msg, "测试群", "chat1@chatroom",
        )

        self.assertEqual(standardized["content"], "@林小满 推荐几首英文歌")
        self.assertTrue(standardized["is_at_mentioned"])

    @staticmethod
    def _zstd_hex(text):
        return zstd.ZstdCompressor().compress(
            text.encode("utf-8")
        ).hex()

    def test_standardize_quote_reply_to_bot_triggers_chat(self):
        xml = """wxid_user:
<?xml version="1.0"?>
<msg>
  <appmsg>
    <title>都可以</title>
    <type>57</type>
    <refermsg>
      <type>1</type>
      <svrid>123</svrid>
      <fromusr>chat1@chatroom</fromusr>
      <chatusr>wxid_me</chatusr>
      <displayname>林小满</displayname>
      <content>啊？你喜欢哪种风格的呀</content>
    </refermsg>
  </appmsg>
</msg>"""
        msg = {
            "sender_username": "wxid_user",
            "message_content": self._zstd_hex(xml),
            "local_type": str((57 << 32) | 49),
            "create_time": int(time.time()),
            "local_id": "m_quote",
        }

        standardized = self.make_backend()._standardize(
            msg, "测试群", "chat1@chatroom",
        )

        self.assertEqual(standardized["content"], "都可以")
        self.assertEqual(standardized["msg_type"], 49)
        self.assertTrue(standardized["is_at_mentioned"])
        self.assertTrue(standardized["is_reply_to_bot"])
        self.assertEqual(
            standardized["quoted_content"],
            "啊？你喜欢哪种风格的呀",
        )
        self.assertEqual(standardized["quoted_sender_name"], "林小满")

    def test_standardize_private_chat_uses_talker_as_sender_fallback(self):
        msg = {
            "sender_username": "",
            "message_content": "你好呀",
            "local_type": 1,
            "create_time": int(time.time()),
            "local_id": "m3",
        }

        standardized = self.make_backend()._standardize(
            msg, "私聊好友", "wxid_friend", is_group=False,
        )

        self.assertFalse(standardized["is_group"])
        self.assertFalse(standardized["is_at_mentioned"])
        self.assertEqual(standardized["sender_id"], "wxid_friend")
        self.assertEqual(standardized["sender_name"], "私聊好友")
        self.assertEqual(standardized["group_name"], "私聊好友")

    def test_resolve_groups_can_add_private_chats_when_enabled(self):
        sessions = [
            {"username": "room1@chatroom", "displayName": "测试群"},
            {"username": "wxid_friend", "displayName": "私聊好友"},
            {"username": "filehelper", "displayName": "文件传输助手"},
            {"username": "gh_public", "displayName": "公众号"},
            {"username": "mcdonalds888", "displayName": "麦当劳"},
        ]
        backend = WcdbBackend(
            bot_display_name="林小满",
            groups=["*"],
            config=BotConfig(private_chat_enabled=True, chat_persona_profile="custom"),
        )
        backend._client = FakeClient(sessions=sessions)

        backend._resolve_groups()

        self.assertEqual(backend._talker_ids["测试群"], "room1@chatroom")
        self.assertEqual(backend._talker_ids["私聊好友"], "wxid_friend")
        self.assertTrue(backend._chat_is_group["room1@chatroom"])
        self.assertFalse(backend._chat_is_group["wxid_friend"])
        self.assertNotIn("文件传输助手", backend._talker_ids)
        self.assertNotIn("公众号", backend._talker_ids)
        self.assertNotIn("麦当劳", backend._talker_ids)

    def test_private_chat_refresh_adds_new_session_after_startup(self):
        client = FakeClient(
            sessions=[{"username": "room1@chatroom", "displayName": "测试群"}]
        )
        backend = WcdbBackend(
            bot_display_name="林小满",
            groups=["*"],
            config=BotConfig(private_chat_enabled=True, chat_persona_profile="custom"),
        )
        backend._client = client
        backend._resolve_groups()
        backend._last_session_refresh = 0

        client.sessions.append(
            {"username": "wxid_friend", "displayName": "私聊好友"}
        )
        backend._refresh_private_chats_if_due()

        self.assertEqual(backend._talker_ids["私聊好友"], "wxid_friend")
        self.assertFalse(backend._chat_is_group["wxid_friend"])


if __name__ == "__main__":
    unittest.main()
