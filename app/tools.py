"""ResearchFlow · 工具（Agent 的手）

Agent 之所以叫 Agent，是因为它能**动手做事**，而不只是聊天。
这里给它三个只读工具：

    web_search   搜索资料
    read_url     读一个网页，抽出正文
    (计算/收尾这类不需要工具)

设计上的三个要点（面试常问）：

1. **只读**。所有工具都不改外部世界，所以天然可以重试、可以缓存。
   这是阶段 2 做缓存和重试的前提 —— 有副作用的工具才需要幂等设计。

2. **统一返回值结构**。成功返回 {"ok": True, "data": ...}，
   失败返回 {"ok": False, "error": "..."}。
   让 Agent 看见错误比让程序抛异常更有用 —— 模型能据此换一种策略。

3. **每个工具都有超时**。网络请求卡住是常态，不设超时会把整个 Worker 拖死。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .config import settings

UA = "ResearchFlow/0.2 (+https://github.com/fgyeu/researchflow)"


def _http_get(url: str, timeout: int) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def _strip_html(html: str) -> str:
    """把 HTML 变成可读文本。

    故意用正则而不是 BeautifulSoup：
    少一个依赖，而且这个项目不需要完美的 HTML 解析。
    """
    # 去掉 script / style 整块
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    # 去掉所有标签
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    # 常见实体
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    # 压缩空白
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# 工具实现
# --------------------------------------------------------------------------

def web_search(query: str, max_results: int = 5) -> dict:
    """用 DuckDuckGo 的轻量接口搜索。

    为什么是 DuckDuckGo？
    不需要 API Key，注册成本为零 —— 对"想先把链路跑通"的人最友好。
    缺点是结果质量一般、偶尔被限流。阶段 1 够用，阶段 3 可以换更大的搜索源。
    """
    try:
        q = urllib.parse.quote(query)
        url = f"https://duckduckgo.com/html/?q={q}"
        html = _http_get(url, settings.tool_timeout)
        text = _strip_html(html)

        # 从结果页里抓标题 + 链接
        results = []
        for m in re.finditer(r'result__a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
            href, title = m.group(1), _strip_html(m.group(2))
            # DDG 的跳转链接里带着真实地址
            if "uddg=" in href:
                href = urllib.parse.unquote(href.split("uddg=")[1].split("&")[0])
            if href.startswith("http"):
                results.append({"title": title.strip()[:200], "url": href})
            if len(results) >= max_results:
                break

        if not results:
            # 抓不到结构就退化成"给一段文本摘要"，总比空手而归好
            return {"ok": True, "data": {"results": [], "note": "未解析到结果，页面摘要片段：" + text[:800]}}

        return {"ok": True, "data": {"results": results}}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def read_url(url: str, max_chars: int = 6000) -> dict:
    """读取一个网页并抽取正文。

    只取前 max_chars 个字符 —— 这是刻意的：
    把整个网页塞进上下文会迅速烧掉 token，而且模型也读不完。
    "截断"这个决定本身就值得在面试里讲。
    """
    try:
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": f"不支持的 URL 协议: {url[:80]}"}

        html = _http_get(url, settings.tool_timeout)
        text = _strip_html(html)

        truncated = len(text) > max_chars
        return {
            "ok": True,
            "data": {
                "url": url,
                "text": text[:max_chars],
                "total_chars": len(text),
                "truncated": truncated,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------
# 工具注册表：Agent 靠它把"模型说要调的工具名"映射到真实函数
# --------------------------------------------------------------------------

TOOLS = {
    "web_search": {
        "fn": web_search,
        "schema": {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "搜索互联网上的资料，返回若干条结果的标题和链接。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                        "max_results": {
                            "type": "integer",
                            "description": "最多返回几条，默认 5",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
    },
    "read_url": {
        "fn": read_url,
        "schema": {
            "type": "function",
            "function": {
                "name": "read_url",
                "description": "打开一个网页，返回抽取出的正文（可能被截断）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "要读取的完整网址"},
                    },
                    "required": ["url"],
                },
            },
        },
    },
}


def call_tool(name: str, arguments: dict) -> dict:
    """统一入口：查表、调用、兜住异常。

    注意这里对未知工具的处理：返回错误而不是抛异常。
    Agent 看到 "unknown tool" 会自己换个工具试，比整个任务崩掉好。
    """
    entry = TOOLS.get(name)
    if entry is None:
        return {"ok": False, "error": f"不存在这个工具: {name}"}
    try:
        return entry["fn"](**arguments)
    except TypeError as exc:
        return {"ok": False, "error": f"参数不匹配: {exc}"}


def tool_schemas() -> list[dict]:
    return [t["schema"] for t in TOOLS.values()]


def reset_cache() -> None:
    """阶段 2 会在这里加工具结果缓存；阶段 1 先留个空实现。"""
    return None
