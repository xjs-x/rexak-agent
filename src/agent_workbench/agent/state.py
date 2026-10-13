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
#: v3：增加 `current_focus.source`（explicit | plan_inferred）与 `work_assessment`
#: （最近一次明确判断）。1→2→3 顺序兼容，不允许跳过迁移。
CURRENT_STATE_SCHEMA_VERSION = 3

StateSchemaVersion = Literal[3]

#: 可迁移到当前版本的旧版本（由 `state_migration` 提供显式适配）。
MIGRATABLE_STATE_SCHEMA_VERSIONS = (1, 2)


class StateSchemaIncompatibleError(RuntimeError):
    """已有 checkpoint 缺少兼容版本；禁止把它当作新任务重放。"""


def assert_compatible_state(values: dict[str, Any]) -> None:
    """恢复前校验已有图状态；空状态代表首次运行。"""
    if not values:
        return
    version = values.get("state_schema_version")
    if type(version) is not int or version != CURRENT_STATE_SCHEMA_VERSION:
        raise StateSchemaIncompatibleError(
            f"checkpoint 状态版本不兼容：实际 {version!r}，"
            f"当前 {CURRENT_STATE_SCHEMA_VERSION}。请先备份并执行对应状态迁移；"
            "在迁移前拒绝恢复，避免重复执行工具。"
        )


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
    done_when: NotRequired[str | None]
    resume_note: NotRequired[str | None]
    evidence_refs: NotRequired[list[str]]


class CurrentFocus(TypedDict):
    step_id: str | None
    lead_id: str | None
    intent: str
    reason: str
    return_to_step_id: str | None
    #: 焦点来源：模型明确选择 / 由唯一进行中步骤承接。自动推导不伪造模型判断。
    source: NotRequired[Literal["explicit", "plan_inferred"]]


class WorkAssessment(TypedDict):
    """**最近一次明确判断**（§5.1）。

    历史由消息、领域来源与阶段报告保存，**不在** checkpoint 无限追加日志。
    保存的是"解释"而不是漏洞事实；只读黑板和报告只做投影。
    """

    step_id: str | None
    lead_id: str | None
    #: 这次结果改变了什么
    changed: str
    #: 下一步准备消除哪个不确定性
    next_action: str
    #: 已有来源引用；纯计划调整可为空
    evidence_refs: list[str]
    #: 未测到、条件不成立、假设受反证等
    uncertainty: str | None
    #: 程序注入：产生这次判断的工具调用
    source_tool_call_id: str
    #: 程序维护的修订号
    revision: int


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
    source_message_ids: NotRequired[list[str]]
    summary_text: NotRequired[str]
    previous_summary_text: NotRequired[str]
    structured_status: NotRequired[str]
    source_archive_artifact_id: NotRequired[str]
    summary_artifact_id: NotRequired[str]
    previous_summary_artifact_id: NotRequired[str | None]
    previous_summary_text_truncated: NotRequired[bool]
    summary_version: int


class CompletionProposal(TypedDict):
    """完成提议（§6.5）。`finish_gate` 会做确定性检查后才允许真正结束。"""

    reason: Literal["goal_answered", "exploration_converged"]
    goal_answer: str
    verified_findings: list[str]
    coverage_summary: dict[str, Any]
    remaining_leads: list[str]
    relevant_lead_ids: NotRequired[list[str]]
    stop_rationale: str
    follow_up_suggestions: list[str]
    missing_conditions: list[str]
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
    #: **本模型轮次开始时固定下来的** control_version。
    #: 派发工具前必须与数据库当前值比较，不一致 => 该响应已过期，工具不得执行（§4.3）。
    #: 不能"用当前值对比当前值"，那就是没有检查。
    dispatch_control_version: NotRequired[int | None]
    work_summary: NotRequired[WorkSummary]
    plan: NotRequired[list[PlanStep]]
    plan_revision: NotRequired[int]
    current_focus: NotRequired[CurrentFocus | None]
    #: 最近一次明确判断（解释，不是漏洞事实）。
    work_assessment: NotRequired[WorkAssessment | None]
    review_cursor: NotRequired[str | None]
    last_review_turn: NotRequired[int]
    last_review_source_signature: NotRequired[str]
    last_progress_turn: NotRequired[int]
    last_progress_reminder_turn: NotRequired[int]
    progress_feedback: NotRequired[str | None]
    #: 相似结果连续回合数（**不是**"已证实的无进展"），只影响提醒节奏。
    similar_result_streak: NotRequired[int]
    last_similar_signature: NotRequired[str]
    last_similar_reminder_turn: NotRequired[int]
    state_migration: NotRequired[dict[str, Any]]
    report_progress_signature: NotRequired[str]
    report_retry_key: NotRequired[str | None]

    # --- 线索与任务 ---
    active_lead_ids: NotRequired[list[str]]
    pending_job_ids: NotRequired[list[str]]

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
    segment_completed_rounds: NotRequired[int]
    completed_decision_rounds: NotRequired[int]
    last_completed_model_message_id: NotRequired[str | None]
    segment_yield_reason: NotRequired[str | None]
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
        current_focus=None,
        work_assessment=None,
        active_lead_ids=[],
        pending_job_ids=[],
        coverage_revision=0,
        working_context_version=1,
        completion_proposal=None,
        finish_gate_feedback=[],
        finish_gate_attempts=0,
        no_progress_streak=0,
        last_progress_signature="",
        similar_result_streak=0,
        last_similar_signature="",
        segment_work_units=0,
        segment_completed_rounds=0,
        completed_decision_rounds=0,
        last_completed_model_message_id=None,
        segment_yield_reason=None,
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
        "messages": [{"role": "user", "content": user_message, "id": f"objective-{run_id}"}],
        "session_id": str(session_id),
        "active_run_id": str(run_id),
        "objective": objective,
        "plan": list(previous_plan or []),
        "completion_proposal": None,
        "finish_gate_feedback": [],
        "finish_gate_attempts": 0,
        "pending_job_ids": [],
        "no_progress_streak": 0,
        "last_progress_signature": "",
        "similar_result_streak": 0,
        "last_similar_signature": "",
        "segment_work_units": 0,
        "segment_completed_rounds": 0,
        "segment_yield_reason": None,
        "state_schema_version": CURRENT_STATE_SCHEMA_VERSION,
    }


__all__ = [
    "CURRENT_STATE_SCHEMA_VERSION",
    "MIGRATABLE_STATE_SCHEMA_VERSIONS",
    "AgentGraphState",
    "CompletionProposal",
    "CurrentFocus",
    "PlanStep",
    "StateSchemaIncompatibleError",
    "StateSchemaVersion",
    "WorkAssessment",
    "WorkSummary",
    "assert_compatible_state",
    "coalesce_plan",
    "merge_unique",
    "new_graph_state",
    "next_input_state",
]
