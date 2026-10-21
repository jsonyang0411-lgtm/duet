"""MEMORY_EMBEDDINGS: facts matched by meaning through an OpenAI-compatible endpoint."""

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.config import BotConfig, load_config
from src.db.schema import initialize_db
from src.db.store import MessageStore
from src.memory.context import build_memory_context
from src.memory.embeddings import (
    MAX_RETRY_SEC, MIN_RETRY_SEC, Embedder, VectorIndex, embedder_from_config, normalized,
)
from src.memory.search import rank_facts, semantic_closeness
from src.memory.store import MemoryStore

CHAT = "room@chatroom"
T0 = 1_780_000_000.0

# A toy meaning space: each concept is one axis.
CONCEPTS = {
    "seafood": ("虾", "海鲜", "螃蟹", "鱼"),
    "game": ("王者", "游戏", "开黑"),
    "sleep": ("熬夜", "睡", "失眠"),
}


def toy_vector(text):
    return [1.0 if any(word in text for word in words) else 0.0 for words in CONCEPTS.values()] + [0.2]


class ToyEmbedder:
    model = "toy"

    def __init__(self):
        self.calls, self.fail = [], None

    def embed(self, texts, *, timeout=None):
        self.calls.append((list(texts), timeout))
        if self.fail:
            raise self.fail
        return [normalized(toy_vector(t)) for t in texts]


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


@pytest.fixture
def mstore():
    conn = initialize_db(":memory:")
    yield MemoryStore(MessageStore(conn))
    conn.close()


def add_fact(mstore, subject, predicate, obj, *, chat=CHAT):
    fid, _ = mstore.upsert_fact(chat, kind="preference", subject=subject, predicate=predicate, obj=obj,
                                confidence=0.9, now=T0, evidence=[{
                                    "message_id": f"m{subject}{obj}", "quote": f"{predicate}{obj}",
                                    "sender_id": f"wxid_{subject}", "ts": T0}])
    return fid


def index(mstore, embedder=None, clock=None):
    return VectorIndex(mstore, embedder or ToyEmbedder(), clock=clock or Clock())


# ── the problem it solves ───────────────────────────────────────────────────

def test_a_fact_is_found_by_meaning_not_only_by_words(mstore):
    add_fact(mstore, "小明", "对", "海鲜过敏")
    add_fact(mstore, "阿强", "喜欢", "熬夜打王者")
    assert rank_facts(mstore, CHAT, query="我不能吃虾", people=[], now=T0) == []      # words only: missed
    vectors = index(mstore)
    assert vectors.index_pending() == 2
    ctx = build_memory_context(mstore, chat_id=CHAT, query="我不能吃虾", people=[], now=T0,
                               vectors=vectors, min_similarity=0.55, directed=True)
    assert [mstore.get_fact(fid)["object"] for fid in ctx.sources["facts"]] == ["海鲜过敏"]


def test_without_vectors_the_ranking_is_unchanged(mstore):
    add_fact(mstore, "小明", "喜欢", "喝美式咖啡")
    add_fact(mstore, "阿强", "对", "海鲜过敏")
    for query, people in (("小明喝什么", []), ("海鲜", ["阿强"]), ("随便聊聊", [])):
        plain = rank_facts(mstore, CHAT, query=query, people=people, now=T0)
        same = rank_facts(mstore, CHAT, query=query, people=people, now=T0, semantic=None)
        assert plain == same


def test_closeness_is_rescaled_above_the_threshold():
    assert semantic_closeness(0.5, 0.55) == 0.0
    assert semantic_closeness(0.55, 0.55) == 0.0
    assert semantic_closeness(1.0, 0.55) == 1.0
    assert semantic_closeness(0.775, 0.55) == pytest.approx(0.5)
    assert semantic_closeness(0.9, 1.0) == 0.0


# ── indexing ────────────────────────────────────────────────────────────────

def test_only_new_or_edited_facts_are_embedded(mstore):
    fid = add_fact(mstore, "小明", "对", "海鲜过敏")
    embedder = ToyEmbedder()
    vectors = index(mstore, embedder)
    assert vectors.index_pending() == 1 and vectors.index_pending() == 0
    assert len(embedder.calls) == 1
    mstore.conn.execute("UPDATE memory_facts SET object = '螃蟹过敏' WHERE id = ?", (fid,))
    assert vectors.index_pending() == 1                                        # the text changed
    assert set(vectors.vectors_for(CHAT)) == {fid} and vectors.vectors_for("other@chatroom") == {}


def test_a_retracted_fact_loses_its_vector(mstore):
    fid = add_fact(mstore, "小明", "对", "海鲜过敏")
    vectors = index(mstore)
    vectors.index_pending()
    mstore.set_fact_status(CHAT, fid, "retracted")
    vectors.index_pending()
    assert vectors.vectors_for(CHAT) == {}


def test_facts_are_sent_in_batches(mstore):
    for i in range(40):
        add_fact(mstore, f"人{i}", "喜欢", f"东西{i}")
    embedder = ToyEmbedder()
    assert index(mstore, embedder).index_pending() == 40
    assert [len(texts) for texts, _ in embedder.calls] == [32, 8]


def test_a_failing_endpoint_is_retried_later_with_a_growing_pause(mstore):
    add_fact(mstore, "小明", "对", "海鲜过敏")
    embedder, clock = ToyEmbedder(), Clock()
    vectors = index(mstore, embedder, clock)
    embedder.fail = RuntimeError("401")
    assert vectors.index_pending() == 0 and len(embedder.calls) == 1
    assert vectors.index_pending() == 0 and len(embedder.calls) == 1          # still pausing
    clock.now += MIN_RETRY_SEC
    vectors.index_pending()
    assert len(embedder.calls) == 2 and vectors.index_pause.seconds == MIN_RETRY_SEC * 4
    vectors.index_pause.seconds = MAX_RETRY_SEC
    clock.now += MAX_RETRY_SEC
    vectors.index_pending()
    assert vectors.index_pause.seconds == MAX_RETRY_SEC                        # capped
    assert not vectors.query_pause.waiting()                                   # replies keep their search
    embedder.fail = None
    clock.now += MAX_RETRY_SEC
    assert vectors.index_pending() == 1 and vectors.index_pause.seconds == MIN_RETRY_SEC


# ── the reply path ──────────────────────────────────────────────────────────

def test_a_chat_without_vectors_costs_no_call(mstore):
    embedder = ToyEmbedder()
    build_memory_context(mstore, chat_id=CHAT, query="我不能吃虾", people=[], now=T0,
                         vectors=index(mstore, embedder), directed=True)
    assert embedder.calls == []


def test_a_failing_endpoint_means_words_only(mstore):
    add_fact(mstore, "小明", "对", "海鲜过敏")
    add_fact(mstore, "阿强", "喜欢", "喝咖啡")
    embedder = ToyEmbedder()
    vectors = index(mstore, embedder)
    vectors.index_pending()
    embedder.fail = TimeoutError("slow")
    ctx = build_memory_context(mstore, chat_id=CHAT, query="阿强喜欢什么", people=[], now=T0, vectors=vectors,
                               directed=True)
    assert [mstore.get_fact(fid)["object"] for fid in ctx.sources["facts"]] == ["喝咖啡"]
    assert embedder.calls[-1][1] == 3.0                                        # the short reply-path timeout
    calls = len(embedder.calls)
    build_memory_context(mstore, chat_id=CHAT, query="吃虾", people=[], now=T0, vectors=vectors, directed=True)
    assert len(embedder.calls) == calls                                        # pausing: not even tried


# ── the endpoint ────────────────────────────────────────────────────────────

class FakeClient:
    def __init__(self, data):
        self.data, self.requests, self.timeouts = data, [], []
        self.embeddings = SimpleNamespace(create=self.create)

    def with_options(self, timeout):
        self.timeouts.append(timeout)
        return self

    def create(self, model, input):
        self.requests.append((model, input))
        return SimpleNamespace(data=self.data)


def test_the_endpoint_answer_is_put_in_order_and_normalised():
    client = FakeClient([SimpleNamespace(index=1, embedding=[0.0, 2.0]), SimpleNamespace(index=0, embedding=[3.0, 4.0])])
    embedder = Embedder(base_url="https://x/v1", api_key="k", model="bge-m3", client=client)
    first, second = embedder.embed(["甲", "乙" * 500], timeout=3.0)
    assert list(first) == pytest.approx([0.6, 0.8]) and list(second) == pytest.approx([0.0, 1.0])
    assert client.requests[0][0] == "bge-m3" and len(client.requests[0][1][1]) == 300
    assert client.timeouts == [3.0]
    with pytest.raises(ValueError):
        Embedder(base_url="", api_key="k", model="m", client=FakeClient([])).embed(["甲"])


# ── settings and wiring ─────────────────────────────────────────────────────

def load(**env):
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test", **env}, clear=True):
        return load_config()


def test_the_settings_are_off_by_default_and_checked():
    config = load()
    assert (config.memory_embeddings, config.embedding_model, config.memory_embedding_min_sim) == ("off", "", 0.55)
    assert embedder_from_config(config) is None
    on = load(MEMORY_EMBEDDINGS="on", EMBEDDING_BASE_URL="https://api.siliconflow.cn/v1",
              EMBEDDING_API_KEY="sk-x", EMBEDDING_MODEL="BAAI/bge-m3", MEMORY_EMBEDDING_MIN_SIM="0.6")
    embedder = embedder_from_config(on)
    assert embedder.model == "BAAI/bge-m3" and on.memory_embedding_min_sim == 0.6
    with pytest.raises(RuntimeError, match="EMBEDDING_API_KEY"):
        load(MEMORY_EMBEDDINGS="on", EMBEDDING_MODEL="m")
    with pytest.raises(RuntimeError, match="EMBEDDING_MODEL"):
        load(MEMORY_EMBEDDINGS="on", EMBEDDING_API_KEY="k")
    local = load(MEMORY_EMBEDDINGS="on", EMBEDDING_BASE_URL="http://127.0.0.1:8080/v1", EMBEDDING_MODEL="bge")
    assert local.embedding_api_key == ""                                       # a local server needs no key
    with pytest.raises(RuntimeError, match="MEMORY_EMBEDDING_MIN_SIM"):
        load(MEMORY_EMBEDDING_MIN_SIM="-0.1")
    with pytest.raises(RuntimeError, match="MEMORY_EMBEDDING_MIN_SIM"):
        load(MEMORY_EMBEDDING_MIN_SIM="1")


def test_the_console_treats_the_key_as_a_secret():
    from src.web.server import _SECRET_ENV_KEYS
    assert "EMBEDDING_API_KEY" in _SECRET_ENV_KEYS


def test_the_router_and_the_worker_get_the_index():
    from test_router_priority import RouterTestCase

    class Case(RouterTestCase):
        def runTest(self):
            router, _ = self.make_router(memory_v2="on", memory_embeddings="on",
                                         embedding_api_key="k", embedding_model="m")
            self.addCleanup(router.close)
            assert router._vectors is not None and router._memory_worker._vectors is router._vectors
            off, _ = self.make_router(memory_v2="on")
            self.addCleanup(off.close)
            assert off._vectors is None and off._memory_worker._vectors is None

    result = Case().run()
    assert result.wasSuccessful(), result.failures + result.errors


# ── review findings ─────────────────────────────────────────────────────────

class Refused(Exception):
    status_code = 400


class PickyEmbedder(ToyEmbedder):
    """Refuses any text with 敏感 in it, like a provider's content filter."""

    def embed(self, texts, *, timeout=None):
        self.calls.append((list(texts), timeout))
        if self.fail:
            raise self.fail
        if any("敏感" in t for t in texts):
            raise Refused("content filter")
        return [normalized(toy_vector(t)) for t in texts]


def test_one_refused_fact_does_not_stop_the_others(mstore):
    for i in range(5):
        add_fact(mstore, f"人{i}", "喜欢", f"东西{i}")
    bad = add_fact(mstore, "某人", "说过", "敏感内容")
    embedder = PickyEmbedder()
    vectors = index(mstore, embedder)
    assert vectors.index_pending() == 5
    assert bad not in vectors.vectors_for(CHAT) and len(vectors.vectors_for(CHAT)) == 5
    assert not vectors.index_pause.waiting() and not vectors.query_pause.waiting()
    calls = len(embedder.calls)
    assert vectors.index_pending() == 0 and len(embedder.calls) == calls        # skipped until it changes
    mstore.conn.execute("UPDATE memory_facts SET object = '普通内容' WHERE id = ?", (bad,))
    assert vectors.index_pending() == 1


def test_a_refused_batch_then_a_failing_endpoint_pauses(mstore):
    add_fact(mstore, "某人", "说过", "敏感内容")
    add_fact(mstore, "小明", "喜欢", "咖啡")
    embedder = PickyEmbedder()
    vectors = index(mstore, embedder)
    real_embed = embedder.embed

    def embed(texts, *, timeout=None):
        if len(texts) == 1:
            raise TimeoutError("down")                                         # the retry one by one fails
        return real_embed(texts, timeout=timeout)

    embedder.embed = embed
    assert vectors.index_pending() == 0 and vectors.index_pause.waiting()


class PairEmbedder(ToyEmbedder):
    """Query 'q' = [1, 0]; every fact = [s, sqrt(1-s²)] with s from the object text."""

    def embed(self, texts, *, timeout=None):
        self.calls.append((list(texts), timeout))
        out = []
        for t in texts:
            if t == "q":
                out.append(normalized([1.0, 0.0]))
            else:
                sim = float(t.split("相似")[1])
                out.append(normalized([sim, (1 - sim * sim) ** 0.5]))
        return out


def test_the_threshold_setting_is_the_real_threshold(mstore):
    add_fact(mstore, "甲", "是", "相似0.6")
    add_fact(mstore, "乙", "是", "相似0.5")
    vectors = index(mstore, PairEmbedder())
    vectors.index_pending()

    def chosen(min_sim):
        ctx = build_memory_context(mstore, chat_id=CHAT, query="q", people=[], now=T0,
                                   vectors=vectors, min_similarity=min_sim, directed=True)
        return [mstore.get_fact(fid)["object"] for fid in ctx.sources["facts"]]

    assert chosen(0.55) == ["相似0.6"]
    assert chosen(0.65) == []
    assert chosen(0.45) == ["相似0.6", "相似0.5"]


def test_a_meaning_match_scores_by_the_weight(mstore):
    add_fact(mstore, "小明", "对", "海鲜过敏")
    vectors = index(mstore)
    vectors.index_pending()
    semantic = (vectors.query_vector("吃虾"), vectors.vectors_for(CHAT), 0.55)
    (hit,) = rank_facts(mstore, CHAT, query="吃虾", people=[], now=T0, semantic=semantic)
    close = semantic_closeness(float(sum(a * b for a, b in zip(semantic[0], next(iter(semantic[1].values()))))), 0.55)
    assert hit.score == pytest.approx(0.5 * close + 0.1 * 0.9 + 0.05, abs=1e-3)


def test_only_a_message_to_the_bot_is_embedded(mstore):
    add_fact(mstore, "小明", "对", "海鲜过敏")
    embedder = ToyEmbedder()
    vectors = index(mstore, embedder)
    vectors.index_pending()
    calls = len(embedder.calls)
    build_memory_context(mstore, chat_id=CHAT, query="吃虾", people=[], now=T0, vectors=vectors)
    assert len(embedder.calls) == calls


def test_forgetting_removes_vectors_at_once_even_while_pausing(mstore):
    keep = add_fact(mstore, "小明", "喜欢", "咖啡")
    gone = add_fact(mstore, "阿强", "对", "海鲜过敏")
    embedder, clock = ToyEmbedder(), Clock()
    vectors = index(mstore, embedder, clock)
    vectors.index_pending()
    embedder.fail = RuntimeError("down")
    clock.now += 1
    vectors.index_pending()                                                    # now pausing
    mstore.forget_sender(CHAT, "wxid_阿强")
    assert set(vectors.vectors_for(CHAT)) == {keep}
    mstore.set_fact_status(CHAT, keep, "retracted")
    assert vectors.vectors_for(CHAT) == {}
    assert gone


def test_a_fact_retracted_while_being_embedded_gets_no_vector(mstore):
    fid = add_fact(mstore, "小明", "对", "海鲜过敏")
    embedder = ToyEmbedder()
    vectors = index(mstore, embedder)
    real_embed = embedder.embed

    def embed(texts, *, timeout=None):
        mstore.set_fact_status(CHAT, fid, "retracted")
        return real_embed(texts, timeout=timeout)

    embedder.embed = embed
    vectors.index_pending()
    assert mstore.conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 0


def test_rows_of_another_model_or_kind_are_not_used(mstore):
    fid = add_fact(mstore, "小明", "对", "海鲜过敏")
    vectors = index(mstore)
    vectors.index_pending()
    rows = [("fact", fid, CHAT, "other-model", "h", 4, normalized([1, 0, 0, 0]).tobytes(), T0),
            ("episode", fid + 200, CHAT, "toy", "h", 4, normalized([1, 0, 0, 0]).tobytes(), T0),
            ("fact", fid + 300, CHAT, "toy", "h", 0, b"", T0)]
    mstore.conn.executemany("INSERT INTO memory_vectors VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    assert set(vectors.vectors_for(CHAT)) == {fid}
    vectors.index_pending()
    models = {row[0] for row in mstore.conn.execute("SELECT model FROM memory_vectors")}
    assert models == {"toy"}                                                  # another model's rows are gone


def test_a_model_that_now_returns_other_sizes_is_reindexed(mstore):
    add_fact(mstore, "小明", "对", "海鲜过敏")
    embedder = ToyEmbedder()
    vectors = index(mstore, embedder)
    vectors.index_pending()
    embedder.embed = lambda texts, timeout=None: [normalized([1.0] * 7) for _ in texts]
    assert vectors.query_vector("吃虾") is not None
    assert vectors.index_pending() == 1
    assert {len(v) for v in vectors.vectors_for(CHAT).values()} == {7}


def test_a_query_success_ends_the_pause():
    clock = Clock()
    embedder = ToyEmbedder()
    vectors = VectorIndex(SimpleNamespace(), embedder, clock=clock)
    embedder.fail = TimeoutError("slow")
    assert vectors.query_vector("a") is None
    clock.now += MIN_RETRY_SEC
    embedder.fail = None
    assert vectors.query_vector("a") is not None
    assert vectors.query_pause.seconds == MIN_RETRY_SEC and not vectors.query_pause.waiting()


def test_the_batch_limit_and_the_fact_text(mstore):
    for i in range(10):
        add_fact(mstore, f"人{i}", "喜欢", f"东西{i}")
    assert index(mstore).index_pending(limit=5) == 5
    from src.memory.embeddings import dot, fact_text
    assert fact_text({"subject": "小明", "predicate": "对", "object": "海鲜过敏"}) == "小明对海鲜过敏"
    assert dot(normalized([1, 0]), normalized([1, 0, 0])) == 0.0


def test_the_client_never_retries_and_works_without_a_key():
    created = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.embeddings = SimpleNamespace(create=lambda model, input: SimpleNamespace(
                data=[SimpleNamespace(index=0, embedding=[1.0, 0.0])]))

    with patch("openai.OpenAI", FakeOpenAI):
        Embedder(base_url="http://127.0.0.1:8080/v1", api_key="", model="bge").embed(["甲"])
    assert created["max_retries"] == 0 and created["api_key"] == "none"


def test_the_router_hands_the_index_to_the_memory_lookup():
    from test_router_pipeline import PipelineSummarizer
    from test_router_priority import BOT, RouterTestCase, StubWebSearch
    from src.pipeline.types import Decision

    class Case(RouterTestCase):
        def runTest(self):
            model = PipelineSummarizer(Decision(action="reply", messages=("好",), addressee="me"))
            router, _ = self.make_router(summarizer=model, pipeline_v2="single", web=StubWebSearch(),
                                         memory_v2="on", memory_embeddings="on", embedding_api_key="k",
                                         embedding_model="m", memory_embedding_min_sim=0.7)
            self.addCleanup(router.close)
            seen = {}
            with patch("src.router.build_memory_context",
                       side_effect=lambda *a, **kw: seen.update(kw) or SimpleNamespace(
                           empty=True, text="", sources={"facts": [], "episodes": [], "messages": []})):
                router.handle(self.group_msg("m1", f"@{BOT} 我不能吃虾"))
            assert seen["vectors"] is router._vectors and seen["min_similarity"] == 0.7

    result = Case().run()
    assert result.wasSuccessful(), result.failures + result.errors


def test_the_worker_indexes_after_each_tick(mstore):
    from src.memory.worker import MemoryWorker
    import threading
    called = threading.Event()
    spy = SimpleNamespace(index_pending=lambda: called.set() or 0)
    worker = MemoryWorker(mstore, SimpleNamespace(complete_json=lambda *a, **k: None),
                          interval_sec=0.01, vectors=spy)
    worker.start()
    try:
        assert called.wait(5)
    finally:
        worker.stop()


def test_without_memory_v2_the_switch_only_warns():
    from test_router_priority import RouterTestCase
    import src.router as router_module

    class Case(RouterTestCase):
        def runTest(self):
            router, _ = self.make_router(memory_embeddings="on", embedding_api_key="k", embedding_model="m")
            assert router._vectors is None

    with patch.object(router_module.logger, "warning") as warning:
        result = Case().run()
    assert result.wasSuccessful(), result.failures + result.errors
    assert any("MEMORY_EMBEDDINGS only works with MEMORY_V2=on" in str(c.args[0]) for c in warning.call_args_list)
