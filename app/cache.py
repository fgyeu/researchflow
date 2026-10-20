"""ResearchFlow · 工具缓存（阶段 2）

## 为什么只读工具才能缓存

缓存的前提是：**同样的输入永远得到同样的输出**，而且重复执行没有副作用。

    web_search     只读 → 可以缓存
    read_url       只读 → 可以缓存
    下单 / 转账     有副作用 → 绝对不能缓存

这个判断不是可有可无的细节，它决定了你的系统会不会在缓存上做错事。

## 缓存键怎么设计

键必须包含**所有影响结果的参数**。少一个就会串味。

比如 web_search 的键要包含 query 和 max_results：
    搜 "agent" 取 3 条  ≠  搜 "agent" 取 10 条

这里用「工具名 + 排序后的参数 JSON」做键，
参数顺序不影响结果，所以排序后再算 hash。

## TTL 怎么定

    read_url   → 1 天（网页内容变化慢）
    web_search → 1 小时（搜索结果会变）

TTL 太短没意义，太长会返回过期信息。这是个权衡，不是标准答案。
"""

from __future__ import annotations

import hashlib
import json

from . import db
from .config import settings

# 各工具结果的缓存时长（秒）
TTL = {
    "web_search": 3600,      # 1 小时
    "read_url": 86400,       # 1 天
}


def _cache_key(tool_name: str, arguments: dict) -> str:
    """规范化参数 → 稳定 hash。

    sort_keys=True 很关键：
    {"a":1,"b":2} 和 {"b":2,"a":1} 是同一件事，必须算出同一个键。
    """
    normalized = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(f"{tool_name}:{normalized}".encode("utf-8")).hexdigest()
    return f"{tool_name}:{digest[:32]}"


def get(tool_name: str, arguments: dict) -> dict | None:
    """查缓存。命中返回结果，未命中返回 None。"""
    if not settings.cache_enabled:
        return None
    if tool_name not in TTL:
        return None  # 不在白名单里的工具不缓存
    raw = db.cache_get(_cache_key(tool_name, arguments))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def put(tool_name: str, arguments: dict, result: dict) -> None:
    """写缓存。只缓存**成功**的结果。

    为什么不缓存失败？
    因为失败可能是瞬时的（网络抖动、限流），缓存起来会让故障延续 1 小时。
    失败应该由重试机制处理，而不是缓存。
    """
    if not settings.cache_enabled:
        return
    if tool_name not in TTL:
        return
    if not result.get("ok"):
        return
    db.cache_put(
        _cache_key(tool_name, arguments),
        tool_name,
        json.dumps(result, ensure_ascii=False),
        TTL[tool_name],
    )


def stats() -> dict:
    return db.cache_stats()


def clear() -> int:
    return db.cache_clear()