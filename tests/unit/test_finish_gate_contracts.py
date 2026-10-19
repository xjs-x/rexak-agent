"""P1-B：合同未收口时 FinishGate 不放行。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from agent_workbench.domain import exploration_lock, plan_contracts
from agent_workbench.runtime.stopping import FinishGate


@pytest.mark.asyncio
async def test_legacy_done_contract_blocks_finish(monkeypatch):
    async def _noop(*_args, **_kwargs):
        return None

    async def _rows(*_args, **_kwargs):
        return [
            SimpleNamespace(
                contract_id=uuid4(),
                contract_revision=1,
                state="legacy_done",
                acceptance_result_ref=None,
            )
        ]

    monkeypatch.setattr(exploration_lock, "lock_exploration", _noop)
    monkeypatch.setattr(plan_contracts, "load_contract_refs", _rows)
    gate = FinishGate(cast(Any, None))
    decision = await gate.evaluate(
        session_id=uuid4(),
        proposal={
            "reason": "goal_answered",
            "goal_answer": "已检查",
            "stop_rationale": "没有更多方向",
        },
        contract_refs=[
            {"contract_id": str(uuid4()), "contract_revision": 1, "content_hash": "a" * 64}
        ],
    )
    assert decision.decision == "continue"
    assert "open_contracts" in decision.blocking_objects
