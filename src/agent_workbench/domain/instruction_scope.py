"""把用户原文中的目标应用到任务；模型回复和工具内容不能调用此入口。"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.enums import CommandKind
from agent_workbench.contracts.events import EventType
from agent_workbench.contracts.identity import ScopeRule, ScopeSnapshot
from agent_workbench.domain.run_state import bump_control_version
from agent_workbench.domain.scope import (
    METADATA_HOSTS,
    ScopeViolation,
    parse_target,
    validate_scope,
)
from agent_workbench.observability.events import append_event
from agent_workbench.persistence.tables import CommandRow, RunRow, SessionRow

_TARGET = re.compile(
    r"https?://[^\s<>\"'`，。；）)]+|(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", re.I
)
_EXCLUSION = re.compile(r"不要|禁止|排除|不测试|不访问|例如|比如|仅作示例", re.I)


def instruction_hosts(texts: Iterable[str]) -> list[str]:
    hosts: dict[str, None] = {}
    for text in texts:
        for match in _TARGET.finditer(text):
            prefix = re.split(r"[\n，。；;]", text[: match.start()])[-1][-80:]
            if _EXCLUSION.search(prefix):
                continue
            raw = match.group().rstrip(".,;!?，。；！？”)}")
            try:
                target = parse_target(raw)
                if "://" not in raw:
                    ipaddress.ip_address(target.host)
                host = target.host.encode("idna").decode("ascii")
                if host in METADATA_HOSTS or any(c in host for c in "*?[/\\"):
                    continue
            except (ValueError, UnicodeError, ScopeViolation):
                continue
            hosts[host] = None
    return list(hosts)


def scope_from_instructions(scope: ScopeSnapshot, texts: Iterable[str]) -> ScopeSnapshot:
    if scope.mode != "instruction":
        return scope
    rules = list(scope.rules)
    for host in instruction_hosts(texts):
        if host in scope.denied_metadata_hosts:
            continue
        if any(rule.host == host and rule.scheme == "any" and rule.port is None for rule in rules):
            continue
        rules.append(
            ScopeRule(scheme="any", host=host, notes="来自用户目标指令；允许该主机的协议与端口探索")
        )
    updated = scope.model_copy(update={"rules": rules})
    validate_scope(updated)
    return updated


async def sync_instruction_scope(
    db: AsyncSession,
    *,
    session_id: UUID,
    run_id: UUID,
    content: str | None = None,
    control_changed: bool = False,
) -> ScopeSnapshot:
    task = (
        await db.execute(select(SessionRow).where(SessionRow.id == session_id).with_for_update())
    ).scalar_one()
    old = ScopeSnapshot.model_validate(task.scope_snapshot or {})
    if "mode" in (task.scope_snapshot or {}) and old.mode == "scoped":
        # 旧前端传 scope={}，曾被新版契约默认值存成 scoped，并非用户选择严格模式。
        origin = (
            await db.execute(
                select(RunRow)
                .where(RunRow.session_id == session_id)
                .order_by(RunRow.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        config = origin.config_snapshot if origin is not None else {}
        explicit_update = (
            await db.execute(
                select(CommandRow.id)
                .where(
                    CommandRow.session_id == session_id,
                    CommandRow.kind == str(CommandKind.SCOPE_CHANGE),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if (
            old.rules
            or config.get("scope_mode_explicit")
            or explicit_update is not None
            or not (
                config.get("created_from_session_create") or config.get("created_from") == "message"
            )
        ):
            return old
    # 兼容既有任务：只从用户目标和 message 指令恢复，绝不扫描 events / 工具工件。
    scope = old.model_copy(update={"mode": "instruction"})
    if content is not None:
        texts = [content]
    else:
        texts = list(
            (
                await db.execute(select(RunRow.objective).where(RunRow.session_id == session_id))
            ).scalars()
        )
        commands = (
            await db.execute(
                select(CommandRow).where(
                    CommandRow.session_id == session_id,
                    CommandRow.kind.in_([str(CommandKind.MESSAGE), str(CommandKind.RESUME)]),
                )
            )
        ).scalars()
        texts.extend(
            command.content
            if command.kind == str(CommandKind.MESSAGE)
            else str(command.payload.get("content") or "")
            for command in commands
        )
    updated = scope_from_instructions(scope, texts)
    if updated == old:
        return old
    updated = updated.model_copy(update={"revision": old.revision + 1})
    task.scope_snapshot = updated.model_dump(mode="json")
    task.revision = int(task.revision) + 1
    if control_changed:
        run = await db.get(RunRow, run_id)
        if run is None:
            raise RuntimeError("应用用户目标时运行记录不存在")
        version = int(run.control_version)
    else:
        version = await bump_control_version(db, run_id=run_id)
    await append_event(
        db,
        session_id=session_id,
        run_id=run_id,
        event_type=EventType.RUN_STATUS,
        payload={
            "event": "instruction_targets_applied",
            "mode": "instruction",
            "scope_revision": updated.revision,
            "control_version": version,
            "hosts": [rule.host for rule in updated.rules],
            "note": "用户指令中的目标已自动应用，无需手动设置 scope。",
        },
    )
    await db.flush()
    return updated
