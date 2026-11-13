"""Configuration, read from environment variables only (never from the repo)."""

import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Tuple

TOKEN_RE = re.compile(r"^[\x21-\x7e]{32,512}$")


class ConfigError(ValueError):
    """A configuration value is missing or invalid (message is for the operator)."""


@dataclass
class Config:
    admin_token: str
    db_path: str = "./data/sample_site.db"
    host: str = "127.0.0.1"
    port: int = 8080
    public_url: str = ""
    retention_days: int = 90
    trust_proxy: bool = False
    brand: str = "ACE7CN"
    # Injected clock (tests move time forward); seconds since the epoch.
    clock: Callable[[], float] = field(default=time.time, repr=False)
    # Rate limits as (max events, window seconds), per client IP.
    rate_page: Tuple[int, int] = (300, 300)
    rate_asset: Tuple[int, int] = (3000, 300)
    rate_post: Tuple[int, int] = (30, 600)
    rate_admin_fail: Tuple[int, int] = (10, 60)
    # Body caps in bytes.
    max_form_bytes: int = 32 * 1024
    max_catalog_bytes: int = 2 * 1024 * 1024
    max_image_bytes: int = 400 * 1024
    max_admin_json_bytes: int = 64 * 1024
    # Purge job interval (seconds); the job also runs lazily on requests.
    purge_interval: int = 3600

    def __repr__(self):  # never print the admin token
        return "Config(db_path=%r, host=%r, port=%r, public_url=%r)" % (
            self.db_path, self.host, self.port, self.public_url)

    def validate(self):
        """Raise ConfigError when a value cannot be used."""
        if not isinstance(self.admin_token, str) or not TOKEN_RE.match(self.admin_token):
            raise ConfigError(
                "管理口令（SAMPLE_SITE_ADMIN_TOKEN 或 SAMPLE_SITE_ADMIN_TOKEN_FILE）未设置、"
                "少于 32 个字符或含有空格等字符，拒绝启动。可以用 "
                "`python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"` 生成一个。")
        url = (self.public_url or "").strip()
        if url:
            if not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?(/[A-Za-z0-9._~\-/]*)?$", url):
                raise ConfigError("SAMPLE_SITE_PUBLIC_URL 格式不对，应当像 https://xuan.example.com")
            self.public_url = url.rstrip("/")
        if not (1 <= int(self.retention_days) <= 3650):
            raise ConfigError("SAMPLE_SITE_RETENTION_DAYS 要在 1 到 3650 之间")
        if not (0 <= int(self.port) <= 65535):
            raise ConfigError("SAMPLE_SITE_PORT 不是有效的端口")
        brand = (self.brand or "").strip()
        if not brand or len(brand) > 30 or any(ord(c) < 32 or ord(c) == 127 for c in brand):
            raise ConfigError("SAMPLE_SITE_BRAND 要是 1 到 30 个字符")
        self.brand = brand
        return self

    @property
    def https(self):
        return self.public_url.startswith("https://")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None):
        """Build a Config from environment variables (see DEPLOY.md)."""
        env = os.environ if env is None else env
        token = (env.get("SAMPLE_SITE_ADMIN_TOKEN") or "").strip()
        token_file = (env.get("SAMPLE_SITE_ADMIN_TOKEN_FILE") or "").strip()
        if not token and token_file:
            try:
                with open(token_file, "r", encoding="utf-8") as fh:
                    token = fh.read().strip()
            except OSError as exc:
                raise ConfigError("读不了 SAMPLE_SITE_ADMIN_TOKEN_FILE 指向的文件（%s）" % exc.__class__.__name__)

        def _int(name, default):
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                raise ConfigError("%s 要是整数" % name)

        def _bool(name):
            raw = (env.get(name) or "").strip().lower()
            if raw in ("", "0", "false", "no", "off"):
                return False
            if raw in ("1", "true", "yes", "on"):
                return True
            raise ConfigError("%s 只能是 0 或 1" % name)

        cfg = cls(
            admin_token=token,
            db_path=(env.get("SAMPLE_SITE_DB") or "").strip() or "./data/sample_site.db",
            host=(env.get("SAMPLE_SITE_HOST") or "").strip() or "127.0.0.1",
            port=_int("SAMPLE_SITE_PORT", 8080),
            public_url=(env.get("SAMPLE_SITE_PUBLIC_URL") or "").strip(),
            retention_days=_int("SAMPLE_SITE_RETENTION_DAYS", 90),
            trust_proxy=_bool("SAMPLE_SITE_TRUST_PROXY"),
            brand=(env.get("SAMPLE_SITE_BRAND") or "").strip() or "ACE7CN",
        )
        return cfg.validate()
