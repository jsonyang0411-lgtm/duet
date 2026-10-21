"""Retrieval: message search (FTS5 + LIKE + recency, fused) and fact ranking."""

import pytest

from memory_support import CHAT, T0, add_history, add_messages, make_stores

from src.memory.context import (
    EPISODES_HEADER, FACTS_HEADER, RECALL_HEADER, build_memory_context,
)
from src.memory.search import rank_facts, search_messages

CHAT_LINES = [
    ("小明", "周六我们去爬山吧"),
    ("阿红", "好啊，九点在南门集合"),
    ("小明", "我最近老是加班，累死了"),
    ("阿强", "南通人都爱吃鱼"),
    ("阿红", "我昨天加班到十点才回家"),
    ("阿强", "今天天气真不错"),
    ("小明", "晚上想吃火锅，谁来"),
    ("阿红", "火锅我可以，在海底捞订位"),
    ("阿强", "我下个月要去北京出差"),
]


@pytest.fixture(params=["indexed", "like-only"])
def env(request):
    conn, store, mstore = make_stores(index=request.param == "indexed")
    add_messages(store, CHAT_LINES)
    yield store, mstore
    conn.close()


def contents(hits):
    return [h.content for h in hits]


def test_a_paraphrased_question_finds_the_message(env):
    _store, mstore = env
    hits = search_messages(mstore, CHAT, "上次小明说想去哪里玩来着")
    assert hits == [] or "爬山" not in hits[0].content   # no word in common: nothing to claim
    hits = search_messages(mstore, CHAT, "上次说周六去哪里爬山")
    assert contents(hits)[0] == "周六我们去爬山吧"


def test_a_two_character_keyword_is_found_by_like_in_both_modes(env):
    _store, mstore = env
    assert set(contents(search_messages(mstore, CHAT, "加班"))) == {
        "我最近老是加班，累死了", "我昨天加班到十点才回家",
    }


def test_a_two_character_word_inside_a_question_is_found(env):
    _store, mstore = env
    hits = search_messages(mstore, CHAT, "谁说过加班的事")
    assert set(contents(hits)[:2]) == {"我最近老是加班，累死了", "我昨天加班到十点才回家"}


def test_a_three_character_phrase_ranks_its_message_first(env):
    _store, mstore = env
    hits = search_messages(mstore, CHAT, "南通人")
    assert contents(hits)[0] == "南通人都爱吃鱼"


def test_newer_messages_win_ties(env):
    _store, mstore = env
    hits = search_messages(mstore, CHAT, "火锅")
    assert contents(hits)[0] == "火锅我可以，在海底捞订位"


def test_the_question_itself_and_shown_context_are_excluded(env):
    store, mstore = env
    ids = add_messages(store, [("小明", "还记得谁说过加班吗")], start=T0 + 10_000, prefix="q")
    hits = search_messages(mstore, CHAT, "谁说过加班", exclude_ids=set(ids))
    assert all(h.message_id not in ids for h in hits) and len(hits) == 2
    shown = {h.message_id for h in hits}
    assert search_messages(mstore, CHAT, "谁说过加班", exclude_ids=set(ids) | shown) == []


def test_before_ts_hides_newer_messages(env):
    _store, mstore = env
    hits = search_messages(mstore, CHAT, "加班", before_ts=T0 + 3 * 60)
    assert contents(hits) == ["我最近老是加班，累死了"]


def test_nothing_in_common_means_no_hit(env):
    _store, mstore = env
    assert search_messages(mstore, CHAT, "量子力学的哥本哈根诠释") == []
    assert search_messages(mstore, CHAT, "") == []


def test_a_long_question_needs_real_overlap_not_one_shared_trigram(env):
    _store, mstore = env
    # shares only "南通人" with one message; the rest of the question is about something else
    hits = search_messages(mstore, CHAT, "南通人工智能量子计算机研究所成立时间表")
    assert hits == []


def test_other_chats_never_leak_in(env):
    store, mstore = env
    add_messages(store, [("路人", "别的群也在聊爬山这件事")], chat="elsewhere")
    assert all("别的群" not in c for c in contents(search_messages(mstore, CHAT, "爬山")))


def test_imported_history_is_searched_and_duplicates_with_live_rows_collapse():
    conn, store, mstore = make_stores(index=True)
    add_messages(store, [("小明", "去年夏天我们在青岛吃了海鲜")], start=T0)
    add_history(store, [("小明", "去年夏天我们在青岛吃了海鲜"),
                        ("阿红", "青岛的啤酒节真热闹")], start=T0)
    hits = search_messages(mstore, CHAT, "青岛")
    assert len(hits) == 2
    assert {h.source for h in hits} <= {"live", "history"}
    conn.close()


def test_hits_report_which_lists_found_them():
    conn, store, mstore = make_stores(index=True)
    add_messages(store, CHAT_LINES)
    hit = search_messages(mstore, CHAT, "南通人")[0]
    assert "fts" in hit.via and hit.score > 0
    conn.close()


def test_the_limit_is_respected_and_at_least_one_is_returned():
    conn, store, mstore = make_stores(index=True)
    add_messages(store, [("a", f"关于火锅的第{i}次讨论") for i in range(30)])
    assert len(search_messages(mstore, CHAT, "火锅的", limit=5)) == 5
    assert len(search_messages(mstore, CHAT, "火锅的", limit=0)) == 1
    conn.close()


# ── facts ───────────────────────────────────────────────────────────────

def add_fact(mstore, subject, predicate, obj, *, kind="other", confidence=0.9, now=T0,
             expires_at=None):
    """A fact two different people backed (so even hearsay kinds are used)."""
    quote = f"{subject}{predicate}{obj}"
    return mstore.upsert_fact(
        CHAT, kind=kind, subject=subject, predicate=predicate, obj=obj, confidence=confidence,
        evidence=[{"message_id": f"e1-{subject}-{obj}", "quote": quote, "sender_id": "wxid_x",
                   "sender_name": "群友甲", "ts": now},
                  {"message_id": f"e2-{subject}-{obj}", "quote": quote, "sender_id": "wxid_y",
                   "sender_name": "群友乙", "ts": now}],
        expires_at=expires_at, now=now,
    )[0]


def test_facts_about_the_people_present_are_selected():
    conn, store, mstore = make_stores()
    add_fact(mstore, "小明", "喜欢", "咖啡")
    add_fact(mstore, "阿红", "养了", "一只猫")
    add_fact(mstore, "老王", "住在", "杭州")
    hits = rank_facts(mstore, CHAT, query="今天吃什么", people=["小明", "阿红"], now=T0)
    assert {h.fact["subject"] for h in hits} == {"小明", "阿红"}
    conn.close()


def test_an_alias_is_selected_when_the_alias_is_mentioned():
    conn, store, mstore = make_stores()
    add_fact(mstore, "宇肆", "称呼是", "次元哥哥", kind="alias")
    add_fact(mstore, "老王", "喜欢", "钓鱼")
    hits = rank_facts(mstore, CHAT, query="次元哥哥呢，今天怎么没来", people=["小明"], now=T0)
    assert [h.fact["subject"] for h in hits] == ["宇肆"]
    conn.close()


def test_a_fact_whose_subject_is_named_in_the_message_is_selected():
    conn, store, mstore = make_stores()
    add_fact(mstore, "老王", "住在", "杭州")
    hits = rank_facts(mstore, CHAT, query="老王现在住哪", people=[], now=T0)
    assert [h.fact["object"] for h in hits] == ["杭州"]
    conn.close()


def test_unrelated_facts_are_not_injected():
    conn, store, mstore = make_stores()
    add_fact(mstore, "老王", "住在", "杭州")
    assert rank_facts(mstore, CHAT, query="今天天气真好", people=["小明"], now=T0) == []
    conn.close()


def test_weak_expired_and_retracted_facts_are_never_ranked():
    conn, store, mstore = make_stores()
    add_fact(mstore, "小明", "喜欢", "咖啡", confidence=0.4)
    add_fact(mstore, "小明", "约了", "周六爬山", expires_at=T0 - 5)
    gone = add_fact(mstore, "小明", "讨厌", "香菜")
    mstore.set_fact_status(CHAT, gone, "retracted")
    assert rank_facts(mstore, CHAT, query="小明", people=["小明"], now=T0, min_confidence=0.6) == []
    conn.close()


def test_the_limit_and_the_evidence_come_with_the_hit():
    conn, store, mstore = make_stores()
    for i in range(6):
        add_fact(mstore, "小明", "喜欢", f"东西{i}号")
    hits = rank_facts(mstore, CHAT, query="", people=["小明"], now=T0, limit=4)
    assert len(hits) == 4 and hits[0].evidence[0]["message_id"].startswith("e1-小明")
    conn.close()


# ── the text a prompt gets ───────────────────────────────────────────────

def test_the_context_carries_facts_episodes_and_recall_with_their_sources():
    conn, store, mstore = make_stores(index=True)
    add_messages(store, CHAT_LINES)
    fid = add_fact(mstore, "宇肆", "称呼是", "次元哥哥", kind="alias")
    add_fact(mstore, "小明", "喜欢", "火锅")
    mstore.add_episode(CHAT, summary="大家商量周六去爬山", topics=["爬山"], start_ts=T0,
                       end_ts=T0 + 600, message_count=9, evidence=[], now=T0)
    ctx = build_memory_context(
        mstore, chat_id=CHAT, query="次元哥哥是谁，还有谁说过加班", people=["小明"],
        directed=True, recall=True, now=T0,
    )
    assert FACTS_HEADER in ctx.memory_text and EPISODES_HEADER in ctx.memory_text
    assert "宇肆 在这个群里的固定称呼/别名是「次元哥哥」" in ctx.memory_text
    assert "小明喜欢火锅" in ctx.memory_text
    assert "群友乙说：「宇肆称呼是次元哥哥」" in ctx.memory_text
    assert "大家商量周六去爬山" in ctx.memory_text
    assert ctx.recall_text.startswith(RECALL_HEADER)
    assert "小明：我最近老是加班，累死了" in ctx.recall_text
    assert fid in ctx.sources["facts"] and ctx.sources["episodes"] and ctx.sources["messages"]
    assert not ctx.empty
    conn.close()


def test_episodes_and_recall_are_only_added_when_asked():
    conn, store, mstore = make_stores(index=True)
    add_messages(store, CHAT_LINES)
    mstore.add_episode(CHAT, summary="摘要", topics=[], start_ts=T0, end_ts=T0, message_count=1,
                       evidence=[], now=T0)
    ctx = build_memory_context(mstore, chat_id=CHAT, query="加班", people=[], directed=False,
                               recall=False, now=T0)
    assert ctx.empty and ctx.sources == {"facts": [], "episodes": [], "messages": []}
    ctx = build_memory_context(mstore, chat_id=CHAT, query="加班", people=[], directed=True,
                               recall=True, with_memory=False, now=T0)
    assert ctx.memory_text == "" and "加班" in ctx.recall_text
    conn.close()


def test_the_memory_block_is_capped():
    conn, store, mstore = make_stores()
    for i in range(40):
        add_fact(mstore, "小明", "喜欢", "很长很长的东西" * 3 + str(i))
    ctx = build_memory_context(mstore, chat_id=CHAT, query="", people=["小明"], now=T0,
                               max_facts=40)
    assert len(ctx.memory_text) <= 1500 and ctx.memory_text.count("\n") >= 5
    conn.close()


def test_message_hits_use_the_nickname_callback():
    conn, store, mstore = make_stores()
    add_messages(store, [("wxid-ish", "我昨天加班到十点")])
    ctx = build_memory_context(
        mstore, chat_id=CHAT, query="加班", people=[], recall=True, now=T0,
        name_of=lambda sid, name: "大明", with_memory=False,
    )
    assert "大明：我昨天加班到十点" in ctx.recall_text
    conn.close()


# ── trust: hearsay about a person needs two backers or the person's own word ──

def add_claim(mstore, subject, predicate, obj, *, kind, speakers, now=T0):
    return mstore.upsert_fact(
        CHAT, kind=kind, subject=subject, predicate=predicate, obj=obj, confidence=0.9, now=now,
        evidence=[{"message_id": f"c{i}-{subject}-{obj}", "quote": f"{subject}{predicate}{obj}",
                   "sender_id": f"wxid_{who}", "sender_name": who, "ts": now} for i, who in enumerate(speakers)],
    )[0]


def test_one_persons_word_about_someone_else_is_not_used_for_claims_but_is_for_nicknames():
    conn, store, mstore = make_stores()
    add_claim(mstore, "老王", "住在", "杭州", kind="other", speakers=["路人"])
    add_claim(mstore, "老王", "是", "小明的表哥", kind="relationship", speakers=["路人"])
    add_claim(mstore, "老王", "称呼是", "王哥", kind="alias", speakers=["路人"])
    add_claim(mstore, "老王", "喜欢", "钓鱼", kind="preference", speakers=["路人"])
    hits = rank_facts(mstore, CHAT, query="老王", people=["老王"], now=T0)
    assert {h.fact["object"] for h in hits} == {"王哥", "钓鱼"}
    conn.close()


def test_a_claim_is_used_when_the_person_said_it_himself_or_two_people_back_it():
    conn, store, mstore = make_stores()
    add_claim(mstore, "老王", "住在", "杭州", kind="other", speakers=["老王"])
    add_claim(mstore, "阿红", "住在", "上海", kind="other", speakers=["路人", "老王"])
    hits = rank_facts(mstore, CHAT, query="", people=["老王", "阿红"], now=T0)
    assert {h.fact["object"] for h in hits} == {"杭州", "上海"}
    conn.close()


def test_a_weakly_related_fact_stays_out():
    conn, store, mstore = make_stores()
    add_fact(mstore, "小明", "喜欢", "咖啡")
    # a single shared pair out of many is below the relevance threshold
    assert rank_facts(mstore, CHAT, query="咖啡馆营业时间介绍与菜单价格", people=[], now=T0) == []
    assert len(rank_facts(mstore, CHAT, query="咖啡", people=["小明"], now=T0)) == 1
    conn.close()


def test_the_search_survives_an_index_that_vanishes_underneath_it():
    conn, store, mstore = make_stores(index=True)
    add_messages(store, [("小明", "周六去爬山好不好"), ("阿红", "我昨天加班到十点")])
    assert contents(search_messages(mstore, CHAT, "周六去爬山")) == ["周六去爬山好不好"]
    conn.execute("DROP TABLE fts_messages")                  # another process removed it
    assert contents(search_messages(mstore, CHAT, "周六去爬山")) == ["周六去爬山好不好"]    # LIKE fallback
    conn.close()


def test_a_recency_only_candidate_cannot_outrank_a_relevant_hit():
    conn, store, mstore = make_stores(index=True)
    add_messages(store, [("a", "南通人都爱吃鱼")], start=T0)
    add_messages(store, [("b", "南通的天气今天不错")], start=T0 + 100_000, prefix="new")
    assert contents(search_messages(mstore, CHAT, "南通人"))[0] == "南通人都爱吃鱼"
    conn.close()
