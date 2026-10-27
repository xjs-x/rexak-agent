"""既有决策前快照只读适配；旧开发/旧留出全部标为已见，不调用旧执行器。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from experiments.evidence_action_control_v1.contracts import ActionSnapshot

from .contracts import Evidence, Execution, HistoricalExecution, Snapshot

REPO = Path(__file__).resolve().parents[2]
SOURCES = {
    "seen-development": "experiments/evidence_action_control_reliability_v1/inputs/development-snapshots.json",
    "seen-boundary": "experiments/evidence_action_control_reliability_v1/inputs/boundary-snapshots.json",
    "seen-former-holdout": "experiments/evidence_action_control_reliability_v1/heldout/inputs.json",
}


def _conditions(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    # 旧抽象 route/entry 不伪装成已核实的完整 HTTP URL/规范请求 digest。
    result.pop("request_digest", None)
    return result


def adapt(old: ActionSnapshot, source_ref: str) -> Snapshot:
    candidate = old.candidate
    view = Execution(
        tool_call_id=candidate.tool_call_id,
        tool_name=candidate.name,
        arguments=candidate.arguments,
        semantic_contract="executor_http/v1" if candidate.name == "http_request" else None,
        conditions=_conditions(candidate.conditions),
        source_refs=candidate.source_refs,
        logical_test_key=candidate.information_key,
        related_operation_refs=[candidate.operation_ref] if candidate.operation_ref else [],
        # 旧 DTO 没有逐维权威条件来源和完整已解析参数绑定，不自行提升。
        provenance_verified=False,
    )
    history = []
    for record in old.historical_actions:
        history.append(
            HistoricalExecution(
                tool_call_id=f"legacy:{record.record_ref}",
                tool_name=record.tool_name,
                arguments={},
                operation_ref=record.record_ref,
                session_id="legacy-fixed-session",
                semantic_contract="executor_http/v1"
                if record.tool_name == "http_request"
                else None,
                conditions=_conditions(record.conditions),
                source_refs=record.source_refs,
                logical_test_key=record.information_key,
                status=record.status,
                job_refs=record.job_refs,
                job_states=record.job_states,
                evidence_refs=record.artifact_refs,
                finished_step=None,
                provenance_verified=False,
                origin="recorder"
                if record.origin_kind == "execution_observation"
                else record.origin_kind,
            )
        )
    evidence = [
        Evidence(
            evidence_ref=e.record_ref,
            operation_ref=e.operation_ref or "operation:unavailable",
            session_id="legacy-fixed-session",
            kind="artifact" if e.kind == "artifact_body" else "model_claim",
            content=e.content if e.visible else None,
            sha256=e.sha256,
            visible=e.visible,
            complete=e.complete,
            provenance_verified=e.provenance_verified,
            available_step=1,
            source_refs=e.visibility_refs,
        )
        for e in old.evidence
    ]
    return Snapshot(
        snapshot_id=old.snapshot_id,
        session_id="legacy-fixed-session",
        task_version="legacy-task/1",
        context_version=old.snapshot_id,
        evidence_version=source_ref,
        decision_step=2,
        candidate=view,
        history=history,
        evidence=evidence,
        retest_intent="unspecified",
        same_batch_tool_call_ids=old.boundary.get("same_batch_tool_call_ids_excluded", []),
        limitations=[
            *old.limitations,
            source_ref,
            "旧快照无已核实规范 HTTP 参数/逐维来源/完成时点，不补造；不满足新触发合同。",
            "旧任务自由文本不经关键词推断复测意图。",
        ],
    )


def load_seen() -> dict[str, list[Snapshot]]:
    return {
        name: [
            adapt(ActionSnapshot.model_validate(row), f"{path}#/{index}")
            for index, row in enumerate(json.loads((REPO / path).read_text(encoding="utf-8")))
        ]
        for name, path in SOURCES.items()
    }
