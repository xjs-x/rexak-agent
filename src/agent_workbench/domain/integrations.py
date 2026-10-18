"""领域整合回执：窄审计，不保存计划状态。"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.persistence.tables import DomainIntegrationRow


def canonical_request_hash(
    *,
    action: str,
    arguments: dict[str, Any],
    session_id: UUID,
    run_id: UUID,
) -> str:
    payload = {
        "action": action,
        "arguments": arguments,
        "session_id": str(session_id),
        "run_id": str(run_id),
    }
    body = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


async def record_integration(
    db: AsyncSession,
    *,
    session_id: UUID,
    run_id: UUID | None,
    entity_kind: str,
    entity_id: UUID | None,
    action: str,
    source_tool_call_id: str,
    request_hash: str,
    status: str,
    error_code: str | None = None,
    error_detail: str | None = None,
    retryable: bool = False,
    result_event_seq: int | None = None,
    successful_domain_revision: int | None = None,
) -> DomainIntegrationRow:
    """按请求哈希幂等记录一次整合。

    同一来源调用不能改写为不同请求；同一请求重复提交返回已有回执。
    已成功的整合不会被后续失败降级。
    """
    by_call = (
        await db.execute(
            select(DomainIntegrationRow).where(
                DomainIntegrationRow.session_id == session_id,
                DomainIntegrationRow.action == action,
                DomainIntegrationRow.source_tool_call_id == source_tool_call_id,
            )
        )
    ).scalar_one_or_none()
    if by_call is not None and by_call.request_hash != request_hash:
        raise ValueError("同一工具调用不能改写为不同请求哈希")

    row = by_call or (
        await db.execute(
            select(DomainIntegrationRow).where(
                DomainIntegrationRow.session_id == session_id,
                DomainIntegrationRow.action == action,
                DomainIntegrationRow.request_hash == request_hash,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = DomainIntegrationRow(
            session_id=session_id,
            run_id=run_id,
            entity_kind=entity_kind,
            entity_id=entity_id,
            action=action,
            source_tool_call_id=source_tool_call_id,
            request_hash=request_hash,
            status=status,
            error_code=error_code,
            error_detail=error_detail,
            retryable=retryable,
            result_event_seq=result_event_seq,
            successful_domain_revision=successful_domain_revision,
        )
        db.add(row)
        await db.flush()
        return row

    if row.status == "integrated" and status != "integrated":
        return row
    row.entity_kind = entity_kind
    row.entity_id = entity_id or row.entity_id
    row.status = status
    row.error_code = error_code
    row.error_detail = error_detail
    row.retryable = retryable
    row.result_event_seq = result_event_seq or row.result_event_seq
    row.successful_domain_revision = (
        successful_domain_revision
        if successful_domain_revision is not None
        else row.successful_domain_revision
    )
    await db.flush()
    return row


__all__ = ["canonical_request_hash", "record_integration"]
