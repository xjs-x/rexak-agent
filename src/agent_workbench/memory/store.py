"""长期记忆：按项目与方法类别组织的 Store 文档。

与当前任务事实**分开**（§7.1 第 4 层）。

namespace 约定（§7.4）：
- `(owner_id, project_id, "project_facts")`
- `(owner_id, "methods")`

条目字段：memory_id、类型、内容、来源 artifact/run、适用条件、verified_status、
valid_from、last_verified_at、失效条件、版本、可选 expires_at。

硬约束：
- 模型提出的经验先为 `candidate`；有**可追溯验证支持**才提升为 `verified`；
- **禁止**未经验证的总结自动成为强约束；
- 冲突条目保留来源并等待复核，**不按"最后写入赢"覆盖事实**；
- 敏感凭据**不写入** Store 或摘要。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from langgraph.store.base import BaseStore

from agent_workbench.config.settings import Settings, get_settings

VerifiedStatus = Literal["candidate", "verified", "rejected", "superseded"]


def project_namespace(owner_id: str, project_id: UUID | str) -> tuple[str, ...]:
    return (owner_id, str(project_id), "project_facts")


def methods_namespace(owner_id: str) -> tuple[str, ...]:
    return (owner_id, "methods")


def memory_id_for(content: str, memory_type: str) -> str:
    """内容寻址的记忆 ID：同一内容重复写入不会产生两条互相冲突的条目。"""
    digest = hashlib.sha256(f"{memory_type}\x00{content}".encode()).hexdigest()
    return digest[:32]


def _entry_payload(
    *,
    memory_type: str,
    content: str,
    verified_status: VerifiedStatus,
    source_run_id: UUID | str | None,
    source_artifact_ids: list[str],
    applicable_conditions: str | None,
    invalid_conditions: str | None,
    expires_at: str | None,
    version: int,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "memory_id": memory_id_for(content, memory_type),
        "type": memory_type,
        "content": content,
        "verified_status": verified_status,
        "source_run_id": str(source_run_id) if source_run_id else None,
        "source_artifact_ids": list(source_artifact_ids),
        "applicable_conditions": applicable_conditions,
        "invalid_conditions": invalid_conditions,
        "valid_from": now,
        "last_verified_at": now if verified_status == "verified" else None,
        "expires_at": expires_at,
        "version": version,
        "updated_at": now,
    }


class MemoryStore:
    """LangGraph Store 的领域封装。

    Store 是**唯一可写**的跨任务记忆；本地不保留第二份可写副本（§4.4）。
    """

    def __init__(self, store: BaseStore, settings: Settings | None = None) -> None:
        self._store = store
        self._settings = settings or get_settings()

    @property
    def owner_id(self) -> str:
        return self._settings.owner_id

    async def propose(
        self,
        *,
        project_id: UUID | str,
        memory_type: str,
        content: str,
        source_run_id: UUID | str | None = None,
        source_artifact_ids: list[str] | None = None,
        applicable_conditions: str | None = None,
        invalid_conditions: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """写入一条**候选**经验。冲突时保留来源，不覆盖已有条目。"""
        namespace = project_namespace(self.owner_id, project_id)
        key = memory_id_for(content, memory_type)
        existing = await self._store.aget(namespace, key)

        if existing is not None:
            current = dict(existing.value)
            if current.get("verified_status") == "verified" and current.get("content") != content:
                # 冲突：保留双方来源，等待复核，不按"最后写入赢"覆盖
                merged_sources = sorted(
                    set(current.get("source_artifact_ids") or []) | set(source_artifact_ids or [])
                )
                conflict = {
                    "conflicts_with": current.get("memory_id"),
                    "existing_content": current.get("content"),
                    "existing_verified_status": current.get("verified_status"),
                    "note": "存在已验证条目且内容不同；保留来源等待复核，未覆盖原条目。",
                }
                alternate_key = f"{key}-alt-{uuid4().hex[:8]}"
                payload = _entry_payload(
                    memory_type=memory_type,
                    content=content,
                    verified_status="candidate",
                    source_run_id=source_run_id,
                    source_artifact_ids=merged_sources,
                    applicable_conditions=applicable_conditions,
                    invalid_conditions=invalid_conditions,
                    expires_at=expires_at,
                    version=1,
                )
                payload["conflict"] = conflict
                await self._store.aput(namespace, alternate_key, payload)
                return payload

            current.update(
                {
                    "source_artifact_ids": sorted(
                        set(current.get("source_artifact_ids") or [])
                        | set(source_artifact_ids or [])
                    ),
                    "applicable_conditions": applicable_conditions
                    or current.get("applicable_conditions"),
                    "invalid_conditions": invalid_conditions or current.get("invalid_conditions"),
                    "version": int(current.get("version", 1)) + 1,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            await self._store.aput(namespace, key, current)
            return current

        payload = _entry_payload(
            memory_type=memory_type,
            content=content,
            verified_status="candidate",
            source_run_id=source_run_id,
            source_artifact_ids=list(source_artifact_ids or []),
            applicable_conditions=applicable_conditions,
            invalid_conditions=invalid_conditions,
            expires_at=expires_at,
            version=1,
        )
        await self._store.aput(namespace, key, payload)
        return payload

    async def promote_to_verified(
        self,
        *,
        project_id: UUID | str,
        memory_type: str,
        content: str,
        verified_by_run_id: UUID | str,
        evidence_refs: list[str],
    ) -> dict[str, Any] | None:
        """把候选提升为已验证。

        **必须有可追溯的验证支持**（`evidence_refs` 非空）：没有证据就只能是 candidate。
        """
        if not evidence_refs:
            raise ValueError("提升为 verified 需要至少一条可追溯证据引用")
        namespace = project_namespace(self.owner_id, project_id)
        key = memory_id_for(content, memory_type)
        existing = await self._store.aget(namespace, key)
        payload = (
            dict(existing.value)
            if existing is not None
            else _entry_payload(
                memory_type=memory_type,
                content=content,
                verified_status="candidate",
                source_run_id=verified_by_run_id,
                source_artifact_ids=[],
                applicable_conditions=None,
                invalid_conditions=None,
                expires_at=None,
                version=1,
            )
        )
        payload.update(
            {
                "verified_status": "verified",
                "last_verified_at": datetime.now(UTC).isoformat(),
                "source_artifact_ids": sorted(
                    set(payload.get("source_artifact_ids") or []) | set(evidence_refs)
                ),
                "version": int(payload.get("version", 1)) + 1,
            }
        )
        await self._store.aput(namespace, key, payload)
        return payload

    async def search(
        self,
        *,
        project_id: UUID | str,
        query: str | None = None,
        limit: int = 10,
        include_candidates: bool = True,
    ) -> list[dict[str, Any]]:
        """按项目检索。**只检索当前项目命名空间**，不召回其他项目的记忆。"""
        namespace = project_namespace(self.owner_id, project_id)
        items = await self._store.asearch(namespace, query=query, limit=max(1, min(limit, 50)))
        results: list[dict[str, Any]] = []
        now = datetime.now(UTC)
        for item in items:
            payload = dict(item.value)
            status = payload.get("verified_status")
            if status != "verified" and not include_candidates:
                continue
            expires_at = payload.get("expires_at")
            if expires_at:
                try:
                    if datetime.fromisoformat(expires_at) < now:
                        payload["expired"] = True
                except ValueError:
                    payload["expired"] = None
            results.append(payload)
        return results

    async def list_methods(self, *, limit: int = 50) -> list[dict[str, Any]]:
        namespace = methods_namespace(self.owner_id)
        items = await self._store.asearch(namespace, limit=max(1, min(limit, 50)))
        return [dict(item.value) for item in items]


def summarize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "memory_id": entry.get("memory_id"),
        "type": entry.get("type"),
        "verified_status": entry.get("verified_status"),
        "content": entry.get("content"),
        "applicable_conditions": entry.get("applicable_conditions"),
        "invalid_conditions": entry.get("invalid_conditions"),
        "source_artifact_ids": entry.get("source_artifact_ids"),
        "last_verified_at": entry.get("last_verified_at"),
        "expires_at": entry.get("expires_at"),
        "expired": entry.get("expired"),
        "conflict": entry.get("conflict"),
    }


def dump(entry: dict[str, Any]) -> str:
    return json.dumps(summarize_entry(entry), ensure_ascii=False)
