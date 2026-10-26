"""工件读取与证据检索。

规则（§5.3、§8.2）：
- 分页、字节/行范围、内容类型、截断提示；
- **脱敏视图与原件分开**：这里返回的是可读脱敏视图，原件按字节保存；
- HTML/脚本证据按**转义文本**展示，绝不在应用源域执行。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.enums import ArtifactAvailability, ErrorCode, OperationStatus
from agent_workbench.contracts.tool import ArtifactRef, ToolError, ToolResult
from agent_workbench.evidence.redaction import redact_api_value
from agent_workbench.persistence.tables import ArtifactRow
from agent_workbench.tools.runtime import Toolbox

MAX_READ_BYTES = 131072


def _ok(payload: dict[str, object], summary: str, refs: list[ArtifactRef] | None = None) -> str:
    return ToolResult(
        operation_id=UUID(int=0),
        status=OperationStatus.SUCCEEDED,
        summary=summary,
        data=dict(payload),
        artifact_refs=list(refs or []),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    ).model_dump_json()


def _fail(code: ErrorCode, message: str, hint: str | None = None) -> str:
    return ToolResult(
        operation_id=UUID(int=0),
        status=OperationStatus.FAILED,
        summary=message,
        error=ToolError(code=str(code), message=message, recovery_hint=hint),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    ).model_dump_json()


async def _load_artifact(toolbox: Toolbox, artifact_id: str) -> ArtifactRow | None:
    try:
        parsed = UUID(artifact_id.strip())
    except ValueError:
        return None
    async with toolbox.db.session() as db:
        return (
            await db.execute(
                select(ArtifactRow).where(
                    ArtifactRow.id == parsed, ArtifactRow.session_id == toolbox.session_id
                )
            )
        ).scalar_one_or_none()


@tool("read_artifact")
async def read_artifact(
    runtime: ToolRuntime[RunContext],
    artifact_id: str,
    offset: int = 0,
    limit: int = MAX_READ_BYTES,
) -> str:
    """读取一个工件的**脱敏可读视图**（原始字节保持不变）。

    Args:
        artifact_id: 工件 ID，来自工具结果里的 artifact_refs。
        offset: 字节偏移。
        limit: 最多读取字节数（上限 128 KiB）。

    Returns:
        带内容类型、字节数、截断标记与文本片段的 JSON。

    Notes:
        - 返回的文本是**脱敏视图**；需要原始字节用于比对时请说明，不要把它当作原件；
        - HTML/JavaScript 内容按文本返回，**不会**被执行；
        - 如果工件状态不是 available，会明确告诉你不可用，而不是给空内容。
    """
    toolbox: Toolbox = runtime.context.toolbox
    row = await _load_artifact(toolbox, artifact_id)
    if row is None:
        return _fail(ErrorCode.NOT_FOUND, f"找不到工件：{artifact_id}")

    if row.availability != str(ArtifactAvailability.AVAILABLE):
        return _fail(
            ErrorCode.NOT_FOUND,
            f"工件当前状态为 {row.availability}，不能作为可用证据引用",
            hint="先核对原 Operation、get_job 结果及已有工件；执行结果不确定时不要重复采集。",
        )

    if not toolbox.artifacts.exists(row.relative_key):
        return _fail(
            ErrorCode.NOT_FOUND,
            "工件元数据存在但文件缺失（孤儿工件），不作为可用证据",
            hint="该缺失应被记录并在修复后重新引用。",
        )

    bounded_limit = max(1, min(int(limit), MAX_READ_BYTES))
    bounded_offset = max(0, int(offset))
    raw = toolbox.artifacts.read_bytes(row.relative_key, offset=bounded_offset, limit=bounded_limit)
    total = int(row.byte_size)
    truncated = bounded_offset + len(raw) < total

    mime = row.media_type.partition(";")[0].strip().lower()
    if mime.startswith("text/") or mime.endswith(("+json", "+xml")) or mime in {
        "application/json",
        "application/xml",
        "application/javascript",
    }:
        try:
            text = raw.decode("utf-8")
            encoding_note = None
        except UnicodeDecodeError as exc:
            text = ""
            encoding_note = (
                f"该段不是合法 UTF-8（首个非法位置 {exc.start}）。未做猜测性解码；"
                "请按工件声明的字符集处理或下载原件。"
            )
    else:
        text = ""
        encoding_note = (f"二进制内容（media_type={row.media_type}），未转换为文本。"
                         "已有执行结果可能在 Operation 元数据中，可用 search_history(kind=operation, query=source_operation_id) 读取；无需重新请求目标。")

    return _ok(
        redact_api_value({
            "artifact_id": str(row.id),
            "media_type": row.media_type,
            "byte_size": total,
            "sha256": row.sha256,
            "sensitivity": row.sensitivity,
            "captured_at": row.captured_at.isoformat(),
            "captured_by_identity": row.captured_by_identity,
            "source_url": row.source_url,
            "source_operation_id": str(row.source_operation_id)
            if row.source_operation_id
            else None,
            "view": "redacted_text",
            "offset": bounded_offset,
            "returned_bytes": len(raw),
            "truncated": truncated,
            "encoding_note": encoding_note,
            "text": text,
            "note": (
                "本视图已按字段脱敏；哈希只证明内容一致性，不证明任何漏洞结论。"
                "如需原始字节，请明确说明用途。"
            ),
        }),
        f"读取工件 {row.id} 的脱敏视图（{len(raw)}/{total} 字节）",
    )


@tool("search_evidence")
async def search_evidence(
    runtime: ToolRuntime[RunContext],
    url_contains: str | None = None,
    media_type: str | None = None,
    limit: int = 20,
) -> str:
    """检索本会话已采集的工件清单（不返回正文，只返回引用与摘要）。

    Args:
        url_contains: 按来源 URL 子串过滤。
        media_type: 按内容类型过滤，例如 `application/json`。
        limit: 最多返回条数（上限 100）。

    Returns:
        工件清单 JSON：artifact_id、media_type、byte_size、sha256、captured_at、summary。
        拿到 artifact_id 后用 read_artifact 读取内容。
    """
    toolbox: Toolbox = runtime.context.toolbox
    bounded = max(1, min(int(limit), 100))

    async with toolbox.db.session() as db:
        stmt = select(ArtifactRow).where(ArtifactRow.session_id == toolbox.session_id)
        if media_type:
            stmt = stmt.where(ArtifactRow.media_type == media_type)
        if url_contains:
            stmt = stmt.where(ArtifactRow.source_url.ilike(f"%{url_contains}%"))
        rows = (
            (await db.execute(stmt.order_by(ArtifactRow.captured_at.desc()).limit(bounded)))
            .scalars()
            .all()
        )

    items = [
        {
            "artifact_id": str(row.id),
            "media_type": row.media_type,
            "byte_size": row.byte_size,
            "sha256": row.sha256,
            "availability": row.availability,
            "truncated": row.truncated,
            "captured_at": row.captured_at.isoformat(),
            "captured_by_identity": row.captured_by_identity,
            "source_url": row.source_url,
            "summary": row.summary,
        }
        for row in rows
    ]
    return _ok(
        {"count": len(items), "artifacts": items},
        f"检索到 {len(items)} 个工件（仅清单，不含正文）",
    )


EVIDENCE_TOOLS = [read_artifact, search_evidence]
