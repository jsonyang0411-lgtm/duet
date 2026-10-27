"""Data models and schemas for Influencer Business Module."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from .settings import APPROVAL_TTL_SECONDS

class GateStatus(str, Enum):
    PENDING = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"  # 已批准，正在调用聚水潭和小红书
    FULFILLED = "FULFILLED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


# 审批单状态的中文叫法，用在给运营的提示里
STATUS_LABELS = {
    GateStatus.PENDING: "待审批",
    GateStatus.APPROVED: "已批准",
    GateStatus.REJECTED: "已驳回",
    GateStatus.EXECUTING: "执行中",
    GateStatus.FULFILLED: "已执行",
    GateStatus.FAILED: "执行失败",
    GateStatus.EXPIRED: "已过期",
}


@dataclass
class InfluencerProfile:
    """达人统一上下文档案 (Layer 1: Context)"""
    wechat_id: str
    nickname: str = ""
    xhs_id: str = ""
    xhs_nickname: str = ""
    # 拿不到小红书数据时为 None（未知），不编造（B8）
    follower_count: Optional[int] = None
    recent_like_count: Optional[int] = None  # 近期爆款点赞数
    category: str = "美妆个护"  # 带货类目
    status: str = "NEW"  # NEW, QUALIFIED, REJECTED, SAMPLE_SENT, COOPERATING
    cooperation_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class SampleRequestDraft:
    """要生成审批单的寄样信息（prm_bridge 从合作单上取），先查商品再提交审批。"""
    chat_id: str
    product: str = ""
    recipient_name: str = ""
    recipient_phone: str = ""
    shipping_address: str = ""
    xhs_account: str = ""
    inventory: Optional[Dict[str, Any]] = None  # check_product 查到的商品
    evidence: List[str] = field(default_factory=list)  # 信息从哪来，供运营核对（R5）


@dataclass
class ApprovalActionPacket:
    """原子行动审批数据包 (Layer 6: Gate)"""
    packet_id: str
    influencer_id: str
    influencer_name: str
    follower_count: Optional[int]  # None 表示拿不到数据
    requested_product: str
    sku_id: str
    stock_available: int
    sample_cost: Optional[float]  # None 表示聚水潭没给成本
    proposed_commission_rate: float
    shipping_address: str
    what_cannot_be_undone: str
    qualification: str = ""  # 资质标注：粉丝数（只作参考）/ 待核实（D4、D30，只标注不拦单）
    recipient_name: str = ""
    recipient_phone: str = ""
    xhs_account: str = ""  # 小红书账号，选填（抖音达人没有）；不能用微信 ID 代替（B10）
    evidence: str = ""  # 达人的原话，供运营核对（R5）
    status: GateStatus = GateStatus.PENDING
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + APPROVAL_TTL_SECONDS)
    failure_reason: str = ""  # 执行失败时写明原因，留给运营人工处理
    # 执行结果
    order_sn: str = ""
    tracking_number: str = ""
    commission_plan: str = ""
    handled_at: Optional[float] = None  # 驳回、执行完成、失败或过期的时间
    personal_info_cleared_at: Optional[float] = None  # 到期清除收件信息的时间（Q3）
    # 2026-10-26 第 2 步：抖音、小红书都做（D31），寄几款按销售额分档（D30），合作形式看主播类型（D36）
    platform: str = ""  # DY / XHS（聚水潭的平台代码）
    platform_no: str = ""  # 抖音号 / 小红书号
    tier: str = ""  # 档位：3 款 / 问专场 / 不限 / 无销售数据（tiers.py）
    channel: str = ""  # 主播类型：直播 / 视频（近 30 日哪边卖得多）
    sales_label: str = ""  # 分档用的销售额和来源，如"近 30 日销售总额 250万~500万（聚水潭，10-26 查）"
    items_json: str = ""  # 多款商品：[{"name", "colour", "spec", "qty"}] 的 JSON；空表示只有 requested_product 一款
    live_session: str = ""  # 答应开直播专场的说明（日期等）；没答应为空
