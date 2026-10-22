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
#: （最近一次明确判断）。
#: v4：计划步骤支持多线索（`lead_ids`）与类型（`kind`）；新增弱点呈现与复查进度字段
#: （`presented_weakness_ids` / `weakness_review_cursor` / `last_recall_keys`）。
#: v5：固定版本的 active_skills 引用与有界 skill_directives；1→2→3→4→5 顺序迁移。
#: v6：旧快照 assessment、分型完成、不可变修订来源；旧完成仅标记待复查。
#: v7：轻量可选计划与结束信号；旧协议字段仅兼容读取，不再参与生产决策。
CURRENT_STATE_SCHEMA_VERSION = 7

StateSchemaVersion = Literal[7]

#: 可迁移到当前版本的旧版本（由 `state_migration` 提供显式适配）。
MIGRATABLE_STATE_SCHEMA_VERSIONS = (1, 2, 3, 4, 5, 6)


def merge_skill_refs(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    merged = {key: value for key, value in (left or {}).items() if value is not None}
    for key, value in (right or {}).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


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
    rationale: NotRequired[str | None]
    lead_id: NotRequired[str | None]
    #: 步骤类型：覆盖检查 / 链条跟进 / 混合。缺失时按 `mixed` 读取（旧 checkpoint 兼容）。
    kind: NotRequired[Literal["coverage", "followup", "mixed"]]
    #: 本步骤服务的**全部**线索。旧步骤只有 `lead_id`，读取时映射为单元素列表。
    #: 一个步骤可以服务多条线索，但**不能**因此虚构多个当前焦点。
    lead_ids: NotRequired[list[str]]
    done_when: NotRequired[str | None]
    resume_note: NotRequired[str | None]
    evidence_refs: NotRequired[list[str]]
    completion_protocol: NotRequired[Literal[1]]
    completion_spec: NotRequired[dict[str, Any] | None]
    completion: NotRequired[dict[str, Any] | None]
    legacy_completion: NotRequired[bool]


def plan_step_lead_ids(step: dict[str, Any]) -> list[str]:
    """统一读取步骤服务的线索集合（兼容只有 `lead_id` 的旧步骤）。"""
    values = [str(item) for item in (step.get("lead_ids") or []) if item]
    primary = step.get("lead_id")
    if primary and str(primary) not in values:
        values.insert(0, str(primary))
    return list(dict.fromkeys(values))


def plan_step_kind(step: dict[str, Any]) -> str:
    kind = str(step.get("kind") or "").strip()
    return kind if kind in {"coverage", "followup", "mixed"} else "mixed"


class CurrentFocus(TypedDict):
    step_id: str | None
    lead_id: str | None
    intent: str
    reason: str
    return_to_step_id: str | None
    #: 焦点来源：模型明确选择 / 由唯一进行中步骤承接。自动推导不伪造模型判断。
    source: NotRequired[Literal["explicit", "plan_inferred"]]


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
    """最终回答或显式结束信号；不绑定计划、Coverage 或 Lead。"""

    reason: Literal["goal_answered", "exploration_converged"]
    goal_answer: str
    limitations: NotRequired[str]
    evidence_refs: list[str]
    proposed_at_turn: int
    source: NotRequired[str]


class LegacyProtocolState(AgentState):
    """仅为旧 checkpoint/导出保留的通道；新循环不写入或读取判断。"""

    active_skills: NotRequired[Annotated[dict[str, Any], merge_skill_refs]]
    skill_directives: NotRequired[dict[str, Any]]
    work_assessment: NotRequired[dict[str, Any] | None]
    presented_weakness_ids: NotRequired[list[str]]
    weakness_review_cursor: NotRequired[str | None]
    weakness_review_signature: NotRequired[str]
    last_weakness_revisions: NotRequired[dict[str, int]]
    last_recall_keys: NotRequired[list[str]]
    last_identity_signature: NotRequired[str]
    review_cursor: NotRequired[str | None]
    last_review_turn: NotRequired[int]
    last_review_source_signature: NotRequired[str]
    last_progress_turn: NotRequired[int]
    last_progress_reminder_turn: NotRequired[int]
    progress_feedback: NotRequired[str | None]
    similar_result_streak: NotRequired[int]
    last_similar_signature: NotRequired[str]
    last_similar_reminder_turn: NotRequired[int]
    report_progress_signature: NotRequired[str]
    report_retry_key: NotRequired[str | None]
    coverage_revision: NotRequired[int]
    no_progress_streak: NotRequired[int]
    last_progress_signature: NotRequired[str]
    segment_work_units: NotRequired[int]


class AgentGraphState(LegacyProtocolState):
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
    # 持久化交付回执，独立于可压缩的 messages；只保留最近一批。
    execution_delivery_batch_id: NotRequired[str | None]
    execution_delegation_id: NotRequired[str | None]
    execution_delegation_revision: NotRequired[int | None]
    work_summary: NotRequired[WorkSummary]
    plan: NotRequired[list[PlanStep]]
    plan_revision: NotRequired[int]
    #: 最近一次更新的来源；完整历史在不可变工件链，事件只是可重建投影。
    last_plan_change: NotRequired[dict[str, Any] | None]
    current_focus: NotRequired[CurrentFocus | None]
    state_migration: NotRequired[dict[str, Any]]

    # --- 线索与任务 ---
    active_lead_ids: NotRequired[list[str]]
    pending_job_ids: NotRequired[list[str]]

    # --- 版本与上下文 ---
    working_context_version: NotRequired[int]
    context_manifest: NotRequired[dict[str, Any]]

    # --- 结束判定 ---
    completion_proposal: NotRequired[CompletionProposal | None]
    finish_gate_feedback: NotRequired[list[str]]
    finish_gate_attempts: NotRequired[int]

    # --- 运行边界 ---
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
        last_plan_change=None,
        current_focus=None,
        active_lead_ids=[],
        pending_job_ids=[],
        working_context_version=1,
        completion_proposal=None,
        finish_gate_feedback=[],
        finish_gate_attempts=0,
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
    "WorkSummary",
    "assert_compatible_state",
    "coalesce_plan",
    "merge_unique",
    "new_graph_state",
    "next_input_state",
    "plan_step_kind",
    "plan_step_lead_ids",
]
