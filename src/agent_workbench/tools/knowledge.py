"""方法与记忆工具：`search_methods`、`search_memory`、`propose_memory`。

规则（§6.3、§7.4）：
- 方法资料**按需加载**，不把全部资料塞进系统提示；
- 跨任务记忆**按项目隔离**；运行中按需检索，禁止每轮全量重读全部历史；
- 模型提出的经验先为 `candidate`；**禁止**未经验证的总结自动成为强约束；
- 敏感凭据不写入 Store 或摘要。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.prompts.methods import MethodNotAvailable, list_methods, load_method
from agent_workbench.contracts.enums import ErrorCode, OperationStatus
from agent_workbench.contracts.tool import ToolError, ToolResult
from agent_workbench.memory.store import MemoryStore, summarize_entry
from agent_workbench.tools.params import coerce_str_list
from agent_workbench.tools.runtime import Toolbox


def _ok(payload: dict[str, Any], summary: str) -> str:
    return ToolResult(
        operation_id=UUID(int=0),
        status=OperationStatus.SUCCEEDED,
        summary=summary,
        data=payload,
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


@tool("search_methods")
async def search_methods(name: str | None = None) -> str:
    """列出或加载**版本化方法资料**。

    Args:
        name: 方法名；留空则只返回目录（不返回正文）。

    Returns:
        目录或单份资料正文（带版本与 sha256）。

    Notes:
        - 先看目录再按需加载，不要把全部方法一次读入上下文；
        - 缺少方法资料时会给出确定错误与可用列表，不会让你去扫描文件系统。
    """
    if not name:
        items = list_methods()
        return _ok(
            {"bundle_version": None, "methods": items},
            f"可用方法资料 {len(items)} 份（未加载正文）",
        )

    try:
        doc = load_method(name)
    except MethodNotAvailable as exc:
        available = [item["name"] for item in list_methods()]
        return _fail(
            ErrorCode.NOT_FOUND,
            str(exc),
            hint=f"可用：{available}",
        )
    except Exception as exc:
        return _fail(ErrorCode.INTERNAL_ERROR, f"加载方法资料失败：{type(exc).__name__}: {exc}")

    return _ok(
        {
            "name": doc.name,
            "title": doc.title,
            "sha256": doc.sha256,
            "byte_size": doc.byte_size,
            "content": doc.content,
        },
        f"已加载方法资料《{doc.title}》",
    )


@tool("search_memory")
async def search_memory(
    runtime: ToolRuntime[RunContext],
    query: str | None = None,
    include_candidates: bool = True,
    limit: int = 10,
) -> str:
    """检索**本项目**的跨任务经验。

    Args:
        query: 关键词；留空返回最近的条目。
        include_candidates: 是否包含未验证的候选经验（默认包含，但会标注状态）。
        limit: 最多返回条数。

    Returns:
        记忆条目 JSON：content、verified_status、适用条件、失效条件、来源引用、冲突信息。

    Notes:
        - 只检索当前项目命名空间，**不会**召回其他项目的记忆；
        - `verified_status=candidate` 的经验**不能**当作已验证事实使用；
        - 条目带 `applicable_conditions` / `invalid_conditions` 时，请先判断是否适用。
    """
    toolbox: Toolbox = runtime.context.toolbox
    store = runtime.store
    if store is None:
        return _fail(
            ErrorCode.INTERNAL_ERROR,
            "本次运行没有挂载长期记忆 Store，无法检索跨任务经验",
            hint="这是配置问题，请报告给用户；不要改用猜测来替代。",
        )

    async with toolbox.db.session() as db:
        from sqlalchemy import select

        from agent_workbench.persistence.tables import SessionRow

        row = (
            await db.execute(select(SessionRow).where(SessionRow.id == toolbox.session_id))
        ).scalar_one_or_none()
    if row is None:
        return _fail(ErrorCode.NOT_FOUND, "找不到当前 session，无法定位记忆命名空间")

    memory = MemoryStore(store, toolbox.settings)
    entries = await memory.search(
        project_id=row.project_id,
        query=query,
        limit=int(limit),
        include_candidates=bool(include_candidates),
    )
    return _ok(
        {
            "project_id": str(row.project_id),
            "count": len(entries),
            "entries": [summarize_entry(entry) for entry in entries],
            "usage_note": (
                "candidate 条目只是候选经验，不能作为已验证事实；"
                "与当前观察冲突时以当前证据为准，并记录冲突。"
            ),
        },
        f"检索到 {len(entries)} 条本项目经验",
    )


@tool("propose_memory")
async def propose_memory(
    runtime: ToolRuntime[RunContext],
    content: str,
    memory_type: str = "observation",
    source_artifact_ids_json: list[str] | str | None = None,
    applicable_conditions: str | None = None,
    invalid_conditions: str | None = None,
) -> str:
    """提交一条**候选**跨任务经验。

    Args:
        content: 经验内容（应当是可复用的、跨任务成立的判断，而不是本次任务的临时状态）。
        memory_type: 类别，例如 endpoint_behavior / auth_mechanism / tech_stack / tool_usage。
        source_artifact_ids_json: 支持该经验的工件 ID 的 JSON 数组字符串。
        applicable_conditions: 适用条件（什么情况下成立）。
        invalid_conditions: 失效条件（什么情况下不再成立）。

    Returns:
        写入结果。`verified_status` 一定是 `candidate`。

    Notes:
        - **不要**把本次任务的临时事实写进长期记忆；
        - **不要**写入凭据、Cookie、token 等敏感内容；
        - 与已有已验证条目冲突时不会被覆盖，而是作为候选保留来源等待复核。
    """

    toolbox: Toolbox = runtime.context.toolbox
    store = runtime.store
    if store is None:
        return _fail(ErrorCode.INTERNAL_ERROR, "本次运行没有挂载长期记忆 Store，无法写入经验")

    if _looks_sensitive(content):
        return _fail(
            ErrorCode.VALIDATION_FAILED,
            "内容看起来包含凭据或令牌，长期记忆不保存敏感信息",
            hint="请改为描述『如何取得凭据』，而不是凭据本身。",
        )

    try:
        refs = coerce_str_list(source_artifact_ids_json, "source_artifact_ids_json")
    except ValueError as exc:
        return _fail(ErrorCode.VALIDATION_FAILED, str(exc))

    async with toolbox.db.session() as db:
        from sqlalchemy import select

        from agent_workbench.persistence.tables import SessionRow

        row = (
            await db.execute(select(SessionRow).where(SessionRow.id == toolbox.session_id))
        ).scalar_one_or_none()
    if row is None:
        return _fail(ErrorCode.NOT_FOUND, "找不到当前 session，无法定位记忆命名空间")

    memory = MemoryStore(store, toolbox.settings)
    entry = await memory.propose(
        project_id=row.project_id,
        memory_type=memory_type,
        content=content,
        source_run_id=toolbox.run_id,
        source_artifact_ids=refs,
        applicable_conditions=applicable_conditions,
        invalid_conditions=invalid_conditions,
    )
    return _ok(
        {"entry": summarize_entry(entry)},
        "候选经验已写入（不会自动成为已验证事实）",
    )


_SENSITIVE_MARKERS = ("password", "passwd", "secret", "api_key", "apikey", "token", "bearer ")


def _looks_sensitive(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _SENSITIVE_MARKERS)


KNOWLEDGE_TOOLS = [search_methods, search_memory, propose_memory]
