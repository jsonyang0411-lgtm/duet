"""达人模块的数据存储（src/influencer/store.py）。"""

import time

import pytest

from src.db.schema import initialize_db
from src.influencer.settings import DRAFT_TTL_SECONDS, PERSONAL_INFO_RETENTION_SECONDS
from src.influencer.store import SOURCE_BOT, SOURCE_CONSOLE, SOURCE_ENV, InfluencerStore
from src.influencer.types import ApprovalActionPacket, GateStatus, SampleRequestDraft

NEXT_YEAR = time.time() + 365 * 24 * 3600


def make_packet(packet_id="GATE-1", chat_id="wxid_a", **overrides):
    values = dict(
        packet_id=packet_id, influencer_id=chat_id, influencer_name="小美", follower_count=None,
        requested_product="眼霜", sku_id="SKU-眼霜", stock_available=100, sample_cost=18.5,
        proposed_commission_rate=0.25, shipping_address="上海市浦东新区世纪大道100号",
        what_cannot_be_undone="建出库单，绑定佣金", recipient_name="张三",
        recipient_phone="13812345678", evidence="想申请眼霜样品",
    )
    values.update(overrides)
    return ApprovalActionPacket(**values)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "messages.db")


# ---- 审批单 ----

def test_an_approval_survives_a_restart(db_path):
    packet = make_packet(xhs_account="xhs_1", qualification="达标", follower_count=35000)
    InfluencerStore(db_path).add_approval(packet, source=SOURCE_BOT)
    assert InfluencerStore(db_path).get_approval("GATE-1") == packet


def test_only_one_of_two_claims_wins_even_across_store_instances(db_path):
    InfluencerStore(db_path).add_approval(make_packet(), source=SOURCE_BOT)
    first, second = InfluencerStore(db_path), InfluencerStore(db_path)
    assert first.transition("GATE-1", GateStatus.PENDING, GateStatus.EXECUTING) is True
    assert second.transition("GATE-1", GateStatus.PENDING, GateStatus.EXECUTING) is False
    assert second.get_approval("GATE-1").status == GateStatus.EXECUTING


def test_a_handled_packet_records_when_it_was_handled():
    store = InfluencerStore(":memory:")
    store.add_approval(make_packet(), source=SOURCE_BOT)
    store.transition("GATE-1", GateStatus.PENDING, GateStatus.REJECTED)
    assert store.get_approval("GATE-1").handled_at is not None


def test_pending_only_updates_leave_handled_packets_alone():
    store = InfluencerStore(":memory:")
    store.add_approval(make_packet(), source=SOURCE_BOT)
    assert store.update_pending("GATE-1", xhs_account="xhs_1") is True
    store.transition("GATE-1", GateStatus.PENDING, GateStatus.REJECTED)
    assert store.update_pending("GATE-1", xhs_account="xhs_2") is False
    assert store.get_approval("GATE-1").xhs_account == "xhs_1"


def test_unknown_fields_are_refused():
    store = InfluencerStore(":memory:")
    store.add_approval(make_packet(), source=SOURCE_BOT)
    with pytest.raises(ValueError):
        store.update_pending("GATE-1", **{"status = 'FULFILLED' --": "x"})


def test_approvals_are_listed_newest_first_and_filtered_by_status():
    store = InfluencerStore(":memory:")
    store.add_approval(make_packet("GATE-A", created_at=1.0, expires_at=NEXT_YEAR), source=SOURCE_BOT)
    store.add_approval(make_packet("GATE-B", created_at=2.0, expires_at=NEXT_YEAR), source=SOURCE_BOT)
    store.transition("GATE-A", GateStatus.PENDING, GateStatus.REJECTED)
    assert [p.packet_id for p in store.list_approvals()] == ["GATE-B", "GATE-A"]
    assert [p.packet_id for p in store.list_approvals([GateStatus.REJECTED])] == ["GATE-A"]
    assert store.count_by_status() == {GateStatus.PENDING.value: 1, GateStatus.REJECTED.value: 1}


def test_every_step_of_a_packet_is_on_record():
    store = InfluencerStore(":memory:")
    store.add_approval(make_packet(), source=SOURCE_BOT)
    store.add_event("驳回", packet_id="GATE-1", detail="粉丝太少", source=SOURCE_CONSOLE)
    events = store.events_for(["GATE-1"])["GATE-1"]
    assert [(e["action"], e["source"], e["detail"]) for e in events] == [
        ("提交审批", SOURCE_BOT, "眼霜"), ("驳回", SOURCE_CONSOLE, "粉丝太少"),
    ]


# ---- 草稿 ----

def test_a_draft_survives_a_restart(db_path):
    draft = SampleRequestDraft(
        chat_id="wxid_a", product="眼霜", recipient_phone="13812345678",
        inventory={"sku_id": "SKU-眼霜", "available_quantity": 3},
        asked_for=("recipient_name",), evidence=["想申请眼霜样品"],
    )
    InfluencerStore(db_path).save_draft(draft)
    assert InfluencerStore(db_path).get_draft("wxid_a") == draft


# ---- 达人名单 ----

def test_the_environment_whitelist_only_ever_adds(db_path):
    store = InfluencerStore(db_path)
    assert store.add_contacts(["wxid_a"], source=SOURCE_ENV) == ["wxid_a"]
    store.save_contact("wxid_a", enabled=False, source=SOURCE_CONSOLE)
    assert store.add_contacts(["wxid_a", "wxid_b"], source=SOURCE_ENV) == ["wxid_b"]
    assert not store.is_influencer("wxid_a"), "控制台里停用的达人不能被环境变量加回来"
    assert store.is_influencer("wxid_b")


def test_the_bot_does_not_overwrite_what_the_operator_entered():
    store = InfluencerStore(":memory:")
    store.save_contact("wxid_a", xhs_account="xhs_operator", source=SOURCE_CONSOLE)
    store.record_profile(
        "wxid_a", nickname="小美", xhs_account="xhs_other", follower_count=500, recent_like_count=10,
    )
    contact = store.get_contact("wxid_a")
    assert contact["xhs_account"] == "xhs_operator"
    assert contact["display_name"] == "小美"
    assert contact["follower_count"] is None, "数据属于另一个账号，不能记在这个账号名下"


def test_recent_private_chats_leave_out_groups_and_system_chats(db_path):
    conn = initialize_db(db_path)
    conn.executemany(
        "INSERT INTO messages (message_id, chat_id, sender_id, sender_name, content, timestamp)"
        " VALUES (?, ?, ?, ?, '', ?)",
        [
            ("m1", "wxid_a", "wxid_a", "小美", 100),
            ("m2", "123@chatroom", "wxid_c", "群友", 200),
            ("m3", "gh_abc", "gh_abc", "某公众号", 300),
            ("m4", "filehelper", "filehelper", "文件传输助手", 400),
            ("m5", "wxid_b", "wxid_b", "阿杰", 500),
            ("m6", "wxid_a", "wxid_a", "小美美", 600),
        ],
    )
    conn.commit()
    conn.close()
    store = InfluencerStore(db_path)
    store.add_contacts(["wxid_b"], source=SOURCE_CONSOLE)
    chats = store.recent_private_chats()
    assert [c["chat_id"] for c in chats] == ["wxid_a", "wxid_b"]
    assert (chats[0]["name"], chats[0]["message_count"], chats[0]["in_list"]) == ("小美美", 2, False)
    assert chats[1]["in_list"] is True


def test_recent_private_chats_before_the_bot_ever_ran():
    assert InfluencerStore(":memory:").recent_private_chats() == []


# ---- 维护 ----

def test_an_overdue_packet_is_marked_expired():
    store = InfluencerStore(":memory:")
    store.add_approval(make_packet(expires_at=time.time() - 1), source=SOURCE_BOT)
    store.run_maintenance()
    assert store.get_approval("GATE-1").status == GateStatus.EXPIRED
    assert store.events_for(["GATE-1"])["GATE-1"][-1]["action"] == "过期"


def test_personal_info_is_cleared_90_days_after_a_packet_is_handled():
    store = InfluencerStore(":memory:")
    store.add_approval(make_packet("GATE-DONE", expires_at=NEXT_YEAR), source=SOURCE_BOT)
    store.add_approval(make_packet("GATE-WAITING", expires_at=NEXT_YEAR), source=SOURCE_BOT)
    handled = time.time()
    store.transition("GATE-DONE", GateStatus.PENDING, GateStatus.REJECTED, handled_at=handled)

    store.run_maintenance(handled + PERSONAL_INFO_RETENTION_SECONDS - 60)
    assert store.get_approval("GATE-DONE").recipient_phone == "13812345678"

    store.run_maintenance(handled + PERSONAL_INFO_RETENTION_SECONDS + 60)
    done = store.get_approval("GATE-DONE")
    assert (done.recipient_name, done.recipient_phone, done.shipping_address, done.evidence) == ("", "", "", "")
    assert done.personal_info_cleared_at is not None
    assert (done.requested_product, done.status) == ("眼霜", GateStatus.REJECTED), "统计信息要留着"
    assert store.get_approval("GATE-WAITING").recipient_phone == "13812345678", "没处理完的单不清"


def test_abandoned_drafts_are_deleted():
    store = InfluencerStore(":memory:")
    store.save_draft(SampleRequestDraft(chat_id="wxid_a", updated_at=time.time() - DRAFT_TTL_SECONDS - 60))
    store.save_draft(SampleRequestDraft(chat_id="wxid_b"))
    store.run_maintenance()
    assert store.get_draft("wxid_a") is None
    assert store.get_draft("wxid_b") is not None
