"""选款网站 on the bot's side (PRM_SAMPLE_SITE_MIN, default off; 2026-11-13).

The site (``sample_site/`` of this repository, deployed by the boss on a public server) lets a creator pick her sample
hats herself and type the receiver.  The bot never talks to the site: the Mac does (``tools/claude_relay/
sample_site.py``; the site cannot reach the Mac, which is behind NAT), asked by relay jobs like every other Mac job.

- 「选款链接 <达人>」 (合作单指令) / the deal action ``site_link``: one draft job with ``meta.site_link``; the Mac makes
  her link on the site and answers with a fixed sentence carrying it (no model).  The draft always waits for a person
  (``LINK_NOTE``), like 「补发货盘」.  Only into her private chat: the link is hers alone, never posted in a group.
- every PRM_SAMPLE_SITE_MIN minutes one job (``meta.op = PULL_OP``) has the Mac take the new submissions (the site
  locks them: she cannot change them any more) and tell the site which ones the bot has stored (``meta.acks``: the
  site then keeps only masked remnants of their receivers).  A submission not acked comes again every round, so none
  is lost; the bot knows each by the site's id (``site_orders``), so none is taken twice.
- each submission becomes one job (``meta.op = ORDER_OP``, her codes, the receiver with the phone as 〈手机N〉, her
  抖音号); the Mac answers with a previewed ``jst.sample_order`` and a 选款单 for the operators, and the bot keeps it as
  a draft of its own in 待审批 (no job of hers: nothing she writes puts it out of date).  **No photo check**
  (``picked``): she picked our own codes in our own catalogue, there is no screenshot to tell a hat from, so
  PRM_DRAFT_CONTEXT's 对图 does not apply to these orders (approving, 重新预览, the Mac's gate all skip it).
  批准并执行 makes the order and audits it (the Mac's plan ``("create", "audit")``); PRM_SAMPLE_SITE_AUTO=on approves a
  ready one by itself.
- a general link's submission (the boss posted the link; she typed her 抖音号) goes to the registered creator known by
  that 抖音号 (the 达人名单, her links, her verified sample orders); nobody: it waits on the site, the admin is told
  once, and every round looks again (register her / link her 抖音号 and it goes on by itself).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Optional

from ..relay.store import PRM
from . import draft_context, gates, platforms
from .result import DraftError, parse_draft
from .store import APPROVED, COPILOT, LANE_BUYIN, LANE_SYSTEM, PENDING, SENDING

logger = logging.getLogger(__name__)

PULL_OP = "site_pull"                   # the round's job
ORDER_OP = "site_order"                 # one submission's job (an op: never taken for a draft job of her chat)
SYNC_CHAT = "prm:sample-site"           # the round's chat id: no WeChat chat, no creator, never a draft
LINK = "site_link"                      # a link draft job's meta, its packet's tag, the deal's event
ORDER = "site_order"                    # an order job's meta, its packet's tag (``picked``), the deal's event
FIRST_SEC = 60                          # a bot that never asked: its first round this long after its start
TRIES = 3                               # an order that could not be drafted: tried in this many rounds in a row ...
RETRY_SEC = 3600                        # ... then once in this long
MAX_HATS = 10
LINK_NOTE = "运营要的「选款链接」：这份是我们主动发的，要人看过再发"
OFF_TEXT = "选款网站没打开（设置里 PRM_SAMPLE_SITE_MIN 是 0）"
GROUP_TEXT = "她现在只能在群里联系：选款链接是她一个人的，不往群里发（私聊能发时再点；要发群里用老板的通用链接）"
NO_PHOTO_CHECK = "她在选款网站上自己点的款，编码是我们目录里的：不用对图"
OLD_WORKER = "Mac 的 worker 还是旧版，不认识选款网站的任务：更新代码、重启 Mac 的 worker"
NO_LINK = "选款链接的草稿里没有链接（Mac 的 worker 还是旧版？更新、重启以后再点一次）"
_LINK = re.compile(r"https?://[^\s/]+(?:/[^\s]*)?/[sg]/[A-Za-z0-9_-]{20,}")
_REF = re.compile(r"^p(\d{1,12})$")
_ACCOUNT = re.compile(r"^[0-9A-Za-z_.][0-9A-Za-z_.-]{1,39}$")
_CODE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{2,63}$")
_KEY = re.compile(r"^[0-9a-f]{32}$")
RECEIVER_LIMITS = (("name", 30), ("phone", 20), ("province", 20), ("city", 20), ("district", 20), ("address", 120))


def picked(packet: Optional[dict]) -> bool:
    """Is this draft a 选款网站 order (the creator picked our own codes herself: no photo check)?"""
    return isinstance(((packet or {}).get("meta") or {}).get(ORDER), dict)


def has_link(text) -> bool:
    """Does this draft carry a link of the site (``…/s/<token>``)?  An older Mac worker does not know ``meta.site_link``
    and lets the model write something: such a draft is none (``PrmService.on_result``)."""
    return bool(_LINK.search(str(text or "")))


def ref_party(ref) -> Optional[int]:
    """The party a link's ``ref`` names (``p<party id>``, the Mac's ``party_ref``), or None."""
    m = _REF.match(str(ref or ""))
    return int(m.group(1)) if m else None


def _line(value, limit: int) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", value if isinstance(value, str) else "").strip()[:limit]


def check_pull(value) -> dict:
    """The Mac's answer to a round, checked field by field (``ValueError`` when it is not one): ``{"acked": [ids],
    "submissions": [{"id", "ref", "kind", "account", "items": [{"sku", "name", "colour", "size", "image"}],
    "receiver": {…}, "purged"}], "stopped": None | {"verdict", "message"}}``."""
    if not isinstance(value, dict) or value.get("op") != PULL_OP:
        raise ValueError("not a site_pull answer")
    stopped = None
    if isinstance(value.get("stopped"), dict):
        stopped = {"verdict": _line(value["stopped"].get("verdict"), 30) or "error",
                   "message": _line(value["stopped"].get("message"), 300)}
    acked = [i for i in value.get("acked") or [] if isinstance(i, int) and not isinstance(i, bool) and i > 0][:1000]
    out = []
    for raw in (value.get("submissions") if isinstance(value.get("submissions"), list) else [])[:500]:
        if not isinstance(raw, dict) or isinstance(raw.get("id"), bool) or not isinstance(raw.get("id"), int) \
                or raw["id"] <= 0:
            continue
        items = []
        for item in (raw.get("items") if isinstance(raw.get("items"), list) else [])[:MAX_HATS]:
            if isinstance(item, dict) and _CODE.match(str(item.get("sku") or "")):
                image = str(item.get("image") or "")
                items.append({"sku": str(item["sku"]), "name": _line(item.get("name"), 60),
                              "colour": _line(item.get("colour"), 60), "size": _line(item.get("size"), 60),
                              "image": image if _KEY.match(image) else ""})
        receiver = raw.get("receiver") if isinstance(raw.get("receiver"), dict) else {}
        account = str(raw.get("account") or "").strip()
        out.append({"id": raw["id"], "ref": _line(raw.get("ref"), 64),
                    "kind": "general" if raw.get("kind") == "general" else "personal",
                    "account": account if _ACCOUNT.match(account) else "", "items": items,
                    "receiver": {key: _line(receiver.get(key), limit) for key, limit in RECEIVER_LIMITS},
                    "purged": raw.get("purged") is True})
    return {"acked": acked, "submissions": out, "stopped": stopped}


class SampleSite:
    """The 选款网站's links, rounds and orders (see the module's doc).  ``service``: the PrmService (its store, relay,
    clock, where a creator is reached, the approval of a platform draft)."""

    def __init__(self, service, config=None):
        self._svc = service
        try:
            self._sec = max(0, int(getattr(config, "prm_sample_site_min", 0) or 0)) * 60
        except (TypeError, ValueError):
            self._sec = 0
        self._auto = getattr(config, "prm_sample_site_auto", "off") == "on"
        self._lock = threading.RLock()          # one round queued or applied at a time

    @property
    def enabled(self) -> bool:
        return self._sec > 0

    def status(self) -> dict:
        """For the console's snapshot: the switch, how the last round went, the submissions nobody's yet."""
        store = self._svc.store
        try:
            last = json.loads(store.meta_value("site_status") or "null")
        except ValueError:
            last = None
        return {"min": self._sec // 60, "auto": self._auto, "last": last if isinstance(last, dict) else None,
                "unmatched": [{"id": r["id"], "account": r["account"], "hats": r["hats"], "at": r["created_at"]}
                              for r in store.site_orders("unmatched", limit=20)],
                "failed": [{"id": r["id"], "party_id": r["party_id"], "why": r["why"], "tries": r["tries"]}
                           for r in store.site_orders("failed", limit=20)]}

    # ── 「选款链接」 ─────────────────────────────────────────────────────────

    def ask_link(self, deal_id, most=None, actor: str = "console") -> dict:
        """One draft job that makes her link on the site and sends it in a fixed sentence (``meta.site_link``:
        ``{"by", "max"?, "ask_account"}``; ``most``: how many hats she may pick, 1–10, None: the Mac's default).
        ``{"ok": True, "job"}``, or ``{"ok": False, "error"}`` saying why not."""
        svc = self._svc
        if not self.enabled:
            return {"ok": False, "error": OFF_TEXT}
        if most is not None and (isinstance(most, bool) or not isinstance(most, int) or not (1 <= most <= MAX_HATS)):
            return {"ok": False, "error": f"最多选几顶要是 1–{MAX_HATS} 的整数"}
        try:
            deal = svc.store.deal(int(deal_id))
        except (TypeError, ValueError):
            deal = None
        party = svc.store.party(deal["party_id"]) if deal else None
        if deal is None or party is None:
            return {"ok": False, "error": "没有找到这个合作单"}
        with svc._handle_lock:
            now = svc._clock()
            deal = svc.store.deal_of(party["id"], now)
            chat, group, _ = svc._reaching(party, made=True, deal=deal)
            why = self._no_link(party, deal, now, chat, group)
            if why:
                return {"ok": False, "error": why}
            asked = {"by": actor, "ask_account": len(self.accounts(party, deal)) != 1}
            if most is not None:
                asked["max"] = most
            meta = {"party_id": party["id"], "deal_id": deal["id"], "name": party["name"], LINK: asked}
            _, job_id = svc._relay.submit(chat, chat, "", kind=PRM, meta=meta, fresh=True)
            svc.store.add_event(deal["id"], party["id"], LINK, {"job": job_id, **({"max": most} if most else {})},
                                actor=actor, now=now)
        logger.info("PRM: party %d: job #%d makes her 选款链接 (%s)", party["id"], job_id, actor)
        return {"ok": True, "job": job_id}

    def _no_link(self, party: dict, deal: dict, now: float, chat, group) -> str:
        """Why no 选款链接 is drafted for her now ("" = it is): as ``PrmService._no_huopan`` (off the list, a person has
        the chat, she cannot be reached, a draft of hers is being written or waits), and never into a group."""
        svc = self._svc
        from .service import UNREACHABLE_TEXT

        if not party.get("active"):
            return "这位达人已经停用（不在达人名单里），不发选款链接"
        if svc._mode(deal, now) != COPILOT:
            return "现在不在副驾驶模式（人工接管或暂停），不发选款链接"
        if chat is None:
            return f"{UNREACHABLE_TEXT}，选款链接发不出去"
        if group is not None:
            return GROUP_TEXT
        if svc._relay.store.latest_open(chat or party["wxid"], drafts_only=True, system=False) is not None:
            return "她的草稿正在写，写好会出现在「待审批」里"
        if any((p.get("meta") or {}).get("lane") not in (LANE_SYSTEM, LANE_BUYIN) and p.get("job_id") is not None
               for p in svc.store.deal_packets(deal["id"], statuses=(PENDING, APPROVED, SENDING))):
            return "「待审批」里已经有她的草稿：先处理那一份"
        return ""

    def accounts(self, party: dict, deal: Optional[dict] = None) -> list:
        """Her 抖音号 as the bot knows them, each once (any case), as written: the 达人名单's, her links', the ones her
        verified 抖店 sample orders found.  Exactly one: the link need not ask her, and her order is made for it."""
        svc, found = self._svc, []

        def add(value) -> None:
            value = str(value or "").strip()
            if _ACCOUNT.match(value) and value.lower() not in {f.lower() for f in found}:
                found.append(value)

        if svc.creator_directory is not None:
            try:
                for row in svc.creator_directory() or []:
                    if (str((row or {}).get("wxid") or "") == party["wxid"]
                            and str(row.get("platform") or "").strip().upper() != "XHS"):
                        add(row.get("platform_no"))
            except Exception:
                logger.exception("PRM: the creator list could not be read for a 选款链接")
        for link in svc.store.creator_links(party["id"]):
            add(link.get("account"))
        deal = deal or svc.store.deal_of(party["id"], svc._clock())
        for douyin in (svc._creator_ids(deal["id"]) or {}).get("douyin_ids") or []:
            add(douyin)
        return found

    # ── the rounds ──────────────────────────────────────────────────────────

    def _due(self, now: float) -> float:
        svc = self._svc
        try:
            last = float(svc.store.meta_value("site_sync_at"))
        except (TypeError, ValueError):
            return svc._started + FIRST_SEC
        return min(last, now) + self._sec

    def sweep(self) -> Optional[int]:
        """Once an interval, one job asks the Mac for the site's new submissions and carries the acks owed
        (``meta.acks``).  One job at a time; when the last round was is kept in the database.  Returns the job id when
        one was queued.  Errors are logged (the sender thread goes on)."""
        if not self.enabled:
            return None
        svc = self._svc
        now = svc._clock()
        try:
            with self._lock:
                if now < self._due(now) or svc._relay.store.latest_open(SYNC_CHAT) is not None:
                    return None
                svc.store.set_meta_value("site_sync_at", now)
                acks = sorted(r["id"] for r in svc.store.site_orders(to_ack=True, limit=500))
                _, job_id = svc._relay.submit(SYNC_CHAT, SYNC_CHAT, "取选款网站的新提交", kind=PRM,
                                              meta={"op": PULL_OP, "acks": acks}, fresh=True, input_closed=True)
        except Exception:
            logger.exception("PRM: the 选款网站's submissions could not be asked for")
            return None
        logger.info("PRM: job #%d asks the Mac for the 选款网站's new submissions (%d ack(s) with it)", job_id, len(acks))
        return job_id

    def on_pull(self, job: dict, meta: dict) -> bool:
        """The Mac's answer to a round: the acks that got there, each submission (``_take``), how the round went
        (``site_status``).  Whatever the status, the job then keeps only a summary of its answer (the receivers were
        in it).  A job lost, cancelled or failed waits for the next round: the site has everything still."""
        svc = self._svc
        status = job["status"]
        if status in ("pending", "running"):
            return True
        now = svc._clock()
        result, stopped = None, None
        if status == "done":
            try:
                result = check_pull(json.loads(job.get("result_text") or ""))
                stopped = result["stopped"]
            except ValueError:
                stopped = {"verdict": "error", "message": "Mac 交回的选款网站的答复读不出来"}
        else:
            from .service import _job_error
            why = {"lost": "Mac 失联了", "cancelled": "任务被取消了"}.get(status) or _job_error(job)[:300]
            old = "达人编号" in why                       # (an older worker takes the round for a creator's job)
            stopped = {"verdict": "old_worker" if old else "error", "message": OLD_WORKER if old else why}
        summary = {"acked": 0, "submissions": 0, "orders": 0, "unmatched": 0, "failed": 0, "waiting": 0}
        with self._lock:
            if result is not None:
                summary["acked"] = svc.store.mark_site_acked(result["acked"], now)
                for sub in result["submissions"]:
                    summary["submissions"] += 1
                    try:
                        self._take(sub, now, summary)
                    except Exception:
                        summary["failed"] += 1
                        logger.exception("PRM: a 选款网站 submission (#%d) could not be taken in; read again next round",
                                         sub["id"])
            svc.store.set_meta_value("site_status", json.dumps({"at": now, "stopped": stopped, **summary},
                                                                ensure_ascii=False))
        svc._relay.store.replace_result(job["id"], json.dumps({"op": PULL_OP, "summary": summary, "stopped": stopped},
                                                              ensure_ascii=False))
        if stopped:
            logger.warning("PRM: 选款网站 round #%d did not go through (%s): %s", job["id"], stopped["verdict"],
                           stopped["message"])
        else:
            logger.info("PRM: 选款网站 round #%d: %d submission(s) there, %d order job(s), %d of nobody on the list, "
                        "%d failed, %d ack(s) taken", job["id"], summary["submissions"], summary["orders"],
                        summary["unmatched"], summary["failed"], summary["acked"])
        return True

    def _party_of(self, sub: dict) -> Optional[dict]:
        """Whose submission: the link's party (``ref``), else the one registered creator known by the 抖音号 she typed
        (a general link); None: nobody, or more than one (never a guess)."""
        svc = self._svc
        party_id = ref_party(sub["ref"])
        if party_id is not None:
            party = svc.store.party(party_id)
            return party if party is not None and party.get("active") else None
        if not sub["account"]:
            return None
        return svc._buyin_party({"aweme_id": sub["account"]}, svc._buyin_index())

    def _take(self, sub: dict, now: float, summary: dict) -> None:
        """One submission of a round: known and drafted (its ack is owed or on its way): nothing; its job still with
        the Mac: nothing; else its order's job is queued (again: a failed one TRIES rounds in a row, then once in
        RETRY_SEC).  One of nobody on the list is kept ``unmatched`` and looked at again every round."""
        svc, store = self._svc, self._svc.store
        row = store.site_order(sub["id"])
        if row is not None and row["state"] in ("drafted", "dropped"):
            return
        if row is not None and row["state"] == "queued":
            job = svc._relay.store.get(int(row["job_id"])) if row["job_id"] else None
            if job is not None and job["status"] in ("pending", "running"):
                summary["waiting"] += 1
                return
        if row is not None and row["state"] == "failed" and row["tries"] >= TRIES \
                and now - float(row["updated_at"]) < RETRY_SEC:
            summary["failed"] += 1
            return
        hats = len(sub["items"])
        party = self._party_of(sub)
        if party is None:
            told = bool(row and row["told"])
            store.save_site_order(sub["id"], now, ref=sub["ref"], kind=sub["kind"], account=sub["account"],
                                  state="unmatched", hats=hats)
            summary["unmatched"] += 1
            if not told:
                who = f"抖音号 {sub['account']}" if sub["account"] else "一位达人"
                sent = svc._tell_admin(f"选款网站：{who} 提交了选款（{hats} 顶），名单里没找到她。把她登记进达人名单"
                                       "（或者在她的合作单里关联这个抖音号）以后，机器人会自己接着起草寄样单。")
                store.save_site_order(sub["id"], now, told=1 if sent is not False else 0)
                logger.info("PRM: 选款网站 submission #%d is of nobody on the list (told the admin: %s)", sub["id"], sent)
            return
        deal = store.deal_of(party["id"], now)
        tries = int(row["tries"]) if row is not None and row["state"] in ("failed", "queued") else 0
        why = ""
        account = sub["account"]
        if not account:
            known = self.accounts(party, deal)
            account = known[0] if len(known) == 1 else ""
            if not account:
                why = "不知道她的抖音号（名单里没有，或者不止一个）：在达人名单里补上，机器人会再试"
        if sub["purged"]:
            why = "网站上的收货信息已经过了保留期、清掉了：请她重新提交，或者手动建单"
        elif not hats:
            why = "这份提交里没有选款"
        if why:
            self._failed(sub["id"], party, deal, why, now, tries=TRIES if sub["purged"] or not hats else tries + 1,
                         dropped=sub["purged"] or not hats, ref=sub["ref"], kind=sub["kind"], account=sub["account"],
                         hats=hats)
            summary["failed"] += 1
            return
        # (the phone as her 〈手机N〉: the Mac's preview goes without it, the number goes in only when the approved
        # action is executed, ``action_for_mac``; the clean-up cuts a job's ``receiver`` like any sample order's)
        masked = {k: svc._mask(party["id"], v, now) if v else v for k, v in sub["receiver"].items()}
        order = {"id": sub["id"], "account": account, "items": sub["items"], "receiver": masked}
        meta = {"op": ORDER_OP, "party_id": party["id"], "deal_id": deal["id"], "name": party["name"], ORDER: order}
        creator = svc._creator_ids(deal["id"])
        if creator:
            meta["creator"] = creator
        chat = party["wxid"]
        _, job_id = svc._relay.submit(chat, chat, "", kind=PRM, meta=meta, fresh=True, input_closed=True)
        store.save_site_order(sub["id"], now, ref=sub["ref"], kind=sub["kind"], account=sub["account"],
                              party_id=party["id"], deal_id=deal["id"], state="queued", job_id=job_id, tries=tries,
                              hats=hats, why="")
        summary["orders"] += 1
        logger.info("PRM: 选款网站 submission #%d (party %d, %d hat(s)): job #%d drafts its sample order", sub["id"],
                    party["id"], hats, job_id)

    def _failed(self, site_id: int, party: dict, deal: dict, why: str, now: float, *, tries: int, dropped: bool = False,
                **fields) -> None:
        """A submission's order was not drafted: the deal says why (one notice a submission); it is tried again in
        later rounds (``_take``), or (``dropped``: nothing to try again with) given up and acked."""
        store = self._svc.store
        store.save_site_order(site_id, now, party_id=party["id"], deal_id=deal["id"],
                              state="dropped" if dropped else "failed", tries=tries, why=why[:300], **fields)
        store.set_notice(deal["id"], f"site:{site_id}", f"选款网站：她提交的选款（#{site_id}）没起草成寄样单：{why}"[:300],
                         now)
        logger.warning("PRM: 选款网站 submission #%d (party %d): no order drafted (%s)", site_id, party["id"],
                       "given up" if dropped else f"try {tries}")

    # ── one submission's order ──────────────────────────────────────────────

    def on_order(self, job: dict, meta: dict, deal: dict, now: float) -> bool:
        """The Mac's answer to an order job: its previewed ``jst.sample_order`` becomes a draft of the bot's own in
        待审批 (no job of hers, so her next message does not put it out of date; ``meta.site_order``: no photo check),
        with the 选款单 for the operators; the submission is ``drafted`` (acked in the next round).  An answer without
        a usable action (the Mac's map does not know a code, say) fails the submission: tried again later.
        PRM_SAMPLE_SITE_AUTO: a ready one is approved at once (``_approve_execute``: every check of a person's
        approval but the photo ones)."""
        svc, store = self._svc, self._svc.store
        status = job["status"]
        if status in ("pending", "running"):
            return True
        order = meta.get(ORDER) if isinstance(meta.get(ORDER), dict) else {}
        try:
            site_id = int(order.get("id"))
        except (TypeError, ValueError):
            return True
        row = store.site_order(site_id)
        if row is None or row["state"] != "queued" or int(row["job_id"] or 0) != int(job["id"]):
            self._drop_sheets(job["id"])
            return True                                  # (an earlier try's answer, or one handled already)
        party = store.party(int(meta["party_id"]))
        tries = int(row["tries"]) + 1
        if status != "done":
            from .service import _job_error
            why = {"lost": "Mac 失联了", "cancelled": "任务被取消了"}.get(status) or _job_error(job)
            if status != "lost":
                self._drop_sheets(job["id"])
            self._failed(site_id, party, deal, why, now, tries=tries)
            return True
        try:
            draft = svc._mask_draft(party["id"], parse_draft(job.get("result_text") or ""), now)
        except DraftError as e:
            self._drop_sheets(job["id"])
            self._failed(site_id, party, deal, f"Mac 交回的草稿格式不对（{e.code}）", now, tries=tries)
            return True
        block = draft.platform
        action = (block or {}).get("action")
        if not action or action.get("action") != "jst.sample_order":
            why = draft.platform_refused or "；".join((block or {}).get("problems") or []) or "Mac 没给出寄样单"
            self._drop_sheets(job["id"])
            self._failed(site_id, party, deal, why, now, tries=tries)
            return True
        _, sheets, extra = draft_context.take_operator_files(
            svc._relay.store.operator_files(job["id"]), draft.operator_files, [])
        draft_context.delete_files(extra)
        ready = block.get("verdict") == "ready"
        why = "" if ready else ("；".join(block.get("problems") or [])
                                or platforms.VERDICTS.get(block.get("verdict"), ""))[:300]
        packet_meta = {ORDER: {"id": site_id, "hats": int(row["hats"] or 0), "photo_check": False}}
        if sheets:
            packet_meta[draft_context.OPERATOR_FILES] = sheets
        packet_id = store.add_packet(
            deal_id=deal["id"], party_id=party["id"], job_id=None, chat_id=party["wxid"], kind="other",
            text=draft.reply_draft, attachments=[], intent=draft.intent, slots={}, stage_proposal="",
            needs_attention=not ready, reason=(f"平台动作预览没通过：{why}" if not ready else NO_PHOTO_CHECK)[:500],
            status=PENDING, route=gates.REVIEW, meta=packet_meta, tool="jst.sample_order",
            platform={"action": action, "preview": block}, now=now)
        store.save_site_order(site_id, now, state="drafted", packet_id=packet_id, tries=tries, why="")
        store.add_event(deal["id"], party["id"], ORDER, {"packet": packet_id, "site": site_id,
                                                         "hats": int(row["hats"] or 0)}, actor="mac", now=now)
        store.set_notice(deal["id"], f"site:{site_id}",
                         (f"选款网站：她提交的选款（#{site_id}）的寄样单预览没通过：{why}" if not ready else ""), now)
        logger.info("PRM: 选款网站 submission #%d -> packet %d (%s; not checked by photo: she picked our codes)",
                    site_id, packet_id, block.get("verdict"))
        if self._auto and ready:
            self._approve_by_itself(packet_id, deal, party, now)
        return True

    def _approve_by_itself(self, packet_id: int, deal: dict, party: dict, now: float) -> None:
        """PRM_SAMPLE_SITE_AUTO: 批准并执行 without a person, through the same door (``_approve_execute``).  Refused
        (another sample order of the deal under way, the bot not connected to WeChat …): the draft waits for a person
        and the deal says why."""
        svc, store = self._svc, self._svc.store
        try:
            answer = svc._approve_execute(store.packet(packet_id), None, now)
        except Exception:
            logger.exception("PRM: packet %d (选款网站) could not be approved by itself", packet_id)
            answer = {"ok": False, "error": "机器人出错了"}
        if answer.get("ok"):
            store.add_event(deal["id"], party["id"], "site_auto", {"packet": packet_id, "job": answer.get("job")},
                            actor="system", now=now)
            logger.info("PRM: packet %d (选款网站) approved by itself (PRM_SAMPLE_SITE_AUTO); job #%s", packet_id,
                        answer.get("job"))
        else:
            store.set_notice(deal["id"], f"site-auto:{packet_id}",
                             f"选款网站的寄样单（草稿 #{packet_id}）没能自动建：{answer.get('error')}；在「待审批」里处理"[:300],
                             now)
            logger.warning("PRM: packet %d (选款网站) not approved by itself: %s", packet_id, answer.get("error"))

    def _drop_sheets(self, job_id: int) -> None:
        """An order job that gives no draft keeps no 选款单."""
        try:
            draft_context.delete_files([f["path"] for f in self._svc._relay.store.operator_files(job_id)])
        except Exception:
            logger.exception("PRM: the 选款单 of job #%s could not be deleted", job_id)
