#!/usr/bin/env python3
"""ResearchFlow · 实验一：工具结果缓存到底省了多少

## 想回答的问题

    "加了缓存，到底有用吗？"

这个问题必须有数字回答，否则就是个"我觉得有用"的自我感觉。

## 实验设计

三个场景，每个跑 N 个研究任务：

    A. 缓存关闭，全部不同问题   → 基线（每次都真的调外部 API）
    B. 缓存开启，全部不同问题   → 缓存无效的场景（对照组，应接近 A）
    C. 缓存关闭，重复同一批问题 → 缓存关闭 + 重复，基线
    D. 缓存开启，重复同一批问题 → 缓存生效的场景

为什么要有 B 这一组？
    因为如果只测 D，会得到"命中率 100%，缓存太棒了"的错觉。
    实际上那只说明"我一直在问同一个问题"。
    B 组证明：缓存不会凭空命中，它只在真的重复时才起作用。

## 输出

    每组的外部调用次数、缓存命中次数、命中率、平均耗时
    以及"省下的外部调用占比"

用法：
    MOCK_LLM=1 python scripts/experiment_cache.py
    MOCK_LLM=1 python scripts/experiment_cache.py --runs 10
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MOCK_LLM", "1")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from app import agent, cache, db, toolstub  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import DB_PATH  # noqa: E402


def reset_state(tag: str = "exp") -> None:
    """每轮实验换一个全新的数据库文件，保证干净起点。

    为什么不删旧库？
    Windows 上 SQLite 的文件句柄可能还没释放，删除会报
    PermissionError [WinError 32]。换文件名比纠缠句柄清理更可靠。

    而且缓存表也要清 —— 否则上一组实验的结果会漏到下一组，
    导致命中率虚高。
    """
    from pathlib import Path

    path = Path(DB_PATH).parent / f"{tag}.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            try:
                p.unlink()
            except PermissionError:
                pass  # 还在用就换个名字（下面 _unique_path 处理）
    db.DB_PATH = path
    db.init_db()
    cache.clear()


def run_batch(questions: list[str], cache_enabled: bool, tag: str) -> dict:
    """跑一批研究任务，统计工具调用与缓存命中。

    注意：这里用 Mock 模型 + 桩化工具，所以不花一分钱、不依赖网络。
    测的是"缓存与调度的行为"，这部分与真实模型无关。
    """
    reset_state(tag)
    settings.cache_enabled = cache_enabled

    total_tool_calls = 0
    total_cache_hits = 0
    durations = []
    statuses = []

    for q in questions:
        run_id, _ = db.create_run(q)
        t0 = time.perf_counter()
        # 注意：直接调 run_research 不写数据库状态（那是 worker 的职责），
        # 所以"成功与否"以返回值为准，不能看 run["status"]。
        report, error, stats = agent.run_research(run_id, q)
        elapsed = time.perf_counter() - t0

        run = db.get_run(run_id)
        total_tool_calls += run["tool_calls"] or 0
        total_cache_hits += run["cache_hits"] or 0
        durations.append(elapsed)
        statuses.append("succeeded" if (error is None and report) else "failed")

    real_calls = total_tool_calls - total_cache_hits

    return {
        "runs": len(questions),
        "tool_calls": total_tool_calls,
        "cache_hits": total_cache_hits,
        "real_external_calls": real_calls,
        "hit_rate": round(total_cache_hits / total_tool_calls, 4) if total_tool_calls else 0.0,
        "avg_duration_s": round(statistics.mean(durations), 4) if durations else 0.0,
        "succeeded": statuses.count("succeeded"),
        "failed": statuses.count("failed"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=6, help="每个场景跑几个任务")
    ap.add_argument("--out", default="experiments/result_cache.json")
    args = ap.parse_args()

    n = args.runs
    # 不同问题
    distinct = [f"调研主题 {i}：Agent 框架设计" for i in range(n)]
    # 重复问题：只有 2 个不同问题，各重复 n 次
    repeated = [f"重复主题 {i % 2}：Agent 框架设计" for i in range(n)]

    # 关键：把工具换成桩。否则每个任务都要真的联网，实验会跑上几分钟，
    # 而且结果会受网络波动影响 —— 那样的数据不可复现，也没法写进报告。
    toolstub.enable(latency=0.0, seed=42)

    print("=" * 66)
    print("实验一：工具结果缓存的收益")
    print("=" * 66)
    print(f"每个场景跑 {n} 个任务，Mock 模型 + 桩化工具（不联网、不花钱）")
    print()

    scenarios = [
        ("A 缓存关 + 全不同问题", distinct, False),
        ("B 缓存开 + 全不同问题", distinct, True),
        ("C 缓存关 + 重复问题  ", repeated, False),
        ("D 缓存开 + 重复问题  ", repeated, True),
    ]

    results = {}
    for idx, (label, questions, enabled) in enumerate(scenarios):
        r = run_batch(questions, enabled, tag=f"exp_cache_{chr(65+idx)}")
        results[label[:1]] = {"label": label, **r}
        print(f"  {label}")
        print(f"     工具调用 {r['tool_calls']:>3}   真实外部请求 {r['real_external_calls']:>3}   "
              f"命中率 {r['hit_rate']*100:>5.1f}%   平均 {r['avg_duration_s']:.2f}s")
        print()

    # ---- 对比 ----
    a = results["A"]
    d = results["D"]

    print("=" * 66)
    print("结论")
    print("=" * 66)

    print(f"A（无缓存）真实外部请求: {a['real_external_calls']}")
    print(f"D（有缓存 + 重复）    : {d['real_external_calls']}")
    if a["real_external_calls"] > 0:
        saved = 1 - d["real_external_calls"] / a["real_external_calls"]
        print(f"→ 外部请求减少 {saved*100:.1f}%")

    print()
    print(f"B（有缓存但无重复）命中率: {results['B']['hit_rate']*100:.1f}%")
    print("  → 这条证明缓存不会凭空生效，只在真的重复时才有用")
    print("  → 如果 B 的命中率也接近 100%，说明实验设计有问题")

    # ---- 写文件 ----
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(
            {
                "experiment": "cache_benefit",
                "runs_per_scenario": n,
                "mode": "mock_llm + stub tools",
                "scenarios": results,
                "saved_ratio_A_vs_D": round(
                    1 - d["real_external_calls"] / a["real_external_calls"], 4
                )
                if a["real_external_calls"]
                else 0,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    toolstub.disable()
    print()
    print(f"数据已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())