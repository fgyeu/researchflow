#!/usr/bin/env bash
# ============================================================
# ResearchFlow · 环境引导脚本（Linux / 魔搭 ModelScope Notebook）
#
# 用法（在 Notebook 的 Terminal 里）：
#     cd <项目目录>
#     bash scripts/bootstrap.sh
#
# 它做三件事：
#   1. 用国内镜像装包（默认清华，可换阿里云）
#   2. 建一个独立的 Python 环境 .venv，不污染预装环境
#   3. 装依赖 + 语法自检
#
# 为什么要独立环境？
#   魔搭镜像里预装了一堆包（torch 等）。直接往系统环境里装，很容易把
#   版本搅乱、甚至弄坏预装环境。独立环境搞坏了，删掉重建的代价极低。
#
# 换镜像：
#     MIRROR=https://mirrors.aliyun.com/pypi/simple bash scripts/bootstrap.sh
# ============================================================
set -euo pipefail

MIRROR="${MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> 项目目录 : $ROOT"
echo "==> pip 镜像 : $MIRROR"

# ---------- 1. 选一个基础解释器 ----------
if command -v python3 >/dev/null 2>&1; then
  BASE_PY=python3
elif command -v python >/dev/null 2>&1; then
  BASE_PY=python
else
  echo "❌ 找不到 python3 / python，请先在终端确认 Python 可用" >&2
  exit 1
fi
echo "==> 基础解释器: $("$BASE_PY" --version 2>&1)"

# ---------- 2. 建独立环境（已存在就复用） ----------
if [ ! -d .venv ]; then
  echo "==> 创建独立环境 .venv"
  "$BASE_PY" -m venv .venv
else
  echo "==> 复用已存在的 .venv"
fi
PY="$ROOT/.venv/bin/python"

# ---------- 3. 装依赖 ----------
echo "==> 升级 pip"
"$PY" -m pip install -q --upgrade pip -i "$MIRROR"

echo "==> 安装 requirements.txt"
"$PY" -m pip install -q -r requirements.txt -i "$MIRROR"

echo "==> 已安装的关键包："
"$PY" -m pip list 2>/dev/null | grep -Ei 'fastapi|uvicorn|pydantic|starlette|anyio' || true

# ---------- 4. 语法自检（不需要依赖也能跑，但要装完才算环境干净） ----------
echo "==> 语法自检"
"$PY" -m py_compile app/db.py app/worker.py app/main.py scripts/smoke_test.py
echo "✅ 语法通过"

cat <<'EOF'

------------------------------------------------------------
环境就绪。接下来开两个终端：

  终端 1（启动服务）：
      .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000

  终端 2（跑端到端冒烟测试）：
      .venv/bin/python scripts/smoke_test.py

说明：
  · 魔搭 Notebook 里要用 --host 0.0.0.0，才能通过预览/转发访问；
    本机自己开发用默认的 127.0.0.1 就够了。
  · 冒烟测试脚本默认连 http://127.0.0.1:8000。
    如果不在同一个终端里，用环境变量指定：
        BASE_URL=http://127.0.0.1:8000 .venv/bin/python scripts/smoke_test.py
------------------------------------------------------------
EOF