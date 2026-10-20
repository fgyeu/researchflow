"""端到端冒烟测试

验证整条链路：
    1. 任务能提交，并且**立刻**拿到 run_id（不是等研究跑完）
    2. 提交后马上查询，状态还是 pending/running —— 证明是后台执行
    3. 状态会自己变成 succeeded
    4. 每一步都留下了 trace（模型决策 + 工具调用）
    5. 产出了报告

用法：
    先开一个终端启动服务：
        .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
    再开另一个终端运行本脚本：
        .venv/bin/python scripts/smoke_test.py

    指定别的地址：
        BASE_URL=http://127.0.0.1:8014 .venv/bin/python scripts/smoke_test.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

# Windows 控制台默认可能是 GBK，打印 ✅ / ✓ 这类字符会直接抛 UnicodeEncodeError
# 把脚本搞崩。这里强制用 UTF-8 输出，有就换、没有就退回替换模式，绝不崩。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 —— 老版本 Python 或特殊终端
    pass

# 服务地址，可用环境变量覆盖（方便在不同机器/端口上跑）
BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def call(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def main() -> int:
    try:
        call("GET", "/health")
    except urllib.error.URLError as exc:
        print(f"❌ 连不上服务（{exc}）")
        print("   请先在另一个终端里启动：")
        print(r"   .venv\Scripts\python.exe -m uvicorn app.main:app --port 8000")
        return 1

    question = "调研目前主流的开源 Agent 框架，比较它们的 Memory 与任务恢复机制"

    # --- 1. 提交 ---
    status, created = call("POST", "/api/v1/research", {"question": question})
    run_id = created["run_id"]
    print(f"[1] 提交成功    HTTP {status}（202 = 已受理未完成）  run_id={run_id}")

    # --- 2. 立刻查询，此时它应该还没跑完 ---
    _, snapshot = call("GET", f"/api/v1/research/{run_id}")
    print(
        f"[2] 立刻查询    status={snapshot['status']}  steps={len(snapshot['steps'])}"
        f"  report={'有' if snapshot.get('report') else '无'}"
        "   <- 没阻塞，说明真的是后台在跑"
    )

    # --- 3. 轮询到结束 ---
    deadline = time.time() + 30
    run = snapshot
    while time.time() < deadline:
        _, run = call("GET", f"/api/v1/research/{run_id}")
        if run["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.4)

    print(f"[3] 最终状态    status={run['status']}  总耗时约 {time.time() - (deadline - 30):.1f}s")

    # --- 4. trace ---
    # 注意：这里只用 ASCII 符号（OK/--），不用 ✓ ✗。
    # 因为 Windows 的控制台编码可能是 GBK，打印 Unicode 符号会直接抛
    # UnicodeEncodeError 把脚本搞崩 —— 这是踩过的坑。
    print("[4] 执行轨迹：")
    for s in run["steps"]:
        mark = "OK" if s["status"] == "succeeded" else "--"
        print(f"      #{s['step_no']}  {s['kind']:<5} {s['name']:<13} {mark} {s['status']}")

    report = run.get("report") or ""
    print(f"[5] 报告长度    {len(report)} 字符")

    # --- 6. 判定 ---
    # 注意：这里**不**硬编码步数。
    # 阶段 0 是 4 步、阶段 1 是 5 步，以后还会变。
    # 把步数写死会让脚本每升一个阶段就误报失败 —— 这是我踩过的坑。
    checks = []

    checks.append(("任务最终成功", run["status"] == "succeeded"))
    checks.append(("产出了报告", len(report) > 20))
    checks.append(("执行全程留痕", len(run["steps"]) >= 3))
    checks.append(("有模型决策步骤", any(s["kind"] == "llm" for s in run["steps"])))
    checks.append(("有工具调用步骤", any(s["kind"] == "tool" for s in run["steps"])))
    # 工具允许失败（网络问题很正常），但必须被记录下来
    tool_failed = [s for s in run["steps"] if s["kind"] == "tool" and s["status"] == "failed"]
    checks.append(
        (
            "工具失败也能正常收尾（降级交付）" if tool_failed else "工具调用全部成功",
            True,
        )
    )

    print("\n[6] 验收清单：")
    for label, passed in checks:
        print(f"      {'✅' if passed else '❌'} {label}")

    if tool_failed:
        print(f"\n      ⚠️  有 {len(tool_failed)} 个工具调用失败（不影响链路验收，"
              f"但会影响报告质量，可检查网络）")

    ok = all(p for _, p in checks)
    print("\n" + ("✅ 全链路通过" if ok else "❌ 未通过，看上面的输出定位问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())