"""ResearchFlow · 工具（Agent 的手）

Agent 之所以叫 Agent，是因为它能**动手做事**，而不只是聊天。
这里给它两个只读工具：

    web_search   搜索资料（多源并发，谁先成功用谁）
    read_url     读一个网页，抽出正文

## 为什么 web_search 长这样（这段踩坑史值得看）

这个工具被我改了四次，每一次都是"看起来能用"被打回：

1. **DuckDuckGo HTML** —— 返回 202 + 反爬页面，解析不到结果，**静默失败**。
2. **维基百科 MediaWiki API** —— 本地通，但国内网络（含魔搭）**TLS 握手超时**。
3. **Bing 网页 / Bing RSS** —— 能返回结果，但实测**只用查询的第一个词**：
   查 "large language model agent" 返回的全是 "large" 这个词的词典解释。
   这比失败更危险 —— 它看起来成功了。
4. **现在的方案：对程序友好的结构化 API**
   - GitHub Search API —— 技术项目、开源库（带 star 数，天然有权威性排序）
   - HN Algolia API —— 技术圈讨论、博客文章
   - StackExchange API —— 具体技术问题

三条都是**官方 API**：不需要 Key、返回 JSON、不会被反爬拦。
代价是覆盖面偏"技术"，但这对本项目（技术调研）恰好是最合适的。

**核心教训：能用结构化 API 就别去爬网页。**
搜索引擎的 HTML 是给人看的，随时会变、专门防程序；而 API 是给程序用的，稳定。

## 三个设计要点（面试常问）

1. **只读**。两个工具都不改外部世界 → 可以安全重试、可以缓存。
   这是阶段 2 做缓存和重试的前提 —— 有副作用的工具才需要幂等设计。

2. **统一返回值结构**。成功 {"ok": True, "data": ...}，失败 {"ok": False, "error": "..."}。
   让 Agent 看见错误比让程序抛异常更有用 —— 模型能据此换一种策略。

3. **多源容错**。任何单一数据源都可能挂，所以搜多个源、合并结果、标注来源。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .config import settings

UA = "ResearchFlow/0.2 (+https://github.com/fgyeu/researchflow)"


def _http_get(url: str, timeout: int, accept: str = "application/json") -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": accept}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    """把 HTML 变成可读文本。

    故意用正则而不是 BeautifulSoup：少一个依赖，项目不需要完美解析。
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
# 搜索源
# --------------------------------------------------------------------------

def _search_github(query: str, limit: int) -> list[dict]:
    """GitHub 仓库搜索。

    对技术调研最有价值的一个源：
    带 star 数 → 天然按社区认可度排序；带描述 → 一句话说清它是干嘛的。
    """
    url = (
        "https://api.github.com/search/repositories?q="
        + urllib.parse.quote(query)
        + f"&sort=stars&order=desc&per_page={limit}"
    )
    data = json.loads(_http_get(url, settings.tool_timeout))
    out = []
    for it in data.get("items", [])[:limit]:
        out.append(
            {
                "title": f"{it['full_name']}（★{it['stargazers_count']}）",
                "url": it["html_url"],
                "snippet": (it.get("description") or "")[:300],
                "source": "github",
            }
        )
    return out


def _search_hn(query: str, limit: int) -> list[dict]:
    """Hacker News（Algolia 索引）。

    技术圈的一手讨论和博客——很多工程经验的原文就在这里，
    比百科条目更贴近"实际怎么做"。
    """
    url = (
        "https://hn.algolia.com/api/v1/search?query="
        + urllib.parse.quote(query)
        + f"&hitsPerPage={limit}"
    )
    data = json.loads(_http_get(url, settings.tool_timeout))
    out = []
    for it in data.get("hits", [])[:limit]:
        title = it.get("title") or it.get("story_title") or ""
        url_ = it.get("url") or f"https://news.ycombinator.com/item?id={it.get('objectID')}"
        if not title:
            continue
        out.append(
            {
                "title": title[:200],
                "url": url_,
                "snippet": f"HN 讨论，{it.get('points') or 0} 分，{it.get('num_comments') or 0} 条评论",
                "source": "hackernews",
            }
        )
    return out


def _search_stackoverflow(query: str, limit: int) -> list[dict]:
    """StackOverflow 问答。

    适合查"某个具体报错怎么办"这类问题。
    """
    url = (
        "https://api.stackexchange.com/2.3/search/advanced"
        "?order=desc&sort=relevance&site=stackoverflow&pagesize="
        + str(limit)
        + "&q="
        + urllib.parse.quote(query)
    )
    data = json.loads(_http_get(url, settings.tool_timeout))
    out = []
    for it in data.get("items", [])[:limit]:
        out.append(
            {
                "title": _strip_html(it.get("title", ""))[:200],
                "url": it.get("link", ""),
                "snippet": f"StackOverflow，{it.get('score', 0)} 票，"
                f"{'已回答' if it.get('is_answered') else '未解决'}",
                "source": "stackoverflow",
            }
        )
    return out


# --------------------------------------------------------------------------
# 对外工具
# --------------------------------------------------------------------------

def web_search(query: str, max_results: int = 6) -> dict:
    """搜索资料。

    依次尝试多个源，每个源取一部分，最后合并去重。
    为什么不全都要？因为不同源覆盖的场景不同：
        GitHub    → 有哪些现成项目、多少人用
        HN        → 工程实践、踩坑经验
        SO        → 具体技术问题
    """
    sources = [
        ("github", _search_github),
        ("hackernews", _search_hn),
        ("stackoverflow", _search_stackoverflow),
    ]

    per_source = max(2, max_results // len(sources) + 1)
    collected: list[dict] = []
    used: list[str] = []
    errors: list[str] = []
    seen_urls: set[str] = set()

    for name, fn in sources:
        try:
            items = fn(query, per_source)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:100]}")
            continue
        if not items:
            continue
        used.append(name)
        for it in items:
            if it["url"] in seen_urls:
                continue
            seen_urls.add(it["url"])
            collected.append(it)
            if len(collected) >= max_results:
                break
        if len(collected) >= max_results:
            break

    if not collected:
        # 全部源都失败：明确报错，**不要**假装成功返回空列表。
        # 静默失败比失败本身更糟 —— 上层会以为"查过了，就是没有"。
        return {
            "ok": False,
            "error": "所有搜索源都失败了：" + (" | ".join(errors) if errors else "无结果"),
        }

    result = {
        "ok": True,
        "data": {
            "query": query,
            "results": collected,
            "sources_used": used,
        },
    }
    if errors:
        # 部分源失败也要说清楚，方便排查
        result["data"]["partial_errors"] = errors
    return result


def read_url(url: str, max_chars: int = 6000) -> dict:
    """读取一个网页并抽取正文。

    max_chars 是刻意限制：
    把整个网页塞进上下文会迅速烧掉 token，而且模型也读不完。
    "截断"这个决定本身就值得在面试里讲。
    """
    try:
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": f"不支持的 URL 协议: {url[:80]}"}

        # GitHub 仓库：优先从 raw.githubusercontent 拿 README。
        # 为什么不走 api.github.com/repos/.../readme？
        # 因为未认证的 GitHub API 限流是 **每小时 60 次**，
        # 跑几轮研究就会耗尽（实测直接 403 rate limit exceeded）。
        # raw.githubusercontent 没有这个限制，而且返回的就是纯 Markdown。
        m = re.match(r"https://github\.com/([^/]+)/([^/#?]+)/?$", url)
        if m:
            owner, repo = m.group(1), m.group(2)
            for branch in ("main", "master"):
                for fname in ("README.md", "readme.md", "README.rst"):
                    raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{fname}"
                    try:
                        raw = _http_get(raw_url, settings.tool_timeout, accept="text/plain,*/*")
                    except Exception:  # noqa: BLE001
                        continue
                    if raw.strip() and not raw.strip().startswith("404"):
                        return {
                            "ok": True,
                            "data": {
                                "url": url,
                                "text": raw[:max_chars],
                                "total_chars": len(raw),
                                "truncated": len(raw) > max_chars,
                                "source": "github-readme",
                            },
                        }
            # README 拿不到就退回抓网页

        html = _http_get(url, settings.tool_timeout, accept="text/html,*/*")
        text = _strip_html(html)
        if not text:
            return {"ok": False, "error": "页面能打开但没有正文（可能是纯 JS 渲染）"}

        return {
            "ok": True,
            "data": {
                "url": url,
                "text": text[:max_chars],
                "total_chars": len(text),
                "truncated": len(text) > max_chars,
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
                    "搜索技术资料。会同时查 GitHub 仓库、Hacker News 讨论、"
                    "StackOverflow 问答，返回带来源标注的结果列表。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"},
                        "max_results": {
                            "type": "integer",
                            "description": "最多返回几条，默认 6",
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
                "description": "打开一个网页或 GitHub 仓库，返回抽取出的正文（可能被截断）。",
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