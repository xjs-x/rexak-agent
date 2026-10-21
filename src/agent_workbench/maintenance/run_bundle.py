"""本机私有实验包：来源、时点与缺失明确可查，不启动任何目标或模型。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent_workbench.contracts.experiments import (
    BUNDLE_SCHEMA_VERSION,
    BundleFile,
    BundleIndex,
    TrialRegistration,
)
from agent_workbench.evidence.artifacts import ArtifactError, ArtifactStore
from agent_workbench.maintenance.diagnostics import _safe, collect_run_diagnostic
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.db import Database

_PROJECTION_FIELDS = (
    "active_skills",
    "skill_directives",
    "state_schema_version",
    "active_run_id",
    "segment_index",
    "objective",
    "user_constraints",
    "plan",
    "plan_revision",
    "last_plan_change",
    "work_summary",
    "active_lead_ids",
    "pending_job_ids",
    "current_focus",
    "review_cursor",
    "last_review_turn",
    "work_assessment",
    "presented_weakness_ids",
    "weakness_review_cursor",
    "weakness_review_signature",
    "last_weakness_revisions",
    "last_recall_keys",
    "last_identity_signature",
    "segment_yield_reason",
)


async def collect_run_bundle(database: Database, run_id: UUID) -> dict[str, Any]:
    """数据库一致读后另读 checkpoint，始终标明组合快照非原子。"""
    payload = await collect_run_diagnostic(database, run_id, include_session_runs=True)
    started_at = datetime.now(UTC).isoformat()
    async with open_checkpointer_connection(database.settings) as connection:
        saved = await AsyncPostgresSaver(connection).aget_tuple(
            {"configurable": {"thread_id": payload["session"]["thread_id"]}}
        )
    values = saved.checkpoint.get("channel_values", {}) if saved is not None else {}
    payload["checkpoint_projection"] = {
        "status": "available" if saved is not None else "missing",
        "read_started_at": started_at,
        "read_completed_at": datetime.now(UTC).isoformat(),
        "checkpoint_id": saved.checkpoint.get("id") if saved is not None else None,
        "checkpoint_created_at": saved.checkpoint.get("ts") if saved is not None else None,
        "channel_versions": _safe(saved.checkpoint.get("channel_versions", {})) if saved else {},
        "values": _safe({key: values[key] for key in _PROJECTION_FIELDS if key in values}),
        "scope": "latest_checkpoint_at_export_not_historical_model_input",
    }
    payload["snapshot"]["combined_consistency"] = "non_atomic_database_checkpoint_artifacts"
    from agent_workbench.skills.export import collect_skill_dependencies

    await collect_skill_dependencies(database, payload, values)
    return payload


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def _trial(payload: dict[str, Any], registration: TrialRegistration | None) -> dict[str, Any]:
    session_id = UUID(payload["session"]["id"])
    run_ids = [UUID(run["id"]) for run in payload["runs"]]
    registration = registration or TrialRegistration(trial_id=payload["run"]["id"])
    if registration.session_id is not None and registration.session_id != session_id:
        raise ValueError("实验登记 session_id 与导出会话不一致")
    if registration.run_ids and set(registration.run_ids) != set(run_ids):
        raise ValueError("实验登记 run_ids 与快照中的完整会话 run 集合不一致")
    result = _safe(registration.model_dump(mode="json"))
    result["model_config"] = result.pop("model_config_snapshot")
    result.update(
        schema_version=BUNDLE_SCHEMA_VERSION,
        session_id=str(session_id),
        run_ids=[str(identifier) for identifier in run_ids],
        selected_run_id=payload["run"]["id"],
        exported_at=payload["exported_at"],
        runtime_records=[
            {
                key: run[key]
                for key in (
                    "id",
                    "code_version",
                    "prompt_version",
                    "provider_id",
                    "model_name",
                    "config_snapshot",
                    "created_at",
                    "updated_at",
                    "status",
                )
            }
            for run in payload["runs"]
        ],
        provenance={
            "registration": "operator_supplied_not_independently_verified"
            if registration.model_fields_set - {"trial_id"}
            else "missing",
            "runtime_fingerprints": "missing_in_baseline_unless_recorded_in_run_config",
            "exporter_source": "export_time_only_not_runtime_source",
        },
    )
    result["missing_registration_fields"] = [
        key
        for key in (
            "environment_alias",
            "group",
            "repetition",
            "source_commit",
            "source_tree_hash",
            "lock_hash",
            "prompt_version",
            "model_config",
            "tool_manifest_hash",
            "environment_snapshot_id",
            "memory_snapshot_id",
            "started_at",
            "ended_at",
            "end_kind",
        )
        if result.get(key) is None
    ]
    result["local_times"] = {
        key: value.astimezone(ZoneInfo(registration.timezone)).isoformat() if value else None
        for key, value in (
            ("started_at", registration.started_at),
            ("ended_at", registration.ended_at),
            ("assisted_from", registration.assisted_from),
        )
    }
    # 统一时间字段为 UTC；本地时间和时区单独保存。
    for key in ("started_at", "ended_at", "assisted_from"):
        value = getattr(registration, key)
        result[key] = value.astimezone(UTC).isoformat() if value else None
    return result


def _copy_evidence(
    directory: Path, payload: dict[str, Any], store: ArtifactStore
) -> list[dict[str, Any]]:
    operation_ids = {row["operation_id"] for row in payload["operations"]}
    report_ids = {
        row[key]
        for row in payload["reports"]
        for key in ("markdown_artifact_id", "html_artifact_id")
        if row.get(key)
    }
    audit_roles = payload.get("audit_artifact_roles", {})
    index = []
    for artifact in payload["artifacts"]:
        record = {
            "artifact_id": artifact["id"],
            "source_sha256": artifact["sha256"],
            "source_byte_size": artifact["byte_size"],
            "path": None,
        }
        # 只复制有明确来源的工具、报告、审计和固定 Skill 依赖；不伪造目标证据。
        if (
            artifact.get("source_operation_id") not in operation_ids
            and artifact["id"] not in report_ids
            and artifact["id"] not in audit_roles
        ):
            record["status"] = "excluded_without_tool_or_report_provenance"
        elif artifact.get("availability") != "available":
            record["status"] = "unavailable"
        else:
            relative = (
                f"manifests/{UUID(artifact['id'])}.json"
                if audit_roles.get(artifact["id"]) == "request_manifest"
                else f"plans/revisions/{UUID(artifact['id'])}.json"
                if audit_roles.get(artifact["id"]) == "plan_revision_candidate"
                else f"skills/catalogs/{UUID(artifact['id'])}.json"
                if audit_roles.get(artifact["id"]) == "skill_catalog"
                else f"skills/packages/{UUID(artifact['id'])}.zip"
                if audit_roles.get(artifact["id"]) == "skill_package"
                else f"evidence/{UUID(artifact['id'])}.bin"
            )
            record["role"] = audit_roles.get(artifact["id"], "tool_or_report")
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                source = store.resolve(artifact["relative_key"])
                # 校验复制后的字节，避免预先校验和实际复制之间的变化被掩盖。
                with source.open("rb") as reader, target.open("xb") as writer:
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := reader.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                        writer.write(chunk)
                if digest.hexdigest() != artifact["sha256"] or size != artifact["byte_size"]:
                    target.unlink()
                    record["status"] = "hash_or_size_mismatch"
                else:
                    record.update(
                        path=relative, status="copied", sha256=digest.hexdigest(), byte_size=size
                    )
            except (ArtifactError, OSError) as exc:
                target.unlink(missing_ok=True)
                record.update(status="copy_failed", error_code=type(exc).__name__)
        index.append(record)
    index.extend(
        {"artifact_id": identifier, "status": "missing_metadata", "path": None}
        for identifier in payload["missing_artifact_metadata"]
    )
    return index


def _seal(directory: Path) -> None:
    files = [
        BundleFile(
            path=path.relative_to(directory).as_posix(),
            byte_size=path.stat().st_size,
            sha256=_file_hash(path),
        )
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    ]
    _write_json(directory / "bundle-index.json", BundleIndex(files=files).model_dump())
    index_file = directory / "bundle-index.json"
    files.append(
        BundleFile(
            path=index_file.name, byte_size=index_file.stat().st_size, sha256=_file_hash(index_file)
        )
    )
    (directory / "checksums.sha256").write_text(
        "".join(f"{item.sha256}  {item.path}\n" for item in files), encoding="ascii", newline="\n"
    )


def _file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def write_run_bundle(
    target_dir: Path,
    payload: dict[str, Any],
    store: ArtifactStore,
    *,
    registration: TrialRegistration | None = None,
    exporter_source: dict[str, Any] | None = None,
) -> Path:
    """在相邻临时目录完整构建后发布；已存在的实验包绝不覆盖。"""
    trial = _trial(payload, registration)
    if target_dir.exists():
        raise FileExistsError(f"实验包已存在，请选择新的输出目录：{target_dir}")
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".run-bundle-", dir=target_dir.parent) as temporary:
        directory = Path(temporary) / "bundle"
        directory.mkdir()
        # 即使操作者选择仓库内输出路径，也阻止普通 git add 收录私有原件。
        (directory / ".gitignore").write_text("*\n", encoding="utf-8", newline="\n")
        _write_json(directory / "trial.json", trial)
        _write_json(directory / "diagnostic.json", payload)
        _write_json(
            directory / "pending-model-observations.json",
            payload.get("pending_model_observations", []),
        )
        _write_json(directory / "exporter-source.json", exporter_source or {"status": "missing"})
        (directory / "timeline.jsonl").write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in payload["events"]),
            encoding="utf-8",
            newline="\n",
        )
        _write_json(
            directory / "snapshots" / "export.json",
            {
                "snapshot": payload["snapshot"],
                "session_id": payload["session"]["id"],
                "session_knowledge": payload["session_knowledge"],
                "checkpoint": payload.get("checkpoint_projection", {"status": "missing"}),
                "scope": "export_time_only_not_historical_stage_snapshots",
            },
        )
        _write_json(directory / "reports" / "index.json", payload["reports"])
        _write_json(
            directory / "skills" / "index.json",
            payload.get("skills", {"versions": [], "errors": []}),
        )
        stage_snapshots = []
        for report in payload["reports"]:
            if report.get("captured_snapshot") is None:
                continue
            relative = f"snapshots/stages/{report['id']}.json"
            _write_json(
                directory / relative,
                {
                    "report_id": report["id"],
                    "report_version": report["version"],
                    "input_data_revision": report.get("input_data_revision"),
                    "snapshot": report["captured_snapshot"],
                    "scope": "recorded_report_boundary",
                    "atomic_with_checkpoint": False,
                },
            )
            stage_snapshots.append(
                {"report_id": report["id"], "version": report["version"], "path": relative}
            )
        _write_json(
            directory / "snapshots/index.json",
            {
                "status": "available" if stage_snapshots else "missing",
                "stages": stage_snapshots,
                "scope": "recorded_report_boundaries_only_not_every_historical_state",
            },
        )
        evidence = _copy_evidence(directory, payload, store)
        copied = {row["artifact_id"]: row["path"] for row in evidence if row["status"] == "copied"}
        manifests = [
            {**row, "path": copied.get(row["manifest_artifact_id"])}
            for row in payload.get("request_manifests", [])
        ]
        associated = {row["model_call_id"] for row in manifests if row["path"]}
        missing_calls = [
            row["model_call_id"]
            for row in payload["model_calls"]
            if row["model_call_id"] not in associated
        ]
        manifest_status = (
            "available"
            if manifests and not missing_calls
            else "partial"
            if manifests
            else "missing"
        )
        _write_json(
            directory / "manifests" / "index.json",
            {
                "status": manifest_status,
                "requests": manifests,
                "missing_model_call_ids": missing_calls,
                "reason": "缺失项不能从最终 checkpoint 反推历史输入；已记录的 manifest 对应发送前准备的请求，不证明网络成功。",
            },
        )
        _write_json(directory / "evidence" / "index.json", evidence)
        (directory / "operator-notes.jsonl").write_bytes(b"")
        _write_json(
            directory / "evaluation-private.json", {"status": "not_registered", "items": []}
        )
        _write_json(
            directory / "bundle.json",
            {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "private": True,
                "model_observations_pending_sync": len(
                    payload.get("pending_model_observations", [])
                ),
                "consistency": "non_atomic_database_checkpoint_artifacts",
                "evidence_complete": all(row["status"] == "copied" for row in evidence)
                and not payload.get("skills", {}).get("errors"),
                "missing_capabilities": [
                    *([] if stage_snapshots else ["historical_stage_snapshots"]),
                    *(["final_request_manifests"] if manifest_status != "available" else []),
                    "runtime_source_fingerprints",
                ],
                "notice": "工具及报告原件可能含敏感自由文本，仅留本机私有目录；哈希只检查内容一致性。",
            },
        )
        _seal(directory)
        directory.rename(target_dir)
    return target_dir


def verify_run_bundle(directory: Path) -> list[str]:
    """核对索引、大小、SHA-256、缺件和额外文件；不将一致性当作结论正确。"""
    index = BundleIndex.model_validate_json((directory / "bundle-index.json").read_bytes())
    errors = []
    expected = {"bundle-index.json", "checksums.sha256"}
    root = directory.resolve()
    for item in index.files:
        path = directory / item.path
        if (
            Path(item.path).is_absolute()
            or ".." in Path(item.path).parts
            or path.is_symlink()
            or not path.resolve().is_relative_to(root)
            or "\\" in item.path
        ):
            errors.append(f"unsafe_path:{item.path}")
            continue
        if item.path in expected:
            errors.append(f"duplicate_path:{item.path}")
            continue
        expected.add(item.path)
        if not path.is_file():
            errors.append(f"missing:{item.path}")
        elif path.stat().st_size != item.byte_size or _file_hash(path) != item.sha256:
            errors.append(f"hash_or_size_mismatch:{item.path}")
    # 清单也覆盖索引自身；修改索引不能无声绕过缺件检测。
    checksums = directory / "checksums.sha256"
    expected_lines = {f"{item.sha256}  {item.path}" for item in index.files}
    expected_lines.add(f"{_file_hash(directory / 'bundle-index.json')}  bundle-index.json")
    if (
        not checksums.is_file()
        or set(checksums.read_text(encoding="ascii").splitlines()) != expected_lines
    ):
        errors.append("checksum_manifest_mismatch")
    actual = {
        path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()
    }
    errors.extend(f"unexpected:{name}" for name in sorted(actual - expected))
    return errors


async def export_run_bundle(
    database: Database,
    run_id: UUID,
    target_dir: Path,
    *,
    registration: TrialRegistration | None = None,
    exporter_source: dict[str, Any] | None = None,
) -> Path:
    payload = await collect_run_bundle(database, run_id)
    return await asyncio.to_thread(
        write_run_bundle,
        target_dir,
        payload,
        ArtifactStore(database.settings),
        registration=registration,
        exporter_source=exporter_source,
    )
