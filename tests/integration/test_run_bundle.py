"""隔离 PostgreSQL 中核对续聊归属、checkpoint 时点和不可变导出。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import StateGraph

from agent_workbench.agent.state import AgentGraphState
from agent_workbench.contracts.events import EventType
from agent_workbench.domain.run_state import create_run
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.maintenance.diagnostics import collect_run_diagnostic
from agent_workbench.maintenance.run_bundle import (
    collect_run_bundle,
    verify_run_bundle,
    write_run_bundle,
)
from agent_workbench.observability.events import append_event
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import ArtifactRow, LeadRow, ModelCallRow, ToolOperationRow

pytestmark = pytest.mark.integration


async def test_bundle_includes_continuations_but_not_other_session(database, unique_name, tmp_path):
    store = ArtifactStore(database.settings)
    stored = store.write_bytes("工具返回的真实响应".encode(), media_type="text/plain")
    foreign = store.write_bytes(b"foreign-private-body", media_type="text/plain")
    foreign_id, artifact_id, operation_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="bundle-test",
            name=unique_name,
            target_description="本地替身",
            default_scope={},
        )
        task = await SessionRepository(db).create(
            project_id=project.id, title="完整会话", scope_snapshot={}, policy_snapshot={}
        )
        other = await SessionRepository(db).create(
            project_id=project.id, title="其他会话", scope_snapshot={}, policy_snapshot={}
        )
        first = await create_run(
            db,
            session_id=task.id,
            objective="初始目标",
            provider_id="fake",
            model_name="scripted",
            config_snapshot={},
        )
        first.status = "completed"
        await db.flush()
        second = await create_run(
            db,
            session_id=task.id,
            objective="续聊",
            provider_id="fake",
            model_name="scripted",
            config_snapshot={},
        )
        second.status = "paused"
        outsider = await create_run(
            db,
            session_id=other.id,
            objective="其他",
            provider_id="fake",
            model_name="scripted",
            config_snapshot={},
        )
        db.add(
            ToolOperationRow(
                operation_id=operation_id,
                run_id=first.id,
                session_id=task.id,
                tool_call_id="tool-1",
                tool_name="http_request",
                request_digest="a" * 64,
                effect_class="read_only",
                status="succeeded",
                result_artifact_ids=[str(artifact_id), str(foreign_id)],
            )
        )
        for identifier, body, session_id, source in (
            (artifact_id, stored, task.id, operation_id),
            (foreign_id, foreign, other.id, None),
        ):
            db.add(
                ArtifactRow(
                    id=identifier,
                    sha256=body.sha256,
                    byte_size=body.byte_size,
                    media_type=body.media_type,
                    relative_key=body.relative_key,
                    session_id=session_id,
                    source_operation_id=source,
                    captured_at=body.captured_at,
                )
            )
        lead = LeadRow(
            session_id=task.id,
            hypothesis="需要核实的候选",
            evidence_refs=[str(artifact_id)],
            next_action="复查响应",
        )
        db.add(lead)
        for run in (first, second, outsider):
            db.add(
                ModelCallRow(
                    model_call_id=uuid4(),
                    logical_call_id=uuid4(),
                    run_id=run.id,
                    session_id=run.session_id,
                    phase="agent",
                    provider_id="fake",
                    model_name="scripted",
                    usage_status="unknown",
                    submitted_at=now,
                )
            )
            await append_event(
                db,
                session_id=run.session_id,
                run_id=run.id,
                event_type=EventType.RUN_STATUS,
                payload={"run_id": str(run.id)},
            )
        await append_event(
            db, session_id=task.id, event_type=EventType.PLAN_UPDATED, payload={"revision": 2}
        )

    def noop(state: AgentGraphState) -> dict:
        return {}

    graph = StateGraph(AgentGraphState)
    graph.add_node("noop", noop)
    graph.set_entry_point("noop")
    graph.set_finish_point("noop")
    async with open_checkpointer_connection(database.settings) as connection:
        compiled = graph.compile(checkpointer=AsyncPostgresSaver(connection))
        await compiled.aupdate_state(
            {"configurable": {"thread_id": task.thread_id}},
            {
                "messages": [],
                "plan_revision": 2,
                "state_schema_version": 1,
                "plan": [
                    {
                        "step_id": "s1",
                        "description": "核实响应 password=secret-sentinel",
                        "status": "in_progress",
                        "rationale": None,
                        "lead_id": str(lead.id),
                    }
                ],
            },
        )
    legacy = await collect_run_diagnostic(database, second.id)
    assert len(legacy["runs"]) == 1 and len(legacy["model_calls"]) == 1
    payload = await collect_run_bundle(database, second.id)
    assert {run["id"] for run in payload["runs"]} == {str(first.id), str(second.id)}
    assert {call["run_id"] for call in payload["model_calls"]} == {str(first.id), str(second.id)}
    assert [event["session_seq"] for event in payload["events"]] == [1, 2, 3]
    assert payload["events"][-1]["run_id"] is None
    assert payload["snapshot"]["session_event_seq"] == 3
    assert payload["snapshot"]["database_read_at"]
    checkpoint = payload["checkpoint_projection"]
    assert checkpoint["checkpoint_id"] and checkpoint["checkpoint_created_at"]
    assert checkpoint["values"]["plan_revision"] == 2
    assert "secret-sentinel" not in json.dumps(checkpoint)
    assert str(foreign_id) in payload["missing_artifact_metadata"]
    assert len(payload["artifacts"]) == 1
    assert "non_atomic" in payload["snapshot"]["combined_consistency"]
    directory = write_run_bundle(tmp_path / "private-bundle", payload, store)
    assert verify_run_bundle(directory) == []
    # 通过真实 CLI 的独立进程导出同一隔离数据库，覆盖参数接线与源码指纹。
    cli_result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "agent_workbench.cli",
            "export-run",
            str(second.id),
            "--bundle",
            "--output",
            str(tmp_path / "cli-export"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=30,
        check=False,
    )
    assert cli_result.returncode == 0, cli_result.stderr
    cli_directory = tmp_path / "cli-export" / str(second.id) / "bundle"
    assert verify_run_bundle(cli_directory) == []
    assert (
        json.loads((cli_directory / "exporter-source.json").read_text(encoding="utf-8"))["status"]
        == "available"
    )
    async with database.transaction() as db:
        current = await db.get(LeadRow, lead.id)
        assert current is not None
        current.hypothesis = "后来修改的判断"
        current.revision = 2
    snapshot = json.loads((directory / "snapshots/export.json").read_text(encoding="utf-8"))
    assert snapshot["session_knowledge"]["leads"][0]["revision"] == 1
    assert snapshot["session_knowledge"]["leads"][0]["hypothesis"] == "需要核实的候选"
