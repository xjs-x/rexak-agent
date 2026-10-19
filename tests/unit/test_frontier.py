"""P1：frontier 只排序，不把 blocked/stale 伪装成可执行。"""

from __future__ import annotations

from agent_workbench.domain.frontier import build_frontier, frontier_signature


def _contract(cid: str, state: str, missing: bool = False):
    return {
        "contract_id": cid,
        "contract_revision": 1,
        "state": state,
        "requires_json": [
            {"condition_key": "c1", "satisfied": not missing},
        ],
    }


def test_running_and_ready_come_before_review_and_blocked():
    items = build_frontier(
        [
            _contract("blocked", "blocked", True),
            _contract("review", "needs_review"),
            _contract("ready", "ready"),
            _contract("running", "running"),
            _contract("done", "done"),
        ]
    )
    assert [item.contract_id for item in items[:3]] == ["running", "ready", "review"]
    assert items[0].eligible is True
    assert next(item for item in items if item.contract_id == "blocked").eligible is False
    assert next(item for item in items if item.contract_id == "done").eligible is False


def test_missing_condition_makes_ready_ineligible():
    items = build_frontier([_contract("ready", "ready", missing=True)])
    assert items[0].eligible is False
    assert items[0].missing_conditions == ["c1"]


def test_signature_changes_when_state_changes():
    a = build_frontier([_contract("c1", "ready")])
    b = build_frontier([_contract("c1", "running")])
    assert frontier_signature(a) != frontier_signature(b)
