"""控制端 FastAPI 应用。

- **只绑定回环地址**；
- 错误体统一为 `code/message/retryable/request_id/details`；
- **不在**请求处理函数或 SSE generator 中驱动整段 Agent；
- 发布时可由本应用同时服务前端构建出的静态文件；开发时 Vite 代理 `/api`。
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError

from agent_workbench import __version__
from agent_workbench.api.auth import LocalAuth
from agent_workbench.config.settings import REPO_ROOT, get_settings
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError, ErrorBody
from agent_workbench.observability.logging import configure_logging, get_logger
from agent_workbench.persistence.bootstrap import assert_business_schema_current
from agent_workbench.persistence.db import get_database
from agent_workbench.platform_compat import (
    assert_supported_event_loop,
    configure_event_loop_policy,
)

log = get_logger("agent_workbench.api")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001
    settings = get_settings()
    configure_logging()
    # 快速失败：Windows 上若事件循环是 Proactor，psycopg 异步会在第一次查询时报
    # `InterfaceError`，表现为 500 而不是可操作的错误信息。这里提前给出明确指引。
    assert_supported_event_loop()
    settings.ensure_directories()
    db = get_database()
    await db.start()
    try:
        async with db.session() as session:
            await assert_business_schema_current(session, settings)
        yield
    finally:
        # 生命周期成对：启动与关闭都显式处理
        await db.close()


def _constraint_name(exc: IntegrityError) -> str | None:
    """从 psycopg 的异常里取出约束名，用于给出可理解的冲突说明。"""
    diagnostic = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    return str(diagnostic) if diagnostic else None


_CONFLICT_MESSAGES = {
    "uq_projects_owner_name": "同一用户下已存在同名项目；请换一个名称。",
    "uq_commands_session_idem": "该幂等键已被使用；同键同内容会返回原结果，同键不同内容会被拒绝。",
    "uq_runs_active_per_session": "该 session 已存在活动 run；请提交消息而不是新建 run。",
    "uq_events_session_seq": "事件序号冲突，请重试本次操作。",
}


def _conflict_message(constraint: str | None) -> str:
    if constraint and constraint in _CONFLICT_MESSAGES:
        return _CONFLICT_MESSAGES[constraint]
    return "请求与当前数据状态冲突；请检查是否重复提交或顺序有误。"


def create_app() -> FastAPI:
    # Windows 上 psycopg 异步需要 Selector 事件循环；与 Runner/CLI 保持一致
    configure_event_loop_policy()
    settings = get_settings()
    app = FastAPI(
        title="Agent Workbench API",
        version=__version__,
        lifespan=lifespan,
    )

    # 精确 Origin 白名单。不因为"本地"就放任任意跨源读取。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id"],
    )

    @app.middleware("http")
    async def _request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        body = exc.to_body(getattr(request.state, "request_id", None))
        return JSONResponse(status_code=body.http_status, content=body.model_dump(mode="json"))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        body = ErrorBody(
            code=ErrorCode.VALIDATION_FAILED,
            message="请求参数校验失败",
            retryable=False,
            request_id=getattr(request.state, "request_id", None),
            details={"errors": exc.errors()[:20]},
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @app.exception_handler(IntegrityError)
    async def _integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        """数据库约束冲突 -> 409，而不是 500。

        典型场景：项目名在同一 owner 下重复、幂等键并发插入、同一 session 起第二个
        活动 run。这些都是**客户端可理解的冲突**，必须给出稳定的错误码，
        并且**不能**把完整 SQL 与参数回给客户端（可能含业务数据）。
        """
        constraint = _constraint_name(exc)
        log.info(
            "db_constraint_violation",
            constraint=constraint,
            path=request.url.path,
        )
        body = ErrorBody(
            code=ErrorCode.CONFLICT,
            message=_conflict_message(constraint),
            retryable=False,
            request_id=getattr(request.state, "request_id", None),
            details={"constraint": constraint} if constraint else {},
        )
        return JSONResponse(status_code=409, content=body.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 记录完整异常用于诊断；返回给客户端的只有稳定错误码与简短信息
        log.error(
            "unhandled_api_error",
            error=f"{type(exc).__name__}: {exc}",
            path=request.url.path,
        )
        body = ErrorBody(
            code=ErrorCode.INTERNAL_ERROR,
            message="服务内部错误（详情见服务端日志）",
            retryable=True,
            request_id=getattr(request.state, "request_id", None),
            details={},
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    from agent_workbench.api.routes import router

    app.include_router(router)

    # ------------------------------------------------------------------ #
    # 最小观察器（P2）与 P7 基础控制台
    #
    # 只读窗口显示快照、事件流、助手输出与用量；基础控制台由 web/dist 提供。
    # ------------------------------------------------------------------ #
    observer = REPO_ROOT / "web" / "observer.html"

    @app.get("/observer", include_in_schema=False)
    async def observer_page() -> Any:
        if not observer.exists():
            raise HTTPException(
                status_code=404, detail="观察器页面不存在（web/observer.html 未找到）"
            )
        return FileResponse(
            observer,
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    web_dist = REPO_ROOT / "web" / "dist"
    if web_dist.is_dir():
        app.mount("/console", StaticFiles(directory=str(web_dist), html=True), name="console")

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, Any]:
        db = get_database()
        try:
            info = await db.ping()
            db_ok = True
        except Exception as exc:
            info = {"error": f"{type(exc).__name__}: {exc}"}
            db_ok = False
        return {
            "service": "agent-workbench-api",
            "version": __version__,
            "database": {"ok": db_ok, **info},
            "auth_configured": not LocalAuth()._is_default_secret(settings.session_secret),
            "note": ("本服务只应监听回环地址；健康检查不需要鉴权，因此不返回任何配置细节。"),
        }

    return app


app = create_app()
