"""任务级配方/真实 PostgreSQL Runner 的一次性评价封存；不含真实模型或执行器。"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
ALLOWED = {
    "src/agent_workbench/config/settings.py",
    "src/agent_workbench/domain/trusted_http_shadow.py",
    "src/agent_workbench/tools/history_action_shadow.py",
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def preservation():
    initial = json.loads((ROOT / "preservation-before.json").read_text(encoding="utf-8"))
    changed = [p for p, h in initial.items() if sha(REPO / p) != h]
    if set(changed) - ALLOWED:
        raise ValueError("unexpected_baseline_change:" + str(changed))
    return {
        "initial_files": len(initial),
        "changed": changed,
        "unchanged": len(initial) - len(changed),
    }


def prepare():
    from agent_workbench.tools.http import http_request
    from agent_workbench.tools.shadow_test_profile import run_http_test_profile

    scenarios = [
        {"case": "get-again"},
        {"case": "post-again", "request": {"method": "POST", "body": "fixture-payload"}},
        {"case": "params-again", "request": {"params_json": '{"check":"one"}'}},
        {"case": "json-again", "request": {"method": "POST", "json_body_json": '{"check":"one"}'}},
        {"case": "new-environment", "next_profile": {"environment": "fixture-environment/2"}},
        {"case": "new-configuration", "next_profile": {"configuration": "fixture-configuration/2"}},
        {
            "case": "new-prerequisite",
            "next_profile": {"prerequisites": {"controller_fixture": "ready/2"}},
        },
        {"case": "new-params", "next_request": {"params_json": '{"check":"two"}'}},
        {
            "case": "new-body",
            "request": {"method": "POST", "body": "first"},
            "next_request": {"body": "second"},
        },
        {"case": "new-target", "next_request": {"url": "https://contract.invalid/other"}},
        {"case": "explicit-retest", "next_profile": {"retest_required": True}},
        {"case": "navigation", "navigation": True},
        {"case": "authentication", "next_request": {"identity": "changed-user"}},
        {"case": "cookie-state", "next_request": {"headers_json": '{"Cookie":"synthetic-cookie"}'}},
        {"case": "redirect-state", "next_request": {"follow_redirects": True}},
        {
            "case": "csrf-state",
            "next_request": {
                "csrf_source_url": "https://contract.invalid/form",
                "csrf_input_name": "csrf",
                "csrf_target_name": "csrf",
            },
        },
    ]
    # Oracle 在执行之前固定；实现/标注同作者，非盲评。预测入口不读取这里的标签。
    labels = [
        {
            "case": row["case"],
            "bucket": "COMPARABLE" if i < 11 else "NAVIGATION" if i == 11 else "UNSUPPORTED",
            "expected_trigger": i < 4,
            "basis": "fixed production HTTP recipe and original visible result; variations remain distinct; unsupported states abstain",
        }
        for i, row in enumerate(scenarios)
    ]
    write(ROOT / "frozen/labels.json", labels)
    write(ROOT / "frozen/scenarios.json", scenarios)
    write(
        ROOT / "frozen/protocol.json",
        {
            "source": "PRODUCTION_RUNNER_POSTGRES_MOCK_EXECUTOR",
            "independence": "SAME_AUTHOR_PREDECLARED_NOT_BLIND",
            "initial_history": "empty new API-created session; first tool creates actual ORM/Artifact history",
            "identities": "new Run/Session per scenario allocated by production API; task policy bound before model call; no ToolCall-specific grant",
            "formal_once": True,
            "default_prototype_registered": False,
            "normalization": "business operation/job/artifact IDs by provenance; technical UUIDs by first occurrence; timestamps normalized; technical artifact hashes mapped while original model messages compared independently",
        },
    )
    write(
        ROOT / "frozen/schemas.json",
        {
            "http": cast(Any, http_request.tool_call_schema).model_json_schema(),
            "prototype": cast(Any, run_http_test_profile.tool_call_schema).model_json_schema(),
        },
    )
    files = {
        REPO / p
        for p in json.loads((ROOT / "preservation-before.json").read_text(encoding="utf-8"))
    }
    files.update((REPO / "src/agent_workbench").rglob("*.py"))
    files.update(ROOT.glob("*.py"))
    files.update(ROOT.glob("frozen/*.json"))
    files.update(
        [
            REPO / "tests/integration/test_autonomous_shadow.py",
            REPO / "tests/experiments/test_autonomous_shadow.py",
            REPO / "tests/experiments/test_autonomous_shadow_formal.py",
        ]
    )
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
            "files": {p.relative_to(REPO).as_posix(): sha(p) for p in sorted(files)},
            "loaded_modules": loaded,
            "preservation": preservation(),
            "runtime": sys.version,
        },
    )
    print(sha(ROOT / "freeze.json"))


def verify():
    frozen = json.loads((ROOT / "freeze.json").read_text(encoding="utf-8"))
    for p, h in frozen["files"].items():
        if sha(REPO / p) != h:
            raise ValueError("frozen_file_drift:" + p)
    for p, h in frozen["loaded_modules"].items():
        if sha(Path(p)) != h:
            raise ValueError("loaded_module_drift:" + p)
    return {"freeze_sha256": sha(ROOT / "freeze.json"), "preservation": preservation()}


if __name__ == "__main__":
    if sys.argv[1:] == ["prepare"]:
        prepare()
    elif sys.argv[1:] == ["verify"]:
        print(json.dumps(verify(), ensure_ascii=False))
    else:
        raise SystemExit(
            "choose prepare or read-only verify; formal execution requires isolated pytest entry"
        )
