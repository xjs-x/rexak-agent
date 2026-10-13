"""远端工具执行的共享骨架。

`http_request` 与文件工具走的是同一套顺序（§8.5）：

    prepare tool_operation -> 提交控制端记录 -> Executor 幂等接收
    -> 等待/接受 -> 记录结果 -> 写 tool message/checkpoint

把它抽出来是为了避免两套实现漂移 —— 尤其是 `operation_id` 的复用语义，
一旦某条路径自己随机生成 ID，幂等保证就破了。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from agent_workbench.contracts.enums import (
    ErrorCode,
    ExecutionCertainty,
    OperationStatus,
    ToolEffectClass,
)
from agent_workbench.contracts.tool import ArtifactRef, JobSnapshot, ToolError, ToolResult
from agent_workbench.execution.client import (
    ExecutorConnectionError,
    ExecutorProtocolError,
    ExecutorRejected,
)
from agent_workbench.persistence.repositories import (
    ToolJobRepository,
    ToolOperationRepository,
)
from agent_workbench.tools.operation import OperationRegistry
from agent_workbench.tools.runtime import Toolbox


async def prepare_operation(
    toolbox: Toolbox,
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    effect_class: ToolEffectClass,
    context_extra: dict[str, Any] | None = None,
) -> tuple[UUID, bool]:
    """在一个**短事务**内准备操作身份。禁止在事务里等待网络或模型。

    `context_extra` 记录派发时**真实**的请求上下文（方法、入口、身份），
    供领域登记工具把证据关联到真实采集操作，而不是默认成 GET。
    """
    async with toolbox.db.transaction() as db:
        prepared = await OperationRegistry(db).prepare(
            run_id=toolbox.run_id,
            session_id=toolbox.session_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            effect_class=effect_class,
            policy_revision=toolbox.policy_revision,
            scope_revision=toolbox.scope_revision,
            dispatch_control_version=toolbox.control_version,
            identity_id=toolbox.identity_id,
            context_extra=context_extra,
        )
        return prepared.operation_id, prepared.replayed


async def record_submitted(
    toolbox: Toolbox, operation_id: UUID, job_id: UUID, *, accepted_at: datetime
) -> None:
    async with toolbox.db.transaction() as db:
        await ToolOperationRepository(db).attach_job(operation_id, job_id, status="running")
        await ToolJobRepository(db).upsert(
            job_id=job_id,
            operation_id=operation_id,
            executor_id=toolbox.settings.executor_id,
            state="received",
            executor_accepted_at=accepted_at,
        )


async def record_result(
    toolbox: Toolbox,
    *,
    operation_id: UUID,
    job_id: UUID | None,
    status: str,
    refs: list[ArtifactRef] | None = None,
    failure: str | None = None,
    snapshot: JobSnapshot | None = None,
) -> None:
    from agent_workbench.contracts.events import EventType
    from agent_workbench.observability.events import append_event

    async with toolbox.db.transaction() as db:
        await ToolOperationRepository(db).mark_result(
            operation_id, status=status, artifact_ids=[str(r.artifact_id) for r in (refs or [])]
        )
        if snapshot is not None and (job_id is None or snapshot.job_id != job_id):
            raise ValueError("远端快照与当前 job_id 不一致")
        if job_id is not None:
            await ToolJobRepository(db).upsert(
                job_id=job_id,
                operation_id=operation_id,
                executor_id=toolbox.settings.executor_id,
                state=str(snapshot.state) if snapshot else status,
                sync_status="in_sync",
                executor_started_at=snapshot.started_at if snapshot else None,
                executor_finished_at=snapshot.finished_at if snapshot else None,
            )
        if failure is not None:
            await append_event(
                db,
                session_id=toolbox.session_id,
                run_id=toolbox.run_id,
                event_type=EventType.ERROR,
                payload={
                    "scope": "tool_submit",
                    "operation_id": str(operation_id),
                    "message": failure,
                    "execution_certainty": str(ExecutionCertainty.NOT_APPLIED),
                },
            )


def rejection_result(operation_id: UUID, exc: ExecutorRejected) -> ToolResult:
    """执行岛**明确拒绝** -> 操作确定没有执行，重试同样会被拒。"""
    return ToolResult(
        operation_id=operation_id,
        status=OperationStatus.FAILED,
        summary=f"执行岛明确拒绝了该操作，请求未发出：{exc}",
        error=ToolError(
            code=str(ErrorCode.VALIDATION_FAILED),
            message=str(exc),
            retryable=False,
            recovery_hint="这属于参数/策略不匹配，重试同样会被拒绝；请修正参数。",
        ),
        execution_certainty=ExecutionCertainty.NOT_APPLIED,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )


def transport_unknown_result(operation_id: UUID, exc: Exception) -> ToolResult:
    """传输不确定 -> 可能已经执行，必须先查询再决定。"""
    return ToolResult(
        operation_id=operation_id,
        status=OperationStatus.UNKNOWN,
        summary="无法确认远端是否已接收该操作；必须先查询再决定后续，不要直接重试",
        error=ToolError(
            code=str(ErrorCode.EXECUTOR_UNAVAILABLE),
            message=str(exc),
            retryable=True,
            recovery_hint="按同一 operation_id 重新提交只会得到原 job，不会产生第二个副作用。",
        ),
        execution_certainty=ExecutionCertainty.UNCERTAIN,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )


async def wait_or_accept(
    toolbox: Toolbox, operation_id: UUID, job_id: UUID, *, accept_note: str
) -> ToolResult | None:
    """在交互阈值内等待；超时返回 `accepted`，让模型调用链不被占住（§8.3）。

    返回 `None` 表示已经到达终态，调用方继续后续处理。
    """
    try:
        await asyncio.wait_for(
            toolbox.wait_for_terminal(job_id), timeout=toolbox.interactive_threshold_seconds
        )
    except TimeoutError:
        return ToolResult.accepted(operation_id, job_id, accept_note)
    return None


async def download_result_json(toolbox: Toolbox, job_id: UUID) -> dict[str, Any] | None:
    """取回 Executor 写在 job 目录里的 `result.json`。

    返回 `None` 表示拿不到（工件缺失或传输失败）——调用方必须按"结果不可用"处理，
    **不能**把缺失当成成功。
    """
    try:
        data, _headers = await toolbox.executor.download_artifact(
            job_id, "result.json", limit=2 * 1024 * 1024
        )
    except (ExecutorConnectionError, ExecutorProtocolError):
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def json_text(result: ToolResult) -> str:
    """工具进入模型上下文的形态：统一结果契约的 JSON 文本。"""
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))


def scope_payload(toolbox: Toolbox) -> dict[str, Any]:
    """构造执行岛可接受的范围载荷。

    **只发送执行岛 schema 声明过的字段**（执行岛对未知字段是 `extra="forbid"`）。
    控制端内部的 `notes` 等展示字段不进载荷。
    """
    snapshot = toolbox.scope.snapshot
    return {
        "revision": snapshot.revision,
        "rules": [
            {
                "scheme": rule.scheme,
                "host": rule.host,
                "port": rule.port,
                "include_subdomains": rule.include_subdomains,
                "allow_private": rule.allow_private,
            }
            for rule in snapshot.rules
        ],
        "mappings": [item.model_dump() for item in getattr(snapshot, "mappings", [])],
        "denied_metadata_hosts": list(getattr(snapshot, "denied_metadata_hosts", []) or []),
    }


async def store_remote_artifact(
    toolbox: Toolbox,
    *,
    name: str,
    job_id: UUID,
    operation_id: UUID,
    media_type: str,
    identity: str,
    source_url: str = "",
    expected_sha256: str = "",
) -> ArtifactRef:
    """把一个远端工件同步为不可变工件并提交元数据。

    顺序：先写文件（原子替换），再提交数据库元数据。
    哈希不匹配 -> 标 `orphaned`，**不作为可用证据**（§5.3）。
    同步失败 -> 标 `remote_only`，如实说明，不静默丢弃。
    """
    from agent_workbench.contracts.enums import ArtifactAvailability, Sensitivity
    from agent_workbench.evidence.artifacts import ArtifactStore  # noqa: F401
    from agent_workbench.persistence.repositories import ArtifactRepository

    try:
        data, headers = await _download_complete_artifact(
            toolbox.executor,
            job_id=job_id,
            name=name,
            limit=toolbox.artifact_sync_limit_bytes,
            resume_root=toolbox.artifacts.root,
        )
    except Exception as exc:
        return ArtifactRef(
            artifact_id=UUID(int=0),
            media_type=media_type,
            byte_size=0,
            sha256="",
            availability=ArtifactAvailability.REMOTE_ONLY,
            source_operation_id=operation_id,
            summary=f"工件仍在远端、未能同步：{type(exc).__name__}",
        )

    expected = expected_sha256 or headers.get("sha256") or ""
    stored = toolbox.artifacts.write_bytes(
        data,
        media_type=media_type,
        sensitivity=Sensitivity.INTERNAL,
        captured_by=identity,
        source_url=source_url,
        source_operation=str(operation_id),
        capture_time=datetime.now(UTC),
    )

    if expected and stored.sha256 != expected:
        return ArtifactRef(
            artifact_id=UUID(int=0),
            media_type=media_type,
            byte_size=stored.byte_size,
            sha256=stored.sha256,
            availability=ArtifactAvailability.ORPHANED,
            source_operation_id=operation_id,
            summary=(
                f"哈希核对失败（期望 {expected[:12]}…，实际 {stored.sha256[:12]}…）；不作为可用证据"
            ),
        )

    try:
        async with toolbox.db.transaction() as db:
            row = await ArtifactRepository(db).create(
                sha256=stored.sha256,
                byte_size=stored.byte_size,
                media_type=media_type,
                relative_key=stored.relative_key,
                session_id=toolbox.session_id,
                source_operation_id=operation_id,
                sensitivity=str(Sensitivity.INTERNAL),
                availability=str(ArtifactAvailability.AVAILABLE),
                captured_at=stored.captured_at,
                truncated=stored.truncated,
                captured_by_identity=identity,
                source_url=source_url,
                summary=f"{name}（{stored.byte_size} 字节）",
            )
    except Exception as exc:
        return ArtifactRef(
            artifact_id=UUID(int=0),
            media_type=media_type,
            byte_size=stored.byte_size,
            sha256=stored.sha256,
            availability=ArtifactAvailability.ORPHANED,
            source_operation_id=operation_id,
            summary=f"元数据提交失败，工件未成为可用证据：{type(exc).__name__}",
        )
    return ArtifactRef(
        artifact_id=row.id,
        media_type=media_type,
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        availability=ArtifactAvailability.AVAILABLE,
        sensitivity=Sensitivity.INTERNAL,
        truncated=stored.truncated,
        source_operation_id=operation_id,
        captured_at=stored.captured_at,
        summary=f"{name}（{stored.byte_size} 字节）",
    )


async def _download_complete_artifact(
    executor: Any,
    *,
    job_id: UUID,
    name: str,
    limit: int,
    resume_root: Path | None = None,
) -> tuple[bytes, dict[str, str]]:
    """分块续取并核对远端文件；不能把首块误当成完整证据。"""
    if limit <= 0:
        raise ValueError("工件同步上限必须大于零")
    part_path: Path | None = None
    meta_path: Path | None = None
    if resume_root is not None:
        sync_dir = resume_root / ".sync"
        sync_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(f"{job_id}/{name}".encode()).hexdigest()
        part_path = sync_dir / f"{key}.part"
        meta_path = sync_dir / f"{key}.json"

    offset = 0
    chunks: list[bytes] = []
    expected_size: int | None = None
    expected_sha = ""
    if (
        part_path is not None
        and meta_path is not None
        and part_path.exists()
        and meta_path.exists()
    ):
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            expected_size = int(metadata["size"])
            expected_sha = str(metadata["sha256"])
            offset = part_path.stat().st_size
            if not (0 <= offset <= expected_size <= limit and len(expected_sha) == 64):
                raise ValueError("无效的续传元数据")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            part_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            expected_size = None
            expected_sha = ""
            offset = 0
    elif part_path is not None and meta_path is not None:
        part_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)

    restarted = False
    while True:
        chunk, headers = await executor.download_artifact(
            job_id, name, offset=offset, limit=max(1, min(1_048_576, limit - offset))
        )
        size = int(headers.get("size", "-1"))
        remote_sha = headers.get("sha256", "")
        if size < 0 or size > limit or len(remote_sha) != 64:
            raise ValueError("远端工件大小无效或超过同步上限")
        if expected_size is None:
            expected_size = size
            expected_sha = remote_sha
            if meta_path is not None:
                temporary = meta_path.with_suffix(".json.tmp")
                temporary.write_text(
                    json.dumps({"size": size, "sha256": remote_sha}), encoding="utf-8"
                )
                temporary.replace(meta_path)
        if size != expected_size or remote_sha != expected_sha:
            if part_path is not None and meta_path is not None and offset and not restarted:
                part_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                offset = 0
                expected_size = None
                expected_sha = ""
                restarted = True
                continue
            raise ValueError("远端工件在分块下载期间发生变化")
        if int(headers.get("offset", "-1")) != offset:
            raise ValueError("远端工件分块偏移不一致")
        if not chunk and offset < size:
            raise ValueError("远端工件提前结束")
        if part_path is None:
            chunks.append(chunk)
        else:
            with part_path.open("ab") as handle:
                handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        offset += len(chunk)
        if offset > size:
            raise ValueError("远端工件返回的数据超过声明大小")
        if offset == size:
            data = b"".join(chunks) if part_path is None else part_path.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected_sha:
                if part_path is not None and meta_path is not None:
                    part_path.unlink(missing_ok=True)
                    meta_path.unlink(missing_ok=True)
                raise ValueError("远端工件内容哈希不一致")
            if part_path is not None and meta_path is not None:
                part_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
            return data, headers
