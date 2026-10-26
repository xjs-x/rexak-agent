"""正式 API/Runner/生产 middleware/PostgreSQL/Saver；不访问真实模型或执行端。"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from experiments.history_action_autonomous_shadow_v1.fixture import policy_for
from experiments.history_action_trusted_shadow_v1.fixture import parsed, tool_arguments
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select

from agent_workbench.agent import factory
from agent_workbench.api.app import create_app
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.domain.instruction_scope import sync_instruction_scope
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.tables import (
    ArtifactRow,
    ModelCallRow,
    RunRow,
    SessionRow,
    ToolJobRow,
    ToolOperationRow,
)
from agent_workbench.runtime import runner as runner_module
from agent_workbench.runtime.runner import AgentRunner
from tests.integration.test_autonomous_shadow import MockExecutor, NoTunnel
from tests.integration.test_instruction_scope import create_task
from tests.integration.test_weakness_closure import SteppedModel
from tests.support.scripted_model import final_text, tool_call

pytestmark = pytest.mark.integration


def references(model):
    result = []
    for index, messages in enumerate(model.received):
        for message in messages:
            if not isinstance(message, (AIMessage, ToolMessage)):
                continue
            try:
                value = json.loads(str(message.content))
            except ValueError:
                continue
            if isinstance(value, dict) and "_host_history_reference" in value:
                result.append((index, message, value["_host_history_reference"]))
    return result


async def production_run(
    database,
    name,
    root,
    monkeypatch,
    mode,
    *,
    variation=None,
    restart=False,
    memory_fault=False,
    overloaded=False,
    navigation=False,
    legacy=False,
    force_off_on_resume=False,
    trim_request_view=False,
):
    from types import SimpleNamespace

    from agent_workbench.agent.middleware import history_assistance
    from agent_workbench.tools.history_action_shadow import ShadowService

    executor = MockExecutor()
    profile = parsed(tool_arguments()).model_dump(exclude={"note"})
    steps = [
        lambda _: tool_call("run_http_test_profile", {"profile_id": "baseline"}, "first"),
        lambda _: (
            tool_call("http_request", tool_arguments(), "repeat")
            if navigation
            else tool_call("run_http_test_profile", {"profile_id": "next"}, "repeat")
        ),
        lambda _: tool_call("run_http_test_profile", {"profile_id": "next"}, "after-hint"),
        lambda _: final_text("原 Agent 自主继续后的离线最终结果；不作安全结论。"),
    ]
    model = SteppedModel(steps=steps)
    monkeypatch.setenv("XIAOMI_API_KEY", "synthetic-unused-no-network")
    monkeypatch.setattr(factory, "build_chat_model", lambda *_a, **_kw: model)
    monkeypatch.setattr(runner_module, "HttpExecutorClient", lambda *_a, **_kw: executor)
    monkeypatch.setattr(runner_module, "SshTunnel", NoTunnel)
    if trim_request_view:
        from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware

        original_builder = ContextBuilderMiddleware.awrap_model_call

        async def trimmed(self, request, handler):
            async def forward(prepared):
                return await handler(
                    prepared.override(
                        messages=[m for m in prepared.messages if isinstance(m, HumanMessage)]
                    )
                )

            return await original_builder(self, request, forward)

        monkeypatch.setattr(ContextBuilderMiddleware, "awrap_model_call", trimmed)
    if memory_fault:

        async def broken(*_args):
            raise OSError("offline history retrieval failed")

        monkeypatch.setattr(history_assistance, "build_blackboard", broken)
    if overloaded:
        monkeypatch.setattr(
            ShadowService,
            "enqueue",
            lambda self, *_a, **_kw: setattr(self, "skipped", self.skipped + 1),
        )
    root.mkdir(parents=True)
    settings = database.settings.model_copy(
        update={
            "executor_id": "offline-executor",
            "data_root": root,
            "history_assistance_mode": mode,
            "history_action_test_profiles_enabled": True,
            "history_action_shadow_grants_path": None,
            "history_action_shadow_context_path": root / "policy.json",
            "segment_decision_rounds": 1,
            "history_action_shadow_timeout_seconds": 2,
            "history_assistance_retrieval_seconds": 3,
        }
    )
    try:
        with TestClient(create_app()) as client:
            sid, rid, headers = create_task(
                client, name, objective="test https://contract.invalid/check"
            )
            async with database.transaction() as db:
                run = await db.get(RunRow, rid)
                config_snapshot = {**run.config_snapshot, "history_assistance_mode": mode}
                if legacy:
                    config_snapshot.pop("history_assistance_mode")
                run.config_snapshot = config_snapshot
                scope = await sync_instruction_scope(db, session_id=sid, run_id=rid)
                session = await db.get(SessionRow, sid)
            box = SimpleNamespace(
                session_id=sid,
                run_id=rid,
                scope_revision=session.scope_snapshot["revision"],
                scope=ScopePolicy(scope),
                policy_revision=1,
                control_version=0,
                executor=executor,
            )
            policy = policy_for(
                box,
                {
                    "baseline": {"request": profile},
                    "next": {"request": profile, **(variation or {})},
                },
            )
            settings.history_action_shadow_context_path.write_text(
                json.dumps(policy), encoding="utf-8"
            )
            runner = AgentRunner(database=database, settings=settings, owner="mvp-runner")
            if restart:
                original = runner._stream_once
                paused = False

                async def pause_after_notice(*args, **kwargs):
                    nonlocal paused
                    await original(*args, **kwargs)
                    if not paused and any(
                        "reflection" in value for _, _, value in references(model)
                    ):
                        paused = True
                        response = client.post(
                            f"/api/runs/{rid}/pause",
                            headers={**headers, "Idempotency-Key": str(rid) + "pause"},
                            json={},
                        )
                        assert response.status_code == 202

                monkeypatch.setattr(runner, "_stream_once", pause_after_notice)
                result = await runner.step(only_run_id=rid)
                assert result is not None
                assert result.status is RunStatus.PAUSED
                response = client.post(
                    f"/api/runs/{rid}/resume",
                    headers={**headers, "Idempotency-Key": str(rid) + "resume"},
                    json={},
                )
                assert response.status_code == 202
                async with database.session() as db:
                    resumed = await db.get(RunRow, rid)
                policy["control_version"] = resumed.control_version
                settings.history_action_shadow_context_path.write_text(
                    json.dumps(policy), encoding="utf-8"
                )
                # 全局模式变化不热应用至已固定的 Run。
                runner = AgentRunner(
                    database=database,
                    settings=settings.model_copy(
                        update={
                            "history_assistance_mode": "off",
                            "history_assistance_force_off": force_off_on_resume,
                        }
                    ),
                    owner="mvp-recreated",
                )
            result = await runner.step(only_run_id=rid)
            assert result is not None
            assert result.status is RunStatus.COMPLETED, result
            async with database.session() as db:
                task = await db.get(SessionRow, sid)
                operations = list(
                    (
                        await db.scalars(
                            select(ToolOperationRow).where(ToolOperationRow.run_id == rid)
                        )
                    ).all()
                )
                jobs = list(
                    (
                        await db.scalars(
                            select(ToolJobRow).where(
                                ToolJobRow.operation_id.in_([o.operation_id for o in operations])
                            )
                        )
                    ).all()
                )
                artifacts = list(
                    (
                        await db.scalars(select(ArtifactRow).where(ArtifactRow.session_id == sid))
                    ).all()
                )
                calls = list(
                    (await db.scalars(select(ModelCallRow).where(ModelCallRow.run_id == rid))).all()
                )
                saved_run = await db.get(RunRow, rid)
            async with open_checkpointer_connection(settings) as conn:
                checkpoint = await AsyncPostgresSaver(conn).aget_tuple(
                    cast(Any, {"configurable": {"thread_id": task.thread_id}})
                )
            assert checkpoint is not None
            messages = checkpoint.checkpoint["channel_values"]["messages"]
            assert [m.tool_call_id for m in messages if isinstance(m, ToolMessage)] == [
                "first",
                "repeat",
                "after-hint",
            ]
            assert len(operations) == len(jobs) == len(executor.jobs) == 3
            assert all(o.status == "succeeded" for o in operations)
            assert len(model.received) == len(calls) == 4
            assert not any("_host_history_reference" in str(m.content) for m in messages)
            assert not any("_host_history_reference" in str(o.result_snapshot) for o in operations)
            return {
                "model": model,
                "calls": calls,
                "run": saved_run,
                "operations": operations,
                "jobs": jobs,
                "submissions": executor.submissions,
                "checkpoint": checkpoint.checkpoint["channel_values"],
                "artifacts": artifacts,
                "root": root,
                "settings": settings,
            }
    finally:
        await executor.client.aclose()


@pytest.mark.parametrize("mode", ["off", "memory", "passive"])
async def test_production_modes_deliver_reference_without_extra_calls(
    database, unique_name, tmp_path, monkeypatch, mode
):
    result = await production_run(database, unique_name, tmp_path / mode, monkeypatch, mode)
    refs = references(result["model"])
    if mode == "off":
        assert not refs
        assert not (tmp_path / mode / "history_action_shadow").exists()
    else:
        manifests = [
            json.loads(
                    (database.settings.artifact_root / a.relative_key).read_text(encoding="utf-8")
            )
            for a in result["artifacts"]
            if (a.summary or "").startswith("最终请求 manifest")
        ]
        assert len(manifests) == 4
        assert all(
            m["history_assistance"]["memory"]
            in {"already_visible", "selected", "no_relevant_history"}
            for m in manifests
        )
        assert any("memory" in value for _, _, value in refs) or any(
            m["history_assistance"]["memory"] == "already_visible" for m in manifests
        )
        assert all(
            m["runtime_versions"]["run_assembly"]["code_version"] == result["run"].code_version
            for m in manifests
        )
        assert all(
            m["runtime_versions"]["process_start"]["code_version"] == result["run"].code_version
            for m in manifests
        )
        if mode == "passive":
            notes = [
                (index, message, value) for index, message, value in refs if "reflection" in value
            ]
            assert len(notes) == 1
            assert notes[0][0] == 2 and isinstance(notes[0][1], ToolMessage)
            assert "可以继续原计划" in notes[0][2]["reflection"]["notice"]
            assert len(list((tmp_path / mode).rglob("feedback/*.json"))) == 1
        else:
            assert not any("reflection" in value for _, _, value in refs)
            assert not (tmp_path / mode / "history_action_shadow").exists()


async def test_runner_memory_recalls_after_context_builder_trims_request_view(
    database, unique_name, tmp_path, monkeypatch
):
    result = await production_run(
        database, unique_name, tmp_path / "trimmed", monkeypatch, "memory", trim_request_view=True
    )
    assert any("memory" in value for _, _, value in references(result["model"]))
    assert len(result["calls"]) == 4 and len(result["operations"]) == 3
    assert all(
        "_host_history_reference" not in str(m.content) for m in result["checkpoint"]["messages"]
    )


@pytest.mark.parametrize("variation", [{"configuration": "changed/v2"}, {"retest_required": True}])
async def test_necessary_new_tests_continue(
    database, unique_name, tmp_path, monkeypatch, variation
):
    result = await production_run(
        database, unique_name, tmp_path / "new", monkeypatch, "passive", variation=variation
    )
    assert len(result["operations"]) == 3
    # v2-v2 后续重复可提示；v1-v2 本身不能判相同；明确复测不提示。
    notes = [(i, value) for i, _, value in references(result["model"]) if "reflection" in value]
    assert not notes if variation.get("retest_required") else all(i >= 3 for i, _ in notes)


@pytest.mark.parametrize("fault", ["memory", "overload", "restart", "rollback"])
async def test_failure_and_checkpoint_recreation_preserve_execution(
    database, unique_name, tmp_path, monkeypatch, fault
):
    result = await production_run(
        database,
        unique_name,
        tmp_path / fault,
        monkeypatch,
        "passive",
        memory_fault=fault == "memory",
        overloaded=fault == "overload",
        restart=fault in {"restart", "rollback"},
        force_off_on_resume=fault == "rollback",
    )
    assert len(result["operations"]) == 3
    refs = references(result["model"])
    if fault == "memory":
        assert not any("memory" in value for _, _, value in refs)
    if fault == "overload":
        assert not any("reflection" in value for _, _, value in refs)
    if fault == "restart":
        assert sum("reflection" in value for _, _, value in refs) == 1
        assert result["run"].config_snapshot["history_assistance_mode"] == "passive"
    if fault == "rollback":
        assert not any(
            "_host_history_reference" in str(m.content) for m in result["model"].received[-1]
        )


async def test_existing_unknown_history_conditions_are_low_priority_and_session_scoped(
    database, unique_name
):
    from langchain.agents import create_agent
    from langchain_core.messages import HumanMessage, SystemMessage

    from agent_workbench.agent.context import RunContext
    from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
    from agent_workbench.agent.middleware.history_assistance import HistoryAssistanceMiddleware
    from agent_workbench.agent.state import AgentGraphState
    from tests.integration.test_decision_memory import seeded

    run, task, box, old_op, _, artifact = await seeded(database, unique_name)
    _, _, _, foreign_op, _, _ = await seeded(database, unique_name + "foreign")
    objective = f"核对 operation:{old_op} 的条件与未知执行状态；不要把 operation:{foreign_op} 当成本 Session 来源。"
    async with database.transaction() as db:
        row = await db.get(RunRow, run.id)
        row.objective = objective
    box.settings = box.settings.model_copy(update={"history_assistance_mode": "memory"})
    box.history_assistance_manifest = None
    model = SteppedModel(steps=[lambda _: final_text("unknown 保留；旧身份结论不沿用。")])
    async with open_checkpointer_connection(box.settings) as conn:
        saver = AsyncPostgresSaver(conn)
        graph = create_agent(
            model=model,
            tools=[],
            system_prompt="original system",
            middleware=[ContextBuilderMiddleware(), HistoryAssistanceMiddleware()],
            state_schema=AgentGraphState,
            context_schema=RunContext,
            checkpointer=saver,
        )
        config: Any = {"configurable": {"thread_id": str(task.thread_id)}}
        result = await graph.ainvoke(
            cast(Any, {"messages": [HumanMessage(content=objective, id=f"objective-{run.id}")]}),
            config,
            context=RunContext(toolbox=cast(Any, box), thread_id=task.thread_id),
        )
    refs = references(model)
    assert len(refs) == 1 and isinstance(refs[0][1], AIMessage)
    serialized = json.dumps(refs[0][2], ensure_ascii=False)
    assert str(artifact) in serialized and str(foreign_op) not in serialized
    evidence = refs[0][2]["memory"]["execution_evidence"][0]
    assert evidence["execution"]["operation_status"] == "unknown"
    changed = next(
        d for d in evidence["condition_differences"] if d["field"] == "identity_auth_revision"
    )
    assert changed["status"] == "changed" and (changed["old_value"], changed["new_value"]) == (1, 2)
    assert evidence["evidence_availability"] == "metadata_only_no_body_read"
    assert "UNTRUSTED_ARTIFACT_SENTINEL" not in serialized
    assert not any("_host_history_reference" in str(m.content) for m in result["messages"])
    assert not any(
        "_host_history_reference" in str(m.content)
        for m in model.received[0]
        if isinstance(m, SystemMessage)
    )


async def test_off_is_business_equivalent_to_legacy_runner(
    database, unique_name, tmp_path, monkeypatch
):
    from experiments.history_action_autonomous_shadow_v1.projections import (
        business_view,
        differences,
    )

    original = await production_run(
        database, unique_name + "legacy", tmp_path / "legacy", monkeypatch, "off", legacy=True
    )
    off = await production_run(database, unique_name + "off", tmp_path / "off", monkeypatch, "off")
    before = cast(dict[str, Any], business_view(original))
    after = cast(dict[str, Any], business_view(off))
    for view in (before, after):
        for name in ("operations", "jobs", "http_artifacts"):
            view[name].sort(key=lambda row: json.dumps(row, sort_keys=True))
    assert differences(before, after) == []


async def test_api_pins_new_run_mode_and_keeps_active_run_mode(database, unique_name, monkeypatch):
    from uuid import uuid4

    chosen = database.settings.model_copy(update={"history_assistance_mode": "memory"})
    monkeypatch.setattr("agent_workbench.api.routes.get_settings", lambda: chosen)
    with TestClient(create_app()) as client:
        sid, rid, headers = create_task(client, unique_name)
        chosen = chosen.model_copy(update={"history_assistance_mode": "off"})
        response = client.post(
            f"/api/sessions/{sid}/messages",
            json={"content": "继续当前阶段"},
            headers={**headers, "Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 202
        _, new_rid, _ = create_task(client, unique_name + "new-off")
    async with database.session() as db:
        assert (await db.get(RunRow, rid)).config_snapshot["history_assistance_mode"] == "memory"
        assert (await db.get(RunRow, new_rid)).config_snapshot["history_assistance_mode"] == "off"


async def test_navigation_keeps_memory_but_is_not_a_shadow_candidate(
    database, unique_name, tmp_path, monkeypatch
):
    result = await production_run(
        database, unique_name, tmp_path / "navigation", monkeypatch, "passive", navigation=True
    )
    observations = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in (tmp_path / "navigation").rglob("observations/*.json")
    ]
    assert all(o["candidate_tool_call_id"] != "repeat" for o in observations)
    assert any("memory" in value for _, _, value in references(result["model"]))
