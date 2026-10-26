"""Session 双图原子聚合：语义归模型，来源/版本/控制归程序。"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select

from agent_workbench.contracts.session_graph import (
    AddEdge,
    AddNode,
    AttackState,
    Attempt,
    Entity,
    GraphOperation,
    Relation,
)
from agent_workbench.evidence.redaction import redact_api_value
from agent_workbench.persistence.tables import (
    ArtifactRow,
    RunRow,
    SessionGraphReceiptRow,
    SessionGraphRow,
    SessionRow,
    ToolOperationRow,
)


def empty_graph() -> dict[str, Any]:
    return {name: {"nodes": [], "edges": []} for name in ("information_graph", "attack_graph")}


def apply_operations(
    document: dict[str, Any], operations: list[GraphOperation], request_id: str
) -> tuple[dict[str, Any], dict[str, str]]:
    result = copy.deepcopy(document or empty_graph())
    mapping: dict[str, str] = {}
    origins: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
    for operation in operations:
        if isinstance(operation, AddNode | AddEdge):
            if operation.temp_id in mapping:
                raise ValueError("重复 temp_id；请为本批新增对象使用不同临时 ID")
            mapping[operation.temp_id] = str(
                uuid5(NAMESPACE_URL, request_id + ":" + operation.temp_id)
            )
    for index, operation in enumerate(operations):
        node = operation.op.endswith("node")
        expected = (
            (Entity if node else Relation)
            if operation.graph == "information"
            else (AttackState if node else Attempt)
        )
        if not isinstance(operation.data, expected):
            raise ValueError(f"{operation.graph}/{operation.op} 的 data 类型不匹配")
        objects = result[operation.graph + "_graph"]["nodes" if node else "edges"]
        adding = operation.op.startswith("add")
        key = (
            mapping[operation.temp_id] if isinstance(operation, AddNode | AddEdge) else operation.id
        )
        existing = next((item for item in objects if item["id"] == key), None)
        if (adding and existing) or (not adding and existing is None):
            raise ValueError("对象不存在或 ID 已使用；请 get_graph 后按当前 ID 更新")
        value = operation.data.model_dump()
        for field in ("from_id", "to_id"):
            if field in value:
                value[field] = mapping.get(value[field], value[field])
        if "entity_refs" in value:
            value["entity_refs"] = [mapping.get(x, x) for x in value["entity_refs"]]
        # 真实读取、写入都脱敏；不复制原始证据。
        value = {"id": key, **redact_api_value(value)}
        origins[(operation.graph + "_graph", key)] = (index, operation.data.model_dump())
        if existing is not None:
            objects.remove(existing)
        objects.append(value)
    entities = {x["id"] for x in result["information_graph"]["nodes"]}
    errors = []
    for name, graph in result.items():
        nodes = {x["id"] for x in graph["nodes"]}
        for edge in graph["edges"]:
            for field in ("from_id", "to_id"):
                if edge[field] in nodes:
                    continue
                index, original = origins.get((name, edge["id"]), (-1, edge))
                other = "information_graph" if name == "attack_graph" else "attack_graph"
                actual = (
                    (
                        "InformationGraph Entity"
                        if other == "information_graph"
                        else "AttackGraph AttackState"
                    )
                    if any(x["id"] == edge[field] for x in result[other]["nodes"])
                    else "不存在的节点"
                )
                expected = (
                    "AttackGraph AttackState"
                    if name == "attack_graph"
                    else "InformationGraph Entity"
                )
                errors.append(
                    f"operations[{index}].data.{field}={original[field]}：引用属于{actual}，要求{expected}；边引用不存在的同图节点。Entity通过State.entity_refs关联，不能作为Attempt端点。"
                )
        if name == "attack_graph":
            for state in graph["nodes"]:
                if not set(state["entity_refs"]) <= entities:
                    raise ValueError("攻击状态 entity_refs 必须引用当前信息图实体")
        for objects in graph.values():
            objects.sort(key=lambda x: x["id"])
    if errors:
        raise ValueError("\n".join(errors[:4]))
    return result, mapping


async def lock_graph(db: Any, session_id: UUID, interval: int = 5) -> SessionGraphRow:
    session = (
        await db.execute(select(SessionRow).where(SessionRow.id == session_id).with_for_update())
    ).scalar_one_or_none()
    if session is None:
        raise ValueError("Session 不存在")
    row = await db.get(SessionGraphRow, session_id)
    if row is None:
        row = SessionGraphRow(
            session_id=session_id,
            document=empty_graph(),
            revision=0,
            reviewed_seq=0,
            completed_rounds=0,
            reviewed_rounds=0,
            next_sync_round=interval,
            missed_syncs=0,
        )
        db.add(row)
        await db.flush()
    return row


async def check_writer(db: Any, toolbox: Any) -> None:
    run = (
        await db.execute(select(RunRow).where(RunRow.id == toolbox.run_id).with_for_update())
    ).scalar_one_or_none()
    if (
        run is None
        or run.session_id != toolbox.session_id
        or run.status != "running"
        or run.owner != toolbox.owner
        or run.owner_generation != toolbox.owner_generation
        or run.control_version != toolbox.control_version
        or run.lease_until is None
        or run.lease_until <= datetime.now(UTC)
    ):
        raise ValueError("运行已暂停、取消、控制版本或租约已变化；图未修改")


async def validate_sources(
    db: Any, session_id: UUID, operations: list[GraphOperation], artifact_store: Any = None
) -> None:
    for operation in operations:
        for ref in getattr(operation.data, "source_refs", []):
            kind, separator, raw = ref.partition(":")
            if not separator or kind not in {"artifact", "operation"}:
                raise ValueError("来源须为 artifact:<UUID> 或 operation:<UUID>")
            try:
                key = UUID(raw)
            except ValueError as exc:
                raise ValueError("来源 UUID 无效") from exc
            row = await db.get(ArtifactRow if kind == "artifact" else ToolOperationRow, key)
            if row is None or row.session_id != session_id:
                raise ValueError("来源不存在或不属于当前 Session；请使用真实执行回执中的引用")
            if kind == "artifact":
                if artifact_store is None or not artifact_store.exists(row.relative_key):
                    raise ValueError("工件文件不可读取；元数据存在不代表工件可用")
                if row.source_operation_id:
                    origin = await db.get(ToolOperationRow, row.source_operation_id)
                    if origin is None or origin.session_id != session_id:
                        raise ValueError("工件原始操作不属于当前 Session")
            if kind == "artifact" and (
                row.availability != "available" or row.sensitivity == "secret"
            ):
                raise ValueError("工件不可读取或包含秘密；请引用操作 ID 和脱敏结论")


async def read_graph(db: Any, session_id: UUID) -> dict[str, Any]:
    row = await db.get(SessionGraphRow, session_id)
    return redact_api_value(
        {"revision": row.revision if row else 0, **(row.document if row else empty_graph())}
    )


async def update_graph(
    db: Any,
    toolbox: Any,
    request_id: str,
    expected_revision: int,
    operations: list[GraphOperation],
    snapshot: dict[str, Any] | None = None,
    page_id: str | None = None,
) -> dict[str, Any]:
    row = await lock_graph(db, toolbox.session_id, toolbox.settings.graph_sync_interval)
    await check_writer(db, toolbox)
    payload = [x.model_dump() for x in operations]
    if page_id is None and snapshot:
        page_id = snapshot.get("page_id")
    digest = hashlib.sha256(
        json.dumps(
            [expected_revision, payload, page_id] if page_id else [expected_revision, payload],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    identity = {"session_id": toolbox.session_id, "request_id": request_id}
    previous = await db.get(SessionGraphReceiptRow, identity)
    if previous:
        if previous.digest != digest:
            raise ValueError("重放参数与原调用不同；请读取当前图后发起新调用")
        return previous.receipt["result"]
    if expected_revision != row.revision:
        raise ValueError(
            f"图版本冲突：expected={expected_revision}, current={row.revision}；请 get_graph"
        )
    await validate_sources(db, toolbox.session_id, operations, getattr(toolbox, "artifacts", None))
    if page_id is not None:
        page = await db.get(
            SessionGraphReceiptRow, {"session_id": toolbox.session_id, "request_id": page_id}
        )
        if page is None or not page.receipt.get("presented"):
            raise ValueError("page_id不存在、未展示或不属于当前Session；游标未推进")
        if page.receipt["revision"] != row.revision:
            raise ValueError("页面版本冲突；请读取最新同步页")
        if page.receipt["from_seq"] != row.reviewed_seq:
            raise ValueError("页面起点不连续或已确认；不允许越页确认")
        snapshot = page.receipt
    else:
        snapshot = None
    before = copy.deepcopy(row.document)
    document, mapping = apply_operations(before, operations, str(toolbox.session_id) + request_id)
    row.document = document
    row.revision += 1
    if snapshot and snapshot.get("complete"):
        row.missed_syncs = max(0, row.missed_syncs - 1)
        row.reviewed_seq = max(row.reviewed_seq, int(snapshot["through_seq"]))
        row.reviewed_rounds = max(row.reviewed_rounds, int(snapshot["rounds"]))
        row.next_sync_round = row.completed_rounds + toolbox.settings.graph_sync_interval
    result = {
        "revision": row.revision,
        "id_mapping": mapping,
        "reviewed_seq": row.reviewed_seq,
        "semantic_verification": "model_judgment",
    }
    db.add(
        SessionGraphReceiptRow(
            **identity,
            digest=digest,
            receipt={
                "result": result,
                "before": before,
                "after": document,
                "reviewed_seq": row.reviewed_seq,
            },
        )
    )
    return result
