"""离线复盘不把未知算零，不把重复候选/召回展示转换为效果结论。"""

import json
import subprocess

import pytest
from typer.testing import CliRunner

from agent_workbench.cli import app
from agent_workbench.config.settings import Settings
from agent_workbench.contracts.experiments import TrialRegistration
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.maintenance.experiment_analysis import analyze_bundle, compare_bundles
from agent_workbench.maintenance.experiment_config import prepare_trial
from agent_workbench.maintenance.run_bundle import write_run_bundle
from tests.unit.test_run_bundle import _payload


def test_analysis_preserves_unknowns_currency_and_repeat_candidate_scope(tmp_path):
    store = ArtifactStore(Settings(data_root=tmp_path / "data"))
    payload = _payload(store)
    payload["model_calls"] += [
        {
            "model_call_id": "known-1",
            "phase": "compaction",
            "input_tokens": 30,
            "output_tokens": 5,
            "cost_amount": "0.2",
            "cost_currency": "CNY",
            "usage_status": "reported",
            "retry_index": 0,
        },
        {
            "model_call_id": "known-2",
            "phase": "compaction",
            "input_tokens": 20,
            "output_tokens": 4,
            "cost_amount": "0.1",
            "cost_currency": "USD",
            "usage_status": "reported",
            "retry_index": 1,
        },
    ]
    payload["operations"] += [
        {
            "operation_id": "op-1",
            "run_id": "r",
            "tool_name": "http_request",
            "request_digest": "same",
        },
        {
            "operation_id": "op-2",
            "run_id": "r",
            "tool_name": "http_request",
            "request_digest": "same",
        },
    ]
    directory = write_run_bundle(tmp_path / "bundle", payload, store)
    analysis = analyze_bundle(directory)
    assert analysis["phases"]["agent"]["known_input_tokens_sum"] is None
    assert analysis["phases"]["agent"]["unknown_input_attempts"] == 1
    assert analysis["phases"]["compaction"]["known_cost_by_currency"] == {
        "CNY": "0.2",
        "USD": "0.1",
    }
    assert len(analysis["repeat_candidates"]) == 1
    assert (
        analysis["actual_result"]
        is analysis["conclusion_accuracy"]
        is analysis["site_recall_rate"]
        is None
    )
    with pytest.raises(ValueError, match="重复"):
        compare_bundles([directory, directory])
    output = tmp_path / "analysis.json"
    result = CliRunner().invoke(app, ["analyze-bundle", str(directory), "--output", str(output)])
    assert result.exit_code == 0, result.output
    assert json.loads(output.read_text(encoding="utf-8"))["effect_estimate"] is None
    (directory / "diagnostic.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="完整性"):
        analyze_bundle(directory)


def test_trial_requires_frozen_source_and_generates_isolated_config_without_launch(tmp_path):
    root = tmp_path / "source"
    root.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init")
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    git("add", "uv.lock")
    git(
        "-c",
        "user.name=Trial Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    registration = TrialRegistration(
        trial_id="T01-V1-1", group="V1", environment_alias="T01", repetition=1
    )
    output = prepare_trial(registration, root, tmp_path / "trial", Settings())
    environment = json.loads((output / "environment.json").read_text(encoding="utf-8"))
    setup = json.loads((output / "setup.json").read_text(encoding="utf-8"))
    assert (
        environment["WORKBENCH_MEMORY_READ_BRANCH"]
        == environment["WORKBENCH_MEMORY_WRITE_BRANCH"]
        == "T01-V1-1"
    )
    assert environment["WORKBENCH_OWNER_ID"] == "trial-T01-V1-1"
    assert setup["automatic_target_access"] is False and setup["task_time_or_cost_cap"] is None
    assert setup["database_name"] in environment["WORKBENCH_DATABASE_URL"]
    with pytest.raises(FileExistsError):
        prepare_trial(registration, root, output, Settings())
    with pytest.raises(ValueError, match="原登记"):
        prepare_trial(
            registration.model_copy(update={"source_commit": "mismatched-commit"}),
            root,
            tmp_path / "mismatched-trial",
            Settings(),
        )
    (root / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="冻结"):
        prepare_trial(registration, root, tmp_path / "dirty-trial", Settings())
