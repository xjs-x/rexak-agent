"""合同 frontier：确定性排序，不做事实推断。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: 状态优先级只表达“现在最该处理什么”，不是事实置信度。
_STATE_RANK = {
    "running": 100,
    "ready": 80,
    "needs_review": 60,
    "blocked": 20,
    "proposed": 0,
    "stale": -1,
    "done": -1,
    "legacy_done": -1,
    "dropped": -1,
}
_ELIGIBLE = frozenset({"running", "ready", "needs_review"})


@dataclass(frozen=True, slots=True)
class FrontierItem:
    contract_id: str
    contract_revision: int
    state: str
    eligible: bool
    rank: int
    reasons: list[str] = field(default_factory=list)
    missing_conditions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "contract_revision": self.contract_revision,
            "state": self.state,
            "eligible": self.eligible,
            "rank": self.rank,
            "reasons": self.reasons,
            "missing_conditions": self.missing_conditions,
        }


def _value(contract: Any, key: str, default: Any = None) -> Any:
    if isinstance(contract, Mapping):
        return contract.get(key, default)
    return getattr(contract, key, default)


def _missing_conditions(contract: Any) -> list[str]:
    missing: list[str] = []
    for item in _value(contract, "requires_json", None) or _value(contract, "requires", None) or []:
        if not isinstance(item, Mapping):
            continue
        if item.get("satisfied") is False:
            missing.append(
                str(item.get("condition_key") or item.get("description") or "unknown_condition")
            )
    return missing


def build_frontier(
    contracts: Sequence[Any],
    *,
    limit: int = 5,
) -> list[FrontierItem]:
    """按状态与缺失条件排序；不把 blocked 伪装成可执行。"""
    items: list[FrontierItem] = []
    for contract in contracts:
        state = str(_value(contract, "state", "") or "")
        if state not in _STATE_RANK:
            continue
        missing = _missing_conditions(contract)
        # blocked 一律不可执行；条件解除后必须追加 ready 版本。
        eligible = state in _ELIGIBLE and not missing
        reasons: list[str] = []
        if state == "running":
            reasons.append("resume_running_contract")
        elif state == "ready":
            reasons.append("ready_contract")
        elif state == "needs_review":
            reasons.append("needs_review_contract")
        elif state == "blocked":
            reasons.append("blocked_contract_requires_condition")
        if missing:
            reasons.append("missing_conditions")
        if state in {"legacy_done", "stale", "done", "dropped"}:
            reasons.append("not_frontier_state")
        items.append(
            FrontierItem(
                contract_id=str(_value(contract, "contract_id", "")),
                contract_revision=int(_value(contract, "contract_revision", 0) or 0),
                state=state,
                eligible=eligible,
                rank=_STATE_RANK[state],
                reasons=reasons,
                missing_conditions=missing,
            )
        )
    items.sort(
        key=lambda item: (
            -int(item.eligible),
            -item.rank,
            len(item.missing_conditions),
            -item.contract_revision,
            item.contract_id,
        )
    )
    return items[: max(0, limit)]


def frontier_signature(items: Sequence[FrontierItem]) -> str:
    payload = [
        (item.contract_id, item.contract_revision, item.state, item.eligible)
        for item in items
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = ["FrontierItem", "build_frontier", "frontier_signature"]
