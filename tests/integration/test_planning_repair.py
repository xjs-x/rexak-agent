"""P0/P1 回归：真实隔离库、工件、官方图；不调用模型供应商或目标。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Annotated, Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.factory import build_agent_middleware, loaded_code_version
from agent_workbench.agent.middleware.tool_dispatch import _render_tool_failure
from agent_workbench.agent.prompts import PROMPT_VERSION
from agent_workbench.agent.state import AgentGraphState, new_graph_state
from agent_workbench.agent.working_context import assemble_working_context
from agent_workbench.config.catalog import default_price_catalog
from agent_workbench.contracts.planning import PlanningValidationError, planning_digest
from agent_workbench.domain.planning import completion_is_current
from agent_workbench.domain.planning_applicability import condition_snapshot
from agent_workbench.models.provider import ModelBinding
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
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
from agent_workbench.tools.completion import propose_completion
from agent_workbench.tools.task import update_plan
from tests.integration.test_incremental_planning import claim, result_source, step, update
from tests.integration.test_tool_dispatch import _build_context
from tests.integration.test_weakness_closure import SteppedModel
from tests.support.scripted_model import tool_call
from tests.unit.test_context_budget import profile

pytestmark = pytest.mark.integration
URL = "http://127.0.0.1:8899/check"


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
            project_id=task.project_id, display_name="fixture-member", auth_revision=1
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


async def test_key_revision_short_assessment_and_precise_change_ids(database, unique_name):
    ctx = await _build_context(database, unique_name)
    initial = await update(
        ctx,
        {},
        [step("A"), step("B", "pending")],
        "initial",
        focus_json=json.dumps({"step_id": "A", "intent": "当前工作", "reason": "原方向"}),
    )
    state = {**initial.update, "plan_revision": 1}
    new = [{"step_id": "A"}, {"step_id": "B"}, step("C", "pending")]
    with pytest.raises(PlanningValidationError, match="简短判断"):
        await update(ctx, state, new, "needs-assessment")
    revised = await update(
        ctx,
        state,
        new,
        "expand",
        assessment_json=json.dumps(
            {
                "changed": "需要新工作",
                "next_action": "检查 C",
                "decision": "expand",
                "basis_refs": [],
            }
        ),
    )
    assessment = revised.update["work_assessment"]
    assert assessment["evaluated_step_ids"] == ["A"] and assessment["evaluated_plan_revision"] == 1
    change = revised.update["last_plan_change"]
    assert change["changed_step_ids"] == ["C"] and change["added_step_ids"] == ["C"]
    assert not change["order_changed"]
    reordered = await update(
        ctx,
        {**state, **revised.update},
        [{"step_id": "B"}, {"step_id": "A"}, {"step_id": "C"}],
        "reorder",
    )
    assert reordered.update["last_plan_change"]["order_changed"]
    assert reordered.update["last_plan_change"]["changed_step_ids"] == []


async def test_working_context_reuses_results_and_completed_records_can_be_carried(
    database, unique_name
):
    ctx = await _build_context(database, unique_name)
    refs = await http_source(ctx.toolbox, "prior")
    command = await update(ctx, {}, [http_step(refs=[refs])])
    state = {**command.update, "plan_revision": 1}
    noop = await update(ctx, state, [{"step_id": "s8"}], "carry")
    assert "plan" not in noop.update
    exact = await update(ctx, state, [state["plan"][0]], "exact-carry")
    assert "plan" not in exact.update
    async with database.session() as db:
        context = await assemble_working_context(db, ctx.toolbox.session_id, state)
    available = context["progress"]["results_and_work"]["available_results"]
    assert available[0]["operation_id"] == refs["operation_id"]
    assert available[0]["source_step_id"] == "s7"
    assert refs["artifact_id"] in available[0]["artifact_ids"]


async def test_unchanged_legacy_completion_does_not_block_work_but_gate_requires_review(
    database, unique_name
):
    ctx = await _build_context(database, unique_name)
    refs = await result_source(ctx.toolbox, "history", True)
    command = await update(ctx, {}, [step(status="done", completion=claim(refs))])
    old = command.update["plan"][0]
    old["completion"]["check"]["version"] = 1
    state = {**command.update, "plan": [old], "plan_revision": 1}
    before = planning_digest(state["plan"])
    carried = await update(ctx, state, [{"step_id": "A"}], "retain-history")
    assert "plan" not in carried.update and planning_digest(state["plan"]) == before
    async with database.session() as db:
        decision = await FinishGate(db).evaluate(
            session_id=ctx.toolbox.session_id,
            run_id=ctx.toolbox.run_id,
            plan=[old],
            proposal={"reason": "goal_answered", "goal_answer": "结果", "stop_rationale": "复查"},
        )
    assert decision.decision == "continue" and decision.unreviewed_count == 1
    with pytest.raises(ValueError, match="复查"):
        await update(ctx, state, [], "cannot-hide-old-completion")


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
            "loaded-planning:"
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


async def test_official_graph_parallel_sources_close_two_steps_after_restart(database, unique_name):
    ctx = await _build_context(database, unique_name)
    effects = []
    received = {}

    @tool("http_request")
    async def fixture_http(
        method: str,
        runtime: ToolRuntime[RunContext],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> str:
        """本地有计数回执；不向网络发送请求。"""
        refs = await http_source(
            runtime.context.toolbox,
            tool_call_id,
            method,
            source_step="A",
            status={"GET": 200, "PUT": 403, "OPTIONS": 405}[method],
        )
        effects.append(tool_call_id)
        received[method] = refs
        return json.dumps(refs)

    a = http_step("A")
    b = http_step("B", methods=("PUT", "OPTIONS"))

    def finish(_messages):
        return tool_call(
            "update_plan",
            {
                "expected_plan_revision": 1,
                "rationale": "按原声明范围关闭调查",
                "steps_json": json.dumps(
                    [
                        http_step("A", refs=[received["GET"]]),
                        http_step(
                            "B",
                            methods=("PUT", "OPTIONS"),
                            refs=[received["PUT"], received["OPTIONS"]],
                            outcome="not_satisfied",
                        ),
                    ]
                ),
            },
            "complete-steps",
        )

    # 并行结果顺序可能变化；按来源回执的方法匹配，不依赖执行顺序。
    model = SteppedModel(
        steps=[
            lambda _: tool_call(
                "update_plan",
                {
                    "expected_plan_revision": 0,
                    "steps_json": json.dumps([a, b]),
                    "focus_json": json.dumps(
                        {"step_id": "A", "intent": "并行检查", "reason": "已有两项调查"}
                    ),
                },
                "initial",
            ),
            lambda _: AIMessage(
                content="",
                tool_calls=[
                    {"name": "http_request", "args": {"method": method}, "id": f"http-{method}"}
                    for method in ("GET", "PUT", "OPTIONS")
                ],
            ),
            finish,
            lambda _: tool_call(
                "propose_completion",
                {
                    "reason": "goal_answered",
                    "goal_answer": "声明的调查已有明确结果",
                    "stop_rationale": "回执齐全，阴性未冒充达成",
                    "expected_plan_revision": 2,
                },
                "end",
            ),
        ]
    )
    tools = [fixture_http, update_plan, propose_completion]
    config = {"configurable": {"thread_id": ctx.thread_id}, "recursion_limit": 100}
    for index in range(4):
        async with open_checkpointer_connection(database.settings) as connection:
            graph = create_agent(
                model=model,
                tools=tools,
                middleware=build_agent_middleware(
                    model,
                    ModelBinding(profile(200000)),
                    database.settings,
                    tools,
                    default_price_catalog(),
                ),
                state_schema=AgentGraphState,
                context_schema=RunContext,
                checkpointer=AsyncPostgresSaver(connection),
                interrupt_after=["tools"],
            )
            await graph.ainvoke(
                cast(
                    Any,
                    new_graph_state(
                        session_id=ctx.toolbox.session_id, run_id=ctx.toolbox.run_id, objective="x"
                    ),
                )
                if index == 0
                else None,
                cast(Any, config),
                context=ctx,
            )
    async with open_checkpointer_connection(database.settings) as connection:
        saved = await AsyncPostgresSaver(connection).aget_tuple(cast(Any, config))
        assert saved is not None
        values = saved.checkpoint["channel_values"]
    assert len(effects) == 3 and values["plan_revision"] == 2
    assert [z["status"] for z in values["plan"]] == ["done", "done"]
    assert values["plan"][1]["completion"]["source_refs"][0]["relation"] == "supporting"
    assert values["completion_proposal"] and values["finish_gate_attempts"] == 0
    async with database.session() as db:
        assert (
            len(
                list(
                    (
                        await db.execute(
                            select(ToolOperationRow).where(
                                ToolOperationRow.run_id == ctx.toolbox.run_id
                            )
                        )
                    ).scalars()
                )
            )
            == 3
        )
