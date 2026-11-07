"""Read Worker token usage from existing result artifacts, without a second ledger."""

import hashlib
from uuid import UUID

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.config.settings import Settings
from agent_workbench.contracts.delegation import WorkerResult, WorkerUsage
from agent_workbench.contracts.usage import AgentTokenSummary
from agent_workbench.evidence.artifacts import ArtifactError, ArtifactStore
from agent_workbench.persistence.tables import AgentDelegationRow, ArtifactRow


def add_worker_usage(summary: AgentTokenSummary, usage: WorkerUsage) -> None:
    if usage.usage_status == "mock":
        return
    summary.call_count += usage.calls
    total = usage.input_tokens
    read = usage.cache_read_tokens
    if usage.usage_status != "reported":
        total, read = None, None
    elif usage.input_token_basis == "uncached":
        # Legacy Pi input excluded both cache categories. Missing categories cannot
        # be reconstructed safely; leave total unknown instead of guessing zero.
        total = (total + read + usage.cache_write_tokens
                 if total is not None and read is not None
                 and usage.cache_write_tokens is not None else None)
    if total is None:
        summary.unknown_input_calls += usage.calls
    else:
        summary.input_tokens = (summary.input_tokens or 0) + total
    if read is None:
        summary.unknown_cache_calls += usage.calls
    else:
        summary.cache_read_tokens = (summary.cache_read_tokens or 0) + read


async def worker_token_summary(
    db: AsyncSession, settings: Settings, session_id: UUID, run_id: UUID | None,
) -> AgentTokenSummary:
    statement = select(AgentDelegationRow, ArtifactRow).outerjoin(
        ArtifactRow, ArtifactRow.id == AgentDelegationRow.result_artifact_id,
    ).where(AgentDelegationRow.parent_session_id == session_id)
    if run_id is not None:
        statement = statement.where(AgentDelegationRow.parent_run_id == run_id)
    summary = AgentTokenSummary()
    store = ArtifactStore(settings)
    for delegation, artifact in (await db.execute(statement)).all():
        if artifact is None or artifact.session_id != session_id or artifact.availability != "available":
            summary.unavailable_delegations += 1
            continue
        try:
            if artifact.byte_size > 2 * 1024 * 1024:
                raise ValueError("worker_result_size_limit")
            raw = store.read_bytes(artifact.relative_key, limit=artifact.byte_size + 1)
            if len(raw) != artifact.byte_size or hashlib.sha256(raw).hexdigest() != artifact.sha256:
                raise ValueError("worker_result_integrity_mismatch")
            result = WorkerResult.model_validate_json(raw)
            if (result.task_id != delegation.a2a_task_id
                or result.context_id != delegation.context_id):
                raise ValueError("worker_result_identity_mismatch")
        except (ArtifactError, OSError, ValidationError, ValueError) as exc:
            structlog.get_logger(__name__).warning("worker_usage_unavailable",
                delegation_id=str(delegation.id), error_type=type(exc).__name__)
            summary.unavailable_delegations += 1
            continue
        if result.model_usage is None:
            summary.unavailable_delegations += 1
        else:
            add_worker_usage(summary, result.model_usage)
    return summary
