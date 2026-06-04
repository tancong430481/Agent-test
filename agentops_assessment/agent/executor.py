from __future__ import annotations

import re
from typing import Any

from agentops_assessment.agent.planner import PlanStep
from agentops_assessment.agent.state import InMemoryRunStateStore, RunState, StepState
from agentops_assessment.agent.tools import ToolRegistry, sanitize_output
from agentops_assessment.backend import database


def _render_template(template: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """将 input_template 中的 ${key} 占位符替换为 context 中的值。"""
    result: dict[str, Any] = {}
    for key, value in template.items():
        if isinstance(value, str):
            m = re.match(r"^\$\{(.+)\}$", value)
            if m:
                var_name = m.group(1)
                result[key] = context.get(var_name, value)
            else:
                result[key] = value
        else:
            result[key] = value
    return result


class Executor:
    def __init__(
        self,
        registry: ToolRegistry,
        state_store: InMemoryRunStateStore | None = None,
    ) -> None:
        self.registry = registry
        self.state_store = state_store or InMemoryRunStateStore()

    def execute(
        self,
        run_id: str,
        plan: list[PlanStep],
        context: dict[str, Any],
    ) -> RunState:
        """执行计划并持久化步骤事件。

        实现：
        - 渲染入参模板
        - 调用工具（含重试）
        - 敏感字段脱敏
        - OA 写操作前校验用户权限
        - 异常时中断并记录错误
        - 汇总最终业务结果
        """
        steps = [
            StepState(step_id=step.id, tool_name=step.tool_name, status="pending")
            for step in plan
        ]
        state = RunState(run_id=run_id, status="running", steps=steps)
        self.state_store.save(state)

        result: dict[str, Any] = {}
        user_permissions: list[str] = context.get("user_permissions", [])
        oa_permission = "oa:approval:write"
        oa_was_called = False

        for i, step in enumerate(plan):
            input_args = _render_template(step.input_template, context)

            # 权限检查：OA 写操作
            if step.tool_name == "oa.create_approval_draft":
                if oa_permission not in user_permissions:
                    database.insert_run_event(
                        database.connect(),
                        run_id,
                        event_type="tool.skipped",
                        payload={
                            "tool_name": step.tool_name,
                            "reason": "missing_permission",
                            "required_permission": oa_permission,
                        },
                        tool_name=step.tool_name,
                    )
                    state.steps[i].status = "skipped"
                    state.steps[i].output = {
                        "reason": "missing_permission",
                        "required_permission": oa_permission,
                    }
                    continue

            # 执行工具（含重试）
            try:
                output = self.registry.call(step.tool_name, input_args)
            except Exception as exc:
                error_msg = str(exc)
                state.steps[i].status = "failed"
                state.steps[i].error = error_msg
                database.insert_run_event(
                    database.connect(),
                    run_id,
                    event_type="tool.call",
                    payload={
                        "tool_name": step.tool_name,
                        "error": error_msg,
                    },
                    tool_name=step.tool_name,
                )
                state.status = "failed"
                state.result = result
                self.state_store.save(state)
                return state

            # 保存脱敏后的输出到事件
            safe_output = sanitize_output(output)
            database.insert_run_event(
                database.connect(),
                run_id,
                event_type="tool.call",
                payload={
                    "tool_name": step.tool_name,
                    "output_summary": safe_output,
                },
                tool_name=step.tool_name,
            )

            # 更新状态
            state.steps[i].status = "completed"
            state.steps[i].output = safe_output

            # 将关键字段注入 context 供后续步骤使用
            if isinstance(safe_output, dict):
                context.update(safe_output)
                result.update(safe_output)

            if step.tool_name == "oa.create_approval_draft":
                oa_was_called = True

        # 构建最终业务结果
        business_result: dict[str, Any] = {}

        # SKU
        sku = context.get("sku", result.get("sku", ""))
        business_result["sku"] = sku

        # ERP fields
        if "warehouse" in result:
            business_result["warehouse"] = result["warehouse"]
        if "stock_gap" in result:
            business_result["stock_gap"] = result["stock_gap"]

        # BI fields
        if "forecast_units_next_14d" in result:
            business_result["forecast_units_next_14d"] = result["forecast_units_next_14d"]

        # Supplier risk
        supplier_keys = {"supplier_id", "risk_level"}
        supplier_risk = {k: result[k] for k in supplier_keys if k in result}
        if supplier_risk:
            business_result["supplier_risk"] = supplier_risk

        # Citations from knowledge search
        if "citations" in result:
            business_result["citations"] = result["citations"]

        # Recommended action
        has_approval_plan = any(s.tool_name == "oa.create_approval_draft" for s in plan)
        if has_approval_plan and oa_was_called:
            business_result["recommended_action"] = "create_replenishment_approval"
            if "approval_draft_id" in result:
                business_result["approval_draft_id"] = result["approval_draft_id"]
        else:
            business_result["recommended_action"] = "analysis_complete"

        state.status = "completed"
        state.result = business_result
        self.state_store.save(state)
        return state
