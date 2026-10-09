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

from agent_workbench import __version__
from agent_workbench.api.auth import LocalAuth
from agent_workbench.config.settings import REPO_ROOT, get_settings
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError, ErrorBody
from agent_workbench.observability.logging import configure_logging, get_logger
from agent_workbench.persistence.db import get_database
from agent_workbench.platform_compat import configure_event_loop_policy

log = get_logger("agent_workbench.api")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # noqa: ARG001
    settings = get_settings()
    configure_logging()
    settings.ensure_directories()
    db = get_database()
    await db.start()
    try:
        yield
    finally:
        # 生命周期成对：启动与关闭都显式处理
        await db.close()


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
    # 最小观察器（P2）
    #
    # 只读窗口：显示快照、事件流、助手输出与用量。完整控制台属 P7，尚未实现。
    # 以静态文件方式提供，不引入前端构建链；发布时同一入口可服务 web/dist。
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
