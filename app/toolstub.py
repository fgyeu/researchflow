"""ResearchFlow · 工具桩（实验与测试专用）

## 为什么需要它

做缓存、并发、恢复实验时，如果工具真的去联网：

1. 每次实验要等几十秒网络（几十个任务就是几分钟）
2. 结果受网络波动影响，同样的实验两次结果不一样
3. 可能撞上对方限流，实验直接失败
4. 无法模拟"工具很慢"或"工具必失败"这类关键场景

所以实验必须能把工具换成可控的桩（stub）。

## 怎么用

    from app import tools, toolstub

    # 让工具立即返回假数据
    toolstub.enable(latency=0.0)

    # 让工具慢一点（模拟网络延迟）
    toolstub.enable(latency=0.2)

    # 让工具按比例失败（测重试和降级）
    toolstub.enable(fail_rate=0.3)

    # 用完恢复真实工具
    toolstub.disable()

## 为什么做成"可注入"而不是改 tools.py

如果直接在 tools.py 里写 `if settings.mock:`，真实代码里会混进实验逻辑，
既难维护，也容易在真实运行时误触发。

注入的方式把"测试关注点"和"生产代码"分开 —— 这是更干净的做法。
"""

from __future__ import annotations

import random
import time

from . import tools

_original_call = tools.call_tool

_state = {
    "enabled": False,
    "latency": 0.0,
    "fail_rate": 0.0,
    "seed": None,
    # 硬失败模式：所有调用都失败，用来测"工具彻底不可用"时的降级
    "hard_fail": False,
    # 确定性失败模式：每 fail_every 次调用失败一次
    "fail_every": 0,
    "call_count": 0,
}


def _stub_call(name: str, arguments: dict) -> dict:
    """假的工具实现：模拟延迟与失败，返回结构化的假结果。

    ## 两种失败模式，为什么要两种

    随机失败（fail_rate）看起来更真实，但有个致命问题：
    **样本小的时候波动巨大**。实测 20% 的失败率，16 次调用可能失败 7 次。

    做对照实验时这会毁掉结论 —— 你分不清"重试有用"还是"这轮运气不好"。

    所以加一种确定性模式（fail_every）：
        每 N 次调用**必然**失败一次。
    这样两组实验的失败输入完全相同，唯一变量就是"有没有重试"。
    这才是干净的对照实验。

    随机模式留着，用来做"真实波动下表现如何"的补充实验。
    """
    _state["call_count"] += 1

    latency = _state["latency"]
    if latency:
        time.sleep(latency)

    if _state["hard_fail"]:
        return {"ok": False, "error": "URLError: simulated permanent failure"}

    # 确定性失败：每 N 次失败一次
    fail_every = _state["fail_every"]
    if fail_every and _state["call_count"] % fail_every == 0:
        return {"ok": False, "error": "URLError: simulated transient failure"}

    fail_rate = _state["fail_rate"]
    if fail_rate and random.random() < fail_rate:
        return {"ok": False, "error": "URLError: simulated transient failure"}

    if name == "web_search":
        return {
            "ok": True,
            "data": {
                "query": arguments.get("query", ""),
                "results": [
                    {
                        "title": f"stub result for {arguments.get('query', '')}",
                        "url": "https://example.com/stub",
                        "snippet": "stub snippet",
                        "source": "stub",
                    }
                ],
                "sources_used": ["stub"],
            },
        }
    if name == "read_url":
        return {
            "ok": True,
            "data": {
                "url": arguments.get("url", ""),
                "text": "stub page content " * 20,
                "total_chars": 360,
                "truncated": False,
            },
        }
    return {"ok": False, "error": f"stub 不支持这个工具: {name}"}


def enable(
    latency: float = 0.0,
    fail_rate: float = 0.0,
    seed: int | None = None,
    hard_fail: bool = False,
    fail_every: int = 0,
) -> None:
    """打开桩。所有工具调用都会走假实现。

    参数：
        latency     每次调用固定延迟（秒），模拟网络等待
        fail_rate   随机失败概率（0~1）
        hard_fail   所有调用都失败
        fail_every  确定性失败：每 N 次调用失败一次（做对照实验首选）
    """
    _state.update(
        enabled=True,
        latency=latency,
        fail_rate=fail_rate,
        seed=seed,
        hard_fail=hard_fail,
        fail_every=fail_every,
        call_count=0,
    )
    if seed is not None:
        random.seed(seed)
    tools.call_tool = _stub_call


def disable() -> None:
    """恢复真实工具。"""
    _state["enabled"] = False
    tools.call_tool = _original_call


def is_enabled() -> bool:
    return bool(_state["enabled"])