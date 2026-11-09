"""The console's settings endpoints:

- GET/POST /api/advanced (src/web/advanced_config.py): sending, relay, PRM, OCR, trace;
- onboarding: a hand-typed WeChat key in step 2, the private chats in step 3 (the last);
- /api/load-config: group/contact names as lists, no chat-bot keys;
- the chat-bot endpoints (sandbox, nicknames, todos, lots, welcome) are gone.

Every test works on temporary .env files; nothing touches the project's .env.
"""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class _Sock:
    def __init__(self, raw: bytes):
        self._r = io.BytesIO(raw)
        self._w = io.BytesIO()
        self.chunks: list[bytes] = []

    def makefile(self, mode, *a, **k):
        return self._r if mode == "rb" else self._w

    def sendall(self, data):
        self.chunks.append(data)

    def response(self):
        text = b"".join(self.chunks).decode("utf-8", errors="replace")
        head, _, body = text.partition("\r\n\r\n")
        return int(head.split()[1]), (json.loads(body) if body.strip() else None)


def _request(path, method="GET", payload=None, headers=None):
    from src.web.server import _UIHandler

    body = b"" if payload is None else (payload if isinstance(payload, bytes) else json.dumps(payload).encode())
    extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    raw = (
        f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{extra}"
        + (f"Content-Length: {len(body)}\r\nContent-Type: application/json\r\n" if body else "")
        + "\r\n"
    ).encode() + body
    sock = _Sock(raw)
    _UIHandler(sock, ("127.0.0.1", 9999), MagicMock())
    return sock.response()


class _TempEnv(unittest.TestCase):
    """A temporary shared .env and machine .env.local for each test."""

    shared_text = ""
    local_text = None

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.env = self.dir / ".env"
        self.env.write_text(self.shared_text, encoding="utf-8")
        self.local = self.dir / ".env.local"
        if self.local_text is not None:
            self.local.write_text(self.local_text, encoding="utf-8")
        patches = [
            patch("src.web.server._find_or_create_env", return_value=self.env),
            patch("src.web.server._find_or_create_machine_env", return_value=self.local),
            patch("src.config.find_local_env_file", return_value=self.local if self.local_text is not None else None),
            patch.dict(os.environ, {}, clear=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def env_values(self, path=None):
        out = {}
        for line in (path or self.env).read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
        return out


class AdvancedGetTests(_TempEnv):
    shared_text = "# comment\nSEND_QUEUE=true\nSEND_QUIET=yes\nCLAUDE_RELAY_TOKEN=abcdefghijkl1234abcd\n"
    local_text = "SEND_QUIET=on\nCLAUDE_RELAY_PROGRESS_MIN=\n"

    def test_returns_every_allow_listed_field_with_normalized_values(self):
        from src.web import advanced_config as adv

        status, body = _request("/api/advanced")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        fields = {f["key"]: f for g in body["groups"] for f in g["fields"]}
        self.assertEqual(set(fields), set(adv.FIELDS))
        self.assertEqual(fields["SEND_QUEUE"]["value"], "on")              # alias "true"
        self.assertEqual(fields["SEND_QUIET"]["value"], "on")
        self.assertEqual(fields["CLAUDE_RELAY_PROGRESS_MIN"]["value"], "3")  # default
        self.assertEqual(fields["STICKER_PASTE"]["value"], "file")

    def test_only_the_gateways_switches_are_offered(self):
        from src.web import advanced_config as adv

        self.assertEqual([g["id"] for g in adv.GROUPS], ["send", "relay", "prm", "ocr", "trace"])
        for gone in ("PIPELINE_V2", "MEMORY_V2", "MODEL_ROUTE", "PERSONA_EXAMPLES", "WECHAT_EMOJI", "SEND_STICKERS",
                     "SEND_BUBBLES", "PROACTIVE_GATE_MODE", "IMAGE_VISION_ENABLED", "CLAUDE_CHAT_MODEL", "BOT_ALIASES"):
            self.assertNotIn(gone, adv.FIELDS)

    def test_secret_is_masked(self):
        _, body = _request("/api/advanced")
        fields = {f["key"]: f for g in body["groups"] for f in g["fields"]}
        self.assertIn("***", fields["CLAUDE_RELAY_TOKEN"]["value"])
        self.assertNotIn("abcdefghijkl", json.dumps(body))

    def test_reports_keys_overridden_by_env_local(self):
        _, body = _request("/api/advanced")
        self.assertEqual(body["local_overrides"], ["SEND_QUIET"])   # an empty value is not an override


class AdvancedPostTests(_TempEnv):
    shared_text = "AI_BACKEND=deepseek\n# keep me\nSEND_QUEUE=off\nCLAUDE_RELAY_TOKEN=stored-secret-token-0000\n"

    def test_writes_only_posted_keys_and_updates_environment(self):
        before = self.env.read_text(encoding="utf-8")
        status, body = _request("/api/advanced", "POST", {"values": {"SEND_QUEUE": "yes", "CLAUDE_RELAY_PROGRESS_MIN": "6"}})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["saved"], ["CLAUDE_RELAY_PROGRESS_MIN", "SEND_QUEUE"])
        values = self.env_values()
        self.assertEqual(values["SEND_QUEUE"], "on")                     # written the canonical way
        self.assertEqual(values["CLAUDE_RELAY_PROGRESS_MIN"], "6")
        self.assertEqual(values["AI_BACKEND"], "deepseek")               # an old line stays as it was
        self.assertIn("# keep me", self.env.read_text(encoding="utf-8"))
        self.assertEqual(len(self.env.read_text(encoding="utf-8").splitlines()), len(before.splitlines()) + 1)
        self.assertEqual(os.environ["SEND_QUEUE"], "on")

    def test_rejects_invalid_values_and_writes_nothing(self):
        before = self.env.read_text(encoding="utf-8")
        cases = [
            {"CLAUDE_RELAY_PROGRESS_MIN": "121"},
            {"CLAUDE_RELAY_PROGRESS_MIN": "2.5"},
            {"OCR_POLL_SEC": "0.5"},
            {"SEND_QUIET": "maybe"},
            {"PRM_HOLDING_TEXT": "好\nSEND_QUIET=on"},
            {"CLAUDE_RELAY_WXIDS": "a\u2028CLAUDE_RELAY_TOKEN=attacker"},   # str.splitlines() splits here
            {"CLAUDE_RELAY_HOST": "0.0.0.0 #frag"},                         # dotenv would cut at " #"
            {"STICKER_PASTE": "gif"},
            {"WCDB_KEY": "0" * 64},                                         # not allow-listed
            {"PIPELINE_V2": "single"},                                      # the chat bot's: not any more
        ]
        for values in cases:
            with self.subTest(values=values):
                status, body = _request("/api/advanced", "POST", {"values": values})
                self.assertEqual(status, 200)
                self.assertFalse(body["ok"])
                self.assertIn(next(iter(values)), body["errors"])
        self.assertEqual(self.env.read_text(encoding="utf-8"), before)

    def test_cross_field_check_runs_validate_config(self):
        status, body = _request("/api/advanced", "POST", {"values": {"PRM_COPILOT": "on"}})
        self.assertFalse(body["ok"])
        self.assertIn("PRM_COPILOT", body["errors"])
        status, body = _request("/api/advanced", "POST", {"values": {
            "PRM_COPILOT": "on", "CLAUDE_RELAY": "on", "ADMIN_WXID": "wxid_admin",
        }})
        self.assertTrue(body["ok"], body)

    def test_an_error_is_shown_on_the_field_it_names_not_on_a_shorter_key(self):
        """PRM_FRIEND_ADD_BRAND's message starts with PRM_FRIEND_ADD too (加百应达人微信, 功能 R)."""
        status, body = _request("/api/advanced", "POST", {"values": {"PRM_FRIEND_ADD_BRAND": "牌" * 21}})
        self.assertFalse(body["ok"])
        self.assertIn("PRM_FRIEND_ADD_BRAND", body["errors"])
        self.assertNotIn("PRM_FRIEND_ADD", body["errors"])

    def test_the_group_settings_are_shown_and_their_errors_land_on_them(self):
        """三方群 (src/prm/three_party.py): PRM_GROUP_WITH empty by default (no 「拉三方群」), PRM_GROUP_NAME's default; a bad
        value is refused on its own field."""
        groups = _request("/api/advanced")[1]["groups"]
        fields = {f["key"]: f for g in groups for f in g["fields"]}
        self.assertEqual((fields["PRM_GROUP_WITH"]["value"], fields["PRM_GROUP_NAME"]["value"]), ("", "{品牌}×{昵称}"))
        for key, value in (("PRM_GROUP_WITH", "a,b,c,d"), ("PRM_GROUP_NAME", "三方群")):
            status, body = _request("/api/advanced", "POST", {"values": {key: value}})
            self.assertFalse(body["ok"])
            self.assertEqual(list(body["errors"]), [key], body)

    def test_reply_parts_is_shown_and_its_error_lands_on_it(self):
        """PRM_REPLY_PARTS (src/prm/service.py reply_parts, 2026-10-30): 3 by default; 0 or 6 is refused on its own field."""
        groups = _request("/api/advanced")[1]["groups"]
        fields = {f["key"]: f for g in groups for f in g["fields"]}
        self.assertEqual(fields["PRM_REPLY_PARTS"]["value"], "3")
        for value in ("0", "6"):
            status, body = _request("/api/advanced", "POST", {"values": {"PRM_REPLY_PARTS": value}})
            self.assertFalse(body["ok"])
            self.assertEqual(list(body["errors"]), ["PRM_REPLY_PARTS"], body)

    def test_send_pace_is_shown_after_reply_parts_and_on_by_default(self):
        """PRM_SEND_PACE (发得慢一点, 2026-11-04): an on/off switch right after PRM_REPLY_PARTS, on by default; a bad value is
        refused on its own field."""
        groups = _request("/api/advanced")[1]["groups"]
        keys = [f["key"] for g in groups for f in g["fields"]]
        fields = {f["key"]: f for g in groups for f in g["fields"]}
        self.assertEqual(keys.index("PRM_SEND_PACE"), keys.index("PRM_REPLY_PARTS") + 1)
        self.assertEqual((fields["PRM_SEND_PACE"]["type"], fields["PRM_SEND_PACE"]["value"]), ("onoff", "on"))
        status, body = _request("/api/advanced", "POST", {"values": {"PRM_SEND_PACE": "maybe"}})
        self.assertFalse(body["ok"])
        self.assertEqual(list(body["errors"]), ["PRM_SEND_PACE"], body)

    def test_the_boss_group_settings_are_shown_and_their_errors_land_on_them(self):
        """老板拉的群 (src/prm/boss_groups.py, 2026-10-31): off, 「老板」 and 6 by default; PRM_BOSS_CALL (review 2026-10-31:
        what the Mac is told the boss is called) right after the switch; a bad value is refused on its own field
        (PRM_BOSS_GROUP_MAX's and PRM_BOSS_CALL's messages do not start with PRM_BOSS_GROUPS)."""
        from src.web import advanced_config as adv

        groups = _request("/api/advanced")[1]["groups"]
        fields = {f["key"]: f for g in groups for f in g["fields"]}
        self.assertEqual((fields["PRM_BOSS_GROUPS"]["value"], fields["PRM_BOSS_CALL"]["value"],
                          fields["PRM_BOSS_GROUP_MAX"]["value"]), ("off", "老板", "6"))
        for key in ("PRM_BOSS_GROUP_MAX", "PRM_BOSS_CALL"):
            self.assertEqual(fields[key]["requires"][0]["key"], "PRM_BOSS_GROUPS")
        prm = next(g for g in groups if g["id"] == "prm")
        self.assertEqual([f["key"] for f in prm["fields"]][-4:-1], ["PRM_BOSS_GROUPS", "PRM_BOSS_CALL", "PRM_BOSS_GROUP_MAX"])
        for key, value in (("PRM_BOSS_GROUP_MAX", "2"), ("PRM_BOSS_GROUP_MAX", "21"), ("PRM_BOSS_GROUPS", "maybe"),
                           ("PRM_BOSS_CALL", "一二三四五六七八九十一")):
            status, body = _request("/api/advanced", "POST", {"values": {key: value}})
            self.assertFalse(body["ok"])
            self.assertEqual(list(body["errors"]), [key], body)
        self.assertEqual(adv.validate_updates({"PRM_BOSS_GROUPS": "on", "PRM_BOSS_GROUP_MAX": "10",
                                               "PRM_BOSS_CALL": "大老板"}, {}), {})
        self.assertEqual(adv.normalize("PRM_BOSS_CALL", ""), "老板")             # (empty: the default)

    def test_linked_groups_auto_is_shown_last_off_by_default(self):
        """PRM_LINKED_GROUPS_AUTO (接手任何合作群, decision 2026-11-10): an on/off switch after the 老板拉的群 settings, off by
        default (a group linked by hand: its drafts always wait for a person), used with the copilot on, not with
        PRM_BOSS_GROUPS (a link by hand works whatever that says); a bad value is refused on its own field."""
        from src.web import advanced_config as adv

        groups = _request("/api/advanced")[1]["groups"]
        prm = next(g for g in groups if g["id"] == "prm")
        field = prm["fields"][-1]
        self.assertEqual((field["key"], field["type"], field["value"]), ("PRM_LINKED_GROUPS_AUTO", "onoff", "off"))
        self.assertEqual(field["requires"][0]["key"], "PRM_COPILOT")
        self.assertIn("一律等你看过再发", field["help"])
        status, body = _request("/api/advanced", "POST", {"values": {"PRM_LINKED_GROUPS_AUTO": "maybe"}})
        self.assertFalse(body["ok"])
        self.assertEqual(list(body["errors"]), ["PRM_LINKED_GROUPS_AUTO"], body)
        self.assertEqual(adv.validate_updates({"PRM_LINKED_GROUPS_AUTO": "on"}, {}), {})
        self.assertEqual(adv.normalize("PRM_LINKED_GROUPS_AUTO", "yes"), "on")

    def test_wechat_review_is_shown_after_auto_register_off_by_default(self):
        """PRM_WECHAT_REVIEW (微信里审批草稿, src/prm/wechat_review.py, 2026-11-09): an on/off switch right after
        PRM_AUTO_REGISTER, off by default, used with the copilot on; a bad value is refused on its own field; on without
        ADMIN_WXID is not refused (待审批 says why)."""
        from src.web import advanced_config as adv

        groups = _request("/api/advanced")[1]["groups"]
        prm = next(g for g in groups if g["id"] == "prm")
        keys = [f["key"] for f in prm["fields"]]
        self.assertEqual(keys.index("PRM_WECHAT_REVIEW"), keys.index("PRM_AUTO_REGISTER") + 1)
        field = next(f for f in prm["fields"] if f["key"] == "PRM_WECHAT_REVIEW")
        self.assertEqual((field["type"], field["value"], field["requires"][0]["key"]), ("onoff", "off", "PRM_COPILOT"))
        status, body = _request("/api/advanced", "POST", {"values": {"PRM_WECHAT_REVIEW": "maybe"}})
        self.assertFalse(body["ok"])
        self.assertEqual(list(body["errors"]), ["PRM_WECHAT_REVIEW"], body)
        self.assertEqual(adv.validate_updates({"PRM_WECHAT_REVIEW": "on", "ADMIN_WXID": ""}, {}), {})

    def test_the_remark_can_be_saved_empty_and_stays_empty(self):
        """PRM_FRIEND_ADD_REMARK (加百应达人微信, 功能 R): unset shows the default; saved empty it stays empty (no remark),
        where any other empty text field would be its default; a bad one is refused on its own field."""
        def shown():
            groups = _request("/api/advanced")[1]["groups"]
            return next(f for g in groups for f in g["fields"] if f["key"] == "PRM_FRIEND_ADD_REMARK")

        self.assertEqual((shown()["value"], shown()["placeholder"]), ("抖音-{昵称}", "空着：不设备注"))
        status, body = _request("/api/advanced", "POST", {"values": {"PRM_FRIEND_ADD_REMARK": ""}})
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.env_values()["PRM_FRIEND_ADD_REMARK"], "")
        self.assertEqual(shown()["value"], "")
        status, body = _request("/api/advanced", "POST", {"values": {"PRM_FRIEND_ADD_REMARK": "抖音-{昵称}{昵称}"}})
        self.assertFalse(body["ok"])
        self.assertIn("PRM_FRIEND_ADD_REMARK", body["errors"])
        self.assertNotIn("PRM_FRIEND_ADD", body["errors"])
        status, body = _request("/api/advanced", "POST", {"values": {"PRM_FRIEND_ADD_GREETING": ""}})
        self.assertTrue(body["ok"], body)                                       # (the greeting: empty is its default)
        self.assertEqual(self.env_values()["PRM_FRIEND_ADD_GREETING"], "您好，我是{品牌}，百应上联系您的～")

    def test_masked_secret_keeps_the_stored_one(self):
        status, body = _request("/api/advanced", "POST", {"values": {"CLAUDE_RELAY_TOKEN": "stor***0000",
                                                                     "SEND_TYPING_DELAY": "on"}})
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["saved"], ["SEND_TYPING_DELAY"])
        self.assertEqual(self.env_values()["CLAUDE_RELAY_TOKEN"], "stored-secret-token-0000")

    def test_every_duplicate_of_a_key_is_replaced(self):
        self.env.write_text("SEND_QUIET=off\nAI_BACKEND=deepseek\nSEND_QUIET=on\n", encoding="utf-8")
        status, body = _request("/api/advanced", "POST", {"values": {"SEND_QUIET": "off"}})
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.env.read_text(encoding="utf-8"), "SEND_QUIET=off\nAI_BACKEND=deepseek\nSEND_QUIET=off\n")

    def test_cross_origin_post_is_refused(self):
        before = self.env.read_text(encoding="utf-8")
        status, body = _request("/api/advanced", "POST", {"values": {"SEND_TYPING_DELAY": "on"}},
                                headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.env.read_text(encoding="utf-8"), before)

    def test_a_get_with_a_body_does_not_write(self):
        before = self.env.read_text(encoding="utf-8")
        status, body = _request("/api/advanced", "GET", {"values": {"SEND_TYPING_DELAY": "on"}})
        self.assertEqual(status, 200)
        self.assertIn("groups", body)
        self.assertEqual(self.env.read_text(encoding="utf-8"), before)

    def test_bad_requests(self):
        for payload in (b"not json", {"values": {}}, {"values": "x"}, ["x"]):
            with self.subTest(payload=payload):
                status, body = _request("/api/advanced", "POST", payload)
                self.assertEqual(status, 200)
                self.assertFalse(body["ok"])


class AllowListMatchesValidateConfig(unittest.TestCase):
    """The table's ranges must agree with src/config.py::_validate_config.

    Range checks happen twice: per field in advanced_config.check_value and,
    as a backstop, in _validate_config. If someone changes one and not the
    other, either the console offers values the bot refuses or refuses values
    the bot accepts; probing the boundaries through _validate_config alone
    catches both.
    """

    def _validate_only(self, key, value, **extra):
        from src.config import _validate_config
        from src.web import advanced_config as adv

        merged = {k: adv.normalize(k, None) for k in adv.FIELDS}
        merged[key] = value
        merged.update(extra)
        try:
            _validate_config(adv._kwargs(merged))
        except RuntimeError as exc:
            return str(exc)
        return ""

    def test_numeric_boundaries(self):
        from src.web import advanced_config as adv

        for key, field in adv.FIELDS.items():
            if field["type"] not in ("int", "float"):
                continue
            step = 1 if field["type"] == "int" else 0.01
            lo, hi = field["min"], field["max"]
            with self.subTest(key=key):
                self.assertEqual(self._validate_only(key, str(lo)), "", f"{key}={lo} should pass")
                self.assertNotEqual(self._validate_only(key, str(lo - step)), "", f"{key} below min should fail")
                if field.get("max_exclusive"):
                    self.assertNotEqual(self._validate_only(key, str(hi)), "", f"{key}={hi} should fail")
                    self.assertEqual(self._validate_only(key, str(round(hi - step, 4))), "")
                else:
                    self.assertEqual(self._validate_only(key, str(hi)), "", f"{key}={hi} should pass")
                    self.assertNotEqual(self._validate_only(key, str(hi + step)), "", f"{key} above max should fail")

    def test_every_option_and_default_is_accepted(self):
        from src.web import advanced_config as adv

        for key, field in adv.FIELDS.items():
            with self.subTest(key=key):
                self.assertEqual(adv.check_value(key, field["default"]), "")
                for option in field.get("options", []):
                    self.assertEqual(adv.check_value(key, option), "")
                    self.assertEqual(self._validate_only(key, option), "")


class OnboardingManualKeyTests(_TempEnv):
    shared_text = ""
    local_text = ""

    def setUp(self):
        super().setUp()
        from src.web import server

        saved = dict(server._onboarding_data)
        self.addCleanup(lambda: (server._onboarding_data.clear(), server._onboarding_data.update(saved)))

    def test_rejects_a_malformed_key(self):
        status, body = _request("/api/onboarding/step2", "POST", {"bot_display_name": "bot", "wcdb_key": "abc"})
        self.assertFalse(body["ok"])
        self.assertNotIn("WCDB_KEY", self.env_values(self.local))

    def test_stores_a_typed_key_like_an_extracted_one(self):
        from src.web import server

        key = "ab" * 32
        status, body = _request("/api/onboarding/step2", "POST", {"bot_display_name": "bot", "wcdb_key": key})
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.env_values(self.local)["WCDB_KEY"], key)
        self.assertEqual(server._onboarding_data["key"], key)
        self.assertEqual(os.environ["WCDB_KEY"], key)

    def test_without_a_key_nothing_is_written_to_the_machine_env(self):
        status, body = _request("/api/onboarding/step2", "POST", {"bot_display_name": "bot"})
        self.assertTrue(body["ok"])
        self.assertEqual(self.local.read_text(encoding="utf-8"), "")


class OnboardingStep3WithMachineEnv(_TempEnv):
    """With a separate machine .env.local the key stays out of the shared .env."""
    shared_text = ""
    local_text = ""

    def test_key_goes_to_the_machine_env_only(self):
        from src.web import server

        saved = dict(server._onboarding_data)
        self.addCleanup(lambda: (server._onboarding_data.clear(), server._onboarding_data.update(saved)))
        key = "cd" * 32
        _request("/api/onboarding/step2", "POST", {"bot_display_name": "bot", "wcdb_key": key})
        status, body = _request("/api/onboarding/step3", "POST", {"private_chat_enabled": True})
        self.assertTrue(body["ok"], body)
        self.assertNotIn("WCDB_KEY", self.env_values())
        self.assertEqual(self.env_values()["ONBOARDING_DONE"], "true")
        self.assertEqual(self.env_values(self.local)["WCDB_KEY"], key)


class OnboardingWritesOnlyTheGatewaysKeys(_TempEnv):
    shared_text = ""
    local_text = ""

    def test_step3_writes_the_private_chats_and_no_chat_switch(self):
        from src.web import server

        saved = dict(server._onboarding_data)
        self.addCleanup(lambda: (server._onboarding_data.clear(), server._onboarding_data.update(saved)))
        _request("/api/onboarding/step2", "POST", {"bot_display_name": "商务号", "wechat_groups": "*"})
        status, body = _request("/api/onboarding/step3", "POST", {"private_chat_enabled": True,
                                                                   "private_chats": "wxid_me"})
        self.assertTrue(body["ok"], body)
        values = self.env_values()
        self.assertEqual((values["PRIVATE_CHAT_ENABLED"], values["PRIVATE_CHATS"]), ("true", "wxid_me"))
        self.assertEqual(values["BOT_DISPLAY_NAME"], "商务号")
        for gone in ("AI_BACKEND", "DEEPSEEK_API_KEY", "FUN_ENABLED", "PROACTIVE_ENABLED", "STICKY_MENTION_ENABLED",
                     "FOLLOWUP_LISTEN_ENABLED", "WEB_SEARCH_ENABLED", "MODEL_ROUTE"):
            self.assertNotIn(gone, values)

    def test_there_is_no_fourth_step(self):
        status, body = _request("/api/onboarding/step4", "POST", {"fun_enabled": True})
        self.assertEqual(status, 405)


class LoadConfigNames(_TempEnv):
    shared_text = (
        "CHAT_PERSONA_PROFILE=custom\n"                 # an old chat-bot line: never read, never shown
        "WECHAT_GROUPS=%E6%B5%8B%E8%AF%95%2C%E7%BE%A4,%E8%AF%BB%E4%B9%A6%E7%BE%A4\n"
        "PRIVATE_CHATS=*\n"
    )

    def test_names_come_back_as_lists(self):
        _, body = _request("/api/load-config")
        cfg = body["config"]
        self.assertEqual(cfg["wechat_groups_list"], ["测试,群", "读书群"])
        self.assertEqual(cfg["private_chats_list"], [])
        self.assertFalse([k for k in cfg if k.startswith(("chat_", "yang_", "feishu_", "todo_"))])

    def test_saving_leaves_the_old_lines_alone(self):
        _, body = _request("/api/load-config")
        status, res = _request("/api/config", "POST", dict(body["config"]))
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.env_values()["CHAT_PERSONA_PROFILE"], "custom")


class TheChatEndpointsAreGone(_TempEnv):
    def test_they_answer_nothing(self):
        for path in ("/api/sandbox/test", "/api/nicknames", "/api/todos/action", "/api/lots", "/api/welcome/templates"):
            with self.subTest(path=path):
                status, body = _request(path, "POST", {"message": "你好"})
                self.assertEqual(status, 405)
        for path in ("/api/nicknames/groups", "/api/todos", "/api/lots", "/api/welcome/templates"):
            with self.subTest(path=path):
                try:
                    status, body = _request(path)
                except ValueError:              # not JSON: the console page, no API behind the path
                    continue
                self.assertNotEqual((status, (body or {}).get("ok")), (200, True))


if __name__ == "__main__":
    unittest.main()
