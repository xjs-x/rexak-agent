"""P0：工件 provenance 与证据用途解析。"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from langchain.tools import ToolRuntime
from sqlalchemy import select

from agent_workbench.contracts.errors import ApiError
from agent_workbench.domain.evidence import EvidencePolicyError, EvidenceUse, resolve_evidence
from agent_workbench.domain.findings import FindingService
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import (
    ArtifactProvenanceRow,
    ArtifactRow,
    DomainIntegrationRow,
    ExecutionResourceRow,
    FindingRow,
    ToolOperationRow,
    VerificationRow,
)
from agent_workbench.tools.jobs import export_execution_output
from agent_workbench.tools.task import record_lead
from agent_workbench.tools.verification import verify_finding
from tests.integration.test_request_budget import context_for

pytestmark = pytest.mark.integration


async def _call(tool_obj: Any, context: Any, args: dict[str, Any], call_id: str) -> Any:
    runtime = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _: None,
        tool_call_id=call_id,
        store=None,
    )
    function = cast(Any, tool_obj).coroutine
    if "tool_call_id" in inspect.signature(function).parameters:
        args = {**args, "tool_call_id": call_id}
    return await function(runtime, **args)


async def _add_operation(db: Any, session_id: UUID, run_id: UUID) -> UUID:
    operation_id = uuid4()
    db.add(
        ToolOperationRow(
            operation_id=operation_id,
            run_id=run_id,
            session_id=session_id,
            tool_call_id=f"op-{operation_id}",
            tool_name="run_script",
            request_digest="a" * 64,
            effect_class="read_only",
            status="succeeded",
            execution_context={"method": "GET", "url": "http://example.test/"},
            result_artifact_ids=[],
        )
    )
    await db.flush()
    return operation_id


async def _add_artifact(
    db: Any,
    store: ArtifactStore,
    session_id: UUID,
    run_id: UUID,
    *,
    data: bytes,
    operation_ids: list[UUID] | None = None,
    producer_kind: str = "execution_output",
    source_trust: str = "ledger",
    resolution_status: str = "resolved",
    source_operation_id: UUID | None = None,
) -> UUID:
    stored = store.write_bytes(data, media_type="text/plain")
    artifact = await ArtifactRepository(db).create(
        sha256=stored.sha256,
        byte_size=stored.byte_size,
        media_type=stored.media_type,
        relative_key=stored.relative_key,
        session_id=session_id,
        source_operation_id=source_operation_id,
        sensitivity="internal",
        availability="available",
        captured_at=stored.captured_at,
        export_tool_call_id=f"export-{uuid4()}",
        producer_kind=producer_kind,
        source_trust=source_trust,
        resolution_status=resolution_status,
    )
    for segment, operation_id in enumerate(operation_ids or []):
        count = len(operation_ids or [])
        start = (stored.byte_size * segment) // count
        end = (stored.byte_size * (segment + 1)) // count
        if end <= start:
            end = min(stored.byte_size, start + 1)
        db.add(
            ArtifactProvenanceRow(
                artifact_id=artifact.id,
                provenance_revision=1,
                segment_no=segment,
                export_tool_call_id=f"export-{artifact.id}-{segment}",
                origin_operation_id=operation_id,
                origin_run_id=run_id,
                resource_id=uuid4(),
                stream="stdout",
                artifact_byte_start=start,
                artifact_byte_end=end,
                source_byte_start=start,
                source_byte_end=end,
                snapshot_hash=stored.sha256,
                connection_origin={"executor_id": "test"},
                producer_kind=producer_kind,
                source_trust=source_trust,
                resolution_status=resolution_status,
                confidence="receipt_verified" if source_trust == "ledger" else "legacy",
                captured_at=stored.captured_at,
            )
        )
    await db.flush()
    return artifact.id


async def test_provenance_resolution_supports_verification_and_lead(database, unique_name):
    context = await context_for(database, unique_name)
    context.toolbox.artifacts = ArtifactStore(database.settings)
    async with database.transaction() as db:
        op1 = await _add_operation(db, context.toolbox.session_id, context.toolbox.run_id)
        op2 = await _add_operation(db, context.toolbox.session_id, context.toolbox.run_id)
        first = await _add_artifact(
            db, context.toolbox.artifacts, context.toolbox.session_id, context.toolbox.run_id,
            data=b"uid=48(apache)\nLinux phineas\n", operation_ids=[op1],
        )
        second = await _add_artifact(
            db, context.toolbox.artifacts, context.toolbox.session_id, context.toolbox.run_id,
            data=b"uid=48(apache)\nLinux phineas\n", operation_ids=[op2],
        )

        resolution = await resolve_evidence(
            db, context.toolbox, [str(first), str(second)], use=EvidenceUse.VERIFICATION
        )
        assert resolution.trust == "ledger"
        assert resolution.resolution == "resolved"
        assert resolution.independent_operations == 2

    result = json.loads(
        await _call(
            record_lead,
            context,
            {
                "hypothesis": "已取得 Web 服务账户执行证据",
                "evidence_refs_json": [str(first), str(second)],
                "next_action": "验证影响边界",
            },
            "lead-with-provenance",
        )
    )
    assert result["ok"] is True
    assert result["evidence_resolution"]["trust"] == "ledger"
    assert result["evidence_resolution"]["independent_operations"] == 2
    async with database.session() as db:
        integration = (
            await db.execute(
                select(DomainIntegrationRow).where(
                    DomainIntegrationRow.session_id == context.toolbox.session_id,
                    DomainIntegrationRow.action == "record_lead",
                )
            )
        ).scalar_one()
    assert integration.status == "integrated"
    assert integration.entity_id is not None


async def test_legacy_artifact_is_lead_hypothesis_but_not_verification(database, unique_name):
    context = await context_for(database, unique_name)
    context.toolbox.artifacts = ArtifactStore(database.settings)
    async with database.transaction() as db:
        artifact = await _add_artifact(
            db, context.toolbox.artifacts, context.toolbox.session_id, context.toolbox.run_id,
            data=b"legacy output",
            operation_ids=[],
            producer_kind="history_archive",
            source_trust="unresolved",
            resolution_status="legacy_unresolved",
        )
        resolution = await resolve_evidence(
            db, context.toolbox, [str(artifact)], use=EvidenceUse.LEAD
        )
        assert resolution.legacy is True
        assert resolution.trust == "unresolved"
        with pytest.raises(EvidencePolicyError):
            await resolve_evidence(
                db, context.toolbox, [str(artifact)], use=EvidenceUse.VERIFICATION
            )


async def test_one_artifact_can_have_multiple_provenance_segments(database, unique_name):
    context = await context_for(database, unique_name)
    context.toolbox.artifacts = ArtifactStore(database.settings)
    async with database.transaction() as db:
        op1 = await _add_operation(db, context.toolbox.session_id, context.toolbox.run_id)
        op2 = await _add_operation(db, context.toolbox.session_id, context.toolbox.run_id)
        artifact = await _add_artifact(
            db, context.toolbox.artifacts, context.toolbox.session_id, context.toolbox.run_id,
            data=b"first\nsecond\n", operation_ids=[op1, op2],
        )
        resolution = await resolve_evidence(
            db, context.toolbox, [str(artifact)], use=EvidenceUse.VERIFICATION
        )
        assert resolution.independent_operations == 2
        rows = (
            await db.execute(
                select(ArtifactProvenanceRow).where(
                    ArtifactProvenanceRow.artifact_id == artifact
                )
            )
        ).scalars().all()
        assert len(rows) == 2


async def test_cross_session_and_hash_mismatch_are_rejected(database, unique_name):
    context = await context_for(database, unique_name)
    context.toolbox.artifacts = ArtifactStore(database.settings)
    other = await context_for(database, f"{unique_name}-other")
    async with database.transaction() as db:
        foreign = await _add_artifact(
            db, context.toolbox.artifacts, other.toolbox.session_id, other.toolbox.run_id,
            data=b"foreign", operation_ids=[await _add_operation(db, other.toolbox.session_id, other.toolbox.run_id)],
        )
        with pytest.raises(EvidencePolicyError):
            await resolve_evidence(
                db, context.toolbox, [str(foreign)], use=EvidenceUse.LEAD
            )

        tampered = await _add_artifact(
            db, context.toolbox.artifacts, context.toolbox.session_id, context.toolbox.run_id,
            data=b"original", operation_ids=[await _add_operation(db, context.toolbox.session_id, context.toolbox.run_id)],
        )
        row = await db.get(ArtifactRow, tampered)
        assert row is not None
        row.sha256 = "0" * 64
        await db.flush()
        with pytest.raises(EvidencePolicyError):
            await resolve_evidence(
                db, context.toolbox, [str(tampered)], use=EvidenceUse.LEAD
            )


async def test_lead_expected_revision_conflict_is_not_masked(database, unique_name):
    context = await context_for(database, unique_name)
    context.toolbox.artifacts = ArtifactStore(database.settings)
    created = json.loads(
        await _call(
            record_lead, context,
            {"hypothesis": "初始假设", "next_action": "验证"},
            "lead-create",
        )
    )
    lead_id = created["lead"]["lead_id"]
    updated = json.loads(
        await _call(
            record_lead, context,
            {"lead_id": lead_id, "hypothesis": "已更新", "expected_revision": 1},
            "lead-update-1",
        )
    )
    assert updated["lead"]["revision"] == 2
    with pytest.raises(ApiError):
        await _call(
            record_lead, context,
            {"lead_id": lead_id, "hypothesis": "过期更新", "expected_revision": 1},
            "lead-update-2",
        )


async def test_export_execution_output_writes_export_and_origin_provenance(
    database, unique_name
):
    context = await context_for(database, unique_name)
    context.toolbox.artifacts = ArtifactStore(database.settings)
    data = b"uid=48(apache)\nLinux phineas\n"
    resource_id = uuid4()
    origin_operation_id = uuid4()
    captured_at = "2026-10-17T17:26:19.147381+00:00"

    async def fake_raw_output(resource, *, session_id, stream, offset, limit):
        return {
            "resource_id": str(resource),
            "stream": stream,
            "stored_offset": 0,
            "next_offset": len(data),
            "observed_size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "captured_at": captured_at,
            "data_base64": base64.b64encode(data).decode(),
        }

    cast(Any, context.toolbox).executor = SimpleNamespace(
        execution_raw_output=fake_raw_output
    )
    async with database.transaction() as db:
        db.add(
            ExecutionResourceRow(
                resource_id=resource_id,
                session_id=context.toolbox.session_id,
                executor_id="kali-01",
                kind="script",
                state="exited",
                origin_run_id=context.toolbox.run_id,
                origin_operation_id=origin_operation_id,
            )
        )
        await db.flush()

    result = json.loads(
        await _call(
            export_execution_output,
            context,
            {"job_id": str(resource_id), "limit": 3000},
            "export-provenance",
        )
    )
    artifact_id = UUID(result["data"]["artifact_id"])
    async with database.session() as db:
        artifact = await db.get(ArtifactRow, artifact_id)
        provenance = (
            await db.execute(
                select(ArtifactProvenanceRow).where(
                    ArtifactProvenanceRow.artifact_id == artifact_id
                )
            )
        ).scalar_one()
    assert artifact is not None
    assert artifact.export_tool_call_id == "export-provenance"
    assert artifact.producer_kind == "execution_output"
    assert artifact.source_trust == "ledger"
    assert artifact.resolution_status == "resolved"
    assert provenance.export_tool_call_id == "export-provenance"
    assert provenance.origin_operation_id == origin_operation_id
    assert provenance.resource_id == resource_id
    assert provenance.artifact_byte_start == 0
    assert provenance.artifact_byte_end == len(data)
    assert provenance.source_byte_start == 0
    assert provenance.source_byte_end == len(data)
    assert provenance.snapshot_hash == hashlib.sha256(data).hexdigest()
    assert provenance.captured_at.isoformat() == "2026-10-17T17:26:19.147381+00:00"


async def test_verify_finding_confirms_only_when_recipe_passes(database, unique_name):
    context = await context_for(database, unique_name)
    context.toolbox.artifacts = ArtifactStore(database.settings)
    async with database.transaction() as db:
        op1 = await _add_operation(db, context.toolbox.session_id, context.toolbox.run_id)
        op2 = await _add_operation(db, context.toolbox.session_id, context.toolbox.run_id)
        op3 = await _add_operation(db, context.toolbox.session_id, context.toolbox.run_id)
        initial = await _add_artifact(
            db, context.toolbox.artifacts, context.toolbox.session_id, context.toolbox.run_id,
            data=b"uid=48(apache)\nLinux phineas\n", operation_ids=[op1],
        )
        recheck = await _add_artifact(
            db, context.toolbox.artifacts, context.toolbox.session_id, context.toolbox.run_id,
            data=b"uid=48(apache)\nLinux phineas\n", operation_ids=[op2],
        )
        control = await _add_artifact(
            db, context.toolbox.artifacts, context.toolbox.session_id, context.toolbox.run_id,
            data=b"uid=0(root)\nLinux phineas\n", operation_ids=[op3],
        )
        finding = await FindingService(db).propose(
            session_id=context.toolbox.session_id,
            title="命令执行候选",
            description="两次独立操作出现预登记输出语义",
            evidence_refs=[str(initial), str(recheck)],
        )
        finding_id = finding.id

    result = json.loads(
        await _call(
            verify_finding,
            context,
            {
                "finding_id": str(finding_id),
                "recipe_id": "command_echo",
                "evidence_refs_json": [str(initial), str(recheck), str(control)],
                "checks_json": [
                    {"artifact_id": str(initial), "role": "initial", "must_match": [r"uid=48\(apache\)"]},
                    {"artifact_id": str(recheck), "role": "recheck", "must_match": [r"uid=48\(apache\)"]},
                    {"artifact_id": str(control), "role": "control", "must_not_match": [r"uid=48\(apache\)"]},
                ],
                "claim_scope_json": {"identity": "apache", "host": "phineas"},
            },
            "verify-command-echo",
        )
    )
    assert result["verified"] is True
    assert result["outcome"] == "pass"
    async with database.session() as db:
        finding = await db.get(FindingRow, finding_id)
        verification = (
            await db.execute(select(VerificationRow).where(VerificationRow.finding_id == finding_id))
        ).scalar_one()
    assert finding is not None and finding.status == "confirmed"
    assert verification.method_version == "command-echo/v2"


async def test_verify_finding_keeps_candidate_when_recipe_fails(database, unique_name):
    context = await context_for(database, unique_name)
    context.toolbox.artifacts = ArtifactStore(database.settings)
    async with database.transaction() as db:
        op1 = await _add_operation(db, context.toolbox.session_id, context.toolbox.run_id)
        op2 = await _add_operation(db, context.toolbox.session_id, context.toolbox.run_id)
        op3 = await _add_operation(db, context.toolbox.session_id, context.toolbox.run_id)
        first = await _add_artifact(
            db, context.toolbox.artifacts, context.toolbox.session_id, context.toolbox.run_id,
            data=b"not the expected output", operation_ids=[op1],
        )
        second = await _add_artifact(
            db, context.toolbox.artifacts, context.toolbox.session_id, context.toolbox.run_id,
            data=b"still not the expected output", operation_ids=[op2],
        )
        control = await _add_artifact(
            db, context.toolbox.artifacts, context.toolbox.session_id, context.toolbox.run_id,
            data=b"control without the marker", operation_ids=[op3],
        )
        finding = await FindingService(db).propose(
            session_id=context.toolbox.session_id,
            title="未成立的候选",
            description="判据不满足",
            evidence_refs=[str(first), str(second)],
        )
        finding_id = finding.id

    result = json.loads(
        await _call(
            verify_finding,
            context,
            {
                "finding_id": str(finding_id),
                "recipe_id": "command_echo",
                "evidence_refs_json": [str(first), str(second), str(control)],
                "checks_json": [
                    {"artifact_id": str(first), "role": "initial", "must_match": [r"uid=48\(apache\)"]},
                    {"artifact_id": str(second), "role": "recheck", "must_match": [r"uid=48\(apache\)"]},
                    {"artifact_id": str(control), "role": "control", "must_not_match": [r"uid=48\(apache\)"]},
                ],
                "claim_scope_json": {"identity": "apache", "host": "phineas"},
            },
            "verify-command-echo-fail",
        )
    )
    assert result["verified"] is False
    assert result["outcome"] == "fail"
    async with database.session() as db:
        finding = await db.get(FindingRow, finding_id)
    assert finding is not None and finding.status == "candidate"


