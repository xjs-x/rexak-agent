"""第一轮规划闭环协议验收。脚本模型不能作为自主规划效果证明。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Annotated, Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from langchain.agents import create_agent
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.factory import AgentRuntime, build_agent_middleware
from agent_workbench.agent.middleware.progress import ProgressMiddleware
from agent_workbench.agent.prompts import SYSTEM_PROMPT
from agent_workbench.agent.state import AgentGraphState, new_graph_state
from agent_workbench.config.catalog import default_price_catalog
from agent_workbench.contracts.enums import ToolEffectClass
from agent_workbench.domain.planning import completion_is_current
from agent_workbench.models.provider import ModelBinding
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import ArtifactRow, EventRow, SessionRow, ToolOperationRow
from agent_workbench.runtime.runner import AgentRunner
from agent_workbench.runtime.stopping import FinishGate
from agent_workbench.tools.completion import propose_completion
from agent_workbench.tools.operation import OperationRegistry
from agent_workbench.tools.plan_revisions import project_committed_plan
from agent_workbench.tools.task import update_plan
from tests.integration.test_chain_planning import _call
from tests.integration.test_tool_dispatch import _build_context
from tests.integration.test_weakness_closure import SteppedModel, _last_tool_payload, _system_text
from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call
from tests.unit.test_context_budget import profile

pytestmark = pytest.mark.integration


async def result_source(
    box: Any, call_id: str, value: bool, *, step_id: str | None = "A"
) -> dict[str, str]:
    async with box.db.transaction() as db:
        prepared = await OperationRegistry(db).prepare(
            run_id=box.run_id,
            session_id=box.session_id,
            tool_call_id=call_id,
            tool_name="fixture_result",
            arguments={"value": value},
            effect_class=ToolEffectClass.READ_ONLY,
            policy_revision=1,
            scope_revision=1,
            context_extra={"step_id": step_id} if step_id else {},
        )
        op = prepared.row
        if op.result_snapshot is not None:
            return dict(op.result_snapshot["references"])
        stored = box.artifacts.write_bytes(
            json.dumps({"exists": value}).encode(), media_type="application/json"
        )
        artifact = await ArtifactRepository(db).create(
            artifact_id=uuid5(NAMESPACE_URL, f"fixture:{op.operation_id}"),
            sha256=stored.sha256,
            byte_size=stored.byte_size,
            media_type=stored.media_type,
            relative_key=stored.relative_key,
            session_id=box.session_id,
            source_operation_id=op.operation_id,
            sensitivity="internal",
            availability="available",
            captured_at=stored.captured_at,
        )
        refs = {"operation_id": str(op.operation_id), "artifact_id": str(artifact.id)}
        op.status = "succeeded"
        op.result_snapshot = {"data": {"exists": value}, "references": refs}
    return refs


def step(identifier="A", status="in_progress", *, mode="investigate", **kwargs):
    return {
        "step_id": identifier,
        "description": f"检查任务 {identifier}",
        "status": status,
        "completion_spec": {"mode": mode, "done_when": "取得指定结果", "check_type": "model"},
        **kwargs,
    }


def claim(refs: dict[str, str], outcome="satisfied"):
    return {
        "outcome": outcome,
        "summary": "基于实际来源的结果",
        "source_refs": [{"kind": "artifact", "id": refs["artifact_id"]}],
    }


async def update(context, state, steps, call_id="plan", **extra):
    return await _call(
        update_plan,
        context,
        {
            "steps_json": json.dumps(steps, ensure_ascii=False),
            "expected_plan_revision": state.get("plan_revision", 0),
            "rationale": "根据结果调整工作",
            **extra,
        },
        call_id,
        state,
    )


@pytest.mark.parametrize(
    "mode,outcome,accepted",
    [
        ("investigate", "not_satisfied", True),
        ("achieve", "not_satisfied", False),
        ("investigate", "inconclusive", False),
        ("achieve", "satisfied", True),
    ],
)
async def test_completion_modes_require_real_sources(
    database, unique_name, mode, outcome, accepted
):
    ctx = await _build_context(database, unique_name)
    refs = await result_source(ctx.toolbox, "read", False)
    terminal = step(status="done", mode=mode, completion=claim(refs, outcome))
    if not accepted:
        with pytest.raises(ValueError):
            await update(ctx, {}, [terminal])
        return
    command = await update(ctx, {}, [terminal])
    completed = command.update["plan"][0]
    assert completed["completion"]["check"]["method"] == "model"
    assert "不代表独立验证" in completed["completion"]["check"]["scope"]
    async with database.session() as db:
        assert (
            await completion_is_current(
                db, ctx.toolbox.artifacts, ctx.toolbox.session_id, completed
            )
            is None
        )


async def test_predicate_and_sources_cannot_be_forged_or_swapped(database, unique_name):
    ctx = await _build_context(database, unique_name)
    refs = await result_source(ctx.toolbox, "read", False)
    terminal = step(status="done", completion=claim(refs, "not_satisfied"))
    terminal["completion_spec"].update(
        check_type="predicate",
        predicate={
            "operation_id": refs["operation_id"],
            "path": ["data", "exists"],
            "equals": True,
        },
    )
    command = await update(ctx, {}, [terminal])
    checked = command.update["plan"][0]
    assert checked["completion"]["check"]["method"] == "predicate"
    forged = {**terminal, "completion": {**claim(refs), "check": {"method": "verification"}}}
    with pytest.raises(ValueError):
        await update(ctx, {}, [forged], "forged")
    with pytest.raises(ValueError, match="字段检查不一致"):
        await update(ctx, {}, [{**terminal, "completion": claim(refs)}], "false-success")
    wrong = step("B", status="done", completion=claim(refs))
    normalized = await update(ctx, {}, [wrong], "automatic-support")
    assert normalized.update["plan"][0]["completion"]["source_refs"][0]["relation"] == "supporting"
    supporting = claim(refs)
    supporting["source_refs"][0]["relation"] = "supporting"
    adopted = await update(ctx, {}, [step("B", status="done", completion=supporting)], "supporting")
    assert adopted.update["plan"][0]["completion"]["source_refs"][0]["relation"] == "supporting"
    async with database.transaction() as db:
        op = await db.get(ToolOperationRow, UUID(refs["operation_id"]))
        op.status = "unknown"
    async with database.session() as db:
        assert await completion_is_current(
            db, ctx.toolbox.artifacts, ctx.toolbox.session_id, checked
        )
        decision = await FinishGate(db).evaluate(
            session_id=ctx.toolbox.session_id,
            proposal={
                "reason": "goal_answered",
                "goal_answer": "不能假完成",
                "stop_rationale": "检查",
            },
            plan=[checked],
        )
        assert decision.decision == "continue" and decision.unreviewed_count == 1


async def test_assessment_stays_on_old_task_when_focus_moves(database, unique_name):
    ctx = await _build_context(database, unique_name)
    refs = await result_source(ctx.toolbox, "read", False)
    previous = {
        "plan": [step()],
        "plan_revision": 4,
        "current_focus": {
            "step_id": "A",
            "lead_id": None,
            "intent": "检查",
            "reason": "原工作",
            "source": "explicit",
        },
    }
    assessment = {
        "changed": "缺少条件",
        "next_action": "寻找替代来源",
        "decision": "repair",
        "trigger": "new_evidence",
        "evaluated_step_ids": ["A"],
        "evaluated_plan_revision": 4,
        "basis_refs": [{"kind": "operation", "id": refs["operation_id"]}],
    }
    args = {
        "focus_json": json.dumps({"step_id": "C", "intent": "找替代", "reason": "修复 A 的条件"}),
        "assessment_json": json.dumps(assessment),
    }
    command = await update(
        ctx,
        previous,
        [step(status="blocked", rationale="缺少配置", resume_note="找到配置"), step("C")],
        **args,
    )
    assert command.update["current_focus"]["step_id"] == "C"
    judged = command.update["work_assessment"]
    assert judged["step_id"] == "A" and judged["evaluated_step_ids"] == ["A"]
    assert judged["evaluated_plan_revision"] == 4
    for bad in ({"evaluated_step_ids": ["C"]}, {"evaluated_plan_revision": 3}):
        with pytest.raises(ValueError):
            await update(
                ctx,
                previous,
                [step(), step("C")],
                "bad",
                assessment_json=json.dumps({**assessment, **bad}),
            )
    with pytest.raises(ValueError, match="过期"):
        await update(ctx, previous, [step()], "stale", expected_plan_revision=3)


async def test_noop_replay_removal_and_same_batch_are_checked(database, unique_name):
    ctx = await _build_context(database, unique_name)
    first = await update(ctx, {}, [step()])
    state = dict(first.update)
    again = await update(ctx, state, [step()], "noop")
    assert "plan_revision" not in again.update
    replay = await update(ctx, state, [step()], expected_plan_revision=0)
    assert json.loads(replay.update["messages"][0].content)["replayed"]
    with pytest.raises(ValueError, match="参数发生冲突"):
        await update(ctx, state, [step("C")], expected_plan_revision=0)
    with pytest.raises(ValueError, match="不能直接删除"):
        await update(ctx, state, [], "remove")
    with pytest.raises(ValueError, match="dropped"):
        await update(ctx, state, [step(status="dropped")], "drop")
    state["messages"] = [
        AIMessage(
            content="",
            tool_calls=[
                {"name": "update_plan", "args": {}, "id": key, "type": "tool_call"}
                for key in ("one", "two")
            ],
        )
    ]
    with pytest.raises(ValueError, match="同批"):
        await update(ctx, state, [step()], "one")


async def test_plan_and_assessment_text_do_not_count_as_new_facts(database, unique_name):
    ctx = await _build_context(database, unique_name)
    middleware = ProgressMiddleware()
    state: dict[str, Any] = {}
    runtime = cast(Any, SimpleNamespace(context=ctx))
    baseline = await middleware.abefore_model(cast(Any, state), runtime)
    assert baseline is not None
    state.update(baseline)
    state.update(
        completed_decision_rounds=1,
        work_assessment={"changed": "换一种说法", "next_action": "再看看"},
        current_focus={"step_id": "other"},
        plan_revision=8,
        messages=[AIMessage(content="已完成一个回合", id="completed-round")],
    )
    changed = await middleware.abefore_model(cast(Any, state), runtime)
    assert changed is not None
    assert changed["no_progress_streak"] == 1


@pytest.mark.parametrize(
    "missing", [True, False], ids=["repair-failed-premise", "expand-after-success"]
)
async def test_official_runner_path_uses_result_to_execute_next_stage(
    database, unique_name, missing
):
    ctx = await _build_context(database, unique_name)
    box = ctx.toolbox
    effects: list[str] = []

    @tool
    async def fixture_probe(
        where: str,
        runtime: ToolRuntime[RunContext],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> str:
        """只操作本地 fixture，保存真实操作与工件；不访问目标。"""
        refs = await result_source(
            runtime.context.toolbox,
            tool_call_id,
            not (missing and where == "primary"),
            step_id=None,
        )
        if tool_call_id not in effects:
            effects.append(tool_call_id)
        return json.dumps(refs)

    plan = [step("A"), step("B", "pending", mode="achieve")]
    received_refs: dict[str, dict[str, str]] = {}

    def initial(_messages):
        return tool_call(
            "update_plan",
            {"expected_plan_revision": 0, "steps_json": json.dumps(plan), "rationale": "开始检查"},
            "plan-1",
        )

    def assess_primary(messages):
        refs = _last_tool_payload(messages)
        received_refs["A"] = refs
        plan[0].update(
            status="done", completion=claim(refs, "not_satisfied" if missing else "satisfied")
        )
        if missing:
            plan[1].update(status="blocked", rationale="配置缺失", resume_note="取得可用配置")
        plan.append(
            step(
                "C",
                mode="achieve",
                description="取得替代配置" if missing else "利用配置检查下一阶段",
            )
        )
        return tool_call(
            "update_plan",
            {
                "expected_plan_revision": 1,
                "steps_json": json.dumps(plan),
                "rationale": "根据结果修复条件" if missing else "利用新条件展开后续工作",
                "focus_json": json.dumps(
                    {"step_id": "C", "intent": "准备后续条件", "reason": "关键结果带来下一步"}
                ),
                "assessment_json": json.dumps(
                    {
                        "changed": "配置缺失" if missing else "已取得配置",
                        "next_action": "获取后续所需条件",
                        "decision": "repair" if missing else "expand",
                        "trigger": "new_evidence",
                        "evaluated_step_ids": ["A"],
                        "evaluated_plan_revision": 1,
                        "basis_refs": [{"kind": "operation", "id": refs["operation_id"]}],
                    }
                ),
            },
            "plan-2",
        )

    def act_next(messages):
        text = _system_text(messages)
        assert "已提交计划版本：2" in text and "evaluated_step_ids" in text
        assert "investigate" in text and "achieve" in text
        return tool_call(
            "fixture_probe", {"where": "alternative" if missing else "next-stage"}, "probe-next"
        )

    def restore(messages):
        refs = _last_tool_payload(messages)
        received_refs["C"] = refs
        plan[2].update(status="done", completion=claim(refs))
        plan[1].update(status="in_progress")
        return tool_call(
            "update_plan",
            {
                "expected_plan_revision": 2,
                "steps_json": json.dumps(plan),
                "rationale": "条件已取得，继续原目标",
            },
            "plan-3",
        )

    def close_steps(messages):
        refs = _last_tool_payload(messages)
        received_refs["B"] = refs
        plan[1].update(status="done", completion=claim(refs))
        return tool_call(
            "update_plan",
            {
                "expected_plan_revision": 3,
                "steps_json": json.dumps(plan),
                "rationale": "最终条件已检查",
            },
            "plan-4",
        )

    model = SteppedModel(
        steps=[
            initial,
            lambda _: tool_call("fixture_probe", {"where": "primary"}, "probe-primary"),
            assess_primary,
            act_next,
            restore,
            lambda _: tool_call("fixture_probe", {"where": "final"}, "probe-final"),
            close_steps,
            lambda _: tool_call(
                "propose_completion",
                {
                    "reason": "goal_answered",
                    "goal_answer": "已核对模拟服务",
                    "stop_rationale": "所有工作有来源且最终条件成立",
                    "expected_plan_revision": 4,
                },
                "finish",
            ),
        ]
    )
    tools = [update_plan, fixture_probe, propose_completion]
    binding = ModelBinding(profile(200000))
    middleware = build_agent_middleware(
        model, binding, database.settings, tools, default_price_catalog()
    )
    # 本测试执行超过一个片段，Runner 必须真实续跑；压缩阈值在大上下文下不会触发。
    runner = AgentRunner(settings=database.settings, database=database, owner=box.owner)
    config = {"configurable": {"thread_id": ctx.thread_id}, "recursion_limit": 120}
    async with database.session() as db:
        task = await db.get(SessionRow, box.session_id)
        from agent_workbench.persistence.tables import RunRow

        run = await db.get(RunRow, box.run_id)
    async with open_checkpointer_connection(database.settings) as connection:
        saver = AsyncPostgresSaver(connection)
        async def read_head():
            committed = await saver.aget_tuple(cast(Any, config))
            return dict(committed.checkpoint["channel_values"]) if committed is not None else {}

        box.planning_head_reader = read_head
        graph = create_agent(
            model=model,
            tools=tools,
            middleware=middleware,
            system_prompt=SYSTEM_PROMPT,
            state_schema=AgentGraphState,
            context_schema=RunContext,
            checkpointer=saver,
        )
        runtime = cast(
            AgentRuntime,
            SimpleNamespace(graph=graph, checkpointer=saver, config=lambda **_: config),
        )
        for index in range(1, 8):
            payload = await runner._segment_input(runtime, run, task, index)
            await runner._stream_once(runtime, ctx, box, task, run, payload)
            saved = await saver.aget_tuple(cast(Any, config))
            assert saved is not None
            state = saved.checkpoint["channel_values"]
            if state.get("completion_proposal"):
                break
        else:
            pytest.fail("官方图未形成完整闭环")
        async with database.session() as db:
            decision = await FinishGate(db).evaluate(
                session_id=box.session_id,
                proposal=state["completion_proposal"],
                plan=state["plan"],
                run_id=box.run_id,
            )
        assert decision.allowed
        assert effects == ["probe-primary", "probe-next", "probe-final"]
        assert state["plan_revision"] == 4
        assert state["work_assessment"]["evaluated_step_ids"] == ["A"]
        assert state["work_assessment"]["evaluated_plan_revision"] == 1
        assert [item["step_id"] for item in state["plan"]] == ["A", "B", "C"]
        assert all(item["status"] == "done" for item in state["plan"])
        assert await project_committed_plan(database, database.settings, box.session_id, saved) == 0
    async with database.session() as db:
        operations = list(
            (
                await db.execute(
                    select(ToolOperationRow).where(ToolOperationRow.run_id == box.run_id)
                )
            ).scalars()
        )
        events = list(
            (
                await db.execute(
                    select(EventRow)
                    .where(EventRow.session_id == box.session_id, EventRow.type == "plan.updated")
                    .order_by(EventRow.session_seq)
                )
            ).scalars()
        )
    assert len(operations) == 3
    assert [
        op.execution_context["step_id"]
        for op in sorted(operations, key=lambda item: item.created_at)
    ] == ["A", "C", "B"]
    assert [event.payload["revision"] for event in events] == [1, 2, 3, 4]
    assert all(event.payload["source"] == "committed_langgraph_checkpoint" for event in events)


async def test_uncommitted_artifact_is_not_a_revision_and_projection_repairs_once(
    database, unique_name, monkeypatch
):
    ctx = await _build_context(database, unique_name)
    command = await update(ctx, {}, [step()])
    config = {"configurable": {"thread_id": ctx.thread_id}}
    async with open_checkpointer_connection(database.settings) as conn:
        saver = AsyncPostgresSaver(conn)
        assert (
            await project_committed_plan(
                database,
                database.settings,
                ctx.toolbox.session_id,
                await saver.aget_tuple(cast(Any, config)),
            )
            == 0
        )
        graph = create_agent(
            model=ScriptedChatModel(responses=[final_text("保留状态")]),
            tools=[],
            state_schema=AgentGraphState,
            checkpointer=saver,
        )
        await graph.aupdate_state(cast(Any, config), cast(Any, command.update), as_node="__start__")
    # 关闭原连接后重新构图，投影只从实际提交的 checkpoint 读取。
    async with open_checkpointer_connection(database.settings) as conn:
        saved = await AsyncPostgresSaver(conn).aget_tuple(cast(Any, config))
        from agent_workbench.tools import plan_revisions

        original = plan_revisions.append_event

        async def fail_after_insert(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("模拟事件已插入但事务未提交时中断")

        with monkeypatch.context() as patch:
            patch.setattr(plan_revisions, "append_event", fail_after_insert)
            with pytest.raises(RuntimeError, match="未提交"):
                await project_committed_plan(
                    database, database.settings, ctx.toolbox.session_id, saved
                )
        counts = await asyncio.gather(
            *[
                project_committed_plan(database, database.settings, ctx.toolbox.session_id, saved)
                for _ in range(2)
            ]
        )
        assert sum(counts) == 1
        assert (
            await project_committed_plan(database, database.settings, ctx.toolbox.session_id, saved)
            == 0
        )
        # 已提交调用重放不制造新版本；没有外部操作会因投影补齐而执行。
        replay = await update(ctx, dict(command.update), [step()], expected_plan_revision=0)
        assert json.loads(replay.update["messages"][0].content)["replayed"]
    async with database.session() as db:
        assert (
            list(
                (
                    await db.execute(
                        select(ToolOperationRow).where(
                            ToolOperationRow.run_id == ctx.toolbox.run_id
                        )
                    )
                ).scalars()
            )
            == []
        )


async def test_persisted_head_conflict_rejects_stale_snapshot(database, unique_name):
    ctx = await _build_context(database, unique_name)

    async def changed_head():
        return {"plan_revision": 1}

    ctx.toolbox.planning_head_reader = changed_head
    with pytest.raises(ValueError, match="快照已过期"):
        await update(ctx, {}, [step()])


async def test_private_bundle_keeps_revision_source_without_calling_it_target_evidence(database, unique_name, tmp_path):
    from agent_workbench.maintenance.run_bundle import (
        collect_run_bundle,
        verify_run_bundle,
        write_run_bundle,
    )

    ctx = await _build_context(database, unique_name)
    command = await update(ctx, {}, [step()])
    async with open_checkpointer_connection(database.settings) as connection:
        graph = create_agent(model=ScriptedChatModel(responses=[final_text("只读来源")]), tools=[],
            state_schema=AgentGraphState, checkpointer=AsyncPostgresSaver(connection))
        await graph.aupdate_state({"configurable": {"thread_id": ctx.thread_id}},
            cast(Any, command.update), as_node="__start__")
    payload = await collect_run_bundle(database, ctx.toolbox.run_id)
    reference = payload["checkpoint_projection"]["values"]["last_plan_change"]
    assert payload["audit_artifact_roles"][reference["artifact_id"]] == "plan_revision_candidate"
    directory = write_run_bundle(tmp_path / "private-bundle", payload, ctx.toolbox.artifacts)
    body = json.loads((directory / "plans" / "revisions" / f"{reference['artifact_id']}.json").read_text(encoding="utf-8"))
    assert body["change"]["update_id"] == reference["update_id"]
    assert verify_run_bundle(directory) == []


async def test_completion_proposal_cannot_use_a_plan_changed_in_same_batch(database, unique_name):
    ctx = await _build_context(database, unique_name)
    refs = await result_source(ctx.toolbox, "actual-result", True)
    state = {"plan": [step()], "plan_revision": 1}
    proposal = await _call(propose_completion, ctx, {"reason": "goal_answered",
        "goal_answer": "已取得结果", "stop_rationale": "有执行来源", "expected_plan_revision": 1},
        "propose-on-old-snapshot", state)
    completed = await update(ctx, state, [step(status="done", completion=claim(refs))], "finish-step")
    async with database.session() as db:
        decision = await FinishGate(db).evaluate(session_id=ctx.toolbox.session_id,
            proposal=proposal.update["completion_proposal"], plan=completed.update["plan"])
    assert decision.decision == "continue" and "旧计划快照" in decision.reasons[0]


async def test_completion_rechecks_conditions_context_and_evidence(database, unique_name):
    from agent_workbench.domain.run_state import bump_control_version

    ctx = await _build_context(database, unique_name)
    refs = await result_source(ctx.toolbox, "source", True)
    checked = (await update(ctx, {}, [step(status="done", completion=claim(refs))])).update["plan"][
        0
    ]
    async with database.session() as db:
        changed = {
            **checked,
            "completion_spec": {**checked["completion_spec"], "done_when": "换一个目标"},
        }
        assert "完成条件已变化" in str(
            await completion_is_current(db, ctx.toolbox.artifacts, ctx.toolbox.session_id, changed)
        )
    async with database.transaction() as db:
        await bump_control_version(db, run_id=ctx.toolbox.run_id)
    async with database.session() as db:
        assert "指令已变化" in str(
            await completion_is_current(db, ctx.toolbox.artifacts, ctx.toolbox.session_id, checked)
        )
    async with database.transaction() as db:
        artifact = await db.get(ArtifactRow, UUID(refs["artifact_id"]))
        artifact.availability = "remote_only"
    with pytest.raises(ValueError, match="真实、可读"):
        await update(ctx, {}, [step(status="done", completion=claim(refs))], "not-synced")
    other = await _build_context(database, unique_name + "-other")
    with pytest.raises(ValueError, match="真实、可读"):
        await update(other, {}, [step(status="done", completion=claim(refs))])


async def test_verification_completion_uses_current_code_record_without_confirming_more(
    database, unique_name
):
    from agent_workbench.contracts.enums import FindingStatus
    from agent_workbench.domain.findings import VerificationService
    from agent_workbench.persistence.tables import FindingRow

    ctx = await _build_context(database, unique_name)
    refs = await result_source(ctx.toolbox, "verification-input", True)
    async with database.transaction() as db:
        finding = FindingRow(
            session_id=ctx.toolbox.session_id,
            title="本地验证对象",
            evidence_refs=[refs["artifact_id"]],
        )
        db.add(finding)
        await db.flush()
        verification = await VerificationService(db).record(
            finding_id=finding.id,
            method_version="fixture-v1",
            conclusion=FindingStatus.CONFIRMED,
            input_evidence_refs=[refs["artifact_id"]],
        )
        verification_id = verification.id
        finding_id = finding.id
    completed = step(status="done", mode="achieve", completion=claim(refs))
    completed["completion_spec"].update(
        check_type="verification", verification_id=str(verification_id)
    )
    record = (await update(ctx, {}, [completed])).update["plan"][0]
    assert record["completion"]["check"]["method"] == "verification"
    async with database.transaction() as db:
        finding = await db.get(FindingRow, finding_id)
        finding.revision += 1
    async with database.session() as db:
        assert await completion_is_current(
            db, ctx.toolbox.artifacts, ctx.toolbox.session_id, record
        )


async def test_dispatch_rejects_all_same_batch_plan_writers_before_artifacts(database, unique_name):
    from agent_workbench.agent.middleware.command_guard import CommandGuardMiddleware
    from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware

    ctx = await _build_context(database, unique_name)
    batch = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "update_plan",
                "args": {"expected_plan_revision": 0, "steps_json": json.dumps([step(identifier)])},
                "id": identifier,
                "type": "tool_call",
            }
            for identifier in ("A", "C")
        ],
    )
    graph = create_agent(
        model=ScriptedChatModel(responses=[batch, final_text("合并后重试")]),
        tools=[update_plan],
        middleware=[CommandGuardMiddleware(), ToolDispatchMiddleware()],
        state_schema=AgentGraphState,
        context_schema=RunContext,
    )
    result = await graph.ainvoke(
        cast(Any, {"messages": [HumanMessage(content="检查同批竞争")]}), context=ctx
    )
    assert not result.get("plan")
    receipts = [item for item in result["messages"] if isinstance(item, ToolMessage)]
    assert len(receipts) == 2 and all("全部拒绝" in item.content for item in receipts)
    async with database.session() as db:
        assert (
            list(
                (
                    await db.execute(
                        select(ArtifactRow).where(ArtifactRow.session_id == ctx.toolbox.session_id)
                    )
                ).scalars()
            )
            == []
        )


async def test_checkpoint_restart_after_each_tool_keeps_one_external_operation(
    database, unique_name
):
    ctx = await _build_context(database, unique_name)
    effects = []

    @tool
    async def fixture_once(
        runtime: ToolRuntime[RunContext], tool_call_id: Annotated[str, InjectedToolCallId]
    ) -> str:
        """本地计数，模拟可能有副作用的外部行动。"""
        refs = await result_source(runtime.context.toolbox, tool_call_id, True, step_id=None)
        effects.append(refs["operation_id"])
        return json.dumps(refs)

    def done(messages):
        return tool_call(
            "update_plan",
            {
                "expected_plan_revision": 1,
                "steps_json": json.dumps(
                    [step(status="done", completion=claim(_last_tool_payload(messages)))]
                ),
                "rationale": "核对已取得的结果",
            },
            "close",
        )

    model = SteppedModel(
        steps=[
            lambda _: tool_call(
                "update_plan",
                {"expected_plan_revision": 0, "steps_json": json.dumps([step()])},
                "initial",
            ),
            lambda _: tool_call("fixture_once", {}, "stable-external-call"),
            done,
            lambda _: tool_call(
                "propose_completion",
                {
                    "reason": "goal_answered",
                    "goal_answer": "结果已核对",
                    "stop_rationale": "依据完整",
                    "expected_plan_revision": 2,
                },
                "end",
            ),
        ]
    )
    tools = [fixture_once, update_plan, propose_completion]
    config = {"configurable": {"thread_id": ctx.thread_id}, "recursion_limit": 100}
    for index in range(4):
        # 每个工具边界都关闭连接并重新构建官方图，禁止用另一条工具循环补跑。
        async with open_checkpointer_connection(database.settings) as connection:
            graph = create_agent(
                model=model,
                tools=tools,
                middleware=build_agent_middleware(
                    model,
                    ModelBinding(profile()),
                    database.settings,
                    tools,
                    default_price_catalog(),
                ),
                state_schema=AgentGraphState,
                context_schema=RunContext,
                checkpointer=AsyncPostgresSaver(connection),
                interrupt_after=["tools"],
            )
            payload = (
                cast(
                    Any,
                    new_graph_state(
                        session_id=ctx.toolbox.session_id, run_id=ctx.toolbox.run_id, objective="x"
                    ),
                )
                if index == 0
                else None
            )
            await graph.ainvoke(payload, cast(Any, config), context=ctx)
    assert len(effects) == 1
    async with open_checkpointer_connection(database.settings) as connection:
        saved = await AsyncPostgresSaver(connection).aget_tuple(cast(Any, config))
        assert saved is not None
        state = saved.checkpoint["channel_values"]
        assert state["plan_revision"] == 2 and state["plan"][0]["status"] == "done"
    # 独立进程只读取已提交状态并补投影，不持有原模型或原图对象，也不调用供应商。
    import os
    import subprocess
    import sys

    child = r"""
import asyncio, json, sys
from uuid import UUID
from agent_workbench.platform_compat import configure_event_loop_policy
configure_event_loop_policy()
from agent_workbench.config.settings import get_settings
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from agent_workbench.tools.plan_revisions import project_committed_plan
async def main():
    settings = get_settings()
    database = Database(settings)
    await database.start()
    try:
        async with open_checkpointer_connection(settings) as connection:
            saved = await AsyncPostgresSaver(connection).aget_tuple({"configurable": {"thread_id": sys.argv[1]}})
        count = await project_committed_plan(database, settings, UUID(sys.argv[2]), saved)
        values = saved.checkpoint["channel_values"]
        print(json.dumps({"revision": values["plan_revision"], "projected": count, "status": values["plan"][0]["status"]}))
    finally:
        await database.close()
asyncio.run(main())
"""
    completed = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-B", "-c", child, ctx.thread_id, str(ctx.toolbox.session_id)],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    restored = json.loads(completed.stdout.strip().splitlines()[-1])
    assert restored == {"revision": 2, "projected": 2, "status": "done"}
    assert (
        await project_committed_plan(database, database.settings, ctx.toolbox.session_id, saved)
        == 0
    )
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
            == 1
        )
