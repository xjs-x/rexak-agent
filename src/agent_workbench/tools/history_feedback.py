"""已验证观察的有界读取及投递收据；不是新事实库，不等待 Shadow 工作线程。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.messages import ToolMessage
from sqlalchemy import select

from agent_workbench.domain.history_action_reflection import digest
from agent_workbench.domain.passive_reflection import reflection_view
from agent_workbench.domain.shadow_authorization import load_authorization
from agent_workbench.persistence.tables import ArtifactRow, ToolJobRow, ToolOperationRow
from agent_workbench.tools.shadow_storage import ShadowStore


def read_bounded(path: Path, size: int) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(size + 1)
    if len(raw) > size:
        raise ValueError("feedback_record_size_limit")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("feedback_record_not_object")
    return value


async def pending_reference(box: Any, thread: str, messages: list) -> tuple[dict, Path] | None:
    root = Path(box.settings.data_root) / "history_action_shadow" / str(box.session_id)
    directory = root / "observations"
    if not directory.exists():
        return None
    visible_calls = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    # 本 MVP 只投递具有可重新核验的固定配方授权的提示；旧逐调用批准仍仅 Shadow。
    policy = load_authorization(box)
    paths = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for path in paths[: box.settings.history_action_shadow_history_limit]:
        raw = read_bounded(path, box.settings.history_action_shadow_record_bytes)
        summary = raw.get("predecision_summary", {})
        if (summary.get("run_id"), summary.get("session_id"), summary.get("thread_id")) != (
            str(box.run_id),
            str(box.session_id),
            thread,
        ):
            continue
        if raw.get("candidate_tool_call_id") not in visible_calls or not raw.get(
            "emit_shadow_event"
        ):
            continue
        value = reflection_view(raw)
        if value is None:
            continue
        key = digest({"run": str(box.run_id), "thread": thread, "material": value["material_key"]})
        receipt = root / "feedback" / (key + ".json")
        if receipt.exists():
            continue
        operation = UUID(raw["snapshot_id"].removeprefix("http-shadow:"))
        capture = read_bounded(
            root / "captures" / (str(operation) + ".json"),
            box.settings.history_action_shadow_record_bytes,
        )
        if (capture.get("run_id"), capture.get("session_id"), capture.get("thread_id")) != (
            str(box.run_id),
            str(box.session_id),
            thread,
        ):
            continue
        if capture.get("trusted_task", {}).get("authorization_sha256") != digest(
            policy.model_dump()
        ):
            continue
        operations = {
            operation,
            *(UUID(r.partition(":")[2]) for r in value["historical_operations"]),
        }
        artifacts = {
            UUID(r.partition(":")[2])
            for r in value["verified_sources_at_detection"]
            if r.startswith("artifact:")
        }
        if not artifacts:
            continue
        async with box.db.session() as db:
            rows = list(
                (
                    await db.scalars(
                        select(ToolOperationRow).where(
                            ToolOperationRow.operation_id.in_(operations),
                            ToolOperationRow.run_id == box.run_id,
                            ToolOperationRow.session_id == box.session_id,
                        )
                    )
                ).all()
            )
            if len(rows) != len(operations) or any(
                r.dispatch_control_version != box.control_version
                or r.scope_revision != box.scope_revision
                or r.policy_revision != box.policy_revision
                or r.status not in {"succeeded", "failed"}
                for r in rows
            ):
                continue
            jobs = list(
                (
                    await db.scalars(
                        select(ToolJobRow).where(ToolJobRow.operation_id.in_(operations))
                    )
                ).all()
            )
            if len(jobs) != len(operations) or any(
                j.state not in {"succeeded", "failed"} or j.sync_status != "in_sync" for j in jobs
            ):
                continue
            sources = list(
                (
                    await db.scalars(
                        select(ArtifactRow).where(
                            ArtifactRow.id.in_(artifacts),
                            ArtifactRow.session_id == box.session_id,
                            ArtifactRow.source_operation_id.in_(operations),
                        )
                    )
                ).all()
            )
            if (
                len(sources) != len(artifacts)
                or sum(a.byte_size for a in sources)
                > box.settings.history_action_shadow_input_bytes
                or any(
                    a.availability != "available"
                    or a.truncated
                    or a.sensitivity == "secret"
                    or a.byte_size > box.settings.history_action_shadow_input_bytes
                    or not box.artifacts.verify(a.relative_key, a.sha256)
                    for a in sources
                )
            ):
                continue
        return value, receipt
    return None


def claim_reference(box: Any, thread: str, value: dict, path: Path) -> bool:
    root = Path(box.settings.data_root) / "history_action_shadow" / str(box.session_id)
    return ShadowStore(root, box.settings).write(
        path,
        {
            "schema": "passive-feedback-receipt/1",
            "run_id": str(box.run_id),
            "thread_id": thread,
            "material_key": value["material_key"],
            "status": "claimed_before_normal_model_request_may_be_lost_on_failure",
        },
    )
