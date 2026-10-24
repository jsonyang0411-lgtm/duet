"""JuShuiTan ERP (聚水潭) Client Adapter.

查询库存、创建样品/赠品出库单。

模拟模式只能手动打开（D6）：构造时传 mock_mode=True，或设置环境变量 INFLUENCER_MOCK=1。
没打开模拟模式时一律走真实接口。真实接口还没接入（计划在第 5 阶段），调用会直接
抛出 ERPNotReadyError，绝不返回假的成功结果。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from .settings import mock_enabled

logger = logging.getLogger(__name__)


class ERPNotReadyError(RuntimeError):
    """聚水潭用不了：没配密钥，或者真实接口还没接入。"""


class JuShuiTanERPClient:
    def __init__(
        self,
        app_key: str = "",
        app_secret: str = "",
        access_token: str = "",
        mock_mode: Optional[bool] = None,
    ):
        self.app_key = app_key or os.getenv("JST_APP_KEY", "")
        self.app_secret = app_secret or os.getenv("JST_APP_SECRET", "")
        self.access_token = access_token or os.getenv("JST_ACCESS_TOKEN", "")
        # None 表示按环境变量 INFLUENCER_MOCK 决定；没配密钥不会自动进入模拟模式
        self.mock_mode = mock_enabled() if mock_mode is None else mock_mode
        if self.mock_mode:
            logger.warning("⚠️ [JST] 聚水潭处于模拟模式：库存和单号都是假的，不会真实建单")

    async def check_inventory(self, product_name: str) -> Dict[str, Any]:
        """查询商品与样品库存"""
        if self.mock_mode:
            logger.info(f"[JST Mock] 查询商品库存: {product_name}")
            return {
                "sku_id": f"SKU-{abs(hash(product_name)) % 10000:04d}",
                "product_name": product_name,
                "in_stock": True,
                "available_quantity": 1280,
                "sample_limit": 50,
                "cost_price": 18.5,
                "retail_price": 89.0,
                "mock": True,
            }

        # 真实环境调用聚水潭 API
        # endpoint: https://openapi.jushuitan.com/open/jushuitan/inventory/query
        raise self._not_ready_error()

    async def create_sample_order(
        self,
        influencer_id: str,
        sku_id: str,
        recipient_name: str,
        recipient_phone: str,
        shipping_address: str,
        memo: str = "达人样品专属寄送",
        order_ref: str = "",
    ) -> Dict[str, Any]:
        """在聚水潭创建赠品/手工寄样单并生成出库任务。

        order_ref 是我方单号（审批单号）。接入真实接口时作为外部单号传给聚水潭，
        防止同一张审批单重复建单。
        """
        if self.mock_mode:
            order_sn = f"JST-SMP-{int(time.time())}"
            logger.info(f"[JST Mock] 创建样品订单: {order_sn}（审批单 {order_ref or '-'}）")
            return {
                "success": True,
                "order_sn": order_sn,
                "tracking_number": f"SF{int(time.time()) * 7 % 100000000000:012d}",
                "carrier": "顺丰速运",
                "message": "样品单已录入聚水潭待打单队列（模拟）",
                "mock": True,
            }

        # 真实环境调用聚水潭手工单/订单上传接口
        raise self._not_ready_error()

    def readiness(self) -> Dict[str, Any]:
        """给控制台看的状态：能不能用、是不是模拟模式、为什么不能用。"""
        if self.mock_mode:
            return {"ready": True, "mock": True, "detail": "模拟模式：库存和单号都是假的，不会真实建单"}
        return {"ready": False, "mock": False, "detail": str(self._not_ready_error())}

    def _not_ready_error(self) -> ERPNotReadyError:
        if not self.app_key:
            return ERPNotReadyError(
                "没有配置聚水潭密钥（JST_APP_KEY），也没有打开模拟模式（INFLUENCER_MOCK=1）"
            )
        return ERPNotReadyError("聚水潭真实接口还没接入（计划在第 5 阶段）")
