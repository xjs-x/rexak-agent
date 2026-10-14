"""官方 `create_agent` 合成闭环：弱点被真正消费，而不是只写进表里。

断言四者一致：**实际请求 payload、checkpoint、业务关系、操作的 execution_context**。
不能只 mock 掉 WorkingContext 后断言新增表里有数据（方案 §11.3）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any, cast
from uuid import UUID

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import Field
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.command_guard import CommandGuardMiddleware
from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
from agent_workbench.agent.middleware.finish_gate import FinishGateMiddleware
from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware
from agent_workbench.agent.middleware.working_context import WorkingContextMiddleware
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.domain.findings import FindingService
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.observability.spans import SpanRecorder
from agent_workbench.persistence.tables import FindingRow, LeadRow, LeadWeaknessLinkRow
from agent_workbench.tools.completion import propose_completion
from agent_workbench.tools.execution_context import execution_context
from agent_workbench.tools.task import record_lead, record_weakness, update_plan
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
    return "\n".join(str(message.content) for message in messages if isinstance(message, SystemMessage))


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
def probe(where: str) -> str:
    """只读探测（记录派发时的执行关联，不产生远端副作用）。"""
    PROBE_CALLS.append(dict(execution_context.get() or {}))
    return f"probe:{where}"


def _middleware() -> list[AgentMiddleware[Any, Any]]:
    return [
        CommandGuardMiddleware(),
        WorkingContextMiddleware(),
        ContextBuilderMiddleware(profile()),
        ToolDispatchMiddleware(),
        FinishGateMiddleware(),
    ]


async def test_official_loop_consumes_weakness_and_links_execution(database, unique_name):
    context = await context_for(database, unique_name)
    _prepare_context(context, database)
    PROBE_CALLS.clear()
    seen: dict[str, str] = {}
    presented: list[str] = []

    def step_weakness(messages: list[BaseMessage]) -> AIMessage:
        return _call(
            "record_weakness",
            {
                "title": "错误页回显内部路径",
                "description": "不存在路径返回带绝对路径的堆栈",
                "weakness_kind": "enabling_condition",
                "provided_conditions_json": [{"statement": "错误页包含绝对路径信息"}],
            },
            "weak-1",
        )

    def step_lead(messages: list[BaseMessage]) -> AIMessage:
        payload = _last_tool_payload(messages)["weakness"]
        seen["weakness_id"] = payload["weakness_id"]
        seen["condition_key"] = payload["condition_keys"][0]
        seen["revision"] = str(payload["revision"])
        # 关键断言：**下一次模型请求**里真的带上了这个弱点。
        text = _system_text(messages)
        assert payload["weakness_id"] in text, "新弱点必须进入下一次决策的上下文"
        assert "错误页回显内部路径" in text
        presented.append(payload["weakness_id"])
        # 呈现记录来自**本次请求**的 manifest，而不是数据库里有记录。
        manifest = context.toolbox.context_manifest or {}
        assert manifest.get("presented_weakness_ids") == [payload["weakness_id"]]
        assert manifest["working_context"]["weakness_consumption"]["changed_weaknesses"]
        return _call(
            "record_lead",
            {
                "hypothesis": "借错误页路径做目录穿越",
                "next_action": "用 ../../ 探测静态目录",
                "source_weaknesses_json": [
                    {
                        "weakness_id": payload["weakness_id"],
                        "condition_key": payload["condition_keys"][0],
                        "source_weakness_revision": payload["revision"],
                    }
                ],
                "actionability_json": {"state": "ready", "note": "下一步明确"},
            },
            "lead-1",
        )

    def step_plan(messages: list[BaseMessage]) -> AIMessage:
        payload = _last_tool_payload(messages)["lead"]
        seen["lead_id"] = payload["lead_id"]
        return _call(
            "update_plan",
            {
                "steps_json": json.dumps(
                    [
                        {
                            "step_id": "s1",
                            "description": "沿错误页路径推进",
                            "status": "in_progress",
                            "kind": "followup",
                            "lead_ids": [payload["lead_id"]],
                        },
                        {
                            "step_id": "s2",
                            "description": "补检未覆盖入口",
                            "status": "pending",
                            "kind": "coverage",
                        },
                    ],
                    ensure_ascii=False,
                ),
                "rationale": "链条建立",
            },
            "plan-1",
        )

    def step_probe(messages: list[BaseMessage]) -> AIMessage:
        # 新弱点与依赖其 ID 的线索放在不同批次之后，这里执行真实的下一步动作。
        return _call("probe", {"where": "/static"}, "probe-1")

    def step_review(messages: list[BaseMessage]) -> AIMessage:
        return _call(
            "record_weakness",
            {
                "weakness_id": seen["weakness_id"],
                "expected_revision": int(seen["revision"]),
                "connection_review_json": {"linked_lead_ids": [seen["lead_id"]]},
            },
            "weak-review",
        )

    def step_finish(messages: list[BaseMessage]) -> AIMessage:
        return AIMessage(content="链条已建立并推进")

    model = SteppedModel(
        steps=[step_weakness, step_lead, step_plan, step_probe, step_review, step_finish]
    )
    graph = create_agent(
        model=model,
        tools=[record_weakness, record_lead, update_plan, probe, propose_completion],
        middleware=_middleware(),
        state_schema=AgentGraphState,
        context_schema=RunContext,
        name="closure_fixture",
    )
    result = await graph.ainvoke(
        cast(Any, {"messages": [HumanMessage(content="本地闭环验证", id="user-1")]}),
        {"recursion_limit": 50},
        context=context,
    )

    # 1) 业务关系真的建立，且 `result` 关系为空时不会伪造组合。
    async with database.session() as db:
        links = (
            (
                await db.execute(
                    select(LeadWeaknessLinkRow).where(
                        LeadWeaknessLinkRow.session_id == context.toolbox.session_id
                    )
                )
            )
            .scalars()
            .all()
        )
        weakness = await FindingService(db).get(UUID(seen["weakness_id"]))
    assert [link.role for link in links] == ["basis"]
    assert str(links[0].lead_id) == seen["lead_id"]
    assert weakness is not None and weakness.status == "candidate"
    assert weakness.connection_review, "用途判断必须写进业务表"

    # 2) checkpoint：计划带多线索与类型，且呈现记录已提交。
    if "plan" not in result:
        for message in result["messages"]:
            print(type(message).__name__, str(message.content)[:300])
    assert "plan" in result, sorted(result.keys())
    plan = result["plan"]
    assert plan[0]["lead_ids"] == [seen["lead_id"]] and plan[0]["kind"] == "followup"
    assert result["presented_weakness_ids"] == presented
    assert result["last_weakness_revisions"][seen["weakness_id"]] >= 1

    # 3) 真实操作的 execution_context 指向该步骤与该线索。
    assert PROBE_CALLS, "至少有一次真实工具派发"
    assert PROBE_CALLS[0]["lead_ids"] == [seen["lead_id"]]
    assert PROBE_CALLS[0]["step_kind"] == "followup"
    assert PROBE_CALLS[0]["step_id"] == "s1"


async def test_official_loop_never_consuming_weakness_yields_specific_review(database, unique_name):
    """模型只存不消费时，自主结束必须收到具体复查反馈，而不是静默结束。"""
    context = await context_for(database, unique_name)
    _prepare_context(context, database)
    seen: dict[str, str] = {}

    def step_weakness(messages: list[BaseMessage]) -> AIMessage:
        return _call(
            "record_weakness",
            {"title": "没人管的弱点", "description": "存下来就结束"},
            "weak-1",
        )

    def step_finish(messages: list[BaseMessage]) -> AIMessage:
        seen["weakness_id"] = _last_tool_payload(messages)["weakness"]["weakness_id"]
        return _call(
            "propose_completion",
            {
                "reason": "goal_answered",
                "goal_answer": "已完成",
                "stop_rationale": "没有更多方向",
            },
            "proposal-1",
        )

    def step_after_feedback(messages: list[BaseMessage]) -> AIMessage:
        text = _system_text(messages)
        assert seen["weakness_id"] in text, "复查反馈必须点名具体对象"
        return AIMessage(content="按反馈继续")

    model = SteppedModel(steps=[step_weakness, step_finish, step_after_feedback])
    graph = create_agent(
        model=model,
        tools=[record_weakness, propose_completion],
        middleware=_middleware(),
        state_schema=AgentGraphState,
        context_schema=RunContext,
        name="closure_review_fixture",
    )
    result = await graph.ainvoke(
        cast(Any, {"messages": [HumanMessage(content="本地结束判定验证", id="user-1")]}),
        {"recursion_limit": 50},
        context=context,
    )
    assert result.get("completion_proposal") is None, "未通过检查的提议必须被清除"
    assert result["finish_gate_feedback"], "必须给出具体反馈"
    assert any("尚未复查" in reason for reason in result["finish_gate_feedback"])
    assert result["finish_gate_attempts"] >= 1


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
            (await db.execute(select(FindingRow).where(FindingRow.session_id == context.toolbox.session_id)))
            .scalars()
            .all()
        )
        leads = (await db.execute(select(LeadRow).where(LeadRow.session_id == context.toolbox.session_id))).scalars().all()
    assert len(rows) == 4 and sorted(str(row.id) for row in rows) == sorted(ids)
    assert leads == []
