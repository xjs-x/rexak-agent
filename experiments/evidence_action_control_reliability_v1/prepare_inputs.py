"""只读增强决策前元数据，先封存输入和旧标签，再运行新检测器。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from experiments.evidence_action_control_v1.contracts import ActionSnapshot
from experiments.evidence_action_control_v1.replay import RESULT_PATHS, _object, _pairs

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def enrich(snapshot, request, request_ref, phase):
    """仅读取当前 request 的消息与已经返回的唯一配对，不看当前响应结果。"""
    messages = request.get("messages", [])
    evidence = {e.record_ref: e for e in snapshot.evidence}
    boundary = {**snapshot.boundary, "phase": phase}
    graph = {
        "revision": None,
        "visible_revision": None,
        "provenance_verified": False,
        "source_refs": [],
    }
    for index, call, message in _pairs(messages):
        payload = _object(message.get("content"))
        source = f"{request_ref}/messages/{index}:tool_call:{call['id']}"
        if call["name"] in {"get_graph", "update_graph"} and type(payload.get("revision")) is int:
            graph.update(revision=payload["revision"], provenance_verified=True)
            graph["source_refs"].append(source)
            if call["name"] == "get_graph":
                graph["visible_revision"] = payload["revision"]
                graph["visible_arguments"] = call["args"]
        if call["name"] == "read_artifact":
            data = payload.get("data") or {}
            ref = f"artifact:{call['args'].get('artifact_id', '')}"
            if (
                ref in evidence
                and isinstance(data, dict)
                and data.get("artifact_id") == call["args"].get("artifact_id")
            ):
                item = evidence[ref]
                conditions = {
                    **item.conditions,
                    "offset": data.get("offset"),
                    "returned_bytes": data.get("returned_bytes"),
                    "view": data.get("view"),
                    "read_arguments": call["args"],
                }
                evidence[ref] = item.model_copy(update={"conditions": conditions})
    boundary["graph_context"] = graph
    # 摘要提供“本会话历史归档入口”，只支持导航潜力，不证明未读归档包含 A。
    for index, message in enumerate(messages):
        text = str(message.get("content") or "")
        if message.get("role") not in {"user", "human"}:
            continue
        for match in re.finditer(r"摘要可检索来源 artifact_id=([0-9a-f-]{36})", text):
            ref = f"artifact:{match[1]}"
            if ref in evidence:
                item = evidence[ref]
                evidence[ref] = item.model_copy(
                    update={
                        "conditions": {
                            **item.conditions,
                            "navigation_role": "conversation_archive",
                            "navigation_refs": [f"{request_ref}/messages/{index}"],
                            "relevance_refs": snapshot.task.source_refs,
                            "relevance_limitation": "same_session_history_navigation_not_guaranteed_target_content",
                        }
                    }
                )
    boundary["semantics_adapter"] = "decision_before_metadata/2"
    return snapshot.model_copy(update={"boundary": boundary, "evidence": list(evidence.values())})


def build_inputs():
    original = json.loads(
        (ROOT / "inputs/original-development-snapshots.json").read_text(encoding="utf-8")
    )
    sources = {
        arm: json.loads(path.read_text(encoding="utf-8")) for arm, path in RESULT_PATHS.items()
    }
    enriched = []
    for raw in original:
        snapshot = ActionSnapshot.model_validate(raw)
        boundary = snapshot.boundary
        if boundary.get("origin") == "real_recorded_trajectory" or snapshot.snapshot_id.startswith(
            "real-"
        ):
            arm = boundary.get("arm") or snapshot.snapshot_id.split("-")[1]
            # 定位只用于取当前请求，不根据 ID 决定任何标签。
            candidate_id = snapshot.candidate.tool_call_id
            physical = next(
                c
                for c in sources[arm]["calls"]
                if any(
                    t.get("id") == candidate_id
                    for t in (c.get("visible_response_message") or {}).get("tool_calls", [])
                )
            )
            ref = snapshot.task.source_refs[0]
            snapshot = enrich(snapshot, physical["request"], ref, physical["phase"])
        enriched.append(snapshot.model_dump(mode="json"))
    boundary_rows = json.loads(
        (ROOT / "inputs/original-boundary-snapshots.json").read_text(encoding="utf-8")
    )
    return enriched, boundary_rows


def main():
    enriched, boundary_rows = build_inputs()
    write_once(ROOT / "inputs/development-snapshots.json", enriched)
    write_once(ROOT / "inputs/boundary-snapshots.json", boundary_rows)
    paths = [
        ROOT / "inputs" / name
        for name in (
            "original-development-snapshots.json",
            "original-boundary-snapshots.json",
            "evaluation_oracle.json",
            "development-snapshots.json",
            "boundary-snapshots.json",
        )
    ]
    paths += [Path(__file__), *RESULT_PATHS.values()]
    write_once(
        ROOT / "inputs/freeze.json",
        {
            "version": 2,
            "status": "INPUTS_AND_LABELS_FIXED_BEFORE_IMPROVED_DETECTOR",
            "dataset_role": "DEVELOPMENT_SET_ALREADY_SEEN_NOT_HELDOUT",
            "sample_count": len(enriched),
            "original_labels_unchanged": True,
            "detector_executed": False,
            "changes": "Only traceable current-request graph versions and summary archive navigation metadata added to the 16 real inputs. Original 26 synthetic snapshots unchanged.",
            "files": {str(p.relative_to(REPO)).replace("\\", "/"): sha(p) for p in paths},
        },
    )
    print(
        json.dumps(
            {
                "development_inputs_fixed": len(enriched),
                "boundaries": len(boundary_rows),
                "freeze_sha256": sha(ROOT / "inputs/freeze.json"),
            }
        )
    )


if __name__ == "__main__":
    main()
