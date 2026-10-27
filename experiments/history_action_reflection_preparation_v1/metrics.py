"""冻结的后续实验统计口径；不把脚本响应或工具减少当任务成功。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast


@dataclass
class RequestBudget:
    """出站前保留物理名额；失败/未知响应不退还。无真实 Provider 连接能力。"""

    ordinary_limit: int = 6
    maintenance_limit: int = 2
    token_limit: int = 262144
    input_envelope: int = 131072
    ordinary: int = 0
    maintenance: int = 0
    known_tokens: int = 0
    pending: bool = False
    stopped: bool = False
    ledger: list[dict] = field(default_factory=list)

    def reserve(self, phase: Literal["ordinary", "maintenance"], estimate: int, completion: int) -> int:
        if phase not in {"ordinary", "maintenance"} or min(estimate, completion) < 0:
            raise ValueError("invalid_request_envelope")
        if self.stopped or self.pending:
            raise RuntimeError("unknown_response_or_stopped_no_retry")
        if estimate > self.input_envelope or self.known_tokens + estimate + completion > self.token_limit:
            self.stopped = True
            raise RuntimeError("token_budget_preflight_stop")
        if (phase == "ordinary" and self.ordinary >= self.ordinary_limit) or (
            phase == "maintenance" and self.maintenance >= self.maintenance_limit
        ):
            self.stopped = True
            raise RuntimeError("physical_request_budget_stop")
        if phase == "ordinary":
            self.ordinary += 1
        else:
            self.maintenance += 1
        self.pending = True
        self.ledger.append({"sequence": len(self.ledger) + 1, "phase": phase,
                            "estimated_input_units": estimate, "status": "reserved_send_unknown"})
        return len(self.ledger)

    def complete(self, native_usage: dict | None, status: str) -> None:
        if not self.pending:
            raise RuntimeError("no_reserved_request")
        self.pending = False
        self.ledger[-1].update(status=status, native_usage=native_usage)
        if status != "completed" or not native_usage or type(native_usage.get("total_tokens")) is not int:
            self.stopped = True
            return
        self.known_tokens += native_usage["total_tokens"]
        if self.known_tokens >= self.token_limit:
            self.stopped = True


def usage_summary(ledger: list[dict]) -> dict:
    def group(rows):
        fields = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "cached_tokens", "total_tokens")
        totals = {}
        for key in fields:
            values = [(r.get("native_usage") or {}).get(key) for r in rows]
            totals[key] = sum(cast(list[int], values)) if values and all(type(v) is int for v in values) else "NOT_AVAILABLE"
        return {"physical_requests": len(rows), **totals}
    return {"ordinary": group([r for r in ledger if r["phase"] == "ordinary"]),
            "maintenance": group([r for r in ledger if r["phase"] == "maintenance"]),
            "all": group(ledger)}


def quality(judgment: dict) -> dict:
    """judgment 必须由单独审阅者/预声明来源提供；检测器输出不是任务 Oracle。"""
    required = set(judgment["necessary_action_ids"])
    performed = set(judgment["performed_action_ids"])
    missing = sorted(required - performed)
    return {"necessary_test_retention": len(required & performed) / len(required) if required else "NOT_APPLICABLE",
            "missing_necessary_actions": missing,
            "worthless_repeat_count": len(judgment["independently_labeled_worthless_repeats"]),
            "task_complete": judgment.get("task_complete", "NOT_EVALUABLE"),
            "evidence_correct": judgment.get("evidence_correct", "NOT_EVALUABLE"),
            "scope_interpretation_correct": judgment.get("scope_interpretation_correct", "NOT_EVALUABLE"),
            "final_report_quality": judgment.get("final_report_quality", "NOT_EVALUABLE"),
            "hint_caused_cancellation": "NOT_INFERRED_FROM_MISSING_ACTION_ALONE",
            "label_provenance": judgment.get("label_provenance", "UNREVIEWED")}


def net_cost(baseline: dict, passive: dict) -> dict:
    verified = all(row.get("currency_verified") is True for row in (baseline, passive))
    return {"elapsed_ms_saved": baseline["elapsed_ms"] - passive["elapsed_ms"],
            "provider_requests_saved": baseline["provider_requests"] - passive["provider_requests"],
            "tool_calls_saved": baseline["tool_calls"] - passive["tool_calls"],
            "currency_saved": baseline["charged_currency"] - passive["charged_currency"] if verified else "NOT_AVAILABLE",
            "first_triggering_call_saved": False,
            "success": "NOT_DETERMINED_BY_COST_ALONE_REQUIRE_TASK_QUALITY_AND_NECESSARY_ACTION_RETENTION"}
