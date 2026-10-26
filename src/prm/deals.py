"""What the operators do to a deal in the console: stage, mode, shipment.

Plain functions on a ``PrmStore`` so the console can use them whether the bot runs
(through ``PrmService.deal_action``, which then wakes the sender) or not.  Every change
is a ``deal_events`` row (actor ``console``): the audit trail.

A waybill entered here makes a shipping notice draft at once, from a fixed template
with the system's carrier and waybill (the model never writes the number).  It waits
for review like any draft, unless shipping notices have earned ``auto`` (8.1); either
way the number in what is sent must be the deal's latest waybill (``gates.waybill_ok``).
The creator writing does not make it out of date; a newer waybill does.
"""

from __future__ import annotations

import re
from typing import Optional

from . import gates
from .store import APPROVED, COPILOT, HUMAN, MODES, PENDING, SHIPMENT_STATES, STAGES, PrmStore

_WAYBILL = re.compile(r"^[A-Za-z0-9-]{6,30}$")
# Stages before the sample arrived: going back to one means it has not been delivered (8.2 waits).
BEFORE_DELIVERY = ("new", "talk", "agreed", "hold", "address", "to_ship", "shipped", "transit", "ship_error")


class DealError(ValueError):
    """The change cannot be made (the message says why, in Chinese)."""


def _deal(store: PrmStore, deal_id) -> dict:
    try:
        deal = store.deal(int(deal_id))
    except (TypeError, ValueError):
        deal = None
    if deal is None:
        raise DealError("没有找到这张合作单")
    return deal


def set_stage(store: PrmStore, deal_id, stage: str, *, now: float, actor: str = "console") -> dict:
    deal = _deal(store, deal_id)
    if stage not in STAGES:
        raise DealError(f"不认识的阶段 {stage}")
    fields = {"stage": stage, "stage_proposal": ""}   # the AI's proposal was about the stage before
    if stage == "delivered" and not deal.get("delivered_at"):
        fields["delivered_at"] = now                  # 8.2 counts from here
    elif stage in BEFORE_DELIVERY and deal.get("delivered_at") and not deal.get("purged_at"):
        fields["delivered_at"] = None                 # set by mistake, or sent again: not delivered yet
    store.update_deal(deal["id"], now, **fields)
    store.add_event(deal["id"], deal["party_id"], "stage", {"from": deal["stage"], "to": stage},
                    actor=actor, now=now)
    return store.deal(deal["id"])


def adopt_proposal(store: PrmStore, deal_id, *, now: float, actor: str = "console") -> dict:
    deal = _deal(store, deal_id)
    if not deal["stage_proposal"]:
        raise DealError("AI 没有建议新的阶段")
    return set_stage(store, deal["id"], deal["stage_proposal"], now=now, actor=actor)


def set_mode(store: PrmStore, deal_id, mode: str, *, now: float, takeover_sec: float,
             actor: str = "console") -> dict:
    """copilot / human (for ``takeover_sec``, like an automatic takeover) / paused (until changed)."""
    deal = _deal(store, deal_id)
    if mode not in MODES:
        raise DealError(f"不认识的模式 {mode}")
    until = now + takeover_sec if mode == HUMAN else None
    store.update_deal(deal["id"], now, mode=mode, mode_until=until)
    store.add_event(deal["id"], deal["party_id"], "mode", {"from": deal["mode"], "to": mode, "until": until},
                    actor=actor, now=now)
    return store.deal(deal["id"])


def effective_mode(deal: dict, now: float) -> str:
    """The deal's mode now: a takeover whose time is up is copilot again."""
    mode = deal.get("mode") or COPILOT
    if mode == HUMAN and deal.get("mode_until") and now >= float(deal["mode_until"]):
        return COPILOT
    return mode if mode in MODES else COPILOT


def notice_text(name: str, carrier: str, waybill: str) -> str:
    return f"{name}你好～样品已经寄出啦：{carrier} {waybill}。签收之后跟我说一声哦～"


def add_shipment(store: PrmStore, deal_id, carrier: str, waybill: str, *, now: float,
                 actor: str = "console") -> tuple[dict, int]:
    """Record the waybill, move the deal to 已寄出, draft the shipping notice: ``(shipment, packet_id)``."""
    deal = _deal(store, deal_id)
    carrier, waybill = str(carrier or "").strip(), str(waybill or "").strip()
    if not carrier or len(carrier) > 20 or "\n" in carrier:
        raise DealError("快递公司要填，最多 20 个字")
    if not _WAYBILL.match(waybill):
        raise DealError("运单号只能是 6–30 位字母、数字或短横线")
    party = store.party(deal["party_id"]) or {}
    shipment = store.add_shipment(deal["id"], carrier, waybill, now)
    store.add_event(deal["id"], deal["party_id"], "shipment", {"carrier": carrier, "waybill": waybill},
                    actor=actor, now=now)
    set_stage(store, deal["id"], "shipped", now=now, actor=actor)
    text = notice_text(party.get("name", ""), carrier, waybill)
    route, status, decided_at = route_for(store, "shipping_notice", deal=store.deal(deal["id"]), now=now,
                                          text=text, waybill=waybill)
    packet_id = store.add_packet(
        deal_id=deal["id"], party_id=deal["party_id"], job_id=None, chat_id=party.get("wxid", ""),
        kind="shipping_notice", text=text, attachments=[], intent="通知达人样品已寄出",
        slots={"快递公司": carrier, "运单号": waybill}, stage_proposal="", needs_attention=False,
        reason="运营在控制台填了运单号，系统按模板生成", status=status, route=route, decided_at=decided_at,
        meta={"waybill": waybill, "carrier": carrier, "source": "system", "shipment_id": shipment["id"]}, now=now,
    )
    store.supersede(deal["id"], now, keep=packet_id, system_kind="shipping_notice")   # an earlier waybill's
    return shipment, packet_id


def set_shipment_status(store: PrmStore, deal_id, status: str, *, now: float, actor: str = "console") -> dict:
    deal = _deal(store, deal_id)
    if status not in SHIPMENT_STATES:
        raise DealError(f"不认识的物流状态 {status}")
    shipment = store.latest_shipment(deal["id"])
    if shipment is None:
        raise DealError("还没有填运单号")
    store.set_shipment_status(shipment["id"], status, now)
    store.add_event(deal["id"], deal["party_id"], "shipment_status", {"status": status, "waybill": shipment["waybill"]},
                    actor=actor, now=now)
    return set_stage(store, deal["id"], status, now=now, actor=actor)


def route_for(store: PrmStore, kind: str, *, deal: dict, now: float, text: str, needs_attention: bool = False,
              unknown_tokens: Optional[list] = None, reason: str = "", waybill: str = "",
              attachments: Optional[list] = None) -> tuple[str, str, Optional[float]]:
    """``(route, status, decided_at)`` of a new draft: ``auto`` / approved now when its kind has
    earned it and nothing about this draft needs a person; else ``review`` / pending."""
    gate = store.gate(kind)
    if gate is None or gate["locked"] or gate["route"] != gates.AUTO:
        return gates.REVIEW, PENDING, None
    why = gates.plain_reason(kind, needs_attention=needs_attention, mode=effective_mode(deal, now),
                             unknown_tokens=unknown_tokens or [], reason=reason, text=text, waybill=waybill,
                             attachments=attachments or [])
    if why:
        return gates.REVIEW, PENDING, None
    return gates.AUTO, APPROVED, now
