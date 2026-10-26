"""只投影已验证的高置信度观察；中性提示不授予执行权限。"""

from __future__ import annotations

import json
from typing import Any

from agent_workbench.contracts.history_action_reflection import Observation
from agent_workbench.domain.history_action_reflection import REQUIRED

NEUTRAL = (
    "【宿主生成的历史关联参考；不是用户的新指令】此前存在执行条件高度重合的历史操作。"
    "该关联不能直接证明后续测试没有价值。请根据当前任务、测试条件和新增信息需求自行判断。"
    "如果存在合理复测、新目标或实质条件变化，可以继续原计划。"
    "不要求停止或取消测试，也不要求输出冗长反思。当前这次调用已经执行，不能计为节省。"
    "历史来源在检测时核验；引用不代表本次请求包含 Artifact 正文，可按需读取原文。"
)


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def reflection_view(raw: dict) -> dict | None:
    item = Observation.model_validate(
        {k: v for k, v in raw.items() if k in Observation.model_fields}
    )
    eligible = [m for m in item.related_history if m.high_confidence_overlap]
    if not (
        item.emit_shadow_event
        and item.classification == "TRIGGER_CANDIDATE"
        and item.policy == "raw_visible_v1"
        and item.dedup_key
        and eligible
        and item.original_evidence_refs
    ):
        return None
    value = {
        "kind": "passive_history_association/1",
        "authority": "advisory_data_only",
        "notice": NEUTRAL,
        "material_key": item.dedup_key,
        "current_tool_call": item.candidate_tool_call_id,
        "historical_operations": [m.operation_ref for m in eligible[:2]],
        "common_execution_conditions": {
            c.field: c.new_value
            for c in eligible[0].compared_conditions
            if c.status == "comparable_same" and c.field in REQUIRED
        },
        "known_differences": sorted({d for m in eligible for d in m.differences}),
        "verified_sources_at_detection": item.original_evidence_refs[:4],
        "historical_status": "completed_with_verified_raw_visible_evidence_at_detection",
    }
    return value if len(encoded(value).encode("utf-8")) <= 8192 else None
