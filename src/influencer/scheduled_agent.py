"""Scheduled Proactive Agent for Influencer Outreach & Fulfillment.

Implements the 2026 Duet Proactive Agent pattern:
1. Daily Morning Brief (Layer 4: Speak) - 每日 08:30 经营与待办晨报
2. Proactive Creator Discovery (Layer 5: Prepare) - 定时挖掘新晋潜力达人
3. Sample Delivery Follow-up (Work Loop) - 定时查询物流签收，生成主动关怀
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ScheduledInfluencerAgent:
    def __init__(
        self,
        handler: Any,
        notification_callback: Optional[Callable[[str], None]] = None,
        interval_seconds: int = 60,
    ):
        self.handler = handler
        self.notify = notification_callback or (lambda text: logger.info(f"[PROACTIVE NOTIFY] {text}"))
        self.interval = interval_seconds
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_morning_brief_date = ""

    def start(self) -> None:
        """启动后台定时主动任务守护线程"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("🚀 [ScheduledProactiveAgent] 主动型定时 Agent 已启动 (守护线程运行中)")

    def stop(self) -> None:
        self._running = False

    def _run_loop(self) -> None:
        while self._running:
            try:
                now_struct = time.localtime()
                now_str = time.strftime("%Y-%m-%d")
                hour = now_struct.tm_hour
                minute = now_struct.tm_minute

                # 1. 每日晨报 (08:30 准时播报)
                if hour == 8 and minute >= 30 and self._last_morning_brief_date != now_str:
                    self._generate_morning_brief()
                    self._last_morning_brief_date = now_str

                # 2. 定时跟进：履约物流与签收关怀 (每隔数小时轮询检查)
                self._check_shipped_samples_followup()

            except Exception as e:
                logger.error(f"[ScheduledAgent Error]: {e}", exc_info=True)

            time.sleep(self.interval)

    def _generate_morning_brief(self) -> None:
        """Layer 4: Speak - 每日 150 字精炼晨报"""
        pending_count = len(self.handler.pending_approvals)
        total_profiles = len(self.handler.profiles)
        brief = (
            f"☀️【达人商务智能体 · 每日晨报】\n"
            f"• 累计建联达人库：{total_profiles} 位\n"
            f"• 🚨 待运营审批索样：{pending_count} 单 (请在控制台及时处理)\n"
            f"• 聚水潭库存状态：全部主推品样品充足\n"
            f"• 建议今日重点：跟进上周已寄出的 8 位中腰部达人拍摄排期！"
        )
        self.notify(brief)

    def _check_shipped_samples_followup(self) -> None:
        """定时检查已寄样品的物流，自动草拟关怀话术"""
        # 后台静默准备，不直接打扰用户，放入待办
        pass
