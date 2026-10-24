"""Influencer Business Handler.

Wires Intent Recognition, Multi-turn Context, Qualification Screening,
JST ERP, XHS CLI, and Layer 6 Gate into a unified business pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from typing import Any, Dict, Iterable, Optional

from .extract import extract_request_info
from .jst_erp import JuShuiTanERPClient
from .settings import (
    DEFAULT_COMMISSION_RATE,
    DRAFT_TTL_SECONDS,
    MAX_COMMISSION_RATE,
    MIN_FOLLOWERS,
    MIN_RECENT_LIKES,
    influencer_chat_ids_from_env,
)
from .types import (
    RECIPIENT_FIELD_LABELS,
    ApprovalActionPacket,
    GateStatus,
    InfluencerProfile,
    SampleRequestDraft,
)
from .xhs_cli import XiaohongshuCLIClient

logger = logging.getLogger(__name__)

QUALIFICATION_OK = "达标"
QUALIFICATION_FAILED = "不达标"
QUALIFICATION_UNKNOWN = "待核实：没有小红书数据，需要运营人工核实"
# 出了问题时的中性回复：不承诺任何事，也不让这条消息落到聊天模型那里随口作答
HOLDING_REPLY = "收到老师的消息啦~ 我们这边核对一下，再回复老师哦！"


def _mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) == 11 else phone


class InfluencerBusinessHandler:
    def __init__(
        self,
        erp_client: Optional[JuShuiTanERPClient] = None,
        xhs_client: Optional[XiaohongshuCLIClient] = None,
        influencer_ids: Optional[Iterable[str]] = None,
    ):
        self.erp = erp_client or JuShuiTanERPClient()
        self.xhs = xhs_client or XiaohongshuCLIClient()
        # 待审批动作队列 (Layer 6: Gate)
        self.pending_approvals: Dict[str, ApprovalActionPacket] = {}
        # 达人档案缓存 (Layer 1: Context)
        self.profiles: Dict[str, InfluencerProfile] = {}
        # 每位达人正在补齐的索样申请 {chat_id: draft}
        self.drafts: Dict[str, SampleRequestDraft] = {}
        # 白名单（D3）：只处理名单里的私聊 chat_id。没传就读环境变量 INFLUENCER_CHAT_IDS
        self._influencers = (
            set(influencer_ids) if influencer_ids is not None else influencer_chat_ids_from_env()
        )
        # router 在多个工作线程里调用本处理器，共享状态的检查和修改都在这把锁里完成
        self._lock = threading.Lock()

    # ---- 白名单 ----

    def is_influencer(self, chat_id: str) -> bool:
        with self._lock:
            return chat_id in self._influencers

    def mark_influencer(self, chat_id: str) -> None:
        with self._lock:
            self._influencers.add(chat_id)

    def unmark_influencer(self, chat_id: str) -> None:
        with self._lock:
            self._influencers.discard(chat_id)

    # ---- 资质 ----

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

    async def _fetch_creator_stats(self, xhs_account: str) -> Dict[str, Any]:
        """查达人数据。拿不到就返回空字典，审批单会标成"待核实"，不编造数据（B8）。"""
        try:
            return await self.xhs.get_creator_stats(xhs_account) or {}
        except Exception as exc:
            logger.warning(f"[XHS] 查询达人数据失败，按待核实处理：{exc}")
            return {}

    async def _refresh_profile(self, chat_id: str, nickname: str, xhs_account: str) -> InfluencerProfile:
        """更新达人档案。只用达人提供的小红书账号查数据，绝不拿微信 ID 或昵称去查（B10）。"""
        stats = await self._fetch_creator_stats(xhs_account) if xhs_account else {}
        profile = InfluencerProfile(
            wechat_id=chat_id,
            nickname=nickname,
            xhs_id=xhs_account,
            follower_count=stats.get("follower_count"),
            recent_like_count=stats.get("recent_highest_likes"),
        )
        with self._lock:
            self.profiles[chat_id] = profile
        return profile

    # ---- 对话 ----

    def handle_private_message(self, chat_id: str, content: str, nickname: str = "") -> Optional[str]:
        """供同步的 router 调用。返回 None 表示这条私聊不归达人模块管。

        router 在工作线程里运行，线程里没有事件循环，所以直接用 asyncio.run（B19）。
        """
        if not chat_id or not self.is_influencer(chat_id):
            return None
        try:
            return asyncio.run(self.process_incoming_message(chat_id, content, nickname))
        except Exception:
            logger.exception("[达人模块] 处理私信出错，需要人工处理")
            return HOLDING_REPLY

    def _active_draft(self, chat_id: str) -> Optional[SampleRequestDraft]:
        """取这位达人还没补齐的申请；超过 DRAFT_TTL_SECONDS 没有动静的作废。"""
        with self._lock:
            draft = self.drafts.get(chat_id)
            if draft is not None and time.time() - draft.updated_at > DRAFT_TTL_SECONDS:
                del self.drafts[chat_id]
                return None
            return draft

    async def _attach_xhs_account(self, chat_id: str, xhs_account: str) -> bool:
        """审批单提交后达人才发来小红书主页：补进这位达人最近一张还缺账号的待审批单。"""
        with self._lock:
            waiting = [
                p for p in self.pending_approvals.values()
                if p.influencer_id == chat_id and p.status == GateStatus.PENDING and not p.xhs_account
            ]
        if not waiting:
            return False
        packet = max(waiting, key=lambda p: p.created_at)
        profile = await self._refresh_profile(chat_id, packet.influencer_name, xhs_account)
        with self._lock:
            packet.xhs_account = xhs_account
            packet.follower_count = profile.follower_count
            packet.qualification = self.assess_qualification(profile)
        return True

    async def process_incoming_message(
        self, chat_id: str, content: str, nickname: str = ""
    ) -> Optional[str]:
        """处理达人的私信。chat_id 是私聊的稳定 ID，不能用昵称（B10）。

        返回要回复的话；返回 None 表示这条消息不归达人模块管。
        """
        if not self.is_influencer(chat_id):
            return None
        draft = self._active_draft(chat_id)
        expecting_name = draft is not None and draft.asked_for == ("recipient_name",)
        info = extract_request_info(content, expecting_name=expecting_name)
        if draft is None:
            if info.xhs_account and not info.is_sample_request:
                if await self._attach_xhs_account(chat_id, info.xhs_account):
                    return "收到老师的小红书主页啦，已经补充到申请里了~"
            if not (info.is_sample_request or (info.recipient_phone and info.shipping_address)):
                return None
            draft = SampleRequestDraft(chat_id=chat_id)
            with self._lock:
                self.drafts[chat_id] = draft
        draft.merge(info)

        # 资质只在审批单上标注，由运营决定，不自动拒绝（D4）

        # 1. 不知道要哪款商品就先问，不拿占位名去查库存（B12）
        if not draft.product:
            draft.asked_for = ("product",)
            return "收到老师的合作意向啦~ ✨ 请问老师想申请我们哪一款产品的样品呢？"

        # 2. 聚水潭 ERP 查库存（换了商品才重新查）
        if draft.inventory is None:
            try:
                inventory = await self.erp.check_inventory(draft.product)
            except Exception as exc:
                logger.error(f"[ERP] 查询库存失败，需要人工处理：{exc}")
                return HOLDING_REPLY
            if not inventory.get("in_stock") or inventory.get("available_quantity", 0) <= 0:
                logger.warning(f"[ERP库存不足] {draft.product}")
                product, draft.product = draft.product, ""  # 达人可以改选其他产品
                draft.asked_for = ("product",)
                return (
                    f"亲亲，【{product}】的样品目前暂时没有库存，非常抱歉~ "
                    "老师也可以看看我们的其他产品哦！"
                )
            draft.inventory = inventory
        inventory = draft.inventory

        # 3. 收件信息没补齐：只追问缺的那几项
        missing = draft.missing_recipient_fields()
        if missing:
            draft.asked_for = tuple(missing)
            if len(missing) == len(RECIPIENT_FIELD_LABELS):
                return (
                    f"收到老师的需求啦！【{inventory['product_name']}】目前有货，我们很期待和老师合作~ ✨\n"
                    "麻烦老师发一下【收件人姓名、联系电话、收货地址】，方便的话再附上小红书主页链接，"
                    "我们登记后会提交审核，审核结果第一时间告诉老师~"
                )
            labels = "、".join(RECIPIENT_FIELD_LABELS[name] for name in missing)
            return f"收到~ 还差【{labels}】，麻烦老师补充一下哦~"

        # 4. 信息齐了：生成审批单，交给运营审批（R2）
        with self._lock:
            self.drafts.pop(chat_id, None)
        profile = await self._refresh_profile(chat_id, nickname, draft.xhs_account)
        packet_id = f"GATE-{uuid.uuid4().hex[:8].upper()}"
        action_packet = ApprovalActionPacket(
            packet_id=packet_id,
            influencer_id=chat_id,
            influencer_name=nickname or chat_id,
            follower_count=profile.follower_count,
            requested_product=inventory["product_name"],
            sku_id=inventory["sku_id"],
            stock_available=inventory["available_quantity"],
            sample_cost=inventory.get("cost_price", 18.5),
            proposed_commission_rate=DEFAULT_COMMISSION_RATE,
            shipping_address=draft.shipping_address,
            what_cannot_be_undone=(
                f"聚水潭ERP将自动扣减库存并出具打单运单，小红书将绑定 {DEFAULT_COMMISSION_RATE:.0%} 专属定向计划"
            ),
            qualification=self.assess_qualification(profile),
            recipient_name=draft.recipient_name,
            recipient_phone=draft.recipient_phone,
            xhs_account=draft.xhs_account,
            evidence="\n".join(draft.evidence),
        )
        with self._lock:
            self.pending_approvals[packet_id] = action_packet

        # 日志里不打印收件人信息
        logger.info(f"🚨 [Gate拦截-待运营审批] 审批单 {packet_id}：{action_packet.requested_product}（达人 {chat_id}）")
        reply = (
            f"收到老师的收件信息啦！【{inventory['product_name']}】的寄样申请已经提交审核~ 📝\n"
            f"收件人：{draft.recipient_name}（{_mask_phone(draft.recipient_phone)}）\n"
            f"收件地址：{draft.shipping_address}\n"
            "审核通过后我们会第一时间通知老师，请稍等哦~"
        )
        if not draft.xhs_account:
            reply += "\n另外麻烦老师发一下小红书主页链接，方便我们核对哦~"
        return reply

    # ---- 审批 ----

    async def execute_operator_approval(
        self,
        packet_id: str,
        approved: bool,
        *,
        xhs_account: str = "",
        commission_rate: Optional[float] = None,
    ) -> Dict[str, Any]:
        """运营审批。每张审批单只能处理一次：驳回、过期、执行中或已执行的单都不能再执行。

        xhs_account / commission_rate 是运营在审批时填写的：补填或更正达人的小红书账号，
        把佣金调低（不能超过上限）。
        """
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
            if xhs_account:
                packet.xhs_account = xhs_account
            if commission_rate is not None:
                if not 0 < commission_rate <= MAX_COMMISSION_RATE:
                    return {
                        "success": False,
                        "error": f"佣金 {commission_rate:.0%} 不在允许范围内（上限 {MAX_COMMISSION_RATE:.0%}）",
                    }
                packet.proposed_commission_rate = commission_rate
            # 以下检查不通过时都不改状态：运营补齐或改正后还能再批
            if packet.proposed_commission_rate > MAX_COMMISSION_RATE:
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
            if not (packet.recipient_name and packet.recipient_phone and packet.shipping_address):
                return {"success": False, "error": "收件人、电话或地址不全，不能建单"}
            if not packet.xhs_account:
                return {
                    "success": False,
                    "error": "缺少达人的小红书账号，不能绑定佣金；请让达人补发主页链接，或在审批时填写",
                }
            # 先占住这张单，调用外部系统期间，重复的"通过"会因为状态不是 PENDING 被挡下
            packet.status = GateStatus.EXECUTING

        try:
            order_res = await self.erp.create_sample_order(
                influencer_id=packet.influencer_id,
                sku_id=packet.sku_id,
                recipient_name=packet.recipient_name,
                recipient_phone=packet.recipient_phone,
                shipping_address=packet.shipping_address,
                order_ref=packet.packet_id,
            )
        except Exception as exc:
            return self._fail(packet, f"聚水潭建单出错：{exc}")
        if not order_res.get("success"):
            return self._fail(packet, f"聚水潭建单失败：{order_res.get('error') or '未知原因'}")

        try:
            comm_res = await self.xhs.set_exclusive_commission(
                xhs_uid=packet.xhs_account,
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
