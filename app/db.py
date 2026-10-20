"""ResearchFlow · 存储层（阶段 0）

阶段 0 刻意只用 Python 标准库里的 sqlite3：
少装一个组件，就少一个能把你卡住的地方。

注意一个后面要回来讨论的点：
    这里的读写都是"同步阻塞"的。阶段 0 数据量小、写的是本地文件，
    所以感受不到问题。阶段 2 我们会亲手把它变成瓶颈，再讨论怎么解决。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "researchflow.db"

SCHEMA = """
-- 一次"研究任务"的生命周期
CREATE TABLE IF NOT EXISTS research_runs (
    run_id     TEXT PRIMARY KEY,
    question   TEXT NOT NULL,
    status     TEXT NOT NULL,      -- pending | running | succeeded | failed
    report     TEXT,
    error      TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);

-- 这一次任务里发生的每一步（这就是后面"可观测性"的地基）
CREATE TABLE IF NOT EXISTS research_steps (
    step_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    step_no     INTEGER NOT NULL,
    kind        TEXT NOT NULL,     -- llm | tool | system
    name        TEXT NOT NULL,
    payload     TEXT,              -- 这一步的输入
    output      TEXT,              -- 这一步的输出
    status      TEXT NOT NULL,     -- running | succeeded | failed
    started_at  REAL NOT NULL,
    finished_at REAL,
    duration_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_steps_run ON research_steps(run_id, step_no);
-- 这个索引是为了"查最近 20 条失败任务"这类查询准备的，阶段 3 会用到
CREATE INDEX IF NOT EXISTS idx_runs_status_created ON research_runs(status, created_at);
"""


def _now() -> float:
    return time.time()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL 让"一边写、一边读"成为可能 —— 否则查询会被写操作整个堵住
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """建表。可以重复执行，不会覆盖已有数据。"""
    with _connect() as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------
# research_runs
# --------------------------------------------------------------------------

def create_run(question: str) -> str:
    """新建一个任务，立刻返回 run_id（此时还没开始执行）。"""
    run_id = uuid.uuid4().hex[:12]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO research_runs (run_id, question, status, created_at) "
            "VALUES (?, ?, 'pending', ?)",
            (run_id, question, _now()),
        )
    return run_id


def mark_running(run_id: str) -> None:
    """Worker 真正开始干活了：pending -> running。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE research_runs SET status = 'running', started_at = ? WHERE run_id = ?",
            (_now(), run_id),
        )


def mark_finished(run_id: str, report: str | None = None, error: str | None = None) -> None:
    """收尾：要么成功带报告，要么失败带原因。绝不会两者都是空。"""
    status = "failed" if error else "succeeded"
    with _connect() as conn:
        conn.execute(
            "UPDATE research_runs SET status = ?, report = ?, error = ?, finished_at = ? "
            "WHERE run_id = ?",
            (status, report, error, _now(), run_id),
        )


def get_run(run_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM research_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, question, status, created_at, finished_at "
            "FROM research_runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_runs() -> dict[str, int]:
    """按状态统计任务数 —— /health 用它，阶段 3 的指标也用它。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM research_runs GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}


# --------------------------------------------------------------------------
# research_steps（trace）
# --------------------------------------------------------------------------

def add_step(run_id: str, step_no: int, kind: str, name: str, payload: str) -> int:
    """记下"某一步开始了"，返回 step_id 供结束时回填。"""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO research_steps "
            "(run_id, step_no, kind, name, payload, status, started_at) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (run_id, step_no, kind, name, payload, _now()),
        )
        return int(cur.lastrowid)


def finish_step(step_id: int, status: str, output: str, duration_ms: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE research_steps SET status = ?, output = ?, finished_at = ?, duration_ms = ? "
            "WHERE step_id = ?",
            (status, output, _now(), duration_ms, step_id),
        )


def get_steps(run_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT step_id, step_no, kind, name, payload, output, status, duration_ms "
            "FROM research_steps WHERE run_id = ? ORDER BY step_no",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]