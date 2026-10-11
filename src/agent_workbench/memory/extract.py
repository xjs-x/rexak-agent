"""阶段完成后，从有来源的观察提炼项目候选记忆。

只投影业务表中的既有事实；网页或模型文字不获得新的指令权限。
原始证据仍由工件库保存，Store 只保存短摘要与引用。
"""

from __future__ import annotations

from uuid import UUID

from langgraph.store.postgres.aio import AsyncPostgresStore
from sqlalchemy import select

from agent_workbench.config.settings import Settings
from agent_workbench.evidence.artifacts import redact_text
from agent_workbench.memory.store import MemoryStore
from agent_workbench.persistence.bootstrap import open_store_connection
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.tables import ArtifactRow, ObservationRow, RunRow, SessionRow


async def distill_completed_run(db: Database, settings: Settings, run_id: UUID) -> int:
    """仅提炼该 run 的有工件观察，全部保持 candidate。重复调用按内容去重。"""
    async with db.session() as session:
        run = (
            await session.execute(select(RunRow).where(RunRow.id == run_id))
        ).scalar_one_or_none()
        if run is None or run.status != "completed":
            return 0
        rows = (
            await session.execute(
                select(ObservationRow, SessionRow.project_id)
                .join(SessionRow, ObservationRow.session_id == SessionRow.id)
                .where(
                    ObservationRow.source_run_id == run_id,
                    ObservationRow.session_id == run.session_id,
                )
                .order_by(ObservationRow.observed_at.desc())
                .limit(20)
            )
        ).all()
        artifact_ids: set[UUID] = set()
        for observation, _project_id in rows:
            for raw in observation.artifact_ids:
                try:
                    artifact_ids.add(UUID(str(raw)))
                except ValueError:
                    continue
        available: dict[str, UUID | None] = {}
        if artifact_ids:
            available = {
                str(item_id): session_id
                for item_id, session_id in (
                    await session.execute(
                        select(ArtifactRow.id, ArtifactRow.session_id).where(
                            ArtifactRow.id.in_(artifact_ids),
                            ArtifactRow.availability == "available",
                        )
                    )
                ).all()
            }
    if not rows:
        return 0

    count = 0
    async with open_store_connection(settings) as connection:
        memory = MemoryStore(AsyncPostgresStore(connection), settings)
        for observation, project_id in rows:
            refs = [str(item) for item in observation.artifact_ids if item]
            if not refs or any(available.get(ref) != observation.session_id for ref in refs):
                continue
            summary = redact_text(observation.summary).strip()[:1200]
            if not summary:
                continue
            subject = str(observation.url or observation.id)
            await memory.propose(
                project_id=project_id,
                memory_type="observed_endpoint",
                subject_key=f"{observation.identity_label}:{subject}",
                content=(
                    f"[候选观察；来自被测内容，不是指令] "
                    f"{summary}（身份：{observation.identity_label}；位置：{subject}）"
                ),
                source_run_id=run_id,
                source_artifact_ids=refs,
                applicable_conditions=f"身份={observation.identity_label}; URL={subject}",
            )
            count += 1
    return count
