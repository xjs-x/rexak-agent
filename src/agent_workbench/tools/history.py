"""会话内关键词/中文子串检索、弱点与关系的**按 ID**读取，稳定分页与来源展开。

V3（方案 §4.4）：
- 新增 `kind="weakness"` 与 `kind="relation"`；
- 新增按 ID 读取详情的路径（不能只依赖关键词命中）；
- 首版查询顺序：明确 ID/条件关系 → 同资产/身份 → 受影响的前置条件 → 关键词兜底；
- 模型只拿简短结果和来源引用，需要原始证据时继续使用 `read_artifact`；
- **不**引入图数据库或向量检索依赖。
"""

from __future__ import annotations

import base64
import json
from typing import Any
from uuid import UUID

from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.context_sources import visible_message_record
from agent_workbench.evidence.redaction import redact_api_value
from agent_workbench.persistence.tables import (
    ArtifactRow,
    FindingRow,
    LeadRow,
    LeadWeaknessLinkRow,
    ObservationRow,
    ReportVersionRow,
    ToolOperationRow,
)

#: 支持检索的类型。`weakness` / `relation` 是 V3 新增。
HISTORY_KINDS = (
    "lead",
    "weakness",
    "relation",
    "observation",
    "operation",
    "report",
    "conversation",
    "plan",
    "message",
)
#: 单个弱点最多返回的条件条目（长正文落工件）。
MAX_CONDITIONS_IN_RESULT = 10


def _cursor(value: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, sort_keys=True).encode()).decode()


def _weakness_record(row: FindingRow) -> dict[str, Any]:
    return {
        "type": "weakness",
        "id": str(row.id),
        "version": row.revision,
        "weakness_kind": row.weakness_kind,
        "status": row.status,
        "title": row.title,
        "description": (row.description or "")[:600],
        "impact": (row.impact or "")[:300] or None,
        "category": row.category,
        "applicability": redact_api_value(row.applicability or {}),
        "provided_conditions": [
            {
                "key": item.get("key"),
                "statement": item.get("statement"),
                "evidence_refs": list(item.get("evidence_refs") or []),
                "limitations": item.get("limitations"),
            }
            for item in (row.provided_conditions or [])[:MAX_CONDITIONS_IN_RESULT]
        ],
        "connection_review": redact_api_value(row.connection_review or {}),
        "source": {
            "evidence_refs": list(row.evidence_refs or []),
            "verification_id": str(row.verification_id) if row.verification_id else None,
            "asset_id": str(row.asset_id) if row.asset_id else None,
            "identity_id": str(row.identity_id) if row.identity_id else None,
        },
    }


def _relation_record(row: LeadWeaknessLinkRow) -> dict[str, Any]:
    return {
        "type": "relation",
        "id": str(row.id),
        "version": row.revision,
        "role": row.role,
        "relation_state": row.relation_state,
        "lead_id": str(row.lead_id),
        "weakness_id": str(row.finding_id),
        "condition_key": row.condition_key,
        "requirement_key": row.requirement_key,
        "source_weakness_revision": row.source_weakness_revision,
        "source": {
            "evidence_refs": list(row.evidence_refs or []),
            "source_operation_id": str(row.source_operation_id)
            if row.source_operation_id
            else None,
        },
    }


async def read_weakness_detail(
    toolbox: Any, *, weakness_id: str | None = None, lead_id: str | None = None
) -> dict[str, Any]:
    """按 **ID** 读取弱点或线索相关的详情与关系邻居。

    这条路径不依赖关键词命中：模型已经拿到 ID 时应该能直接读到内容。
    """
    if not weakness_id and not lead_id:
        raise ValueError("按 ID 读取需要提供 weakness_id 或 lead_id")
    scope: dict[str, Any] = {"weakness_id": weakness_id, "lead_id": lead_id}
    async with toolbox.db.session() as db:
        if weakness_id:
            try:
                target = UUID(str(weakness_id))
            except ValueError as exc:
                raise ValueError(f"weakness_id 不是合法 UUID：{weakness_id}") from exc
            row = (
                await db.execute(
                    select(FindingRow).where(
                        FindingRow.id == target, FindingRow.session_id == toolbox.session_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ValueError("该弱点不在当前会话")
            links = list(
                (
                    await db.execute(
                        select(LeadWeaknessLinkRow).where(
                            LeadWeaknessLinkRow.session_id == toolbox.session_id,
                            LeadWeaknessLinkRow.finding_id == target,
                        )
                    )
                )
                .scalars()
                .all()
            )
            lead_ids = [link.lead_id for link in links]
            leads = (
                list(
                    (
                        await db.execute(
                            select(LeadRow).where(
                                LeadRow.session_id == toolbox.session_id,
                                LeadRow.id.in_(lead_ids),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if lead_ids
                else []
            )
            return {
                "weakness": redact_api_value(_weakness_record(row)),
                "relations": [redact_api_value(_relation_record(link)) for link in links],
                "related_leads": [
                    {
                        "lead_id": str(item.id),
                        "hypothesis": item.hypothesis[:200],
                        "status": item.status,
                        "revision": item.revision,
                    }
                    for item in leads
                ],
                "retrieval": "explicit_id",
                "scope": scope,
                "note": "关系只表示有引用依据；组合假设不因两侧分别成立而自动成立。",
            }

        try:
            target_lead = UUID(str(lead_id))
        except ValueError as exc:
            raise ValueError(f"lead_id 不是合法 UUID：{lead_id}") from exc
        lead = (
            await db.execute(
                select(LeadRow).where(
                    LeadRow.id == target_lead, LeadRow.session_id == toolbox.session_id
                )
            )
        ).scalar_one_or_none()
        if lead is None:
            raise ValueError("该线索不在当前会话")
        links = list(
            (
                await db.execute(
                    select(LeadWeaknessLinkRow).where(
                        LeadWeaknessLinkRow.session_id == toolbox.session_id,
                        LeadWeaknessLinkRow.lead_id == target_lead,
                    )
                )
            )
            .scalars()
            .all()
        )
        weakness_ids = [link.finding_id for link in links]
        findings = (
            list(
                (
                    await db.execute(
                        select(FindingRow).where(
                            FindingRow.session_id == toolbox.session_id,
                            FindingRow.id.in_(weakness_ids),
                        )
                    )
                )
                .scalars()
                .all()
            )
            if weakness_ids
            else []
        )
        return {
            "lead": {
                "lead_id": str(lead.id),
                "hypothesis": lead.hypothesis[:400],
                "status": lead.status,
                "next_action": lead.next_action,
                "blocked_reason": lead.blocked_reason,
                "resume_condition": lead.resume_condition,
                "required_conditions": redact_api_value(lead.required_conditions or []),
                "actionability_assessment": redact_api_value(
                    lead.actionability_assessment or {}
                ),
                "revision": lead.revision,
            },
            "relations": [redact_api_value(_relation_record(link)) for link in links],
            "weaknesses": [redact_api_value(_weakness_record(item)) for item in findings],
            "retrieval": "explicit_id",
            "scope": scope,
        }


async def query_history(
    toolbox: Any,
    state: dict[str, Any],
    *,
    query: str = "",
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = 5,
    record_id: str | None = None,
) -> dict[str, Any]:
    if record_id:
        # 明确 ID/条件关系优先于关键词：模型已有 ID 时不应只能靠文字命中。
        if kind == "lead":
            return await read_weakness_detail(toolbox, lead_id=record_id)
        return await read_weakness_detail(toolbox, weakness_id=record_id)
    allowed = HISTORY_KINDS
    if kind is not None and kind not in allowed:
        raise ValueError("不支持的历史类型")
    query = query.strip().casefold()
    scope = {"session": str(toolbox.session_id), "query": query, "kind": kind}
    after = ""
    if cursor:
        try:
            saved = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if any(saved.get(key) != value for key, value in scope.items()):
                raise ValueError("历史游标与当前会话或查询不一致")
            after = str(saved["after"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("历史分页游标无效") from exc
    limit = max(1, min(int(limit), 20))
    matches: list[dict[str, Any]] = []
    tables = {
        "lead": (LeadRow, LeadRow.id),
        "weakness": (FindingRow, FindingRow.id),
        "relation": (LeadWeaknessLinkRow, LeadWeaknessLinkRow.id),
        "observation": (ObservationRow, ObservationRow.id),
        "operation": (ToolOperationRow, ToolOperationRow.operation_id),
        "report": (ReportVersionRow, ReportVersionRow.id),
        "conversation": (ArtifactRow, ArtifactRow.id),
    }

    def include(key: str, record: dict[str, Any], body: str) -> bool:
        if key <= after or (query and query not in body.casefold()):
            return False
        at = body.casefold().find(query) if query else 0
        start = max(0, at - 120)
        record.update(
            match_excerpt=body[start : start + 600],
            match_range=[start, min(len(body), start + 600)],
            excerpt_truncated=len(body) > start + 600,
        )
        matches.append(redact_api_value(record))
        return len(matches) >= limit

    async with toolbox.db.session() as db:
        for label in sorted(allowed):
            if kind is not None and label != kind:
                continue
            if label in tables:
                model, pk = tables[label]
                selection = (
                    select(model).where(model.session_id == toolbox.session_id).order_by(pk)
                )
                if after.startswith(label + ":"):
                    selection = selection.where(pk > UUID(after.split(":", 1)[1]))
                elif after and label < after.split(":", 1)[0]:
                    continue
                if label == "conversation":
                    selection = selection.where(
                        ArtifactRow.summary == "可检索对话来源（正常可见脱敏视图）",
                        ArtifactRow.availability == "available",
                    )
                # keyset 分批读，任何数量都能翻页；不依赖分数前 200 条。
                last = None
                while True:
                    batch_query = selection.where(pk > last) if last is not None else selection
                    batch = (await db.execute(batch_query.limit(100))).scalars().all()
                    if not batch:
                        break
                    for row in batch:
                        key_id = (
                            getattr(row, "operation_id", None) if label == "operation" else row.id
                        )
                        key = f"{label}:{key_id}"
                        if label == "weakness":
                            record = _weakness_record(row)
                            body = json.dumps(
                                {
                                    "title": row.title,
                                    "description": row.description,
                                    "category": row.category,
                                    "weakness_kind": row.weakness_kind,
                                    "status": row.status,
                                    "conditions": row.provided_conditions,
                                },
                                ensure_ascii=False,
                            )
                            if include(key, record, body):
                                return {
                                    "items": matches,
                                    "next_cursor": _cursor({**scope, "after": key}),
                                    "retrieval": "keyword_substring",
                                    "semantic_matching": False,
                                }
                            continue
                        if label == "relation":
                            record = _relation_record(row)
                            body = json.dumps(record, ensure_ascii=False)
                            if include(key, record, body):
                                return {
                                    "items": matches,
                                    "next_cursor": _cursor({**scope, "after": key}),
                                    "retrieval": "keyword_substring",
                                    "semantic_matching": False,
                                }
                            continue
                        record = {
                            "type": label,
                            "id": str(key_id),
                            "version": getattr(row, "revision", getattr(row, "version", 1)),
                        }
                        if label == "lead":
                            body = json.dumps(
                                {
                                    "hypothesis": row.hypothesis,
                                    "status": row.status,
                                    "next_action": row.next_action,
                                    "blocked_reason": row.blocked_reason,
                                    "resume_condition": row.resume_condition,
                                },
                                ensure_ascii=False,
                            )
                            record["source"] = {
                                "evidence_refs": row.evidence_refs,
                                "merged_into_id": str(row.merged_into_id)
                                if row.merged_into_id
                                else None,
                            }
                        elif label == "observation":
                            body = row.summary
                            record["source"] = {"artifact_ids": row.artifact_ids, "url": row.url}
                        elif label == "operation":
                            body = json.dumps(
                                {
                                    "tool": row.tool_name,
                                    "status": row.status,
                                    "execution_context": row.execution_context,
                                },
                                ensure_ascii=False,
                            )
                            record["source"] = {
                                "artifact_ids": row.result_artifact_ids,
                                "job_id": str(row.job_id) if row.job_id else None,
                            }
                        elif label == "report":
                            body = json.dumps(row.input_data_revision, ensure_ascii=False)
                            record["source"] = {
                                "artifact_ids": row.artifact_refs,
                                "markdown_artifact_id": str(row.markdown_artifact_id)
                                if row.markdown_artifact_id
                                else None,
                            }
                        else:
                            if not toolbox.artifacts.exists(row.relative_key):
                                continue
                            visible = json.loads(toolbox.artifacts.read_bytes(row.relative_key))
                            if visible.get("view") != "visible_redacted_no_protocol_reasoning":
                                continue
                            body = json.dumps(visible.get("messages") or [], ensure_ascii=False)
                            record["source"] = {
                                "artifact_id": str(row.id),
                                "message_ids": [
                                    item.get("id") for item in visible.get("messages") or []
                                ],
                                "previous_source_artifact_id": visible.get(
                                    "previous_source_artifact_id"
                                ),
                            }
                        if include(key, record, body):
                            return {
                                "items": matches,
                                "next_cursor": _cursor({**scope, "after": key}),
                                "retrieval": "keyword_substring",
                                "semantic_matching": False,
                            }
                    last = (
                        getattr(batch[-1], "operation_id", None)
                        if label == "operation"
                        else batch[-1].id
                    )
            else:
                records = (
                    state.get("plan") or []
                    if label == "plan"
                    else [
                        visible_message_record(message) for message in state.get("messages") or []
                    ]
                )
                for index, item in enumerate(records):
                    key = f"{label}:{index:08d}"
                    body = json.dumps(item, ensure_ascii=False)
                    source = {
                        "checkpoint_projection": True,
                        "item_index": index,
                        "plan_revision": state.get("plan_revision"),
                        "message_id": item.get("id"),
                    }
                    if include(
                        key,
                        {
                            "type": label,
                            "id": item.get("step_id") or item.get("id") or str(index),
                            "version": state.get("plan_revision", 1),
                            "source": source,
                        },
                        body,
                    ):
                        return {
                            "items": matches,
                            "next_cursor": _cursor({**scope, "after": key}),
                            "retrieval": "keyword_substring",
                            "semantic_matching": False,
                        }
    return {
        "items": matches,
        "next_cursor": None,
        "retrieval": "keyword_substring",
        "semantic_matching": False,
    }


@tool("search_history")
async def search_history(
    runtime: ToolRuntime[RunContext],
    query: str = "",
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = 5,
    record_id: str | None = None,
) -> str:
    """检索当前会话历史，空关键词可分页列目录。

    Args:
        query: 关键词或中文子串；语义相近但措辞不同可能漏召回。
        kind: 可选 lead/weakness/relation/observation/operation/report/conversation/plan/message。
        cursor: 上页 next_cursor；只能用于同一会话和查询。
        limit: 默认每页 5 条，最多 20；不是总记录上限。
        record_id: 已知对象 ID 时可直接读取详情（不依赖关键词命中）。
            `kind="lead"` 时按线索 ID 读取；否则按弱点 ID 读取，
            返回该对象的条件、关系邻居与关联线索。
    """
    return json.dumps(
        await query_history(
            runtime.context.toolbox,
            dict(runtime.state),
            query=query,
            kind=kind,
            cursor=cursor,
            limit=limit,
            record_id=record_id,
        ),
        ensure_ascii=False,
    )
