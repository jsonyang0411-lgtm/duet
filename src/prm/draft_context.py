"""PRM_DRAFT_CONTEXT (default off, 2026-11-09): the bot's half of the draft context.

The Mac (tools/claude_relay/draft_context.py) tells a creator's draft what WeChat does not show: her 聚水潭 sample orders
(<sample_status>: sent or not, which hats, which waybills) and what the pictures she sent are in the hat photo index
(<photo_matches>).  It also checks a draft's jst.sample_order by photo (the user's rule: every hat of a sample order is
matched by photo, never by name, and a person sees a side-by-side sheet before the order is made): a hat with no sure
picture behind it leaves the preview not ready, and the sheet comes back with the draft for the operators.  This side:

- ``job_context``: ``meta.draft_context`` of a creator's WeChat draft job (``PrmService._reply_meta``) and of a new
  preview of a draft's sample order (``_repreview``) while the switch is on: the 聚水潭 sample orders her deal knows (the
  bot's own verified jst.sample_order ones, ``sample_orders``, also listed as ``verified``; ours made by hand whose
  waybill went into her deal, ``hand_samples``) and her platform accounts (``accounts_of``: 抖音号 / 小红书号 of the
  达人名单, of her verified sample orders and of the links; an account that is two active creators' is left out,
  ``unambiguous``).  The Mac finds her orders by these, never by her nickname.  Without the key the Mac does none of it
  (the switch off, an older bot): the prompt as before, byte for byte.
- ``ungated``: with the switch on, a jst.sample_order preview the Mac did not check by photo (no ``draft_context:
  {"v": 3, "gated": true}`` next to it: an older worker, a v1 or v2 one, one not restarted, one whose module did not
  load) is held: not ready,
  「Mac 没有对图：worker 旧了或没重启，更新、重启以后点「重新预览」」.  The packet keeps the mark of its last preview (``meta.draft_context``); one
  without it (``unchecked``: a draft from before the switch was on, too) cannot be approved until a new preview.
- ``confirmable`` / ``held_out`` / ``withdrawn``: 「对过图了」 (2026-11-09; words in the 起草备注 confirm nothing): the Mac
  names the codes its check held for photo reasons (``photo_held``) and those it let go only by an operator's
  confirmation (``photo_confirmed``), kept in the packet's meta.  The console shows 「对过图了，用 <编码>」 for each held one;
  the deal action ``photo_confirm`` keeps it for the deal (``PrmStore.photo_confirmations``, until 「撤销」:
  ``photo_unconfirm``), the next preview takes it, and a draft that went by one taken back waits for a new preview.
- ``held_hats``: where an operator sees each held hat (2026-11-10, 微信里「对过图」, src/prm/wechat_review.py): the
  sheet (the held hats' one, 「对图单-<任务>-扣着的.jpg」, or the side-by-side one) and the row there, and whether its hold
  says there is no picture of its code, read from the Mac's own lines in the preview (its facts labelled ``HELD_LABEL``
  / ``PAIR_LABEL``, its problems).  The admin's 「对过图 1」 quoting the draft's notice confirms only a hat with a row on
  a sheet that notice sent; ``photo_confirm`` is the same for both, and once it leaves no held hat unconfirmed the
  service asks for the new preview itself.
- ``take_operator_files`` / ``preview_operator_files``: the Mac sends the sheet on the relay's operator-only channel
  (``relay_operator_files``, POST /relay/jobs/<id>/operator-files, which an older relay does not have): never one of the
  draft's attachments, kept as the packet's ``meta.operator_files`` = ``[{"name", "path", "why"}]`` (``path`` as an
  attachment's: where the relay stored the file, ``outfiles/job-<id>/``), never sent to her.  An outfile named like the
  sheet (``SHEET_PREFIX``) is never an attachment either.  Files past MAX_OPERATOR_FILES, of a job that failed, was
  cancelled or answered what the bot cannot read, of a draft that is not waiting for a person, of a new preview that
  came too late or cannot be read, and the ones a new preview replaces are deleted (``delete_files``): her photo is on a
  sheet.  (A job lost on the way keeps its files: its answer may still come; else the sweep takes them.)
- ``picture`` / ``operator_files_out``: the console shows them (GET /api/prm/file?packet=&kind=operator&n=); the same
  endpoint serves a draft's own attachments (``kind=attachment``), either kind only from inside the relay's files.
- ``sweep``: by the files' own time, the sheets go after KEEP_DAYS, as the pictures she sends do (the relay's
  KEEP_FILES_DAYS), referenced by a packet or not.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

KEY = "draft_context"               # the job's meta, and the Mac's mark next to a checked sample order
MARK_VERSION = 3                    # the mark's ``v`` the bot takes; an older one is held like no mark: a v1 worker took a
                                    # colour never compared for sure, a v2 one let a hat go on 「确认 X」 in the 起草备注,
                                    # its false positives too (3 since 2026-11-10: the console's 「对过图了」 only)
OPERATOR_FILES = "operator_files"   # the draft's (the Mac's answer) and the packet's meta
SHEET_PREFIX = "对图单-"            # the Mac's sheet (worker.sample_order_gate): never an attachment, named or not
SHEET_WHY = "寄样单里每一顶和她发的图并排对比；只给运营看，不发给达人"
NOT_GATED = "Mac 没有对图：worker 旧了或没重启，更新、重启以后点「重新预览」"
OLD_RULE = "这份是旧版 Mac 按旧规则核的：更新、重启 Mac 的 worker 以后，点「重新预览」按新规则再核"   # a kept draft
                                    # whose mark is older than MARK_VERSION
# 「对过图了」 (2026-11-09): the Mac's answer, and the packet's meta, name the item codes its check held for photo reasons
# (``photo_held``: the console offers 「对过图了，用 <编码>」 for each) and those it let go only by an operator's
# confirmation (``photo_confirmed``: taken back, 「撤销」, the draft waits for a new preview); the deal's confirmations
# go with every draft and new-preview job (``job_context``'s ``confirmed``)
PHOTO_HELD = "photo_held"
PHOTO_CONFIRMED = "photo_confirmed"
CONFIRM_HOW = "对过图后，在控制台这份草稿上点这顶旁边的「对过图了」"
MAX_HELD = 50                       # codes of one preview (held, or let go by a confirmation)
MAX_CONFIRMED = 100                 # codes confirmed for one deal that go with a job (the Mac's MAX_CONFIRMED)
_CODE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{2,63}$")      # a 聚水潭 item code (platforms._ERP_CODE)
MAX_ORDERS = 20
MAX_ACCOUNTS = 10
MAX_OPERATOR_FILES = 4
KEEP_DAYS = 30
ACCOUNT_PLATFORMS = ("DY", "XHS")
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
PICTURE_KINDS = ("attachment", "operator")

_ORDER_NO = re.compile(r"^YP[0-9A-Za-z]{6,30}$")
_ACCOUNT = re.compile(r"^[0-9A-Za-z_.][0-9A-Za-z_.-]{0,39}$")     # an 抖音号 / 小红书号 (platforms._CREATOR_NO)
_SHA = re.compile(r"^[0-9a-f]{64}$")


def accounts_of(store, party: dict, creator_ids: dict, directory: Optional[Callable[[], list]] = None) -> list:
    """Her platform accounts, ``[(platform, number)]``: the 抖音号 her verified sample orders and the 抖音 links found
    (``creator_ids``: ``PrmService._creator_ids``), the 达人名单's platform number of her wxid (``directory``: the
    service's ``creator_directory``), her 小红书 links.  A list that cannot be read is only logged."""
    out = [("DY", d) for d in (creator_ids or {}).get("douyin_ids") or [] if isinstance(d, str)]
    rows: list = []
    if directory is not None:
        try:
            rows = list(directory() or [])
        except Exception:
            logger.exception("PRM: the creator list could not be read for a draft's context")
    for row in rows:
        if isinstance(row, dict) and str(row.get("wxid") or "") == str(party.get("wxid") or ""):
            out.append((str(row.get("platform") or "").strip().upper(), str(row.get("platform_no") or "")))
    for link in store.creator_links(party["id"]):
        if link.get("platform") == "xhs":
            out.append(("XHS", str(link.get("account") or "")))
    return out


def unambiguous(accounts, owners: dict, wxid: str) -> list:
    """``accounts`` without the ones another active creator has too: ``owners`` is ``PrmService._hand_index``'s
    ``keys`` ((platform, account lower case) -> the wxids of the active creators known by it); her 抖音号 shared with
    another registered creator finds neither's orders (as the hand-made orders and the 百应 conversations go)."""
    out = []
    for platform, number in accounts or []:
        key = (str(platform or "").strip().upper(), str(number or "").strip().lower())
        if not (owners.get(key) or set()) - {wxid}:
            out.append((platform, number))
    return out


def job_context(store, deal_id: int, accounts) -> dict:
    """``meta.draft_context`` of a draft job of the deal: ``{"orders": ["YP…"], "verified": ["YP…"], "accounts":
    [{"platform", "no"}], "confirmed": [item codes]}``, the newest orders first (the bot's own, which are also
    ``verified``, then ours made by hand that went into her deal), each once, of their shapes; ``confirmed``: the codes
    an operator confirmed for the deal in the console (「对过图了」, ``PrmStore.photo_confirmations``)."""
    made = [str(row.get("order_no") or "").strip() for row in reversed(store.sample_orders(deal_id=deal_id))]
    orders = list(made)
    for row in store.hand_samples(limit=500):
        if row.get("deal_id") == deal_id:
            orders.append(str(row.get("order_no") or ""))
    orders = [o for o in dict.fromkeys(o.strip() for o in orders) if _ORDER_NO.match(o)][:MAX_ORDERS]
    seen, out = set(), []
    for platform, number in accounts or []:
        platform, number = str(platform or "").strip().upper(), str(number or "").strip()
        if platform in ACCOUNT_PLATFORMS and _ACCOUNT.match(number) and (platform, number.lower()) not in seen:
            seen.add((platform, number.lower()))
            out.append({"platform": platform, "no": number})
    confirmed = [str(r["code"]) for r in store.photo_confirmations(deal_id)][-MAX_CONFIRMED:]
    return {"orders": orders, "verified": [o for o in orders if o in made], "accounts": out[:MAX_ACCOUNTS],
            "confirmed": confirmed}


def gated_of(value) -> bool:
    """Did the Mac check this answer's sample order by photo: its ``draft_context`` is ``{"v": 3, "gated": true}``
    (``value``: the answer as a dict, or its JSON text; a packet's meta too).  A mark older than MARK_VERSION is not
    one: that worker took a colour it never compared for sure (v1), or a 「确认 X」 in the 起草备注 (v2)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return False
    mark = value.get(KEY) if isinstance(value, dict) else None
    version = mark.get("v") if isinstance(mark, dict) else None
    return (mark.get("gated") is True and isinstance(version, int) and not isinstance(version, bool)
            and version >= MARK_VERSION) if isinstance(mark, dict) else False


def mark(gated: bool) -> dict:
    """The packet's ``meta.draft_context``: whether its last preview was checked by photo."""
    return {"v": MARK_VERSION, "gated": bool(gated)}


def codes_of(value, key: str) -> list:
    """The item codes under ``key`` (``PHOTO_HELD`` / ``PHOTO_CONFIRMED``) of the Mac's answer or a packet's meta
    (``value``: a dict, or its JSON text): upper case, of their shape, each once, at most MAX_HELD."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    codes = value.get(key) if isinstance(value, dict) else None
    out = [str(c).strip().upper() for c in codes if isinstance(c, str)] if isinstance(codes, list) else []
    return [c for c in dict.fromkeys(out) if _CODE.match(c)][:MAX_HELD]


def code_of(value) -> str:
    """An item code the console sent (upper case), or "" when it is not one."""
    code = str(value or "").strip().upper() if isinstance(value, str) else ""
    return code if _CODE.match(code) else ""


def confirmable(packet: Optional[dict]) -> list:
    """The codes 「对过图了」 may confirm on this draft: the ones its last preview held for photo reasons, or let go only
    by a confirmation (taken back, it may be given again)."""
    meta = (packet or {}).get("meta") or {}
    return list(dict.fromkeys(codes_of(meta, PHOTO_HELD) + codes_of(meta, PHOTO_CONFIRMED)))


def withdrawn(packet: Optional[dict], confirmed) -> list:
    """The codes the draft's last preview let go only by an operator's 「对过图了」 that is not there any more
    (``confirmed``: the deal's confirmed codes now): then it waits for a new preview."""
    meta = (packet or {}).get("meta") or {}
    return [c for c in codes_of(meta, PHOTO_CONFIRMED) if c not in confirmed]


def withdrawn_reason(codes) -> str:
    return f"撤销了 {'、'.join(codes)} 的「对过图了」：点「重新预览」再核" if codes else ""


def held_unconfirmed(packet: Optional[dict], confirmed) -> list:
    """The codes the draft's last preview held for photo reasons (``photo_held``: its verdict is not ready then) that no
    operator confirmed since (``confirmed``: the deal's confirmed codes now): such a sample order never runs, whatever its
    verdict says (``PrmService._approve_execute``, review 2026-11-10 A-L4: a second line; a hat let go only by a
    confirmation is ``photo_confirmed``, ``withdrawn``)."""
    meta = (packet or {}).get("meta") or {}
    return [c for c in codes_of(meta, PHOTO_HELD) if c not in confirmed]


def held_reason(codes, confirm: bool = True) -> str:
    """Why a sample order with these held hats (``held_unconfirmed``) does not run: 「对过图了」, then 「重新预览」; with
    ``confirm`` False (PRM_DRAFT_CONTEXT off: the console has no 「对过图了」 then, ``photo_confirm`` refuses it) only
    「重新预览」, whose new preview no photo check holds (final check 2026-11-10 A4)."""
    if not codes:
        return ""
    if not confirm:
        return f"{'、'.join(codes)} 还扣着（没对过图），不能执行：点「重新预览」再核"
    return f"{'、'.join(codes)} 还扣着（没对过图），不能执行：{CONFIRM_HOW}，再点「重新预览」"


def held_out(packet: Optional[dict], confirmed: dict) -> list:
    """The console's view of a waiting sample order's hats the Mac held for photos (PRM_DRAFT_CONTEXT): ``[{"code",
    "held", "used", "confirmed", "at"}]`` for each code its last preview held (``held``: then 「对过图了，用 <编码>」, or
    once confirmed 「已确认 <编码>，点「重新预览」按确认再核」) or let go only by a confirmation (``used``); ``confirmed`` /
    ``at``: an operator's 「对过图了」 for the deal is there, and when (``confirmed``: code -> time; 「撤销」 takes it
    back)."""
    meta = (packet or {}).get("meta") or {}
    held = set(codes_of(meta, PHOTO_HELD))
    return [{"code": c, "held": c in held, "used": c not in held, "confirmed": c in confirmed, "at": confirmed.get(c)}
            for c in confirmable(packet)]


# 微信里「对过图」 (2026-11-10, src/prm/wechat_review.py): which row of which sheet shows a held hat, read from the Mac's
# own words in the preview (tools/claude_relay/draft_context.py ``_gate`` / ``_held_sheet``; the worker's tests run the
# real gate through ``held_hats``, tests/test_prm_draft_context_worker.py).  A line that does not read as one of these
# gives the hat no row: it is confirmed in the console only, never from WeChat
HELD_LABEL = "对图（扣着的那几顶）"   # the facts of the held hats' sheet (the Mac's HELD_LABEL)
PAIR_LABEL = "对图"                 # ... and of the side-by-side sheet's rows
NO_CODE_PHOTO = "没有这个编码的图"   # a held hat nothing on a sheet shows (the Mac's NO_CODE_PHOTO starts with it)
HELD_SHEET = "held"                 # ``held_hats``'s ``sheet``: the held hats' sheet (「对图单-<任务>-扣着的.jpg」)
PAIR_SHEET = "pair"                 # ... the side-by-side sheet (「对图单-<任务>.jpg」)
HELD_SHEET_END = "-扣着的"          # how the held hats' sheet's name ends (worker.sample_order_gate)
_CODE_PART = r"[0-9A-Za-z][0-9A-Za-z_-]{2,63}"
_HELD_ROW = re.compile(rf"^(?P<code>{_CODE_PART})：扣着（(?P<why>.+)）；扣着的那张对图单第 (?P<row>\d{{1,2}}) 行，"
                       r"[^；]*右边是(?P<right>[^；]+)", re.S)
PAIR_ROW_WORDS = "：对的是"         # a fact of the side-by-side sheet's row: 「<编码> <颜色>：对的是<她的哪张图>…」
_LOOK_AGAIN = re.compile(rf"^(?P<code>{_CODE_PART}) 对图要再看（(?P<why>.+)）：先核对再建", re.S)


def is_held_sheet(name) -> bool:
    """Is an operator file's name the held hats' sheet's (「对图单-<任务>-扣着的.jpg」; the relay may add 「(2)」)?"""
    stem = Path(str(name or "")).stem
    return stem.startswith(SHEET_PREFIX) and HELD_SHEET_END in stem


def is_pair_sheet(name) -> bool:
    """... the side-by-side sheet's (「对图单-<任务>.jpg」)?"""
    stem = Path(str(name or "")).stem
    return stem.startswith(SHEET_PREFIX) and HELD_SHEET_END not in stem


def _preview_lines(packet: Optional[dict]) -> tuple:
    """``(facts, problems)`` of a packet's preview, its steps' too: ``[(label, value)]`` and ``[text]``."""
    preview = ((packet or {}).get("platform") or {}).get("preview") or {}
    blocks = [preview, *(s for s in preview.get("steps") or [] if isinstance(s, dict))]
    facts = [(str(f.get("label") or ""), str(f.get("value") or "")) for b in blocks for f in b.get("facts") or []
             if isinstance(f, dict)]
    problems = [p for b in blocks for p in b.get("problems") or [] if isinstance(p, str)]
    return facts, problems


def held_hats(packet: Optional[dict]) -> list:
    """The hats a sample order's last preview held for photos (``photo_held``), each with where an operator sees it:
    ``[{"code", "why", "sheet", "row", "no_photo"}]``, the rows of the held hats' sheet first (in its order), then the
    side-by-side sheet's rows that say to look again, then the hats on neither (as ``photo_held`` has them).  ``why``:
    the Mac's short reason; ``sheet`` / ``row``: HELD_SHEET or PAIR_SHEET and the row there (1 the first), "" / 0 when
    the preview names no row for it; ``no_photo``: its hold says there is no picture of this code (NO_CODE_PHOTO: the
    right side of its row is empty).  Read from the preview's facts and problems as the Mac writes them; whatever does
    not read so leaves the hat without a row (微信里「对过图」 takes only a hat with a row on a sheet that was sent)."""
    held = codes_of((packet or {}).get("meta") or {}, PHOTO_HELD)
    if not held:
        return []
    facts, problems = _preview_lines(packet)
    rows: dict = {}
    pair_row = 0
    for label, value in facts:
        if label == HELD_LABEL:
            m = _HELD_ROW.match(value)
            code = m.group("code").upper() if m else ""
            if m and code in held and code not in rows:
                rows[code] = {"why": m.group("why"), "sheet": HELD_SHEET, "row": int(m.group("row")),
                              "no_photo": m.group("right").strip().startswith(NO_CODE_PHOTO)}
        elif label == PAIR_LABEL and PAIR_ROW_WORDS in value:
            pair_row += 1                            # (the side-by-side sheet's rows, in the facts' order: every one
            code = value.split(" ", 1)[0].upper()    # counts, whatever its code and colour read as)
            if _CODE.match(code) and code in held and code not in rows:
                rows[code] = {"why": "", "sheet": PAIR_SHEET, "row": pair_row, "no_photo": False}
    out = []
    for code in held:
        hat = {"code": code, "why": "", "sheet": "", "row": 0, "no_photo": False, **rows.get(code, {})}
        mine = [p for p in problems if p[:len(code)].upper() == code and p[len(code):len(code) + 1] in ("（", " ")]
        for problem in mine:
            look = _LOOK_AGAIN.match(problem)
            if look:
                hat["why"] = hat["why"] or f"对图单说要再看：{look.group('why').lstrip('！!')}"
            elif "这顶没有照片对过" in problem:
                hat["why"] = hat["why"] or "没有照片对过"
            elif "对图没把握" in problem:
                hat["why"] = hat["why"] or "对图没把握"
            if NO_CODE_PHOTO in problem:
                hat["no_photo"] = True
        if hat["sheet"] == PAIR_SHEET and not any(_LOOK_AGAIN.match(p) for p in mine):
            hat.update(sheet="", row=0)              # (held for another reason than its row: no row to go by)
        hat["why"] = hat["why"] or "没对上图"
        out.append(hat)
    order = {HELD_SHEET: 0, PAIR_SHEET: 1, "": 2}
    return sorted(out, key=lambda h: (order[h["sheet"]], h["row"] if h["sheet"] else held.index(h["code"])))


def mark_version(value) -> Optional[int]:
    """The ``v`` of a mark that says checked (``draft_context: {"v": …, "gated": true}``; ``value`` as ``gated_of``'s),
    or None (no such mark)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    mark = value.get(KEY) if isinstance(value, dict) else None
    if not isinstance(mark, dict) or mark.get("gated") is not True:
        return None
    version = mark.get("v")
    return version if isinstance(version, int) and not isinstance(version, bool) else 1


def unchecked_reason(packet: Optional[dict]) -> str:
    """Why a draft's jst.sample_order cannot be approved as it is (``unchecked``), in the console's words: its mark is an
    older rule's (``OLD_RULE``: a new preview checks it by the new one), or there is none (``NOT_GATED``); "" when it is
    checked (or no sample order)."""
    if not unchecked(packet):
        return ""
    version = mark_version((packet or {}).get("meta"))
    return OLD_RULE if version is not None and version < MARK_VERSION else NOT_GATED


def unchecked(packet: Optional[dict]) -> bool:
    """A draft's jst.sample_order the Mac did not check by photo: its ``meta.draft_context`` is not ``mark(True)``
    (made while PRM_DRAFT_CONTEXT was off, by a worker that does not check, or its last new preview was not checked).
    With the switch on such a draft cannot be approved (``PrmService._approve_execute``) and the console says so."""
    action = ((packet or {}).get("platform") or {}).get("action") or {}
    return action.get("action") == "jst.sample_order" and not gated_of((packet or {}).get("meta"))


def ungated(block: Optional[dict], gated: bool) -> Optional[dict]:
    """A jst.sample_order preview the Mac did not check by photo (``gated`` False), held: not ready, ``NOT_GATED`` among
    its problems (a worse verdict stays).  Any other block, and a checked one, as it is."""
    if gated or not isinstance(block, dict) or ((block.get("action") or {}).get("action") != "jst.sample_order"):
        return block
    problems = [p for p in block.get("problems") or [] if p != NOT_GATED]
    verdict = "blocked" if block.get("verdict") == "ready" else block.get("verdict")
    return {**block, "verdict": verdict, "problems": [*problems, NOT_GATED], "run": {} if verdict != "ready" else
            block.get("run")}


def operator_entries(value) -> list:
    """The draft's ``operator_files`` as the Mac names them: ``[{"name", "why", "sha256"}]`` (a name with no folder, the
    reason short, the bytes' sha256 or ""); anything else is left out, at most MAX_OPERATOR_FILES."""
    out = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()[:120]
        if not name or "/" in name or "\\" in name:
            continue
        sha = str(entry.get("sha256") or "").strip().lower()
        out.append({"name": name, "why": str(entry.get("why") or "").strip()[:200], "sha256": sha if _SHA.match(sha) else ""})
        if len(out) >= MAX_OPERATOR_FILES:
            break
    return out


def split_operator_files(files: list, named: list) -> tuple:
    """A job's outfiles (``{"name", "path", "size", "sha256"}``) as ``(attachments, operator_files)``: one the draft
    names in ``operator_files`` (by its bytes' sha256 when the Mac gave it, else by its name), or one named like the
    Mac's sheet (``SHEET_PREFIX``), is the operators' only, ``{"name", "path", "why"}``; every other file stays an
    attachment, as before.  (The Mac sends its sheet on the operator-only channel; this is the second line.)"""
    wanted = list(named or [])
    rest, theirs = [], []
    for f in files or []:
        sha = str(f.get("sha256") or "").lower()
        match = next((n for n in wanted if (n["sha256"] and n["sha256"] == sha)
                      or (not n["sha256"] and n["name"] == f.get("name"))), None)
        if match is not None:
            wanted.remove(match)
            theirs.append({"name": str(f.get("name") or match["name"]), "path": str(f.get("path") or ""),
                           "why": match["why"]})
        elif str(f.get("name") or "").startswith(SHEET_PREFIX):
            theirs.append({"name": str(f["name"]), "path": str(f.get("path") or ""), "why": SHEET_WHY})
        else:
            rest.append(f)
    return rest, theirs


def take_operator_files(rows: list, named: list, outfiles: list) -> tuple:
    """A draft's files: ``(attachments, keep, drop)``.  ``rows``: the job's operator-only files (the relay's
    ``operator_files``: all of them the operators'), their reasons from ``named`` (the draft's ``operator_files``, by
    sha256 or name; else ``SHEET_WHY``); ``outfiles``: the job's other files, attachments but the ones ``split_
    operator_files`` takes.  ``keep``: ``[{"name", "path", "why"}]``, at most MAX_OPERATOR_FILES; ``drop``: the paths of
    the ones past that (to delete)."""
    attachments, legacy = split_operator_files(outfiles, named)
    reasons = list(named or [])
    found = []
    for row in rows or []:
        sha = str(row.get("sha256") or "").lower()
        match = next((n for n in reasons if (n["sha256"] and n["sha256"] == sha)
                      or (not n["sha256"] and n["name"] == row.get("name"))), None)
        if match is not None:
            reasons.remove(match)
        found.append({"name": str(row.get("name") or ""), "path": str(row.get("path") or ""),
                      "why": match["why"] if match is not None and match["why"] else SHEET_WHY})
    everything = [*found, *legacy]
    return attachments, everything[:MAX_OPERATOR_FILES], [f["path"] for f in everything[MAX_OPERATOR_FILES:]]


def preview_operator_files(result_text, rows: list, outfiles: list) -> tuple:
    """The operator files of a new preview (``platform_preview``): ``(keep, drop)`` of its operator-only files and of
    its outfiles named like the sheet (``SHEET_PREFIX``), reasons from the ``operator_files`` its answer names."""
    try:
        data = json.loads(result_text or "")
    except (TypeError, ValueError):
        data = None
    named = operator_entries(data.get(OPERATOR_FILES)) if isinstance(data, dict) else []
    sheets = [f for f in outfiles or [] if str(f.get("name") or "").startswith(SHEET_PREFIX)]
    _, keep, drop = take_operator_files(rows, named, sheets)
    return keep, drop


def delete_files(paths) -> int:
    """Delete these files (her photo may be on them); a path that is gone already is fine.  Returns how many went."""
    gone = 0
    for path in paths or []:
        if not path:
            continue
        try:
            Path(path).unlink()
            gone += 1
        except FileNotFoundError:
            pass
        except OSError as e:                         # in use, say: the sweep takes it later
            logger.warning("PRM: an operator file could not be deleted (%s)", e)
    return gone


def files_of(packet: dict) -> list:
    """A packet's operator files as kept (``meta.operator_files``), each ``{"name", "path", "why"}``."""
    out = []
    for entry in ((packet or {}).get("meta") or {}).get(OPERATOR_FILES) or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and isinstance(entry.get("path"), str):
            out.append({"name": entry["name"], "path": entry["path"], "why": str(entry.get("why") or "")})
    return out[:MAX_OPERATOR_FILES]


def picture(packet: Optional[dict], kind: str, n: int, root=None) -> Optional[tuple]:
    """``(path, content type)`` of a draft's picture for GET /api/prm/file, or None: ``kind`` ``attachment`` (its own
    files, ``packet.attachments[n]``: the 直播手卡) or ``operator`` (``meta.operator_files[n]``: the photo sheet, and only
    a file the relay keeps for a job, ``…/outfiles/job-<id>/<name>``); only a picture (``IMAGE_TYPES``) that is still
    there, and of either kind only one inside ``root`` (the relay's files, ``ClaudeRelay.files_dir``) once its links
    are followed (review 2026-11-10: as ``WechatReview._relay_picture``; never any other file of this PC).  ``root``
    None: none.  The path given back is the one checked (links followed)."""
    if not packet or kind not in PICTURE_KINDS or not isinstance(n, int) or n < 0 or root is None:
        return None
    files = files_of(packet) if kind == "operator" else list(packet.get("attachments") or [])   # (n as the console counts)
    entry = files[n] if n < len(files) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]:
        return None
    try:
        path = Path(entry["path"]).resolve()
        inside = path.is_relative_to(Path(root).resolve())
    except (OSError, ValueError, RuntimeError):
        return None
    content_type = IMAGE_TYPES.get(path.suffix.lower())
    if content_type is None or not inside:
        return None
    if kind == "operator" and (path.parent.parent.name != "outfiles" or not path.parent.name.startswith("job-")):
        return None
    return (path, content_type) if path.is_file() else None


def operator_file(packet: dict, n, root=None) -> Optional[tuple]:
    """``picture(packet, "operator", n, root)`` (``n`` as given; not a whole number: None)."""
    try:
        return picture(packet, "operator", int(n), root)
    except (TypeError, ValueError):
        return None


def operator_files_out(packet: dict) -> list:
    """The console's view of a packet's operator files: ``[{"n", "name", "why", "url", "kept"}]`` (``kept`` False: its
    file is gone, ``sweep`` or by hand)."""
    out = []
    for n, entry in enumerate(files_of(packet)):
        out.append({"n": n, "name": entry["name"], "why": entry["why"],
                    "url": f"/api/prm/file?packet={int(packet['id'])}&kind=operator&n={n}",
                    "kept": bool(entry["path"]) and Path(entry["path"]).is_file()})
    return out


LIVE_STATUSES = ("pending", "approved", "sending", "executing")   # a draft still to be decided, or going out
SHEET_GONE = "对图单已清理：点「重新预览」"   # a sample order whose sheet is not on this PC any more (final check B)


def _resolved(path) -> str:
    try:
        return str(Path(path).resolve())
    except (OSError, ValueError, RuntimeError):
        return str(path)


def sheets_gone(packet: Optional[dict]) -> bool:
    """A draft's operator files (``files_of``: its 对图单) not all on this PC any more (cleared by the sweep, or deleted):
    its sample order is not approved until a new preview makes them again (``PrmService._approve_execute``, the
    console's 「批准并执行」 off: SHEET_GONE)."""
    for entry in files_of(packet or {}):
        try:
            if not entry["path"] or not Path(entry["path"]).is_file():
                return True
        except OSError:
            return True
    return False


def sweep(store, now: Optional[float] = None, days: int = KEEP_DAYS, files_dir=None, relay_store=None) -> int:
    """The operator files older than ``days`` (by the file's own time): the packets' files deleted (her picture is on a
    sheet) and their paths cleared (the console says the picture is gone), each packet's list written back only if it
    is still the one read (``merge_packet_meta``'s ``expect``: a new preview's sheet that came meanwhile stays; the next
    sweep looks again); with ``relay_store``, every file the relay's operator-only channel took (``relay_operator_files``:
    a packet's or not, its row forgotten once the file is gone), and with ``files_dir`` (the relay's), any outfile
    named like a sheet in ``outfiles/job-*/`` that old too (an older worker's way).  Never a file of a draft still
    waiting or going out (LIVE_STATUSES: final check 2026-11-10 B, a sample order waiting a month kept 「批准并执行」
    with its sheet gone), however old.  Returns how many files went."""
    now = time.time() if now is None else now
    cutoff = now - days * 86400

    def old(path: Optional[Path]) -> bool:
        try:
            return path is not None and (not path.is_file() or path.stat().st_mtime < cutoff)
        except OSError:
            return False

    gone = 0
    live: set = set()
    for packet in store.operator_file_packets():
        read = ((packet.get("meta") or {}).get(OPERATOR_FILES))
        if packet.get("status") in LIVE_STATUSES:
            live |= {_resolved(f["path"]) for f in files_of(packet) if f["path"]}
            continue
        kept, doomed = [], []
        for entry in read if isinstance(read, list) else []:
            entry = dict(entry) if isinstance(entry, dict) else {}
            path = Path(str(entry.get("path") or "")) if entry.get("path") else None
            if old(path):
                doomed.append(str(path))
                entry["path"] = ""
            kept.append(entry)
        if doomed and store.merge_packet_meta(packet["id"], {OPERATOR_FILES: kept},
                                              expect=lambda meta, read=read: meta.get(OPERATOR_FILES) == read):
            gone += delete_files(doomed)
    if relay_store is not None:
        forget = []
        for row in relay_store.operator_files():
            path = Path(str(row.get("path") or ""))
            if not str(row.get("path") or "") or not path.exists():
                forget.append(row["id"])
            elif old(path) and _resolved(path) not in live:
                gone += delete_files([str(path)])
                if not path.exists():
                    forget.append(row["id"])
        relay_store.forget_operator_files(forget)
    if files_dir is not None:
        for path in Path(files_dir).glob(f"outfiles/job-*/{SHEET_PREFIX}*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff and _resolved(path) not in live:
                    gone += delete_files([str(path)])
            except OSError:
                pass
    if gone:
        logger.info("PRM: %d operator file(s) older than %d days deleted", gone, days)
    return gone
