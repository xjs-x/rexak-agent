"""`create_agent` 装配的**唯一入口**（§6.1）。

统一装配：模型实例、工具集合、middleware、checkpointer、store、系统提示与版本配置。

不在这里做的事：
- 不写第二套模型—工具循环；
- 不把数据库连接/HTTP client/浏览器对象写进 state；
- 不在 import 时建立连接（通过 `open_agent_runtime` 上下文管理器成对起停）。
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from types import CodeType
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
from agent_workbench.agent.middleware.collaboration import CollaborationMiddleware
from agent_workbench.agent.middleware.command_guard import CommandGuardMiddleware
from agent_workbench.agent.middleware.history_assistance import HistoryAssistanceMiddleware
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
from agent_workbench.tools.registry import ToolSelection, build_tools, default_selection

LOADED_CODE_SCOPE = "loaded_entrypoints_and_history_assistance_v2_not_entire_process"


def _code_constant(value: Any) -> Any:
    """规范化已加载对象；不含marshal引用标志、对象地址、文件路径或磁盘读取。"""
    if isinstance(value, CodeType):
        return {key: _code_constant(getattr(value, key)) for key in (
            "co_code", "co_consts", "co_names", "co_varnames", "co_freevars", "co_cellvars",
            "co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_flags", "co_exceptiontable",
        )}
    if value is None or isinstance(value, str | int | bool):
        return [type(value).__name__, value]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if isinstance(value, float | complex):
        return [type(value).__name__, repr(value)]
    if isinstance(value, tuple | list):
        return [type(value).__name__, [_code_constant(v) for v in value]]
    if isinstance(value, frozenset):
        return ["frozenset", sorted((_code_constant(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))]
    if isinstance(value, dict):
        return {k: _code_constant(v) for k, v in sorted(value.items())}
    if value is Ellipsis:
        return ["ellipsis"]
    raise TypeError(f"不支持的代码常量类型：{type(value).__name__}")


def loaded_runtime_versions() -> dict[str, Any]:
    """由内存中实际函数生成稳定指纹；范围有界，不冒充整个进程或源码树。"""
    from agent_workbench.agent import history_assistance
    from agent_workbench.agent.decision_memory import build_blackboard
    from agent_workbench.agent.middleware import history_assistance as history_middleware
    from agent_workbench.contracts.plan import PlanStepPatch
    from agent_workbench.domain.decision_memory import read_snapshot, select_memory
    from agent_workbench.domain.graph_sync import graph_context
    from agent_workbench.domain.session_graph import apply_operations, update_graph
    from agent_workbench.tools.planning import apply_plan_update, normalize_steps

    functions = [
        build_agent_middleware,
        open_agent_runtime,
        apply_plan_update,
        normalize_steps,
        apply_operations, update_graph, graph_context,
        ToolDispatchMiddleware._dispatch_tool_call,
        ContextBuilderMiddleware.awrap_model_call,
        WorkingContextMiddleware.abefore_model,
        build_blackboard, read_snapshot, select_memory,
    ]
    for module in (history_assistance, history_middleware):
        functions.extend(v for v in vars(module).values()
            if inspect.isfunction(v) and v.__module__ == module.__name__)
    functions.extend(v for v in vars(HistoryAssistanceMiddleware).values() if inspect.isfunction(v))
    components = {}
    for function in functions:
        raw = inspect.unwrap(function)
        value = {"code": _code_constant(raw.__code__), "defaults": _code_constant(raw.__defaults__),
                 "kwdefaults": _code_constant(raw.__kwdefaults__)}
        components[function.__module__ + "." + function.__qualname__] = hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    # 请求中实际调用的导入绑定也单独记录，避免模块重载后别名与定义不是同一对象。
    for name in ("memory_view", "build_blackboard"):
        function = inspect.unwrap(getattr(history_middleware, name))
        components["history_request_binding." + name] = hashlib.sha256(json.dumps(
            _code_constant(function.__code__), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    payload = {"components": components, "system_prompt": SYSTEM_PROMPT,
               "plan_schema": PlanStepPatch.model_json_schema(), "algorithm": "canonical_code_v2"}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return {"code_version": "loaded-runtime-v2:" + digest.hexdigest()[:40],
            "scope": LOADED_CODE_SCOPE, "algorithm": "canonical_code_v2", "components": components}


def loaded_code_version() -> str:
    return loaded_runtime_versions()["code_version"]


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
    loaded_versions: dict[str, Any] | None = None

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


def checkpoint_needs_skill_bridge(checkpoint: dict[str, Any]) -> bool:
    """识别旧图节点；兼容节点不读取 Skill 数据库或修改任务状态。"""
    return any(
        "SkillsMiddleware." in str(key)
        for field in ("versions_seen", "channel_versions", "channel_values")
        for key in (checkpoint.get(field) or {})
    )


def build_agent_middleware(
    model: Any,
    binding: ModelBinding,
    settings: Settings,
    tools: list[Any],
    price_catalog: PriceCatalog,
    *,
    legacy_skill_checkpoint: bool = False,
    legacy_protocol_checkpoint: bool = False,
    legacy_finish_checkpoint: bool = False,
) -> list[Any]:
    """纯装配函数，捕获请求测试验证 wrap 顺序与最终视图。

    模型调用的包装顺序**必须**是：
    `ResponseRecovery -> ContextBuilder -> ModelObserver -> 官方模型调用`。
    这样每次恢复尝试都会重新构造发送视图，并由 ModelObserver 独立入账。
    """
    external_skills = any(getattr(item, "name", "") == "load_skill" for item in tools)
    return [
        CommandGuardMiddleware(),
        *(
            [SkillsMiddleware(enabled=external_skills)]
            if external_skills or legacy_skill_checkpoint
            else []
        ),
        SegmentMiddleware(settings.segment_decision_rounds),
        WorkingContextMiddleware(),
        *([StageReportMiddleware(enabled=False)] if legacy_protocol_checkpoint else []),
        ResponseRecoveryMiddleware(
            max_recoveries=binding.profile.max_response_recoveries,
            recovery_output_limit=binding.profile.recovery_output_limit,
        ),
        ContextBuilderMiddleware(binding.profile),
        *([HistoryAssistanceMiddleware(binding.profile)] if settings.history_assistance_mode != "off" else []),
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
        # Deliver after compaction so a new hint is not summarized before first use.
        *([CollaborationMiddleware()] if any(getattr(t, "name", "") == "delegate_task" for t in tools) else []),
        *([FinishGateMiddleware()] if legacy_finish_checkpoint else []),
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

    async with contextlib.AsyncExitStack() as stack:
        checkpointer_conn = await stack.enter_async_context(open_checkpointer_connection(cfg))
        store_conn = await stack.enter_async_context(open_store_connection(cfg))
        checkpointer = AsyncPostgresSaver(checkpointer_conn)
        store = AsyncPostgresStore(store_conn)
        # 注意：`setup()` 只由 `workbench db-init` 执行；这里不重复建表。
        saved = (
            await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
            if thread_id
            else None
        )
        middleware = build_agent_middleware(
            model,
            binding,
            cfg,
            tools,
            price_catalog or default_price_catalog(),
            legacy_finish_checkpoint=bool(
                saved
                and any(
                    "FinishGateMiddleware." in str(key)
                    for field in ("versions_seen", "channel_versions", "channel_values")
                    for key in (saved.checkpoint.get(field) or {})
                )
            ),
            legacy_skill_checkpoint=bool(
                saved and checkpoint_needs_skill_bridge(dict(saved.checkpoint))
            ),
            legacy_protocol_checkpoint=bool(
                saved
                and any(
                    "StageReportMiddleware." in str(key)
                    for field in ("versions_seen", "channel_versions", "channel_values")
                    for key in (saved.checkpoint.get(field) or {})
                )
            ),
        )

        graph = create_agent(
            model=model,
            tools=tools,
            system_prompt=SYSTEM_PROMPT + (
                """

## Ultra 模式：Main 与 Pi Worker 协作

### 自主委派与任务分工

- 你可以使用 delegate_task 自主委派 Pi Worker，并通过 get_delegated_task、wait_delegated_task、send_delegated_message、cancel_delegated_task 管理协作。是否委派、委派数量和任务分工由你根据当前目标与实际需求自主决定，无需固定角色或委派流程。
- 优先将适合独立深入研究、并行探索或需要不同分析思路的局部任务交给 Worker。Main 继续负责整体目标、跨线索关联和关键决策，不必等待 Worker 完成才能推进其他工作。
- **参考委派情形（不限于此）：** 存在源码或附件时，Main 可以探索 Web 服务与业务链路，Worker 并行分析源码或附件；存在多个值得研究的漏洞方向时，可以分别开展独立验证；遇到复杂过滤、编码或程序逻辑时，可以委派 Worker 深入分析，Main 继续推进其他线索；需要不同解题思路时，也可以让 Worker 独立研究同一局部问题。
- 委派任务应具有清晰目标与适当边界，但不要预先规定 Worker 的具体操作顺序。允许 Worker 自主选择技术路线、调整假设并深入探索有价值的线索。以上情形仅用于启发委派决策，不构成固定分工或必须并行的要求。

### 上下文交接与独立分析

- 委派时通过 objective 明确目标，通过 background 提供必要背景，按需使用 context_refs 和 completion_requirements。重点传递已经观察到的事实、相关证据、当前假设和待解决问题，避免复制无关历史。
- Worker 可以独立评价 Main 的技术假设，提出不同解释或新的探索方向。已有可靠证据优先复用，不必重复执行已经完成的原始测试。
- Worker 使用独立配置的模型，并根据实际开放的能力使用 Kali 工具和 Session 证据。context_refs 用于推荐重要证据，Worker 也可以自主检索其他相关的 Session 工件。
- 对于具有持久历史的已完成 Worker，可以通过 resume_from_delegation_id 发起关联的新任务，利用已有探索上下文继续研究。

### 阶段性发现与动态协作

- **重要发现应及时汇报：** Worker 可以在执行过程中通过 report_finding 主动向 Main 汇报足以影响整体决策的重要发现，而不必等待子任务结束。尤其是关键漏洞线索、利用链突破、重要前置条件或对现有假设的有力反证。汇报后 Worker 可以继续深入探索，形成“发现即反馈、探索不中断”的协作方式。
- Main 应及时评估 Worker 的重要发现，并结合当前任务决定是否调整攻击方向、关联其他线索、补充委派或改变其他 Worker 的调查重点，而不是仅将其作为普通任务日志。
- wait_delegated_task 可能因阶段性发现提前返回，此时 Worker 仍可能运行。Main 可以根据进展继续行动、等待后续结果，或通过 send_delegated_message 补充信息和调整调查重点。
- Main 应综合自身与 Worker 的发现推进任务，合理利用不同 Worker 的独立探索成果，避免重复投入和无价值的并行工作。

### 目标完成与结果汇总

- 对 CTF 或指定目标，由 Main 综合实际执行结果、Worker 发现和平台反馈判断目标是否完成。Worker 报告的 Flag 可以作为重要候选线索推进任务，不要求固定 Flag 格式或机械重放已完成的探索。
- 达到用户目标后，及时结束不再需要的协作任务；尚未完成时，根据实际进展自主继续探索、调整策略或结束，不设置固定失败次数。
- 最终报告简洁说明目标结果、关键执行路径、Main 与 Worker 的贡献、重要证据引用及尚未解决的问题。
"""
                if chosen.ultra else ""
            ),
            middleware=middleware,
            state_schema=AgentGraphState,
            context_schema=RunContext,
            checkpointer=checkpointer,
            store=store,
            name="agent_workbench",
        )
        versions = {**loaded_runtime_versions(), "capture_stage": "agent_assembly",
                    "captured_at": datetime.now(UTC).isoformat()}
        yield AgentRuntime(
            graph=graph,
            checkpointer=checkpointer,
            store=store,
            binding=binding,
            prompt_version=PROMPT_VERSION,
            code_version=versions["code_version"],
            tool_manifest=build_tool_manifest(chosen),
            loaded_versions=versions,
        )
