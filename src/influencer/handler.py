"""Influencer Business Handler.

Wires Intent Recognition, Multi-turn Context, Qualification Screening,
JST ERP, XHS CLI, and Layer 6 Gate into a unified business pipeline.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from .jst_erp import JuShuiTanERPClient
from .types import ApprovalActionPacket, GateStatus, InfluencerProfile, SampleRequestIntent
from .xhs_cli import XiaohongshuCLIClient

logger = logging.getLogger(__name__)


class InfluencerBusinessHandler:
    def __init__(
        self,
        erp_client: Optional[JuShuiTanERPClient] = None,
        xhs_client: Optional[XiaohongshuCLIClient] = None,
    ):
        self.erp = erp_client or JuShuiTanERPClient()
        self.xhs = xhs_client or XiaohongshuCLIClient()
        # 待审批动作队列 (Layer 6: Gate)
        self.pending_approvals: Dict[str, ApprovalActionPacket] = {}
        # 达人档案缓存 (Layer 1: Context)
        self.profiles: Dict[str, InfluencerProfile] = {}
        # 达人会话上下文：等待补充地址中的达人 {sender_id: requested_product}
        self.awaiting_address: Dict[str, str] = {}

    def extract_intent(self, text: str, sender_id: str = "") -> SampleRequestIntent:
        """从微信消息中研判是否属于达人索样/合作意图 (Layer 3: Judge)"""
        sample_keywords = ["样品", "样单", "寄样", "带货", "合作", "试用", "选品", "测品", "推品", "佣金"]
        is_sample = any(kw in text for kw in sample_keywords)
        is_in_address_turn = sender_id in self.awaiting_address

        # 手机号提取
        phone_match = re.search(r"1[3-9]\d{9}", text)
        phone = phone_match.group(0) if phone_match else ""

        # 地址提取
        address = ""
        address_match = re.search(
            r"(?:收件地址|地址|寄到|寄送至)?[:：\s]*((?:[\u4e00-\u9fa5]{2,}(?:省|市|区|县|街道|路|镇|村))[^，。\n\r]+)",
            text,
        )
        if address_match:
            address = address_match.group(1).strip()
        elif "市" in text or "区" in text or "路" in text:
            # 宽容模式匹配地址
            parts = re.split(r"[，,\s\n]+", text)
            for part in parts:
                if any(x in part for x in ["省", "市", "区", "县", "路", "街", "号"]):
                    address = part.strip()
                    break

        # 如果既不是索样关键词，达人也不在等待收件信息状态，且没有收件手机地址特征，则判定为普通闲聊
        if not is_sample and not is_in_address_turn and not (phone and address):
            return SampleRequestIntent(is_sample_request=False)

        # 商品抽取
        product = self.awaiting_address.get(sender_id, "主推爆款商品")
        for prod_candidate in ["VC精华液", "冻干粉", "泥膜", "眼霜", "洁面乳", "精华", "面膜"]:
            if prod_candidate in text:
                product = prod_candidate
                break

        return SampleRequestIntent(
            is_sample_request=True,
            requested_product=product,
            recipient_phone=phone,
            shipping_address=address,
            raw_evidence=text,
        )

    def check_qualification(self, profile: InfluencerProfile) -> Tuple[bool, str]:
        """核查达人带货资质（小红书粉丝 >= 10000 且 爆款点赞 >= 1000）"""
        if profile.follower_count < 10000:
            return False, f"粉丝量（{profile.follower_count}）未达基础门槛（10,000）"
        if profile.recent_like_count < 1000:
            return False, f"近期点赞（{profile.recent_like_count}）未达爆款点赞门槛（1,000）"
        return True, "资质合格"

    async def process_incoming_message(
        self, sender_id: str, content: str, nickname: str = ""
    ) -> Optional[str]:
        """处理进入的达人私信消息。"""
        intent = self.extract_intent(content, sender_id=sender_id)
        if not intent.is_sample_request:
            return None

        # 1. 获取或创建达人档案
        if sender_id not in self.profiles:
            stats = await self.xhs.get_creator_stats(sender_id)
            self.profiles[sender_id] = InfluencerProfile(
                wechat_id=sender_id,
                nickname=nickname,
                follower_count=stats.get("follower_count", 20000),
                recent_like_count=stats.get("recent_highest_likes", 1500),
            )
        profile = self.profiles[sender_id]

        # 2. 资质初筛
        qualified, reason = self.check_qualification(profile)
        if not qualified:
            logger.info(f"[达人初筛未过] {profile.nickname} ({sender_id}): {reason}")
            return (
                "亲亲，非常感谢您对我们品牌的认可与喜爱！💖\n"
                "目前我们本期的免费寄样名额已经约满啦，您可以先关注我们小店的最新活动，"
                "后续一旦开启新一轮体验官招募，我们第一时间私信您哦~"
            )

        # 3. 聚水潭 ERP 查库存
        inventory = await self.erp.check_inventory(intent.requested_product)
        if not inventory.get("in_stock") or inventory.get("available_quantity", 0) <= 0:
            logger.warning(f"[ERP库存不足] {intent.requested_product}")
            return (
                f"亲亲，您看中的【{intent.requested_product}】太火爆啦，目前仓库的样品库存暂时告罄，"
                "我们已经在紧急补货调拨中，到货后第一时间通知您寄送！"
            )

        # 4. 地址信息不完整：引导达人补充地址并记录上下文状态
        if not intent.shipping_address or not intent.recipient_phone:
            self.awaiting_address[sender_id] = inventory["product_name"]
            return (
                f"收到老师的需求啦！【{inventory['product_name']}】目前库存充足，我们非常期待与老师合作~ ✨\n"
                "麻烦老师发一下您的【收件人姓名、联系电话、收货地址】，我们运营小姐姐马上为您安排寄出哦！"
            )

        # 5. 达人已提供完整收件地址，解除等待状态，生成 Layer 6 Gate 审批卡片
        self.awaiting_address.pop(sender_id, None)
        packet_id = f"GATE-{uuid.uuid4().hex[:8].upper()}"
        action_packet = ApprovalActionPacket(
            packet_id=packet_id,
            influencer_id=sender_id,
            influencer_name=nickname or sender_id,
            follower_count=profile.follower_count,
            requested_product=inventory["product_name"],
            sku_id=inventory["sku_id"],
            stock_available=inventory["available_quantity"],
            sample_cost=inventory.get("cost_price", 18.5),
            proposed_commission_rate=0.25,
            shipping_address=f"{intent.shipping_address} (电话:{intent.recipient_phone})",
            what_cannot_be_undone="聚水潭ERP将自动扣减库存并出具打单运单，小红书将绑定 25% 专属定向计划",
        )
        self.pending_approvals[packet_id] = action_packet

        logger.info(f"🚨 [Gate拦截-待运营审批] 任务单 {packet_id}: {action_packet}")
        return (
            f"收到老师的寄样收件信息！【{inventory['product_name']}】我们已经录入发货排单队列~ 📦\n"
            f"收件地址：{intent.shipping_address}\n"
            "仓库今天下午安排顺丰寄出，单号生成后第一时间发给老师，合作愉快！"
        )

    async def execute_operator_approval(self, packet_id: str, approved: bool) -> Dict[str, Any]:
        """运营审批放行"""
        if packet_id not in self.pending_approvals:
            return {"success": False, "error": "未找到对应的审批单号"}

        packet = self.pending_approvals[packet_id]
        if not approved:
            packet.status = GateStatus.REJECTED
            return {"success": True, "message": f"任务单 {packet_id} 已驳回"}

        order_res = await self.erp.create_sample_order(
            influencer_id=packet.influencer_id,
            sku_id=packet.sku_id,
            recipient_name=packet.influencer_name,
            recipient_phone="13800000000",
            shipping_address=packet.shipping_address,
        )

        comm_res = await self.xhs.set_exclusive_commission(
            xhs_uid=packet.influencer_id,
            sku_id=packet.sku_id,
            rate=packet.proposed_commission_rate,
        )

        packet.status = GateStatus.FULFILLED
        return {
            "success": True,
            "packet_id": packet_id,
            "order_sn": order_res.get("order_sn"),
            "tracking_number": order_res.get("tracking_number"),
            "commission_plan": comm_res.get("plan_id"),
            "message": "已成功下发聚水潭打单，并设置小红书定向佣金！",
        }
