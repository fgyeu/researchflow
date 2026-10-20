"""ResearchFlow · 工具（Agent 的手）

Agent 之所以叫 Agent，是因为它能**动手做事**，而不只是聊天。
这里给它两个只读工具：

    web_search   搜索资料
    read_url     读一个网页，抽出正文

## 为什么用维基百科，而不是搜索引擎？

最初这里用的是 DuckDuckGo 的 HTML 页面。实测发现：它对脚本请求返回的是
**202 + 反爬页面**，解析不到任何结果 —— 也就是说工具会静默失败。

这不是 bug，是搜索引擎的常态：它们专门防程序抓取。
所以这里改用 **MediaWiki API**（维基百科的官方接口）：

    优点：明确允许程序访问、无需 API Key、返回结构化 JSON、不会被反爬拦
    缺点：覆盖面偏百科，不覆盖博客/论坛/GitHub 讨论

这对阶段 1 够用。想接更强的搜索源（Tavily、Serper、Bing API），
只要换掉 `web_search` 的实现，上层完全不用改 —— 因为工具是注册表驱动的。

## 三个设计要点（面试常问）

1. **只读**。两个工具都不改外部世界，所以天然可以重试、可以缓存。
   这是阶段 2 做缓存和重试的前提 —— 有副作用的工具才需要幂等设计。

2. **统一返回值结构**。成功 {"ok": True, "data": ...}，失败 {"ok": False, "error": "..."}。
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


def _http_get(url: str, timeout: int, accept_json: bool = False) -> str:
    headers = {"User-Agent": UA}
    if accept_json:
        headers["Accept"] = "application/json"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def _strip_html(html: str) -> str:
    """把 HTML 变成可读文本。

    故意用正则而不是 BeautifulSoup：
    少一个依赖，而且这个项目不需要完美的 HTML 解析。
    """
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------
# 工具实现
# --------------------------------------------------------------------------

def web_search(query: str, max_results: int = 5, lang: str = "auto") -> dict:
    """用维基百科的搜索 API 查资料。

    默认中英各查一次然后合并 —— 因为技术话题的英文条目往往比中文详细，
    而中文条目更贴近国内语境。两边都看一下，比单查一边好。

    lang 参数：
        "zh"   只查中文
        "en"   只查英文
        "auto" 两个都查（默认）
    """
    try:
        langs = ["zh", "en"] if lang == "auto" else [lang]
        results: list[dict] = []
        seen: set[str] = set()

        for lg in langs:
            if len(results) >= max_results:
                break
            url = (
                f"https://{lg}.wikipedia.org/w/api.php"
                f"?action=query&list=search&srsearch={urllib.parse.quote(query)}"
                f"&format=json&srlimit={max_results}"
            )
            raw = _http_get(url, settings.tool_timeout, accept_json=True)
            data = json.loads(raw)

            for item in data.get("query", {}).get("search", []):
                title = item.get("title", "")
                if not title or title in seen:
                    continue
                seen.add(title)
                page_url = f"https://{lg}.wikipedia.org/wiki/{urllib.parse.quote(title)}"
                # snippet 里带 HTML 高亮标签，去掉再给模型
                snippet = _strip_html(item.get("snippet", ""))
                results.append(
                    {
                        "title": title,
                        "url": page_url,
                        "snippet": snippet[:300],
                        "lang": lg,
                        "source": "wikipedia",
                    }
                )
                if len(results) >= max_results:
                    break

        if not results:
            return {"ok": True, "data": {"results": [], "note": "没有搜到结果，可以换个关键词再试"}}

        return {"ok": True, "data": {"query": query, "results": results}}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}（搜索服务拒绝请求）"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def read_url(url: str, max_chars: int = 6000) -> dict:
    """读取一个网页并抽取正文。

    对维基百科页面走专门的接口（拿纯文本，比解析 HTML 干净得多）；
    对其它网址退回通用的 HTML 去标签。

    max_chars 是刻意限制：
    把整个网页塞进上下文会迅速烧掉 token，而且模型也读不完。
    "截断"这个决定本身就值得在面试里讲。
    """
    try:
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": f"不支持的 URL 协议: {url[:80]}"}

        # 维基百科：用 extracts 接口直接拿纯文本
        m = re.match(r"https://([a-z]+)\.wikipedia\.org/wiki/(.+)$", url)
        if m:
            lg, title = m.group(1), urllib.parse.unquote(m.group(2))
            api = (
                f"https://{lg}.wikipedia.org/w/api.php"
                f"?action=query&prop=extracts&explaintext=1&format=json"
                f"&titles={urllib.parse.quote(title)}"
            )
            data = json.loads(_http_get(api, settings.tool_timeout, accept_json=True))
            pages = (data.get("query", {}) or {}).get("pages", {}) or {}
            for page in pages.values():
                text = (page.get("extract") or "").strip()
                if text:
                    return {
                        "ok": True,
                        "data": {
                            "url": url,
                            "text": text[:max_chars],
                            "total_chars": len(text),
                            "truncated": len(text) > max_chars,
                            "source": "wikipedia-extract",
                        },
                    }
            return {"ok": False, "error": "维基百科没有这个条目的正文"}

        # 其它网址：通用 HTTP + 去标签
        html = _http_get(url, settings.tool_timeout)
        text = _strip_html(html)
        return {
            "ok": True,
            "data": {
                "url": url,
                "text": text[:max_chars],
                "total_chars": len(text),
                "truncated": len(text) > max_chars,
                "source": "html",
            },
        }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"HTTP {exc.code}（页面打不开）"}
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
                "description": (
                    "搜索资料（维基百科中英文条目），返回标题、链接和摘要。"
                    "适合查概念、技术背景、历史沿革。"
                ),
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