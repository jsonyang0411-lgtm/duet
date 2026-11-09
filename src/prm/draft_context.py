"""PRM_DRAFT_CONTEXT (default off, 2026-11-09): the bot's half of the draft context.

The Mac (tools/claude_relay/draft_context.py) tells a creator's draft what WeChat does not show: her 聚水潭 sample orders
(<sample_status>: sent or not, which hats, which waybills) and what the pictures she sent are in the hat photo index
(<photo_matches>).  It also checks a draft's jst.sample_order by photo (the user's rule: every hat of a sample order is
matched by photo, never by name, and a person sees a side-by-side sheet before the order is made): a hat with no picture
behind it leaves the preview not ready, and the sheet comes back with the draft for the operators.  This side:

- ``job_context``: ``meta.draft_context`` of a creator's WeChat draft job (``PrmService._reply_meta``) and of a new
  preview of a draft's sample order (``_repreview``) while the switch is on: the 聚水潭 sample orders her deal knows (the
  bot's own verified jst.sample_order ones, ``sample_orders``; ours made by hand whose waybill went into her deal,
  ``hand_samples``) and her platform accounts (``accounts_of``: 抖音号 / 小红书号 of the 达人名单, of her verified sample
  orders and of the links).  The Mac finds her orders by these, never by her nickname.  Without the key the Mac does none
  of it (the switch off, an older bot): the prompt as before, byte for byte.
- ``operator_entries`` / ``split_operator_files``: the files a draft job sends back are attachments for the creator,
  except the ones the Mac names in the draft's ``operator_files`` (``[{"name", "why", "sha256"}]``; and any file named
  like the Mac's sheet): those are the operators' only, the packet's ``meta.operator_files`` = ``[{"name", "path",
  "why"}]`` (``path`` as an attachment's: where the relay stored the file), never sent to her.  ``preview_operator_files``:
  the same for a new preview's sheet.
- ``operator_files_out`` / ``operator_file``: the console's draft card shows them as pictures (GET /api/prm/file).
- ``sweep``: a sheet shows her picture: its file goes after KEEP_DAYS, as the pictures she sends do (the relay's
  KEEP_FILES_DAYS).
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

KEY = "draft_context"               # the job's meta
OPERATOR_FILES = "operator_files"   # the draft's (the Mac's answer) and the packet's meta
SHEET_PREFIX = "对图单-"            # the Mac's sheet (worker.sample_order_gate): never an attachment, named or not
SHEET_WHY = "寄样单里每一顶和她发的图并排对比；只给运营看，不发给达人"
MAX_ORDERS = 20
MAX_ACCOUNTS = 10
MAX_OPERATOR_FILES = 4
KEEP_DAYS = 30
ACCOUNT_PLATFORMS = ("DY", "XHS")
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

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


def job_context(store, deal_id: int, accounts) -> dict:
    """``meta.draft_context`` of a draft job of the deal: ``{"orders": ["YP…"], "accounts": [{"platform", "no"}]}``, the
    newest orders first (the bot's own, then ours made by hand that went into her deal), each once, of their shapes."""
    orders: list = []
    for row in reversed(store.sample_orders(deal_id=deal_id)):
        orders.append(str(row.get("order_no") or ""))
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
    return {"orders": orders, "accounts": out[:MAX_ACCOUNTS]}


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
    """A draft job's files (the relay's outfiles: ``{"name", "path", "size", "sha256"}``) as ``(attachments,
    operator_files)``: a file the draft names in ``operator_files`` (by its bytes' sha256 when the Mac gave it, else by its
    name), or one named like the Mac's sheet (``SHEET_PREFIX``), is the operators' only, ``{"name", "path", "why"}``;
    every other file stays an attachment, as before."""
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
    return rest, theirs[:MAX_OPERATOR_FILES]


def preview_operator_files(result_text, files: list) -> list:
    """The operator files of a new preview (``platform_preview``): the ones its answer names (``operator_files`` next
    to the block) among the job's outfiles; [] for none (an older worker, another action)."""
    try:
        data = json.loads(result_text or "")
    except (TypeError, ValueError):
        return []
    named = operator_entries(data.get(OPERATOR_FILES)) if isinstance(data, dict) else []
    return split_operator_files(files, named)[1] if named else []


def files_of(packet: dict) -> list:
    """A packet's operator files as kept (``meta.operator_files``), each ``{"name", "path", "why"}``."""
    out = []
    for entry in ((packet or {}).get("meta") or {}).get(OPERATOR_FILES) or []:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str) and isinstance(entry.get("path"), str):
            out.append({"name": entry["name"], "path": entry["path"], "why": str(entry.get("why") or "")})
    return out[:MAX_OPERATOR_FILES]


def operator_file(packet: dict, n) -> Optional[tuple]:
    """``(path, content type)`` of a packet's ``n``-th operator file for GET /api/prm/file, or None: only a picture the
    relay keeps for a job (``…/outfiles/job-<id>/<name>``) that is still there."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    files = files_of(packet)
    if not 0 <= n < len(files) or not files[n]["path"]:
        return None
    path = Path(files[n]["path"])
    kind = IMAGE_TYPES.get(path.suffix.lower())
    if kind is None or path.parent.parent.name != "outfiles" or not path.parent.name.startswith("job-"):
        return None
    return (path, kind) if path.is_file() else None


def operator_files_out(packet: dict) -> list:
    """The console's view of a packet's operator files: ``[{"n", "name", "why", "url", "kept"}]`` (``kept`` False: its
    file is gone, ``sweep`` or by hand)."""
    out = []
    for n, entry in enumerate(files_of(packet)):
        out.append({"n": n, "name": entry["name"], "why": entry["why"],
                    "url": f"/api/prm/file?packet={int(packet['id'])}&n={n}",
                    "kept": bool(entry["path"]) and Path(entry["path"]).is_file()})
    return out


def sweep(store, now: Optional[float] = None, days: int = KEEP_DAYS) -> int:
    """The operator files of drafts made ``days`` ago and more: their files deleted (her picture is on a sheet), their
    paths cleared in the packet (the console says the picture is gone).  Returns how many files went."""
    now = time.time() if now is None else now
    gone = 0
    for packet in store.operator_file_packets(now - days * 86400):
        kept = []
        for entry in (packet.get("meta") or {}).get(OPERATOR_FILES) or []:
            entry = dict(entry) if isinstance(entry, dict) else {}
            path = str(entry.get("path") or "")
            if path:
                try:
                    Path(path).unlink()
                    gone += 1
                except FileNotFoundError:
                    pass
                except OSError as e:                 # in use, say: tried again at the next round
                    logger.warning("PRM: an operator file of packet %d could not be deleted (%s)", packet["id"], e)
                    kept.append(entry)
                    continue
                entry["path"] = ""
            kept.append(entry)
        store.merge_packet_meta(packet["id"], {OPERATOR_FILES: kept})
    if gone:
        logger.info("PRM: %d operator file(s) older than %d days deleted", gone, days)
    return gone
