"""独立原型入口：默认不注册到生产 Agent；复用原 HTTP 执行与持久化路径。"""

from __future__ import annotations

import json

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agent_workbench.agent.context import RunContext
from agent_workbench.domain.shadow_authorization import bind_profile, load_authorization
from agent_workbench.tools.http import HttpRequestArgs, run_http_request


@tool
async def run_http_test_profile(profile_id: str, runtime: ToolRuntime[RunContext]) -> str:
    """执行控制端已发布的固定 HTTP 测试配方；不接受 URL、凭据或授权布尔值。"""
    box = runtime.context.toolbox
    if "http_request" in runtime.context.disabled_tools:
        raise ValueError("original_http_capability_disabled")
    policy = load_authorization(box)
    if profile_id not in policy.profiles:
        raise ValueError("unpublished_http_test_profile")
    args = HttpRequestArgs.model_validate(policy.profiles[profile_id].request)
    # 原有控制版本、Scope、请求体检查、资源锁、幂等及 submit 全部继续执行。
    context = None
    if box.settings.history_action_shadow_enabled:
        from agent_workbench.tools.history_action_shadow import dispatch_context

        try:
            context = dispatch_context(runtime)
            context["profile_binding"] = bind_profile(
                policy, profile_id, args.model_dump(exclude={"note"})
            )
        except Exception:
            # 观察输入失败不能阻止已授权配方的原 HTTP 路径。
            import structlog

            structlog.get_logger(__name__).warning("shadow_profile_context_failed")
    result = await run_http_request(args, box, runtime.tool_call_id or "", shadow_context=context)
    return json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
