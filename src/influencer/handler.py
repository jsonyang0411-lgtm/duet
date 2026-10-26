"""Influencer Business Handler.

Wires Intent Recognition, Multi-turn Context, Qualification Screening,
JST ERP, XHS CLI, and Layer 6 Gate into a unified business pipeline.

名单、没补齐的申请和审批单都存在数据库里（store.py）。机器人和控制台各自构造处理器，
通过数据库共享数据，不共享内存对象（第 6 项，B5）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Dict, Optional

from .extract import asks_for_higher_commission, extract_request_info
from .jst_erp import JuShuiTanERPClient
from .settings import (
    DEFAULT_COMMISSION_RATE,
    DRAFT_TTL_SECONDS,
    HOLDING_REPLY_INTERVAL_SECONDS,
    MAX_COMMISSION_RATE,
    MIN_FOLLOWERS,
    STUCK_EXECUTION_SECONDS,
    brain_enabled,
    sample_limits,
    influencer_chat_ids_from_env,
)
from .send_policy import SendPolicy
from .store import (
    EVENT_EXECUTION_STARTED,
    INBOX_COMMISSION,
    INBOX_MESSAGE,
    INBOX_OUT_OF_STOCK,
    SOURCE_BOT,
    SOURCE_CONSOLE,
    SOURCE_ENV,
    InfluencerStore,
)
from .types import (
    RECIPIENT_FIELD_LABELS,
    STATUS_LABELS,
    ApprovalActionPacket,
    GateStatus,
    InfluencerProfile,
    SampleRequestDraft,
)
from .xhs_cli import XiaohongshuCLIClient

if TYPE_CHECKING:
    from .claude_brain import ClaudeBrain

logger = logging.getLogger(__name__)

QUALIFICATION_OK = "达标"
QUALIFICATION_FAILED = "不达标"
QUALIFICATION_UNKNOWN = "待核实：没有小红书数据，需要运营人工核实"
# 出了问题时的中性回复：不承诺任何事，也不让这条消息落到聊天模型那里随口作答
HOLDING_REPLY = "收到老师的消息啦~ 我们这边核对一下，再回复老师哦！"
# 达人要求提高佣金时的固定回复（2026-10-25 用户给的原文）。佣金不交给模型谈，上限见 MAX_COMMISSION_RATE
COMMISSION_REPLY = (
    "特别理解老师的想法！坦白讲我也特别希望能给老师申请更高点位，但咱们这款是全网破价的招牌款，"
    "利润空间被供应链卡得特别紧，目前这个佣金已经是我们能争取到的最大诚意啦😭\n"
    "不过老师放心，这款产品在小红书的自带转化率和复购率非常惊人！很多合作的老师虽然佣金点数适中，"
    "但因为出单量极大，最终实际落袋的收益反而比那些高佣却带不动的品香很多！"
    "建议老师可以先收样品感受下质感，合适咱们再对排期，您看好不好？"
)


def _mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}" if len(phone) == 11 else phone


def approval_notice(packet: ApprovalActionPacket) -> str:
    """执行成功后发给达人的确认消息。现阶段显示在审批卡上，由运营复制后手动发送（Q2）。"""
    tracking = (
        f"快递单号：{packet.tracking_number}，" if packet.tracking_number
        else "快递单号出来后我们再发给老师，"
    )
    # 不发推广链接：定向计划开好以后，达人在小红书的选品里自己就能看到这款商品和专属佣金（D17）
    return (
        f"老师好~ 【{packet.requested_product}】的样品已经安排寄出啦 📦\n"
        f"{tracking}请老师留意查收哦~\n"
        f"另外已经为老师开通了【{packet.requested_product}】的专属佣金，老师在小红书的选品里就能找到这款商品~ "
        "收到后有任何问题随时找我们！"
    )


def rejection_notice(packet: ApprovalActionPacket) -> str:
    """驳回后发给达人的婉拒话术，同样由运营复制后发送。驳回原因只留在操作记录里，不告诉达人。"""
    return (
        f"老师好~ 非常感谢老师对我们的关注！这次【{packet.requested_product}】的样品申请暂时没能通过审核，"
        "非常抱歉~ 后续有合适的合作机会，我们会第一时间联系老师哦！"
    )


def build_influencer_handler(db_path: str) -> "InfluencerBusinessHandler":
    """机器人启动时调用：打开数据库，把环境变量 INFLUENCER_CHAT_IDS 并进名单（只增不删，Q4），
    再做一次维护（过期、清除到期的个人信息、删掉作废的草稿）。"""
    store = InfluencerStore(db_path)
    added = store.add_contacts(
        sorted(influencer_chat_ids_from_env()), source=SOURCE_ENV, note="来自环境变量 INFLUENCER_CHAT_IDS",
    )
    if added:
        logger.info(f"[达人模块] 从环境变量把 {len(added)} 位达人加进了名单")
    store.run_maintenance()
    brain = None
    if brain_enabled():
        from .claude_brain import ClaudeBrain

        brain = ClaudeBrain(store, db_path)
        logger.info("[达人模块] 达人的消息交给 Claude 回复（INFLUENCER_BRAIN=claude）")
    return InfluencerBusinessHandler(store, brain=brain, send_policy=SendPolicy.from_env())


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
        brain: Optional["ClaudeBrain"] = None,
        send_policy: Optional[SendPolicy] = None,
    ):
        self.store = store
        self.erp = erp_client or JuShuiTanERPClient()
        self.xhs = xhs_client or XiaohongshuCLIClient()
        # 交给 Claude 回复（claude_brain.py）；None 表示只用规则流程
        self.brain = brain
        # 防封：回复前像真人一样等一会儿（send_policy.py）；None 表示不等（测试里直接构造时）
        self.send_policy = send_policy
        # router 在多个工作线程里调用本处理器。同一位达人的消息一条一条处理：
        # 草稿是"读出来、改、写回去"，两条消息同时处理会丢掉其中一条的信息
        self._chat_locks: Dict[str, threading.Lock] = {}
        self._chat_locks_guard = threading.Lock()

    def is_influencer(self, chat_id: str) -> bool:
        """只处理名单里、并且没有停用的私聊 chat_id（D3）。"""
        return self.store.is_influencer(chat_id)

    # ---- 资质 ----

    def assess_qualification(self, profile: InfluencerProfile) -> str:
        """给审批单标注资质（D4）：只标注、不自动拒绝；拿不到数据就是"待核实"。"""
        if profile.follower_count is None:
            return QUALIFICATION_UNKNOWN
        # 只看粉丝数（D16）：小红书 CLI 查不到点赞，点赞由运营审批时自己看
        if profile.follower_count < MIN_FOLLOWERS:
            return f"{QUALIFICATION_FAILED}：粉丝 {profile.follower_count} 低于 {MIN_FOLLOWERS}"
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
        self.store.record_profile(
            chat_id, nickname=nickname, xhs_account=xhs_account,
            follower_count=profile.follower_count, recent_like_count=profile.recent_like_count,
        )
        return profile

    # ---- 对话 ----

    def handle_private_message(self, chat_id: str, content: str, nickname: str = "") -> Optional[str]:
        """供同步的 router 调用。返回 None 表示这条私聊不归达人模块管；
        返回空字符串表示归达人模块管、但这次不回复（router 不能再交给聊天模型）。

        router 在工作线程里运行，线程里没有事件循环，所以直接用 asyncio.run（B19）。
        """
        if not chat_id:
            return None
        try:
            if not self.is_influencer(chat_id):
                return None
        except Exception:
            logger.exception("[达人模块] 读不到达人名单，这条私聊按普通私聊处理")
            return None
        try:
            with self._chat_lock(chat_id):
                started = time.time()
                reply = asyncio.run(self.process_incoming_message(chat_id, content, nickname))
                if reply and self.send_policy is not None:
                    time.sleep(self.send_policy.reply_delay(reply, already_spent=time.time() - started))
                return reply
        except Exception:
            logger.exception("[达人模块] 处理私信出错，需要人工处理")
            return HOLDING_REPLY

    def _chat_lock(self, chat_id: str) -> threading.Lock:
        with self._chat_locks_guard:
            return self._chat_locks.setdefault(chat_id, threading.Lock())

    def _active_draft(self, chat_id: str) -> Optional[SampleRequestDraft]:
        """取这位达人还没补齐的申请；超过 DRAFT_TTL_SECONDS 没有动静的作废。"""
        draft = self.store.get_draft(chat_id)
        if draft is not None and time.time() - draft.updated_at > DRAFT_TTL_SECONDS:
            self.store.delete_draft(chat_id)
            return None
        return draft

    async def _attach_xhs_account(self, chat_id: str, xhs_account: str) -> bool:
        """审批单提交后达人才发来小红书主页：补进这位达人最近一张还缺账号的待审批单。"""
        packet = self.store.latest_pending_without_xhs_account(chat_id)
        if packet is None:
            return False
        profile = await self._refresh_profile(chat_id, packet.influencer_name, xhs_account)
        attached = self.store.update_pending(
            packet.packet_id,
            xhs_account=xhs_account,
            follower_count=profile.follower_count,
            qualification=self.assess_qualification(profile),
        )
        if attached:
            self.store.add_event(
                "补充小红书账号", packet_id=packet.packet_id, chat_id=chat_id,
                detail="达人发来了小红书主页", source=SOURCE_BOT,
            )
        return attached

    async def process_incoming_message(
        self, chat_id: str, content: str, nickname: str = ""
    ) -> Optional[str]:
        """处理达人的私信。chat_id 是私聊的稳定 ID，不能用昵称（B10）。

        返回要回复的话；返回空字符串表示不回复；返回 None 表示这条消息不归达人模块管
        （只有名单以外的人才会这样）。
        """
        if not self.is_influencer(chat_id):
            return None
        reply = await self._reply(chat_id, content, nickname)
        if self.brain is not None and reply is not None:
            # 记下这一轮，下次交给 Claude 时带上，它才记得前面聊过什么
            self.store.add_dialog(chat_id, content, reply)
        return reply

    async def _reply(self, chat_id: str, content: str, nickname: str) -> Optional[str]:
        if asks_for_higher_commission(content):
            return self._answer_commission_request(chat_id, content)
        if self.brain is not None:
            answer = self.brain.reply(chat_id, content, nickname)
            if answer is not None and not answer.blocked:
                return answer.text
            if answer is not None:
                # 回复里有不该说的话：不发，交给运营；运营在「待处理」里能看到原本要发的话
                return self._hand_over_to_operator(
                    chat_id, f"{content}\n（Claude 的回复没发出：{answer.blocked}。原本要发：{answer.text}）",
                )
            # Claude 出错或超时：退回下面的规则流程
        return await self._rule_reply(chat_id, content, nickname)

    async def _rule_reply(self, chat_id: str, content: str, nickname: str) -> Optional[str]:
        """规则流程：没打开 Claude，或者 Claude 这次没能回复时用。"""
        draft = self._active_draft(chat_id)
        expecting_name = draft is not None and draft.asked_for == ("recipient_name",)
        info = extract_request_info(content, expecting_name=expecting_name)
        if draft is None:
            if info.xhs_account and not info.is_sample_request:
                if await self._attach_xhs_account(chat_id, info.xhs_account):
                    return "收到老师的小红书主页啦，已经补充到申请里了~"
            if not (info.is_sample_request or (info.recipient_phone and info.shipping_address)):
                return self._hand_over_to_operator(chat_id, content)
            draft = SampleRequestDraft(chat_id=chat_id)
        draft.merge(info)

        # 资质只在审批单上标注，由运营决定，不自动拒绝（D4）
        reply = await self._ask_for_what_is_missing(draft)
        if reply is not None:
            self.store.save_draft(draft)
            return reply
        return await self._submit_for_approval(chat_id, nickname, draft)

    def _hand_over_to_operator(self, chat_id: str, content: str) -> str:
        """达人发的不是索样（闲聊、问发货等）：不交给聊天模型随口作答（B20），记进控制台的
        待处理事项，由运营人工回复。机器人先回一句不做任何承诺的话，一段时间内只回一次。"""
        last = self.store.last_auto_reply_at(chat_id)
        reply_now = last is None or time.time() - last > HOLDING_REPLY_INTERVAL_SECONDS
        self.store.add_inbox_item(chat_id, INBOX_MESSAGE, content, auto_replied=reply_now)
        return HOLDING_REPLY if reply_now else ""

    def _answer_commission_request(self, chat_id: str, content: str) -> str:
        """达人要求提高佣金：回固定话术，不交给模型谈，也不改审批单上的佣金；记进「待处理」
        让运营知道。同一位达人一段时间内再提，不重复回同一段话，只记待处理。
        还没补齐的申请原样留着，达人接着发收件信息还能继续。"""
        last = self.store.last_auto_reply_at(chat_id, INBOX_COMMISSION)
        reply_now = last is None or time.time() - last > HOLDING_REPLY_INTERVAL_SECONDS
        self.store.add_inbox_item(chat_id, INBOX_COMMISSION, content, auto_replied=reply_now)
        return COMMISSION_REPLY if reply_now else ""

    async def _ask_for_what_is_missing(self, draft: SampleRequestDraft) -> Optional[str]:
        """申请还缺东西就返回要追问的话；都齐了返回 None。"""
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
                # 缺货要让运营知道（B21），提醒只做在控制台里（D14）
                self.store.add_inbox_item(
                    draft.chat_id, INBOX_OUT_OF_STOCK, f"想要【{draft.product}】的样品，但聚水潭显示没有库存",
                )
                product, draft.product = draft.product, ""  # 达人可以改选其他产品
                draft.asked_for = ("product",)
                return (
                    f"亲亲，【{product}】的样品目前暂时没有库存，非常抱歉~ "
                    "老师也可以看看我们的其他产品哦！"
                )
            draft.inventory = inventory

        # 3. 收件信息没补齐：只追问缺的那几项
        missing = draft.missing_recipient_fields()
        if missing:
            draft.asked_for = tuple(missing)
            if len(missing) == len(RECIPIENT_FIELD_LABELS):
                return (
                    f"收到老师的需求啦！【{draft.inventory['product_name']}】目前有货，我们很期待和老师合作~ ✨\n"
                    "麻烦老师发一下【收件人姓名、联系电话、收货地址】，方便的话再附上小红书主页链接，"
                    "我们登记后会提交审核，审核结果第一时间告诉老师~"
                )
            labels = "、".join(RECIPIENT_FIELD_LABELS[name] for name in missing)
            return f"收到~ 还差【{labels}】，麻烦老师补充一下哦~"
        return None

    async def _submit_for_approval(self, chat_id: str, nickname: str, draft: SampleRequestDraft) -> str:
        """信息齐了：生成审批单，交给运营审批（R2）。"""
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
            what_cannot_be_undone=(
                f"聚水潭ERP将自动扣减库存并出具打单运单，小红书将绑定 {DEFAULT_COMMISSION_RATE:.0%} 专属定向计划"
            ),
            qualification=self.assess_qualification(profile),
            recipient_name=draft.recipient_name,
            recipient_phone=draft.recipient_phone,
            xhs_account=draft.xhs_account,
            evidence="\n".join(draft.evidence),
        )
        self.store.add_approval(packet, source=SOURCE_BOT)
        self.store.delete_draft(chat_id)

        # 日志里不打印收件人信息
        logger.info(f"🚨 [Gate拦截-待运营审批] 审批单 {packet.packet_id}：{packet.requested_product}（达人 {chat_id}）")
        reply = (
            f"收到老师的收件信息啦！【{packet.requested_product}】的寄样申请已经提交审核~ 📝\n"
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
        reason: str = "",
        source: str = SOURCE_CONSOLE,
    ) -> Dict[str, Any]:
        """运营审批。每张审批单只能处理一次：驳回、过期、执行中或已执行的单都不能再执行。

        xhs_account / commission_rate 是运营在审批时填写的：补填或更正达人的小红书账号，
        把佣金调低（不能超过上限）。reason 是驳回原因，记进操作记录，每周复盘用（R7）。
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

        # 审批单最长能等 72 小时，批准时重新查一次库存（Q1）
        try:
            inventory = await self.erp.check_inventory(packet.requested_product)
        except Exception as exc:
            return {"success": False, "error": f"批准前复查库存失败：{exc}"}
        available = inventory.get("available_quantity", 0) if inventory.get("in_stock") else 0
        if available <= 0:
            return {
                "success": False,
                "error": f"【{packet.requested_product}】现在没有库存，不能建单；可以先驳回，或者等补货后再批",
            }

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
                f"出库单 {order_sn} 已建，但小红书佣金绑定失败："
                f"{comm_res.get('error') or '未知原因'}，需要人工处理",
                source,
                order_sn=order_sn,
                tracking_number=tracking_number,
            )

        commission_plan = str(comm_res.get("plan_id") or "")
        self.store.transition(
            packet_id, GateStatus.EXECUTING, GateStatus.FULFILLED,
            order_sn=order_sn, tracking_number=tracking_number, commission_plan=commission_plan,
        )
        self.store.add_event(
            "执行成功", packet_id=packet_id, chat_id=packet.influencer_id,
            detail=f"出库单 {order_sn}，佣金 {packet.proposed_commission_rate:.0%}", source=source,
        )
        packet.tracking_number = tracking_number
        return {
            "success": True,
            "packet_id": packet_id,
            "order_sn": order_sn,
            "tracking_number": tracking_number,
            "commission_plan": commission_plan,
            "message": "已成功下发聚水潭打单，并设置小红书定向佣金！",
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

        标成已执行要填出库单号（核对过才知道）；标成失败要写原因。标完以后这张单算处理完，
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
                return {"success": False, "error": "标成已执行要填聚水潭的出库单号"}
            if not self.store.transition(
                packet_id, GateStatus.EXECUTING, GateStatus.FULFILLED,
                order_sn=order_sn, tracking_number=tracking_number, commission_plan=commission_plan,
                handled_at=now,
            ):
                return {"success": False, "error": f"审批单 {packet_id} 的状态刚刚变了，请刷新后再看"}
            detail = f"出库单 {order_sn}" + (f"；{note}" if note else "")
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
        if packet.qualification.startswith(QUALIFICATION_FAILED):
            # R3：数据明确不达标的达人不寄样，批准了也不执行
            return f"达人资质{packet.qualification}，按规则不能寄样，请驳回"
        if not (packet.recipient_name and packet.recipient_phone and packet.shipping_address):
            return "收件人、电话或地址不全，不能建单"
        if not packet.xhs_account:
            return "缺少达人的小红书账号，不能绑定佣金；请让达人补发主页链接，或在审批时填写"
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
