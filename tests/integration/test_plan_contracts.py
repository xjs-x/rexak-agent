"""P1-A：不可变 TaskContract 版本与状态迁移。"""

from __future__ import annotations

import inspect
from typing import Any, cast
from uuid import uuid4

import pytest
from langchain.tools import ToolRuntime
from sqlalchemy import select

from agent_workbench.contracts.plan import (
    AcceptanceSpec,
    ContractCondition,
    ContractState,
    TaskContractCreate,
)
from agent_workbench.domain.exploration_review import build_review_inventory
from agent_workbench.domain.plan_contracts import (
    ContractMutationConflict,
    ContractValidationError,
    append_contract,
    append_transition,
    latest_contract,
)
from agent_workbench.persistence.tables import PlanContractRow
from agent_workbench.tools.task import update_plan
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration


def _contract(
    contract_id, mutation_id, *, question="确认入口是否存在", state: ContractState = "proposed", **extra
):
    return TaskContractCreate(
        contract_id=str(contract_id),
        question=question,
        acceptance=AcceptanceSpec(evaluator_id="test", evaluator_version="v1"),
        requires=[ContractCondition(description="目标可达", satisfied=True)],
        mutation_id=str(mutation_id),
        state=state,
        **extra,
    )


async def test_contract_versions_are_append_only_and_mutation_idempotent(database, unique_name):
    context = await context_for(database, unique_name)
    contract_id = uuid4()
    mutation = uuid4()
    async with database.transaction() as db:
        first = await append_contract(
            db,
            session_id=context.toolbox.session_id,
            run_id=context.toolbox.run_id,
            contract=_contract(contract_id, mutation),
        )
        again = await append_contract(
            db,
            session_id=context.toolbox.session_id,
            run_id=context.toolbox.run_id,
            contract=_contract(contract_id, mutation),
        )
        assert first.contract_revision == 1
        assert again.contract_revision == 1
        with pytest.raises(ContractMutationConflict):
            await append_contract(
                db,
                session_id=context.toolbox.session_id,
                run_id=context.toolbox.run_id,
                contract=_contract(contract_id, mutation, question="改写内容"),
            )
        second = await append_contract(
            db,
            session_id=context.toolbox.session_id,
            run_id=context.toolbox.run_id,
            contract=_contract(contract_id, uuid4(), question="补充问题"),
        )
        assert second.contract_revision == 2
    async with database.session() as db:
        rows = (
            await db.execute(
                select(PlanContractRow).where(PlanContractRow.contract_id == contract_id)
            )
        ).scalars().all()
    assert [row.contract_revision for row in rows] == [1, 2]


async def test_contract_transition_is_versioned_and_validated(database, unique_name):
    context = await context_for(database, unique_name)
    contract_id = uuid4()
    async with database.transaction() as db:
        await append_contract(
            db,
            session_id=context.toolbox.session_id,
            run_id=context.toolbox.run_id,
            contract=_contract(contract_id, uuid4()),
        )
        with pytest.raises(ContractValidationError):
            await append_transition(
                db,
                session_id=context.toolbox.session_id,
                contract_id=contract_id,
                to_state="done",
                mutation_id=uuid4(),
                run_id=context.toolbox.run_id,
            )
        ready = await append_transition(
            db,
            session_id=context.toolbox.session_id,
            contract_id=contract_id,
            to_state="ready",
            mutation_id=uuid4(),
            run_id=context.toolbox.run_id,
            state_reason="条件已满足",
        )
        assert ready.contract_revision == 2
        assert ready.state == "ready"
        latest = await latest_contract(db, context.toolbox.session_id, contract_id)
        assert latest is not None and latest.contract_revision == 2


async def test_contract_parent_must_exist_at_exact_revision(database, unique_name):
    context = await context_for(database, unique_name)
    child = uuid4()
    async with database.transaction() as db:
        with pytest.raises(ContractValidationError):
            await append_contract(
                db,
                session_id=context.toolbox.session_id,
                run_id=context.toolbox.run_id,
                contract=_contract(
                    child,
                    uuid4(),
                    parent_contract_id=str(uuid4()),
                    parent_contract_revision=1,
                ),
            )


async def _call(
    tool_obj: Any,
    context: Any,
    args: dict[str, Any],
    call_id: str,
    state: dict[str, Any] | None = None,
) -> Any:
    runtime = ToolRuntime(
        state=state or {},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id=call_id,
        store=None,
    )
    function = cast(Any, tool_obj).coroutine
    if "tool_call_id" in inspect.signature(function).parameters:
        args = {**args, "tool_call_id": call_id}
    return await function(runtime, **args)


async def test_update_plan_appends_contract_and_activates_exact_refs(database, unique_name):
    context = await context_for(database, unique_name)
    contract_id = uuid4()
    payload = _contract(contract_id, uuid4()).model_dump(mode="json")
    result = await _call(
        update_plan,
        context,
        {
            "contracts_json": [payload],
            "activate_contract_ids_json": [str(contract_id)],
            "rationale": "建立合同工作集",
        },
        "plan-contract-1",
    )
    assert result.update is not None
    refs = result.update["contract_refs"]
    assert refs[0]["contract_id"] == str(contract_id)
    assert refs[0]["contract_revision"] == 1
    assert result.update["active_contract_ids"] == [str(contract_id)]
    assert result.update["plan_revision"] == 1
    with pytest.raises(ValueError):
        await _call(
            update_plan,
            context,
            {
                "steps_json": [
                    {"step_id": "s1", "description": "旧计划", "status": "pending"}
                ]
            },
            "plan-contract-legacy-write",
            state=result.update,
        )
    with pytest.raises(ValueError):
        await _call(
            update_plan,
            context,
            {
                "steps_json": [
                    {"step_id": "s1", "description": "旧计划", "status": "pending"}
                ]
            },
            "plan-contract-legacy-write",
            state=result.update,
        )


async def test_review_inventory_uses_contracts_instead_of_old_plan(database, unique_name):
    context = await context_for(database, unique_name)
    contract_id = uuid4()
    async with database.transaction() as db:
        row = await append_contract(
            db,
            session_id=context.toolbox.session_id,
            run_id=context.toolbox.run_id,
            contract=_contract(contract_id, uuid4(), state="legacy_done"),
        )
        inventory = await build_review_inventory(
            db,
            context.toolbox.session_id,
            plan=[],
            contracts=[row],
        )
    assert inventory.unreviewed_count >= 1
    assert any(item.kind == "contract" for item in inventory.items)
    assert not any(item.kind == "plan_step" for item in inventory.items)
