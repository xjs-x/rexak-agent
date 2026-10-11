"""Executor 客户端：控制端唯一的远端调用入口。

设计要点：
- 实现 `ExecutorClient` Protocol（BUILD PLAN 附录 A）；
- 使用**两个独立连接池**：控制面（提交/查询/取消）与数据面（输出/工件下载）分离，
  避免一个长输出下载把取消接口堵死（§8.7、ACCEPTANCE A12）；
- 连接失败 -> `ExecutorConnectionError`（基础设施故障），**不是**工具的阴性结论；
- `submit` 的传输异常**不能吞掉 operation_id**：调用方先按 ID 查询再决定后续（§8.5）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import httpx

from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.contracts.tool import (
    CancelReceipt,
    ExecutorCapabilities,
    ExecutorHealth,
    JobReceipt,
    JobSnapshot,
    OutputPage,
)
from agent_workbench.execution.tunnel import SshTunnel, TunnelUnavailable


class ExecutorConnectionError(RuntimeError):
    """基础设施故障：隧道/网络/服务不可达。**副作用不确定**。"""


class ExecutorRejected(RuntimeError):
    """执行岛**明确拒绝**了本次提交（4xx，例如参数校验失败、范围拒绝）。

    这个区分很重要：
    - 拒绝 => 操作**确定没有执行**（`execution_certainty=not_applied`）；
    - 连接失败 => 操作**可能已经执行**（`uncertain`），必须先查询再决定。

    把两者混为"失败"会导致要么误报副作用、要么漏掉真实副作用。
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class ExecutorProtocolError(RuntimeError):
    """协议不兼容或响应结构非法。控制端**不默认远端一定支持**（§8.6）。"""


@runtime_checkable
class ExecutorClient(Protocol):
    async def capabilities(self) -> ExecutorCapabilities: ...

    async def health(self) -> ExecutorHealth: ...

    async def submit(self, operation: dict[str, Any]) -> JobReceipt: ...

    async def get_job(self, job_id: UUID) -> JobSnapshot: ...

    async def read_output(
        self, job_id: UUID, cursor: str | None, *, stream: str = "stdout", limit: int = 65536
    ) -> OutputPage: ...

    async def cancel(self, job_id: UUID, request_id: UUID) -> CancelReceipt: ...

    async def read_events(self, after: int) -> dict[str, Any]: ...

    async def download_artifact(
        self, job_id: UUID, name: str, *, offset: int = 0, limit: int = 1_048_576
    ) -> tuple[bytes, dict[str, str]]: ...


class HttpExecutorClient:
    """基于 HTTPX 的实现，经 SSH 隧道访问执行岛。"""

    def __init__(
        self,
        tunnel: SshTunnel,
        *,
        token: str | None = None,
        settings: Settings | None = None,
        expected_protocol_version: int = 1,
        expected_executor_id: str | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._tunnel = tunnel
        self._token = token or self._settings.executor_token
        self._expected_protocol_version = expected_protocol_version
        self._expected_executor_id = expected_executor_id or self._settings.executor_id
        self._control: httpx.AsyncClient | None = None
        self._data: httpx.AsyncClient | None = None
        self._preflight: ExecutorCapabilities | None = None

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        base_url = await self._tunnel.ensure()
        headers = {"Authorization": f"Bearer {self._token}"}
        self._control = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            trust_env=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        # 数据面独立连接池与更长超时：大输出下载不得影响取消/状态查询
        self._data = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            trust_env=False,
            timeout=httpx.Timeout(300.0, connect=10.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def close(self) -> None:
        for client in (self._control, self._data):
            if client is not None:
                await client.aclose()
        self._control = None
        self._data = None

    @property
    def _c(self) -> httpx.AsyncClient:
        if self._control is None:
            raise ExecutorConnectionError("客户端未启动：请先 await start()")
        return self._control

    @property
    def _d(self) -> httpx.AsyncClient:
        if self._data is None:
            raise ExecutorConnectionError("客户端未启动：请先 await start()")
        return self._data

    # ---------------- 基础请求 ----------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data_client: bool = False,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        client = self._d if data_client else self._c
        try:
            response = await client.request(
                method, path, params=params, json=json_body, timeout=timeout
            )
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            # 隧道可能已断；尝试一次退避重建后重试（**不加创建新 job 的副作用**）
            try:
                await self._rebind()
            except TunnelUnavailable as tunnel_error:
                raise ExecutorConnectionError(
                    f"执行岛不可达：{type(exc).__name__}: {exc}；重建隧道失败：{tunnel_error}"
                ) from exc
            try:
                response = await (self._d if data_client else self._c).request(
                    method, path, params=params, json=json_body, timeout=timeout
                )
            except (httpx.TransportError, httpx.TimeoutException) as second:
                raise ExecutorConnectionError(
                    f"执行岛不可达（重试后仍失败）：{type(second).__name__}: {second}"
                ) from second
        return response

    async def _rebind(self) -> None:
        """隧道重建后把已有的 HTTP 客户端重新指向新的本地端口。"""
        old_control, old_data = self._control, self._data
        base_url = await self._tunnel.reconnect_with_backoff()
        headers = {"Authorization": f"Bearer {self._token}"}
        self._control = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            trust_env=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._data = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            trust_env=False,
            timeout=httpx.Timeout(300.0, connect=10.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
        for client in (old_control, old_data):
            if client is not None:
                await client.aclose()

    @staticmethod
    def _raise_for_status(response: httpx.Response, *, context: str) -> None:
        if response.status_code in {401, 403}:
            raise ExecutorConnectionError(
                f"{context}：执行岛拒绝鉴权（HTTP {response.status_code}）"
            )
        if response.status_code == 503:
            raise ExecutorConnectionError(
                f"{context}：执行岛不可用（HTTP 503）：{response.text[:200]}"
            )
        if 400 <= response.status_code < 500:
            # 明确拒绝：远端**没有**执行该操作
            raise ExecutorRejected(
                f"{context} 被拒绝（HTTP {response.status_code}）：{response.text[:400]}",
                status_code=response.status_code,
            )
        if response.status_code >= 500:
            raise ExecutorProtocolError(
                f"{context} 失败（HTTP {response.status_code}）：{response.text[:400]}"
            )

    # ---------------- 协议方法 ----------------

    async def health(self) -> ExecutorHealth:
        response = await self._request("GET", "/health")
        self._raise_for_status(response, context="健康检查")
        return ExecutorHealth.model_validate(response.json())

    async def capabilities(self) -> ExecutorCapabilities:
        """能力预检。不兼容时返回**明确阻碍**，不默认远端一定支持。"""
        if self._preflight is not None:
            return self._preflight
        response = await self._request("GET", "/capabilities")
        self._raise_for_status(response, context="能力预检")
        payload = response.json()
        caps = ExecutorCapabilities.model_validate(
            {
                "executor_id": payload["executor_id"],
                "service_version": payload["service_version"],
                "protocol_version": payload["protocol_version"],
                "boot_id": payload["boot_id"],
                "started_at": payload["started_at"],
                "browser_available": payload["browser_available"],
                "browser_version": payload.get("browser_version"),
                "http_available": payload["http_available"],
                "command_available": payload["command_available"],
                "interpreter": payload.get("interpreter"),
                "filesystem_available": payload["filesystem_available"],
                "network_isolation": payload.get("network_isolation", "unavailable"),
                "capabilities_version": payload.get("capabilities_version", "1"),
            }
        )
        if caps.protocol_version != self._expected_protocol_version:
            raise ExecutorProtocolError(
                "执行岛协议版本不兼容："
                f"期望 {self._expected_protocol_version}，实际 {caps.protocol_version}"
            )
        if self._expected_executor_id and caps.executor_id != self._expected_executor_id:
            raise ExecutorProtocolError(
                f"执行岛身份不匹配：期望 {self._expected_executor_id}，实际 {caps.executor_id}"
            )
        self._preflight = caps
        return caps

    async def probe_target(self, url: str, scope: dict[str, Any]) -> dict[str, Any]:
        """让执行岛报告目标实际解析与连接结果。"""
        response = await self._request(
            "POST", "/target-probe", json_body={"url": url, "scope": scope}
        )
        self._raise_for_status(response, context="目标连通性检查")
        payload = response.json()
        if not isinstance(payload, dict):
            raise ExecutorProtocolError("目标连通性检查返回了无效响应")
        return payload

    async def submit(self, operation: dict[str, Any]) -> JobReceipt:
        """提交任务。

        传输异常时**不吞 operation_id**：抛出的异常文本包含 operation_id，
        调用方必须先 `get_job` 查询再决定后续（§8.5）。
        """
        operation_id = operation.get("operation_id")
        response = await self._request("POST", "/jobs", json_body=operation)
        if response.status_code == 409:
            raise ExecutorProtocolError(f"执行岛报告 operation 冲突：{response.text[:300]}")
        self._raise_for_status(response, context=f"提交任务 operation_id={operation_id}")
        payload = response.json()
        return JobReceipt(
            job_id=UUID(payload["job_id"]),
            operation_id=UUID(payload["operation_id"]),
            state=payload["state"],
            accepted_at=datetime.fromisoformat(payload["accepted_at"]).astimezone(UTC),
            deduplicated=bool(payload.get("deduplicated", False)),
        )

    async def get_job(self, job_id: UUID) -> JobSnapshot:
        response = await self._request("GET", f"/jobs/{job_id}")
        self._raise_for_status(response, context=f"查询 job={job_id}")
        return JobSnapshot.model_validate(_snapshot_payload(response.json()))

    async def read_output(
        self,
        job_id: UUID,
        cursor: str | None,
        *,
        stream: str = "stdout",
        limit: int = 65536,
    ) -> OutputPage:
        # 走数据面连接池：长输出不阻塞控制面（取消/状态查询）
        response = await self._request(
            "GET",
            f"/jobs/{job_id}/output",
            data_client=True,
            params={"cursor": cursor, "stream": stream, "limit": limit},
        )
        self._raise_for_status(response, context=f"读取输出 job={job_id}")
        payload = response.json()
        return OutputPage(
            job_id=UUID(payload["job_id"]),
            cursor=payload.get("cursor"),
            next_cursor=payload.get("next_cursor"),
            stream=payload.get("stream", stream),
            text=payload.get("text", ""),
            byte_count=int(payload.get("byte_count", 0)),
            eof=bool(payload.get("eof", False)),
            truncated=bool(payload.get("truncated", False)),
        )

    async def cancel(self, job_id: UUID, request_id: UUID) -> CancelReceipt:
        response = await self._request(
            "POST", f"/jobs/{job_id}/cancel", json_body={"request_id": str(request_id)}
        )
        self._raise_for_status(response, context=f"取消 job={job_id}")
        payload = response.json()
        return CancelReceipt(
            job_id=UUID(payload["job_id"]),
            request_id=UUID(payload["request_id"]),
            state=payload["state"],
            process_confirmed_exited=bool(payload.get("process_confirmed_exited", False)),
        )

    async def read_events(self, after: int) -> dict[str, Any]:
        response = await self._request("GET", "/events", params={"after": after})
        self._raise_for_status(response, context="读取远端事件")
        return response.json()

    async def download_artifact(
        self, job_id: UUID, name: str, *, offset: int = 0, limit: int = 1_048_576
    ) -> tuple[bytes, dict[str, str]]:
        response = await self._request(
            "GET",
            f"/artifacts/{job_id}/{name}",
            data_client=True,
            params={"offset": offset, "limit": limit},
        )
        self._raise_for_status(response, context=f"下载工件 {name}")
        headers = {
            "sha256": response.headers.get("X-Artifact-Sha256", ""),
            "size": response.headers.get("X-Artifact-Size", "0"),
            "offset": response.headers.get("X-Chunk-Offset", "0"),
        }
        return response.content, headers


def _snapshot_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """把 Executor 的响应映射为控制端契约。

    `state` 值域在两侧一致（选版时已核对），这里只做字段裁剪而不是猜测。
    """
    return {
        "job_id": raw["job_id"],
        "operation_id": raw["operation_id"],
        "kind": raw.get("kind", "unknown"),
        "state": raw["state"],
        "exit_code": raw.get("exit_code"),
        "output_cursor": raw.get("output_cursor"),
        "byte_count": int(raw.get("byte_count", 0)),
        "truncated": bool(raw.get("truncated", False)),
        "started_at": raw.get("started_at"),
        "finished_at": raw.get("finished_at"),
        "last_heartbeat_at": raw.get("last_heartbeat_at"),
        "cancel_requested": bool(raw.get("cancel_requested", False)),
        "pid": raw.get("pid"),
        "pid_start_ticks": raw.get("pid_start_ticks"),
        "worker_boot_id": raw.get("worker_boot_id"),
        "error": (
            {
                "code": raw["error_code"],
                "message": raw.get("error_message") or "",
                "retryable": raw.get("error_code")
                not in {"scope_denied", "operation_conflict", "tls_certificate_error"},
            }
            if raw.get("error_code")
            else None
        ),
    }


async def wait_for_terminal(
    client: ExecutorClient,
    job_id: UUID,
    *,
    poll_interval: float = 1.0,
    timeout_seconds: float | None = None,
) -> JobSnapshot:
    """有界轮询到终态。

    **模型侧不应密集轮询**（§8.3）：本函数主要供控制面的同步等待使用
    （例如交互阈值内的短任务）。长任务应走事件唤醒。
    """
    deadline = (
        None if timeout_seconds is None else asyncio.get_running_loop().time() + timeout_seconds
    )
    terminal = {"succeeded", "failed", "cancelled", "unknown"}
    while True:
        snapshot = await client.get_job(job_id)
        if snapshot.state in terminal:
            return snapshot
        if deadline is not None and asyncio.get_running_loop().time() > deadline:
            return snapshot
        await asyncio.sleep(poll_interval)
