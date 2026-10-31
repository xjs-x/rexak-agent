"""历史完成记录读取与来源真实性兼容；生产计划不再调用这些证明校验器。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from agent_workbench.agent.factory import loaded_code_version
from agent_workbench.agent.middleware.tool_dispatch import _render_tool_failure
from agent_workbench.agent.prompts import PROMPT_VERSION
from agent_workbench.contracts.planning import PlanningValidationError, planning_digest
from agent_workbench.domain.planning import completion_is_current
from agent_workbench.domain.planning_applicability import condition_snapshot
from agent_workbench.persistence.tables import (
    ArtifactRow,
    FindingRow,
    IdentityRow,
    RunRow,
    SessionRow,
    ToolOperationRow,
)
from agent_workbench.runtime.runner import AgentRunner
from agent_workbench.runtime.stopping import FinishGate
from tests.integration.test_incremental_planning import result_source
from tests.integration.test_tool_dispatch import _build_context

pytestmark = pytest.mark.integration
URL = "http://127.0.0.1:8899/check"


def step(identifier="A", status="in_progress", *, mode="investigate", **kwargs):
    return {
        "step_id": identifier,
        "description": f"历史任务 {identifier}",
        "status": status,
        "completion_spec": {"mode": mode, "done_when": "取得结果", "check_type": "model"},
        **kwargs,
    }


def claim(refs, outcome="satisfied"):
    return {
        "outcome": outcome,
        "summary": "历史来源",
        "source_refs": [{"kind": "artifact", "id": refs["artifact_id"]}],
    }


async def update(context, state, steps, call_id="legacy-validation", **extra) -> Any:
    """只验证已有历史协议；不调用生产 update_plan，不提交图或外部执行。"""
    from copy import deepcopy

    from langgraph.types import Command

    from agent_workbench.tools.planning_validation import validate_plan_steps

    _ = call_id, extra
    old = {step["step_id"]: step for step in state.get("plan") or []}
    values = [{**deepcopy(old.get(item["step_id"], {})), **deepcopy(item)} for item in steps]
    async with context.toolbox.db.session() as db:
        await validate_plan_steps(db, context.toolbox, list(old.values()), values, steps)
    return Command(
        update={"plan": values, "plan_revision": int(state.get("plan_revision") or 0) + 1}
    )


async def http_source(box, call, method="GET", *, source_step="s7", url=URL, status=200):
    refs = await result_source(box, call, True, step_id=source_step)
    async with box.db.transaction() as db:
        row = await db.get(ToolOperationRow, UUID(refs["operation_id"]))
        row.tool_name = "http_request"
        row.execution_context = {
            **(row.execution_context or {}),
            "method": method,
            "url": url,
            "identity": "anonymous",
            "authentication": "anonymous",
            "target_mapping_signature": planning_digest(box.scope.snapshot.target_mappings),
        }
        row.result_snapshot = {"data": {"status_code": status}, "references": refs}
    return refs


def http_step(identifier="s8", *, methods=("GET",), refs=None, outcome="satisfied", **extra):
    value = step(identifier, status="done" if refs else "in_progress", **extra)
    value["completion_spec"]["http_checks"] = [{"method": method, "url": URL} for method in methods]
    if refs:
        value["completion"] = {
            "outcome": outcome,
            "summary": "所声明端点检查已取得明确结果，非漏洞确认",
            "source_refs": [{"kind": "artifact", "id": ref["artifact_id"]} for ref in refs],
        }
    return value


async def test_cross_step_sources_normalize_without_changing_operation(database, unique_name):
    ctx = await _build_context(database, unique_name)
    refs = await http_source(ctx.toolbox, "collected-under-s7")
    async with database.session() as db:
        original = dict(
            (await db.get(ToolOperationRow, UUID(refs["operation_id"]))).execution_context
        )
    command = await update(ctx, {}, [http_step(refs=[refs])])
    record = command.update["plan"][0]["completion"]
    assert record["source_refs"][0]["relation"] == "supporting"
    assert record["check"]["source_links"][0]["source_step_ids"] == ["s7"]
    assert record["check"]["http_coverage"][0]["operation_id"] == refs["operation_id"]
    assert record["check"]["sufficiency"] == "model_judgment"
    async with database.session() as db:
        assert (
            await db.get(ToolOperationRow, UUID(refs["operation_id"]))
        ).execution_context == original
        assert (
            await completion_is_current(
                db, ctx.toolbox.artifacts, ctx.toolbox.session_id, command.update["plan"][0]
            )
            is None
        )


async def test_options_does_not_complete_put_and_batch_stays_atomic(database, unique_name):
    ctx = await _build_context(database, unique_name)
    options = await http_source(ctx.toolbox, "options", "OPTIONS", status=405)
    previous = [http_step(methods=("OPTIONS", "PUT")), step("unchanged", "pending")]
    initial = await update(ctx, {}, previous, "initial")
    state = {**initial.update, "plan_revision": 1}
    before = planning_digest(state["plan"])
    with pytest.raises(PlanningValidationError) as error:
        await update(
            ctx,
            state,
            [http_step(methods=("OPTIONS", "PUT"), refs=[options]), {"step_id": "unchanged"}],
            "incomplete",
        )
    assert error.value.details["step_errors"][0]["code"] == "http_work_scope_missing"
    assert error.value.details["step_errors"][0]["missing_check"]["method"] == "PUT"
    assert planning_digest(state["plan"]) == before
    put = await http_source(ctx.toolbox, "put", "PUT", status=403)
    accepted = await update(
        ctx,
        state,
        [
            http_step(methods=("OPTIONS", "PUT"), refs=[options, put], outcome="not_satisfied"),
            {"step_id": "unchanged"},
        ],
        "negative",
    )
    record = accepted.update["plan"][0]["completion"]
    assert {entry["status_code"] for entry in record["check"]["http_coverage"]} == {403, 405}
    assert record["outcome"] == "not_satisfied"
    assert accepted.update["plan"][1] == initial.update["plan"][1]


async def test_http_model_completion_needs_scope_and_reports_all_step_errors(database, unique_name):
    ctx = await _build_context(database, unique_name)
    refs = await http_source(ctx.toolbox, "get")
    with pytest.raises(PlanningValidationError) as error:
        await update(
            ctx,
            {},
            [
                step("A", "done", completion=claim(refs)),
                http_step("B", methods=("PUT",), refs=[refs]),
            ],
        )
    failure = json.loads(_render_tool_failure("update_plan", error.value))
    issues = failure["error"]["details"]["step_errors"]
    assert [(item["target_step_id"], item["code"]) for item in issues] == [
        ("A", "http_work_scope_required"),
        ("B", "http_work_scope_missing"),
    ]
    assert (
        "已有" in failure["error"]["recovery_hint"]
        or "不要重放" in failure["error"]["recovery_hint"]
    )


@pytest.mark.parametrize(
    "mismatch",
    ["path", "identity", "authentication", "time", "scope", "mapping", "policy", "unknown"],
)
async def test_reuse_does_not_bypass_applicability(database, unique_name, mismatch):
    ctx = await _build_context(database, unique_name)
    refs = await http_source(ctx.toolbox, "read", status=404)
    value = http_step(refs=[refs])
    async with database.transaction() as db:
        row = await db.get(ToolOperationRow, UUID(refs["operation_id"]))
        if mismatch == "path":
            value["completion_spec"]["http_checks"][0]["url"] += "/other"
        elif mismatch in {"identity", "authentication"}:
            value["completion_spec"]["http_checks"][0][mismatch] = (
                "member" if mismatch == "identity" else "explicit_headers"
            )
        elif mismatch == "time":
            value["completion_spec"]["not_before"] = (
                datetime.now(UTC) + timedelta(minutes=1)
            ).isoformat()
        elif mismatch == "scope":
            task = await db.get(SessionRow, ctx.toolbox.session_id)
            task.scope_snapshot = {**task.scope_snapshot, "rules": []}
        elif mismatch == "mapping":
            task = await db.get(SessionRow, ctx.toolbox.session_id)
            task.scope_snapshot = {
                **task.scope_snapshot,
                "target_mappings": [
                    {
                        "from_host": "127.0.0.1",
                        "from_port": "8899",
                        "to_host": "127.0.0.2",
                        "to_port": "8899",
                    }
                ],
            }
        elif mismatch == "policy":
            row.policy_revision = 999
        elif mismatch == "unknown":
            row.status = "unknown"
    with pytest.raises(ValueError):
        await update(ctx, {}, [value])


async def test_cross_session_and_missing_source_errors_do_not_suggest_replay(database, unique_name):
    ctx = await _build_context(database, unique_name)
    other = await _build_context(database, unique_name + "-other")
    refs = await http_source(ctx.toolbox, "own")
    with pytest.raises(ValueError):
        await update(other, {}, [http_step(refs=[refs])])
    with pytest.raises(PlanningValidationError) as error:
        bad = {"operation_id": str(UUID(int=999))}
        await update(
            ctx,
            {},
            [
                step(
                    status="done",
                    completion={
                        "outcome": "satisfied",
                        "summary": "结果",
                        "source_refs": [{"kind": "operation", "id": bad["operation_id"]}],
                    },
                )
            ],
        )
    assert "resource_id/job_id" in error.value.details["step_errors"][0]["recovery"]


async def test_changed_identity_and_unavailable_artifact_cannot_be_reused(database, unique_name):
    ctx = await _build_context(database, unique_name)
    refs = await http_source(ctx.toolbox, "identity-result")
    async with database.transaction() as db:
        task = await db.get(SessionRow, ctx.toolbox.session_id)
        identity = IdentityRow(
            project_id=task.project_id, session_id=task.id, display_name="fixture-member", auth_revision=1
        )
        db.add(identity)
        await db.flush()
        op = await db.get(ToolOperationRow, UUID(refs["operation_id"]))
        op.identity_id = identity.id
        op.execution_context = {**op.execution_context, "identity_auth_revision": 0}
    with pytest.raises(ValueError, match="认证状态"):
        await update(ctx, {}, [http_step(refs=[refs])])
    async with database.transaction() as db:
        op = await db.get(ToolOperationRow, UUID(refs["operation_id"]))
        op.execution_context = {**op.execution_context, "identity_auth_revision": 1}
        artifact = await db.get(ArtifactRow, UUID(refs["artifact_id"]))
        artifact.availability = "missing"
    with pytest.raises(ValueError, match="真实、可读"):
        await update(ctx, {}, [http_step(refs=[refs])], "missing-artifact")


async def test_new_conditions_do_not_invalidate_old_conditions_but_retraction_does(
    database, unique_name
):
    ctx = await _build_context(database, unique_name)
    refs = await http_source(ctx.toolbox, "result")
    async with database.transaction() as db:
        finding = FindingRow(
            session_id=ctx.toolbox.session_id,
            title="候选条件",
            category="fixture",
            status="candidate",
            weakness_kind="enabling_condition",
            evidence_refs=[refs["artifact_id"]],
            provided_conditions=[
                {
                    "key": "existing",
                    "statement": "已取得条件",
                    "evidence_refs": [refs["artifact_id"]],
                }
            ],
            applicability={},
        )
        db.add(finding)
        await db.flush()
        identifier = finding.id
        op = await db.get(ToolOperationRow, UUID(refs["operation_id"]))
        op.execution_context = {
            **op.execution_context,
            "source_weakness_revisions": {str(identifier): 1},
            "source_condition_snapshots": {str(identifier): condition_snapshot(finding)},
        }
    accepted = await update(ctx, {}, [http_step(refs=[refs])])
    recorded = accepted.update["plan"][0]
    async with database.transaction() as db:
        row = await db.get(FindingRow, identifier)
        row.revision = 2
        row.provided_conditions = [*row.provided_conditions, {"key": "new", "statement": "新结果"}]
    async with database.session() as db:
        assert (
            await completion_is_current(db, ctx.toolbox.artifacts, ctx.toolbox.session_id, recorded)
            is None
        )
    async with database.transaction() as db:
        row = await db.get(FindingRow, identifier)
        row.provided_conditions = [{"key": "existing", "statement": "原条件已修改"}]
    with pytest.raises(ValueError, match="条件"):
        await update(ctx, {}, [http_step(refs=[refs])], "changed-condition")


async def test_legacy_completion_is_preserved_but_not_a_finish_obligation(database, unique_name):
    from tests.integration.test_incremental_planning import update as update_current

    ctx = await _build_context(database, unique_name)
    refs = await result_source(ctx.toolbox, "history", True)
    command = await update(ctx, {}, [step(status="done", completion=claim(refs))])
    old = command.update["plan"][0]
    old["completion"]["check"]["version"] = 1
    state = {**command.update, "plan": [old], "plan_revision": 1}
    before = planning_digest(state["plan"])
    carried = await update_current(ctx, state, [{"step_id": "A"}], "retain-history")
    assert "plan" not in carried.update and planning_digest(state["plan"]) == before
    async with ctx.toolbox.db.session() as db:
        decision = await FinishGate(db).evaluate(
            session_id=ctx.toolbox.session_id,
            run_id=ctx.toolbox.run_id,
            plan=[old],
            proposal={"reason": "goal_answered", "goal_answer": "历史完成记录保留原样"},
        )
    assert decision.allowed


async def test_loaded_runtime_versions_are_captured_without_changing_history(database, unique_name):
    ctx = await _build_context(database, unique_name)
    async with database.session() as db:
        run = await db.get(RunRow, ctx.toolbox.run_id)
    assert run.prompt_version == run.code_version == "unbound"
    runner = AgentRunner(settings=database.settings, database=database, owner=ctx.toolbox.owner)
    runtime = SimpleNamespace(prompt_version=PROMPT_VERSION, code_version=loaded_code_version())
    await runner._capture_loaded_versions(run, cast(Any, runtime), ctx.toolbox.owner_generation)
    await runner._capture_loaded_versions(run, cast(Any, runtime), ctx.toolbox.owner_generation)
    async with database.session() as db:
        saved = await db.get(RunRow, run.id)
        assert saved.prompt_version == PROMPT_VERSION and saved.code_version.startswith(
            "loaded-runtime-v2:"
        )
        assert len(saved.config_snapshot["loaded_runtime_activations"]) == 1


async def test_health_reports_loaded_versions_without_model_call(database):
    from agent_workbench.api.app import create_app

    with TestClient(create_app()) as client:
        response = client.get("/health")
        assert response.status_code == 200
        versions = response.json()["runtime_versions"]
        assert versions["prompt_version"] == PROMPT_VERSION
        assert versions["code_version"] == loaded_code_version()
        assert "not_entire_process" in versions["scope"]
        assert versions["process_start"]["code_version"] == versions["code_version"]
        assert versions["algorithm"] == "canonical_code_v2"


async def test_v2_activation_appends_without_rewriting_legacy_fingerprint(database, unique_name):
    from agent_workbench.agent.factory import loaded_runtime_versions

    ctx = await _build_context(database, unique_name)
    legacy = {"code_version": "loaded-minimal-loop:historical", "prompt_version": "16",
              "owner_generation": -1, "captured_at": "2026-10-01T00:00:00Z"}
    async with database.transaction() as db:
        row = await db.get(RunRow, ctx.toolbox.run_id)
        row.config_snapshot = {**row.config_snapshot, "loaded_runtime_activations": [legacy]}
    async with database.session() as db:
        run = await db.get(RunRow, ctx.toolbox.run_id)
    runner = AgentRunner(settings=database.settings, database=database, owner=ctx.toolbox.owner)
    version = loaded_runtime_versions()
    runtime = SimpleNamespace(prompt_version=PROMPT_VERSION, code_version=version["code_version"], loaded_versions=version)
    await runner._capture_loaded_versions(run, cast(Any, runtime), ctx.toolbox.owner_generation)
    async with database.session() as db:
        saved = await db.get(RunRow, run.id)
        history = saved.config_snapshot["loaded_runtime_activations"]
        assert history[0] == legacy and len(history) == 2
        assert history[1]["loaded_implementation"]["code_version"] == version["code_version"]
        assert history[1]["process_start"]["capture_stage"] == "runner_initialization"
