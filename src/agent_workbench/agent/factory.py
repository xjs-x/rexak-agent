"""`create_agent` 装配的**唯一入口**（§6.1）。

统一装配：模型实例、工具集合、middleware、checkpointer、store、系统提示与版本配置。

不在这里做的事：
- 不写第二套模型—工具循环；
- 不把数据库连接/HTTP client/浏览器对象写进 state；
- 不在 import 时建立连接（通过 `open_agent_runtime` 上下文管理器成对起停）。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from langchain.agents import create_agent
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware import (
    ContextBuilderMiddleware,
    FinishGateMiddleware,
    ModelObserverMiddleware,
    ToolDispatchMiddleware,
    build_compaction_middleware,
)
from agent_workbench.agent.middleware.command_guard import CommandGuardMiddleware
from agent_workbench.agent.middleware.compaction import CompactionValidatorMiddleware
from agent_workbench.agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.config.catalog import PriceCatalog, default_price_catalog
from agent_workbench.config.secrets import SecretStore, build_secret_store
from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.models.provider import (
    ModelBinding,
    build_chat_model,
    resolve_provider_profile,
)
from agent_workbench.persistence.bootstrap import (
    open_checkpointer_connection,
    open_store_connection,
)
from agent_workbench.tools.registry import ToolSelection, build_tools, default_selection

CODE_VERSION = "dev"


@dataclass(slots=True)
class AgentRuntime:
    """已装配好的图 + 其资源句柄。"""

    graph: Any
    checkpointer: AsyncPostgresSaver
    store: AsyncPostgresStore
    binding: ModelBinding
    prompt_version: str
    code_version: str
    tool_manifest: dict[str, Any]

    def config(self, *, thread_id: str, run_id: str, recursion_limit: int) -> dict[str, Any]:
        """RunnableConfig。

        `recursion_limit` 只是**片段内**的保护，不能当作长期运行方案；
        达到上限时进入恢复流程而不是当成完成（§6.4）。
        """
        return {
            "configurable": {"thread_id": thread_id, "run_id": run_id},
            "recursion_limit": recursion_limit,
        }


def build_tool_manifest(selection: ToolSelection) -> dict[str, Any]:
    manifest = selection.to_manifest()
    manifest["tools"] = [getattr(item, "name", str(item)) for item in build_tools(selection)]
    return manifest


@contextlib.asynccontextmanager
async def open_agent_runtime(
    *,
    settings: Settings | None = None,
    binding: ModelBinding | None = None,
    selection: ToolSelection | None = None,
    price_catalog: PriceCatalog | None = None,
    secret_store: SecretStore | None = None,
) -> AsyncIterator[AgentRuntime]:
    """构建并持有一次进程运行期需要的 Agent 资源。

    checkpointer 与 store 使用**独立连接**（各自 schema），
    与业务表连接池分开，生命周期成对。
    """
    cfg = settings or get_settings()
    store_secrets = secret_store or build_secret_store()
    chosen = selection or default_selection(cfg)

    if binding is None:
        profile = resolve_provider_profile(
            cfg.default_provider,
            cfg.xiaomi_model if cfg.default_provider == "xiaomi" else None,
            cfg,
        )
        binding = ModelBinding(
            profile=profile, request_timeout_seconds=cfg.single_operation_timeout_seconds
        )

    model = build_chat_model(binding, secret_store=store_secrets, streaming=True)
    tools = build_tools(chosen)
    middleware = [
        CommandGuardMiddleware(),
        ContextBuilderMiddleware(),
        ModelObserverMiddleware(price_catalog or default_price_catalog()),
        ToolDispatchMiddleware(),
        build_compaction_middleware(
            model,
            max_input_tokens=binding.profile.input_budget_tokens,
            trigger_fraction=cfg.context_trigger_ratio,
        ),
        CompactionValidatorMiddleware(),
        FinishGateMiddleware(),
    ]

    checkpointer_ctx = open_checkpointer_connection(cfg)
    store_ctx = open_store_connection(cfg)
    checkpointer_conn = await checkpointer_ctx.__aenter__()
    store_conn = await store_ctx.__aenter__()
    try:
        checkpointer = AsyncPostgresSaver(checkpointer_conn)
        store = AsyncPostgresStore(store_conn)
        # 注意：`setup()` 只由 `workbench db-init` 执行；这里不重复建表。

        graph = create_agent(
            model=model,
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
            middleware=middleware,
            state_schema=AgentGraphState,
            context_schema=RunContext,
            checkpointer=checkpointer,
            store=store,
            name="agent_workbench",
        )
        yield AgentRuntime(
            graph=graph,
            checkpointer=checkpointer,
            store=store,
            binding=binding,
            prompt_version=PROMPT_VERSION,
            code_version=CODE_VERSION,
            tool_manifest=build_tool_manifest(chosen),
        )
    finally:
        await store_ctx.__aexit__(None, None, None)
        await checkpointer_ctx.__aexit__(None, None, None)
