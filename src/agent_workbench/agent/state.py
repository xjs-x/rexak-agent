"""图状态与版本。

**权威归属**：图 messages、工作摘要、当前计划保存在 LangGraph checkpoint（§4.4）。
UI 与上下文组装只做**只读投影**，不持有第二份可变会话历史。

状态里**不放**：数据库连接、HTTP client、浏览器对象、进程句柄、明文密钥。
这些属于运行依赖，通过 `context_schema` 注入（§6.1）。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, NotRequired, Required, TypedDict
from uuid import UUID

from langchain.agents import AgentState
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

#: 状态 schema 版本。升级时必须提供迁移或**明确拒绝恢复**，不能加载不兼容状态（§16.3）。
CURRENT_STATE_SCHEMA_VERSION = 1

StateSchemaVersion = Literal[1]


def merge_unique(left: list[str] | None, right: list[str] | None) -> list[str]:
    """稳定去重合并，保持首次出现的顺序。用于 id 集合类字段。"""
    seen: dict[str, None] = {}
    for item in list(left or []) + list(right or []):
        seen.setdefault(str(item), None)
    return list(seen)


class PlanStep(TypedDict):
    step_id: str
    description: str
    status: Literal["pending", "in_progress", "done", "dropped", "blocked"]
    rationale: str | None
    lead_id: str | None


def coalesce_plan(_left: list[PlanStep], right: list[PlanStep]) -> list[PlanStep]:
    """计划采用"整体替换 + 显式 revision"语义，不做逐项合并。

    有界、可修订，不是预先锁死的 DAG（§6.5）。
    """
    return list(right)


class WorkSummary(TypedDict):
    """结构化任务摘要 + **来源范围**。

    `evidence_refs` 与 `source_message_range` 让"当时给模型看了什么"可复盘（§7.2）。
    """

    objective: str
    current_focus: str
    key_findings: list[dict[str, Any]]
    failed_approaches: list[dict[str, Any]]
    unverified_hypotheses: list[str]
    open_questions: list[str]
    next_steps: list[str]
    source_message_range: list[int]
    summary_version: int


class CompletionProposal(TypedDict):
    """完成提议（§6.5）。`finish_gate` 会做确定性检查后才允许真正结束。"""

    reason: Literal["goal_answered", "exploration_converged"]
    goal_answer: str
    verified_findings: list[str]
    coverage_summary: dict[str, Any]
    remaining_leads: list[str]
    stop_rationale: str
    follow_up_suggestions: list[str]
    evidence_refs: list[str]
    proposed_at_turn: int


class AgentGraphState(AgentState):
    """扩展后的图状态。

    以 `AgentState` 为基类，因此 `messages` 仍由框架的 `add_messages` reducer 管理，
    **不要**自己用列表拼接替代消息协议。
    """

    messages: Required[Annotated[list[AnyMessage], add_messages]]

    # --- 身份 ---
    session_id: NotRequired[str]
    active_run_id: NotRequired[str]
    segment_index: NotRequired[int]

    # --- 目标与计划 ---
    #: 当前阶段目标；用户原文另存，不被摘要覆盖
    objective: NotRequired[str]
    #: 用户原文与最新约束（**永不**被摘要替换）
    user_constraints: NotRequired[list[str]]
    work_summary: NotRequired[WorkSummary]
    plan: NotRequired[list[PlanStep]]
    plan_revision: NotRequired[int]

    # --- 线索与任务 ---
    active_lead_ids: NotRequired[Annotated[list[str], merge_unique]]
    pending_job_ids: NotRequired[Annotated[list[str], merge_unique]]

    # --- 版本与上下文 ---
    coverage_revision: NotRequired[int]
    working_context_version: NotRequired[int]
    context_manifest: NotRequired[dict[str, Any]]

    # --- 结束判定 ---
    completion_proposal: NotRequired[CompletionProposal | None]
    finish_gate_feedback: NotRequired[list[str]]
    finish_gate_attempts: NotRequired[int]

    # --- 无进展收敛 ---
    no_progress_streak: NotRequired[int]
    last_progress_signature: NotRequired[str]

    # --- 运行边界 ---
    segment_work_units: NotRequired[int]
    state_schema_version: NotRequired[StateSchemaVersion]


def new_graph_state(
    *,
    session_id: UUID,
    run_id: UUID,
    objective: str,
    user_constraints: list[str] | None = None,
) -> AgentGraphState:
    """构造首轮输入状态。

    只包含**可序列化**的领域数据；运行依赖走 context。
    """
    return AgentGraphState(
        messages=[],
        session_id=str(session_id),
        active_run_id=str(run_id),
        segment_index=0,
        objective=objective,
        user_constraints=list(user_constraints or []),
        plan=[],
        plan_revision=0,
        active_lead_ids=[],
        pending_job_ids=[],
        coverage_revision=0,
        working_context_version=1,
        completion_proposal=None,
        finish_gate_feedback=[],
        finish_gate_attempts=0,
        no_progress_streak=0,
        last_progress_signature="",
        segment_work_units=0,
        state_schema_version=CURRENT_STATE_SCHEMA_VERSION,
    )


def next_input_state(
    *,
    session_id: UUID,
    run_id: UUID,
    objective: str,
    user_message: str,
    previous_plan: list[PlanStep] | None = None,
) -> dict[str, Any]:
    """阶段结束后的新 run 输入。

    复用同一 thread（session 映射的 thread_id 不变），因此这里只提供**增量输入**：
    保留旧计划、清空本阶段结束标记。
    """
    return {
        "messages": [{"role": "user", "content": user_message}],
        "session_id": str(session_id),
        "active_run_id": str(run_id),
        "objective": objective,
        "plan": list(previous_plan or []),
        "completion_proposal": None,
        "finish_gate_feedback": [],
        "finish_gate_attempts": 0,
        "pending_job_ids": [],
        "no_progress_streak": 0,
        "segment_work_units": 0,
        "state_schema_version": CURRENT_STATE_SCHEMA_VERSION,
    }


__all__ = [
    "CURRENT_STATE_SCHEMA_VERSION",
    "AgentGraphState",
    "CompletionProposal",
    "PlanStep",
    "StateSchemaVersion",
    "WorkSummary",
    "coalesce_plan",
    "merge_unique",
    "new_graph_state",
    "next_input_state",
]
