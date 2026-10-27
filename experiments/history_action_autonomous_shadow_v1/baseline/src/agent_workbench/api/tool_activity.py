"""工具活动只读投影：展示事件与执行台账关联；旧调用按需读取官方 checkpoint。"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on

from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.tool_activity import ToolActivityDetail, ToolActivityPage
from agent_workbench.evidence.redaction import redact_api_value
from agent_workbench.observability.tool_activity import visible_text
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.db import get_database
from agent_workbench.persistence.tables import (
    EventRow,
    RunRow,
    SessionRow,
    ToolJobRow,
    ToolOperationRow,
)

router = APIRouter(tags=["read"])


async def require_task(session_id: UUID, request: Request) -> Any:
    from agent_workbench.api.routes import _require_session_row, get_auth

    get_auth().require_session(request)
    async with get_database().session() as db:
        await _require_session_row(db, session_id)
        task = await db.get(SessionRow, session_id)
        assert task is not None
        return task


def latest_trace(session_id: UUID, event_type: str):
    call_id = EventRow.payload["tool_call_id"].astext
    return (
        select(EventRow)
        .where(
            EventRow.session_id == session_id,
            EventRow.type == event_type,
            EventRow.payload["scope"].astext == "tool_activity",
        )
        .ext(distinct_on(EventRow.run_id, call_id))
        .order_by(EventRow.run_id, call_id, EventRow.session_seq.desc())
    )


@router.get("/sessions/{session_id}/tool-activity", response_model=ToolActivityPage)
async def activities(
    session_id: UUID,
    _task: Annotated[Any, Depends(require_task)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=2000),
) -> dict[str, Any]:
    needed = offset + limit + 1
    async with get_database().session() as db:
        starts_query = latest_trace(session_id, "tool.accepted").subquery()
        from sqlalchemy.orm import aliased

        start_row = aliased(EventRow, starts_query)
        starts = list(
            (
                await db.execute(
                    select(start_row).order_by(start_row.occurred_at.desc()).limit(needed)
                )
            ).scalars()
        )
        operations = list(
            (
                await db.execute(
                    select(ToolOperationRow, ToolJobRow)
                    .outerjoin(ToolJobRow, ToolOperationRow.job_id == ToolJobRow.job_id)
                    .where(ToolOperationRow.session_id == session_id)
                    .order_by(ToolOperationRow.created_at.desc())
                    .limit(needed)
                )
            ).all()
        )
        keys = {(e.run_id, str(e.payload["tool_call_id"])) for e in starts}
        ends = (
            list(
                (
                    await db.execute(
                        latest_trace(session_id, "tool.completed").where(
                            EventRow.run_id.in_({key[0] for key in keys}),
                            EventRow.payload["tool_call_id"].astext.in_({key[1] for key in keys}),
                        )
                    )
                ).scalars()
            )
            if keys
            else []
        )
    items: dict[tuple[Any, str], dict[str, Any]] = {}
    for op, job in operations:
        display_status = op.status
        finished_at = op.updated_at if op.status not in {"accepted", "running"} else None
        if job and op.status in {"accepted", "running"}:
            display_status = job.state if job.sync_status == "in_sync" else "unknown"
            finished_at = job.executor_finished_at
        items[(op.run_id, op.tool_call_id)] = {
            "run_id": op.run_id,
            "tool_call_id": op.tool_call_id,
            "tool_name": op.tool_name,
            "status": display_status,
            "started_at": op.created_at,
            "finished_at": finished_at,
            "operation_id": op.operation_id,
            "job_id": op.job_id,
            "job_state": job.state if job else None,
            "sync_status": job.sync_status if job else None,
        }
    finishes = {(e.run_id, str(e.payload["tool_call_id"])): e for e in ends}
    for e in starts:
        key = (e.run_id, str(e.payload["tool_call_id"]))
        end = finishes.get(key)
        # 重放后的新开始不能配对上一轮的结束事件。
        if end and end.session_seq < e.session_seq:
            end = None
        item = items.get(
            key, {"operation_id": None, "job_id": None, "job_state": None, "sync_status": None}
        )
        item.update(
            {
                "run_id": e.run_id,
                "tool_call_id": key[1],
                "tool_name": e.payload["tool"],
                "status": end.payload.get("status", "completed") if end else "preparing",
                "started_at": e.occurred_at,
                "finished_at": end.occurred_at if end else None,
            }
        )
        if item["status"] == "accepted" and item["job_state"]:
            item["status"] = item["job_state"] if item["sync_status"] == "in_sync" else "unknown"
            operation_job = next(
                (job for op, job in operations if (op.run_id, op.tool_call_id) == key), None
            )
            item["finished_at"] = operation_job.executor_finished_at if operation_job else None
        items[key] = item
    ordered = sorted(
        items.values(),
        key=lambda item: (item["started_at"], str(item["run_id"]), item["tool_call_id"]),
        reverse=True,
    )
    return {
        "activities": ordered[offset : offset + limit],
        "next_offset": offset + limit if len(ordered) > offset + limit else None,
    }


@router.get(
    "/sessions/{session_id}/tool-activity/{run_id}/{tool_call_id}",
    response_model=ToolActivityDetail,
)
async def activity_detail(
    session_id: UUID,
    run_id: UUID,
    tool_call_id: str,
    task: Annotated[Any, Depends(require_task)],
) -> dict[str, Any]:
    async with get_database().session() as db:
        run = await db.get(RunRow, run_id)
        if run is None or run.session_id != session_id:
            raise ApiError(ErrorCode.NOT_FOUND, "该任务没有此工具调用")
        operation = (
            await db.execute(
                select(ToolOperationRow).where(
                    ToolOperationRow.run_id == run_id,
                    ToolOperationRow.session_id == session_id,
                    ToolOperationRow.tool_call_id == tool_call_id,
                )
            )
        ).scalar_one_or_none()
        trace = list(
            (
                await db.execute(
                    select(EventRow)
                    .where(
                        EventRow.session_id == session_id,
                        EventRow.run_id == run_id,
                        EventRow.payload["scope"].astext == "tool_activity",
                        EventRow.payload["tool_call_id"].astext == tool_call_id,
                    )
                    .order_by(EventRow.session_seq.desc())
                    .limit(4)
                )
            ).scalars()
        )
        if trace:
            start = next((e for e in trace if e.type == "tool.accepted"), None)
            end = next((e for e in trace if e.type == "tool.completed"), None)
            if end and start and end.session_seq < start.session_seq:
                end = None
            return {
                "request_text": start.payload.get("request_text", "")
                if start
                else "参数展示事件已过期",
                "result_text": end.payload.get("result_text", "") if end else "尚未返回结果",
                "note": "展示内容已脱敏并限制长度；远端工作状态以执行台账为准。",
                "execution_context": redact_api_value(operation.execution_context)
                if operation
                else None,
            }
    if operation is None:
        raise ApiError(ErrorCode.NOT_FOUND, "该任务没有此工具调用")
    # 仅展开旧记录时读取官方 checkpoint；不将历史参数复制到另一份可写状态。
    async with open_checkpointer_connection(get_database().settings) as connection:
        checkpoint = await AsyncPostgresSaver(connection).aget_tuple(
            {"configurable": {"thread_id": task.thread_id}}
        )
    messages = (
        checkpoint.checkpoint.get("channel_values", {}).get("messages", []) if checkpoint else []
    )
    args = None
    result = None
    for message in messages:
        for call in getattr(message, "tool_calls", []):
            if call.get("id") == tool_call_id:
                args = call.get("args", {})
        if getattr(message, "tool_call_id", None) == tool_call_id:
            result = getattr(message, "content", "")
    return {
        "request_text": visible_text(args)
        if args is not None
        else "旧参数已被上下文整理移出，无法还原。",
        "result_text": visible_text(result)
        if result is not None
        else "官方历史中没有可用结果，请结合执行台账。",
        "note": "来自官方 checkpoint 的历史只读展示，已脱敏并限制长度。",
        "execution_context": redact_api_value(operation.execution_context),
    }
