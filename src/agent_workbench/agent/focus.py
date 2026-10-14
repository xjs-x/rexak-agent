"""焦点解析：程序维护可确定的执行上下文（§5.2）。

`current_focus` 表示**正在处理什么**。本模块是纯函数，由 `update_plan` 与
WorkingContext 共用同一套规则，避免两处推导出不同焦点。

规则：
1. 模型明确选择有效步骤/线索时采用（`source=explicit`）；
2. 没有明确选择，恰好一个 `in_progress` 步骤时承接该步骤，`source=plan_inferred`，
   reason 只写"由唯一进行中步骤承接"，**不伪造模型判断**；
   `lead_id` 只取该步骤已有绑定，**不从最近工具名猜测**；
3. 多个进行中步骤且旧焦点仍有效时保留；无有效旧焦点时保留 unresolved，
   **不任意选第一项**，并给一次简短提示；
4. 步骤 done/dropped/blocked 或被移除时，不继续沿用为当前执行焦点；
5. 没有计划的初始探索仍可执行 —— 不得为了让关联率达到 100% 而强造步骤。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent_workbench.agent.state import CurrentFocus

#: 自动推导的 reason 固定文案：只陈述执行上下文，不冒充模型判断。
INFERRED_REASON = "由唯一进行中步骤承接"

FOCUS_SOURCES = ("explicit", "plan_inferred")

_ACTIVE_STATUSES = frozenset({"pending", "in_progress"})


def _step_id(step: Mapping[str, Any]) -> str:
    return str(step.get("step_id") or "")


def focus_signature(focus: CurrentFocus | None) -> tuple[Any, ...]:
    """焦点的语义签名；用于判断"是否真的变了"，避免无意义地递增 revision。"""
    if not focus:
        return ()
    return (
        focus.get("step_id"),
        focus.get("lead_id"),
        str(focus.get("intent") or ""),
        str(focus.get("reason") or ""),
        focus.get("return_to_step_id"),
        focus.get("source"),
    )


def resolve_focus(
    plan: Sequence[Mapping[str, Any]] | None,
    previous_focus: CurrentFocus | None,
    explicit_focus: Mapping[str, Any] | None,
) -> tuple[CurrentFocus | None, str | None]:
    """解析当前焦点，返回 `(focus, hint)`。

    `hint` 非空表示需要给模型一次简短提示（例如多主线未指定焦点）。
    校验失败抛 `ValueError`，由工具层转成结构化反馈。
    """
    steps: list[Mapping[str, Any]] = list(plan or [])
    by_id = {_step_id(step): step for step in steps}

    if explicit_focus:
        step_id = explicit_focus.get("step_id")
        if step_id is not None and str(step_id) not in by_id:
            raise ValueError("焦点 step_id 不属于当前计划")
        if step_id is not None and by_id[str(step_id)].get("status") not in _ACTIVE_STATUSES:
            raise ValueError("焦点步骤已完成、已放弃或受阻，请先调整步骤状态")
        return_id = explicit_focus.get("return_to_step_id")
        if return_id is not None and str(return_id) not in by_id:
            raise ValueError("焦点 return_to_step_id 不属于当前计划")
        intent = str(explicit_focus.get("intent") or "").strip()
        reason = str(explicit_focus.get("reason") or "").strip()
        if not intent or not reason:
            raise ValueError("焦点必须说明行动目的与简短理由")
        return (
            CurrentFocus(
                step_id=str(step_id) if step_id is not None else None,
                lead_id=explicit_focus.get("lead_id"),
                intent=intent,
                reason=reason,
                return_to_step_id=explicit_focus.get("return_to_step_id"),
                source="explicit",
            ),
            None,
        )

    if previous_focus and previous_focus.get("source", "explicit") == "explicit":
        previous_id = previous_focus.get("step_id")
        if previous_id is None or (
            previous_id in by_id and by_id[previous_id].get("status") in _ACTIVE_STATUSES
        ):
            kept = dict(previous_focus)
            kept.setdefault("source", "explicit")
            if kept.get("return_to_step_id") not in by_id:
                kept["return_to_step_id"] = None
            return CurrentFocus(**kept), None  # type: ignore[typeddict-item]

    in_progress = [step for step in steps if step.get("status") == "in_progress"]
    if len(in_progress) == 1:
        step = in_progress[0]
        return (
            CurrentFocus(
                step_id=_step_id(step),
                # 只取该步骤已有绑定；不从最近工具名猜测线索。
                lead_id=step.get("lead_id"),
                intent=str(step.get("description") or _step_id(step)),
                reason=INFERRED_REASON,
                return_to_step_id=None,
                source="plan_inferred",
            ),
            None,
        )
    if len(in_progress) > 1:
        active_ids = {_step_id(step) for step in in_progress}
        if previous_focus and previous_focus.get("step_id") in active_ids:
            kept = dict(previous_focus)
            kept.setdefault("source", "explicit")
            return CurrentFocus(**kept), None  # type: ignore[typeddict-item]
        return None, (
            "存在多个进行中步骤，未自动选择焦点。"
            "请在 update_plan 的 focus_json 里明确当前主线（step_id、intent、reason）。"
        )
    # 零个进行中步骤：可能是初始探索，也可能是原焦点步骤已完成/被移除。
    # 两种情况都不继续沿用旧焦点，但也不强造步骤。
    return None, None


def active_step_ids(plan: Sequence[Mapping[str, Any]] | None) -> set[str]:
    return {_step_id(step) for step in (plan or []) if step.get("status") in _ACTIVE_STATUSES}


def assessment_applies(state: Mapping[str, Any]) -> bool:
    """最近判断只作为其绑定的当前焦点的下一步；历史判断仍保留。"""
    assessment = state.get("work_assessment") or {}
    focus = state.get("current_focus") or {}
    if not assessment:
        return False
    return all(assessment.get(key) == focus.get(key) for key in ("step_id", "lead_id"))


__all__ = [
    "FOCUS_SOURCES",
    "INFERRED_REASON",
    "active_step_ids",
    "focus_signature",
    "resolve_focus",
]
