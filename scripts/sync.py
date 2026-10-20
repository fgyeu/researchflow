#!/usr/bin/env python3
"""从 GitHub 拉取代码快照 —— 不依赖 git，也不需要 curl/wget。

## 为什么需要这个

魔搭 Notebook 上 `git pull` 经常卡在 "Failed to connect to github.com port 443"
—— 直连 GitHub 在国内网络里时通时断，而且一卡就是两分钟。

这个脚本走 HTTP 下载 tarball，并支持多个镜像自动降级：

    codeload.github.com  →  ghproxy.net  →  gh-proxy.com  →  jsDelivr

它们中任意一个能通就行，而"某个镜像挂了"的概率远低于"GitHub 直连挂了"。

## 用法

    # 默认：拉 main 分支，覆盖到当前目录（保留 .git / .venv / .env / *.db）
    python scripts/sync.py

    # 指定分支
    python scripts/sync.py --branch dev

    # 只下载到临时目录看看，不动现有文件
    python scripts/sync.py --dry-run

    # 拉完顺便跑测试
    python scripts/sync.py --test

## 它不会碰的东西

    .git/  .venv/  venv/  .env  *.db  __pycache__/  .pytest_cache/

所以本地配置、虚拟环境、数据库都安全。
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.environ.get("RF_REPO", "fgyeu/researchflow")
BRANCH = os.environ.get("RF_BRANCH", "main")

# 按"通常最快"排序。全挂才会报错。
SOURCES = [
    ("ghproxy.net", "https://ghproxy.net/https://github.com/{repo}/archive/refs/heads/{branch}.tar.gz"),
    ("直连 codeload", "https://codeload.github.com/{repo}/tar.gz/refs/heads/{branch}"),
    ("gh-proxy.com", "https://gh-proxy.com/https://github.com/{repo}/archive/refs/heads/{branch}.tar.gz"),
]

# 这些目录/文件永远不覆盖
PROTECTED_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}
PROTECTED_FILES = {".env"}


def log(msg: str) -> None:
    print(msg, flush=True)


def download(url: str, timeout: int) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": "ResearchFlow-sync/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_tarball(branch: str, timeout: int) -> tuple[bytes, str]:
    """依次尝试各个源，返回 (tar.gz 字节, 用的源名)。"""
    errors = []
    for name, tpl in SOURCES:
        url = tpl.format(repo=REPO, branch=branch)
        t0 = time.time()
        log(f"==> 尝试 {name} ...")
        try:
            data = download(url, timeout)
            if len(data) < 500:
                raise ValueError(f"返回内容太小 ({len(data)}B)，可能不是 tarball")
            log(f"    成功  {len(data)} 字节  {time.time() - t0:.1f}s")
            return data, name
        except Exception as exc:  # noqa: BLE001
            log(f"    失败  {type(exc).__name__}: {str(exc)[:80]}")
            errors.append(f"{name}: {type(exc).__name__}")
    raise RuntimeError(
        "所有源都失败了：" + " | ".join(errors) + "\n"
        "可以试试：\n"
        "  1. 加大超时  --timeout 60\n"
        "  2. 重启实例再试（魔搭有时是路由问题）\n"
        "  3. 用 scripts/sync.sh（单文件 bundle 版本）"
    )


def extract(tarball: bytes, dest: str) -> int:
    """解压并覆盖到 dest。返回写入的文件数。

    GitHub 的 tarball 会把所有内容放在一个 `repo-branch/` 顶层目录里，
    所以要去掉那一层。
    """
    written = 0
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tf:
        members = tf.getmembers()
        if not members:
            raise RuntimeError("tarball 是空的")

        # 找到顶层目录名
        top = members[0].name.split("/")[0]

        for m in members:
            if not m.isfile():
                continue

            # 去掉顶层目录：repo-main/app/db.py → app/db.py
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue
            rel = parts[1]

            # 跳过受保护路径
            first = rel.split("/")[0]
            if first in PROTECTED_DIRS:
                continue
            if rel in PROTECTED_FILES or rel.endswith((".db", ".db-wal", ".db-shm")):
                continue

            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)

            f = tf.extractfile(m)
            if f is None:
                continue
            with open(target, "wb") as out:
                shutil.copyfileobj(f, out)
            written += 1

    return written


def run_tests(dest: str) -> int:
    """跑测试（用当前 Python，不折腾 venv 发现逻辑）。"""
    py = sys.executable
    # 优先用项目里的 venv
    for cand in (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe"):
        p = os.path.join(dest, cand)
        if os.path.exists(p):
            py = p
            break

    log(f"==> 用 {py} 跑测试")
    env = dict(os.environ, MOCK_LLM="1")
    return subprocess.call(
        [py, "-m", "pytest", "tests/", "-q"], cwd=dest, env=env
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="从 GitHub 拉取代码快照（不依赖 git）")
    ap.add_argument("--branch", default=BRANCH, help=f"分支名，默认 {BRANCH}")
    ap.add_argument("--dest", default=".", help="目标目录，默认当前目录")
    ap.add_argument("--timeout", type=int, default=30, help="每个源的单次超时秒数")
    ap.add_argument("--dry-run", action="store_true", help="只下载到临时目录，不动现有文件")
    ap.add_argument("--test", action="store_true", help="拉完顺便跑测试")
    args = ap.parse_args()

    dest = os.path.abspath(args.dest)
    log(f"==> 仓库 {REPO}  分支 {args.branch}")

    try:
        data, source = fetch_tarball(args.branch, args.timeout)
    except RuntimeError as exc:
        log(f"!! {exc}")
        return 1

    if args.dry_run:
        tmp = tempfile.mkdtemp(prefix="rf-sync-")
        n = extract(data, tmp)
        log(f"==> [dry-run] 已解压 {n} 个文件到 {tmp}")
        log("    看一眼没问题的话，去掉 --dry-run 再跑一次就会覆盖到项目目录")
        return 0

    if not os.path.isdir(dest):
        os.makedirs(dest, exist_ok=True)

    n = extract(data, dest)
    log(f"==> 已更新 {n} 个文件（来自 {source}）")

    if args.test:
        return run_tests(dest)

    log("")
    log("下一步：")
    log("    MOCK_LLM=1 .venv/bin/python -m pytest tests/ -q")
    return 0


if __name__ == "__main__":
    sys.exit(main())