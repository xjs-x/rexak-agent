"""统一错误体（BUILD_PLAN §12.1）。

校验失败 422，状态冲突 409，资源不存在 404；异步接收 202 与完成 200 不能混用。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from agent_workbench.contracts.enums import ErrorCode

#: 错误码 -> HTTP 状态码
ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_FAILED: 422,
    ErrorCode.CONFLICT: 409,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.SCOPE_DENIED: 403,
    ErrorCode.IDEMPOTENCY_KEY_REUSED: 409,
    ErrorCode.RUN_NOT_RESUMABLE: 409,
    ErrorCode.RUN_ALREADY_ACTIVE: 409,
    ErrorCode.LEASE_LOST: 409,
    ErrorCode.OPERATION_CONFLICT: 409,
    ErrorCode.PROFILE_NOT_REGISTERED: 409,
    ErrorCode.EXECUTOR_UNAVAILABLE: 503,
    ErrorCode.EXECUTOR_INCOMPATIBLE: 503,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.CANCELLED: 409,
    ErrorCode.PROVIDER_ERROR: 502,
    ErrorCode.CONTEXT_OVERFLOW: 422,
    ErrorCode.GRAPH_RECURSION_LIMIT: 500,
    ErrorCode.INTERNAL_ERROR: 500,
}


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    retryable: bool = False
    request_id: str | None = None
    details: dict[str, Any] = {}

    @property
    def http_status(self) -> int:
        return ERROR_HTTP_STATUS.get(self.code, 500)


class ApiError(Exception):
    """控制端内部异常。API 层统一渲染为 `ErrorBody`。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}

    def to_body(self, request_id: str | None = None) -> ErrorBody:
        return ErrorBody(
            code=self.code,
            message=self.message,
            retryable=self.retryable,
            request_id=request_id,
            details=self.details,
        )
