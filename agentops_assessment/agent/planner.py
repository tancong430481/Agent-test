from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from agentops_assessment.agent.fake_llm import FakeLLM

RE_SKU = re.compile(r"SKU-[A-Z0-9-]+", re.IGNORECASE)
RE_ANALYSIS_ONLY = re.compile(
    r"只分析|only\s+analyze|不创建|analysis\s+only|分析结论",
    re.IGNORECASE,
)
RE_APPROVAL = re.compile(
    r"审批|approval|草稿|draft|补货|replenishment",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PlanStep:
    id: str
    tool_name: str
    description: str
    input_template: dict[str, Any] = field(default_factory=dict)


class Planner:
    def __init__(self, llm: FakeLLM | None = None) -> None:
        self.llm = llm or FakeLLM()

    def create_plan(self, prompt: str, context: dict[str, Any] | None = None) -> list[PlanStep]:
        """为业务请求创建多步骤工具计划。

        推断 SKU 和业务意图，选择必要工具，并返回一个
        确定性的计划。计划覆盖 ERP、BI、知识库、供应商风险
        和可能的 OA 审批步骤，不写死单个用户、SKU 或样例 prompt。
        """
        self.llm.complete(prompt)

        skus = RE_SKU.findall(prompt)
        sku = skus[0].upper() if skus else "UNKNOWN"

        is_analysis_only = bool(RE_ANALYSIS_ONLY.search(prompt))
        _has_approval_intent = bool(RE_APPROVAL.search(prompt))

        steps: list[PlanStep] = []

        steps.append(
            PlanStep(
                id="get_inventory",
                tool_name="erp.get_inventory",
                description=f"查询 SKU {sku} 的 ERP 库存数据",
                input_template={"sku": sku},
            )
        )

        steps.append(
            PlanStep(
                id="get_sales",
                tool_name="bi.get_sales",
                description=f"查询 SKU {sku} 的销售与预测数据",
                input_template={"sku": sku},
            )
        )

        steps.append(
            PlanStep(
                id="search_knowledge",
                tool_name="knowledge.search",
                description="检索库存处理规则与审批策略",
                input_template={
                    "query": f"SKU {sku} 库存异常处理规则 补货审批",
                    "user_permissions": "${user_permissions}",
                    "top_k": 3,
                },
            )
        )

        steps.append(
            PlanStep(
                id="get_supplier_risk",
                tool_name="supplier.get_risk",
                description="查询供应商风险",
                input_template={"supplier_id": "${supplier_id}"},
            )
        )

        if not is_analysis_only:
            steps.append(
                PlanStep(
                    id="create_approval",
                    tool_name="oa.create_approval_draft",
                    description="创建补货审批草稿",
                    input_template={
                        "sku": sku,
                        "approval_type": "inventory_replenishment",
                        "stock_gap": "${stock_gap}",
                        "recommended_action": "create_replenishment_approval",
                    },
                )
            )

        return steps
