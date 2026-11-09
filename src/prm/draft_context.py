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
  {"gated": true}`` next to it: an older worker, one not restarted, one whose module did not load) is held: not ready,
  「Mac 没有对图：worker 旧了或没重启」.  The packet keeps the mark of its last preview (``meta.draft_context``); one
  without it (``unchecked``: a draft from before the switch was on, too) cannot be approved until a new preview.
- ``take_operator_files`` / ``preview_operator_files``: the Mac sends the sheet on the relay's operator-only channel
  (``relay_operator_files``, POST /relay/jobs/<id>/operator-files, which an older relay does not have): never one of the
  draft's attachments, kept as the packet's ``meta.operator_files`` = ``[{"name", "path", "why"}]`` (``path`` as an
  attachment's: where the relay stored the file, ``outfiles/job-<id>/``), never sent to her.  An outfile named like the
  sheet (``SHEET_PREFIX``) is never an attachment either.  Files past MAX_OPERATOR_FILES, of a job that failed, was
  cancelled or answered what the bot cannot read, of a draft that is not waiting for a person, of a new preview that
  came too late or cannot be read, and the ones a new preview replaces are deleted (``delete_files``): her photo is on a
  sheet.  (A job lost on the way keeps its files: its answer may still come; else the sweep takes them.)
- ``picture`` / ``operator_files_out``: the console shows them (GET /api/prm/file?packet=&kind=operator&n=); the same
  endpoint serves a draft's own attachments (``kind=attachment``).
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
OPERATOR_FILES = "operator_files"   # the draft's (the Mac's answer) and the packet's meta
SHEET_PREFIX = "对图单-"            # the Mac's sheet (worker.sample_order_gate): never an attachment, named or not
SHEET_WHY = "寄样单里每一顶和她发的图并排对比；只给运营看，不发给达人"
NOT_GATED = "Mac 没有对图：worker 旧了或没重启"
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
    [{"platform", "no"}]}``, the newest orders first (the bot's own, which are also ``verified``, then ours made by hand
    that went into her deal), each once, of their shapes."""
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
    return {"orders": orders, "verified": [o for o in orders if o in made], "accounts": out[:MAX_ACCOUNTS]}


def gated_of(value) -> bool:
    """Did the Mac check this answer's sample order by photo: its ``draft_context`` is ``{"gated": true}`` (``value``:
    the answer as a dict, or its JSON text)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return False
    mark = value.get(KEY) if isinstance(value, dict) else None
    return isinstance(mark, dict) and mark.get("gated") is True


def unchecked(packet: Optional[dict]) -> bool:
    """A draft's jst.sample_order the Mac did not check by photo: its ``meta.draft_context`` is not ``{"gated": true}``
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


def picture(packet: Optional[dict], kind: str, n: int) -> Optional[tuple]:
    """``(path, content type)`` of a draft's picture for GET /api/prm/file, or None: ``kind`` ``attachment`` (its own
    files, ``packet.attachments[n]``: the 直播手卡) or ``operator`` (``meta.operator_files[n]``: the photo sheet, and only
    a file the relay keeps for a job, ``…/outfiles/job-<id>/<name>``); only a picture (``IMAGE_TYPES``) that is still
    there."""
    if not packet or kind not in PICTURE_KINDS or not isinstance(n, int) or n < 0:
        return None
    files = files_of(packet) if kind == "operator" else list(packet.get("attachments") or [])   # (n as the console counts)
    entry = files[n] if n < len(files) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]:
        return None
    path = Path(entry["path"])
    content_type = IMAGE_TYPES.get(path.suffix.lower())
    if content_type is None:
        return None
    if kind == "operator" and (path.parent.parent.name != "outfiles" or not path.parent.name.startswith("job-")):
        return None
    return (path, content_type) if path.is_file() else None


def operator_file(packet: dict, n) -> Optional[tuple]:
    """``picture(packet, "operator", n)`` (``n`` as given; not a whole number: None)."""
    try:
        return picture(packet, "operator", int(n))
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


def sweep(store, now: Optional[float] = None, days: int = KEEP_DAYS, files_dir=None, relay_store=None) -> int:
    """The operator files older than ``days`` (by the file's own time): the packets' files deleted (her picture is on a
    sheet) and their paths cleared (the console says the picture is gone), each packet's list written back only if it
    is still the one read (``merge_packet_meta``'s ``expect``: a new preview's sheet that came meanwhile stays; the next
    sweep looks again); with ``relay_store``, every file the relay's operator-only channel took (``relay_operator_files``:
    a packet's or not, its row forgotten once the file is gone), and with ``files_dir`` (the relay's), any outfile
    named like a sheet in ``outfiles/job-*/`` that old too (an older worker's way).  Returns how many files went."""
    now = time.time() if now is None else now
    cutoff = now - days * 86400

    def old(path: Optional[Path]) -> bool:
        try:
            return path is not None and (not path.is_file() or path.stat().st_mtime < cutoff)
        except OSError:
            return False

    gone = 0
    for packet in store.operator_file_packets():
        read = ((packet.get("meta") or {}).get(OPERATOR_FILES))
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
            elif old(path):
                gone += delete_files([str(path)])
                if not path.exists():
                    forget.append(row["id"])
        relay_store.forget_operator_files(forget)
    if files_dir is not None:
        for path in Path(files_dir).glob(f"outfiles/job-*/{SHEET_PREFIX}*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    gone += delete_files([str(path)])
            except OSError:
                pass
    if gone:
        logger.info("PRM: %d operator file(s) older than %d days deleted", gone, days)
    return gone
