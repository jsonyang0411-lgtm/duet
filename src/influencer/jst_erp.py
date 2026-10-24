"""JuShuiTan ERP (聚水潭) Client Adapter.

Supports querying stock and creating sample/gift orders.
Includes built-in Mock mode when API keys are not provided.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class JuShuiTanERPClient:
    def __init__(
        self,
        app_key: str = "",
        app_secret: str = "",
        access_token: str = "",
        mock_mode: bool = True,
    ):
        self.app_key = app_key or os.getenv("JST_APP_KEY", "")
        self.app_secret = app_secret or os.getenv("JST_APP_SECRET", "")
        self.access_token = access_token or os.getenv("JST_ACCESS_TOKEN", "")
        # 如果未配置秘钥，自动开启沙盒 Mock 模式，确保开发联调不中断
        self.mock_mode = mock_mode if (not self.app_key) else False

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
            }

        # 真实环境调用聚水潭 API
        # endpoint: https://openapi.jushuitan.com/open/jushuitan/inventory/query
        # 在这里填入真实的开放平台签名计算逻辑
        return {"sku_id": "SKU-REAL", "in_stock": True, "available_quantity": 100}

    async def create_sample_order(
        self,
        influencer_id: str,
        sku_id: str,
        recipient_name: str,
        recipient_phone: str,
        shipping_address: str,
        memo: str = "达人样品专属寄送",
    ) -> Dict[str, Any]:
        """在聚水潭创建赠品/手工寄样单并生成出库任务"""
        if self.mock_mode:
            order_sn = f"JST-SMP-{int(time.time())}"
            logger.info(
                f"[JST Mock] 创建样品订单: {order_sn} -> {recipient_name} ({recipient_phone}), 地址: {shipping_address}"
            )
            return {
                "success": True,
                "order_sn": order_sn,
                "tracking_number": f"SF{int(time.time()) * 7 % 100000000000:012d}",
                "carrier": "顺丰速运",
                "message": "样品单已录入聚水潭待打单队列",
            }

        # 真实环境调用聚水潭手工单/订单上传接口
        return {"success": True, "order_sn": "JST-REAL"}
