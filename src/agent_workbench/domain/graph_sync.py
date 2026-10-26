"""在完整逻辑工具轮次后计数；会话序号游标只由成功图提交推进。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import func, or_, select

from agent_workbench.domain.session_graph import check_writer, lock_graph, read_graph
from agent_workbench.persistence.tables import (
    ArtifactRow,
    EventRow,
    ExecutionResourceRow,
    SessionGraphReceiptRow,
    SessionRow,
    ToolJobRow,
    ToolOperationRow,
)


async def graph_context(
    toolbox: Any, completed: Any, *, force_page: bool = False
) -> dict[str, Any]:
    async with toolbox.db.transaction() as db:
        row = await lock_graph(db, toolbox.session_id, toolbox.settings.graph_sync_interval)
        await check_writer(db, toolbox)
        if completed is not None and completed.tool_calls:
            key = "round:" + str(completed.id)
            receipt = await db.get(
                SessionGraphReceiptRow, {"session_id": toolbox.session_id, "request_id": key}
            )
            if receipt is None:
                names = {x["name"] for x in completed.tool_calls}
                if names - {"get_graph", "update_graph"}:
                    row.completed_rounds += 1
                db.add(
                    SessionGraphReceiptRow(
                        session_id=toolbox.session_id,
                        request_id=key,
                        digest="round",
                        receipt={"rounds": row.completed_rounds},
                    )
                )
        # Reconcile durable job/operation projections even when the original UI event was lost.
        # Stable fingerprints identify source revisions, not execution ordering; Session events order them.
        from agent_workbench.contracts.events import EventType
        from agent_workbench.observability.events import append_event

        source_records = (
            await db.execute(
                select(ToolOperationRow, ToolJobRow)
                .outerjoin(ToolJobRow, ToolJobRow.job_id == ToolOperationRow.job_id)
                .where(ToolOperationRow.session_id == toolbox.session_id)
            )
        ).all()
        for operation, job in source_records:
            source = {
                "operation_id": str(operation.operation_id),
                "status": operation.status,
                "artifact_ids": operation.result_artifact_ids,
                "job_id": str(job.job_id) if job else None,
                "job_state": job.state if job else None,
                "remote_revision": job.remote_revision if job else None,
            }
            fingerprint = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
            key = "source:" + fingerprint
            if (
                await db.get(
                    SessionGraphReceiptRow, {"session_id": toolbox.session_id, "request_id": key}
                )
                is None
            ):
                await append_event(
                    db,
                    session_id=toolbox.session_id,
                    run_id=operation.run_id,
                    event_type=EventType.TOOL_COMPLETED,
                    payload={"scope": "graph_source", **source},
                )
                db.add(
                    SessionGraphReceiptRow(
                        session_id=toolbox.session_id,
                        request_id=key,
                        digest=fingerprint,
                        receipt={"source": source},
                    )
                )
        await db.flush()
        due = row.completed_rounds >= row.next_sync_round
        view: dict[str, Any] = {
            "revision": row.revision,
            "rounds": row.completed_rounds,
            "reviewed_seq": row.reviewed_seq,
            "sync_due": due,
            "note": "必要时 get_graph 主动召回；图是工作资料，不改变授权或自然结束。",
        }
        toolbox.graph_sync_snapshot = None
        window_id = f"review:{row.completed_rounds}" if due and not force_page else None
        active = (
            (
                await db.execute(
                    select(SessionGraphReceiptRow)
                    .where(
                        SessionGraphReceiptRow.session_id == toolbox.session_id,
                        SessionGraphReceiptRow.request_id.like("review:%"),
                    )
                    .order_by(SessionGraphReceiptRow.created_at.desc())
                )
            )
            .scalars()
            .first()
        )
        bounded = getattr(toolbox.settings, "graph_bounded_review", False)
        if not force_page and bounded and active and active.receipt.get("status") == "active":
            window_id = active.request_id
        if not force_page:
            toolbox.graph_review_window_id = window_id if bounded else None
        if (
            not due
            and not force_page
            and not toolbox.settings.graph_inject_every_round
            and toolbox.graph_review_window_id is None
        ):
            return view
        # delivery receipts distinguish omitted syncs from physical model retries.
        delivery_key = f"delivery:{row.completed_rounds}:{row.reviewed_seq}"
        delivery = await db.get(
            SessionGraphReceiptRow, {"session_id": toolbox.session_id, "request_id": delivery_key}
        )
        if delivery is None and not force_page:
            row.missed_syncs += 1  # unresolved until an update acknowledges this presentation
            db.add(
                SessionGraphReceiptRow(
                    session_id=toolbox.session_id,
                    request_id=delivery_key,
                    digest="delivery",
                    receipt={"reviewed_seq": row.reviewed_seq},
                )
            )
            # One reminder per interval if ignored; never block exploration or ending.
            row.next_sync_round = row.completed_rounds + toolbox.settings.graph_sync_interval
        graph = await read_graph(db, toolbox.session_id)
        if not toolbox.settings.graph_attack_enabled:
            graph["attack_graph"] = {"nodes": [], "edges": []}
        high_water = (await db.get(SessionRow, toolbox.session_id)).event_seq
        event_filter = (
            EventRow.session_id == toolbox.session_id,
            EventRow.session_seq > row.reviewed_seq,
            EventRow.session_seq <= high_water,
            or_(
                EventRow.type.startswith("tool."),
                EventRow.type.startswith("job."),
                EventRow.type.startswith("operation."),
            ),
            or_(
                EventRow.payload["tool"].astext.is_(None),
                EventRow.payload["tool"].astext.not_in(["get_graph", "update_graph"]),
            ),
        )
        remaining_count = (
            await db.execute(select(func.count()).select_from(EventRow).where(*event_filter))
        ).scalar_one()
        events = list(
            (
                await db.execute(
                    select(EventRow).where(*event_filter).order_by(EventRow.session_seq).limit(200)
                )
            ).scalars()
        )
        by_id = {str(op.operation_id): (op, job) for op, job in source_records}
        by_call = {(op.run_id, op.tool_call_id): str(op.operation_id) for op, _ in source_records}
        by_job = {str(op.job_id): str(op.operation_id) for op, _ in source_records if op.job_id}
        artifacts: dict[str, list[str]] = {}
        artifact_details: dict[str, list[dict[str, Any]]] = {}
        if source_records:
            for artifact in (
                await db.execute(
                    select(ArtifactRow).where(
                        ArtifactRow.session_id == toolbox.session_id,
                        ArtifactRow.source_operation_id.in_(
                            [op.operation_id for op, _ in source_records]
                        ),
                    )
                )
            ).scalars():
                artifacts.setdefault(str(artifact.source_operation_id), []).append(
                    f"artifact:{artifact.id}"
                )
                artifact_details.setdefault(str(artifact.source_operation_id), []).append(
                    {
                        "ref": f"artifact:{artifact.id}",
                        "media_type": artifact.media_type,
                        "summary": (artifact.summary or "")[:120],
                        "availability": artifact.availability,
                    }
                )
        from agent_workbench.evidence.redaction import redact_api_value

        resources = {
            r.origin_operation_id: r
            for r in (
                await db.execute(
                    select(ExecutionResourceRow).where(
                        ExecutionResourceRow.session_id == toolbox.session_id
                    )
                )
            ).scalars()
        }

        def page(prefix):
            selected: dict[str, list[int]] = {}
            auxiliary = []
            for event in prefix:
                payload = event.payload or {}
                if not event.type.startswith(("tool.", "job.", "operation.")):
                    continue
                if payload.get("tool") in {"get_graph", "update_graph"}:
                    continue
                candidates = [payload]
                if payload.get("scope") == "job_wakeup" and isinstance(
                    payload.get("summary"), list
                ):
                    candidates += [x for x in payload["summary"] if isinstance(x, dict)]
                matched = False
                for item in candidates:
                    key = (
                        str(item.get("operation_id") or "")
                        or by_call.get((event.run_id, item.get("tool_call_id")))
                        or by_job.get(str(item.get("job_id", "")))
                    )
                    if key in by_id:
                        selected.setdefault(key, []).append(event.session_seq)
                        matched = True
                if not matched:
                    auxiliary.append(
                        {
                            "seq": event.session_seq,
                            "type": event.type,
                            **{
                                k: payload[k]
                                for k in ("tool", "tool_call_id", "status", "state")
                                if k in payload
                            },
                        }
                    )
            pending = []

            for key, seqs in selected.items():
                operation, job = by_id[key]
                result = operation.result_snapshot or {}
                data = result.get("data") or {}
                uncertain = operation.status == "unknown" or bool(
                    job and (job.state == "unknown" or job.sync_status == "uncertain")
                )
                resource = resources.get(operation.operation_id)
                entry = {
                    "operation_ref": f"operation:{key}",
                    "tool": operation.tool_name,
                    "tool_call_id": operation.tool_call_id,
                    "event_seqs": sorted(set(seqs)),
                    "operation_status": operation.status,
                    "job_id": str(job.job_id) if job else None,
                    "job_state": job.state if job else None,
                    "certainty": "uncertain"
                    if uncertain
                    else "terminal"
                    if operation.status in {"succeeded", "failed", "cancelled"}
                    else "unresolved",
                    "result": "无法确认操作最终结果"
                    if uncertain
                    else str(redact_api_value(result.get("summary") or "未记录结果摘要"))[:400],
                    "metadata": redact_api_value(
                        {
                            k: data[k]
                            for k in (
                                "status_code",
                                "content_type",
                                "elapsed_ms",
                                "exit_code",
                                "delivery_state",
                                "error_category",
                            )
                            if k in data
                        }
                    ),
                    "artifact_refs": sorted(artifacts.get(key, [])),
                    "artifacts": redact_api_value(
                        sorted(artifact_details.get(key, []), key=lambda item: item["ref"])
                    ),
                    "purpose": str(
                        redact_api_value(
                            resource.purpose if resource and resource.purpose else "未提供用途"
                        )
                    )[:160],
                }
                if resource:
                    entry["execution"] = {
                        "state": resource.state,
                        "exit_code": resource.exit_code,
                        "termination_reason": resource.termination_reason,
                        "output_bytes_total": resource.quota.get("output_bytes"),
                        "dropped_bytes_total": resource.quota.get("dropped_bytes"),
                        "output_locator": {
                            "tool": "read_job_output",
                            "job_id": str(resource.resource_id),
                        },
                        "note": "总字节数不代表stdout/stderr分别大小；退出码不是攻击结果。",
                    }
                pending.append(entry)
            through = prefix[-1].session_seq if prefix else row.reviewed_seq
            if len(prefix) == remaining_count:
                through = high_water
            return {
                **view,
                "graph": graph,
                "pending": pending,
                "auxiliary_events": auxiliary,
                "through_seq": through,
                "high_water_seq": high_water,
                "remaining_events": remaining_count - len(prefix),
                "instruction": "核对本页并使用page_id确认；无变更可提交空operations。工具成功不等于攻击成功，unknown不是negative；不必逐操作建Attempt。",
            }

        chosen = []
        for event in events:
            candidate = page([*chosen, event])
            if (
                len(json.dumps(candidate, ensure_ascii=False)) + 512
                > toolbox.settings.graph_context_chars
            ):
                break
            chosen.append(event)
        view = page(chosen)
        if (events and not chosen) or len(
            json.dumps(view, ensure_ascii=False)
        ) + 512 > toolbox.settings.graph_context_chars:
            toolbox.graph_review_window_id = None
            return {
                "revision": row.revision,
                "sync_due": due,
                "overflow": True,
                "note": "完整记录无法放入同步预算，未展示、未确认；可读取工件，正常工作及自然结束不受限。",
            }
        digest = hashlib.sha256(
            json.dumps(view, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        page_id = "page:" + digest[:40]
        page_row = await db.get(
            SessionGraphReceiptRow, {"session_id": toolbox.session_id, "request_id": page_id}
        )
        if page_row is None:
            page_row = SessionGraphReceiptRow(
                session_id=toolbox.session_id,
                request_id=page_id,
                digest=digest,
                receipt={
                    "complete": True,
                    "presented": False,
                    "from_seq": row.reviewed_seq,
                    "through_seq": view["through_seq"],
                    "revision": row.revision,
                    "rounds": row.completed_rounds,
                    "page_id": page_id,
                },
            )
            db.add(page_row)
        view["page_id"] = page_id
        view["acknowledgement"] = {
            "tool": "update_graph",
            "expected_revision": row.revision,
            "page_id": page_id,
            "operations_if_no_change": [],
        }
        if bounded and window_id:
            window = await db.get(
                SessionGraphReceiptRow, {"session_id": toolbox.session_id, "request_id": window_id}
            )
            if window is None:
                window = SessionGraphReceiptRow(
                    session_id=toolbox.session_id,
                    request_id=window_id,
                    digest="review",
                    receipt={"status": "active", "attempts": 0},
                )
                db.add(window)
            if window.receipt.get("status") == "active":
                view["review_window_id"] = window_id
            else:
                toolbox.graph_review_window_id = None
        encoded = json.dumps(view, ensure_ascii=False)
        view["estimated_tokens"] = (len(encoded) + 3) // 4
        toolbox.graph_sync_snapshot = {
            "complete": True,
            "page_id": page_id,
            "through_seq": view["through_seq"],
            "rounds": row.completed_rounds,
        }
        return view


class GraphReviewExhausted(RuntimeError):
    """当前核对窗口不再允许物理请求；回到正常主请求，不是任务失败。"""


async def present_page(db: Any, toolbox: Any, page_id: str) -> None:
    await check_writer(db, toolbox)
    page = await db.get(
        SessionGraphReceiptRow, {"session_id": toolbox.session_id, "request_id": page_id}
    )
    if page is None:
        raise ValueError("同步页不存在")
    page.receipt = {**page.receipt, "presented": True}


async def finish_review(db: Any, toolbox: Any, reason: str) -> None:
    key = getattr(toolbox, "graph_review_window_id", None)
    if key:
        await lock_graph(db, toolbox.session_id, toolbox.settings.graph_sync_interval)
        await check_writer(db, toolbox)
        window = await db.get(
            SessionGraphReceiptRow, {"session_id": toolbox.session_id, "request_id": key}
        )
        if window and window.receipt.get("status") == "active":
            window.receipt = {**window.receipt, "status": "closed", "reason": reason}
            # Request-local presentation only; the receipt remains the durable authority.
            memory = (getattr(toolbox, "working_context", None) or {}).get("session_memory") or {}
            if memory.get("review_window_id") == key:
                memory["review_status"] = "closed"
                memory["review_exit_reason"] = reason


async def review_ready(toolbox: Any, previous_window: str | None, structural_failure: bool) -> bool:
    key = getattr(toolbox, "graph_review_window_id", None)
    if not key or not getattr(toolbox.settings, "graph_bounded_review", False):
        return False
    async with toolbox.db.transaction() as db:
        await lock_graph(db, toolbox.session_id, toolbox.settings.graph_sync_interval)
        await check_writer(db, toolbox)
        window = await db.get(
            SessionGraphReceiptRow, {"session_id": toolbox.session_id, "request_id": key}
        )
        if window is None or window.receipt.get("status") != "active":
            return False
        if window.receipt.get("last_outcome") == "disallowed_tool":
            await finish_review(db, toolbox, "disallowed_tool")
            return False
        used = window.receipt["attempts"]
        repair_pending = window.receipt.get("repair_pending", False)
        if used >= 2 or (
            used and not repair_pending and (previous_window != key or not structural_failure)
        ):
            await finish_review(db, toolbox, "exhausted" if used >= 2 else "no_structural_retry")
            return False
        return True


async def record_review_outcome(db: Any, toolbox: Any, outcome: str, **metadata: Any) -> int:
    """仅保存可观测的协议元数据；不保存正文、参数或供应商推理。"""
    key = getattr(toolbox, "graph_review_window_id", None)
    if not key:
        return 0
    await lock_graph(db, toolbox.session_id, toolbox.settings.graph_sync_interval)
    await check_writer(db, toolbox)
    window = await db.get(
        SessionGraphReceiptRow, {"session_id": toolbox.session_id, "request_id": key}
    )
    if window is None or window.receipt.get("status") != "active":
        return 0
    from agent_workbench.contracts.events import EventType
    from agent_workbench.observability.events import append_event

    used = window.receipt["attempts"]
    record = {
        "outcome": outcome,
        "attempt": used,
        "model_call_id": (window.receipt.get("model_call_ids") or [None])[-1],
        **metadata,
    }
    window.receipt = {
        **window.receipt,
        "last_outcome": outcome,
        "repair_pending": used < 2
        and outcome in {"no_tool_call", "invalid_arguments"},
        "observations": [*window.receipt.get("observations", []), record][-4:],
    }
    await append_event(
        db,
        session_id=toolbox.session_id,
        run_id=toolbox.run_id,
        event_type=EventType.MODEL_RESPONSE_QUALITY,
        payload={"scope": "graph_review", "window_id": key, **record},
    )
    return used


async def reserve_review(db: Any, toolbox: Any, key: str, model_call_id: str) -> None:
    # Same Session lock as graph writes. Pending/unknown attempts count, including crashes.
    await lock_graph(db, toolbox.session_id, toolbox.settings.graph_sync_interval)
    await check_writer(db, toolbox)
    window = await db.get(
        SessionGraphReceiptRow, {"session_id": toolbox.session_id, "request_id": key}
    )
    if (
        window is None
        or window.receipt.get("status") != "active"
        or window.receipt["attempts"] >= 2
    ):
        raise GraphReviewExhausted("图核对请求额度耗尽")
    if not getattr(toolbox, "sdk_retries_disabled", False):
        raise GraphReviewExhausted("SDK隐藏重试未关闭，拒绝有限核对请求")
    window.receipt = {
        **window.receipt,
        "attempts": window.receipt["attempts"] + 1,
        "model_call_ids": [*window.receipt.get("model_call_ids", []), model_call_id],
    }
