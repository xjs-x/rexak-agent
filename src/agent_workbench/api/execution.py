"""控制端执行 API：资源、输出、值班与环境（挂到 `/api/sessions/{id}/execution/...`）。

边界（规格 §13）：
- API **不驱动 Agent**，也不把 stdout 全文塞进 SSE；
- 复用现有 Cookie/Origin/CSRF 与命令受理机制；
- 取消/关闭使用**持久操作 ID**（服务端生成；同幂等键重复请求返回同一结果）；
- 面板只读投影与执行端真实状态分开显示："取消值班"不关闭资源；
  "关闭资源"必须显示取消是否确认。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Header, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.execution_runtime import (
    EXECUTION_SUBPROTOCOL_VERSION,
    DelegationRevocation,
    ExecutionControlSync,
    ExecutionEnvironmentReport,
    ExecutionOperationEnvelope,
    OutputPageV2,
    ResourceStatusView,
    execution_request_digest,
)
from agent_workbench.execution.client import (
    ExecutorConnectionError,
    ExecutorProtocolError,
    ExecutorRejected,
    HttpExecutorClient,
)
from agent_workbench.execution.tunnel import SshTunnel, TunnelUnavailable
from agent_workbench.persistence.repositories import (
    ExecutionDelegationRepository,
    ExecutionResourceRepository,
    ToolOperationRepository,
)
from agent_workbench.persistence.tables import RunRow, SessionRow

router = APIRouter(prefix="/api/sessions/{session_id}/execution", tags=["execution"])


class DutyCreateRequest(BaseModel):
    """UI 建立值班的请求体。`authorization_quote` 必须引用用户消息原话。"""

    model_config = ConfigDict(extra="forbid")

    resource_ids: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    goal: str = ""
    mode: str = "once"
    authorization_quote: str = ""


class ExecutionResourceCard(BaseModel):
    """资源卡：真实状态、交互是否可用、创建时间、最近核对、用途与配额。"""

    model_config = ConfigDict(extra="forbid")

    resource_id: str
    kind: str
    mode: str = ""
    purpose: str = ""
    state: str
    interaction_state: str = "not_applicable"
    exit_code: int | None = None
    termination_reason: str | None = None
    close_state: str = "open"
    close_confirmed: bool = False
    last_checked_at: str | None = None
    sync_status: str = "in_sync"
    output_limit_action: str = "preserve_process"
    quota: dict[str, Any] = Field(default_factory=dict)
    detail: dict[str, Any] = Field(default_factory=dict)


class DutyCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delegation_id: str
    mode: str
    status: str
    revision: int
    goal: str = ""
    resource_ids: list[str] = Field(default_factory=list)
    event_filters: list[str] = Field(default_factory=list)
    triggered_count: int = 0
    created_at: str | None = None
    cancelled_at: str | None = None


class ExecutionResourcesResponse(BaseModel):
    """资源列表：资源状态与值班状态**分开**呈现（取消值班不关闭资源）。"""

    model_config = ConfigDict(extra="forbid")

    resources: list[ExecutionResourceCard]
    count: int
    duties: list[DutyCard]
    executor_note: str = ""
    note: str = ""


class DutyListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duties: list[DutyCard]


class DutyMutationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delegation_id: str
    status: str
    revision: int | None = None
    note: str = ""


class ExecutionOperationResponse(BaseModel):
    """关闭/取消操作回执。`process_confirmed_exited=false` 时必须显示为"未确认"。"""

    model_config = ConfigDict(extra="allow")

    operation_id: str
    status: str
    resource_id: str | None = None
    process_state: str | None = None
    process_confirmed_exited: bool | None = None
    termination_reason: str | None = None
    deduplicated: bool = False
    note: str = ""


class TerminateAllResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    terminated: list[dict[str, Any]]
    unresolved: list[str]
    note: str


class ResourceOutputResponse(OutputPageV2):
    """资源输出页（stream 或 screen）。"""

    model_config = ConfigDict(extra="allow")

    view: str = "stream"

#: 面板一次刷新最多枚举的资源数量（有界）。
MAX_RESOURCE_SCAN = 200


def _now() -> datetime:
    return datetime.now(UTC)


async def _session_row(db: Any, session_id: UUID) -> SessionRow:
    row = (
        await db.execute(
            select(SessionRow).where(
                SessionRow.id == session_id, SessionRow.status != "deleted"
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "任务不存在或已删除")
    return row


async def _guard(request: Request, session_id: UUID, *, write: bool) -> None:
    from agent_workbench.api.routes import get_auth

    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.require_session(request)
    if write:
        auth.require_csrf(request, ticket)
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as db:
        await _session_row(db, session_id)


def _require_execution_capability(caps: Any) -> None:
    if not getattr(caps, "execution", None):
        raise ApiError(
            ErrorCode.EXECUTOR_INCOMPATIBLE,
            "执行岛未声明 /execution/v1 子协议：执行资源功能不可用",
        )


class _ExecutorSession:
    """一个请求内的执行端连接（隧道 + 客户端），确保成对关闭。"""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.tunnel = SshTunnel(self.settings)
        self.client = HttpExecutorClient(
            self.tunnel, settings=self.settings, expected_executor_id=self.settings.executor_id
        )

    async def __aenter__(self) -> HttpExecutorClient:
        try:
            await self.client.start()
        except (TunnelUnavailable, ExecutorConnectionError) as exc:
            await self._close()
            raise ApiError(
                ErrorCode.EXECUTOR_UNAVAILABLE,
                f"执行岛不可达：{type(exc).__name__}",
            ) from exc
        return self.client

    async def __aexit__(self, *exc: Any) -> None:
        await self._close()

    async def _close(self) -> None:
        for item in (self.client, self.tunnel):
            close = getattr(item, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                continue


# --------------------------------------------------------------------------- #
# 资源
# --------------------------------------------------------------------------- #


@router.get("/resources", response_model=ExecutionResourcesResponse)
async def list_resources(
    session_id: UUID,
    request: Request,
    kind: str | None = Query(default=None),
    active_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    refresh: bool = Query(default=True),
) -> ExecutionResourcesResponse:
    """资源列表（只读投影 + 执行端刷新）。真实状态归执行端。"""
    await _guard(request, session_id, write=False)
    from agent_workbench.persistence.db import get_database

    resources: list[dict[str, Any]] = []
    executor_note = ""
    if refresh:
        try:
            async with _ExecutorSession() as client:
                caps = await client.capabilities()
                _require_execution_capability(caps)
                page = await client.execution_resources(
                    session_id=str(session_id), limit=MAX_RESOURCE_SCAN, offset=0, kind=kind
                )
            resources = list(page.get("resources") or [])
        except ApiError as exc:
            executor_note = exc.message
        except (ExecutorProtocolError, ExecutorConnectionError) as exc:
            executor_note = f"执行端刷新失败（显示上次同步的投影）：{type(exc).__name__}"
    async with get_database().transaction() as db:
        repository = ExecutionResourceRepository(db)
        for item in resources:
            try:
                resource_id = UUID(str(item.get("resource_id")))
            except (TypeError, ValueError):
                continue
            await repository.upsert(
                resource_id=resource_id,
                session_id=session_id,
                executor_id=get_settings().executor_id,
                kind=str(item.get("kind") or "command"),
                state=str(item.get("state") or "unknown"),
                interaction_state=str(item.get("interaction_state") or "not_applicable"),
                mode=str(item.get("mode") or ""),
                purpose=str(item.get("purpose") or ""),
                exit_code=item.get("exit_code"),
                termination_reason=item.get("termination_reason"),
                output_limit_action=str(item.get("output_limit_action") or "preserve_process"),
                quota={
                    "output_bytes": item.get("output_bytes"),
                    "dropped_bytes": item.get("dropped_bytes"),
                    "output_limit_bytes": item.get("output_limit_bytes"),
                },
                detail={"notes": item.get("notes") or []},
            )
        projections = list(
            await repository.list_for_session(
                session_id, active_only=active_only, limit=limit, offset=offset
            )
        )
        delegations = list(
            await ExecutionDelegationRepository(db).list_for_session(
                session_id, include_cancelled=True
            )
        )
    cards = [
        ExecutionResourceCard(
            resource_id=str(row.resource_id),
            kind=row.kind,
            mode=row.mode,
            purpose=row.purpose,
            state=row.state,
            interaction_state=row.interaction_state,
            exit_code=row.exit_code,
            termination_reason=row.termination_reason,
            close_state=row.close_state,
            close_confirmed=row.close_confirmed,
            last_checked_at=row.last_checked_at.isoformat() if row.last_checked_at else None,
            sync_status=row.sync_status,
            output_limit_action=row.output_limit_action,
            quota=dict(row.quota or {}),
            detail=dict(row.detail or {}),
        )
        for row in projections
    ]
    if active_only:
        cards = [item for item in cards if item.state in {"starting", "running", "unknown"}]
    return ExecutionResourcesResponse(
        resources=cards,
        count=len(cards),
        duties=[_duty_card(row) for row in delegations],
        executor_note=executor_note,
        note=(
            "资源状态（执行端）与值班状态（委托）是分开的两件事；"
            "取消值班不会关闭资源。列表显示的是控制端投影，真实状态以执行台账为准。"
        ),
    )


def _duty_card(row: Any) -> DutyCard:
    return DutyCard(
        delegation_id=str(row.id),
        mode=row.mode,
        status=row.status,
        revision=int(row.revision),
        goal=row.goal,
        resource_ids=list(row.resource_ids or []),
        event_filters=list(row.event_filters or []),
        triggered_count=int(row.triggered_count),
        created_at=row.created_at.isoformat() if row.created_at else None,
        cancelled_at=row.cancelled_at.isoformat() if row.cancelled_at else None,
    )


@router.get("/resources/{resource_id}", response_model=ResourceStatusView)
async def get_resource(
    session_id: UUID, resource_id: str, request: Request
) -> ResourceStatusView:
    await _guard(request, session_id, write=False)
    await _assert_resource_owner(session_id, resource_id)
    async with _ExecutorSession() as client:
        payload = await client.execution_resource(resource_id)
    return ResourceStatusView.model_validate(
        {key: value for key, value in payload.items() if key in ResourceStatusView.model_fields}
    )


@router.get("/resources/{resource_id}/output", response_model=ResourceOutputResponse)
async def read_resource_output(
    session_id: UUID,
    resource_id: str,
    request: Request,
    stream: str = Query(default="stdout"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=16384, ge=1, le=262144),
    view: str = Query(default="stream"),
) -> ResourceOutputResponse:
    """分页读取 stdout/stderr 或屏幕快照。缺失区间与证据不可用会明确标注。"""
    await _guard(request, session_id, write=False)
    await _assert_resource_owner(session_id, resource_id)
    async with _ExecutorSession() as client:
        payload = await client.execution_output(
            resource_id,
            stream=stream,
            cursor=cursor,
            limit=limit,
            view=view,
            session_id=str(session_id),
        )
    payload.setdefault("view", view)
    return ResourceOutputResponse.model_validate(payload)


@router.get("/resources/{resource_id}/raw-output")
async def download_raw_output(
    session_id: UUID, resource_id: str, request: Request, stream: str = "stdout",
    offset: int = Query(default=0, ge=0), limit: int = Query(default=1048576, ge=1, le=1048576),
) -> Response:
    import base64
    import hashlib

    await _guard(request, session_id, write=False)
    await _assert_resource_owner(session_id, resource_id)
    async with _ExecutorSession() as client:
        snapshot = await client.execution_raw_output(resource_id, session_id=str(session_id),
                                                     stream=stream, offset=offset, limit=limit)
    data = base64.b64decode(snapshot["data_base64"], validate=True)
    if hashlib.sha256(data).hexdigest() != snapshot["sha256"]:
        raise ApiError(ErrorCode.EXECUTOR_INCOMPATIBLE, "日志快照 hash 不匹配")
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="execution-{resource_id}-{offset}.bin"',
        "X-Snapshot-Sha256": snapshot["sha256"], "X-Next-Offset": str(snapshot["next_offset"]),
        "X-Observed-Size": str(snapshot["observed_size"]),
    })


async def _assert_resource_owner(session_id: UUID, resource_id: str) -> None:
    from agent_workbench.persistence.db import get_database
    from agent_workbench.persistence.tables import ExecutionResourceRow

    try:
        parsed = UUID(resource_id)
    except ValueError:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "resource_id 不是合法 UUID") from None
    async with get_database().session() as db:
        projection = (
            await db.execute(
                select(ExecutionResourceRow).where(
                    ExecutionResourceRow.resource_id == parsed,
                    ExecutionResourceRow.session_id == session_id,
                )
            )
        ).scalar_one_or_none()
    if projection is None:
        raise ApiError(
            ErrorCode.NOT_FOUND,
            "该资源不属于当前任务（跨会话访问被拒绝）；如为早期资源请先用列表刷新",
        )


# --------------------------------------------------------------------------- #
# 关闭 / 终止
# --------------------------------------------------------------------------- #


async def _issue_operation(
    *,
    session_id: UUID,
    resource_id: str,
    action: str,
    arguments: dict[str, Any],
    idempotency_key: str,
    wait_ms: int = 20_000,
) -> dict[str, Any]:
    """为 UI 操作生成**持久操作身份**并派发（幂等键 -> tool_call_id）。"""
    from agent_workbench.persistence.db import get_database

    async with get_database().transaction() as db:
        from agent_workbench.persistence.tables import ExecutionResourceRow

        projection = (
            await db.execute(
                select(ExecutionResourceRow).where(
                    ExecutionResourceRow.resource_id == UUID(resource_id)
                )
            )
        ).scalar_one_or_none()
        if projection is None or projection.session_id != session_id:
            raise ApiError(ErrorCode.NOT_FOUND, "资源不属于当前任务")
        run_id = (await db.execute(
            select(RunRow.id).where(RunRow.session_id == session_id)
            .order_by(RunRow.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if run_id is None:
            raise ApiError(ErrorCode.CONFLICT, "该任务没有可关联的 run，无法生成执行操作身份")
        run = await db.get(RunRow, run_id)
        assert run is not None
        repository = ToolOperationRepository(db)
        digest_payload = {"tool": "ui_execution", "action": action, "resource": resource_id}
        from agent_workbench.persistence.repositories import compute_request_digest

        digest = compute_request_digest(digest_payload)
        operation_id = uuid4()
        row, _created = await repository.prepare(
            operation_id=operation_id,
            run_id=run_id,
            session_id=session_id,
            tool_call_id=f"ui:{idempotency_key}",
            tool_name="ui_execution",
            request_digest=digest,
            effect_class="side_effecting",
            policy_revision=int((await _policy_revision(session_id)) or 1),
            scope_revision=1,
            dispatch_control_version=run.control_version,
        )
        operation_id = row.operation_id
        # 关闭操作可用同一逻辑身份继续核对；unknown 不伪装成已确认，也不创建新命令。
        from agent_workbench.domain.execution_delegations import revocation_payload
        revocations = await revocation_payload(db, session_id)
        owner_generation = int(run.owner_generation)
        control_version = int(run.control_version)
        owner = run.owner or ""
    try:
        async with _ExecutorSession() as client:
            ack = await client.execution_sync_control(
                ExecutionControlSync(
                    session_id=str(session_id),
                    run_id=str(run_id),
                    owner=owner,
                    owner_generation=owner_generation,
                    control_version=control_version,
                    expected_seq=None,
                    revocations=[DelegationRevocation.model_validate(item) for item in revocations],
                ).model_dump(mode="json")
            )
            fence_seq = int((ack.get("fence") or {}).get("seq") or 0)
            envelope = ExecutionOperationEnvelope(
                schema_version=EXECUTION_SUBPROTOCOL_VERSION,
                operation_id=str(operation_id),
                session_id=str(session_id),
                run_id=str(run_id),
                tool_call_id=f"ui:{idempotency_key}",
                request_digest=execution_request_digest(action, arguments),
                action=action,
                arguments=arguments,
                owner_generation=owner_generation,
                dispatch_control_version=control_version,
                session_control_seq=fence_seq,
                wait_ms=wait_ms,
                created_at=_now().isoformat(),
            )
            receipt = await client.execution_submit(envelope.model_dump(mode="json"))
    except (TunnelUnavailable, ExecutorConnectionError) as exc:
        raise ApiError(
            ErrorCode.EXECUTOR_UNAVAILABLE,
            f"执行岛不可达，操作未送达：{type(exc).__name__}",
        ) from exc
    except ExecutorProtocolError as exc:
        raise ApiError(ErrorCode.EXECUTOR_INCOMPATIBLE, str(exc)) from exc
    async with get_database().transaction() as db:
        await ToolOperationRepository(db).mark_result(
            operation_id, status=str(receipt.get("status") or "succeeded")
        )
        try:
            resource_uuid = UUID(str(receipt.get("resource_id") or resource_id))
        except (TypeError, ValueError):
            resource_uuid = UUID(resource_id)
        confirmed = receipt.get("process_confirmed_exited") is True if receipt.get("process_confirmed_exited") is not None else receipt.get("process_state") in {"closed", "exited"}
        await ExecutionResourceRepository(db).apply_close_result(
            resource_uuid,
            confirmed=confirmed,
            state=str(receipt.get("process_state") or "closed") if confirmed else "unknown",
            detail={"operation_id": str(operation_id)},
        )
    return {**receipt, "operation_id": str(operation_id), "deduplicated": False}


async def _policy_revision(session_id: UUID) -> int | None:
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as db:
        row = await db.get(SessionRow, session_id)
    return int((row.policy_snapshot or {}).get("revision", 1)) if row is not None else 1


@router.post("/resources/{resource_id}/close", response_model=ExecutionOperationResponse)
async def close_resource(
    session_id: UUID,
    resource_id: str,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> ExecutionOperationResponse:
    """显式关闭**单个**资源。取消/关闭未确认时返回 unknown，不虚报"已停止"。"""
    await _guard(request, session_id, write=True)
    await _assert_resource_owner(session_id, resource_id)
    from agent_workbench.persistence.db import get_database

    async with get_database().transaction() as db:
        await ExecutionResourceRepository(db).mark_close_requested(UUID(resource_id))
    async with get_database().session() as db:
        from agent_workbench.persistence.tables import ExecutionResourceRow

        projection = (
            await db.execute(
                select(ExecutionResourceRow).where(
                    ExecutionResourceRow.resource_id == UUID(resource_id)
                )
            )
        ).scalar_one_or_none()
    action = "terminal_close" if projection is not None and projection.kind == "terminal" else "cancel_resource"
    arguments: dict[str, Any] = {"resource_id": resource_id}
    if action == "terminal_close":
        arguments = {"terminal_id": resource_id, "escalate": True}
    try:
        receipt = await _issue_operation(
            session_id=session_id,
            resource_id=resource_id,
            action=action,
            arguments=arguments,
            idempotency_key=idempotency_key,
            wait_ms=20_000,
        )
    except ApiError:
        raise
    return ExecutionOperationResponse.model_validate(receipt)


@router.post("/resources/terminate-all", response_model=TerminateAllResponse)
async def terminate_all(
    session_id: UUID,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> TerminateAllResponse:
    """**明确终止全部资源**：作用于当前对话（含早期 run 创建后保留的资源）。

    返回逐资源结果与未决清单；无法确认的资源保持未决，不清空记录。
    """
    await _guard(request, session_id, write=True)
    from agent_workbench.persistence.db import get_database

    async with get_database().transaction() as db:
        for duty in list(await ExecutionDelegationRepository(db).active_for_session(session_id)):
            await ExecutionDelegationRepository(db).cancel(duty.id, reason="用户终止全部资源")
        rows = list(await ExecutionResourceRepository(db).active_for_session(session_id))
    results: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for index, row in enumerate(rows):
        resource_id = str(row.resource_id)
        action = "terminal_close" if row.kind == "terminal" else "cancel_resource"
        arguments: dict[str, Any] = (
            {"terminal_id": resource_id, "escalate": True}
            if action == "terminal_close"
            else {"resource_id": resource_id}
        )
        try:
            receipt = await _issue_operation(
                session_id=session_id,
                resource_id=resource_id,
                action=action,
                arguments=arguments,
                idempotency_key=f"{idempotency_key}:{index}:{resource_id}",
                wait_ms=20_000,
            )
            confirmed = (receipt.get("process_confirmed_exited") is True
                         if receipt.get("process_confirmed_exited") is not None
                         else receipt.get("process_state") in {"closed", "exited"})
            results.append(
                {"resource_id": resource_id, "confirmed": confirmed, "receipt": receipt}
            )
            if not confirmed:
                unresolved.append(resource_id)
        except ApiError as exc:
            results.append(
                {"resource_id": resource_id, "confirmed": False, "error": exc.message}
            )
            unresolved.append(resource_id)
    return TerminateAllResponse(
        terminated=results,
        unresolved=unresolved,
        note=(
            "未确认的资源仍会出现在列表中；系统不会虚报全部关闭，"
            "也不会删除台账记录。"
        ),
    )


# --------------------------------------------------------------------------- #
# 值班
# --------------------------------------------------------------------------- #


@router.get("/duties", response_model=DutyListResponse)
async def list_duties(session_id: UUID, request: Request) -> DutyListResponse:
    await _guard(request, session_id, write=False)
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as db:
        rows = await ExecutionDelegationRepository(db).list_for_session(
            session_id, include_cancelled=True
        )
    return DutyListResponse(duties=[_duty_card(row) for row in rows])


@router.post("/duties")
async def create_duty(
    session_id: UUID,
    request: Request,
    payload: DutyCreateRequest,
) -> DutyMutationResponse:
    """UI 建立值班：`authorization_quote` 必须引用用户消息原话（确定性核对）。"""
    await _guard(request, session_id, write=True)
    from agent_workbench.domain.execution_delegations import create_delegation
    from agent_workbench.persistence.db import get_database

    async with get_database().transaction() as db:
        row = await create_delegation(
            db,
            session_id=session_id,
            run_id=None,
            resource_ids=list(payload.resource_ids),
            event_filters=list(payload.events),
            goal=payload.goal,
            mode=payload.mode,
            authorization_quote=payload.authorization_quote,
            authorization_source="ui_action",
        )
        return DutyMutationResponse(
            delegation_id=str(row.id),
            status=row.status,
            revision=int(row.revision),
            note=f"{row.mode} 委托已建立（仅在此后有效委托期间自动处理事件）。",
        )


@router.post("/duties/{delegation_id}/cancel", response_model=DutyMutationResponse)
async def cancel_duty(
    session_id: UUID,
    delegation_id: UUID,
    request: Request,
    reason: Annotated[str, Body(embed=True)] = "",
) -> DutyMutationResponse:
    """取消值班：**不关闭资源**；旧 revision 不再触发新副作用。"""
    await _guard(request, session_id, write=True)
    from agent_workbench.persistence.db import get_database

    async with get_database().transaction() as db:
        row = await ExecutionDelegationRepository(db).get(delegation_id)
        if row is None or row.session_id != session_id:
            raise ApiError(ErrorCode.NOT_FOUND, "委托不存在或不属于当前任务")
        cancelled = await ExecutionDelegationRepository(db).cancel(
            delegation_id, reason=reason or "UI 取消值班"
        )
    from agent_workbench.domain.execution_delegations import revocation_payload
    remote_sync = "pending"
    async with get_database().session() as db:
        run = (await db.execute(select(RunRow).where(RunRow.session_id == session_id)
                               .order_by(RunRow.created_at.desc()).limit(1))).scalar_one_or_none()
        revocations = await revocation_payload(db, session_id)
    if run is not None:
        try:
            async with _ExecutorSession() as client:
                ack = await client.execution_sync_control({"session_id": str(session_id),
                    "run_id": str(run.id), "owner": run.owner or "",
                    "owner_generation": int(run.owner_generation), "control_version": int(run.control_version),
                    "expected_seq": None, "revocations": revocations})
                if ack.get("applied"):
                    remote_sync = "acknowledged"
        except (TunnelUnavailable, ExecutorConnectionError, ExecutorProtocolError, ExecutorRejected):
            remote_sync = "pending"
    return DutyMutationResponse(
        delegation_id=str(delegation_id),
        status=cancelled.status if cancelled else "unknown",
        revision=int(cancelled.revision) if cancelled else None,
        note="值班已取消；资源未被关闭。" + (
            "远端撤销已确认。" if remote_sync == "acknowledged" else "远端撤销待同步，在途操作可能已执行。"),
    )


# --------------------------------------------------------------------------- #
# 环境
# --------------------------------------------------------------------------- #


@router.get("/environment", response_model=ExecutionEnvironmentReport)
async def environment(session_id: UUID, request: Request) -> ExecutionEnvironmentReport:
    await _guard(request, session_id, write=False)
    async with _ExecutorSession() as client:
        try:
            payload = await client.execution_environment()
        except (TunnelUnavailable, ExecutorConnectionError, ExecutorProtocolError) as exc:
            # 执行端不可达是可解释的服务状态，不是未处理异常；返回 503 让 UI 明确显示“不可达”。
            raise ApiError(
                ErrorCode.EXECUTOR_UNAVAILABLE,
                f"执行端不可达：{exc}",
            ) from None
    return ExecutionEnvironmentReport.model_validate(payload)
