"""端到端冒烟测试（阶段 0）

验证四件事：
    1. 任务能提交，并且**立刻**拿到 run_id（不是等研究跑完）
    2. 提交后马上查询，状态还是 pending/running —— 证明是后台执行
    3. 状态会自己变成 succeeded
    4. 每一步都留下了 trace

用法：
    先开一个终端启动服务：
        .venv\\Scripts\\python.exe -m uvicorn app.main:app --port 8000
    再开另一个终端运行本脚本：
        .venv\\Scripts\\python.exe scripts\\smoke_test.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

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
    print("[4] 执行轨迹：")
    for s in run["steps"]:
        print(f"      #{s['step_no']}  {s['kind']:<5} {s['name']:<13} {s['status']:<10} {s['duration_ms']:>5}ms")

    report = run.get("report") or ""
    print(f"[5] 报告长度    {len(report)} 字符")

    ok = run["status"] == "succeeded" and len(run["steps"]) == 4 and bool(report)
    print("\n" + ("✅ 阶段 0 全链路通过" if ok else "❌ 未通过，看上面的输出定位问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())