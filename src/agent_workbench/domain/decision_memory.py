"""同 Session 的本地只读关联和保守比较；不调用模型、工具或 Executor。"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import select

from agent_workbench.contracts.decision_memory import (
    ConditionDifference,
    DecisionBlackboard,
    DecisionInput,
    MemorySnapshot,
    MemoryUnit,
)
from agent_workbench.contracts.identity import ScopeSnapshot
from agent_workbench.contracts.planning import http_endpoint, planning_digest
from agent_workbench.domain.planning_applicability import condition_snapshot
from agent_workbench.domain.scope import ScopePolicy, ScopeViolation
from agent_workbench.domain.session_graph import read_graph
from agent_workbench.evidence.redaction import redact_api_value
from agent_workbench.persistence.tables import (
    ArtifactRow,
    ExecutionResourceRow,
    FindingRow,
    IdentityRow,
    LeadRow,
    SessionRow,
    ToolJobRow,
    ToolOperationRow,
)

_REF = re.compile(
    r"\b(operation|artifact|job|entity|attempt|state|lead|finding):([0-9a-fA-F-]{36})\b"
)


def explicit_refs(value: Any) -> set[str]:
    """只识别带命名空间的引用；不从自由文本猜目标、身份或攻击意图。"""
    if isinstance(value, str):
        return {f"{kind}:{UUID(raw)}" for kind, raw in _REF.findall(value) if valid_uuid(raw)}
    if isinstance(value, dict):
        refs = set().union(*(explicit_refs(v) for v in value.values())) if value else set()
        if value.get("kind") in {"operation", "artifact"} and valid_uuid(value.get("id")):
            refs.add(f"{value['kind']}:{UUID(str(value['id']))}")
        return refs
    if isinstance(value, list | tuple):
        return set().union(*(explicit_refs(v) for v in value)) if value else set()
    return set()


def valid_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
    except ValueError:
        return False
    return True


def endpoint(value: Any) -> str | None:
    try:
        return http_endpoint(str(value)) if value else None
    except ValueError:
        return None


def safe(value: Any) -> Any:
    """仅用于已筛选的字段；永不传入原 HTTP/命令正文或隐藏推理。"""
    return redact_api_value(value)


def recorded_refs(values: Any, data: MemorySnapshot) -> set[str]:
    """兼容既有 evidence_refs 的裸UUID：仅在本地元数据唯一命中时补命名空间。"""
    result = explicit_refs(values)
    known = [a["record_ref"] for a in data.artifacts] + [o["record_ref"] for o in data.operations]
    for value in values if isinstance(values, list) else []:
        if not isinstance(value, str) or not valid_uuid(value):
            continue
        matches = [ref for ref in known if ref.partition(":")[2] == str(UUID(value))]
        if len(matches) == 1:
            result.add(matches[0])
    return result


def compare_dimensions(
    old: dict[str, Any], new: dict[str, Any], refs: list[str]
) -> list[ConditionDifference]:
    return [
        ConditionDifference(
            field=key,
            status="insufficient"
            if old.get(key) is None or new.get(key) is None
            else "comparable_same"
            if old[key] == new[key]
            else "changed",
            old_value=safe(old.get(key)),
            new_value=safe(new.get(key)),
            source_refs=refs,
        )
        for key in sorted(old.keys() | new.keys())
    ]


async def read_snapshot(db: Any, request: DecisionInput, artifact_store: Any) -> MemorySnapshot:
    """由调用方在单一 READ ONLY / REPEATABLE READ 事务中执行。无同步/写回。"""
    sid = UUID(request.session_id)
    task = await db.get(SessionRow, sid)
    if task is None or task.status == "deleted":
        raise ValueError("decision_memory_session_unavailable")
    scope = ScopeSnapshot.model_validate(task.scope_snapshot)
    policy = ScopePolicy(scope)
    operations = list(
        (
            await db.scalars(
                select(ToolOperationRow)
                .where(ToolOperationRow.session_id == sid)
                .order_by(ToolOperationRow.created_at, ToolOperationRow.operation_id)
            )
        ).all()
    )
    jobs: dict[str, list[dict[str, Any]]] = {}
    for job in (
        await db.scalars(
            select(ToolJobRow)
            .join(ToolOperationRow, ToolOperationRow.operation_id == ToolJobRow.operation_id)
            .where(ToolOperationRow.session_id == sid)
        )
    ).all():
        jobs.setdefault(str(job.operation_id), []).append(
            {
                "record_ref": f"job:{job.job_id}",
                "last_observed_state": job.state,
                "sync_status": job.sync_status,
                "remote_revision": job.remote_revision,
                "last_synced_at": str(job.last_synced_at) if job.last_synced_at else None,
                "executor_id": job.executor_id,
            }
        )
    resources: dict[str, list[dict[str, Any]]] = {}
    for resource in (
        await db.scalars(select(ExecutionResourceRow).where(ExecutionResourceRow.session_id == sid))
    ).all():
        resources.setdefault(str(resource.origin_operation_id), []).append(
            {
                "record_ref": f"resource:{resource.resource_id}",
                "state": resource.state,
                "sync_status": resource.sync_status,
                "executor_id": resource.executor_id,
                "exit_code": resource.exit_code,
                "last_checked_at": str(resource.last_checked_at),
            }
        )
    op_ids = {str(op.operation_id) for op in operations}
    operation_urls = {
        str(op.operation_id): (op.execution_context or {}).get("url") for op in operations
    }
    artifacts = []
    for row in (await db.scalars(select(ArtifactRow).where(ArtifactRow.session_id == sid))).all():
        available = row.availability
        if row.sensitivity == "secret":
            available = "secret_not_readable"
        elif row.source_operation_id and str(row.source_operation_id) not in op_ids:
            available = "origin_unavailable"
        elif available == "available":
            try:
                if artifact_store is None or not artifact_store.exists(row.relative_key):
                    available = "file_unavailable"
            except OSError:
                available = "file_unreadable"
        artifacts.append(
            {
                "record_ref": f"artifact:{row.id}",
                "source_operation_ref": f"operation:{row.source_operation_id}"
                if row.source_operation_id and str(row.source_operation_id) in op_ids
                else None,
                "availability": available,
                "sha256": row.sha256,
                "truncated": row.truncated,
                "media_type": row.media_type,
                "captured_at": str(row.captured_at),
                # 不读正文或摘要；目录不表示正文足以支持结论。
            }
        )
        source_url = row.source_url or operation_urls.get(str(row.source_operation_id))
        artifacts[-1]["access"] = "target_unrecorded"
        if source_url:
            try:
                policy.check_execution_url(str(source_url))
                artifacts[-1]["access"] = "within_current_scope"
            except (ScopeViolation, ValueError):
                artifacts[-1]["access"] = "outside_current_scope"
    op_views = []
    for row in operations:
        context = row.execution_context or {}
        url = context.get("url")
        access = "target_unrecorded"
        if url:
            try:
                policy.check_execution_url(str(url))
                access = "within_current_scope"
            except (ScopeViolation, ValueError):
                access = "outside_current_scope"
        result = (row.result_snapshot or {}).get("data") or {}
        op_views.append(
            {
                "record_ref": f"operation:{row.operation_id}",
                "run_id": str(row.run_id),
                "tool_call_id": row.tool_call_id,
                "tool_name": row.tool_name,
                "operation_status": row.status,
                "result_artifact_refs": [
                    a["record_ref"]
                    for a in artifacts
                    if a["record_ref"].partition(":")[2] in (row.result_artifact_ids or [])
                ],
                "created_at": str(row.created_at),
                "updated_at": str(row.updated_at),
                "access": access,
                "context": safe(
                    {
                        k: context.get(k)
                        for k in (
                            "step_id",
                            "lead_id",
                            "lead_ids",
                            "plan_revision",
                            "authentication",
                            "identity_auth_revision",
                            "target_mapping_signature",
                            "source_condition_snapshots",
                        )
                    }
                ),
                "dimensions": {
                    "method": str(context["method"]).upper() if context.get("method") else None,
                    "endpoint": endpoint(url),
                    "identity_id": str(row.identity_id) if row.identity_id else None,
                    "identity_auth_revision": context.get("identity_auth_revision"),
                    "authentication": context.get("authentication"),
                    "target_mapping_signature": context.get("target_mapping_signature"),
                    "scope_revision": row.scope_revision,
                    "policy_revision": row.policy_revision,
                },
                "result": safe(
                    {
                        k: result[k]
                        for k in ("status_code", "exit_code", "delivery_state", "error_category")
                        if k in result and isinstance(result[k], str | int | bool | type(None))
                    }
                ),
                "jobs": sorted(jobs.get(str(row.operation_id), []), key=lambda x: x["record_ref"]),
                "resources": sorted(
                    resources.get(str(row.operation_id), []), key=lambda x: x["record_ref"]
                ),
            }
        )
    graph = await read_graph(db, sid)
    claims = []
    for graph_name in ("information_graph", "attack_graph"):
        for objects in graph[graph_name].values():
            for item in objects:
                kind = (
                    ("attempt" if "outcome" in item else "state")
                    if graph_name == "attack_graph"
                    else ("relation" if "relation" in item else "entity")
                )
                claims.append(
                    {
                        "record_ref": f"{kind}:{item['id']}@{graph['revision']}",
                        "object_ref": f"{kind}:{item['id']}",
                        "origin_kind": "model_claim",
                        "graph_revision": graph["revision"],
                        "statement": safe(item),
                        "source_refs": list(item.get("source_refs") or []),
                    }
                )
    leads = []
    for row in (await db.scalars(select(LeadRow).where(LeadRow.session_id == sid))).all():
        leads.append(
            {
                "record_ref": f"lead:{row.id}",
                "origin_kind": "plan_intent",
                "source_refs": list(row.evidence_refs or []),
                "asset_id": str(row.asset_id) if row.asset_id else None,
                "identity_refs": row.identity_refs,
                "revision": row.revision,
                "statement": safe(
                    {
                        "hypothesis": row.hypothesis,
                        "status": row.status,
                        "blocked_reason": row.blocked_reason,
                        "resume_condition": row.resume_condition,
                        "required_conditions": row.required_conditions,
                    }
                ),
            }
        )
    condition_rows = list(
        (await db.scalars(select(FindingRow).where(FindingRow.session_id == sid))).all()
    )
    conditions = {str(row.id): safe(condition_snapshot(row)) for row in condition_rows}
    identities = {
        str(i): rev
        for i, rev in (
            await db.execute(
                select(IdentityRow.id, IdentityRow.auth_revision).where(
                    (IdentityRow.session_id == task.id) | (IdentityRow.id == task.active_identity_id)
                )
            )
        ).all()
    }
    return MemorySnapshot(
        boundary={
            "session_id": str(sid),
            "run_id": request.run_id,
            "control_version": request.control_version,
            "event_seq": task.event_seq,
            "plan_revision": request.state.get("plan_revision"),
            "graph_revision": graph["revision"],
            "scope_revision": task.scope_snapshot.get("revision"),
            "scope_signature": planning_digest(task.scope_snapshot),
            "target_mapping_signature": planning_digest(scope.target_mappings)
            if "target_mappings" in task.scope_snapshot
            else None,
            "policy_revision": (task.policy_snapshot or {}).get("revision"),
            "active_identity_id": str(task.active_identity_id) if task.active_identity_id else None,
        },
        operations=tuple(op_views),
        artifacts=tuple(artifacts),
        claims=tuple(claims),
        leads=tuple(leads),
        conditions=conditions,
        condition_refs={str(row.id): f"finding:{row.id}@{row.revision}" for row in condition_rows},
        identity_revisions=identities,
    )


def select_memory(
    request: DecisionInput, data: MemorySnapshot
) -> tuple[DecisionBlackboard, dict[str, Any]]:
    """纯选择器：稳定关联跨所有 Run；不判等价，不改 outcome，不生成下一行动。"""
    if data.boundary.get("session_id") != request.session_id:
        raise ValueError("decision_memory_session_mismatch")
    state = request.state
    focus = state.get("current_focus") or {}
    steps = list(state.get("plan") or [])
    valid_step = not focus.get("step_id") or any(
        s.get("step_id") == focus["step_id"] and s.get("status") in {"pending", "in_progress"}
        for s in steps
    )
    usable_focus = valid_step and request.focus_freshness in {
        "initial_input",
        "reexpressed_after_user_input",
    }
    step_id = focus.get("step_id") if usable_focus else None
    step = next((s for s in steps if s.get("step_id") == step_id), {})
    refs = set(request.explicit_refs)
    if usable_focus:
        refs |= explicit_refs(focus) | explicit_refs(step)
        refs |= recorded_refs(step.get("evidence_refs"), data)
    lead_ids = {str(v) for v in (step.get("lead_ids") or [])}
    lead_ids.update(
        str(v) for v in (step.get("lead_id"), focus.get("lead_id") if usable_focus else None) if v
    )
    refs.update(f"lead:{key}" for key in lead_ids)
    checks = (step.get("completion_spec") or {}).get("http_checks") or []
    endpoints = {value for c in checks if (value := endpoint(c.get("url"))) is not None}
    claims = [dict(c) for c in data.claims]
    for s in steps:
        claims.append(
            {
                "record_ref": f"plan:{request.run_id}:{state.get('plan_revision', 0)}:{s.get('step_id')}",
                "object_ref": f"step:{s.get('step_id')}",
                "origin_kind": "plan_intent",
                "source_refs": sorted(
                    explicit_refs(s) | recorded_refs(s.get("evidence_refs"), data)
                ),
                "statement": safe(
                    {
                        k: s[k]
                        for k in (
                            "step_id",
                            "description",
                            "status",
                            "rationale",
                            "lead_id",
                            "lead_ids",
                            "done_when",
                            "resume_note",
                            "completion_spec",
                        )
                        if k in s
                    }
                ),
            }
        )
    summary = state.get("work_summary") or {}
    for kind in (
        "key_findings",
        "failed_approaches",
        "unverified_hypotheses",
        "open_questions",
        "next_steps",
    ):
        for index, value in enumerate(summary.get(kind) or []):
            claims.append(
                {
                    "record_ref": f"summary:{request.run_id}:{summary.get('summary_version')}:{kind}:{index}",
                    "origin_kind": "summary_claim",
                    "statement": safe({kind: value}),
                    "source_refs": sorted(
                        explicit_refs(value)
                        | (
                            {f"artifact:{summary['summary_artifact_id']}"}
                            if summary.get("summary_artifact_id")
                            else set()
                        )
                    ),
                }
            )
    claims.extend(dict(c) for c in data.leads)
    for claim in claims:
        claim["source_refs"] = sorted(recorded_refs(claim.get("source_refs"), data))
    active_identity = data.boundary.get("active_identity_id")
    for op in data.operations:
        context = op["context"]
        if (
            (step_id and context.get("step_id") == step_id)
            or (op["dimensions"].get("endpoint") in endpoints)
            or (active_identity and op["dimensions"].get("identity_id") == active_identity)
            or (
                lead_ids
                & {str(v) for v in [context.get("lead_id"), *(context.get("lead_ids") or [])]}
            )
        ):
            refs.add(op["record_ref"])
    artifact_map = {a["record_ref"]: dict(a) for a in data.artifacts}
    for artifact in artifact_map.values():
        artifact["evidence_group_ref"] = artifact.get("source_operation_ref")
        if not artifact["evidence_group_ref"] and artifact["availability"] != "origin_unavailable":
            pointers = [
                op["record_ref"]
                for op in data.operations
                if artifact["record_ref"] in op.get("result_artifact_refs", [])
            ]
            if len(pointers) == 1:
                artifact["evidence_group_ref"] = pointers[0]
                artifact["association"] = "unique_result_reference_original_owner_unrecorded"
    # 只沿实际图端点/实体及来源闭包扩展，不靠 label 相似度。
    changed = True
    while changed:
        before = set(refs)
        for claim in claims:
            item = claim["statement"]
            links = {claim.get("object_ref", claim["record_ref"])}
            if isinstance(item, dict):
                links |= {
                    f"state:{item[k]}"
                    for k in ("from_id", "to_id")
                    if k in item and "outcome" in item
                }
                links |= {f"entity:{v}" for v in item.get("entity_refs", [])}
                if "relation" in item:
                    links |= {f"entity:{item[k]}" for k in ("from_id", "to_id")}
            source_refs = explicit_refs(claim.get("source_refs", []))
            if refs & (links | source_refs):
                refs |= links | explicit_refs(claim.get("source_refs", []))
        for ref in list(refs):
            if ref in artifact_map and artifact_map[ref].get("evidence_group_ref"):
                refs.add(artifact_map[ref]["evidence_group_ref"])
        changed = refs != before
    for ref in list(refs):
        if ref in artifact_map and artifact_map[ref].get("evidence_group_ref"):
            refs.add(artifact_map[ref]["evidence_group_ref"])
    selected: dict[str, list[str]] = {}
    trace = []
    strong = bool(refs or step_id or endpoints)
    artifact_activity: dict[str, str] = {}
    for artifact in artifact_map.values():
        group = artifact.get("evidence_group_ref")
        if group and artifact.get("captured_at"):
            artifact_activity[group] = max(artifact_activity.get(group, ""), artifact["captured_at"])

    def activity_time(op: dict) -> str:
        return max(op["created_at"], op.get("updated_at", ""),
                   artifact_activity.get(op["record_ref"], ""))

    recent = {op["record_ref"] for op in sorted(data.operations, key=activity_time)[-5:]} if not strong else set()
    for op in data.operations:
        ref, context = op["record_ref"], op["context"]
        reasons = []
        linked = {ref} | {j["record_ref"] for j in op["jobs"]}
        linked |= {
            a["record_ref"] for a in artifact_map.values() if a.get("evidence_group_ref") == ref
        }
        if refs & linked:
            reasons.append("explicit_ref_or_source_closure")
        if step_id and context.get("step_id") == step_id:
            reasons.append("same_step_navigation_not_equivalence")
        if lead_ids & {str(v) for v in [context.get("lead_id"), *(context.get("lead_ids") or [])]}:
            reasons.append("same_lead")
        if op["dimensions"].get("endpoint") in endpoints:
            reasons.append("same_endpoint_navigation_not_equivalence")
        if active_identity and op["dimensions"].get("identity_id") == active_identity:
            reasons.append("same_active_identity_navigation")
        unresolved = op["operation_status"] not in {"succeeded", "failed", "cancelled"} or any(
            j["last_observed_state"] not in {"succeeded", "failed", "cancelled"}
            or j["sync_status"] != "in_sync"
            for j in op["jobs"]
        )
        if unresolved:
            reasons.append("session_unresolved_directory")
        if ref in recent:
            reasons.append("recent_execution_anchor")
        if reasons:
            selected[ref] = reasons
        trace.append(
            {
                "record_ref": ref,
                "selected": bool(reasons),
                "match_reasons": reasons,
                "association_status": "matched"
                if reasons
                else "no_structural_key"
                if not any(
                    (
                        context.get("step_id"),
                        context.get("lead_id"),
                        context.get("lead_ids"),
                        op["dimensions"].get("endpoint"),
                        op["dimensions"].get("identity_id"),
                    )
                )
                else "candidate_not_selected",
            }
        )
    evidence, differences, references, unresolved_refs = [], {}, {}, []
    current = {
        key: data.boundary.get(key)
        for key in (
            "scope_revision",
            "policy_revision",
            "target_mapping_signature",
            "active_identity_id",
        )
    }
    current["identity_id"] = current.pop("active_identity_id")
    current.update(
        endpoint=next(iter(endpoints)) if len(endpoints) == 1 else None,
        method=checks[0].get("method", "").upper() if len(checks) == 1 else None,
        authentication=None,
    )
    for op in data.operations:
        ref = op["record_ref"]
        if ref not in selected:
            continue
        outside = op["access"] == "outside_current_scope"
        attached = [a for a in artifact_map.values() if a.get("evidence_group_ref") == ref]
        sources = (
            [ref] + [j["record_ref"] for j in op["jobs"]] + [a["record_ref"] for a in attached]
        )
        execution = {
            "operation_status": op["operation_status"],
            "jobs_last_observed": op["jobs"],
            "resources_last_observed": op["resources"],
            "not_live_executor_check": True,
        }
        evidence.append(
            MemoryUnit(
                record_ref=ref,
                origin_kind="execution_observation",
                statement={
                    "tool": op["tool_name"],
                    "run_id": op["run_id"],
                    "tool_call_id": op["tool_call_id"],
                    "result_fields": {} if outside else op["result"],
                    "access": op["access"],
                },
                source_refs=sources,
                match_reasons=selected[ref],
                support_status="execution_metadata_only",
                source_status="local_operation_exists",
                execution=execution,
                observed_dimensions={} if outside else safe(op["dimensions"]),
                evidence_availability="outside_current_scope"
                if outside
                else "metadata_only_no_body_read",
            )
        )
        if "session_unresolved_directory" in selected[ref]:
            unresolved_refs.append(ref)
        old = dict(op["dimensions"])
        now = dict(current)
        old["executor_id"] = op["jobs"][0]["executor_id"] if len(op["jobs"]) == 1 else None
        now["executor_id"] = request.executor_id
        old["target_environment"] = None
        now["target_environment"] = None
        identity = old.get("identity_id")
        now["identity_auth_revision"] = (
            data.identity_revisions.get(str(identity))
            if identity == current["identity_id"]
            else None
        )
        if old.get("authentication") == "explicit_headers":
            now["identity_auth_revision"] = None
        for key, previous in (op["context"].get("source_condition_snapshots") or {}).items():
            old[f"condition_source:{key}"] = previous
            now[f"condition_source:{key}"] = data.conditions.get(key)
        differences[ref] = compare_dimensions(
            old if not outside else {},
            now if not outside else {},
            [ref, f"session:{request.session_id}"],
        )
        for index, difference in enumerate(differences[ref]):
            source_ref = None
            if difference.field.startswith("condition_source:"):
                source_ref = data.condition_refs.get(difference.field.partition(":")[2])
            elif (
                difference.field == "identity_auth_revision" and identity in data.identity_revisions
            ):
                source_ref = f"identity:{identity}@auth:{data.identity_revisions[str(identity)]}"
            elif difference.field in {"method", "endpoint"} and checks:
                source_ref = (
                    f"checkpoint_plan:{request.run_id}:{state.get('plan_revision')}:{step_id}"
                )
            if source_ref:
                differences[ref][index] = difference.model_copy(
                    update={"source_refs": [*difference.source_refs, source_ref]}
                )
                references[source_ref] = {
                    "record_ref": source_ref,
                    "availability": "local_metadata_only",
                }
        references[ref] = {"record_ref": ref, "availability": op["access"]}
        for item in op["jobs"]:
            references[item["record_ref"]] = {
                "record_ref": item["record_ref"],
                "availability": "local_last_observed",
            }
        for a in attached:
            references[a["record_ref"]] = a
    attempts, open_items, claim_trace = [], [], []
    for claim in claims:
        source_refs = explicit_refs(claim.get("source_refs", []))
        related = (
            bool(source_refs & (set(selected) | set(references)))
            or claim.get("object_ref", claim["record_ref"]) in refs
        )
        statement = claim["statement"]
        is_open = (
            claim["origin_kind"] == "plan_intent"
            and statement.get("status") not in {"done", "dropped", "disproved"}
        ) or (
            claim["origin_kind"] == "summary_claim"
            and any(
                k in statement for k in ("open_questions", "next_steps", "unverified_hypotheses")
            )
        )
        if claim["record_ref"].startswith("lead:") and not related:
            is_open = False  # 旧领域项只做明确关联读取，不恢复默认旧目录。
        claim_trace.append(
            {
                "record_ref": claim["record_ref"],
                "origin_kind": claim["origin_kind"],
                "selected": related or is_open,
                "association_status": "linked_source_or_graph"
                if related
                else "open_item_background"
                if is_open
                else "no_provable_association",
            }
        )
        if not related and not is_open:
            continue
        unavailable = []
        for ref in sorted(source_refs):
            if ref in artifact_map:
                references[ref] = artifact_map[ref]
                if (
                    artifact_map[ref]["availability"] != "available"
                    or artifact_map[ref].get("access") == "outside_current_scope"
                ):
                    unavailable.append(ref)
            elif ref not in {op["record_ref"] for op in data.operations}:
                unavailable.append(ref)
        outside = any(
            op["record_ref"] in source_refs and op["access"] == "outside_current_scope"
            for op in data.operations
        )
        outside = outside or any(
            artifact_map.get(ref, {}).get("access") == "outside_current_scope"
            for ref in source_refs
        )
        unit = MemoryUnit(
            record_ref=claim["record_ref"],
            origin_kind=claim["origin_kind"],
            statement={"withheld": "source_outside_current_scope"} if outside else statement,
            source_refs=sorted(source_refs),
            match_reasons=["linked_source_or_graph"]
            if related
            else ["existing_open_item_background"],
            support_status="source_unavailable_semantic_unverified"
            if unavailable
            else "semantic_unverified",
            source_status="missing_or_unreadable"
            if unavailable
            else "local_sources_exist"
            if source_refs
            else "no_execution_source",
            evidence_availability="source_unavailable" if unavailable else "source_metadata_only",
        )
        (open_items if is_open else attempts).append(unit)
    # 明确引用的独立工件（如摘要）没有原操作时保持独立，不伪造归属。
    for ref in refs:
        if ref in artifact_map:
            references[ref] = artifact_map[ref]
            if not artifact_map[ref].get("evidence_group_ref"):
                evidence.append(
                    MemoryUnit(
                        record_ref=ref,
                        origin_kind="execution_observation",
                        statement=artifact_map[ref],
                        source_refs=[ref],
                        match_reasons=["explicit_unbound_artifact"],
                        support_status="metadata_only_no_original_operation",
                        evidence_availability=artifact_map[ref]["availability"],
                    )
                )
    available_refs = (
        set(references)
        | {op["record_ref"] for op in data.operations}
        | {c.get("object_ref", c["record_ref"]) for c in claims}
    )
    missing = sorted(ref for ref in refs if ref not in available_refs)
    # 先形成可信候选优先级；展示层随后按实际预算装配，不能先截正序前三条。
    operation_map = {op["record_ref"]: op for op in data.operations}
    latest_evidence = max(
        (op for op in data.operations if op["record_ref"] in selected and op["access"] != "outside_current_scope"
         and op["operation_status"] == "succeeded" and op.get("result_artifact_refs")),
        key=lambda op: (activity_time(op), op["record_ref"]),
        default=None,
    )
    latest_change = max((op for op in data.operations if op["record_ref"] in selected
        and op["access"] != "outside_current_scope"),
        key=lambda op: (activity_time(op), op["record_ref"]), default=None)

    def priority(unit: MemoryUnit) -> tuple:
        op = operation_map.get(unit.record_ref, {})
        structural = bool(set(unit.match_reasons) & {
            "explicit_ref_or_source_closure", "same_step_navigation_not_equivalence",
            "same_lead", "same_endpoint_navigation_not_equivalence",
        })
        return (
            structural,
            bool(latest_change and unit.record_ref == latest_change["record_ref"]
                 and (op.get("operation_status") != "succeeded" or op.get("result_artifact_refs"))),
            bool(latest_evidence and unit.record_ref == latest_evidence["record_ref"]),
            unit.record_ref in unresolved_refs,
            activity_time(op) if op else "",
            unit.record_ref,
        )

    evidence.sort(key=priority, reverse=True)
    if latest_evidence:
        evidence = [unit.model_copy(update={"match_reasons": [*unit.match_reasons,
            "latest_evidenced_operation"]}) if unit.record_ref == latest_evidence["record_ref"]
            else unit for unit in evidence]
    if latest_change:
        evidence = [unit.model_copy(update={"match_reasons": [*unit.match_reasons,
            "latest_operation_change"]}) if unit.record_ref == latest_change["record_ref"]
            else unit for unit in evidence]
    board = DecisionBlackboard(
        snapshot=data.boundary,
        current_focus=safe(
            {
                "objective": state.get("objective"),
                "user_constraints": state.get("user_constraints") or [],
                "effective_user_inputs": request.user_inputs,
                "checkpoint_focus": focus,
                "focus_freshness": request.focus_freshness,
                "focus_validity": "valid_or_unbound" if valid_step else "invalid_step",
                "anchor_refs": sorted(refs),
                "focus_used_for_filtering": usable_focus,
                "limitation": "旧焦点不自动覆盖新指引；missing字段不证明匿名、同环境或条件未变。",
            }
        ),
        relevant_evidence=evidence,
        previous_attempts=attempts,
        open_items=open_items,
        unresolved_results=unresolved_refs,
        condition_differences=differences,
        evidence_references=sorted(references.values(), key=lambda v: v["record_ref"]),
        retrieval_status={
            "status": "selected" if evidence or attempts or open_items else "not_retrieved",
            "mode": "structural" if strong else "focus_ambiguous_navigation",
            "source_count": len(data.operations),
            "source_inventory": {
                "operations": len(data.operations),
                "artifacts": len(data.artifacts),
                "claims": len(claims),
            },
            "candidate_count": len(trace),
            "selected_count": len(selected),
            "unavailable_source_refs": missing,
            "coverage": "not_a_completeness_proof",
            "equivalence": "not_determined",
            "limitations": [
                "不读取工件正文；使用已有read_artifact补读。",
                "执行成功不等于漏洞成功，模型声明均未独立验证。",
            ],
        },
    )
    return board, {
        "candidates": trace,
        "claim_candidates": claim_trace,
        "requested_refs": sorted(refs),
        "source_resolution": [
            {
                "record_ref": ref,
                "status": "unavailable_or_outside_session" if ref in missing else "found",
            }
            for ref in sorted(refs)
        ],
    }
