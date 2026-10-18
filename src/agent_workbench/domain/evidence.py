"""证据来源解析：把工件、provenance 与业务用途分开判断。

规则（P0）：
- 工件存在与哈希匹配只证明**完整性**，不证明来源或漏洞成立；
- 一个工件可以对应多个产生操作区间，不能按“最近一次操作”归属；
- `ledger` 来源可支持事实/验证；`derived` 只能作为候选/假设；`legacy_unresolved`
  保留可读性，但不能用于确认；
- 验证用途要求每个工件都有 ledger 来源、resolved 状态，且至少两个独立操作。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.persistence.tables import ArtifactProvenanceRow, ArtifactRow
from agent_workbench.tools.runtime import Toolbox


class EvidenceUse(StrEnum):
    OBSERVATION = "observation"
    WEAKNESS = "weakness"
    LEAD = "lead"
    VERIFICATION = "verification"
    REPORT = "report"


class EvidencePolicyError(ValueError):
    """证据不满足当前业务入口的来源/独立性要求。"""


@dataclass(frozen=True, slots=True)
class ArtifactEvidence:
    artifact_id: UUID
    provenance_ids: list[UUID]
    origin_operation_ids: list[UUID]
    trust: str
    resolution: str
    producer_kind: str
    legacy: bool


@dataclass(frozen=True, slots=True)
class EvidenceResolution:
    refs: list[str]
    artifacts: list[ArtifactEvidence]
    trust: str
    resolution: str
    legacy: bool
    independent_operations: int

    @property
    def origin_operation_ids(self) -> list[UUID]:
        seen: dict[UUID, None] = {}
        for artifact in self.artifacts:
            for operation_id in artifact.origin_operation_ids:
                seen.setdefault(operation_id, None)
        return list(seen)


def _worse_trust(left: str, right: str) -> str:
    order = {"ledger": 0, "derived": 1, "imported": 2, "unresolved": 3}
    return left if order.get(left, 3) >= order.get(right, 3) else right


def _worse_resolution(left: str, right: str) -> str:
    order = {"resolved": 0, "legacy_unresolved": 1, "ambiguous": 2}
    return left if order.get(left, 2) >= order.get(right, 2) else right


async def resolve_evidence(
    db: AsyncSession,
    toolbox: Toolbox,
    raw_refs: list[str],
    *,
    use: EvidenceUse,
) -> EvidenceResolution:
    """解析并校验一组证据引用。

    调用方必须先完成“是否允许空证据”的业务判断；这里只处理非空引用的来源。
    """
    artifacts: list[ArtifactEvidence] = []
    seen: set[UUID] = set()
    for raw in raw_refs:
        candidate = str(raw).strip()
        if not candidate:
            continue
        try:
            artifact_id = UUID(candidate)
        except ValueError as exc:
            raise EvidencePolicyError(f"证据引用不是工件 ID：{candidate}") from exc
        if artifact_id in seen:
            continue
        seen.add(artifact_id)

        artifact = await db.get(ArtifactRow, artifact_id)
        if (
            artifact is None
            or artifact.session_id != toolbox.session_id
            or artifact.availability != "available"
        ):
            raise EvidencePolicyError("证据必须是当前会话中可用的工件")
        if not toolbox.artifacts.verify(artifact.relative_key, artifact.sha256):
            raise EvidencePolicyError(f"工件文件哈希不匹配：{artifact_id}")

        provenance = list(
            (
                await db.execute(
                    select(ArtifactProvenanceRow)
                    .where(ArtifactProvenanceRow.artifact_id == artifact_id)
                    .order_by(
                        ArtifactProvenanceRow.provenance_revision,
                        ArtifactProvenanceRow.segment_no,
                    )
                )
            )
            .scalars()
            .all()
        )
        if provenance:
            trust = "ledger"
            resolution = "resolved"
            producer_kind = "execution_output"
            origin_ids: list[UUID] = []
            for row in provenance:
                trust = _worse_trust(trust, row.source_trust)
                resolution = _worse_resolution(resolution, row.resolution_status)
                producer_kind = row.producer_kind
                if row.origin_operation_id is not None:
                    origin_ids.append(row.origin_operation_id)
        else:
            # 旧工件兼容：只有 Executor 操作直接产生的工件才可视为 ledger；
            # 其余没有 provenance 的历史件不能补造操作，只能标 legacy。
            if artifact.source_operation_id is not None:
                trust = "ledger"
                resolution = "resolved"
                producer_kind = "execution_output"
                origin_ids = [artifact.source_operation_id]
            else:
                trust = "unresolved"
                resolution = "legacy_unresolved"
                producer_kind = artifact.producer_kind or "unknown"
                origin_ids = []

        legacy = resolution != "resolved" or trust == "unresolved"
        artifacts.append(
            ArtifactEvidence(
                artifact_id=artifact_id,
                provenance_ids=[row.provenance_id for row in provenance],
                origin_operation_ids=list(dict.fromkeys(origin_ids)),
                trust=trust,
                resolution=resolution,
                producer_kind=producer_kind,
                legacy=legacy,
            )
        )

    if not artifacts:
        return EvidenceResolution(
            refs=[],
            artifacts=[],
            trust="unresolved",
            resolution="legacy_unresolved",
            legacy=True,
            independent_operations=0,
        )

    trust = "ledger"
    resolution = "resolved"
    for artifact in artifacts:
        trust = _worse_trust(trust, artifact.trust)
        resolution = _worse_resolution(resolution, artifact.resolution)
    independent = len(
        {operation_id for artifact in artifacts for operation_id in artifact.origin_operation_ids}
    )
    result = EvidenceResolution(
        refs=[str(artifact.artifact_id) for artifact in artifacts],
        artifacts=artifacts,
        trust=trust,
        resolution=resolution,
        legacy=resolution != "resolved" or trust == "unresolved",
        independent_operations=independent,
    )
    _enforce_use(result, use)
    return result


def _enforce_use(result: EvidenceResolution, use: EvidenceUse) -> None:
    if use is EvidenceUse.VERIFICATION:
        if result.trust != "ledger" or result.resolution != "resolved":
            raise EvidencePolicyError("验证证据必须全部来自已解析的 ledger 来源")
        if result.independent_operations < 2:
            raise EvidencePolicyError("验证证据至少需要两个独立操作")
    elif use is EvidenceUse.REPORT:
        if result.trust not in {"ledger", "derived"}:
            raise EvidencePolicyError("报告不得引用未解析来源")
    # observation / weakness / lead 允许 legacy/derived，但调用方必须保留 flag，
    # 不得据此把发现提升为 confirmed。


__all__ = [
    "ArtifactEvidence",
    "EvidencePolicyError",
    "EvidenceResolution",
    "EvidenceUse",
    "resolve_evidence",
]
