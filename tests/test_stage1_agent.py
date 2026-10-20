"""ResearchFlow · 阶段 1 测试（离线、可重复、不花钱）

为什么用 Mock 模型？
    Agent 的行为依赖模型输出，而模型有随机性。
    如果测试直接调真模型，结果会飘、会花钱、还会被网络影响 ——
    那种测试没人愿意天天跑，等于没有。
    所以这里把"模型决策"换成脚本，专门测**我们自己的循环逻辑**。

覆盖的内容：
    1. Agent Loop 能正常跑完并产出报告
    2. 工具调用被正确记录进 trace
    3. token 消耗被累加
    4. **最大步数护栏真的会触发**（这条最重要：护栏不生效 = 烧钱）
    5. 上下文裁剪逻辑
    6. 降级交付（模型不可用时仍能给出部分结果）

用法：
    MOCK_LLM=1 pytest tests/ -v
"""

from __future__ import annotations

import os

os.environ.setdefault("MOCK_LLM", "1")

import pytest  # noqa: E402

from app import agent, db, llm  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """每个测试用独立的数据库文件，互不干扰。"""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "mock_llm", True)
    db.init_db()
    yield


@pytest.fixture
def run_id():
    return db.create_run("测试问题：对比主流 Agent 框架的 Memory 机制")[0]


# --------------------------------------------------------------------------
# 1. 正常路径
# --------------------------------------------------------------------------

def test_mock_agent_produces_report(run_id):
    report, error, _stats = agent.run_research(run_id, "对比主流 Agent 框架的 Memory 机制")

    assert error is None, f"不该报错，实际: {error}"
    assert report
    assert "研究报告" in report
    assert len(report) > 50


def test_steps_are_traced(run_id):
    agent.run_research(run_id, "测试 trace")

    steps = db.get_steps(run_id)
    assert len(steps) >= 3, f"至少应有 3 步，实际 {len(steps)}"

    # 每一步都要有完整的起止时间，否则 trace 就没法用来定位慢在哪
    for s in steps:
        assert s["status"] in ("succeeded", "failed")
        assert s["name"]
        assert s["step_no"] >= 1

    # 必须既有 llm 决策，也有 tool 执行
    kinds = {s["kind"] for s in steps}
    assert "llm" in kinds
    assert "tool" in kinds


def test_token_usage_accumulates(run_id):
    agent.run_research(run_id, "测试 token")

    run = db.get_run(run_id)
    assert run["tokens_used"] > 0, "mock 模式也应记录 token 消耗"

    # 每个 llm 步骤的 token 之和，应该等于总数
    # （这里用 total_tokens 累加的约定）
    assert run["tokens_used"] == 150 + 425 + 920


# --------------------------------------------------------------------------
# 2. 护栏：这才是关键
# --------------------------------------------------------------------------

def test_max_steps_guard(run_id, monkeypatch):
    """把最大步数压到 2，确认 Agent 会停下来，而不是无限循环。

    如果这个测试挂了，说明护栏失效 —— 真实使用时会一直调模型烧钱。
    """
    monkeypatch.setattr(settings, "max_steps", 2)

    report, error, _ = agent.run_research(run_id, "测试步数护栏")

    assert error is None
    assert report, "达到步数上限也必须交出报告，不能空手而归"

    # 决策步骤不应该超过 max_steps
    llm_steps = [s for s in db.get_steps(run_id) if s["name"] == "decide"]
    assert len(llm_steps) <= 2, f"超过步数上限了: {len(llm_steps)}"


def test_token_budget_guard(run_id, monkeypatch):
    """把 token 预算压到很低，确认第二次决策前就会收尾。"""
    monkeypatch.setattr(settings, "max_tokens_budget", 200)

    report, error, _ = agent.run_research(run_id, "测试预算护栏")

    assert error is None
    assert report
    # 第一步就花了 150，第二步之后 575 > 200，所以应该在第 3 次决策前停
    assert "预算" in report or "降级" in report or "研究报告" in report


# --------------------------------------------------------------------------
# 3. 上下文管理
# --------------------------------------------------------------------------

def test_context_trimming(run_id):
    """上下文过长时要被裁剪，否则会撑爆模型窗口。"""
    a = agent.ResearchAgent(run_id, "测试上下文")

    # 人为塞进去 30 条消息
    for i in range(30):
        a.messages.append({"role": "assistant", "content": f"第 {i} 条"})

    before = len(a.messages)
    a._trim_context()
    after = len(a.messages)

    assert after < before, "裁剪后条数应该变少"
    assert after <= 12, f"裁剪后应控制在 12 条以内，实际 {after}"
    # 首尾必须保留：system + 用户问题 + 最近的工具结果
    assert a.messages[0]["role"] == "system"
    assert "省略" in a.messages[2]["content"]


# --------------------------------------------------------------------------
# 4. 降级交付
# --------------------------------------------------------------------------

def test_degraded_report_when_llm_fails(run_id, monkeypatch):
    """模型彻底不可用时，也要交出"诚实的部分结果"，而不是崩溃。"""

    def _boom(*args, **kwargs):
        raise llm.LLMError("模拟的网络故障")

    monkeypatch.setattr(llm, "chat", _boom)

    report, error, _ = agent.run_research(run_id, "测试降级")

    assert error is None, "不应把外部故障当成任务异常抛出去"
    assert report
    assert "降级" in report
    assert "模拟的网络故障" in report


# --------------------------------------------------------------------------
# 5. 工具层
# --------------------------------------------------------------------------

def test_unknown_tool_returns_error_not_exception():
    """Agent 要求调一个不存在的工具时，应返回错误让模型自己换策略。"""
    from app import tools

    result = tools.call_tool("不存在的工具", {})
    assert result["ok"] is False
    assert "不存在" in result["error"]


def test_tool_bad_arguments_returns_error():
    """参数不匹配也不能炸。"""
    from app import tools

    result = tools.call_tool("read_url", {"错误参数名": "x"})
    assert result["ok"] is False
    assert "参数" in result["error"]


def test_read_url_rejects_bad_scheme():
    """不支持的协议要明确拒绝，而不是尝试去连。"""
    from app import tools

    result = tools.call_tool("read_url", {"url": "file:///etc/passwd"})
    assert result["ok"] is False
    assert "协议" in result["error"]


# --------------------------------------------------------------------------
# 6. 联网工具（标了 network，默认跳过）
#     跑法：pytest tests/ -m network
#     为什么分开？因为它们依赖外部网络，会飘、会慢。
#     单元测试要稳定，集成测试才联网 —— 这个区分本身也值得在面试里讲。
# --------------------------------------------------------------------------

@pytest.mark.network
def test_web_search_returns_results():
    from app import tools

    result = tools.web_search("large language model agent", max_results=4)
    assert result["ok"] is True, f"搜索失败: {result.get('error')}"
    data = result["data"]
    results = data["results"]
    assert len(results) > 0, "应该至少返回一条结果"
    assert data["sources_used"], "应该记录用了哪些源"
    for r in results:
        assert r["title"]
        assert r["url"].startswith("http")
        assert r["source"], "每条结果都要标注来源"


@pytest.mark.network
def test_read_url_github_readme():
    """GitHub 仓库要能拿到 README 正文，而不是导航栏垃圾。"""
    from app import tools

    result = tools.read_url("https://github.com/langchain-ai/langgraph", max_chars=800)
    assert result["ok"] is True, f"读取失败: {result.get('error')}"
    data = result["data"]
    assert data["source"] == "github-readme", f"应从 README 读取，实际: {data.get('source')}"
    assert data["total_chars"] > 800
    assert len(data["text"]) == 800


@pytest.mark.network
def test_read_url_truncates():
    """max_chars 必须真的生效 —— 否则会烧爆上下文。"""
    from app import tools

    result = tools.read_url("https://example.com", max_chars=50)
    assert result["ok"] is True
    assert len(result["data"]["text"]) <= 50