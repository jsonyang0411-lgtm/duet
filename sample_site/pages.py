"""Server-rendered HTML for the creator pages.

All creator-facing text lives in ``TEXT`` (and in ``static/site.js``'s
``MSG``); tests scan both for forbidden words. Every dynamic value goes
through ``e()``; there are no inline scripts or styles (strict CSP).
"""

import html

from .forms import MESSAGES, PROVINCES, receiver_line

TEXT = {
    "subtitle": "样品选款",
    "title": "样品选款",
    "index": "请使用运营发给您的专属链接",
    "invalid": "这个链接已经失效了，请联系运营重新获取",
    "greet": "老师您好～",
    "intro": "挑选您喜欢的款式和颜色，再留下收货信息就好。",
    "cap": "最多选 %d 顶",
    "fallback_hint": "勾选您想要的颜色",
    "pick_btn": "选这顶",
    "picked_btn": "已选 ✓（点此取消）",
    "need_size": "请先选尺码",
    "colour_label": "颜色",
    "size_label": "尺码",
    "empty_catalog": "款式正在整理中，请过一会儿再打开这个链接。",
    "bar_picked": "已选",
    "bar_unit": "顶",
    "next": "下一步",
    "back_pick": "‹ 返回改选款",
    "your_picks": "您选的款式",
    "details": "收货信息",
    "paste_label": "粘贴整段地址（可选）",
    "paste_ph": "把整段收货地址粘贴到这里，可以自动填好下面几项",
    "paste_go": "自动填写",
    "f_name": "收货人",
    "f_phone": "手机号",
    "f_province": "省份",
    "f_province_ph": "请选择",
    "f_city": "城市",
    "f_district": "区/县",
    "f_address": "详细地址",
    "f_address_ph": "街道、小区、楼栋、门牌号",
    "f_addr_full": "地址",
    "f_account": "抖音号",
    "f_account_hint": "在抖音「我」页面，名字下方的那一串",
    "privacy": "您填的收货信息只用于这次样品的寄出，我们会妥善保管。",
    "to_confirm": "下一步：核对信息",
    "fix_above": "有几项需要再看一下，请按提示修改。",
    "confirm_title": "请核对一下",
    "confirm_sub": "确认无误后点「确认提交」。",
    "receiver": "收货信息",
    "submit": "确认提交",
    "edit": "返回修改",
    "thanks": "已收到您的选款，谢谢老师～",
    "thanks_sub": "需要调整的话，可以点下面的「修改」重新选。",
    "edit_btn": "修改",
    "locked": "您的选款我们已经在安排了，如需调整请直接微信联系运营",
    "keep_link": "请收藏这个页面（点右上角「…」），之后可以用它查看或修改您的选择。",
    "stale": MESSAGES["pick_stale"],
    "busy": "操作有点频繁，请过一会儿再试",
    "too_big": "提交的内容太长了，请检查后再试",
    "bad_request": "页面出了点问题，请返回重新打开链接",
    "server_error": "页面出了点问题，请过一会儿再试，或直接微信联系运营",
    "not_found": "没有这个页面",
    "slow": "网络有点慢，这次没有提交成功，请再试一次",
    "receiver_short": "收货",
    "receiver_note": "为了保护您的信息，这里只显示手机尾号和所在城市。",
    "refill": "为了保护您的信息，手机号和详细地址请再填一次",
    "refill_account": "为了保护您的信息，手机号、详细地址和抖音号请再填一次",
    "choice_title": "这台手机刚用这个链接提交过选款",
    "choice_sub": "如果是帮另一位老师填写，请点下面第二个按钮。",
    "choice_mine": "查看刚才的提交",
    "choice_other": "替另一位老师填写",
    "duplicate_sub": "这份选款已经提交过了。如需查看或修改，请用提交后打开的页面，或直接微信联系运营。",
    "per_client": "这台设备用这个链接提交的次数有点多了，如需继续，请直接微信联系运营",
}

DEFAULT_SIZE = (600, 600)


def e(value):
    return html.escape("" if value is None else str(value), quote=True)


def layout(brand, body, static, page_class="", bar=""):
    return (
        '<!doctype html>\n<html lang="zh-CN" class="no-js">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<meta name="format-detection" content="telephone=no">\n'
        '<meta name="referrer" content="no-referrer">\n'
        '<meta name="robots" content="noindex, nofollow">\n'
        '<meta name="color-scheme" content="light">\n'
        '<title>%(brand)s · %(title)s</title>\n'
        '<link rel="stylesheet" href="%(css)s">\n'
        '<script src="%(boot)s"></script>\n'
        '</head>\n<body class="%(cls)s">\n'
        '<header class="top"><div class="top-in"><span class="brand">%(brand)s</span>'
        '<span class="sub">%(subtitle)s</span></div></header>\n'
        '<main class="wrap">\n%(body)s\n</main>\n%(bar)s'
        '<script src="%(js)s" defer></script>\n'
        '</body>\n</html>\n'
    ) % {
        "brand": e(brand), "title": e(TEXT["title"]), "subtitle": e(TEXT["subtitle"]),
        "css": e(static["css"]), "boot": e(static["boot"]), "js": e(static["js"]),
        "cls": e(page_class), "body": body, "bar": bar,
    }


def message_page(brand, static, text, sub="", icon="i", page_class="page-msg"):
    # Break a long sentence after its comma so no single character wraps alone.
    lines = text.split("，")
    title = "".join("<span>%s</span>" % e(line + ("，" if i < len(lines) - 1 else ""))
                    for i, line in enumerate(lines))
    mark = ('<div class="tick" aria-hidden="true"></div>' if icon == "ok"
            else '<div class="msg-icon msg-icon-%s" aria-hidden="true"></div>' % e(icon))
    body = (
        '<section class="msg">%s<h1 class="msg-title">%s</h1>%s</section>'
    ) % (mark, title, ('<p class="msg-sub">%s</p>' % e(sub)) if sub else "")
    return layout(brand, body, static, page_class)


def page_index(brand, static):
    return message_page(brand, static, TEXT["index"], icon="link")


def page_invalid(brand, static):
    return message_page(brand, static, TEXT["invalid"], icon="link")


def _img(src_key, sizes, alt, cls):
    w, h = sizes.get(src_key) or DEFAULT_SIZE
    if not w or not h:
        w, h = DEFAULT_SIZE
    return '<img class="%s" src="/img/%s" width="%d" height="%d" loading="lazy" alt="%s">' % (
        cls, e(src_key), w, h, e(alt))


def _error(errors, key):
    msg = errors.get(key)
    return ('<p class="err" id="err-%s">%s</p>' % (e(key), e(msg))) if msg else ""


def _field(key, label, control, errors, hint=""):
    cls = "field has-err" if errors.get(key) else "field"
    return '<div class="%s" data-field="%s"><label for="f-%s">%s</label>%s%s%s</div>' % (
        cls, e(key), e(key), e(label), control,
        ('<p class="hint">%s</p>' % e(hint)) if hint else "", _error(errors, key))


def _input(key, value, errors, type_="text", maxlength=60, extra=""):
    invalid = ' aria-invalid="true" aria-describedby="err-%s"' % e(key) if errors.get(key) else ""
    return '<input id="f-%s" name="%s" type="%s" value="%s" maxlength="%d"%s%s>' % (
        e(key), e(key), type_, e(value), maxlength, extra, invalid)


def _province_select(value, errors):
    invalid = ' aria-invalid="true"' if errors.get("province") else ""
    opts = ['<option value="">%s</option>' % e(TEXT["f_province_ph"])]
    for name in PROVINCES:
        sel = " selected" if name == value else ""
        opts.append('<option value="%s"%s>%s</option>' % (e(name), sel, e(name)))
    return '<select id="f-province" name="province"%s>%s</select>' % (invalid, "".join(opts))


def _card(style, selected, sizes):
    colours = style["colours"]
    first = 0
    for ci, colour in enumerate(colours):
        if any(z["sku"] in selected for z in colour["sizes"]):
            first = ci
            break
    image = colours[first]["image"]
    chips = []
    opts = []
    for ci, colour in enumerate(colours):
        w, h = sizes.get(colour["image"]) or DEFAULT_SIZE
        chips.append(
            '<button type="button" class="chip" data-ci="%d" data-img="/img/%s" data-w="%d" '
            'data-h="%d">%s</button>' % (ci, e(colour["image"]), w or 600, h or 600, e(colour["name"])))
        multi = len(colour["sizes"]) > 1
        for size in colour["sizes"]:
            label = colour["name"] + ((" · " + size["label"]) if multi else "")
            checked = " checked" if size["sku"] in selected else ""
            opts.append(
                '<label class="opt"><input type="checkbox" name="pick" value="%s" data-ci="%d" '
                'data-size="%s" data-multi="%d"%s><span>%s</span></label>' % (
                    e(size["sku"]), ci, e(size["label"]), 1 if multi else 0, checked, e(label)))
    desc = ('<p class="ds">%s</p>' % e(style["desc"])) if style.get("desc") else ""
    return (
        '<article class="card" data-card data-first="%d">'
        '<div class="ph">%s<span class="badge" data-badge></span></div>'
        '<div class="cb"><h3 class="nm">%s</h3>%s'
        '<div class="js-only"><div class="chips" data-colours>%s</div>'
        '<div class="chips sizes" data-sizes></div>'
        '<p class="chosen" data-chosen></p>'
        '<button type="button" class="pick" data-pick>%s</button></div>'
        '<div class="fallback"><p class="fb-hint">%s</p>%s</div>'
        '</div></article>'
    ) % (first, _img(image, sizes, style["name"] + " " + colours[first]["name"], "ph-img"),
         e(style["name"]), desc, "".join(chips), e(TEXT["pick_btn"]),
         e(TEXT["fallback_hint"]), "".join(opts))


def page_pick(brand, static, action, catalog, sizes, max_items, ask_account, typed=None,
              errors=None, stale=False, start="pick", refill=False):
    """The pick page (also re-rendered with errors and when editing)."""
    typed = typed or {}
    errors = errors or {}
    selected = set(typed.get("pick") or [])
    styles = catalog.styles if catalog is not None else []
    parts = []
    parts.append(
        '<section class="intro"><h1 class="greet">%s</h1><p>%s</p>'
        '<p class="cap"><span>%s</span></p></section>' % (
            e(TEXT["greet"]), e(TEXT["intro"]), e(TEXT["cap"] % max_items)))
    if stale:
        parts.append('<p class="notice">%s</p>' % e(TEXT["stale"]))
    if errors:
        parts.append('<p class="notice notice-err" id="form-err">%s</p>' % e(TEXT["fix_above"]))
    form = []
    form.append('<form method="post" action="%s" id="pickform" accept-charset="utf-8" '
                'novalidate data-max="%d" data-start="%s">' % (e(action), max_items, e(start)))
    form.append('<input type="hidden" name="action" value="review">')
    form.append('<section class="step step-pick" id="step-pick">')
    if errors.get("pick"):
        form.append('<p class="err err-pick" id="err-pick">%s</p>' % e(errors["pick"]))
    if styles:
        form.append('<div class="cards">%s</div>' % "".join(_card(s, selected, sizes) for s in styles))
    else:
        form.append('<p class="notice">%s</p>' % e(TEXT["empty_catalog"]))
    form.append('</section>')

    form.append('<section class="step step-details" id="details">')
    form.append('<div class="js-only"><button type="button" class="linkbtn" data-back>%s</button>'
                '<div class="panel picked-panel"><h2>%s</h2><ul class="items" data-picked></ul></div>'
                '</div>' % (e(TEXT["back_pick"]), e(TEXT["your_picks"])))
    form.append('<div class="panel"><h2>%s</h2>' % e(TEXT["details"]))
    form.append(
        '<div class="js-only paste"><label for="paste">%s</label>'
        '<textarea id="paste" rows="3" maxlength="300" placeholder="%s"></textarea>'
        '<div class="paste-row"><button type="button" class="btn btn-ghost" data-paste>%s</button>'
        '<span class="hint" data-paste-msg></span></div></div>' % (
            e(TEXT["paste_label"]), e(TEXT["paste_ph"]), e(TEXT["paste_go"])))
    if refill:
        form.append('<p class="notice notice-refill">%s</p>' % e(
            TEXT["refill_account"] if ask_account else TEXT["refill"]))
    form.append(_field("name", TEXT["f_name"],
                       _input("name", typed.get("name", ""), errors, maxlength=30,
                              extra=' autocomplete="name"'), errors))
    form.append(_field("phone", TEXT["f_phone"],
                       _input("phone", typed.get("phone", ""), errors, type_="tel", maxlength=20,
                              extra=' inputmode="tel" autocomplete="tel"'), errors))
    form.append(_field("province", TEXT["f_province"],
                       _province_select(typed.get("province", ""), errors), errors))
    form.append('<div class="row2">')
    form.append(_field("city", TEXT["f_city"],
                       _input("city", typed.get("city", ""), errors, maxlength=20), errors))
    form.append(_field("district", TEXT["f_district"],
                       _input("district", typed.get("district", ""), errors, maxlength=20), errors))
    form.append('</div>')
    form.append(_field("address", TEXT["f_address"],
                       _input("address", typed.get("address", ""), errors, maxlength=120,
                              extra=' placeholder="%s"' % e(TEXT["f_address_ph"])), errors))
    if ask_account:
        form.append(_field("account", TEXT["f_account"],
                           _input("account", typed.get("account", ""), errors, maxlength=41,
                                  extra=' autocapitalize="off" autocorrect="off" spellcheck="false"'),
                           errors, hint=TEXT["f_account_hint"]))
    form.append('<p class="privacy">%s</p>' % e(TEXT["privacy"]))
    form.append('<button type="submit" class="btn btn-primary btn-block" data-submit>%s</button>'
                % e(TEXT["to_confirm"]))
    form.append('</div></section></form>')
    parts.append("".join(form))
    bar = (
        '<div class="bar js-only" id="bar"><div class="bar-in">'
        '<span class="bar-count">%s <b data-count>%d</b> / %d %s</span>'
        '<button type="button" class="btn btn-primary" data-next>%s</button></div></div>\n'
        '<div class="toast" data-toast role="status" aria-live="polite"></div>\n'
    ) % (e(TEXT["bar_picked"]), len(selected), max_items, e(TEXT["bar_unit"]), e(TEXT["next"]))
    return layout(brand, "\n".join(parts), static, "page-pick", bar)


def _items_list(items, sizes):
    rows = []
    for it in items:
        rows.append(
            '<li>%s<div class="it"><b>%s</b><span>%s · %s</span></div></li>' % (
                _img(it["image"], sizes, it["style_name"], "thumb"), e(it["style_name"]),
                e(it["colour"]), e(it["size"])))
    return '<ul class="items">%s</ul>' % "".join(rows)


def _receiver_list(receiver, account):
    """The full receiver (the confirm page, right after she typed it)."""
    phone = receiver.get("phone", "")
    addr = "".join(x for x in (receiver.get("province", ""), _city(receiver),
                               receiver.get("district", ""), receiver.get("address", "")) if x)
    rows = [
        (TEXT["f_name"], receiver.get("name", "")),
        (TEXT["f_phone"], phone),
        (TEXT["f_addr_full"], addr),
    ]
    if account:
        rows.append((TEXT["f_account"], account))
    return '<dl class="kv">%s</dl>' % "".join(
        '<div><dt>%s</dt><dd>%s</dd></div>' % (e(k), e(v)) for k, v in rows)


def _city(receiver):
    # "上海市上海市…" reads oddly: show the city only when it differs.
    city = receiver.get("city", "")
    return "" if city == receiver.get("province") else city


def page_confirm(brand, static, action, items, receiver, account, typed, nonce, sizes):
    hidden = ['<input type="hidden" name="nonce" value="%s">' % e(nonce)]
    for it in items:
        hidden.append('<input type="hidden" name="pick" value="%s">' % e(it["sku"]))
    for key in ("name", "phone", "province", "city", "district", "address", "account"):
        if key == "account" and account is None:
            continue
        value = account if key == "account" else receiver.get(key, typed.get(key, ""))
        hidden.append('<input type="hidden" name="%s" value="%s">' % (key, e(value)))
    body = (
        '<section class="intro intro-tight"><h1 class="greet">%s</h1><p>%s</p></section>'
        '<div class="panel"><h2>%s</h2>%s</div>'
        '<div class="panel"><h2>%s</h2>%s<p class="privacy">%s</p></div>'
        '<form method="post" action="%s" accept-charset="utf-8" class="actions" data-once>%s'
        '<button type="submit" name="action" value="submit" class="btn btn-primary btn-block">%s</button>'
        '<button type="submit" name="action" value="edit" class="btn btn-secondary btn-block">%s</button>'
        '</form>'
    ) % (e(TEXT["confirm_title"]), e(TEXT["confirm_sub"]), e(TEXT["your_picks"]),
         _items_list(items, sizes), e(TEXT["receiver"]),
         _receiver_list(receiver, account), e(TEXT["privacy"]), e(action),
         "".join(hidden), e(TEXT["submit"]), e(TEXT["edit"]))
    return layout(brand, body, static, "page-confirm")


def page_summary(brand, static, sub, sizes, editable, edit_url, keep_link):
    """Her page after submitting. The receiver is masked in every state (the
    link may be forwarded): phone tail and province/city only."""
    receiver = sub["receiver"]
    parts = ['<section class="done"><div class="tick" aria-hidden="true"></div>'
             '<h1 class="done-title">%s</h1>' % e(TEXT["thanks"])]
    if editable:
        parts.append('<p class="done-sub">%s</p>' % e(TEXT["thanks_sub"]))
    parts.append('</section>')
    if not editable:
        parts.append('<p class="notice notice-lock">%s</p>' % e(TEXT["locked"]))
    if keep_link and editable:
        parts.append('<p class="notice">%s</p>' % e(TEXT["keep_link"]))
    parts.append('<div class="panel"><h2>%s</h2>%s</div>' % (
        e(TEXT["your_picks"]), _items_list(sub["items"], sizes)))
    parts.append(
        '<div class="panel"><h2>%s</h2><dl class="kv"><div><dt>%s</dt><dd data-receiver>%s</dd></div></dl>'
        '<p class="hint">%s</p></div>' % (
            e(TEXT["receiver"]), e(TEXT["receiver_short"]), e(receiver_line(receiver)),
            e(TEXT["receiver_note"])))
    if editable:
        parts.append('<div class="actions"><a class="btn btn-secondary btn-block" href="%s">%s</a></div>'
                     % (e(edit_url), e(TEXT["edit_btn"])))
    return layout(brand, "".join(parts), static, "page-summary")


def page_duplicate(brand, static):
    """A form that was submitted already (double tap, replay): no link."""
    return message_page(brand, static, TEXT["thanks"], TEXT["duplicate_sub"], icon="ok")


def page_general_choice(brand, static, own_url, fresh_url):
    """The general link opened again in a browser that submitted through it."""
    body = (
        '<section class="msg msg-choice"><div class="msg-icon msg-icon-link" aria-hidden="true"></div>'
        '<h1 class="msg-title">%s</h1><p class="msg-sub">%s</p></section>'
        '<div class="actions">'
        '<a class="btn btn-primary btn-block" href="%s">%s</a>'
        '<a class="btn btn-secondary btn-block" href="%s">%s</a></div>'
    ) % (e(TEXT["choice_title"]), e(TEXT["choice_sub"]), e(own_url), e(TEXT["choice_mine"]),
         e(fresh_url), e(TEXT["choice_other"]))
    return layout(brand, body, static, "page-msg")
