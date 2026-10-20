"""ResearchFlow · 阶段 2 测试（幂等 / 缓存 / 恢复 / 重试）

这些测试全部离线运行，不需要模型也不需要网络 —— 因为阶段 2 关心的
是"系统的行为"而不是"模型聪不聪明"。

阶段 2 的每个功能，都对应一个真实故障场景：

    幂等    → 用户网络超时后重发请求，不能建两个任务、烧两份钱
    缓存    → 同一个搜索反复查，不能每次都打外部 API
    恢复    → 服务崩了，卡在 running 的任务必须有人捡回来
    重试    → 瞬时故障应该自动重试，参数错误不该重试
    租约    → 两个 Worker 不能同时跑同一个任务
"""

from __future__ import annotations

import json
import os
import time

os.environ.setdefault("MOCK_LLM", "1")

import pytest  # noqa: E402

from app import agent, cache, db  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """每个测试独立的数据库，互不干扰。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "mock_llm", True)
    monkeypatch.setattr(settings, "cache_enabled", True)
    monkeypatch.setattr(settings, "tool_max_retries", 2)
    monkeypatch.setattr(settings, "retry_backoff", 0.01)  # 测试里别真等
    db.init_db()
    yield


# ==========================================================================
# 一、幂等
# ==========================================================================

def test_same_key_creates_one_run():
    """同一个幂等键提交两次，必须只建一个任务。"""
    rid1, created1 = db.create_run("问题 A", idempotency_key="key-1")
    rid2, created2 = db.create_run("问题 A 改动版", idempotency_key="key-1")

    assert created1 is True
    assert created2 is False, "第二次不该新建"
    assert rid1 == rid2, "应返回同一个 run_id"
    assert len(db.list_runs()) == 1


def test_different_keys_create_two_runs():
    """不同幂等键 → 两个任务（这是正常需求，不能被误挡）。"""
    rid1, c1 = db.create_run("问题 A", idempotency_key="key-1")
    rid2, c2 = db.create_run("问题 B", idempotency_key="key-2")

    assert c1 and c2
    assert rid1 != rid2
    assert len(db.list_runs()) == 2


def test_no_key_allows_duplicates():
    """没有幂等键时，允许重复（调用方自己负责）。"""
    rid1, c1 = db.create_run("同一句话", idempotency_key=None)
    rid2, c2 = db.create_run("同一句话", idempotency_key=None)
    assert c1 and c2
    assert rid1 != rid2


def test_idempotency_under_concurrency():
    """并发提交同一个键，也只能建出一个。

    这是幂等最难的部分：两个请求同时通过了"检查"，都以为自己是第一个。
    靠唯一索引兜底才正确。这个测试就是在验证那个兜底真的有效。
    """
    import threading

    results = []
    lock = threading.Lock()

    def worker(i):
        rid, created = db.create_run("并发问题", idempotency_key="race-key")
        with lock:
            results.append((rid, created))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    ids = {rid for rid, _ in results}
    created_count = sum(1 for _, c in results if c)

    assert len(ids) == 1, f"应该只有一个 run_id，实际 {len(ids)} 个: {ids}"
    assert created_count == 1, f"应该只创建一次，实际 {created_count} 次"
    assert len(db.list_runs()) == 1


# ==========================================================================
# 二、缓存
# ==========================================================================

def test_cache_hit_returns_same_result():
    args = {"query": "agent framework"}
    payload = {"ok": True, "data": {"results": [{"title": "x"}]}}

    cache.put("web_search", args, payload)
    hit = cache.get("web_search", args)

    assert hit is not None
    assert hit["data"]["results"][0]["title"] == "x"


def test_cache_key_ignores_arg_order():
    """参数顺序不同但内容相同 → 必须命中同一个缓存。

    如果这里漏了 sort_keys，就会出现"明明查过却永远不命中"的诡异现象。
    """
    cache.put("read_url", {"url": "http://a", "max_chars": 100}, {"ok": True, "data": {"text": "hi"}})

    hit = cache.get("read_url", {"max_chars": 100, "url": "http://a"})
    assert hit is not None, "参数顺序不该影响命中"


def test_cache_different_args_miss():
    """参数不同必须不命中，否则会串味。"""
    cache.put("web_search", {"query": "a", "max_results": 3}, {"ok": True, "data": {}})
    assert cache.get("web_search", {"query": "a", "max_results": 10}) is None


def test_failures_are_not_cached():
    """失败的调用绝不能缓存 —— 否则一次网络抖动会被记住一小时。"""
    args = {"query": "whatever"}
    cache.put("web_search", args, {"ok": False, "error": "网络超时"})
    assert cache.get("web_search", args) is None


def test_cache_disabled():
    """关掉缓存后必须真的不生效（做对照实验靠这个开关）。"""
    settings.cache_enabled = False
    cache.put("web_search", {"query": "x"}, {"ok": True, "data": {}})
    settings.cache_enabled = True
    assert cache.get("web_search", {"query": "x"}) is None


def test_cache_ttl_expiry():
    """过期的缓存条目要自动失效。"""
    db.cache_put("k1", "web_search", json.dumps({"ok": True}), ttl_seconds=-1)
    assert db.cache_get("k1") is None, "负 TTL 应该立刻过期"


def test_cache_stats_counts_hits():
    cache.put("web_search", {"query": "s"}, {"ok": True, "data": {}})
    cache.get("web_search", {"query": "s"})
    cache.get("web_search", {"query": "s"})

    stats = cache.stats()
    assert stats["entries"] >= 1
    assert stats["total_hits"] >= 2


# ==========================================================================
# 三、租约与恢复
# ==========================================================================

def test_claim_and_release():
    rid, _ = db.create_run("租约测试")
    assert db.claim_run(rid, "w1", 30) is True

    # 同一个 Worker 再领一次：状态已是 running 且租约未过期 → 失败
    assert db.claim_run(rid, "w1", 30) is False

    db.release_lease(rid)
    run = db.get_run(rid)
    assert run["lease_owner"] is None


def test_expired_lease_can_be_reclaimed():
    """租约过期后，别的 Worker 应该能接手 —— 这是任务恢复的核心。"""
    rid, _ = db.create_run("崩溃任务")
    db.claim_run(rid, "w1", lease_seconds=-1)  # 立刻过期

    # 另一个 Worker 来抢
    assert db.claim_run(rid, "w2", 30) is True
    run = db.get_run(rid)
    assert run["lease_owner"] == "w2"
    assert run["attempts"] == 2


def test_find_orphans_detects_pending_and_expired():
    rid_pending, _ = db.create_run("从没跑过")
    rid_dead, _ = db.create_run("跑挂了")
    db.claim_run(rid_dead, "w1", lease_seconds=-1)

    orphans = db.find_orphans()
    ids = {o["run_id"] for o in orphans}

    assert rid_pending in ids, "pending 任务应被认出"
    assert rid_dead in ids, "租约过期的任务应被认出"


def test_finished_runs_are_not_orphans():
    """已完成的任务绝不能被"恢复"机制重新跑一遍 —— 那是灾难。"""
    rid, _ = db.create_run("已完成")
    db.mark_finished(rid, report="done")

    orphans = db.find_orphans()
    assert rid not in {o["run_id"] for o in orphans}


def test_renew_lease_fails_after_takeover():
    """租约被别人抢走后，原 Worker 的续约必须失败 —— 它据此停手。"""
    rid, _ = db.create_run("抢锁")
    db.claim_run(rid, "w1", lease_seconds=-1)
    db.claim_run(rid, "w2", 30)

    assert db.renew_lease(rid, "w1", 30) is False
    assert db.renew_lease(rid, "w2", 30) is True


# ==========================================================================
# 四、重试与错误分类
# ==========================================================================

@pytest.mark.parametrize(
    "message,expected",
    [
        ("URLError: connection reset", "network"),
        ("超时 20s", "timeout"),
        ("HTTP 503", "http_retryable"),
        ("HTTP 429 rate limit", "http_retryable"),
        ("HTTP 404", "http_client"),
        ("参数不匹配: unexpected keyword", "bad_args"),
        ("不存在这个工具: foo", "unknown_tool"),
        ("something weird", "unknown"),
    ],
)
def test_error_classification(message, expected):
    assert agent._classify_error(message) == expected


def test_retry_on_transient_error(monkeypatch):
    """网络类错误应该重试，最终成功。"""
    calls = {"n": 0}

    def flaky(name, args):
        calls["n"] += 1
        if calls["n"] < 3:
            return {"ok": False, "error": "URLError: connection reset"}
        return {"ok": True, "data": {"ok": 1}}

    monkeypatch.setattr(agent.tools, "call_tool", flaky)

    a = agent.ResearchAgent("r1", "q")
    result, err_type, retries = a._call_with_retry("web_search", {})

    assert result["ok"] is True
    assert retries == 2, f"应该重试 2 次，实际 {retries}"
    assert calls["n"] == 3


def test_no_retry_on_bad_args(monkeypatch):
    """参数错误不该重试 —— 再试还是错，只是更慢。"""
    calls = {"n": 0}

    def bad(name, args):
        calls["n"] += 1
        return {"ok": False, "error": "参数不匹配: unexpected keyword"}

    monkeypatch.setattr(agent.tools, "call_tool", bad)

    a = agent.ResearchAgent("r1", "q")
    result, err_type, retries = a._call_with_retry("web_search", {})

    assert result["ok"] is False
    assert err_type == "bad_args"
    assert retries == 0
    assert calls["n"] == 1, "不该重试"


def test_retry_exhausted(monkeypatch):
    """一直失败的话，重试次数用完就放弃，并标出来。"""
    def always_fail(name, args):
        return {"ok": False, "error": "URLError: timeout"}

    monkeypatch.setattr(agent.tools, "call_tool", always_fail)

    a = agent.ResearchAgent("r1", "q")
    result, err_type, retries = a._call_with_retry("web_search", {})

    assert result["ok"] is False
    assert retries == settings.tool_max_retries
    assert result.get("retries_exhausted") == settings.tool_max_retries


# ==========================================================================
# 五、端到端：缓存真的能减少工具调用
# ==========================================================================

def test_cache_reduces_actual_calls(monkeypatch):
    """同一工具同样参数调用两次，第二次应该走缓存、不再真正执行。

    这个测试直接回答了面试问题："你怎么证明缓存有效？"
    """
    real_calls = {"n": 0}

    def counting(name, args):
        real_calls["n"] += 1
        return {"ok": True, "data": {"results": [{"title": "t", "url": "u"}]}}

    monkeypatch.setattr(agent.tools, "call_tool", counting)

    rid, _ = db.create_run("缓存效果")
    a = agent.ResearchAgent(rid, "缓存效果")

    tc = {"id": "1", "name": "web_search", "arguments": {"query": "same"}}
    a._execute_tool(tc)
    first = real_calls["n"]
    a._execute_tool(tc)
    second = real_calls["n"]

    assert first == 1
    assert second == 1, "第二次应该命中缓存，不该再真正调用"
    assert a.stats["cache_hits"] == 1

    run = db.get_run(rid)
    assert run["cache_hits"] >= 1
    assert run["tool_calls"] >= 2


def test_agent_stats_tracked():
    """Agent 的统计要能落到数据库，阶段 3 的实验数据就靠它。"""
    rid, _ = db.create_run("统计")
    a = agent.ResearchAgent(rid, "统计")

    tc = {"id": "1", "name": "read_url", "arguments": {"url": "https://example.com"}}
    # 不真的联网：预先塞一条缓存
    cache.put("read_url", {"url": "https://example.com"}, {"ok": True, "data": {"text": "x"}})
    a._execute_tool(tc)

    assert a.stats["cache_hits"] == 1
    run = db.get_run(rid)
    assert run["cache_hits"] == 1
    assert run["tool_calls"] == 1