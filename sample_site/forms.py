"""Server-side checks of a creator's form, phone normalising and masking."""

import re
import unicodedata

from .catalog import has_control_chars

# The 34 province-level divisions, in the order the select lists them.
PROVINCES = (
    "北京市", "天津市", "上海市", "重庆市",
    "河北省", "山西省", "辽宁省", "吉林省", "黑龙江省",
    "江苏省", "浙江省", "安徽省", "福建省", "江西省", "山东省",
    "河南省", "湖北省", "湖南省", "广东省", "海南省",
    "四川省", "贵州省", "云南省", "陕西省", "甘肃省", "青海省", "台湾省",
    "内蒙古自治区", "广西壮族自治区", "西藏自治区", "宁夏回族自治区", "新疆维吾尔自治区",
    "香港特别行政区", "澳门特别行政区",
)
MUNICIPALITIES = ("北京市", "天津市", "上海市", "重庆市")

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
ACCOUNT_RE = re.compile(r"^[0-9A-Za-z_.][0-9A-Za-z_.-]{1,39}$")

# Field -> message shown next to it (creator-facing, see the wording rules).
MESSAGES = {
    "pick_none": "请至少选一顶",
    "pick_many": "最多选 %d 顶，请取消几顶再提交",
    "pick_stale": "款式有更新，请重新确认您的选择",
    "pick_again": "请重新确认您的选择",
    "name": "请填写收货人（30 字以内）",
    "phone": "手机号好像不对，请填 11 位手机号",
    "province": "请选择省份",
    "city": "请填写城市（2–20 字）",
    "district": "请填写区/县（2–20 字）",
    "address": "请填写详细地址（4–120 字）",
    "account": "请填写正确的抖音号（字母、数字、下划线或点）",
    "control": "这里有无法识别的字符，请重新输入",
}

RECEIVER_FIELDS = ("name", "phone", "province", "city", "district", "address")


def normalise_phone(raw):
    """Strip spaces, dashes, brackets and a +86 / 0086 prefix; return the digits."""
    s = unicodedata.normalize("NFKC", raw or "")
    s = re.sub(r"[\s\-()（）]", "", s)
    if s.startswith("+86"):
        s = s[3:]
    elif s.startswith("0086"):
        s = s[4:]
    elif s.startswith("86") and len(s) == 13:
        s = s[2:]
    return s


def normalise_account(raw):
    s = unicodedata.normalize("NFKC", raw or "").strip()
    if s.startswith("@"):
        s = s[1:].strip()
    return s


def _first(form, key):
    values = form.get(key) or [""]
    return values[0] if values else ""


def check_form(form, catalog, max_items, ask_account):
    """Validate a posted form against the current catalogue.

    ``form`` maps field -> list of values (``urllib.parse.parse_qs`` shape).
    Returns ``(clean, errors, typed)``: ``clean`` is ``None`` when there are
    errors, else ``{"items": [...], "receiver": {...}, "account": str|None}``;
    ``errors`` maps field -> message; ``typed`` is what was typed (for
    re-rendering the form).
    """
    errors = {}
    typed = {}
    for key in RECEIVER_FIELDS + ("account",):
        value = _first(form, key)
        typed[key] = value[:200]

    picks = []
    seen = set()
    duplicate = False
    for raw in form.get("pick", []):
        raw = (raw or "").strip()
        if not raw:
            continue
        if raw in seen:
            duplicate = True
            continue
        seen.add(raw)
        picks.append(raw)
    typed["pick"] = picks

    items = []
    stale = False
    for sku in picks:
        snap = catalog.snapshot(sku) if catalog is not None else None
        if snap is None:
            stale = True
        else:
            items.append(snap)
    if stale:
        errors["pick"] = MESSAGES["pick_stale"]
    elif duplicate:
        errors["pick"] = MESSAGES["pick_again"]
    elif not items:
        errors["pick"] = MESSAGES["pick_none"]
    elif len(items) > max_items:
        errors["pick"] = MESSAGES["pick_many"] % max_items

    receiver = {}
    for key in RECEIVER_FIELDS:
        value = typed[key]
        if has_control_chars(value):
            errors[key] = MESSAGES["control"]
            continue
        value = value.strip()
        if key == "name":
            if not (1 <= len(value) <= 30):
                errors[key] = MESSAGES["name"]
        elif key == "phone":
            value = normalise_phone(value)
            if not PHONE_RE.match(value):
                errors[key] = MESSAGES["phone"]
        elif key == "province":
            if value not in PROVINCES:
                errors[key] = MESSAGES["province"]
        elif key in ("city", "district"):
            if not (2 <= len(value) <= 20):
                errors[key] = MESSAGES[key]
        elif key == "address":
            if not (4 <= len(value) <= 120):
                errors[key] = MESSAGES["address"]
        receiver[key] = value

    account = None
    if ask_account:
        raw = typed["account"]
        if has_control_chars(raw):
            errors["account"] = MESSAGES["control"]
        else:
            account = normalise_account(raw)
            if not ACCOUNT_RE.match(account):
                errors["account"] = MESSAGES["account"]

    if errors:
        return None, errors, typed
    return {"items": items, "receiver": receiver, "account": account}, {}, typed


def mask_phone(phone):
    phone = phone or ""
    if len(phone) >= 7:
        return phone[:3] + "****" + phone[-4:]
    return "****"


def mask_name(name):
    name = name or ""
    return (name[:1] + "＊＊") if name else "＊＊"


def mask_receiver(receiver):
    """The remnants kept after ack / retention: no district, no address."""
    receiver = receiver or {}
    return {
        "name": mask_name(receiver.get("name")),
        "phone": mask_phone(receiver.get("phone")),
        "province": receiver.get("province", ""),
        "city": receiver.get("city", ""),
        "district": "",
        "address": "",
    }
