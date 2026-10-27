"""达人模块和同事的 PRM 副驾驶（src/prm）之间的桥（Mac 端，WECHAT_BACKEND=edge + PRM_COPILOT=on）：
名单合一（PartyListSync），顺带把达人资料（档位、主播类型、平台 UID）写给起草的 Claude。
寄样单不在这里：由 Claude 起草 PRM 的平台动作 jst.sample_order，运营在「待审批」批准（2026-10-27 起）。

名单合一：两边各有一份名单，达人模块的 influencer_contacts（控制台「达人名单」页）和 PRM 的
parties（控制台「合作单」页登记）。以达人模块的名单为准，PRM 的登记跟着它：名单里启用的达人都登记成
PRM 的达人（起草、审核、接管都走 PRM），停用的取消登记。在合作单页登记或停用的，也同步回名单，
两个页面改哪个都行。

哪边改了怎么知道：PRM 那边每次登记、停用都会调 ``prm_changed``（接在 PrmService.on_parties_changed 上），
只把和上次看到的不一样的那几位写回名单；名单这边没有通知，``sync`` 每次都整份比一遍（edge 后端每拉
一轮事件就调一次，见 bot._wire_edge_prm）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from src.prm.store import PartyError

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

