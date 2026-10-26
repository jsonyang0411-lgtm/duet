"""达人模块和同事的 PRM 副驾驶（src/prm）之间的桥（Mac 端，WECHAT_BACKEND=edge + PRM_COPILOT=on）。
两件事：名单合一（PartyListSync），寄样（SampleBridge，见下面）。

名单合一：两边各有一份名单，达人模块的 influencer_contacts（控制台「达人审批 → 名单」）和 PRM 的
parties（控制台「合作单」页登记）。以达人模块的名单为准，PRM 的登记跟着它：名单里启用的达人都登记成
PRM 的达人（起草、审核、接管都走 PRM），停用的取消登记。在合作单页登记或停用的，也同步回名单，
两个页面改哪个都行。

哪边改了怎么知道：PRM 那边每次登记、停用都会调 ``prm_changed``（接在 PrmService.on_parties_changed 上），
只把和上次看到的不一样的那几位写回名单；名单这边没有通知，``sync`` 每次都整份比一遍（edge 后端每拉
一轮事件就调一次，见 bot._wire_edge_prm）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Dict, List, Optional

from src.pii import mask_pii
from src.prm.store import PartyError

from .extract import asks_for_higher_commission, extract_xhs_account
from .store import InfluencerStore

logger = logging.getLogger(__name__)

SOURCE_PRM = "合作单页"
MAX_NAME = 30      # PRM 的称呼最多 30 个字
MAX_NOTE = 200


class PartyListSync:
    def __init__(self, store: InfluencerStore, prm) -> None:
        self._store = store
        self._prm = prm
        self._lock = threading.RLock()
        # 上次同步后 PRM 每位达人的状态：wxid → 是否登记着。用来认出合作单页上的改动
        self._seen: Dict[str, bool] = {}
        self._refused: Dict[str, str] = {}

    def sync(self) -> List[str]:
        """先把合作单页上的改动写回名单，再让 PRM 跟上名单。返回 PRM 登记着的达人（推给 Windows 的名单）。"""
        with self._lock:
            self._from_prm()
            contacts = {c["chat_id"]: c for c in self._store.list_contacts()}
            parties = {p["wxid"]: p for p in self._prm.store.parties(include_inactive=True)}
            for chat_id, contact in contacts.items():
                party = parties.get(chat_id)
                active = bool(party and party["active"])
                if contact["enabled"] and not active:
                    self._register(contact)
                elif not contact["enabled"] and active:
                    self._prm.set_active(party["id"], False)
                    logger.info("名单合一：%s 在名单里停用了，PRM 取消登记", chat_id[:20])
            self._snapshot()
            return sorted(self._prm.store.active_wxids())

    def prm_changed(self) -> None:
        """合作单页登记或停用了达人（PrmService.on_parties_changed）。"""
        with self._lock:
            self._from_prm()
            self._snapshot()

    def _from_prm(self) -> None:
        for party in self._prm.store.parties(include_inactive=True):
            wxid, active = party["wxid"], bool(party["active"])
            if self._seen.get(wxid) == active:
                continue
            contact = self._store.get_contact(wxid)
            # 刚启动（还没看过这位）时两边对不上，以名单为准：只把名单里没有的加进来
            known = wxid in self._seen
            if active and (contact is None or (not contact["enabled"] and known)):
                self._store.save_contact(
                    wxid, source=SOURCE_PRM, enabled=True,
                    display_name=None if contact and contact["display_name"] else party["name"],
                    note=None if contact else party["note"],
                )
                logger.info("名单合一：合作单页登记了 %s，加进名单", wxid[:20])
            elif not active and contact is not None and contact["enabled"] and known:
                self._store.save_contact(wxid, source=SOURCE_PRM, enabled=False)
                logger.info("名单合一：合作单页停用了 %s，名单里也停用", wxid[:20])

    def _register(self, contact: dict) -> None:
        chat_id = contact["chat_id"]
        name = " ".join(str(contact["display_name"] or "").split())[:MAX_NAME] or chat_id[:MAX_NAME]
        try:
            party = self._prm.register(chat_id, name, str(contact["note"] or "")[:MAX_NOTE])
        except PartyError as exc:
            if self._refused.get(chat_id) != str(exc):
                self._refused[chat_id] = str(exc)
                logger.warning("名单合一：%s 登记不进 PRM（%s），这位达人的消息不会起草", chat_id[:20], exc)
            return
        self._refused.pop(chat_id, None)
        logger.info("名单合一：名单里的 %s 登记成 PRM 达人（编号 %d）", chat_id[:20], party["id"])

    def _snapshot(self) -> None:
        self._seen = {p["wxid"]: bool(p["active"]) for p in self._prm.store.parties(include_inactive=True)}


# ── 寄样：PRM 的合作单 → 我们的审批单 → 聚水潭、小红书 → 通知回到 PRM ──────────────

# 合作单里的已确认信息（Claude 填的 slots，键是中文）→ 审批单要的字段。按顺序取第一个有值的
PRODUCT_KEYS = ("商品", "产品", "样品", "SKU", "款式", "规格")
NAME_KEYS = ("收件人", "收件人姓名", "姓名")
PHONE_KEYS = ("手机", "电话", "手机号", "联系电话")
ADDRESS_KEYS = ("地址", "收件地址", "收货地址", "详细地址")
REGION_KEYS = ("省市区", "所在地区")
XHS_KEYS = ("小红书", "小红书主页", "小红书账号", "小红书号")
FIELD_LABELS = {"product": "商品", "recipient_name": "收件人", "recipient_phone": "手机", "shipping_address": "地址"}
READY_STAGE = "to_ship"          # 合作单到了「待寄出」：收件信息达人确认过了，可以生成审批单
SYSTEM = "system"


def _first(slots: dict, keys: tuple) -> str:
    for key in keys:
        value = str(slots.get(key) or "").strip()
        if value:
            return value
    return ""


class SampleBridge:
    """把 PRM 合作单上确认好的寄样信息变成我们的审批单（运营在「达人审批」页批准，R2），
    批准或驳回以后，给达人的通知作为系统消息放进 PRM 的待审批（**一定要人审**，D22），运单号进合作单。
    达人要求提高佣金时，我们的固定话术也作为系统消息放进待审批（D22）。

    什么时候生成审批单：Claude 的草稿建议合作单进「待寄出」，或者运营在合作单页把阶段改成「待寄出」。
    缺东西、没库存时不生成，在合作单上写明原因（attention），由运营看着办。"""

    def __init__(self, handler, prm) -> None:
        self._handler = handler
        self._prm = prm
        self._lock = threading.Lock()

    def attach(self) -> None:
        from .console import register_decision_hook
        self._prm.draft_hooks.append(self.on_draft)
        self._prm.stage_hooks.append(self.on_stage)
        register_decision_hook(self.on_decision)

    def detach(self) -> None:
        from .console import unregister_decision_hook
        for hooks, hook in ((self._prm.draft_hooks, self.on_draft), (self._prm.stage_hooks, self.on_stage)):
            if hook in hooks:
                hooks.remove(hook)
        unregister_decision_hook(self.on_decision)

    # ---- PRM 那边来的 ----

    def on_draft(self, job: dict, draft, deal: Optional[dict]) -> None:
        if deal is None:
            return
        self._commission_template(job, deal)
        if draft.stage_proposal == READY_STAGE:
            self.request_sample(deal)

    def on_stage(self, deal: dict) -> None:
        if deal.get("stage") == READY_STAGE:
            self.request_sample(deal)

    def request_sample(self, deal: dict) -> Optional[str]:
        """合作单上的信息齐了就生成审批单，返回审批单号；不齐、没货、已经有在等的单就返回 None。"""
        from src.prm import pii

        from .types import GateStatus, SampleRequestDraft

        party = self._prm.store.party(deal["party_id"])
        if party is None:
            return None
        chat_id = party["wxid"]
        now = time.time()
        with self._lock:
            waiting = [p for p in self._handler.store.list_approvals([GateStatus.PENDING, GateStatus.EXECUTING])
                       if p.influencer_id == chat_id]
            if waiting:
                logger.info("寄样：达人 %s 已经有在等的审批单 %s，不重复生成", chat_id[:20], waiting[0].packet_id)
                return None
            tokens = self._prm.store.tokens(party["id"])
            slots = {k: pii.unmask(str(v), tokens) for k, v in (deal.get("slots") or {}).items()}
            contact = self._handler.store.get_contact(chat_id) or {}
            address = _first(slots, ADDRESS_KEYS)
            region = _first(slots, REGION_KEYS)
            if region and address and not address.startswith(region):
                address = region + address
            draft = SampleRequestDraft(
                chat_id=chat_id,
                product=_first(slots, PRODUCT_KEYS),
                recipient_name=_first(slots, NAME_KEYS),
                recipient_phone=_first(slots, PHONE_KEYS),
                shipping_address=address,
                xhs_account=extract_xhs_account(_first(slots, XHS_KEYS)) or _first(slots, XHS_KEYS)
                or str(contact.get("xhs_account") or ""),
            )
            draft.evidence.append("来自合作单（PRM）上 Claude 整理、达人确认过的信息")
            missing = [label for name, label in FIELD_LABELS.items() if not getattr(draft, name)]
            if missing:
                self._attention(deal, f"要生成寄样审批单，合作单上还缺：{'、'.join(missing)}", now)
                return None
            follow_up = asyncio.run(self._handler._ask_for_what_is_missing(draft))
            if follow_up is not None:
                from .handler import HOLDING_REPLY
                why = ("聚水潭查不了库存（连不上或者还没接入），请人工处理" if follow_up == HOLDING_REPLY
                       else f"【{draft.product or _first(slots, PRODUCT_KEYS)}】聚水潭显示没有库存（已记进「待处理」）")
                self._attention(deal, f"寄样审批单没有生成：{why}", now)
                return None
            name = str(contact.get("display_name") or party["name"] or "")
            asyncio.run(self._handler._submit_for_approval(chat_id, name, draft))
            packet = next((p for p in self._handler.store.list_approvals([GateStatus.PENDING])
                           if p.influencer_id == chat_id), None)
        if packet is None:
            return None
        self._prm.store.add_event(deal["id"], party["id"], "sample_request",
                                  {"approval": packet.packet_id, "product": packet.requested_product},
                                  actor="system", now=now)
        logger.info("寄样：合作单 %d 生成了审批单 %s（%s）", deal["id"], packet.packet_id, packet.requested_product)
        return packet.packet_id

    def _commission_template(self, job: dict, deal: dict) -> None:
        """达人要求提高佣金：我们的固定话术作为系统消息等审核（D22），一段时间内只放一次。"""
        from .handler import COMMISSION_REPLY
        from .settings import HOLDING_REPLY_INTERVAL_SECONDS
        from .store import INBOX_COMMISSION

        text = str(job.get("text") or "")
        if not asks_for_higher_commission(text):
            return
        party = self._prm.store.party(deal["party_id"]) or {}
        chat_id = party.get("wxid", "")
        last = self._handler.store.last_auto_reply_at(chat_id, INBOX_COMMISSION)
        fresh = last is None or time.time() - last > HOLDING_REPLY_INTERVAL_SECONDS
        self._handler.store.add_inbox_item(chat_id, INBOX_COMMISSION, mask_pii(text)[:300], auto_replied=fresh)
        if fresh:
            self._system_packet(deal, "money", COMMISSION_REPLY, intent="达人要求提高佣金",
                                reason="达人要求提高佣金，按固定话术回复（D22，要人审）", meta={"template": "commission"})

    # ---- 达人审批页来的（console.register_decision_hook） ----

    def on_decision(self, packet, approved: bool, result: dict) -> None:
        from src.prm import deals

        party = self._prm.store.party_by_wxid(packet.influencer_id)
        if party is None:
            return
        now = time.time()
        deal = self._prm.store.deal_of(party["id"], now)
        self._prm.store.add_event(deal["id"], party["id"], "sample_decision",
                                  {"approval": packet.packet_id, "approved": approved,
                                   "order_sn": str(result.get("order_sn") or "")}, actor="console", now=now)
        waybill = str(result.get("tracking_number") or "")
        if approved and waybill:
            # 运单号从聚水潭出库单来，不用运营再填；批准通知里已经带了单号，就不再另生成发货通知
            carrier = str(result.get("carrier") or "") or "快递"
            self._prm.store.add_shipment(deal["id"], carrier, waybill, now)
            self._prm.store.add_event(deal["id"], party["id"], "shipment", {"carrier": carrier, "waybill": waybill},
                                      actor="system", now=now)
            deals.set_stage(self._prm.store, deal["id"], "shipped", now=now, actor="system")
        notice = str(result.get("notice") or "")
        if notice:
            # 批准通知提到专属佣金，和钱有关（money 类，永远人审）；驳回的婉拒也要人审
            self._system_packet(self._prm.store.deal(deal["id"]), "money" if approved else "other", notice,
                                intent="寄样审批通过，通知达人" if approved else "寄样审批没通过，婉拒达人",
                                reason=f"审批单 {packet.packet_id} {'已批准执行' if approved else '已驳回'}，系统按模板生成",
                                meta={"approval": packet.packet_id, "waybill": waybill} if waybill
                                else {"approval": packet.packet_id})

    # ---- 内部 ----

    def _system_packet(self, deal: dict, kind: str, text: str, *, intent: str, reason: str, meta: dict) -> int:
        """系统生成的消息：一定是待审核（不看自动放行），达人再发消息也不作废。"""
        from src.prm import pii

        party = self._prm.store.party(deal["party_id"]) or {}
        now = time.time()
        masked = pii.mask(text, lambda k, label, value: self._prm.store.token_for(party["id"], k, label, value, now))
        packet_id = self._prm.store.add_packet(
            deal_id=deal["id"], party_id=deal["party_id"], job_id=None, chat_id=party.get("wxid", ""), kind=kind,
            text=masked, attachments=[], intent=intent, slots={}, stage_proposal="", needs_attention=False,
            reason=reason, status="pending", route="review", meta={**meta, "source": SYSTEM}, now=now,
        )
        logger.info("寄样：合作单 %d 有一条系统消息等审核（草稿 %d，%s）", deal["id"], packet_id, kind)
        return packet_id

    def _attention(self, deal: dict, text: str, now: float) -> None:
        self._prm.store.update_deal(deal["id"], now, attention=text[:300])
        logger.info("寄样：合作单 %d：%s", deal["id"], text)
