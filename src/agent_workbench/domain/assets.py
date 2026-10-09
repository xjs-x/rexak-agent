"""资产与入口。

去重规则（§5.2）：
- 去重键包含 **method** 与 **身份集合**：不能盲目合并不同 HTTP 方法或忽略身份；
- 用于显示的归一化**不改变原始请求**；动态参数、重复参数及编码差异保存在原件里。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.domain.scope import parse_target
from agent_workbench.persistence.tables import AssetRow


def identity_key(identity_ids: Sequence[UUID | str]) -> str:
    """身份集合的稳定序列化键，用于唯一约束。"""
    return "|".join(sorted(str(item) for item in identity_ids))


async def record_asset(
    session: AsyncSession,
    *,
    session_id: UUID,
    url: str,
    method: str,
    identity_ids: Sequence[UUID | str] | None = None,
    source_artifact_id: UUID | None = None,
    notes: str | None = None,
) -> tuple[AssetRow, bool]:
    """登记一个入口。返回 `(row, created)`。

    `path` 使用**保留查询参数**的形态；重复参数与编码差异保留在原始工件里。
    """
    target = parse_target(url)
    key = identity_key(identity_ids or [])
    now = datetime.now(UTC)

    existing = (
        await session.execute(
            select(AssetRow).where(
                AssetRow.session_id == session_id,
                AssetRow.scheme == target.scheme,
                AssetRow.host == target.host,
                AssetRow.port == target.port,
                AssetRow.path == target.path,
                AssetRow.method == method.upper(),
                AssetRow.identity_key == key,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        existing.last_seen_at = now
        if source_artifact_id is not None:
            existing.source_artifact_id = existing.source_artifact_id or source_artifact_id
        await session.flush()
        return existing, False

    row = AssetRow(
        id=uuid4(),
        session_id=session_id,
        scheme=target.scheme,
        host=target.host,
        port=target.port,
        path=target.path,
        method=method.upper(),
        query_keys=sorted(_query_keys(target.path)),
        source_artifact_id=source_artifact_id,
        first_seen_at=now,
        last_seen_at=now,
        identity_refs=[str(item) for item in (identity_ids or [])],
        identity_key=key,
        notes=notes,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise
    return row, True


def _query_keys(path: str) -> list[str]:
    if "?" not in path:
        return []
    return [item.split("=", 1)[0] for item in path.split("?", 1)[1].split("&") if item]


async def list_assets(
    session: AsyncSession, session_id: UUID, *, limit: int = 500
) -> list[AssetRow]:
    return list(
        (
            await session.execute(
                select(AssetRow)
                .where(AssetRow.session_id == session_id)
                .order_by(AssetRow.last_seen_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


def asset_summary(row: AssetRow) -> dict[str, Any]:
    return {
        "asset_id": str(row.id),
        "endpoint": f"{row.method} {row.scheme}://{row.host}:{row.port}{row.path}",
        "query_keys": list(row.query_keys or []),
        "identity_refs": list(row.identity_refs or []),
        "first_seen_at": row.first_seen_at.isoformat(),
        "last_seen_at": row.last_seen_at.isoformat(),
    }
