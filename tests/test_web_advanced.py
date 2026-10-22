"""Backend changes that came with the console redesign:

- GET/POST /api/advanced (src/web/advanced_config.py);
- onboarding: a hand-typed WeChat key in step 2, FUN_ENABLED in step 4,
  "everything on a local model" in step 3;
- /api/load-config: the custom persona's values while Yang is selected (the
  preset comes separately), group/contact names as lists;
- the sandbox replies with the model MODEL_ROUTE picks for chat.

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
    shared_text = "# comment\nPIPELINE_V2=true\nSEND_QUIET=yes\nEMBEDDING_API_KEY=sk-abcdefghijkl1234\n"
    local_text = "SEND_QUIET=on\nMEMORY_V2=\n"

    def test_returns_every_allow_listed_field_with_normalized_values(self):
        from src.web import advanced_config as adv

        status, body = _request("/api/advanced")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        fields = {f["key"]: f for g in body["groups"] for f in g["fields"]}
        self.assertEqual(set(fields), set(adv.FIELDS))
        self.assertEqual(fields["PIPELINE_V2"]["value"], "single")   # alias "true"
        self.assertEqual(fields["SEND_QUIET"]["value"], "on")
        self.assertEqual(fields["MEMORY_V2"]["value"], "off")         # default
        self.assertEqual(fields["PERSONA_EXAMPLES"]["value"], "4")

    def test_secret_is_masked(self):
        _, body = _request("/api/advanced")
        fields = {f["key"]: f for g in body["groups"] for f in g["fields"]}
        self.assertIn("***", fields["EMBEDDING_API_KEY"]["value"])
        self.assertNotIn("abcdefghijkl", json.dumps(body))

    def test_reports_keys_overridden_by_env_local(self):
        _, body = _request("/api/advanced")
        self.assertEqual(body["local_overrides"], ["SEND_QUIET"])   # empty MEMORY_V2= is not an override


class AdvancedPostTests(_TempEnv):
    shared_text = "AI_BACKEND=deepseek\n# keep me\nPIPELINE_V2=off\nEMBEDDING_API_KEY=sk-stored-secret-0000\n"

    def test_writes_only_posted_keys_and_updates_environment(self):
        before = self.env.read_text(encoding="utf-8")
        status, body = _request("/api/advanced", "POST", {"values": {"PIPELINE_V2": "two-step", "PERSONA_EXAMPLES": "6"}})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["saved"], ["PERSONA_EXAMPLES", "PIPELINE_V2"])
        values = self.env_values()
        self.assertEqual(values["PIPELINE_V2"], "two_step")            # written the canonical way
        self.assertEqual(values["PERSONA_EXAMPLES"], "6")
        self.assertEqual(values["AI_BACKEND"], "deepseek")
        self.assertIn("# keep me", self.env.read_text(encoding="utf-8"))
        self.assertEqual(len(self.env.read_text(encoding="utf-8").splitlines()), len(before.splitlines()) + 1)
        self.assertEqual(os.environ["PIPELINE_V2"], "two_step")

    def test_rejects_invalid_values_and_writes_nothing(self):
        before = self.env.read_text(encoding="utf-8")
        cases = [
            {"PERSONA_EXAMPLES": "9"},
            {"PERSONA_EXAMPLES": "2.5"},
            {"MEMORY_EMBEDDING_MIN_SIM": "1"},
            {"SEND_QUIET": "maybe"},
            {"STICKER_DIR": "data/stickers\nPIPELINE_V2=single"},
            {"BOT_ALIASES": "a\u2028DEEPSEEK_API_KEY=attacker"},       # str.splitlines() splits here
            {"EMBEDDING_BASE_URL": "http://x #frag"},                 # dotenv would cut at " #"
            {"STICKER_DIR": "   "},
            {"WCDB_KEY": "0" * 64},                                  # not allow-listed
            {"WECHAT_EMOJI_CODES": "[]"},
        ]
        for values in cases:
            with self.subTest(values=values):
                status, body = _request("/api/advanced", "POST", {"values": values})
                self.assertEqual(status, 200)
                self.assertFalse(body["ok"])
                self.assertIn(next(iter(values)), body["errors"])
        self.assertEqual(self.env.read_text(encoding="utf-8"), before)

    def test_cross_field_check_runs_validate_config(self):
        status, body = _request("/api/advanced", "POST", {"values": {"MEMORY_EMBEDDINGS": "on", "EMBEDDING_API_KEY": ""}})
        self.assertFalse(body["ok"])
        self.assertIn("MEMORY_EMBEDDINGS", body["errors"])
        status, body = _request("/api/advanced", "POST", {"values": {
            "MEMORY_EMBEDDINGS": "on", "EMBEDDING_MODEL": "BAAI/bge-m3", "EMBEDDING_BASE_URL": "http://127.0.0.1:8000/v1",
        }})
        self.assertTrue(body["ok"], body)

    def test_masked_secret_keeps_the_stored_one(self):
        status, body = _request("/api/advanced", "POST", {"values": {"EMBEDDING_API_KEY": "sk-s***0000", "SEND_BUBBLES": "on"}})
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["saved"], ["SEND_BUBBLES"])
        self.assertEqual(self.env_values()["EMBEDDING_API_KEY"], "sk-stored-secret-0000")

    def test_every_duplicate_of_a_key_is_replaced(self):
        self.env.write_text("SEND_QUIET=off\nAI_BACKEND=deepseek\nSEND_QUIET=on\n", encoding="utf-8")
        status, body = _request("/api/advanced", "POST", {"values": {"SEND_QUIET": "off"}})
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.env.read_text(encoding="utf-8"), "SEND_QUIET=off\nAI_BACKEND=deepseek\nSEND_QUIET=off\n")

    def test_cross_origin_post_is_refused(self):
        before = self.env.read_text(encoding="utf-8")
        status, body = _request("/api/advanced", "POST", {"values": {"SEND_BUBBLES": "on"}},
                                headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.env.read_text(encoding="utf-8"), before)

    def test_a_get_with_a_body_does_not_write(self):
        before = self.env.read_text(encoding="utf-8")
        status, body = _request("/api/advanced", "GET", {"values": {"SEND_BUBBLES": "on"}})
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
                    # a local route and the local fallback need the model name (cross-field)
                    extra = {"LOCAL_MODEL": "qwen3.5:4b"} if key in ("MODEL_ROUTE", "CLOUD_FALLBACK") else {}
                    self.assertEqual(self._validate_only(key, option, **extra), "")


class LocalModelSettings(_TempEnv):
    """MODEL_ROUTE / LOCAL_* from main 25f3494."""
    shared_text = "LOCAL_EXTRA_BODY=\n"

    def test_empty_extra_body_stays_empty(self):
        from src.web import advanced_config as adv

        self.assertEqual(adv.normalize("LOCAL_EXTRA_BODY", ""), "")           # "" = send nothing extra
        self.assertIn("enable_thinking", adv.normalize("LOCAL_EXTRA_BODY", None))
        self.assertIn("reasoning_effort", adv.normalize("LOCAL_EXTRA_BODY", None))
        _, body = _request("/api/advanced")
        fields = {f["key"]: f for g in body["groups"] for f in g["fields"]}
        self.assertEqual(fields["LOCAL_EXTRA_BODY"]["value"], "")

    def test_json_and_task_checks(self):
        from src.web import advanced_config as adv

        self.assertEqual(adv.check_value("LOCAL_EXTRA_BODY", '{"reasoning_effort": "none"}'), "")
        self.assertNotEqual(adv.check_value("LOCAL_EXTRA_BODY", "[1, 2]"), "")
        self.assertNotEqual(adv.check_value("LOCAL_EXTRA_BODY", "{broken"), "")
        self.assertEqual(adv.check_value("LOCAL_TASKS", "chat, Memory"), "")
        self.assertEqual(adv.normalize("LOCAL_TASKS", "chat, Memory"), "chat,memory")
        self.assertNotEqual(adv.check_value("LOCAL_TASKS", "chat,vision"), "")
        self.assertNotEqual(adv.check_value("LOCAL_BASE_URL", "127.0.0.1:11434"), "")

    def test_local_route_needs_a_model_name(self):
        status, body = _request("/api/advanced", "POST", {"values": {"MODEL_ROUTE": "local"}})
        self.assertFalse(body["ok"])
        self.assertIn("MODEL_ROUTE", body["errors"])
        status, body = _request("/api/advanced", "POST", {"values": {"MODEL_ROUTE": "split", "LOCAL_MODEL": "qwen3.5:4b", "LOCAL_TASKS": "memory,proactive"}})
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.env_values()["LOCAL_TASKS"], "memory,proactive")


class OnboardingManualKeyTests(_TempEnv):
    shared_text = "AI_BACKEND=deepseek\n"
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


class OnboardingStep4WithMachineEnv(_TempEnv):
    """With a separate machine .env.local the key stays out of the shared .env."""
    shared_text = ""
    local_text = ""

    def test_key_goes_to_the_machine_env_only(self):
        from src.web import server

        saved = dict(server._onboarding_data)
        self.addCleanup(lambda: (server._onboarding_data.clear(), server._onboarding_data.update(saved)))
        key = "cd" * 32
        _request("/api/onboarding/step2", "POST", {"bot_display_name": "bot", "wcdb_key": key})
        status, body = _request("/api/onboarding/step4", "POST", {"fun_enabled": True})
        self.assertTrue(body["ok"], body)
        self.assertNotIn("WCDB_KEY", self.env_values())
        self.assertEqual(self.env_values()["ONBOARDING_DONE"], "true")
        self.assertEqual(self.env_values(self.local)["WCDB_KEY"], key)


class OnboardingWritesFunEnabled(_TempEnv):
    shared_text = ""
    local_text = ""

    def test_step4_writes_fun_enabled(self):
        from src.web import server

        saved = dict(server._onboarding_data)
        self.addCleanup(lambda: (server._onboarding_data.clear(), server._onboarding_data.update(saved)))
        status, body = _request("/api/onboarding/step4", "POST", {"fun_enabled": False})
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.env_values()["FUN_ENABLED"], "false")


class LoadConfigPersonaAndNames(_TempEnv):
    shared_text = (
        "CHAT_PERSONA_PROFILE=yang_yujing\n"
        "CHAT_PERSONA_NAME=小柔\n"
        "CHAT_PERSONA_PROMPT=我的自定义人设\\n第二行\n"
        "CHAT_MAX_CHARS=77\n"
        "WECHAT_GROUPS=%E6%B5%8B%E8%AF%95%2C%E7%BE%A4,%E8%AF%BB%E4%B9%A6%E7%BE%A4\n"
        "PRIVATE_CHATS=*\n"
    )

    def test_custom_values_stay_while_yang_is_selected(self):
        _, body = _request("/api/load-config")
        cfg = body["config"]
        self.assertEqual(cfg["chat_persona_profile"], "yang_yujing")
        self.assertEqual(cfg["chat_persona_name"], "小柔")
        self.assertEqual(cfg["chat_persona_prompt"], "我的自定义人设\n第二行")
        self.assertEqual(cfg["chat_max_chars"], 77)
        self.assertEqual(cfg["yang_preset"]["name"], "假企鹅")
        self.assertTrue(cfg["yang_preset"]["prompt"])

    def test_switching_back_to_custom_keeps_the_custom_persona(self):
        _, body = _request("/api/load-config")
        cfg = dict(body["config"])
        cfg["chat_persona_profile"] = "custom"
        cfg.pop("yang_preset")
        status, res = _request("/api/config", "POST", cfg)
        self.assertTrue(res["ok"], res)
        values = self.env_values()
        self.assertEqual(values["CHAT_PERSONA_PROFILE"], "custom")
        self.assertEqual(values["CHAT_PERSONA_NAME"], "小柔")
        self.assertEqual(values["CHAT_PERSONA_PROMPT"], "我的自定义人设\\n第二行")
        self.assertEqual(values["CHAT_MAX_CHARS"], "77")

    def test_names_come_back_as_lists(self):
        _, body = _request("/api/load-config")
        cfg = body["config"]
        self.assertEqual(cfg["wechat_groups_list"], ["测试,群", "读书群"])
        self.assertEqual(cfg["private_chats_list"], [])

    def test_export_has_no_preset(self):
        from src.web.server import _persona_config_from_raw

        self.assertNotIn("yang_preset", _persona_config_from_raw({"CHAT_PERSONA_PROFILE": "yang_yujing"}))


class OnboardingLocalModel(_TempEnv):
    shared_text = ""
    local_text = ""

    def setUp(self):
        super().setUp()
        from src.web import server

        saved = dict(server._onboarding_data)
        self.addCleanup(lambda: (server._onboarding_data.clear(), server._onboarding_data.update(saved)))

    def test_rejects_a_local_route_without_model_or_with_a_bad_url(self):
        for payload in (
            {"model_route": "local", "local_base_url": "http://127.0.0.1:11434/v1", "local_model": ""},
            {"model_route": "local", "local_base_url": "127.0.0.1:11434", "local_model": "qwen"},
            {"model_route": "local", "local_base_url": "http://x", "local_model": "a\u2028b"},
            {"model_route": "split"},
        ):
            with self.subTest(payload=payload):
                _, body = _request("/api/onboarding/step3", "POST", payload)
                self.assertFalse(body["ok"])

    def test_local_route_is_written_by_step4(self):
        _request("/api/onboarding/step3", "POST", {
            "model_route": "local", "local_base_url": "http://192.168.1.5:1234/v1", "local_model": "qwen3.5:4b",
        })
        status, body = _request("/api/onboarding/step4", "POST", {})
        self.assertTrue(body["ok"], body)
        values = self.env_values()
        self.assertEqual(values["MODEL_ROUTE"], "local")
        self.assertEqual(values["LOCAL_BASE_URL"], "http://192.168.1.5:1234/v1")
        self.assertEqual(values["LOCAL_MODEL"], "qwen3.5:4b")
        self.assertEqual(values["LOCAL_API_KEY"], "")

    def test_cloud_route_leaves_local_settings_alone(self):
        self.env.write_text("MODEL_ROUTE=split\nLOCAL_MODEL=keep\n", encoding="utf-8")
        _request("/api/onboarding/step3", "POST", {"ai_backend": "deepseek", "deepseek_api_key": "sk-0123456789"})
        _request("/api/onboarding/step4", "POST", {})
        values = self.env_values()
        self.assertEqual(values["MODEL_ROUTE"], "split")
        self.assertEqual(values["LOCAL_MODEL"], "keep")


class SandboxUsesTheChatModel(unittest.TestCase):
    def test_reply_comes_from_create_models_chat(self):
        from src.config import BotConfig

        chat_model = MagicMock()
        chat_model.chat.return_value = "本地回复"
        models = MagicMock(chat=chat_model)
        with patch("dotenv.load_dotenv"), \
                patch("src.config.find_env_file", return_value=None), \
                patch("src.config.load_config", return_value=BotConfig(model_route="local", local_model="qwen")), \
                patch("src.summarize.create_models", return_value=models) as create:
            status, body = _request("/api/sandbox/test", "POST", {"message": "你好"})
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["reply"], "本地回复")
        create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
