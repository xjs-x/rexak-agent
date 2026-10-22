"""官方 `create_agent` 合成闭环：弱点被真正消费，而不是只写进表里。

断言四者一致：**实际请求 payload、checkpoint、业务关系、操作的 execution_context**。
不能只 mock 掉 WorkingContext 后断言新增表里有数据（方案 §11.3）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Annotated, Any, cast

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import InjectedToolCallId, tool
from pydantic import Field
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.command_guard import CommandGuardMiddleware
from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
from agent_workbench.agent.middleware.finish_gate import FinishGateMiddleware
from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware
from agent_workbench.agent.middleware.working_context import WorkingContextMiddleware
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.contracts.enums import ToolEffectClass
from agent_workbench.domain.findings import FindingService
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.observability.spans import SpanRecorder
from agent_workbench.persistence.tables import (
    FindingRow,
    LeadRow,
    ToolOperationRow,
)
from agent_workbench.tools.completion import propose_completion
from agent_workbench.tools.execution_context import execution_context
from agent_workbench.tools.operation import OperationRegistry
from agent_workbench.tools.task import record_lead, update_plan
from tests.integration.test_request_budget import context_for
from tests.unit.test_context_budget import profile

pytestmark = pytest.mark.integration


class SteppedModel(BaseChatModel):
    """按步骤函数动态构造响应的模型替身（可读取上一批工具结果）。

    与 `ScriptedChatModel` 的区别：每一步可以基于**收到的消息**决定参数，
    因此可以验证"模型从工具回执里拿到弱点 ID 再建立线索"的真实链路。
    """

    steps: list[Callable[[list[BaseMessage]], AIMessage]] = Field(default_factory=list)
    received: list[list[BaseMessage]] = Field(default_factory=list)
    advance: int = 0

    @property
    def _llm_type(self) -> str:
        return "stepped-test-model"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> SteppedModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.received.append(list(messages))
        index = min(self.advance, len(self.steps) - 1)
        self.advance += 1
        return ChatResult(generations=[ChatGeneration(message=self.steps[index](list(messages)))])


def _last_tool_payload(messages: list[BaseMessage]) -> dict[str, Any]:
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            try:
                return json.loads(str(message.content))
            except json.JSONDecodeError:
                continue
    raise AssertionError("没有找到工具回执")


def _system_text(messages: list[BaseMessage]) -> str:
    return "\n".join(
        str(message.content) for message in messages if isinstance(message, SystemMessage)
    )


def _call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}]
    )


def _prepare_context(context: Any, database: Any) -> None:
    toolbox = context.toolbox
    toolbox.artifacts = ArtifactStore(database.settings)
    toolbox.spans = SpanRecorder()
    toolbox.owner = None
    toolbox.owner_generation = 0
    toolbox.scope_revision = 0
    toolbox.policy_revision = 1
    toolbox.identity_id = None
    toolbox.disabled_tools = {}
    toolbox.control_version = 0
    counts: dict[str, int] = {}

    def _bump(name: str) -> int:
        counts[name] = counts.get(name, 0) + 1
        return counts[name]

    toolbox.bump = _bump


PROBE_CALLS: list[dict[str, Any]] = []


@tool
async def probe(
    where: str, runtime: ToolRuntime[RunContext], tool_call_id: Annotated[str, InjectedToolCallId]
) -> str:
    """只读探测（记录派发时的执行关联，不产生远端副作用）。"""
    PROBE_CALLS.append(dict(execution_context.get() or {}))
    toolbox = runtime.context.toolbox
    async with toolbox.db.transaction() as db:
        prepared = await OperationRegistry(db).prepare(
            run_id=toolbox.run_id,
            session_id=toolbox.session_id,
            tool_call_id=tool_call_id,
            tool_name="probe",
            arguments={"where": where},
            effect_class=ToolEffectClass.READ_ONLY,
            policy_revision=toolbox.policy_revision,
            scope_revision=toolbox.scope_revision,
            dispatch_control_version=runtime.state.get("dispatch_control_version"),
        )
        prepared.row.status = "succeeded"
        prepared.row.result_snapshot = {"fixture_result": "observed"}
    return f"probe:{where}"


def _middleware() -> list[AgentMiddleware[Any, Any]]:
    return [
        CommandGuardMiddleware(),
        WorkingContextMiddleware(),
        ContextBuilderMiddleware(profile()),
        ToolDispatchMiddleware(),
        FinishGateMiddleware(),
    ]


async def test_optional_plan_and_lead_associate_execution_without_proof_protocol(
    database, unique_name
):
    context = await context_for(database, unique_name)
    _prepare_context(context, database)
    PROBE_CALLS.clear()
    seen = {}

    def add_plan(messages):
        lead = _last_tool_payload(messages)["lead"]
        seen["lead"] = lead["lead_id"]
        return _call(
            "update_plan",
            {
                "expected_plan_revision": 0,
                "steps": [
                    {
                        "step_id": "s1",
                        "description": "可选调查",
                        "status": "in_progress",
                        "lead_id": lead["lead_id"],
                    }
                ],
            },
            "plan",
        )

    model = SteppedModel(
        steps=[
            lambda _: _call("record_lead", {"hypothesis": "可选轻量假设"}, "lead"),
            add_plan,
            lambda _: _call("probe", {"where": "local"}, "probe"),
            lambda _: AIMessage(content="已取得本地结果；假设尚未验证"),
        ]
    )
    graph = create_agent(
        model=model,
        tools=[record_lead, update_plan, probe],
        middleware=_middleware(),
        state_schema=AgentGraphState,
        context_schema=RunContext,
    )
    result = await graph.ainvoke(
        cast(Any, {"messages": [HumanMessage(content="仅本地工具")]}), context=context
    )
    assert result["completion_proposal"]["goal_answer"].startswith("已取得")
    assert result["plan"][0]["status"] == "in_progress"
    assert "work_assessment" not in result
    assert PROBE_CALLS[0]["step_id"] == "s1"
    assert PROBE_CALLS[0]["lead_ids"] == [seen["lead"]]
    async with database.session() as db:
        operation = (
            await db.execute(
                select(ToolOperationRow).where(
                    ToolOperationRow.run_id == context.toolbox.run_id,
                    ToolOperationRow.tool_name == "probe",
                )
            )
        ).scalar_one()
        assert operation.status == "succeeded" and operation.execution_context["step_id"] == "s1"


async def test_candidate_remains_candidate_when_model_finishes_without_verification(
    database, unique_name
):
    from agent_workbench.tools.task import propose_finding

    context = await context_for(database, unique_name)
    _prepare_context(context, database)
    model = SteppedModel(
        steps=[
            lambda _: _call(
                "propose_finding", {"title": "未验证候选", "description": "本地记录"}, "candidate"
            ),
            lambda _: _call("propose_completion", {"goal_answer": "候选未经过独立验证"}, "finish"),
        ]
    )
    graph = create_agent(
        model=model,
        tools=[propose_finding, propose_completion],
        middleware=_middleware(),
        state_schema=AgentGraphState,
        context_schema=RunContext,
    )
    result = await graph.ainvoke(
        cast(Any, {"messages": [HumanMessage(content="保存候选并结束")]}), context=context
    )
    assert result["completion_proposal"]["goal_answer"] == "候选未经过独立验证"
    assert len(model.received) == 2
    async with database.session() as db:
        row = (
            await db.execute(
                select(FindingRow).where(FindingRow.session_id == context.toolbox.session_id)
            )
        ).scalar_one()
        assert row.status == "candidate"


async def test_budget_trimming_does_not_mark_unshown_weakness_as_presented(database, unique_name):
    """预算裁掉的条目不算已呈现：manifest 与 checkpoint 都不能推进游标。"""
    context = await context_for(database, unique_name)
    _prepare_context(context, database)
    async with database.transaction() as db:
        service = FindingService(db)
        ids = []
        for index in range(4):
            created = await service.upsert_weakness(
                session_id=context.toolbox.session_id,
                title=f"候选弱点 {index}",
                description="用于预算裁剪",
            )
            ids.append(str(created.weakness_id))

    from agent_workbench.agent.middleware.context_builder import _copy_working, _trim_working
    from agent_workbench.agent.working_context import assemble_working_context

    async with database.session() as db:
        view = await assemble_working_context(
            db, context.toolbox.session_id, {"objective": "预算裁剪"}
        )
    consumption = view["weakness_consumption"]
    assert consumption["weakness_candidate_count"] == 4
    assert len(consumption["changed_weaknesses"]) == 2, "初始呈现最多 2 条变化弱点"
    assert consumption["presentation_budget"]["max_changed_weaknesses"] == 2

    copy = _copy_working(view)
    assert copy is not None
    assert _trim_working(copy) in {
        f"weakness:{item['weakness_id']}" for item in consumption["changed_weaknesses"]
    }
    trimmed_ids = [
        item["weakness_id"] for item in copy["weakness_consumption"]["changed_weaknesses"]
    ]
    assert len(trimmed_ids) == 1
    assert set(trimmed_ids) < {item["weakness_id"] for item in consumption["changed_weaknesses"]}

    async with database.session() as db:
        rows = (
            (
                await db.execute(
                    select(FindingRow).where(FindingRow.session_id == context.toolbox.session_id)
                )
            )
            .scalars()
            .all()
        )
        leads = (
            (
                await db.execute(
                    select(LeadRow).where(LeadRow.session_id == context.toolbox.session_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 4 and sorted(str(row.id) for row in rows) == sorted(ids)
    assert leads == []
