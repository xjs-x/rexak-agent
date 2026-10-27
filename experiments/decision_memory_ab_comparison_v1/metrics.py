"""只读可观察指标；不根据关键词自动判语义 PASS，不改原结果。"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from experiments.decision_memory_live_v1.harness import TOOLS

A_OPERATION = "ab5afd5c-2030-48cf-b750-4bfd9bb71547"
A_ARTIFACT = "e00066fc-37ab-47bd-b7fc-69fa90216f66"
U_OPERATION = "3f3b012b-f123-45ca-b4fe-a44dfcfc47c5"


def usage(rows):
    names = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cached_tokens",
    )
    totals = dict.fromkeys(names, 0)
    missing = {name: [] for name in names}
    for row in rows:
        value = row.get("usage") or {}
        fields = {
            **value,
            "reasoning_tokens": (value.get("completion_tokens_details") or {}).get(
                "reasoning_tokens"
            ),
            "cached_tokens": (value.get("prompt_tokens_details") or {}).get("cached_tokens"),
        }
        for name in names:
            if fields.get(name) is None:
                missing[name].append(row["index"])
            else:
                totals[name] += fields[name]
    return {
        "request_count": len(rows),
        "reported_sums": totals,
        "complete_totals": {name: totals[name] if not missing[name] else None for name in names},
        "missing_fields_at_requests": missing,
        "currency_cost": "NOT_AVAILABLE",
    }


def summarize(result):
    calls = result["calls"]
    parsed = {
        c["id"]: c
        for m in result["messages"]
        if m["type"] == "ai"
        for c in m["data"].get("tool_calls", [])
    }
    messages = {
        m["data"].get("tool_call_id"): m["data"] for m in result["messages"] if m["type"] == "tool"
    }
    traces, fingerprints, duplicate_calls = [], Counter(), 0
    for call in calls:
        for native in (call.get("visible_response_message") or {}).get("tool_calls", []):
            name = (native.get("function") or {}).get("name")
            try:
                arguments = json.loads(native["function"]["arguments"])
            except (ValueError, TypeError, KeyError):
                arguments = None
            cid = native.get("id")
            schemas: dict[str, Any] = {t.name: t.tool_call_schema for t in TOOLS}
            try:
                if not isinstance(name, str):
                    raise ValueError("tool_name_required")
                resolved = schemas[name].model_validate(arguments).model_dump(mode="json")
                arguments_valid = True
            except (ValueError, TypeError, KeyError):
                resolved, arguments_valid = arguments, False
            paired = messages.get(cid)
            try:
                returned = json.loads(paired["content"]) if paired else None
            except (ValueError, TypeError):
                returned = None
            same = (
                cid in parsed
                and parsed[cid].get("name") == name
                and parsed[cid].get("args") == arguments
            )
            key = json.dumps([name, resolved], sort_keys=True, ensure_ascii=False)
            duplicate_calls += int(fingerprints[key] > 0)
            fingerprints[key] += 1
            success = bool(
                paired
                and paired.get("status") == "success"
                and (
                    not isinstance(returned, dict)
                    or (
                        not returned.get("error")
                        and returned.get("status") not in {"failed", "unknown"}
                    )
                )
            )
            visible = [
                later["index"]
                for later in calls
                if later["index"] > call["index"]
                and any(m.get("tool_call_id") == cid for m in later["request"]["messages"])
            ]
            data = returned.get("data", {}) if isinstance(returned, dict) else {}
            if not isinstance(data, dict):
                data = {}
            traces.append(
                {
                    "request": call["index"],
                    "phase": call["phase"],
                    "tool_call_id": cid,
                    "name": name,
                    "arguments": arguments,
                    "arguments_valid": arguments_valid,
                    "native_matches_parsed": same,
                    "toolmessage_exists": paired is not None,
                    "local_success": success,
                    "visible_in_later_requests": visible,
                    "artifact_id": data.get("artifact_id"),
                    "artifact_text_chars": len(data.get("text") or ""),
                    "artifact_complete": bool(data.get("text"))
                    and data.get("offset") == 0
                    and data.get("truncated") is False,
                }
            )
    ordinary = [t for t in traces if t["phase"] == "agent"]
    reads = [t for t in ordinary if t["name"] == "read_artifact"]
    actions = [t for t in ordinary if t["name"] == "simulate_check" and t["local_success"]]
    first_ref = next(
        (
            {"physical_request": c["index"], "phase": c["phase"]}
            for c in calls
            if A_OPERATION in json.dumps(c["request"]) or A_ARTIFACT in json.dumps(c["request"])
        ),
        None,
    )
    views = [
        {"physical_request": e["request_index"], "phase": e["phase"]}
        for e in result["evidence_exposure"]
        if any(
            v.get("artifact_id") == A_ARTIFACT and v.get("complete_text_view")
            for v in e["artifact_views"]
        )
    ]
    estimates = [
        {
            "phase": m.get("phase"),
            "model_call_id": m.get("model_call_id"),
            "estimated_input_tokens": (m.get("experiment_request_estimate") or {}).get(
                "estimated_input_tokens"
            ),
            "system_chars": m.get("system_message_chars"),
        }
        for m in result["manifests"]
    ]
    observed_estimates = [
        m["estimated_input_tokens"] for m in estimates if m["estimated_input_tokens"] is not None
    ]
    final = next(
        (
            c["visible_response_message"].get("content")
            for c in reversed(calls)
            if c["phase"] == "agent" and c.get("finish_reason") == "stop"
        ),
        None,
    )
    return {
        "case": result["case"],
        "status": result["status"],
        "stop_reason": result.get("stop_reason"),
        "database_name": result.get("database_name"),
        "natural_END": result["status"] == "completed" and not result["pending_nodes"],
        "physical_requests": len(calls),
        "ordinary_requests": sum(c["phase"] == "agent" for c in calls),
        "graph_review_requests": sum(c["phase"] == "graph_review" for c in calls),
        "ordinary_tool_rounds": sum(
            c["phase"] == "agent"
            and bool((c.get("visible_response_message") or {}).get("tool_calls"))
            for c in calls
        ),
        "native_tool_calls": len(traces),
        "executed_tool_calls": sum(t["toolmessage_exists"] for t in traces),
        "ordinary_tool_calls": len(ordinary),
        "maintenance_tool_calls": len(traces) - len(ordinary),
        "duplicate_identical_tool_calls": duplicate_calls,
        "search_history_calls": sum(t["name"] == "search_history" for t in ordinary),
        "read_artifact_calls": len(reads),
        "A_direct_read_calls": sum(
            t["artifact_id"] == A_ARTIFACT and t["local_success"] for t in reads
        ),
        "read_failures": sum(not t["local_success"] for t in reads),
        "non_A_artifact_reads": sum(
            t["artifact_id"] != A_ARTIFACT and t["local_success"] for t in reads
        ),
        "first_A_reference_request": first_ref,
        "first_complete_A_body_request": views[0] if views else None,
        "simulate_check_calls": len(actions),
        "A_Q_v1_simulations": sum(
            t["arguments"] == {"route": "A", "environment": "Q", "configuration": "v1"}
            for t in actions
        ),
        "A_Q_v2_simulations": sum(
            t["arguments"] == {"route": "A", "environment": "Q", "configuration": "v2"}
            for t in actions
        ),
        "unknown_U_replay_proposals": sum(t["arguments"].get("route") == "U" for t in actions),
        "U_local_reads": sum(
            t["name"] == "read_local_execution"
            and t["arguments"].get("operation_id") == U_OPERATION
            for t in ordinary
        ),
        "initial_system_chars": len(calls[0]["request"]["messages"][0]["content"])
        if calls
        else None,
        "max_input_estimate": max(observed_estimates) if observed_estimates else None,
        "request_estimates": estimates,
        "usage": {
            "agent": usage([c for c in calls if c["phase"] == "agent"]),
            "graph_review": usage([c for c in calls if c["phase"] == "graph_review"]),
            "all": usage(calls),
        },
        "tool_trace": traces,
        "final_answer": final,
        "semantic_scores": "REQUIRES_VISIBLE_EVIDENCE_REVIEW_NOT_AUTOMATIC",
    }
