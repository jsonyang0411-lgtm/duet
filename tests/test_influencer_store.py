"""达人模块的数据存储（src/influencer/store.py）。"""

import time

import pytest

from src.db.schema import initialize_db
from src.influencer.settings import DRAFT_TTL_SECONDS, PERSONAL_INFO_RETENTION_SECONDS
from src.influencer.store import SOURCE_CONSOLE, SOURCE_ENV, InfluencerStore, open_store


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "messages.db")


def enabled(store, chat_id):
    return (store.get_contact(chat_id) or {}).get("enabled") is True


# ---- 名单 ----

def test_the_environment_whitelist_only_ever_adds(db_path):
    store = InfluencerStore(db_path)
    assert store.add_contacts(["wxid_a"], source=SOURCE_ENV) == ["wxid_a"]
    store.save_contact("wxid_a", enabled=False, source=SOURCE_CONSOLE)
    assert store.add_contacts(["wxid_a", "wxid_b"], source=SOURCE_ENV) == ["wxid_b"]
    assert not enabled(store, "wxid_a"), "控制台里停用的达人不能被环境变量加回来"
    assert enabled(store, "wxid_b")


def test_opening_the_store_takes_the_environment_list(db_path, monkeypatch):
    monkeypatch.setenv("INFLUENCER_CHAT_IDS", "wxid_a, wxid_b")
    store = open_store(db_path)
    assert {c["chat_id"] for c in store.list_contacts()} == {"wxid_a", "wxid_b"}


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

OLD_APPROVAL = (
    "INSERT INTO influencer_approvals (packet_id, influencer_id, requested_product, sku_id,"
    " proposed_commission_rate, shipping_address, recipient_name, recipient_phone, status, created_at, expires_at)"
    " VALUES (?, 'wxid_a', '眼霜', 'SKU-1', 0.2, '上海市浦东新区世纪大道100号', '张三', '13812345678',"
    " 'PENDING_APPROVAL', ?, ?)"
)


def test_old_approvals_and_their_records_are_deleted_90_days_after_they_were_made():
    # 寄样审批单 2026-10-27 起不再写入（寄样单走 PRM 的 jst.sample_order）；老库里的连同收件信息到期删掉
    store = InfluencerStore(":memory:")
    now = time.time()
    old, new = now - PERSONAL_INFO_RETENTION_SECONDS - 60, now - 60
    with store._connect() as conn:
        conn.executemany(OLD_APPROVAL, [("GATE-OLD", old, old + 3600), ("GATE-NEW", new, new + 3600)])
        conn.executemany(
            "INSERT INTO influencer_events (packet_id, chat_id, action, detail, source, created_at)"
            " VALUES (?, 'wxid_a', '提交审批', '眼霜', '机器人', ?)", [("GATE-OLD", old), ("GATE-NEW", new)],
        )
    store.save_contact("wxid_a", source=SOURCE_CONSOLE)        # 名单的操作记录不跟着删
    store.run_maintenance(now)
    with store._connect() as conn:
        assert [r[0] for r in conn.execute("SELECT packet_id FROM influencer_approvals")] == ["GATE-NEW"]
        assert [r[0] for r in conn.execute("SELECT packet_id FROM influencer_events WHERE packet_id != ''")] == ["GATE-NEW"]
        assert conn.execute("SELECT COUNT(*) FROM influencer_events WHERE action = '加入名单'").fetchone()[0] == 1
    store.run_maintenance(now + PERSONAL_INFO_RETENTION_SECONDS)
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM influencer_approvals").fetchone()[0] == 0
        assert "13812345678" not in str(conn.execute("SELECT * FROM influencer_events").fetchall())


def test_leftovers_of_the_old_dialogue_in_an_old_database_are_deleted_in_time():
    # 旧的规则对话、直接回复、发送队列都删了（第 8 步），不再写这三张表；老库里留下的照样到期删
    store = InfluencerStore(":memory:")
    now = time.time()
    with store._connect() as conn:
        conn.executemany("INSERT INTO influencer_drafts (chat_id, draft_json, updated_at) VALUES (?, '{}', ?)",
                         [("wxid_old", now - DRAFT_TTL_SECONDS - 60), ("wxid_new", now)])
        conn.executemany("INSERT INTO influencer_dialog (chat_id, incoming, created_at) VALUES (?, '地址……', ?)",
                         [("wxid_old", now - PERSONAL_INFO_RETENTION_SECONDS - 60), ("wxid_new", now)])
        conn.executemany("INSERT INTO influencer_outbox (chat_id, text, created_at) VALUES (?, '……', ?)",
                         [("wxid_old", now - PERSONAL_INFO_RETENTION_SECONDS - 60), ("wxid_new", now)])
    store.run_maintenance()
    with store._connect() as conn:
        for table in ("influencer_drafts", "influencer_dialog", "influencer_outbox"):
            left = [r[0] for r in conn.execute(f"SELECT chat_id FROM {table}")]
            assert left == ["wxid_new"], table


def test_inbox_items_are_handled_once_and_deleted_90_days_later():
    store = InfluencerStore(":memory:")
    store.save_contact("wxid_a", display_name="小美", source=SOURCE_CONSOLE)
    item_id = store.add_inbox_item("wxid_a", "draft", "老师好", auto_replied=True)
    [item] = store.list_inbox()
    assert (item["display_name"], item["auto_replied"]) == ("小美", True)
    assert store.count_open_inbox() == 1
    assert store.mark_inbox_handled(item_id, source=SOURCE_CONSOLE) is True
    assert store.mark_inbox_handled(item_id, source=SOURCE_CONSOLE) is False
    assert store.count_open_inbox() == 0
    store.run_maintenance(time.time() + PERSONAL_INFO_RETENTION_SECONDS + 60)
    assert store.list_inbox(handled=True) == []


def test_an_old_database_gets_the_new_columns_without_losing_rows(tmp_path):
    # 第 2 步（2026-10-26）加了平台、号码、档位等列：老库启动时补上，原来的数据不动
    import sqlite3

    from src.influencer.store import _ADDED_COLUMNS

    db = tmp_path / "old.db"
    InfluencerStore(str(db)).save_contact("wxid_old", display_name="老达人", source="test")
    conn = sqlite3.connect(db)
    for table, columns in _ADDED_COLUMNS.items():
        for name, _ in columns:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
    conn.commit()
    conn.close()
    store = InfluencerStore(str(db))
    contact = store.get_contact("wxid_old")
    assert contact["display_name"] == "老达人" and contact["platform"] == "" and contact["tier"] == ""
    store.save_contact("wxid_old", source="test", platform="DY", platform_no="dy1")
    assert store.get_contact("wxid_old")["platform_no"] == "dy1"
