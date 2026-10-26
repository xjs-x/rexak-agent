"""HTTP 控制端捕获与只读历史适配；模型参数没有授予可信来源的权限。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from agent_workbench.contracts.history_action_reflection import (
    Evidence,
    Execution,
    HistoricalExecution,
    Snapshot,
)
from agent_workbench.domain.history_action_reflection import digest, request_signature
from agent_workbench.persistence.tables import ArtifactRow, ToolJobRow, ToolOperationRow


class ControlGrant(BaseModel):
    """独立控制端批准；不是 Tool Schema 的一部分，也不从模型计划读取。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    approval_id: str
    session_id: str
    run_id: str
    tool_call_id: str
    purpose: Literal["security_test", "navigation"]
    production_request_digest: str
    task_version: str
    environment: str
    configuration: str
    prerequisites: dict[str, Any]
    executor_id: str
    executor_boot_id: str
    mapping_signature: str
    retest_required: bool = False


@dataclass(frozen=True)
class Records:
    operations: list[ToolOperationRow]
    jobs: list[ToolJobRow]
    artifacts: list[ArtifactRow]


def step(moment: datetime) -> int:
    if moment.tzinfo is None:
        raise ValueError("unrecorded_timezone")
    return int(moment.timestamp() * 1_000_000)


def safe_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """保留完整参数的确定性哈希投影，不把请求体、头或参数值写入观察副本。"""
    value = dict(arguments)
    for key in ("headers", "params"):
        if value.get(key) is not None:
            value[key] = {name: "sha256:" + digest(item) for name, item in value[key].items()}
    if value.get("body") is not None:
        value["body"] = "sha256:" + digest(value["body"])
    if value.get("json_body") is not None:
        value["json_body"] = {"opaque_sha256": digest(value["json_body"])}
    return value


def capture(
    *,
    session_id: str,
    run_id: str,
    tool_call_id: str,
    arguments: dict[str, Any],
    operation: dict[str, Any],
    actual_identity_id: str | None,
    executor: Any,
    grant: ControlGrant | None,
    mapping_signature: str,
    now: datetime,
) -> tuple[dict[str, Any] | None, list[str]]:
    if grant is None or grant.purpose != "security_test":
        return None, ["no_controller_security_test_approval"]
    reasons = []
    if (grant.session_id, grant.run_id, grant.tool_call_id) != (session_id, run_id, tool_call_id):
        reasons.append("approval_identity_mismatch")
    if (
        operation.get("kind") != "http_request"
        or operation.get("request_digest") != grant.production_request_digest
    ):
        reasons.append("approved_parameters_mismatch")
    # 当前生产匿名会话仍有 Cookie jar；只有原候选显式 Cookie:"" 且无重定向才可比较。
    headers = {key.lower(): value for key, value in arguments.get("headers", {}).items()}
    if (
        actual_identity_id is not None
        or arguments.get("identity") != "anonymous"
        or headers.get("cookie") != ""
        or "authorization" in headers
        or arguments.get("follow_redirects") is not False
    ):
        reasons.append("dynamic_auth_or_cookie_state_unobserved")
    if any(
        arguments.get(key) is not None
        for key in (
            "csrf_source_url",
            "csrf_input_name",
            "csrf_target_name",
            "form_fields",
            "multipart_parts",
        )
    ):
        reasons.append("dynamic_encoding_or_csrf_unsupported")
    parts = urlsplit(str(arguments.get("url", "")))
    if parts.query or parts.fragment or parts.username or parts.password:
        reasons.append("url_sensitive_or_dynamic_components_unsupported")
    caps = getattr(executor, "_preflight", None)
    if (
        caps is None
        or caps.executor_id != grant.executor_id
        or caps.boot_id != grant.executor_boot_id
        or not caps.http_available
        or caps.protocol_version != 1
    ):
        reasons.append("executor_identity_not_preflight_verified")
    if mapping_signature != grant.mapping_signature:
        reasons.append("mapping_attestation_mismatch")
    from agent_workbench.persistence.repositories import compute_request_digest

    persisted_arguments = dict(arguments)
    if persisted_arguments.get("verify_tls") is True:
        persisted_arguments.pop("verify_tls")
    if operation.get("request_digest") != compute_request_digest(
        {"tool": "http_request", "arguments": persisted_arguments}
    ):
        reasons.append("production_parameter_digest_conflict")
    if headers.get("content-encoding", "identity").lower() != "identity":
        reasons.append("unknown_request_body_encoding")
    payload = operation.get("payload") or {}
    if (
        payload.get("url") != arguments.get("url")
        or payload.get("method") != str(arguments.get("method", "")).upper()
    ):
        reasons.append("executor_payload_binding_mismatch")
    for key in ("headers", "params", "body", "json_body", "verify_tls"):
        if payload.get(key) != arguments.get(key):
            reasons.append(f"executor_payload_binding_mismatch:{key}")
    if payload.get("identity_id") != f"session-{session_id}" or payload.get("max_redirects") != 0:
        reasons.append("executor_identity_or_redirect_binding_mismatch")
    if reasons:
        return None, reasons
    projected = safe_arguments(arguments)
    conditions = {
        "target": f"{parts.scheme}://{parts.netloc}",
        "url": arguments["url"],
        "method": str(arguments["method"]).upper(),
        "environment": grant.environment,
        "configuration": grant.configuration,
        "identity_id": payload["identity_id"],
        "auth_revision": "explicit-empty-cookie-no-auth/v1",
        "privileges": ["unauthenticated-http"],
        "executor_id": grant.executor_id + "@boot:" + grant.executor_boot_id,
        "mapping_signature": mapping_signature,
        "prerequisites": grant.prerequisites,
        "request_digest": request_signature(projected),
    }
    return {
        "schema": "trusted_http_capture/1",
        "session_id": session_id,
        "run_id": run_id,
        "operation_id": operation["operation_id"],
        "tool_call_id": tool_call_id,
        "production_request_digest": operation["request_digest"],
        "arguments": projected,
        "conditions": conditions,
        "captured_at": now.isoformat(),
        "approval": grant.model_dump(),
        "approval_sha256": digest(grant.model_dump()),
        "payload_identity_id": payload["identity_id"],
    }, []


def execution(value: dict[str, Any], *, source_ref: str) -> Execution:
    return Execution(
        tool_call_id=value["tool_call_id"],
        tool_name="http_request",
        arguments=value["arguments"],
        operation_ref="operation:" + value["operation_id"],
        semantic_contract="executor_http/v1",
        conditions=value["conditions"],
        condition_sources={key: [source_ref] for key in value["conditions"]},
        provenance_verified=True,
        origin="recorder",
        source_refs=[source_ref],
        logical_test_key="approved-http:" + value["conditions"]["url"],
    )


def visible_bodies(messages: list[dict[str, Any]]) -> dict[str, list[tuple[str, str]]]:
    """只接受唯一先 AI 后 Tool 的来源；不按模型自由文本或 Artifact ID 出现次数判断可见。"""
    pending: dict[str, str] = {}
    counts: dict[str, int] = {}
    for m in messages:
        if m.get("type") == "tool":
            key = str(m.get("tool_call_id"))
            counts[key] = counts.get(key, 0) + 1
    result: dict[str, list[tuple[str, str]]] = {}
    seen: set[str] = set()
    for m in messages:
        if m.get("type") == "ai":
            for call in m.get("tool_calls", []):
                key = str(call.get("id"))
                if key in pending or key in seen:
                    pending.pop(key, None)
                    seen.add(key)
                else:
                    pending[key] = str(call.get("name"))
        elif m.get("type") == "tool":
            key = str(m.get("tool_call_id"))
            tool = pending.pop(key, None)
            if counts.get(key) != 1 or key in seen or m.get("status") == "error":
                continue
            seen.add(key)
            try:
                payload = json.loads(m.get("content", ""))
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                continue
            data = payload["data"]
            if (
                tool == "http_request"
                and payload.get("truncated") is False
                and isinstance(data.get("body_preview"), str)
            ):
                for ref in payload.get("artifact_refs", []):
                    result.setdefault(str(ref.get("artifact_id")), []).append(
                        (data["body_preview"], f"tool:{key}/body_preview")
                    )
            elif (
                tool == "read_artifact"
                and data.get("offset") == 0
                and data.get("truncated") is False
                and isinstance(data.get("text"), str)
            ):
                result.setdefault(str(data.get("artifact_id")), []).append(
                    (data["text"], f"tool:{key}/text")
                )
    return result


def adapt_history(
    current: dict[str, Any],
    records: Records,
    captures: dict[str, dict[str, Any]],
    artifact_bytes: dict[str, bytes],
    context: dict[str, Any],
    *,
    visibility: dict[str, list[tuple[str, str]]] | None = None,
) -> tuple[Snapshot, list[str]]:
    cutoff = step(datetime.fromisoformat(current["captured_at"]))
    session = current["session_id"]
    if visibility is None:
        visibility = visible_bodies(context.get("messages", []))
    batch = set(context.get("batch_ids", []))
    history, evidence, diagnostics = [], [], []
    jobs_by_operation: dict[Any, list] = defaultdict(list)
    artifacts_by_operation: dict[Any, list] = defaultdict(list)
    for job in records.jobs:
        jobs_by_operation[job.operation_id].append(job)
    for artifact in records.artifacts:
        artifacts_by_operation[artifact.source_operation_id].append(artifact)
    for op in records.operations:
        oid = str(op.operation_id)
        if (
            str(op.session_id) != session
            or oid == current["operation_id"]
            or op.tool_call_id in batch
            or op.tool_name != "http_request"
        ):
            continue
        prior = captures.get(oid)
        if prior is None:
            diagnostics.append(f"operation:{oid}:no_prior_control_capture")
            continue
        if (
            current.get("trusted_task")
            and prior.get("trusted_task", {}).get("authorization_sha256")
            != current["trusted_task"]["authorization_sha256"]
        ):
            diagnostics.append(f"operation:{oid}:task_authorization_version_boundary")
            continue
        # 本版没有跨 Run 授权合同；同一 session 不自动授权合并其它 Run 的执行。
        if (
            str(op.run_id) != current["run_id"]
            or prior.get("approval", {}).get("task_version") != current["approval"]["task_version"]
        ):
            diagnostics.append(f"operation:{oid}:run_or_task_authorization_boundary")
            continue
        recorded_context = op.execution_context or {}
        if (
            op.identity_id is not None
            or recorded_context.get("method") != prior["conditions"]["method"]
            or recorded_context.get("url") != prior["conditions"]["url"]
            or recorded_context.get("target_mapping_signature")
            != prior["conditions"]["mapping_signature"]
        ):
            diagnostics.append(f"operation:{oid}:recorded_context_conflict")
            continue
        if (
            prior.get("session_id") != session
            or prior.get("run_id") != str(op.run_id)
            or prior.get("tool_call_id") != op.tool_call_id
            or prior.get("production_request_digest") != op.request_digest
            or digest(prior["approval"]) != prior.get("approval_sha256")
        ):
            diagnostics.append(f"operation:{oid}:capture_binding_conflict")
            continue
        jobs = jobs_by_operation[op.operation_id]
        times = [step(j.executor_finished_at) for j in jobs if j.executor_finished_at is not None]
        finished = max(times) if len(times) == len(jobs) and times else None
        if finished is not None and finished >= cutoff:
            diagnostics.append(f"operation:{oid}:not_before_candidate")
            continue
        refs = []
        for art in artifacts_by_operation[op.operation_id]:
            aid = str(art.id)
            if (
                art.source_operation_id != op.operation_id
                or str(art.session_id) != session
                or aid not in (op.result_artifact_ids or [])
                or art.availability != "available"
                or art.truncated
                or step(art.captured_at) >= cutoff
                or aid not in visibility
            ):
                continue
            raw = artifact_bytes.get(aid)
            if (
                raw is None
                or len(raw) != art.byte_size
                or hashlib.sha256(raw).hexdigest() != art.sha256
            ):
                diagnostics.append(f"artifact:{aid}:bytes_or_hash_unavailable")
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                diagnostics.append(f"artifact:{aid}:not_supported_utf8")
                continue
            visible = [r for value, r in visibility.get(aid, []) if value == text]
            if not visible:
                continue
            ref = "artifact:" + aid
            refs.append(ref)
            evidence.append(
                Evidence(
                    evidence_ref=ref,
                    operation_ref="operation:" + oid,
                    session_id=session,
                    kind="artifact",
                    content=text,
                    sha256=art.sha256,
                    visible=True,
                    complete=True,
                    provenance_verified=True,
                    available_step=step(art.captured_at),
                    source_refs=visible,
                )
            )
        view = execution(prior, source_ref="controller-capture:" + oid)
        states = {
            "job:" + str(j.job_id): j.state if j.sync_status == "in_sync" else "unknown"
            for j in jobs
        }
        if len(jobs) != 1 or op.job_id != jobs[0].job_id:
            states = dict.fromkeys(states, "unknown")
            diagnostics.append(f"operation:{oid}:operation_job_reverse_link_conflict")
        if any(j.executor_id != prior["approval"]["executor_id"] for j in jobs):
            states = dict.fromkeys(states, "unknown")
        history.append(
            HistoricalExecution(
                **view.model_dump(),
                session_id=session,
                status=op.status,
                job_refs=list(states),
                job_states=states,
                finished_step=finished,
                evidence_refs=refs,
            )
        )
    candidate = execution(current, source_ref="controller-dispatch:" + current["operation_id"])
    grant = current["approval"]
    snapshot = Snapshot(
        snapshot_id="http-shadow:" + current["operation_id"],
        session_id=session,
        task_version=grant["task_version"],
        context_version=grant["task_version"],
        evidence_version=digest(sorted((e.evidence_ref, e.sha256) for e in evidence)),
        decision_step=cutoff,
        candidate=candidate,
        history=history,
        evidence=evidence,
        same_batch_tool_call_ids=sorted(batch),
        retest_intent="required" if grant["retest_required"] else "not_required",
        limitations=[
            "仅支持控制端逐调用批准、显式空 Cookie、匿名、无重定向的 HTTP 子集；环境/配置/前提为控制端版本声明。"
        ],
    )
    return snapshot, diagnostics
