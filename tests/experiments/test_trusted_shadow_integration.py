"""实际 create_agent/HTTP Tool/仓储接口的离线非干预接缝；不连接 PostgreSQL。"""

from __future__ import annotations

import asyncio
import copy
from typing import Any, cast

import httpx
import pytest
from experiments.history_action_trusted_shadow_v1.fixture import (
    ToolOperationRow,
    approve,
    business,
    graph_run,
    make_toolbox,
    parsed,
    tool_arguments,
)
from langchain_core.messages import AIMessage

from agent_workbench.config.settings import Settings
from agent_workbench.execution.client import ExecutorConnectionError
from agent_workbench.tools import history_action_shadow as shadow
from agent_workbench.tools.http import http_request


async def close(box):
    if box.history_action_shadow is not None:
        await box.history_action_shadow.aclose()
    await box.executor.close()


@pytest.mark.parametrize(
    "scenario",
    [
        "repeat",
        "parallel",
        "retry",
        "bad-schema",
        "rejected-scope",
        "transport-error",
        "disabled-tool",
    ],
)
async def test_shadow_off_on_business_equivalence(tmp_path, scenario):
    a = tool_arguments()
    rounds = [[("http_request", a, "seed")], [("http_request", a, "repeat")]]
    if scenario == "parallel":
        rounds[1].append(("http_request", a, "parallel"))
    if scenario == "retry":
        rounds[1][0] = ("http_request", a, "seed")
    if scenario == "bad-schema":
        rounds[1][0] = ("http_request", tool_arguments(headers_json="{"), "repeat")
    if scenario == "rejected-scope":
        rounds[1][0] = ("http_request", tool_arguments(url="https://outside.invalid/x"), "repeat")
    results = []
    for enabled in (False, True):
        grants = [approve("seed", a), approve("repeat", a), approve("parallel", a)]
        box = make_toolbox(tmp_path / str(enabled), enabled, grants)
        if scenario == "transport-error":
            box.executor.error = ExecutorConnectionError("offline unknown delivery")
        try:
            if scenario == "disabled-tool":
                # 原工具能力约束由生产 middleware 处理；不向工具函数注入额外规则。
                from experiments.history_action_trusted_shadow_v1.fixture import Model
                from langchain.agents import create_agent
                from langgraph.checkpoint.memory import InMemorySaver

                from agent_workbench.agent.context import RunContext
                from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware
                from agent_workbench.agent.state import AgentGraphState

                model = Model(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "http_request",
                                    "args": a,
                                    "id": "seed",
                                    "type": "tool_call",
                                }
                            ],
                        ),
                        AIMessage(content="final"),
                    ]
                )
                graph = create_agent(
                    model=model,
                    tools=[http_request],
                    middleware=[ToolDispatchMiddleware()],
                    state_schema=AgentGraphState,
                    context_schema=RunContext,
                    checkpointer=InMemorySaver(),
                )
                result = await cast(Any, graph).ainvoke(
                    {"messages": [], "dispatch_control_version": 0},
                    {"configurable": {"thread_id": "offline-thread"}},
                    context=RunContext(
                        toolbox=box, disabled_tools={"http_request": "disabled by control"}
                    ),
                )
            else:
                graph, model, result, _, _ = await graph_run(box, rounds)
            if box.history_action_shadow:
                await box.history_action_shadow.drain()
            results.append(business(box, result, model))
            if not enabled:
                assert box.history_action_shadow is None
                assert not (tmp_path / str(enabled) / "history_action_shadow").exists()
            if enabled and scenario == "repeat":
                assert box.history_action_shadow is not None
                assert any(o.get("emit_shadow_event") for o in box.history_action_shadow.outcomes)
            if scenario == "retry":
                assert len(box.executor.jobs) == 1
            if scenario in {"rejected-scope", "bad-schema"}:
                assert len(box.executor.submissions) == 1
            if scenario == "disabled-tool":
                assert not box.executor.submissions
        finally:
            await close(box)
    assert results[0] == results[1]


@pytest.mark.parametrize(
    "failure", ["source-error", "timeout", "enqueue-error", "corrupt-grants", "save-error"]
)
async def test_shadow_failure_cannot_change_original_call(tmp_path, monkeypatch, failure):
    a = tool_arguments()
    baseline = make_toolbox(tmp_path / "off", False, [approve("seed", a)])
    _, model, result, _, _ = await graph_run(baseline, [[("http_request", a, "seed")]])
    expected = business(baseline, result, model)
    await close(baseline)
    box = make_toolbox(tmp_path / "on", True, [approve("seed", a)])
    if failure == "corrupt-grants":
        box.settings.history_action_shadow_grants_path.write_text("{", encoding="utf-8")
    if failure == "timeout":

        async def slow(*args):
            await asyncio.sleep(1)

        monkeypatch.setattr(shadow, "read_records", slow)
        box.settings.history_action_shadow_timeout_seconds = 0.001
    if failure == "source-error":

        async def source_fail(*args):
            raise RuntimeError("source unavailable")

        monkeypatch.setattr(shadow, "read_records", source_fail)
    if failure == "enqueue-error":

        def fail(*args):
            raise RuntimeError("queue unavailable")

        monkeypatch.setattr(shadow, "enqueue_http", fail)
    if failure == "save-error":

        def fail(*args):
            raise OSError("independent shadow disk unavailable")

        monkeypatch.setattr(shadow, "write_once", fail)
    try:
        _, model, result, _, _ = await graph_run(box, [[("http_request", a, "seed")]])
        if box.history_action_shadow:
            await box.history_action_shadow.drain()
        assert business(box, result, model) == expected
        if box.history_action_shadow:
            assert not any(o.get("emit_shadow_event") for o in box.history_action_shadow.outcomes)
    finally:
        await close(box)


async def test_parallel_dedup_is_stable_across_service_restart(tmp_path):
    a = tool_arguments()
    calls = ["seed", "r1", "r2", "r3", "r4", "r5"]
    box = make_toolbox(tmp_path, True, [approve(c, a) for c in calls])
    try:
        _graph, _model, result, _config, context = await graph_run(
            box, [[("http_request", a, "seed")], [("http_request", a, c) for c in calls[1:]]]
        )
        await box.history_action_shadow.drain()
        observed = box.history_action_shadow.outcomes
        assert sum(o.get("emit_shadow_event", False) for o in observed) == 1
        assert sum(o.get("duplicate_suppressed", False) for o in observed) == 4
        root = box.history_action_shadow.root
        assert len(list((root / "events").glob("*.json"))) == 1
        # 新 service 从控制端独立事件回执去重；不依赖模型轮次计数或内存全局集合。
        old = box.history_action_shadow
        box.history_action_shadow = shadow.ShadowService(box)
        packet = copy.deepcopy(box.executor.submissions[-1])
        view = shadow.dispatch_context(type("Runtime", (), {"state": result, "context": context})())
        view["messages"] = view["messages"][:4]
        view["batch_ids"] = calls[1:]
        box.history_action_shadow.enqueue(
            parsed(a).model_dump(exclude={"note"}), packet, "r5", view
        )
        await box.history_action_shadow.drain()
        assert box.history_action_shadow.outcomes[0]["duplicate_suppressed"]
        assert not box.history_action_shadow.outcomes[0]["emit_shadow_event"]
        # 同一物理调用重新投递，历史证据未发生实质变化时旧键仍被保护。
        assert len(list((root / "events").glob("*.json"))) == 1
        await old.aclose()
    finally:
        await close(box)


async def test_interrupt_resume_preserves_checkpoint_and_original_result(tmp_path):
    a = tool_arguments()
    values = []
    for enabled in (False, True):
        box = make_toolbox(
            tmp_path / str(enabled), enabled, [approve("seed", a), approve("repeat", a)]
        )
        try:
            graph, model, _first, config, context = await graph_run(
                box,
                [[("http_request", a, "seed")], [("http_request", a, "repeat")]],
                interrupt_after=["tools"],
            )
            if box.history_action_shadow:
                await box.history_action_shadow.drain()
            state = await graph.aget_state(config)
            assert state.next
            await graph.ainvoke(None, config, context=context)
            await graph.ainvoke(None, config, context=context)
            saved = await graph.aget_state(config)
            assert not saved.next
            if box.history_action_shadow:
                await box.history_action_shadow.drain()
            values.append(business(box, saved.values, model))
        finally:
            await close(box)
    assert values[0] == values[1]


async def test_actual_task_cancellation_propagates_and_does_not_submit_twice(tmp_path):
    a = tool_arguments()
    results = []
    for enabled in (False, True):
        box = make_toolbox(tmp_path / str(enabled), enabled, [approve("seed", a)])
        box.executor.submit_barrier = asyncio.Event()
        task = asyncio.create_task(graph_run(box, [[("http_request", a, "seed")]]))
        try:
            for _ in range(100):
                if box.executor.submissions:
                    break
                await asyncio.sleep(0.002)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            if box.history_action_shadow:
                await box.history_action_shadow.drain()
            assert len(box.executor.submissions) == 1 and not box.executor.jobs
            results.append([(r.status, r.tool_call_id) for r in box.db.rows[ToolOperationRow]])
        finally:
            await close(box)
    assert results[0] == results[1]


async def test_empty_cookie_rule_is_verified_against_executor_http_implementation():
    from workbench_executor.http_sessions import HttpSessionRegistry
    from workbench_executor.policy import NetworkPolicy, ScopeRuleSpec
    from workbench_executor.settings import ExecutorSettings

    registry = HttpSessionRegistry(None)
    session = registry.get("identity")
    await session.client.aclose()
    seen = []

    def respond(request):
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, content=b"offline response")

    session.client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond), cookies={"session": "preexisting"}
    )
    policy = NetworkPolicy(
        [ScopeRuleSpec(scheme="https", host="127.0.0.1", port=443, allow_private=True)],
        [],
        ExecutorSettings(token="offline"),
    )
    try:
        for headers in (None, {"Cookie": ""}):
            await registry.request(
                identity_id="identity",
                method="GET",
                url="https://127.0.0.1/check",
                headers=headers,
                max_redirects=0,
                policy=policy,
            )
        assert seen == ["session=preexisting", ""]
    finally:
        await registry.close()


def test_shadow_defaults_and_tool_schema_have_no_model_trust_switch():
    assert cast(Any, Settings)(_env_file=None).history_action_shadow_enabled is False
    fields = cast(Any, http_request.tool_call_schema).model_fields
    assert not {
        "trusted",
        "verified",
        "provenance_verified",
        "security_test",
        "history_action_shadow_enabled",
    } & set(fields)


async def test_exact_prechange_http_baseline_matches_off_and_on(tmp_path):
    import hashlib
    import importlib.util
    import json
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "experiments/history_action_trusted_shadow_v1"
    path = root / "baseline/src__agent_workbench__tools__http.py"
    expected = json.loads((root / "preservation-before.json").read_text(encoding="utf-8"))[
        "src/agent_workbench/tools/http.py"
    ]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
    spec = importlib.util.spec_from_file_location("offline_prechange_http", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    a = tool_arguments()
    observed = []
    for index, (enabled, tool) in enumerate(
        [(False, module.http_request), (False, http_request), (True, http_request)]
    ):
        box = make_toolbox(
            tmp_path / str(index), enabled, [approve("seed", a), approve("repeat", a)]
        )
        try:
            _, model, result, _, _ = await graph_run(
                box,
                [[("http_request", a, "seed")], [("http_request", a, "repeat")]],
                tool_impl=tool,
            )
            if box.history_action_shadow:
                await box.history_action_shadow.drain()
            observed.append(business(box, result, model))
        finally:
            await close(box)
    assert observed[0] == observed[1] == observed[2]


async def test_actual_same_batch_result_cannot_become_past_evidence(tmp_path):
    a = tool_arguments()
    box = make_toolbox(tmp_path, True, [approve("seed", a), approve("repeat", a)])
    try:
        await graph_run(box, [[("http_request", a, "seed"), ("http_request", a, "repeat")]])
        await box.history_action_shadow.drain()
        assert len(box.executor.jobs) == 2
        assert not any(o.get("emit_shadow_event") for o in box.history_action_shadow.outcomes)
    finally:
        await close(box)


async def test_paused_run_is_checked_before_http_shadow_boundary(tmp_path):
    from agent_workbench.persistence.tables import RunRow

    a = tool_arguments()
    observed = []
    for enabled in (False, True):
        box = make_toolbox(tmp_path / str(enabled), enabled, [approve("seed", a)])
        box.db.rows[RunRow][0].status = "paused"
        try:
            _, model, result, _, _ = await graph_run(box, [[("http_request", a, "seed")]])
            assert not box.executor.submissions and box.history_action_shadow is None
            observed.append(business(box, result, model))
        finally:
            await close(box)
    assert observed[0] == observed[1]


@pytest.mark.parametrize(
    "poison",
    [
        "model-trust",
        "body-binding",
        "artifact-hash",
        "artifact-owner",
        "job-unknown",
        "completion-time",
        "unread-body",
        "cross-session",
    ],
)
async def test_adapter_rejects_untrusted_and_incomplete_engineering_sources(tmp_path, poison):
    from experiments.history_action_trusted_shadow_v1.dataset import generate, predict

    frame = await generate(tmp_path, "development-source")
    baseline = predict(frame)
    assert baseline["observation"]["emit_shadow_event"]
    if poison == "model-trust":
        frame["grant"]["purpose"] = "navigation"
        frame["arguments"]["provenance_verified"] = True
    if poison == "body-binding":
        frame["operation"]["payload"]["body"] = "different"
    if poison == "artifact-hash":
        for art in frame["artifacts"]:
            art["sha256"] = "0" * 64
    if poison == "artifact-owner":
        for art in frame["artifacts"]:
            art["source_operation_id"] = frame["operation"]["operation_id"]
    if poison == "job-unknown":
        for job in frame["jobs"]:
            job["state"] = "unknown"
    if poison == "completion-time":
        for job in frame["jobs"]:
            job["executor_finished_at"] = None
    if poison == "unread-body":
        frame["context"]["messages"] = []
    if poison == "cross-session":
        for op in frame["operations"]:
            op["session_id"] = "00000000-0000-0000-0000-000000000044"
    prediction = predict(frame)
    assert prediction["observation"] is None or not prediction["observation"]["emit_shadow_event"]
