"""执行端 SQLite 台账：版本化迁移与唯一 operation 约束（验收 A06/C06 的执行端部分）。

- 全新库迁移到 v2；
- 旧 v1 库（无 user_version + 旧唯一索引）可升级且保留旧 job；
- 历史重复 operation_id -> 明确报错、绝不删记录；
- operation_id 单列唯一：同 ID 不同 digest 在索引层就被拒绝。

风格与 `test_processes.py` 一致：同步测试 + `run()` 包装，避免额外引入 pytest-asyncio。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from workbench_executor.ledger import (
    SCHEMA_V1,
    SCHEMA_VERSION,
    Ledger,
    LedgerMigrationError,
    OperationConflict,
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _legacy_db(path: Path, *, duplicate_operation: bool = False) -> None:
    db = await aiosqlite.connect(path)
    await db.executescript(SCHEMA_V1)
    for index in range(2 if duplicate_operation else 1):
        await db.execute(
            "INSERT INTO jobs (job_id, operation_id, request_digest, kind, payload, state, created_at) "
            "VALUES (?, ?, ?, 'command', '{}', 'succeeded', '2026-10-01T00:00:00+00:00')",
            (f"legacy-{index}", "legacy-op", f"digest-{index}"),
        )
    await db.commit()
    await db.close()


def test_fresh_database_migrates_to_current_version(tmp_path: Path) -> None:
    async def scenario() -> int:
        ledger = Ledger(tmp_path / "ledger.sqlite3")
        await ledger.open()
        try:
            async with ledger.db.execute("PRAGMA user_version") as cursor:
                row = await cursor.fetchone()
            async with ledger.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='operations'"
            ) as cursor:
                assert await cursor.fetchone() is not None
            return int(row[0])
        finally:
            await ledger.close()

    assert run(scenario()) == SCHEMA_VERSION


def test_legacy_database_upgrades_and_keeps_rows(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "ledger.sqlite3"
        await _legacy_db(path)
        ledger = Ledger(path)
        await ledger.open()
        try:
            record = await ledger.get("legacy-0")
            assert record is not None
            assert record.runtime == "legacy"  # 历史记录保留 legacy 标识
            assert record.session_id == ""
            # 唯一索引已切换到 operation_id 单列（sqlite 完整性错误）
            with pytest.raises(aiosqlite.IntegrityError):
                await ledger.insert(
                    job_id="new-1",
                    operation_id="legacy-op",
                    request_digest="another",
                    kind="command",
                    payload={},
                    state="received",
                )
        finally:
            await ledger.close()

    run(scenario())


def test_duplicate_operation_history_refuses_migration(tmp_path: Path) -> None:
    async def scenario() -> str:
        path = tmp_path / "ledger.sqlite3"
        await _legacy_db(path, duplicate_operation=True)
        ledger = Ledger(path)
        with pytest.raises(LedgerMigrationError) as excinfo:
            await ledger.open()
        # 不删记录：旧数据仍在
        db = await aiosqlite.connect(path)
        try:
            async with db.execute("SELECT count(*) FROM jobs") as cursor:
                row = await cursor.fetchone()
        finally:
            await db.close()
        assert int(row[0]) == 2
        return str(excinfo.value)

    assert "legacy-op" in run(scenario())


def test_operation_identity_conflict_is_detected(tmp_path: Path) -> None:
    async def scenario() -> None:
        ledger = Ledger(tmp_path / "ledger.sqlite3")
        await ledger.open()
        try:
            await ledger.insert(
                job_id="job-1",
                operation_id="op-1",
                request_digest="digest-a",
                kind="command",
                payload={},
                state="received",
                runtime="execution_v1",
            )
            same = await ledger.find_by_operation("op-1", "digest-a")
            assert same is not None and same.job_id == "job-1"
            with pytest.raises(OperationConflict):
                await ledger.find_by_operation("op-1", "digest-b")
        finally:
            await ledger.close()

    run(scenario())


def test_budget_reservation_release_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        ledger = Ledger(tmp_path / "ledger.sqlite3")
        await ledger.open()
        try:
            await ledger.reserve_output_budget(
                resource_id="res-1", session_id="s-1", bytes_needed=100, session_budget=1000
            )
            assert await ledger.session_budget_used("s-1") == 100
            assert await ledger.release_output_budget("res-1") is True
            assert await ledger.release_output_budget("res-1") is False  # 重复释放无第二次效果
            assert await ledger.session_budget_used("s-1") == 0
            with pytest.raises(ValueError):
                await ledger.reserve_output_budget(
                    resource_id="res-2", session_id="s-2", bytes_needed=200, session_budget=100
                )
        finally:
            await ledger.close()

    run(scenario())


def test_terminal_input_sequence_is_persistent_and_deduplicated(tmp_path: Path) -> None:
    async def scenario() -> None:
        ledger = Ledger(tmp_path / "ledger.sqlite3")
        await ledger.open()
        try:
            await ledger.insert_terminal(
                terminal_id="t-1",
                session_id="s-1",
                run_id="r-1",
                operation_id="op-open",
                mode="interactive",
                argv_json='["/bin/bash"]',
                cwd="/tmp",
                state="running",
                log_dir=str(tmp_path / "logs"),
            )
            seq1, _receipt1, dedup1 = await ledger.allocate_input_seq(
                terminal_id="t-1", operation_id="op-send-1", request_digest="d1", receipt={"a": 1}
            )
            assert (seq1, dedup1) == (1, False)
            seq1b, _r1b, dedup1b = await ledger.allocate_input_seq(
                terminal_id="t-1", operation_id="op-send-1", request_digest="d1", receipt={"a": 1}
            )
            assert (seq1b, dedup1b) == (1, True)  # 同 ID 重试：返回原序号，不重复写入
            seq2, _r2, dedup2 = await ledger.allocate_input_seq(
                terminal_id="t-1", operation_id="op-send-2", request_digest="d2", receipt={}
            )
            assert (seq2, dedup2) == (2, False)
            # 输入回执只在写入完成后更新
            await ledger.mark_input_receipt("op-send-1", {"status": "written", "input_seq": 1})
            stored = await ledger.get_input_receipt("op-send-1")
            assert stored is not None and stored[1]["status"] == "written"
        finally:
            await ledger.close()

    run(scenario())


def test_events_survive_reopen_and_sequence_never_resets(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "ledger.sqlite3"
        ledger = Ledger(path)
        await ledger.open()
        await ledger.append_event(event_id="e-1", type="resource.started", session_id="s-1")
        first_seq = await ledger.max_event_seq()
        await ledger.close()
        # 重新打开（模拟服务重启）：序号不归零
        ledger = Ledger(path)
        await ledger.open()
        try:
            await ledger.append_event(event_id="e-2", type="resource.exited", session_id="s-1")
            assert await ledger.max_event_seq() > first_seq
            rows = await ledger.read_events(after=first_seq - 1, limit=10)
            assert [row["event_id"] for row in rows] == ["e-1", "e-2"]
        finally:
            await ledger.close()

    run(scenario())


def test_fence_sync_ordering(tmp_path: Path) -> None:
    async def scenario() -> None:
        ledger = Ledger(tmp_path / "ledger.sqlite3")
        await ledger.open()
        try:
            applied, view, _reason = await ledger.sync_fence(
                session_id="s-1",
                run_id="r-1",
                owner="runner",
                owner_generation=2,
                control_version=3,
                revocations=[],
                expected_seq=None,
            )
            assert applied and view["seq"] == 1
            # 过期 expected_seq：不应用，返回当前视图
            applied2, view2, reason2 = await ledger.sync_fence(
                session_id="s-1",
                run_id="r-1",
                owner="runner",
                owner_generation=2,
                control_version=4,
                revocations=[],
                expected_seq=0,
            )
            assert not applied2 and "expected_seq" in reason2 and view2["seq"] == 1
            # control_version 回退：拒绝把 fence 退回旧方向
            applied3, _view3, reason3 = await ledger.sync_fence(
                session_id="s-1",
                run_id="r-1",
                owner="runner",
                owner_generation=2,
                control_version=2,
                revocations=[],
                expected_seq=1,
            )
            assert not applied3 and "回退" in reason3
            # 正常前进 + 撤销合并
            applied4, view4, _ = await ledger.sync_fence(
                session_id="s-1",
                run_id="r-1",
                owner="runner",
                owner_generation=2,
                control_version=5,
                revocations=[{"delegation_id": "d-1", "revision": 2}],
                expected_seq=1,
            )
            assert applied4 and view4["seq"] == 2
            assert view4["revocations"] == [{"delegation_id": "d-1", "revision": 2}]
        finally:
            await ledger.close()

    run(scenario())
