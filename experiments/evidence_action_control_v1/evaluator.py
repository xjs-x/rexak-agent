"""确定性、无 IO 的行动观察原型；输入是决策前快照，输出没有执行权限。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError

from agent_workbench.domain.decision_memory import compare_dimensions

from .contracts import (
    ActionObservation,
    ActionSnapshot,
    AvailableEvidence,
    CandidateAction,
    EquivalenceAssessment,
    GainAssessment,
    HistoricalAction,
    RepeatRisk,
    SufficiencyAssessment,
)

CRITICAL_DIMENSIONS = (
    "target", "method", "route", "entry", "environment", "configuration",
    "identity_id", "auth_revision", "privileges", "executor_id",
    "execution_semantics", "mapping_signature", "prerequisites",
)
_LOGICAL_REQUIRED = ("route", "environment", "configuration")
_BODY_FIELDS = {"check", "config", "control_ok", "effect_observed", "entry", "environment", "method"}
_PREREQUISITE_FIELDS = {"condition", "available"}
_ALIASES = {"config": "configuration", "check": "information_key", "endpoint": "route"}
_UNRECORDED = {"", "unknown", "unrecorded", "not_available", "not_exercised", "unproven"}
_UNRESOLVED_STATES = {
    "unknown", "uncertain", "pending", "running", "submitted", "dispatching",
    "accepted", "reconciling", "pending_reconciliation",
}
_READ_VARIANTS = {
    "page", "cursor", "offset", "start", "start_line", "end_line", "revision", "version",
}


def _refs(*groups: list[str]) -> list[str]:
    return sorted({item for group in groups for item in group if item})


def _endpoint(value: str) -> str:
    """只归一化明确的 URL 语法；query、fragment、路径及转义原样保留。"""
    if "://" not in value:
        return value
    try:
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.scheme.lower() not in {"http", "https"}:
            return value
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port is not None and (parsed.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            host = f"{host}:{port}"
        if "@" in parsed.netloc:
            host = f"{parsed.netloc.rsplit('@', 1)[0]}@{host}"
        return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, parsed.fragment))
    except ValueError:
        return value


def _value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value.lower() in _UNRECORDED:
            return None
        if key == "method":
            return value.upper()
        if key in {"target", "route", "entry"}:
            return _endpoint(value)
    if key == "privileges" and isinstance(value, list) and all(isinstance(v, str) for v in value):
        return sorted(set(value))
    return value


def _conditions(values: dict[str, Any]) -> dict[str, Any]:
    result = {key: _value(key, values.get(key)) for key in CRITICAL_DIMENSIONS}
    for source, target in _ALIASES.items():
        if target in result and result[target] is None and source in values:
            result[target] = _value(target, values[source])
    return result


@dataclass(frozen=True)
class _Body:
    evidence: AvailableEvidence
    fields: dict[str, Any]


def _visible_bodies(snapshot: ActionSnapshot) -> tuple[list[_Body], list[str]]:
    bodies: list[_Body] = []
    missing: list[str] = []
    for item in snapshot.evidence:
        if item.kind != "artifact_body":
            continue
        if not item.visible:
            # 目录中的正文不得成为决策前已知事实；这里不解析、校验或取出隐藏正文。
            missing.append(f"{item.record_ref} 的原始正文尚未进入决策时点可见上下文。")
            continue
        if not (
            item.available and item.complete and item.provenance_verified
            and item.visibility_refs and item.source_refs and item.sha256 and item.content is not None
        ):
            missing.append(f"{item.record_ref} 缺少完整正文、可见性出处或可验证来源。")
            continue
        digest = hashlib.sha256(item.content.encode("utf-8")).hexdigest()
        if digest != item.sha256.lower():
            missing.append(f"{item.record_ref} 的正文 SHA256 不匹配。")
            continue
        owners = {history.record_ref for history in snapshot.historical_actions
                  if item.record_ref in history.artifact_refs}
        if item.operation_ref and owners and owners != {item.operation_ref}:
            missing.append(f"{item.record_ref} 的 Artifact 与 Operation 归属冲突。")
            continue
        try:
            decoded = json.loads(item.content)
        except (json.JSONDecodeError, ValueError):
            missing.append(f"{item.record_ref} 不是本原型可解释的 JSON 原始检查正文。")
            continue
        if not isinstance(decoded, dict):
            missing.append(f"{item.record_ref} 的正文不是检查字段对象。")
            continue
        # 未知字段（包括伪指令、模型声明和建议）没有参与判定的权限。
        fields = {key: decoded[key] for key in _BODY_FIELDS | _PREREQUISITE_FIELDS if key in decoded}
        bodies.append(_Body(item, fields))
    conflicting = {
        body.evidence.operation_ref for body in bodies
        if body.evidence.operation_ref and any(
            other.evidence.operation_ref == body.evidence.operation_ref
            and any(body.fields[key] != other.fields[key]
                    for key in body.fields.keys() & other.fields.keys())
            for other in bodies
        )
    }
    if conflicting:
        missing.extend(f"{ref} 的可见原始正文相互冲突。" for ref in sorted(conflicting))
        bodies = [body for body in bodies if body.evidence.operation_ref not in conflicting]
    return bodies, missing


def _linked(body: _Body, history: HistoricalAction) -> bool:
    if body.evidence.operation_ref and body.evidence.operation_ref != history.record_ref:
        return False
    return (
        body.evidence.operation_ref == history.record_ref
        or body.evidence.record_ref in history.artifact_refs
    )


def _complete_check(body: _Body) -> bool:
    return (
        body.fields.keys() >= _BODY_FIELDS
        and type(body.fields["control_ok"]) is bool
        and type(body.fields["effect_observed"]) is bool
        and all(
            isinstance(body.fields[key], str) and bool(body.fields[key].strip())
            for key in ("check", "config", "entry", "environment", "method")
        )
    )


def _historical_conditions(
    history: HistoricalAction, bodies: list[_Body]
) -> tuple[dict[str, Any], list[_Body]]:
    result = _conditions(history.conditions)
    linked = [body for body in bodies if _linked(body, history) and _complete_check(body)]
    conflicts: set[str] = set()
    for body in linked:
        body_conditions = _conditions(body.fields)
        for key in CRITICAL_DIMENSIONS:
            if key in conflicts:
                continue
            if result[key] is None:
                result[key] = body_conditions[key]
            elif body_conditions[key] is not None and result[key] != body_conditions[key]:
                # 同一出处链冲突时，不能挑选有利值来认定重复。
                result[key] = None
                conflicts.add(key)
    return result, linked


def _related(candidate: CandidateAction, history: HistoricalAction) -> bool:
    if candidate.operation_ref == history.record_ref:
        return True
    if candidate.artifact_ref and candidate.artifact_ref in history.artifact_refs:
        return True
    if candidate.information_key and candidate.information_key == history.information_key:
        return True
    if candidate.information_key and history.information_key:
        return False
    old, new = _conditions(history.conditions), _conditions(candidate.conditions)
    if any(old[key] is not None and old[key] == new[key] for key in ("route", "entry")):
        return True
    if any(old[key] is not None and new[key] is not None for key in ("route", "entry")):
        return False
    return old["target"] is not None and old["target"] == new["target"]


def _unknown(history: HistoricalAction) -> bool:
    return (
        history.status.lower() in _UNRESOLVED_STATES
        or any(state.lower() in _UNRESOLVED_STATES for state in history.job_states.values())
    )


def _compare(
    snapshot: ActionSnapshot, history: HistoricalAction, bodies: list[_Body]
) -> EquivalenceAssessment:
    candidate = snapshot.candidate
    old, linked = _historical_conditions(history, bodies)
    new = _conditions(candidate.conditions)
    references = _refs(
        [history.record_ref], history.source_refs, candidate.source_refs,
        [body.evidence.record_ref for body in linked],
    )
    dimensions = compare_dimensions(old, new, references)
    unknowns = [item.field for item in dimensions if item.status == "insufficient"]
    differences = [item.field for item in dimensions if item.status == "changed"]
    provenance = bool(history.source_refs and candidate.source_refs)
    observed = history.origin_kind == "execution_observation" and provenance
    trusted = observed and bool(linked) and not _unknown(history)
    logical_known = all(old[key] is not None and new[key] is not None for key in _LOGICAL_REQUIRED)
    local_projection = (
        snapshot.task.goal == "interpret_history" and candidate.name == "simulate_check"
        and new["execution_semantics"] == "local_simulator"
        and old["execution_semantics"] is not None
    )
    ignored_logical = {"executor_id", "execution_semantics"} if local_projection else set()
    logical_differences = [key for key in differences if key not in ignored_logical]
    key = history.information_key
    if key is None and linked:
        checks = {body.fields["check"] for body in linked}
        key = next(iter(checks)) if len(checks) == 1 else None
    same_information = bool(candidate.information_key and candidate.information_key == key)

    classification = "INSUFFICIENT_INFORMATION"
    logical = "INSUFFICIENT_INFORMATION"
    if observed and differences:
        classification = "CONDITION_CHANGED"
    elif trusted and not unknowns:
        classification = "EXACT_REPEAT" if candidate.name == history.tool_name else "SEMANTIC_REPEAT"
    if observed and logical_differences:
        logical = "CONDITION_CHANGED"
    elif trusted and logical_known and same_information and (not unknowns or local_projection):
        logical = "SEMANTIC_REPEAT"
    if classification == "EXACT_REPEAT":
        logical = "EXACT_REPEAT"
    if snapshot.task.goal == "verify_current":
        unknowns.append("observation_time")
        if classification in {"EXACT_REPEAT", "SEMANTIC_REPEAT"}:
            classification = "INSUFFICIENT_INFORMATION"
        if logical in {"EXACT_REPEAT", "SEMANTIC_REPEAT"}:
            logical = "INSUFFICIENT_INFORMATION"
    reason = (
        f"比较历史 {history.record_ref}：完整执行条件判为 {classification}，"
        f"问题范围内的逻辑关系判为 {logical}。"
    )
    if logical == "SEMANTIC_REPEAT":
        reason += "显式 information_key 与逻辑检查路由、环境、配置一致，且有当时已可见的可信原始正文；未记录的 HTTP 方法或物理入口不因此补齐。"
    if differences:
        reason += f"已记录差异为 {', '.join(differences)}；差异不直接证明新行动有价值。"
    if unknowns:
        reason += f"缺少可比条件 {', '.join(unknowns)}，不得当作相同。"
    if not trusted:
        reason += "历史执行来源、完整原文或已确定执行结果不足，不能升级为高可信重复。"
    if snapshot.task.goal == "verify_current":
        reason += "当前验证问题缺少当前时点证据，历史条件相同不能证明本次验证无新信息。"
    return EquivalenceAssessment(
        classification=classification, logical_classification=logical,
        confidence="HIGH" if trusted and not unknowns else "MEDIUM" if trusted else "LOW",
        compared_dimensions=dimensions, material_differences=differences,
        unknown_dimensions=unknowns, classification_reason=reason,
    )


def _equivalence(
    snapshot: ActionSnapshot, related: list[HistoricalAction], bodies: list[_Body]
) -> EquivalenceAssessment:
    if snapshot.candidate.kind != "test":
        return EquivalenceAssessment(
            classification="INSUFFICIENT_INFORMATION", logical_classification="INSUFFICIENT_INFORMATION",
            unknown_dimensions=["action_semantics"] if snapshot.candidate.kind == "other" else [],
            classification_reason=(
                "读取或查询与历史测试操作的来源关联，不证明两者是同类行动；读取重复在信息增益中独立检查。"
                if snapshot.candidate.kind in {"read", "query"}
                else "当前工具没有明确的测试、读取或查询语义，不能从条件相似推断行动等价。"
            ),
        )
    if not related:
        return EquivalenceAssessment(
            classification="UNRELATED_ACTION", logical_classification="UNRELATED_ACTION",
            unknown_dimensions=list(CRITICAL_DIMENSIONS),
            classification_reason="决策前快照中没有通过显式来源、信息问题或已知目标关联的历史操作。",
        )
    assessments = [_compare(snapshot, history, bodies) for history in related]
    ranks = {
        "EXACT_REPEAT": 5, "SEMANTIC_REPEAT": 4, "CONDITION_CHANGED": 3,
        "INSUFFICIENT_INFORMATION": 2, "UNRELATED_ACTION": 1,
    }
    return max(
        assessments,
        key=lambda result: (
            ranks[result.logical_classification], ranks[result.classification],
            -len(result.unknown_dimensions),
        ),
    )


def _focus_matches(snapshot: ActionSnapshot, body: _Body) -> bool:
    if not snapshot.task.focus_conditions:
        return False
    normalized = _conditions(body.evidence.conditions)
    for key, value in _conditions(body.fields).items():
        if value is not None:
            normalized[key] = value
    for field, value in snapshot.task.focus_conditions.items():
        key = _ALIASES.get(field, field)
        observed = body.fields.get("check") if key == "information_key" else normalized.get(key)
        if observed is None or _value(key, observed) != _value(key, value):
            return False
    return True


def _sufficiency(
    snapshot: ActionSnapshot, bodies: list[_Body], unavailable: list[str]
) -> SufficiencyAssessment:
    if snapshot.task.goal == "unspecified" or not snapshot.task.focus_conditions:
        return SufficiencyAssessment(
            status="INDETERMINATE", missing_evidence=["当前问题缺少明确的目标类型或结构化条件。"],
            classification_reason="充分性必须相对于当前待回答的问题；不从候选动作反推任务目标。",
        )
    relevant = [body for body in bodies if _focus_matches(snapshot, body)]
    complete = [body for body in relevant if _complete_check(body)]
    references = [body.evidence.record_ref for body in relevant]
    if snapshot.task.goal == "interpret_history" and complete:
        conclusions = []
        for body in complete:
            fields = body.fields
            scope = (
                f"{fields['check']}/{fields['environment']}/{fields['config']}，"
                f"{str(fields['method']).upper()} {fields['entry']}"
            )
            if fields["control_ok"]:
                conclusion = f"原始正文记录 {scope} 的控制检查成立，effect_observed={str(fields['effect_observed']).lower()}。"
            else:
                conclusion = f"原始正文记录 {scope} 的控制检查未成立，不能据此得出效果阴性或阳性结论。"
            conclusions.append(conclusion + "仅限该历史已检查条件，不证明当前状态或漏洞确认。")
        return SufficiencyAssessment(
            status="SUFFICIENT", supported_conclusions=conclusions,
            supporting_evidence_refs=sorted(references),
            classification_reason="当前问题要求解释历史检查；匹配任务条件的完整原始 JSON 已可见，来源和 SHA256 可验证。",
        )
    missing = list(unavailable)
    if snapshot.task.goal == "verify_current":
        missing.append("缺少当前时点、当前部署与当前条件对应的独立验证；历史正文不能证明仍然成立。")
    elif snapshot.task.goal == "investigate_unknown":
        missing.append("需要对原 Operation/Job 的执行状态核对；独立操作成功不能追认原未知结果。")
    elif snapshot.task.goal == "progress_blocked":
        missing.append("需要验证前提变化后原检查的实际效果；解除阻塞不等于效果验证。")
    else:
        missing.append("缺少匹配当前问题条件、已可见且字段完整的原始检查正文。")
    metadata = any(
        evidence.available and evidence.visible
        and evidence.kind in {"execution_metadata", "execution_state", "artifact_body"}
        for evidence in snapshot.evidence
    )
    status = ("INDETERMINATE" if any("冲突" in item for item in unavailable)
              else "PARTIAL" if relevant or metadata else "INSUFFICIENT")
    return SufficiencyAssessment(
        status=status, supporting_evidence_refs=sorted(references), missing_evidence=sorted(set(missing)),
        classification_reason="执行状态、HTTP 状态和模型/摘要声明不能代替任务所需的原始效果证据；正文充分性不随候选动作的目标变化。",
    )


def _read_targets(snapshot: ActionSnapshot) -> list[AvailableEvidence]:
    candidate = snapshot.candidate
    return [
        evidence for evidence in snapshot.evidence
        if (
            candidate.artifact_ref is not None
            and (candidate.artifact_ref == evidence.record_ref or candidate.artifact_ref in evidence.source_refs)
        ) or (candidate.operation_ref is not None and candidate.operation_ref == evidence.operation_ref)
    ]


def _prerequisite_changes(snapshot: ActionSnapshot, bodies: list[_Body]) -> list[tuple[str, str]]:
    """独立前提观察只表示前提变化，不替候选填入未声明的执行条件。"""
    changes = []
    focus = _conditions(snapshot.task.focus_conditions)
    for history in snapshot.historical_actions:
        if history.origin_kind != "execution_observation":
            continue
        old = _conditions(history.conditions)
        if focus["route"] is None or focus["route"] != old["route"]:
            continue
        prerequisites = old.get("prerequisites")
        if not isinstance(prerequisites, dict):
            continue
        for body in bodies:
            name, available = body.fields.get("condition"), body.fields.get("available")
            if old["environment"] is None or body.fields.get("environment") != old["environment"]:
                continue
            if (isinstance(name, str) and type(available) is bool
                    and type(prerequisites.get(name)) is bool and prerequisites[name] != available):
                changes.append((f"prerequisites.{name}", body.evidence.record_ref))
    return sorted(set(changes))


def _read_relevant(snapshot: ActionSnapshot, evidence: AvailableEvidence) -> bool:
    """仅用可见目录/操作元数据关联当前问题，不触碰尚未补读的正文。"""
    if not snapshot.task.focus_conditions:
        return False
    views = [evidence.conditions]
    views.extend(
        history.conditions for history in snapshot.historical_actions
        if evidence.operation_ref == history.record_ref or evidence.record_ref in history.artifact_refs
    )
    matches = 0
    for field, value in snapshot.task.focus_conditions.items():
        key = _ALIASES.get(field, field)
        recorded = [_conditions(view).get(key) for view in views]
        known = [item for item in recorded if item is not None]
        if known and any(item != _value(key, value) for item in known):
            return False
        matches += bool(known)
    return matches > 0


def _gain(
    snapshot: ActionSnapshot, equivalence: EquivalenceAssessment,
    sufficiency: SufficiencyAssessment, risk: RepeatRisk, bodies: list[_Body],
) -> GainAssessment:
    candidate = snapshot.candidate
    value = "UNCERTAIN"
    expected: list[str] = []
    uncertainties: list[str] = []
    reason = "候选语义或现有条件不足，预期信息增益保留不确定。"
    prerequisite_changes = (_prerequisite_changes(snapshot, bodies)
                            if snapshot.task.goal == "progress_blocked" else [])
    if candidate.kind == "read":
        targets = _read_targets(snapshot)
        changed = [
            key for key in sorted(_READ_VARIANTS & candidate.arguments.keys())
            if any(evidence.conditions.get(key) != candidate.arguments[key] for evidence in targets)
        ]
        trusted_ids = {body.evidence.record_ref for body in bodies}
        if changed:
            expected = ["可能取得不同页、不同版本或不同修订的内容；尚未观测其结果。"]
            value = "POTENTIAL"
            reason = "读取范围或版本条件尚未证明与已见内容一致，不能仅因曾读过该工件就判为低价值。"
            uncertainties.append(f"读取变化或未记录维度：{', '.join(changed)}。")
        elif targets and all(item.record_ref in trusted_ids for item in targets):
            value = "LOW"
            reason = "目标原文已完整、可信且可见，当前读取没有记录新的分页或版本条件。"
        elif any(item.available and not item.visible and _read_relevant(snapshot, item) for item in targets):
            value = "HIGH" if snapshot.task.goal == "interpret_history" else "POTENTIAL"
            expected = ["可能把已定位但尚未进入当前上下文的原始证据补齐，以回答当前问题。"]
            reason = "必要历史补读可能具有信息价值；读取成本和上下文体积单独记录。"
        else:
            uncertainties.append("读取目标的任务相关性、来源、可用性或完整可见状态不足。")
    elif (candidate.kind == "query" and candidate.operation_ref and risk.unknown_execution_risk
          and any(_read_relevant(snapshot, item) for item in _read_targets(snapshot))):
        value = "HIGH"
        expected = ["可能取得原 Operation/Job 的新执行状态，从而收敛未知执行结果。"]
        reason = "候选查询指向原执行身份；预期状态核对不等于已确认其结果。"
    elif candidate.kind == "test":
        if risk.unknown_execution_risk:
            expected = ["可能产生另一份独立观察，但无法据此确定原未知 Operation/Job 的结果。"]
            reason = "存在未决执行身份，重新测试可能导致重复执行；新 Operation ID 本身不增加信息。"
            uncertainties.append("原 Operation/Job 仍须保持未知或未决，不得由独立成功追认。")
        elif (
            equivalence.logical_classification in {"EXACT_REPEAT", "SEMANTIC_REPEAT"}
            and sufficiency.status == "SUFFICIENT"
        ):
            value = "LOW"
            reason = "在当前历史解释问题范围内，已有可验证原文充分回答同一信息问题；未记录新增条件。"
        elif equivalence.classification == "CONDITION_CHANGED":
            value = "POTENTIAL"
            expected = ["可能观察到已变化条件下的不同结果；是否有助于当前目标仍须判断。"]
            reason = "存在已记录条件变化，但变化不自动构成高行动价值或执行必要性。"
            uncertainties.append("新条件与当前目标的关联和新增观察的实际内容尚未证实。")
        elif snapshot.task.goal == "verify_current":
            value = "POTENTIAL"
            expected = ["可能取得当前时点的条件和效果证据；历史结果不能替代该观察。"]
            reason = "当前验证目标存在时间差异，但本原型无法证明这次测试的具体必要性。"
        else:
            uncertainties.append("关键条件或可见原始来源不足，不能机械视为重复或必要新测试。")
    else:
        uncertainties.append("本原型没有该工具语义的确定性判定规则。")
    if prerequisite_changes:
        uncertainties.append("独立原文表明历史阻塞前提已变化；候选是否使用该前提尚未证明，原检查效果仍未验证。")
        if candidate.kind == "test":
            expected.append("可能区分前提变化后原检查的结果；前提解除本身不构成效果验证。")
    return GainAssessment(
        classification=value, estimated_action_value=value,
        expected_new_information=expected, unresolved_question=snapshot.task.text,
        supporting_evidence_refs=_refs(sufficiency.supporting_evidence_refs,
                                      [ref for _, ref in prerequisite_changes]),
        condition_differences=_refs(equivalence.material_differences,
                                   [field for field, _ in prerequisite_changes]),
        uncertainties=uncertainties, cost_and_side_effects=dict(candidate.estimated_cost),
        classification_reason=reason,
    )


def evaluate(snapshot: ActionSnapshot) -> ActionObservation:
    """只观察传入快照；不执行工具、不写状态、不读取文件或 Oracle。"""
    bodies, unavailable = _visible_bodies(snapshot)
    related = sorted(
        (history for history in snapshot.historical_actions if _related(snapshot.candidate, history)),
        key=lambda history: history.record_ref,
    )
    equivalence = _equivalence(snapshot, related, bodies)
    sufficiency = _sufficiency(snapshot, bodies, unavailable)
    unresolved = [history for history in related if _unknown(history)]
    unknown_risk = bool(unresolved)
    logical_repeat = equivalence.logical_classification in {"EXACT_REPEAT", "SEMANTIC_REPEAT"}
    nonexecuting = snapshot.candidate.kind in {"read", "query"}
    risk = RepeatRisk(
        status="LOW" if nonexecuting else "HIGH" if unknown_risk or logical_repeat else "POSSIBLE" if related else "UNCERTAIN",
        unknown_execution_risk=unknown_risk,
        classification_reason=(
            "当前动作只读取或查询；关联原 Operation/Job 的未知状态仍保留，但这不是重复执行该测试。"
            if nonexecuting else "关联的原 Operation/Job 执行结果仍未决；不得把未知等同未执行，也不得借另一独立操作追认。"
            if unknown_risk else "当前问题范围内有可信逻辑重复候选；这不授予阻止执行的权限。"
            if logical_repeat else "条件变化或证据缺失使重复风险仍需保留；无权据此下达执行命令。"
        ),
    )
    gain = _gain(snapshot, equivalence, sufficiency, risk, bodies)
    sources = _refs(
        snapshot.task.source_refs, snapshot.candidate.source_refs,
        [history.record_ref for history in related],
        [ref for history in related for ref in history.source_refs],
        [ref for history in unresolved for ref in history.job_refs],
        sufficiency.supporting_evidence_refs,
        gain.supporting_evidence_refs,
    )
    uncertainties = sorted(set(
        snapshot.limitations + gain.uncertainties
        + [f"完整执行条件未明确：{key}。" for key in equivalence.unknown_dimensions]
    ))
    feedback = (
        f"观察结果：逻辑关系 {equivalence.logical_classification}；"
        f"当前问题证据 {sufficiency.status}；预期行动价值 {gain.classification}。"
        "以上仅为带来源的观察，不改变候选参数、执行状态或 Agent 的最终决策。"
    )
    return ActionObservation(
        snapshot_id=snapshot.snapshot_id, candidate_action=snapshot.candidate.model_copy(deep=True),
        related_historical_actions=[history.record_ref for history in related],
        equivalence=equivalence, evidence_sufficiency=sufficiency, information_gain=gain,
        repeat_risk=risk, source_refs=sources, uncertainties=uncertainties,
        recommended_feedback=feedback,
    )


def _json_value(value: Any, depth: int = 0) -> bool:
    if depth > 24:
        return False
    if value is None or type(value) in {str, int, bool}:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_json_value(item, depth + 1) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _json_value(item, depth + 1) for key, item in value.items())
    return False


def evaluate_input(value: object) -> ActionObservation:
    """非法输入安全降级；错误输入不变为执行建议，也不泄露原始异常内容。"""
    try:
        raw = value.model_dump(mode="python") if isinstance(value, ActionSnapshot) else value
        if not _json_value(raw):
            raise ValueError("unsupported_snapshot_value")
        return evaluate(ActionSnapshot.model_validate(raw))
    except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
        return ActionObservation(
            snapshot_id="invalid_input",
            candidate_action=CandidateAction(tool_call_id="unavailable", name="unavailable", arguments={}),
            related_historical_actions=[],
            equivalence=EquivalenceAssessment(
                classification="INSUFFICIENT_INFORMATION", logical_classification="INSUFFICIENT_INFORMATION",
                unknown_dimensions=list(CRITICAL_DIMENSIONS),
                classification_reason="输入不能被验证为受支持的决策前快照，未比较行动等价性。",
            ),
            evidence_sufficiency=SufficiencyAssessment(
                status="INDETERMINATE", missing_evidence=["缺少合法且完整的结构化输入。"],
                classification_reason="无有效任务与证据来源，无法评价充分性。",
            ),
            information_gain=GainAssessment(
                classification="UNCERTAIN", estimated_action_value="UNCERTAIN", unresolved_question="不可用",
                classification_reason="输入验证失败，信息增益保留不确定。",
            ),
            repeat_risk=RepeatRisk(status="UNCERTAIN", classification_reason="输入不足，不能判断重复执行风险。"),
            source_refs=[], uncertainties=["输入验证失败；未执行任何候选工具。"],
            recommended_feedback="仅记录输入不完整；当前观察不能代替 Agent 决策。",
        )
