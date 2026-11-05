"""Fake Executor for Pi: real isolated files, simulated process ledger; never uses SSH or targets."""

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from workbench_executor.worker_files import perform

from agent_workbench.contracts.enums import JobState
from agent_workbench.contracts.tool import CancelReceipt
from agent_workbench.execution.client import ExecutorConnectionError, ExecutorRejected
from tests.support.executor_fixtures import FakeExecutor


class PiFakeExecutor(FakeExecutor):
    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.a2a_records: dict[UUID, dict[str, Any]] = {}
        self.capabilities_obj.execution["worker_tools"] = "available"
        self.lost_response = False
        self.hang = False
        self.active = 0
        self.peak = 0
        self.fences = {}
        self.barrier_count = 0
        self.barrier = asyncio.Event()

    async def execution_environment(self):
        return {"workspace_root": "/kali/workspaces"}

    async def execution_sync_control(self, payload):
        old = self.fences.get(payload["session_id"], {})
        revoked = [*old.get("revocations", []), *payload.get("revocations", [])]
        result = await super().execution_sync_control({**payload, "revocations": revoked})
        self.fences[payload["session_id"]] = result["fence"]
        return result

    async def execution_submit(self, envelope):
        fence = self.fences[envelope["session_id"]]
        if (envelope["run_id"] != fence["run_id"] or envelope["owner_generation"] != fence["owner_generation"]
                or envelope["dispatch_control_version"] != fence["control_version"]
                or any(r["delegation_id"] == envelope.get("delegation_id") for r in fence["revocations"])):
            raise ExecutorRejected("stale_control", status_code=409)
        existing = self.operations.get(envelope["operation_id"])
        if existing:
            return existing
        receipt = await super().execution_submit(envelope)
        if envelope["action"] == "cancel_resource":
            resource = self.resources[envelope["arguments"]["resource_id"]]
            resource.state, resource.exit_code, resource.termination_reason = "exited", 143, "user_cancelled"
            return {**receipt, "process_confirmed_exited": True}
        resource = self.resources[receipt["resource_id"]]
        args = envelope["arguments"]
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.barrier_count:
            if self.active >= self.barrier_count:
                self.barrier.set()
            await asyncio.wait_for(self.barrier.wait(), 5)
        else:
            await asyncio.sleep(0.03)
        self.active -= 1
        if not self.hang:
            result = {"ok": True}
            root = self.root / envelope["session_id"] / str(args.get("worker_workspace_id") or "main")
            root.mkdir(parents=True, exist_ok=True)
            if args.get("worker_file"):
                data = args["worker_file"]
                try:
                    if data["action"] == "glob":
                        value = {"lines": [p.relative_to(root).as_posix() for p in root.glob(data["pattern"]) if p.is_file()], "truncated": False}
                    elif data["action"] == "grep":
                        lines = []
                        for path in root.glob("**/*"):
                            if path.is_file() and not path.name.startswith("."):
                                for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                                    if data["pattern"] in line:
                                        lines.append(f"{path.relative_to(root).as_posix()}:{i}:{line}")
                        value = {"lines": lines, "truncated": False}
                    else:
                        value = await asyncio.to_thread(perform, data, root)
                    result = {"ok": True, "data": value}
                except Exception as exc:
                    result = {"ok": False, "error": getattr(exc, "code", type(exc).__name__)}
                resource.output = json.dumps(result).encode()
            else:
                resource.output = b"worker-ok\n"
            exit_code = 1 if args.get("worker_file") and not result["ok"] else 0
            resource.state, resource.exit_code, resource.termination_reason = "exited", exit_code, "natural_exit"
            receipt.update(process_state="exited", exit_code=exit_code, termination_reason="natural_exit")
        if self.lost_response:
            self.lost_response = False
            raise ExecutorConnectionError("injected response lost after acceptance")
        return receipt

    async def execution_raw_output(self, resource_id, *, session_id, stream="stdout", offset=0, limit=262144):
        raw = self.resources[resource_id].output if stream == "stdout" else b""
        data = raw[offset:offset+limit]
        return {"resource_id": resource_id, "stream": stream, "stored_offset": offset, "next_offset": offset+len(data),
                "source_bytes": len(raw), "missing_ranges": [], "collection_metadata_known": True,
                "observed_size": len(raw), "sha256": hashlib.sha256(data).hexdigest(), "data_base64": base64.b64encode(data).decode(), "at_current_end": offset+len(data) >= len(raw)}

    async def cancel(self, job_id, request_id):
        resource = self.resources[str(job_id)]
        resource.state, resource.exit_code, resource.termination_reason = "exited", 143, "user_cancelled"
        return CancelReceipt(job_id=UUID(str(job_id)), request_id=UUID(str(request_id)), state=JobState.CANCELLED, process_confirmed_exited=True)
