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
from uuid import UUID

from langchain.agents import create_agent
from langgraph.checkpoint.base import BaseCheckpointSaver
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
from agent_workbench.agent.middleware.progress import ProgressMiddleware
from agent_workbench.agent.middleware.response_recovery import ResponseRecoveryMiddleware
from agent_workbench.agent.middleware.segment import SegmentMiddleware
from agent_workbench.agent.middleware.skills import SkillsMiddleware
from agent_workbench.agent.middleware.stage_report import StageReportMiddleware
from agent_workbench.agent.middleware.working_context import WorkingContextMiddleware
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
from agent_workbench.persistence.db import Database
from agent_workbench.runtime.checkpoint_fencing import FencedCheckpointSaver
from agent_workbench.tools.registry import ToolSelection, build_tools, default_selection

CODE_VERSION = "dev"


@dataclass(slots=True)
class AgentRuntime:
    """已装配好的图 + 其资源句柄。"""

    graph: Any
    checkpointer: BaseCheckpointSaver
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
            # LangGraph 将 metadata.run_id 相同的输入视为同次调用重连；
            # configurable 的简单字段会被提升为 metadata，不能在这里复用业务 run_id。
            "configurable": {"thread_id": thread_id},
            "metadata": {"workbench_run_id": run_id},
            "recursion_limit": recursion_limit,
        }


def build_tool_manifest(selection: ToolSelection) -> dict[str, Any]:
    manifest = selection.to_manifest()
    manifest["tools"] = [getattr(item, "name", str(item)) for item in build_tools(selection)]
    return manifest


def build_agent_middleware(
    model: Any,
    binding: ModelBinding,
    settings: Settings,
    tools: list[Any],
    price_catalog: PriceCatalog,
) -> list[Any]:
    """纯装配函数，捕获请求测试验证 wrap 顺序与最终视图。

    模型调用的包装顺序**必须**是：
    `ResponseRecovery -> ContextBuilder -> ModelObserver -> 官方模型调用`。
    这样每次恢复尝试都会重新构造发送视图，并由 ModelObserver 独立入账。
    """
    return [
        CommandGuardMiddleware(),
        SkillsMiddleware(),
        SegmentMiddleware(settings.segment_decision_rounds),
        ProgressMiddleware(),
        WorkingContextMiddleware(),
        StageReportMiddleware(),
        ResponseRecoveryMiddleware(
            max_recoveries=binding.profile.max_response_recoveries,
            recovery_output_limit=binding.profile.recovery_output_limit,
        ),
        ContextBuilderMiddleware(binding.profile),
        ModelObserverMiddleware(
            price_catalog,
            request_timeout_seconds=settings.model_request_max_seconds,
            idle_timeout_seconds=binding.request_timeout_seconds,
        ),
        ToolDispatchMiddleware(),
        build_compaction_middleware(
            model,
            max_input_tokens=binding.profile.input_budget_tokens,
            trigger_fraction=settings.context_trigger_ratio,
            profile=binding.profile,
            system_prompt=SYSTEM_PROMPT,
            tools=tools,
            request_timeout_seconds=settings.model_request_max_seconds,
            idle_timeout_seconds=binding.request_timeout_seconds,
            price_catalog=price_catalog,
        ),
        FinishGateMiddleware(),
    ]


@contextlib.asynccontextmanager
async def open_agent_runtime(
    *,
    settings: Settings | None = None,
    binding: ModelBinding | None = None,
    selection: ToolSelection | None = None,
    price_catalog: PriceCatalog | None = None,
    secret_store: SecretStore | None = None,
    thread_id: str | None = None,
    run_id: UUID | None = None,
    database: Database | None = None,
    owner_generation: int = 0,
    control_version: int = 0,
    writer_epoch: int = 0,
    writer_owner: str | None = None,
) -> AsyncIterator[AgentRuntime]:
    """构建并持有一次进程运行期需要的 Agent 资源。

    checkpointer 与 store 使用**独立连接**（各自 schema），
    与业务表连接池分开，生命周期成对。
    """
    cfg = settings or get_settings()
    store_secrets = secret_store or build_secret_store()
    chosen = selection or default_selection(cfg)

    if binding is None:
        profile = resolve_provider_profile(settings=cfg)
        binding = ModelBinding(
            profile=profile, request_timeout_seconds=cfg.model_request_timeout_seconds
        )

    model = build_chat_model(binding, secret_store=store_secrets, streaming=True)
    tools = build_tools(chosen)
    middleware = build_agent_middleware(
        model, binding, cfg, tools, price_catalog or default_price_catalog()
    )

    async with contextlib.AsyncExitStack() as stack:
        checkpointer_conn = await stack.enter_async_context(open_checkpointer_connection(cfg))
        store_conn = await stack.enter_async_context(open_store_connection(cfg))
        checkpointer: BaseCheckpointSaver = AsyncPostgresSaver(checkpointer_conn)
        if run_id is not None:
            if database is None or thread_id is None:
                raise ValueError("fenced checkpointer 需要 database 与 thread_id")
            checkpointer = FencedCheckpointSaver(
                checkpointer,
                database,
                run_id=run_id,
                thread_id=thread_id,
                owner_generation=owner_generation,
                control_version=control_version,
                writer_epoch=writer_epoch,
                writer_owner=writer_owner,
            )
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
