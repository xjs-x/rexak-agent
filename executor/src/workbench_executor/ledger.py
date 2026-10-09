"""执行岛本地 SQLite 台账。

**用途仅限**：任务接收去重、重启核对（§5.1、P1 清单）。
**不充当第二套项目数据库**，控制端不直接访问该文件。

去重语义（§8.5）：
- 同 `operation_id` + 同 `request_digest` -> 返回原 `job_id`；
- 同 `operation_id` + 不同 digest -> 冲突。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;

CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    operation_id    TEXT NOT NULL,
    request_digest  TEXT NOT NULL,
    kind            TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}',
    state           TEXT NOT NULL,
    pid             INTEGER,
    pid_start_ticks INTEGER,
    worker_boot_id  TEXT,
    exit_code       INTEGER,
    byte_count      INTEGER NOT NULL DEFAULT 0,
    truncated       INTEGER NOT NULL DEFAULT 0,
    output_path     TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    last_heartbeat  TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    cancel_request_id TEXT,
    error_code      TEXT,
    error_message   TEXT,
    created_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_operation_digest
    ON jobs(operation_id, request_digest);
"""


class OperationConflict(RuntimeError):
    """同 operation_id 复用了不同参数。调用方应返回 409，**不得**重放。"""


@dataclass(slots=True)
class JobRecord:
    job_id: str
    operation_id: str
    request_digest: str
    kind: str
    payload: dict[str, Any]
    state: str
    pid: int | None
    pid_start_ticks: int | None
    worker_boot_id: str | None
    exit_code: int | None
    byte_count: int
    truncated: bool
    output_path: str | None
    started_at: str | None
    finished_at: str | None
    last_heartbeat: str | None
    cancel_requested: bool
    cancel_request_id: str | None
    error_code: str | None
    error_message: str | None
    created_at: str


def _row_to_record(row: aiosqlite.Row) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        operation_id=row["operation_id"],
        request_digest=row["request_digest"],
        kind=row["kind"],
        payload=json.loads(row["payload"] or "{}"),
        state=row["state"],
        pid=row["pid"],
        pid_start_ticks=row["pid_start_ticks"],
        worker_boot_id=row["worker_boot_id"],
        exit_code=row["exit_code"],
        byte_count=row["byte_count"],
        truncated=bool(row["truncated"]),
        output_path=row["output_path"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        last_heartbeat=row["last_heartbeat"],
        cancel_requested=bool(row["cancel_requested"]),
        cancel_request_id=row["cancel_request_id"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        created_at=row["created_at"],
    )


class Ledger:
    """SQLite 台账的唯一入口。所有写操作自带事务。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        if self._db is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self._path)
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA)
        await db.commit()
        self._db = db

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Ledger 未打开")
        return self._db

    async def find_by_operation(self, operation_id: str, request_digest: str) -> JobRecord | None:
        """按 operation_id 查找。

        - 命中且 digest 相同 -> 返回原 job（幂等重放）；
        - 命中且 digest 不同 -> 抛 `OperationConflict`。
        """
        async with self.db.execute(
            "SELECT * FROM jobs WHERE operation_id = ?", (operation_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise OperationConflict(
                f"operation_id={operation_id} 已绑定不同参数（原 digest="
                f"{row['request_digest'][:12]}…，新 digest={request_digest[:12]}…）"
            )
        return _row_to_record(row)

    async def get(self, job_id: str) -> JobRecord | None:
        async with self.db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)) as cursor:
            row = await cursor.fetchone()
        return _row_to_record(row) if row is not None else None

    async def insert(
        self,
        *,
        job_id: str,
        operation_id: str,
        request_digest: str,
        kind: str,
        payload: dict[str, Any],
        state: str,
    ) -> JobRecord:
        now = datetime.now(UTC).isoformat()
        await self.db.execute(
            """
            INSERT INTO jobs (job_id, operation_id, request_digest, kind, payload, state, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                operation_id,
                request_digest,
                kind,
                json.dumps(payload, ensure_ascii=False),
                state,
                now,
            ),
        )
        await self.db.commit()
        record = await self.get(job_id)
        assert record is not None
        return record

    async def update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "state",
            "pid",
            "pid_start_ticks",
            "worker_boot_id",
            "exit_code",
            "byte_count",
            "truncated",
            "output_path",
            "started_at",
            "finished_at",
            "last_heartbeat",
            "cancel_requested",
            "cancel_request_id",
            "error_code",
            "error_message",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"不允许更新台账字段：{sorted(unknown)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        await self.db.execute(
            f"UPDATE jobs SET {assignments} WHERE job_id = ?",
            (*values, job_id),
        )
        await self.db.commit()

    async def list_unfinished(self) -> list[JobRecord]:
        """重启核对用：所有非终态任务。"""
        async with self.db.execute(
            "SELECT * FROM jobs WHERE state IN ('received','starting','running') "
            "ORDER BY created_at"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_record(row) for row in rows]

    async def list_all(self) -> list[JobRecord]:
        async with self.db.execute("SELECT * FROM jobs ORDER BY created_at") as cursor:
            rows = await cursor.fetchall()
        return [_row_to_record(row) for row in rows]

    async def active_count(self) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE state IN ('received','starting','running')"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["n"]) if row is not None else 0
