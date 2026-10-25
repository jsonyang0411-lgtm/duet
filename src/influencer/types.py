"""Data models and schemas for Influencer Business Module."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .settings import APPROVAL_TTL_SECONDS

# 收件信息的字段名和对达人说的叫法，顺序就是追问的顺序
RECIPIENT_FIELD_LABELS = {
    "recipient_name": "收件人姓名",
    "recipient_phone": "联系电话",
    "shipping_address": "收货地址",
}


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
class SampleRequestIntent:
    """达人索样/合作意图 (Layer 3: Judge)"""
    is_sample_request: bool = False
    requested_product: str = ""
    target_sku: str = ""
    recipient_name: str = ""
    recipient_phone: str = ""
    shipping_address: str = ""
    xhs_account: str = ""  # 小红书主页链接或小红书号
    proposed_commission_rate: Optional[float] = None
    raw_evidence: str = ""  # 事实溯源原话 (No memory without a source)


@dataclass
class SampleRequestDraft:
    """一位达人正在补齐的索样申请：多轮对话里逐条补充，信息齐了才生成审批单。"""
    chat_id: str
    product: str = ""
    recipient_name: str = ""
    recipient_phone: str = ""
    shipping_address: str = ""
    xhs_account: str = ""
    inventory: Optional[Dict[str, Any]] = None  # 当前商品的库存查询结果，换商品后清空
    asked_for: Tuple[str, ...] = ()  # 上一轮追问了哪些字段
    evidence: List[str] = field(default_factory=list)  # 达人的原话，供运营核对（R5）
    updated_at: float = field(default_factory=time.time)

    def merge(self, info: SampleRequestIntent) -> None:
        """补进一条消息里提取到的字段。新的非空值覆盖旧值，因为达人可能会更正。"""
        if info.requested_product and info.requested_product != self.product:
            self.product = info.requested_product
            self.inventory = None
        for name in RECIPIENT_FIELD_LABELS:
            value = getattr(info, name)
            if value:
                setattr(self, name, value)
        if info.xhs_account:
            self.xhs_account = info.xhs_account
        if info.raw_evidence:
            self.evidence = (self.evidence + [info.raw_evidence])[-10:]
        self.updated_at = time.time()

    def missing_recipient_fields(self) -> List[str]:
        return [name for name in RECIPIENT_FIELD_LABELS if not getattr(self, name)]


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
    qualification: str = ""  # 资质标注：达标 / 不达标：原因 / 待核实（D4，只标注不拒绝）
    recipient_name: str = ""
    recipient_phone: str = ""
    xhs_account: str = ""  # 达人提供或运营填写；绑佣金必须有，不能用微信 ID 代替（B10）
    evidence: str = ""  # 达人的原话，供运营核对（R5）
    status: GateStatus = GateStatus.PENDING
    created_at: float = field(default_factory=time.time)
    expires_at: float = field(default_factory=lambda: time.time() + APPROVAL_TTL_SECONDS)
    failure_reason: str = ""  # 执行失败时写明原因，留给运营人工处理
    # 执行结果
    order_sn: str = ""
    tracking_number: str = ""
    commission_plan: str = ""
    product_link: str = ""  # 小红书 CLI 生成的商品推广链接，写进发给达人的确认消息
    handled_at: Optional[float] = None  # 驳回、执行完成、失败或过期的时间
    personal_info_cleared_at: Optional[float] = None  # 到期清除收件信息的时间（Q3）
