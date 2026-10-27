"""正常可见历史的只读归档与请求正文替换，隐藏协议正文不进入检索。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from langchain_core.messages import BaseMessage
from sqlalchemy import select

from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.evidence.redaction import redact_api_value
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import ArtifactRow


def visible_message_record(message: BaseMessage) -> dict[str, Any]:
    content = message.content
    if isinstance(content, list):
        content = [
            block
            for block in content
            if isinstance(block, str)
            or (isinstance(block, dict) and block.get("type") in {"text", "image_url"})
        ]
    return redact_api_value(
        {
            "id": message.id,
            "role": message.type,
            "content": content,
            "tool_call_id": getattr(message, "tool_call_id", None),
            "tool_calls": getattr(message, "tool_calls", []),
        }
    )


async def persist_history(
    toolbox: Any, messages: list[BaseMessage], *, previous_source: str | None = None
) -> dict[str, Any]:
    record = {
        "schema_version": 1,
        "view": "visible_redacted_no_protocol_reasoning",
        "previous_source_artifact_id": previous_source,
        "messages": [visible_message_record(message) for message in messages],
    }
    body = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    stored = await asyncio.to_thread(
        ArtifactStore(toolbox.db.settings).write_bytes, body, media_type="application/json"
    )
    async with toolbox.db.transaction() as db:
        existing = (
            await db.execute(
                select(ArtifactRow)
                .where(
                    ArtifactRow.session_id == toolbox.session_id,
                    ArtifactRow.sha256 == stored.sha256,
                    ArtifactRow.summary == "可检索对话来源（正常可见脱敏视图）",
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        artifact = existing or await ArtifactRepository(db).create(
            sha256=stored.sha256,
            byte_size=stored.byte_size,
            media_type=stored.media_type,
            relative_key=stored.relative_key,
            session_id=toolbox.session_id,
            captured_at=stored.captured_at,
            summary="可检索对话来源（正常可见脱敏视图）",
            source_operation_id=None,
            sensitivity="internal",
            availability="available",
        )
    return {
        "artifact_id": str(artifact.id),
        "sha256": stored.sha256,
        "message_ids": [message.id for message in messages],
        "source_version": 1,
    }
