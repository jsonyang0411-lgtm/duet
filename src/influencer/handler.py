"""Influencer Business Handler.

Wires Intent Recognition, Multi-turn Context, Qualification Screening,
JST ERP, XHS CLI, and Layer 6 Gate into a unified business pipeline.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from typing import Any, Dict, Optional

from .jst_erp import JuShuiTanERPClient
from .settings import DEFAULT_COMMISSION_RATE, MAX_COMMISSION_RATE, MIN_FOLLOWERS, MIN_RECENT_LIKES
from .types import ApprovalActionPacket, GateStatus, InfluencerProfile, SampleRequestIntent
from .xhs_cli import XiaohongshuCLIClient

logger = logging.getLogger(__name__)

QUALIFICATION_OK = "达标"
QUALIFICATION_FAILED = "不达标"
QUALIFICATION_UNKNOWN = "待核实：没有小红书数据，需要运营人工核实"


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
        # router 在多个工作线程里调用本处理器，审批状态的检查和修改都在这把锁里完成
        self._lock = threading.Lock()

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

    def assess_qualification(self, profile: InfluencerProfile) -> str:
        """给审批单标注资质（D4）：只标注、不自动拒绝；拿不到数据就是"待核实"。"""
        if profile.follower_count is None or profile.recent_like_count is None:
            return QUALIFICATION_UNKNOWN
        problems = []
        if profile.follower_count < MIN_FOLLOWERS:
            problems.append(f"粉丝 {profile.follower_count} 低于 {MIN_FOLLOWERS}")
        if profile.recent_like_count < MIN_RECENT_LIKES:
            problems.append(f"近期最高点赞 {profile.recent_like_count} 低于 {MIN_RECENT_LIKES}")
        if problems:
            return f"{QUALIFICATION_FAILED}：{'；'.join(problems)}"
        return QUALIFICATION_OK

    async def _fetch_creator_stats(self, xhs_uid: str) -> Dict[str, Any]:
        """查达人数据。拿不到就返回空字典，审批单会标成"待核实"，不编造数据（B8）。"""
        try:
            return await self.xhs.get_creator_stats(xhs_uid) or {}
        except Exception as exc:
            logger.warning(f"[XHS] 查询达人数据失败，按待核实处理：{exc}")
            return {}

    async def process_incoming_message(
        self, sender_id: str, content: str, nickname: str = ""
    ) -> Optional[str]:
        """处理进入的达人私信消息。"""
        intent = self.extract_intent(content, sender_id=sender_id)
        if not intent.is_sample_request:
            return None

        # 1. 获取或创建达人档案
        if sender_id not in self.profiles:
            stats = await self._fetch_creator_stats(sender_id)
            self.profiles[sender_id] = InfluencerProfile(
                wechat_id=sender_id,
                nickname=nickname,
                follower_count=stats.get("follower_count"),
                recent_like_count=stats.get("recent_highest_likes"),
            )
        profile = self.profiles[sender_id]

        # 2. 资质只在审批单上标注，由运营决定，不自动拒绝（D4）

        # 3. 聚水潭 ERP 查库存
        try:
            inventory = await self.erp.check_inventory(intent.requested_product)
        except Exception as exc:
            logger.error(f"[ERP] 查询库存失败，需要人工处理：{exc}")
            return "收到老师的需求啦~ 我们这边核对一下，再回复老师哦！"
        if not inventory.get("in_stock") or inventory.get("available_quantity", 0) <= 0:
            logger.warning(f"[ERP库存不足] {intent.requested_product}")
            return (
                f"亲亲，【{intent.requested_product}】的样品目前暂时没有库存，非常抱歉~ "
                "老师也可以看看我们的其他产品哦！"
            )

        # 4. 地址信息不完整：引导达人补充地址并记录上下文状态
        if not intent.shipping_address or not intent.recipient_phone:
            self.awaiting_address[sender_id] = inventory["product_name"]
            return (
                f"收到老师的需求啦！【{inventory['product_name']}】目前有货，我们很期待和老师合作~ ✨\n"
                "麻烦老师发一下【收件人姓名、联系电话、收货地址】，我们登记后会提交审核，审核结果第一时间告诉老师~"
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
            proposed_commission_rate=DEFAULT_COMMISSION_RATE,
            shipping_address=f"{intent.shipping_address} (电话:{intent.recipient_phone})",
            what_cannot_be_undone=(
                f"聚水潭ERP将自动扣减库存并出具打单运单，小红书将绑定 {DEFAULT_COMMISSION_RATE:.0%} 专属定向计划"
            ),
            qualification=self.assess_qualification(profile),
        )
        with self._lock:
            self.pending_approvals[packet_id] = action_packet

        logger.info(f"🚨 [Gate拦截-待运营审批] 任务单 {packet_id}: {action_packet}")
        return (
            f"收到老师的收件信息啦！【{inventory['product_name']}】的寄样申请已经提交审核~ 📝\n"
            f"收件地址：{intent.shipping_address}\n"
            "审核通过后我们会第一时间通知老师，请稍等哦~"
        )

    async def execute_operator_approval(self, packet_id: str, approved: bool) -> Dict[str, Any]:
        """运营审批。每张审批单只能处理一次：驳回、过期、执行中或已执行的单都不能再执行。"""
        with self._lock:
            packet = self.pending_approvals.get(packet_id)
            if packet is None:
                return {"success": False, "error": "未找到对应的审批单号"}
            if packet.status != GateStatus.PENDING:
                return {
                    "success": False,
                    "error": f"审批单 {packet_id} 当前状态为 {packet.status.value}，不能再处理",
                }
            if not approved:
                packet.status = GateStatus.REJECTED
                return {"success": True, "message": f"任务单 {packet_id} 已驳回"}
            if time.time() > packet.expires_at:
                packet.status = GateStatus.EXPIRED
                return {"success": False, "error": f"审批单 {packet_id} 已过期，需要和达人重新确认"}
            if packet.proposed_commission_rate > MAX_COMMISSION_RATE:
                # 不改状态：运营把佣金改到上限以内后还能再批
                return {
                    "success": False,
                    "error": (
                        f"佣金 {packet.proposed_commission_rate:.0%} 超过上限 "
                        f"{MAX_COMMISSION_RATE:.0%}，不能执行"
                    ),
                }
            if packet.qualification.startswith(QUALIFICATION_FAILED):
                # R3：数据明确不达标的达人不寄样，批准了也不执行
                return {
                    "success": False,
                    "error": f"达人资质{packet.qualification}，按规则不能寄样，请驳回",
                }
            # 先占住这张单，调用外部系统期间，重复的"通过"会因为状态不是 PENDING 被挡下
            packet.status = GateStatus.EXECUTING

        try:
            order_res = await self.erp.create_sample_order(
                influencer_id=packet.influencer_id,
                sku_id=packet.sku_id,
                recipient_name=packet.influencer_name,
                recipient_phone="13800000000",
                shipping_address=packet.shipping_address,
                order_ref=packet.packet_id,
            )
        except Exception as exc:
            return self._fail(packet, f"聚水潭建单出错：{exc}")
        if not order_res.get("success"):
            return self._fail(packet, f"聚水潭建单失败：{order_res.get('error') or '未知原因'}")

        try:
            comm_res = await self.xhs.set_exclusive_commission(
                xhs_uid=packet.influencer_id,
                sku_id=packet.sku_id,
                rate=packet.proposed_commission_rate,
                idempotency_key=packet.packet_id,
            )
        except Exception as exc:
            comm_res = {"success": False, "error": str(exc)}
        if not comm_res.get("success"):
            return self._fail(
                packet,
                f"出库单 {order_res.get('order_sn')} 已建，但小红书佣金绑定失败："
                f"{comm_res.get('error') or '未知原因'}，需要人工处理",
                order_sn=order_res.get("order_sn"),
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

    def _fail(self, packet: ApprovalActionPacket, reason: str, **extra: Any) -> Dict[str, Any]:
        """把审批单标为失败并写明原因，后续步骤一律不再执行。"""
        packet.status = GateStatus.FAILED
        packet.failure_reason = reason
        logger.error(f"[审批执行失败] {packet.packet_id}: {reason}")
        return {"success": False, "packet_id": packet.packet_id, "error": reason, **extra}
