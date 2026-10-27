"""仅实验使用的被动反馈合同与投递；生产装配不导入此模块。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_workbench.contracts.history_action_reflection import Observation
from agent_workbench.domain.history_action_reflection import REQUIRED, digest

NEUTRAL = (
    "此前存在执行条件高度重合的历史操作及证据。这只说明已记录的历史关联，不能确定后续行动价值。"
    "请结合当前任务判断后续是否需要相同测试；如有新的目标、重要条件变化或合理复测需求，"
    "可以继续原计划。无需输出长篇反思。当前这次调用已经执行，不能计为被提示节省。"
)


def notice(observation: dict[str, Any]) -> dict[str, Any] | None:
    item = Observation.model_validate(
        {k: v for k, v in observation.items() if k in Observation.model_fields}
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
    # 只投影结构化比较与来源；不复制正文、模型声明或任意工具文本。
    value = {
        "kind": "passive_history_association/1",
        "instruction": NEUTRAL,
        "current_tool_call": item.candidate_tool_call_id,
        "current_test": item.candidate_tool_name,
        "historical_operations": [m.operation_ref for m in eligible[:2]],
        "common_execution_conditions": {
            c.field: c.new_value
            for c in eligible[0].compared_conditions
            if c.status == "comparable_same" and c.field in REQUIRED
        },
        "known_differences": sorted({d for m in eligible for d in m.differences}),
        "historical_status": "completed_with_verified_raw_visible_evidence",
        "verified_sources": item.original_evidence_refs[:4],
        "limitations": [
            "不是漏洞结论或行动价值判断",
            "目标当前状态与未记录变化无法由历史证明",
            "来源引用不替代原文；提示不包含 Artifact 正文或指令",
        ],
        "material_key": item.dedup_key,
    }
    if len(json.dumps(value, ensure_ascii=False).encode()) > 8192:
        return None  # 不截断可信条件后继续提示。
    return value


def all_tools_paired(messages: list) -> bool:
    pending: set[str] = set()
    seen: set[str] = set()
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if not call["id"] or call["id"] in seen:
                    return False
                seen.add(call["id"])
                pending.add(call["id"])
        elif isinstance(message, ToolMessage):
            if message.tool_call_id not in pending:
                return False
            pending.remove(message.tool_call_id)
    return not pending


class FeedbackLedger:
    """仅投递收据，不是事实库。单研究 Run 的控制器，独占 claim 支持重建去重。"""

    def __init__(self, root: Path, run: str, thread: str) -> None:
        self.root = root / digest({"run": run, "thread": thread})
        self.root.mkdir(parents=True, exist_ok=True)
        self.scope = {"run": run, "thread": thread}

    def enqueue(self, value: dict) -> bool:
        if len(json.dumps(value, ensure_ascii=False).encode()) > 8192:
            return False
        key = digest({**self.scope, "material": value["material_key"]})
        path = self.root / f"pending-{key}.json"
        if len(list(self.root.glob("pending-*.json"))) >= 64:
            return False
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump({"scope": self.scope, "notice": value}, handle, ensure_ascii=False)
        except FileExistsError:
            return False
        return True

    def claim_next(self) -> HumanMessage | None:
        for path in sorted(self.root.glob("pending-*.json")):
            key = path.stem.removeprefix("pending-")
            if path.stat().st_size > 16384:
                continue
            row = json.loads(path.read_text(encoding="utf-8"))
            if row["scope"] != self.scope:
                continue
            try:
                with (self.root / f"claimed-{key}.json").open("x", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "status": "claimed_before_model_call_may_be_lost_on_failure",
                            "scope": self.scope,
                        },
                        handle,
                    )
            except FileExistsError:
                continue
            return HumanMessage(
                content=json.dumps(row["notice"], ensure_ascii=False),
                id="passive-reflection:" + key,
            )
        return None
