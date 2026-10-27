"""Trajectory statistics plus a deliberately separate human quality interface."""

from __future__ import annotations

import json
from pathlib import Path

from .protocol import review_check


def summarize(
    trace: dict,
    receipts: list[dict],
    annotation: dict | None = None,
    *,
    verified_trace_hash: str | None = None,
) -> dict:
    deliveries = trace["deliveries"]
    positions = []
    for index, messages in enumerate(trace["model_inputs"]):
        if any("_host_history_reference" in str(m.get("content")) for m in messages):
            positions.append(index)
    calls = trace["tool_calls"]
    by_id = {c["id"]: i for i, c in enumerate(calls)}
    after_ids = []
    for d in deliveries:
        at = by_id.get(d["tool_call_id"])
        if at is not None:
            after_ids.extend(c["id"] for c in calls[at + 1 :])
    rows = [r for r in receipts if r["run"] == trace["identity"]["run"]]
    usage = [json.loads(r["usage"]) for r in rows if r.get("usage")]
    complete_usage = len(usage) == len(rows) and bool(rows)
    result = {
        "task": trace["task"],
        "arm": trace["arm"],
        "identity": trace["identity"],
        "candidate_count": sum(
            o["classification"] == "TRIGGER_CANDIDATE" for o in trace["observations"]
        ),
        "notice_claims": len(deliveries),
        "actual_input_positions": positions,
        "post_notice_tool_call_ids": sorted(set(after_ids)),
        "has_post_notice_decision_opportunity": bool(positions),
        "has_post_notice_test": bool(after_ids),
        "first_triggering_call_saved": False,
        "native_usage_origin": trace["usage_origin"],
        "physical_requests": len(rows),
        "ordinary": sum(r["kind"] == "ordinary" for r in rows),
        "maintenance": sum(r["kind"] == "maintenance" for r in rows),
        "prompt_tokens": sum(u["prompt_tokens"] for u in usage)
        if complete_usage
        else "NOT_AVAILABLE",
        "completion_tokens": sum(u["completion_tokens"] for u in usage)
        if complete_usage
        else "NOT_AVAILABLE",
        "reasoning_tokens": sum(u["completion_tokens_details"]["reasoning_tokens"] for u in usage)
        if complete_usage
        and all("reasoning_tokens" in u.get("completion_tokens_details", {}) for u in usage)
        else "NOT_AVAILABLE",
        "cache_read_tokens": sum(u["prompt_tokens_details"]["cached_tokens"] for u in usage)
        if complete_usage
        and all("cached_tokens" in u.get("prompt_tokens_details", {}) for u in usage)
        else "NOT_AVAILABLE",
        "cache_write_tokens": "NOT_AVAILABLE",
        "currency": "NOT_AVAILABLE",
        "tool_execution_count": len(calls),
        "elapsed_ms": trace["elapsed_ms"],
        "shadow_timings": [o.get("timing_ms", {}) for o in trace["observations"]],
        "termination": trace["end"],
        "faults": [r["state"] for r in rows if r["state"] != "complete"],
        "task_quality": "NOT_EVALUABLE",
        "false_intervention": "NOT_EVALUABLE",
        "worthless_subsequent_repeats": "NOT_EVALUABLE",
        "necessary_action_omissions": "NOT_EVALUABLE",
    }
    if annotation is not None:
        gate = review_check(annotation.get("oracle_review", {}))
        result["annotation_acceptance"] = gate
        if (
            gate["status"] == "PASS"
            and verified_trace_hash is not None
            and annotation.get("trace_sha256") == verified_trace_hash
            and annotation.get("adjudicated") is True
        ):
            boundary_id = annotation.get("analysis_boundary_tool_call_id")
            worthless = annotation["worthless_subsequent_repeat_ids"]
            if worthless:
                boundary = by_id.get(boundary_id)
                if boundary is None or not set(worthless).issubset(
                    {c["id"] for c in calls[boundary + 1 :]}
                ):
                    raise ValueError("worthless_repeats_must_be_strictly_after_matched_boundary")
                if trace["arm"] == "C" and (
                    not deliveries or boundary_id != deliveries[0]["tool_call_id"]
                ):
                    raise ValueError("reflection_boundary_must_match_actual_delivery")
            required = set(annotation["necessary_action_ids"])
            performed = set(annotation["performed_action_ids"])
            missed = sorted(required - performed)
            result.update(
                task_quality=annotation["task_quality"],
                necessary_action_omissions=missed,
                worthless_subsequent_repeats=annotation["worthless_subsequent_repeat_ids"],
                false_intervention="POSSIBLE_REQUIRES_CAUSAL_REVIEW"
                if missed and positions
                else "NOT_ESTABLISHED",
            )
            if (
                missed
                and positions
                and annotation.get("hint_causality_adjudication") == "SUPPORTED"
            ):
                result["false_intervention"] = "ADJUDICATED_SUPPORTED"
    return result


def annotation_template(trace_hash: str) -> dict:
    return {
        "trace_sha256": trace_hash,
        "adjudicated": False,
        "oracle_review": {},
        "necessary_action_ids": [],
        "performed_action_ids": [],
        "worthless_subsequent_repeat_ids": [],
        "analysis_boundary_tool_call_id": None,
        "matched_comparison_boundary_basis": None,
        "task_quality": "NOT_EVALUABLE",
        "completion": None,
        "incorrect_security_conclusion": None,
        "evidence_read_toolmessage_and_request_refs": [],
        "final_claim_support_refs": [],
        "hint_causality_adjudication": "NOT_EVALUABLE",
        "disputes": [],
        "environment_failure_excluded_from_decision_failure": True,
    }


def read_annotation(path: Path, trace_path: Path) -> dict:
    import hashlib

    annotation = json.loads(path.read_text(encoding="utf-8"))
    if annotation.get("trace_sha256") != hashlib.sha256(trace_path.read_bytes()).hexdigest():
        raise ValueError("annotation_trace_hash_mismatch")
    return annotation


def main():
    import argparse

    from .protocol import sha

    parser = argparse.ArgumentParser(
        description="Read-only trajectory statistics and reviewed annotations"
    )
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--annotation", type=Path)
    args = parser.parse_args()
    trace = json.loads(args.trace.read_text(encoding="utf-8"))
    receipts = json.loads(args.ledger.read_text(encoding="utf-8"))["rows"]
    annotation = read_annotation(args.annotation, args.trace) if args.annotation else None
    print(
        json.dumps(
            summarize(trace, receipts, annotation, verified_trace_hash=sha(args.trace)),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
