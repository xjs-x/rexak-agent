"""离线工程复盘：只读真实包，保留未知；不推断效果或目标真值。"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent_workbench.maintenance.run_bundle import verify_run_bundle


def analyze_bundle(directory: Path) -> dict[str, Any]:
    errors = verify_run_bundle(directory)
    if errors:
        raise ValueError(f"实验包完整性核对失败：{errors}")
    payload = json.loads((directory / "diagnostic.json").read_text(encoding="utf-8"))
    trial = json.loads((directory / "trial.json").read_text(encoding="utf-8"))
    manifest_index = json.loads((directory / "manifests/index.json").read_text(encoding="utf-8"))
    calls = payload.get("model_calls") or []
    phases: dict[str, dict[str, Any]] = {}
    for phase in sorted({str(row.get("phase") or "unknown") for row in calls}):
        rows = [row for row in calls if str(row.get("phase") or "unknown") == phase]
        known_input = [row["input_tokens"] for row in rows if row.get("input_tokens") is not None]
        known_output = [
            row["output_tokens"] for row in rows if row.get("output_tokens") is not None
        ]
        costs: dict[str, Decimal] = defaultdict(Decimal)
        for row in rows:
            if row.get("cost_amount") is not None and row.get("cost_currency"):
                costs[row["cost_currency"]] += Decimal(str(row["cost_amount"]))
        phases[phase] = {
            "physical_attempts": len(rows),
            "retry_attempts": sum(int(row.get("retry_index") or 0) > 0 for row in rows),
            "known_input_tokens_sum": sum(known_input) if known_input else None,
            "unknown_input_attempts": len(rows) - len(known_input),
            "known_output_tokens_sum": sum(known_output) if known_output else None,
            "unknown_output_attempts": len(rows) - len(known_output),
            "known_cost_by_currency": {key: str(value) for key, value in costs.items()},
            "unknown_cost_attempts": sum(row.get("cost_amount") is None for row in rows),
            "usage_statuses": dict(
                Counter(str(row.get("usage_status") or "unknown") for row in rows)
            ),
        }
    by_call = {row["model_call_id"]: row for row in calls}
    calibration = []
    recalls = []
    for source in manifest_index.get("requests") or []:
        if not source.get("path"):
            continue
        manifest_path = directory / source["path"]
        if (
            not manifest_path.resolve().is_relative_to(directory.resolve())
            or manifest_path.is_symlink()
        ):
            raise ValueError("manifest 来源路径越过实验包边界")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        row = by_call.get(source["model_call_id"], {})
        estimated = (manifest.get("budget") or {}).get("estimated_input_tokens")
        reported = row.get("input_tokens")
        calibration.append(
            {
                "model_call_id": source["model_call_id"],
                "phase": row.get("phase"),
                "estimated_input_tokens": estimated,
                "reported_input_tokens": reported,
                "estimate_minus_reported": estimated - reported
                if estimated is not None and reported is not None
                else None,
                "input_projection_is_exact": False,
            }
        )
        view = manifest.get("working_context") or {}
        if (view.get("next") or {}).get("review_due"):
            recalls.append(
                {
                    "model_call_id": source["model_call_id"],
                    "selection": view["next"],
                    "leads": (view.get("judgment") or {}).get("leads") or [],
                    "proves_reevaluation": False,
                }
            )
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for row in payload.get("operations") or []:
        if row.get("request_digest"):
            groups[
                (str(row.get("run_id")), str(row.get("tool_name")), row["request_digest"])
            ].append(row["operation_id"])
    repeated = [
        {
            "run_id": key[0],
            "tool_name": key[1],
            "request_digest": key[2],
            "operation_ids": ids,
            "classification": "repeat_candidate_requires_condition_and_purpose_review",
        }
        for key, ids in groups.items()
        if len(ids) > 1
    ]
    events = sorted(payload.get("events") or [], key=lambda row: int(row.get("session_seq") or 0))
    return {
        "schema_version": 1,
        "trial": trial,
        "integrity": "verified",
        "runs": [
            {
                "id": row["id"],
                "status": row.get("status"),
                "created_at": row.get("created_at"),
                "source_at_runner_start": (row.get("config_snapshot") or {}).get(
                    "runtime_source_fingerprint"
                ),
            }
            for row in payload.get("runs") or []
        ],
        "phases": phases,
        "request_budget_calibration": calibration,
        "repeat_candidates": repeated,
        "recall_selections": recalls,
        "timeline": [
            {
                "session_seq": row.get("session_seq"),
                "run_id": row.get("run_id"),
                "created_at": row.get("created_at"),
                "type": row.get("type"),
                "payload": row.get("payload"),
            }
            for row in events
        ],
        "event_counts": dict(Counter(str(row.get("type")) for row in events)),
        "manifest_status": manifest_index.get("status"),
        "reports": [
            {
                "id": row.get("id"),
                "version": row.get("version"),
                "generation_status": row.get("generation_status"),
                "input_data_revision": row.get("input_data_revision"),
            }
            for row in payload.get("reports") or []
        ],
        "actual_result": None,
        "conclusion_accuracy": None,
        "site_recall_rate": None,
        "limitations": [
            "工程日志不代替私有结果项与原始证据核对。",
            "展示旧线索不证明重新评估有效。",
            "重复请求候选可能是有意复查；不得自动判无效。",
            "未知 tokens 和费用单列；各币种不相加。",
            "保留事件可能不完整，不能由最终状态反推历史；估计不等于精确计费。",
        ],
    }


def compare_bundles(directories: list[Path]) -> dict[str, Any]:
    trials = [analyze_bundle(directory) for directory in directories]
    identities = [str(item["trial"].get("trial_id")) for item in trials]
    if len(set(identities)) != len(identities):
        raise ValueError("试验 ID 重复，不能当作独立样本")
    return {
        "schema_version": 1,
        "trials": trials,
        "effect_estimate": None,
        "notice": "逐次保留基础设施失败、提前停止和辅助介入；无真值与可比条件时不计算胜率或平均完成时间。",
    }
