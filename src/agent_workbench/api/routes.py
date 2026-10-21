"""控制端 API 路由（§12.1）。

硬约束：
- 请求处理函数**不运行整段 Agent**：消息只写 `commands` 并返回 202；
- 异步接收用 202，完成用 200，**两者不混用**；
- 统一错误体：`code、message、retryable、request_id、details`；
  422 校验失败、409 状态冲突、404 资源不存在。
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Body,
    Header,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import Text, and_, cast, literal, or_, select, tuple_, union_all
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from agent_workbench.agent.state import plan_step_kind, plan_step_lead_ids
from agent_workbench.api.auth import LocalAuth
from agent_workbench.api.redaction import redact_api_value as _redact_api_value
from agent_workbench.api.redaction import redact_event_payload
from agent_workbench.api.sse import SSE_HEADERS, event_stream
from agent_workbench.api.tool_activity import router as tool_activity_router
from agent_workbench.config.catalog import PriceCatalog
from agent_workbench.config.model_settings import read_preferences
from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.conversation import ConversationBody, ConversationPage
from agent_workbench.contracts.diagnostics import ModelCallPage, OperationPage, SpanPage
from agent_workbench.contracts.enums import ACTIVE_RUN_STATUSES, CommandKind, ErrorCode, RunStatus
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.events import EventPage, EventType
from agent_workbench.contracts.identity import (
    ProjectCreate,
    ScopeSnapshot,
    ScopeUpdate,
    SessionCreate,
)
from agent_workbench.contracts.models import (
    ModelSettingsRead,
    ModelSettingsUpdate,
    SessionSnapshotRead,
)
from agent_workbench.contracts.plan import (
    CurrentFocusRead,
    PlanRead,
    PlanStepRead,
    WorkAssessmentRead,
)
from agent_workbench.domain.findings import FindingService
from agent_workbench.domain.instruction_scope import scope_from_instructions, sync_instruction_scope
from agent_workbench.domain.prices import PriceUpdate
from agent_workbench.domain.run_state import (
    assert_resumable,
    bump_control_version,
    set_run_status,
)
from agent_workbench.domain.scope import validate_scope
from agent_workbench.models.provider import (
    ModelBinding,
    ProviderConfigurationError,
    resolve_provider_profile,
)
from agent_workbench.observability.costs import summarize_costs
from agent_workbench.observability.events import append_event, latest_seq, read_events_after
from agent_workbench.persistence.repositories import (
    ArtifactRepository,
    CommandRepository,
    ModelCallRepository,
    ProjectRepository,
    SessionRepository,
    ToolJobRepository,
)
from agent_workbench.persistence.tables import (
    ArtifactRow,
    AssetRow,
    CommandRow,
    CoverageRow,
    FindingRow,
    IdentityRow,
    LeadRow,
    LeadWeaknessLinkRow,
    ModelCallRow,
    ReportVersionRow,
    RunRow,
    SessionRow,
    VerificationRow,
)
from agent_workbench.runtime.commands import submit_control, submit_message

router = APIRouter(prefix="/api")
router.include_router(tool_activity_router)

_auth = LocalAuth()


def get_auth() -> LocalAuth:
    return _auth


# --------------------------------------------------------------------------- #
# 会话与鉴权
# --------------------------------------------------------------------------- #


@router.post("/session", status_code=status.HTTP_200_OK, tags=["auth"])
async def create_local_session(response: Response, request: Request) -> dict[str, Any]:
    """获取本地会话 Cookie 与 CSRF 令牌。

    已持有有效会话时**复用当前会话**（不重新签发、不覆盖 Cookie）：控制台可能
    同时存在多个页面实例（多标签页、断线重连），每个实例都会调用本接口。若每次
    都签发新会话，最后签发的会覆盖浏览器共享的 Cookie，让其它页面内存中的 CSRF
    令牌立即失效，表现为「保存类写操作全部 403」。只允许从回环地址调用；
    同源校验在中间件里执行。
    """
    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.verify_cookie(request)
    reused = ticket is not None
    if ticket is None:
        ticket = auth.issue(response)
    return {
        "issued_at": ticket.issued_at,
        "csrf_token": ticket.csrf_token,
        "reused": reused,
        "cookie": "workbench_session（HttpOnly, SameSite=Strict）",
        "note": (
            "写请求需带 X-Workbench-CSRF 头；服务只监听回环地址，"
            "但本地会话 + Origin 校验防止其他网站操控本服务。"
        ),
    }


# --------------------------------------------------------------------------- #
# 项目 / 会话
# --------------------------------------------------------------------------- #


@router.get("/projects", tags=["projects"])
async def list_projects(request: Request) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as session:
        rows = await ProjectRepository(session).list_for_owner(get_settings().owner_id)
        return {
            "projects": [
                {
                    "id": str(row.id),
                    "name": row.name,
                    "target_description": row.target_description,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]
        }


@router.post("/projects", status_code=status.HTTP_201_CREATED, tags=["projects"])
async def create_project(
    request: Request,
    payload: ProjectCreate,
) -> dict[str, Any]:
    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.require_session(request)
    auth.require_csrf(request, ticket)

    _validate_scope_or_422(payload.default_scope)

    from agent_workbench.persistence.db import get_database

    async with get_database().transaction() as session:
        row = await ProjectRepository(session).create(
            owner_id=get_settings().owner_id,
            name=payload.name,
            target_description=payload.target_description,
            default_scope=payload.default_scope.model_dump(),
            settings_version=payload.settings_version,
        )
        return {
            "id": str(row.id),
            "name": row.name,
            "target_description": row.target_description,
            "default_scope": row.default_scope,
            "created_at": row.created_at.isoformat(),
        }


@router.get("/projects/{project_id}/sessions", tags=["sessions"])
async def list_project_sessions(
    project_id: UUID,
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as db:
        project = await ProjectRepository(db).get(project_id)
        if project is None or project.owner_id != get_settings().owner_id:
            raise ApiError(ErrorCode.NOT_FOUND, f"项目不存在：{project_id}")
        rows = list(
            (
                await db.execute(
                    select(SessionRow)
                    .where(SessionRow.project_id == project_id, SessionRow.status != "deleted")
                    .order_by(SessionRow.created_at.desc(), SessionRow.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
    return {
        "sessions": [
            {
                "id": str(row.id),
                "project_id": str(row.project_id),
                "title": _redact_api_value(row.title),
                "status": row.status,
                "provider_id": row.provider_id,
                "model_name": row.model_name,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows[:limit]
        ],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


@router.post(
    "/projects/{project_id}/sessions",
    status_code=status.HTTP_201_CREATED,
    tags=["sessions"],
)
async def create_session(
    project_id: UUID,
    request: Request,
    payload: SessionCreate,
) -> dict[str, Any]:
    """创建会话并**启动首个 run**。

    返回 201：会话与首个 run 都已在同一事务内持久化。
    """
    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.require_session(request)
    auth.require_csrf(request, ticket)

    from agent_workbench.domain.run_state import create_run
    from agent_workbench.persistence.db import get_database

    mode_explicit = "scope" in payload.model_fields_set and "mode" in payload.scope.model_fields_set
    supplied_scope = (
        payload.scope if mode_explicit else payload.scope.model_copy(update={"mode": "instruction"})
    )
    settings = get_settings()
    preferences = read_preferences(settings)
    selected_provider = payload.provider_id or preferences.default_provider
    if payload.initial_skill_refs:
        from agent_workbench.skills.service import SkillService

        try:
            await SkillService(get_database()).validate_references(
                payload.initial_skill_refs, str(project_id)
            )
        except ValueError as exc:
            raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc)) from exc
    selected_model = payload.model_name or (
        preferences.default_model
        if selected_provider == preferences.default_provider
        else get_settings().model_for_provider(selected_provider)
    )
    try:
        profile = resolve_provider_profile(selected_provider, selected_model, settings)
    except ProviderConfigurationError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc)) from None
    initial_scope = scope_from_instructions(supplied_scope, [payload.initial_objective])
    _validate_scope_or_422(initial_scope)
    settings = get_settings()

    async with get_database().transaction() as session:
        project = await ProjectRepository(session).get(project_id)
        if (
            project is None
            or project.owner_id != settings.owner_id
            or project.deleted_at is not None
        ):
            raise ApiError(ErrorCode.NOT_FOUND, f"项目不存在：{project_id}")
        if payload.project_id != project_id:
            raise ApiError(ErrorCode.VALIDATION_FAILED, "请求中的项目与当前项目不一致")

        session_row = await SessionRepository(session).create(
            project_id=project_id,
            title=payload.title,
            scope_snapshot=initial_scope.model_dump(),
            policy_snapshot=payload.policy.model_dump(),
            provider_id=selected_provider,
            model_name=selected_model,
        )

        created_identities: dict[str, IdentityRow] = {}
        for identity in payload.identities:
            identity_row = IdentityRow(
                project_id=project_id,
                display_name=identity.display_name,
                role_label=identity.role_label,
                secret_ref=identity.secret_ref,
                auth_state_ref=identity.auth_state_ref,
                auth_revision=identity.auth_revision,
            )
            session.add(identity_row)
            created_identities[identity.display_name] = identity_row
        await session.flush()
        if payload.active_identity_name:
            selected = created_identities.get(payload.active_identity_name)
            if selected is None:
                raise ApiError(
                    ErrorCode.VALIDATION_FAILED,
                    "active_identity_name 必须引用本次创建的测试身份",
                )
            session_row.active_identity_id = selected.id

        run = await create_run(
            session,
            session_id=session_row.id,
            objective=payload.initial_objective,
            provider_id=session_row.provider_id or settings.default_provider,
            model_name=session_row.model_name
            or settings.model_for_provider(session_row.provider_id),
            config_snapshot={
                "scope_revision": payload.scope.revision,
                "policy_revision": payload.policy.revision,
                "created_from_session_create": True,
                "initial_skill_refs": payload.initial_skill_refs,
                "model_binding": ModelBinding(
                    profile=profile, request_timeout_seconds=settings.model_request_timeout_seconds
                ).to_snapshot(),
                "scope_mode_explicit": mode_explicit,
            },
        )

        await append_event(
            session,
            session_id=session_row.id,
            run_id=run.id,
            event_type=EventType.RUN_STATUS,
            payload={"status": str(RunStatus.QUEUED), "reason": "session_created"},
        )

        return {
            "session": {
                "id": str(session_row.id),
                "project_id": str(session_row.project_id),
                "active_identity_id": (
                    str(session_row.active_identity_id) if session_row.active_identity_id else None
                ),
                "thread_id": session_row.thread_id,
                "title": session_row.title,
                "status": session_row.status,
                "scope": session_row.scope_snapshot,
                "policy": session_row.policy_snapshot,
                "created_at": session_row.created_at.isoformat(),
            },
            "run": {
                "id": str(run.id),
                "status": str(run.status),
                "provider_id": run.provider_id,
                "model_name": run.model_name,
                "objective": run.objective,
            },
            "note": (
                "thread_id 由服务端生成并在 session 生命周期内保持稳定；"
                "阶段结束后继续输入只会创建新 run，不会换 thread。"
            ),
        }


# --------------------------------------------------------------------------- #
# 消息与运行控制
# --------------------------------------------------------------------------- #


@router.delete("/sessions/{session_id}", tags=["sessions"])
async def delete_session(
    session_id: UUID,
    request: Request,
    terminate_resources: bool = False,
) -> dict[str, Any]:
    """从列表移除任务，保留审计记录；活动运行经取消流程确认停止。

    执行资源预检（规格 §13）：仍存活的执行资源不允许被"顺手删掉" ——
    默认返回 409 与资源清单；显式 `terminate_resources=true` 时先发出终止请求，
    再执行删除。台账记录（投影）保留，不制造孤儿。
    """
    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.require_session(request)
    auth.require_csrf(request, ticket)
    from agent_workbench.persistence.db import get_database

    if terminate_resources:
        from agent_workbench.api.execution import terminate_all

        termination = await terminate_all(session_id, request, f"delete-session:{session_id}")
        if termination.unresolved:
            raise ApiError(
                ErrorCode.CONFLICT,
                "资源终止尚未确认，对话保留以便继续管理。",
                details={"active_resources": termination.unresolved},
            )

    async with get_database().transaction() as db:
        row = (
            await db.execute(
                select(SessionRow).where(SessionRow.id == session_id).with_for_update()
            )
        ).scalar_one_or_none()
        project = await ProjectRepository(db).get(row.project_id) if row else None
        if row is None or project is None or project.owner_id != get_settings().owner_id:
            raise ApiError(ErrorCode.NOT_FOUND, "任务不存在")
        from agent_workbench.persistence.repositories import ExecutionResourceRepository

        active_resources = list(
            await ExecutionResourceRepository(db).active_for_session(session_id)
        )
        if row.status != "deleted":
            if active_resources:
                raise ApiError(
                    ErrorCode.CONFLICT,
                    "该对话仍有存活的执行资源；删除前请显式选择终止（terminate_resources=true）"
                    "或先逐个关闭。系统不会在删除时静默丢弃它们的台账。",
                    details={
                        "active_resources": [
                            f"{item.kind}:{item.resource_id}:{item.state}"
                            for item in active_resources
                        ],
                        "required_field": "terminate_resources",
                    },
                )
            runs = list(
                (
                    await db.execute(
                        select(RunRow)
                        .where(
                            RunRow.session_id == session_id,
                            RunRow.status.in_([str(item) for item in ACTIVE_RUN_STATUSES]),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            row.status = "deleted"
            for run in runs:
                await bump_control_version(db, run_id=run.id)
                await submit_control(
                    db,
                    run=run,
                    kind=CommandKind.CANCEL,
                    idempotency_key=f"delete-session:{session_id}:{run.id}",
                )
                current = RunStatus(run.status)
                if current is RunStatus.RECOVERING or current is RunStatus.PAUSING:
                    await set_run_status(db, run_id=run.id, target=RunStatus.PAUSED)
                if RunStatus(run.status) is not RunStatus.CANCELLING:
                    await set_run_status(db, run_id=run.id, target=RunStatus.CANCELLING)
            return {
                "id": str(session_id),
                "deleted": True,
                "cancellation_pending": bool(runs),
                "active_resources": [
                    f"{item.kind}:{item.resource_id}" for item in active_resources
                ],
                "resources_note": (
                    "删除只做软删除并取消运行；执行资源台账与投影保留，"
                    "请通过执行面板或 terminate_resources 显式终止。"
                    if active_resources
                    else ""
                ),
            }
        return {"id": str(session_id), "deleted": True, "cancellation_pending": False}


@router.post(
    "/sessions/{session_id}/messages",
    status_code=status.HTTP_202_ACCEPTED,
    tags=["runs"],
)
async def post_message(
    session_id: UUID,
    request: Request,
    content: Annotated[str, Body(embed=True)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    provider_id: Annotated[str | None, Body()] = None,
    model_name: Annotated[str | None, Body()] = None,
    skill_refs: Annotated[list[str] | None, Body(max_length=20)] = None,
) -> dict[str, Any]:
    """**只写入指令并返回 202**，不在请求处理函数里驱动 Agent。"""
    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.require_session(request)
    auth.require_csrf(request, ticket)

    from agent_workbench.persistence.db import get_database

    settings = get_settings()
    async with get_database().transaction() as session:
        row = (
            await session.execute(
                select(SessionRow).where(SessionRow.id == session_id).with_for_update()
            )
        ).scalar_one_or_none()
        project = await ProjectRepository(session).get(row.project_id) if row else None
        if (
            row is None
            or row.status == "deleted"
            or project is None
            or project.owner_id != settings.owner_id
        ):
            raise ApiError(ErrorCode.NOT_FOUND, "任务不存在或已删除")
        from agent_workbench.domain.run_state import find_active_run

        active = await find_active_run(session, session_id)
        if skill_refs:
            from agent_workbench.skills.service import SkillService

            try:
                service = SkillService(get_database())
                await service.validate_references(skill_refs, str(row.project_id))
                if active and (active.config_snapshot or {}).get("skill_catalog_artifact_id"):
                    # 当前运行的目录已固定，刚安装的技能应在下一运行使用。
                    from agent_workbench.persistence.tables import ArtifactRow

                    artifact = await session.get(
                        ArtifactRow, UUID(active.config_snapshot["skill_catalog_artifact_id"])
                    )
                    if artifact is None or not service.store.verify(
                        artifact.relative_key, artifact.sha256
                    ):
                        raise ValueError("skill_version_missing: 目录快照不可用")
                    visible = {
                        item["skill_id"]
                        for item in json.loads(service.store.read_bytes(artifact.relative_key))
                    }
                    if any(reference not in visible for reference in skill_refs):
                        raise ValueError(
                            "skill_not_found: 技能不在当前运行固定目录，请在下一运行使用"
                        )
            except ValueError as exc:
                raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc)) from exc
        preferences = read_preferences(settings)
        selected_provider = provider_id or (
            active.provider_id if active else preferences.default_provider
        )
        selected_model = model_name or (
            active.model_name
            if active
            else (
                preferences.default_model
                if selected_provider == preferences.default_provider
                else settings.model_for_provider(selected_provider)
            )
        )
        if active and (selected_provider, selected_model) != (
            active.provider_id,
            active.model_name,
        ):
            raise ApiError(
                ErrorCode.CONFLICT, "当前阶段固定模型；请继续当前模型，或新建聊天选择其他模型"
            )
        config: dict[str, object] = {"created_from": "message"}
        if active is None:
            try:
                profile = resolve_provider_profile(selected_provider, selected_model, settings)
            except ProviderConfigurationError as exc:
                raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc)) from None
            config["model_binding"] = ModelBinding(
                profile=profile, request_timeout_seconds=settings.model_request_timeout_seconds
            ).to_snapshot()
        receipt = await submit_message(
            session,
            session_id=session_id,
            content=content,
            idempotency_key=idempotency_key,
            provider_id=selected_provider,
            model_name=selected_model,
            config_snapshot=config,
            payload=(
                {
                    "provider_id": provider_id,
                    "model_name": model_name,
                    **({"skill_refs": skill_refs} if skill_refs else {}),
                }
                if provider_id or model_name or skill_refs
                else None
            ),
        )
        if receipt.created and receipt.run_id:
            run = await session.get(RunRow, receipt.run_id)
            if (
                run is not None
                and RunStatus(run.status) is RunStatus.RECOVERING
                and not await _unresolved_jobs(session, run.id)
            ):
                run.config_snapshot = {
                    **run.config_snapshot,
                    "restart_checkpoint_on_input": str(receipt.command_id),
                }
                await set_run_status(session, run_id=run.id, target=RunStatus.QUEUED)
    return {
        "command_id": str(receipt.command_id),
        "status": str(receipt.status),
        "created": receipt.created,
        "run_id": str(receipt.run_id) if receipt.run_id else None,
        "created_run": receipt.created_run,
        "message": receipt.message,
    }


@router.post("/runs/{run_id}/pause", status_code=status.HTTP_202_ACCEPTED, tags=["runs"])
async def pause_run(
    run_id: UUID,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, Any]:
    """202 pausing。**不虚报"所有工具已停止"**。"""
    return await _control(run_id, request, idempotency_key, CommandKind.PAUSE, RunStatus.PAUSING)


@router.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED, tags=["runs"])
async def cancel_run(
    run_id: UUID,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, Any]:
    """202 cancelling。最终状态通过事件同步。"""
    return await _control(
        run_id, request, idempotency_key, CommandKind.CANCEL, RunStatus.CANCELLING
    )


@router.post("/runs/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED, tags=["runs"])
async def resume_run(
    run_id: UUID,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    content: Annotated[str | None, Body(embed=True)] = None,
    acknowledge_unknown_operations: Annotated[bool, Body(embed=True)] = False,
) -> dict[str, Any]:
    """resume **只作用于可恢复的 run**；已 completed 的 run 必须创建新 run。

    `recovering` 既可能是模型超时，也可能是远端状态不明。只有实际存在
    未核实的工具任务时才要求显式确认，不能把模型超时误报成未知副作用。
    """
    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.require_session(request)
    auth.require_csrf(request, ticket)

    from agent_workbench.persistence.db import get_database

    async with get_database().transaction() as session:
        run = await assert_resumable(session, run_id)
        from agent_workbench.domain.run_state import get_session

        await get_session(session, run.session_id)
        # 与消息提交、范围应用、删除保持 Session -> Run 的锁顺序。
        await session.execute(
            select(SessionRow).where(SessionRow.id == run.session_id).with_for_update()
        )

        if RunStatus(run.status) is RunStatus.RECOVERING and not acknowledge_unknown_operations:
            unresolved = await _unresolved_jobs(session, run_id)
            if unresolved:
                raise ApiError(
                    ErrorCode.CONFLICT,
                    "该 run 处于 recovering：启动核对未能确认远端副作用是否已经发生。"
                    "请先确认这些任务的实际影响，再带 acknowledge_unknown_operations=true 恢复；"
                    "系统不会自动重跑未知任务。",
                    details={
                        "status": str(RunStatus.RECOVERING),
                        "stop_reason": run.stop_reason,
                        "unresolved_jobs": unresolved,
                        "required_field": "acknowledge_unknown_operations",
                    },
                )

        receipt = await submit_control(
            session,
            run=run,
            kind=CommandKind.RESUME,
            idempotency_key=idempotency_key,
            payload={
                "content": content,
                "acknowledge_unknown_operations": acknowledge_unknown_operations,
            },
        )
        if receipt.created:
            await sync_instruction_scope(session, session_id=run.session_id, run_id=run.id)
            run.config_snapshot = {
                **run.config_snapshot,
                "restart_checkpoint_on_input": str(receipt.command_id),
            }
        await set_run_status(session, run_id=run_id, target=RunStatus.QUEUED)
    return {
        "run_id": str(run_id),
        "status": str(RunStatus.QUEUED),
        "command_id": str(receipt.command_id),
        "acknowledged_unknown_operations": acknowledge_unknown_operations,
        "message": "已校验可恢复状态并重新排队",
    }


async def _unresolved_jobs(session: Any, run_id: UUID) -> list[str]:
    """该 run 名下无法确认终态的 job 明细（供 resume 拒绝时给出具体对象）。"""
    from agent_workbench.persistence.tables import ToolJobRow, ToolOperationRow

    rows = (
        await session.execute(
            select(ToolJobRow.job_id, ToolJobRow.state, ToolJobRow.sync_status)
            .join(ToolOperationRow, ToolOperationRow.job_id == ToolJobRow.job_id)
            .where(ToolOperationRow.run_id == run_id)
            .where(ToolJobRow.state.in_(["unknown"]) | ToolJobRow.sync_status.in_(["uncertain"]))
        )
    ).all()
    return [f"{job_id}:{state}/{sync_status}" for job_id, state, sync_status in rows]


async def _control(
    run_id: UUID,
    request: Request,
    idempotency_key: str,
    kind: CommandKind,
    target: RunStatus,
) -> dict[str, Any]:
    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.require_session(request)
    auth.require_csrf(request, ticket)

    from agent_workbench.persistence.db import get_database

    async with get_database().transaction() as session:
        run = (
            await session.execute(select(RunRow).where(RunRow.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"run 不存在：{run_id}")
        current = RunStatus(run.status)
        if current in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
            raise ApiError(ErrorCode.CONFLICT, f"run 已处于终态 {current}，不能再{kind}")
        if kind is CommandKind.CANCEL and current is RunStatus.PAUSING:
            await set_run_status(session, run_id=run_id, target=RunStatus.PAUSED)
        if kind is CommandKind.CANCEL and current is RunStatus.RECOVERING:
            await set_run_status(session, run_id=run_id, target=RunStatus.PAUSED)
        receipt = await submit_control(session, run=run, kind=kind, idempotency_key=idempotency_key)
        if kind is CommandKind.CANCEL and current is not RunStatus.FAILED:
            if receipt.created:
                await bump_control_version(session, run_id=run.id)
            if RunStatus(run.status) is not target:
                await set_run_status(session, run_id=run_id, target=target)
        elif kind is CommandKind.PAUSE and current in {
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.WAITING_TOOL,
            RunStatus.WAITING_USER,
        }:
            if receipt.created:
                await bump_control_version(session, run_id=run.id)
                await set_run_status(session, run_id=run_id, target=RunStatus.PAUSING)

    return {
        "run_id": str(run_id),
        "status": str(target),
        "command_id": str(receipt.command_id),
        "accepted": True,
        "note": (
            "已受理控制请求。实际停止/暂停以工具核对结果为准；"
            "在核对完成前界面应显示『正在取消/正在暂停』，不要显示『已全部停止』。"
        ),
    }


# --------------------------------------------------------------------------- #
# 快照与事件
# --------------------------------------------------------------------------- #


@router.get("/sessions/{session_id}/plan", response_model=PlanRead, tags=["read"])
async def session_plan(session_id: UUID, request: Request) -> PlanRead:
    """从图 checkpoint 读取当前计划，不把事件投影当作第二份权威状态。"""
    get_auth().require_session(request)
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from agent_workbench.persistence.bootstrap import open_checkpointer_connection
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as db:
        session_row = (
            await db.execute(select(SessionRow).where(SessionRow.id == session_id))
        ).scalar_one_or_none()
        if session_row is None or session_row.status == "deleted":
            raise ApiError(ErrorCode.NOT_FOUND, f"session 不存在：{session_id}")
        thread_id = session_row.thread_id

    async with open_checkpointer_connection(get_settings()) as connection:
        saved = await AsyncPostgresSaver(connection).aget_tuple(
            {"configurable": {"thread_id": thread_id}}
        )
    values = saved.checkpoint.get("channel_values", {}) if saved is not None else {}
    raw_steps = values.get("plan") or []
    from agent_workbench.tools.plan_revisions import project_committed_plan

    projection_error = None
    try:
        await project_committed_plan(get_database(), get_settings(), session_id, saved)
    except Exception as exc:
        # 当前计划仍按 checkpoint 返回；展示故障不影响执行，也不冒充已经补齐历史。
        projection_error = f"修订事件待补投影：{type(exc).__name__}"
    return PlanRead(
        revision=int(values.get("plan_revision") or 0),
        last_plan_change=_redact_api_value(values.get("last_plan_change")),
        revision_projection_error=projection_error,
        steps=[
            PlanStepRead.model_validate(
                {
                    **_redact_api_value(step),
                    # 旧步骤只有 lead_id：读取时映射为单元素 lead_ids（不回写历史）。
                    "lead_ids": plan_step_lead_ids(step),
                    "kind": plan_step_kind(step),
                }
            )
            for step in raw_steps
        ],
        current_focus=CurrentFocusRead.model_validate(_redact_api_value(values["current_focus"]))
        if values.get("current_focus")
        else None,
        work_assessment=WorkAssessmentRead.model_validate(
            _redact_api_value(values["work_assessment"])
        )
        if values.get("work_assessment")
        else None,
    )


@router.get("/sessions/{session_id}/snapshot", response_model=SessionSnapshotRead, tags=["read"])
async def session_snapshot(session_id: UUID, request: Request) -> dict[str, Any]:
    """**一致读**的业务快照 + `event_cursor`。

    客户端随后订阅**大于**该 cursor 的事件，避免快照与订阅之间漏更新。
    """
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database

    db = get_database()
    async with db.session() as session:
        session_row = (
            await session.execute(select(SessionRow).where(SessionRow.id == session_id))
        ).scalar_one_or_none()
        if session_row is None or session_row.status == "deleted":
            raise ApiError(ErrorCode.NOT_FOUND, f"session 不存在：{session_id}")
        cursor = await latest_seq(session, session_id)
        runs = (
            (
                await session.execute(
                    select(RunRow)
                    .where(RunRow.session_id == session_id)
                    .order_by(RunRow.created_at)
                )
            )
            .scalars()
            .all()
        )
        leads = (
            (
                await session.execute(
                    select(LeadRow)
                    .where(LeadRow.session_id == session_id)
                    .order_by(LeadRow.priority.desc())
                )
            )
            .scalars()
            .all()
        )
        findings = (
            (await session.execute(select(FindingRow).where(FindingRow.session_id == session_id)))
            .scalars()
            .all()
        )
        coverage = (
            (await session.execute(select(CoverageRow).where(CoverageRow.session_id == session_id)))
            .scalars()
            .all()
        )

    return {
        "session": {
            "id": str(session_row.id),
            "project_id": str(session_row.project_id),
            "thread_id": session_row.thread_id,
            "title": session_row.title,
            "status": session_row.status,
            "scope": session_row.scope_snapshot,
            "policy": session_row.policy_snapshot,
        },
        "runs": [
            {
                "id": str(row.id),
                "provider_id": row.provider_id,
                "model_name": row.model_name,
                "status": row.status,
                "completion_reason": row.completion_reason,
                "objective": row.objective,
                "control_version": row.control_version,
                "stop_reason": _redact_api_value(row.stop_reason),
                "owner_generation": row.owner_generation,
                "created_at": row.created_at.isoformat(),
            }
            for row in runs
        ],
        "leads": [
            {
                "id": str(row.id),
                "hypothesis": row.hypothesis,
                "status": row.status,
                "priority": row.priority,
                "blocked_reason": row.blocked_reason,
            }
            for row in leads
        ],
        "findings": [
            {
                "id": str(row.id),
                "title": row.title,
                "status": row.status,
                "category": row.category,
                "weakness_kind": row.weakness_kind,
                "verification_id": str(row.verification_id) if row.verification_id else None,
            }
            for row in findings
        ],
        "coverage": {
            "total": len(coverage),
            "by_status": _count_by(coverage, "status"),
            "ratio_definition": (
                "分母是已登记的 资产/入口 × 身份 × 检查类别 组合数；**不是**「网站漏洞覆盖率」。"
            ),
        },
        "event_cursor": cursor,
        "note": "这是同一一致读下的快照；随后请订阅 session_seq > event_cursor 的事件。",
    }


@router.get("/sessions/{session_id}/events", tags=["read"])
async def session_events(
    session_id: UUID,
    request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """SSE。断线重连用 `Last-Event-ID` 或 `after` 补读；慢客户端**不阻塞 Runner**。"""
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database

    db = get_database()
    async with db.session() as session:
        session_row = (
            await session.execute(select(SessionRow).where(SessionRow.id == session_id))
        ).scalar_one_or_none()
        if session_row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"session 不存在：{session_id}")

    start = after
    if last_event_id:
        async with db.session() as session:
            events = await read_events_after(session, session_id=session_id, after_seq=0, limit=1)
            _ = events
        # Last-Event-ID 是 event_id，需要映射回 session_seq
        start = await _seq_for_event_id(db, session_id, last_event_id) or after

    return StreamingResponse(
        event_stream(db.session_factory(), session_id=session_id, after_seq=start),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _seq_for_event_id(db: Any, session_id: UUID, event_id: str) -> int | None:
    from agent_workbench.persistence.tables import EventRow

    try:
        parsed = UUID(event_id)
    except ValueError:
        return None
    async with db.session() as session:
        row = (
            await session.execute(
                select(EventRow).where(
                    EventRow.session_id == session_id, EventRow.event_id == parsed
                )
            )
        ).scalar_one_or_none()
    return int(row.session_seq) if row is not None else None


@router.get("/sessions/{session_id}/events/page", response_model=EventPage, tags=["read"])
async def page_session_events(
    session_id: UUID,
    request: Request,
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    """有界读取最近事件；历史日志不靠 SSE 从零重放到浏览器。"""
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database
    from agent_workbench.persistence.tables import EventRow

    async with get_database().session() as db:
        await _require_session_row(db, session_id)
        stmt = select(EventRow).where(EventRow.session_id == session_id)
        if before is not None:
            stmt = stmt.where(EventRow.session_seq < before)
        rows = list(
            (await db.execute(stmt.order_by(EventRow.session_seq.desc()).limit(limit + 1)))
            .scalars()
            .all()
        )
    page = rows[:limit]
    return {
        "events": [
            {
                "schema_version": row.schema_version,
                "event_id": str(row.event_id),
                "session_seq": row.session_seq,
                "session_id": str(row.session_id),
                "run_id": str(row.run_id) if row.run_id else None,
                "type": row.type,
                "occurred_at": row.occurred_at.isoformat(),
                "payload": redact_event_payload(row.type, row.payload),
            }
            for row in reversed(page)
        ],
        "next_before": page[-1].session_seq if len(rows) > limit else None,
    }


# --------------------------------------------------------------------------- #
# 发现 / 覆盖 / 任务 / 工件 / 用量
# --------------------------------------------------------------------------- #


async def _require_session_row(db: Any, session_id: UUID) -> None:
    exists = (
        await db.execute(
            select(SessionRow.id).where(SessionRow.id == session_id, SessionRow.status != "deleted")
        )
    ).scalar_one_or_none()
    if exists is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"session 不存在：{session_id}")


def _conversation_sources(session_id: UUID) -> Any:
    """每条正文只保留一个来源；运行恢复不重复展示目标。"""
    empty_text = cast(literal(None), Text)
    empty_uuid = cast(literal(None), PG_UUID(as_uuid=True))
    return union_all(
        select(
            RunRow.id.label("id"),
            literal("objective").label("source"),
            literal(0).label("rank"),
            RunRow.created_at.label("occurred_at"),
            RunRow.id.label("run_id"),
            RunRow.objective.label("body"),
            empty_uuid.label("artifact_id"),
        ).where(
            RunRow.session_id == session_id,
            RunRow.config_snapshot["created_from_session_create"].as_boolean().is_(True),
        ),
        select(
            CommandRow.id,
            literal("message"),
            literal(1),
            CommandRow.created_at,
            CommandRow.run_id,
            CommandRow.content,
            empty_uuid,
        ).where(CommandRow.session_id == session_id, CommandRow.kind == "message"),
        select(
            ModelCallRow.model_call_id,
            literal("assistant"),
            literal(2),
            ModelCallRow.completed_at,
            ModelCallRow.run_id,
            empty_text,
            ModelCallRow.response_artifact_id,
        ).where(
            ModelCallRow.session_id == session_id,
            ModelCallRow.response_artifact_id.is_not(None),
            ModelCallRow.completed_at.is_not(None),
        ),
    ).subquery("conversation")


def _conversation_cursor(row: Any) -> str:
    key = f"{row['occurred_at'].isoformat()}|{row['rank']}|{row['id']}"
    return base64.urlsafe_b64encode(key.encode("utf-8")).decode("ascii").rstrip("=")


def _parse_conversation_cursor(cursor: str) -> tuple[datetime, int, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8")
        when, rank_text, entry_id = raw.split("|", 2)
        timestamp = datetime.fromisoformat(when)
        rank = int(rank_text)
        if timestamp.tzinfo is None or rank not in {0, 1, 2}:
            raise ValueError("无效游标")
        return timestamp, rank, UUID(entry_id)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "对话分页游标无效") from exc


@router.get("/sessions/{session_id}/conversation", response_model=ConversationPage, tags=["read"])
async def page_conversation(
    session_id: UUID,
    request: Request,
    limit: int = Query(default=30, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=256),
) -> dict[str, Any]:
    """按时间倒序分页；旧版本未归档的模型正文不会凭事件增量补造。"""
    get_auth().require_session(request)
    from agent_workbench.evidence.artifacts import ArtifactError, build_artifact_store, redact_text
    from agent_workbench.persistence.db import get_database

    source = _conversation_sources(session_id)
    from agent_workbench.persistence.tables import EventRow

    stmt = select(source)
    if cursor is not None:
        timestamp, rank, entry_id = _parse_conversation_cursor(cursor)
        stmt = stmt.where(
            tuple_(source.c.occurred_at, source.c.rank, source.c.id)
            < tuple_(timestamp, rank, entry_id)
        )
    async with get_database().session() as db:
        await _require_session_row(db, session_id)
        rows = list(
            (
                await db.execute(
                    stmt.order_by(
                        source.c.occurred_at.desc(), source.c.rank.desc(), source.c.id.desc()
                    ).limit(limit + 1)
                )
            )
            .mappings()
            .all()
        )
        artifact_ids = [row["artifact_id"] for row in rows[:limit] if row["artifact_id"]]
        artifacts = (
            {
                row.id: row
                for row in (
                    (
                        await db.execute(
                            select(ArtifactRow).where(
                                ArtifactRow.id.in_(artifact_ids),
                                ArtifactRow.session_id == session_id,
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            }
            if artifact_ids
            else {}
        )

        model_ids = [str(row["id"]) for row in rows[:limit] if row["source"] == "assistant"]
        qualities = {}
        if model_ids:
            for event in (
                await db.execute(
                    select(EventRow)
                    .where(
                        EventRow.session_id == session_id,
                        EventRow.type == "model.response_quality",
                        EventRow.payload["model_call_id"].astext.in_(model_ids),
                    )
                    .order_by(EventRow.session_seq)
                )
            ).scalars():
                qualities[event.payload["model_call_id"]] = event.payload.get("outcome")

    store = build_artifact_store()
    entries: list[dict[str, Any]] = []
    for row in rows[:limit]:
        artifact = artifacts.get(row["artifact_id"])
        available = row["source"] != "assistant"
        body = redact_text(row["body"] or "")
        if artifact is not None and artifact.availability == "available":
            try:
                body = store.render_redacted_view(
                    artifact.relative_key, media_type=artifact.media_type
                )
                available = True
            except (ArtifactError, OSError, ValueError):
                available = False
        entries.append(
            {
                "id": row["id"],
                "role": "assistant" if row["source"] == "assistant" else "user",
                "source": row["source"],
                "run_id": row["run_id"],
                "occurred_at": row["occurred_at"],
                "preview": body[:1200] if available else "[正文工件不可用]",
                "has_more": len(body) > 1200 if available else False,
                "available": available,
                "response_outcome": qualities.get(str(row["id"])),
            }
        )
    return {
        "entries": entries,
        "next_cursor": _conversation_cursor(rows[limit - 1]) if len(rows) > limit else None,
    }


@router.get(
    "/sessions/{session_id}/conversation/{source}/{entry_id}/text",
    response_model=ConversationBody,
    tags=["read"],
)
async def read_conversation_body(
    session_id: UUID,
    source: str,
    entry_id: UUID,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=65536, ge=1, le=1048576),
) -> dict[str, Any]:
    """对完整脱敏视图按字符切片，避免把凭证拆到两次响应中。"""
    get_auth().require_session(request)
    from agent_workbench.evidence.artifacts import build_artifact_store, redact_text
    from agent_workbench.persistence.db import get_database

    if source not in {"objective", "message", "assistant"}:
        raise ApiError(ErrorCode.NOT_FOUND, "对话记录不存在")
    union = _conversation_sources(session_id)
    async with get_database().session() as db:
        await _require_session_row(db, session_id)
        row = (
            (
                await db.execute(
                    select(union).where(union.c.id == entry_id, union.c.source == source)
                )
            )
            .mappings()
            .one_or_none()
        )
        artifact = (
            await ArtifactRepository(db).get(row["artifact_id"])
            if row and row["artifact_id"]
            else None
        )
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "对话记录不存在")
    if source == "assistant":
        if (
            artifact is None
            or artifact.session_id != session_id
            or artifact.availability != "available"
        ):
            raise ApiError(ErrorCode.CONFLICT, "模型正文工件不可用")
        store = build_artifact_store()
        if not store.exists(artifact.relative_key):
            raise ApiError(ErrorCode.CONFLICT, "模型正文工件文件缺失")
        body = store.render_redacted_view(artifact.relative_key, media_type=artifact.media_type)
    else:
        body = redact_text(row["body"] or "")
    end = offset + limit
    return {
        "id": entry_id,
        "role": "assistant" if source == "assistant" else "user",
        "text": body[offset:end],
        "offset": offset,
        "next_offset": end if end < len(body) else None,
    }


@router.get("/sessions/{session_id}/assets", tags=["read"])
async def list_assets(
    session_id: UUID,
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.evidence.artifacts import redact_text
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as db:
        await _require_session_row(db, session_id)
        rows = list(
            (
                await db.execute(
                    select(AssetRow)
                    .where(AssetRow.session_id == session_id)
                    .order_by(AssetRow.first_seen_at, AssetRow.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
    has_more = len(rows) > limit
    return {
        "assets": [
            {
                "id": str(row.id),
                "scheme": row.scheme,
                "host": row.host,
                "port": row.port,
                "path": redact_text(row.path),
                "method": row.method,
                "query_keys": list(row.query_keys or []),
                "identity_refs": list(row.identity_refs or []),
                "source_artifact_id": (
                    str(row.source_artifact_id) if row.source_artifact_id else None
                ),
                "first_seen_at": row.first_seen_at.isoformat(),
                "last_seen_at": row.last_seen_at.isoformat(),
            }
            for row in rows[:limit]
        ],
        "next_offset": offset + limit if has_more else None,
    }


@router.get("/sessions/{session_id}/leads", tags=["read"])
async def list_leads(
    session_id: UUID,
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.evidence.artifacts import redact_text
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as db:
        await _require_session_row(db, session_id)
        stmt = select(LeadRow).where(LeadRow.session_id == session_id)
        if status_filter is not None:
            stmt = stmt.where(LeadRow.status == status_filter)
        rows = list(
            (
                await db.execute(
                    stmt.order_by(LeadRow.priority.desc(), LeadRow.created_at, LeadRow.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
    has_more = len(rows) > limit
    return {
        "leads": [
            {
                "id": str(row.id),
                "hypothesis": redact_text(row.hypothesis),
                "asset_id": str(row.asset_id) if row.asset_id else None,
                "identity_refs": list(row.identity_refs or []),
                "priority": row.priority,
                "priority_rationale": _redact_api_value(row.priority_rationale),
                "merged_into_id": str(row.merged_into_id) if row.merged_into_id else None,
                "status": row.status,
                "next_action": redact_text(row.next_action) if row.next_action else None,
                "blocked_reason": (redact_text(row.blocked_reason) if row.blocked_reason else None),
                "resume_condition": (
                    redact_text(row.resume_condition) if row.resume_condition else None
                ),
                "evidence_refs": list(row.evidence_refs or []),
                "required_conditions": _redact_api_value(row.required_conditions or []),
                "actionability": (row.actionability_assessment or {}).get("state"),
                "actionability_note": _redact_api_value(
                    (row.actionability_assessment or {}).get("note")
                ),
                "revision": row.revision,
            }
            for row in rows[:limit]
        ],
        "next_offset": offset + limit if has_more else None,
    }


@router.get("/sessions/{session_id}/findings/{finding_id}/verifications", tags=["read"])
async def list_verifications(
    session_id: UUID,
    finding_id: UUID,
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.evidence.artifacts import redact_text
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as db:
        await _require_session_row(db, session_id)
        finding = (
            await db.execute(
                select(FindingRow.id).where(
                    FindingRow.id == finding_id, FindingRow.session_id == session_id
                )
            )
        ).scalar_one_or_none()
        if finding is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"发现不存在：{finding_id}")
        rows = list(
            (
                await db.execute(
                    select(VerificationRow)
                    .where(VerificationRow.finding_id == finding_id)
                    .order_by(VerificationRow.created_at, VerificationRow.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
    has_more = len(rows) > limit
    return {
        "verifications": [
            {
                "id": str(row.id),
                "method_version": row.method_version,
                "input_evidence_refs": list(row.input_evidence_refs or []),
                "recheck_evidence_refs": list(row.recheck_evidence_refs or []),
                "conclusion": row.conclusion,
                "limitations": (redact_text(row.limitations) if row.limitations else None),
                "tested_conditions": _redact_api_value(row.tested_conditions),
                "executed_by_identity_id": (
                    str(row.executed_by_identity_id) if row.executed_by_identity_id else None
                ),
                "performed_by": row.performed_by,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows[:limit]
        ],
        "next_offset": offset + limit if has_more else None,
    }


@router.get("/sessions/{session_id}/exploration-relations", tags=["read"])
async def list_exploration_relations(
    session_id: UUID,
    request: Request,
    weakness_id: Annotated[UUID | None, Query()] = None,
    lead_id: Annotated[UUID | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """关系分页：`弱点 → 线索 → 弱点` 的一跳关系。

    `evidence_linked` 只表示关系有引用依据，不代表组合路径已确认。
    """
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as db:
        await _require_session_row(db, session_id)
        stmt = select(LeadWeaknessLinkRow).where(LeadWeaknessLinkRow.session_id == session_id)
        if weakness_id is not None:
            stmt = stmt.where(LeadWeaknessLinkRow.finding_id == weakness_id)
        if lead_id is not None:
            stmt = stmt.where(LeadWeaknessLinkRow.lead_id == lead_id)
        if cursor:
            try:
                raw = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                after_at = datetime.fromisoformat(str(raw["at"]))
                after_id = UUID(str(raw["id"]))
            except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise ApiError(ErrorCode.VALIDATION_FAILED, "关系分页游标无效") from exc
            stmt = stmt.where(
                or_(
                    LeadWeaknessLinkRow.created_at > after_at,
                    and_(
                        LeadWeaknessLinkRow.created_at == after_at,
                        LeadWeaknessLinkRow.id > after_id,
                    ),
                )
            )
        rows = list(
            (
                await db.execute(
                    stmt.order_by(LeadWeaknessLinkRow.created_at, LeadWeaknessLinkRow.id).limit(
                        limit + 1
                    )
                )
            )
            .scalars()
            .all()
        )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = base64.urlsafe_b64encode(
            json.dumps({"at": last.created_at.isoformat(), "id": str(last.id)}).encode()
        ).decode()
    return {
        "relations": [
            {
                "link_id": str(row.id),
                "lead_id": str(row.lead_id),
                "weakness_id": str(row.finding_id),
                "role": row.role,
                "relation_state": row.relation_state,
                "condition_key": row.condition_key,
                "requirement_key": row.requirement_key,
                "source_weakness_revision": row.source_weakness_revision,
                "evidence_refs": list(row.evidence_refs or []),
                "source_operation_id": (
                    str(row.source_operation_id) if row.source_operation_id else None
                ),
                "revision": row.revision,
            }
            for row in rows
        ],
        "next_cursor": next_cursor,
        "retrieval": "explicit_id_or_condition_relation",
        "note": "关系只表示有引用依据；两侧弱点分别成立不代表组合已成立。",
    }


@router.get("/sessions/{session_id}/weaknesses/{weakness_id}", tags=["read"])
async def get_weakness(session_id: UUID, weakness_id: UUID, request: Request) -> dict[str, Any]:
    """弱点详情：适用条件、后续用途判断与关系邻居（按需展开，不加载全图）。"""
    get_auth().require_session(request)
    from agent_workbench.evidence.artifacts import redact_text
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as db:
        await _require_session_row(db, session_id)
        row = (
            await db.execute(
                select(FindingRow).where(
                    FindingRow.id == weakness_id, FindingRow.session_id == session_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"弱点不存在：{weakness_id}")
        links = list(
            (
                await db.execute(
                    select(LeadWeaknessLinkRow)
                    .where(
                        LeadWeaknessLinkRow.session_id == session_id,
                        LeadWeaknessLinkRow.finding_id == weakness_id,
                    )
                    .order_by(LeadWeaknessLinkRow.created_at, LeadWeaknessLinkRow.id)
                )
            )
            .scalars()
            .all()
        )
        lead_ids = [link.lead_id for link in links]
        leads = (
            list(
                (await db.execute(select(LeadRow).where(LeadRow.id.in_(lead_ids)))).scalars().all()
            )
            if lead_ids
            else []
        )
        verifications = list(
            (
                await db.execute(
                    select(VerificationRow)
                    .where(VerificationRow.finding_id == weakness_id)
                    .order_by(VerificationRow.created_at, VerificationRow.id)
                )
            )
            .scalars()
            .all()
        )
        stale = not FindingService(db).review_is_current(row)
    return {
        "weakness": {
            "id": str(row.id),
            "title": redact_text(row.title),
            "description": redact_text(row.description),
            "category": row.category,
            "status": row.status,
            "weakness_kind": row.weakness_kind,
            "impact": redact_text(row.impact) if row.impact else None,
            "applicability": _redact_api_value(row.applicability or {}),
            "provided_conditions": [
                {
                    "key": item.get("key"),
                    "statement": redact_text(str(item.get("statement") or "")),
                    "limitations": redact_text(str(item.get("limitations") or "")) or None,
                    "evidence_refs": list(item.get("evidence_refs") or []),
                }
                for item in (row.provided_conditions or [])
            ],
            "connection_review": _redact_api_value(row.connection_review or {}),
            "connection_review_stale": stale,
            "evidence_refs": list(row.evidence_refs or []),
            "verification_id": str(row.verification_id) if row.verification_id else None,
            "revision": row.revision,
        },
        "relations": [
            {
                "link_id": str(link.id),
                "lead_id": str(link.lead_id),
                "role": link.role,
                "relation_state": link.relation_state,
                "condition_key": link.condition_key,
                "requirement_key": link.requirement_key,
                "source_weakness_revision": link.source_weakness_revision,
                "revision": link.revision,
            }
            for link in links
        ],
        "related_leads": [
            {
                "id": str(item.id),
                "hypothesis": redact_text(item.hypothesis),
                "status": item.status,
                "next_action": redact_text(item.next_action) if item.next_action else None,
                "required_conditions": _redact_api_value(item.required_conditions or []),
                "actionability": (item.actionability_assessment or {}).get("state"),
                "revision": item.revision,
            }
            for item in leads
        ],
        "verifications": [
            {
                "id": str(item.id),
                "conclusion": item.conclusion,
                "method_version": item.method_version,
                "performed_by": item.performed_by,
                "limitations": redact_text(item.limitations) if item.limitations else None,
                "created_at": item.created_at.isoformat(),
            }
            for item in verifications
        ],
        "note": (
            "候选不等于确认；组合假设不因两侧分别成立而成立。"
            "`connection_review_stale=true` 表示该用途判断已过期，需要重新评估。"
        ),
    }


@router.get("/sessions/{session_id}/weaknesses", tags=["read"])
async def list_weaknesses(
    session_id: UUID,
    request: Request,
    weakness_kind: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """弱点列表（与旧 `/findings` 返回同一实体）。"""
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as db:
        await _require_session_row(db, session_id)
        stmt = select(FindingRow).where(FindingRow.session_id == session_id)
        if weakness_kind is not None:
            stmt = stmt.where(FindingRow.weakness_kind == weakness_kind)
        if status_filter is not None:
            stmt = stmt.where(FindingRow.status == status_filter)
        rows = list(
            (
                await db.execute(
                    stmt.order_by(FindingRow.created_at.desc(), FindingRow.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        service = FindingService(db)
        stale_ids = {
            str(row.id)
            for row in rows[:limit]
            if row.connection_review and not service.review_is_current(row)
        }
        unreviewed_ids = {str(row.id) for row in rows[:limit] if not service.review_is_current(row)}
    has_more = len(rows) > limit
    return {
        "weaknesses": [
            {
                "id": str(row.id),
                "title": _redact_api_value(row.title),
                "category": row.category,
                "status": row.status,
                "weakness_kind": row.weakness_kind,
                "impact": _redact_api_value(row.impact),
                "condition_keys": [
                    str(item.get("key")) for item in (row.provided_conditions or [])
                ],
                "connection_reviewed": str(row.id) not in unreviewed_ids,
                "connection_review_stale": str(row.id) in stale_ids,
                "evidence_refs": list(row.evidence_refs or []),
                "verification_id": str(row.verification_id) if row.verification_id else None,
                "revision": row.revision,
            }
            for row in rows[:limit]
        ],
        "next_offset": offset + limit if has_more else None,
        "note": (
            "弱点是弱点，不等于漏洞：独立缺陷与可供后续利用的条件分别标注，"
            "**不能**把弱点数量当作漏洞数量。"
        ),
    }


@router.get("/sessions/{session_id}/findings", tags=["read"])
async def list_findings(
    session_id: UUID,
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as session:
        await _require_session_row(session, session_id)
        stmt = select(FindingRow).where(FindingRow.session_id == session_id)
        if status_filter is not None:
            stmt = stmt.where(FindingRow.status == status_filter)
        rows = list(
            (
                await session.execute(
                    stmt.order_by(FindingRow.created_at.desc(), FindingRow.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
    has_more = len(rows) > limit
    return {
        "findings": [
            {
                "id": str(row.id),
                "title": _redact_api_value(row.title),
                "category": row.category,
                "status": row.status,
                "weakness_kind": row.weakness_kind,
                "impact": _redact_api_value(row.impact),
                "description": _redact_api_value(row.description),
                "evidence_refs": list(row.evidence_refs or []),
                "verification_id": str(row.verification_id) if row.verification_id else None,
                "revision": row.revision,
            }
            for row in rows[:limit]
        ],
        "next_offset": offset + limit if has_more else None,
        "note": (
            "candidate 是候选，不计入已确认数量；confirmed 必须有 verification_id 且引用可用工件。"
            "`/weaknesses` 返回同一实体的弱点视图（含条件与关系）。"
        ),
    }


@router.get("/sessions/{session_id}/coverage", tags=["read"])
async def get_coverage(session_id: UUID, request: Request) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.domain.findings import CoverageService
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as session:
        await _require_session_row(session, session_id)
        summary = await CoverageService(session).summary(session_id)
    return dict(summary)


@router.get("/sessions/{session_id}/jobs", tags=["read"])
async def list_jobs(
    session_id: UUID,
    request: Request,
    state_filter: str | None = Query(default=None, alias="state"),
) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as session:
        rows = await ToolJobRepository(session).list_for_session(session_id)
    if state_filter:
        rows = [row for row in rows if row.state == state_filter]
    return {
        "jobs": [
            {
                "job_id": str(row.job_id),
                "operation_id": str(row.operation_id),
                "executor_id": row.executor_id,
                "state": row.state,
                "sync_status": row.sync_status,
                "cancel_requested": row.cancel_requested_at is not None,
                "cancel_confirmed": row.cancel_confirmed,
                "last_synced_at": row.last_synced_at.isoformat() if row.last_synced_at else None,
            }
            for row in rows
        ],
        "note": "这是控制端对 Executor 台账的**同步投影**，不是真实执行状态的权威。",
    }


@router.get("/artifacts/{artifact_id}", tags=["read"])
async def read_artifact(
    artifact_id: UUID,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=65536, ge=1, le=1048576),
) -> dict[str, Any]:
    """**脱敏预览**。原件按字节保存，此接口不返回可执行内容。"""
    get_auth().require_session(request)
    from agent_workbench.evidence.artifacts import build_artifact_store
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as session:
        row = await ArtifactRepository(session).get(artifact_id)
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"工件不存在：{artifact_id}")

    store = build_artifact_store()
    if row.availability != "available" or not store.exists(row.relative_key):
        raise ApiError(
            ErrorCode.CONFLICT,
            f"工件当前不可用（availability={row.availability}）",
            details={"availability": row.availability},
        )

    raw = store.read_bytes(row.relative_key, offset=offset, limit=limit)
    preview = store.render_redacted_view(row.relative_key, media_type=row.media_type)
    return {
        "artifact_id": str(row.id),
        "media_type": row.media_type,
        "byte_size": row.byte_size,
        "sha256": row.sha256,
        "sensitivity": row.sensitivity,
        "captured_at": row.captured_at.isoformat(),
        "captured_by_identity": row.captured_by_identity,
        "source_url": row.source_url,
        "view": "redacted_text",
        "offset": offset,
        "returned_bytes": len(raw),
        "preview": preview[:limit],
        "note": (
            "预览已脱敏且按转义文本展示；HTML/脚本不会在应用源域执行。完整证据包需单独受控导出。"
        ),
    }


@router.get("/sessions/{session_id}/usage", tags=["read"])
async def get_usage(
    session_id: UUID,
    request: Request,
    run_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as session:
        await _require_session_row(session, session_id)
        if run_id is not None:
            owned_run = (
                await session.execute(
                    select(RunRow.id).where(RunRow.id == run_id, RunRow.session_id == session_id)
                )
            ).scalar_one_or_none()
            if owned_run is None:
                raise ApiError(ErrorCode.NOT_FOUND, f"run 不存在：{run_id}")
            rows = await ModelCallRepository(session).list_for_run(run_id)
        else:
            from agent_workbench.persistence.tables import ModelCallRow

            rows = (
                (
                    await session.execute(
                        select(ModelCallRow).where(ModelCallRow.session_id == session_id)
                    )
                )
                .scalars()
                .all()
            )
    payloads = [
        {
            "cost_status": row.cost_status,
            "usage_status": row.usage_status,
            "cost_amount": row.cost_amount,
            "cost_currency": row.cost_currency,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "cache_read_tokens": row.cache_read_tokens,
            "cache_write_tokens": row.cache_write_tokens,
            "reasoning_tokens": row.reasoning_tokens,
        }
        for row in rows
    ]
    summary = summarize_costs(payloads)
    return {**summary, "call_count": len(payloads)}


@router.get("/sessions/{session_id}/model-calls", response_model=ModelCallPage, tags=["read"])
async def list_model_calls(
    session_id: UUID,
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """费用时间线读取台账最终值；不累加流式估计与最终用量。"""
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database
    from agent_workbench.persistence.tables import ModelCallRow

    async with get_database().session() as db:
        await _require_session_row(db, session_id)
        rows = list(
            (
                await db.execute(
                    select(ModelCallRow)
                    .where(ModelCallRow.session_id == session_id)
                    .order_by(
                        ModelCallRow.submitted_at.desc().nullslast(),
                        ModelCallRow.model_call_id,
                    )
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
    return {
        "model_calls": [
            {
                "model_call_id": str(row.model_call_id),
                "run_id": str(row.run_id),
                "phase": row.phase,
                "retry_index": row.retry_index,
                "provider_id": row.provider_id,
                "model_name": row.model_name,
                "usage_status": row.usage_status,
                "cost_status": row.cost_status,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "cache_read_tokens": row.cache_read_tokens,
                "cache_write_tokens": row.cache_write_tokens,
                "reasoning_tokens": row.reasoning_tokens,
                "cost_amount": str(row.cost_amount) if row.cost_amount is not None else None,
                "cost_currency": row.cost_currency,
                "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
                "first_token_at": (row.first_token_at.isoformat() if row.first_token_at else None),
                "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                "error_code": row.error_code,
            }
            for row in rows[:limit]
        ],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


@router.get("/sessions/{session_id}/operations", response_model=OperationPage, tags=["read"])
async def list_tool_operations(
    session_id: UUID,
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """只读工具操作时间线；时间戳为控制端记录，不冒充远端执行耗时。"""
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database
    from agent_workbench.persistence.tables import ToolJobRow, ToolOperationRow

    async with get_database().session() as db:
        await _require_session_row(db, session_id)
        rows = list(
            (
                await db.execute(
                    select(ToolOperationRow, ToolJobRow)
                    .outerjoin(ToolJobRow, ToolOperationRow.job_id == ToolJobRow.job_id)
                    .where(ToolOperationRow.session_id == session_id)
                    .order_by(ToolOperationRow.created_at.desc(), ToolOperationRow.operation_id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
        )
    return {
        "operations": [
            {
                "operation_id": str(operation.operation_id),
                "run_id": str(operation.run_id),
                "tool_name": operation.tool_name,
                "status": operation.status,
                "created_at": operation.created_at.isoformat(),
                "updated_at": operation.updated_at.isoformat(),
                "job_state": job.state if job else None,
                "sync_status": job.sync_status if job else None,
                "executor_accepted_at": job.executor_accepted_at if job else None,
                "executor_started_at": job.executor_started_at if job else None,
                "executor_finished_at": job.executor_finished_at if job else None,
            }
            for operation, job in rows[:limit]
        ],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


@router.get("/sessions/{session_id}/spans", response_model=SpanPage, tags=["read"])
async def list_diagnostic_spans(
    session_id: UUID,
    request: Request,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """只读阶段耗时；仅暴露确定的类别和结果，不返回原始属性或异常正文。"""
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database
    from agent_workbench.persistence.tables import SpanRow

    async with get_database().session() as db:
        await _require_session_row(db, session_id)
        rows = list(
            (
                await db.execute(
                    select(SpanRow)
                    .where(SpanRow.session_id == session_id)
                    .order_by(SpanRow.started_at.desc(), SpanRow.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
    return {
        "spans": [
            {
                "id": str(row.id),
                "run_id": str(row.run_id) if row.run_id else None,
                "name": row.name,
                "started_at": row.started_at,
                "duration_ms": float(row.duration_ms) if row.duration_ms is not None else None,
                "error": (
                    row.error
                    if row.error and row.error.isascii() and row.error.isidentifier()
                    else None
                ),
                "decision": (
                    row.attributes.get("decision")
                    if row.attributes.get("decision")
                    in {"complete", "continue", "wait_user", "wait_tool"}
                    else None
                ),
                "status": (
                    row.attributes.get("status")
                    if row.attributes.get("status") in {"completed", "failed"}
                    else None
                ),
            }
            for row in rows[:limit]
        ],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


@router.post(
    "/sessions/{session_id}/reports",
    status_code=status.HTTP_201_CREATED,
    tags=["reports"],
)
async def create_report(session_id: UUID, request: Request) -> dict[str, Any]:
    """从当前业务表创建不可变报告版本；已确认发现必须通过证据校验。"""
    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.require_session(request)
    auth.require_csrf(request, ticket)
    from agent_workbench.domain.reports import create_report_snapshot
    from agent_workbench.evidence.artifacts import build_artifact_store
    from agent_workbench.persistence.db import get_database

    result = await create_report_snapshot(get_database(), build_artifact_store(), session_id)
    return {
        "version": result.version,
        "markdown_artifact_id": str(result.markdown_artifact_id),
        "html_artifact_id": str(result.html_artifact_id),
        "confirmed_count": result.confirmed_count,
        "candidate_count": result.candidate_count,
    }


@router.get("/sessions/{session_id}/reports", tags=["reports"])
async def list_reports(session_id: UUID, request: Request) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as session:
        exists = (
            await session.execute(select(SessionRow.id).where(SessionRow.id == session_id))
        ).scalar_one_or_none()
        if exists is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"session 不存在：{session_id}")
        rows = (
            (
                await session.execute(
                    select(ReportVersionRow)
                    .where(ReportVersionRow.session_id == session_id)
                    .order_by(ReportVersionRow.version.desc())
                )
            )
            .scalars()
            .all()
        )
    return {"reports": [_report_metadata(row) for row in rows]}


@router.get("/sessions/{session_id}/reports/{version}", tags=["reports"])
async def get_report(session_id: UUID, version: int, request: Request) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as session:
        row = (
            await session.execute(
                select(ReportVersionRow).where(
                    ReportVersionRow.session_id == session_id,
                    ReportVersionRow.version == version,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, f"报告版本不存在：{version}")
    return _report_metadata(row)


@router.get("/sessions/{session_id}/reports/{version}/export", tags=["reports"])
async def export_report(
    session_id: UUID,
    version: int,
    request: Request,
    format: str = Query(default="markdown", pattern="^(markdown|html)$"),
) -> Response:
    """导出生成时已脱敏的快照；始终作为附件下载。"""
    get_auth().require_session(request)
    from agent_workbench.evidence.artifacts import ArtifactError, build_artifact_store
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as session:
        row = (
            await session.execute(
                select(ReportVersionRow).where(
                    ReportVersionRow.session_id == session_id,
                    ReportVersionRow.version == version,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"报告版本不存在：{version}")
        artifact_id = row.html_artifact_id if format == "html" else row.markdown_artifact_id
        artifact = await ArtifactRepository(session).get(artifact_id) if artifact_id else None
    store = build_artifact_store()
    valid = False
    if (
        artifact is not None
        and artifact.session_id == session_id
        and artifact.availability == "available"
    ):
        try:
            valid = store.verify(artifact.relative_key, artifact.sha256)
        except ArtifactError:
            valid = False
    if not valid or artifact is None:
        raise ApiError(ErrorCode.CONFLICT, "报告工件缺失或哈希校验失败")
    extension = "html" if format == "html" else "md"
    return Response(
        content=store.read_bytes(artifact.relative_key),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="report-v{version}.{extension}"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
        },
    )


def _report_metadata(row: ReportVersionRow) -> dict[str, Any]:
    return {
        "version": row.version,
        "status": row.generation_status,
        "is_historical": row.is_historical,
        "created_at": row.created_at.isoformat(),
        "run_id": str(row.run_id) if row.run_id else None,
        "markdown_artifact_id": str(row.markdown_artifact_id) if row.markdown_artifact_id else None,
        "html_artifact_id": str(row.html_artifact_id) if row.html_artifact_id else None,
        "artifact_refs": list(row.artifact_refs or []),
        "input_data_revision": row.input_data_revision,
    }


# --------------------------------------------------------------------------- #
# 设置与诊断
# --------------------------------------------------------------------------- #


@router.get("/settings/models", response_model=ModelSettingsRead, tags=["settings"])
async def get_model_settings(request: Request) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.config.catalog import default_provider_profiles
    from agent_workbench.config.model_settings import read_model_key, read_preferences
    from agent_workbench.domain.prices import current_price_catalog
    from agent_workbench.persistence.db import get_database

    settings = get_settings()
    preferences = read_preferences(settings)
    async with get_database().session() as db:
        catalog = await current_price_catalog(db)
    profiles = []
    for profile in default_provider_profiles().values():
        endpoint = preferences.endpoints.get(profile.provider_id) or (
            settings.xiaomi_base_url
            if profile.provider_id == "xiaomi"
            else settings.dashscope_base_url
        )
        profiles.append(
            {
                **profile.model_dump(),
                "base_url": endpoint,
                "secret_configured": bool(read_model_key(profile.provider_id, settings)),
                "request_output_limit": profile.effective_output_limit,
                "allowed_input_tokens": int(
                    profile.input_budget_tokens * (1 - profile.budget_safety_margin_ratio)
                ),
            }
        )
    return {
        "default_provider": preferences.default_provider,
        "default_model": preferences.default_model,
        "profiles": profiles,
        "price_catalog_version": catalog.version,
        "effective_scope": "模型选择用于新阶段；当前阶段及暂停继续保留原模型。Key 在本机保存，新的运行阶段或暂停后继续会读取新 Key。",
    }


@router.put("/settings/models", response_model=ModelSettingsRead, tags=["settings"])
async def update_model_settings(request: Request, payload: ModelSettingsUpdate) -> dict[str, Any]:
    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.require_session(request)
    auth.require_csrf(request, ticket)
    from agent_workbench.config.catalog import default_provider_profiles
    from agent_workbench.config.model_settings import (
        read_preferences,
        save_preferences,
        validate_endpoint,
        write_model_key,
    )

    if (payload.provider_id, payload.model_name) not in default_provider_profiles():
        raise ApiError(ErrorCode.VALIDATION_FAILED, "请选择列表中的模型")
    try:
        endpoint = validate_endpoint(payload.provider_id, payload.base_url)
    except ValueError as exc:
        raise ApiError(ErrorCode.VALIDATION_FAILED, str(exc)) from None
    key = payload.api_key.get_secret_value().strip() if payload.api_key is not None else None
    if key is not None and (not key or any(char.isspace() for char in key)):
        raise ApiError(ErrorCode.VALIDATION_FAILED, "API Key 不能为空或包含空白字符")
    if payload.delete_api_key and key is not None:
        raise ApiError(ErrorCode.VALIDATION_FAILED, "删除 Key 时不能同时填写新 Key")
    from agent_workbench.config.model_settings import configuration_lock

    with configuration_lock:
        settings = get_settings()
        preferences = read_preferences(settings)
        preferences.endpoints[payload.provider_id] = endpoint
        if payload.set_default:
            preferences.default_provider = payload.provider_id
            preferences.default_model = payload.model_name
        if key is not None or payload.delete_api_key:
            write_model_key(payload.provider_id, key, settings)
        save_preferences(preferences, settings)
    return await get_model_settings(request)


@router.get("/settings/prices", response_model=PriceCatalog, tags=["settings"])
async def get_price_settings(request: Request) -> PriceCatalog:
    get_auth().require_session(request)
    from agent_workbench.domain.prices import current_price_catalog
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as db:
        return await current_price_catalog(db)


@router.post("/settings/prices", response_model=PriceCatalog, tags=["settings"])
async def update_price_settings(request: Request, payload: PriceUpdate) -> PriceCatalog:
    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.require_session(request)
    auth.require_csrf(request, ticket)
    from agent_workbench.domain.prices import update_price_catalog
    from agent_workbench.persistence.db import get_database

    async with get_database().transaction() as db:
        return await update_price_catalog(db, payload)


@router.post("/executors/{executor_id}/doctor", status_code=status.HTTP_200_OK, tags=["settings"])
async def executor_doctor(executor_id: str, request: Request) -> dict[str, Any]:
    """从**执行端**检查能力与网络；**控制端能打开网页不代表执行岛能访问**。"""
    get_auth().require_session(request)
    from agent_workbench.execution.client import (
        ExecutorConnectionError,
        HttpExecutorClient,
    )
    from agent_workbench.execution.tunnel import SshTunnel, TunnelUnavailable

    settings = get_settings()
    tunnel = SshTunnel(settings)
    client = HttpExecutorClient(tunnel, settings=settings, expected_executor_id=executor_id)
    try:
        await client.start()
        health = await client.health()
        caps = await client.capabilities()
    except (TunnelUnavailable, ExecutorConnectionError) as exc:
        return {
            "executor_id": executor_id,
            "reachable": False,
            "classification": "infrastructure_failure",
            "detail": f"{type(exc).__name__}: {exc}",
            "note": "这是基础设施故障，不是目标网站的阴性结论。",
        }
    finally:
        await client.close()
        await tunnel.close()

    return {
        "executor_id": health.executor_id,
        "reachable": True,
        "service_version": health.service_version,
        "protocol_version": health.protocol_version,
        "boot_id": health.boot_id,
        "uptime_seconds": health.uptime_seconds,
        "free_bytes": health.free_bytes,
        "clock_utc": health.clock_utc.isoformat(),
        "active_jobs": health.active_jobs,
        "capabilities": {
            "browser_available": caps.browser_available,
            "browser_version": caps.browser_version,
            "http_available": caps.http_available,
            "command_available": caps.command_available,
            "filesystem_available": caps.filesystem_available,
            "network_isolation": caps.network_isolation,
            "capabilities_version": caps.capabilities_version,
            "execution_mode": caps.execution_mode,
            "execution": caps.execution,
        },
        "honesty_note": (
            "命令能力按执行端声明的 trusted 模式和依赖开放；"
            "可信执行不承诺系统级网络隔离，工作目录也不是隔离边界。"
        ),
    }


@router.get("/sessions/{session_id}/commands", tags=["read"])
async def list_commands(session_id: UUID, request: Request) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database
    from agent_workbench.persistence.repositories import CommandRepository

    async with get_database().session() as session:
        pending = await CommandRepository(session).list_pending(session_id)
    return {
        "pending": [
            {
                "id": str(row.id),
                "kind": row.kind,
                "status": row.status,
                "priority": row.priority,
                "created_at": row.created_at.isoformat(),
            }
            for row in pending
        ]
    }


@router.post("/runs/{run_id}/objective", tags=["runs"])
async def change_objective(
    run_id: UUID,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    content: Annotated[str, Body(embed=True)],
) -> dict[str, Any]:
    """用户明确改变方向：**递增 control_version**，让过期模型响应不再派发派生操作。"""
    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.require_session(request)
    auth.require_csrf(request, ticket)

    from agent_workbench.persistence.db import get_database

    async with get_database().transaction() as session:
        run = (
            await session.execute(select(RunRow).where(RunRow.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"run 不存在：{run_id}")
        await session.execute(
            select(SessionRow).where(SessionRow.id == run.session_id).with_for_update()
        )
        run = (
            await session.execute(
                select(RunRow)
                .where(RunRow.id == run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        existing = (
            await session.execute(
                select(CommandRow).where(
                    CommandRow.session_id == run.session_id,
                    CommandRow.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if (
                existing.run_id != run_id
                or existing.kind != str(CommandKind.MESSAGE)
                or existing.content != content
                or existing.payload.get("explicit_direction_change") is not True
            ):
                raise ApiError(
                    ErrorCode.IDEMPOTENCY_KEY_REUSED,
                    "幂等键已用于不同指令或内容；请使用新键",
                )
            return {
                "run_id": str(run_id),
                "control_version": existing.control_version,
                "command_id": str(existing.id),
                "status": existing.status,
            }
        if RunStatus(run.status) not in ACTIVE_RUN_STATUSES:
            raise ApiError(ErrorCode.CONFLICT, "该 run 已结束，请在任务中发送新消息开始下一阶段")
        version = await bump_control_version(session, run_id=run_id)
        receipt = await submit_message(
            session,
            session_id=run.session_id,
            content=content,
            idempotency_key=idempotency_key,
            provider_id=run.provider_id,
            model_name=run.model_name,
            config_snapshot={"control_version": version},
            payload={"control_version": version, "explicit_direction_change": True},
        )
        await append_event(
            session,
            session_id=run.session_id,
            run_id=run_id,
            event_type=EventType.RUN_STATUS,
            payload={
                "event": "control_version_bumped",
                "control_version": version,
                "note": "旧模型响应可以记录用量，但派发前必须重新检查版本。",
            },
        )
    return {
        "run_id": str(run_id),
        "control_version": version,
        "command_id": str(receipt.command_id),
        "status": str(receipt.status),
    }


@router.put("/sessions/{session_id}/scope", tags=["sessions"])
async def change_session_scope(
    session_id: UUID,
    request: Request,
    payload: ScopeUpdate,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> dict[str, Any]:
    """显式应用新范围，并让旧模型提议和已派发 job 带上历史版本标记。"""
    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.require_session(request)
    auth.require_csrf(request, ticket)
    _validate_scope_or_422(payload.scope)
    if payload.scope.revision != payload.expected_revision + 1:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            "新范围 revision 必须等于 expected_revision + 1",
        )

    from agent_workbench.persistence.db import get_database

    scope_data = payload.scope.model_dump(mode="json")
    async with get_database().transaction() as session:
        task = (
            await session.execute(
                select(SessionRow).where(SessionRow.id == session_id).with_for_update()
            )
        ).scalar_one_or_none()
        if task is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"session 不存在：{session_id}")
        existing = (
            await session.execute(
                select(CommandRow).where(
                    CommandRow.session_id == session_id,
                    CommandRow.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if (
                existing.kind != str(CommandKind.SCOPE_CHANGE)
                or existing.content != payload.reason
                or existing.payload.get("expected_revision") != payload.expected_revision
                or existing.payload.get("scope") != scope_data
            ):
                raise ApiError(ErrorCode.IDEMPOTENCY_KEY_REUSED, "幂等键已用于不同范围变更")
            return {
                "session_id": str(session_id),
                "run_id": str(existing.run_id),
                "scope_revision": existing.payload["scope_revision"],
                "control_version": existing.control_version,
                "command_id": str(existing.id),
                "status": existing.status,
            }

        old_scope = ScopeSnapshot.model_validate(task.scope_snapshot or {})
        if old_scope.revision != payload.expected_revision:
            raise ApiError(
                ErrorCode.CONFLICT,
                "范围修订已变化，请获取最新范围后重试",
                details={"current_revision": old_scope.revision},
            )
        run = (
            await session.execute(
                select(RunRow)
                .where(
                    RunRow.session_id == session_id,
                    RunRow.status.in_([str(item) for item in ACTIVE_RUN_STATUSES]),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if run is None:
            raise ApiError(ErrorCode.CONFLICT, "当前没有活动 run，请先开始新的运行阶段")

        await SessionRepository(session).update_scope(session_id, scope_data)
        version = await bump_control_version(session, run_id=run.id)
        submitted = await CommandRepository(session).submit(
            session_id=session_id,
            run_id=run.id,
            kind=CommandKind.SCOPE_CHANGE,
            content=payload.reason,
            idempotency_key=idempotency_key,
            payload={
                "applied": True,
                "expected_revision": payload.expected_revision,
                "scope_revision": payload.scope.revision,
                "scope": scope_data,
            },
            control_version=version,
        )
        await append_event(
            session,
            session_id=session_id,
            run_id=run.id,
            event_type=EventType.RUN_STATUS,
            payload={
                "event": "scope_updated",
                "scope_revision": payload.scope.revision,
                "control_version": version,
                "reason": payload.reason,
                "note": "旧提议不再派发；已派发 job 继续核对并标记为历史结果。",
            },
        )
    return {
        "session_id": str(session_id),
        "run_id": str(run.id),
        "scope_revision": payload.scope.revision,
        "control_version": version,
        "command_id": str(submitted.row.id),
        "status": submitted.row.status,
    }


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _validate_scope_or_422(snapshot: Any) -> None:
    """范围校验失败必须是 **422 validation_failed**，不能漏成 500。

    `validate_scope` 抛的是领域层的 `ValueError`；这里统一转成统一错误体，
    让客户端能区分"我给的参数有问题"和"服务端出错了"。
    """
    try:
        validate_scope(snapshot)
    except ValueError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_FAILED,
            str(exc),
            details={"field": "scope.rules"},
        ) from exc


def _count_by(rows: Sequence[Any], attribute: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(getattr(row, attribute))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _now() -> datetime:
    return datetime.now(UTC)
