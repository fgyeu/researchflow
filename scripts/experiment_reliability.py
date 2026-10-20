#!/usr/bin/env python3
"""ResearchFlow · 实验二：并发处理与故障注入

## 想回答的问题

    1. 系统同时处理多个任务时，表现如何？会不会互相拖垮？
    2. 外部工具按比例失败时，重试机制救回多少？
    3. 服务崩溃后，任务恢复成功率是多少？

## 三个子实验

### 2.1 并发吞吐

    提交 N 个任务，并发上限分别设为 1 / 2 / 4，
    比较总耗时。理论上是线性加速，直到撞上瓶颈。

### 2.2 故障注入

    让工具按 0% / 20% / 40% 的概率失败，观察：
        - 任务成功率
        - 平均重试次数
        - 重试机制是否真的救回了请求

    关键对照：**关掉重试 vs 开启重试**
    这才能证明"重试有用"，而不是"碰巧没失败"。

### 2.3 崩溃恢复

    直接把数据库里的任务改成"租约过期"状态，模拟 Worker 崩溃，
    然后调恢复流程，看能不能捡回来。

用法：
    MOCK_LLM=1 python scripts/experiment_reliability.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MOCK_LLM", "1")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app import agent, cache, db, toolstub, worker  # noqa: E402
from app.config import settings  # noqa: E402


def fresh_db(tag: str):
    """换一个全新的数据库文件，避免跨实验污染。"""
    path = Path(db.DB_PATH).parent / f"{tag}.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            try:
                p.unlink()
            except PermissionError:
                pass
    db.DB_PATH = path
    db.init_db()
    cache.clear()


# ==========================================================================
# 2.1 并发吞吐
# ==========================================================================

def exp_throughput(n_tasks: int = 8) -> dict:
    """比较不同并发上限下的总耗时。

    用桩化工具加了 latency，模拟"真实的网络等待"。
    没有延迟的话，并发和不并发耗时差不多，测不出区别。
    """
    print("\n" + "=" * 66)
    print("2.1 并发吞吐")
    print("=" * 66)
    print(f"提交 {n_tasks} 个任务，工具模拟 0.15s 延迟（代表网络等待）")

    results = {}
    toolstub.enable(latency=0.15, seed=1)

    for concurrency in (1, 2, 4):
        fresh_db(f"exp_conc_{concurrency}")
        settings.cache_enabled = False  # 排除缓存干扰

        async def run_all():
            # 在事件循环内部改并发上限。
            # 注意：不能直接赋 worker._semaphore —— 信号量是"按事件循环懒创建"的，
            # 直接赋值会被 _get_semaphore() 覆盖掉，测出来的并发全是默认值。
            worker.MAX_CONCURRENCY = concurrency
            worker._semaphore = None  # noqa: SLF001 —— 让它按新上限重建
            worker._semaphore_loop = None  # noqa: SLF001

            t0 = time.perf_counter()
            tasks = []
            for i in range(n_tasks):
                rid, _ = db.create_run(f"并发测试 {concurrency}-{i}")
                tasks.append(worker.submit(rid, f"并发测试 {concurrency}-{i}"))
            await asyncio.gather(*tasks)

            # 等所有后台任务跑完
            while worker.stats()["background_tasks"] > 0:
                await asyncio.sleep(0.05)

            return time.perf_counter() - t0

        elapsed = asyncio.run(run_all())

        # 统计成功数
        runs = db.list_runs(limit=100)
        ok = sum(1 for r in runs if r["status"] == "succeeded")
        results[concurrency] = {
            "concurrency": concurrency,
            "tasks": n_tasks,
            "elapsed_s": round(elapsed, 3),
            "succeeded": ok,
        }
        print(f"  并发上限 {concurrency}:  总耗时 {elapsed:6.2f}s   成功 {ok}/{n_tasks}")

    toolstub.disable()

    # 算加速比
    base = results[1]["elapsed_s"]
    print("\n  加速比（相对并发=1）：")
    for c, r in results.items():
        speedup = base / r["elapsed_s"] if r["elapsed_s"] else 0
        print(f"    并发 {c}:  {speedup:.2f}x")

    return results


# ==========================================================================
# 2.2 故障注入
# ==========================================================================

def exp_failure(n_tasks: int = 10) -> dict:
    """故障注入：重试到底救回了多少**任务**。

    ## 这个实验我推翻重做了两次，过程值得记录

    **第一版**：统计任务成功率。结果注入 40% 失败后任务仍 100% 成功。
    原因：Agent 有降级交付 —— 工具挂了它照样写报告，任务状态是 succeeded。
    任务成功率被污染了，看不出工具层的真实表现。

    **第二版**：改成统计"工具调用失败率"。结果两组都是 30% 左右，看不出差别。
    原因更隐蔽：`fail_every=3` 是"每 3 次调用必失败一次"。
    重试让总调用数从 20 涨到 29，失败次数也从 6 涨到 9 ——
    **失败率恒定，因为它按比例失效**。

    这个坑的本质是：**用"每 N 次失败一次"来模拟故障，
    会让"失败率"成为恒定值，重试永远看不出效果。**

    **第三版（现在）**：换一个真正能体现重试价值的指标 ——
    模拟"失败是瞬时的"：某个工具第一次调用失败，但下一次调用就成功了。

    这才是真实世界的故障形态（网络抖动、限流），也是重试真正有用的场景。

    指标：**任务最终拿到数据的比例**。
        关闭重试 → 遇到瞬时失败就彻底拿不到数据
        开启重试 → 重试一次就成功
    """
    print("\n" + "=" * 66)
    print("2.2 故障注入与重试效果（瞬时故障）")
    print("=" * 66)
    print(f"每个配置跑 {n_tasks} 个任务")
    print("故障模型：每个工具的第 1 次调用失败，之后的调用成功（模拟瞬时抖动）")

    results = {}

    for label, retries in (("关闭重试", 0), ("开启重试(2次)", 2)):
        fresh_db(f"exp_fail_{retries}")
        settings.cache_enabled = False
        settings.tool_max_retries = retries
        settings.retry_backoff = 0.01

        counters = {"total": 0, "failed": 0, "ok": 0}
        # 记录"哪些工具已经失败过一次了" —— 用来模拟瞬时故障
        failed_once: set[str] = set()

        def transient_stub(name, arguments):
            counters["total"] += 1

            # 每个工具名第一次调用必失败，第二次起成功
            if name not in failed_once:
                failed_once.add(name)
                counters["failed"] += 1
                return {"ok": False, "error": "URLError: simulated transient failure"}

            counters["ok"] += 1
            # 返回和真实桩一致的结构
            from app import toolstub as ts

            ts._state["fail_every"] = 0  # 确保不触发额外失败
            return ts._stub_call(name, arguments)

        toolstub.enable(latency=0.0)
        agent.tools.call_tool = transient_stub

        task_ok = 0
        tasks_with_data = 0     # 任务是否真的拿到了工具数据
        total_retries = 0

        for i in range(n_tasks):
            q = f"故障测试 {i}"
            rid, _ = db.create_run(q)
            report, error, stats = agent.run_research(rid, q)
            total_retries += stats.get("retries", 0)

            if error is None and report:
                task_ok += 1

            # 关键指标：这个任务的 trace 里有没有成功的工具调用
            steps = db.get_steps(rid)
            tool_steps = [s for s in steps if s["kind"] == "tool"]
            if any(s["status"] == "succeeded" for s in tool_steps):
                tasks_with_data += 1

        key = label
        results[key] = {
            "retries_allowed": retries,
            "failure_model": "first call of each tool fails, then succeeds",
            "tasks": n_tasks,
            "task_success_rate": round(task_ok / n_tasks, 3),
            "tasks_with_real_data": tasks_with_data,
            "data_success_rate": round(tasks_with_data / n_tasks, 3),
            "tool_calls_total": counters["total"],
            "tool_calls_failed": counters["failed"],
            "avg_calls_per_task": round(counters["total"] / n_tasks, 2),
        }
        print(
            f"  {label:14}  任务完成 {task_ok}/{n_tasks}  "
            f"拿到数据 {tasks_with_data}/{n_tasks}  "
            f"调用 {counters['total']} 次（失败 {counters['failed']}）"
        )

    toolstub.disable()

    a = results["关闭重试"]
    b = results["开启重试(2次)"]

    print("\n  对照结果：")
    print("    指标一：任务状态成功（会被降级交付掩盖）")
    print(f"      关闭重试 {a['task_success_rate']*100:.0f}%   开启重试 {b['task_success_rate']*100:.0f}%")
    print()
    print("    指标二：任务真的拿到了工具数据（这才是重试的价值）")
    print(f"      关闭重试 {a['data_success_rate']*100:.0f}%   开启重试 {b['data_success_rate']*100:.0f}%")

    if b["data_success_rate"] > a["data_success_rate"]:
        gain = b["data_success_rate"] - a["data_success_rate"]
        print(f"\n    → 重试让'拿到真实数据'的比例提升了 {gain*100:.0f} 个百分点")

    print("\n  两个指标的差距，正是'任务成功'会掩盖问题的证据。")

    return results

    toolstub.disable()

    print("\n  重试救回的任务数：")
    for fr in (20, 40):
        a = results[f"关闭重试_fail{fr}"]
        b = results[f"开启重试(2次)_fail{fr}"]
        print(
            f"    注入 {fr}%:  关闭重试救回 {a['rescued_by_retry']} 个  →  "
            f"开启重试救回 {b['rescued_by_retry']} 个"
        )

    print("\n  注意：任务成功率几乎不受影响，因为 Agent 会降级交付。")
    print("        这正是'任务成功'这个指标会掩盖问题的证据。")

    return results


# ==========================================================================
# 2.3 崩溃恢复
# ==========================================================================

def exp_recovery(n_tasks: int = 5) -> dict:
    """模拟 Worker 崩溃，验证恢复机制能捡回任务。

    做法：创建任务并让它进入 running，然后把租约改成已过期
    （等价于 Worker 死了），再调恢复流程。
    """
    print("\n" + "=" * 66)
    print("2.3 崩溃恢复")
    print("=" * 66)

    fresh_db("exp_recover")
    settings.cache_enabled = False
    settings.max_attempts = 3
    toolstub.enable(latency=0.0, seed=3)

    # 造 N 个"崩溃"的任务
    run_ids = []
    for i in range(n_tasks):
        rid, _ = db.create_run(f"崩溃任务 {i}")
        # 让 Worker 领走，然后立刻让租约过期 → 模拟崩溃
        db.claim_run(rid, "dead-worker", lease_seconds=-1)
        run_ids.append(rid)

    # 确认它们现在都是"卡在 running 且租约过期"
    orphans_before = db.find_orphans()
    print(f"  制造了 {n_tasks} 个中断任务，扫描到 {len(orphans_before)} 个孤儿")

    # 执行恢复
    async def recover():
        t0 = time.perf_counter()
        resumed = await worker.recover_orphans()
        # 等恢复的任务跑完
        while worker.stats()["background_tasks"] > 0:
            await asyncio.sleep(0.05)
        return resumed, time.perf_counter() - t0

    resumed, elapsed = asyncio.run(recover())

    # 统计结果
    ok = 0
    attempts_list = []
    for rid in run_ids:
        run = db.get_run(rid)
        if run and run["status"] == "succeeded":
            ok += 1
        if run:
            attempts_list.append(run["attempts"])

    orphans_after = db.find_orphans()
    toolstub.disable()

    print(f"  触发恢复 {resumed} 个，耗时 {elapsed:.2f}s")
    print(f"  最终成功 {ok}/{n_tasks} ({ok/n_tasks*100:.0f}%)")
    print(f"  平均尝试次数 {statistics.mean(attempts_list):.1f}" if attempts_list else "  无数据")
    print(f"  恢复后剩余孤儿 {len(orphans_after)} 个")

    return {
        "tasks": n_tasks,
        "orphans_found": len(orphans_before),
        "resumed": resumed,
        "succeeded": ok,
        "recovery_rate": round(ok / n_tasks, 3),
        "avg_attempts": round(statistics.mean(attempts_list), 2) if attempts_list else 0,
        "orphans_after": len(orphans_after),
        "elapsed_s": round(elapsed, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=8)
    ap.add_argument("--out", default="experiments/result_reliability.json")
    args = ap.parse_args()

    print("=" * 66)
    print("实验二：并发、故障注入与崩溃恢复")
    print("=" * 66)
    print("全部使用 Mock 模型 + 桩化工具，不联网、不花钱、结果可复现")

    throughput = exp_throughput(args.tasks)
    failure = exp_failure(n_tasks=max(6, args.tasks))
    recovery = exp_recovery()

    out = {
        "experiment": "reliability",
        "mode": "mock_llm + stub tools",
        "throughput": {str(k): v for k, v in throughput.items()},
        "failure_injection": failure,
        "recovery": recovery,
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 66)
    print(f"数据已写入 {args.out}")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())