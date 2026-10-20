#!/usr/bin/env bash
# ============================================================
# ResearchFlow · 拉取代码（带镜像自动降级）
#
# 魔搭 / 国内网络直连 GitHub 经常超时。这个脚本会依次尝试：
#     1. 直连 github.com
#     2. ghproxy.net 镜像
#     3. ghfast.top 镜像
#     4. gh-proxy.com 镜像
#     5. gitclone.com 镜像（换 remote 方式）
#
# 用法：
#     bash scripts/pull.sh
#
# 也支持指定分支：
#     BRANCH=dev bash scripts/pull.sh
# ============================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

BRANCH="${BRANCH:-main}"
# 这些是本项目的默认上游。如果你 fork 过，用 UPSTREAM 环境变量覆盖。
UPSTREAM="${UPSTREAM:-https://github.com/fgyeu/researchflow.git}"
REPO_PATH="${UPSTREAM#https://github.com/}"   # fgyeu/researchflow.git

# 镜像前缀。github.com/<path> 会被替换成 <镜像>/<path>
MIRRORS=(
  ""                                  # 直连（第一个试）
  "https://ghproxy.net/https://github.com/"
  "https://ghfast.top/https://github.com/"
  "https://gh-proxy.com/https://github.com/"
)

# 每次尝试的超时（秒）。太短会误判，太长每次都要等。
TIMEOUT="${TIMEOUT:-25}"

echo "==> 仓库: $UPSTREAM  分支: $BRANCH"

# ---------- 先确认当前是不是 git 仓库 ----------
if [ ! -d .git ]; then
  echo "!! 当前目录不是 git 仓库。你大概是还没 clone 过？"
  echo "   首次用：git clone $UPSTREAM"
  exit 1
fi

# ---------- 有没有本地改动？有就先藏起来 ----------
if ! git diff-index --quiet HEAD -- 2>/dev/null; then
  echo "==> 检测到本地改动，先 git stash 存起来"
  git stash push -u -m "pull.sh auto-stash $(date +%s)" >/dev/null
  STASHED=1
else
  STASHED=0
fi

pull_via() {
  local base="$1"
  local label="$2"
  local url
  if [ -z "$base" ]; then
    url="$UPSTREAM"
  else
    url="${base}${REPO_PATH}"
  fi

  echo -n "==> 尝试 ${label} ... "
  # 用 timeout 包住 git，避免像你上次那样卡 130 秒
  if timeout "$TIMEOUT" git fetch --quiet "$url" "$BRANCH" 2>/dev/null; then
    echo "成功"
    # 用 fetch 到的提交直接更新，不依赖 origin 这个 remote
    if git merge --ff-only FETCH_HEAD >/dev/null 2>&1; then
      echo "==> 已快进到 $(git rev-parse --short HEAD)"
      return 0
    else
      echo "==> fetch 成功但无法快进（可能有分叉），改用 rebase"
      if git rebase FETCH_HEAD >/dev/null 2>&1; then
        echo "==> rebase 完成，当前 $(git rev-parse --short HEAD)"
        return 0
      else
        echo "!! rebase 冲突，需要手工处理：git status 看看"
        git rebase --abort 2>/dev/null || true
        return 1
      fi
    fi
  else
    echo "超时或失败"
    return 1
  fi
}

OK=0
# 直连放最后试？不，先试直连：能用就用，最快。
for i in "${!MIRRORS[@]}"; do
  base="${MIRRORS[$i]}"
  if [ -z "$base" ]; then
    label="直连 github.com"
  else
    label="镜像 $(echo "$base" | sed -E 's#https://([^/]+)/.*#\1#')"
  fi
  if pull_via "$base" "$label"; then
    OK=1
    break
  fi
done

# ---------- 所有镜像都不行 ----------
if [ "$OK" -ne 1 ]; then
  echo ""
  echo "============================================================"
  echo "所有通道都失败了。可以试试："
  echo "  1. 加大超时：  TIMEOUT=60 bash scripts/pull.sh"
  echo "  2. 换镜像：    MIRRORS 里的地址可能过期了，用浏览器打开"
  echo "                 https://ghproxy.net 看看还活着吗"
  echo "  3. 断开重连：  魔搭的网络有时是路由问题，重启实例常能好"
  echo "  4. 不走 git：  用 scripts/sync.sh 从单文件 bundle 覆盖代码"
  echo "============================================================"
fi

# ---------- 恢复本地改动 ----------
if [ "$STASHED" -eq 1 ]; then
  echo "==> 恢复之前的本地改动"
  if ! git stash pop >/dev/null 2>&1; then
    echo "!! 恢复失败（可能有冲突）。你的改动还在 stash 里："
    echo "   git stash list"
    echo "   git stash pop"
  fi
fi

exit $((1 - OK))