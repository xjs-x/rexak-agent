"""封存交付并只读审计已存预测；绝不重新运行正式检测或加载评分标签。"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import math
import statistics
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def distribution(values):
    ordered = sorted(values)
    return {
        "n": len(values),
        "p50_ms": statistics.median(values),
        "p95_ms": ordered[math.ceil(len(values) * 0.95) - 1],
    }


def main():
    frames = {
        row["case"]: row
        for row in json.loads((ROOT / "frozen/inputs.json").read_text(encoding="utf-8"))
    }
    predictions = json.loads((ROOT / "results/predictions.json").read_text(encoding="utf-8"))
    checks = []
    for prediction in predictions:
        observation = prediction["observation"]
        if not observation or not observation["emit_shadow_event"]:
            continue
        frame = frames[prediction["case"]]
        cutoff = datetime.fromisoformat(frame["cutoff"])
        for match in observation["related_history"]:
            if not match["high_confidence_overlap"]:
                continue
            oid = match["operation_ref"].removeprefix("operation:")
            operation = next(row for row in frame["operations"] if row["operation_id"] == oid)
            jobs = [row for row in frame["jobs"] if row["operation_id"] == oid]
            assert len(jobs) == 1 and jobs[0]["job_id"] == operation["job_id"]
            assert operation["status"] == jobs[0]["state"] == "succeeded"
            assert datetime.fromisoformat(jobs[0]["executor_finished_at"]) < cutoff
            assert operation["tool_call_id"] not in frame["context"]["batch_ids"]
            assert operation["run_id"] == frame["grant"]["run_id"]
            evidence = []
            for ref in match["evidence_refs"]:
                aid = ref.removeprefix("artifact:")
                art = next(row for row in frame["artifacts"] if row["id"] == aid)
                raw = base64.b64decode(frame["bytes"][aid])
                assert art["source_operation_id"] == oid
                assert aid in operation["result_artifact_ids"]
                assert art["session_id"] == operation["session_id"] == frame["grant"]["session_id"]
                assert art["sha256"] == hashlib.sha256(raw).hexdigest()
                assert art["byte_size"] == len(raw)
                assert art["availability"] == "available" and not art["truncated"]
                assert datetime.fromisoformat(art["captured_at"]) < cutoff
                text = raw.decode("utf-8")
                visible = []
                for message in frame["context"]["messages"]:
                    if message["type"] != "tool":
                        continue
                    payload = json.loads(message["content"])
                    if payload.get("data", {}).get("body_preview") == text and any(
                        item["artifact_id"] == aid for item in payload.get("artifact_refs", [])
                    ):
                        visible.append(message["tool_call_id"])
                assert visible
                evidence.append(
                    {"artifact_id": aid, "sha256": art["sha256"], "visible_in": visible}
                )
            checks.append({"case": prediction["case"], "operation_id": oid, "evidence": evidence})
    phase_names = ["capture_and_hydration_ms", "history_association_ms", "trigger_ms", "elapsed_ms"]
    phases = {
        name: distribution([row[name] for row in predictions if name in row])
        for name in phase_names
    }
    write(
        ROOT / "verification/saved-prediction-audit.json",
        {
            "method": "read saved predictions and source rows only; detector not called; labels not loaded",
            "checks": checks,
            "phases": phases,
            "history_records_total": sum(row.get("historical_records", 0) for row in predictions),
            "related_candidates_total": sum(
                row.get("related_candidates", 0) for row in predictions
            ),
        },
    )
    before = json.loads((ROOT / "preservation-before.json").read_text(encoding="utf-8"))
    changed = [path for path, expected in before.items() if sha(REPO / path) != expected]
    patch = []
    for path in changed:
        baseline = ROOT / "baseline" / path.replace("/", "__")
        assert sha(baseline) == before[path]
        patch.extend(
            difflib.unified_diff(
                baseline.read_text(encoding="utf-8").splitlines(keepends=True),
                (REPO / path).read_text(encoding="utf-8").splitlines(keepends=True),
                fromfile="pre-task/" + path,
                tofile="delivery/" + path,
            )
        )
    with (ROOT / "verification/production-changes.patch").open(
        "x", encoding="utf-8", newline="\n"
    ) as stream:
        stream.write("".join(patch))
    original = json.loads((ROOT / "freeze.json").read_text(encoding="utf-8"))
    files = {REPO / path for path in before}
    files.update(ROOT.glob("*.py"))
    files.update(ROOT.glob("frozen/*.json"))
    files.update(ROOT.glob("evaluation-source/*.py"))
    files.update(ROOT.glob("baseline/*.py"))
    files.update((REPO / "src/agent_workbench").rglob("*.py"))
    files.update((REPO / "tests/experiments").glob("test_trusted_shadow*.py"))
    files.update(
        ROOT / name for name in ["freeze.json", "ATTEMPT.json", "post-evaluation-fix-plan.json"]
    )
    dependencies = {}
    for path in original["loaded_modules"]:
        resolved = Path(path)
        if not resolved.is_relative_to(REPO):
            dependencies[path] = sha(resolved)
    write(
        ROOT / "delivery-code-freeze.json",
        {
            "purpose": "final implementation and regression source; not a new heldout evaluation",
            "files": {path.relative_to(REPO).as_posix(): sha(path) for path in sorted(files)},
            "dependencies": dependencies,
            "original_evaluation_freeze_sha256": sha(ROOT / "freeze.json"),
        },
    )
    print(
        json.dumps(
            {
                "checked_trigger_sources": len(checks),
                "phases": phases,
                "changed_existing_files": changed,
                "delivery_code_freeze_sha256": sha(ROOT / "delivery-code-freeze.json"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
