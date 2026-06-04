from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone

from agentops_assessment.backend import database


def build_dashboard(conn: sqlite3.Connection) -> dict:
    task_count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    run_count = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
    failed_count = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status = 'failed'"
    ).fetchone()["c"]
    completed_count = conn.execute(
        "SELECT COUNT(*) AS c FROM runs WHERE status = 'completed'"
    ).fetchone()["c"]
    token_cost = conn.execute("SELECT COALESCE(SUM(token_cost), 0) AS c FROM runs").fetchone()["c"]
    events = conn.execute("SELECT tool_name FROM run_events WHERE tool_name IS NOT NULL").fetchall()
    tool_counts = Counter(row["tool_name"] for row in events)

    # 补充平均耗时、最近失败、队列健康度。
    # 平均耗时（已结束 run）
    finished_runs = conn.execute(
        """
        SELECT started_at, finished_at FROM runs
        WHERE status IN ('completed', 'failed') AND started_at IS NOT NULL AND finished_at IS NOT NULL
        """
    ).fetchall()
    total_seconds = 0.0
    finished_count = len(finished_runs)
    for row in finished_runs:
        start = datetime.fromisoformat(row["started_at"])
        end = datetime.fromisoformat(row["finished_at"])
        total_seconds += (end - start).total_seconds()
    average_run_seconds = total_seconds / finished_count if finished_count else 0.0

    # 最近失败（最多 5 条）
    recent_failures_rows = conn.execute(
        """
        SELECT r.id AS run_id, r.task_id, r.error, r.finished_at
        FROM runs r
        WHERE r.status = 'failed'
        ORDER BY r.finished_at DESC
        LIMIT 5
        """
    ).fetchall()
    recent_failures = [
        {
            "run_id": row["run_id"],
            "task_id": row["task_id"],
            "error": row["error"] if row["error"] else "未知错误",
            "finished_at": row["finished_at"],
        }
        for row in recent_failures_rows
    ]

    # 权限拒绝数
    deny_count = conn.execute(
        "SELECT COUNT(*) AS c FROM audit_logs WHERE decision = 'deny'"
    ).fetchone()["c"]

    return {
        "task_count": task_count,
        "run_count": run_count,
        "completed_count": completed_count,
        "failed_count": failed_count,
        "failure_rate": failed_count / run_count if run_count else 0,
        "token_cost": token_cost,
        "average_run_seconds": average_run_seconds,
        "tool_call_counts": dict(tool_counts),
        "recent_failures": recent_failures,
        "deny_count": deny_count,
        "generated_at": database.now_iso(),
    }
