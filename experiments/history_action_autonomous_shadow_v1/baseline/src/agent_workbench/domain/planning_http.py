"""从真实操作回执检查 HTTP 工作范围；不解析自然语言漏洞结论。"""

from __future__ import annotations

from typing import Any

from agent_workbench.contracts.planning import (
    PlanningValidationError,
    StepCompletionSpec,
    http_endpoint,
)
from agent_workbench.persistence.tables import ToolOperationRow


def http_receipt(row: ToolOperationRow) -> dict[str, Any] | None:
    if row.tool_name != "http_request":
        return None
    context = row.execution_context or {}
    result = row.result_snapshot or {}
    data = result.get("data") or {}
    if not context.get("method") or not context.get("url"):
        return None
    try:
        url = http_endpoint(str(context["url"]))
    except ValueError:
        return None
    return {
        "operation_id": str(row.operation_id),
        "method": str(context["method"]).upper(),
        "url": url,
        "identity": context.get("identity", "anonymous"),
        "authentication": context.get("authentication", "unknown"),
        "status_code": data.get("status_code"),
        "source_step_id": context.get("step_id"),
        "operation_created_at": row.created_at.isoformat(),
    }


def check_http_work(
    spec: StepCompletionSpec, operations: dict[str, ToolOperationRow], *, step_id: str
) -> list[dict[str, Any]]:
    http_ops = [row for row in operations.values() if row.tool_name == "http_request"]
    receipts = [value for row in http_ops if (value := http_receipt(row)) is not None]
    recovery = (
        "把完成范围列为 completion_spec.http_checks=[{method,url,identity?}]，"
        "引用已有匹配操作/工件；缺少的检查保持未决，只在确实未取得结果时补测。"
        "HTTP 状态码不证明漏洞；自然语言结论仍属模型判断。"
    )
    if http_ops and not spec.http_checks and spec.check_type == "model":
        raise PlanningValidationError(
            "http_work_scope_required",
            f"步骤 {step_id} 的 HTTP 完成记录缺少明确工作范围",
            recovery=recovery,
            target_step_id=step_id,
        )
    coverage = []
    for required in spec.http_checks:
        matches = [
            r
            for r in receipts
            if r["method"] == required.method
            and r["url"] == required.url
            and (required.identity is None or r["identity"] == required.identity)
            and (required.authentication is None or r["authentication"] == required.authentication)
            and type(r["status_code"]) is int
            and 100 <= r["status_code"] <= 599
        ]
        if not matches:
            raise PlanningValidationError(
                "http_work_scope_missing",
                f"步骤 {step_id} 尚无覆盖指定方法、端点和身份的实际回执",
                recovery=recovery,
                target_step_id=step_id,
                missing_check=required.model_dump(mode="json"),
                available_receipts=receipts[:12],
            )
        # 只在模型已引用的真实来源中选择；不另查未提交的调用，也不执行请求。
        coverage.append(matches[-1])
    return coverage
