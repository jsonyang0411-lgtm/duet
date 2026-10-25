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


def morning_brief(overview: Dict[str, int], date: str = "") -> str:
    """晨报 / 今日概览的文字。只用 InfluencerStore.overview() 的真实数字，不写任何推测（B13、R5）。"""
    lines = [
        f"【达人商务 · 今日概览】{date or time.strftime('%Y-%m-%d')}",
        f"• 待审批：{overview['pending']} 单（其中 {overview['expiring_soon']} 单 12 小时内过期）",
        f"• 待处理：{overview['inbox']} 条（达人发来的消息、缺货）",
        (
            f"• 近 24 小时：新申请 {overview['submitted_24h']} 单，已执行 {overview['fulfilled_24h']} 单，"
            f"驳回 {overview['rejected_24h']} 单，失败 {overview['failed_24h']} 单，缺货 {overview['out_of_stock_24h']} 次"
        ),
        f"• 名单里启用的达人：{overview['contacts']} 位",
    ]
    if overview["executing"]:
        lines.append(f"• 注意：有 {overview['executing']} 单停在\"执行中\"，需要人工到聚水潭和小红书核对")
    lines.append("数据来源：控制台「达人审批」，每个数字都能在对应的列表里查到")
    return "\n".join(lines)


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
        """Layer 4: Speak - 每日晨报。按 D14 提醒只做在控制台里，这个定时任务目前没有启动；
        控制台的「今日概览」用的是同一段文字。"""
        self.handler.store.run_maintenance()
        self.notify(morning_brief(self.handler.store.overview()))

    def _check_shipped_samples_followup(self) -> None:
        """定时检查已寄样品的物流，自动草拟关怀话术。
        还没有实现：要等聚水潭真实接口（第 5 阶段）才能查物流。"""
