"""Xiaohongshu CLI (小红书命令行工具) Adapter.

封装同事开发的小红书 CLI：查询达人数据（读），设置专属佣金（写）。
命令格式按 handoff.md 第 7 节的接口约定草案，最终以同事交付的 CLI 为准。

模拟模式只能手动打开（D6）：构造时传 mock_mode=True，或设置环境变量 INFLUENCER_MOCK=1。
没打开模拟模式时，找不到 CLI、执行超时、退出码非 0 或输出不是 JSON，都会抛出
XHSCLIError，绝不返回假数据。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import Any, Dict, Optional

from .settings import mock_enabled

logger = logging.getLogger(__name__)

CLI_TIMEOUT_SECONDS = 30


class XHSCLIError(RuntimeError):
    """小红书 CLI 不可用，或者执行失败。"""


class XiaohongshuCLIClient:
    def __init__(self, cli_path: str = "", mock_mode: Optional[bool] = None):
        self.cli_path = cli_path or os.getenv("XHS_CLI_PATH", "xhs")
        self.is_available = shutil.which(self.cli_path) is not None
        # None 表示按环境变量 INFLUENCER_MOCK 决定；找不到 CLI 不会自动进入模拟模式
        self.mock_mode = mock_enabled() if mock_mode is None else mock_mode
        if self.mock_mode:
            logger.warning("⚠️ [XHS] 小红书 CLI 处于模拟模式：达人数据是假的，不会真实绑定佣金")

    async def get_creator_stats(self, xhs_uid: str) -> Dict[str, Any]:
        """获取小红书达人主页指标（粉丝数、点赞量、带货标签）"""
        if self.mock_mode:
            logger.info(f"[XHS Mock] 检索达人指标: {xhs_uid}")
            return {
                "uid": xhs_uid,
                "nickname": f"美妆博主_{xhs_uid[-4:] if len(xhs_uid)>=4 else '01'}",
                "follower_count": 35000,
                "recent_highest_likes": 3200,
                "category": "美妆护肤",
                "is_qualified": True,
                "mock": True,
            }

        return await self._run("creator", "info", "--uid", xhs_uid, "--json")

    async def set_exclusive_commission(
        self, xhs_uid: str, sku_id: str, rate: float, idempotency_key: str = ""
    ) -> Dict[str, Any]:
        """为达人绑定商品专属定向佣金。

        idempotency_key 传审批单号：同一张审批单重复调用，CLI 只应生效一次。
        """
        if self.mock_mode:
            logger.info(f"[XHS Mock] 绑定达人 {xhs_uid} 专属佣金: {rate * 100:.1f}%")
            return {
                "success": True,
                "plan_id": f"PLAN-{abs(hash(xhs_uid)) % 100000}",
                "rate": rate,
                "message": f"小红书定向推广佣金已生效 ({rate * 100:.1f}%)（模拟）",
                "mock": True,
            }

        args = ["commission", "set", "--uid", xhs_uid, "--sku", sku_id, "--rate", str(rate)]
        if idempotency_key:
            args += ["--idempotency-key", idempotency_key]
        return await self._run(*args, "--json")

    async def _run(self, *args: str) -> Dict[str, Any]:
        """执行一次 CLI，结果必须是 JSON。任何失败都抛出 XHSCLIError。"""
        if not self.is_available:
            raise XHSCLIError(
                f"找不到小红书 CLI：{self.cli_path}（也没有打开模拟模式 INFLUENCER_MOCK=1）"
            )
        proc = await asyncio.create_subprocess_exec(
            self.cli_path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLI_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise XHSCLIError(f"小红书 CLI 超过 {CLI_TIMEOUT_SECONDS} 秒没有返回：{' '.join(args[:2])}")
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()[:200]
            raise XHSCLIError(f"小红书 CLI 执行失败（退出码 {proc.returncode}）：{detail}")
        try:
            return json.loads(stdout.decode())
        except ValueError as exc:
            raise XHSCLIError(f"小红书 CLI 的输出不是 JSON：{exc}") from exc
