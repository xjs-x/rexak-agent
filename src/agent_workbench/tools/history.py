"""会话内关键词/中文子串检索，稳定分页与来源展开；不检索协议推理。"""

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
    LeadRow,
    ObservationRow,
    ReportVersionRow,
    ToolOperationRow,
)


def _cursor(value: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, sort_keys=True).encode()).decode()


async def query_history(
    toolbox: Any,
    state: dict[str, Any],
    *,
    query: str = "",
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    allowed = ("lead", "observation", "operation", "report", "conversation", "plan", "message")
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
                selection = select(model).where(model.session_id == toolbox.session_id).order_by(pk)
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
                        record: dict[str, Any] = {
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
) -> str:
    """检索当前会话历史，空关键词可分页列目录。

    Args:
        query: 关键词或中文子串；语义相近但措辞不同可能漏召回。
        kind: 可选 lead/observation/operation/report/conversation/plan/message。
        cursor: 上页 next_cursor；只能用于同一会话和查询。
        limit: 默认每页 5 条，最多 20；不是总记录上限。
    """
    return json.dumps(
        await query_history(
            runtime.context.toolbox,
            dict(runtime.state),
            query=query,
            kind=kind,
            cursor=cursor,
            limit=limit,
        ),
        ensure_ascii=False,
    )
