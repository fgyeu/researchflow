#!/usr/bin/env python3
"""网络体检 —— 在魔搭 Notebook 上跑一下，就知道该怎么配。

## 为什么要有这个

国内网络访问 GitHub 是"时通时断"，同一台机器今天能 clone、明天就超时。
凭感觉猜没用，必须实测。

这个脚本检查三组关键链路，并直接给出建议：

    代码同步  github.com / codeload / 镜像
    依赖安装  pypi 清华源
    工具数据  api.github.com / hn.algolia.com / api.stackexchange.com
              （这三个是 Agent 的 web_search 真正要用的）

## 用法

    python scripts/netcheck.py

    # 手工指定代理（如果你自己搭了隧道）
    python scripts/netcheck.py --proxy http://127.0.0.1:7897

    # 只看某一组
    python scripts/netcheck.py --only code
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

# (分组, 名称, host, port, 用来测 HTTP 的 URL 或 None)
CHECKS = [
    # ---- 代码同步 ----
    ("code", "github.com (git 直连)", "github.com", 443, None),
    ("code", "codeload.github.com", "codeload.github.com", 443, None),
    ("code", "ghproxy.net 镜像", "ghproxy.net", 443, None),
    ("code", "gh-proxy.com 镜像", "gh-proxy.com", 443, None),
    # ---- 依赖安装 ----
    ("deps", "pypi 清华源", "pypi.tuna.tsinghua.edu.cn", 443, None),
    # ---- Agent 工具真正要用的 ----
    ("tools", "api.github.com", "api.github.com", 443, "https://api.github.com/rate_limit"),
    ("tools", "hn.algolia.com", "hn.algolia.com", 443, "https://hn.algolia.com/api/v1/search?query=test&hitsPerPage=1"),
    ("tools", "api.stackexchange.com", "api.stackexchange.com", 443, "https://api.stackexchange.com/2.3/info?site=stackoverflow"),
    # ---- 模型 API ----
    ("llm", "api.deepseek.com", "api.deepseek.com", 443, None),
]

SLOW = 3.0  # 超过这个秒数算"慢"


def build_opener(proxy: str | None) -> urllib.request.OpenerDirector:
    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def probe(host: str, port: int, url: str | None, opener, timeout: float) -> dict:
    """先测 TLS 握手（快），有 URL 再测真实 HTTP 请求。"""
    result = {"ok": False, "tls_ms": None, "http": None, "err": None}
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        ctx = ssl.create_default_context()
        ss = ctx.wrap_socket(s, server_hostname=host)
        result["tls_ms"] = int((time.time() - t0) * 1000)
        ss.close()
        result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["err"] = f"{type(exc).__name__}: {str(exc)[:60]}"
        return result

    if url:
        t1 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ResearchFlow-netcheck"})
            with opener.open(req, timeout=timeout) as r:
                r.read(1024)
                result["http"] = {"status": r.status, "ms": int((time.time() - t1) * 1000)}
        except urllib.error.HTTPError as exc:
            # 403/429 也算"通了" —— 说明能连上对方
            result["http"] = {"status": exc.code, "ms": int((time.time() - t1) * 1000)}
        except Exception as exc:  # noqa: BLE001
            result["http"] = {"status": None, "ms": None, "err": str(exc)[:60]}
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="ResearchFlow 网络体检")
    ap.add_argument("--proxy", default=None, help="可选：http://host:port")
    ap.add_argument("--timeout", type=float, default=10.0, help="单个检查超时秒数")
    ap.add_argument("--only", default=None, choices=["code", "deps", "tools", "llm"])
    args = ap.parse_args()

    opener = build_opener(args.proxy)
    if args.proxy:
        print(f"使用代理: {args.proxy}\n")

    results: dict[str, list] = {}
    for group, name, host, port, url in CHECKS:
        if args.only and group != args.only:
            continue
        r = probe(host, port, url, opener, args.timeout)
        results.setdefault(group, []).append((name, r))

        if r["ok"]:
            tls = f"{r['tls_ms']}ms"
            flag = "慢" if (r["tls_ms"] or 0) > SLOW * 1000 else "OK"
            line = f"  [{flag}] {name:28} TLS {tls:>7}"
            if r["http"]:
                h = r["http"]
                if h.get("status"):
                    line += f"  HTTP {h['status']} ({h['ms']}ms)"
                elif h.get("err"):
                    line += f"  HTTP 失败: {h['err']}"
            print(line)
        else:
            print(f"  [失败] {name:28} {r['err']}")

    print()
    print("=" * 62)

    def ok(group: str) -> list[str]:
        return [n for n, r in results.get(group, []) if r["ok"]]

    # ---- 结论 ----
    code_ok = ok("code")
    tools_ok = ok("tools")

    print("结论：")
    if results.get("code"):
        if "github.com (git 直连)" in code_ok:
            print("  代码同步：git 直连可用 → 直接用 git pull 就行")
        elif any("镜像" in n for n in code_ok):
            names = [n for n in code_ok if "镜像" in n]
            print(f"  代码同步：git 直连不通，但 {', '.join(names)} 可用")
            print("           → 用  python scripts/sync.py   或者  bash scripts/pull.sh")
        else:
            print("  代码同步：全部不可用 → 重启实例试试，或等网络恢复")

    if results.get("tools"):
        bad = [n for n in ("api.github.com", "hn.algolia.com", "api.stackexchange.com") if n not in tools_ok]
        if not bad:
            print("  工具数据：三个数据源都能连 → web_search 可以正常工作")
        else:
            print(f"  工具数据：{', '.join(bad)} 连不上")
            print("           → 真实模式下这些工具会失败，但系统会降级交付")
            print("           → 跑测试/演示时用 MOCK_LLM=1 不受影响")

    if results.get("deps") and not ok("deps"):
        print("  依赖安装：pypi 镜像不通 → 检查 /etc/resolv.conf 或换阿里源")

    print()
    print("提示：慢（>3s）的链路也容易在 git 操作中直接超时，")
    print("      所以即使显示 OK，git pull 卡住也属正常，优先用 sync.py。")
    return 0


if __name__ == "__main__":
    sys.exit(main())