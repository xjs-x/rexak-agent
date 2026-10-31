"""请求边界适配：验证已进入请求的用户输入，再重建本轮只读板。"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import select, text

from agent_workbench.agent.signals import GraphRunAborted, StaleResponseError
from agent_workbench.contracts.decision_memory import DecisionBlackboard, DecisionInput
from agent_workbench.contracts.planning import planning_digest
from agent_workbench.domain.decision_memory import (
    explicit_refs,
    read_snapshot,
    select_memory,
    valid_uuid,
)
from agent_workbench.persistence.tables import CommandRow, IdentityRow, RunRow, SessionRow


def verified_inputs(
    messages: list[Any], run: Any, commands: dict[str, Any]
) -> tuple[list[dict[str, Any]], int]:
    inputs, last = [], -1
    for index, message in enumerate(messages):
        if not isinstance(message, HumanMessage):
            continue
        identifier = str(message.id or "")
        if identifier == f"objective-{run.id}" and message.content == run.objective:
            inputs.append({"record_ref": f"run:{run.id}:objective", "text": run.objective})
            last = index
        elif identifier.startswith("command-"):
            command = commands.get(identifier[8:])
            if (
                command is None
                or command.status != "consumed"
                or command.payload.get("source") in {"execution_duty", "execution_events"}
                or command.kind not in {"message", "resume"}
            ):
                continue
            content = command.content
            if command.kind == "resume":
                content = str(command.payload.get("content") or content or "继续当前任务。")
                # 纯控制恢复不被解释为方向切换。
                if not command.payload.get("content") and not command.content:
                    continue
            if command.payload.get("constraints"):
                content += f"\n\n【本次补充的约束】{command.payload['constraints']}"
            if message.content != content:
                continue
            inputs.append({"record_ref": f"command:{command.id}", "text": content})
            last = index
    return inputs, last


def focus_freshness(
    state: dict[str, Any], messages: list[Any], inputs: list[dict[str, Any]], last: int, run_id: str
) -> str:
    change = state.get("last_plan_change") or {}
    for message in messages[last + 1 :] if last >= 0 else []:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls:
            args = call.get("args") or {}
            if call["name"] != "update_plan" or "focus" not in args or args["focus"] is None:
                continue
            if change.get("source_tool_call_id") != call["id"] or change.get("run_id") != run_id:
                continue
            expressed = args["focus"]
            current = state.get("current_focus")
            if not isinstance(expressed, str) or expressed.strip() != (current or {}).get(
                "intent", ""
            ):
                continue
            for response in messages[messages.index(message) + 1 :]:
                if not isinstance(response, ToolMessage) or response.tool_call_id != call["id"]:
                    continue
                try:
                    receipt = json.loads(str(response.content))
                except ValueError:
                    continue
                if (
                    isinstance(receipt, dict)
                    and receipt.get("ok") is True
                    and (
                        receipt.get("current_focus") == state.get("current_focus")
                        and receipt.get("plan_revision") == state.get("plan_revision")
                    )
                ):
                    return "reexpressed_after_user_input"
    if (
        inputs
        and all(i["record_ref"].startswith("run:") for i in inputs)
        and (not state.get("current_focus") or change.get("run_id") == run_id)
    ):
        return "initial_input"
    return "unproven_after_user_input" if inputs else "unproven"


async def build_blackboard(
    box: Any, state: dict[str, Any], messages: list[Any]
) -> tuple[DecisionBlackboard, dict[str, Any]]:
    """一次一致只读快照；板不缓存、不写 checkpoint，不消费排队输入。"""
    if state.get("session_id") not in {None, str(box.session_id)} or state.get(
        "active_run_id"
    ) not in {None, str(box.run_id)}:
        raise ValueError("decision_memory_request_boundary_mismatch")
    pinned = state.get("dispatch_control_version")
    if pinned is not None and pinned != box.control_version:
        raise StaleResponseError(pinned_version=pinned, current_version=box.control_version)
    await box.assert_control_current()
    async with box.db.session() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        run = await db.get(RunRow, box.run_id)
        if run is None or run.session_id != box.session_id:
            raise ValueError("decision_memory_run_unavailable")
        if run.control_version != box.control_version:
            raise StaleResponseError(
                pinned_version=box.control_version, current_version=run.control_version
            )
        command_ids = [
            str(m.id)[8:]
            for m in messages
            if isinstance(m, HumanMessage)
            and str(m.id or "").startswith("command-")
            and valid_uuid(str(m.id)[8:])
        ]
        commands = {
            str(c.id): c
            for c in (
                await db.scalars(
                    select(CommandRow).where(
                        CommandRow.session_id == box.session_id,
                        CommandRow.id.in_([UUID(i) for i in command_ids]),
                    )
                )
            ).all()
        }
        inputs, last = verified_inputs(messages, run, commands)
        request = DecisionInput(
            session_id=str(box.session_id),
            run_id=str(box.run_id),
            control_version=box.control_version,
            state=state,
            user_inputs=inputs,
            focus_freshness=focus_freshness(state, messages, inputs, last, str(run.id)),
            explicit_refs=tuple(sorted(explicit_refs(inputs))),
            executor_id=getattr(box.settings, "executor_id", None),
        )
        data = await read_snapshot(db, request, box.artifacts)
        board, trace = select_memory(request, data)
    # READ COMMITTED 新连接复核；不把 RR 的旧快照当成最新控制/授权。
    await box.assert_control_current()
    async with box.db.session() as db:
        task = await db.get(SessionRow, box.session_id)
        run = await db.get(RunRow, box.run_id)
        if run is None or task is None:
            raise ValueError("decision_memory_boundary_unavailable")
        if run.status in {"pausing", "paused", "cancelling", "cancelled"}:
            raise GraphRunAborted("paused" if run.status.startswith("paus") else "cancelled")
        if run.control_version != box.control_version or (
            planning_digest(task.scope_snapshot) != data.boundary["scope_signature"]
            or (task.policy_snapshot or {}).get("revision") != data.boundary["policy_revision"]
            or (str(task.active_identity_id) if task.active_identity_id else None)
            != data.boundary["active_identity_id"]
        ):
            raise StaleResponseError(
                pinned_version=box.control_version, current_version=run.control_version
            )
        used_identities = {
            unit.observed_dimensions.get("identity_id") for unit in board.relevant_evidence
        }
        used_identities.add(data.boundary["active_identity_id"])
        expected_auth = {
            key: value for key, value in data.identity_revisions.items() if key in used_identities
        }
        current_auth = {
            str(i): revision
            for i, revision in (
                await db.execute(
                    select(IdentityRow.id, IdentityRow.auth_revision).where(
                        ((IdentityRow.session_id == task.id) | (IdentityRow.id == task.active_identity_id)),
                        IdentityRow.id.in_([UUID(key) for key in expected_auth]),
                    )
                )
            ).all()
        }
        if current_auth != expected_auth:
            raise StaleResponseError(
                pinned_version=box.control_version, current_version=run.control_version
            )
    return board, trace
