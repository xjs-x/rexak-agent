"""资产与入口。

去重规则（§5.2）：
- 去重键包含 **method** 与 **身份集合**：不能盲目合并不同 HTTP 方法或忽略身份；
- 用于显示的归一化**不改变原始请求**；动态参数、重复参数及编码差异保存在原件里。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
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

    **原子 upsert**（§4.1）：使用 PostgreSQL `INSERT ... ON CONFLICT DO UPDATE`，
    继续以 `uq_assets_endpoint_identity` 为去重键（仍包含 method 与身份集合）。

    行为约束：
    - 冲突时**只更新** `last_seen_at`（取较大值）与缺失的 `source_artifact_id`；
      不覆盖已有 `first_seen_at`、来源、备注与其他领域内容；
    - **不自行 rollback 外层事务**：业务函数不是事务拥有者，失败由事务拥有者处理；
      因此并行工具同时登记同一入口时，一个失败不会撤销另一个已提交的观察；
    - `created` 由"本次预生成 UUID 与返回 UUID 是否相等"判定，不依赖未约定的内部字段。

    `path` 使用**保留查询参数**的形态；重复参数与编码差异保留在原始工件里。
    """
    target = parse_target(url)
    key = identity_key(identity_ids or [])
    now = datetime.now(UTC)
    new_id = uuid4()
    table = cast(Any, AssetRow.__table__)
    stmt = pg_insert(table).values(
        id=new_id,
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
    stmt = stmt.on_conflict_do_update(
        constraint="uq_assets_endpoint_identity",
        set_={
            "last_seen_at": func.greatest(AssetRow.last_seen_at, stmt.excluded.last_seen_at),
            "source_artifact_id": func.coalesce(
                AssetRow.source_artifact_id, stmt.excluded.source_artifact_id
            ),
        },
    ).returning(table.c.id)
    returned_id = (await session.execute(stmt)).scalar_one()
    # 刷新 identity map：冲突分支可能命中已加载对象，必须拿到数据库中的真实值。
    row = await session.get(AssetRow, returned_id, populate_existing=True)
    if row is None:  # pragma: no cover - upsert 后必然可见
        raise RuntimeError("资产 upsert 后无法读回该行")
    return row, str(returned_id) == str(new_id)


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
