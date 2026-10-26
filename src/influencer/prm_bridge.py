"""达人模块和同事的 PRM 副驾驶（src/prm）之间的桥（Mac 端，WECHAT_BACKEND=edge + PRM_COPILOT=on）。

名单合一：两边各有一份名单，达人模块的 influencer_contacts（控制台「达人审批 → 名单」）和 PRM 的
parties（控制台「合作单」页登记）。以达人模块的名单为准，PRM 的登记跟着它：名单里启用的达人都登记成
PRM 的达人（起草、审核、接管都走 PRM），停用的取消登记。在合作单页登记或停用的，也同步回名单，
两个页面改哪个都行。

哪边改了怎么知道：PRM 那边每次登记、停用都会调 ``prm_changed``（接在 PrmService.on_parties_changed 上），
只把和上次看到的不一样的那几位写回名单；名单这边没有通知，``sync`` 每次都整份比一遍（edge 后端每拉
一轮事件就调一次，见 bot._wire_edge_prm）。
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List

from src.prm.store import PartyError

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
