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
    """
    列出当前 Session 已绑定的用户附件元数据。
    当任务涉及源码、配置文件、程序、题目文档等附件时，可通过本工具了解有哪些材料，以及后续适合采用哪种分析方式。
    本工具只返回附件目录和元数据，不读取文件正文。支持通过 next_offset 翻页。
    """
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
    """
    按页和行读取用户附件中可解析的文本内容，适用于分析题目文档、源码、配置文件和其他文本资料。
    可以结合实际内容寻找关键函数、接口、参数、业务逻辑及潜在漏洞条件。内容过长时按返回的 next_cursor 继续读取。
    PDF 使用真实页码，文本默认第 1 页，DOCX 包含结构标记。返回稳定的附件来源信息。此工具不负责执行文件或分析二进制指令。
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
    """
    在附件文本中搜索指定关键词，返回匹配位置及相关片段。
    适合在较长源码或文档中定位已知函数、接口路径、变量、错误信息及其他关键线索，并结合 read_attachment 阅读上下文。
    使用字面关键词匹配，不支持正则表达式，也不发起外部请求。
    """
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


@tool
async def materialize_attachment(runtime: ToolRuntime[RunContext], attachment_id: str) -> str:
    """将本 Session 已绑定附件原件交付到 Kali，返回相对路径、哈希及真实操作来源。

    不解析或执行文件，不扩大 Scope。ZIP 使用现有 Shell/run_script 在独立新目录有界解压，
    检查成员路径、链接和展开量；不要覆盖已有文件。内容保持低信任。
    """
    from agent_workbench.execution.client import (
        ExecutorConnectionError,
        ExecutorProtocolError,
        ExecutorRejected,
    )
    from agent_workbench.tools.attachment_delivery import deliver_attachment
    from agent_workbench.tools.execution_dispatch import ExecutionUnavailable

    try:
        result = await deliver_attachment(runtime.context.toolbox, UUID(attachment_id), runtime.tool_call_id or "")
        return json.dumps(result, ensure_ascii=False)
    except (ValueError, ApiError, ExecutionUnavailable, ExecutorRejected) as exc:
        return json.dumps({"status": "error", "note": str(exc)}, ensure_ascii=False)
    except (ExecutorConnectionError, ExecutorProtocolError) as exc:
        return json.dumps({"status": "unknown", "note": str(exc),
            "hint": "通过原 ToolCall/Operation 身份核对；不要新建调用盲目重发"}, ensure_ascii=False)


ATTACHMENT_TOOLS = [list_attachments, read_attachment, search_attachment]
