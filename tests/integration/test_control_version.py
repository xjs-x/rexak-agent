"""方向变更必须可靠拦截旧模型响应（BUILD_PLAN §4.3）。

这个文件针对一个**真实缺陷**：早期实现里 `ToolDispatchMiddleware` 在工具参数没有
版本信息时，会退化成"读数据库当前值再和当前值比"，永远相等 —— 于是用户在模型
调用期间改变方向，旧响应里的工具仍然会被派发。

正确语义：
1. 模型请求**发出之前**把 `run.control_version` 固定下来（`CommandGuardMiddleware`）；
2. 工具派发前用这个**固定的**版本与数据库当前值比较；
3. 不一致 => 该工具调用**未执行**，且留下事件与可读理由，用量仍记录。

测试方式：用一个会在 `_generate()` 期间真的修改数据库 `control_version` 的脚本模型，
模拟"用户在模型思考期间改变方向"。这是真实的并发时序，不是手动抛异常。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from sqlalchemy import create_engine, select, text

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.command_guard import CommandGuardMiddleware
from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.contracts.enums import RunStatus, ToolEffectClass
from agent_workbench.contracts.identity import PolicySnapshot, ScopeRule, ScopeSnapshot
from agent_workbench.domain.run_state import claim_run, create_run, set_run_status
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import EventRow, RunRow, ToolOperationRow
from agent_workbench.tools.remote import prepare_operation
from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call

pytestmark = pytest.mark.integration

#: 被"侦察"的工具记录自己是否真的被执行过
_SPY_CALLS: list[str] = []


@tool("spy_tool")
def spy_tool(marker: str) -> str:
    """记录调用并返回成功（无外部副作用）。

    Args:
        marker: 标记
    """
    _SPY_CALLS.append(marker)
    return f"executed:{marker}"


class VersionBumpingChatModel(ScriptedChatModel):
    """在 `_generate()` 期间把 run 的 control_version +1 的脚本模型。

    用**同步** SQLAlchemy 引擎写库，保证时序确定：版本变化发生在
    "模型请求已发出、响应尚未返回"的窗口内 —— 正是要防的那种竞态。
    """

    bump_sync_url: str = ""
    bump_run_id: str = ""
    bump_enabled: bool = True

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
        if self.bump_enabled and self.bump_sync_url and self.bump_run_id:
            self.bump_enabled = False
            engine = create_engine(self.bump_sync_url, future=True)
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(
                            "UPDATE app.runs SET control_version = control_version + 1 "
                            "WHERE id = :rid"
                        ),
                        {"rid": self.bump_run_id},
                    )
            finally:
                engine.dispose()
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


async def _make_run(database, name: str):
    scope = ScopeSnapshot(revision=1, rules=[ScopeRule(scheme="http", host="127.0.0.1", port=8899)])
    async with database.transaction() as session:
        project = await ProjectRepository(session).create(
            owner_id="cv-owner",
            name=name,
            target_description="控制版本测试",
            default_scope=scope.model_dump(),
        )
        session_row = await SessionRepository(session).create(
            project_id=project.id,
            title="控制版本",
            scope_snapshot=scope.model_dump(),
            policy_snapshot=PolicySnapshot().model_dump(),
        )
        run = await create_run(
            session,
            session_id=session_row.id,
            objective="x",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            config_snapshot={},
        )
        await set_run_status(session, run_id=run.id, target=RunStatus.RUNNING)
    async with database.transaction() as session:
        acquired, generation = await claim_run(
            session, run_id=run.id, owner="cv-runner", lease_seconds=120
        )
    assert acquired
    return session_row, run, generation, scope


async def _make_toolbox(database, run, session_row, generation, scope):
    from agent_workbench.config.settings import get_settings
    from agent_workbench.evidence.artifacts import ArtifactStore
    from agent_workbench.observability.spans import SpanRecorder
    from agent_workbench.tools.runtime import ResourceLocks, Toolbox

    settings = get_settings()
    return Toolbox(
        run_id=run.id,
        session_id=session_row.id,
        owner="cv-runner",
        owner_generation=generation,
        settings=settings,
        db=database,
        scope=ScopePolicy(scope),
        # 本用例不走远端执行路径（工具被拦下或只是侦察工具），executor 用替身占位
        executor=None,  # type: ignore[arg-type]
        artifacts=ArtifactStore(settings),
        spans=SpanRecorder(),
        locks=ResourceLocks(settings),
        sdk_retries_disabled=True,
    )


def _sync_url(database) -> str:
    """同步引擎 URL。

    SQLAlchemy 的 `psycopg` 方言同时支持同步与异步：用 `create_engine` 建就是同步
    （psycopg3 同步连接）。这里刻意用同步写库，让"版本变化发生在模型生成期间"
    这个时序完全确定，不受事件循环调度影响。
    """
    return database.settings.database_url


async def _run_agent(*, model: ScriptedChatModel, toolbox: Any, thread_id: str):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from agent_workbench.persistence.bootstrap import open_checkpointer_connection

    settings = toolbox.settings
    async with open_checkpointer_connection(settings) as conn:
        agent = create_agent(
            model=model,
            tools=[spy_tool],
            middleware=[CommandGuardMiddleware(), ToolDispatchMiddleware()],
            state_schema=AgentGraphState,
            context_schema=RunContext,
            checkpointer=AsyncPostgresSaver(conn),
        )
        context = RunContext(toolbox=toolbox, thread_id=thread_id)
        graph_input: Any = {"messages": [HumanMessage(content="调用 spy_tool")]}
        return await agent.ainvoke(  # type: ignore[arg-type]
            graph_input,
            {"configurable": {"thread_id": thread_id}},
            context=context,
        )


async def test_new_tool_operation_records_dispatch_version(database, unique_name) -> None:
    session_row, run, generation, scope = await _make_run(database, unique_name)
    toolbox = await _make_toolbox(database, run, session_row, generation, scope)
    toolbox.control_version = 0
    operation_id, replayed = await prepare_operation(
        toolbox,
        "call-version-pin",
        "http_request",
        {"url": "http://127.0.0.1:8899/"},
        effect_class=ToolEffectClass.READ_ONLY,
    )
    assert not replayed
    async with database.session() as session:
        row = (
            await session.execute(
                select(ToolOperationRow).where(ToolOperationRow.operation_id == operation_id)
            )
        ).scalar_one()
    assert row.dispatch_control_version == 0


async def test_normal_turn_pins_version_and_executes_tool(database, unique_name) -> None:
    """正常情况：轮次开始固定版本，工具正常执行，pin 与 DB 一致。"""
    _SPY_CALLS.clear()
    session_row, run, generation, scope = await _make_run(database, unique_name)
    toolbox = await _make_toolbox(database, run, session_row, generation, scope)

    model = ScriptedChatModel(
        responses=[
            tool_call("spy_tool", {"marker": "ok"}, "call_ok"),
            final_text("完成"),
        ]
    )
    result = await _run_agent(model=model, toolbox=toolbox, thread_id=f"cv-{uuid.uuid4().hex}")

    assert _SPY_CALLS == ["ok"], "正常轮次工具必须被执行"
    assert result["dispatch_control_version"] == toolbox.control_version == 0

    tool_messages = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert tool_messages and "executed:ok" in str(tool_messages[0].content)


async def test_user_direction_change_during_model_call_blocks_tool(database, unique_name) -> None:
    """**核心用例**：用户在模型调用期间改变方向 -> 旧响应的工具不得执行。

    这是真实并发时序，不是手动抛异常：
    `VersionBumpingChatModel._generate()` 在模型"思考"期间真的修改数据库版本，
    等价于用户点了"改变方向"。
    """
    _SPY_CALLS.clear()
    session_row, run, generation, scope = await _make_run(database, unique_name)
    toolbox = await _make_toolbox(database, run, session_row, generation, scope)

    model = VersionBumpingChatModel(
        responses=[
            tool_call("spy_tool", {"marker": "stale"}, "call_stale"),
            final_text("完成"),
        ],
        bump_sync_url=_sync_url(database),
        bump_run_id=str(run.id),
    )
    result = await _run_agent(model=model, toolbox=toolbox, thread_id=f"cv-{uuid.uuid4().hex}")

    assert _SPY_CALLS == [], "方向变更后，旧响应里的工具**不得**被执行"
    assert model.bump_enabled is False, "版本变更确实发生过（否则本用例没有验证意义）"

    tool_messages = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert tool_messages, "被拦下的 tool_call 必须形成合法的终结 ToolMessage（闭合消息组）"
    content = str(tool_messages[0].content)
    assert "未执行" in content
    assert "过期" in content

    # 拦截依据是"派发那一刻固定值(0) != 数据库当前值(1)"。
    # 注意：最终状态里的 dispatch_control_version 会是 **1**，因为工具结果之后还会
    # 有第二个模型轮次，那一轮会重新固定到最新版本 —— 这是期望行为。
    # 真正要核对的是**事件里记录的**那次派发用的固定值。
    assert result["dispatch_control_version"] == 1, "后续轮次会重新固定到最新版本"
    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
    assert int(row.control_version) == 1

    # 必须留下事件，便于 UI 与诊断复盘
    async with database.session() as session:
        events = (
            (await session.execute(select(EventRow).where(EventRow.session_id == session_row.id)))
            .scalars()
            .all()
        )
    stale_events = [
        item for item in events if item.payload.get("scope") == "stale_response_blocked"
    ]
    assert stale_events, "拦下过期响应必须写入事件"
    assert stale_events[0].payload["pinned_control_version"] == 0
    assert stale_events[0].payload["current_control_version"] == 1


async def test_stale_block_creates_no_tool_operation(database, unique_name) -> None:
    """被拦下的工具调用**不能**留下 operation 记录（否则等于"记录了一次没发生的操作"）。"""
    _SPY_CALLS.clear()
    session_row, run, generation, scope = await _make_run(database, unique_name)
    toolbox = await _make_toolbox(database, run, session_row, generation, scope)

    model = VersionBumpingChatModel(
        responses=[
            tool_call("spy_tool", {"marker": "stale2"}, "call_stale2"),
            final_text("完成"),
        ],
        bump_sync_url=_sync_url(database),
        bump_run_id=str(run.id),
    )
    await _run_agent(model=model, toolbox=toolbox, thread_id=f"cv-{uuid.uuid4().hex}")

    async with database.session() as session:
        operations = (
            (
                await session.execute(
                    select(ToolOperationRow).where(ToolOperationRow.run_id == run.id)
                )
            )
            .scalars()
            .all()
        )
    assert operations == [], "被拦下的工具调用不应产生 tool_operations 记录"


async def test_missing_pin_fails_closed(database, unique_name) -> None:
    """状态里没有固定版本时**必须拒绝派发**（fail-closed），而不是退化成放行。

    早期实现正是在这里退化成"用当前值比当前值"，导致拦截失效。
    """
    _SPY_CALLS.clear()
    session_row, run, generation, scope = await _make_run(database, unique_name)
    toolbox = await _make_toolbox(database, run, session_row, generation, scope)

    from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware as M

    class _Runtime:
        def __init__(self, context: RunContext) -> None:
            self.context = context

    class _Request:
        def __init__(self) -> None:
            self.runtime = _Runtime(RunContext(toolbox=toolbox, thread_id="t"))
            self.tool_call = {"name": "spy_tool", "args": {"marker": "nopin"}, "id": "c1"}
            # 刻意**不带** dispatch_control_version
            self.state: dict[str, Any] = {}

    async def handler(_request: Any) -> Any:  # pragma: no cover
        raise AssertionError("缺少版本固定信息时不得派发工具")

    result = await M().awrap_tool_call(_Request(), handler)  # type: ignore[arg-type]
    # 中间件可能返回 ToolMessage 或 Command；被拦下时必然是 ToolMessage
    content = str(getattr(result, "content", ""))
    assert _SPY_CALLS == []
    assert "未执行" in content
    assert "pinning" in content or "无法确认" in content


async def test_toolbox_control_check_compares_pinned_not_current(database, unique_name) -> None:
    """`Toolbox.assert_control_current()` 也必须比较**固定值**，而不是读当前值自比。"""
    from agent_workbench.agent.signals import StaleResponseError
    from agent_workbench.runtime.runner import AgentRunner

    session_row, run, generation, scope = await _make_run(database, unique_name)
    toolbox = await _make_toolbox(database, run, session_row, generation, scope)

    runner = AgentRunner(owner="cv-runner", database=database)
    toolbox.control_check = runner._assert_dispatch_allowed

    # 固定版本 = 当前版本 -> 通过
    toolbox.control_version = 0
    await toolbox.assert_control_current()

    # 用户改变方向
    async with database.transaction() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
        row.control_version = 5

    with pytest.raises(StaleResponseError) as excinfo:
        await toolbox.assert_control_current()
    assert excinfo.value.pinned_version == 0
    assert excinfo.value.current_version == 5
