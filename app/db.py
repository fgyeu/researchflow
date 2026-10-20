"""ResearchFlow · 存储层（阶段 2）

阶段 2 相比之前做了三件大事：

1. **幂等键**（idempotency_key）
   同一个请求重复提交，只建一个任务、只花一次钱。

2. **租约**（lease_owner / lease_expires_at）
   这是"任务恢复"的基础。Worker 领任务时盖个时间戳，
   相当于说"这个任务我在处理，到期没续约就说明我死了，别人可以接手"。

3. **步骤缓存表**（tool_cache）
   只读工具的结果可以复用，能直接测出命中率。

## 关于并发安全

SQLite 的写锁是库级的，多人同时写会排队。所以这里所有"先查再写"的
关键操作都放进一个事务里，并依赖 UNIQUE 约束兜底：

    检查 + 创建必须原子，否则两个并发请求会同时通过检查

这就是经典的 check-then-act 竞态。真实系统里用
    SELECT ... FOR UPDATE  或  唯一索引 + 捕获冲突
来解决。SQLite 没有行锁，所以这里靠**唯一索引**。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "researchflow.db"

# 任务状态的合法取值
PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

SCHEMA = """
-- 一次"研究任务"的生命周期
CREATE TABLE IF NOT EXISTS research_runs (
    run_id     TEXT PRIMARY KEY,
    question   TEXT NOT NULL,
    status     TEXT NOT NULL,          -- pending | running | succeeded | failed
    report     TEXT,
    error      TEXT,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,

    -- 幂等：同一个幂等键只能对应一个任务
    idempotency_key TEXT,

    -- 租约：谁在处理、什么时候到期
    lease_owner      TEXT,
    lease_expires_at REAL,
    attempts         INTEGER NOT NULL DEFAULT 0,

    -- 命中缓存省下的工具调用次数（用于算命中率）
    cache_hits   INTEGER NOT NULL DEFAULT 0,
    tool_calls   INTEGER NOT NULL DEFAULT 0,

    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);

-- 幂等键唯一索引。
-- 注意：允许 NULL（没有幂等键的任务不受约束），
-- 而 SQLite 的 UNIQUE 索引里 NULL 之间不冲突，正好符合需要。
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idem
    ON research_runs(idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- 恢复扫描用：快速找出所有卡在 pending/running 的任务
CREATE INDEX IF NOT EXISTS idx_runs_status_lease
    ON research_runs(status, lease_expires_at);

-- 这一次任务里发生的每一步（可观测性的地基）
CREATE TABLE IF NOT EXISTS research_steps (
    step_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    step_no     INTEGER NOT NULL,
    kind        TEXT NOT NULL,     -- llm | tool | system
    name        TEXT NOT NULL,
    payload     TEXT,
    output      TEXT,
    status      TEXT NOT NULL,     -- running | succeeded | failed
    started_at  REAL NOT NULL,
    finished_at REAL,
    duration_ms INTEGER,
    attempt     INTEGER NOT NULL DEFAULT 1,
    error_type  TEXT               -- timeout | http | parse | unknown …
);

CREATE INDEX IF NOT EXISTS idx_steps_run ON research_steps(run_id, step_no);

-- 工具结果缓存。键 = 工具名 + 规范化后的参数
CREATE TABLE IF NOT EXISTS tool_cache (
    cache_key   TEXT PRIMARY KEY,
    tool_name   TEXT NOT NULL,
    result      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cache_expiry ON tool_cache(expires_at);
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
    """建表 + 补齐缺失的列。可以重复执行，不会覆盖已有数据。

    为什么要自己写"补列"这段？
    因为 `CREATE TABLE IF NOT EXISTS` 只在表不存在时建表。
    如果旧版本已经建过表，新加的列**永远不会出现**，
    然后程序会在运行时抛 `no such column` —— 这个错误看起来莫名其妙。

    生产环境用 Alembic 这类迁移工具；这里数据量小，用最简办法：
    检查列是否存在，缺了就 ALTER TABLE 加上。
    """
    with _connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


# 每个新列都要登记在这里，否则老数据库升级时会缺列
_MIGRATIONS = {
    "research_runs": [
        ("tokens_used", "INTEGER NOT NULL DEFAULT 0"),
        ("duration_ms", "INTEGER"),
        ("idempotency_key", "TEXT"),
        ("lease_owner", "TEXT"),
        ("lease_expires_at", "REAL"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("cache_hits", "INTEGER NOT NULL DEFAULT 0"),
        ("tool_calls", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "research_steps": [
        ("attempt", "INTEGER NOT NULL DEFAULT 1"),
        ("error_type", "TEXT"),
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    # 索引在补列之后重建，否则老库上会因缺列而失败
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_idem "
        "ON research_runs(idempotency_key) WHERE idempotency_key IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_status_lease "
        "ON research_runs(status, lease_expires_at)"
    )


# --------------------------------------------------------------------------
# 创建任务（幂等）
# --------------------------------------------------------------------------

def create_run(question: str, idempotency_key: str | None = None) -> tuple[str, bool]:
    """新建任务。返回 (run_id, 是否为新建)。

    **幂等的实现方式：唯一索引 + 捕获冲突。**

    流程：
        1. 先查这个幂等键是否已经有任务了 → 有就返回它（created=False）
        2. 没有就插入；如果插入时撞了唯一索引（并发场景），
           再查一次并返回已存在的那个

    第 2 步的"再查一次"是关键：
    第 1 步的检查和第 2 步的插入之间有时间窗口，
    两个并发请求可能都通过了检查。靠唯一索引兜底才不会建出两条。
    """
    run_id = uuid.uuid4().hex[:12]

    with _connect() as conn:
        if idempotency_key:
            row = conn.execute(
                "SELECT run_id FROM research_runs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row:
                return row["run_id"], False

        try:
            conn.execute(
                "INSERT INTO research_runs "
                "(run_id, question, status, idempotency_key, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, question, PENDING, idempotency_key, _now()),
            )
        except sqlite3.IntegrityError:
            # 并发下另一个请求先插进去了 —— 返回它建的那个
            row = conn.execute(
                "SELECT run_id FROM research_runs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row:
                return row["run_id"], False
            raise

    return run_id, True


# --------------------------------------------------------------------------
# 租约：任务恢复的基础
# --------------------------------------------------------------------------

def claim_run(run_id: str, owner: str, lease_seconds: float) -> bool:
    """领取任务（拿租约）。返回是否成功。

    条件：任务还是 pending，或者租约已过期（说明原来的 Worker 死了）。
    用一条 UPDATE ... WHERE 完成，避免 check-then-act 竞态。
    """
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE research_runs
               SET status = ?, lease_owner = ?, lease_expires_at = ?,
                   started_at = COALESCE(started_at, ?),
                   attempts = attempts + 1
             WHERE run_id = ?
               AND (
                     status = ?
                  OR (status = ? AND (lease_expires_at IS NULL OR lease_expires_at < ?))
               )
            """,
            (
                RUNNING,
                owner,
                now + lease_seconds,
                now,
                run_id,
                PENDING,
                RUNNING,
                now,
            ),
        )
        return cur.rowcount == 1


def renew_lease(run_id: str, owner: str, lease_seconds: float) -> bool:
    """续约。返回 False 说明租约已经丢了（被别人接手），当前 Worker 应停止。"""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE research_runs SET lease_expires_at = ? "
            "WHERE run_id = ? AND lease_owner = ?",
            (_now() + lease_seconds, run_id, owner),
        )
        return cur.rowcount == 1


def release_lease(run_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE research_runs SET lease_owner = NULL, lease_expires_at = NULL "
            "WHERE run_id = ?",
            (run_id,),
        )


def force_claim(run_id: str, owner: str, lease_seconds: float) -> bool:
    """强制领取任务 —— **只给启动恢复用**。

    和 claim_run 的区别：不看租约是否过期，只要是 pending/running 就抢。

    为什么这样是安全的：
        服务刚启动时，本机上不可能有别的 Worker 在跑。
        如果真有，那说明有人在同一台机器上用同一个 SQLite 文件跑多个
        Worker —— 那本来就不是支持的部署方式（SQLite 锁不住跨进程的租约）。

    为什么需要在运行期区分：
        运行期如果也强抢，两个 Worker 就会同时跑同一个任务，
        导致重复调模型、trace 交错、状态被后写的覆盖。那是真 bug。
    """
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """
            UPDATE research_runs
               SET status = ?, lease_owner = ?, lease_expires_at = ?,
                   started_at = COALESCE(started_at, ?),
                   attempts = attempts + 1
             WHERE run_id = ? AND status IN (?, ?)
            """,
            (
                RUNNING,
                owner,
                now + lease_seconds,
                now,
                run_id,
                PENDING,
                RUNNING,
            ),
        )
        return cur.rowcount == 1


def find_stale_running() -> list[dict[str, Any]]:
    """找出所有状态是 running 的任务，不管租约有没有过期。

    启动恢复用它：重启是个明确信号 —— 本机没有 Worker 活着了。
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, question, status, attempts, lease_expires_at "
            "FROM research_runs WHERE status = ? ORDER BY created_at",
            (RUNNING,),
        ).fetchall()
    return [dict(r) for r in rows]


def find_orphans(now: float | None = None) -> list[dict[str, Any]]:
    """找出"没人管"的任务：

    - pending 但从来没被领过（可能是刚提交，也可能是服务崩在提交之后）
    - running 但租约已过期（Worker 死了）

    这就是任务恢复的入口。
    """
    now = now if now is not None else _now()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT run_id, question, status, attempts, lease_expires_at
              FROM research_runs
             WHERE status = ?
                OR (status = ? AND (lease_expires_at IS NULL OR lease_expires_at < ?))
             ORDER BY created_at
            """,
            (PENDING, RUNNING, now),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# 状态更新
# --------------------------------------------------------------------------

def mark_running(run_id: str) -> None:
    """兼容旧接口：不推荐直接用，应该走 claim_run。"""
    with _connect() as conn:
        conn.execute(
            "UPDATE research_runs SET status = ?, started_at = COALESCE(started_at, ?) "
            "WHERE run_id = ?",
            (RUNNING, _now(), run_id),
        )


def mark_finished(run_id: str, report: str | None = None, error: str | None = None) -> None:
    """收尾：要么成功带报告，要么失败带原因。同时释放租约。"""
    status = FAILED if error else SUCCEEDED
    with _connect() as conn:
        conn.execute(
            "UPDATE research_runs SET status = ?, report = ?, error = ?, finished_at = ?, "
            "lease_owner = NULL, lease_expires_at = NULL WHERE run_id = ?",
            (status, report, error, _now(), run_id),
        )


def set_run_duration(run_id: str, duration_ms: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE research_runs SET duration_ms = ? WHERE run_id = ?",
            (duration_ms, run_id),
        )


def add_run_tokens(run_id: str, tokens: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE research_runs SET tokens_used = tokens_used + ? WHERE run_id = ?",
            (tokens, run_id),
        )


def bump_counters(run_id: str, tool_calls: int = 0, cache_hits: int = 0) -> None:
    """累加工具调用数和缓存命中数 —— 阶段 3 算命中率要用。"""
    if not tool_calls and not cache_hits:
        return
    with _connect() as conn:
        conn.execute(
            "UPDATE research_runs SET tool_calls = tool_calls + ?, "
            "cache_hits = cache_hits + ? WHERE run_id = ?",
            (tool_calls, cache_hits, run_id),
        )


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------

def get_run(run_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM research_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, question, status, created_at, finished_at, "
            "tokens_used, duration_ms, attempts, tool_calls, cache_hits "
            "FROM research_runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def count_runs() -> dict[str, int]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM research_runs GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}


# --------------------------------------------------------------------------
# 步骤（trace）
# --------------------------------------------------------------------------

def add_step(
    run_id: str,
    step_no: int,
    kind: str,
    name: str,
    payload: str,
    attempt: int = 1,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO research_steps "
            "(run_id, step_no, kind, name, payload, status, started_at, attempt) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?, ?)",
            (run_id, step_no, kind, name, payload, _now(), attempt),
        )
        return int(cur.lastrowid)


def finish_step(
    step_id: int,
    status: str,
    output: str,
    duration_ms: int,
    error_type: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE research_steps SET status = ?, output = ?, finished_at = ?, "
            "duration_ms = ?, error_type = ? WHERE step_id = ?",
            (status, output, _now(), duration_ms, error_type, step_id),
        )


def get_steps(run_id: str) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT step_id, step_no, kind, name, payload, output, status, "
            "duration_ms, attempt, error_type "
            "FROM research_steps WHERE run_id = ? ORDER BY step_id",
            (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# 工具缓存
# --------------------------------------------------------------------------

def cache_get(cache_key: str) -> str | None:
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT result, expires_at FROM tool_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < now:
            conn.execute("DELETE FROM tool_cache WHERE cache_key = ?", (cache_key,))
            return None
        conn.execute(
            "UPDATE tool_cache SET hits = hits + 1 WHERE cache_key = ?", (cache_key,)
        )
        return row["result"]


def cache_put(cache_key: str, tool_name: str, result: str, ttl_seconds: float) -> None:
    now = _now()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tool_cache "
            "(cache_key, tool_name, result, created_at, expires_at, hits) "
            "VALUES (?, ?, ?, ?, ?, COALESCE("
            "(SELECT hits FROM tool_cache WHERE cache_key = ?), 0))",
            (cache_key, tool_name, result, now, now + ttl_seconds, cache_key),
        )


def cache_stats() -> dict[str, Any]:
    """缓存整体统计 —— 阶段 3 的命中率数据就来自这里。"""
    now = _now()
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM tool_cache").fetchone()["n"]
        alive = conn.execute(
            "SELECT COUNT(*) AS n FROM tool_cache WHERE expires_at >= ?", (now,)
        ).fetchone()["n"]
        hits = conn.execute(
            "SELECT COALESCE(SUM(hits), 0) AS n FROM tool_cache"
        ).fetchone()["n"]
    return {"entries": total, "alive": alive, "total_hits": hits}


def cache_clear() -> int:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM tool_cache")
        return cur.rowcount