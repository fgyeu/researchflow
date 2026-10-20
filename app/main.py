"""ResearchFlow · API 层

这一层只干三件事：
    1. 接请求、校验参数
    2. 把任务交给 Worker，立刻返回 run_id
    3. 把数据库里的状态读出来返回给调用方

它故意很薄 —— 业务逻辑不该长在 API 层里。
"""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from . import cache, db, worker
from .config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 服务启动时确保表结构存在（可重复执行）
    db.init_db()

    # 阶段 2：把上次崩溃时留下的孤儿任务捡回来
    if settings.recover_on_startup:
        resumed = await worker.recover_orphans()
        if resumed:
            import logging

            logging.getLogger("uvicorn").info("恢复了 %d 个中断的任务", resumed)

    yield


app = FastAPI(
    title="ResearchFlow",
    version="0.3.0",
    description="基于异步任务调度与持久化执行的 AI 研究助手",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# 请求 / 响应模型
# --------------------------------------------------------------------------

class ResearchRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="你想让它研究的问题",
        examples=["调研目前主流的开源 Agent 框架，比较它们的 Memory 与任务恢复机制"],
    )


def _fingerprint(question: str) -> str:
    """没有显式幂等键时，用问题内容本身当键。

    这叫"天然幂等"：同一个问题在短时间内重复提交，只算一次。
    对用户来说这几乎总是想要的行为 —— 他不会想为同一个问题付两次钱。
    """
    normalized = " ".join(question.lower().split())
    return "q:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------

@app.get("/health", summary="健康检查")
async def health() -> dict:
    return {
        "status": "ok",
        "runs_by_status": db.count_runs(),
        "worker": worker.stats(),
        "cache": cache.stats(),
    }


@app.post("/api/v1/research", status_code=202, summary="提交一个研究任务")
async def create_research(
    req: ResearchRequest,
    response: Response,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description=(
            "幂等键。同一个键重复提交只会创建一个任务。"
            "不传则用问题内容的 hash 代替。"
        ),
    ),
) -> dict:
    """提交任务，**立刻**返回 run_id，不等研究跑完。

    返回 202 Accepted 而不是 200 OK —— 这个状态码的含义就是
    "我收下了，但还没做完"，跟同步接口区分开。

    ## 幂等

    - 传了 `Idempotency-Key` 头 → 用你给的值
    - 没传 → 用问题内容的 hash（同一问题重复提交不重复建任务）

    重复提交时返回**同一个 run_id**，并用 `X-Idempotent-Replay: true`
    标出来，让调用方能区分"新建"和"复用"。
    """
    key = idempotency_key or _fingerprint(req.question)

    run_id, created = db.create_run(req.question, idempotency_key=key)

    if created:
        await worker.submit(run_id, req.question)
        response.headers["X-Idempotent-Replay"] = "false"
    else:
        response.headers["X-Idempotent-Replay"] = "true"

    run = db.get_run(run_id)
    return {
        "run_id": run_id,
        "status": run["status"] if run else "pending",
        "replayed": not created,
    }


@app.get("/api/v1/research", summary="列出最近的任务")
async def list_research(limit: int = Query(20, ge=1, le=200)) -> dict:
    return {"items": db.list_runs(limit)}


@app.get("/api/v1/research/{run_id}", summary="查询任务状态、执行轨迹与报告")
async def get_research(run_id: str) -> dict:
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"没有这个任务: {run_id}")
    run["steps"] = db.get_steps(run_id)

    # 顺手算好缓存命中率，省得前端再算一遍
    total = run.get("tool_calls") or 0
    hits = run.get("cache_hits") or 0
    run["cache_hit_rate"] = round(hits / total, 3) if total else 0.0
    return run


@app.get("/api/v1/cache/stats", summary="缓存统计")
async def cache_stats() -> dict:
    return cache.stats()


@app.delete("/api/v1/cache", summary="清空缓存（做对照实验用）")
async def clear_cache() -> dict:
    n = cache.clear()
    return {"cleared": n}


@app.post("/api/v1/research/{run_id}/retry", summary="手工重试一个失败的任务")
async def retry_research(run_id: str) -> dict:
    """把失败的任务重新入队。

    注意：这里不复用原来的 run，而是把状态改回 pending 再走一遍 claim 流程。
    保留同一个 run_id，方便对比多次尝试的 trace。
    """
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"没有这个任务: {run_id}")
    if run["status"] not in (db.FAILED,):
        raise HTTPException(
            status_code=409,
            detail=f"只能重试失败的任务，当前状态是 {run['status']}",
        )

    with db._connect() as conn:  # noqa: SLF001 —— 内部小工具，不值得为它开新接口
        conn.execute(
            "UPDATE research_runs SET status = ?, error = NULL, "
            "lease_owner = NULL, lease_expires_at = NULL, attempts = 0 "
            "WHERE run_id = ?",
            (db.PENDING, run_id),
        )

    ok = await worker.submit(run_id, run["question"])
    return {"run_id": run_id, "submitted": ok}