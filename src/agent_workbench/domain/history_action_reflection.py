"""只发现可追溯执行重合的纯函数；不解释结果、不评价行动价值、不执行工具。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from agent_workbench.contracts.history_action_reflection import (
    Evidence,
    Execution,
    HistoricalExecution,
    Match,
    Observation,
    Policy,
    Snapshot,
    TriggerState,
)
from agent_workbench.domain.decision_memory import compare_dimensions, explicit_refs

REQUIRED = (
    "target",
    "url",
    "method",
    "configuration",
    "environment",
    "identity_id",
    "auth_revision",
    "privileges",
    "executor_id",
    "mapping_signature",
    "prerequisites",
    "request_digest",
)
# 规范类别只映射已检查的生产 HTTP 路径；其它包装器没有自动别名权限。
CONTRACTS = {"http_request": "executor_http/v1"}
NON_TEST_TOOLS = frozenset(
    {
        "get_graph",
        "read_artifact",
        "read_local_execution",
        "update_graph",
        "search_history",
        "get_job",
        "query_graph",
        "write_graph",
        "session_info",
    }
)
UNKNOWN = {"", "unknown", "unrecorded", "not_available", "not_exercised"}
HTTP_ARGUMENTS = frozenset(
    {
        "method",
        "url",
        "headers",
        "params",
        "body",
        "json_body",
        "identity",
        "follow_redirects",
        "verify_tls",
        "save_body_artifact",
        "form_fields",
        "multipart_parts",
        "csrf_source_url",
        "csrf_input_name",
        "csrf_destination",
        "csrf_target_name",
        "note",
    }
)


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def normalize_url(value: str) -> str:
    p = urlsplit(value)
    if p.scheme.lower() not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("unsupported_url")
    host = p.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if p.port is not None and (p.scheme.lower(), p.port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{p.port}"
    # 不排序 query、不解码路径、不合并重复参数、不删除 fragment。
    return urlunsplit((p.scheme.lower(), host, p.path or "/", p.query, p.fragment))


def request_signature(arguments: dict[str, Any]) -> str:
    value = dict(arguments)
    if not isinstance(value.get("url"), str) or not isinstance(value.get("method"), str):
        raise ValueError("missing_method_or_url")
    value["url"] = normalize_url(value["url"])
    value["method"] = value["method"].upper()
    return digest(value)


def _known(value: Any) -> bool:
    if value is None or (isinstance(value, str) and value.strip().lower() in UNKNOWN):
        return False
    if isinstance(value, dict):
        return all(_known(v) for v in value.values())
    if isinstance(value, list):
        return bool(value) and all(_known(v) for v in value)
    return True


def conditions(record: Execution) -> dict[str, Any]:
    values = {key: record.conditions.get(key) for key in REQUIRED}
    for key, value in list(values.items()):
        if not _known(value):
            values[key] = None
        elif key in {"target", "url"} and isinstance(value, str):
            try:
                values[key] = normalize_url(value)
            except ValueError:
                values[key] = None  # 旧抽象目标或非法 URL 不是可比较的完整 HTTP 资源。
        elif key == "method" and isinstance(value, str):
            values[key] = value.upper()
        elif key == "privileges" and isinstance(value, list):
            values[key] = sorted(set(value))
    return values


def semantic_issues(record: Execution) -> list[str]:
    issues = []
    if (
        CONTRACTS.get(record.tool_name) != record.semantic_contract
        or record.semantic_contract is None
    ):
        issues.append("tool_semantic_mapping_unverified")
    if not record.provenance_verified or record.origin != "recorder" or not record.source_refs:
        issues.append("execution_context_not_verified")
    values = conditions(record)
    for key in REQUIRED:
        if values[key] is None:
            issues.append(f"missing:{key}")
        if not record.condition_sources.get(key) or not set(record.condition_sources[key]) <= set(
            record.source_refs
        ):
            issues.append(f"condition_source_missing:{key}")
    if record.tool_name in CONTRACTS:
        if set(record.arguments) - HTTP_ARGUMENTS:
            issues.append("unsupported_http_parameters")
        for key in ("headers", "params"):
            value = record.arguments.get(key)
            if value is not None and (
                not isinstance(value, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
            ):
                issues.append(f"invalid_http_argument:{key}")
        for key in ("follow_redirects", "verify_tls", "save_body_artifact"):
            if key in record.arguments and type(record.arguments[key]) is not bool:
                issues.append(f"invalid_http_argument:{key}")
        for key in ("identity", "body", "note"):
            value = record.arguments.get(key)
            if value is not None and not isinstance(value, str):
                issues.append(f"invalid_http_argument:{key}")
        if any(
            record.arguments.get(key) is not None
            for key in (
                "form_fields",
                "multipart_parts",
                "csrf_source_url",
                "csrf_input_name",
                "csrf_target_name",
            )
        ):
            issues.append("dynamic_form_or_csrf_semantics_not_supported")
        try:
            signature = request_signature(record.arguments)
        except (TypeError, ValueError):
            issues.append("canonical_request_parameters_unavailable")
            return issues
        if values["request_digest"] != signature:
            issues.append("argument_digest_conflicts_with_context")
        if normalize_url(record.arguments["url"]) != values["url"]:
            issues.append("argument_url_conflicts_with_context")
        if record.arguments["method"].upper() != values["method"]:
            issues.append("argument_method_conflicts_with_context")
    return issues


def retrieve(snapshot: Snapshot) -> list[tuple[HistoricalExecution, list[str]]]:
    """宽检索只沿已有来源/目标/规范类别；候选被检出不代表重复。"""
    result = []
    current = snapshot.candidate
    references = explicit_refs(current.arguments) | set(current.related_operation_refs)
    for old in sorted(snapshot.history, key=lambda x: x.operation_ref or ""):
        if old.session_id != snapshot.session_id:
            continue
        reasons = []
        if old.operation_ref in references:
            reasons.append("explicit_source_relation")
        if current.logical_test_key and current.logical_test_key == old.logical_test_key:
            reasons.append("same_recorded_logical_test_key_only")
        target = current.conditions.get("target")
        if target and target == old.conditions.get("target"):
            reasons.append("same_recorded_target_navigation")
        if (
            current.semantic_contract
            and current.semantic_contract == old.semantic_contract
            and current.arguments.get("url") == old.arguments.get("url")
        ):
            reasons.append("same_contract_and_resource_navigation")
        if reasons:
            result.append((old, reasons))
    return result


def verified_evidence(
    snapshot: Snapshot, old: HistoricalExecution, policy: Policy
) -> list[Evidence]:
    counts = Counter(item.evidence_ref for item in snapshot.evidence)
    found = []
    for item in snapshot.evidence:
        if (
            counts[item.evidence_ref] != 1
            or item.evidence_ref not in old.evidence_refs
            or item.operation_ref != old.operation_ref
            or item.session_id != snapshot.session_id
            or not item.visible
            or not item.complete
            or not item.provenance_verified
            or not item.source_refs
            or item.content is None
            or item.sha256 is None
            or item.available_step >= snapshot.decision_step
            or item.source_tool_call_id in snapshot.same_batch_tool_call_ids
        ):
            continue
        permitted = (
            {"artifact"} if policy == "raw_visible_v1" else {"artifact", "execution_receipt"}
        )
        if (
            item.kind in permitted
            and hashlib.sha256(item.content.encode()).hexdigest() == item.sha256
        ):
            found.append(item)
    return sorted(found, key=lambda x: x.evidence_ref)


def _match(
    snapshot: Snapshot, old: HistoricalExecution, reasons: list[str], policy: Policy
) -> Match:
    left, right = conditions(old), conditions(snapshot.candidate)
    comparisons = compare_dimensions(
        left, right, sorted(set(old.source_refs + snapshot.candidate.source_refs))
    )
    common = [c.field for c in comparisons if c.status == "comparable_same"]
    differences = [c.field for c in comparisons if c.status == "changed"]
    missing = [c.field for c in comparisons if c.status == "insufficient"]
    problems = semantic_issues(old)
    if old.semantic_contract != snapshot.candidate.semantic_contract:
        differences.append("semantic_contract")
    if not old.operation_ref or not old.operation_ref.startswith("operation:"):
        problems.append("invalid_operation_identity_namespace")
    unresolved = old.status in {"unknown", "uncertain", "running", "accepted", "pending"}
    unresolved |= any(
        s in {"unknown", "uncertain", "running", "accepted", "pending"}
        for s in old.job_states.values()
    )
    if (
        old.status != "succeeded"
        or not old.job_refs
        or set(old.job_refs) != set(old.job_states)
        or any(state != "succeeded" for state in old.job_states.values())
        or old.finished_step is None
        or old.finished_step >= snapshot.decision_step
        or old.tool_call_id in snapshot.same_batch_tool_call_ids
    ):
        problems.append("no_predecision_completed_execution")
    evidence = verified_evidence(snapshot, old, policy)
    if not evidence:
        problems.append("no_traceable_visible_result_for_policy")
    if differences:
        problems.append("known_condition_changes")
    if missing:
        problems.append("decisive_conditions_incomplete")
    if sum(item.operation_ref == old.operation_ref for item in snapshot.history) != 1:
        problems.append("ambiguous_operation_records")
    return Match(
        operation_ref=old.operation_ref or "operation:unavailable",
        retrieval_reasons=reasons,
        compared_conditions=comparisons,
        common_conditions=common,
        differences=sorted(set(differences)),
        missing=missing,
        evidence_refs=[e.evidence_ref for e in evidence],
        nontrigger_reasons=sorted(set(problems)),
        unknown_execution=unresolved,
        high_confidence_overlap=not problems,
    )


def dedup_signature(snapshot: Snapshot, matches: list[Match], policy: Policy) -> str:
    """不含新 ToolCall/Operation ID；历史支持、内容哈希和任务版本有变化才可重新评估。"""
    evidence = {e.evidence_ref: e for e in snapshot.evidence}
    supports = [
        {
            "operation_ref": m.operation_ref,
            "evidence": [{"ref": r, "sha256": evidence[r].sha256} for r in m.evidence_refs],
        }
        for m in matches
        if m.high_confidence_overlap
    ]
    return digest(
        {
            "session": snapshot.session_id,
            "task": snapshot.task_version,
            "context": snapshot.context_version,
            "evidence_version": snapshot.evidence_version,
            "conditions": conditions(snapshot.candidate),
            "semantic_contract": snapshot.candidate.semantic_contract,
            "supports": sorted(supports, key=lambda x: x["operation_ref"]),
            "policy": policy,
        }
    )


def observe(snapshot: Snapshot, policy: Policy = "raw_visible_v1") -> Observation:
    summary = {
        "session_id": snapshot.session_id,
        "task_version": snapshot.task_version,
        "context_version": snapshot.context_version,
        "evidence_version": snapshot.evidence_version,
        "decision_step": snapshot.decision_step,
        "history_scanned": len(snapshot.history),
        "retest_intent": snapshot.retest_intent,
    }
    common = {
        "snapshot_id": snapshot.snapshot_id,
        "candidate_tool_call_id": snapshot.candidate.tool_call_id,
        "candidate_tool_name": snapshot.candidate.tool_name,
        "predecision_summary": summary,
        "policy": policy,
        "diagnostics": list(snapshot.limitations),
    }
    if snapshot.candidate.tool_name in NON_TEST_TOOLS:
        return Observation(
            **common,
            classification="NOT_IN_SCOPE",
            nontrigger_reasons=["not_a_supported_security_test"],
        )
    found = retrieve(snapshot)
    matches = [_match(snapshot, old, reasons, policy) for old, reasons in found]
    issues = semantic_issues(snapshot.candidate)
    if issues:
        return Observation(
            **common,
            classification="UNKNOWN_SEMANTICS",
            related_history=matches,
            nontrigger_reasons=issues,
        )
    if not matches:
        return Observation(
            **common,
            classification="NO_RELEVANT_HISTORY",
            nontrigger_reasons=["no_related_execution_in_session"],
        )
    if snapshot.retest_intent == "required":
        return Observation(
            **common,
            classification="RELATED_ONLY",
            related_history=matches,
            nontrigger_reasons=["structured_explicit_retest_request"],
        )
    eligible = [match for match in matches if match.high_confidence_overlap]
    if not eligible:
        return Observation(
            **common,
            classification="RELATED_ONLY",
            related_history=matches,
            nontrigger_reasons=sorted({r for m in matches for r in m.nontrigger_reasons}),
        )
    key = dedup_signature(snapshot, eligible, policy)
    duplicate = any(state.dedup_key == key for state in snapshot.previous_triggers)
    refs = sorted({r for match in eligible for r in match.evidence_refs})
    operations = sorted(match.operation_ref for match in eligible)
    draft = (
        f"当前候选与历史操作 {', '.join(operations)} 存在已核验的执行条件重合。"
        f"历史证据：{', '.join(refs)}。已核验重合记录中未发现已知关键条件差异。"
        "这只说明历史重合，不能确定本次行动的信息价值。请结合当前任务判断是否有新的测试目标、"
        "复测需求或重要条件变化；可以继续原计划，无需输出长篇反思报告。"
    )
    return Observation(
        **common,
        classification="TRIGGER_CANDIDATE",
        related_history=matches,
        trigger_reasons=[
            "verified_execution_contract_and_conditions_overlap",
            "completed_execution_with_traceable_result",
        ],
        nontrigger_reasons=["same_context_already_triggered_or_agent_responded"]
        if duplicate
        else [],
        original_evidence_refs=refs,
        dedup_key=key,
        emit_shadow_event=not duplicate,
        duplicate_suppressed=duplicate,
        neutral_message_draft=draft,
    )


def observe_input(value: object, policy: Policy = "raw_visible_v1") -> Observation:
    """观察层发生错误时显式记录；不持有 handler/请求对象，无法改变原调用。"""
    if policy not in {"raw_visible_v1", "receipt_allowed_v1"}:
        return Observation(
            snapshot_id="invalid_policy",
            candidate_tool_call_id="unavailable",
            candidate_tool_name="unavailable",
            predecision_summary={},
            classification="UNKNOWN_SEMANTICS",
            policy="raw_visible_v1",
            nontrigger_reasons=["unsupported_trigger_policy"],
            diagnostics=["observation_error:unsupported_policy"],
        )
    snapshot = None
    try:
        raw = value.model_dump(mode="json") if isinstance(value, Snapshot) else value
        canonical(raw)
        snapshot = Snapshot.model_validate(raw)
        return observe(snapshot, policy)
    except Exception as exc:
        return Observation(
            snapshot_id=snapshot.snapshot_id if snapshot is not None else "invalid_snapshot",
            candidate_tool_call_id=snapshot.candidate.tool_call_id
            if snapshot is not None
            else "unavailable",
            candidate_tool_name=snapshot.candidate.tool_name
            if snapshot is not None
            else "unavailable",
            predecision_summary={},
            classification="UNKNOWN_SEMANTICS",
            policy=policy,
            nontrigger_reasons=["observation_failed_no_trigger"],
            diagnostics=[f"observation_error:{type(exc).__name__}"],
        )


def record_trigger(observation: Observation) -> TriggerState | None:
    if observation.emit_shadow_event and observation.dedup_key:
        return TriggerState(
            dedup_key=observation.dedup_key,
            status="triggered",
            source_ref=f"shadow:{observation.snapshot_id}",
        )
    return None


def record_feedback(state: TriggerState, status: str) -> TriggerState:
    if status not in {"feedback_recorded", "agent_continued", "agent_changed"}:
        raise ValueError("unsupported_feedback_state")
    return TriggerState.model_validate({**state.model_dump(), "status": status})
