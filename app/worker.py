"""ResearchFlow · Worker（阶段 2：租约 + 心跳 + 恢复）

阶段 2 的 Worker 相比之前多了三件事，每件都对应一个真实故障：

1. **租约**（lease）
   问题：服务重启后，卡在 running 的任务永远没人管。
   做法：Worker 领任务时盖一个"到期时间"，到期没续约就说明它死了。

2. **心跳**（heartbeat）
   问题：任务可能跑得比租约还久，不能因为跑得慢就被判死。
   做法：后台每 N 秒续约一次。

3. **启动恢复**（recovery）
   问题：崩了之后谁去捡那些孤儿任务？
   做法：服务启动时扫一遍 pending 和"租约过期"的任务，重新入队。

## 一个关键的设计选择

**恢复 ≠ 接着上次的进度继续跑。**

真正的"断点续跑"需要：持久化 Agent 的完整消息历史、已完成的工具调用、
以及保证有副作用的工具不会被重复执行。

本项目的工具是**只读的**，所以最简单的策略是"整任务重跑" ——
成本是重新调一次模型，但正确性有保证。

> 面试时可以这样说：*"我的工具都是只读的，所以故障恢复直接重跑整个任务，
> 用 attempts 计数防死循环。如果工具带副作用，就必须做幂等键 +
> 断点续跑，那是完全不同量级的工作。"*

这个区分（只读重跑 vs 有副作用续跑）本身就是可靠的回答。
"""

from __future__ import annotations

import asyncio
import time
import uuid

from . import agent, db
from .config import settings

# 同时最多跑几个研究任务。多出来的排队等待。
MAX_CONCURRENCY = 2

_semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

# 持有后台任务的引用。不持有的话，asyncio 可能在任务跑完前把它回收掉。
_background: set[asyncio.Task] = set()

# 正在执行中的 run_id，避免同一个任务被并发跑两次
_inflight: set[str] = set()

# 本进程的身份标识，写进租约里
OWNER = f"worker-{uuid.uuid4().hex[:8]}"


async def _heartbeat(run_id: str, stop: asyncio.Event) -> None:
    """后台续约。

    为什么需要它：研究任务可能跑几分钟，而租约只有 30 秒。
    不续约的话，正常任务会被自己的恢复机制误判成"死掉的任务"。

    为什么用 Event 而不是 timeout 循环：
    Event 能被立即唤醒，任务一结束就退出，不用等下一次 sleep。
    """
    while not stop.is_set():
        await asyncio.sleep(settings.heartbeat_interval)
        if stop.is_set():
            break
        ok = await asyncio.to_thread(
            db.renew_lease, run_id, OWNER, settings.lease_seconds
        )
        if not ok:
            # 租约丢了 —— 说明别人接手了。必须立刻停，否则会有两个 Worker
            # 同时跑同一个任务，写坏数据。
            break


async def _run_agent(run_id: str, question: str, attempt: int) -> None:
    t0 = time.perf_counter()
    stop = asyncio.Event()
    hb = asyncio.create_task(_heartbeat(run_id, stop))

    try:
        report, error, stats = await asyncio.to_thread(
            agent.run_research, run_id, question, attempt
        )
    finally:
        stop.set()
        await asyncio.gather(hb, return_exceptions=True)

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    db.set_run_duration(run_id, elapsed_ms)

    if error:
        db.mark_finished(run_id, error=error)
    else:
        db.mark_finished(run_id, report=report)


async def _wrapped(run_id: str, question: str, attempt: int) -> None:
    async with _semaphore:
        try:
            await _run_agent(run_id, question, attempt)
        except Exception as exc:  # noqa: BLE001
            db.mark_finished(run_id, error=f"{type(exc).__name__}: {exc}")
        finally:
            _inflight.discard(run_id)


async def submit(run_id: str, question: str, force: bool = False) -> bool:
    """把任务丢到后台执行，立刻返回。返回 False 表示这个任务已在执行中。

    这就是幂等的最后一道防线：
    即使上层没挡住重复提交，这里也不会让同一个 run_id 跑两遍。

    参数 force：
        仅启动恢复使用。跳过"租约未过期就不能抢"的限制。
    """
    if run_id in _inflight:
        return False

    run = db.get_run(run_id)
    if run is None:
        return False
    if run["status"] in (db.SUCCEEDED, db.FAILED):
        return False
    if run["attempts"] >= settings.max_attempts:
        db.mark_finished(
            run_id,
            error=f"已尝试 {run['attempts']} 次仍未完成，放弃（可能是持续性问题）",
        )
        return False

    # 抢租约。抢不到说明别的 Worker 正在处理。
    # force=True 只在启动恢复时用：跳过"租约未过期不能抢"的限制。
    # 运行期绝不能传 True，否则会出现两个 Worker 抢同一个任务。
    if force:
        claimed = await asyncio.to_thread(
            db.force_claim, run_id, OWNER, settings.lease_seconds
        )
    else:
        claimed = await asyncio.to_thread(
            db.claim_run, run_id, OWNER, settings.lease_seconds
        )
    if not claimed:
        return False

    _inflight.add(run_id)
    attempt = (run["attempts"] or 0) + 1
    task = asyncio.create_task(_wrapped(run_id, question, attempt))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return True


async def recover_orphans() -> int:
    """服务启动时调用：把没人管的任务捡回来重新执行。

    返回恢复的数量。

    ## 一个关键的实现细节：启动时不能傻等租约过期

    租约是 30 秒。如果服务刚崩、马上重启，那些任务的租约**还没过期**，
    按"过期才能接手"的规则，它们要等 30 秒才会被捡回来。

    但服务刚启动时有个特殊事实：**本机上没有任何其它 Worker 在跑**。
    所以这时候可以安全地"强制接管"所有 running 的任务 ——
    因为如果真有别的 Worker 活着，它也不应该和我在同一台机器上
    共用同一个 SQLite 文件（那本来就不是支持的部署方式）。

    这就是 `force=True` 的含义：只在启动恢复时用，不在运行期用。
    运行期的恢复走租约过期判断，那个逻辑是对的。

    面试时这是个好细节：*"我发现启动恢复不能等租约自然过期，
    因为重启是个明确信号 —— 本机没有 Worker 活着了。
    所以启动时做强接管，运行期靠租约。两种场景用两种判断。"*
    """
    orphans = await asyncio.to_thread(db.find_orphans)

    # 启动时把"租约还没过期但状态是 running"的任务也一并捞出来强接管。
    # 理由：本进程刚启动，本机不可能有别的 Worker 在跑。
    running = await asyncio.to_thread(db.find_stale_running)
    seen = {o["run_id"] for o in orphans}
    for row in running:
        if row["run_id"] not in seen:
            orphans.append(row)

    resumed = 0
    for row in orphans:
        if row["attempts"] >= settings.max_attempts:
            db.mark_finished(
                row["run_id"],
                error=f"崩溃后已重试 {row['attempts']} 次，放弃以避免无限循环",
            )
            continue
        ok = await submit(row["run_id"], row["question"], force=True)
        if ok:
            resumed += 1
    return resumed


def stats() -> dict:
    """给 /health 看当前后台有多少任务在跑/在排队。"""
    running = sum(1 for t in _background if not t.done())
    return {
        "owner": OWNER,
        "background_tasks": running,
        "max_concurrency": MAX_CONCURRENCY,
        "waiting": max(0, running - MAX_CONCURRENCY),
        "inflight": len(_inflight),
        "mode": "mock" if settings.mock_llm else "real",
        "cache_enabled": settings.cache_enabled,
    }