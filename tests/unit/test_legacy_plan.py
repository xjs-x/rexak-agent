"""P1-D：旧 PlanStep 迁移映射。"""

from __future__ import annotations

from uuid import uuid4

from agent_workbench.domain.legacy_plan import migrate_legacy_plan


def test_done_step_becomes_legacy_done_without_fake_acceptance():
    session_id = uuid4()
    contracts = migrate_legacy_plan(
        session_id,
        [
            {
                "step_id": "s1",
                "description": "旧步骤",
                "status": "done",
                "lead_ids": ["l1"],
                "evidence_refs": ["e1"],
            }
        ],
    )
    contract = contracts[0]
    assert contract.state == "legacy_done"
    assert contract.acceptance_result_ref is None
    assert contract.evidence_refs == ["e1"]
    assert contract.state_reason


def test_status_mapping_and_stable_ids():
    session_id = uuid4()
    plan = [
        {"step_id": "a", "description": "待办", "status": "pending"},
        {"step_id": "b", "description": "进行", "status": "in_progress"},
        {"step_id": "c", "description": "受阻", "status": "blocked", "rationale": "缺账号"},
        {"step_id": "d", "description": "放弃", "status": "dropped"},
    ]
    first = migrate_legacy_plan(session_id, plan)
    second = migrate_legacy_plan(session_id, plan)
    assert [item.state for item in first] == ["proposed", "running", "blocked", "dropped"]
    assert [item.contract_id for item in first] == [item.contract_id for item in second]
    assert first[2].state_reason == "缺账号"
