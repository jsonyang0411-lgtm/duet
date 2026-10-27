"""从达人的话里认小红书账号（src/influencer/extract.py 的 extract_xhs_account，prm_bridge 用它读合作单）。

收件信息、商品的提取是旧的规则对话用的，2026-10-26 第 8 步删了（收件信息在 PRM 合作单上）。
要求提高佣金的识别在 tests/test_influencer_commission.py。"""

from src.influencer.extract import extract_xhs_account


def test_a_profile_link_is_picked_up():
    link = "https://www.xiaohongshu.com/user/profile/5f0000000000000000000001"
    assert extract_xhs_account(f"张三 13812345678 上海市浦东新区世纪大道100号 小红书主页 {link}") == link
    assert extract_xhs_account("https://xhslink.com/a/AbC123") == "https://xhslink.com/a/AbC123"


def test_an_xhs_number_is_picked_up():
    assert extract_xhs_account("小红书号：abc12345") == "abc12345"
    assert extract_xhs_account("好的谢谢老师") == ""
