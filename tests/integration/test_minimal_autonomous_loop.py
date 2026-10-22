"""第一阶段验收：真实 API/Runner/官方图/PostgreSQL，本地文件工具与脚本模型。

不调用模型供应商、不扫描任何目标。脚本模型证明工程闭环，不证明真实自主测试效果。
"""

from __future__ import annotations

import json
from typing import Annotated, Any, cast
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi.testclient import TestClient
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.factory import loaded_code_version
from agent_workbench.api.app import create_app
from agent_workbench.contracts.enums import RunStatus, ToolEffectClass
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import (
    ArtifactRow,
    CoverageRow,
    EventRow,
    FindingRow,
    LeadRow,
    ModelCallRow,
    RunRow,
    SessionRow,
    ToolOperationRow,
)
from agent_workbench.runtime.runner import AgentRunner
from agent_workbench.tools.operation import OperationRegistry
from agent_workbench.tools.task import update_plan
from tests.integration.test_instruction_scope import create_task
from tests.integration.test_weakness_closure import SteppedModel, _last_tool_payload, _system_text
from tests.support.executor_fixtures import FakeExecutor
from tests.support.scripted_model import final_text, tool_call

pytestmark = pytest.mark.integration


async def test_api_explicitly_rejects_paused_skills(database, unique_name):
    with TestClient(create_app()) as client:
        session_id, _run_id, headers = create_task(client, unique_name)
        response = client.post(
            f"/api/sessions/{session_id}/messages",
            headers={**headers, "Idempotency-Key": "disabled-skill"},
            json={"content": "继续", "skill_refs": ["user:example"]},
        )
        assert response.status_code == 422 and "暂不启用 Skills" in response.text
        async with database.session() as db:
            task = await db.get(SessionRow, session_id)
        response = client.post(
            f"/api/projects/{task.project_id}/sessions",
            headers=headers,
            json={
                "project_id": str(task.project_id),
                "title": "暂停技能",
                "initial_objective": "本地任务",
                "initial_skill_refs": ["user:example"],
            },
        )
        assert response.status_code == 422 and "暂不启用 Skills" in response.text


@pytest.mark.parametrize(
    "with_plan,tool_rounds,parallel,interruption",
    [(False, 0, False, None), (False, 1, False, None), (False, 2, False, None),
     (True, 2, False, None), (False, 2, True, None),
     (False, 2, False, "pause"), (False, 2, False, "cancel")],
)
async def test_api_to_runner_minimal_loop_persists_tools_and_final_result(
    database, unique_name, tmp_path, monkeypatch, with_plan, tool_rounds, parallel, interruption
):
    from agent_workbench.agent import factory
    from agent_workbench.runtime import runner as runner_module

    fixture = tmp_path / "local-result.json"
    fixture.write_text(json.dumps({"first": "next", "next": "done"}), encoding="utf-8")
    performed: list[str] = []

    @tool
    async def local_fixture(
        key: str,
        runtime: ToolRuntime[RunContext],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> str:
        """读取可控本地文件，真实保存操作与工件，不访问网络。"""
        box = runtime.context.toolbox
        async with box.db.transaction() as db:
            operation = await OperationRegistry(db).prepare(
                run_id=box.run_id,
                session_id=box.session_id,
                tool_call_id=tool_call_id,
                tool_name="local_fixture",
                arguments={"key": key},
                effect_class=ToolEffectClass.READ_ONLY,
                policy_revision=box.policy_revision,
                scope_revision=box.scope_revision,
                dispatch_control_version=runtime.state.get("dispatch_control_version"),
            )
            if operation.row.result_snapshot is not None:
                return json.dumps(operation.row.result_snapshot)
            data = json.loads(fixture.read_text(encoding="utf-8"))[key]
            stored = box.artifacts.write_bytes(data.encode("utf-8"), media_type="text/plain")
            artifact = await ArtifactRepository(db).create(
                artifact_id=uuid5(NAMESPACE_URL, str(operation.operation_id)),
                sha256=stored.sha256,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
                relative_key=stored.relative_key,
                session_id=box.session_id,
                source_operation_id=operation.operation_id,
                sensitivity="internal",
                availability="available",
                captured_at=stored.captured_at,
            )
            result = {
                "value": data,
                "artifact_id": str(artifact.id),
                "operation_id": str(operation.operation_id),
            }
            operation.row.status = "succeeded"
            operation.row.result_snapshot = result
            performed.append(key)
        return json.dumps(result)

    def next_from_result(messages):
        result = _last_tool_payload(messages)
        assert result["value"] == "next"
        text = _system_text(messages)
        assert "assessment_json" not in text and "completion_spec" not in text
        return tool_call("local_fixture", {"key": result["value"]}, "read-second")

    def finish(messages):
        if tool_rounds:
            assert _last_tool_payload(messages)["value"] == ("done" if tool_rounds == 2 else "next")
        assert "结束一致性检查" not in _system_text(messages)
        return final_text("未发现漏洞；仅验证本地闭环，条件不足，未进行真实目标测试。")

    stages = []
    if tool_rounds:
        first = tool_call("local_fixture", {"key": "first"}, "read-first")
        if parallel:
            first.tool_calls.append(
                {
                    "name": "local_fixture",
                    "args": {"key": "first"},
                    "id": "read-parallel",
                    "type": "tool_call",
                }
            )
        first.content = "这是中间分析，不是最终结果"
        stages.append(lambda _: first)
    if tool_rounds == 2:
        stages.append(next_from_result)
    stages.append(finish)
    if with_plan:
        stages.insert(
            0,
            lambda _: tool_call(
                "update_plan",
                {
                    "expected_plan_revision": 0,
                    "steps": [
                        {
                            "step_id": "optional",
                            "description": "可选进一步调查",
                            "status": "pending",
                        }
                    ],
                    "focus": "读取本地结果",
                },
                "optional-plan",
            ),
        )
    model = SteppedModel(steps=stages)
    tools = [local_fixture, update_plan]

    class LocalExecutor(FakeExecutor):
        async def start(self):
            return None

        async def close(self):
            return None

    executor = LocalExecutor()
    monkeypatch.setenv("XIAOMI_API_KEY", "synthetic-do-not-send")
    monkeypatch.setattr(factory, "build_chat_model", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(factory, "build_tools", lambda *_args, **_kwargs: tools)
    monkeypatch.setattr(runner_module, "HttpExecutorClient", lambda *_args, **_kwargs: executor)
    settings = database.settings.model_copy(update={"segment_decision_rounds": 1})

    with TestClient(create_app()) as client:
        session_id, run_id, headers = create_task(
            client, unique_name, objective="读取可控本地文件两次并回答"
        )
        runner = AgentRunner(database=database, settings=settings, owner="minimal-runner")
        if interruption:
            stream = runner._stream_once
            interrupted = False

            async def stream_then_interrupt(*args, **kwargs):
                nonlocal interrupted
                await stream(*args, **kwargs)
                if not interrupted:
                    interrupted = True
                    response = client.post(f"/api/runs/{run_id}/{interruption}", headers={**headers, "Idempotency-Key": f"{interruption}-{run_id}"}, json={})
                    assert response.status_code == 202, response.text

            monkeypatch.setattr(runner, "_stream_once", stream_then_interrupt)
            stopped = await runner.step(only_run_id=run_id)
            assert stopped is not None
            assert stopped.status is (RunStatus.PAUSED if interruption == "pause" else RunStatus.CANCELLED)
            async with database.session() as db:
                paused = await db.get(RunRow, run_id)
                assert "completion_result_artifact_id" not in paused.config_snapshot
            if interruption == "cancel":
                assert await runner.step(only_run_id=run_id) is None
                assert performed == ["first"], "取消在首个完整工具边界生效，后续工具不得执行"
                return
            response = client.post(f"/api/runs/{run_id}/resume", headers={**headers, "Idempotency-Key": f"resume-{run_id}"}, json={})
            assert response.status_code == 202, response.text
            runner = AgentRunner(database=database, settings=settings, owner="restarted-minimal-runner")
        outcome = await runner.step(only_run_id=run_id)
        assert outcome is not None and outcome.status is RunStatus.COMPLETED, outcome
        assert outcome.segments >= (2 if tool_rounds else 1)
        async with database.session() as db:
            saved_run = await db.get(RunRow, run_id)
            task = await db.get(SessionRow, session_id)
            assert (
                saved_run.status == "completed" and saved_run.completion_reason == "model_finished"
            )
            assert saved_run.code_version == loaded_code_version()
            operations = list(
                (
                    await db.execute(
                        select(ToolOperationRow).where(ToolOperationRow.run_id == run_id)
                    )
                ).scalars()
            )
            artifacts = list(
                (
                    await db.execute(
                        select(ArtifactRow).where(
                            ArtifactRow.session_id == session_id,
                            ArtifactRow.source_operation_id.is_not(None),
                        )
                    )
                ).scalars()
            )
            for table in (LeadRow, CoverageRow, FindingRow):
                assert (
                    list(
                        (
                            await db.execute(select(table).where(table.session_id == session_id))
                        ).scalars()
                    )
                    == []
                )
            events = list(
                (await db.execute(select(EventRow).where(EventRow.run_id == run_id))).scalars()
            )
            model_calls = list(
                (
                    await db.execute(select(ModelCallRow).where(ModelCallRow.run_id == run_id))
                ).scalars()
            )
        assert sorted(performed) == sorted(
            (["first"] if tool_rounds else [])
            + (["next"] if tool_rounds == 2 else [])
            + (["first"] if parallel else [])
        )
        assert len(operations) == len(artifacts) == tool_rounds + int(parallel)
        assert all(op.status == "succeeded" for op in operations)
        assert len(model_calls) == len(stages)
        assert not any(event.type == "report.generated" for event in events)
        if not with_plan:
            assert all(op.execution_context["step_id"] is None for op in operations)
        assert all(ArtifactStore(settings).verify(a.relative_key, a.sha256) for a in artifacts)
        config = {"configurable": {"thread_id": task.thread_id}}
        # 原 runtime 已关闭，独立 Saver 仍能恢复完整 ToolMessage 与最终结果。
        async with open_checkpointer_connection(settings) as connection:
            saved = await AsyncPostgresSaver(connection).aget_tuple(cast(Any, config))
        assert saved is not None
        values = saved.checkpoint["channel_values"]
        assert values["state_schema_version"] == 8
        assert "completion_proposal" not in values
        assert not any(e.type == "completion.review" for e in events)
        assert "work_assessment" not in values and "progress_feedback" not in values
        assert len(
            [
                m
                for m in values["messages"]
                if isinstance(m, ToolMessage) and m.name == "local_fixture"
            ]
        ) == tool_rounds + int(parallel)
        if with_plan:
            assert values["plan"][0]["status"] == "pending", "可选未完成步骤不能阻止结束"
        # 历史查询与真实工件读取仍可用。
        response = client.get(f"/api/sessions/{session_id}/snapshot")
        assert response.status_code == 200
        if artifacts:
            response = client.get(f"/api/artifacts/{artifacts[0].id}")
            assert response.status_code == 200
        page = client.get(f"/api/sessions/{session_id}/conversation").json()
        result_entry = next(item for item in page["entries"] if item["source"] == "result")
        assert result_entry["role"] == "assistant" and result_entry["available"]
        body = client.get(
            f"/api/sessions/{session_id}/conversation/result/{result_entry['id']}/text"
        )
        assert body.status_code == 200 and "未发现漏洞" in body.json()["text"]
        final_events = [
            e
            for e in events
            if e.type == "message.delta" and e.payload.get("event") == "final_response_saved"
        ]
        assert len(final_events) == 1 and final_events[0].payload.get("artifact_id")
        assert final_events[0].payload["delta"] == ""
        assert all(
            str(item["id"])
            != values["messages"][-1].response_metadata["workbench_model_call_id"]
            for item in page["entries"]
            if item["source"] == "assistant"
        )
        from agent_workbench.domain.reports import create_report_snapshot

        report = await create_report_snapshot(database, ArtifactStore(settings), session_id)
        async with database.session() as db:
            report_artifact = await db.get(ArtifactRow, report.markdown_artifact_id)
        assert "未发现漏洞" in ArtifactStore(settings).read_bytes(
            report_artifact.relative_key
        ).decode("utf-8")

        # 已完成运行不会再被 Runner 领取或再次执行工具。
        assert await runner.step(only_run_id=run_id) is None
        assert len(performed) == tool_rounds + int(parallel)
