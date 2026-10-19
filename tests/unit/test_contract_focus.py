"""P1-B：合同状态投影为焦点，不复制合同正文。"""

from __future__ import annotations

from agent_workbench.agent.focus import contract_steps, resolve_focus


def test_contract_steps_map_states_and_single_running_becomes_focus():
    steps = contract_steps(
        [
            {"contract_id": "c1", "question": "确认入口", "state": "running", "lead_ids": ["l1"]},
            {"contract_id": "c2", "question": "补充证据", "state": "ready"},
        ]
    )
    assert [step["status"] for step in steps] == ["in_progress", "pending"]
    focus, hint = resolve_focus(steps, None, None)
    assert hint is None
    assert focus is not None
    assert focus["step_id"] == "c1"
    assert focus["lead_id"] == "l1"
    assert focus.get("source") == "plan_inferred"


def test_legacy_done_contract_never_becomes_running_focus():
    steps = contract_steps(
        [{"contract_id": "c1", "question": "旧完成", "state": "legacy_done"}]
    )
    assert steps[0]["status"] == "blocked"
    focus, _ = resolve_focus(steps, None, None)
    assert focus is None
