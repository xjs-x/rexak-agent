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
    """
    动态委派独立 Pi Worker 子任务，支持 Main 与 Worker 协同推进当前目标。Worker 可使用独立模型、允许的 Session 证据及已授权 Kali 工具。
    当某个问题适合独立深入研究、探索不同技术假设，或与 Main 当前工作并行推进时，可以自主创建 Worker。委派不是必需的，简单问题可以直接处理；Main 可以在 Worker 工作期间继续推进其他有价值的方向。
    objective 应明确局部研究目标及其对整体任务的价值。background 提供必要背景，区分已有实际观察、尚未验证的推断和需要解决的问题。Worker 可以独立判断技术路线，不必机械执行 Main 的预设步骤。
    context_refs 提供当前 Session 的相关证据 UUID，仅作为优先参考，Worker 可按需发现和读取其他允许访问的 Session 工件。引用元数据不代表内容已被读取；不需要复制完整历史或传递凭据。
    completion_requirements 为可选完成要求，不必限制开放式探索。Worker 可以通过 report_finding 提前报告重要阶段性发现，并继续执行任务。
    resume_from_delegation_id 可选择复用同 Session 内已完成、具有完整 Pi 历史的委派上下文；新任务默认具有独立身份。
    任务运行期间，可使用 send_delegated_message 补充新线索或调整研究重点。completed 仅表示子任务完成，不直接证明漏洞确认；unknown 状态应先核对，不要盲目重新执行。
    """
    box = runtime.context.toolbox
    from agent_workbench.tools.attachment_delivery import deliver_attachment

    async def input_delivery(delegation_id, identifiers):
        files = []
        for identifier in identifiers:
            receipt = await deliver_attachment(box, UUID(identifier),
                f"attachment:{delegation_id}:{identifier}", worker_id=delegation_id)
            source = receipt["input_file"]
            # Executor receipts include host-only paths and usage hints. The A2A
            # handoff carries only the Worker task-file protocol fields.
            keys = ("attachment_id", "filename", "sha256", "byte_size",
                    "worker_workspace_id", "relative_path", "session_id", "operation_id", "source")
            if (receipt.get("status") != "succeeded" or source.get("worker_workspace_id") != str(delegation_id)
                    or source.get("attachment_id") != identifier):
                raise ValueError("Worker 附件交付回执不匹配")
            files.append({key: source[key] for key in keys})
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
async def get_delegated_task(runtime: ToolRuntime[RunContext], delegation_id: str,
                             history_cursor: int | None = None, history_limit: int = 5) -> str:
    """核对子任务并查看近期进度；指定 history_cursor 后分页读取可见历史，单页最多 10 条。"""
    from agent_workbench.domain.worker_progress import worker_progress
    from agent_workbench.persistence.tables import AgentDelegationRow

    if (history_cursor is not None and history_cursor < 0) or not 1 <= history_limit <= 10:
        raise ValueError("Worker 历史游标或页大小无效")
    delegation = UUID(delegation_id)
    service = manager(runtime)
    result = json.loads(tool_result(await service.refresh(delegation)))
    box = runtime.context.toolbox
    # Authorization is repeated immediately before the local history query.
    await service.read(delegation)
    async with box.db.session() as db:
        row = await db.get(AgentDelegationRow, delegation)
        assert row is not None
        result["progress"] = await worker_progress(db, row, cursor=history_cursor, limit=history_limit)
    result["message_receipts"] = await CollaborationService(box.db, box.settings).message_receipts(
        box.session_id, box.run_id, delegation)
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
    """
    向指定正在运行的 Pi Worker 补充重要线索、证据、技术建议或新的研究重点，帮助 Worker 根据 Main 的最新发现调整局部探索。

    适用于 Main 获得新的关键事实、发现原有假设存在问题，或需要让 Worker 关注新的技术条件时。消息应简洁说明新增信息及其价值，区分实际观察与技术推断，不必重新发送完整任务背景。

    Worker 可以独立判断如何利用补充信息；本工具不要求 Worker 立即停止当前研究，也不会扩大其执行权限。

    context_refs 仅包含当前 Session 可共享的证据引用。消息只发送给指定 Worker，不自动广播，也不会重新启动已结束的任务。

    回执区分消息接收、入队和 Pi 实际消费。必要时可使用 get_delegated_task 核对后续状态。
    """
    box = runtime.context.toolbox
    await box.assert_control_current()
    result = await CollaborationService(box.db, box.settings).send(box.session_id, box.run_id,
        UUID(delegation_id), runtime.tool_call_id or "", DelegatedMessageInput(
            message=message, context_refs=[UUID(ref) for ref in context_refs or []]))
    return json.dumps(result, ensure_ascii=False)


DELEGATION_TOOLS = [delegate_task, get_delegated_task, wait_delegated_task, cancel_delegated_task, send_delegated_message]
