"""ResearchFlow · Worker（阶段 1：真 Agent）

阶段 1 的变化：把阶段 0 的"假剧本"换成真正的 Agent Loop。

但有一件事**没变**，而且是有意保留的：
    Worker 依然是 asyncio 后台任务，依然只有一个进程、一个内存队列。

也就是说，阶段 0 埋下的问题（服务重启后任务卡在 running、
没有幂等、重启就丢任务）**现在全部还在**。
这是故意的 —— 阶段 2 会带着真实 Agent 一起解决它们。
先把"能跑"和"跑得对"分开，别一次改两件事。

一个关键实现细节：
    Agent 的循环是**同步阻塞**的（urllib 发请求、sqlite 读写）。
    在 asyncio 里直接调用会卡住整个事件循环，让 API 完全没响应。
    所以这里用 asyncio.to_thread 把它扔到线程池里跑。
    —— 这也是阶段 2 要正视的"同步阻塞"问题的第一个体现。
"""

from __future__ import annotations

import asyncio
import time

from . import agent, db
from .config import settings

# 同时最多跑几个研究任务。多出来的排队等待。
MAX_CONCURRENCY = 2

_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

# 持有后台任务的引用。不持有的话，asyncio 可能在任务跑完前把它回收掉。
_background: set[asyncio.Task] = set()


async def _run_agent(run_id: str, question: str) -> None:
    db.mark_running(run_id)
    t0 = time.perf_counter()

    # 关键：把同步的 Agent 循环丢到线程池，别堵住事件循环。
    # to_thread 会让出事件循环，所以 10 秒的研究任务不会让 /health 无响应。
    report, error = await asyncio.to_thread(agent.run_research, run_id, question)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    db.set_run_duration(run_id, elapsed_ms)

    if error:
        db.mark_finished(run_id, error=error)
    else:
        db.mark_finished(run_id, report=report)


async def _wrapped(run_id: str, question: str) -> None:
    """用信号量限制并发，并且保证异常不会让任务永远卡在 running。"""
    async with _semaphore:
        try:
            await _run_agent(run_id, question)
        except Exception as exc:  # noqa: BLE001
            db.mark_finished(run_id, error=f"{type(exc).__name__}: {exc}")


async def submit(run_id: str, question: str) -> None:
    """把任务丢到后台执行，然后立刻返回。

    这个函数是"异步"的关键：
    API 不是等研究跑完才响应，而是先记下任务、马上返回 run_id。
    """
    task = asyncio.create_task(_wrapped(run_id, question))
    _background.add(task)
    task.add_done_callback(_background.discard)


def stats() -> dict[str, int | str]:
    """给 /health 看当前后台有多少任务在跑/在排队。"""
    running = sum(1 for t in _background if not t.done())
    return {
        "background_tasks": running,
        "max_concurrency": MAX_CONCURRENCY,
        "waiting": max(0, running - MAX_CONCURRENCY),
        "mode": "mock" if settings.mock_llm else "real",
    }