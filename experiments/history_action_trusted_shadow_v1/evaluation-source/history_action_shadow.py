"""默认关闭的 HTTP 分发旁路观察。队列不等待、不调用 Executor、不写业务表。"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from agent_workbench.contracts.history_action_reflection import Observation
from agent_workbench.domain.history_action_reflection import digest, observe_input
from agent_workbench.domain.trusted_http_shadow import ControlGrant, Records, adapt_history, capture
from agent_workbench.persistence.tables import ArtifactRow, ToolJobRow, ToolOperationRow

log = structlog.get_logger(__name__)


def write_once(path: Path, value: Any) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    except FileExistsError:
        return False
    return True


async def read_records(toolbox: Any) -> Records:
    async with toolbox.db.session() as db:
        operations = list(
            (
                await db.scalars(
                    select(ToolOperationRow).where(
                        ToolOperationRow.session_id == toolbox.session_id,
                        ToolOperationRow.tool_name == "http_request",
                    )
                )
            ).all()
        )
        ids = [op.operation_id for op in operations]
        jobs = (
            list(
                (await db.scalars(select(ToolJobRow).where(ToolJobRow.operation_id.in_(ids)))).all()
            )
            if ids
            else []
        )
        artifacts = (
            list(
                (
                    await db.scalars(
                        select(ArtifactRow).where(
                            ArtifactRow.session_id == toolbox.session_id,
                            ArtifactRow.source_operation_id.in_(ids),
                        )
                    )
                ).all()
            )
            if ids
            else []
        )
    return Records(operations, jobs, artifacts)


def dispatch_context(runtime: Any) -> dict[str, Any]:
    messages = []
    batch = []
    for message in (runtime.state or {}).get("messages", []):
        messages.append(
            {
                "type": getattr(message, "type", ""),
                "content": message.content,
                "tool_call_id": getattr(message, "tool_call_id", None),
                "status": getattr(message, "status", None),
                "tool_calls": copy.deepcopy(getattr(message, "tool_calls", [])),
            }
        )
        if getattr(message, "tool_calls", None):
            batch = [str(call["id"]) for call in message.tool_calls]
    return {"thread_id": runtime.context.thread_id, "batch_ids": batch, "messages": messages}


class ShadowService:
    def __init__(self, toolbox: Any) -> None:
        self.toolbox = toolbox
        self.root = (
            Path(toolbox.settings.data_root) / "history_action_shadow" / str(toolbox.session_id)
        )
        self.pending: set[asyncio.Task] = set()
        self.outcomes: list[dict[str, Any]] = []
        self._gate = asyncio.Semaphore(1)

    def enqueue(
        self,
        arguments: dict[str, Any],
        operation: dict[str, Any],
        tool_call_id: str,
        context: dict[str, Any] | None,
    ) -> None:
        # 所有输入在实际 submit 之前复制；异步观察不得读取随后变化的 Agent 消息。
        task = asyncio.create_task(
            self._worker(
                copy.deepcopy(arguments),
                copy.deepcopy(operation),
                tool_call_id,
                copy.deepcopy(context),
                datetime.now(UTC),
            )
        )
        self.pending.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self.pending.discard(task)
        if task.cancelled():
            log.info("history_action_shadow_cancelled")
        elif task.exception() is not None:
            log.error(
                "history_action_shadow_task_error", error_type=type(task.exception()).__name__
            )

    async def _worker(
        self,
        arguments: dict[str, Any],
        operation: dict[str, Any],
        tool_call_id: str,
        context: dict[str, Any] | None,
        cutoff: datetime,
    ) -> None:
        started = asyncio.get_running_loop().time()
        try:
            async with asyncio.timeout(self.toolbox.settings.history_action_shadow_timeout_seconds):
                async with self._gate:
                    await self._observe(arguments, operation, tool_call_id, context, cutoff)
        except Exception as exc:
            diagnostic = {
                "mode": "SHADOW_ONLY",
                "operation_id": operation["operation_id"],
                "tool_call_id": tool_call_id,
                "classification": "UNKNOWN_SEMANTICS",
                "emit_shadow_event": False,
                "diagnostic": type(exc).__name__,
            }
            self.outcomes.append(diagnostic)
            log.warning("history_action_shadow_failed", error_type=type(exc).__name__)
            try:
                write_once(self.root / "diagnostics" / (digest(diagnostic) + ".json"), diagnostic)
            except Exception as save_error:
                log.warning(
                    "history_action_shadow_diagnostic_failed", error_type=type(save_error).__name__
                )
        finally:
            log.debug(
                "history_action_shadow_elapsed",
                elapsed_ms=(asyncio.get_running_loop().time() - started) * 1000,
            )

    async def _observe(
        self,
        arguments: dict[str, Any],
        operation: dict[str, Any],
        tool_call_id: str,
        context: dict[str, Any] | None,
        cutoff: datetime,
    ) -> None:
        grants = []
        path = self.toolbox.settings.history_action_shadow_grants_path
        if path is not None:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            grants = [ControlGrant.model_validate(row) for row in payload]
        matches = [
            g
            for g in grants
            if (g.session_id, g.run_id, g.tool_call_id)
            == (str(self.toolbox.session_id), str(self.toolbox.run_id), tool_call_id)
        ]
        if len(matches) > 1:
            raise ValueError("ambiguous_controller_approval")
        from agent_workbench.contracts.planning import planning_digest

        current, reasons = capture(
            session_id=str(self.toolbox.session_id),
            run_id=str(self.toolbox.run_id),
            tool_call_id=tool_call_id,
            arguments=arguments,
            operation=operation,
            actual_identity_id=self.toolbox.identity_id,
            executor=self.toolbox.executor,
            grant=matches[0] if matches else None,
            mapping_signature=planning_digest(self.toolbox.scope.snapshot.target_mappings),
            now=cutoff,
        )
        if current is None or not context or not context.get("thread_id"):
            observed = Observation(
                snapshot_id="http-shadow:" + operation["operation_id"],
                candidate_tool_call_id=tool_call_id,
                candidate_tool_name="http_request",
                predecision_summary={
                    "run_id": str(self.toolbox.run_id),
                    "session_id": str(self.toolbox.session_id),
                    "thread_id": (context or {}).get("thread_id"),
                },
                classification="NOT_IN_SCOPE"
                if reasons == ["no_controller_security_test_approval"]
                else "UNKNOWN_SEMANTICS",
                policy="raw_visible_v1",
                nontrigger_reasons=reasons or ["thread_context_unavailable"],
            )
        else:
            current["thread_id"] = context["thread_id"]
            write_once(self.root / "captures" / (operation["operation_id"] + ".json"), current)
            records = await read_records(self.toolbox)
            captures = {}
            for row in records.operations:
                source = self.root / "captures" / (str(row.operation_id) + ".json")
                if source.is_file():
                    captures[str(row.operation_id)] = json.loads(source.read_text(encoding="utf-8"))
            raw = {}
            for art in records.artifacts:
                if (
                    art.availability == "available"
                    and not art.truncated
                    and art.byte_size <= 262144
                ):
                    raw[str(art.id)] = self.toolbox.artifacts.read_bytes(
                        art.relative_key, limit=art.byte_size + 1
                    )
            snapshot, diagnostics = adapt_history(current, records, captures, raw, context)
            observed = observe_input(snapshot)
            observed = observed.model_copy(
                update={
                    "predecision_summary": {
                        **observed.predecision_summary,
                        "run_id": str(self.toolbox.run_id),
                        "thread_id": context["thread_id"],
                    },
                    "diagnostics": [*observed.diagnostics, *diagnostics],
                }
            )
            if observed.emit_shadow_event:
                claimed = write_once(
                    self.root / "events" / (str(observed.dedup_key) + ".json"),
                    {
                        "dedup_key": observed.dedup_key,
                        "status": "triggered",
                        "mode": "SHADOW_ONLY",
                        "operation_id": operation["operation_id"],
                        "evidence_refs": observed.original_evidence_refs,
                    },
                )
                if not claimed:
                    observed = observed.model_copy(
                        update={"emit_shadow_event": False, "duplicate_suppressed": True}
                    )
        data = observed.model_dump(mode="json")
        self.outcomes.append(data)
        write_once(
            self.root
            / "observations"
            / (
                digest({"operation": operation["operation_id"], "cutoff": cutoff.isoformat()})
                + ".json"
            ),
            data,
        )

    async def drain(self) -> None:
        while self.pending:
            await asyncio.gather(*tuple(self.pending), return_exceptions=True)

    async def aclose(self) -> None:
        await self.drain()


def enqueue_http(
    toolbox: Any,
    arguments: dict[str, Any],
    operation: dict[str, Any],
    tool_call_id: str,
    context: dict[str, Any] | None,
) -> None:
    """只捕获副本和调度观察；普通故障不得覆盖合法 HTTP 调用。"""
    try:
        service = toolbox.history_action_shadow
        if service is None:
            service = ShadowService(toolbox)
            toolbox.history_action_shadow = service
        service.enqueue(arguments, operation, tool_call_id, context)
    except Exception as exc:
        log.warning("history_action_shadow_enqueue_failed", error_type=type(exc).__name__)
