"""不可变修订来源及已提交 checkpoint 的幂等事件投影；不是第二份计划。"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import select

from agent_workbench.contracts.events import EventType
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.evidence.redaction import redact_api_value
from agent_workbench.observability.events import append_event
from agent_workbench.persistence.tables import ArtifactRow, EventRow, SessionRow


async def persist_revision(
    box: Any, change: dict[str, Any], details: dict[str, Any]
) -> dict[str, Any]:
    from agent_workbench.tools.planning import store_revision_artifact

    reference = await store_revision_artifact(
        box,
        change["update_id"],
        {"schema_version": 1, "session_id": str(box.session_id), "change": change, **details},
    )
    return {**change, **reference}


async def project_committed_plan(database: Any, settings: Any, session_id: UUID, saved: Any) -> int:
    """只接受 Saver 读取到的 channel_values，不投影流式 updates 或 pending writes。

    修订链由已提交状态引用的不可变工件回溯。事件提交中断后，下一次读取或续跑补齐。
    """
    if saved is None:
        return 0
    change = saved.checkpoint.get("channel_values", {}).get("last_plan_change")
    if not change:
        return 0
    checkpoint_id = saved.checkpoint["id"]
    store = ArtifactStore(settings)
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    while change:
        identifier = str(change["update_id"])
        if identifier in seen:
            raise ValueError("计划修订来源链形成循环")
        seen.add(identifier)
        async with database.session() as db:
            existing = (
                await db.execute(select(EventRow).where(EventRow.event_id == UUID(identifier)))
            ).scalar_one_or_none()
            if existing is not None:
                if existing.session_id != session_id or existing.type != str(
                    EventType.PLAN_UPDATED
                ):
                    raise ValueError("修订事件身份冲突")
                break
            artifact = await db.get(ArtifactRow, UUID(change["artifact_id"]))
            if (
                artifact is None
                or artifact.session_id != session_id
                or artifact.availability != "available"
                or artifact.sha256 != change["sha256"]
                or not store.verify(artifact.relative_key, artifact.sha256)
            ):
                raise ValueError("已提交规划的修订来源工件不可用")
            body = json.loads(store.read_bytes(artifact.relative_key))
        if (
            body.get("session_id") != str(session_id)
            or body.get("change", {}).get("update_id") != identifier
        ):
            raise ValueError("修订来源与已提交状态不匹配")
        if {key: change[key] for key in body["change"]} != body["change"]:
            raise ValueError("修订元数据与来源工件不一致")
        chain.append(change)
        change = body.get("previous_change")
    count = 0
    for item in reversed(chain):
        async with database.transaction() as db:
            # 与 append_event 相同的锁顺序；锁内重查，避免并发补投影多分配序号。
            await db.execute(
                select(SessionRow).where(SessionRow.id == session_id).with_for_update()
            )
            if (
                await db.execute(
                    select(EventRow.event_id).where(EventRow.event_id == UUID(item["update_id"]))
                )
            ).scalar_one_or_none() is not None:
                continue
            await append_event(
                db,
                session_id=session_id,
                run_id=UUID(item["run_id"]),
                event_type=EventType.PLAN_UPDATED,
                event_id=UUID(item["update_id"]),
                payload=redact_api_value(
                    {
                        **item,
                        "source": "committed_langgraph_checkpoint",
                        "checkpoint_id": checkpoint_id,
                        "note": "checkpoint 是权威；事件为可重建投影",
                    }
                ),
            )
            count += 1
    return count
