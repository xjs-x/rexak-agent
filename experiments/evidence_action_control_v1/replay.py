"""只读真实轨迹适配：以每次已出站 request.messages 重建决策前快照。

不导入旧实验执行器，不访问数据库、Provider 或 Executor。原始工件字节只用于
来源核验；未进入本次请求的正文不会交给评估器。Oracle 和事后评分不在输入内。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .contracts import (
    ActionSnapshot,
    AvailableEvidence,
    CandidateAction,
    HistoricalAction,
    TaskQuestion,
)

ROOT = Path(__file__).resolve().parents[2]
HISTORY_PATH = ROOT / "experiments/decision_memory_live_v1/bundle/history.json"
RESULT_PATHS = {
    "A": ROOT / "experiments/decision_memory_ab_comparison_v1/live-results/A/result.json",
    "B": ROOT / "experiments/decision_memory_ab_comparison_v1/live-results/B/result.json",
    "C": ROOT / "experiments/decision_memory_s14c_r3/live-results/S14-C-R3.json",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _object(text: Any) -> dict[str, Any]:
    if isinstance(text, dict):
        return text
    if not isinstance(text, str):
        return {}
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    raw_calls = message.get("tool_calls")
    if raw_calls is None:
        return result
    if not isinstance(raw_calls, list):
        return [{"id": "", "name": "unknown_tool", "args": {},
                 "parse_errors": ["tool_calls_not_array"]}]
    for call in raw_calls:
        errors = []
        if not isinstance(call, dict):
            result.append({"id": "", "name": "unknown_tool", "args": {},
                           "parse_errors": ["tool_call_not_object"]})
            continue
        function = call.get("function", {})
        if not isinstance(function, dict):
            errors.append("function_not_object")
            function = {}
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            errors.append("missing_tool_call_id")
            call_id = ""
        name = call.get("name") or function.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append("missing_tool_name")
            name = "unknown_tool"
        raw_args = call.get("args", function.get("arguments"))
        if isinstance(raw_args, dict):
            args = raw_args
        else:
            args = _object(raw_args)
            try:
                valid_args = isinstance(json.loads(raw_args), dict) if isinstance(raw_args, str) else False
            except ValueError:
                valid_args = False
            if not valid_args:
                errors.append("arguments_not_json_object")
        result.append({
            "id": call_id, "name": name, "args": args, "parse_errors": errors,
        })
    counts: dict[str, int] = {}
    for call in result:
        counts[call["id"]] = counts.get(call["id"], 0) + 1
    for call in result:
        if counts[call["id"]] > 1:
            call["parse_errors"].append("duplicate_tool_call_id_in_batch")
    return result


def _pairs(messages: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
    """只接受请求内先 AI、后 Tool 的唯一配对；不读取本次响应或以后请求。"""
    pending: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    pairs = []
    tool_counts: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            key = str(message.get("tool_call_id") or "")
            tool_counts[key] = tool_counts.get(key, 0) + 1
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") in {"assistant", "ai"}:
            for call in _calls(message):
                call_id = call["id"]
                if call["parse_errors"]:
                    pending.pop(call_id, None)
                    seen.add(call_id)
                    continue
                if not call_id or call_id in pending or call_id in seen:
                    pending.pop(call_id, None)
                    seen.add(call_id)
                else:
                    pending[call_id] = call
        elif message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            call = pending.pop(call_id, None)
            if call is not None and call_id not in seen and tool_counts[call_id] == 1:
                pairs.append((index, call, message))
            seen.add(call_id)
    return pairs


def _body_conditions(body: dict[str, Any]) -> dict[str, Any]:
    """只映射结构字段，不解释工件内自然语言或伪指令。"""
    keys = {
        "check": "route", "entry": "entry", "environment": "environment",
        "config": "configuration", "configuration": "configuration", "method": "method",
    }
    conditions = {target: body[key] for key, target in keys.items() if body.get(key) is not None}
    if body.get("prerequisite") == "K_missing":
        conditions["prerequisites"] = {"K": False}
    if body.get("condition") == "K" and isinstance(body.get("available"), bool):
        conditions["prerequisites"] = {"K": body["available"]}
    return conditions


def _conditions(context: dict[str, Any], identity: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    keys = {
        "method": "method", "step_id": "route", "route": "route",
        "environment": "environment", "configuration": "configuration",
        "identity_auth_revision": "auth_revision", "target_mapping_signature": "mapping_signature",
        "privileges": "privileges", "prerequisites": "prerequisites",
    }
    for key, target in keys.items():
        if context.get(key) is not None:
            result[target] = context[key]
    if identity is not None:
        result["identity_id"] = identity
    if context.get("url"):
        url = urlsplit(str(context["url"]))
        result["target"] = f"{url.scheme}://{url.netloc}"
        result["entry"] = url.path + (f"?{url.query}" if url.query else "")
    return result


def _information(conditions: dict[str, Any]) -> str | None:
    route = conditions.get("route")
    return f"check:{route}" if isinstance(route, str) and route else None


class _History:
    def __init__(self, data: dict[str, Any]):
        tables = data.get("tables") or {}
        self.operations = {row["operation_id"]: row for row in tables.get("tool_operations", [])}
        self.jobs = {row["job_id"]: row for row in tables.get("tool_jobs", [])}
        self.artifacts = {row["id"]: row for row in tables.get("artifacts", [])}
        self.bytes = data.get("artifact_bytes_base64") or {}

    def verified_bytes(self, artifact_id: str) -> bytes | None:
        row = self.artifacts.get(artifact_id)
        if row is None or artifact_id not in self.bytes:
            return None
        try:
            raw = base64.b64decode(self.bytes[artifact_id], validate=True)
        except (ValueError, TypeError):
            return None
        if hashlib.sha256(raw).hexdigest() != row.get("sha256") or len(raw) != row.get("byte_size"):
            return None
        operation_id = row.get("source_operation_id")
        if operation_id is not None and operation_id not in self.operations:
            return None
        if operation_id is not None and artifact_id not in (
            self.operations[operation_id].get("result_artifact_ids") or []
        ):
            return None
        return raw

    def actions(self) -> list[HistoricalAction]:
        actions = []
        for operation_id, row in self.operations.items():
            jobs = [job for job in self.jobs.values() if job.get("operation_id") == operation_id]
            conditions = _conditions(row.get("execution_context") or {}, row.get("identity_id"))
            executors = {job.get("executor_id") for job in jobs if job.get("executor_id")}
            if len(executors) == 1:
                conditions["executor_id"] = next(iter(executors))
            if row.get("tool_name") == "http_request":
                conditions["execution_semantics"] = "executor_http"
            ref = f"operation:{operation_id}"
            actions.append(HistoricalAction(
                record_ref=ref, tool_name=row["tool_name"], conditions=conditions,
                information_key=_information(conditions), status=row.get("status", "unknown"),
                job_refs=[f"job:{job['job_id']}" for job in jobs],
                job_states={f"job:{job['job_id']}": job.get("state", "unknown") for job in jobs},
                artifact_refs=[f"artifact:{aid}" for aid in row.get("result_artifact_ids") or []],
                source_refs=[ref, f"{HISTORY_PATH.relative_to(ROOT).as_posix()}#/tables/tool_operations/{operation_id}"],
            ))
        return actions

    def directories(self) -> dict[str, AvailableEvidence]:
        evidence = {}
        for artifact_id, row in self.artifacts.items():
            ref = f"artifact:{artifact_id}"
            operation_id = row.get("source_operation_id")
            evidence[ref] = AvailableEvidence(
                record_ref=ref, kind="artifact_body",
                operation_ref=f"operation:{operation_id}" if operation_id else None,
                available=row.get("availability") == "available",
                visible=False, complete=False, provenance_verified=False, content=None,
                sha256=row.get("sha256"), source_refs=[ref],
            )
        for operation_id in self.operations:
            ref = f"operation:{operation_id}"
            evidence[f"metadata:{ref}"] = AvailableEvidence(
                record_ref=f"metadata:{ref}", kind="execution_metadata", operation_ref=ref,
                available=True, visible=False, complete=False,
                source_refs=[ref],
            )
        return evidence


def _candidate(call: dict[str, Any], history: _History, response_ref: str) -> CandidateAction:
    name, arguments = str(call.get("name") or "unknown_tool"), call["args"]
    conditions: dict[str, Any] = {}
    information_key = None
    artifact_ref = None
    operation_ref = None
    cost: dict[str, Any] = {"executes_candidate": False}
    kind = "other"
    if call.get("parse_errors"):
        return CandidateAction(
            tool_call_id=call["id"], name=name, arguments=arguments, kind="other",
            source_refs=[response_ref], estimated_cost={"executes_candidate": False,
                                                       "input_parse_valid": False},
        )
    if name == "simulate_check":
        kind = "test"
        conditions = _conditions(arguments)
        # 固定模拟工具源码定义的执行位置；不是模型或 Oracle 推断。
        conditions.update(executor_id="control_local", execution_semantics="local_simulator")
        information_key = _information(conditions)
        cost["side_effects"] = "local_simulation_only"
    elif name == "read_artifact":
        kind = "read"
        artifact_id = str(arguments.get("artifact_id") or "")
        artifact_ref = f"artifact:{artifact_id}"
        information_key = artifact_ref
        row = history.artifacts.get(artifact_id) or {}
        cost["source_bytes"] = row.get("byte_size")
        if row.get("source_operation_id"):
            operation_ref = f"operation:{row['source_operation_id']}"
    elif name in {"read_local_execution", "get_graph", "search_history"}:
        kind = "query"
        if arguments.get("operation_id"):
            operation_ref = f"operation:{arguments['operation_id']}"
    return CandidateAction(
        tool_call_id=call["id"], name=name, arguments=arguments, kind=kind,
        conditions=conditions, information_key=information_key, artifact_ref=artifact_ref,
        operation_ref=operation_ref, source_refs=[response_ref], estimated_cost=cost,
    )


def _visible_body(
    history: _History, artifact_id: str, text: str, visibility_refs: list[str], *,
    complete: bool, claimed_sha256: Any = None,
) -> AvailableEvidence | None:
    row = history.artifacts.get(artifact_id)
    if row is None:
        return None
    raw = history.verified_bytes(artifact_id)
    exact = raw is not None and text.encode("utf-8") == raw
    ref = f"artifact:{artifact_id}"
    operation_id = row.get("source_operation_id")
    verified = exact and claimed_sha256 in {None, row.get("sha256")}
    return AvailableEvidence(
        record_ref=ref, kind="artifact_body", operation_ref=f"operation:{operation_id}" if operation_id else None,
        source_refs=[ref] + ([f"operation:{operation_id}"] if operation_id else []),
        available=row.get("availability") == "available", visible=True, complete=complete,
        provenance_verified=verified, content=text, sha256=row.get("sha256"),
        conditions=_body_conditions(_object(text)) if verified and complete else {},
        visibility_refs=visibility_refs,
    )


def _read_result(
    history: _History, call: dict[str, Any], message: dict[str, Any], message_ref: str,
) -> list[AvailableEvidence]:
    payload = _object(message.get("content"))
    data = _mapping(payload.get("data"))
    artifact_id = str(call["args"].get("artifact_id") or "")
    text = data.get("text")
    if data.get("artifact_id") != artifact_id or not isinstance(text, str):
        return []
    complete = (
        payload.get("status") == "succeeded" and data.get("offset") == 0
        and data.get("truncated") is False and payload.get("truncated") is False
        and data.get("returned_bytes") == data.get("byte_size")
    )
    body = _visible_body(history, artifact_id, text, [message_ref], complete=complete,
                         claimed_sha256=data.get("sha256"))
    expected_operation = (history.artifacts.get(artifact_id) or {}).get("source_operation_id")
    if body is not None and data.get("source_operation_id") != expected_operation:
        body = body.model_copy(update={"provenance_verified": False, "conditions": {}})
    evidence = [body] if body else []
    # 归档正常视图可能重排 JSON。先验证原始归档 hash 和对象同一性，再验证
    # 内嵌 AI/Tool 唯一配对及独立 Artifact 原字节；不凭相似内容升级信任。
    raw = history.verified_bytes(artifact_id)
    archive = _object(text)
    if not complete or raw is None or data.get("sha256") != hashlib.sha256(raw).hexdigest():
        return evidence
    try:
        original = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return evidence
    if archive != original or not isinstance(archive.get("messages"), list):
        return evidence
    for index, nested_call, nested_message in _pairs(archive["messages"]):
        if nested_call["name"] != "read_artifact" or not isinstance(nested_message.get("content"), str):
            continue
        nested_id = str(nested_call["args"].get("artifact_id") or "")
        nested = _visible_body(
            history, nested_id, nested_message["content"],
            [message_ref, f"artifact:{artifact_id}#/messages/{index}",
             f"archive_tool_call:{nested_call['id']}"], complete=True,
        )
        if nested is not None and nested.provenance_verified:
            evidence.append(nested)
    return evidence


def _completed(
    history: _History, messages: list[dict[str, Any]], request_ref: str,
    evidence: dict[str, AvailableEvidence], actions: list[HistoricalAction],
    excluded_ids: set[str],
) -> list[str]:
    completed = []
    for index, call, message in _pairs(messages):
        if call["id"] in excluded_ids:
            continue
        message_ref = f"{request_ref}/messages/{index}:tool_call:{call['id']}"
        completed.append(call["id"])
        if call["name"] == "read_artifact":
            for body in _read_result(history, call, message, message_ref):
                evidence[body.record_ref] = body
        payload = _object(message.get("content"))
        if call["name"] == "read_local_execution":
            operation = _mapping(payload.get("operation"))
            requested_id = str(call["args"].get("operation_id") or "")
            ref = f"operation:{requested_id}"
            row = history.operations.get(requested_id)
            if row is not None and operation.get("record_ref") == ref:
                status_matches = operation.get("operation_status") == row.get("status")
                evidence[f"state:{ref}"] = AvailableEvidence(
                    record_ref=f"state:{ref}", kind="execution_state", operation_ref=ref,
                    content=_json(operation), visible=True, complete=status_matches,
                    provenance_verified=status_matches, source_refs=[ref, message_ref],
                    visibility_refs=[message_ref],
                )
        if call["name"] == "simulate_check" and payload.get("simulated_only") is True:
            operation_id = payload.get("operation_id")
            if not isinstance(operation_id, str):
                continue
            conditions = _conditions(call["args"])
            conditions.update(executor_id="control_local", execution_semantics="local_simulator")
            ref = f"operation:{operation_id}"
            result = _mapping(payload.get("result"))
            result_valid = isinstance(payload.get("result"), dict) and all(
                payload.get(key) == call["args"].get(key)
                for key in ("route", "environment", "configuration")
            )
            actions.append(HistoricalAction(
                record_ref=ref, tool_name="simulate_check", conditions=conditions,
                information_key=_information(conditions),
                status=("unknown" if not result_valid else
                        "failed" if result.get("performed") is False else "succeeded"),
                source_refs=[ref, message_ref],
            ))
            evidence[ref] = AvailableEvidence(
                record_ref=ref, kind="execution_metadata", operation_ref=ref, content=_json(payload),
                visible=True, complete=result_valid, provenance_verified=result_valid,
                source_refs=[ref, message_ref],
                visibility_refs=[message_ref], conditions=conditions,
            )
        if call["name"] in {"get_graph", "update_graph"}:
            edges = _mapping(payload.get("attack_graph")).get("edges")
            for edge in edges if isinstance(edges, list) else []:
                if not isinstance(edge, dict):
                    continue
                refs = [ref for ref in edge.get("source_refs") or [] if isinstance(ref, str)]
                operation_ref = next((ref for ref in refs if ref.startswith("operation:")), None)
                ref = f"attempt:{edge.get('id', call['id'])}"
                evidence[ref] = AvailableEvidence(
                    record_ref=ref, kind="model_claim", operation_ref=operation_ref,
                    source_refs=[*refs, message_ref], visible=True, content=_json(edge),
                    complete=False, provenance_verified=False, visibility_refs=[message_ref],
                )
            if payload.get("semantic_verification") == "model_judgment":
                ref = f"graph_write:{call['id']}"
                evidence[ref] = AvailableEvidence(
                    record_ref=ref, kind="model_claim", source_refs=[message_ref], visible=True,
                    content=_json(payload), visibility_refs=[message_ref],
                )
    # 只有实际可见、完整且已验证的原始正文可以补充历史条件。
    for action_index, action in enumerate(actions):
        conditions = dict(action.conditions)
        refs = list(action.source_refs)
        for item in evidence.values():
            if (item.operation_ref == action.record_ref and item.visible and item.complete
                    and item.provenance_verified and item.kind == "artifact_body"):
                conditions.update(item.conditions)
                refs.extend(item.visibility_refs)
        actions[action_index] = action.model_copy(update={"conditions": conditions, "source_refs": refs})
    return completed


def _board_metadata(
    history: _History, messages: list[dict[str, Any]], request_ref: str,
    evidence: dict[str, AvailableEvidence],
) -> None:
    """只接受实际 SystemMessage 内结构化工作板的执行观察，不解析自由文本声明。"""
    decoder = json.JSONDecoder()
    marker = re.compile(r'\{\s*"schema_version"\s*:\s*"decision_blackboard/1"')
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        text = message.get("content")
        if not isinstance(text, str):
            continue
        for match in marker.finditer(text):
            try:
                board, _ = decoder.raw_decode(text, match.start())
            except ValueError:
                continue
            units = board.get("relevant_evidence") if isinstance(board, dict) else None
            for unit_index, unit in enumerate(units if isinstance(units, list) else []):
                if not isinstance(unit, dict) or unit.get("origin_kind") != "execution_observation":
                    continue
                ref = unit.get("record_ref")
                if not isinstance(ref, str) or not ref.startswith("operation:"):
                    continue
                operation_id = ref.split(":", 1)[1]
                row = history.operations.get(operation_id)
                statement = _mapping(unit.get("statement"))
                execution = _mapping(unit.get("execution"))
                if row is None or statement.get("tool") != row.get("tool_name"):
                    continue
                result_fields = _mapping(statement.get("result_fields"))
                expected_fields = _mapping(_mapping(row.get("result_snapshot")).get("data"))
                matches = execution.get("operation_status") == row.get("status") and all(
                    expected_fields.get(key) == value for key, value in result_fields.items()
                )
                jobs = execution.get("jobs_last_observed")
                for job in jobs if isinstance(jobs, list) else []:
                    if not isinstance(job, dict):
                        matches = False
                        continue
                    job_ref = str(job.get("record_ref") or "")
                    known = history.jobs.get(job_ref.removeprefix("job:"))
                    if (known is None or known.get("operation_id") != operation_id
                            or known.get("state") != job.get("last_observed_state")):
                        matches = False
                visibility = f"{request_ref}/messages/{message_index}/board/relevant_evidence/{unit_index}"
                metadata = {"record_ref": ref, "statement": statement, "execution": execution,
                            "observed_dimensions": unit.get("observed_dimensions")}
                evidence[f"metadata:{ref}"] = AvailableEvidence(
                    record_ref=f"metadata:{ref}", kind="execution_metadata", operation_ref=ref,
                    available=True, visible=True, complete=False, provenance_verified=matches,
                    content=_json(metadata), source_refs=[ref, visibility], visibility_refs=[visibility],
                )


def _task(messages: list[dict[str, Any]], request_ref: str) -> TaskQuestion:
    users = [message for message in messages if isinstance(message, dict)
             and message.get("role") in {"user", "human"}]
    text = str(users[-1].get("content") or "") if users else ""
    # 严格识别本固定历史问题，不由最终答复或研究报告推断目标。
    historical_a = "历史检查 A" in text and "仅就资产 Q" in text and "适用范围" in text
    blocked_b = "本轮目标仅是资产 Q 上的 B；核对它当前是否能够推进" in text
    unknown_u = "核对原 U 执行结果目前能确定到什么程度，同时考虑独立 C 记录" in text
    return TaskQuestion(
        goal=("interpret_history" if historical_a else "progress_blocked" if blocked_b
              else "investigate_unknown" if unknown_u else "unspecified"), text=text,
        focus_conditions=({"route": "A", "environment": "Q"} if historical_a else
                          {"route": "B", "environment": "Q"} if blocked_b else
                          {"route": "U"} if unknown_u else {}),
        source_refs=[request_ref],
    )


def build_snapshots(
    history: dict[str, Any], result: dict[str, Any], arm: str, source_path: str,
    *, origin: str = "real_recorded_trajectory",
) -> list[ActionSnapshot]:
    """纯解析单条轨迹。result 最终 messages/actions/semantic_review 均不读取。"""
    fixed = _History(history)
    snapshots = []
    previous_ids: set[str] = set()
    for position, physical in enumerate(result.get("calls") or []):
        index = physical.get("index", position + 1)
        request = physical.get("request") or {}
        messages = request.get("messages") or []
        if not isinstance(messages, list):
            messages = []
        request_ref = f"{source_path}#/calls/{position}/request"
        actual_hash = _hash(request)
        expected_hash = physical.get("visible_request_sha256")
        limitations = [
            "固定历史元数据在系统可用，不等于原始正文已进入当前模型请求。",
            "完整原始 wire 请求未另存；wire_sha256 为原记录，visible_request_sha256 可重算。",
            "自由文本图声明及未显式记录的身份、环境、配置维度不猜测。",
            "请求中出现来源 UUID 只证明导航标识可见，不证明其执行状态或完整元数据已展示。",
        ]
        if expected_hash != actual_hash:
            limitations.append("visible_request_sha256 缺失或不一致；该快照来源未通过完整性核验。")
        legacy_offline = origin == "recorded_offline_script" and expected_hash is None
        if legacy_offline:
            limitations.append("旧离线预检未保存响应视图；候选仅由下一请求中新增的原 AI ToolCall 重建，未读取其 Tool 结果。")
            limitations.append("旧可见导出没有单独请求 hash；只核验独立 Artifact 原字节，不声称完整 wire 已复原。")
        response = _mapping(physical.get("visible_response_message"))
        current_calls = _calls(response)
        batch_ids = [call["id"] for call in current_calls]
        evidence = fixed.directories()
        actions = fixed.actions()
        _board_metadata(fixed, messages, request_ref, evidence)
        completed = _completed(fixed, messages, request_ref, evidence, actions, set(batch_ids))
        if expected_hash != actual_hash and not legacy_offline:
            actions = fixed.actions()
            evidence = {
                ref: item.model_copy(update={"provenance_verified": False, "complete": False,
                                             "conditions": {}})
                for ref, item in evidence.items()
            }
        for tool_index, call in enumerate(current_calls):
            response_ref = (f"{physical['reconstructed_candidate_source']}/tool_calls/{tool_index}"
                            if physical.get("reconstructed_candidate_source") else
                            f"{source_path}#/calls/{position}/visible_response_message/tool_calls/{tool_index}")
            candidate = _candidate(call, fixed, response_ref)
            local_limits = list(limitations)
            if call["parse_errors"]:
                local_limits.append("候选工具输入未通过解析：" + ", ".join(call["parse_errors"]))
            if call["id"] in completed or call["id"] in previous_ids:
                local_limits.append("候选 ToolCall ID 重复；不能视为独立新操作。")
            mismatch = None
            operation_id = candidate.arguments.get("operation_id")
            if candidate.name == "read_local_execution" and operation_id in fixed.jobs:
                mismatch = {
                    "supplied_kind": "job", "supplied_id": operation_id,
                    "expected_kind": "operation",
                    "related_operation_ref": f"operation:{fixed.jobs[operation_id]['operation_id']}",
                    "source_ref": f"job:{operation_id}",
                }
            snapshots.append(ActionSnapshot(
                snapshot_id=(f"{'offline' if legacy_offline else 'real'}-{arm}-p{index}-{call['id']}"
                             + (f"-invalid-position-{tool_index}" if call["parse_errors"] else "")),
                task=_task(messages, request_ref),
                candidate=candidate, historical_actions=actions, evidence=list(evidence.values()),
                boundary={
                    "sample_origin": origin, "arm": arm,
                    "case": result.get("case"), "run_id": result.get("run_id"),
                    "thread_id": result.get("thread_id"), "physical_request_index": index,
                    "phase": physical.get("phase"), "candidate_position": tool_index,
                    "candidate_caller": response_ref, "request_ref": request_ref,
                    "candidate_input_valid": not call["parse_errors"],
                    "candidate_parse_diagnostics": call["parse_errors"],
                    "candidate_reconstructed_from_recorded_ai": legacy_offline,
                    "wire_sha256_recorded": physical.get("wire_sha256"),
                    "visible_request_sha256_recorded": expected_hash,
                    "visible_request_sha256_computed": actual_hash,
                    "request_integrity_verified": expected_hash == actual_hash,
                    "pre_request_message_count": len(messages),
                    "completed_tool_call_ids": completed,
                    "same_batch_tool_call_ids_excluded": batch_ids,
                    "current_response_content_used": False, "future_results_used": False,
                    "system_available_operation_count": len(fixed.operations),
                    "identifier_navigation_refs": [f"operation:{operation_id}"
                        for operation_id in fixed.operations if operation_id in _json(request)],
                    "identity_namespace_mismatch": mismatch,
                }, limitations=local_limits,
            ))
        previous_ids.update(batch_ids)
    return snapshots


def build_real_snapshots() -> list[ActionSnapshot]:
    """读取固定原件并验证历史 hash；不改写任一原实验文件。"""
    raw = HISTORY_PATH.read_bytes()
    history = json.loads(raw)
    snapshots = []
    for arm, path in RESULT_PATHS.items():
        result = json.loads(path.read_text(encoding="utf-8"))
        if hashlib.sha256(raw).hexdigest() != result.get("history_sha256"):
            raise ValueError("recorded_history_sha256_mismatch")
        snapshots.extend(build_snapshots(history, result, arm, path.relative_to(ROOT).as_posix()))
    return snapshots


def build_recorded_offline_snapshots() -> list[ActionSnapshot]:
    """复用旧 S15/S17 脚本预检，只作边界测试，不冒充真实模型决策。"""
    history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    snapshots = []
    for case in ("S15-C", "S17-C"):
        path = ROOT / f"experiments/decision_memory_live_v1/preflight/{case}.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        if original.get("real_provider_requests") != 0:
            raise ValueError("legacy_offline_source_is_not_scripted")
        result = copy.deepcopy(original)
        source_path = path.relative_to(ROOT).as_posix()
        calls = result.get("calls") or []
        for index, physical in enumerate(calls[:-1]):
            if physical.get("finish_reason") != "tool_calls":
                continue
            current = physical["request"]["messages"]
            prior_ids = {call["id"] for message in current for call in _calls(message)}
            matches = []
            for message_index, message in enumerate(calls[index + 1]["request"]["messages"]):
                candidate_calls = _calls(message) if message.get("role") == "assistant" else []
                if (candidate_calls and all(call["id"] not in prior_ids for call in candidate_calls)
                        and [call["name"] for call in candidate_calls] == physical.get("native_tool_names")):
                    matches.append((message_index, message))
            if len(matches) != 1:
                raise ValueError("legacy_candidate_reconstruction_is_ambiguous")
            message_index, message = matches[0]
            physical["visible_response_message"] = {"tool_calls": copy.deepcopy(message["tool_calls"])}
            physical["reconstructed_candidate_source"] = (
                f"{source_path}#/calls/{index + 1}/request/messages/{message_index}"
            )
        snapshots.extend(build_snapshots(history, result, case, source_path,
                                         origin="recorded_offline_script"))
    return snapshots
