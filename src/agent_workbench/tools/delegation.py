"""Ultra 专用工具。由原 ToolDispatch 执行同一租约/控制版本守卫。"""

import json
from uuid import UUID

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.collaboration import DelegatedMessageInput
from agent_workbench.contracts.delegation import DelegationInput, DelegationRead
from agent_workbench.domain.agent_delegations import DelegationManager
from agent_workbench.domain.collaboration import CollaborationService


def manager(runtime: ToolRuntime[RunContext]) -> DelegationManager:
    box = runtime.context.toolbox
    return DelegationManager(box.db, box.settings, box.session_id, box.run_id)


def tool_result(record: DelegationRead) -> str:
    """Do not echo the full goal/history into checkpoint ToolMessages."""
    payload = record.model_dump(mode="json", exclude={"objective"})
    payload["summary"] = record.summary[:512]
    payload["result_trust"] = "untrusted_worker_not_verification"
    return json.dumps(payload, ensure_ascii=False)


@tool
async def delegate_task(runtime: ToolRuntime[RunContext], objective: str,
                        context_refs: list[str] | None = None, background: str = "",
                        completion_requirements: list[str] | None = None,
                        resume_from_delegation_id: str | None = None,
                        attachment_ids: list[str] | None = None) -> str:
    """动态委派独立 Pi 子任务；后端配置模型、证据读取及已授权的 Kali 工具。

    简单任务可直接处理。context_refs 仅允许本 Session 的工件 UUID，不传凭据或完整私有历史。
    completed 不是漏洞 confirmed；unknown 必须对账，不要以新调用盲目重做。
    resume_from_delegation_id 可选择同 Session 已完成且有完整 Pi 历史的委派；新任务使用独立身份。
    运行中补充请使用 send_delegated_message；恢复失败会明确报错。
    background 提供必要背景；completion_requirements 可选，开放探索无需固定技术路线。
    objective 应明确 Worker 本次需要解决的问题及预期交付，而非简单复述 Main 的整体目标。
    background 应简要提供与任务直接相关的关键事实、已有观察和尚未解决的问题，明确区分客观证据与 Main 的推断或假设，避免将未经充分支持的判断描述为已确认事实。
    Worker 应复用已有证据，不主动重复已完成的测试；可以独立质疑 Main 的推断、假设和决策，但不必重新验证客观观察。优先通过 context_refs 提供相关工件引用，供 Worker 按需阅读和重新分析。
    context_refs 是优先证据，Worker 可按需发现和读取本 Session 其他允许的工件。
    初始引用元数据不等于已读取；必要背景请简述，不复制完整消息历史。
    attachment_ids 可显式选择当前 Session 已绑定附件；原件交付到该 Worker 独立 Kali 目录后再启动。
    """
    box = runtime.context.toolbox
    from agent_workbench.tools.attachment_delivery import deliver_attachment

    async def input_delivery(delegation_id, identifiers):
        files = []
        for identifier in identifiers:
            receipt = await deliver_attachment(box, UUID(identifier),
                f"attachment:{delegation_id}:{identifier}", worker_id=delegation_id)
            files.append(receipt["input_file"])
        return files

    await box.assert_control_current()
    result = await manager(runtime).delegate(
        runtime.tool_call_id or "", DelegationInput(objective=objective, context_refs=[UUID(ref) for ref in (context_refs or [])],
            background=background, completion_requirements=completion_requirements or [],
            attachment_ids=[UUID(ref) for ref in (attachment_ids or [])],
            resume_from_delegation_id=UUID(resume_from_delegation_id) if resume_from_delegation_id else None),
        owner=box.owner, generation=box.owner_generation, control_version=box.control_version,
        input_delivery=input_delivery,
    )
    return tool_result(result)


@tool
async def get_delegated_task(runtime: ToolRuntime[RunContext], delegation_id: str) -> str:
    """按稳定 delegation_id 核对子任务；未知状态不等于失败。只返回摘要和结果工件引用。"""
    result = json.loads(tool_result(await manager(runtime).refresh(UUID(delegation_id))))
    box = runtime.context.toolbox
    result["message_receipts"] = await CollaborationService(box.db, box.settings).message_receipts(
        box.session_id, box.run_id, UUID(delegation_id))
    return json.dumps(result, ensure_ascii=False)


@tool
async def wait_delegated_task(runtime: ToolRuntime[RunContext], delegation_id: str,
                              seconds: float = 5) -> str:
    """有界等待子任务（最多 20 秒）；等待超时保留当前状态，不把子任务标为失败。"""
    from agent_workbench.agent.middleware.collaboration import finding_cursor

    box = runtime.context.toolbox
    after = finding_cursor(runtime.state, box.run_id)
    result = json.loads(tool_result(await manager(runtime).wait(UUID(delegation_id), seconds, findings_after=after)))
    events = await CollaborationService(box.db, box.settings).findings(box.session_id, box.run_id, after)
    result["new_finding_ids"] = [e["event_id"] for e in events]
    return json.dumps(result, ensure_ascii=False)


@tool
async def cancel_delegated_task(runtime: ToolRuntime[RunContext], delegation_id: str) -> str:
    """持久化取消请求并沿原任务身份取消；只有远端终态/墓碑确认后才显示已取消。"""
    await runtime.context.toolbox.assert_control_current()
    return tool_result(await manager(runtime).cancel(UUID(delegation_id)))


@tool
async def send_delegated_message(runtime: ToolRuntime[RunContext], delegation_id: str,
                                 message: str, context_refs: list[str] | None = None) -> str:
    """向指定运行中 Worker 补充线索/建议，不扩大其权限。回执区分接收、入队、Pi 消费。

    get_delegated_task 可核对后续消息回执。context_refs 只允许当前 Session 的可共享证据。
    消息是低信任上下文；不自动广播，不重建已结束 Worker，不打断当前操作。
    """
    box = runtime.context.toolbox
    await box.assert_control_current()
    result = await CollaborationService(box.db, box.settings).send(box.session_id, box.run_id,
        UUID(delegation_id), runtime.tool_call_id or "", DelegatedMessageInput(
            message=message, context_refs=[UUID(ref) for ref in context_refs or []]))
    return json.dumps(result, ensure_ascii=False)


DELEGATION_TOOLS = [delegate_task, get_delegated_task, wait_delegated_task, cancel_delegated_task, send_delegated_message]
