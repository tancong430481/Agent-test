from __future__ import annotations

from importlib import resources
from pathlib import Path

from agentops_assessment.agent.executor import Executor
from agentops_assessment.agent.planner import Planner
from agentops_assessment.agent.tools import ToolRegistry
from agentops_assessment.backend import database
from agentops_assessment.backend.auth import get_user


def _resolve_fixtures_dir() -> Path:
    """按环境变量或默认路径确定 fixture 目录。"""
    import os

    env = os.getenv("ASSESSMENT_FIXTURES_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "fixtures"


def execute_run(run_id: str) -> None:
    """后台执行入口。

    用完整的 Planner -> Executor 流程替换此占位实现。
    实现更新 running/completed/failed 状态，持久化步骤事件，
    通过 ToolRegistry 调用工具，记录 token 成本，并保存最终业务结果。
    """
    conn = None
    try:
        conn = database.connect()
        database.init_db(conn)

        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            return

        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (run["task_id"],)).fetchone()
        if not task:
            return

        now = database.now_iso()
        conn.execute(
            "UPDATE runs SET status = ?, started_at = ? WHERE id = ?",
            ("running", now, run_id),
        )
        conn.commit()

        user = get_user(task["created_by"])
        if not user:
            conn.execute(
                "UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                ("failed", "用户不存在", database.now_iso(), run_id),
            )
            conn.commit()
            return

        # 创建计划
        planner = Planner()
        plan = planner.create_plan(task["prompt"])

        # 构建执行上下文
        exec_context: dict = {
            "user_permissions": user["permissions"],
            "user_id": user["id"],
        }

        # 创建工具注册表
        fixtures_dir = _resolve_fixtures_dir()
        registry = ToolRegistry.with_default_clients(fixtures_dir=fixtures_dir, retry_attempts=2)

        # 执行
        executor = Executor(registry)
        state = executor.execute(run_id, plan, exec_context)

        # 更新运行结果
        now = database.now_iso()
        if state.status == "completed":
            conn.execute(
                """
                UPDATE runs
                SET status = ?, result_json = ?, finished_at = ?, token_cost = COALESCE(token_cost, 0) + 1
                WHERE id = ?
                """,
                ("completed", database.encode_json(state.result), now, run_id),
            )
        elif state.status == "failed":
            error_msg = state.steps[-1].error if state.steps else "执行失败"
            conn.execute(
                """
                UPDATE runs
                SET status = ?, error = ?, finished_at = ?, token_cost = COALESCE(token_cost, 0) + 1
                WHERE id = ?
                """,
                ("failed", error_msg, now, run_id),
            )
        else:
            conn.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE id = ?",
                (state.status, now, run_id),
            )
        conn.commit()

    except Exception as exc:
        try:
            if conn is None:
                conn = database.connect()
                database.init_db(conn)
            conn.execute(
                "UPDATE runs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                ("failed", str(exc), database.now_iso(), run_id),
            )
            conn.commit()
        except Exception:
            pass
    finally:
        if conn:
            conn.close()
