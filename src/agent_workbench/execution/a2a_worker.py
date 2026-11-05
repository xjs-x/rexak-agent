"""受控回环 Worker；所有 A2A 调用使用官方 1.0 SDK，额外 HTTP 仅用于去重对账。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from a2a.client import ClientConfig, create_client
from a2a.client.client import Client
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
    TaskState,
)
from a2a.utils.errors import TaskNotCancelableError
from google.protobuf.json_format import ParseDict

from agent_workbench.config.settings import Settings


class WorkerUnavailable(RuntimeError):
    pass


class A2AWorkerClient:
    def __init__(self, settings: Settings) -> None:
        from agent_workbench.config.ultra_worker import read_worker_config

        url = urlsplit(settings.ultra_worker_url)
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}):
            raise WorkerUnavailable("Ultra 第一阶段仅允许配置受控回环 HTTP 地址")
        token = settings.ultra_worker_token or read_worker_config(settings).get("token", "")
        if len(token) < 24:
            raise WorkerUnavailable("Ultra Worker 未配置有效服务令牌（至少 24 字符）")
        self.base_url = settings.ultra_worker_url.rstrip("/")
        self.http = httpx.AsyncClient(
            base_url=self.base_url, timeout=settings.ultra_request_seconds,
            headers={"Authorization": f"Bearer {token}", "A2A-Version": "1.0"},
            follow_redirects=False, trust_env=False,
        )
        self.client: Client | None = None

    async def __aenter__(self) -> A2AWorkerClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        if self.client is not None:
            await self.client.close()
        await self.http.aclose()

    async def connect(self) -> Client:
        if self.client is None:
            response = await self.http.get("/.well-known/agent-card.json")
            response.raise_for_status()
            card = ParseDict(response.json(), AgentCard())
            if (len(card.supported_interfaces) != 1
                    or card.supported_interfaces[0].url != f"{self.base_url}/a2a"
                    or card.supported_interfaces[0].protocol_version != "1.0"
                    or card.supported_interfaces[0].protocol_binding != "JSONRPC"):
                raise WorkerUnavailable("Worker Card 不匹配固定 A2A 1.0 JSONRPC 地址")
            self.client = await create_client(card, ClientConfig(
                streaming=False, polling=True, httpx_client=self.http,
                supported_protocol_bindings=["JSONRPC"], accepted_output_modes=["application/json"],
            ))
        return self.client

    async def submit(self, request_id: UUID, payload: dict[str, Any]) -> Task:
        client = await self.connect()
        part = Part(media_type="application/json")
        ParseDict(payload, part.data)
        request = SendMessageRequest(
            message=Message(message_id=str(request_id), role=Role.ROLE_USER, parts=[part]),
            configuration=SendMessageConfiguration(return_immediately=True, history_length=0),
        )
        async for event in client.send_message(request):
            if event.HasField("task"):
                return event.task
        raise WorkerUnavailable("A2A 提交响应没有 Task；必须按请求 ID 对账")

    async def lookup(self, request_id: UUID) -> dict[str, Any] | None:
        response = await self.http.get(f"/requests/{request_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def get(self, task_id: str) -> Task:
        return await (await self.connect()).get_task(GetTaskRequest(id=task_id, history_length=0))

    async def message_status(self, request_id: UUID, message_id: UUID) -> dict[str, Any] | None:
        response = await self.http.get(f"/requests/{request_id}/messages/{message_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def send_context(self, request_id: UUID, message_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.http.post(f"/requests/{request_id}/messages/{message_id}", json=payload)
        response.raise_for_status()
        return response.json()

    async def cancel(self, request_id: UUID, task_id: str | None) -> Task | None:
        # Durable tombstone before A2A cancellation blocks late arriving SendMessage.
        response = await self.http.post(f"/requests/{request_id}/cancel")
        response.raise_for_status()
        if task_id is None:
            found = await self.lookup(request_id)
            if not found or not found.get("task"):
                return None
            task_id = str(found["task_id"])
        client = await self.connect()
        current = await client.get_task(GetTaskRequest(id=task_id, history_length=0))
        if current.status.state in {
            TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_CANCELED, TaskState.TASK_STATE_REJECTED,
        }:
            return current
        try:
            return await client.cancel_task(CancelTaskRequest(id=task_id))
        except TaskNotCancelableError:
            # Completion may win after the GET; reconcile the actual terminal state.
            return await client.get_task(GetTaskRequest(id=task_id, history_length=0))
