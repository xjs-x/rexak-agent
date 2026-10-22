"""schema 1/2/3 到 4 的显式迁移：保留消息、计划与待执行节点，历史操作不重放。"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import pytest
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from agent_workbench.agent.state import (
    CURRENT_STATE_SCHEMA_VERSION,
    AgentGraphState,
    StateSchemaIncompatibleError,
)
from agent_workbench.agent.state_migration import (
    adapt_v2,
    adapt_v3,
    ensure_current_checkpoint,
    needs_migration,
)
from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call

pytestmark = pytest.mark.integration


def test_needs_migration_only_for_older_supported_versions() -> None:
    assert needs_migration({"state_schema_version": 1})
    assert needs_migration({"state_schema_version": 2})
    assert needs_migration({"state_schema_version": 3})
    assert not needs_migration({})
    assert needs_migration({"state_schema_version": 4})
    assert needs_migration({"state_schema_version": 5})
    assert needs_migration({"state_schema_version": 6})
    assert not needs_migration({"state_schema_version": 7})


def test_adapt_v2_only_accepts_explicit_v2() -> None:
    with pytest.raises(StateSchemaIncompatibleError):
        adapt_v2({"state_schema_version": 3}, checkpoint_id="x", next_nodes=())


def test_adapt_v3_only_accepts_explicit_v3() -> None:
    with pytest.raises(StateSchemaIncompatibleError):
        adapt_v3({"state_schema_version": 4}, checkpoint_id="x", next_nodes=())


def test_adapt_v3_maps_lead_id_and_clears_weakness_progress() -> None:
    update = adapt_v3(
        {
            "state_schema_version": 3,
            "plan": [
                {
                    "step_id": "s1",
                    "description": "旧步骤",
                    "status": "in_progress",
                    "lead_id": "L1",
                },
                {"step_id": "s2", "description": "无线索", "status": "pending"},
            ],
        },
        checkpoint_id="cp-3",
        next_nodes=("tools",),
    )
    assert update["state_schema_version"] == 4
    assert update["plan"][0]["lead_ids"] == ["L1"]
    assert update["plan"][0]["lead_id"] == "L1"
    assert update["plan"][1]["lead_ids"] == []
    # 不回写虚构的步骤类型与关系
    assert "kind" not in update["plan"][0]
    assert update["presented_weakness_ids"] == []
    assert update["last_weakness_revisions"] == {}
    assert update["state_migration"]["from"] == 3 and update["state_migration"]["to"] == 4


def test_adapt_v2_marks_existing_focus_explicit_and_adds_assessment_slot() -> None:
    focus = {
        "step_id": "s1",
        "lead_id": None,
        "intent": "核对条件",
        "reason": "模型选择",
        "return_to_step_id": None,
    }
    update = adapt_v2(
        {"state_schema_version": 2, "current_focus": focus}, checkpoint_id="cp-1", next_nodes=()
    )
    assert update["state_schema_version"] == 3
    assert update["current_focus"]["source"] == "explicit"
    assert update["work_assessment"] is None
    assert update["state_migration"]["from"] == 2 and update["state_migration"]["to"] == 3


def _graph(checkpointer: Any, *, interrupt_before: list[str] | None = None) -> Any:
    submitted: list[str] = []

    @tool
    def local_submit(key: str) -> str:
        """只记录本地提交。"""
        submitted.append(key)
        return key

    model = ScriptedChatModel(
        responses=[tool_call("local_submit", {"key": "once"}, "call-once"), final_text("完成")]
    )
    graph = create_agent(
        model=model,
        tools=[local_submit],
        state_schema=AgentGraphState,
        checkpointer=checkpointer,
        interrupt_before=interrupt_before,
    )
    return graph, submitted


async def test_v2_checkpoint_is_migrated_without_touching_messages_or_plan() -> None:
    graph, _submitted = _graph(InMemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    focus = {
        "step_id": "s1",
        "lead_id": None,
        "intent": "核对条件",
        "reason": "模型选择",
        "return_to_step_id": None,
    }
    plan = [{"step_id": "s1", "description": "保留", "status": "in_progress"}]
    await graph.aupdate_state(
        cast(Any, config),
        cast(
            Any,
            {
                "state_schema_version": 2,
                "messages": [HumanMessage(content="原目标", id="user-original")],
                "plan": plan,
                "current_focus": focus,
            },
        ),
    )
    before = await graph.aget_state(cast(Any, config))
    migrated = await ensure_current_checkpoint(
        graph, config, before, database=None, session_id=None
    )
    assert migrated.values["state_schema_version"] == CURRENT_STATE_SCHEMA_VERSION
    migrated_plan = migrated.values["plan"]
    assert [step["step_id"] for step in migrated_plan] == ["s1"]
    for key in ("description", "status"):
        assert migrated_plan[0][key] == plan[0][key], "迁移不得改写步骤原始字段"
    assert migrated_plan[0]["lead_ids"] == []
    assert migrated.values["current_focus"]["source"] == "explicit"
    assert migrated.values["work_assessment"] is None
    assert [m.id for m in migrated.values["messages"]] == [m.id for m in before.values["messages"]]
    assert tuple(migrated.next) == tuple(before.next)


async def test_pending_v2_nodes_are_preserved_and_not_replayed() -> None:
    graph, submitted = _graph(InMemorySaver(), interrupt_before=["tools"])
    config = {"configurable": {"thread_id": str(uuid4())}}
    await graph.ainvoke(
        cast(Any, {"messages": [HumanMessage(content="原目标", id="user-original")]}),
        cast(Any, config),
    )
    await graph.aupdate_state(cast(Any, config), cast(Any, {"state_schema_version": 2}))
    before = await graph.aget_state(cast(Any, config))
    assert tuple(before.next), "前置条件：checkpoint 仍有待执行节点"
    migrated = await ensure_current_checkpoint(
        graph, config, before, database=None, session_id=None
    )
    assert tuple(migrated.next) == tuple(before.next)
    assert submitted == [], "迁移不得执行任何工具"
    await graph.ainvoke(None, cast(Any, config))
    assert submitted == ["once"], "迁移后继续运行时工具只执行一次"


async def test_v3_checkpoint_with_pending_node_keeps_it_after_migration() -> None:
    """真实暂停会话会停在"还有待执行节点"的边界，迁移必须原样保留。"""
    graph, submitted = _graph(InMemorySaver(), interrupt_before=["model"])
    config = {"configurable": {"thread_id": str(uuid4())}}
    await graph.ainvoke(
        cast(
            Any,
            {
                "messages": [HumanMessage(content="原目标", id="user-original")],
                "state_schema_version": 3,
                "plan": [{"step_id": "s1", "description": "保留", "status": "in_progress"}],
            },
        ),
        cast(Any, config),
    )
    before = await graph.aget_state(cast(Any, config))
    assert tuple(before.next) == ("model",), "前置条件：停在待执行的 model 节点前"
    migrated = await ensure_current_checkpoint(
        graph, config, before, database=None, session_id=None
    )
    assert migrated.values["state_schema_version"] == CURRENT_STATE_SCHEMA_VERSION
    assert tuple(migrated.next) == tuple(before.next), "迁移不得吞掉待执行节点"
    assert [t.name for t in (migrated.tasks or ())] == [t.name for t in (before.tasks or ())], (
        "待执行任务集合也必须一致"
    )
    assert [m.id for m in migrated.values["messages"]] == [m.id for m in before.values["messages"]]
    assert [step["step_id"] for step in migrated.values["plan"]] == ["s1"]
    assert migrated.values["plan"][0]["lead_ids"] == []
    assert submitted == [], "迁移不得执行任何工具"


async def test_pure_adapters_reach_v4_before_skill_migration() -> None:
    """1 -> 2 -> 3 -> 4 必须顺序执行，不允许跳过。"""

    # v1 迁移需要业务库投影（active_lead_ids），因此这里只校验纯函数层：
    v2_update = adapt_v2(
        {"state_schema_version": 2, "current_focus": None}, checkpoint_id="cp", next_nodes=()
    )
    assert v2_update["state_schema_version"] == 3
    assert v2_update["current_focus"] is None
    v3_update = adapt_v3(v2_update, checkpoint_id="cp-2", next_nodes=())
    assert v3_update["state_schema_version"] == 4
    assert v3_update["state_migration"]["from"] == 3


async def test_v4_skill_migration_preserves_pending_tool_and_runs_once() -> None:
    graph, submitted = _graph(InMemorySaver(), interrupt_before=["tools"])
    config = {"configurable": {"thread_id": str(uuid4())}}
    await graph.ainvoke(
        cast(
            Any,
            {
                "messages": [HumanMessage(content="保留目标", id="original")],
                "state_schema_version": 4,
            },
        ),
        cast(Any, config),
    )
    before = await graph.aget_state(cast(Any, config))
    assert submitted == [] and before.next == ("tools",)
    after = await ensure_current_checkpoint(graph, config, before, database=None, session_id=None)
    assert after.values["state_schema_version"] == CURRENT_STATE_SCHEMA_VERSION
    assert after.values["active_skills"] == {} and after.values["skill_directives"] == {}
    assert [(m.id, m.content) for m in after.values["messages"]] == [
        (m.id, m.content) for m in before.values["messages"]
    ]
    assert after.next == before.next and submitted == []
    await graph.ainvoke(None, cast(Any, config))
    assert submitted == ["once"]


async def test_v5_planning_migration_preserves_pending_work_and_marks_old_done() -> None:
    graph, submitted = _graph(InMemorySaver(), interrupt_before=["tools"])
    config = {"configurable": {"thread_id": str(uuid4())}}
    await graph.ainvoke(
        cast(
            Any,
            {
                "messages": [HumanMessage(content="保留旧工作", id="v5-original")],
                "state_schema_version": 5,
                "plan": [{"step_id": "old", "description": "历史完成", "status": "done"}],
            },
        ),
        cast(Any, config),
    )
    before = await graph.aget_state(cast(Any, config))
    after = await ensure_current_checkpoint(graph, config, before, database=None, session_id=None)
    assert after.next == before.next == ("tools",)
    assert after.values["state_schema_version"] == CURRENT_STATE_SCHEMA_VERSION
    assert after.values["plan"][0]["legacy_completion"]
    assert "completion" not in after.values["plan"][0]
    assert submitted == []
    await graph.ainvoke(None, cast(Any, config))
    assert submitted == ["once"]


async def test_v6_checkpoint_retains_legacy_channels_and_pending_operation() -> None:
    graph, submitted = _graph(InMemorySaver(), interrupt_before=["tools"])
    config = {"configurable": {"thread_id": str(uuid4())}}
    legacy = {
        "work_assessment": {"revision": 4, "decision": "replan"},
        "progress_feedback": "历史提醒",
        "coverage_revision": 8,
        "plan": [
            {
                "step_id": "old",
                "description": "旧计划",
                "status": "done",
                "completion_spec": {"mode": "investigate"},
                "completion": {"summary": "旧记录"},
            }
        ],
    }
    await graph.ainvoke(
        cast(
            Any,
            {
                "messages": [HumanMessage(content="原目标", id="original")],
                "state_schema_version": 6,
                **legacy,
            },
        ),
        cast(Any, config),
    )
    before = await graph.aget_state(cast(Any, config))
    after = await ensure_current_checkpoint(graph, config, before, database=None, session_id=None)
    assert after.values["state_schema_version"] == 7 and after.next == before.next == ("tools",)
    for key, value in legacy.items():
        assert after.values[key] == value
    assert submitted == []
    await graph.ainvoke(None, cast(Any, config))
    assert submitted == ["once"]


async def test_old_protocol_node_restores_as_inert_bridge():
    from agent_workbench.agent.middleware.progress import ProgressMiddleware
    from agent_workbench.agent.middleware.stage_report import StageReportMiddleware

    saver = InMemorySaver()
    model = ScriptedChatModel(responses=[final_text("完成")])
    config = {"configurable": {"thread_id": str(uuid4())}}
    old = create_agent(
        model=model,
        tools=[],
        state_schema=AgentGraphState,
        checkpointer=saver,
        middleware=[ProgressMiddleware(enabled=True), StageReportMiddleware(enabled=True)],
        interrupt_before=["ProgressMiddleware.before_model"],
    )
    await old.ainvoke(
        cast(
            Any,
            {
                "state_schema_version": 6,
                "messages": [HumanMessage(content="保留旧节点", id="original")],
            },
        ),
        cast(Any, config),
    )
    new = create_agent(
        model=model,
        tools=[],
        state_schema=AgentGraphState,
        checkpointer=saver,
        middleware=[ProgressMiddleware(enabled=False), StageReportMiddleware(enabled=False)],
    )
    before = await new.aget_state(cast(Any, config))
    assert before.next == ("ProgressMiddleware.before_model",)
    migrated = await ensure_current_checkpoint(new, config, before, database=None, session_id=None)
    assert migrated.next == before.next
    result = await new.ainvoke(None, cast(Any, config))
    assert result["messages"][-1].content == "完成"
    assert "progress_feedback" not in result and "report_retry_key" not in result
