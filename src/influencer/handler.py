"""达人寄样审批：生成审批单、运营批准后在聚水潭建寄样单。

达人的私聊由 PRM 起草、运营审核（D19、D25）；合作单进「待寄出」时，prm_bridge.SampleBridge 先用
check_product 到聚水潭查商品，再用 submit_for_approval 生成审批单。运营在控制台「达人审批」页批准，
execute_operator_approval 建寄样单（每张单只执行一次）。名单、审批单都存在数据库里（store.py），
机器人和控制台各自构造处理器，通过数据库共享数据（B5）。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, Optional

from .jst_erp import JuShuiTanERPClient
from .settings import (
    DEFAULT_COMMISSION_RATE,
    MAX_COMMISSION_RATE,
    STUCK_EXECUTION_SECONDS,
    sample_limits,
    influencer_chat_ids_from_env,
)
from .store import (
    EVENT_EXECUTION_STARTED,
    INBOX_OUT_OF_STOCK,
    SOURCE_BOT,
    SOURCE_CONSOLE,
    SOURCE_ENV,
    InfluencerStore,
)
from .types import (
    STATUS_LABELS,
    ApprovalActionPacket,
    GateStatus,
    InfluencerProfile,
    SampleRequestDraft,
)
from .xhs_cli import XiaohongshuCLIClient

logger = logging.getLogger(__name__)

QUALIFICATION_UNKNOWN = "待核实：没有达人数据，需要运营人工核实"
# 查商品时聚水潭连不上（check_product 的结果）
ERP_UNREACHABLE = "聚水潭查不了商品（连不上或者还没接入），请人工处理"
# 达人要求提高佣金时的固定回复（prm_bridge 作为系统消息放进待审批，D22）。佣金不交给模型谈，上限见 MAX_COMMISSION_RATE。
# D37（2026-10-26）：起步 20%，涨不涨由运营看这位达人近 30 天在我们店的单量和成交额再定，
# 所以这里只说"帮老师申请"，不替运营拒绝，也不承诺能涨。不能有违规词（FORBIDDEN_WORDS）
COMMISSION_REPLY = (
    "特别理解老师的想法！我这边先帮老师跟负责人申请一下，有结果马上回复老师哈~\n"
    "也建议老师先收样品感受下质感，咱们边合作边看数据，您看好不好？"
)
# 广告法和平台常见的违规词：固定话术里不能出现（测试里检查）
FORBIDDEN_WORDS = ("全网", "破价", "最低", "最大", "最好", "第一", "极致", "绝对", "惊人", "顶级", "100%")


def product_in_erp(inventory: Dict[str, Any]) -> bool:
    """聚水潭 ERP 里有没有这个商品（D35：有就能寄，不看现货）。查询结果明确说了 found 就以它为准；
    老的结果没有 found 字段时，有 SKU 就算有。"""
    if "found" in inventory:
        return bool(inventory["found"])
    return bool(inventory.get("sku_id"))


def approval_notice(packet: ApprovalActionPacket) -> str:
    """执行成功后发给达人的确认消息。现阶段显示在审批卡上，由运营复制后手动发送（Q2）。"""
    # 专属佣金由 PRM 的平台动作单独开、单独审核（D34），这里不提，免得佣金还没开就先说开了
    if packet.tracking_number:
        return (
            f"老师好~ 【{packet.requested_product}】的样品已经寄出啦 📦\n"
            f"快递单号：{packet.tracking_number}，请老师留意查收哦~ 收到后有任何问题随时找我们！"
        )
    # 对外口径（D36）：包邮，审核通过后 3 天内寄出
    return (
        f"老师好~ 【{packet.requested_product}】的样品已经通过审核啦 🎉\n"
        "我们 3 天内寄出（包邮），快递单号出来后发给老师~ 收到后有任何问题随时找我们！"
    )


def rejection_notice(packet: ApprovalActionPacket) -> str:
    """驳回后发给达人的婉拒话术，同样由运营复制后发送。驳回原因只留在操作记录里，不告诉达人。"""
    return (
        f"老师好~ 非常感谢老师对我们的关注！这次【{packet.requested_product}】的样品申请暂时没能通过审核，"
        "非常抱歉~ 后续有合适的合作机会，我们会尽快联系老师哦！"
    )


def build_influencer_handler(db_path: str) -> "InfluencerBusinessHandler":
    """机器人启动时调用：打开数据库，把环境变量 INFLUENCER_CHAT_IDS 并进名单（只增不删，Q4），
    再做一次维护（过期、清除到期的个人信息）。"""
    store = InfluencerStore(db_path)
    added = store.add_contacts(
        sorted(influencer_chat_ids_from_env()), source=SOURCE_ENV, note="来自环境变量 INFLUENCER_CHAT_IDS",
    )
    if added:
        logger.info(f"[达人模块] 从环境变量把 {len(added)} 位达人加进了名单")
    store.run_maintenance()
    return InfluencerBusinessHandler(store)


def sample_limit_problem(store: InfluencerStore, packet: ApprovalActionPacket, now: Optional[float] = None) -> str:
    """寄样上限（D5）。超过上限返回原因；没超、或者没设上限，返回空字符串。"""
    now = time.time() if now is None else now
    limits = sample_limits()
    if limits["per_person"]:
        days = limits["window_days"]
        sent = store.count_samples(since=now - days * 24 * 3600, chat_id=packet.influencer_id)
        if sent >= limits["per_person"]:
            return f"这位达人 {days} 天内已经寄了 {sent} 单，到了每人 {limits['per_person']} 单的上限"
    if limits["per_day"]:
        start_of_day = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        sent = store.count_samples(since=start_of_day)
        if sent >= limits["per_day"]:
            return f"今天已经寄了 {sent} 单，到了每天 {limits['per_day']} 单的上限"
    return ""



class InfluencerBusinessHandler:
    def __init__(
        self,
        store: InfluencerStore,
        erp_client: Optional[JuShuiTanERPClient] = None,
        xhs_client: Optional[XiaohongshuCLIClient] = None,
    ):
        self.store = store
        self.erp = erp_client or JuShuiTanERPClient()
        self.xhs = xhs_client or XiaohongshuCLIClient()

    def is_influencer(self, chat_id: str) -> bool:
        """只处理名单里、并且没有停用的私聊 chat_id（D3）。"""
        return self.store.is_influencer(chat_id)

    # ---- 资质 ----

    def assess_qualification(self, profile: InfluencerProfile) -> str:
        """给审批单标注资质（D4）：只标注、不拦单。D30 起寄几款按聚水潭的销售额分档，不看粉丝数
        （找达人时已经按销售额、等级等筛过），这里的粉丝数只给运营参考。"""
        if profile.follower_count is None:
            return QUALIFICATION_UNKNOWN
        return f"粉丝 {profile.follower_count}（只作参考，寄几款看销售额分档）"

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
        self.store.record_profile(
            chat_id, nickname=nickname, xhs_account=xhs_account,
            follower_count=profile.follower_count, recent_like_count=profile.recent_like_count,
        )
        return profile

    # ---- 生成审批单 ----

    async def check_product(self, draft: SampleRequestDraft) -> str:
        """到聚水潭 ERP 查这款商品，查到的结果记进 draft.inventory。能寄返回空字符串，不能寄返回原因。
        D35：ERP 里有这个商品就能寄，不看现货（每个 SKU 挂着虚拟库存，现货多半是 0）；
        只有 ERP 里查不到、或者已停用才不能寄，并记进控制台「待处理」让运营知道（B21、D14）。"""
        try:
            inventory = await self.erp.check_inventory(draft.product)
        except Exception as exc:
            logger.error(f"[ERP] 查询商品失败，需要人工处理：{exc}")
            return ERP_UNREACHABLE
        if not product_in_erp(inventory):
            logger.warning(f"[ERP查不到商品] {draft.product}")
            self.store.add_inbox_item(
                draft.chat_id, INBOX_OUT_OF_STOCK, f"想要【{draft.product}】的样品，但聚水潭 ERP 里查不到这个商品（或已停用）",
            )
            return f"【{draft.product}】在聚水潭 ERP 里查不到（已记进「待处理」）"
        draft.inventory = inventory
        return ""

    async def submit_for_approval(
        self, chat_id: str, nickname: str, draft: SampleRequestDraft, extra: Optional[Dict[str, Any]] = None,
    ) -> str:
        """信息齐了（先过 check_product）：生成审批单，交给运营审批（R2），返回审批单号。extra 是审批单上的
        其他字段（平台、档位、主播类型、多款商品、专场，prm_bridge 从名单和合作单上取）。"""
        inventory = draft.inventory
        profile = await self._refresh_profile(chat_id, nickname, draft.xhs_account)
        packet = ApprovalActionPacket(
            packet_id=f"GATE-{uuid.uuid4().hex[:8].upper()}",
            influencer_id=chat_id,
            influencer_name=nickname or chat_id,
            follower_count=profile.follower_count,
            requested_product=inventory["product_name"],
            sku_id=inventory["sku_id"],
            stock_available=inventory["available_quantity"],
            sample_cost=inventory.get("cost_price"),  # 聚水潭没给成本就是未知，不编一个数（同 B8）
            proposed_commission_rate=DEFAULT_COMMISSION_RATE,
            shipping_address=draft.shipping_address,
            what_cannot_be_undone="在聚水潭「寄样管理」建寄样单，会真的发货（专属佣金另在「待审批」的平台动作里开）",
            qualification=self.assess_qualification(profile),
            recipient_name=draft.recipient_name,
            recipient_phone=draft.recipient_phone,
            xhs_account=draft.xhs_account,
            evidence="\n".join(draft.evidence),
            **(extra or {}),
        )
        self.store.add_approval(packet, source=SOURCE_BOT)
        # 日志里不打印收件人信息
        logger.info(f"🚨 [Gate拦截-待运营审批] 审批单 {packet.packet_id}：{packet.requested_product}（达人 {chat_id}）")
        return packet.packet_id

    # ---- 审批 ----

    async def execute_operator_approval(
        self,
        packet_id: str,
        approved: bool,
        *,
        xhs_account: str = "",
        commission_rate: Optional[float] = None,
        reason: str = "",
        source: str = SOURCE_CONSOLE,
    ) -> Dict[str, Any]:
        """运营审批。每张审批单只能处理一次：驳回、过期、执行中或已执行的单都不能再执行。

        xhs_account / commission_rate 是运营在审批时填写的：补填或更正达人的小红书账号，
        把建议佣金调低（不能超过上限）。reason 是驳回原因，记进操作记录，每周复盘用（R7）。

        批准后只在聚水潭「寄样管理」建寄样单。专属佣金不在这里开：用 PRM 的平台动作（src/prm/platforms.py，
        小红书定向计划等），在「待审批」里单独预览、审核、执行（D34）。commission_rate 只是记在单上的建议值。
        """
        packet = self.store.get_approval(packet_id)
        if packet is None:
            return {"success": False, "error": "未找到对应的审批单号"}
        if packet.status != GateStatus.PENDING:
            return self._not_pending(packet)

        if not approved:
            if not self.store.transition(packet_id, GateStatus.PENDING, GateStatus.REJECTED):
                return self._not_pending(self.store.get_approval(packet_id))
            self.store.add_event(
                "驳回", packet_id=packet_id, chat_id=packet.influencer_id, detail=reason, source=source,
            )
            return {
                "success": True,
                "message": f"任务单 {packet_id} 已驳回",
                "notice": rejection_notice(packet),
            }

        if time.time() > packet.expires_at:
            if self.store.transition(
                packet_id, GateStatus.PENDING, GateStatus.EXPIRED, handled_at=packet.expires_at,
            ):
                self.store.add_event(
                    "过期", packet_id=packet_id, chat_id=packet.influencer_id,
                    detail="批准时已经超过有效期", source=source,
                )
            return {"success": False, "error": f"审批单 {packet_id} 已过期，需要和达人重新确认"}

        if xhs_account and xhs_account != packet.xhs_account:
            # 运营填的账号马上存下：后面的检查没通过的话，下次批准不用再填一遍
            if self.store.update_pending(packet_id, xhs_account=xhs_account):
                self.store.add_event(
                    "填写小红书账号", packet_id=packet_id, chat_id=packet.influencer_id, source=source,
                )
            packet.xhs_account = xhs_account
        if commission_rate is not None:
            if not 0 < commission_rate <= MAX_COMMISSION_RATE:
                return {
                    "success": False,
                    "error": f"佣金 {commission_rate:.0%} 不在允许范围内（上限 {MAX_COMMISSION_RATE:.0%}）",
                }
            packet.proposed_commission_rate = commission_rate
        # 以下检查不通过时都不改状态：运营补齐或改正后还能再批
        problem = self._why_not_executable(packet) or sample_limit_problem(self.store, packet)
        if problem:
            return {"success": False, "error": problem}

        # 审批单最长能等 72 小时，批准时重新查一次商品（Q1）。D35：商品还在 ERP 里就能寄，
        # 现货多少只记在单上给运营参考，不拦单
        try:
            inventory = await self.erp.check_inventory(packet.requested_product)
        except Exception as exc:
            return {"success": False, "error": f"批准前复查商品失败：{exc}"}
        if not product_in_erp(inventory):
            return {
                "success": False,
                "error": f"【{packet.requested_product}】在聚水潭 ERP 里查不到了（或已停用），不能建单；可以先驳回",
            }
        available = int(inventory.get("available_quantity") or 0)

        # 先占住这张单：两个"通过"同时到达时，只有一个能把它从待审批改成执行中
        if not self.store.transition(
            packet_id, GateStatus.PENDING, GateStatus.EXECUTING,
            proposed_commission_rate=packet.proposed_commission_rate, stock_available=available,
        ):
            return self._not_pending(self.store.get_approval(packet_id))
        self.store.add_event(
            EVENT_EXECUTION_STARTED, packet_id=packet_id, chat_id=packet.influencer_id, source=source,
        )

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
            return self._fail(packet, f"聚水潭建单出错：{exc}", source)
        if not order_res.get("success"):
            return self._fail(packet, f"聚水潭建单失败：{order_res.get('error') or '未知原因'}", source)
        order_sn = str(order_res.get("order_sn") or "")
        tracking_number = str(order_res.get("tracking_number") or "")
        carrier = str(order_res.get("carrier") or "")

        self.store.transition(
            packet_id, GateStatus.EXECUTING, GateStatus.FULFILLED,
            order_sn=order_sn, tracking_number=tracking_number,
        )
        self.store.add_event(
            "执行成功", packet_id=packet_id, chat_id=packet.influencer_id,
            detail=f"寄样单 {order_sn}", source=source,
        )
        packet.tracking_number = tracking_number
        return {
            "success": True,
            "packet_id": packet_id,
            "order_sn": order_sn,
            "tracking_number": tracking_number,
            "carrier": carrier,
            "message": "已在聚水潭建寄样单。专属佣金请在「待审批」里用平台动作开",
            "notice": approval_notice(packet),
        }

    def resolve_interrupted_execution(
        self,
        packet_id: str,
        fulfilled: bool,
        *,
        order_sn: str = "",
        tracking_number: str = "",
        commission_plan: str = "",
        note: str = "",
        source: str = SOURCE_CONSOLE,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """执行被中断、一直停在"执行中"的单（B23）：运营到聚水潭和小红书核对后，在这里标成
        已执行或失败。这里不调用聚水潭和小红书，只记下运营核对的结果。

        标成已执行要填寄样单号（核对过才知道）；标成失败要写原因。标完以后这张单算处理完，
        收件信息照常在 90 天后清除。
        """
        now = time.time() if now is None else now
        packet = self.store.get_approval(packet_id)
        if packet is None:
            return {"success": False, "error": "未找到对应的审批单号"}
        if packet.status != GateStatus.EXECUTING:
            return {
                "success": False,
                "error": f"审批单 {packet_id} {STATUS_LABELS[packet.status]}，只有停在执行中的单才需要人工核对",
            }
        started = self.store.execution_started_at(packet_id)
        if started is not None and now - started < STUCK_EXECUTION_SECONDS:
            return {
                "success": False,
                "error": (
                    f"这张单 {int((now - started) // 60)} 分钟前才开始执行，可能还在跑；"
                    f"开始执行 {STUCK_EXECUTION_SECONDS // 60} 分钟后还停在这里再核对"
                ),
            }
        order_sn, tracking_number = order_sn.strip(), tracking_number.strip()
        commission_plan, note = commission_plan.strip(), note.strip()

        if fulfilled:
            if not order_sn:
                return {"success": False, "error": "标成已执行要填聚水潭的寄样单号"}
            if not self.store.transition(
                packet_id, GateStatus.EXECUTING, GateStatus.FULFILLED,
                order_sn=order_sn, tracking_number=tracking_number, commission_plan=commission_plan,
                handled_at=now,
            ):
                return {"success": False, "error": f"审批单 {packet_id} 的状态刚刚变了，请刷新后再看"}
            detail = f"寄样单 {order_sn}" + (f"；{note}" if note else "")
            self.store.add_event(
                "人工核对：已执行", packet_id=packet_id, chat_id=packet.influencer_id, detail=detail, source=source,
            )
            packet.tracking_number = tracking_number
            return {"success": True, "packet_id": packet_id, "order_sn": order_sn, "notice": approval_notice(packet)}

        if not note:
            return {"success": False, "error": "标成失败要写明核对的结果，例如「聚水潭里没有这张单」"}
        if not self.store.transition(
            packet_id, GateStatus.EXECUTING, GateStatus.FAILED,
            failure_reason=f"执行中断，人工核对后标为失败：{note}",
            order_sn=order_sn, tracking_number=tracking_number, handled_at=now,
        ):
            return {"success": False, "error": f"审批单 {packet_id} 的状态刚刚变了，请刷新后再看"}
        self.store.add_event(
            "人工核对：失败", packet_id=packet_id, chat_id=packet.influencer_id, detail=note, source=source,
        )
        return {"success": True, "packet_id": packet_id}

    @staticmethod
    def _why_not_executable(packet: ApprovalActionPacket) -> str:
        """执行前的检查。返回不能执行的原因；可以执行时返回空字符串。"""
        if packet.proposed_commission_rate > MAX_COMMISSION_RATE:
            return (
                f"佣金 {packet.proposed_commission_rate:.0%} 超过上限 "
                f"{MAX_COMMISSION_RATE:.0%}，不能执行"
            )
        if not (packet.recipient_name and packet.recipient_phone and packet.shipping_address):
            return "收件人、电话或地址不全，不能建单"
        # 不再要求小红书账号：抖音达人也要寄样（D31），批准后也不在这里开佣金（D34）
        return ""

    @staticmethod
    def _not_pending(packet: ApprovalActionPacket) -> Dict[str, Any]:
        return {
            "success": False,
            "error": f"审批单 {packet.packet_id} {STATUS_LABELS[packet.status]}，不能再处理",
        }

    def _fail(
        self, packet: ApprovalActionPacket, reason: str, source: str,
        *, order_sn: str = "", tracking_number: str = "",
    ) -> Dict[str, Any]:
        """把审批单标为失败并写明原因，后续步骤一律不再执行。"""
        self.store.transition(
            packet.packet_id, GateStatus.EXECUTING, GateStatus.FAILED,
            failure_reason=reason, order_sn=order_sn, tracking_number=tracking_number,
        )
        self.store.add_event(
            "执行失败", packet_id=packet.packet_id, chat_id=packet.influencer_id, detail=reason, source=source,
        )
        logger.error(f"[审批执行失败] {packet.packet_id}: {reason}")
        result: Dict[str, Any] = {"success": False, "packet_id": packet.packet_id, "error": reason}
        if order_sn:
            result["order_sn"] = order_sn
        return result
