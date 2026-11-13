"""Catalogue validation, the in-memory catalogue index and image sniffing."""

import hashlib
import re
import struct

CODE_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{2,63}$")
IMAGE_KEY_RE = re.compile(r"^[0-9a-f]{32}$")

MAX_STYLES = 200
MAX_COLOURS = 40
MAX_SIZES = 12
MAX_NAME = 60
MAX_DESC = 120


class CatalogError(ValueError):
    """The catalogue body is invalid; ``str()`` says where and why."""


def has_control_chars(text):
    """True when ``text`` holds a character a creator-facing field must not have."""
    for ch in text:
        o = ord(ch)
        if o < 32 or 0x7F <= o <= 0x9F:
            return True
        if o in (0x2028, 0x2029) or 0x202A <= o <= 0x202E or 0x2066 <= o <= 0x2069:
            return True
        if o == 0xFEFF or 0xD800 <= o <= 0xDFFF:
            return True
    return False


def _text(value, where, limit, required=True):
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise CatalogError("%s: must be a string" % where)
    value = value.strip()
    if required and not value:
        raise CatalogError("%s: must not be empty" % where)
    if len(value) > limit:
        raise CatalogError("%s: longer than %d characters" % (where, limit))
    if has_control_chars(value):
        raise CatalogError("%s: contains control characters" % where)
    return value


def validate_catalog(body):
    """Check a catalogue body; return ``(styles, image_keys)`` normalised.

    ``styles`` keeps the display order; ``image_keys`` is the set of picture
    keys it references.
    """
    if not isinstance(body, dict) or not isinstance(body.get("styles"), list):
        raise CatalogError("body: must be an object with a 'styles' list")
    raw_styles = body["styles"]
    if len(raw_styles) > MAX_STYLES:
        raise CatalogError("styles: more than %d styles" % MAX_STYLES)
    styles = []
    codes = set()
    skus = set()
    keys = set()
    for si, raw in enumerate(raw_styles):
        where = "styles[%d]" % si
        if not isinstance(raw, dict):
            raise CatalogError("%s: must be an object" % where)
        code = raw.get("code")
        if not isinstance(code, str) or not CODE_RE.match(code):
            raise CatalogError("%s.code: must match %s" % (where, CODE_RE.pattern))
        if code in codes:
            raise CatalogError("%s.code: duplicate code %s" % (where, code))
        codes.add(code)
        name = _text(raw.get("name"), where + ".name", MAX_NAME)
        desc = _text(raw.get("desc"), where + ".desc", MAX_DESC, required=False)
        colours_raw = raw.get("colours")
        if not isinstance(colours_raw, list) or not colours_raw:
            raise CatalogError("%s.colours: must be a non-empty list" % where)
        if len(colours_raw) > MAX_COLOURS:
            raise CatalogError("%s.colours: more than %d colours" % (where, MAX_COLOURS))
        colours = []
        colour_names = set()
        for ci, craw in enumerate(colours_raw):
            cwhere = "%s.colours[%d]" % (where, ci)
            if not isinstance(craw, dict):
                raise CatalogError("%s: must be an object" % cwhere)
            cname = _text(craw.get("name"), cwhere + ".name", MAX_NAME)
            if cname in colour_names:
                raise CatalogError("%s.name: duplicate colour in this style" % cwhere)
            colour_names.add(cname)
            image = craw.get("image")
            if not isinstance(image, str) or not IMAGE_KEY_RE.match(image):
                raise CatalogError("%s.image: must be an image key (32 hex characters)" % cwhere)
            keys.add(image)
            sizes_raw = craw.get("sizes")
            if not isinstance(sizes_raw, list) or not sizes_raw:
                raise CatalogError("%s.sizes: must be a non-empty list" % cwhere)
            if len(sizes_raw) > MAX_SIZES:
                raise CatalogError("%s.sizes: more than %d sizes" % (cwhere, MAX_SIZES))
            sizes = []
            labels = set()
            for zi, zraw in enumerate(sizes_raw):
                zwhere = "%s.sizes[%d]" % (cwhere, zi)
                if not isinstance(zraw, dict):
                    raise CatalogError("%s: must be an object" % zwhere)
                label = _text(zraw.get("label"), zwhere + ".label", MAX_NAME)
                if label in labels:
                    raise CatalogError("%s.label: duplicate size in this colour" % zwhere)
                labels.add(label)
                sku = zraw.get("sku")
                if not isinstance(sku, str) or not CODE_RE.match(sku):
                    raise CatalogError("%s.sku: must match %s" % (zwhere, CODE_RE.pattern))
                if sku in skus:
                    raise CatalogError("%s.sku: duplicate sku %s" % (zwhere, sku))
                skus.add(sku)
                sizes.append({"label": label, "sku": sku})
            colours.append({"name": cname, "image": image, "sizes": sizes})
        style = {"code": code, "name": name, "colours": colours}
        if desc:
            style["desc"] = desc
        styles.append(style)
    return styles, keys


class Catalog(object):
    """The current catalogue with an index from sku to its place."""

    def __init__(self, version, updated_at, styles):
        self.version = version
        self.updated_at = updated_at
        self.styles = styles
        self.by_sku = {}
        for si, style in enumerate(styles):
            for ci, colour in enumerate(style["colours"]):
                for zi, size in enumerate(colour["sizes"]):
                    self.by_sku[size["sku"]] = (si, ci, zi)

    @property
    def sku_count(self):
        return len(self.by_sku)

    def snapshot(self, sku):
        """What a submission keeps for one chosen sku (None when unknown)."""
        place = self.by_sku.get(sku)
        if place is None:
            return None
        style = self.styles[place[0]]
        colour = style["colours"][place[1]]
        size = colour["sizes"][place[2]]
        return {
            "sku": sku,
            "style": style["code"],
            "style_name": style["name"],
            "colour": colour["name"],
            "size": size["label"],
            "image": colour["image"],
        }


def image_key(data):
    """The key a picture must be uploaded under: first 32 hex of its sha256."""
    return hashlib.sha256(data).hexdigest()[:32]


def sniff_image(data):
    """Return ``(mime, width, height)`` for JPEG/PNG/WebP bytes, else None.

    Width/height are 0 when the header cannot be read far enough.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        w = h = 0
        if len(data) >= 24 and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
        return "image/png", w, h
    if data.startswith(b"\xff\xd8\xff"):
        return ("image/jpeg",) + _jpeg_size(data)
    if len(data) >= 16 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("image/webp",) + _webp_size(data)
    return None


def _jpeg_size(data):
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            break
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 <= n:
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            break
        if seg_len < 2:
            break
        i += 2 + seg_len
    return 0, 0


def _webp_size(data):
    chunk = data[12:16]
    try:
        if chunk == b"VP8X" and len(data) >= 30:
            w = 1 + int.from_bytes(data[24:27], "little")
            h = 1 + int.from_bytes(data[27:30], "little")
            return w, h
        if chunk == b"VP8 " and len(data) >= 30:
            w, h = struct.unpack("<HH", data[26:30])
            return w & 0x3FFF, h & 0x3FFF
        if chunk == b"VP8L" and len(data) >= 25:
            b = data[21:25]
            w = 1 + (((b[1] & 0x3F) << 8) | b[0])
            h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
            return w, h
    except Exception:
        pass
    return 0, 0
