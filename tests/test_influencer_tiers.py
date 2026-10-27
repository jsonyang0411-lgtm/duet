"""达人档位（D30）、主播类型（D36）和给起草 Claude 的达人资料文件（2026-10-26 第 2 步）。
聚水潭命令行用替身，不发真实请求。"""

import time

import pytest

from src.influencer import jst_erp, tiers
from src.influencer.jst_erp import ERPNotReadyError
from src.influencer.prm_bridge import PROFILE_FILE, PartyListSync, profile_text
from src.influencer.store import InfluencerStore
from src.influencer.tiers import TIER_ANY, TIER_ASK_LIVE, TIER_NO_SALES, TIER_THREE, look_up, tier_of


def band(lo, hi=None):
    return {"available": True, "min": lo, "max": hi, "label": f"{lo}~{hi}"}


@pytest.mark.parametrize("lower,expected", [
    (50_000, TIER_THREE), (100_000, TIER_THREE), (250_000, TIER_ASK_LIVE),
    (500_000, TIER_ASK_LIVE), (1_000_000, TIER_ANY), (10_000_000, TIER_ANY),
])
def test_douyin_tiers_by_the_lower_end_of_30_day_sales(lower, expected):
    assert tier_of("DY", {"sales30d": band(lower)})[0] == expected


@pytest.mark.parametrize("lower,expected", [
    (10_000, TIER_THREE), (250_000, TIER_THREE), (500_000, TIER_ASK_LIVE), (1_000_000, TIER_ANY),
])
def test_xiaohongshu_tiers_by_the_live_average(lower, expected):
    # 小红书看直播场均销售额，线是 50 万 / 100 万；有近 30 日销售总额也不看
    assert tier_of("XHS", {"liveAvg": band(lower), "sales30d": band(9_999_999)})[0] == expected


def test_no_sales_data_is_its_own_tier():
    assert tier_of("DY", {"sales30d": {"available": False, "label": "无数据"}})[0] == TIER_NO_SALES
    assert tier_of("XHS", {"liveAvg": None})[0] == TIER_NO_SALES


def fake_cli(monkeypatch, answer=None, error=None):
    calls = []

    def run(*args):
        calls.append(args)
        if error:
            raise ERPNotReadyError(error)
        return answer

    monkeypatch.setattr(tiers, "run_jst_cli", run)
    return calls


def test_look_up_gives_tier_channel_and_where_the_numbers_came_from(monkeypatch):
    calls = fake_cli(monkeypatch, {"found": True, "daren": {
        "sales30d": {"available": True, "min": 2_500_000, "max": 5_000_000, "label": "250万~500万"},
        "channelBySales": "视频", "level": 3, "uid": "10000000001",
    }})
    result = look_up("dy", " test_daren01 ")
    assert calls == [("lookup", "--platform", "DY", "--no", "test_daren01")]
    assert (result["ok"], result["tier"], result["channel"], result["level"]) == (True, TIER_ANY, "视频", 3)
    assert "近 30 日销售总额 250万~500万" in result["sales_label"] and "聚水潭" in result["sales_label"]
    assert result["uid"] == "10000000001"                                  # 开抖音专属佣金要用


def test_a_uid_that_is_not_digits_is_dropped(monkeypatch):
    fake_cli(monkeypatch, {"found": True, "daren": {"sales30d": None, "uid": "v2_abc"}})
    assert look_up("dy", "x")["uid"] == ""


@pytest.mark.parametrize("platform,number,answer,error,expected", [
    ("", "", None, None, "名单上没有"),
    ("DY", "x", {"found": False}, None, "达人广场里没有"),
    ("DY", "x", None, "登录失效", "聚水潭查不了"),
])
def test_what_cannot_be_looked_up_says_why(monkeypatch, platform, number, answer, error, expected):
    fake_cli(monkeypatch, answer, error)
    result = look_up(platform, number)
    assert result["ok"] is False and result["tier"] == "" and expected in result["note"]


# ---- 名单 + 资料文件 ----

class FakePrmStore:
    def __init__(self, parties):
        self._parties = parties

    def parties(self, include_inactive=False):
        return [p for p in self._parties if include_inactive or p["active"]]

    def active_wxids(self):
        return [p["wxid"] for p in self._parties if p["active"]]


class FakePrm:
    def __init__(self, parties):
        self.store = FakePrmStore(parties)

    def register(self, wxid, name, note):
        raise AssertionError("测试里名单和 PRM 本来就对得上")

    def set_active(self, party_id, active):
        raise AssertionError("测试里名单和 PRM 本来就对得上")


@pytest.fixture
def setup(tmp_path):
    store = InfluencerStore(":memory:")
    store.save_contact("wxid_a", source="t", display_name="测试达人", platform="DY", platform_no="test_daren01")
    store.save_contact("wxid_b", source="t", display_name="没号码")
    prm = FakePrm([{"id": 7, "wxid": "wxid_a", "active": 1}, {"id": 8, "wxid": "wxid_b", "active": 1}])
    workspace = tmp_path / "ws"
    workspace.mkdir()
    refreshed = []

    def refresh(store_, chat_id, now=None):
        refreshed.append(chat_id)
        store_.record_daren_profile(chat_id, tier=TIER_ANY, channel="视频",
                                    sales_label="近 30 日销售总额 250万~500万（聚水潭，10-26 查）", level=3, note="",
                                    uid="10000000001", now=now)

    sync = PartyListSync(store, prm, workspace=workspace, refresh=refresh)
    return store, sync, workspace, refreshed


def test_sync_does_not_ask_jushuitan(setup):
    """名单合一在 edge 后端收消息的循环里跑：不查聚水潭（聚水潭慢会把整个循环卡住），资料由后台线程查。"""
    store, sync, workspace, refreshed = setup
    sync.sync()
    assert refreshed == [] and not (workspace / PROFILE_FILE.format(party_id=7)).exists()


def test_the_profile_thread_refreshes_and_stops(setup, monkeypatch):
    import src.influencer.prm_bridge as bridge
    store, sync, workspace, refreshed = setup
    monkeypatch.setattr(bridge, "PROFILE_LOOP_SEC", 0.05)
    sync.start()
    try:
        deadline = time.time() + 3
        while not (workspace / PROFILE_FILE.format(party_id=7)).exists() and time.time() < deadline:
            time.sleep(0.02)
    finally:
        sync.stop()
    assert refreshed == ["wxid_a"] and not sync.running


def test_refresh_profiles_checks_one_due_creator_and_writes_the_profile_files(setup):
    store, sync, workspace, refreshed = setup
    sync.refresh_profiles()
    assert refreshed == ["wxid_a"], "只有填了号码的才查；一轮最多查一位"
    a = (workspace / PROFILE_FILE.format(party_id=7)).read_text(encoding="utf-8")
    assert "test_daren01" in a and "档位：不限" in a and "主播类型：视频" in a and "LV3" in a
    assert "抖音 UID：10000000001" in a
    b = (workspace / PROFILE_FILE.format(party_id=8)).read_text(encoding="utf-8")
    assert "还没有这位达人的平台" in b
    sync.refresh_profiles()
    assert refreshed == ["wxid_a"], "查过的 24 小时内不再查"


def test_changing_the_number_makes_it_due_again(setup):
    store, sync, workspace, refreshed = setup
    sync.refresh_profiles()
    store.save_contact("wxid_a", source="t", platform_no="new_no")
    assert store.get_contact("wxid_a")["tier"] == "" and store.get_contact("wxid_a")["profile_checked_at"] is None
    assert store.get_contact("wxid_a")["platform_uid"] == "", "换了号码，原来的 UID 也作废"
    sync.refresh_profiles()
    assert refreshed == ["wxid_a", "wxid_a"]


def test_profile_text_has_no_personal_information():
    text = profile_text({"platform": "DY", "platform_no": "dy1", "tier": TIER_THREE, "channel": "直播",
                         "sales_label": "近 30 日销售总额 5万~10万", "level": 2,
                         "display_name": "张三", "chat_id": "wxid_secret"}, 0)
    assert "张三" not in text and "wxid_secret" not in text and "只读" in text


def test_no_workspace_means_no_files_and_no_error(tmp_path, monkeypatch):
    store = InfluencerStore(":memory:")
    store.save_contact("wxid_a", source="t", platform="DY", platform_no="x")
    sync = PartyListSync(store, FakePrm([{"id": 1, "wxid": "wxid_a", "active": 1}]),
                         refresh=lambda *a, **k: None)
    sync.sync()
    sync.refresh_profiles()  # conftest 把 PRM_WORKSPACE 指到不存在的目录：不写文件，也不报错
