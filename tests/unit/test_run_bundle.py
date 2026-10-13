"""离线实验包必须保留未知、证明实际复制字节，并拒绝覆盖和身份混淆。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from agent_workbench.cli import app
from agent_workbench.config.settings import Settings
from agent_workbench.contracts.experiments import TrialRegistration
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.maintenance.diagnostics import _safe
from agent_workbench.maintenance.run_bundle import verify_run_bundle, write_run_bundle


def _payload(store: ArtifactStore) -> dict:
    session_id, first, last, operation_id = (str(uuid4()) for _ in range(4))
    artifacts = []
    for content, source in (
        ("真实响应 password=synthetic-secret", operation_id),
        ("协议字段不能导出", None),
    ):
        stored = store.write_bytes(content.encode("utf-8"), media_type="text/plain")
        artifacts.append(
            {
                "id": str(uuid4()),
                "sha256": stored.sha256,
                "byte_size": stored.byte_size,
                "relative_key": stored.relative_key,
                "source_operation_id": source,
                "availability": "available",
            }
        )
    runs = [
        {
            "id": identifier,
            "code_version": "dev",
            "prompt_version": "1",
            "provider_id": "fake",
            "model_name": "scripted",
            "config_snapshot": {},
            "created_at": "2026-10-13T00:00:00+00:00",
            "updated_at": "2026-10-13T00:01:00+00:00",
            "status": "paused",
        }
        for identifier in (first, last)
    ]
    runs[0]["status"] = "completed"
    return {
        "exported_at": datetime.now(UTC).isoformat(),
        "session": {"id": session_id},
        "run": runs[-1],
        "runs": runs,
        "snapshot": {"database_read_at": "2026-10-13T00:02:00+00:00", "session_event_seq": 2},
        "session_knowledge": {"leads": [{"id": "lead-1", "revision": 1}]},
        "events": [
            {"session_seq": index, "run_id": identifier, "type": "run.status"}
            for index, identifier in enumerate((first, last), 1)
        ],
        "operations": [{"operation_id": operation_id}],
        "model_calls": [
            {
                "model_call_id": str(uuid4()),
                "phase": "agent",
                "usage_status": "unknown",
                "input_tokens": None,
            }
        ],
        "reports": [],
        "artifacts": artifacts,
        "missing_artifact_metadata": [str(uuid4())],
    }


def _store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(Settings(data_root=tmp_path / "private-data"))


def test_bundle_contains_actual_evidence_unknowns_and_immutable_snapshot(tmp_path):
    store = _store(tmp_path)
    payload = _payload(store)
    registration = TrialRegistration.model_validate(
        {
            "trial_id": "T01-V1-1",
            "environment_alias": "T01",
            "group": "V1",
            "repetition": 1,
            "started_at": "2026-10-13T08:00:00+08:00",
            "model_config": {"api_key": "never-export-me", "max_tokens": 1024},
        }
    )
    directory = write_run_bundle(tmp_path / "bundle", payload, store, registration=registration)
    assert verify_run_bundle(directory) == []
    trial = json.loads((directory / "trial.json").read_text(encoding="utf-8"))
    assert trial["run_ids"] == [run["id"] for run in payload["runs"]]
    assert trial["source_commit"] is None
    assert trial["ended_at"] is None and trial["end_kind"] is None
    assert trial["started_at"] == "2026-10-13T00:00:00+00:00"
    assert trial["local_times"]["started_at"] == "2026-10-13T08:00:00+08:00"
    assert trial["model_config"]["api_key"] == "<redacted>"
    assert trial["model_config"]["max_tokens"] == 1024
    assert len((directory / "timeline.jsonl").read_text(encoding="utf-8").splitlines()) == 2
    index = json.loads((directory / "evidence/index.json").read_text(encoding="utf-8"))
    assert [row["status"] for row in index] == [
        "copied",
        "excluded_without_tool_or_report_provenance",
        "missing_metadata",
    ]
    copied = directory / index[0]["path"]
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == payload["artifacts"][0]["sha256"]
    assert not list(directory.rglob(f"{payload['artifacts'][1]['id']}.bin"))
    manifest = json.loads((directory / "manifests/index.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "missing"
    assert (
        json.loads((directory / "diagnostic.json").read_text(encoding="utf-8"))["model_calls"][0][
            "input_tokens"
        ]
        is None
    )
    payload["session_knowledge"]["leads"][0]["revision"] = 5
    snapshot = json.loads((directory / "snapshots/export.json").read_text(encoding="utf-8"))
    assert snapshot["session_knowledge"]["leads"][0]["revision"] == 1
    for path in directory.rglob("*.json"):
        assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
    with pytest.raises(FileExistsError):
        write_run_bundle(directory, payload, store)
    assert verify_run_bundle(directory) == []


def test_copy_detects_change_after_database_hash_check(tmp_path):
    store = _store(tmp_path)
    payload = _payload(store)
    store.resolve(payload["artifacts"][0]["relative_key"]).write_bytes(b"changed")
    directory = write_run_bundle(tmp_path / "bundle", payload, store)
    index = json.loads((directory / "evidence/index.json").read_text(encoding="utf-8"))
    assert index[0]["status"] == "hash_or_size_mismatch"
    assert not list((directory / "evidence").glob("*.bin"))
    assert not json.loads((directory / "bundle.json").read_text(encoding="utf-8"))[
        "evidence_complete"
    ]
    assert verify_run_bundle(directory) == []  # 缺证据有明确记录，包本身没有损坏。


def test_bundle_verifier_detects_corruption_missing_extra_and_cli_exit(tmp_path):
    store = _store(tmp_path)
    directory = write_run_bundle(tmp_path / "bundle", _payload(store), store)
    runner = CliRunner()
    assert runner.invoke(app, ["verify-bundle", str(directory)]).exit_code == 0
    (directory / "trial.json").write_bytes(b"{}")
    assert "hash_or_size_mismatch:trial.json" in verify_run_bundle(directory)
    (directory / "timeline.jsonl").unlink()
    (directory / "extra.txt").write_bytes(b"unexpected")
    errors = verify_run_bundle(directory)
    assert "missing:timeline.jsonl" in errors and "unexpected:extra.txt" in errors
    assert runner.invoke(app, ["verify-bundle", str(directory)]).exit_code == 1


def test_trial_identity_and_timestamps_are_validated_before_writing(tmp_path):
    store = _store(tmp_path)
    payload = _payload(store)
    for registration in (
        TrialRegistration(trial_id="x", session_id=uuid4()),
        TrialRegistration(trial_id="x", run_ids=[uuid4()]),
    ):
        with pytest.raises(ValueError, match="不一致"):
            write_run_bundle(tmp_path / "bundle", payload, store, registration=registration)
    assert not (tmp_path / "bundle").exists()
    with pytest.raises(ValidationError, match="UTC"):
        TrialRegistration.model_validate({"trial_id": "x", "started_at": "2026-10-13T08:00:00"})
    with pytest.raises(ValidationError, match="IANA"):
        TrialRegistration(trial_id="x", timezone="unknown/zone")
    with pytest.raises(ValidationError):
        TrialRegistration.model_validate({"trial_id": "x", "reference_answer": "never admitted"})


def test_metadata_argument_requires_bundle_without_connecting_database():
    result = CliRunner().invoke(app, ["export-run", str(uuid4()), "--trial", "missing.json"])
    assert result.exit_code == 2 and "--bundle" in result.stdout


def test_bundle_index_rejects_path_escape_and_modified_index(tmp_path):
    store = _store(tmp_path)
    directory = write_run_bundle(tmp_path / "bundle", _payload(store), store)
    index_path = directory / "bundle-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["files"][0]["path"] = "../outside.txt"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    errors = verify_run_bundle(directory)
    assert "unsafe_path:../outside.txt" in errors
    assert "checksum_manifest_mismatch" in errors


def test_diagnostic_hides_protocol_reasoning_but_preserves_configuration():
    result = _safe(
        {
            "reasoning_content": "hidden-sentinel",
            "model_config": {"thinking": True, "max_tokens": 2048},
            "reasoning_tokens": 123,
        }
    )
    assert result["reasoning_content"] == "<redacted>"
    assert result["model_config"] == {"thinking": True, "max_tokens": 2048}
    assert result["reasoning_tokens"] == 123
