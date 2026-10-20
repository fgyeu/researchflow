"""ResearchFlow · Worker（阶段 0：假 Agent）

阶段 0 的 Worker 不调用任何模型，只按固定剧本模拟"一个研究任务要跑好几步"。

它的唯一使命，是让你先亲眼看到完整链路真的通了：

    提交任务 → 立刻返回 run_id → 后台真的在跑 → 状态在变 → 每一步都留痕

阶段 1 才会把这个假剧本换成真正的 Agent Loop（调用 DeepSeek + 真工具）。

为什么要先做假的？
    因为"异步 + 状态机 + trace 这条链路通不通"和"模型聪不聪明"是两件独立的事。
    一起做，出错时你分不清是哪一半坏了。
"""

from __future__ import annotations

import asyncio
import time

from . import db

# 同时最多跑几个研究任务。多出来的排队等待。
MAX_CONCURRENCY = 2

_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

# 持有后台任务的引用。不持有的话，asyncio 可能在任务跑完前把它回收掉。
_background: set[asyncio.Task] = set()

# 阶段 0 的"剧本"：(类型, 名字, 在干嘛)
_PLAN: list[tuple[str, str, str]] = [
    ("llm", "plan", "拆解研究问题"),
    ("tool", "web_search", "搜索相关资料"),
    ("tool", "read_url", "阅读并提取要点"),
    ("llm", "write_report", "汇总生成研究报告"),
]


async def _run_fake_agent(run_id: str, question: str) -> None:
    """模拟一次完整的研究流程：每一步都写进 trace。"""
    db.mark_running(run_id)

    notes: list[str] = []
    for step_no, (kind, name, what) in enumerate(_PLAN, start=1):
        step_id = db.add_step(
            run_id=run_id,
            step_no=step_no,
            kind=kind,
            name=name,
            payload=f"{what}：{question}",
        )
        t0 = time.perf_counter()

        # 假装这一步要花点时间（真实世界里这里会是模型调用或网络请求）
        await asyncio.sleep(0.8 + 0.4 * step_no)

        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        output = f"[阶段0 模拟] {what} —— 完成"
        db.finish_step(step_id, "succeeded", output, elapsed_ms)
        notes.append(output)

    report = (
        f"# 模拟研究报告\n\n"
        f"**研究问题**：{question}\n\n"
        f"## 执行过程\n"
        + "\n".join(f"{i}. {n}" for i, n in enumerate(notes, start=1))
        + "\n\n> 阶段 0 的产物是假报告。阶段 1 接入真实模型后，这里会是真正的分析内容。\n"
    )
    db.mark_finished(run_id, report=report)


async def _wrapped(run_id: str, question: str) -> None:
    """用信号量限制并发，并且保证异常不会让任务永远卡在 running。"""
    async with _semaphore:
        try:
            await _run_fake_agent(run_id, question)
        except Exception as exc:  # noqa: BLE001 —— 阶段 0 先兜住，阶段 2 会细分错误类型
            db.mark_finished(run_id, error=f"{type(exc).__name__}: {exc}")


async def submit(run_id: str, question: str) -> None:
    """把任务丢到后台执行，然后立刻返回。

    这个函数是"异步"的关键：
    API 不是等研究跑完才响应，而是先记下任务、马上返回 run_id。
    """
    task = asyncio.create_task(_wrapped(run_id, question))
    _background.add(task)
    task.add_done_callback(_background.discard)


def stats() -> dict[str, int]:
    """给 /health 看当前后台有多少任务在跑/在排队。"""
    running = sum(1 for t in _background if not t.done())
    return {
        "background_tasks": running,
        "max_concurrency": MAX_CONCURRENCY,
        "waiting": max(0, running - MAX_CONCURRENCY),
    }