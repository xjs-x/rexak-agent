"""本机 Workbench API 客户端（只用只读接口 + 取消）。

适配器不直接写数据库、不自己驱动图：创建 Run、读取状态、读取最终回答、
取消运行都走现有 HTTP API，因此不会出现"第二套 Runner"。
鉴权使用 ``X-Workbench-Token``（服务端对该调用方式不要求 Cookie/CSRF）。
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import uuid4

import httpx

from agent_workbench.api.auth import ACCESS_HEADER
from agent_workbench.tsecbench.config import BenchmarkConfig

#: 读取最终回答的总长度上限（字符）。超出时截断并显式标注。
MAX_ANSWER_CHARS = 1_000_000
_MAX_ANSWER_PAGE = 65536


class WorkbenchApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details or {}


class WorkbenchUnavailable(WorkbenchApiError):
    """本机 API 没有启动或不可达。"""


class WorkbenchApi(Protocol):
    """流程层依赖的最小本机 API 面（测试用假实现即可满足）。"""

    async def create_project(
        self, *, name: str, target_description: str, scope: dict[str, Any]
    ) -> str: ...

    async def create_session_and_run(
        self,
        *,
        project_id: str,
        title: str,
        objective: str,
        scope: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def snapshot(self, session_id: str) -> dict[str, Any]: ...

    async def jobs(self, session_id: str) -> list[dict[str, Any]]: ...

    async def run_model_calls(self, session_id: str, run_id: str) -> int: ...

    async def count_operations(self, session_id: str) -> int: ...

    async def cancel_run(self, run_id: str) -> dict[str, Any]: ...

    async def read_final_answer(self, session_id: str, run_id: str) -> tuple[str, str | None]: ...



class HttpWorkbenchApi:
    """对 ``/api`` 的最小封装。"""

    def __init__(
        self,
        config: BenchmarkConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.api_base_url,
            headers={ACCESS_HEADER: config.api_token},
            timeout=config.api_timeout,
            transport=transport,
        )

    async def __aenter__(self) -> HttpWorkbenchApi:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    # 传输
    # ------------------------------------------------------------------ #

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise WorkbenchUnavailable(
                f"本机 Workbench API 不可达：{self._config.api_base_url}（{type(exc).__name__}）。"
                "请先在另一个终端启动 `uv run workbench api`，并确认端口一致。"
            ) from exc

        if response.status_code >= 400:
            payload: Any = None
            try:
                payload = response.json()
            except ValueError:
                payload = None
            code = payload.get("code") if isinstance(payload, dict) else None
            message = payload.get("message") if isinstance(payload, dict) else None
            details = payload.get("details") if isinstance(payload, dict) else None
            if response.status_code == 401:
                hint = "本机访问令牌被拒绝：检查 WORKBENCH_ACCESS_TOKEN 是否与 API 进程一致。"
            elif response.status_code == 403:
                hint = "请求被拒绝（403）：本机 API 只允许回环地址与已配置来源。"
            else:
                hint = message or response.text[:300]
            raise WorkbenchApiError(
                f"{method} {path} -> HTTP {response.status_code}: {hint}",
                status_code=response.status_code,
                code=str(code) if code else None,
                details=details if isinstance(details, dict) else None,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise WorkbenchApiError(f"{method} {path} 返回了非 JSON 响应") from exc

    # ------------------------------------------------------------------ #
    # 只读 / 写入接口
    # ------------------------------------------------------------------ #

    async def health(self) -> dict[str, Any]:
        """无需鉴权的运行指纹与数据库状态。"""
        return await self._request("GET", "/health")

    async def create_project(
        self, *, name: str, target_description: str, scope: dict[str, Any]
    ) -> str:
        payload = {
            "name": name,
            "target_description": target_description,
            "default_scope": scope,
            "settings_version": "1",
        }
        try:
            data = await self._request("POST", "/api/projects", json=payload)
        except WorkbenchApiError as exc:
            # 项目名在同一 owner 下唯一；并发/重跑撞名时换一个后缀再试一次。
            if exc.status_code != 409:
                raise
            payload["name"] = f"{name}-{uuid4().hex[:6]}"
            data = await self._request("POST", "/api/projects", json=payload)
        return str(data["id"])

    async def create_session_and_run(
        self,
        *,
        project_id: str,
        title: str,
        objective: str,
        scope: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        payload = {
            "project_id": project_id,
            "title": title,
            "initial_objective": objective,
            "scope": scope,
            "policy": policy,
            "identities": [],
            "initial_skill_refs": [],
        }
        return await self._request(
            "POST", f"/api/projects/{project_id}/sessions", json=payload
        )

    async def snapshot(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/sessions/{session_id}/snapshot")

    async def jobs(self, session_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/api/sessions/{session_id}/jobs")
        rows = data.get("jobs") if isinstance(data, dict) else None
        return list(rows or [])

    async def run_model_calls(self, session_id: str, run_id: str) -> int:
        data = await self._request(
            "GET", f"/api/sessions/{session_id}/usage", params={"run_id": run_id}
        )
        return int(data.get("call_count") or 0)

    async def count_operations(self, session_id: str, *, page_limit: int = 200) -> int:
        """工具操作总数；分页上限固定，避免异常会话拖住评测入口。"""
        total = 0
        offset = 0
        for _ in range(10):
            data = await self._request(
                "GET",
                f"/api/sessions/{session_id}/operations",
                params={"limit": page_limit, "offset": offset},
            )
            total += len(data.get("operations") or [])
            next_offset = data.get("next_offset")
            if next_offset is None:
                break
            offset = int(next_offset)
        return total

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/runs/{run_id}/cancel",
            headers={"Idempotency-Key": f"tsecbench-cancel:{uuid4()}"},
        )

    async def read_final_answer(self, session_id: str, run_id: str) -> tuple[str, str | None]:
        """读取该 run 的最终回答全文；返回 ``(text, artifact_id)``。

        只认对话归档里 ``source=result``（已提交 checkpoint 的最终回答投影），
        退一步接受 ``assistant``；不读原始工具日志。
        """
        page = await self._request(
            "GET", f"/api/sessions/{session_id}/conversation", params={"limit": 100}
        )
        entries = list(page.get("entries") or [])
        candidates = [
            entry
            for entry in entries
            if entry.get("role") == "assistant"
            and str(entry.get("run_id")) == run_id
            and bool(entry.get("available"))
        ]
        if not candidates:
            return "", None
        candidates.sort(key=lambda entry: 0 if entry.get("source") == "result" else 1)
        entry = candidates[0]
        source = str(entry.get("source") or "result")
        entry_id = str(entry["id"])

        chunks: list[str] = []
        offset = 0
        total = 0
        while total < MAX_ANSWER_CHARS:
            body = await self._request(
                "GET",
                f"/api/sessions/{session_id}/conversation/{source}/{entry_id}/text",
                params={"offset": offset, "limit": _MAX_ANSWER_PAGE},
            )
            text = str(body.get("text") or "")
            chunks.append(text)
            total += len(text)
            next_offset = body.get("next_offset")
            if next_offset is None or not text:
                break
            offset = int(next_offset)
        text = "".join(chunks)
        if len(text) >= MAX_ANSWER_CHARS:
            text = text[:MAX_ANSWER_CHARS]
        artifact_id = entry_id if source == "result" else None
        return text, artifact_id


__all__ = [
    "MAX_ANSWER_CHARS",
    "HttpWorkbenchApi",
    "WorkbenchApi",
    "WorkbenchApiError",
    "WorkbenchUnavailable",
]
