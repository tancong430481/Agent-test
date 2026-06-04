from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from agentops_assessment.integrations.bi import BIClient
from agentops_assessment.integrations.erp import ERPClient
from agentops_assessment.integrations.exceptions import TransientIntegrationError
from agentops_assessment.integrations.oa import OAClient
from agentops_assessment.integrations.third_party import SupplierRiskClient
from agentops_assessment.rag.search import KnowledgeIndex

ToolCallable = Callable[[dict[str, Any]], dict[str, Any]]

SENSITIVE_FIELD_KEYS: set[str] = {"vendor_secret", "unit_cost_usd"}


def sanitize_output(obj: Any) -> Any:
    """递归移除 tool output / result / event payload 中的敏感字段。

    当前需要脱敏的字段：
      - vendor_secret
      - unit_cost_usd
    """
    if isinstance(obj, dict):
        return {
            key: sanitize_output(value)
            for key, value in obj.items()
            if key not in SENSITIVE_FIELD_KEYS
        }
    if isinstance(obj, list):
        return [sanitize_output(item) for item in obj]
    return obj


class ToolRegistry:
    def __init__(self, retry_attempts: int = 1) -> None:
        self.retry_attempts = retry_attempts
        self._tools: dict[str, ToolCallable] = {}
        self.last_call_attempts: dict[str, int] = {}

    def register(self, name: str, func: ToolCallable) -> None:
        self._tools[name] = func

    @classmethod
    def with_default_clients(
        cls,
        fixtures_dir: str | Path = "fixtures",
        retry_attempts: int = 1,
        supplier_fail_first: bool = False,
    ) -> "ToolRegistry":
        registry = cls(retry_attempts=retry_attempts)
        fixtures = Path(fixtures_dir)
        erp = ERPClient(fixtures / "business" / "erp_inventory.json")
        bi = BIClient(fixtures / "business" / "bi_sales.json")
        oa = OAClient(fixtures / "business" / "oa_rules.json")
        supplier = SupplierRiskClient(
            fixtures / "business" / "supplier_risk.json",
            fail_first=supplier_fail_first,
        )
        knowledge = KnowledgeIndex()

        registry.register("erp.get_inventory", lambda args: erp.get_inventory(args["sku"]))
        registry.register("bi.get_sales", lambda args: bi.get_sales(args["sku"]))
        registry.register("oa.create_approval_draft", oa.create_approval_draft)
        registry.register("supplier.get_risk", lambda args: supplier.get_supplier_risk(args["supplier_id"]))
        registry.register(
            "knowledge.search",
            lambda args: knowledge.search(
                args["query"],
                user_permissions=args.get("user_permissions", []),
                top_k=args.get("top_k", 3),
            ),
        )
        return registry

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in self._tools:
            raise KeyError(f"未知工具: {name}")

        attempts = 0
        last_error: Exception | None = None
        max_attempts = max(1, self.retry_attempts)
        for _ in range(max_attempts):
            attempts += 1
            self.last_call_attempts[name] = attempts
            try:
                result = self._tools[name](args)
                # 对敏感字段做脱敏；vendor_secret、unit_cost_usd 等不得进入 result/events/audit
                return sanitize_output(result)
            except TransientIntegrationError as exc:
                last_error = exc
                continue
        if last_error:
            raise last_error
        raise RuntimeError(f"工具失败但没有抛出明确异常: {name}")
