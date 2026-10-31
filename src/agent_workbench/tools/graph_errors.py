"""仅双图工具使用的失败投影；不复制原始参数、不自动修复或重试。"""

import json
from typing import Any

from pydantic import ValidationError
from pydantic.v1 import ValidationError as ValidationErrorV1

from agent_workbench.contracts.graph_errors import GraphValidationError
from agent_workbench.contracts.tool import ToolError
from agent_workbench.evidence.redaction import redact_api_value


def _text(value: str, limit: int = 240) -> str:
    return str(redact_api_value(value))[:limit]


def _failure(message: str, hint: str, details: dict[str, Any]) -> str:
    error = ToolError(
        # Graph Review 按此既有代码识别拒绝，细分类别只加到 details。
        code="validation_failed",
        message=_text(message, 600),
        retryable=False,
        recovery_hint=_text(hint, 400),
        details=redact_api_value(details),
    )
    return json.dumps(
        {
            "schema_version": 1,
            "tool": "update_graph",
            "status": "failed",
            "error": error.model_dump(mode="json"),
            "execution_certainty": "not_applied",
            "summary": "双图更新未接受；请按字段提示修正后发起新调用。",
        },
        ensure_ascii=False,
    )


def render_graph_failure(exc: GraphValidationError) -> str:
    """不伪造外部 Operation；图工具沿用 ToolMessage 调用身份。"""
    return _failure(
        exc.public_message,
        exc.recovery,
        {
            "graph_code": exc.reason_code,
            "path": _text(exc.path, 180),
            "expected": _text(exc.expected),
            "actual": _text(exc.actual),
            **exc.details,
        },
    )


def format_graph_schema_error(exc: ValidationError | ValidationErrorV1) -> str:
    """官方工具参数校验的回调；不让默认错误回显整个 kwargs。"""
    errors = (
        exc.errors(include_url=False, include_context=False, include_input=False)
        if isinstance(exc, ValidationError)
        else exc.errors()
    )
    issues = []
    for item in errors:
        loc = [
            x
            for x in item["loc"]
            if x
            not in {
                "add_node",
                "update_node",
                "add_edge",
                "update_edge",
                "Entity",
                "AttackState",
                "Relation",
                "Attempt",
            }
        ]
        if loc and loc[0] in {"runtime", "tool_call_id"}:
            continue
        path = "".join(f"[{x}]" if isinstance(x, int) else "." + str(x) for x in loc).lstrip(".")
        kind = str(item["type"])
        # 联合类型缺判别键时，Pydantic 的 loc 只到 operations[i]。
        if kind in {"union_tag_not_found", "union_tag_invalid"}:
            path += ".op"
        reason = {
            "missing": "缺少当前Schema要求的字段。",
            "extra_forbidden": "当前对象不允许此字段；请核对graph对应的data类型。",
            "union_tag_not_found": "缺少操作判别字段op；每项须明确op与graph。",
            "union_tag_invalid": "op不是当前Schema允许的操作。",
            "literal_error": "字段值不在Schema允许的枚举中。",
            "string_type": "字段要求字符串。",
            "list_type": "字段要求数组。",
            "int_type": "字段要求整数。",
            "int_parsing": "字段不能解析为整数。",
        }.get(kind, "字段类型或约束不符合当前Schema。")
        issue = {
            "path": _text(path or "$", 180),
            "actual": kind,
            "reason": reason,
            "expected": "当前 update_graph JSON Schema",
        }
        if issue not in issues:
            issues.append(issue)
        if len(issues) == 4:
            break
    first = issues[0] if issues else {"path": "$", "actual": "schema_validation"}
    hint = (
        "每项必须含op、graph、data；information节点用label/source_refs，attack节点用"
        "summary/entity_refs（不含source_refs）；其他字段严格按Schema，勿在顶层放summary。"
    )
    # 不从英文错误中提取/执行参数；只展示有界字段路径和错误类别。
    return _failure(
        "双图工具参数未通过 Schema 校验；没有写入图。",
        hint,
        {
            "graph_code": "GRAPH_SCHEMA_INVALID",
            **first,
            "issues": issues,
            "expected": "当前 update_graph JSON Schema",
            "omitted_error_count": max(0, len(errors) - len(issues)),
        },
    )
