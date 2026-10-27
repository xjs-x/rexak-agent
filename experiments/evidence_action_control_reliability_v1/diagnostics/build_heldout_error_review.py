"""Describe the one stored holdout evaluation without importing or running rules."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


BASE = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


inputs = read(BASE / "heldout/inputs.json")
oracle = read(BASE / "heldout/oracle.json")["samples"]
observations = read(BASE / "results/heldout-observations.json")
metrics = read(BASE / "results/heldout-metrics.json")
seal = read(BASE / "heldout/seal.json")
before = {name: digest(BASE / "heldout" / name) for name in seal["files"]}
if before != seal["files"]:
    raise ValueError("Original heldout seal verification failed.")
original_files = [BASE / "heldout/seal.json", BASE / "results/heldout-observations.json", BASE / "results/heldout-metrics.json"]
original_hashes = {str(path.relative_to(BASE)).replace("\\", "/"): digest(path) for path in original_files}
by_input = {item["snapshot_id"]: item for item in inputs}
by_oracle = {item["sample_id"]: item for item in oracle}
by_actual = {item["snapshot_id"]: item for item in observations}

FIELDS = ["equivalence", "logical_classification", "evidence_sufficiency", "estimated_action_value", "repeat_risk"]
FIELD_NAMES = {"equivalence": "执行等价", "logical_classification": "逻辑关系", "evidence_sufficiency": "证据充分性", "estimated_action_value": "动作价值", "repeat_risk": "重复风险"}
CATEGORIES = {
    "R": {"id": "general_rule_defect", "name": "通用规则缺陷", "definition": "在原 DTO 允许、样本已显式提供的可见事实或约定下，固定字段词汇、固定正文格式、历史选择或任务轴处理没有表达所需判断。结论来自持久化输出理由，不声称已定位源码行。"},
    "V": {"id": "visible_evidence_insufficient", "name": "可见证据不足", "definition": "动作前确实缺少可靠正文、执行状态、版本来源、条件成立证明，或存在归属冲突；保守输出可能合理，即使没有落在冻结 Oracle 的精确标签。"},
    "O": {"id": "oracle_dispute", "name": "标签争议", "definition": "冻结标签的因果/时效假设，或未被 DTO 正式定义的标签边界存在争议。只披露争议，不改 Oracle、不把未命中重算通过。"},
    "T": {"id": "unsupported_tool_semantics", "name": "工具语义未支持", "definition": "输出明确不支持图查询、图写入或其可见范围、版本语义；这说明实现适用范围有限，不应归结为任务事实本身未知。"},
}

REVIEW: dict[int, dict[str, Any]] = {}


def case(number: int, note: str) -> None:
    REVIEW[number] = {"note": note, "fields": {}}


def field(number: int, dimension: str, category: str, note: str, *contributing: str) -> None:
    REVIEW[number]["fields"][dimension] = {"primary_category": CATEGORIES[category]["id"], "contributing_categories": [CATEGORIES[key]["id"] for key in contributing], "review": note}


def pair(number: int, category: str, note: str, *contributing: str) -> None:
    for dimension in ("equivalence", "logical_classification"):
        field(number, dimension, category, note, *contributing)


case(1, "相同 operation_ref、新 tool_call_id；源正文已经可见。Oracle 的问题措辞含‘为何’，其因果充分性应单独质疑。")
pair(1, "R", "同一明确逻辑操作身份不应因 configuration/identity_id 等未声明必填词缺失而丢失；DTO 提供 config_revision/identity，输出没有比较这些字段。")
field(1, "evidence_sufficiency", "O", "冻结任务写‘为何返回正常’，正文只证明 HTTP 200/status=ready，未证明因果机制；SUFFICIENT 可能过强。输出以非 JSON 为由拒读是另一个格式支持缺陷，不能用该争议替它证明正确。", "R", "V")
field(1, "estimated_action_value", "R", "再发同操作不能解释既往因果；即使充分性标签有争议，本任务下原样复测的低信息价值仍可确定。")
field(1, "repeat_risk", "R", "同 operation_ref 和已完成台账已给出强重复依据，附加未知环境词不应降为 POSSIBLE。")

case(2, "新操作身份、同请求条件；当前性只在任务和正文文字中声明，没有规范化 observation_time。")
pair(2, "R", "相同 target/method/identity/config_revision/request_profile/prerequisite 均显式给出；规则因另一套字段名全体缺失而无法表达语义重复。当前性争议应独立保留。", "O")
field(2, "evidence_sufficiency", "O", "任务和可见正文声称快照间无变化、属于当前边界，但没有结构化时点收据。按该声明可判充分，若要求独立时间证明则 PARTIAL 合理；原 DTO 没有规定此证明合同。非 JSON 拒读仍属实现限制。", "V", "R")
field(2, "estimated_action_value", "O", "LOW 依赖未变化边界声明，POTENTIAL 依赖不能接受该声明；不能仅由 verify_current 自动制造时间差，也不能把文字承诺当作独立部署验收。", "R", "V")
field(2, "repeat_risk", "R", "封存语境明确无条件变化，POSSIBLE 主要源于私有词汇缺失；现实部署的时间证据仍有单独争议。", "O")

case(3, "候选 cfg-8 与历史 cfg-7 不同，但任务仍明确解释 cfg-7。")
pair(3, "R", "config_revision 的明示变化未进入 material_differences，输出仅列出 configuration 缺失；未知其他维度不能抹掉已知变化。")
field(3, "evidence_sufficiency", "R", "任务 focus 仍是历史 cfg-7，完整可见 cfg-7 正文不能仅因不是私有 JSON 格式而判缺。")
field(3, "estimated_action_value", "R", "输出理由已承认复测不能补历史来源，却仍回退 UNCERTAIN；cfg-8 测试无助该明确历史问题。")
field(3, "repeat_risk", "R", "不同配置不是同条件重复；低任务价值和低重复风险是两条轴。")

case(4, "cfg-8 生效收据已见，旧 cfg-7 效果不能证明新效果。")
pair(4, "R", "部署收据和候选都显式写 cfg-8，历史为 cfg-7；缺失私有配置词汇遮蔽了确定的差异。")
field(4, "estimated_action_value", "R", "新配置效果是明确缺口，不只是泛泛当前时间差；POTENTIAL 低估了直接补缺测试。")
field(4, "repeat_risk", "R", "旧配置已测不应使新配置效果测试升为 POSSIBLE 重复。")

case(5, "原任务正文已可见，候选归档可见索引属于另一个项目。")
field(5, "evidence_sufficiency", "R", "任务所需正文在 artifact:05，候选无关归档尚未读不应降低任务充分性；私有 JSON 要求又使原正文失效。")
field(5, "estimated_action_value", "R", "可见 relevance_refs 明确指向其他任务，且本任务已回答；无需读隐藏正文也可判低价值，不必笼统不确定。")

case(6, "五维全部命中：大归档有可见关联，必要正文未见，读取有价值；没有凭隐藏内容提高充分性。")
case(7, "候选无关归档的隐藏正文碰巧提及答案，不能用于推断动作前相关性。")
field(7, "estimated_action_value", "R", "已见索引明确属于别的任务；LOW 来自负相关证据，不是从隐藏答案猜测。UNCERTAIN 没有体现已知无关信息。")

case(8, "可信 boundary.graph_context 指明图由 11 修订到 12；问题问当前图记录，不问 queue 效果。")
pair(8, "T", "历史动作本身就是 query_graph，当前也是同主题图查询；输出却泛称‘查询与历史测试不同’，未利用版本 11→12 和同类历史。", "R")
field(8, "estimated_action_value", "T", "新修订尚未读且 task 问图记录，读图有直接价值；不能按 progress_blocked 一律要求验证外部效果。未实现图语义是范围缺口。", "R")

case(9, "同版本同范围图正文已见，任务只问图记录本身。")
pair(9, "T", "同 operation_ref、同图版本和主题已明确；查询动作被当成无法比较，而非比较历史图查询。")
field(9, "evidence_sufficiency", "T", "图内容足以回答‘图写了什么’，不需要它证明外部目标效果；要求 JSON 检查结果混淆了任务类型。", "R")
field(9, "estimated_action_value", "T", "已见同修订同范围，现有元数据足以判断重读无信息；未实现图视图比较导致不确定。")
field(9, "repeat_risk", "O", "Oracle 的重复风险覆盖重复读取，实现 LOW 的理由只覆盖不重执行外部测试。二者口径不同；这不是误放一次外部执行，但确实漏报无增益图读取。", "T")

case(10, "图新版本只是未核验摘要；冻结 Oracle 已给 logical_classification 两标签容许集。")
field(10, "evidence_sufficiency", "O", "INDETERMINATE 与 INSUFFICIENT 的边界未由 DTO 定义：只有未核验摘要可合理称证据不足；不应把标签差异说成已验证了新版本。输出还误加‘前提改变后效果’要求。", "V", "R")
field(10, "estimated_action_value", "V", "新版本存在性未核验，图 API 语义未支持；UNCERTAIN 是可辩护的保守表达，Oracle POTENTIAL 代表作者认为核对有潜在收益，两者不是充分性结论。", "O", "T")
field(10, "repeat_risk", "O", "Oracle 对是否重读未知图版本保留不确定，输出 LOW 只说明只读无外部重执行；需要明确重复读取与执行风险口径。", "V", "T")

case(11, "明确 graph_review 阶段首次读待审核图，历史为空；尚未见图正文。")
pair(11, "O", "没有比较对象时用 UNRELATED_ACTION 还是 INSUFFICIENT_INFORMATION 只有作者协议、没有 DTO 规范；不能把这个标签争议等同来源或效果判断失误。", "T")
field(11, "estimated_action_value", "T", "首次读取显式待审图是 GraphReview 的必要信息步骤；实现未支持该图语义，保守回退可解释但未完成任务价值识别。")

case(12, "原 job 执行未知，新同条件测试不能查清原执行；HIGH unknown 执行风险被正确保留。")
field(12, "equivalence", "O", "若 equivalence 表示实际发生的执行，原执行存在性未知时 INSUFFICIENT_INFORMATION 可辩护；若表示已派发请求语义则 SEMANTIC_REPEAT。逻辑关系应另行判断。", "V")
field(12, "logical_classification", "R", "原记录与候选请求条件明确相同，未知发生与否不应让请求语义本身也不足；私有条件词汇进一步遮蔽该关系。")
field(12, "evidence_sufficiency", "O", "原结果未知应被表达；PARTIAL 加明确 missing_evidence 也保留了未知，和 INDETERMINATE 是未规范的层级差异，不能断言模型把 unknown 当未执行。", "V")
field(12, "estimated_action_value", "R", "输出自己的理由已说新 Operation ID 不增信息；任务只查原 job，新测试应低价值，而非以执行风险替代价值判定。")

case(13, "原 job 未知继续保留，但独立同条件成功用于回答当前任务，两条结论不能互相覆盖。")
pair(13, "R", "实现选中了独立成功历史，却仍因私有条件名和自动时间要求丢失语义重复；未知旧 job 不是抹去独立观察的理由。", "O")
field(13, "evidence_sufficiency", "O", "独立正文文字声明当前边界但没有结构化 observation_time；接受该声明则应 SUFFICIENT，不接受则 PARTIAL 可辩护。不能把独立成功追认成旧 job 成功。", "R", "V")
field(13, "estimated_action_value", "R", "当前目标不要求查原未知 job；已选独立成功后仍用另一个未知 job 把动作价值降成 UNCERTAIN，发生任务与执行身份污染。当前性文字证据仍有争议。", "O")

case(14, "get_job 指向原未知执行，是状态核对；他身份成功不结清原 job。")
pair(14, "R", "get_job 与执行状态请求本来不同类，相同 operation_ref 只是关联，不是等价；输出理由承认不同，却没有落到明确不同动作的关系。", "O")
field(14, "evidence_sufficiency", "O", "PARTIAL 的理由明确保留原 job 未知，没有借 reader-b 成功追认。与 INDETERMINATE 的差异是证据状态标签口径，非失去未知保护。", "V")

case(15, "阻塞前提经可见健康收据解除；目标效果仍未验证。")
pair(15, "R", "prerequisite 的已知 offline→ready 被私有 prerequisites 字段缺失覆盖；已知变化与其他未知维度应分开。")
field(15, "estimated_action_value", "R", "前提恢复不等于效果成功，正因如此恢复后第一次效果测试具有明确高价值。")
field(15, "repeat_risk", "R", "经过验证的前提变化支持低重复；POSSIBLE 来自词汇适配缺口而非新的可见重复依据。")

case(16, "解除前提只有摘要，没有验证收据；实际条件不确定是正确保留。")
field(16, "logical_classification", "R", "拟执行 ready 与旧 offline 逻辑条件明确不同，实际 ready 是否成立应只影响执行等价和充分性，不应擦掉拟执行差异。")
field(16, "estimated_action_value", "V", "真实恢复未知，UNCERTAIN 比 POTENTIAL 更保守但可辩护；作者的潜在收益判断不是前提已成立证据。", "O")
field(16, "repeat_risk", "O", "POSSIBLE 已表达存在重复可能，UNCERTAIN 表达概率基础未知；DTO 没有规定二者边界，不能将这一未命中等同无风险放行。", "V")

case(17, "同正文已完整读过，且 registry 与正文 owner 明确冲突。")
field(17, "evidence_sufficiency", "V", "ownership_conflict=true 及两种 owner 均可见，不能确认任务归属。输出仅称非 JSON 而未识别冲突；PARTIAL 没有明确表达不可归因状态。", "R", "O")
field(17, "repeat_risk", "O", "实现已识别 SEMANTIC_REPEAT/LOW 价值，却因‘只是读取’给 LOW 风险；Oracle HIGH 衡量读循环。需承认风险定义不统一，而不是外部执行安全漏判。", "R")

case(18, "执行成功元数据存在，正文明确永久缺失且没有镜像。")
field(18, "estimated_action_value", "R", "已见 unavailable/permanent-missing-no-mirror 是明确可得性负证据；不是仅未知来源。原样读取低价值可判定。")
case(19, "五维全部命中：正文可见但必要身份/配置/TLS 条件未见；关联清单补读有价值。")

case(20, "只有模型声称测试过，不构成真实观察或未知派发台账。")
field(20, "logical_classification", "R", "可以在不相信模型执行主张的同时比较其声称的请求条件；逻辑相同不等于发生过。")
field(20, "estimated_action_value", "O", "首次真实观察直接填补缺口支持 HIGH；但 DTO 仅给 kind=test 和自由工具名，未规定能力/成功可得性时 POTENTIAL 也可辩护。这里不是已测过的证据。", "T")
field(20, "repeat_risk", "R", "仅模型主张且没有执行观察或未知派发记录，不应按真实重复执行给 POSSIBLE；至少没有触发 HIGH，关键边界仍保留。")

case(21, "任务身份是 reader-b，历史 reader-a 成功不满足该任务。")
pair(21, "R", "identity 的 reader-a→reader-b 变化明确，不能因 identity_id 私有键缺失忽略。")
field(21, "estimated_action_value", "R", "新身份是明确任务要求，不是仅 verify_current 带来的抽象时间差；测试有直接补缺价值。")
field(21, "repeat_risk", "R", "不同身份条件测试不能被相同路径的旧测试推高重复风险。")

case(22, "工具名字变了，可见映射说明请求语义不变。")
pair(22, "R", "source 中已有 wrapper-map 和相同请求条件；固定条件词汇及工具名字适配未将同义调用归一。", "T")
field(22, "evidence_sufficiency", "R", "完整可见历史正文已回答任务；没有 DTO 依据要求内容只能是某个 JSON 检查格式。")
field(22, "estimated_action_value", "R", "任务仍是历史解释，换工具别名不会补出新证据；私有正文格式失败不应自动把价值变未知。")
field(22, "repeat_risk", "R", "同义包装已有可见映射与同条件成功，POSSIBLE 漏掉明确重复。", "T")

case(23, "同工件新范围是必要未读内容；价值和 LOW 风险正确。")
pair(23, "R", "输出只比较了候选读取与原测试，忽略了历史 header-read 同类读取；应比较 bytes:0:80→80:240 的范围变化。", "T")

case(24, "候选 kind=other 写图确认声明，不产生所缺效果证据；GraphReview 不授权无证据 confirmed。")
pair(24, "T", "输出明确没有 other 工具语义规则；冻结约定已说明 write_graph 为写图，但 DTO 缺少专门类型，保守回退属于支持范围缺口。", "O")
field(24, "estimated_action_value", "T", "图写入声明不能生成目标效果观察；在已知 write_graph 语义下 LOW 清楚。若不接受自由工具名含义，UNCERTAIN 是合理的能力限制披露。", "O")
field(24, "repeat_risk", "T", "没有同类既往写入应低重复；实现不认识写图语义而回退不确定。不得拿重复风险标签承载确认权限风险，后者仍是 DTO 缺口。", "O")


def values(item: dict[str, Any]) -> dict[str, str]:
    return {
        "equivalence": item["equivalence"]["classification"],
        "logical_classification": item["equivalence"]["logical_classification"],
        "evidence_sufficiency": item["evidence_sufficiency"]["status"],
        "estimated_action_value": item["information_gain"]["estimated_action_value"],
        "repeat_risk": item["repeat_risk"]["status"],
    }


stored_mismatches = {(item["sample_id"], item["field"]) for item in metrics["mismatches"]}
reviewed_mismatches = {(f"holdout-{number:02d}", dimension) for number, details in REVIEW.items() for dimension in details["fields"]}
if stored_mismatches != reviewed_mismatches or len(stored_mismatches) != 75:
    raise ValueError("The review must cover exactly all 75 stored mismatch cells.")

samples = []
for number, details in sorted(REVIEW.items()):
    sid = f"holdout-{number:02d}"
    snapshot = by_input[sid]
    observed = by_actual[sid]
    expected = by_oracle[sid]["expected"]
    actual = values(observed)
    entries = []
    for dimension, detail in details["fields"].items():
        entries.append({"field": dimension, "expected_frozen": expected[dimension], "actual_stored": actual[dimension], **detail})
    samples.append({
        "sample_id": sid, "title": by_oracle[sid]["title"], "summary": details["note"],
        "task_goal": snapshot["task"]["goal"], "task_text": snapshot["task"]["text"],
        "candidate_kind": snapshot["candidate"]["kind"], "candidate_name": snapshot["candidate"]["name"],
        "all_fields_matched_in_stored_evaluation": not entries,
        "expected_frozen": expected, "actual_stored": actual,
        "mismatch_reviews": entries,
        "stored_reasons": {
            "equivalence": observed["equivalence"]["classification_reason"],
            "sufficiency": observed["evidence_sufficiency"]["classification_reason"],
            "missing_evidence": observed["evidence_sufficiency"]["missing_evidence"],
            "value": observed["information_gain"]["classification_reason"],
            "risk": observed["repeat_risk"]["classification_reason"],
            "unknown_execution_risk": observed["repeat_risk"]["unknown_execution_risk"],
        },
    })

uncertain_labels = {
    "equivalence": {"INSUFFICIENT_INFORMATION"}, "logical_classification": {"INSUFFICIENT_INFORMATION"},
    "evidence_sufficiency": {"INDETERMINATE"}, "estimated_action_value": {"UNCERTAIN"}, "repeat_risk": {"UNCERTAIN"},
}
reasonable_ids = {
    "equivalence": {10, 11, 12, 16, 20, 24},
    "logical_classification": {10, 11, 24},
    "evidence_sufficiency": set(),
    "estimated_action_value": {10, 11, 16, 24},
    "repeat_risk": {24},
}
uncertainty_cells = []
uncertainty_omissions = []
for sample in samples:
    number = int(sample["sample_id"].split("-")[1])
    for dimension in FIELDS:
        actual = sample["actual_stored"][dimension]
        expected = sample["expected_frozen"][dimension]
        if actual in uncertain_labels[dimension]:
            reasonable = number in reasonable_ids[dimension]
            uncertainty_cells.append({
                "sample_id": sample["sample_id"], "field": dimension, "actual": actual,
                "qualitative_assessment": "reasonable" if reasonable else "excessive",
                "strict_oracle_inclusion": actual in expected,
                "basis": "真实来源/执行状态缺口，或 DTO 未规范的比较对象/工具语义，使保守回退可辩护；不代表该字段重算通过。" if reasonable else "已知身份、条件差异、已见范围、明确任务无关性或直接补缺价值足以在当前约定下作出更确定判断。",
            })
        if any(label in uncertain_labels[dimension] for label in expected) and actual not in uncertain_labels[dimension]:
            clear = number == 17 and dimension == "evidence_sufficiency"
            uncertainty_omissions.append({
                "sample_id": sample["sample_id"], "field": dimension, "actual": actual, "expected": expected,
                "qualitative_assessment": "clear_underexpression" if clear else "disputed_boundary",
                "basis": "归属冲突已明确可见，但输出只有通用非 JSON/正文缺失理由，没有说明不可归因。" if clear else "PARTIAL/INSUFFICIENT/POSSIBLE 或仅外部执行风险 LOW 已可能表达保守性；DTO 未规范与 INDETERMINATE/UNCERTAIN 的边界。",
            })

uncertainty_counts = Counter(item["qualitative_assessment"] for item in uncertainty_cells)
omission_counts = Counter(item["qualitative_assessment"] for item in uncertainty_omissions)
if uncertainty_counts != {"reasonable": 14, "excessive": 36} or omission_counts != {"disputed_boundary": 5, "clear_underexpression": 1}:
    raise ValueError("Uncertainty accounting is inconsistent.")
category_counts = Counter(entry["primary_category"] for sample in samples for entry in sample["mismatch_reviews"])
field_summary = {dimension: {"matched": metrics["field_results"][dimension]["matched"], "mismatched": metrics["field_results"][dimension]["mismatched"]} for dimension in FIELDS}
high_value_cases = [item for item in samples if item["expected_frozen"]["estimated_action_value"] == ["HIGH"]]
high_value_risks = Counter(item["actual_stored"]["repeat_risk"] for item in high_value_cases)
high_value_values = Counter(item["actual_stored"]["estimated_action_value"] for item in high_value_cases)
useful_cases = [item for item in samples if item["expected_frozen"]["estimated_action_value"] in (["HIGH"], ["POTENTIAL"])]
useful_risks = Counter(item["actual_stored"]["repeat_risk"] for item in useful_cases)
useful_values = Counter(item["actual_stored"]["estimated_action_value"] for item in useful_cases)
if len(high_value_cases) != 10 or high_value_risks.get("HIGH", 0):
    raise ValueError("Stored high-value-action safety diagnostic is inconsistent.")

review = {
    "schema_version": "heldout_error_review/1", "date": "2026-10-25",
    "scope": "Post-evaluation read-only diagnosis of the one already persisted evaluation; no rule invocation, prediction, rescoring, relabeling or frozen-file mutation.",
    "reviewer": "Original AI sample-author subagent /root/heldout_author; not an independent human adjudicator.",
    "limitations": ["Author and implementer shared task priors; this was author-isolated, not human-blind evaluation.", "The original author reviews its own Oracle after observing outputs; interpret label disputes as disclosed author judgments, not independent adjudication.", "No evaluator or replay source was read for this review; root causes are inferred from stored classification reasons and the original DTO, not source-confirmed implementation findings.", "Only deterministic offline fixture behavior is measured; no real model, target, network, or Executor experiment was run.", "The current corpus is spent; these diagnostic findings must not be used to tune then claim a new heldout score on it."],
    "input_provenance": {**original_hashes, **{f"heldout/{name}": value for name, value in before.items()}},
    "stored_result_summary": {"sample_count": 24, "all_fields_matched": 2, "all_fields_matched_sample_ids": ["holdout-06", "holdout-19"], "matched_field_cells": 45, "mismatched_field_cells": 75, "field_summary": field_summary, "high_repeat_risk": metrics["high_repeat_risk"], "logical_repeat_detection": metrics["logical_repeat_detection"]},
    "category_definitions": list(CATEGORIES.values()), "primary_category_counts": dict(category_counts),
    "high_value_action_diagnostic": {"definition": "Oracle single-label estimated_action_value=HIGH, frozen before predictions.", "count": 10, "sample_ids": [item["sample_id"] for item in high_value_cases], "stored_repeat_risk_counts": dict(high_value_risks), "stored_action_value_counts": dict(high_value_values), "false_high_repeat_risk": 0, "qualified_conclusion": "No HIGH-risk false alarm in these ten fixtures. Four POSSIBLE-risk warnings and six value underestimates remain; absence of HIGH false positives here is not a generalization guarantee."},
    "useful_action_diagnostic": {"definition": "Oracle single-label estimated_action_value is HIGH or POTENTIAL.", "count": len(useful_cases), "sample_ids": [item["sample_id"] for item in useful_cases], "stored_repeat_risk_counts": dict(useful_risks), "stored_action_value_counts": dict(useful_values), "false_high_repeat_risk": 0},
    "repeat_risk_miss_partition": {"stored_high_false_negative_count": 5, "external_test_repeat_ids": ["holdout-01", "holdout-02", "holdout-22"], "readonly_repeat_definition_dispute_ids": ["holdout-09", "holdout-17"], "meaning": "Three external-test misses and two read-loop misses have different implications; do not report all five as duplicate external execution failures."},
    "uncertainty_accounting": {
        "unit": "one sample × one field; multiple fields from one sample count separately",
        "explicit_uncertainty_labels": {key: sorted(value) for key, value in uncertain_labels.items()},
        "excluded_from_automatic_uncertainty_count": ["PARTIAL is evidence coverage, POSSIBLE is a repeat-risk possibility; neither is automatically an epistemic abstention."],
        "emitted_explicit_uncertainty_cells": 50, "qualitatively_reasonable": 14, "qualitatively_excessive": 36,
        "strict_oracle_membership_of_emitted_uncertainty": {"inside_frozen_allowed_set": 4, "outside_frozen_allowed_set": 46},
        "explicit_uncertainty_expected_but_not_emitted": 6, "qualitatively_clear_underexpression": 1, "qualitatively_disputed_boundary": 5,
        "definition_reasonable": "Visible provenance/execution gaps or an unformalized comparator/tool-semantics boundary make conservative abstention defensible; this is not a retrospective score correction.",
        "definition_excessive": "The declared DTO conventions and visible facts are sufficient for the specific axis, but a private vocabulary/schema requirement or axis conflation produces abstention.",
        "definition_underexpression": "The frozen Oracle expects explicit epistemic uncertainty but the stored output uses another label. The qualitative audit distinguishes actual omission of known conflict from disputed PARTIAL/INSUFFICIENT/POSSIBLE/LOW boundaries.",
        "cells": uncertainty_cells, "omissions": uncertainty_omissions,
    },
    "matched_label_wrong_or_limited_reason_notes": [
        {"sample_id": "holdout-13", "field": "repeat_risk", "note": "HIGH matches, but the stored reason relies on old unknown dispatch, while the Oracle also has independent same-condition success. The independent current task must remain answerable without retroactively resolving the old operation."},
        {"sample_id": "holdout-15", "field": "evidence_sufficiency", "note": "PARTIAL and the missing-effect explanation correctly preserve prerequisite/effect separation even though the known prerequisite change is missed elsewhere."},
        {"sample_id": "holdout-19", "field": "evidence_sufficiency", "note": "The label is correct, but a generic non-JSON rejection is weaker than specifically recognizing the missing identity/config/TLS conditions."},
    ],
    "samples": samples,
}

lines = [
    "# 第二轮留出集单次评价误差审阅", "",
    "唯一一次已持久化评价为 **2/24 样本五维全部命中**；120 个字段中 45 个命中、75 个未命中。本文只分析该次结果，未调用检测器、未重评、未修改冻结输入、Oracle、规则或原结果。标签争议不会被追算为通过。", "",
    "作者与本轮规则实现隔离，但共享任务先验；样本作者是 AI 子代理，本审阅也由原作者完成，**不是独立人工盲评或独立仲裁**。未读取 evaluator/replay 源码，归因来自原 DTO 和已保存输出理由，属于可追溯诊断推断。", "",
    "## 量化结果与含义", "",
    "| 字段 | 命中 | 未命中 |", "|---|---:|---:|",
]
for dimension, counts in field_summary.items():
    lines.append(f"| {FIELD_NAMES[dimension]} | {counts['matched']} | {counts['mismatched']} |")
lines.extend([
    "", "全部命中的样本是 holdout-06 与 holdout-19。单标签 HIGH 重复风险精确率为 2/2，召回为 2/7；逻辑重复识别为 1/8。样本量小、题目相关，不能据此宣称泛化。", "",
    "Oracle 事前标 HIGH 价值的 10 个行动，没有一个被预测为 HIGH 重复风险；其中 6 个 LOW、4 个 POSSIBLE。其价值只识别出 4 个 HIGH，另 3 个 POTENTIAL、3 个 UNCERTAIN。因此本集未观察到最强误拦，但仍有价值低估与四次温和重复警告。", "",
    "若把事前 HIGH 或 POTENTIAL 都定义为有用行动，则共 12 个：风险 LOW 7、POSSIBLE 5、HIGH 0；价值 HIGH 4、POTENTIAL 3、UNCERTAIN 5。没有错误升级为 SUFFICIENT；7 个应充分被降为 PARTIAL，4 个预期 INDETERMINATE 被输出为 PARTIAL/INSUFFICIENT。前者体现保守漏用证据，后者仍要区分真正归属冲突与标签层级争议。", "",
    "5 个 HIGH 重复风险漏报必须拆开：holdout-01/02/22 是外部测试重复，holdout-09/17 是图或工件的无增益重读。后两项含风险口径争议，不能把五项全说成漏放外部重复执行。", "",
    "## 分类定义与主归因", "",
])
for category in CATEGORIES.values():
    lines.append(f"- **{category['name']}**：{category['definition']} 本次作为主归因共 {category_counts[category['id']]} 个字段。")
lines.extend([
    "", "每个未命中只记一个主归因，另列可并存因素；这些归因不互相证明，也不改变原成绩。", "",
    "## 关键边界", "",
    "**动作轴与任务相关轴。** 读取原响应与原测试是不同动作，但读取之间也需要比较：holdout-23 应比较未读新范围和旧 header 范围，holdout-09 应比较同修订图查询。UNRELATED_ACTION 描述动作关系不等于任务无关，LOW 任务价值也不等于 HIGH 重复风险。", "",
    "**条件变化与任务目标。** holdout-03 和 04 都把 cfg-7 改为 cfg-8；前者任务仍是解释 cfg-7，应低价值，后者明确验证 cfg-8，应高价值。输出因 configuration 等私有键缺失而没有比较 config_revision，并用 goal 泛化替代具体问题。holdout-08 的 progress_blocked 实际问图记录，不能机械要求外部效果测试。", "",
    "**正文格式与合同。** 原 DTO 的 content 是字符串、conditions/boundary 是自由字典，没有规定正文必须是某种 JSON 检查对象或必须提供 identity_id/configuration/prerequisites 等完整键。输出反复按这些隐含要求回退，说明格式/词汇支持范围不足。保守不把未知当相同是合理原则，但不能擦掉已见 operation_ref、identity、config_revision、prerequisite、图版本和读范围。另一方面，holdout-01 的‘为何’措辞可能要求因果证据，02/13 只有文字当前性承诺，不能将作者强标签视为无争议真理。", "",
    "**unknown 与独立结果。** holdout-12 正确保留原派发未知及 HIGH 重复执行风险，新测试无法追认原 job。holdout-14 正确给原 get_job 查询 HIGH 价值、LOW 风险。他身份成功不能结清原执行。holdout-13 必须同时保留‘旧 job 未知’与‘独立同条件当前响应已观察’；输出选到独立成功却仍用旧 unknown 压低当前任务价值，发生跨身份/任务污染，其 HIGH 风险虽命中但理由不完整。", "",
    "**图与工件语义。** 图修订、新范围、明确 graph_review 阶段可决定读取价值，不能只按 query/read 统称无法比较。写图 confirmed 不会生成效果证据；DTO 缺少单独确认权限标签，应披露缺口。holdout-17 归属冲突不能靠正文完整与哈希正确消除；18 永久缺失是低可得性负证据；19 必要条件未见仍应 PARTIAL。", "",
    "## 不确定输出的定义和数量", "",
    "计数单位为‘样本 × 字段’。只把 equivalence/logical_classification 的 INSUFFICIENT_INFORMATION、充分性的 INDETERMINATE、价值/风险的 UNCERTAIN 计为显式认识不确定输出。PARTIAL 是覆盖程度，POSSIBLE 是风险可能性，不自动计为弃判。", "",
    "实际共有 **50 个显式不确定字段**。按原 DTO 与输出理由进行质性审阅，**14 个可辩护、36 个过度回退**。可辩护指真实执行/来源缺口或工具/比较对象边界未规范，不表示其原成绩改为通过；过度指本字段已有确定身份、条件差异、任务无关性或读范围信息却被隐含格式要求遮蔽。逐项清单见 JSON。", "",
    "另有 **6 个冻结 Oracle 期望显式不确定而输出未采用该标签**：10/12/14/17 的充分性以及 10/16 的风险。质性审阅将 **1 个视为明确表达不足**（17：可见归属冲突未被说明），**5 个保留边界争议**，因为 PARTIAL/INSUFFICIENT/POSSIBLE 或外部执行风险 LOW 已有保守含义。按严格 Oracle 纳入计算，50 个实际不确定输出只有 4 个在容许集中、46 个不在；这是另一种统计口径，不要与 14/36 混用。", "",
    "## 逐样本逐字段审阅", "",
])
category_names = {item["id"]: item["name"] for item in CATEGORIES.values()}
for sample in samples:
    lines.extend([f"### {sample['sample_id']} — {sample['title']}", "", sample["summary"], ""])
    if not sample["mismatch_reviews"]:
        lines.extend(["五维全部命中；保留原结果，不新增通过证据。", ""])
        continue
    for entry in sample["mismatch_reviews"]:
        expected_text = "/".join(entry["expected_frozen"])
        contributing = "；并存：" + "、".join(category_names[item] for item in entry["contributing_categories"]) if entry["contributing_categories"] else ""
        lines.append(f"- **{FIELD_NAMES[entry['field']]}**：冻结 `{expected_text}` → 实际 `{entry['actual_stored']}`；主归因：{category_names[entry['primary_category']]}{contributing}。{entry['review']}")
    lines.append("")
lines.extend([
    "## 冻结与后续解释限制", "",
    "已核验 heldout/seal.json 中 builder、输入、Oracle、来源清单哈希；诊断前后再核验原 seal 和两份唯一评价输出未变化。JSON 保存这些哈希、所有 75 个字段的冻结值/实际值/理由、完整不确定性计数台账。", "",
    "本集已经被观察，后续改进若要评价泛化，需另建未见留出集并事前声明词汇/正文/工具语义适用合同。本文不建议把这些样本改写成当前实现可通过的输入后重算本轮成绩。", "",
])

destinations = [OUT / "heldout-error-review.json", OUT / "heldout-error-review.md"]
if any(path.exists() for path in destinations):
    raise FileExistsError("Review outputs already exist; preserve previous diagnostic artifacts.")
json_bytes = (json.dumps(review, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
markdown_bytes = ("\n".join(lines).rstrip() + "\n").encode("utf-8")
for path, data in zip(destinations, [json_bytes, markdown_bytes], strict=True):
    with path.open("xb") as stream:
        stream.write(data)
after = {name: digest(BASE / "heldout" / name) for name in seal["files"]}
if before != after or any(digest(BASE / key) != value for key, value in original_hashes.items()):
    raise ValueError("A frozen original file changed during review.")
print(json.dumps({"reviewed_mismatch_cells": len(stored_mismatches), "category_counts": dict(category_counts), "uncertainty_reasonable": 14, "uncertainty_excessive": 36, "uncertainty_missing_clear": 1, "uncertainty_missing_disputed": 5, "original_files_unchanged": True, "files": {path.name: digest(path) for path in destinations}}, ensure_ascii=False))
