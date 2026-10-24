"""从达人消息里提取索样信息（src/influencer/extract.py）。"""

import pytest

from src.influencer.extract import extract_request_info


@pytest.mark.parametrize("text", [
    "张三 13812345678 上海市浦东新区世纪大道100号",
    "收件人：张三，电话：13812345678，地址：上海市浦东新区世纪大道100号",
    "上海市浦东新区世纪大道100号 张三 13812345678",
    "张三，13812345678，上海市浦东新区世纪大道100号",
])
def test_common_ways_of_writing_the_recipient(text):
    info = extract_request_info(text)
    assert info.recipient_name == "张三"
    assert info.recipient_phone == "13812345678"
    assert info.shipping_address == "上海市浦东新区世纪大道100号"


def test_a_labelled_name_right_before_the_phone():
    info = extract_request_info("收件人张三电话13812345678")
    assert info.recipient_name == "张三"
    assert info.recipient_phone == "13812345678"


def test_a_lone_name_counts_only_when_the_name_was_asked_for():
    assert extract_request_info("李四").recipient_name == ""
    assert extract_request_info("李四", expecting_name=True).recipient_name == "李四"


@pytest.mark.parametrize("reply", ["好的", "好的谢谢", "稍等", "收到啦"])
def test_short_replies_are_not_names_even_when_a_name_was_asked_for(reply):
    assert extract_request_info(reply, expecting_name=True).recipient_name == ""


def test_digits_inside_a_longer_number_are_not_a_phone():
    assert extract_request_info("订单号 2023138123456789").recipient_phone == ""


def test_a_profile_link_is_picked_up_and_kept_out_of_the_address():
    link = "https://www.xiaohongshu.com/user/profile/5f0000000000000000000001"
    info = extract_request_info(f"张三 13812345678 上海市浦东新区世纪大道100号 小红书主页 {link}")
    assert info.xhs_account == link
    assert info.shipping_address == "上海市浦东新区世纪大道100号"
    assert info.recipient_name == "张三"


def test_an_xhs_number_is_picked_up():
    assert extract_request_info("小红书号：abc12345").xhs_account == "abc12345"


def test_the_most_specific_product_wins():
    assert extract_request_info("想要VC精华液的样品").requested_product == "VC精华液"
    assert extract_request_info("想试试面膜").requested_product == "面膜"
