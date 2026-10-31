"""A creator's contact for 加百应达人微信 (src/prm/contact.py): what counts as her 微信号 / 手机号, what never does, and
the masked forms everything outside friend_adds shows.  Every value here is made up."""

import pytest

from src.prm import contact as C
from src.prm.contact import ContactError, find_contacts, mask_contacts, masked, masked_contact, masked_line


def kinds(text):
    return [(f["kind"], f["value"]) for f in find_contacts(text)]


# ── 微信号: only after a label ─────────────────────────────────────────────────

@pytest.mark.parametrize("text, label", [
    ("微信：abc_123", "微信"), ("微信号是 Abc-12345", "微信号"), ("加我微信abcdef1谢谢", "微信"), ("微信ID: abcdef1", "微信ID"),
    ("vx: abcdef1", "vx"), ("VX abcdef1", "VX"), ("我的vx是abcdef1", "vx"), ("wx号：abcdef1", "wx号"), ("WX:abcdef1", "WX"),
    ("v信 abcdef1", "v信"), ("V信：abcdef1", "V信"), ("薇：abcdef1", "薇"), ("威 abcdef1", "威"), ("加V：abcdef1", "加V"),
    ("➕V abcdef1", "➕V"), ("+v abcdef1", "+v"), ("加微 abcdef1", "加微"), ("微信「abcdef1」", "微信"),
])
def test_a_wechat_id_after_a_label_is_found(text, label):
    [found] = find_contacts(text)
    assert (found["kind"], found["value"], found["label"]) == ("wechat", text[found["start"]:found["end"]], label)
    assert found["value"].startswith(("abc", "Abc"))


@pytest.mark.parametrize("text", [
    "abcdef1 是我的账号",                         # no label: an unlabelled latin string is never taken
    "please send me the sample tomorrow",        # English words
    "https://example.com/item?id=abcdef123",     # a link
    "看这个 https://example.com/s?wx=abcdef12",   # a label inside a link is no label
    "货号 HAT-2026-001 和 SKU A1234567",           # product codes
    "商品 3000000000000000101",                    # a product id
    "订单号 692012345678901234",                   # an order number holds no mobile number
    "抖音号 dy_demo01，我的抖音 dy_demo01",         # her 抖音号 is no 微信号
    "wxid_abcdef123456",                          # a wxid without a label
    "vxlan abcdef1", "加Vip abcdef1",              # latin labels are words of their own
    "微信：abc12",                                 # too short (6-20)
    "微信：a12345678901234567890",                 # too long
    "微信：1abcdef",                               # must start with a letter
    "微信同号",                                    # no number after it
])
def test_what_is_never_a_contact(text):
    assert find_contacts(text) == []
    assert mask_contacts(text) == text


def test_the_wechat_rule_takes_6_to_20_characters_starting_with_a_letter():
    assert kinds("微信：a12345") == [("wechat", "a12345")]
    assert kinds("微信：a1234567890123456789") == [("wechat", "a1234567890123456789")]
    assert kinds("微信：ab_cd-ef 谢谢") == [("wechat", "ab_cd-ef")]


# ── 手机号: anywhere ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["13812345678", "138 1234 5678", "138-1234-5678", "+86 138 1234 5678", "+8613812345678",
                                  "86-13812345678", "电话：13812345678", "我的手机是13812345678哦"])
def test_a_mobile_number_in_any_writing_is_found(text):
    assert kinds(text) == [("phone", "13812345678")]


@pytest.mark.parametrize("text, label", [("电话：13812345678", "电话"), ("微信同号13812345678", "微信同号"),
                                         ("微信：13812345678", "微信"), ("vx13812345678", "vx"), ("13812345678", "")])
def test_a_number_says_what_stood_before_it(text, label):
    [found] = find_contacts(text)
    assert (found["kind"], found["value"], found["label"]) == ("phone", "13812345678", label)
    assert text[found["start"]:found["end"]].endswith("13812345678")


@pytest.mark.parametrize("text", ["12812345678", "1381234567", "138123456789", "a13812345678b"])
def test_what_is_no_mobile_number(text):
    assert find_contacts(text) == []


def test_both_in_one_line_in_order_and_masked_in_place():
    text = "微信：abcdefyz，电话 13800000000，抖音号 dy_demo01"
    assert kinds(text) == [("wechat", "abcdefyz"), ("phone", "13800000000")]
    assert mask_contacts(text) == "微信：ab****yz，电话 138****0000，抖音号 dy_demo01"


# ── the masked forms ──────────────────────────────────────────────────────────

def test_masked_forms():
    """The 百应 inbox's rule too (service.buyin_mask_wechat / buyin_mask_phone wrap these)."""
    assert masked("wechat", "abcdefyz") == "ab****yz"
    assert masked("phone", "13800000000") == "138****0000"
    assert masked("wechat", "") == "" and masked("wechat", "abc") == "****" and masked("wechat", " abcd ") == "****"
    assert masked("wechat", "abcde") == "ab****de"
    assert masked("phone", "1234567") == "****" and masked("phone", "+8613800000000") == "+86****0000"
    assert masked_contact({"wechat": "abcdefyz", "phone": "13800000000", "unavailable": False}) == {
        "wechat": "ab****yz", "phone": "138****0000"}
    assert masked_contact(None) == {}


def test_the_card_line_is_the_one_the_baiying_inbox_writes():
    """webot-04 (2026-10-29): 「[联系方式卡片] 微信号 ab****yz、手机 138****0001（加微信 #12）」."""
    assert masked_line({"wechat": "abcdefyz", "phone": "13800000001"}, 12) == (
        "[联系方式卡片] 微信号 ab****yz、手机 138****0001（加微信 #12）")
    assert masked_line({"phone": "13800000001"}) == "[联系方式卡片] 手机 138****0001"
    assert masked_line({"unavailable": True, "why": "read-failed"}, 3) == "[联系方式卡片]（这次没读到号码）（加微信 #3）"
    assert masked_line({"unavailable": True, "why": "own"}) == "[联系方式卡片]（卡片上只有店铺自己的联系方式）"
    assert masked_line({"unavailable": True, "wechat": "abcdefyz"}) == "[联系方式卡片]（这次没读到号码）"   # not shown
    assert masked_line(None) == masked_line({"wechat": "  "}) == "[联系方式卡片]（这次没读到号码）"
    assert masked_line({"wechat": "abcdefyz"}, head="[她打字留的]") == "[她打字留的] 微信号 ab****yz"
    assert masked_line({"wechat": "abcdefyz"}, 0).endswith("（加微信 #0）")


def test_the_first_contact_of_each_kind_she_typed():
    assert C.first_contacts("我微信 abc_12345，电话 13800000000，备用 13900000000，vx: zzz_99999") == {
        "wechat": "abc_12345", "phone": "13800000000"}
    assert C.first_contacts("手机 138 0000 0000") == {"phone": "13800000000"}
    assert C.first_contacts("我的抖音号 abcdef123，链接 https://example.com/vx/abc123456") == {}
    assert C.first_contacts("") == {} and C.first_contacts(None) == {}


def test_has_masked():
    assert C.has_masked("小鹿 138****0000") and C.has_masked("ab****yz")
    assert not C.has_masked("小鹿🌸") and not C.has_masked("")


# ── what an operator types into the console ───────────────────────────────────

def test_parse_contact_normalizes_what_an_operator_types():
    assert C.parse_contact("phone", " +86 138-1234-5678 ") == "13812345678"
    assert C.parse_contact("wechat", "  Ab_cd-123 ") == "Ab_cd-123"
    assert C.parse_contact("wechat", "") == "" and C.parse_contact("phone", None) == ""
    assert C.parse_contacts({"wechat": "abcdef1", "phone": "", "other": "x"}) == {"wechat": "abcdef1"}


@pytest.mark.parametrize("kind, value, word", [
    ("wechat", "1abcdef", "字母开头"), ("wechat", "abc", "6–20"), ("wechat", "abc def 123", "6–20"),
    ("wechat", "13812345678", "手机号"), ("wechat", "a" * 21, "6–20"), ("phone", "12345", "11 位"),
    ("phone", "12812345678", "11 位"), ("qq", "123456", "不认识"),
])
def test_parse_contact_says_why_a_value_is_none(kind, value, word):
    with pytest.raises(ContactError) as e:
        C.parse_contact(kind, value)
    assert word in str(e.value)


def test_parse_contacts_stops_at_the_first_bad_value():
    with pytest.raises(ContactError):
        C.parse_contacts({"wechat": "abcdef1", "phone": "123"})
