"""JuShuiTan ERP (聚水潭) Client Adapter.

查商品、建寄样单，查达人在我们店的合作数据。

聚水潭没有开放平台密钥（D32），真实调用走我们自己的命令行 ~/jushuitan-engineering
（``node bin/jst.mjs``，直接发 HTTP，令牌从调试版 Chrome 现取）。查合作数据（coop_summary）已经接上；
查商品、建寄样单还没接：建寄样单的请求（寄样管理 saveV3）要等运营真建一张时抓下来。

模拟模式只能手动打开（D6）：构造时传 mock_mode=True，或设置环境变量 INFLUENCER_MOCK=1。
没打开模拟模式时，还没接上的调用直接抛出 ERPNotReadyError，绝不返回假的成功结果。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .settings import mock_enabled

logger = logging.getLogger(__name__)

# 涨佣门槛（D37，2026-10-26）：近 30 天在我们店满 40 单，或者成交额（退款前）满 1 万元
RAISE_MIN_ORDERS = 40
RAISE_MIN_AMOUNT = 10000

CLI_TIMEOUT_SECONDS = 60


def jst_cli_dir() -> Path:
    """聚水潭命令行的目录：环境变量 JST_CLI_PATH，默认 ~/jushuitan-engineering。"""
    return Path(os.getenv("JST_CLI_PATH") or "~/jushuitan-engineering").expanduser()


def run_jst_cli(*args: str) -> Dict[str, Any]:
    """跑一条只读的聚水潭命令，返回它输出的 JSON。命令失败（登录失效、限流、找不到命令行）抛 ERPNotReadyError。"""
    entry = jst_cli_dir() / "bin" / "jst.mjs"
    if not entry.exists():
        raise ERPNotReadyError(f"找不到聚水潭命令行 {entry}（设 JST_CLI_PATH）")
    node = os.getenv("JST_NODE_PATH") or "node"
    try:
        done = subprocess.run(
            [node, str(entry), *args], capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ERPNotReadyError(f"聚水潭命令行没跑起来：{exc}") from exc
    if done.returncode != 0:
        why = (done.stderr or "").strip().splitlines()[-1:] or [f"退出码 {done.returncode}"]
        raise ERPNotReadyError(f"聚水潭命令行出错：{why[0][:200]}")
    try:
        return json.loads(done.stdout)
    except ValueError as exc:
        raise ERPNotReadyError("聚水潭命令行的输出不是 JSON") from exc


def coop_summary(platform: str, platform_no: str) -> Dict[str, Any]:
    """达人近 30 天在我们店的合作数据，给运营判断能不能涨佣（D37）。系统只查、不操作。

    返回 {"ok": True, "orders", "amount", "net_amount", "meets", "text"}；查不了时
    {"ok": False, "text": 原因}。meets：满 40 单或成交额（退款前）满 1 万。"""
    if not platform_no:
        return {"ok": False, "text": "名单上没有这位达人的抖音号 / 小红书号，查不了合作数据，请运营在聚水潭里看"}
    try:
        data = run_jst_cli("mine", "--platform", platform, "--no", platform_no)
    except ERPNotReadyError as exc:
        return {"ok": False, "text": f"查合作数据失败：{exc}"}
    row = next((r for r in data.get("rows") or [] if str(r.get("platformNo")) == platform_no), None)
    if row is None or not isinstance(row.get("coop30d"), dict):
        return {"ok": False, "text": f"聚水潭「我的达人」里没有 {platform_no}，查不了合作数据"}
    coop = row["coop30d"]
    orders, amount, net = int(coop.get("orders") or 0), float(coop.get("amount") or 0), float(coop.get("netAmount") or 0)
    meets = orders >= RAISE_MIN_ORDERS or amount >= RAISE_MIN_AMOUNT
    verdict = "满足" if meets else "不满足"
    return {
        "ok": True, "orders": orders, "amount": amount, "net_amount": net, "meets": meets,
        "text": (f"近 30 天在我们店：{orders} 单，成交额 {amount:g} 元（退款后 {net:g} 元）；"
                 f"{verdict}涨佣条件（满 {RAISE_MIN_ORDERS} 单或成交额满 {RAISE_MIN_AMOUNT} 元），由运营决定"),
    }


class ERPNotReadyError(RuntimeError):
    """聚水潭用不了：命令行找不到、登录失效、或者这一项还没接上。"""


class JuShuiTanERPClient:
    def __init__(self, mock_mode: Optional[bool] = None):
        # None 表示按环境变量 INFLUENCER_MOCK 决定；接不上不会自动进入模拟模式
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
                "found": True,
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
            # 和真实的寄样单一样：单号 YP 开头；运单号要等仓库在寄样管理发货后才有，建单时没有
            order_sn = f"YP-MOCK-{int(time.time())}"
            logger.info(f"[JST Mock] 创建寄样单: {order_sn}（审批单 {order_ref or '-'}）")
            return {
                "success": True,
                "order_sn": order_sn,
                "tracking_number": "",
                "carrier": "",
                "message": "寄样单已建（模拟）",
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
        return ERPNotReadyError(
            "聚水潭查商品、建寄样单还没接上（命令行 ~/jushuitan-engineering 缺建寄样单，"
            "要等运营在寄样管理真建一张时抓请求）；要先走通流程可以打开模拟模式（INFLUENCER_MOCK=1）"
        )
