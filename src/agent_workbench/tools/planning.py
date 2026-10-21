"""update_plan 的单一实现：校验旧快照，再一次提交图更新。"""

from __future__ import annotations

import json
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from langchain_core.messages import ToolMessage
from langgraph.types import Command

from agent_workbench.agent.focus import focus_signature, resolve_focus
from agent_workbench.contracts.planning import (
    PLAN_REVISION_SUMMARY,
    AssessmentInput,
    PlanningSource,
    PlanningValidationError,
    StepCompletionSpec,
    planning_digest,
)
from agent_workbench.domain.planning import completion_is_current, resolve_sources
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import ArtifactRow, LeadRow
from agent_workbench.tools.execution_context import batch_tool_names
from agent_workbench.tools.planning_validation import validate_plan_steps


def parse_object(raw: Any, label: str) -> Any:
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} 不是完整 JSON") from exc


def normalize_steps(
    raw: Any, previous: list[dict[str, Any]], rationale: str | None
) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > 20:
        raise ValueError("计划必须是最多 20 步的数组")
    old = {step["step_id"]: step for step in previous}
    built: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed = {
        "step_id",
        "description",
        "status",
        "kind",
        "rationale",
        "lead_id",
        "lead_ids",
        "done_when",
        "resume_note",
        "evidence_refs",
        "completion_spec",
        "completion",
    }
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError("步骤必须为对象且不能包含未实现的字段；第一轮不支持 depends_on")
        identifier = str(item.get("step_id") or f"s{index}").strip()
        carried = {
            key
            for key in ("completion_protocol", "legacy_completion")
            if key in old.get(identifier, {}) and item.get(key) == old[identifier][key]
        }
        if set(item) - allowed - carried:
            raise ValueError("步骤包含未实现字段或被修改的程序字段；第一轮不支持 depends_on")
        if not identifier or len(identifier) > 128 or identifier in seen:
            raise ValueError("step_id 为空、过长或重复")
        seen.add(identifier)
        step = {**old.get(identifier, {}), **item, "step_id": identifier, "completion_protocol": 1}
        step.setdefault("status", "pending")
        step.setdefault("kind", "mixed")
        step.setdefault("rationale", None)
        step.setdefault("lead_id", None)
        for key in ("description", "done_when", "resume_note", "rationale"):
            value = step.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 500):
                raise ValueError(f"{key} 必须是最多 500 字的文本")
        if not str(step.get("description") or "").strip():
            raise ValueError("步骤缺少 description")
        if step["status"] not in {"pending", "in_progress", "done", "dropped", "blocked"}:
            raise ValueError("步骤 status 非法")
        if step["kind"] not in {"coverage", "followup", "mixed"}:
            raise ValueError("步骤 kind 非法")
        if (
            step["status"] in {"blocked", "dropped"}
            and not str(step.get("rationale") or "").strip()
        ):
            raise ValueError("blocked/dropped 必须说明受阻或放弃原因 rationale")
        ids = step.get("lead_ids") or ([step["lead_id"]] if step.get("lead_id") else [])
        if (
            not isinstance(ids, list)
            or len(ids) > 20
            or any(not isinstance(value, str) for value in ids)
        ):
            raise ValueError("lead_ids 必须是最多 20 项的 ID 数组")
        if step.get("lead_id") and ids and step["lead_id"] not in ids:
            raise ValueError("lead_id 与 lead_ids 冲突")
        step["lead_ids"] = list(dict.fromkeys(ids))
        step["lead_id"] = step.get("lead_id") or (ids[0] if ids else None)
        refs = step.get("evidence_refs") or []
        if not isinstance(refs, list) or len(refs) > 8:
            raise ValueError("步骤最多引用 8 个工件")
        step["evidence_refs"] = refs
        if step.get("completion_spec") is not None:
            spec = StepCompletionSpec.model_validate(step["completion_spec"])
            step["completion_spec"] = spec.model_dump(mode="json")
            if "done_when" in item and item["done_when"] != spec.done_when:
                raise ValueError("done_when 与 completion_spec 冲突")
            step["done_when"] = spec.done_when
        if step["status"] != "done":
            step.pop("completion", None)
            step.pop("legacy_completion", None)
        built.append(step)
    removed = [step for step in previous if step["step_id"] not in seen]
    if any(step.get("status") not in {"done", "dropped"} for step in removed):
        raise ValueError("未完成步骤不能直接删除；请先明确标记 dropped 并说明处置理由")
    if removed and not rationale:
        raise ValueError("归档旧步骤必须说明 rationale；关联领域未决项不会被删除")
    return built


async def apply_plan_update(
    runtime: Any,
    tool_call_id: str,
    *,
    steps_json: str | None,
    rationale: str | None,
    focus_json: str | None,
    assessment_json: str | None,
    expected_plan_revision: int | None,
) -> Command:
    state = runtime.state
    box = runtime.context.toolbox
    call_id = tool_call_id or str(runtime.tool_call_id or "")
    if not call_id:
        raise ValueError("计划更新需要稳定 tool_call_id")
    if batch_tool_names(dict(state)).count("update_plan") > 1:
        raise ValueError("同批多个 update_plan 在执行前全部拒绝，请合并为一次更新")
    request = {
        "steps": parse_object(steps_json, "steps_json"),
        "focus": parse_object(focus_json, "focus_json"),
        "assessment": parse_object(assessment_json, "assessment_json"),
        "rationale": rationale,
        "expected_plan_revision": expected_plan_revision,
    }
    request_digest = planning_digest(request)
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
    if expected_plan_revision is None and (
        revision or int(state.get("state_schema_version") or 0) >= 6
    ):
        raise ValueError(f"必须提供 expected_plan_revision，当前为 {revision}")
    if expected_plan_revision is not None and expected_plan_revision != revision:
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
                raise ValueError("当前判断的规划快照已过期，请读取已提交状态重新决策")

    await validate_boundary()
    if rationale is not None and (not rationale.strip() or len(rationale) > 500):
        raise ValueError("rationale 必须为 1–500 字的简短理由")
    previous = list(state.get("plan") or [])
    steps = (
        normalize_steps(request["steps"], previous, rationale)
        if steps_json is not None
        else previous
    )
    old_ids = {step["step_id"] for step in previous}
    explicit = request["focus"]
    if explicit is not None and not isinstance(explicit, dict):
        raise ValueError("focus_json 必须是对象")
    if explicit is not None:
        if set(explicit) - {"step_id", "lead_id", "intent", "reason", "return_to_step_id"}:
            raise ValueError("焦点包含未声明字段，source 由程序维护")
        for field in ("intent", "reason"):
            value = explicit.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError("焦点 intent/reason 必须为 1–500 字的简短文本")
    assessment = None
    async with box.db.session() as db:
        retained_ids = {step["step_id"] for step in steps}
        for retired in previous:
            if retired["step_id"] not in retained_ids and retired.get("status") == "done":
                issue = await completion_is_current(
                    db, box.artifacts, box.session_id, retired, run_id=box.run_id
                )
                if issue:
                    raise ValueError(f"旧完成步骤不能未经复查直接归档：{issue}")
        await validate_plan_steps(db, box, previous, steps, request["steps"])
        if explicit and explicit.get("lead_id"):
            lead = await db.get(LeadRow, UUID(explicit["lead_id"]))
            if lead is None or lead.session_id != box.session_id:
                raise ValueError("焦点线索不属于当前会话")
        if request["assessment"] is not None:
            raw_assessment = dict(request["assessment"])
            raw_assessment.setdefault("evaluated_plan_revision", revision)
            old_focus_id = (state.get("current_focus") or {}).get("step_id")
            raw_assessment.setdefault(
                "evaluated_step_ids", [old_focus_id] if old_focus_id in old_ids else []
            )
            raw_assessment.setdefault(
                "trigger",
                "new_evidence"
                if raw_assessment.get("basis_refs") or raw_assessment.get("evidence_refs")
                else "plan_adjustment",
            )
            parsed = AssessmentInput.model_validate(raw_assessment)
            if parsed.evaluated_plan_revision != revision:
                raise ValueError("判断必须针对更新前的计划版本")
            if set(parsed.evaluated_step_ids) - old_ids:
                raise ValueError("被评估步骤不属于更新前的计划，不能把旧结果挂到新任务")
            refs = [
                *parsed.basis_refs,
                *[
                    PlanningSource(kind="artifact", id=UUID(value))
                    for value in parsed.evidence_refs
                ],
            ]
            if parsed.trigger in {"new_evidence", "condition_changed", "tool_failure"} and not refs:
                raise ValueError("证据或执行变化判断必须引用实际来源")
            resolved = await resolve_sources(
                db,
                box.artifacts,
                box.session_id,
                refs,
                step_ids=set(parsed.evaluated_step_ids) if parsed.evaluated_step_ids else None,
            )
            old_focus = state.get("current_focus") or {}
            assessment = {
                **parsed.model_dump(mode="json"),
                "basis_refs": [ref.model_dump(mode="json") for ref in resolved["normalized_refs"]],
                "step_id": parsed.evaluated_step_ids[0] if parsed.evaluated_step_ids else None,
                "lead_id": old_focus.get("lead_id")
                if old_focus.get("step_id") in parsed.evaluated_step_ids
                else None,
                "source_tool_call_id": call_id,
                "source_signature": resolved["signature"],
                "revision": int((state.get("work_assessment") or {}).get("revision") or 0) + 1,
            }
    focus, hint = resolve_focus(steps, state.get("current_focus"), explicit)
    plan_changed = planning_digest(steps) != planning_digest(previous)
    focus_changed = focus_signature(focus) != focus_signature(state.get("current_focus"))
    old = {step["step_id"]: step for step in previous}
    new = {step["step_id"]: step for step in steps}
    old_focus = state.get("current_focus") or {}
    key_change = bool(previous) and (
        bool(new.keys() - old.keys())
        or any(
            step.get("status") in {"blocked", "dropped"}
            and step.get("status") != old.get(step["step_id"], {}).get("status")
            for step in steps
        )
        or any(
            (old.get(identifier, {}).get("completion_spec") or {}).get("http_checks")
            and old[identifier].get("completion_spec") != value.get("completion_spec")
            for identifier, value in new.items()
        )
        or bool(
            explicit
            and old_focus.get("step_id")
            and focus
            and focus.get("step_id") != old_focus.get("step_id")
            and old.get(old_focus["step_id"], {}).get("status") not in {"done", "dropped"}
        )
    )
    if key_change and assessment is None:
        raise PlanningValidationError(
            "assessment_required",
            "关键计划修订需要简短判断；普通工具调用与单纯完成登记不要求填写",
            recovery="在本次 update_plan 加 assessment_json={changed,next_action,decision,basis_refs}。旧焦点和旧版本由程序补齐；纯计划调整允许 basis_refs=[]。保留已有来源，不要重新执行工具。",
            evaluated_plan_revision=revision,
            evaluated_step_ids=[old_focus["step_id"]]
            if old_focus.get("step_id") in old_ids
            else [],
        )
    update: dict[str, Any] = {"current_focus": focus}
    if plan_changed:
        if previous and not rationale:
            raise ValueError("计划修订必须提供 rationale")
        update.update(plan=steps, plan_revision=revision + 1)
    if assessment is not None:
        assessment["next_focus"] = focus
        assessment["resulting_plan_revision"] = revision + int(plan_changed)
        update["work_assessment"] = assessment
    if plan_changed or focus_changed or assessment is not None:
        from agent_workbench.tools.plan_revisions import persist_revision

        changed = [
            identifier
            for identifier in dict.fromkeys([*old, *new])
            if old.get(identifier) != new.get(identifier)
        ]
        order_changed = [key for key in old if key in new] != [key for key in new if key in old]
        change = {
            "update_id": update_id,
            "request_digest": request_digest,
            "revision": revision + int(plan_changed),
            "previous_revision": revision,
            "rationale": rationale or "记录当前焦点或判断",
            "trigger": (assessment or {}).get("trigger", "plan_adjustment"),
            "changed_step_ids": changed,
            "added_step_ids": [key for key in new if key not in old],
            "removed_step_ids": [key for key in old if key not in new],
            "order_changed": order_changed,
            "run_id": str(box.run_id),
            "assessment_revision": (assessment or {}).get("revision"),
            "source_tool_call_id": call_id,
        }
        update["last_plan_change"] = await persist_revision(
            box,
            change,
            {
                "previous_change": latest or None,
                "previous_plan": previous,
                "plan": steps,
                "assessment": assessment,
                "previous_focus": state.get("current_focus"),
                "focus": focus,
            },
        )
    receipt = {
        "ok": True,
        "plan_revision": update.get("plan_revision", revision),
        "plan_changed": plan_changed,
        "focus_changed": focus_changed,
        "current_focus": focus,
        "focus_hint": hint,
        "steps": len(steps),
        "assessment_revision": (assessment or {}).get("revision"),
        "completion_checks": [
            {
                "step_id": step["step_id"],
                **{
                    key: (step.get("completion") or {}).get("check", {}).get(key)
                    for key in (
                        "source_authenticity",
                        "source_applicability",
                        "sufficiency",
                        "source_links",
                        "http_coverage",
                        "limitations",
                    )
                },
            }
            for step in steps
            if step.get("status") == "done"
        ],
        "note": "仅提交图更新；计划生效以已提交 checkpoint 为准。新焦点的执行请放到下一批。",
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
