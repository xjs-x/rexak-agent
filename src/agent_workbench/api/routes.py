"""控制端 API 路由（§12.1）。

硬约束：
- 请求处理函数**不运行整段 Agent**：消息只写 `commands` 并返回 202；
- 异步接收用 202，完成用 200，**两者不混用**；
- 统一错误体：`code、message、retryable、request_id、details`；
  422 校验失败、409 状态冲突、404 资源不存在。
"""

from __future__ import annotations

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
from sqlalchemy import select

from agent_workbench.api.auth import LocalAuth
from agent_workbench.api.sse import SSE_HEADERS, event_stream
from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.enums import CommandKind, ErrorCode, RunStatus
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.events import EventType
from agent_workbench.contracts.identity import (
    ProjectCreate,
    SessionCreate,
)
from agent_workbench.domain.run_state import (
    assert_resumable,
    bump_control_version,
    set_run_status,
)
from agent_workbench.observability.costs import summarize_costs
from agent_workbench.observability.events import append_event, latest_seq, read_events_after
from agent_workbench.persistence.repositories import (
    ArtifactRepository,
    ModelCallRepository,
    ProjectRepository,
    SessionRepository,
    ToolJobRepository,
)
from agent_workbench.persistence.tables import (
    CoverageRow,
    FindingRow,
    IdentityRow,
    LeadRow,
    RunRow,
    SessionRow,
)
from agent_workbench.runtime.commands import submit_control, submit_message

router = APIRouter(prefix="/api")

_auth = LocalAuth()


def get_auth() -> LocalAuth:
    return _auth


# --------------------------------------------------------------------------- #
# 会话与鉴权
# --------------------------------------------------------------------------- #


@router.post("/session", status_code=status.HTTP_200_OK, tags=["auth"])
async def create_local_session(response: Response, request: Request) -> dict[str, Any]:
    """签发本地会话 Cookie 与 CSRF 令牌。

    只允许从回环地址调用；同源校验在中间件里执行。
    """
    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.issue(response)
    return {
        "issued_at": ticket.issued_at,
        "csrf_token": ticket.csrf_token,
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

    from agent_workbench.domain.scope import validate_scope

    validate_scope(payload.default_scope)

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
    from agent_workbench.domain.scope import validate_scope
    from agent_workbench.persistence.db import get_database

    validate_scope(payload.scope)
    settings = get_settings()

    async with get_database().transaction() as session:
        project = await ProjectRepository(session).get(project_id)
        if project is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"项目不存在：{project_id}")

        session_row = await SessionRepository(session).create(
            project_id=project_id,
            title=payload.title,
            scope_snapshot=payload.scope.model_dump(),
            policy_snapshot=payload.policy.model_dump(),
            provider_id=payload.provider_id or settings.default_provider,
            model_name=payload.model_name or settings.xiaomi_model,
        )

        for identity in payload.identities:
            session.add(
                IdentityRow(
                    project_id=project_id,
                    display_name=identity.display_name,
                    role_label=identity.role_label,
                    secret_ref=identity.secret_ref,
                    auth_state_ref=identity.auth_state_ref,
                    auth_revision=identity.auth_revision,
                )
            )
        await session.flush()

        run = await create_run(
            session,
            session_id=session_row.id,
            objective=payload.initial_objective,
            provider_id=session_row.provider_id or settings.default_provider,
            model_name=session_row.model_name or settings.xiaomi_model,
            config_snapshot={
                "scope_revision": payload.scope.revision,
                "policy_revision": payload.policy.revision,
                "created_from_session_create": True,
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
) -> dict[str, Any]:
    """**只写入指令并返回 202**，不在请求处理函数里驱动 Agent。"""
    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.require_session(request)
    auth.require_csrf(request, ticket)

    from agent_workbench.persistence.db import get_database

    settings = get_settings()
    async with get_database().transaction() as session:
        receipt = await submit_message(
            session,
            session_id=session_id,
            content=content,
            idempotency_key=idempotency_key,
            provider_id=settings.default_provider,
            model_name=settings.xiaomi_model,
            config_snapshot={"created_from": "message"},
        )
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
) -> dict[str, Any]:
    """resume **只作用于可恢复的 run**；已 completed 的 run 必须创建新 run。"""
    auth = get_auth()
    auth.check_origin(request)
    ticket = auth.require_session(request)
    auth.require_csrf(request, ticket)

    from agent_workbench.persistence.db import get_database

    async with get_database().transaction() as session:
        run = await assert_resumable(session, run_id)
        receipt = await submit_control(
            session,
            run=run,
            kind=CommandKind.RESUME,
            idempotency_key=idempotency_key,
            payload={"content": content} if content else {},
        )
        await set_run_status(session, run_id=run_id, target=RunStatus.QUEUED)
    return {
        "run_id": str(run_id),
        "status": str(RunStatus.QUEUED),
        "command_id": str(receipt.command_id),
        "message": "已校验可恢复状态并重新排队",
    }


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
        receipt = await submit_control(session, run=run, kind=kind, idempotency_key=idempotency_key)
        if current is RunStatus.RUNNING or current is RunStatus.WAITING_TOOL:
            await set_run_status(session, run_id=run_id, target=target)

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


@router.get("/sessions/{session_id}/snapshot", tags=["read"])
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
        if session_row is None:
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
                "status": row.status,
                "completion_reason": row.completion_reason,
                "objective": row.objective,
                "control_version": row.control_version,
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


# --------------------------------------------------------------------------- #
# 发现 / 覆盖 / 任务 / 工件 / 用量
# --------------------------------------------------------------------------- #


@router.get("/sessions/{session_id}/findings", tags=["read"])
async def list_findings(
    session_id: UUID,
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as session:
        stmt = select(FindingRow).where(FindingRow.session_id == session_id)
        if status_filter:
            stmt = stmt.where(FindingRow.status == status_filter)
        rows = (await session.execute(stmt.order_by(FindingRow.created_at.desc()))).scalars().all()
    return {
        "findings": [
            {
                "id": str(row.id),
                "title": row.title,
                "category": row.category,
                "status": row.status,
                "impact": row.impact,
                "description": row.description,
                "evidence_refs": list(row.evidence_refs or []),
                "verification_id": str(row.verification_id) if row.verification_id else None,
                "revision": row.revision,
            }
            for row in rows
        ],
        "note": (
            "candidate 是候选，不计入已确认数量；confirmed 必须有 verification_id 且引用可用工件。"
        ),
    }


@router.get("/sessions/{session_id}/coverage", tags=["read"])
async def get_coverage(session_id: UUID, request: Request) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.domain.findings import CoverageService
    from agent_workbench.persistence.db import get_database

    async with get_database().session() as session:
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
        if run_id is not None:
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


@router.post(
    "/sessions/{session_id}/reports",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
    tags=["read"],
)
async def create_report(session_id: UUID, request: Request) -> dict[str, Any]:
    """报告生成在 P6 交付；此处**如实返回未实现**，不用假实现冒充。"""
    get_auth().require_session(request)
    _ = session_id
    return {
        "code": "not_implemented",
        "message": "报告快照与 Markdown/HTML 渲染在 P6 交付；当前版本不提供该能力。",
    }


# --------------------------------------------------------------------------- #
# 设置与诊断
# --------------------------------------------------------------------------- #


@router.get("/settings/models", tags=["settings"])
async def get_model_settings(request: Request) -> dict[str, Any]:
    get_auth().require_session(request)
    from agent_workbench.config.catalog import default_price_catalog, default_provider_profiles
    from agent_workbench.config.secrets import build_secret_store

    settings = get_settings()
    store = build_secret_store()
    profiles = default_provider_profiles()
    return {
        "default_provider": settings.default_provider,
        "profiles": [
            {
                "provider_id": profile.provider_id,
                "model_name": profile.model_name,
                "base_url": profile.base_url,
                "secret_ref": profile.secret_ref,
                # 只暴露"是否已配置"，绝不返回密钥本身
                "secret_configured": store.exists(profile.secret_ref),
                "context_limit": profile.context_limit,
                "output_reserve": profile.output_reserve,
                "supports_tools": profile.supports_tools,
                "supports_streaming": profile.supports_streaming,
                "supports_images": profile.supports_images,
                "supports_usage": profile.supports_usage,
                "token_estimation_method": profile.token_estimation_method,
                "profile_version": profile.profile_version,
            }
            for profile in profiles.values()
        ],
        "price_catalog_version": default_price_catalog().version,
        "effective_scope": "修改默认模型只影响**新建的 run**；活动 run 保持其配置快照。",
    }


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
        },
        "honesty_note": (
            "network_isolation=unavailable 意味着通用命令的无人值守模式应被关闭："
            "正则黑名单、工作目录与提示词都不能替代执行环境级网络隔离。"
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


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _count_by(rows: Sequence[Any], attribute: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(getattr(row, attribute))
        counts[key] = counts.get(key, 0) + 1
    return counts


def _now() -> datetime:
    return datetime.now(UTC)
