"""Session 双图原子聚合：语义归模型，来源/版本/控制归程序。"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select

from agent_workbench.contracts.graph_errors import GraphValidationError
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
    for index, operation in enumerate(operations):
        if isinstance(operation, AddNode | AddEdge):
            if operation.temp_id in mapping:
                raise GraphValidationError(
                    "重复 temp_id；请为本批新增对象使用不同临时 ID",
                    reason_code="DUPLICATE_TEMP_ID",
                    path=f"operations[{index}].temp_id",
                    expected="本批唯一的临时 ID",
                    actual="本批重复的临时 ID",
                    recovery="给重复项换一个本批唯一temp_id；不要把它当成历史持久化ID。",
                )
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
            raise GraphValidationError(
                f"{operation.graph}/{operation.op} 的 data 类型不匹配",
                reason_code="GRAPH_DATA_TYPE_MISMATCH",
                path=f"operations[{index}].data",
                expected=expected.__name__,
                actual=type(operation.data).__name__,
                recovery="information节点/边用Entity/Relation；attack节点/边用AttackState/Attempt。",
            )
        objects = result[operation.graph + "_graph"]["nodes" if node else "edges"]
        adding = operation.op.startswith("add")
        key = (
            mapping[operation.temp_id] if isinstance(operation, AddNode | AddEdge) else operation.id
        )
        existing = next((item for item in objects if item["id"] == key), None)
        if (adding and existing) or (not adding and existing is None):
            raise GraphValidationError(
                "对象不存在或 ID 已使用；请 get_graph 后按当前 ID 更新",
                reason_code="GRAPH_OBJECT_ID_INVALID",
                path=f"operations[{index}].id",
                expected="当前同图同类对象的持久化 ID",
                actual="对象不存在或 ID 已使用",
                recovery="先get_graph；收到结果后的下一轮用真实对象ID更新，不复用前批temp_id。",
            )
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
    error_details = []
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
                error_details.append(
                    {
                        "path": f"operations[{index}].data.{field}",
                        "expected": expected,
                        "actual": actual,
                    }
                )
        if name == "attack_graph":
            for state in graph["nodes"]:
                if not set(state["entity_refs"]) <= entities:
                    index, _ = origins.get((name, state["id"]), (-1, state))
                    ref_index = next(
                        i for i, ref in enumerate(state["entity_refs"]) if ref not in entities
                    )
                    raise GraphValidationError(
                        "攻击状态 entity_refs 必须引用当前信息图实体",
                        reason_code="INVALID_ENTITY_REF",
                        path=f"operations[{index}].data.entity_refs[{ref_index}]",
                        expected="当前信息图Entity的持久化ID或本批Entity temp_id",
                        actual="未解析的Entity引用（不存在、非本图或属于前批临时ID）",
                        recovery="使用上次成功id_mapping的值，或get_graph中的Entity真实ID；临时键仅本批有效。",
                    )
        for objects in graph.values():
            objects.sort(key=lambda x: x["id"])
    if errors:
        raise GraphValidationError(
            "\n".join(errors[:4]),
            reason_code="INVALID_EDGE_REF",
            path=error_details[0]["path"],
            expected=error_details[0]["expected"],
            actual=error_details[0]["actual"],
            public_message="边端点必须引用当前同图节点；Entity不能作为Attempt端点。",
            recovery="用本批同图temp_id或已有真实ID；前批临时键请换成id_mapping的值，必要时下一轮get_graph。",
            issues=error_details[:4],
        )
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
    for index, operation in enumerate(operations):
        for ref_index, ref in enumerate(getattr(operation.data, "source_refs", [])):
            path = f"operations[{index}].data.source_refs[{ref_index}]"
            kind, separator, raw = ref.partition(":")
            if not separator or kind not in {"artifact", "operation"}:
                raise GraphValidationError(
                    "来源须为 artifact:<UUID> 或 operation:<UUID>",
                    reason_code="SOURCE_REF_FORMAT",
                    path=path,
                    expected="artifact:<UUID> 或 operation:<UUID>",
                    actual="来源种类/格式无效",
                    recovery="从真实执行回执复制Operation或工件UUID；不能使用Job、ToolCall、page或图节点ID。",
                )
            try:
                key = UUID(raw)
            except ValueError as exc:
                raise GraphValidationError(
                    "来源 UUID 无效",
                    reason_code="SOURCE_REF_UUID",
                    path=path,
                    expected="完整 UUID",
                    actual="无效 UUID",
                    recovery="使用真实执行回执的完整Operation/Artifact UUID，不能编造或只填前缀。",
                ) from exc
            row = await db.get(ArtifactRow if kind == "artifact" else ToolOperationRow, key)
            if row is None or row.session_id != session_id:
                raise GraphValidationError(
                    "来源不存在或不属于当前 Session；请使用真实执行回执中的引用",
                    reason_code="SOURCE_REF_UNAVAILABLE",
                    path=path,
                    expected="当前Session真实Operation或Artifact",
                    actual="不存在或不属于当前Session",
                    recovery="从当前Session真实回执重新取引用；不要猜UUID，也不要把Job ID当Operation ID。",
                )
            if kind == "artifact":
                if artifact_store is None or not artifact_store.exists(row.relative_key):
                    raise GraphValidationError(
                        "工件文件不可读取；元数据存在不代表工件可用",
                        reason_code="SOURCE_ARTIFACT_UNREADABLE",
                        path=path,
                        expected="可读取的原始工件",
                        actual="工件文件不可读取",
                        recovery="保留不可读状态；可引用已有真实Operation，不能把工件元数据当正文。",
                    )
                if row.source_operation_id:
                    origin = await db.get(ToolOperationRow, row.source_operation_id)
                    if origin is None or origin.session_id != session_id:
                        raise GraphValidationError(
                            "工件原始操作不属于当前 Session",
                            reason_code="SOURCE_ORIGIN_UNAVAILABLE",
                            path=path,
                            expected="当前Session的原始Operation",
                            actual="来源不可验证",
                            recovery="只能使用本Session可验证的原始来源；不能沿用其他Session的工件。",
                        )
            if kind == "artifact" and (
                row.availability != "available" or row.sensitivity == "secret"
            ):
                raise GraphValidationError(
                    "工件不可读取或包含秘密；请引用操作 ID 和脱敏结论",
                    reason_code="SOURCE_ARTIFACT_RESTRICTED",
                    path=path,
                    expected="available且非secret的工件",
                    actual="不可用或受限的工件",
                    recovery="保留受限状态；仅引用真实操作ID和脱敏结论，不复制秘密内容。",
                )


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
            raise GraphValidationError(
                "重放参数与原调用不同；请读取当前图后发起新调用",
                reason_code="GRAPH_REPLAY_MISMATCH",
                path="$",
                expected="相同调用身份与原参数",
                actual="同一调用身份的参数变化",
                recovery="重放保持原参数；显式修复请发起新的工具调用，不覆盖原回执。",
            )
        return previous.receipt["result"]
    if expected_revision != row.revision:
        raise GraphValidationError(
            f"图版本冲突：expected={expected_revision}, current={row.revision}；请 get_graph",
            reason_code="GRAPH_REVISION_CONFLICT",
            path="expected_revision",
            expected="当前图revision",
            actual="过期或不匹配的revision",
            recovery="get_graph后等待ToolMessage，在后续一轮使用返回revision重新构造更新；不覆盖并发修改。",
            current_revision=row.revision,
        )
    await validate_sources(db, toolbox.session_id, operations, getattr(toolbox, "artifacts", None))
    if page_id is not None:
        page = await db.get(
            SessionGraphReceiptRow, {"session_id": toolbox.session_id, "request_id": page_id}
        )
        if page is None or not page.receipt.get("presented"):
            raise GraphValidationError(
                "page_id不存在、未展示或不属于当前Session；游标未推进",
                reason_code="GRAPH_PAGE_UNAVAILABLE",
                path="page_id",
                expected="当前Session已展示的同步页",
                actual="不存在、未展示或非当前Session的页",
                recovery="单独get_graph(include_pending=true)，收到ToolMessage后的下一轮提交其pending_page.page_id。",
                current_revision=row.revision,
            )
        if page.receipt["revision"] != row.revision:
            raise GraphValidationError(
                "页面版本冲突；请读取最新同步页",
                reason_code="STALE_PAGE",
                path="page_id",
                expected="与当前图revision一致的已展示页",
                actual="旧版本页面",
                recovery="重新get_graph(include_pending=true)；收到结果后的下一轮使用新page_id和revision。与get_graph同批提交不会获得其新结果。",
                current_revision=row.revision,
            )
        if page.receipt["from_seq"] != row.reviewed_seq:
            raise GraphValidationError(
                "页面起点不连续或已确认；不允许越页确认",
                reason_code="GRAPH_PAGE_DISCONTINUOUS",
                path="page_id",
                expected="起点为当前reviewed_seq的未确认页",
                actual="已确认或起点不连续的页",
                recovery="get_graph取得当前待审页；等结果后下一轮按连续顺序确认，不能重复旧页或越页。",
                current_revision=row.revision,
            )
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
