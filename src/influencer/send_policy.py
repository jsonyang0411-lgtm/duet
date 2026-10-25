"""个人微信防封的发送规则（参数还没定，先用保守的默认值，都能用环境变量改）。

两件事：
- 回复达人之前等一会儿，像真人打字：按字数算一个时间，减去已经花掉的处理时间（Claude 回复本身就要
  好几秒），不够的再补上。
- 主动发的消息（批准后的确认消息等，B15）先进发送队列（influencer_outbox 表），由 next_due() 决定
  什么时候能发：两条之间至少隔一段时间、每小时有上限、夜间不发，留到早上。真正发出去的那一步在
  第 6 项（经 Windows 发出）里接上。

环境变量：
- INFLUENCER_REPLY_DELAY：回复前等待的秒数范围，例如 "2-6"（默认）；"0" 表示不等
- INFLUENCER_SEND_MIN_INTERVAL：主动发送时两条之间至少隔几秒，默认 30
- INFLUENCER_SEND_MAX_PER_HOUR：每小时最多主动发几条，默认 20
- INFLUENCER_QUIET_HOURS：夜间不主动发的时段，例如 "23-8"（默认，23 点到第二天 8 点）；"" 或 "0" 表示不设
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass
from typing import Optional, Tuple

# 每个字大概的打字时间，加在等待时间里；等待最多这么久
SECONDS_PER_CHAR = 0.15
MAX_REPLY_DELAY_SECONDS = 15.0


def _float_range(raw: str, default: Tuple[float, float]) -> Tuple[float, float]:
    raw = raw.strip()
    if not raw:
        return default
    try:
        parts = [float(p) for p in raw.split("-")]
    except ValueError:
        return default
    if len(parts) == 1:
        parts *= 2
    low, high = sorted(parts[:2])
    return (max(low, 0.0), max(high, 0.0))


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "").strip())
    except ValueError:
        return default
    return value if value >= 0 else default


def _quiet_hours(raw: str) -> Optional[Tuple[int, int]]:
    raw = raw.strip()
    if raw in ("", "0"):
        return None
    try:
        start, end = (int(p) for p in raw.split("-"))
    except ValueError:
        return (23, 8)
    if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
        return (23, 8)
    return (start, end)


@dataclass
class SendPolicy:
    reply_delay_range: Tuple[float, float] = (2.0, 6.0)
    min_interval: int = 30
    max_per_hour: int = 20
    quiet_hours: Optional[Tuple[int, int]] = (23, 8)

    @classmethod
    def from_env(cls) -> "SendPolicy":
        return cls(
            reply_delay_range=_float_range(os.getenv("INFLUENCER_REPLY_DELAY", ""), (2.0, 6.0)),
            min_interval=_int_env("INFLUENCER_SEND_MIN_INTERVAL", 30),
            max_per_hour=_int_env("INFLUENCER_SEND_MAX_PER_HOUR", 20),
            quiet_hours=_quiet_hours(os.getenv("INFLUENCER_QUIET_HOURS", "23-8")),
        )

    # ---- 回复 ----

    def reply_delay(self, text: str, already_spent: float = 0.0, rng: random.Random = random) -> float:
        """回复前还要等几秒：随机的基础时间 + 按字数的打字时间，减去已经花掉的时间。"""
        low, high = self.reply_delay_range
        if high <= 0 or not text:
            return 0.0
        target = min(rng.uniform(low, high) + len(text) * SECONDS_PER_CHAR, MAX_REPLY_DELAY_SECONDS)
        return max(0.0, target - already_spent)

    # ---- 主动发送 ----

    def in_quiet_hours(self, now: float) -> bool:
        if self.quiet_hours is None:
            return False
        start, end = self.quiet_hours
        hour = time.localtime(now).tm_hour
        return start <= hour < end if start < end else (hour >= start or hour < end)

    def quiet_hours_end(self, now: float) -> float:
        """夜间时段结束的时间（本地时间整点）。"""
        _, end = self.quiet_hours
        local = time.localtime(now)
        candidate = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, end, 0, 0, 0, 0, -1))
        if candidate <= now:
            candidate = time.mktime((local.tm_year, local.tm_mon, local.tm_mday + 1, end, 0, 0, 0, 0, -1))
        return candidate

    def wait_before_next_send(self, now: float, last_sent_at: Optional[float], sent_last_hour: int,
                              oldest_in_last_hour: Optional[float]) -> Tuple[float, str]:
        """现在能不能主动发下一条。能发返回 (0, "")，不能发返回 (还要等几秒, 原因)。"""
        if self.in_quiet_hours(now):
            return self.quiet_hours_end(now) - now, "夜间不主动发消息"
        if last_sent_at is not None and now - last_sent_at < self.min_interval:
            return self.min_interval - (now - last_sent_at), f"两条之间至少隔 {self.min_interval} 秒"
        if self.max_per_hour and sent_last_hour >= self.max_per_hour:
            wait = 3600 - (now - oldest_in_last_hour) if oldest_in_last_hour is not None else 60
            return max(wait, 1.0), f"每小时最多主动发 {self.max_per_hour} 条"
        return 0.0, ""
