"""阶段结束工具：`propose_completion`。

**提交结构化结束提议**，不直接结束。是否真的结束由确定性 `finish_gate` 决定（§6.5）。

V3（方案 §6.4）：`coverage_summary` / `remaining_leads` / `relevant_lead_ids`
保留用于**兼容读取与说明**，但**不再**作为放行凭据——复查集合由程序从业务表完整枚举。
`relevant_lead_ids` 只表达展示偏好，不能用来排除其他对象。
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.types import Command

from agent_workbench.agent.context import RunContext
from agent_workbench.tools.params import coerce_json_object, coerce_str_list

COMPLETION_REASONS = {"goal_answered", "exploration_converged"}


@tool("propose_completion")
async def propose_completion(
    runtime: ToolRuntime[RunContext],
    reason: str,
    goal_answer: str,
    stop_rationale: str,
    coverage_summary_json: dict[str, Any] | str | None = None,
    verified_findings_json: list[Any] | str | None = None,
    remaining_leads_json: list[Any] | str | None = None,
    evidence_refs_json: list[Any] | str | None = None,
    follow_up_suggestions_json: list[Any] | str | None = None,
    missing_conditions_json: list[Any] | str | None = None,
    tool_call_id: Annotated[str, InjectedToolCallId] = "",
    expected_plan_revision: int | None = None,
) -> Command:
    """提交**阶段结束提议**。系统会做确定性检查后才真正结束本阶段。

    Args:
        reason: `goal_answered`（目标已回答）或 `exploration_converged`（当前可达范围内收敛）。
        goal_answer: 对用户目标的直接回答。
        stop_rationale: 为什么现在可以结束。
        coverage_summary_json: 覆盖摘要的 JSON 对象字符串，例如
            `{"checked":12,"unresolved_items":[{"lead_id":"...","disposition":"deferred|outside_goal|not_required","reason":"具体搁置理由"}]}`。
            **说明用途**：这些标签不再豁免检查，只是解释；未决对象仍会出现在复查集合中。
        verified_findings_json: 已确认发现的 JSON 数组字符串（每项含 finding_id 与证据引用）。
        remaining_leads_json: 剩余线索的 JSON 数组字符串（说明为何可以搁置）。
        evidence_refs_json: 关键证据的工件 ID 的 JSON 数组字符串。
        follow_up_suggestions_json: 后续建议的 JSON 数组字符串。
        missing_conditions_json: 缺少账号、目标不可达等继续测试所需条件。
            只有在**所有有效方向**都确实需要外部条件时才填；否则请继续推进。
        tool_call_id: 由框架注入，不要自己填写。
        expected_plan_revision: 本轮已提交计划版本；计划更新后请在下一批提交结束。

    Returns:
        提交确认；是否通过由 finish_gate 决定。

    Notes:
        - 提交前请自查：有未处理的高优先级用户指令吗？有状态未知的后台任务吗？
          **每个实质弱点都做过后续用途判断吗**？仍有可推进方向吗？确认发现的证据是否真实可用？
        - `blocked` / `deferred` 不是自动放行；"不是当前目标必需"不再是自主结束的豁免。
        - **缺少账号/目标不可达**这类情况应当说明受阻（会进入等待用户），
          而不是提交"没有问题"的完成提议。
    """
    if reason not in COMPLETION_REASONS:
        raise ValueError(f"reason 必须是 {sorted(COMPLETION_REASONS)} 之一，收到 {reason!r}")
    from agent_workbench.contracts.planning import planning_digest

    revision = int(runtime.state.get("plan_revision") or 0)
    if expected_plan_revision is not None and expected_plan_revision != revision:
        raise ValueError("结束提议的计划版本已过期")

    focus = runtime.state.get("current_focus") or {}
    payload: dict[str, Any] = {
        "reason": reason,
        "goal_answer": goal_answer,
        "stop_rationale": stop_rationale,
        "coverage_summary": coerce_json_object(coverage_summary_json, "coverage_summary_json"),
        "verified_findings": coerce_str_list(verified_findings_json, "verified_findings_json"),
        "remaining_leads": coerce_str_list(remaining_leads_json, "remaining_leads_json"),
        "relevant_lead_ids": [focus["lead_id"]] if focus.get("lead_id") else [],
        "focus_lead_id": focus.get("lead_id"),
        "evidence_refs": coerce_str_list(evidence_refs_json, "evidence_refs_json"),
        "follow_up_suggestions": coerce_str_list(
            follow_up_suggestions_json, "follow_up_suggestions_json"
        ),
        "missing_conditions": coerce_str_list(missing_conditions_json, "missing_conditions_json"),
        "proposed_at_turn": len(runtime.state.get("messages") or []),
        "plan_revision": revision,
        "plan_signature": planning_digest(list(runtime.state.get("plan") or [])),
    }

    text = json.dumps(
        {
            "ok": True,
            "proposal": payload,
            "note": (
                "提议已提交；finish_gate 会从业务表完整枚举复查集合"
                "（未完成线索、暂缓/受阻线索、未完成计划事项、覆盖缺口、待分析弱点），"
                "并检查待处理指令、证据可用性与挂起任务。"
                "`remaining_leads` / `coverage_summary` 里的处置标签只作说明，不作为放行凭据。"
            ),
        },
        ensure_ascii=False,
    )
    return Command(
        update={
            "completion_proposal": payload,
            "messages": [
                ToolMessage(
                    content=text, tool_call_id=tool_call_id or "", name="propose_completion"
                )
            ],
        }
    )


COMPLETION_TOOLS = [propose_completion]
