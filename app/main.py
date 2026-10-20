"""ResearchFlow · API 层（阶段 0）

这一层只干三件事：
    1. 接请求、校验参数
    2. 把任务交给 Worker，立刻返回 run_id
    3. 把数据库里的状态读出来返回给调用方

它故意很薄 —— 业务逻辑不该长在 API 层里。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import db, worker


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 服务启动时确保表结构存在（可重复执行）
    db.init_db()
    yield


app = FastAPI(
    title="ResearchFlow",
    version="0.1.0",
    description="基于异步任务调度与持久化执行的 AI 研究助手（阶段 0）",
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


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------

@app.get("/health", summary="健康检查")
async def health() -> dict:
    return {
        "status": "ok",
        "runs_by_status": db.count_runs(),
        "worker": worker.stats(),
    }


@app.post("/api/v1/research", status_code=202, summary="提交一个研究任务")
async def create_research(req: ResearchRequest) -> dict:
    """提交任务，**立刻**返回 run_id，不等研究跑完。

    返回 202 Accepted 而不是 200 OK —— 这个状态码的含义就是
    "我收下了，但还没做完"，跟同步接口区分开。
    """
    run_id = db.create_run(req.question)
    await worker.submit(run_id, req.question)
    return {"run_id": run_id, "status": "pending"}


@app.get("/api/v1/research", summary="列出最近的任务")
async def list_research(limit: int = Query(20, ge=1, le=200)) -> dict:
    return {"items": db.list_runs(limit)}


@app.get("/api/v1/research/{run_id}", summary="查询任务状态、执行轨迹与报告")
async def get_research(run_id: str) -> dict:
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"没有这个任务: {run_id}")
    run["steps"] = db.get_steps(run_id)
    return run