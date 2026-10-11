"""工具派发守卫：异常分类、暂停/取消闭合、控制版本复核、能力开关。

这些是 BUILD_PLAN §4.3 与 §16.3 的落地检查点。工具异常**必须**变成
合法的终结 `ToolMessage`，否则"某个工具失败"会被升级成"整个 run 失败"。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.contracts.identity import PolicySnapshot, ScopeRule, ScopeSnapshot
from agent_workbench.domain.run_state import (
    bump_control_version,
    claim_run,
    create_run,
    set_run_status,
)
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import EventRow

pytestmark = pytest.mark.integration


@dataclass
class _FakeRuntime:
    context: RunContext


class _FakeRequest:
    """最小工具调用请求替身：只含中间件真正读取的字段。

    `state` 必须包含 `dispatch_control_version` —— 真实运行时由
    `CommandGuardMiddleware.abefore_model` 在模型请求前写入。缺失时中间件会
    fail-closed（见 `tests/integration/test_control_version.py`），
    这不是可选的装饰字段。
    """

    def __init__(
        self,
        runtime: Any,
        name: str,
        args: dict[str, Any],
        tool_call_id: str,
        *,
        dispatch_control_version: int | None = 0,
    ) -> None:
        self.runtime = runtime
        self.tool_call = {"name": name, "args": args, "id": tool_call_id}
        self.state: dict[str, Any] = {}
        if dispatch_control_version is not None:
            self.state["dispatch_control_version"] = dispatch_control_version


async def _build_context(database, name: str, *, executor_stub: Any = None) -> RunContext:
    """构造一个真实 DB 支撑的 RunContext（Executor 用替身，不联网）。"""
    from agent_workbench.config.settings import get_settings
    from agent_workbench.evidence.artifacts import ArtifactStore
    from agent_workbench.observability.spans import SpanRecorder
    from agent_workbench.tools.runtime import ResourceLocks, Toolbox

    settings = get_settings()
    scope = ScopeSnapshot(revision=1, rules=[ScopeRule(scheme="http", host="127.0.0.1", port=8899)])
    async with database.transaction() as session:
        project = await ProjectRepository(session).create(
            owner_id="dispatch-owner",
            name=name,
            target_description="派发守卫测试",
            default_scope=scope.model_dump(),
        )
        session_row = await SessionRepository(session).create(
            project_id=project.id,
            title="派发守卫",
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
            session, run_id=run.id, owner="dispatch-runner", lease_seconds=60
        )
    assert acquired

    toolbox = Toolbox(
        run_id=run.id,
        session_id=session_row.id,
        owner="dispatch-runner",
        owner_generation=generation,
        settings=settings,
        db=database,
        scope=ScopePolicy(scope),
        executor=executor_stub,
        artifacts=ArtifactStore(settings),
        spans=SpanRecorder(),
        locks=ResourceLocks(settings),
        sdk_retries_disabled=True,
    )
    return RunContext(toolbox=toolbox, thread_id=session_row.thread_id)


async def _run_middleware(
    context: RunContext,
    handler: Any,
    name: str = "http_request",
    *,
    pinned_version: int | None = 0,
) -> Any:
    middleware = ToolDispatchMiddleware()
    request = _FakeRequest(
        _FakeRuntime(context),
        name,
        {},
        f"call-{uuid.uuid4().hex[:8]}",
        dispatch_control_version=pinned_version,
    )
    # 这是刻意的协议替身：只实现中间件真正读取的字段（tool_call / runtime / state）。
    # 用真实 ToolCallRequest 需要构造完整图运行时，对"异常分类"这个被测点没有增益。
    return await middleware.awrap_tool_call(request, handler)  # type: ignore[arg-type]


async def test_value_error_becomes_structured_failure(database, unique_name) -> None:
    """参数/校验错误 -> validation_failed 且 retryable=False。"""
    context = await _build_context(database, unique_name)

    async def handler(_request: Any) -> Any:
        raise ValueError("body 与 json_body 不能同时提供")

    result = await _run_middleware(context, handler)
    payload = json.loads(result.content)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "validation_failed"
    assert payload["error"]["retryable"] is False
    assert result.tool_call_id.startswith("call-")


async def test_timeout_becomes_retryable_failure(database, unique_name) -> None:
    context = await _build_context(database, unique_name)

    async def handler(_request: Any) -> Any:
        raise TimeoutError("执行岛响应超时")

    payload = json.loads((await _run_middleware(context, handler)).content)
    assert payload["error"]["code"] == "timeout"
    assert payload["error"]["retryable"] is True


async def test_result_arriving_after_direction_change_is_not_injected(
    database, unique_name
) -> None:
    context = await _build_context(database, unique_name)
    executed: list[str] = []

    async def handler(_request: Any) -> Any:
        executed.append("remote-effect-may-have-happened")
        async with database.transaction() as session:
            await bump_control_version(session, run_id=context.toolbox.run_id)
        return ToolMessage(
            content="旧方向的原始结果",
            tool_call_id="old-call",
            name="http_request",
        )

    result = await _run_middleware(context, handler)
    assert executed == ["remote-effect-may-have-happened"]
    assert "旧方向的原始结果" not in result.content
    assert "远端副作用可能已发生" in result.content
    assert "未注入当前决策" in result.content
    async with database.session() as session:
        events = (
            await session.execute(select(EventRow).where(EventRow.session_id == context.toolbox.session_id))
        ).scalars().all()
    assert any(item.payload.get("scope") == "stale_tool_result" for item in events)


async def test_database_loss_after_tool_result_is_uncertain(database, unique_name) -> None:
    context = await _build_context(database, unique_name)

    class _DisconnectedDatabase:
        def session(self) -> Any:
            raise RuntimeError("数据库失联")

    async def handler(_request: Any) -> Any:
        context.toolbox.db = _DisconnectedDatabase()  # type: ignore[assignment]
        return ToolMessage(content="不能直接采用的结果", tool_call_id="x", name="http_request")

    result = await _run_middleware(context, handler)
    assert "不能直接采用的结果" not in result.content
    assert "可能已经产生远端副作用" in result.content
    assert "不能直接重试" in result.content


async def test_unexpected_error_is_not_reported_as_success(database, unique_name) -> None:
    """未知异常必须明确标失败并提示"不要谎报成功"，不能伪造结果。"""
    context = await _build_context(database, unique_name)

    async def handler(_request: Any) -> Any:
        raise RuntimeError("boom")

    payload = json.loads((await _run_middleware(context, handler)).content)
    assert payload["status"] == "failed"
    assert payload["error"]["code"] == "internal_error"
    assert "不要谎报成功" in payload["error"]["recovery_hint"]


async def test_cancel_propagates_and_is_not_swallowed(database, unique_name) -> None:
    """asyncio.CancelledError 必须向上传播，不能被伪装成工具失败。"""
    context = await _build_context(database, unique_name)

    async def handler(_request: Any) -> Any:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _run_middleware(context, handler)


async def test_disabled_capability_returns_deterministic_reason(database, unique_name) -> None:
    """缺能力时给确定错误与替代能力，而不是让模型去猜。"""
    context = await _build_context(database, unique_name)
    context.disabled_tools["browser_navigate"] = (
        "浏览器工具在 P4 交付；当前 Executor 对 /browser-sessions 返回 501。"
    )

    async def handler(_request: Any) -> Any:  # pragma: no cover - 不应被调用
        raise AssertionError("被关闭的工具不得被执行")

    result = await _run_middleware(context, handler, name="browser_navigate")
    assert "P4 交付" in result.content


async def test_paused_run_blocks_tool_dispatch_without_crashing(database, unique_name) -> None:
    """暂停时被拦下的 tool_call 必须形成合法终结结果，而不是抛异常（§4.3）。"""
    context = await _build_context(database, unique_name)
    async with database.transaction() as session:
        await set_run_status(session, run_id=context.toolbox.run_id, target=RunStatus.PAUSING)

    async def handler(_request: Any) -> Any:  # pragma: no cover
        raise AssertionError("暂停期间不得派发工具")

    result = await _run_middleware(context, handler)
    assert "暂停" in result.content
    assert "未执行" in result.content


async def test_cancelled_run_blocks_tool_dispatch(database, unique_name) -> None:
    context = await _build_context(database, unique_name)
    async with database.transaction() as session:
        await set_run_status(session, run_id=context.toolbox.run_id, target=RunStatus.CANCELLING)

    async def handler(_request: Any) -> Any:  # pragma: no cover
        raise AssertionError("取消期间不得派发工具")

    result = await _run_middleware(context, handler)
    assert "取消" in result.content
    assert "未执行" in result.content


async def test_stale_owner_cannot_dispatch(database, unique_name) -> None:
    """所有权转移后，旧 owner 的工具调用必须被拒绝。"""
    context = await _build_context(database, unique_name)

    # 另一个 Runner 在 lease 过期后接管 -> generation 递增
    context.toolbox.owner_generation -= 1

    async def handler(_request: Any) -> Any:  # pragma: no cover
        raise AssertionError("过期 owner 不得派发工具")

    result = await _run_middleware(context, handler)
    assert "所有权已转移" in result.content


async def test_successful_handler_result_passes_through(database, unique_name) -> None:
    context = await _build_context(database, unique_name)

    async def handler(_request: Any) -> Any:
        from langchain_core.messages import ToolMessage

        return ToolMessage(content="ok", tool_call_id="x", name="http_request")

    result = await _run_middleware(context, handler)
    assert result.content == "ok"
    assert context.toolbox.work_units >= 1, "片段工作计数应被递增"
