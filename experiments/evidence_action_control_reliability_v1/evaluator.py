"""第二轮纯离线观察：复用原验证与条件比较，补充工具和任务相关语义。"""

from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlsplit

from experiments.evidence_action_control_v1 import evaluator as base
from experiments.evidence_action_control_v1.contracts import (
    ActionObservation,
    ActionSnapshot,
    AvailableEvidence,
    EquivalenceAssessment,
    GainAssessment,
    RepeatRisk,
)
from pydantic import ValidationError


def _raw_visible(item: AvailableEvidence) -> bool:
    return bool(
        item.available
        and item.visible
        and item.complete
        and item.provenance_verified
        and item.source_refs
        and item.visibility_refs
        and item.content is not None
        and item.sha256
        and hashlib.sha256(item.content.encode()).hexdigest() == item.sha256
    )


def _focus_actions(snapshot):
    result = []
    for history in snapshot.historical_actions:
        views = [history.conditions]
        views.extend(
            e.conditions
            for e in snapshot.evidence
            if e.operation_ref == history.record_ref and e.visible and e.provenance_verified
        )
        for view in views:
            normalized = base._conditions(view)
            focus = base._conditions(snapshot.task.focus_conditions)
            keys = [
                k
                for k in ("route", "environment", "configuration", "target")
                if focus[k] is not None
            ]
            if (
                keys
                and any(normalized[k] is not None for k in keys)
                and not any(normalized[k] is not None and normalized[k] != focus[k] for k in keys)
            ):
                result.append(history)
                break
    return result


def _read_relation(snapshot, item):
    if base._read_relevant(snapshot, item):
        return True
    focus = base._conditions(snapshot.task.focus_conditions)
    views = [item.conditions] + [
        h.conditions
        for h in snapshot.historical_actions
        if h.record_ref == item.operation_ref or item.record_ref in h.artifact_refs
    ]
    if any(
        any(
            base._conditions(v)[k] is not None
            and focus[k] is not None
            and base._conditions(v)[k] != focus[k]
            for k in ("route", "environment", "target")
        )
        for v in views
    ):
        return False
    refs = set(snapshot.task.source_refs)
    refs.update(h.record_ref for h in _focus_actions(snapshot))
    if refs.intersection(item.conditions.get("relevance_refs") or []):
        return True
    return None


def _relation(snapshot):
    old = base._conditions(snapshot.task.focus_conditions)
    new = base._conditions(snapshot.candidate.conditions)
    known = [
        k
        for k in ("route", "environment", "configuration", "target")
        if old[k] is not None and new[k] is not None
    ]
    if any(old[k] != new[k] for k in known):
        return False
    return True if known else None


def _candidate_conflicts(snapshot):
    """只检查明确字段冲突，不推测工具参数与抽象条件之间未声明的映射。"""
    declared = base._conditions(snapshot.candidate.conditions)
    arguments = base._conditions(snapshot.candidate.arguments)
    conflicts = [
        k
        for k in declared
        if declared[k] is not None and arguments[k] is not None and declared[k] != arguments[k]
    ]
    url = snapshot.candidate.arguments.get("url")
    if isinstance(url, str) and declared["entry"] is not None:
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme in {"http", "https"}
                and parsed.netloc
                and (parsed.path or "/") != declared["entry"]
            ):
                conflicts.append("entry_vs_argument_url_path")
        except ValueError:
            conflicts.append("invalid_argument_url")
    return conflicts


def _equivalence(label, reason, *, confidence="MEDIUM", unknowns=None):
    return EquivalenceAssessment(
        classification=label,
        logical_classification=label,
        confidence=confidence,
        unknown_dimensions=unknowns or [],
        classification_reason=reason,
    )


def evaluate(snapshot: ActionSnapshot) -> ActionObservation:
    result = base.evaluate(snapshot)
    candidate = snapshot.candidate
    eq, risk, gain = result.equivalence, result.repeat_risk, result.information_gain
    sufficient = result.evidence_sufficiency.status == "SUFFICIENT"
    value, reason = gain.classification, gain.classification_reason
    uncertainties = list(gain.uncertainties)
    expected = list(gain.expected_new_information)
    cost = {**candidate.estimated_cost}
    decision_refs = set(gain.supporting_evidence_refs)
    supported_nonexecuting = candidate.name in {
        "get_graph",
        "update_graph",
        "read_artifact",
        "read_local_execution",
        "search_history",
    }

    if candidate.name == "get_graph":
        context = snapshot.boundary.get("graph_context") or {}
        current, seen = context.get("revision"), context.get("visible_revision")
        verified = context.get("provenance_verified") is True and bool(context.get("source_refs"))
        if verified:
            decision_refs.update(context["source_refs"])
        eq = _equivalence(
            "UNRELATED_ACTION",
            "读图是导航/关系读取，不是历史效果测试；此标签比较行动类型，不表示当前任务无关。",
        )
        if verified and type(current) is int and type(seen) is int:
            if current == seen:
                eq = _equivalence(
                    "SEMANTIC_REPEAT", "已见相同图修订；未声明不同页、过滤器或读取范围。"
                )
                previous_args = {"include_pending": True, **context.get("visible_arguments", {})}
                current_args = {"include_pending": True, **candidate.arguments}
                if current_args != previous_args:
                    eq = _equivalence(
                        "INSUFFICIENT_INFORMATION",
                        "修订相同但读取范围不同或缺失，无法证明是同一视图。",
                    )
                    value, reason = "UNCERTAIN", "不同图视图可能有导航价值，缺少范围包含关系。"
                else:
                    value, reason = (
                        "LOW",
                        "相同图版本和范围已见，未记录新图信息；不是外部重复执行风险。",
                    )
            else:
                eq = _equivalence(
                    "CONDITION_CHANGED",
                    "当前已知图修订与上次可见图不同，存在可追溯版本增量；不证明新增事实为真。",
                )
                value, reason = (
                    ("UNCERTAIN" if sufficient else "POTENTIAL"),
                    "图版本有变化，但新视图是否回答当前未决问题尚未观测。",
                )
        else:
            value, reason = (
                ("UNCERTAIN" if sufficient else "POTENTIAL"),
                "首次图导航可能定位历史来源；没有版本/内容证据时不宣称独立新事实。",
            )
            uncertainties.append("图的版本、当前可见范围或新内容相关性未完全证明。")
        cost["action_family"] = "graph_navigation"
    elif candidate.name == "update_graph":
        eq = _equivalence(
            "UNRELATED_ACTION", "图写入保存模型声明/来源或确认维护页，不等同测试或独立验证。"
        )
        maintenance = snapshot.boundary.get("phase") == "graph_review"
        cost.update(
            action_family="graph_maintenance" if maintenance else "ordinary_graph_claim_write",
            creates_independent_execution_evidence=False,
        )
        value, reason = (
            ("LOW" if sufficient or candidate.arguments.get("operations") == [] else "UNCERTAIN"),
            "维护确认与普通模型图写入分开记录；写入成功不增加原始效果证据。",
        )
        expected = ["可能保存已见来源和模型判断；不追认旧操作或语义。"]
    elif candidate.name == "read_local_execution":
        op = candidate.operation_ref or f"operation:{candidate.arguments.get('operation_id', '')}"
        bare = op.removeprefix("operation:")
        if any(f"job:{bare}" in h.job_refs for h in snapshot.historical_actions):
            eq = _equivalence(
                "INSUFFICIENT_INFORMATION",
                "给 operation_id 传入了已知 Job 身份，命名空间不匹配，不能自动改写或当作读取成功。",
                unknowns=["identity_namespace_mismatch"],
            )
            value, reason = (
                "UNCERTAIN",
                "查询身份错误，不能证明会取得当前问题所需的原 Operation 观察。",
            )
            uncertainties.append("Job 与 Operation 不可互换；候选参数保持原样。")
        else:
            eq = _equivalence("UNRELATED_ACTION", "只读查询不是原效果检查的再次执行。")
            history = next((h for h in snapshot.historical_actions if h.record_ref == op), None)
            if history is None:
                value, reason = (
                    "UNCERTAIN",
                    "无法确认查询指向存在的原 Operation，未知身份不自动修复。",
                )
            elif (
                snapshot.task.goal == "investigate_unknown"
                and base._unknown(history)
                and history in _focus_actions(snapshot)
            ):
                value, reason = (
                    "HIGH",
                    "当前任务明确核对原 unknown，正确的原操作查询有潜在状态收敛价值；结果仍未观测。",
                )
            else:
                value, reason = (
                    ("LOW" if sufficient else "UNCERTAIN"),
                    "本地状态读取可用于核对，但当前任务的必要性未由未知状态本身证明。",
                )
        cost["action_family"] = "execution_ledger_read"
    elif candidate.name == "read_artifact":
        targets = base._read_targets(snapshot)
        relations = [_read_relation(snapshot, item) for item in targets]
        for item, relation in zip(targets, relations, strict=True):
            if relation is True:
                decision_refs.update(item.source_refs)
                decision_refs.update(item.conditions.get("navigation_refs") or [])
        conflicts = [
            item
            for item in targets
            if item.operation_ref
            and any(
                item.record_ref in history.artifact_refs
                and history.record_ref != item.operation_ref
                for history in snapshot.historical_actions
            )
        ]
        trusted = [item for item in targets if _raw_visible(item) and item not in conflicts]
        eq = _equivalence("UNRELATED_ACTION", "原文读取与原测试不同类，读取关联不证明测试等价。")
        requested_offset = candidate.arguments.get("offset", 0)
        versions_changed = any(
            item.conditions.get("revision") is not None
            and candidate.arguments.get("revision") is not None
            and item.conditions["revision"] != candidate.arguments["revision"]
            for item in targets
        )
        view_changed = any(
            item.conditions.get("view") is not None
            and candidate.arguments.get("view") is not None
            and item.conditions["view"] != candidate.arguments["view"]
            for item in trusted
        )
        if conflicts:
            eq = _equivalence(
                "INSUFFICIENT_INFORMATION",
                "工件所属 Operation 的来源互相冲突，不能假定该读取可以提供可靠的新证据。",
                unknowns=["artifact_ownership_conflict"],
            )
            value, reason = "UNCERTAIN", "需要先澄清来源归属；不挑有利来源推荐补读。"
        elif (
            trusted
            and not versions_changed
            and not view_changed
            and type(requested_offset) is int
            and requested_offset >= 0
        ):
            eq = _equivalence(
                "SEMANTIC_REPEAT",
                "该原件已完整可见且可校验；完整覆盖意味着改变 offset 不自动增加信息。",
                confidence="HIGH",
            )
            value, reason = "LOW", "同版本完整正文已在决策上下文，未记录新来源或新视图。"
        elif versions_changed:
            eq = _equivalence(
                "CONDITION_CHANGED", "请求明确的新修订与已见原文不同，不能视为原字节重复。"
            )
            value, reason = (
                ("POTENTIAL" if True in relations and not sufficient else "UNCERTAIN"),
                "版本变化仅表示可能取得新内容，当前任务必要性尚未证明。",
            )
        elif view_changed:
            value, reason = (
                "UNCERTAIN",
                "视图变化可能涉及脱敏或原字节语义，不能据名字断言已见或有用。",
            )
        elif sufficient and False in relations and True not in relations:
            value, reason = (
                "LOW",
                "当前任务已有足够原文，目标可明确判为其他历史；该读取没有结构化的任务增益。",
            )
        elif any(
            item.available and not item.visible and rel is True
            for item, rel in zip(targets, relations, strict=True)
        ):
            navigation = any(
                item.conditions.get("navigation_role") == "conversation_archive" for item in targets
            )
            value, reason = (
                ("POTENTIAL" if navigation else "HIGH"),
                "已定位的相关补读有任务价值；归档导航只说明检索潜力，不保证未读内容包含答案。",
            )
            expected = ["可能补齐当前问题尚未看到的来源正文。"]
        elif any(item.visible and not item.complete for item in targets) and any(
            k in candidate.arguments for k in ("offset", "cursor", "page", "start_line")
        ):
            value, reason = (
                ("POTENTIAL" if True in relations else "UNCERTAIN"),
                "已有片段不等于完整原文，新的分页可能补齐缺失范围；缺少覆盖关系时不判确定重复。",
            )
        else:
            value, reason = (
                "UNCERTAIN",
                "来源归属、完整性、可见范围或目标相关性不足，不能仅由可读或大体积决定价值。",
            )
        cost["action_family"] = "artifact_read"
    elif candidate.kind == "test":
        relation = _relation(snapshot)
        if snapshot.task.goal == "interpret_history" and not risk.unknown_execution_risk:
            value = "LOW" if sufficient else "UNCERTAIN"
            reason = "当前目标是解释既有历史；已有足够正文时再测不补历史来源，正文不足时条件变更也不自动解决来源缺口。"
            expected = ["可能产生新条件下的独立观察，但不等于回答当前历史解释问题。"]
        elif relation is False and not risk.unknown_execution_risk:
            value, reason = (
                ("LOW" if sufficient else "UNCERTAIN"),
                "候选的明确条件与当前任务目标不一致；变化不自动带来当前任务价值。",
            )
        if eq.logical_classification == "UNRELATED_ACTION":
            risk = RepeatRisk(
                status="LOW",
                unknown_execution_risk=risk.unknown_execution_risk,
                classification_reason="没有结构关联的另一测试不等于重复当前历史；LOW 不代表安全放行或有价值。",
            )
        elif eq.logical_classification == "CONDITION_CHANGED" and not risk.unknown_execution_risk:
            risk = RepeatRisk(
                status="LOW",
                classification_reason="已知逻辑条件确实不同，不判同条件重复；必要性与其他未记录语义仍独立评价。",
            )

    if supported_nonexecuting:
        risk = RepeatRisk(
            status="LOW",
            unknown_execution_risk=risk.unknown_execution_risk,
            classification_reason="当前动作是读取/查询或图声明维护，不重执行外部测试；不消除原操作的 unknown。",
        )
    if candidate.kind == "test" and (conflicts := _candidate_conflicts(snapshot)):
        eq = _equivalence(
            "INSUFFICIENT_INFORMATION",
            "候选结构化条件与实际参数存在明确冲突，不能任选其一判重复或不同。",
            unknowns=conflicts,
        )
        value, reason = "UNCERTAIN", "需先澄清候选语义映射；不自动更正、执行或赋予正面价值。"
        risk = RepeatRisk(
            status="HIGH" if risk.unknown_execution_risk else "UNCERTAIN",
            unknown_execution_risk=risk.unknown_execution_risk,
            classification_reason="参数冲突不能证明低重复风险；原 unknown 的重执行风险仍保留。",
        )
        uncertainties.append("候选条件与参数冲突：" + ",".join(conflicts))
    updated_gain = GainAssessment(
        classification=value,
        estimated_action_value=value,
        expected_new_information=expected,
        unresolved_question=snapshot.task.text,
        supporting_evidence_refs=sorted(decision_refs),
        condition_differences=gain.condition_differences,
        uncertainties=uncertainties,
        cost_and_side_effects=cost,
        classification_reason=reason,
    )
    return result.model_copy(
        update={
            "equivalence": eq,
            "repeat_risk": risk,
            "information_gain": updated_gain,
            "source_refs": sorted(set(result.source_refs) | decision_refs),
            "uncertainties": sorted(set(result.uncertainties + uncertainties)),
            "recommended_feedback": f"离线观察：{eq.logical_classification}；任务证据 {result.evidence_sufficiency.status}；当前任务价值 {value}。不拦截、不执行、不修改参数。",
        }
    )


def evaluate_input(value: object) -> ActionObservation:
    try:
        raw: Any = value.model_dump(mode="python") if isinstance(value, ActionSnapshot) else value
        if not base._json_value(raw):
            raise ValueError("unsupported_snapshot_value")
        return evaluate(ActionSnapshot.model_validate(raw))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        return base.evaluate_input({"invalid": True})
