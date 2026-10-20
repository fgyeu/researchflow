"""ResearchFlow · 模型客户端

只做一件事：把"给我一段对话，还我一个决定"这件事封装好。
上层完全不关心底下是 DeepSeek、OpenAI 还是本地模型。

支持两种模式：
    MOCK_LLM=1  用预设脚本，不联网、不花钱 —— 测试和故障注入实验全靠它
    真实模式    用 OpenAI 兼容的 /chat/completions 接口

为什么要有 mock？
    1. 跑测试时不该花钱，也不该受网络波动影响
    2. 阶段 2 要做故障注入（让模型超时、返回垃圾），mock 是唯一可控手段
    3. 面试时可以说："我的测试不依赖外部服务，所以能稳定复现" —— 这是加分项
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import settings


class LLMError(Exception):
    """模型调用失败。单独定义，方便上层区分"模型挂了"和"我的代码挂了"。"""


def _post_json(url: str, payload: dict, headers: dict, timeout: int) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise LLMError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"连接失败: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMError(f"超时 {timeout}s") from exc


# --------------------------------------------------------------------------
# Mock：按脚本演戏
# --------------------------------------------------------------------------

def _mock_chat(messages: list[dict], step_no: int) -> dict:
    """不联网的假模型。

    它按"第几步"决定说什么，模拟一个真实 Agent 的行为路径：
        1. 先搜一次
        2. 再读一个网页
        3. 最后给出报告

    这样上层代码走的是**和真实模型完全一样的路径**，mock 只替换了"决策来源"。
    """
    if step_no == 1:
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "mock_call_1",
                    "name": "web_search",
                    "arguments": {"query": "large language model agent"},
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        }
    if step_no == 2:
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "mock_call_2",
                    "name": "read_url",
                    "arguments": {"url": "https://en.wikipedia.org/wiki/Intelligent_agent"},
                }
            ],
            "usage": {"prompt_tokens": 400, "completion_tokens": 25, "total_tokens": 425},
        }
    # 第 3 步及以后：收尾出报告
    question = messages[1]["content"] if len(messages) > 1 else "（未知问题）"
    return {
        "content": (
            f"# 研究报告（Mock 模式）\n\n"
            f"**研究问题**：{question}\n\n"
            f"## 主要发现\n"
            f"1. 这是 Mock 模型生成的占位报告，用于在不联网的情况下验证链路。\n"
            f"2. 真实模式（配置 LLM_API_KEY 后）会由模型基于工具结果写出真正的内容。\n\n"
            f"> 本报告共执行了 {step_no} 步。\n"
        ),
        "tool_calls": [],
        "usage": {"prompt_tokens": 800, "completion_tokens": 120, "total_tokens": 920},
    }


# --------------------------------------------------------------------------
# 真实模型
# --------------------------------------------------------------------------

def _real_chat(messages: list[dict], tools: list[dict] | None) -> dict:
    url = settings.base_url.rstrip("/") + "/chat/completions"
    payload: dict = {
        "model": settings.model,
        "messages": messages,
        "temperature": 0.3,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    data = _post_json(
        url,
        payload,
        {"Authorization": f"Bearer {settings.api_key}"},
        settings.llm_timeout,
    )

    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"响应格式异常: {str(data)[:300]}") from exc

    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            # 模型偶尔会吐出坏 JSON。不当成致命错误，交给上层当"工具调用失败"处理。
            args = {"__parse_error__": raw_args[:200]}
        tool_calls.append(
            {"id": tc.get("id", ""), "name": fn.get("name", ""), "arguments": args}
        )

    return {
        "content": msg.get("content") or "",
        "tool_calls": tool_calls,
        "usage": data.get("usage") or {},
    }


# --------------------------------------------------------------------------
# 对外统一接口
# --------------------------------------------------------------------------

def chat(messages: list[dict], step_no: int, tools: list[dict] | None = None) -> dict:
    """让模型做一次决策。

    返回：
        content     文本回答（可能为空）
        tool_calls  要调用的工具列表（可能为空）
        usage       token 用量
    """
    if settings.mock_llm:
        return _mock_chat(messages, step_no)
    if not settings.api_key:
        raise LLMError(
            "没有配置 LLM_API_KEY。请二选一：\n"
            "  1. 在项目根目录建 .env，写 LLM_API_KEY=sk-xxx\n"
            "  2. 用 MOCK_LLM=1 跑离线模式"
        )
    return _real_chat(messages, tools)