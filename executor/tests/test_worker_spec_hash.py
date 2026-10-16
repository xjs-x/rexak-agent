"""序列化文件哈希与不包含自身的逻辑规范哈希必须分别核验。"""

import hashlib
import json

import pytest
from workbench_executor.job_worker import WorkerError, load_spec


def test_canonical_spec_hash_round_trip_and_tamper(tmp_path):
    jobs = tmp_path / "jobs"
    job = jobs / "job-1"
    workspace = tmp_path / "workspaces"
    job.mkdir(parents=True)
    workspace.mkdir()
    spec = {"job_id": "job-1", "argv": ["echo", "中文"], "cwd": str(workspace)}
    identity = hashlib.sha256(json.dumps(
        spec, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")).hexdigest()
    spec["spec_hash"] = identity
    path = job / "spec.json"
    raw = json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    path.write_bytes(raw)
    path.with_suffix(".sha256").write_text(hashlib.sha256(raw).hexdigest(), encoding="utf-8")
    loaded, digest = load_spec(path, job_id="job-1", jobs_root=jobs, workspace_root=workspace)
    assert loaded["argv"] == ["echo", "中文"]
    assert digest == identity
    spec["argv"] = ["echo", "changed"]
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(WorkerError, match="哈希"):
        load_spec(path, job_id="job-1", jobs_root=jobs, workspace_root=workspace)


def test_python_script_uses_resolved_interpreter_and_json_stdin(tmp_path):
    from uuid import uuid4

    from workbench_executor._generated.contracts.execution_runtime import ExecutionOperationEnvelope
    from workbench_executor.command_runtime import CommandRuntime
    from workbench_executor.settings import ExecutorSettings

    settings = ExecutorSettings(data_root=tmp_path, workspace_root=tmp_path / "workspaces",
                                python_interpreter="", token="fixture")
    runtime = CommandRuntime(settings, None, "fixture")
    envelope = ExecutionOperationEnvelope(operation_id=str(uuid4()), session_id=str(uuid4()),
        run_id=str(uuid4()), tool_call_id="script", request_digest="fixture", action="run_script")
    spec = runtime._build_spec(envelope, {"language": "python", "code": "print('fixture')",
        "params_json": '{"value":"中文"}'}, "script", str(uuid4()), "fixture", 4096)
    assert spec["argv"][0] == settings.resolved_python_interpreter
    assert spec["argv"][0]
    assert spec["stdin_text"] == '{"value":"中文"}'
