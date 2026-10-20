"""ResearchFlow · 配置

阶段 1 需要调用真实模型，所以这里集中管理配置来源。

优先级（从高到低）：
    1. 环境变量
    2. 项目根目录的 .env 文件
    3. 代码里的默认值

为什么不直接读环境变量就完了？
    因为每次开新终端都要重新 set 一遍环境变量，太容易漏。
    用 .env 文件可以让"配置"跟着项目走，而且 .env 已经在 .gitignore 里，
    不会把密钥推到 GitHub 上。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def _load_env_file() -> None:
    """极简 .env 解析：KEY=VALUE，忽略空行和 # 注释。

    故意不引第三方库（python-dotenv）：
    这个项目依赖越少，在别人机器上跑起来的概率越高。
    """
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # 不覆盖已经存在的环境变量（环境变量优先级更高）
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()


class Settings:
    """所有配置项集中在这里，方便一眼看全。"""

    # ---- 模型 API ----
    # 兼容 OpenAI 协议的服务都能用：DeepSeek、Moonshot、OpenAI、本地 vLLM 等
    api_key: str = os.environ.get("LLM_API_KEY", "")
    base_url: str = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
    model: str = os.environ.get("LLM_MODEL", "deepseek-chat")

    # ---- 运行约束（面试会问：为什么要限制？）----
    max_steps: int = int(os.environ.get("AGENT_MAX_STEPS", "8"))
    # 单次研究允许消耗的最大 token；超了就提前收尾，避免烧钱
    max_tokens_budget: int = int(os.environ.get("AGENT_MAX_TOKENS", "20000"))
    # 单次模型调用超时（秒）
    llm_timeout: int = int(os.environ.get("LLM_TIMEOUT", "60"))
    # 工具调用超时（秒）
    tool_timeout: int = int(os.environ.get("TOOL_TIMEOUT", "20"))

    # ---- 是否用假模型（离线开发/测试用）----
    # 设成 1 时，Worker 不会联网，而是用预设脚本模拟模型。
    # 这样跑测试、跑故障注入实验时不用花一分钱，也不受网络影响。
    mock_llm: bool = os.environ.get("MOCK_LLM", "0") == "1"

    # ---- 阶段 2：可靠性相关 ----
    # 是否启用工具结果缓存（做"缓存前后对比"实验时用来开关）
    cache_enabled: bool = os.environ.get("CACHE_ENABLED", "1") == "1"

    # 工具调用失败后的最大重试次数（不含首次）
    tool_max_retries: int = int(os.environ.get("TOOL_MAX_RETRIES", "2"))
    # 重试退避基数（秒）：第 n 次重试等 base * 2^(n-1)
    retry_backoff: float = float(os.environ.get("RETRY_BACKOFF", "0.5"))

    # 租约时长（秒）。Worker 必须在这个时间内续约，否则会被认为已死。
    # 设得太短：正常的慢任务会被误判；设得太长：真死了要等很久才恢复。
    lease_seconds: float = float(os.environ.get("LEASE_SECONDS", "30"))
    # 续约间隔（秒），应远小于 lease_seconds
    heartbeat_interval: float = float(os.environ.get("HEARTBEAT_INTERVAL", "5"))

    # 单个任务最多被领取几次。超过就判死，避免"崩溃-恢复"无限循环。
    max_attempts: int = int(os.environ.get("MAX_ATTEMPTS", "3"))

    # 是否在服务启动时自动恢复孤儿任务
    recover_on_startup: bool = os.environ.get("RECOVER_ON_STARTUP", "1") == "1"

    @property
    def llm_configured(self) -> bool:
        return bool(self.api_key) and not self.mock_llm


settings = Settings()
