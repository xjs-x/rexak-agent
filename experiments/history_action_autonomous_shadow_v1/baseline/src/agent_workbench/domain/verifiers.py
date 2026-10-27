"""窄范围、确定性的发现复核；不把模型判断或 HTTP 200 当作漏洞结论。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import SplitResult, urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import ErrorCode, FindingStatus
from agent_workbench.contracts.errors import ApiError
from agent_workbench.domain.findings import VerificationService
from agent_workbench.evidence.artifacts import ArtifactError, ArtifactStore
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import (
    ArtifactRow,
    FindingRow,
    ToolJobRow,
    ToolOperationRow,
)

_HEAD_REF = re.compile(rb"\Aref: refs/heads/[A-Za-z0-9._/-]{1,200}\r?\n?\Z")


@dataclass(frozen=True, slots=True)
class HttpEvidence:
    operation_id: UUID
    body_artifact_id: UUID
    meta_artifact_id: UUID
    url: str


def validate_public_git_head_url(url: str) -> SplitResult:
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "复核地址格式无效") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.netloc.endswith(":")
        or parts.username is not None
        or parts.password is not None
        or parts.path != "/.git/HEAD"
        or parts.query
        or parts.fragment
    ):
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "当前核验器只接受无凭据、无查询参数的 http(s)://目标/.git/HEAD",
        )
    return parts


def _same_endpoint(left: str, right: str) -> bool:
    try:
        a, b = urlsplit(left), urlsplit(right)
        return (
            a.scheme == b.scheme
            and a.hostname == b.hostname
            and (a.port or (443 if a.scheme == "https" else 80))
            == (b.port or (443 if b.scheme == "https" else 80))
            and a.path == b.path
            and not a.query
            and not b.query
            and not a.fragment
            and not b.fragment
        )
    except ValueError:
        return False


async def _load_artifact(
    db: AsyncSession, store: ArtifactStore, session_id: UUID, artifact_id: UUID, kind: str
) -> tuple[ArtifactRow, bytes]:
    row = (
        await db.execute(select(ArtifactRow).where(ArtifactRow.id == artifact_id))
    ).scalar_one_or_none()
    if (
        row is None
        or row.session_id != session_id
        or row.availability != "available"
        or row.truncated
        or row.source_operation_id is None
        or not (row.summary or "").startswith(f"response.{kind}（")
        or row.byte_size > (512 if kind == "body" else 256 * 1024)
    ):
        raise ApiError(ErrorCode.CONFLICT, f"{kind} 工件不可用于独立核验：{artifact_id}")
    try:
        data = store.read_bytes(row.relative_key)
    except (ArtifactError, OSError, ValueError) as exc:
        raise ApiError(ErrorCode.CONFLICT, f"{kind} 工件无法读取：{artifact_id}") from exc
    if len(data) != row.byte_size or hashlib.sha256(data).hexdigest() != row.sha256:
        raise ApiError(ErrorCode.CONFLICT, f"{kind} 工件大小或哈希不符：{artifact_id}")
    return row, data


async def _read_http_evidence(
    db: AsyncSession,
    store: ArtifactStore,
    session_id: UUID,
    url: str,
    body_id: UUID,
    meta_id: UUID,
    expected_identity: str | None = None,
) -> HttpEvidence:
    body_row, body = await _load_artifact(db, store, session_id, body_id, "body")
    meta_row, meta_bytes = await _load_artifact(db, store, session_id, meta_id, "meta.json")
    if (
        body_row.source_operation_id is None
        or body_row.source_operation_id != meta_row.source_operation_id
    ):
        raise ApiError(ErrorCode.CONFLICT, "正文和元数据必须来自同一 HTTP 操作")
    if expected_identity is not None and (
        body_row.captured_by_identity != expected_identity
        or meta_row.captured_by_identity != expected_identity
    ):
        raise ApiError(ErrorCode.CONFLICT, "复测证据不来自本次隔离的 HTTP 身份")
    operation_id = body_row.source_operation_id
    operation = (
        await db.execute(
            select(ToolOperationRow).where(
                ToolOperationRow.operation_id == operation_id,
                ToolOperationRow.session_id == session_id,
                ToolOperationRow.tool_name == "http_request",
                ToolOperationRow.status == "succeeded",
            )
        )
    ).scalar_one_or_none()
    if operation is None or operation.job_id is None:
        raise ApiError(ErrorCode.CONFLICT, "来源 HTTP 操作未成功完成")
    job = (
        await db.execute(
            select(ToolJobRow).where(
                ToolJobRow.job_id == operation.job_id,
                ToolJobRow.operation_id == operation_id,
                ToolJobRow.state == "succeeded",
            )
        )
    ).scalar_one_or_none()
    if job is None:
        raise ApiError(ErrorCode.CONFLICT, "来源执行任务未成功完成")
    try:
        meta: Any = json.loads(meta_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(ErrorCode.CONFLICT, "HTTP 元数据不是合法 UTF-8 JSON") from exc
    if not isinstance(meta, dict):
        raise ApiError(ErrorCode.CONFLICT, "HTTP 元数据格式无效")
    if (
        meta.get("status_code") != 200
        or meta.get("truncated") is not False
        or meta.get("redirect_chain") != []
        or not isinstance(meta.get("url"), str)
        or not isinstance(meta.get("final_url"), str)
        or not _same_endpoint(meta["url"], url)
        or not _same_endpoint(meta["final_url"], url)
        or meta.get("body_bytes") != len(body)
        or meta.get("body_sha256") != hashlib.sha256(body).hexdigest()
        or not _HEAD_REF.fullmatch(body)
        or b".." in body
        or b"//" in body
    ):
        raise ApiError(
            ErrorCode.CONFLICT,
            "HTTP 证据未满足公开 Git HEAD 引用的确定性判据",
        )
    return HttpEvidence(operation_id, body_id, meta_id, url)


async def confirm_public_git_head(
    db: AsyncSession,
    store: ArtifactStore,
    *,
    session_id: UUID,
    finding_id: UUID,
    url: str,
    initial_body_id: UUID,
    initial_meta_id: UUID,
    recheck_body_id: UUID,
    recheck_meta_id: UUID,
    recheck_identity: str,
) -> UUID:
    """初次观察与独立匿名复测均符合严格判据时，确认唯一、精确的结论。"""
    validate_public_git_head_url(url)
    finding = (
        await db.execute(
            select(FindingRow)
            .where(FindingRow.id == finding_id, FindingRow.session_id == session_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if finding is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"候选发现不存在：{finding_id}")
    if finding.status != "candidate" or finding.category != "info_disclosure":
        raise ApiError(ErrorCode.CONFLICT, "此核验器只处理信息泄露类候选发现")
    original_refs = set(finding.evidence_refs or [])
    if not {str(initial_body_id), str(initial_meta_id)} <= original_refs:
        raise ApiError(ErrorCode.CONFLICT, "初次观察工件未绑定在候选发现上")

    initial = await _read_http_evidence(
        db, store, session_id, url, initial_body_id, initial_meta_id
    )
    recheck = await _read_http_evidence(
        db,
        store,
        session_id,
        url,
        recheck_body_id,
        recheck_meta_id,
        expected_identity=recheck_identity,
    )
    if initial.operation_id == recheck.operation_id:
        raise ApiError(ErrorCode.CONFLICT, "独立复测必须来自另一次 HTTP 操作")

    verification = await VerificationService(db).record(
        finding_id=finding_id,
        method_version="public-git-head-v1",
        conclusion=FindingStatus.CONFIRMED,
        input_evidence_refs=[str(initial.body_artifact_id), str(initial.meta_artifact_id)],
        recheck_evidence_refs=[str(recheck.body_artifact_id), str(recheck.meta_artifact_id)],
        tested_conditions={
            "url": url,
            "method": "GET",
            "response_status": 200,
            "independent_operations": [str(initial.operation_id), str(recheck.operation_id)],
            "body_pattern": "Git HEAD branch reference",
        },
        limitations=(
            "仅确认该地址在两次请求中公开返回 Git HEAD 分支引用；"
            "未验证仓库对象、提交历史或源代码可读取。"
        ),
        performed_by="code",
    )
    finding.title = "公开可读的 Git HEAD 引用"
    finding.description = "候选观察和独立匿名复测均取得符合 Git HEAD 格式的响应。"
    finding.impact = "暴露仓库分支引用；不代表仓库对象或源代码已暴露。"
    await ArtifactRepository(db).protect(
        [initial_body_id, initial_meta_id, recheck_body_id, recheck_meta_id]
    )
    await db.flush()
    return verification.id


async def preflight_public_git_head(
    db: AsyncSession,
    store: ArtifactStore,
    *,
    session_id: UUID,
    finding_id: UUID,
    url: str,
    initial_body_id: UUID,
    initial_meta_id: UUID,
) -> None:
    """派发复测前先确认候选与初次证据，避免无意义的目标请求。"""
    validate_public_git_head_url(url)
    finding = (
        await db.execute(
            select(FindingRow).where(
                FindingRow.id == finding_id, FindingRow.session_id == session_id
            )
        )
    ).scalar_one_or_none()
    if finding is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"候选发现不存在：{finding_id}")
    if finding.status != "candidate" or finding.category != "info_disclosure":
        raise ApiError(ErrorCode.CONFLICT, "此核验器只处理信息泄露类候选发现")
    if not {str(initial_body_id), str(initial_meta_id)} <= set(finding.evidence_refs or []):
        raise ApiError(ErrorCode.CONFLICT, "初次观察工件未绑定在候选发现上")
    await _read_http_evidence(db, store, session_id, url, initial_body_id, initial_meta_id)
