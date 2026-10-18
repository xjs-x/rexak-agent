"""仅开放可确定性复核的窄场景，不接受模型直接写 confirmed。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any
from uuid import UUID

from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolCallId, tool

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.enums import FindingStatus, OperationStatus
from agent_workbench.contracts.errors import ApiError
from agent_workbench.domain.evidence import EvidencePolicyError, EvidenceUse, resolve_evidence
from agent_workbench.domain.verification_recipes import (
    RECIPES,
    RecipeOutcome,
    evaluate_recipe,
    parse_checks,
)
from agent_workbench.domain.verifiers import (
    confirm_public_git_head,
    preflight_public_git_head,
    validate_public_git_head_url,
)
from agent_workbench.observability.logging import get_logger
from agent_workbench.observability.spans import Span, SpanRecorder
from agent_workbench.persistence.tables import ArtifactRow, FindingRow
from agent_workbench.tools.http import HttpRequestArgs, run_http_request
from agent_workbench.tools.runtime import Toolbox

log = get_logger("agent_workbench.verification")


def _response(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def _verification_stage(
    spans: SpanRecorder,
    name: str,
    toolbox: Toolbox,
) -> Iterator[Span]:
    span = spans.start(
        name,
        session_id=toolbox.session_id,
        run_id=toolbox.run_id,
    )
    try:
        yield span
    except (Exception, asyncio.CancelledError) as exc:
        span.finish(error=type(exc).__name__)
        spans.record(span, status="failed")
        raise
    else:
        span.finish()
        spans.record(span, status="completed")


@tool("verify_public_git_head")
async def verify_public_git_head(
    runtime: ToolRuntime[RunContext],
    tool_call_id: Annotated[str, InjectedToolCallId],
    finding_id: str,
    url: str,
    initial_body_artifact_id: str,
    initial_meta_artifact_id: str,
) -> str:
    """独立复核候选发现：目标的 `/.git/HEAD` 是否公开返回 Git 分支引用。

    Args:
        finding_id: 信息泄露类候选发现 ID；初次 HTTP 响应工件必须已绑定该发现。
        url: 初次观察的完整 `http(s)://目标/.git/HEAD` 地址。
        initial_body_artifact_id: 初次 `http_request` 返回的 `response.body` 工件 ID。
        initial_meta_artifact_id: 同次请求返回的 `response.meta.json` 工件 ID。

    Returns:
        只确认“Git HEAD 分支引用公开可读”；不推断仓库对象或源码可读。

    Notes:
        - 本工具会使用隔离的匿名会话重新发起一次 GET，请勿把普通 200 当作结果；
        - 初次观察和复测必须是不同、成功且证据完整的 HTTP 操作；
        - 复测受阻、超时或证据不匹配时保持 candidate，不产生阴性结论。
    """
    toolbox: Toolbox = runtime.context.toolbox
    spans = SpanRecorder()
    try:
        return await _verify_public_git_head_workflow(
            runtime,
            toolbox,
            spans,
            tool_call_id=tool_call_id,
            finding_id=finding_id,
            url=url,
            initial_body_artifact_id=initial_body_artifact_id,
            initial_meta_artifact_id=initial_meta_artifact_id,
        )
    finally:
        try:
            async with toolbox.db.transaction() as db:
                await spans.flush(db)
        except Exception as exc:
            log.warning(
                "verification_span_persist_failed",
                session_id=str(toolbox.session_id),
                error_type=type(exc).__name__,
            )


async def _verify_public_git_head_workflow(
    runtime: ToolRuntime[RunContext],
    toolbox: Toolbox,
    spans: SpanRecorder,
    *,
    tool_call_id: str,
    finding_id: str,
    url: str,
    initial_body_artifact_id: str,
    initial_meta_artifact_id: str,
) -> str:
    try:
        finding_uuid = UUID(finding_id)
        initial_body_uuid = UUID(initial_body_artifact_id)
        initial_meta_uuid = UUID(initial_meta_artifact_id)
    except ValueError:
        return _response(verified=False, reason="发现或工件 ID 不是合法 UUID")

    try:
        with _verification_stage(spans, "verification.git_head.preflight", toolbox):
            validate_public_git_head_url(url)
            async with toolbox.db.session() as db:
                await preflight_public_git_head(
                    db,
                    toolbox.artifacts,
                    session_id=toolbox.session_id,
                    finding_id=finding_uuid,
                    url=url,
                    initial_body_id=initial_body_uuid,
                    initial_meta_id=initial_meta_uuid,
                )
    except ApiError as exc:
        return _response(verified=False, reason=exc.message, code=str(exc.code))

    # 调用 ID 固定后，崩溃重放复用同一远端 operation；新工具调用才是显式重测。
    digest = hashlib.sha256(tool_call_id.encode("utf-8")).hexdigest()[:32]
    recheck_identity = f"verify-git-head-{digest}"
    with _verification_stage(spans, "verification.git_head.recheck_dispatch", toolbox) as span:
        recheck = await run_http_request(
            HttpRequestArgs(
                url=url,
                method="GET",
                identity=recheck_identity,
                follow_redirects=False,
                save_body_artifact=True,
            ),
            toolbox,
            f"verify-git-head:{digest}",
        )
        span.operation_id = recheck.operation_id
    if recheck.status is not OperationStatus.SUCCEEDED:
        return _response(
            verified=False,
            reason="独立复测未成功完成，候选状态不变",
            recheck_status=str(recheck.status),
            recheck_job_id=str(recheck.job_id) if recheck.job_id else None,
            operation_id=str(recheck.operation_id),
        )

    refs = {
        "body": next(
            (
                ref.artifact_id
                for ref in recheck.artifact_refs
                if (ref.summary or "").startswith("response.body（")
            ),
            None,
        ),
        "meta": next(
            (
                ref.artifact_id
                for ref in recheck.artifact_refs
                if (ref.summary or "").startswith("response.meta.json（")
            ),
            None,
        ),
    }
    if refs["body"] is None or refs["meta"] is None:
        return _response(verified=False, reason="复测工件未完整同步，候选状态不变")

    try:
        with _verification_stage(spans, "verification.git_head.confirm", toolbox):
            async with toolbox.db.transaction() as db:
                verification_id = await confirm_public_git_head(
                    db,
                    toolbox.artifacts,
                    session_id=toolbox.session_id,
                    finding_id=finding_uuid,
                    url=url,
                    initial_body_id=initial_body_uuid,
                    initial_meta_id=initial_meta_uuid,
                    recheck_body_id=refs["body"],
                    recheck_meta_id=refs["meta"],
                    recheck_identity=recheck_identity,
                )
    except ApiError as exc:
        return _response(verified=False, reason=exc.message, code=str(exc.code))
    runtime.context.note_progress(f"verification:{verification_id}")
    return _response(
        verified=True,
        finding_id=finding_id,
        verification_id=str(verification_id),
        scope="仅确认 Git HEAD 分支引用公开可读；未确认仓库对象或源代码可读",
        recheck_operation_id=str(recheck.operation_id),
    )


@tool("verify_finding")
async def verify_finding(
    runtime: ToolRuntime[RunContext],
    tool_call_id: Annotated[str, InjectedToolCallId],
    finding_id: str,
    recipe_id: str,
    evidence_refs_json: list[str] | str,
    checks_json: list[dict[str, Any]] | str,
    claim_scope_json: dict[str, Any] | str | None = None,
    statistics_json: dict[str, Any] | str | None = None,
) -> str:
    """用确定性 recipe 复核候选发现；只有 pass 才写 Verification 并提升 Finding。

    Args:
        finding_id: 当前会话的候选发现 ID。
        recipe_id: command_echo / sentinel_read / identity_boundary /
            paired_difference / public_read / default_login。
        evidence_refs_json: 证据工件 ID 数组；必须来自当前会话且有 ledger provenance。
        checks_json: 判据数组。每项
            `{"artifact_id": "...", "role": "initial|recheck|control|...",
              "must_match": ["正则"], "must_not_match": ["正则"], "sha256": "可选"}`。
        claim_scope_json: 本次结论的适用范围（目标、身份、路径、条件），不会自动扩大。
        statistics_json: 仅 paired_difference 使用；`{"pairs":[{"baseline":..,"treatment":..}],
            "delta":..,"min_pairs":20}`，由工具重新计算效应与区间。

    Returns:
        `verified=true` 仅表示 recipe 的预登记判据与负对照通过，不表示影响范围外也成立。
    """
    from agent_workbench.domain.findings import VerificationService

    toolbox: Toolbox = runtime.context.toolbox
    try:
        finding_uuid = UUID(finding_id)
    except ValueError:
        return _response(verified=False, outcome="invalid_evidence", reason="finding_id 不是合法 UUID")
    try:
        refs = _json_list(evidence_refs_json, "evidence_refs_json")
        checks = parse_checks(_json_value(checks_json, "checks_json"))
        claim_scope = _json_object(claim_scope_json, "claim_scope_json")
        statistics_payload = _json_object(statistics_json, "statistics_json")
    except ValueError as exc:
        return _response(verified=False, outcome="invalid_evidence", reason=str(exc))
    if not refs:
        return _response(verified=False, outcome="invalid_evidence", reason="证据引用不能为空")

    spans = SpanRecorder()
    try:
        with _verification_stage(spans, "verification.recipe.preflight", toolbox):
            async with toolbox.db.transaction() as db:
                finding = await db.get(FindingRow, finding_uuid)
                if finding is None or finding.session_id != toolbox.session_id:
                    return _response(
                        verified=False, outcome="invalid_evidence", reason="发现不属于当前会话"
                    )
                try:
                    resolution = await resolve_evidence(
                        db, toolbox, refs, use=EvidenceUse.VERIFICATION
                    )
                except EvidencePolicyError as exc:
                    return _response(
                        verified=False, outcome="invalid_evidence", reason=str(exc)
                    )
                check_ids = {str(item["artifact_id"]) for item in checks}
                if not check_ids <= set(resolution.refs):
                    return _response(
                        verified=False,
                        outcome="invalid_evidence",
                        reason="checks_json 引用了未在 evidence_refs_json 中提交的工件",
                    )
                role_by_id = {str(item["artifact_id"]): item["role"] for item in checks}
                used_operations: set[str] = set()
                control_operations: set[str] = set()
                for artifact in resolution.artifacts:
                    artifact_id = str(artifact.artifact_id)
                    if artifact_id not in check_ids:
                        continue
                    operations = {str(item) for item in artifact.origin_operation_ids}
                    if role_by_id.get(artifact_id) == "control":
                        control_operations.update(operations)
                    else:
                        used_operations.update(operations)
                if len(used_operations) < 2:
                    return _response(
                        verified=False,
                        outcome="invalid_evidence",
                        reason="参与判定的证据不足两个独立操作；额外无关工件不能凑独立性",
                    )
                if control_operations and control_operations & used_operations:
                    return _response(
                        verified=False,
                        outcome="invalid_evidence",
                        reason="负对照与目标证据共享操作，不能作为独立对照",
                    )
                artifact_bytes: dict[str, bytes] = {}
                for ref in resolution.refs:
                    row = await db.get(ArtifactRow, UUID(ref))
                    if row is None:
                        return _response(
                            verified=False,
                            outcome="invalid_evidence",
                            reason=f"工件不存在：{ref}",
                        )
                    artifact_bytes[ref] = toolbox.artifacts.read_bytes(row.relative_key)
                evaluation = evaluate_recipe(
                    recipe_id,
                    checks,
                    artifact_bytes,
                    claim_scope=claim_scope,
                    statistics_payload=statistics_payload,
                )

        if evaluation.outcome is not RecipeOutcome.PASS:
            runtime.context.note_progress(f"verification:{recipe_id}:{evaluation.outcome}")
            return _response(
                verified=False,
                finding_id=finding_id,
                recipe_id=recipe_id,
                outcome=str(evaluation.outcome),
                reason=evaluation.reason,
                limitations=evaluation.limitations,
                tested_conditions=evaluation.tested_conditions,
            )

        first_roles = {"initial", "anonymous", "baseline", "login"}
        input_refs = [
            str(item["artifact_id"]) for item in checks if item["role"] in first_roles
        ]
        recheck_refs = [
            str(item["artifact_id"])
            for item in checks
            if item["role"] not in first_roles and item["role"] != "control"
        ]
        control_refs = [str(item["artifact_id"]) for item in checks if item["role"] == "control"]
        if not input_refs:
            input_refs = resolution.refs[:1]
        if not recheck_refs:
            recheck_refs = [ref for ref in resolution.refs if ref not in input_refs][:1]
        if not recheck_refs:
            recheck_refs = input_refs

        with _verification_stage(spans, "verification.recipe.confirm", toolbox) as span:
            async with toolbox.db.transaction() as db:
                verification = await VerificationService(db).record(
                    finding_id=finding_uuid,
                    method_version=RECIPES[recipe_id].method_version,
                    conclusion=FindingStatus.CONFIRMED,
                    input_evidence_refs=[*input_refs, *control_refs],
                    recheck_evidence_refs=recheck_refs,
                    tested_conditions={
                        **evaluation.tested_conditions,
                        "claim_scope": claim_scope,
                        "control_artifact_ids": control_refs,
                        "source_tool_call_id": tool_call_id,
                    },
                    limitations=evaluation.limitations,
                    performed_by="code",
                )
                span.attributes["verification_id"] = str(verification.id)
        runtime.context.note_progress(f"verification:{verification.id}")
        return _response(
            verified=True,
            finding_id=finding_id,
            verification_id=str(verification.id),
            recipe_id=recipe_id,
            outcome=str(evaluation.outcome),
            scope=claim_scope,
            limitations=evaluation.limitations,
        )
    finally:
        try:
            async with toolbox.db.transaction() as db:
                await spans.flush(db)
        except Exception as exc:
            log.warning(
                "verification_span_persist_failed",
                session_id=str(toolbox.session_id),
                error_type=type(exc).__name__,
            )


def _json_value(raw: Any, field: str) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} 不是合法 JSON") from exc
    return raw


def _json_list(raw: Any, field: str) -> list[str]:
    value = _json_value(raw, field)
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是数组")
    return [str(item) for item in value if str(item).strip()]


def _json_object(raw: Any, field: str) -> dict[str, Any]:
    if raw is None:
        return {}
    value = _json_value(raw, field)
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    return value


VERIFICATION_TOOLS = [verify_public_git_head, verify_finding]
