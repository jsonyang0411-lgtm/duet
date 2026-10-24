"""Xiaohongshu CLI (小红书命令行工具) Adapter.

Wraps the colleague's CLI tool to query influencer metrics and set commissions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class XiaohongshuCLIClient:
    def __init__(self, cli_path: str = ""):
        self.cli_path = cli_path or os.getenv("XHS_CLI_PATH", "xhs")
        self.is_available = shutil.which(self.cli_path) is not None

    async def get_creator_stats(self, xhs_uid: str) -> Dict[str, Any]:
        """获取小红书达人主页指标（粉丝数、点赞量、带货标签）"""
        if not self.is_available:
            logger.info(f"[XHS Mock] 检索达人指标: {xhs_uid}")
            # 默认返回达标的模拟达人数据 (粉丝 > 10000, 爆款点赞 > 1000)
            return {
                "uid": xhs_uid,
                "nickname": f"美妆博主_{xhs_uid[-4:] if len(xhs_uid)>=4 else '01'}",
                "follower_count": 35000,
                "recent_highest_likes": 3200,
                "category": "美妆护肤",
                "is_qualified": True,
            }

        # 真实环境调用子进程 CLI
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli_path, "creator", "info", "--uid", xhs_uid, "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return json.loads(stdout.decode())
        except Exception as e:
            logger.error(f"XHS CLI error: {e}")
            return {"follower_count": 0, "recent_highest_likes": 0, "is_qualified": False}

    async def set_exclusive_commission(
        self, xhs_uid: str, sku_id: str, rate: float
    ) -> Dict[str, Any]:
        """为达人绑定商品专属定向佣金"""
        if not self.is_available:
            logger.info(f"[XHS Mock] 绑定达人 {xhs_uid} 专属佣金: {rate * 100:.1f}%")
            return {
                "success": True,
                "plan_id": f"PLAN-{abs(hash(xhs_uid)) % 100000}",
                "rate": rate,
                "message": f"小红书定向推广佣金已生效 ({rate * 100:.1f}%)",
            }

        try:
            proc = await asyncio.create_subprocess_exec(
                self.cli_path, "commission", "set",
                "--uid", xhs_uid,
                "--sku", sku_id,
                "--rate", str(rate),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return json.loads(stdout.decode())
        except Exception as e:
            logger.error(f"XHS set commission error: {e}")
            return {"success": False, "error": str(e)}
