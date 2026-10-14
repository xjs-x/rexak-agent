"""决策前刷新只读领域投影；实际展示后才推进 checkpoint 召回与弱点消费游标。

本中间件同时负责**焦点自动承接**（§5.2）：在装配前用与 `update_plan` 相同的纯函数
解析焦点，并用同一解析结果同时构造只读黑板与返回图更新 —— 避免两处推导出不同焦点。

V3（方案 §5.3）：区分"保存 / 呈现 / 判断 / 执行"四件事。
`aafter_model` 只在**模型调用真的发生**之后，按 context manifest 里记录的实际发送内容
更新呈现游标与召回去重集合；供应商失败、恢复尝试、压缩与取消都不会推进它们。
"""

from typing import Any, cast

from langchain.agents.middleware import AgentMiddleware

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.focus import focus_signature, resolve_focus
from agent_workbench.agent.state import AgentGraphState, merge_unique
from agent_workbench.agent.working_context import assemble_working_context
from agent_workbench.contracts.events import EventType
from agent_workbench.observability.events import append_event

#: 召回去重键的保留上限（防止 checkpoint 无限增长）。
MAX_RECALL_KEYS = 60


class WorkingContextMiddleware(AgentMiddleware[AgentGraphState, RunContext]):
    async def abefore_model(self, state, runtime):  # type: ignore[no-untyped-def]
        context = getattr(runtime, "context", None)
        if context is None:
            return None
        values = cast("dict[str, Any]", dict(state or {}))
        # 没有显式选择时，按"唯一进行中步骤"承接焦点；多主线时不乱猜。
        focus, hint = resolve_focus(
            values.get("plan"), values.get("current_focus"), explicit_focus=None
        )
        changed = focus_signature(focus) != focus_signature(values.get("current_focus"))
        values["current_focus"] = focus
        identity = getattr(context.toolbox, "identity_id", None)
        values["active_identity_id"] = str(identity) if identity is not None else None
        async with context.toolbox.db.session() as db:
            view = await assemble_working_context(db, context.toolbox.session_id, values)
        context.toolbox.working_context = view
        update: dict[str, object] = {
            "active_lead_ids": view["next"]["selected_ids"],
            "pending_job_ids": view["pending_job_ids"],
        }
        if changed:
            update["current_focus"] = focus
        if hint and not values.get("progress_feedback"):
            # 只给一次简短提示；不覆盖进度中间件已经给出的策略反馈。
            update["progress_feedback"] = hint
        return update

    async def aafter_model(self, state, runtime):  # type: ignore[no-untyped-def]
        _ = state
        context = getattr(runtime, "context", None)
        if context is None:
            return None
        manifest = context.toolbox.context_manifest or {}
        view = manifest.get("working_context") or {}
        selected = view.get("next") or {}
        consumption = view.get("weakness_consumption") or {}
        commit = dict(selected.get("consumption_commit") or {})
        update: dict[str, object] = {}
        if selected.get("review_due"):
            update["review_cursor"] = selected.get("cursor_after_selection")
            update["last_review_turn"] = selected.get("turn", 0)
            update["last_review_source_signature"] = selected.get("source_signature")
        # 呈现记录：只写**实际进入最终 payload**的弱点 ID（budget 裁剪掉的条目不算呈现）。
        presented = manifest.get("presented_weakness_ids")
        if presented is None:
            presented = commit.get("presented_weakness_ids") or []
            # 未经预算裁剪时仍以最终 payload 中的对象为准。
            presented = [
                str(item["weakness_id"])
                for item in consumption.get("changed_weaknesses") or []
                if item.get("weakness_id")
            ]
        if presented:
            update["presented_weakness_ids"] = merge_unique(
                [str(item) for item in state.get("presented_weakness_ids") or []] if state else [],
                [str(item) for item in presented],
            )[-200:]
        recall_keys = [
            str(item["dedupe_key"])
            for item in (consumption.get("related_leads") or [])
            if item.get("dedupe_key")
        ]
        if recall_keys:
            existing = [str(item) for item in (state.get("last_recall_keys") or [])] if state else []
            update["last_recall_keys"] = merge_unique(existing, recall_keys)[-MAX_RECALL_KEYS:]
        if commit.get("weakness_revisions"):
            revisions = dict(state.get("last_weakness_revisions") or {}) if state else {}
            actual_ids = {str(item) for item in presented or []}
            revisions.update({
                str(key): int(value) for key, value in commit["weakness_revisions"].items() if str(key) in actual_ids
            })
            update["last_weakness_revisions"] = revisions
        if commit.get("identity_signature") is not None and any(item.get("trigger") == "identity_changed" for item in consumption.get("related_leads") or []):
            update["last_identity_signature"] = str(commit.get("identity_signature") or "")
        if presented or recall_keys:
            messages = state.get("messages") or []
            last = messages[-1] if messages else None
            metadata = getattr(last, "response_metadata", {}) or {}
            async with context.toolbox.db.transaction() as db:
                for key in presented or []:
                    await append_event(db, session_id=context.toolbox.session_id, run_id=context.toolbox.run_id,
                        event_type=EventType.WEAKNESS_PRESENTED,
                        payload={"weakness_id": str(key), "revision": (commit.get("weakness_revisions") or {}).get(str(key)),
                            "model_call_id": metadata.get("workbench_model_call_id"), "message_id": getattr(last, "id", None)})
                for item in consumption.get("related_leads") or []:
                    if item.get("dedupe_key"):
                        await append_event(db, session_id=context.toolbox.session_id, run_id=context.toolbox.run_id,
                            event_type=EventType.CONDITION_REVIEW_DUE,
                            payload={key: item.get(key) for key in ("lead_id", "revision", "trigger", "dedupe_key", "relation_ids")})
        return update or None
