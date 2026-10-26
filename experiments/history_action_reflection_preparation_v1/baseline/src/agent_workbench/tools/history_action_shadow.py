"""默认关闭的 HTTP 分发旁路观察。队列不等待、不调用 Executor、不写业务表。"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from agent_workbench.contracts.history_action_reflection import Observation
from agent_workbench.domain.history_action_reflection import digest, observe_input
from agent_workbench.domain.trusted_http_shadow import ControlGrant, Records, adapt_history, capture
from agent_workbench.persistence.tables import ArtifactRow, ToolJobRow, ToolOperationRow
from agent_workbench.tools.shadow_storage import ShadowStore

log = structlog.get_logger(__name__)


class ShadowHistoryLimit(RuntimeError):
    """只限制观察资源，不影响原 HTTP 派发。"""


def input_within_limit(value: Any, limit: int) -> bool:
    pending, consumed = [value], 0
    while pending:
        item = pending.pop()
        consumed += 8
        if consumed > limit:
            return False
        if isinstance(item, str):
            if len(item) > limit - consumed:
                return False
            consumed += len(item.encode("utf-8"))
        elif isinstance(item, dict):
            if len(item) > (limit - consumed) // 16:
                return False
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            if len(item) > (limit - consumed) // 8:
                return False
            pending.extend(item)
    return consumed <= limit


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
                    select(ToolOperationRow)
                    .where(
                        ToolOperationRow.session_id == toolbox.session_id,
                        ToolOperationRow.run_id == toolbox.run_id,
                        ToolOperationRow.tool_name == "http_request",
                    )
                    .limit(toolbox.settings.history_action_shadow_history_limit + 1)
                )
            ).all()
        )
        if len(operations) > toolbox.settings.history_action_shadow_history_limit:
            raise ShadowHistoryLimit("history_exceeds_observation_budget")
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
    size = 0
    for message in (runtime.state or {}).get("messages", []):
        size += len(str(message.content).encode("utf-8"))
        size += len(
            json.dumps(getattr(message, "tool_calls", []), ensure_ascii=False).encode("utf-8")
        )
        if size > runtime.context.toolbox.settings.history_action_shadow_input_bytes:
            raise ValueError("shadow_context_input_limit")
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
        self._closed = False
        self.skipped = 0
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="http-shadow")
        self._computation: Any = None
        self.store = ShadowStore(self.root, toolbox.settings)
        self._timing: dict[str, float] = {}

    def _remember(self, value: dict) -> None:
        self.outcomes.append(value)
        del self.outcomes[:-128]

    def _write(self, path: Path, value: Any) -> bool:
        if self._closed:
            raise RuntimeError("shadow_service_closed")
        return self.store.write(path, value)

    def enqueue(
        self,
        arguments: dict[str, Any],
        operation: dict[str, Any],
        tool_call_id: str,
        context: dict[str, Any] | None,
    ) -> None:
        if (
            self._closed
            or len(self.pending) >= self.toolbox.settings.history_action_shadow_queue_limit
        ):
            self.skipped += 1
            log.info("history_action_shadow_skipped", reason="closed_or_queue_full")
            return
        if not input_within_limit(
            [arguments, operation], self.toolbox.settings.history_action_shadow_input_bytes
        ):
            self.skipped += 1
            log.info("history_action_shadow_skipped", reason="dispatch_input_limit")
            return
        # 所有输入在实际 submit 之前复制；异步观察不得读取随后变化的 Agent 消息。
        control_error = None
        grants = []
        try:
            from agent_workbench.domain.shadow_authorization import grant_for_binding

            binding = (context or {}).get("profile_binding")
            path = self.toolbox.settings.history_action_shadow_grants_path
            if binding is not None:
                grant, provenance = grant_for_binding(
                    self.toolbox, binding, arguments, operation, tool_call_id
                )
                grants = [grant.model_dump()]
                context = {**(context or {}), "trusted_task": provenance}
                context.pop("profile_binding", None)
            elif path is not None:
                if Path(path).stat().st_size > 1048576:
                    raise ValueError("controller_grants_resource_limit")
                grants = json.loads(Path(path).read_text(encoding="utf-8"))
                if not isinstance(grants, list):
                    raise ValueError("controller_grants_must_be_list")
        except Exception as exc:
            control_error = type(exc).__name__
        task = asyncio.create_task(
            self._worker(
                copy.deepcopy(arguments),
                copy.deepcopy(operation),
                tool_call_id,
                copy.deepcopy(context),
                datetime.now(UTC),
                copy.deepcopy(grants),
                control_error,
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
        grants: list,
        control_error: str | None,
    ) -> None:
        started = asyncio.get_running_loop().time()
        try:
            if control_error is not None:
                raise ValueError("controller_input_snapshot_failed")
            async with asyncio.timeout(self.toolbox.settings.history_action_shadow_timeout_seconds):
                async with self._gate:
                    self._timing = {
                        "queue_wait_ms": (datetime.now(UTC) - cutoff).total_seconds() * 1000
                    }
                    await self._observe(arguments, operation, tool_call_id, context, cutoff, grants)
        except Exception as exc:
            diagnostic = {
                "mode": "SHADOW_ONLY",
                "operation_id": operation["operation_id"],
                "tool_call_id": tool_call_id,
                "classification": "UNKNOWN_SEMANTICS",
                "emit_shadow_event": False,
                "diagnostic": type(exc).__name__,
                "control_input_error_type": control_error,
            }
            self._remember(diagnostic)
            log.warning("history_action_shadow_failed", error_type=type(exc).__name__)
            try:
                self._write(self.root / "diagnostics" / (digest(diagnostic) + ".json"), diagnostic)
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
        grant_values: list,
    ) -> None:
        grants = [ControlGrant.model_validate(row) for row in grant_values]
        matches = [
            g
            for g in grants
            if (g.session_id, g.run_id, g.tool_call_id)
            == (str(self.toolbox.session_id), str(self.toolbox.run_id), tool_call_id)
        ]
        if len(matches) > 1:
            raise ValueError("ambiguous_controller_approval")
        from agent_workbench.contracts.planning import planning_digest

        phase = time.perf_counter()
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
        self._timing["capture_ms"] = (time.perf_counter() - phase) * 1000
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
            if context.get("trusted_task"):
                current["trusted_task"] = context["trusted_task"]
            self._write(self.root / "captures" / (operation["operation_id"] + ".json"), current)
            phase = time.perf_counter()
            records = await read_records(self.toolbox)
            self._timing["history_query_ms"] = (time.perf_counter() - phase) * 1000
            if self._computation is not None and not self._computation.done():
                raise ShadowHistoryLimit("previous_cpu_observation_still_running")
            self._computation = self._pool.submit(self._compare, current, records, context)
            snapshot, diagnostics, observed, timings = await asyncio.shield(
                asyncio.wrap_future(self._computation)
            )
            self._timing.update(timings)
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
                # 新的物理 ID / 同字节结果不构成新的证据状态，避免“继续→再提示”循环。
                eligible = {
                    m.operation_ref for m in observed.related_history if m.high_confidence_overlap
                }
                body_states = sorted(
                    {
                        e.sha256
                        for e in snapshot.evidence
                        if e.evidence_ref in observed.original_evidence_refs
                        and e.sha256 is not None
                    }
                )
                result_states = sorted(
                    {
                        digest(op.result_snapshot or {})
                        for op in records.operations
                        if "operation:" + str(op.operation_id) in eligible
                    }
                )
                material_key = digest(
                    {
                        "policy": "trusted-material-evidence/1",
                        "session": snapshot.session_id,
                        "task": snapshot.task_version,
                        "context": snapshot.context_version,
                        "conditions": snapshot.candidate.conditions,
                        "run": str(self.toolbox.run_id),
                        "authorization": current.get("trusted_task", {}).get(
                            "authorization_sha256"
                        ),
                        "body_states": body_states,
                        "result_states": result_states,
                    }
                )
                observed = observed.model_copy(
                    update={
                        "dedup_key": material_key,
                        "predecision_summary": {
                            **observed.predecision_summary,
                            "detector_dedup_key": observed.dedup_key,
                            "dedup_policy": "trusted-material-evidence/1",
                        },
                    }
                )
                claimed = self._write(
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
        data["timing_ms"] = dict(self._timing)
        self._write(
            self.root
            / "observations"
            / (
                digest({"operation": operation["operation_id"], "cutoff": cutoff.isoformat()})
                + ".json"
            ),
            data,
        )
        self._remember(data)

    def _compare(self, current, records, context):
        from agent_workbench.domain.shadow_authorization import canonical_history_context

        captures, raw, consumed = {}, {}, 0
        for row in records.operations:
            source = self.root / "captures" / (str(row.operation_id) + ".json")
            if source.is_file():
                if source.stat().st_size > self.toolbox.settings.history_action_shadow_record_bytes:
                    raise ShadowHistoryLimit("stored_capture_size_limit")
                captures[str(row.operation_id)] = json.loads(source.read_text(encoding="utf-8"))
        for art in records.artifacts:
            if art.availability == "available" and not art.truncated and art.byte_size <= 262144:
                consumed += art.byte_size
                if consumed > self.toolbox.settings.history_action_shadow_input_bytes:
                    raise ShadowHistoryLimit("aggregate_artifact_input_limit")
                raw[str(art.id)] = self.toolbox.artifacts.read_bytes(
                    art.relative_key, limit=art.byte_size + 1
                )
        phase = time.perf_counter()
        authorized_captures = {
            key: value
            for key, value in captures.items()
            if value.get("run_id") == current["run_id"]
            and value.get("trusted_task", {}).get("authorization_sha256")
            == current.get("trusted_task", {}).get("authorization_sha256")
        }
        context = canonical_history_context(context, authorized_captures)
        snapshot, diagnostics = adapt_history(current, records, captures, raw, context)
        association = (time.perf_counter() - phase) * 1000
        phase = time.perf_counter()
        observed = observe_input(snapshot)
        return (
            snapshot,
            diagnostics,
            observed,
            {"association_ms": association, "detection_ms": (time.perf_counter() - phase) * 1000},
        )

    async def drain(self) -> None:
        while self.pending:
            await asyncio.gather(*tuple(self.pending), return_exceptions=True)

    async def aclose(self) -> None:
        try:
            async with asyncio.timeout(self.toolbox.settings.history_action_shadow_close_seconds):
                await self.drain()
        except TimeoutError:
            log.warning("history_action_shadow_close_timeout")
        finally:
            self._closed = True
            for task in tuple(self.pending):
                task.cancel()
            if self.pending:
                await asyncio.gather(*tuple(self.pending), return_exceptions=True)
            self._pool.shutdown(wait=False, cancel_futures=True)


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
