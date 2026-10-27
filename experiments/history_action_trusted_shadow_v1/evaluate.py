"""生产合同夹具的事前封存、一次性离线评价与保全核验。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, cast

from experiments.history_action_trusted_shadow_v1.dataset import CASES, generate, predict

from agent_workbench.tools.http import http_request

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
MODIFIED = {
    "src/agent_workbench/config/settings.py",
    "src/agent_workbench/tools/runtime.py",
    "src/agent_workbench/tools/http.py",
    "src/agent_workbench/runtime/runner.py",
}
NEW_PRODUCTION = [
    "src/agent_workbench/contracts/history_action_reflection.py",
    "src/agent_workbench/domain/history_action_reflection.py",
    "src/agent_workbench/domain/trusted_http_shadow.py",
    "src/agent_workbench/tools/history_action_shadow.py",
]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def preserved():
    before = json.loads((ROOT / "preservation-before.json").read_text(encoding="utf-8"))
    changed = [p for p, h in before.items() if not (REPO / p).is_file() or sha(REPO / p) != h]
    forbidden = set(changed) - MODIFIED
    if forbidden:
        raise ValueError("unexpected_existing_file_changes:" + ",".join(sorted(forbidden)))
    return {
        "baseline_files": len(before),
        "authorized_production_changes": sorted(changed),
        "all_other_existing_files_unchanged": len(before) - len(changed),
    }


async def prepare():
    directory = ROOT / "frozen"
    directory.mkdir(exist_ok=False)
    # 先固化标签；输入产生仅使用 CaptureOnly，不运行重复检测器。
    write(
        directory / "labels.json",
        [
            {
                "case": name,
                "bucket": bucket,
                "expected_trigger": positive,
                "rationale": "明确完成且同条件"
                if positive
                else "不满足高置信度重复条件；保留未支持/来源缺口分层",
            }
            for name, bucket, positive in CASES
        ],
    )
    write(
        directory / "protocol.json",
        {
            "source": "PRODUCTION_CONTRACT_FIXTURE_NOT_REAL_TARGET",
            "independence": "SAME_AUTHOR_PREDECLARED_NOT_HUMAN_BLIND_OR_AUTHOR_ISOLATED",
            "primary_metric_denominator": "ADAPTED cases only; unsupported and history gaps reported separately",
            "normalization": "business UUIDs mapped by ToolCall provenance; known timestamp fields normalized; message technical IDs excluded; ToolCall ID/name/args/content retained",
            "new_evaluation_once": True,
            "input_generation_detector_calls": 0,
        },
    )
    frames = []
    for name, _, _ in CASES:
        frames.append(await generate(ROOT / "generation" / name, name))
    write(directory / "inputs.json", frames)
    write(
        directory / "tool-schema.json", cast(Any, http_request.tool_call_schema).model_json_schema()
    )
    sources = [
        *ROOT.glob("*.py"),
        REPO / "tests/experiments/test_trusted_shadow_integration.py",
        *(REPO / p for p in [*MODIFIED, *NEW_PRODUCTION]),
        REPO / "src/agent_workbench/tools/remote.py",
        REPO / "src/agent_workbench/tools/operation.py",
        REPO / "src/agent_workbench/persistence/repositories.py",
        REPO / "src/agent_workbench/persistence/tables.py",
        REPO / "src/agent_workbench/agent/middleware/tool_dispatch.py",
        REPO / "src/agent_workbench/agent/factory.py",
        REPO / "executor/src/workbench_executor/http_sessions.py",
        REPO / "uv.lock",
        REPO / "pyproject.toml",
        *directory.glob("*.json"),
        *ROOT.glob("baseline/*.py"),
    ]
    loaded = {
        str(Path(str(m.__file__)).resolve()): sha(Path(str(m.__file__)))
        for m in list(sys.modules.values())
        if getattr(m, "__file__", None)
        and Path(str(m.__file__)).is_file()
        and Path(str(m.__file__)).suffix in {".py", ".pyd"}
    }
    write(
        ROOT / "freeze.json",
        {
            "files": {p.relative_to(REPO).as_posix(): sha(p) for p in sources},
            "loaded_modules": loaded,
            "runtime": sys.version,
            "packages": {
                n: importlib.metadata.version(n)
                for n in ("pydantic", "langchain", "langgraph", "sqlalchemy", "httpx", "pytest")
            },
            "preservation": preserved(),
        },
    )
    print("prepared", len(frames), "freeze", sha(ROOT / "freeze.json"))


def verify():
    frozen = json.loads((ROOT / "freeze.json").read_text(encoding="utf-8"))
    for p, h in frozen["files"].items():
        if sha(REPO / p) != h:
            raise ValueError("frozen_source_or_input_drift:" + p)
    for p, h in frozen["loaded_modules"].items():
        if sha(Path(p)) != h:
            raise ValueError("dependency_drift:" + p)
    return {"freeze_sha256": sha(ROOT / "freeze.json"), "preservation": preserved()}


def run():
    write(ROOT / "ATTEMPT.json", {"status": "STARTED_ONCE", **verify()})
    directory = ROOT / "results"
    directory.mkdir(exist_ok=False)
    frames = json.loads((ROOT / "frozen/inputs.json").read_text(encoding="utf-8"))
    predictions = []
    for frame in frames:
        start = time.perf_counter_ns()
        value = predict(frame)
        value["elapsed_ms"] = (time.perf_counter_ns() - start) / 1e6
        predictions.append(value)
    write(directory / "predictions.json", predictions)
    # 所有预测落盘后才读取冻结标签；不重试、不按分数改变规则。
    labels = json.loads((ROOT / "frozen/labels.json").read_text(encoding="utf-8"))
    by_case = {v["case"]: v for v in predictions}
    matrix = Counter({"tp": 0, "fp": 0, "fn": 0, "tn": 0})
    bucket_counts = Counter()
    errors = []
    for label in labels:
        observed = by_case[label["case"]]
        bucket_counts[label["bucket"]] += 1
        triggered = bool(observed["observation"] and observed["observation"]["emit_shadow_event"])
        if label["bucket"] == "ADAPTED":
            outcome = (
                "tp"
                if triggered and label["expected_trigger"]
                else "fp"
                if triggered
                else "fn"
                if label["expected_trigger"]
                else "tn"
            )
            matrix[outcome] += 1
        if triggered != label["expected_trigger"]:
            errors.append(
                {
                    "case": label["case"],
                    "expected": label["expected_trigger"],
                    "actual": triggered,
                    "bucket": label["bucket"],
                }
            )
    tp, fp, fn, tn = (matrix[k] for k in ("tp", "fp", "fn", "tn"))
    metrics = {
        "source": "PRODUCTION_CONTRACT_FIXTURE",
        "total": len(predictions),
        "buckets": dict(bucket_counts),
        "primary_denominator": sum(matrix.values()),
        "matrix": dict(matrix),
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "false_positive_rate": fp / (fp + tn) if fp + tn else None,
        "false_negative_rate": fn / (tp + fn) if tp + fn else None,
        "candidate_adapter_complete": sum(p["adapter_complete"] for p in predictions),
        "trigger_events": sum(
            bool(p["observation"] and p["observation"]["emit_shadow_event"]) for p in predictions
        ),
        "mismatches": errors,
        "unsupported_and_history_gaps_not_counted_as_TN": True,
    }
    write(directory / "metrics.json", metrics)
    write(
        directory / "COMPLETE.json",
        {
            "status": "COMPLETED_ONCE",
            **verify(),
            "real_model_requests": 0,
            "real_target_requests": 0,
        },
    )
    print(json.dumps(metrics, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "run", "verify"])
    cmd = parser.parse_args().command
    if cmd == "prepare":
        asyncio.run(prepare())
    elif cmd == "run":
        run()
    else:
        print(json.dumps(verify(), ensure_ascii=False))
