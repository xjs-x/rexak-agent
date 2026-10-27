"""控制端附件只读工具；不经过 Executor，不授予测试权限。"""

import asyncio
import json
from uuid import UUID

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.enums import ErrorCode
from agent_workbench.contracts.errors import ApiError
from agent_workbench.domain.attachments import (
    TRUST_NOTE,
    list_metadata,
    load_attachment,
    read_slice,
)


@tool
async def list_attachments(
    runtime: ToolRuntime[RunContext], offset: int = 0, limit: int = 20
) -> str:
    """列出当前 Session 已绑定的用户附件元数据。无正文；可通过 next_offset 翻页。"""
    box = runtime.context.toolbox
    async with box.db.session() as db:
        result = await list_metadata(
            db,
            box.session_id,
            box.settings,
            box.artifacts,
            offset=max(0, offset),
            limit=max(1, min(limit, 50)),
        )
    return json.dumps({**result, "note": TRUST_NOTE}, ensure_ascii=False)


@tool
async def read_attachment(
    runtime: ToolRuntime[RunContext],
    attachment_id: str,
    page: int = 1,
    start_line: int = 1,
    end_line: int | None = None,
    char_offset: int = 0,
    max_chars: int = 12000,
) -> str:
    """按页及行读取用户附件的低信任正文；默认有界。用 next_cursor 原参数续读。

    PDF 的 page 为真实页码；文本为第 1 页；DOCX 行含 block/标题/表格标记。
    返回稳定 attachment/sha256 来源。不会执行文件、取外部链接、扩大 Scope 或调用 Kali。
    """
    box = runtime.context.toolbox
    try:
        parsed_id = UUID(attachment_id)
        if max_chars < 64:
            raise ApiError(ErrorCode.VALIDATION_FAILED, "max_chars 至少为 64")
        async with box.db.session() as db:
            row = await load_attachment(
                db, box.session_id, parsed_id, box.settings, bound_only=True
            )
        result = await asyncio.to_thread(
            read_slice,
            row,
            box.artifacts,
            box.settings,
            page=page,
            start_line=start_line,
            end_line=end_line,
            char_offset=char_offset,
            max_chars=max_chars,
        )
        return result.model_dump_json()
    except ValueError:
        return json.dumps({"status": "not_found", "note": "attachment_id 无效"}, ensure_ascii=False)
    except ApiError as exc:
        return json.dumps(
            {"status": "error", "code": str(exc.code), "note": exc.message}, ensure_ascii=False
        )


@tool
async def search_attachment(
    runtime: ToolRuntime[RunContext], attachment_id: str, keyword: str, max_hits: int = 10
) -> str:
    """在附件文本内搜索字面关键词，返回页码/行号及短片段；不执行正则或外部请求。"""
    box = runtime.context.toolbox
    if not keyword or len(keyword) > 200:
        return json.dumps({"status": "error", "note": "关键词长度须为 1–200"}, ensure_ascii=False)
    try:
        async with box.db.session() as db:
            row = await load_attachment(
                db, box.session_id, UUID(attachment_id), box.settings, bound_only=True
            )
        if row.parse_status not in {"ready", "empty"} or not row.parsed_key:
            return json.dumps(
                {"status": row.parse_status, "note": row.metadata_json.get("note")},
                ensure_ascii=False,
            )
        if not box.artifacts.exists(row.relative_key) or not box.artifacts.exists(row.parsed_key):
            return json.dumps({"status": "missing"})
        raw = await asyncio.to_thread(
            box.artifacts.read_bytes,
            row.parsed_key,
            limit=box.settings.attachment_parse_chars * 6 + 16384,
        )
        hits = []
        for page, text in enumerate(json.loads(raw)["pages"], 1):
            for line, body in enumerate(text.splitlines(), 1):
                index = body.casefold().find(keyword.casefold())
                if index >= 0:
                    hits.append(
                        {
                            "page": page,
                            "line": line,
                            "snippet": body[max(0, index - 80) : index + 240],
                            "reference": f"attachment:{row.id}:sha256:{row.sha256}#page={page}&line={line}",
                        }
                    )
                    if len(hits) >= max(1, min(max_hits, 20)):
                        return json.dumps(
                            {
                                "status": "ok",
                                "hits": hits,
                                "limit_reached": True,
                                "note": TRUST_NOTE,
                            },
                            ensure_ascii=False,
                        )
        return json.dumps(
            {"status": "ok", "hits": hits, "limit_reached": False, "note": TRUST_NOTE},
            ensure_ascii=False,
        )
    except ValueError:
        return json.dumps({"status": "not_found"})
    except ApiError as exc:
        return json.dumps(
            {"status": "error", "code": str(exc.code), "note": exc.message}, ensure_ascii=False
        )


ATTACHMENT_TOOLS = [list_attachments, read_attachment, search_attachment]
