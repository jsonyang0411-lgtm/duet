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
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src.pii import mask_pii
from src.prm.platforms import load_catalog
from src.prm.store import PartyError

from .extract import asks_for_higher_commission, extract_xhs_account
from .store import InfluencerStore

logger = logging.getLogger(__name__)

SOURCE_PRM = "合作单页"
MAX_NAME = 30      # PRM 的称呼最多 30 个字
MAX_NOTE = 200

# 达人资料（档位、主播类型）多久按号码重查一次聚水潭；查失败的一小时后再试。每轮同步最多查一位，免得被限流
PROFILE_REFRESH_SECONDS = 24 * 3600
PROFILE_RETRY_SECONDS = 3600
PROFILE_FILE = "deals/{party_id}-达人资料.md"


def prm_workspace_dir() -> Optional[Path]:
    """起草用的业务资料目录（起草的 Claude 在这里读 kb/ 和 deals/ 案卷）：环境变量 PRM_WORKSPACE，
    或者这台 Mac 上 worker 的配置 ~/.claude-relay/config.json 的 prm_workspace。找不到返回 None。"""
    raw = os.getenv("PRM_WORKSPACE", "")
    if not raw:
        try:
            raw = json.loads(Path("~/.claude-relay/config.json").expanduser().read_text(encoding="utf-8")).get("prm_workspace", "")
        except (OSError, ValueError, AttributeError):
            raw = ""
    path = Path(raw).expanduser() if raw else None
    return path if path and path.is_dir() else None


def profile_text(contact: dict, now: float) -> str:
    """给起草的 Claude 看的达人资料：平台、号码、档位、主播类型。没有收件信息，只读。"""
    from .tiers import PLATFORM_LABELS

    platform = PLATFORM_LABELS.get(str(contact.get("platform") or ""), "")
    lines = ["# 达人资料（系统写的，只读：不要改这个文件）", ""]
    if not (platform and contact.get("platform_no")):
        lines.append("- 名单上还没有这位达人的平台和抖音号 / 小红书号：档位、主播类型都不知道，按 kb/合作政策.md 里\"案卷没写\"处理")
    else:
        lines.append(f"- 平台：{platform}；{platform}号：{contact['platform_no']}")
        if contact.get("platform_uid"):
            lines.append(f"- {platform} UID：{contact['platform_uid']}（开专属佣金、建寄样单时用）")
        if contact.get("tier"):
            lines.append(f"- 档位：{contact['tier']}（{contact.get('sales_label') or ''}）")
            lines.append(f"- 主播类型：{contact.get('channel') or '不知道'}")
            if contact.get("level") is not None:
                lines.append(f"- 带货等级：LV{contact['level']}")
        else:
            lines.append(f"- 档位、主播类型：还没查到（{contact.get('profile_note') or '还没查'}），按\"案卷没写\"处理")
    lines += ["", f"更新时间：{time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}", ""]
    return "\n".join(lines)


class PartyListSync:
    def __init__(
        self, store: InfluencerStore, prm, *,
        workspace: Optional[Path] = None, refresh: Optional[Callable] = None,
    ) -> None:
        from .tiers import refresh_contact

        self._store = store
        self._prm = prm
        self._lock = threading.RLock()
        # 写达人资料的业务资料目录；None 表示到用的时候再找（prm_workspace_dir），找不到就不写
        self._workspace = workspace
        self._refresh = refresh or refresh_contact
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
            self._refresh_profiles()
            return sorted(self._prm.store.active_wxids())

    def _refresh_profiles(self, now: Optional[float] = None) -> None:
        """达人资料：最多重查一位到期的达人（聚水潭会限流），再把资料文件写进业务资料目录。
        出错只记日志，不影响名单同步。"""
        now = time.time() if now is None else now
        try:
            due = [c for c in self._store.list_contacts() if c["enabled"] and c.get("platform_no") and self._due(c, now)]
            if due:
                self._refresh(self._store, due[0]["chat_id"], now=now)
            self._write_profiles(now)
        except Exception:
            logger.exception("达人资料：重查或写文件出错（不影响名单同步）")

    @staticmethod
    def _due(contact: dict, now: float) -> bool:
        checked = contact.get("profile_checked_at")
        if checked is None:
            return True
        wait = PROFILE_REFRESH_SECONDS if contact.get("tier") else PROFILE_RETRY_SECONDS
        return now - checked > wait

    def _write_profiles(self, now: float) -> None:
        workspace = self._workspace or prm_workspace_dir()
        if workspace is None:
            return
        parties = {p["wxid"]: p for p in self._prm.store.parties(include_inactive=False)}
        for contact in self._store.list_contacts():
            party = parties.get(contact["chat_id"])
            if party is None or not contact["enabled"]:
                continue
            path = workspace / PROFILE_FILE.format(party_id=int(party["id"]))
            text = profile_text(contact, now)
            try:
                old = path.read_text(encoding="utf-8")
            except OSError:
                old = ""
            # 只有内容变了才重写（更新时间那一行不算）
            if old.split("\n更新时间")[0] == text.split("\n更新时间")[0]:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

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
LIVE_KEYS = ("直播专场", "专场", "专场日期", "专场时间")
FIELD_LABELS = {"product": "商品", "recipient_name": "收件人", "recipient_phone": "手机", "shipping_address": "地址"}
READY_STAGE = "to_ship"          # 合作单到了「待寄出」：收件信息达人确认过了，可以生成审批单
SYSTEM = "system"


def _platform_account(contact: dict) -> tuple:
    """名单上这位达人的平台和号码，查聚水潭合作数据用。名单以后会记抖音号（platform / platform_no）；
    现在只有小红书账号时按小红书查；都没有就返回空号码，由 coop_summary 说明查不了。"""
    platform = str(contact.get("platform") or "").strip().upper()
    number = str(contact.get("platform_no") or "").strip()
    if platform and number:
        return platform, number
    xhs = str(contact.get("xhs_account") or "").strip()
    if xhs:
        return "XHS", xhs
    return "DY", ""


def split_items(text: str) -> List[dict]:
    """合作单上的商品（一段话，可能是好几款）拆成审批单的商品清单：按顿号、逗号、分号、换行分开，
    每款 1 件（D30）。颜色尺码就写在每一款的文字里，运营批准时核对；拆不开的就是一款。"""
    import re

    parts = [p.strip() for p in re.split(r"[、,，;；\n]+", text or "") if p.strip()]
    return [{"name": p, "qty": 1} for p in parts]


CATALOG_PLATFORMS = {"DY": "dy", "XHS": "xhs"}


def _code_tokens(text: str) -> List[str]:
    """文字里像编号的词：平台商品 ID、聚水潭商品编码（带尺码后缀的也算）。至少 8 位、带数字。"""
    import re

    return [t for t in re.findall(r"[0-9A-Za-z]+(?:-[0-9A-Za-z]+)*", text or "")
            if len(t.split("-")[0]) >= 8 and any(c.isdigit() for c in t)]


def match_listings(items: List[dict], platform: str, catalog: Optional[dict]) -> bool:
    """给每款商品对上这个平台的商品（开专属佣金按平台商品设）：文字里有平台商品 ID，或者有聚水潭商品编码
    （SKU 的，或者去掉尺码后缀的款），就到商品目录（worker --sync-products 写的 kb/products.json）里找。
    对上的加 listing {id, title}；写了编号却对不上的标 unlisted（这个平台可能没上架，运营决定换款还是不设
    专属佣金）；没写编号的不动，由运营自己对。返回有没有标上东西。"""
    part = ((catalog or {}).get("platforms") or {}).get(CATALOG_PLATFORMS.get(platform, ""))
    if not isinstance(part, dict) or not part.get("items"):
        return False
    keys = {}
    for entry in part["items"]:
        listing = {"id": str(entry.get("id") or ""), "title": str(entry.get("title") or "")}
        keys[listing["id"]] = listing
        for sku in entry.get("skus") or []:
            code = str(sku.get("code") or "")
            if code:
                keys[code] = keys[code.rsplit("-", 1)[0]] = listing
    marked = False
    for item in items:
        tokens = _code_tokens(item.get("name", ""))
        found = next((keys[t] for t in tokens if t in keys), None)
        if found:
            item["listing"] = found
        elif tokens:
            item["unlisted"] = True
        marked = marked or bool(tokens)
    return marked


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
    缺东西、ERP 里查不到这个商品时不生成，在合作单上写明原因（attention），由运营看着办。
    没有现货不拦（D35：ERP 里有就能寄）。"""

    def __init__(self, handler, prm, *, workspace=None) -> None:
        self._handler = handler
        self._prm = prm
        self._workspace = workspace     # 业务资料目录（读商品目录）；None 就按 prm_workspace_dir() 找
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
        """合作单上的信息齐了就生成审批单，返回审批单号；不齐、ERP 里查不到商品、已经有在等的单就返回 None。"""
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
            problem = asyncio.run(self._handler.check_product(draft))
            if problem:
                self._attention(deal, f"寄样审批单没有生成：{problem}", now)
                return None
            name = str(contact.get("display_name") or party["name"] or "")
            packet_id = asyncio.run(self._handler.submit_for_approval(
                chat_id, name, draft, self._packet_extra(chat_id, slots, now)))
            packet = self._handler.store.get_approval(packet_id)
        self._prm.store.add_event(deal["id"], party["id"], "sample_request",
                                  {"approval": packet.packet_id, "product": packet.requested_product},
                                  actor="system", now=now)
        logger.info("寄样：合作单 %d 生成了审批单 %s（%s）", deal["id"], packet.packet_id, packet.requested_product)
        return packet.packet_id

    def _packet_extra(self, chat_id: str, slots: dict, now: float) -> dict:
        """审批单上的平台、档位、主播类型（名单上的，超过一天没查就先重查一次）、商品清单、专场。"""
        from .tiers import refresh_contact

        store = self._handler.store
        contact = store.get_contact(chat_id) or {}
        checked = contact.get("profile_checked_at")
        if contact.get("platform_no") and (checked is None or now - checked > PROFILE_REFRESH_SECONDS):
            try:
                refresh_contact(store, chat_id, now=now)
            except Exception:
                logger.exception("寄样：重查达人资料出错，审批单上用名单里原来的")
            contact = store.get_contact(chat_id) or {}
        items = split_items(_first(slots, PRODUCT_KEYS))
        platform = str(contact.get("platform") or "")
        workspace = self._workspace or prm_workspace_dir()
        listed = bool(workspace) and match_listings(items, platform, load_catalog(workspace))
        return {
            "platform": platform,
            "platform_no": str(contact.get("platform_no") or ""),
            "tier": str(contact.get("tier") or ""),
            "channel": str(contact.get("channel") or ""),
            "sales_label": str(contact.get("sales_label") or contact.get("profile_note") or ""),
            "items_json": json.dumps(items, ensure_ascii=False) if len(items) > 1 or listed else "",
            "live_session": _first(slots, LIVE_KEYS),
        }

    def _commission_template(self, job: dict, deal: dict) -> None:
        """达人要求提高佣金：我们的固定话术作为系统消息等审核（D22），一段时间内只放一次。
        D37：系统不改佣金，只查他近 30 天在我们店的单量和成交额，写在审核理由里给运营看，由运营决定。"""
        from .handler import COMMISSION_REPLY
        from .jst_erp import coop_summary
        from .settings import HOLDING_REPLY_INTERVAL_SECONDS
        from .store import INBOX_COMMISSION

        text = str(job.get("text") or "")
        if not asks_for_higher_commission(text):
            return
        party = self._prm.store.party(deal["party_id"]) or {}
        chat_id = party.get("wxid", "")
        last = self._handler.store.last_auto_reply_at(chat_id, INBOX_COMMISSION)
        fresh = last is None or time.time() - last > HOLDING_REPLY_INTERVAL_SECONDS
        if not fresh:
            self._handler.store.add_inbox_item(chat_id, INBOX_COMMISSION, mask_pii(text)[:300], auto_replied=False)
            return
        platform, platform_no = _platform_account(self._handler.store.get_contact(chat_id) or {})
        coop = coop_summary(platform, platform_no)
        self._handler.store.add_inbox_item(
            chat_id, INBOX_COMMISSION, f"{mask_pii(text)[:300]}\n{coop['text']}", auto_replied=True,
        )
        self._system_packet(deal, "money", COMMISSION_REPLY, intent="达人要求提高佣金",
                            reason=f"达人要求提高佣金，按固定话术回复（D22，要人审）。{coop['text']}",
                            meta={"template": "commission", "coop": {k: coop[k] for k in coop if k != "text"}})

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
            # 运单号从聚水潭寄样单来（真实的要等发货后才有），不用运营再填；批准通知里已经带了单号，就不再另生成发货通知
            carrier = str(result.get("carrier") or "") or "快递"
            self._prm.store.add_shipment(deal["id"], carrier, waybill, now)
            self._prm.store.add_event(deal["id"], party["id"], "shipment", {"carrier": carrier, "waybill": waybill},
                                      actor="system", now=now)
            deals.set_stage(self._prm.store, deal["id"], "shipped", now=now, actor="system")
        notice = str(result.get("notice") or "")
        if notice:
            # 系统消息一定人审（_system_packet 不看自动放行）；批准通知不再提佣金（D34），和驳回一样算 other
            self._system_packet(self._prm.store.deal(deal["id"]), "other", notice,
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
