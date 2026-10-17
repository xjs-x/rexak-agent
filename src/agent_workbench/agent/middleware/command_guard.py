"""控制指令消费与暂停/取消检查（模型前 / 派发前）。

规则（§4.3）：
- API 收到消息先写入 `commands`，返回排队/已受理状态；
- Runner 在**模型请求前、工具派发前、接收工具结果后**检查控制指令；
- 暂停/取消立即提高优先级；普通补充消息在**下一个一致边界**注入；
- 用户改变范围或明确打断当前方向时递增 `control_version`；
- 旧模型响应可以记录用量，但**派发前必须重新检查版本**；
- **不把新消息插入未闭合的 assistant/tool 配对中**。
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.signals import GraphRunAborted, LeaseLostError
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.contracts.enums import CommandKind, CommandStatus, RunStatus
from agent_workbench.contracts.events import EventType
from agent_workbench.contracts.identity import ScopeSnapshot
from agent_workbench.domain.run_state import is_current_owner
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.observability.events import append_event
from agent_workbench.persistence.repositories import CommandRepository
from agent_workbench.persistence.tables import (
    CommandRow,
    ExecutionDeliveryBatchRow,
    RunRow,
    SessionRow,
)


class CommandGuardMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    async def abefore_model(self, _state, runtime):  # type: ignore[no-untyped-def]
        context: RunContext | None = getattr(runtime, "context", None)
        if context is None:
            return None
        toolbox = context.toolbox

        # 1) 所有权复核：旧 owner 不得继续推进
        async with toolbox.db.session() as db:
            alive = await is_current_owner(
                db,
                run_id=toolbox.run_id,
                owner=toolbox.owner,
                generation=toolbox.owner_generation,
            )
        if not alive:
            raise LeaseLostError(str(toolbox.run_id), toolbox.owner)

        # 2) 暂停 / 取消：一致边界立即生效
        async with toolbox.db.session() as db:
            run = (
                await db.execute(select(RunRow).where(RunRow.id == toolbox.run_id))
            ).scalar_one_or_none()
            task = (
                await db.execute(select(SessionRow).where(SessionRow.id == toolbox.session_id))
            ).scalar_one_or_none()
        if task is None:
            raise RuntimeError("任务不存在，无法核对当前测试范围")
        current_scope = ScopeSnapshot.model_validate(task.scope_snapshot or {})
        if current_scope.revision != toolbox.scope_revision:
            toolbox.scope = ScopePolicy(current_scope)
            toolbox.scope_revision = current_scope.revision
        if run is not None:
            status = RunStatus(run.status)
            if status in {RunStatus.CANCELLING, RunStatus.CANCELLED}:
                raise GraphRunAborted("cancelled", detail="用户在一致边界请求取消")
            if status in {RunStatus.PAUSING, RunStatus.PAUSED}:
                raise GraphRunAborted("paused", detail="用户在一致边界请求暂停")

        # 3) **固定本轮的 control_version**。
        #
        # 这是"方向变更能拦住旧响应"的关键：把模型请求**发出之前**的版本记下来，
        # 派发工具时再和数据库当前值比较。用户在这期间改变方向 -> 版本变化 ->
        # 该响应里的工具一律不执行。
        # 同时写入图状态（可随 checkpoint 持久化）与 Toolbox（供工具内部再自查）。
        pinned = int(run.control_version) if run is not None else 0
        toolbox.control_version = pinned

        # 4) 消费一条待处理指令（高优先级优先）
        update = await self._consume_one(toolbox)
        if update is None:
            update = await self._replay_delivery(toolbox, dict(_state or {}))
        if isinstance(update, GraphRunAborted):
            # 消费记录已经提交后才打断图；在事务内抛异常会把 consumed 回滚。
            raise update
        merged: dict[str, Any] = {"dispatch_control_version": pinned}
        if update:
            merged.update(update)
        effective = {**dict(_state or {}), **merged}
        toolbox.execution_delegation_id = effective.get("execution_delegation_id")
        toolbox.execution_delegation_revision = effective.get("execution_delegation_revision")
        return merged

    @staticmethod
    def _event_update(command: Any) -> dict[str, Any]:
        payload = command.payload or {}
        return {
            "messages": [HumanMessage(
                content=command.content,
                id=f"exec-delivery-{payload['batch_id']}",
                additional_kwargs={"source": "execution_events", "untrusted_data": True},
            )],
            "execution_delivery_batch_id": str(payload["batch_id"]),
            "execution_delegation_id": payload.get("delegation_id"),
            "execution_delegation_revision": payload.get("delegation_revision"),
        }

    async def _replay_delivery(self, toolbox: Any, state: dict[str, Any]) -> dict[str, Any] | None:
        # 指令消费事务可能先于图写盘成功。仅重放有稳定 ID 的通知，不重放工具。
        async with toolbox.db.session() as db:
            rows = (await db.execute(
                select(ExecutionDeliveryBatchRow, CommandRow)
                .join(CommandRow, CommandRow.id == ExecutionDeliveryBatchRow.message_command_id)
                .where(
                    ExecutionDeliveryBatchRow.session_id == toolbox.session_id,
                    ExecutionDeliveryBatchRow.status == "injected",
                    CommandRow.status == str(CommandStatus.CONSUMED),
                )
            )).all()
            for batch, command in rows:
                if str(batch.id) != state.get("execution_delivery_batch_id"):
                    return self._event_update(command)
        return None

    async def _consume_one(self, toolbox: Any) -> dict[str, Any] | GraphRunAborted | None:
        async with toolbox.db.transaction() as db:
            repo = CommandRepository(db)
            pending = await repo.next_pending(toolbox.session_id)
            if pending is None:
                return None

            kind = CommandKind(pending.kind)
            if kind in {CommandKind.PAUSE, CommandKind.CANCEL}:
                await repo.mark_status(pending.id, CommandStatus.CONSUMED)
                return GraphRunAborted(
                    "paused" if kind is CommandKind.PAUSE else "cancelled",
                    detail="用户在一致边界请求暂停/取消",
                )

            if kind is CommandKind.SCOPE_CHANGE:
                await repo.mark_status(pending.id, CommandStatus.CONSUMED)
                if pending.payload.get("applied") is True:
                    revision = pending.payload.get("scope_revision")
                    return {
                        "messages": [
                            HumanMessage(
                                content=(
                                    f"【系统提示】用户已显式应用测试范围修订 {revision}。"
                                    f"变更理由：{pending.content}。"
                                    "请只按当前范围继续；旧方向任务结果需要重新核对。"
                                )
                            )
                        ],
                        "no_progress_streak": 0,
                        "last_progress_signature": "",
                    }
                # 兼容旧的未应用指令：绝不在图里悄悄改范围。
                await append_event(
                    db,
                    session_id=toolbox.session_id,
                    run_id=toolbox.run_id,
                    event_type=EventType.INPUT_REQUIRED,
                    payload={
                        "reason": "scope_change_requires_explicit_apply",
                        "command_id": str(pending.id),
                        "message": (
                            "收到范围变更请求，但范围只能通过显式变更接口应用"
                            "（需要重新校验并递增 scope_revision）。本次运行的范围未改变。"
                        ),
                    },
                )
                return {
                    "messages": [
                        HumanMessage(
                            content=(
                                "【系统提示】用户提交了范围变更请求。为保持范围一致性，"
                                "运行时范围**未**改变。请按当前范围继续；如需变更范围，"
                                "请提示用户通过项目设置显式修改。"
                            )
                        )
                    ]
                }

            # 普通补充消息：注入到下一个一致边界
            checkpoint_id = getattr(toolbox, "checkpoint_id", None)
            await repo.mark_consumed(pending.id, checkpoint_id=checkpoint_id)
            await append_event(
                db,
                session_id=toolbox.session_id,
                run_id=toolbox.run_id,
                event_type=EventType.MESSAGE_COMPLETED,
                payload={
                    "role": "system" if pending.payload.get("source") in {"execution_duty", "execution_events"} else "user",
                    "command_id": str(pending.id),
                    "consumed_at_boundary": "before_model",
                },
            )
            text = pending.content
            is_execution_event = pending.payload.get("source") in {
                "execution_duty", "execution_events"
            }
            if is_execution_event:
                return self._event_update(pending)
            if kind is CommandKind.RESUME:
                text = str(pending.payload.get("content") or text or "继续当前任务。")
            if pending.payload.get("constraints"):
                text = f"{text}\n\n【本次补充的约束】{pending.payload['constraints']}"
            return {
                "messages": [HumanMessage(content=text, id=f"command-{pending.id}",
                    additional_kwargs={"skill_refs": pending.payload.get("skill_refs") or []})],
                "execution_delegation_id": None,
                "execution_delegation_revision": None,
                "no_progress_streak": 0,
                "last_progress_signature": "",
            }


async def consume_all_pending(toolbox: Any) -> list[str]:
    """诊断用：返回当前仍待消费的指令 id（不改状态）。"""
    async with toolbox.db.session() as db:
        pending = await CommandRepository(db).list_pending(toolbox.session_id)
        return [str(item.id) for item in pending]
