"""可选轻量计划：局部 upsert、乐观并发与不可变修订来源。"""

from __future__ import annotations

import json
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent_workbench.agent.focus import focus_signature, resolve_focus
from agent_workbench.contracts.plan import PlanStepPatch
from agent_workbench.contracts.planning import PLAN_REVISION_SUMMARY, planning_digest
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import ArtifactRow, LeadRow
from agent_workbench.tools.execution_context import batch_tool_names


def normalize_steps(
    raw: Any, previous: list[dict[str, Any]], rationale: str | None = None
) -> list[dict[str, Any]]:
    """仅合并提交的步骤；未提交步骤和历史附加信息保持原样。"""
    _ = rationale
    if not isinstance(raw, list) or len(raw) > 20:
        raise ValueError("单次最多更新 20 个步骤")
    built = [dict(step) for step in previous]
    positions = {step["step_id"]: index for index, step in enumerate(built)}
    seen: set[str] = set()
    for item in raw:
        patch = item if isinstance(item, PlanStepPatch) else PlanStepPatch.model_validate(item)
        identifier = patch.step_id
        if identifier in seen:
            raise ValueError("同次更新不能重复 step_id")
        seen.add(identifier)
        fields = patch.model_dump(exclude_unset=True)
        for key in ("description", "status"):
            if key in fields and fields[key] is None:
                raise ValueError(f"{key} 不能为 null；省略即保留原值")
        if identifier in positions:
            built[positions[identifier]].update(fields)
        else:
            if not patch.description:
                raise ValueError("新步骤需要 description")
            built.append({"status": "pending", "rationale": None, "lead_id": None, **fields})
        if len(built) > 20:
            raise ValueError("当前计划最多保留 20 步；可修改或复用已有步骤")
    return built


async def apply_plan_update(
    runtime: Any,
    tool_call_id: str,
    *,
    steps: list[PlanStepPatch] | None = None,
    focus: str | None = None,
    rationale: str | None = None,
    expected_plan_revision: int,
) -> Command:
    state = runtime.state
    box = runtime.context.toolbox
    call_id = tool_call_id or str(runtime.tool_call_id or "")
    if not call_id:
        raise ValueError("计划更新需要稳定 tool_call_id")
    if batch_tool_names(dict(state)).count("update_plan") > 1:
        raise ValueError("同批多个 update_plan 在执行前全部拒绝，请合并为一次更新")
    patches = (
        [
            item.model_dump(exclude_unset=True) if isinstance(item, PlanStepPatch) else item
            for item in steps
        ]
        if steps is not None
        else None
    )
    request_digest = planning_digest(
        {
            "steps": patches,
            "focus": focus,
            "rationale": rationale,
            "expected_plan_revision": expected_plan_revision,
        }
    )
    update_id = str(uuid5(NAMESPACE_URL, f"rexak-plan:{box.session_id}:{box.run_id}:{call_id}"))
    latest = state.get("last_plan_change") or {}
    if latest.get("update_id") == update_id:
        if latest.get("request_digest") != request_digest:
            raise ValueError("同一计划调用的参数发生冲突")
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=json.dumps(
                            {"ok": True, "replayed": True, "plan_revision": latest["revision"]}
                        ),
                        tool_call_id=call_id,
                        name="update_plan",
                    )
                ]
            }
        )
    revision = int(state.get("plan_revision") or 0)
    if type(expected_plan_revision) is not int or expected_plan_revision != revision:
        raise ValueError(
            f"计划版本已过期：提交 {expected_plan_revision}，当前 {revision}；请读取最新状态"
        )
    reader = getattr(box, "planning_head_reader", None)

    async def validate_boundary() -> None:
        check = getattr(box, "assert_control_current", None)
        if check is not None:
            await check()
        if reader is not None:
            head = await reader()
            if int(head.get("plan_revision") or 0) != revision or (
                head.get("last_plan_change") or {}
            ).get("update_id") != latest.get("update_id"):
                raise ValueError("计划快照已过期，请读取已提交状态重新决策")

    await validate_boundary()
    if rationale is not None and (not rationale.strip() or len(rationale) > 500):
        raise ValueError("rationale 必须为 1–500 字的简短理由")
    if focus is not None and len(focus) > 500:
        raise ValueError("focus 最多 500 字；空字符串表示清除")
    previous = list(state.get("plan") or [])
    updated = normalize_steps(patches, previous) if patches is not None else previous
    # 关联可选，但填写的关联不能伪造其他会话的领域记录。
    async with box.db.session() as db:
        for patch in patches or []:
            if patch.get("lead_id"):
                lead = await db.get(LeadRow, UUID(patch["lead_id"]))
                if lead is None or lead.session_id != box.session_id:
                    raise ValueError("关联线索不属于当前会话")
    resolved, _ = resolve_focus(updated, state.get("current_focus"), None)
    if focus is not None:
        resolved = (
            {
                "step_id": None,
                "lead_id": None,
                "intent": focus.strip(),
                "reason": "",
                "return_to_step_id": None,
                "source": "explicit",
            }
            if focus.strip()
            else None
        )
    plan_changed = planning_digest(updated) != planning_digest(previous)
    focus_changed = focus_signature(resolved) != focus_signature(state.get("current_focus"))
    changed = plan_changed or focus_changed
    update: dict[str, Any] = {}
    if changed:
        from agent_workbench.tools.plan_revisions import persist_revision

        old = {step["step_id"]: step for step in previous}
        new = {step["step_id"]: step for step in updated}
        change = {
            "update_id": update_id,
            "request_digest": request_digest,
            "revision": revision + 1,
            "previous_revision": revision,
            "rationale": rationale or "更新可选工作记录",
            "trigger": "plan_adjustment",
            "changed_step_ids": [key for key in new if old.get(key) != new[key]],
            "added_step_ids": [key for key in new if key not in old],
            "removed_step_ids": [],
            "order_changed": False,
            "run_id": str(box.run_id),
            "source_tool_call_id": call_id,
        }
        update.update(plan=updated, current_focus=resolved, plan_revision=revision + 1)
        update["last_plan_change"] = await persist_revision(
            box,
            change,
            {
                "previous_change": latest or None,
                "previous_plan": previous,
                "plan": updated,
                "previous_focus": state.get("current_focus"),
                "focus": resolved,
            },
        )
    receipt = {
        "ok": True,
        "plan_revision": revision + int(changed),
        "plan_changed": plan_changed,
        "focus_changed": focus_changed,
        "current_focus": resolved,
        "steps": len(updated),
        "note": "工作记录以 checkpoint 提交为准；步骤完成不代表漏洞已验证。",
    }
    update["messages"] = [
        ToolMessage(
            content=json.dumps(receipt, ensure_ascii=False),
            tool_call_id=call_id,
            name="update_plan",
        )
    ]
    await validate_boundary()
    return Command(update=update)


async def store_revision_artifact(box: Any, update_id: str, body: dict[str, Any]) -> dict[str, str]:
    """先存不可变候选工件；只有 checkpoint 引用它时才承认修订生效。"""
    identifier = uuid5(NAMESPACE_URL, f"rexak-plan-artifact:{box.session_id}:{update_id}")
    payload = (json.dumps(body, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    stored = box.artifacts.write_bytes(payload, media_type="application/json")
    async with box.db.transaction() as db:
        existing = await db.get(ArtifactRow, identifier)
        if existing is not None:
            if existing.sha256 != stored.sha256:
                raise ValueError("同一规划更新的工件内容冲突，拒绝覆盖")
        else:
            row = await ArtifactRepository(db).create(
                artifact_id=identifier,
                sha256=stored.sha256,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
                relative_key=stored.relative_key,
                session_id=box.session_id,
                source_operation_id=None,
                sensitivity="internal",
                availability="available",
                captured_at=stored.captured_at,
                summary=PLAN_REVISION_SUMMARY,
            )
            row.protected = True
    return {"artifact_id": str(identifier), "sha256": stored.sha256}
