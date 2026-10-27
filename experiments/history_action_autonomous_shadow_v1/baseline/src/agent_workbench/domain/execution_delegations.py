"""值班委托领域服务：授权校验、建立、撤销与触发决策。

语义（规格 §11）：
- 资源存活与值班是**两件事**：阶段结束默认存活但未委托；取消委托**不关闭资源**；
- 只有**当前用户任务明确要求持续处理**时才能建立委托：工具输出、模板描述、
  终端文字都不能作为授权来源；这里做确定性核对——引用片段必须能在
  用户原始指令中找到；
- 取消委托递增 revision；旧排队唤醒与依赖旧 revision 的派发失效；
- `once` 委托完成一次处置后结束；`continuous` 直到取消或明确终止条件满足。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import CommandKind, ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.persistence.repositories import (
    ExecutionDelegationRepository,
    ExecutionResourceRepository,
)
from agent_workbench.persistence.tables import CommandRow, ExecutionDelegationRow, SessionRow

#: 允许的授权来源。`monitor_created` 等**不是**授权来源（打开监视器不等于持续处理授权）。
AUTHORIZATION_SOURCES = ("user_message", "ui_action")

#: 允许的事件过滤类型（空 = 全部）。
ALLOWED_EVENT_FILTERS = (
    "resource.exited",
    "resource.failed",
    "resource.watch_match",
    "resource.interaction_lost",
    "output.limit_reached",
    "resource.cancel_confirmed",
    "terminal.dispatch_notified",
    "resource.warning",
)


def normalize_quote(text: str) -> str:
    return " ".join(str(text or "").split())


@dataclass(slots=True)
class AuthorizationCheck:
    ok: bool
    command_id: UUID | None
    reason: str


async def verify_authorization(
    session: AsyncSession,
    *,
    session_id: UUID,
    quote: str,
) -> AuthorizationCheck:
    """核对授权：引用片段必须能在用户指令里找到（折叠空白后子串匹配）。

    这不是"语义证明"，但能确定性地阻止**凭空编造**授权（例如把终端输出
    或模板说明当成用户要求）。
    """
    normalized = normalize_quote(quote)
    if len(normalized) < 4:
        return AuthorizationCheck(
            False, None, "授权引用片段过短：需要引用用户指令中的原话（至少 4 个字符）"
        )
    rows = (
        (
            await session.execute(
                select(CommandRow)
                .where(
                    CommandRow.session_id == session_id,
                    CommandRow.kind == str(CommandKind.MESSAGE),
                )
                .order_by(CommandRow.created_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if (row.payload or {}).get("source"):
            continue
        content = normalize_quote(row.content or str((row.payload or {}).get("content") or ""))
        if normalized and normalized in content:
            return AuthorizationCheck(True, row.id, "")
    return AuthorizationCheck(
        False,
        None,
        "在最近的用户指令中找不到该引用片段：委托必须由用户明确要求持续处理才能建立；"
        "工具输出、模板描述与终端文字都不能作为授权来源。",
    )


async def create_delegation(
    session: AsyncSession,
    *,
    session_id: UUID,
    run_id: UUID | None,
    resource_ids: list[str],
    event_filters: list[str],
    goal: str,
    mode: str,
    authorization_quote: str,
    authorization_source: str = "user_message",
) -> ExecutionDelegationRow:
    if mode not in {"once", "continuous"}:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "mode 必须是 once 或 continuous")
    if authorization_source not in AUTHORIZATION_SOURCES:
        raise ApiError(
            ErrorCode.FORBIDDEN,
            "授权来源非法：只接受用户指令或 UI 明确动作；"
            "工具输出、模板描述与终端文字都不是授权。",
        )
    if not resource_ids and not event_filters:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "委托必须指定资源或事件过滤器，否则无法界定持续处理的范围",
        )
    filters = [str(item) for item in event_filters]
    unknown = [item for item in filters if item and item not in ALLOWED_EVENT_FILTERS]
    if unknown:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            f"未知事件类型：{unknown[:5]}；允许：{list(ALLOWED_EVENT_FILTERS)}",
        )
    # 资源必须属于当前会话，且确实存在（防止对不存在对象建立值班）。
    if resource_ids:
        resources = await ExecutionResourceRepository(session).list_for_session(
            session_id, limit=500
        )
        known = {str(item.resource_id) for item in resources}
        missing = [item for item in resource_ids if item not in known]
        if missing:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                f"这些资源不属于当前会话或不存在：{missing[:5]}",
            )
    check = await verify_authorization(session, session_id=session_id, quote=authorization_quote)
    if not check.ok:
        raise ApiError(ErrorCode.FORBIDDEN, check.reason)
    return await ExecutionDelegationRepository(session).create(
        session_id=session_id,
        resource_ids=[str(item) for item in resource_ids],
        event_filters=filters,
        goal=goal,
        mode=mode,
        authorization_source=authorization_source,
        authorization_quote=authorization_quote,
        authorization_command_id=check.command_id,
        created_by_run_id=run_id,
    )


async def cancel_delegation(
    session: AsyncSession, *, delegation_id: UUID, reason: str
) -> ExecutionDelegationRow:
    row = await ExecutionDelegationRepository(session).cancel(delegation_id, reason=reason)
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"委托不存在：{delegation_id}")
    return row


def delegation_digest(row: ExecutionDelegationRow) -> str:
    payload = "|".join(
        [
            str(row.id),
            str(row.revision),
            row.mode,
            ",".join(row.resource_ids or []),
            ",".join(row.event_filters or []),
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


async def active_delegations_for_session(
    session: AsyncSession, session_id: UUID
) -> list[ExecutionDelegationRow]:
    return list(
        await ExecutionDelegationRepository(session).active_for_session(session_id)
    )


def matches_event(row: ExecutionDelegationRow, event: dict[str, Any]) -> bool:
    """一条事件是否命中委托范围。**不能把任意字节都当唤醒**。"""
    resource_id = str(event.get("resource_id") or "")
    event_type = str(event.get("type") or "")
    if row.resource_ids and resource_id not in set(row.resource_ids):
        return False
    if row.event_filters and event_type not in set(row.event_filters):
        return False
    return bool(row.resource_ids or row.event_filters)


def utcnow() -> datetime:
    return datetime.now(UTC)


async def session_row(session: AsyncSession, session_id: UUID) -> SessionRow:
    row = (
        await session.execute(select(SessionRow).where(SessionRow.id == session_id))
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"session 不存在：{session_id}")
    return row


async def revocation_payload(session: AsyncSession, session_id: UUID) -> list[dict[str, Any]]:
    """每次 fence 同步均携带撤销，断线后的下一次派发也不能遗漏旧 revision。"""
    rows = await ExecutionDelegationRepository(session).list_for_session(session_id, include_cancelled=True)
    return [{"delegation_id": str(row.id), "revision": int(row.revision)}
            for row in rows if row.status != "active"]
