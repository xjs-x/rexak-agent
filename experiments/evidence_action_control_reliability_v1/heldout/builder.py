"""Build and seal author-isolated offline examples; never import a detector."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
DTO = ROOT / "experiments/evidence_action_control_v1/contracts.py"
sys.path.insert(0, str(ROOT / "src"))
spec = importlib.util.spec_from_file_location("isolated_holdout_contracts", DTO)
assert spec is not None and spec.loader is not None
contracts = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = contracts
spec.loader.exec_module(contracts)

INPUTS: list[dict[str, Any]] = []
ORACLE: list[dict[str, Any]] = []


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def enc(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def cond(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "target": "https://fixture.invalid/status",
        "method": "GET",
        "identity": "reader-a",
        "config_revision": "cfg-7",
        "request_profile": "minimal-status",
        "prerequisite": "ready",
    }
    value.update(changes)
    return value


def hist(op: str, conditions: dict[str, Any], *, status: str = "succeeded", key: str = "status-response", name: str = "probe_status", artifacts: list[str] | None = None, origin: str = "execution_observation") -> dict[str, Any]:
    return {
        "record_ref": op, "tool_name": name, "conditions": conditions,
        "information_key": key, "status": status,
        "job_refs": [f"job:{op}"], "job_states": {f"job:{op}": status},
        "artifact_refs": artifacts or [], "source_refs": [f"ledger:{op}"],
        "origin_kind": origin,
    }


def body(ref: str, op: str | None, content: str, *, visible: bool = True, complete: bool = True, verified: bool = True, conditions: dict[str, Any] | None = None, kind: str = "artifact_body") -> dict[str, Any]:
    return {
        "record_ref": ref, "kind": kind, "operation_ref": op,
        "source_refs": [f"fixture-source:{ref}"], "available": True,
        "visible": visible, "complete": complete, "provenance_verified": verified,
        "content": content, "sha256": sha(content.encode("utf-8")),
        "conditions": conditions or {},
        "visibility_refs": [f"tool-message:{ref}" if visible else f"visibility:hidden:{ref}"],
    }


def cand(case: str, *, kind: str = "test", name: str = "probe_status", conditions: dict[str, Any] | None = None, op: str | None = None, artifact: str | None = None, key: str | None = "status-response", arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "tool_call_id": f"call:new:{case}", "name": name,
        "arguments": arguments or {"fixture_only": True}, "kind": kind,
        "conditions": conditions or {}, "information_key": key,
        "operation_ref": op, "artifact_ref": artifact,
        "source_refs": [f"candidate-proposal:{case}"],
        "estimated_cost": {"network_calls_if_executed": 1 if kind == "test" else 0},
    }


def add(number: int, title: str, *, goal: str, question: str, focus: dict[str, Any], candidate: dict[str, Any], history: list[dict[str, Any]], evidence: list[dict[str, Any]], labels: tuple[str | list[str], str | list[str], str | list[str], str | list[str], str | list[str]], reasons: list[str], boundary: dict[str, Any] | None = None, limitations: list[str] | None = None) -> None:
    sid = f"holdout-{number:02d}"
    snapshot = {
        "snapshot_id": sid,
        "task": {"goal": goal, "text": question, "focus_conditions": focus, "source_refs": [f"task:{sid}"]},
        "candidate": candidate, "historical_actions": history, "evidence": evidence,
        "boundary": {"offline_fixture": True, "phase": "agent", "evidence_cutoff": "before_candidate_execution", **(boundary or {})},
        "limitations": limitations or [],
    }
    validated = contracts.ActionSnapshot.model_validate(snapshot)
    INPUTS.append(validated.model_dump(mode="json"))
    dims = ["equivalence", "logical_classification", "evidence_sufficiency", "estimated_action_value", "repeat_risk"]
    expected = {dimension: value if isinstance(value, list) else [value] for dimension, value in zip(dims, labels, strict=True)}
    ORACLE.append({
        "sample_id": sid, "origin": "author_isolated_holdout", "title": title,
        "expected": expected,
        "rationale": "；".join(f"{dimension}: {reason}" for dimension, reason in zip(dims, reasons, strict=True)),
        "dimension_rationale": dict(zip(dims, reasons, strict=True)),
        "admissible_set_policy": "A multi-label set is fixed before any prediction and justified only by the stated contract ambiguity.",
    })


C = cond()

add(1, "新工具调用身份重放同一逻辑操作", goal="interpret_history", question="解释 op:01 当时为何返回正常，不要求重新验证当前环境。", focus=C,
    candidate=cand("01", conditions=C, op="op:01"), history=[hist("op:01", C, artifacts=["artifact:01"])],
    evidence=[body("artifact:01", "op:01", "Operation op:01; cfg-7; reader-a; HTTP 200; body status=ready.", conditions=C)],
    labels=("EXACT_REPEAT", "EXACT_REPEAT", "SUFFICIENT", "LOW", "HIGH"),
    reasons=["tool_call_id 改变不改变明确相同的 operation_ref。", "请求条件和逻辑操作均相同。", "已见完整且来源核对过的原操作正文回答历史问题。", "再次执行不能解释出新的历史事实。", "已有同一操作成功正文，重放的重复风险高。"])

add(2, "新逻辑操作但请求完全相同", goal="verify_current", question="核对 cfg-7 下 reader-a 的状态；证据快照与候选之间无环境变化。", focus=C,
    candidate=cand("02", conditions=C, op="op:02-new"), history=[hist("op:02-old", C, artifacts=["artifact:02"])],
    evidence=[body("artifact:02", "op:02-old", "cfg-7 reader-a status request completed: HTTP 200, status=ready; observed at the current unchanged boundary.", conditions=C)],
    labels=("SEMANTIC_REPEAT", "SEMANTIC_REPEAT", "SUFFICIENT", "LOW", "HIGH"),
    reasons=["新 operation_ref 阻止按执行身份视为同一次重放，但语义相同。", "目标、身份、配置和请求均无实质差异。", "当前边界内已有完整响应，无时效或变化缺口。", "新 ID 不能制造新信息。", "同条件成功已被读到。"])

add(3, "配置改变不扩大历史解释问题", goal="interpret_history", question="只解释 cfg-7 的 op:03-old 响应；候选提出的 cfg-8 不属于任务。", focus=C,
    candidate=cand("03", conditions=cond(config_revision="cfg-8"), op="op:03-new"), history=[hist("op:03-old", C, artifacts=["artifact:03"])],
    evidence=[body("artifact:03", "op:03-old", "For cfg-7 and reader-a, status=ready, HTTP 200. The original response is complete.", conditions=C)],
    labels=("CONDITION_CHANGED", "CONDITION_CHANGED", "SUFFICIENT", "LOW", "LOW"),
    reasons=["cfg-8 与原 cfg-7 明确不同。", "配置是实质请求条件。", "历史任务固定在 cfg-7，原响应正文足够。", "当前新配置测试不回答已解决的历史问题。", "它会测试不同条件，因此低重复风险与低任务价值可以同时成立。"])

add(4, "相同配置变化回答当前问题", goal="verify_current", question="现在已部署 cfg-8，核对 reader-a 当前状态。", focus=cond(config_revision="cfg-8"),
    candidate=cand("04", conditions=cond(config_revision="cfg-8"), op="op:04-new"), history=[hist("op:04-old", C, artifacts=["artifact:04-old"])],
    evidence=[body("artifact:04-old", "op:04-old", "cfg-7 returned HTTP 200 status=ready.", conditions=C), body("state:04", None, "Deployment receipt: cfg-8 is active; no post-deployment status response has been collected.", conditions={"config_revision": "cfg-8"}, kind="execution_state")],
    labels=("CONDITION_CHANGED", "CONDITION_CHANGED", "PARTIAL", "HIGH", "LOW"),
    reasons=["已验证的配置切换形成新条件。", "旧测试针对 cfg-7，新测试针对 cfg-8。", "新配置生效被证实，效果正文仍缺少。", "一次新配置测试直接补上问题所需结果。", "旧配置成功不能把新配置测试判作高风险重复。"])

add(5, "证据充分后读取明确无关历史", goal="interpret_history", question="解释 op:05 的状态响应。", focus=C,
    candidate=cand("05", kind="read", name="read_artifact", artifact="archive:other-project", key="other-project-archive", arguments={"artifact_id": "archive:other-project"}), history=[hist("op:05", C, artifacts=["artifact:05"])],
    evidence=[body("artifact:05", "op:05", "op:05, cfg-7, reader-a: HTTP 200, status=ready.", conditions=C), body("archive:other-project", "op:other-project", "Unrelated fixture project deployment notes.", visible=False, conditions={"relevance_refs": ["task:other-project"], "artifact_scope": "other-project"})],
    labels=("UNRELATED_ACTION", "UNRELATED_ACTION", "SUFFICIENT", "LOW", "LOW"),
    reasons=["无关归档读取与原状态测试不等价。", "信息目标也明确属于另一个项目。", "原测试完整正文已经可见。", "无关历史不能增加这个历史解释的证据。", "没有相同归档的既往读取，不应把低价值等同高重复。"])

large_archive = "ARCHIVE HEADER: source operation op:06; raw response is retained below.\n" + ("unrelated-log-line\n" * 20000) + "\nORIGINAL RESPONSE: cfg-7 reader-a HTTP 503; body dependency=queue-unavailable.\n"
add(6, "大归档有动作前可见关联且必要正文未读", goal="interpret_history", question="解释 op:06 为什么失败；需要原响应中的原因。", focus=C,
    candidate=cand("06", kind="read", name="read_artifact", artifact="archive:06", key="op:06-original-response", arguments={"artifact_id": "archive:06", "selector": "original-response"}), history=[hist("op:06", C, status="failed", artifacts=["archive:06"])],
    evidence=[body("state:06", "op:06", "Execution failed; body archived as archive:06; failure reason not present in this receipt.", conditions=C, kind="execution_state"), body("archive:06", "op:06", large_archive, visible=False, conditions={"relevance_refs": ["task:holdout-06", "op:06"], "artifact_read_scope": "original-response", "size_bytes": len(large_archive.encode("utf-8"))})],
    labels=("UNRELATED_ACTION", "UNRELATED_ACTION", "PARTIAL", "HIGH", "LOW"),
    reasons=["读取原始正文不是重复执行测试。", "二者是不同操作种类。", "失败状态可见，具体失败原因仍未见，隐藏正文不计入充分性。", "可见归属与关联明确指向任务需要的原正文，体积不消除价值。", "尚无这段正文已被读取的证据。"])

add(7, "隐藏正文碰巧提到答案但可见归属无关", goal="interpret_history", question="解释 op:07 的失败，原响应不可用。", focus=C,
    candidate=cand("07", kind="read", name="read_artifact", artifact="archive:07-other", key="archive-other", arguments={"artifact_id": "archive:07-other"}), history=[hist("op:07", C, status="failed")],
    evidence=[body("state:07", "op:07", "op:07 failed; original response unavailable.", conditions=C, kind="execution_state"), body("archive:07-other", "op:unrelated", "Private unrelated notes accidentally quote: op:07 dependency=queue-unavailable.", visible=False, conditions={"relevance_refs": ["task:unrelated"], "artifact_scope": "unrelated archive"})],
    labels=("UNRELATED_ACTION", "UNRELATED_ACTION", "PARTIAL", "LOW", "LOW"),
    reasons=["候选读取另一操作的归档。", "动作前可见索引明确指向无关任务。", "只知道失败，隐藏偶然文本不能补足原因。", "不能读隐藏 content 猜测归档关联；可见资料不支持本次读取价值。", "无重复读取历史。"])

graph_old = {"revision": 11, "visible_revision": 11, "provenance_verified": True, "source_refs": ["checkpoint:g11"]}
graph_new = {"revision": 12, "visible_revision": 11, "provenance_verified": True, "source_refs": ["checkpoint:g12"]}
add(8, "图已修订而当前修订未读", goal="progress_blocked", question="查明当前图中对 queue 前提的最新记录，以决定后续核查。", focus={"graph_revision": 12, "topic": "queue-prerequisite"},
    candidate=cand("08", kind="query", name="query_graph", conditions={"graph_revision": 12, "topic": "queue-prerequisite"}, key="graph:queue-prerequisite", arguments={"revision": 12, "topic": "queue-prerequisite"}), history=[hist("op:graph-read-11", {"graph_revision": 11, "topic": "queue-prerequisite"}, key="graph:queue-prerequisite", name="query_graph")],
    evidence=[body("graph-body:11", "op:graph-read-11", "Graph revision 11: queue readiness is unresolved; refs=[receipt:queue-before].", conditions={"graph_revision": 11, "artifact_read_scope": "queue-prerequisite"})], boundary={"graph_context": graph_new},
    labels=("CONDITION_CHANGED", "CONDITION_CHANGED", "PARTIAL", "HIGH", "LOW"),
    reasons=["可信图元数据记录 revision 12 已出现，旧读停在 11。", "查询主题相同但版本变了。", "旧图不足以回答当前图记录。", "读取新修订能获得尚未可见的信息，仍需后续核对底层证据。", "不能仅因主题相同就把新修订查询判成高重复。"])

add(9, "图未修订且同范围已见", goal="interpret_history", question="解释图 revision 12 对 queue 前提记录了什么。", focus={"graph_revision": 12, "topic": "queue-prerequisite"},
    candidate=cand("09", kind="query", name="query_graph", conditions={"graph_revision": 12, "topic": "queue-prerequisite"}, op="op:graph-read-12", key="graph:queue-prerequisite", arguments={"revision": 12, "topic": "queue-prerequisite"}), history=[hist("op:graph-read-12", {"graph_revision": 12, "topic": "queue-prerequisite"}, key="graph:queue-prerequisite", name="query_graph")],
    evidence=[body("graph-body:12", "op:graph-read-12", "Graph revision 12: queue_ready is a candidate supported by receipt:queue-after; effect remains untested.", conditions={"graph_revision": 12, "artifact_read_scope": "queue-prerequisite"})], boundary={"graph_context": {**graph_old, "revision": 12, "visible_revision": 12, "source_refs": ["checkpoint:g12"]}},
    labels=("EXACT_REPEAT", "EXACT_REPEAT", "SUFFICIENT", "LOW", "HIGH"),
    reasons=["相同 operation_ref 和相同图读范围。", "版本和主题都不变。", "问题仅问图记录本身，完整当前图正文已见。", "原样重读不能增加信息。", "明确的已读范围与未变化版本支持高重复风险。"])

add(10, "图版本声称变化但来源未核验", goal="progress_blocked", question="查明当前图是否出现关于 queue 的新记录。", focus={"topic": "queue-prerequisite"},
    candidate=cand("10", kind="query", name="query_graph", conditions={"graph_revision": 13, "topic": "queue-prerequisite"}, key="graph:queue-prerequisite", arguments={"revision": 13, "topic": "queue-prerequisite"}), history=[hist("op:graph-read-11", {"graph_revision": 11, "topic": "queue-prerequisite"}, key="graph:queue-prerequisite", name="query_graph")],
    evidence=[body("graph-claim:13", None, "A summary says graph revision 13 exists; checkpoint provenance is unavailable.", verified=False, kind="summary_claim", conditions={"claimed_revision": 13})], boundary={"graph_context": {"revision": 13, "visible_revision": None, "provenance_verified": False, "source_refs": ["summary:claimed-g13"]}},
    labels=("INSUFFICIENT_INFORMATION", ["CONDITION_CHANGED", "INSUFFICIENT_INFORMATION"], "INDETERMINATE", "POTENTIAL", "UNCERTAIN"),
    reasons=["无法确认声称的新版本是否真是可查询当前图。", "候选声明版本与历史不同，但 DTO 未规定未核验版本能否算逻辑条件差异，因此事前只允许这两个判断。", "当前图内容与版本均无可靠可见观察。", "核对新图有潜在收益，但新版本存在性未被证实。", "缺少可信版本和已读范围，无法确定是否只是重读。"], limitations=["Graph revision metadata is explicitly unverified; the DTO has no authoritative current-checkpoint resolver."])

add(11, "明确 GraphReview 阶段首次读取待审核图", goal="interpret_history", question="审核图 revision 21 是否忠实引用原始证据，先读取待审核图。", focus={"graph_revision": 21, "review_target": "source-attribution"},
    candidate=cand("11", kind="query", name="query_graph", conditions={"graph_revision": 21, "topic": "source-attribution"}, key="graph:source-attribution", arguments={"revision": 21, "topic": "source-attribution"}), history=[],
    evidence=[body("graph-body:21", None, "Revision 21 links candidate queue_ready to receipt:queue-after.", visible=False, conditions={"graph_revision": 21, "relevance_refs": ["task:holdout-11"], "artifact_read_scope": "source-attribution"}), body("review-receipt:21", None, "Graph revision 21 is available for source-attribution review; its body has not been exposed in a ToolMessage.", conditions={"graph_revision": 21}, kind="execution_metadata")], boundary={"phase": "graph_review", "graph_context": {"revision": 21, "visible_revision": None, "provenance_verified": True, "source_refs": ["checkpoint:g21"]}},
    labels=("UNRELATED_ACTION", "UNRELATED_ACTION", "PARTIAL", "HIGH", "LOW"),
    reasons=["没有先前相关查询可构成重复。", "这是新的图审核信息目标。", "可见收据仅证明待审图存在，未见正文。", "读取图是显式审核任务的必要前置步骤。", "不能把 GraphReview 的必要首次读当成普通阶段冗余检索。"])

add(12, "原执行未知时另起同条件测试", goal="investigate_unknown", question="查清 op:12-original 到底是否执行及其结果，不是仅求一次新响应。", focus=C,
    candidate=cand("12", conditions=C, op="op:12-new"), history=[hist("op:12-original", C, status="unknown")],
    evidence=[body("state:12", "op:12-original", "Dispatch acknowledgement was lost. Ledger execution state is unknown; query original job before a fresh execution.", conditions=C, kind="execution_state")],
    labels=("SEMANTIC_REPEAT", "SEMANTIC_REPEAT", "INDETERMINATE", "LOW", "HIGH"),
    reasons=["新逻辑操作和原未知操作做相同请求。", "请求条件相同且没有新问题维度。", "原执行发生与否仍未知。", "新响应不能证明原操作的执行状态。", "原请求可能已执行，再发相同请求存在明确重复执行风险。"])

add(13, "未知旧执行不抹去独立同条件成功证据", goal="verify_current", question="核对当前 cfg-7 reader-a 状态；无需判断早先未知操作本身。", focus=C,
    candidate=cand("13", conditions=C, op="op:13-third"), history=[hist("op:13-unknown", C, status="unknown"), hist("op:13-independent", C, artifacts=["artifact:13"])],
    evidence=[body("state:13-unknown", "op:13-unknown", "Original dispatch remains unknown.", conditions=C, kind="execution_state"), body("artifact:13", "op:13-independent", "Independent job op:13-independent, cfg-7 reader-a, current boundary: HTTP 200 status=ready.", conditions=C)],
    labels=("SEMANTIC_REPEAT", "SEMANTIC_REPEAT", "SUFFICIENT", "LOW", "HIGH"),
    reasons=["与独立成功请求相同，新操作 ID 不改变语义。", "独立成功具备完全相同的当前条件。", "任务问当前结果，独立完整正文回答它；旧操作未知事实保留但不阻断这个结论。", "第三次请求不补充任务信息。", "至少一份已见同条件成功使重复风险高。"])

add(14, "他身份成功不能结清原操作未知", goal="investigate_unknown", question="核对 op:14-original 的实际执行状态；reader-b 成功是另一次请求。", focus=C,
    candidate=cand("14", kind="query", name="get_job", conditions={"job_ref": "job:op:14-original"}, op="op:14-original", key="execution-state:op:14-original", arguments={"job_ref": "job:op:14-original"}), history=[hist("op:14-original", C, status="unknown"), hist("op:14-independent", cond(identity="reader-b"), artifacts=["artifact:14"])],
    evidence=[body("state:14", "op:14-original", "Original job execution state remains unknown.", conditions=C, kind="execution_state"), body("artifact:14", "op:14-independent", "reader-b returned HTTP 200 under cfg-7; this does not identify reader-a's earlier dispatch.", conditions=cond(identity="reader-b"))],
    labels=("UNRELATED_ACTION", "UNRELATED_ACTION", "INDETERMINATE", "HIGH", "LOW"),
    reasons=["查询原 job 台账不是重发原测试，即使 operation_ref 相同。", "请求动作与执行状态查询的逻辑目标不同。", "独立他身份响应不证明原操作是否执行。", "原 job 查询直接针对未解决执行状态。", "只读状态查询避免重复目标请求。"])

blocked = cond(prerequisite="queue-offline")
add(15, "阻塞前提已解除但效果尚未验", goal="progress_blocked", question="queue 服务已恢复，验证原状态请求现在能否成功。", focus=C,
    candidate=cand("15", conditions=C, op="op:15-after"), history=[hist("op:15-before", blocked, status="blocked", artifacts=["artifact:15-before"])],
    evidence=[body("artifact:15-before", "op:15-before", "Request blocked before reaching status endpoint: queue offline.", conditions=blocked), body("receipt:15-after", None, "Verified service health receipt: queue is ready. No status endpoint effect test was run after this receipt.", conditions={"prerequisite": "ready"}, kind="execution_state")],
    labels=("CONDITION_CHANGED", "CONDITION_CHANGED", "PARTIAL", "HIGH", "LOW"),
    reasons=["可信前提状态由 offline 变为 ready。", "阻塞条件发生实质变化。", "恢复收据证明前提，尚未证明目标请求效果。", "一次效果测试补上恢复后结果。", "前提变化使这次测试有价值，不能高风险拦成重复。"])

add(16, "解除阻塞仅有未核验摘要", goal="progress_blocked", question="据摘要 queue 可能恢复，判断是否值得重试。", focus=C,
    candidate=cand("16", conditions=C, op="op:16-after"), history=[hist("op:16-before", blocked, status="blocked", artifacts=["artifact:16-before"])],
    evidence=[body("artifact:16-before", "op:16-before", "Verified original failure: queue offline, request blocked.", conditions=blocked), body("claim:16", None, "An operator summary claims queue is now ready; no health receipt is supplied.", verified=False, conditions={"claimed_prerequisite": "ready"}, kind="summary_claim")],
    labels=("INSUFFICIENT_INFORMATION", "CONDITION_CHANGED", "PARTIAL", "POTENTIAL", "UNCERTAIN"),
    reasons=["实际前提变化只有未核验摘要，不能确认执行等价关系已改变。", "候选明确计划在 ready 前提下执行，逻辑条件相对旧 offline 改变。", "旧阻塞正文可见，恢复和恢复后效果都未验证。", "若确实恢复可带来结果，故潜在而非确定高价值。", "无法确定是真新条件还是仍在原阻塞下重复。"])

add(17, "Artifact 归属互相冲突时原样重读", goal="interpret_history", question="解释 op:17-a 的响应；必须先解决 artifact:17 的归属冲突。", focus=C,
    candidate=cand("17", kind="read", name="read_artifact", conditions={"artifact_id": "artifact:17", "read_scope": "full"}, artifact="artifact:17", key="body:artifact:17", arguments={"artifact_id": "artifact:17", "read_scope": "full"}), history=[hist("op:17-a", C, artifacts=["artifact:17"]), hist("op:17-read", {"artifact_id": "artifact:17", "read_scope": "full"}, key="body:artifact:17", name="read_artifact")],
    evidence=[body("artifact:17", "op:17-a", "Artifact internal header: owner operation op:17-b; reader-b HTTP 403. The external registry calls this op:17-a.", conditions={"registry_operation_ref": "op:17-a", "body_operation_ref": "op:17-b", "ownership_conflict": True, "artifact_read_scope": "full"})],
    labels=("SEMANTIC_REPEAT", "SEMANTIC_REPEAT", "INDETERMINATE", "LOW", "HIGH"),
    reasons=["同一正文同一范围已经完整读取，只有调用身份变化。", "没有新来源或读范围。", "正文完整不解决归属矛盾，无法归因给任务操作。", "原样重读不会裁决外部登记与内部头部冲突。", "明确已读且未变化的同范围正文重复风险高。"])

add(18, "只有执行成功元数据且工件正文缺失", goal="interpret_history", question="解释 op:18 成功时实际返回了什么内容。", focus=C,
    candidate=cand("18", kind="read", name="read_artifact", artifact="artifact:18-missing", key="body:artifact:18-missing", arguments={"artifact_id": "artifact:18-missing"}), history=[hist("op:18", C, artifacts=["artifact:18-missing"])],
    evidence=[body("receipt:18", "op:18", "Job succeeded. Response body should be artifact:18-missing, but artifact-store lookup returned permanent missing; no mirror is registered.", conditions=C, kind="execution_metadata"), {"record_ref": "artifact:18-missing", "kind": "unavailable", "operation_ref": "op:18", "source_refs": ["artifact-registry:18"], "available": False, "visible": False, "complete": False, "provenance_verified": True, "content": None, "sha256": None, "conditions": {"relevance_refs": ["task:holdout-18", "op:18"], "unavailability": "permanent-missing-no-mirror"}, "visibility_refs": ["tool-message:missing-lookup-18"]}],
    labels=("UNRELATED_ACTION", "UNRELATED_ACTION", "PARTIAL", "LOW", "LOW"),
    reasons=["读取正文与原执行动作不同。", "没有相同正文读取操作可比较。", "成功元数据不能替代实际响应正文。", "可见台账已说明永久缺失且无镜像，原样读取无新增可得内容。", "没有已经成功读过同正文的记录；低可得性不等于高重复。"])

add(19, "正文可见但必要检查条件缺失", goal="interpret_history", question="确认 op:19 是否在 reader-a、cfg-7、TLS 校验开启时返回 200；三个条件都须有证据。", focus=cond(tls_verify=True),
    candidate=cand("19", kind="read", name="read_artifact", artifact="manifest:19", key="conditions:op:19", arguments={"artifact_id": "manifest:19"}), history=[hist("op:19", {"target": C["target"], "method": "GET"}, artifacts=["artifact:19", "manifest:19"])],
    evidence=[body("artifact:19", "op:19", "HTTP 200; status=ready. The response body does not record request identity, config revision, or TLS policy.", conditions={"target": C["target"], "method": "GET"}), body("manifest:19", "op:19", "Operation op:19 request manifest: reader-a, cfg-7, tls_verify=true.", visible=False, conditions={"relevance_refs": ["task:holdout-19", "op:19"], "artifact_read_scope": "request-conditions"})],
    labels=("UNRELATED_ACTION", "UNRELATED_ACTION", "PARTIAL", "HIGH", "LOW"),
    reasons=["读取请求清单与原请求不同。", "候选信息目标是缺失检查条件。", "响应已见，但归属条件不全；不能因隐藏清单存在就判充分。", "可见索引把清单关联到原操作，读取能补上明确缺口。", "不是重复测试也没有已读相同清单。"])

add(20, "模型声称已测试不等于真实既往执行", goal="verify_current", question="验证 reader-a 当前状态；此前只有模型声称测试过。", focus=C,
    candidate=cand("20", conditions=C, op="op:20-observe"), history=[hist("claim:20", C, status="succeeded", origin="model_claim")],
    evidence=[body("claim-body:20", "claim:20", "The model says it tested cfg-7 reader-a and got HTTP 200, but supplies no job, tool response, or artifact.", verified=False, kind="model_claim", conditions=C)],
    labels=("INSUFFICIENT_INFORMATION", "SEMANTIC_REPEAT", "INSUFFICIENT", "HIGH", "LOW"),
    reasons=["不能从模型声称确认此前真实执行，因此实际执行等价关系不足。", "声称的请求与候选在逻辑条件上相同。", "没有执行观察或原始响应。", "首次真实观察直接建立任务所需证据。", "只有声称不能据此认定存在高重复执行风险。"])

add(21, "身份改变的真实有价值验证", goal="verify_current", question="现在需要验证 reader-b，reader-a 的成功不能代替。", focus=cond(identity="reader-b"),
    candidate=cand("21", conditions=cond(identity="reader-b"), op="op:21-b"), history=[hist("op:21-a", C, artifacts=["artifact:21-a"])],
    evidence=[body("artifact:21-a", "op:21-a", "reader-a cfg-7 HTTP 200 status=ready. No reader-b request is represented.", conditions=C)],
    labels=("CONDITION_CHANGED", "CONDITION_CHANGED", "PARTIAL", "HIGH", "LOW"),
    reasons=["真实请求身份发生变化。", "身份是任务明确要求的新条件。", "只有 reader-a 观察，reader-b 仍未验证。", "新身份测试直接回答任务。", "不同身份成功不可互相替代，不应判高重复。"])

add(22, "工具别名变化但同条件已知响应", goal="interpret_history", question="解释 cfg-7 reader-a 的已有状态响应，无新条件。", focus=C,
    candidate=cand("22", name="http_status_check", conditions=C, op="op:22-new", arguments={"target": C["target"], "method": "GET", "profile": "minimal-status"}), history=[hist("op:22-old", C, name="probe_status", artifacts=["artifact:22"])],
    evidence=[body("artifact:22", "op:22-old", "probe_status is the recorded wrapper for minimal-status GET. It returned cfg-7 reader-a HTTP 200 status=ready.", conditions=C), body("wrapper-map:22", None, "http_status_check and probe_status use the same minimal-status GET request schema for this fixture; neither adds a header, retry, or identity change.", conditions={"tool_aliases": ["probe_status", "http_status_check"], "request_profile": "minimal-status"}, kind="execution_metadata")],
    labels=("SEMANTIC_REPEAT", "SEMANTIC_REPEAT", "SUFFICIENT", "LOW", "HIGH"),
    reasons=["调用名不同但可见映射证明请求相同。", "不存在可见实质条件变化。", "所问历史响应已经完整可见。", "改包装名称不新增解释证据。", "已有同条件成功正文，换名字再测仍是高重复。"])

partial_text = "Archive header: operation op:23; outcome failed. Body range 80:240 has not been exposed."
add(23, "同 Artifact 的必要未读范围", goal="interpret_history", question="解释 op:23 的失败原因，已读 header 不含原因，需要 response-body 范围。", focus=C,
    candidate=cand("23", kind="read", name="read_artifact", conditions={"artifact_id": "archive:23", "read_scope": "bytes:80:240"}, artifact="archive:23", key="body:archive:23", arguments={"artifact_id": "archive:23", "byte_start": 80, "byte_end": 240}), history=[hist("op:23", C, status="failed", artifacts=["archive:23"]), hist("op:23-header-read", {"artifact_id": "archive:23", "read_scope": "bytes:0:80"}, key="body:archive:23", name="read_artifact")],
    evidence=[body("archive:23-visible-header", "op:23", partial_text, complete=False, conditions={"artifact_id": "archive:23", "artifact_read_scope": "bytes:0:80", "relevance_refs": ["task:holdout-23", "op:23"]}), body("archive:23", "op:23", "Response body for op:23: dependency=queue-unavailable; HTTP 503.", visible=False, conditions={"artifact_read_scope": "bytes:80:240", "relevance_refs": ["task:holdout-23", "op:23"]})],
    labels=("CONDITION_CHANGED", "CONDITION_CHANGED", "PARTIAL", "HIGH", "LOW"),
    reasons=["工件相同但读取范围由 header 改为未读 response-body。", "信息范围是实质读取条件。", "header 证明失败但不含失败原因，隐藏 body 不计充分。", "范围索引明确指向必要正文，因此新范围具有高价值。", "不能只按 artifact_id 相同就判高重复。"])

add(24, "GraphReview 写确认声明不会补出效果证据", goal="verify_current", question="审核当前图中 queue 恢复后的状态请求是否真已成功，不能以写入声明替代验证。", focus=C,
    candidate=cand("24", kind="other", name="write_graph", conditions={"graph_revision": 31, "claim_status": "confirmed"}, key="graph:queue-effect", arguments={"revision": 31, "statement": "status request succeeded after recovery", "claim_status": "confirmed"}), history=[hist("op:24-before", blocked, status="blocked")],
    evidence=[body("receipt:24-ready", None, "Verified queue health is ready; no status endpoint request was executed after recovery.", conditions={"prerequisite": "ready"}, kind="execution_state"), body("graph-body:31", None, "Graph revision 31 currently holds candidate queue_ready linked to receipt:24-ready; no effect-success evidence is linked.", conditions={"graph_revision": 31, "artifact_read_scope": "queue-effect"})], boundary={"phase": "graph_review", "graph_context": {"revision": 31, "visible_revision": 31, "provenance_verified": True, "source_refs": ["checkpoint:g31"]}},
    labels=("UNRELATED_ACTION", "UNRELATED_ACTION", "PARTIAL", "LOW", "LOW"),
    reasons=["写图声明与旧阻塞执行并非相同动作。", "候选是写入声明而不是读取或验证。", "已证实前提恢复，尚未观察恢复后的目标效果。", "写 confirmed 不会产生所缺的执行证据。", "没有重复相同图写的历史，低价值不等同高重复；确认权限风险不在五维标签内。"], limitations=["The observation DTO has no separate authorization or confirmation-validity label; the oracle does not overload repeat_risk to express that gap."])


def main() -> None:
    destinations = [OUT / name for name in ("inputs.json", "oracle.json", "source-manifest.json", "seal.json")]
    if any(path.exists() for path in destinations):
        raise FileExistsError("Sealed outputs already exist; this builder is exclusive-create only.")
    if len(INPUTS) != 24 or len({item["snapshot_id"] for item in INPUTS}) != 24:
        raise ValueError("Expected 24 uniquely identified ActionSnapshot inputs.")
    enum_sets = {
        "equivalence": {"EXACT_REPEAT", "SEMANTIC_REPEAT", "CONDITION_CHANGED", "INSUFFICIENT_INFORMATION", "UNRELATED_ACTION"},
        "logical_classification": {"EXACT_REPEAT", "SEMANTIC_REPEAT", "CONDITION_CHANGED", "INSUFFICIENT_INFORMATION", "UNRELATED_ACTION"},
        "evidence_sufficiency": {"SUFFICIENT", "PARTIAL", "INSUFFICIENT", "INDETERMINATE"},
        "estimated_action_value": {"HIGH", "POTENTIAL", "LOW", "UNCERTAIN"},
        "repeat_risk": {"HIGH", "POSSIBLE", "LOW", "UNCERTAIN"},
    }
    for snapshot, oracle in zip(INPUTS, ORACLE, strict=True):
        if snapshot["snapshot_id"] != oracle["sample_id"]:
            raise ValueError("Input-oracle identity mismatch.")
        for ev in snapshot["evidence"]:
            if ev["content"] is not None:
                if ev["sha256"] != sha(ev["content"].encode("utf-8")):
                    raise ValueError("Artifact body SHA256 mismatch.")
                if not ev["source_refs"] or not ev["visibility_refs"]:
                    raise ValueError("Artifact body lacks source or visibility references.")
        for dimension, allowed in oracle["expected"].items():
            if not allowed or not set(allowed) <= enum_sets[dimension]:
                raise ValueError("Invalid or empty oracle label set.")
            if len(allowed) > 2:
                raise ValueError("Oracle label tolerance must be narrowly justified.")
    input_bytes = enc(INPUTS)
    oracle_bytes = enc({"schema_version": "author_isolated_holdout_oracle/1", "samples": ORACLE})
    builder_hash = sha(Path(__file__).read_bytes())
    dto_hash = sha(DTO.read_bytes())
    manifest = {
        "schema_version": "author_isolated_holdout_manifest/1", "authored_date": "2026-10-25",
        "author": "OpenAI Codex sample-author subagent /root/heldout_author",
        "sample_count": len(INPUTS), "builder_path": "experiments/evidence_action_control_reliability_v1/heldout/builder.py",
        "read_allowlist": ["AGENTS.md", "experiments/evidence_action_control_v1/contracts.py"],
        "import_policy": "Only this builder, standard-library modules, the original ActionSnapshot DTO and its transitive DTO dependencies were imported. No evaluator, replay, previous synthetic corpus, previous oracle, detector output or model/executor/network client was read or imported.",
        "dto_sha256": dto_hash, "builder_sha256": builder_hash,
        "inputs_sha256": sha(input_bytes), "oracle_sha256": sha(oracle_bytes),
        "validation": {"action_snapshot_original_dto": True, "artifact_body_sha256": True, "body_source_and_visibility_refs": True, "label_domain_and_identity_checks": True, "predictions_computed": False, "scores_computed": False},
        "independence": {
            "author_isolated_from_round_2_rule_implementation": True,
            "human_blind_assessment": False,
            "blind_to_shared_task_priors": False,
            "limitations": ["The same task specification and named challenge categories were supplied to the author and implementer.", "The sample author is an AI subagent, not an independent human reviewer.", "The parent implementer receives only paths, count, freeze hashes and these limitations before rule completion.", "These are deterministic hand-authored fixtures, not sampled production runs or measured real-model/target efficacy.", "The labels and samples were frozen together before any detector prediction; they are not post-hoc labels selected to accommodate outputs."],
        },
        "label_protocol": {
            "equivalence": "Assess actual action identity and materially matching requests using available trustworthy facts. A different tool_call_id alone does not alter an explicit operation replay. A query of a job is distinct from executing its operation even if it uses the operation_ref.",
            "logical_classification": "Assess requested operation semantics and material conditions independently of whether a claimed prior execution or a proposed current condition is verified. With no related historical operation, use UNRELATED_ACTION.",
            "evidence_sufficiency": "Assess the exact task focus from before-candidate visible evidence only. Success metadata proves execution state, not an unseen response. Historical explanation retains explicitly historical focus conditions.",
            "estimated_action_value": "Assess information added toward the stated unresolved task, considering visible availability and relevance. New valid tests can be HIGH with LOW repeat risk; unrelated novel actions can be LOW with LOW repeat risk.",
            "repeat_risk": "Assess repetition of actual observed operations or known read scopes and exposure to unknown dispatch. Do not treat mere claim similarity, different verified conditions, new graph revisions, new artifact ranges or low task value as automatic HIGH repeat risk.",
            "admissible_sets": "One predeclared two-label logical-classification set acknowledges unverified graph-version semantics absent from the DTO; all other dimensions use a single label. No tolerance was selected from outputs.",
            "graph_convention": "boundary.graph_context contains revision, visible_revision, provenance_verified, source_refs. Read graph uses candidate.kind=query, write graph uses other. Only boundary.phase=graph_review denotes GraphReview.",
            "archive_convention": "Before-candidate evidence.conditions.relevance_refs, ownership and read scopes establish archive relevance; hidden content is never an input to the oracle's sufficiency or relevance inference.",
        },
        "contract_gaps": ["DTO dictionaries do not impose a globally authoritative graph revision resolver.", "DTO has no separate confirmation-write authorization dimension.", "Availability and byte/read-scope semantics are explicitly stated in fixture conditions rather than a new schema."],
    }
    manifest_bytes = enc(manifest)
    seal = {
        "schema_version": "author_isolated_holdout_seal/1", "sample_count": len(INPUTS),
        "frozen_before_detector_predictions": True, "exclusive_create": True,
        "files": {"builder.py": builder_hash, "inputs.json": sha(input_bytes), "oracle.json": sha(oracle_bytes), "source-manifest.json": sha(manifest_bytes)},
        "source_dto_sha256": dto_hash,
        "immutability_policy": "Do not change sealed inputs or labels. Corrections require a new separately named dataset; retain this dataset and disclose the issue.",
    }
    for path, data in zip(destinations, [input_bytes, oracle_bytes, manifest_bytes, enc(seal)], strict=True):
        with path.open("xb") as stream:
            stream.write(data)
    print(json.dumps({"sample_count": len(INPUTS), "files": seal["files"], "seal_sha256": sha(destinations[-1].read_bytes())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
