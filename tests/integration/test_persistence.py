"""P1：持久化、幂等、事件序号与单一活动 run 约束。

对应 ACCEPTANCE A04（重复/并发发消息）、A26（SSE 重连/乱序）与 P1 验证清单。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from agent_workbench.contracts.enums import (
    CommandKind,
    ErrorCode,
    RunStatus,
    ToolEffectClass,
)
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.events import EventEnvelope, EventType
from agent_workbench.contracts.identity import PolicySnapshot, ScopeRule, ScopeSnapshot
from agent_workbench.domain.run_state import (
    claim_run,
    create_run,
    find_active_run,
    is_current_owner,
    set_run_status,
)
from agent_workbench.observability.events import (
    append_event,
    latest_seq,
    min_available_seq,
    read_events_after,
)
from agent_workbench.persistence.repositories import (
    CommandRepository,
    ProjectRepository,
    SessionRepository,
)
from agent_workbench.persistence.tables import RunRow

pytestmark = pytest.mark.integration


async def _make_session(database, name: str):
    scope = ScopeSnapshot(revision=1, rules=[ScopeRule(scheme="http", host="127.0.0.1", port=8899)])
    async with database.transaction() as session:
        project = await ProjectRepository(session).create(
            owner_id="test-owner",
            name=name,
            target_description="测试目标",
            default_scope=scope.model_dump(),
        )
        return await SessionRepository(session).create(
            project_id=project.id,
            title="测试会话",
            scope_snapshot=scope.model_dump(),
            policy_snapshot=PolicySnapshot().model_dump(),
        )


# --------------------------------------------------------------------------- #
# 幂等提交（A04）
# --------------------------------------------------------------------------- #


async def test_same_idempotency_key_same_content_returns_original(database, unique_name) -> None:
    session_row = await _make_session(database, unique_name)
    key = f"key-{uuid.uuid4().hex}"

    async with database.transaction() as session:
        first = await CommandRepository(session).submit(
            session_id=session_row.id,
            run_id=None,
            kind=CommandKind.MESSAGE,
            content="同样的话",
            idempotency_key=key,
        )
    async with database.transaction() as session:
        second = await CommandRepository(session).submit(
            session_id=session_row.id,
            run_id=None,
            kind=CommandKind.MESSAGE,
            content="同样的话",
            idempotency_key=key,
        )

    assert first.created is True
    assert second.created is False
    assert first.row.id == second.row.id, "同键同内容必须返回原指令，不能新建"


async def test_same_idempotency_key_different_content_conflicts(database, unique_name) -> None:
    session_row = await _make_session(database, unique_name)
    key = f"key-{uuid.uuid4().hex}"

    async with database.transaction() as session:
        await CommandRepository(session).submit(
            session_id=session_row.id,
            run_id=None,
            kind=CommandKind.MESSAGE,
            content="原始内容",
            idempotency_key=key,
        )

    with pytest.raises(ApiError) as excinfo:
        async with database.transaction() as session:
            await CommandRepository(session).submit(
                session_id=session_row.id,
                run_id=None,
                kind=CommandKind.MESSAGE,
                content="不同内容",
                idempotency_key=key,
            )
    assert excinfo.value.code is ErrorCode.IDEMPOTENCY_KEY_REUSED


async def test_concurrent_same_key_only_creates_one(database, unique_name) -> None:
    """并发同键提交不能产生第二条指令（数据库唯一约束兜底）。"""
    session_row = await _make_session(database, unique_name)
    key = f"key-{uuid.uuid4().hex}"

    async def submit() -> bool:
        try:
            async with database.transaction() as session:
                result = await CommandRepository(session).submit(
                    session_id=session_row.id,
                    run_id=None,
                    kind=CommandKind.MESSAGE,
                    content="并发内容",
                    idempotency_key=key,
                )
            return result.created
        except ApiError:
            return False

    results = await asyncio.gather(*(submit() for _ in range(5)))
    assert sum(1 for created in results if created) == 1


# --------------------------------------------------------------------------- #
# 单一活动 run（A08）
# --------------------------------------------------------------------------- #


async def test_only_one_active_run_per_session(database, unique_name) -> None:
    session_row = await _make_session(database, unique_name)
    async with database.transaction() as session:
        await create_run(
            session,
            session_id=session_row.id,
            objective="第一段",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            config_snapshot={},
        )

    with pytest.raises(ApiError) as excinfo:
        async with database.transaction() as session:
            await create_run(
                session,
                session_id=session_row.id,
                objective="第二段",
                provider_id="xiaomi",
                model_name="mimo-v2.6-pro",
                config_snapshot={},
            )
    assert excinfo.value.code is ErrorCode.RUN_ALREADY_ACTIVE


async def test_completed_run_allows_next_run_same_thread(database, unique_name) -> None:
    """阶段结束后可以在同一 session/thread 上开新 run（§4.2）。"""
    session_row = await _make_session(database, unique_name)
    async with database.transaction() as session:
        first = await create_run(
            session,
            session_id=session_row.id,
            objective="第一阶段",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            config_snapshot={},
        )
        await set_run_status(session, run_id=first.id, target=RunStatus.CANCELLED)

    async with database.transaction() as session:
        second = await create_run(
            session,
            session_id=session_row.id,
            objective="第二阶段",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            config_snapshot={},
        )
    assert second.id != first.id

    async with database.session() as session:
        current = await find_active_run(session, session_row.id)
    assert current is not None and current.id == second.id


# --------------------------------------------------------------------------- #
# 运行所有权（A08）
# --------------------------------------------------------------------------- #


async def test_lease_conflict_blocks_second_runner(database, unique_name) -> None:
    session_row = await _make_session(database, unique_name)
    async with database.transaction() as session:
        run = await create_run(
            session,
            session_id=session_row.id,
            objective="x",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            config_snapshot={},
        )

    async with database.transaction() as session:
        acquired_a, gen_a = await claim_run(
            session, run_id=run.id, owner="runner-a", lease_seconds=120
        )
    async with database.transaction() as session:
        acquired_b, gen_b = await claim_run(
            session, run_id=run.id, owner="runner-b", lease_seconds=120
        )

    assert acquired_a is True
    assert acquired_b is False, "有效 lease 未过期时第二个 Runner 不得取得所有权"
    assert gen_b == gen_a

    async with database.session() as session:
        assert await is_current_owner(session, run_id=run.id, owner="runner-a", generation=gen_a)
        assert not await is_current_owner(
            session, run_id=run.id, owner="runner-b", generation=gen_b
        )


async def test_expired_lease_can_be_reclaimed(database, unique_name) -> None:
    session_row = await _make_session(database, unique_name)
    async with database.transaction() as session:
        run = await create_run(
            session,
            session_id=session_row.id,
            objective="x",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            config_snapshot={},
        )
    async with database.transaction() as session:
        await claim_run(session, run_id=run.id, owner="runner-a", lease_seconds=1)

    await asyncio.sleep(1.5)

    async with database.transaction() as session:
        acquired, generation = await claim_run(
            session, run_id=run.id, owner="runner-b", lease_seconds=60
        )
    assert acquired is True, "lease 过期后应可被接管"
    assert generation >= 1


# --------------------------------------------------------------------------- #
# 事件序号与游标（A26）
# --------------------------------------------------------------------------- #


async def test_event_seq_is_contiguous_and_ordered(database, unique_name) -> None:
    session_row = await _make_session(database, unique_name)
    for index in range(5):
        async with database.transaction() as session:
            await append_event(
                session,
                session_id=session_row.id,
                event_type=EventType.MESSAGE_DELTA,
                payload={"index": index},
            )

    async with database.session() as session:
        latest = await latest_seq(session, session_row.id)
        earliest = await min_available_seq(session, session_row.id)
        events = await read_events_after(session, session_id=session_row.id, after_seq=0)

    assert latest == 5
    assert earliest == 1
    assert [item.session_seq for item in events] == [1, 2, 3, 4, 5]


async def test_cursor_resume_does_not_skip_uncommitted_events(database, unique_name) -> None:
    """模拟两个事务交错：后开始的事务先提交，序号更大；游标按 seq 续读不漏事件。"""
    session_row = await _make_session(database, unique_name)

    async with database.transaction() as session:
        first = await append_event(
            session, session_id=session_row.id, event_type=EventType.RUN_STATUS, payload={"n": 1}
        )

    async with database.transaction() as session:
        second = await append_event(
            session, session_id=session_row.id, event_type=EventType.RUN_STATUS, payload={"n": 2}
        )
    async with database.transaction() as session:
        third = await append_event(
            session, session_id=session_row.id, event_type=EventType.RUN_STATUS, payload={"n": 3}
        )

    assert first.session_seq < second.session_seq < third.session_seq

    async with database.session() as session:
        page = await read_events_after(
            session, session_id=session_row.id, after_seq=first.session_seq
        )
    assert [item.payload["n"] for item in page] == [2, 3]


async def test_event_envelope_roundtrip_and_sse_frame(database, unique_name) -> None:
    session_row = await _make_session(database, unique_name)
    async with database.transaction() as session:
        event = await append_event(
            session,
            session_id=session_row.id,
            event_type=EventType.TOOL_OUTPUT,
            payload={"job_id": str(uuid.uuid4()), "preview": "文本片段"},
        )

    restored = EventEnvelope.model_validate_json(event.model_dump_json())
    assert restored.session_seq == event.session_seq
    frame = event.to_sse()
    assert frame.startswith(f"id: {event.event_id}")
    assert "event: tool.output" in frame
    assert frame.endswith("\n\n")


async def test_events_are_isolated_per_session(database, unique_name) -> None:
    session_a = await _make_session(database, f"{unique_name}-a")
    session_b = await _make_session(database, f"{unique_name}-b")

    async with database.transaction() as session:
        await append_event(
            session, session_id=session_a.id, event_type=EventType.RUN_STATUS, payload={"s": "a"}
        )
    async with database.transaction() as session:
        await append_event(
            session, session_id=session_b.id, event_type=EventType.RUN_STATUS, payload={"s": "b"}
        )

    async with database.session() as session:
        events_a = await read_events_after(session, session_id=session_a.id)
    assert len(events_a) == 1
    assert events_a[0].payload["s"] == "a"


# --------------------------------------------------------------------------- #
# 工件原子落盘与孤儿检测
# --------------------------------------------------------------------------- #


def test_artifact_write_is_content_addressed_and_atomic(settings) -> None:
    from agent_workbench.evidence.artifacts import ArtifactStore

    store = ArtifactStore(settings)
    payload = "响应体示例".encode()
    first = store.write_bytes(payload, media_type="text/plain")
    second = store.write_bytes(payload, media_type="text/plain")

    assert first.sha256 == second.sha256
    assert first.relative_key == second.relative_key, "相同内容应内容寻址到同一键"
    assert store.verify(first.relative_key, first.sha256)
    assert store.read_bytes(first.relative_key) == payload


def test_artifact_verifies_hash_and_rejects_tampering(settings) -> None:
    from agent_workbench.evidence.artifacts import ArtifactStore

    store = ArtifactStore(settings)
    stored = store.write_bytes(b"original", media_type="text/plain")
    assert store.verify(stored.relative_key, stored.sha256)
    assert not store.verify(stored.relative_key, "0" * 64), "哈希不符必须判为不可用证据"


def test_artifact_rejects_path_escape(settings) -> None:
    from agent_workbench.evidence.artifacts import ArtifactError, ArtifactStore

    store = ArtifactStore(settings)
    with pytest.raises(ArtifactError):
        store.resolve("../../etc/passwd")


def test_artifact_invalid_utf8_is_reported_not_guessed(settings) -> None:
    from agent_workbench.evidence.artifacts import ArtifactStore

    store = ArtifactStore(settings)
    stored = store.write_bytes(b"\xff\xfe\x00\x01", media_type="text/plain")
    view = store.render_redacted_view(stored.relative_key, media_type="text/plain")
    assert "编码问题" in view
    assert "未做猜测性解码" in view


def test_orphan_detection_flags_missing_file(settings) -> None:
    """元数据存在但文件缺失 -> 必须能被识别为孤儿，而不是当作可用证据。"""
    from agent_workbench.evidence.artifacts import ArtifactStore

    store = ArtifactStore(settings)
    stored = store.write_bytes(b"will be removed", media_type="text/plain")
    (store.root / stored.relative_key).unlink()
    assert not store.exists(stored.relative_key)


# --------------------------------------------------------------------------- #
# 工具操作身份（A14）
# --------------------------------------------------------------------------- #


async def test_same_operation_id_with_different_digest_conflicts(database, unique_name) -> None:
    from agent_workbench.persistence.repositories import ToolOperationRepository

    session_row = await _make_session(database, unique_name)
    async with database.transaction() as session:
        run = await create_run(
            session,
            session_id=session_row.id,
            objective="x",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            config_snapshot={},
        )

    operation_id = uuid.uuid4()
    async with database.transaction() as session:
        await ToolOperationRepository(session).prepare(
            operation_id=operation_id,
            run_id=run.id,
            session_id=session_row.id,
            tool_call_id="call-1",
            tool_name="http_request",
            request_digest="digest-a",
            effect_class=str(ToolEffectClass.READ_ONLY),
            policy_revision=1,
            scope_revision=1,
        )

    with pytest.raises(ApiError) as excinfo:
        async with database.transaction() as session:
            await ToolOperationRepository(session).prepare(
                operation_id=operation_id,
                run_id=run.id,
                session_id=session_row.id,
                tool_call_id="call-2",
                tool_name="http_request",
                request_digest="digest-b",
                effect_class=str(ToolEffectClass.READ_ONLY),
                policy_revision=1,
                scope_revision=1,
            )
    assert excinfo.value.code is ErrorCode.OPERATION_CONFLICT


async def test_replay_reuses_operation_id_by_tool_call_id(database, unique_name) -> None:
    """图重放时按 (run_id, tool_call_id) 找回同一 operation，不会产生第二笔副作用。"""
    from agent_workbench.tools.operation import OperationRegistry

    session_row = await _make_session(database, unique_name)
    async with database.transaction() as session:
        run = await create_run(
            session,
            session_id=session_row.id,
            objective="x",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            config_snapshot={},
        )

    arguments = {"url": "http://127.0.0.1:8899/", "method": "GET"}

    async with database.transaction() as session:
        first = await OperationRegistry(session).prepare(
            run_id=run.id,
            session_id=session_row.id,
            tool_call_id="call-replay",
            tool_name="http_request",
            arguments=arguments,
            effect_class=ToolEffectClass.READ_ONLY,
            policy_revision=1,
            scope_revision=1,
        )
    async with database.transaction() as session:
        second = await OperationRegistry(session).prepare(
            run_id=run.id,
            session_id=session_row.id,
            tool_call_id="call-replay",
            tool_name="http_request",
            arguments=arguments,
            effect_class=ToolEffectClass.READ_ONLY,
            policy_revision=1,
            scope_revision=1,
        )

    assert first.replayed is False
    assert second.replayed is True
    assert first.operation_id == second.operation_id


async def test_explicit_retest_gets_new_operation_id(database, unique_name) -> None:
    """显式"再测一次"用新的 tool_call_id -> 新的逻辑操作 ID，不被历史去重误认为重放。"""
    from agent_workbench.tools.operation import OperationRegistry

    session_row = await _make_session(database, unique_name)
    async with database.transaction() as session:
        run = await create_run(
            session,
            session_id=session_row.id,
            objective="x",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            config_snapshot={},
        )

    arguments = {"url": "http://127.0.0.1:8899/", "method": "GET"}
    ids = []
    for call_id in ("call-1", "call-2"):
        async with database.transaction() as session:
            prepared = await OperationRegistry(session).prepare(
                run_id=run.id,
                session_id=session_row.id,
                tool_call_id=call_id,
                tool_name="http_request",
                arguments=arguments,
                effect_class=ToolEffectClass.READ_ONLY,
                policy_revision=1,
                scope_revision=1,
            )
        ids.append(prepared.operation_id)

    assert ids[0] != ids[1]


async def test_run_row_is_persisted_with_config_snapshot(database, unique_name) -> None:
    session_row = await _make_session(database, unique_name)
    async with database.transaction() as session:
        run = await create_run(
            session,
            session_id=session_row.id,
            objective="快照测试",
            provider_id="xiaomi",
            model_name="mimo-v2.6-pro",
            config_snapshot={"scope_revision": 3, "note": "配置快照随 run 固定"},
        )
    async with database.session() as session:
        row = (await session.execute(select(RunRow).where(RunRow.id == run.id))).scalar_one()
    assert row.config_snapshot["scope_revision"] == 3
    assert row.status == str(RunStatus.QUEUED)
