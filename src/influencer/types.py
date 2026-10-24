"""Data models and schemas for Influencer Business Module."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from .settings import APPROVAL_TTL_SECONDS


class GateStatus(str, Enum):
    PENDING = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"  # 已批准，正在调用聚水潭和小红书
    FULFILLED = "FULFILLED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


@dataclass
class InfluencerProfile:
    """达人统一上下文档案 (Layer 1: Context)"""
    wechat_id: str
    nickname: str = ""
    xhs_id: str = ""
    xhs_nickname: str = ""
    follower_count: int = 0
    recent_like_count: int = 0  # 近期爆款点赞数
    category: str = "美妆个护"  # 带货类目
    status: str = "NEW"  # NEW, QUALIFIED, REJECTED, SAMPLE_SENT, COOPERATING
    cooperation_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class SampleRequestIntent:
    """达人索样/合作意图 (Layer 3: Judge)"""
    is_sample_request: bool = False
    requested_product: str = ""
    target_sku: str = ""
    recipient_name: str = ""
    recipient_phone: str = ""
    shipping_address: str = ""
    proposed_commission_rate: Optional[float] = None
    raw_evidence: str = ""  # 事实溯源原话 (No memory without a source)


@dataclass
class ApprovalActionPacket:
    """原子行动审批数据包 (Layer 6: Gate)"""
    packet_id: str
    influencer_id: str
    influencer_name: str
    follower_count: int
    requested_product: str
    sku_id: str
    stock_available: int
    sample_cost: float
    proposed_commission_rate: float
    shipping_address: str
    what_cannot_be_undone: str
    status: GateStatus = GateStatus.PENDING
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + APPROVAL_TTL_SECONDS)
    failure_reason: str = ""  # 执行失败时写明原因，留给运营人工处理
