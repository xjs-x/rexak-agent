"""执行底座的测试替身：能力声明与假执行端。

**边界**：假执行端只用于控制端工具/流程测试（ACCEPTANCE B/F/G 的控制面部分）；
Linux 真进程/PTY 能力（C/D/E）必须由 `scripts/accept_execution_runtime.py`
在真实 Linux 上验证 —— 这里的替身**不能**作为那部分证据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from agent_workbench.contracts.tool import ExecutorCapabilities

#: 区分"未传参"与"显式传 None"（None 表示旧执行端没有执行明细）。
_UNSET: Any = object()


def fixture_capabilities(
    *,
    execution_mode: str = "trusted",
    command: str = "available",
    script: str = "available",
    pty: str = "available",
    monitor: str = "available",
    execution: Any = _UNSET,
) -> ExecutorCapabilities:
    details: dict[str, Any] | None
    if execution is _UNSET:
        details = {
            "execution_mode": execution_mode,
            "execution_runtime_versions": [1],
            "command": command,
            "script": script,
            "pty": pty,
            "screen": pty,
            "monitor": monitor,
            "durable_events": "available",
            "detached_job_recovery": "available",
            "output_limit_actions": ["preserve_process", "terminate"],
            "workspace_storage_enforcement": "accounting_only",
            "dependencies": {"pyte": "0.8.2"},
            "unavailable_reasons": {}
            if execution_mode == "trusted"
            else {"execution": "EXECUTOR_EXECUTION_MODE 未设为 trusted"},
            "notes": ["fixture"],
        }
    elif execution is None:
        details = None
    else:
        details = dict(execution)
    return ExecutorCapabilities(
        executor_id="kali-01",
        service_version="0.1.0",
        protocol_version=1,
        boot_id="fixture-boot",
        started_at=datetime(2026, 10, 15, tzinfo=UTC),
        http_available=True,
        filesystem_available=True,
        capabilities_version="2",
        execution_mode=execution_mode if details else "disabled",
        execution_runtime_versions=[1] if details else [],
        execution=details or {},
    )


@dataclass
class FakeResource:
    resource_id: str
    kind: str
    state: str = "running"
    exit_code: int | None = None
    termination_reason: str | None = None
    interaction_state: str = "not_applicable"
    mode: str = ""
    purpose: str = ""
    output: bytes = b""
    terminated: bool = False


@dataclass
class FakeExecutor:
    """内存假执行端。记录操作包，支持按脚本推进状态。"""

    capabilities_obj: ExecutorCapabilities = field(default_factory=lambda: fixture_capabilities())
    resources: dict[str, FakeResource] = field(default_factory=dict)
    operations: dict[str, dict[str, Any]] = field(default_factory=dict)
    envelopes: list[dict[str, Any]] = field(default_factory=list)
    control_syncs: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    fence_seq: int = 0
    fail_next_submit: Exception | None = None
    reject_next_submit: str | None = None

    async def capabilities(self) -> ExecutorCapabilities:
        return self.capabilities_obj

    async def health(self) -> Any:  # pragma: no cover - 工具路径不使用
        raise NotImplementedError

    async def submit(self, operation: dict[str, Any]) -> Any:  # pragma: no cover
        raise NotImplementedError("旧 /jobs 接口：本替身只支持 /execution/v1")

    async def get_job(self, job_id: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def read_output(
        self, job_id: Any, cursor: str | None, *, stream: str = "stdout", limit: int = 65536
    ) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def cancel(self, job_id: Any, request_id: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    async def read_events(self, after: int) -> dict[str, Any]:  # pragma: no cover
        return {"events": [], "next_after": after}

    async def download_artifact(
        self, job_id: Any, name: str, *, offset: int = 0, limit: int = 1_048_576
    ) -> tuple[bytes, dict[str, str]]:  # pragma: no cover
        raise NotImplementedError

    # ---------------- 执行子协议 ----------------

    async def execution_sync_control(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.control_syncs.append(payload)
        self.fence_seq += 1
        return {
            "applied": True,
            "reason": "",
            "fence": {
                "session_id": payload["session_id"],
                "seq": self.fence_seq,
                "run_id": payload["run_id"],
                "owner": payload.get("owner", ""),
                "owner_generation": payload.get("owner_generation", 0),
                "control_version": payload.get("control_version", 0),
                "revocations": payload.get("revocations", []),
                "updated_at": datetime.now(UTC).isoformat(),
            },
        }

    async def execution_submit(self, envelope: dict[str, Any]) -> dict[str, Any]:
        from agent_workbench.execution.client import ExecutorRejected

        if self.reject_next_submit is not None:
            message = self.reject_next_submit
            self.reject_next_submit = None
            raise ExecutorRejected(message, status_code=409)
        self.envelopes.append(envelope)
        operation_id = envelope["operation_id"]
        existing = self.operations.get(operation_id)
        if existing is not None:
            existing["deduplicated"] = True
            return existing
        action = envelope["action"]
        resource_id = str(uuid4())
        kind = {
            "run_command": "command",
            "run_script": "script",
            "start_monitor": "monitor",
            "terminal_open": "terminal",
            "terminal_send": "terminal",
            "terminal_resize": "terminal",
        }.get(action, "command")
        resource = FakeResource(
            resource_id=resource_id,
            kind=kind,
            purpose=str(envelope.get("arguments", {}).get("description") or ""),
            interaction_state="available" if kind == "terminal" else "not_applicable",
        )
        if action in {"cancel_resource", "terminal_close"}:
            target = str(
                envelope.get("arguments", {}).get("resource_id")
                or envelope.get("arguments", {}).get("terminal_id")
            )
            resource.resource_id = target
            resource_id = target
            resource.terminated = True
            resource.state = "closed" if action == "terminal_close" else "exited"
            resource.termination_reason = "user_cancelled"
            if target in self.resources:
                self.resources[target].state = resource.state
                self.resources[target].termination_reason = "user_cancelled"
                self.resources[target].terminated = True
        else:
            self.resources[resource_id] = resource
        receipt = {
            "schema_version": 1,
            "operation_id": operation_id,
            "action": action,
            "status": "accepted",
            "resource_id": resource_id,
            "job_id": resource_id if kind != "terminal" else None,
            "terminal_id": resource_id if kind == "terminal" else None,
            "process_state": resource.state,
            "process_confirmed_exited": resource.terminated,
            "interaction_state": resource.interaction_state
            if kind == "terminal"
            else "not_applicable",
            "wait_reason": "wait_timeout" if envelope.get("wait_ms") else None,
            "exit_code": None,
            "termination_reason": resource.termination_reason,
            "deduplicated": False,
            "accepted_at": datetime.now(UTC).isoformat(),
            "note": "fixture 受理",
        }
        self.operations[operation_id] = receipt
        return receipt

    async def execution_raw_output(
        self, resource_id: str, *, session_id: str, stream: str = "stdout",
        offset: int = 0, limit: int = 262144,
    ) -> dict[str, Any]:
        import base64
        import hashlib

        data = self.resources[resource_id].output[offset:offset+limit]
        return {"resource_id": resource_id, "stream": stream, "stored_offset": offset,
                "next_offset": offset+len(data), "observed_size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "data_base64": base64.b64encode(data).decode(),
                "captured_at": datetime.now(UTC).isoformat(), "at_current_end": True}

    async def execution_operation(self, operation_id: str) -> dict[str, Any]:
        return self.operations.get(operation_id, {"operation_id": operation_id})

    async def execution_resources(
        self, *, session_id: str, limit: int = 50, offset: int = 0, kind: str | None = None
    ) -> dict[str, Any]:
        items = [
            self._view(item)
            for item in self.resources.values()
            if kind in (None, item.kind)
        ]
        return {
            "executor_id": "kali-01",
            "session_id": session_id,
            "resources": items[offset : offset + limit],
            "next_cursor": None,
            "total": len(items),
            "session_quota": {"active": len(items), "active_pty": 0, "max_resources": 16},
        }

    async def execution_resource(self, resource_id: str) -> dict[str, Any]:
        item = self.resources.get(resource_id)
        if item is None:
            from agent_workbench.execution.client import ExecutorRejected

            raise ExecutorRejected("资源不存在", status_code=404)
        return self._view(item)

    async def execution_output(
        self,
        resource_id: str,
        *,
        stream: str = "stdout",
        cursor: str | None = None,
        limit: int = 16384,
        view: str = "stream",
        max_lines: int | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        item = self.resources.get(resource_id)
        data = item.output if item is not None else b""
        offset = 0
        if cursor:
            import base64

            padded = cursor + "=" * (-len(cursor) % 4)
            parts = base64.urlsafe_b64decode(padded.encode()).decode().split("|")
            offset = int(parts[-1]) if len(parts) == 4 else 0
        chunk = data[offset : offset + limit]
        return {
            "resource_id": resource_id,
            "stream": stream,
            "cursor": cursor,
            "next_cursor": f"fixture|{resource_id}|stdout|{offset + len(chunk)}",
            "byte_start": offset,
            "byte_end": offset + len(chunk),
            "text": chunk.decode("utf-8", errors="replace"),
            "byte_count": len(chunk),
            "at_current_end": offset + len(chunk) >= len(data),
            "process_exited": item.state in {"exited", "closed"} if item else False,
            "truncated": False,
            "missing_ranges": [],
            "encoding_error": None,
            "artifact_refs": [],
            "note": "",
        }

    async def execution_events(self, *, after: int, limit: int = 200) -> dict[str, Any]:
        items = [event for event in self.events if event["seq"] > after][:limit]
        return {
            "executor_id": "kali-01",
            "events": items,
            "next_after": items[-1]["seq"] if items else after,
            "has_more": False,
        }

    async def execution_environment(self) -> dict[str, Any]:
        return {
            "executor_id": "kali-01",
            "platform": "linux",
            "os_release": "fixture",
            "python_version": "3.13.0",
            "shell": {"configured": "/bin/bash", "mode": "non-login (-c)"},
            "interpreters": {"python": "/usr/bin/python3", "shell": "/bin/bash"},
            "tools": {"bash": "/usr/bin/bash"},
            "missing": {},
            "proxy_configured": False,
            "ca_configured": False,
            "workspace_root": "/tmp/fixture-workspaces",
            "workspace_storage_enforcement": "accounting_only",
            "workspace_bytes_used": 0,
            "dependency_environment_hint": "在会话工作区内创建 venv",
            "notes": [],
        }

    @staticmethod
    def _view(item: FakeResource) -> dict[str, Any]:
        return {
            "resource_id": item.resource_id,
            "session_id": "",
            "run_id": "",
            "kind": item.kind,
            "state": item.state,
            "interaction_state": item.interaction_state,
            "mode": item.mode,
            "purpose": item.purpose,
            "created_at": datetime.now(UTC).isoformat(),
            "started_at": None,
            "exited_at": None,
            "closed_at": None,
            "exit_code": item.exit_code,
            "termination_reason": item.termination_reason,
            "deadline_at": None,
            "output_limit_action": "preserve_process",
            "output_bytes": len(item.output),
            "dropped_bytes": 0,
            "output_limit_bytes": 64 * 1024 * 1024,
            "cancel_requested": item.terminated,
            "cancel_confirmed": item.terminated,
            "last_checked_at": datetime.now(UTC).isoformat(),
            "watch_hits": 0,
            "notes": [],
        }
