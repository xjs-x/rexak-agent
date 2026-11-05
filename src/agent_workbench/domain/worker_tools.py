"""Pi Operations → existing OperationRegistry / execution kernel / Executor. No local execution."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from time import perf_counter
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import select

from agent_workbench.config.settings import Settings
from agent_workbench.config.ultra_worker import read_worker_config
from agent_workbench.contracts.enums import ErrorCode, ToolEffectClass
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.identity import ScopeSnapshot
from agent_workbench.contracts.worker_tools import WorkerToolRequest
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.domain.worker_evidence import guard
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.execution.a2a_worker import A2AWorkerClient
from agent_workbench.execution.client import (
    ExecutorClient,
    ExecutorConnectionError,
    ExecutorProtocolError,
    ExecutorRejected,
    HttpExecutorClient,
)
from agent_workbench.execution.tunnel import SshTunnel
from agent_workbench.observability.spans import SpanRecorder
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import ArtifactRepository, ToolOperationRepository
from agent_workbench.persistence.tables import (
    AgentDelegationRow,
    ArtifactRow,
    RunRow,
    SessionRow,
    ToolOperationRow,
)
from agent_workbench.tools.commands import run_execution_kernel
from agent_workbench.tools.execution_context import execution_context
from agent_workbench.tools.execution_dispatch import require_capability
from agent_workbench.tools.operation import OperationRegistry
from agent_workbench.tools.runtime import ResourceLocks, Toolbox

ALLOWED = {"read": {"access", "read"}, "write": {"mkdir", "write"}, "edit": {"access", "read", "write"},
           "find": {"exists", "glob"}, "grep": {"grep"}, "bash": {"bash"}}
TERMINAL = {"exited", "closed", "cancelled"}
logger = logging.getLogger(__name__)


def tool_signature(secret: str, delegation_id: UUID, value: WorkerToolRequest) -> str:
    return hmac.new(secret.encode(), f"tools:v1:{delegation_id}:{value.task_id}:{value.worker_id}:{value.context_id}".encode(), hashlib.sha256).hexdigest()


@asynccontextmanager
async def open_executor(settings: Settings):
    tunnel = SshTunnel(settings)
    client = HttpExecutorClient(tunnel, settings=settings, expected_executor_id=settings.executor_id)
    started = perf_counter()
    try:
        await client.start()
        logger.info("Worker Executor channel opened in %.3fs", perf_counter() - started)
        yield client
    finally:
        await client.close()
        await tunnel.close()


def namespace(delegation_id: UUID, tool_call_id: str) -> str:
    return f"pi:{delegation_id.hex}:{uuid5(delegation_id, tool_call_id).hex}"


class WorkerToolService:
    def __init__(self, db: Database, settings: Settings, executor: ExecutorClient | None = None):
        self.db, self.settings, self.executor = db, settings, executor

    async def handle(self, delegation_id: UUID, value: WorkerToolRequest, bearer: str) -> dict[str, Any]:
        secret = self.settings.ultra_worker_token or read_worker_config(self.settings).get("token", "")
        if len(secret) < 24 or not hmac.compare_digest(bearer, tool_signature(secret, delegation_id, value)):
            raise ApiError(ErrorCode.UNAUTHENTICATED, "Worker 工具凭据无效")
        async with A2AWorkerClient(self.settings) as a2a:
            remote = await a2a.lookup(delegation_id)
        if (not remote or remote.get("task_id") != value.task_id or remote.get("worker_id") != str(value.worker_id)
                or (remote.get("task") or {}).get("contextId") != str(value.context_id)):
            raise ApiError(ErrorCode.CONFLICT, "Worker 持久身份不匹配")
        if value.phase != "settle" and (remote.get("phase") != "running" or remote.get("cancel_requested")):
            raise ApiError(ErrorCode.CONFLICT, "Worker 已停止或取消")
        async with self.db.session() as db:
            row = await db.get(AgentDelegationRow, delegation_id)
            run = await db.get(RunRow, row.parent_run_id) if row else None
            parent = await db.get(SessionRow, row.parent_session_id) if row else None
            if (not row or not parent or parent.owner_id != self.settings.owner_id or parent.status == "deleted"
                    or row.context_id != value.context_id or not row.authorization_snapshot.get("executor_access")
                    or row.handoff_snapshot.get("execution_access") != "pi_tools_v1"):
                raise ApiError(ErrorCode.FORBIDDEN, "委派没有执行授权")
            if (row.a2a_task_id and row.a2a_task_id != value.task_id) or (row.worker_id and row.worker_id != str(value.worker_id)):
                raise ApiError(ErrorCode.CONFLICT, "Worker 身份映射冲突")
            if value.phase != "settle":
                guard(parent, run, row, self.settings)
                if value.tool not in row.authorization_snapshot.get("tool_allowlist", []):
                    raise ApiError(ErrorCode.FORBIDDEN, "工具不在委派授权中")
        assert row and run and parent
        if row.a2a_task_id is None or row.worker_id is None:
            async with self.db.transaction() as db:
                binding = await db.get(AgentDelegationRow, row.id, with_for_update=True)
                assert binding
                if (binding.a2a_task_id and binding.a2a_task_id != value.task_id) or (binding.worker_id and binding.worker_id != str(value.worker_id)):
                    raise ApiError(ErrorCode.CONFLICT, "Worker 身份映射冲突")
                binding.a2a_task_id, binding.worker_id = value.task_id, str(value.worker_id)
            row.a2a_task_id, row.worker_id = value.task_id, str(value.worker_id)
        if value.phase == "settle" and value.arguments.get("cancel"):
            async with self.db.transaction() as db:
                current = await db.get(AgentDelegationRow, row.id, with_for_update=True)
                assert current
                # Failure cleanup revokes execution but is not a user cancellation.
                if value.arguments.get("reason") != "failure":
                    current.cancel_requested = True
                current.authorization_snapshot = {**current.authorization_snapshot, "execution_revoked": True}
        if self.executor is None:
            async with open_executor(self.settings) as client:
                return await self._handle(client, row, run, parent, value)
        return await self._handle(self.executor, row, run, parent, value)

    async def _handle(self, client: ExecutorClient, row: AgentDelegationRow, run: RunRow, parent: SessionRow, value: WorkerToolRequest) -> dict[str, Any]:
        async def control_check(*_args):
            async with self.db.session() as db:
                current = await db.get(AgentDelegationRow, row.id)
                current_run = await db.get(RunRow, run.id)
                current_parent = await db.get(SessionRow, parent.id)
                guard(current_parent, current_run, current, self.settings)

        box = Toolbox(run_id=run.id, session_id=parent.id, owner=run.owner or "worker-cleanup",
            owner_generation=run.owner_generation, settings=self.settings, db=self.db,
            scope=ScopePolicy(ScopeSnapshot.model_validate(row.authorization_snapshot.get("scope") or {})),
            executor=client, artifacts=ArtifactStore(self.settings), spans=SpanRecorder(), locks=ResourceLocks(self.settings),
            control_version=int(row.authorization_snapshot.get("control_version", 0)), control_check=control_check,
            execution_delegation_id=str(row.id), execution_delegation_revision=1,
            identity_id=UUID(row.authorization_snapshot["identity_id"]) if row.authorization_snapshot.get("identity_id") else None)
        root_key = namespace(row.id, value.tool_call_id)
        if value.phase == "settle":
            return {"settled": await settle_worker(self.db, self.settings, row, client, cancel=bool(value.arguments.get("cancel")))}
        workspace = ""
        if value.phase in {"begin", "step"}:
            caps = await client.capabilities()
            require_capability(caps, "worker_tools")
            env = await client.execution_environment()
            workspace = str(PurePosixPath(env["workspace_root"]) / str(parent.id) / ".rexak" / "workers" / str(row.id))
            if not workspace.startswith("/") or "\\" in workspace:
                raise ApiError(ErrorCode.EXECUTOR_INCOMPATIBLE, "Executor 未返回实际 POSIX 工作区")
        if value.phase == "begin":
            await control_check()
            async with self.db.transaction() as db:
                # Same Run lock as Main registration prevents concurrent duplicate first intents.
                await db.scalar(select(RunRow).where(RunRow.id == run.id).with_for_update())
                prepared = await OperationRegistry(db).prepare(run_id=run.id, session_id=parent.id,
                    tool_call_id=root_key, tool_name=f"pi.{value.tool}", arguments=value.arguments,
                    effect_class=ToolEffectClass.SIDE_EFFECTING, policy_revision=1, scope_revision=box.scope.revision,
                    dispatch_control_version=box.control_version, identity_id=box.identity_id,
                    context_extra={"agent_delegation_id": str(row.id), "worker_id": value.worker_id.hex,
                        "worker_tool_aggregate": True, "context_id": str(row.context_id)})
                if prepared.replayed:
                    stored = (prepared.row.result_snapshot or {}).get("worker_result_artifact_id")
                    if stored:
                        artifact = await db.get(ArtifactRow, UUID(stored))
                        if not artifact or not box.artifacts.verify(artifact.relative_key, artifact.sha256):
                            return {"uncertain": True, "operation_id": str(prepared.operation_id)}
                        return {"cached": json.loads(box.artifacts.read_bytes(artifact.relative_key)), "workspace": workspace}
                    return {"uncertain": True, "operation_id": str(prepared.operation_id)}
                prepared.row.status = "running"
            return {"workspace": workspace, "operation_id": str(prepared.operation_id)}
        async with self.db.session() as db:
            aggregate = await ToolOperationRepository(db).find_by_tool_call(run.id, root_key)
            if not aggregate or aggregate.tool_name != f"pi.{value.tool}":
                raise ApiError(ErrorCode.CONFLICT, "没有对应的 Worker 工具意图")
        if value.phase == "finish":
            result = value.result or {}
            encoded = json.dumps(result, ensure_ascii=False).encode()
            if len(encoded) > 524288:
                raise ApiError(ErrorCode.VALIDATION_FAILED, "Worker 格式化结果过大")
            stored = box.artifacts.write_bytes(encoded, media_type="application/json")
            async with self.db.transaction() as db:
                current = await db.get(ToolOperationRow, aggregate.operation_id, with_for_update=True)
                assert current
                result_id = uuid5(aggregate.operation_id, "worker-tool-result")
                existing_result = await db.get(ArtifactRow, result_id)
                if existing_result:
                    if existing_result.sha256 != stored.sha256:
                        raise ApiError(ErrorCode.OPERATION_CONFLICT, "同一工具结果不可覆盖")
                    return {"saved": True}
                artifact = await ArtifactRepository(db).create(artifact_id=result_id,
                    sha256=stored.sha256, byte_size=stored.byte_size, media_type=stored.media_type, relative_key=stored.relative_key,
                    session_id=parent.id, source_operation_id=aggregate.operation_id, sensitivity="internal", availability="available",
                    captured_at=stored.captured_at, captured_by_identity=f"pi-worker:{row.worker_id}", summary=f"Pi {value.tool} 结果")
                current.result_snapshot = {"worker_result_artifact_id": str(artifact.id)}
                current.result_artifact_ids = [str(artifact.id)]
                current.status = "failed" if result.get("isError") else "succeeded"
            return {"saved": True}
        if value.phase == "poll":
            async with self.db.session() as db:
                operation = await db.get(ToolOperationRow, value.operation_id)
                if (not operation or operation.run_id != run.id or
                        (operation.execution_context or {}).get("parent_worker_operation") != str(aggregate.operation_id)):
                    raise ApiError(ErrorCode.NOT_FOUND, "操作不属于该 Worker ToolCall")
            return await self._observe(client, box, operation.operation_id, operation.job_id, value.tool != "bash")
        args = dict(value.arguments)
        action = str(args.pop("action", ""))
        if action not in ALLOWED[value.tool]:
            raise ApiError(ErrorCode.FORBIDDEN, "Pi Operations 动作与工具不匹配")
        file_action = action != "bash"
        if file_action:
            path = str(args.get("path") or ".")
            if path.startswith("/"):
                try:
                    path = str(PurePosixPath(path).relative_to(workspace))
                except ValueError:
                    raise ApiError(ErrorCode.FORBIDDEN, "文件路径不属于当前 Worker 工作区") from None
            if "\\" in path or ":" in path or ".." in PurePosixPath(path).parts:
                raise ApiError(ErrorCode.FORBIDDEN, "仅允许 Kali Worker 工作区内的路径")
            args["path"] = path
            command_args = {"argv": ["true"], "worker_file": {"action": action, **args}, "execution_timeout_seconds": 30}
        else:
            command_args = {"command": str(args["command"]), "execution_timeout_seconds": args.get("timeout")}
        command_args.update(workspace_id=str(parent.id), worker_workspace_id=str(row.id), cwd=".",
            description=f"Pi Worker {row.id} {value.tool}", completion_role="work")
        key = root_key + f":{value.step}"
        from agent_workbench.contracts.execution_runtime import validate_arguments

        try:
            validate_arguments("run_command", command_args)
        except ValueError as exc:
            raise ApiError(ErrorCode.VALIDATION_FAILED, "Worker Operations 参数无效") from exc
        async with self.db.transaction() as db:
            await db.scalar(select(RunRow).where(RunRow.id == run.id).with_for_update())
            leaf = await OperationRegistry(db).prepare(run_id=run.id, session_id=parent.id, tool_call_id=key,
                tool_name=f"pi.{value.tool}.{action}", arguments={"action": "run_command", **command_args},
                effect_class=ToolEffectClass.SIDE_EFFECTING, policy_revision=1, scope_revision=box.scope.revision,
                dispatch_control_version=box.control_version, identity_id=box.identity_id,
                context_extra={"agent_delegation_id": str(row.id), "worker_id": str(value.worker_id),
                    "context_id": str(row.context_id), "parent_worker_operation": str(aggregate.operation_id),
                    "submission_state": "not_submitted"})
            if not leaf.replayed:
                leaf.row.status = "running"
        if leaf.replayed:
            if leaf.row.status == "failed" and leaf.row.job_id is None and (leaf.row.result_snapshot or {}).get("execution_certainty") == "not_applied":
                return {"error": "worker_operation_not_applied", "operation_id": str(leaf.operation_id), "not_applied": True}
            # Never resubmit a potentially accepted effect. Lookup only, even after 404/timeout.
            return await self._observe(client, box, leaf.operation_id, leaf.row.job_id, file_action)
        token = execution_context.set({"agent_delegation_id": str(row.id), "worker_id": str(value.worker_id),
            "context_id": str(row.context_id), "parent_worker_operation": str(aggregate.operation_id),
                    "submission_state": "not_submitted"})
        try:
            result = await run_execution_kernel(box, tool_call_id=key, tool_name=f"pi.{value.tool}.{action}",
                capability="worker_tools", action="run_command", arguments=command_args, wait_ms=1000)
        finally:
            execution_context.reset(token)
        if result.status == "failed" and result.job_id is None:
            # Capabilities/control may fail before the shared kernel resolves its ID.
            # The pre-registered leaf is still the authoritative local operation.
            async with self.db.transaction() as db:
                await ToolOperationRepository(db).mark_result(leaf.operation_id, status="failed")
                current = await db.get(ToolOperationRow, leaf.operation_id)
                assert current
                current.result_snapshot = {"execution_certainty": "not_applied"}
            return {"error": result.summary, "operation_id": str(leaf.operation_id), "not_applied": True}
        return await self._observe(client, box, result.operation_id, result.job_id, file_action)

    async def _observe(self, client: ExecutorClient, box: Toolbox, operation_id: UUID, job_id: UUID | None, file_action: bool):
        try:
            if job_id is None:
                remote = await client.execution_operation(str(operation_id))
                job_id = UUID(remote["resource_id"]) if remote.get("resource_id") else None
            if not job_id:
                return {"uncertain": True, "operation_id": str(operation_id)}
            state = await client.execution_resource(str(job_id))
            if state.get("state") == "unknown":
                return {"uncertain": True, "operation_id": str(operation_id), "resource_id": str(job_id)}
            if state.get("state") not in TERMINAL:
                return {"pending": True, "operation_id": str(operation_id), "resource_id": str(job_id)}
            if state.get("exit_code") is None and state.get("termination_reason") in {None, "unknown"}:
                return {"uncertain": True, "operation_id": str(operation_id), "resource_id": str(job_id)}
        except (ExecutorConnectionError, ExecutorProtocolError, ExecutorRejected, ValueError, KeyError):
            return {"uncertain": True, "operation_id": str(operation_id)}
        # Process certainty has been established independently of log retrieval/decoding.
        from agent_workbench.persistence.execution_projection import execution_operation_status

        status = execution_operation_status(str(state.get("state")), state.get("exit_code"), state.get("termination_reason"))
        from agent_workbench.tools.execution_dispatch import project_resource

        await project_resource(box, str(job_id), {"operation_id": str(operation_id), "resource_id": str(job_id), "action": "run_command", "resource": state,
            "process_state": state.get("state"), "exit_code": state.get("exit_code"),
            "termination_reason": state.get("termination_reason"), "completion_role": "work"})
        async with self.db.transaction() as db:
            current = await db.get(ToolOperationRow, operation_id, with_for_update=True)
            assert current
            current.job_id, current.status = job_id, status
            from agent_workbench.persistence.repositories import ToolJobRepository

            await ToolJobRepository(db).upsert(job_id=job_id, operation_id=operation_id,
                executor_id=box.settings.executor_id, state="exited", sync_status="in_sync")
        try:
            chunks, refs, texts = [], [], []
            for stream in ("stdout", "stderr"):
                ceiling = 6 * 1024 * 1024 if file_action else 65536
                page = await client.execution_raw_output(str(job_id), session_id=str(box.session_id), stream=stream, offset=0, limit=ceiling if ceiling < 262144 else 262144)
                # Bash exposes the tail of large logs; the reference states the exact range.
                start = max(0, int(page["observed_size"]) - ceiling) if not file_action else 0
                if start:
                    page = await client.execution_raw_output(str(job_id), session_id=str(box.session_id), stream=stream, offset=start, limit=ceiling)
                raw = bytearray()
                offset = start
                while True:
                    data = base64.b64decode(page["data_base64"], validate=True)
                    if hashlib.sha256(data).hexdigest() != page["sha256"] or int(page["stored_offset"]) != offset or int(page["next_offset"]) != offset + len(data):
                        raise ExecutorProtocolError("输出范围或哈希不匹配")
                    raw.extend(data)
                    offset = int(page["next_offset"])
                    if page.get("at_current_end") or not data or len(raw) >= ceiling:
                        break
                    page = await client.execution_raw_output(str(job_id), session_id=str(box.session_id), stream=stream, offset=offset, limit=min(262144, ceiling-len(raw)))
                captured = bytes(raw)
                observed = int(page["observed_size"])
                incomplete = start > 0 or offset < observed or bool(page.get("missing_ranges")) or bool(page.get("missing_ranges_truncated")) or not page.get("collection_metadata_known", False)
                decode_status, text = "utf8", ""
                try:
                    text = captured.decode("utf-8")
                except UnicodeDecodeError:
                    # Only trim a known page boundary; invalid interior bytes remain raw.
                    left = 0
                    while start > 0 and left < min(3, len(captured)) and captured[left] & 0xC0 == 0x80:
                        left += 1
                    right = len(captured)
                    try:
                        text = captured[left:right].decode("utf-8")
                        decode_status = "boundary_trimmed" if left else "utf8"
                    except UnicodeDecodeError as exc:
                        if offset < observed and exc.reason == "unexpected end of data":
                            right = left + exc.start
                            text = captured[left:right].decode("utf-8")
                            decode_status = "boundary_trimmed"
                        else:
                            decode_status = "invalid_utf8"
                            text = "[Non-UTF-8 output retained as raw bytes; see output_refs]"
                stored = box.artifacts.write_bytes(captured, media_type="application/octet-stream" if file_action or decode_status != "utf8" else "text/plain")
                ident = uuid5(operation_id, "executor-output:" + stream)
                ref = {"operation_id": str(operation_id), "resource_id": str(job_id), "artifact_id": str(ident),
                    "stream": stream, "sha256": stored.sha256, "stored_offset": start, "next_offset": offset,
                    "observed_size": observed, "source_bytes": page.get("source_bytes") if page.get("source_bytes") is not None else observed,
                    "missing_ranges": page.get("missing_ranges", []), "missing_ranges_truncated": bool(page.get("missing_ranges_truncated")) or not page.get("collection_metadata_known", False),
                    "truncated": incomplete, "decode_status": decode_status}
                async with self.db.transaction() as db:
                    existing = await db.get(ArtifactRow, ident)
                    if existing and existing.sha256 != stored.sha256:
                        raise ExecutorProtocolError("终态输出快照发生变化")
                    if not existing:
                        await ArtifactRepository(db).create(artifact_id=ident, sha256=stored.sha256,
                            byte_size=stored.byte_size, media_type=stored.media_type, relative_key=stored.relative_key,
                            session_id=box.session_id, source_operation_id=operation_id, sensitivity="internal", availability="available",
                            captured_at=stored.captured_at, truncated=incomplete,
                            captured_by_identity=f"worker-transport:{operation_id}" if file_action or decode_status != "utf8" else f"worker-output:{operation_id}",
                            summary=f"Worker {stream} stored bytes [{start},{offset}) of {observed}; decode={decode_status}")
                chunks.append(captured)
                texts.append(text)
                refs.append(ref)
            response: dict[str, Any]
            if file_action:
                try:
                    response = {**json.loads(chunks[0].decode("utf-8")), "operation_id": str(operation_id)}
                except (ValueError, TypeError):
                    response = {"error": "file_helper_output_unreadable", "process_status": status, "operation_id": str(operation_id)}
                if response.get("ok") is False:
                    status = "failed"
            else:
                warning = "\n[Output incomplete or not valid UTF-8; exact ranges and original bytes are in output_refs]" if any(r["truncated"] or r["decode_status"] != "utf8" for r in refs) else ""
                response = {"data": {"output": "".join(texts) + warning, "exit_code": state.get("exit_code"),
                    "truncated": any(r["truncated"] for r in refs)}, "operation_id": str(operation_id)}
            response.update(artifact_id=refs[0]["artifact_id"], output_refs=refs)
            async with self.db.transaction() as db:
                current = await db.get(ToolOperationRow, operation_id, with_for_update=True)
                assert current
                # A concurrent observer can only commit the same immutable result.
                current.status = status
                current.result_artifact_ids = [r["artifact_id"] for r in refs]
                # Store metadata only. Internal Base64 bytes stay in CAS, never business JSON.
                current.result_snapshot = {"output_refs": refs, "file_action": file_action, "helper_ok": response.get("ok")}
            return response
        except (ExecutorConnectionError, ExecutorProtocolError, ExecutorRejected, ValueError, KeyError, OSError) as exc:
            return {"error": "output_unavailable:" + type(exc).__name__, "process_status": status, "operation_id": str(operation_id)}


async def close_worker_aggregates(db: Database, row: AgentDelegationRow) -> None:
    """These are local formatting containers, never remote side effects."""
    async with db.transaction() as session:
        operations = list((await session.scalars(select(ToolOperationRow).where(
            ToolOperationRow.run_id == row.parent_run_id,
            ToolOperationRow.execution_context["agent_delegation_id"].astext == str(row.id),
            ToolOperationRow.status.in_(["accepted", "running", "unknown"])).with_for_update())).all())
        for operation in operations:
            context = operation.execution_context or {}
            if context.get("worker_tool_aggregate") or context.get("submission_state") == "not_submitted":
                operation.status = "failed"
                operation.result_snapshot = {**(operation.result_snapshot or {}), "local_interrupted": True,
                    "execution_certainty": "not_applied" if not context.get("worker_tool_aggregate") else "local_container"}


async def settle_worker(db: Database, _settings: Settings, row: AgentDelegationRow, client: ExecutorClient, *, cancel: bool) -> bool:
    from agent_workbench.contracts.execution_runtime import execution_request_digest
    from agent_workbench.domain.execution_delegations import revocation_payload

    confirmed, ack, current_run = True, None, None
    if cancel:
        async with db.transaction() as session:
            current = await session.get(AgentDelegationRow, row.id, with_for_update=True)
            if current:
                current.authorization_snapshot = {**current.authorization_snapshot, "execution_revoked": True}
            current_run = await session.get(RunRow, row.parent_run_id)
            revocations = await revocation_payload(session, row.parent_session_id)
        if current_run:
            try:
                ack = await client.execution_sync_control({"session_id": str(row.parent_session_id), "run_id": str(current_run.id),
                    "owner": current_run.owner or "worker-cleanup", "owner_generation": current_run.owner_generation,
                    "control_version": current_run.control_version, "expected_seq": None, "revocations": revocations, "note": "Worker cleanup"})
                confirmed = bool(ack.get("applied"))
            except (ExecutorConnectionError, ExecutorProtocolError, ExecutorRejected):
                confirmed = False
    await close_worker_aggregates(db, row)
    async with db.session() as session:
        operations = list((await session.scalars(select(ToolOperationRow).where(
            ToolOperationRow.run_id == row.parent_run_id,
            ToolOperationRow.execution_context["agent_delegation_id"].astext == str(row.id),
            ToolOperationRow.execution_context["parent_worker_operation"].astext.is_not(None)))).all())
    for operation in operations:
        if operation.status in {"succeeded", "failed", "cancelled"}:
            continue
        try:
            remote = await client.execution_operation(str(operation.operation_id))
            resource = remote.get("resource_id")
            if not resource:
                confirmed = False
                async with db.transaction() as session:
                    await ToolOperationRepository(session).mark_result(operation.operation_id, status="unknown")
                continue
            state = await client.execution_resource(resource)
            stopped = state.get("state") in TERMINAL and (state.get("exit_code") is not None or state.get("termination_reason") not in {None, "unknown"})
            if not stopped and cancel and ack and ack.get("applied") and current_run:
                # Use parent control authority: the Worker's revision is already revoked.
                # A durable marker ensures a lost cancellation POST is queried, not reposted.
                ident = uuid5(row.id, f"cancel:{operation.operation_id}")
                arguments = {"resource_id": resource}
                digest = execution_request_digest("cancel_resource", arguments)
                async with db.transaction() as session:
                    leaf = await session.get(ToolOperationRow, operation.operation_id, with_for_update=True)
                    assert leaf
                    should_send = not (leaf.execution_context or {}).get("cancel_operation_id")
                    if should_send:
                        leaf.execution_context = {**(leaf.execution_context or {}), "cancel_operation_id": str(ident)}
                if should_send:
                    try:
                        await client.execution_submit({"operation_id": str(ident), "session_id": str(row.parent_session_id),
                            "run_id": str(row.parent_run_id), "tool_call_id": f"worker-cancel:{operation.operation_id}",
                            "request_digest": digest, "action": "cancel_resource", "arguments": arguments,
                            "owner_generation": current_run.owner_generation, "dispatch_control_version": current_run.control_version,
                            "session_control_seq": ack["fence"]["seq"], "wait_ms": 20000})
                    except (ExecutorConnectionError, ExecutorProtocolError, ExecutorRejected) as exc:
                        logger.warning("Worker cancel receipt unavailable; querying resource %s (%s)", resource, type(exc).__name__)
                state = await client.execution_resource(resource)
                stopped = state.get("state") in TERMINAL and (state.get("exit_code") is not None or state.get("termination_reason") not in {None, "unknown"})
            if stopped:
                async with db.transaction() as session:
                    from agent_workbench.persistence.repositories import (
                        ExecutionResourceRepository,
                        ToolJobRepository,
                    )
                    await ToolOperationRepository(session).attach_job(operation.operation_id, UUID(resource),
                        status="succeeded" if state.get("exit_code") == 0 else "failed")
                    await ToolJobRepository(session).upsert(job_id=UUID(resource), operation_id=operation.operation_id,
                        executor_id=_settings.executor_id, state="exited", sync_status="in_sync")
                    await ExecutionResourceRepository(session).apply_close_result(UUID(resource), confirmed=True, state="exited", detail={"worker_cleanup": True})
            else:
                confirmed = False
        except (ExecutorConnectionError, ExecutorProtocolError, ExecutorRejected, ValueError):
            confirmed = False
            async with db.transaction() as session:
                await ToolOperationRepository(session).mark_result(operation.operation_id, status="unknown")
    return confirmed


async def validate_output_refs(db: Any, settings: Settings, row: AgentDelegationRow, result: Any) -> bool:
    store = ArtifactStore(settings)
    for ref in result.output_refs:
        operation = await db.get(ToolOperationRow, ref.operation_id)
        artifact = await db.get(ArtifactRow, ref.artifact_id)
        if (not operation or not artifact or operation.run_id != row.parent_run_id or operation.session_id != row.parent_session_id
                or (operation.execution_context or {}).get("agent_delegation_id") != str(row.id)
                or (operation.execution_context or {}).get("context_id") != str(row.context_id)
                or artifact.session_id != row.parent_session_id or artifact.source_operation_id != operation.operation_id
                or operation.job_id != ref.resource_id or artifact.sha256 != ref.sha256 or artifact.availability != "available"
                or ref.model_dump(mode="json") not in (operation.result_snapshot or {}).get("output_refs", [])):
            return False
        try:
            if not store.verify(artifact.relative_key, artifact.sha256):
                return False
        except OSError:
            return False
    return True
