"""执行岛本地 SQLite 台账（版本化、事务式、可重复运行迁移）。

**用途仅限**：任务接收去重、资源/操作/终端/事件/控制 fence 的持久记录、
重启核对（§5.1、§12.1）。**不充当第二套项目数据库**，控制端不直接访问该文件。

迁移策略（规格 §12.1）：
- `PRAGMA user_version` 记录 schema 版本，逐级迁移、可重复运行；
- v1 = 旧版（jobs 表 + (operation_id, request_digest) 唯一索引）；
- v2 = 执行底座：jobs 扩展列、operations / terminals / terminal_inputs /
  execution_events / session_fences / budget_reservations；
- **升级前检查历史 operation_id 冲突**：发现歧义直接报告，绝不删除旧记录强行加索引；
- 历史无 session 的记录保留 `legacy` 标识，不允许模型凭猜测 ID 接管。

去重语义（§8.5）：
- 同 `operation_id` + 同 `request_digest` -> 返回原 job；
- 同 `operation_id` + 不同 digest -> 冲突。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import aiosqlite

SCHEMA_VERSION = 2

#: v1 的原始结构（仅用于全新数据库的第一次建表；对旧库不执行）。
SCHEMA_V1 = """
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

#: v2 新增表（对任何版本都幂等执行）。
SCHEMA_V2_TABLES = """
CREATE TABLE IF NOT EXISTS operations (
    operation_id              TEXT PRIMARY KEY,
    session_id                TEXT NOT NULL DEFAULT '',
    run_id                    TEXT NOT NULL DEFAULT '',
    tool_call_id              TEXT NOT NULL DEFAULT '',
    request_digest            TEXT NOT NULL,
    action                    TEXT NOT NULL,
    resource_id               TEXT,
    status                    TEXT NOT NULL DEFAULT 'accepted',
    owner_generation          INTEGER NOT NULL DEFAULT 0,
    dispatch_control_version  INTEGER NOT NULL DEFAULT 0,
    session_control_seq       INTEGER NOT NULL DEFAULT 0,
    delegation_id             TEXT,
    delegation_revision       INTEGER,
    receipt_json              TEXT NOT NULL DEFAULT '{}',
    accepted_at               TEXT NOT NULL,
    executed_at               TEXT
);
CREATE INDEX IF NOT EXISTS ix_operations_session ON operations(session_id, accepted_at);
CREATE INDEX IF NOT EXISTS ix_operations_resource ON operations(resource_id);

CREATE TABLE IF NOT EXISTS terminals (
    terminal_id          TEXT PRIMARY KEY,
    session_id           TEXT NOT NULL DEFAULT '',
    run_id               TEXT NOT NULL DEFAULT '',
    operation_id         TEXT,
    mode                 TEXT NOT NULL DEFAULT 'interactive',
    argv_json            TEXT NOT NULL DEFAULT '[]',
    cwd                  TEXT NOT NULL DEFAULT '',
    state                TEXT NOT NULL DEFAULT 'starting',
    interaction_state    TEXT NOT NULL DEFAULT 'available',
    pid                  INTEGER,
    pid_start_ticks      INTEGER,
    pgid                 INTEGER,
    host_boot_id         TEXT,
    service_boot_id      TEXT,
    cols                 INTEGER NOT NULL DEFAULT 80,
    rows                 INTEGER NOT NULL DEFAULT 24,
    prompt_regex         TEXT,
    quiet_ms             INTEGER NOT NULL DEFAULT 8000,
    startup_grace_ms     INTEGER NOT NULL DEFAULT 15000,
    watch_json           TEXT NOT NULL DEFAULT '[]',
    input_seq            INTEGER NOT NULL DEFAULT 0,
    log_dir              TEXT,
    purpose              TEXT NOT NULL DEFAULT '',
    output_limit_action  TEXT NOT NULL DEFAULT 'preserve_process',
    created_at           TEXT NOT NULL,
    started_at           TEXT,
    exited_at            TEXT,
    closed_at            TEXT,
    exit_code            INTEGER,
    termination_reason   TEXT,
    error_code           TEXT,
    error_message        TEXT
);
CREATE INDEX IF NOT EXISTS ix_terminals_session ON terminals(session_id, created_at);

CREATE TABLE IF NOT EXISTS terminal_inputs (
    terminal_id     TEXT NOT NULL,
    input_seq       INTEGER NOT NULL,
    operation_id    TEXT NOT NULL UNIQUE,
    request_digest  TEXT NOT NULL DEFAULT '',
    receipt_json    TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    PRIMARY KEY (terminal_id, input_seq)
);

CREATE TABLE IF NOT EXISTS execution_events (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL UNIQUE,
    occurred_at     TEXT NOT NULL,
    session_id      TEXT NOT NULL DEFAULT '',
    resource_id     TEXT,
    operation_id    TEXT,
    type            TEXT NOT NULL,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    availability    TEXT NOT NULL DEFAULT 'available'
);
CREATE INDEX IF NOT EXISTS ix_execution_events_session ON execution_events(session_id, seq);
CREATE INDEX IF NOT EXISTS ix_execution_events_resource ON execution_events(resource_id, seq);

CREATE TABLE IF NOT EXISTS session_fences (
    session_id         TEXT PRIMARY KEY,
    seq                INTEGER NOT NULL DEFAULT 0,
    run_id             TEXT NOT NULL DEFAULT '',
    owner              TEXT NOT NULL DEFAULT '',
    owner_generation   INTEGER NOT NULL DEFAULT 0,
    control_version    INTEGER NOT NULL DEFAULT 0,
    revocations_json   TEXT NOT NULL DEFAULT '[]',
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_reservations (
    resource_id     TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    reserved_bytes  INTEGER NOT NULL DEFAULT 0,
    released        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_budget_session ON budget_reservations(session_id, released);
"""

#: v2 对 jobs 的扩展列（(列名, 定义)）。逐列检查后添加，可重复运行。
JOBS_V2_COLUMNS: tuple[tuple[str, str], ...] = (
    ("session_id", "TEXT NOT NULL DEFAULT ''"),
    ("run_id", "TEXT NOT NULL DEFAULT ''"),
    ("runtime", "TEXT NOT NULL DEFAULT 'legacy'"),
    ("spec_hash", "TEXT"),
    ("worker_pid", "INTEGER"),
    ("worker_pid_start_ticks", "INTEGER"),
    ("host_boot_id", "TEXT"),
    ("deadline_at", "TEXT"),
    ("output_limit_action", "TEXT NOT NULL DEFAULT 'preserve_process'"),
    ("output_limit_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("dropped_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ("interaction_state", "TEXT NOT NULL DEFAULT 'not_applicable'"),
    ("termination_reason", "TEXT"),
    ("purpose", "TEXT NOT NULL DEFAULT ''"),
    ("completion_path", "TEXT"),
    ("cancel_confirmed", "INTEGER NOT NULL DEFAULT 0"),
    ("watch_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("delegation_id", "TEXT"),
    ("delegation_revision", "INTEGER"),
)


class OperationConflict(RuntimeError):
    """同 operation_id 复用了不同参数。调用方应返回 409，**不得**重放。"""


class LedgerMigrationError(RuntimeError):
    """迁移无法安全继续（例如历史重复 operation_id）。调用方必须报告，不得清库。"""


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
    # ---- v2 扩展 ----
    session_id: str = ""
    run_id: str = ""
    runtime: str = "legacy"
    spec_hash: str | None = None
    worker_pid: int | None = None
    worker_pid_start_ticks: int | None = None
    host_boot_id: str | None = None
    deadline_at: str | None = None
    output_limit_action: str = "preserve_process"
    output_limit_bytes: int = 0
    dropped_bytes: int = 0
    interaction_state: str = "not_applicable"
    termination_reason: str | None = None
    purpose: str = ""
    completion_path: str | None = None
    cancel_confirmed: bool = False
    watch_json: str = "[]"
    delegation_id: str | None = None
    delegation_revision: int | None = None

    @property
    def is_execution_resource(self) -> bool:
        return self.runtime == "execution_v1"


@dataclass(slots=True)
class OperationRecord:
    operation_id: str
    session_id: str
    run_id: str
    tool_call_id: str
    request_digest: str
    action: str
    resource_id: str | None
    status: str
    owner_generation: int
    dispatch_control_version: int
    session_control_seq: int
    delegation_id: str | None
    delegation_revision: int | None
    receipt: dict[str, Any]
    accepted_at: str
    executed_at: str | None


_UNSET = object()


def _row_to_record(row: aiosqlite.Row) -> JobRecord:
    keys = set(row.keys())

    def get(name: str, default: Any = None) -> Any:
        return row[name] if name in keys else default

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
        session_id=get("session_id", "") or "",
        run_id=get("run_id", "") or "",
        runtime=get("runtime", "legacy") or "legacy",
        spec_hash=get("spec_hash"),
        worker_pid=get("worker_pid"),
        worker_pid_start_ticks=get("worker_pid_start_ticks"),
        host_boot_id=get("host_boot_id"),
        deadline_at=get("deadline_at"),
        output_limit_action=get("output_limit_action", "preserve_process") or "preserve_process",
        output_limit_bytes=int(get("output_limit_bytes", 0) or 0),
        dropped_bytes=int(get("dropped_bytes", 0) or 0),
        interaction_state=get("interaction_state", "not_applicable") or "not_applicable",
        termination_reason=get("termination_reason"),
        purpose=get("purpose", "") or "",
        completion_path=get("completion_path"),
        cancel_confirmed=bool(get("cancel_confirmed", 0)),
        watch_json=get("watch_json", "[]") or "[]",
        delegation_id=get("delegation_id"),
        delegation_revision=get("delegation_revision"),
    )


def _operation_from_row(row: aiosqlite.Row) -> OperationRecord:
    return OperationRecord(
        operation_id=row["operation_id"],
        session_id=row["session_id"],
        run_id=row["run_id"],
        tool_call_id=row["tool_call_id"],
        request_digest=row["request_digest"],
        action=row["action"],
        resource_id=row["resource_id"],
        status=row["status"],
        owner_generation=int(row["owner_generation"]),
        dispatch_control_version=int(row["dispatch_control_version"]),
        session_control_seq=int(row["session_control_seq"]),
        delegation_id=row["delegation_id"],
        delegation_revision=row["delegation_revision"],
        receipt=json.loads(row["receipt_json"] or "{}"),
        accepted_at=row["accepted_at"],
        executed_at=row["executed_at"],
    )


async def _user_version(db: aiosqlite.Connection) -> int:
    async with db.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row is not None else 0


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    async with db.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ) as cursor:
        row = await cursor.fetchone()
    return bool(row and int(row[0]) > 0)


async def _index_exists(db: aiosqlite.Connection, name: str) -> bool:
    async with db.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ) as cursor:
        row = await cursor.fetchone()
    return bool(row and int(row[0]) > 0)


async def _column_names(db: aiosqlite.Connection, table: str) -> set[str]:
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        rows = await cursor.fetchall()
    return {str(row[1]) for row in rows}


async def migrate(db: aiosqlite.Connection) -> int:
    """把数据库迁移到当前 schema 版本，返回迁移后的版本。"""
    version = await _user_version(db)
    if version >= SCHEMA_VERSION:
        return version

    if not await _table_exists(db, "jobs"):
        # 全新数据库：按 v1 -> v2 顺序建表，保证与旧库升级路径等价。
        await db.executescript(SCHEMA_V1)
        version = 1
    elif version == 0:
        # 旧代码建库时未写入 user_version：按 v1 处理。
        version = 1

    if version == 1:
        # 1) 冲突预检：同 operation_id 多行历史记录无法安全加唯一索引。
        async with db.execute(
            "SELECT operation_id, count(*) AS n FROM jobs GROUP BY operation_id HAVING n > 1"
        ) as cursor:
            duplicates = list(await cursor.fetchall())
        if duplicates:
            detail = ", ".join(f"{row[0]}({row[1]})" for row in duplicates[:5])
            raise LedgerMigrationError(
                "历史数据存在重复 operation_id，无法安全升级："
                f"{detail}。请人工核对后再迁移；不会删除任何旧记录。"
            )
        # 2) jobs 扩展列（逐列幂等添加）
        existing = await _column_names(db, "jobs")
        for name, definition in JOBS_V2_COLUMNS:
            if name not in existing:
                await db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        # 3) 唯一索引切换到 operation_id 单列
        if not await _index_exists(db, "uq_jobs_operation"):
            await db.execute("DROP INDEX IF EXISTS uq_jobs_operation_digest")
            await db.execute(
                "CREATE UNIQUE INDEX uq_jobs_operation ON jobs(operation_id)"
            )
        # 4) 新表
        await db.executescript(SCHEMA_V2_TABLES)
        await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        await db.commit()
        version = SCHEMA_VERSION
    return version


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
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=FULL")
        await migrate(db)
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

    # ------------------------------------------------------------------ #
    # 旧接口（保持兼容）
    # ------------------------------------------------------------------ #

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
        session_id: str = "",
        run_id: str = "",
        runtime: str = "legacy",
        spec_hash: str | None = None,
        output_limit_action: str = "preserve_process",
        output_limit_bytes: int = 0,
        purpose: str = "",
        deadline_at: str | None = None,
        watch_json: str = "[]",
        delegation_id: str | None = None,
        delegation_revision: int | None = None,
    ) -> JobRecord:
        now = datetime.now(UTC).isoformat()
        await self.db.execute(
            """
            INSERT INTO jobs (
                job_id, operation_id, request_digest, kind, payload, state, created_at,
                session_id, run_id, runtime, spec_hash, output_limit_action,
                output_limit_bytes, purpose, deadline_at, watch_json,
                delegation_id, delegation_revision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                operation_id,
                request_digest,
                kind,
                json.dumps(payload, ensure_ascii=False),
                state,
                now,
                session_id,
                run_id,
                runtime,
                spec_hash,
                output_limit_action,
                int(output_limit_bytes),
                purpose,
                deadline_at,
                watch_json,
                delegation_id,
                delegation_revision,
            ),
        )
        await self.db.commit()
        record = await self.get(job_id)
        assert record is not None
        return record

    _UPDATABLE: ClassVar[set[str]] = {
        "state",
        "pid",
        "pid_start_ticks",
        "worker_boot_id",
        "worker_pid",
        "worker_pid_start_ticks",
        "host_boot_id",
        "exit_code",
        "byte_count",
        "truncated",
        "output_path",
        "started_at",
        "finished_at",
        "last_heartbeat",
        "cancel_requested",
        "cancel_request_id",
        "cancel_confirmed",
        "error_code",
        "error_message",
        "interaction_state",
        "termination_reason",
        "dropped_bytes",
        "completion_path",
        "spec_hash",
        "deadline_at",
    }

    async def update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - self._UPDATABLE
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
            "SELECT * FROM jobs WHERE state IN ('received','starting','running','unknown') "
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
            "SELECT COUNT(*) AS n FROM jobs WHERE state IN ('received','starting','running') OR (runtime='execution_v1' AND state='unknown')"
        ) as cursor:
            row = await cursor.fetchone()
        count = int(row["n"]) if row is not None else 0
        async with self.db.execute(
            "SELECT COUNT(*) AS n FROM terminals WHERE state IN ('starting','running','unknown')"
        ) as cursor:
            row = await cursor.fetchone()
        return count + (int(row["n"]) if row is not None else 0)

    # ------------------------------------------------------------------ #
    # 执行底座：操作记录
    # ------------------------------------------------------------------ #

    async def get_operation(self, operation_id: str) -> OperationRecord | None:
        async with self.db.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _operation_from_row(row) if row is not None else None

    async def insert_operation(
        self,
        *,
        operation_id: str,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        request_digest: str,
        action: str,
        resource_id: str | None,
        owner_generation: int,
        dispatch_control_version: int,
        session_control_seq: int,
        delegation_id: str | None,
        delegation_revision: int | None,
        status: str = "accepted",
        receipt: dict[str, Any] | None = None,
    ) -> OperationRecord:
        now = datetime.now(UTC).isoformat()
        await self.db.execute(
            """
            INSERT INTO operations (
                operation_id, session_id, run_id, tool_call_id, request_digest, action,
                resource_id, status, owner_generation, dispatch_control_version,
                session_control_seq, delegation_id, delegation_revision, receipt_json,
                accepted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                session_id,
                run_id,
                tool_call_id,
                request_digest,
                action,
                resource_id,
                status,
                int(owner_generation),
                int(dispatch_control_version),
                int(session_control_seq),
                delegation_id,
                delegation_revision,
                json.dumps(receipt or {}, ensure_ascii=False),
                now,
            ),
        )
        await self.db.commit()
        record = await self.get_operation(operation_id)
        assert record is not None
        return record

    async def complete_operation(
        self,
        operation_id: str,
        *,
        status: str,
        resource_id: str | None = None,
        receipt: dict[str, Any] | None = None,
    ) -> None:
        assignments = ["status = ?", "executed_at = ?"]
        values: list[Any] = [status, datetime.now(UTC).isoformat()]
        if resource_id is not None:
            assignments.append("resource_id = ?")
            values.append(resource_id)
        if receipt is not None:
            assignments.append("receipt_json = ?")
            values.append(json.dumps(receipt, ensure_ascii=False))
        values.append(operation_id)
        await self.db.execute(
            f"UPDATE operations SET {', '.join(assignments)} WHERE operation_id = ?",
            tuple(values),
        )
        await self.db.commit()

    # ------------------------------------------------------------------ #
    # 执行底座：资源查询与配额
    # ------------------------------------------------------------------ #

    async def list_session_resources(
        self, session_id: str, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        """session 资源分页：jobs 与 terminals 合并按创建时间倒序。

        返回 `([{"kind": "job"|"terminal", "record": JobRecord|Row}, ...], total)`。
        """
        params = (session_id, session_id)
        query = """SELECT job_id AS resource_id, kind AS resource_kind, 'job' AS source, created_at
                   FROM jobs WHERE session_id=? AND runtime='execution_v1'
                   UNION ALL SELECT terminal_id, 'terminal', 'terminal', created_at
                   FROM terminals WHERE session_id=?"""
        async with self.db.execute("SELECT count(*) FROM ("+query+")", params) as cursor:
            count_row = await cursor.fetchone()
            assert count_row is not None
            total = int(count_row[0])
        async with self.db.execute(query+" ORDER BY created_at DESC, resource_id DESC LIMIT ? OFFSET ?",
                                   (*params, limit, offset)) as cursor:
            page = await cursor.fetchall()
        result = []
        for row in page:
            record = (await self.get(row["resource_id"]) if row["source"] == "job"
                      else await self.get_terminal(row["resource_id"]))
            result.append({"kind": row["source"], "created_at": row["created_at"], "record": record})
        return result, total

    async def non_terminal_execution_jobs(self) -> list[JobRecord]:
        """重启核对：属于执行底座且未到终态的任务。"""
        async with self.db.execute(
            "SELECT * FROM jobs WHERE runtime = 'execution_v1' "
            "AND state IN ('received','starting','running','unknown') ORDER BY created_at"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_record(row) for row in rows]

    async def non_terminal_terminals(self) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM terminals WHERE state IN ('starting','running','unknown') ORDER BY created_at"
        ) as cursor:
            return list(await cursor.fetchall())

    async def count_active_for_session(self, session_id: str) -> tuple[int, int]:
        """返回 (活动资源总数, 活动 PTY 数)。"""
        async with self.db.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE session_id = ? AND runtime = 'execution_v1' "
            "AND state IN ('received','starting','running','unknown')",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        jobs = int(row["n"]) if row is not None else 0
        async with self.db.execute(
            "SELECT COUNT(*) AS n FROM terminals WHERE session_id = ? AND state IN ('starting','running','unknown')",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        terminals = int(row["n"]) if row is not None else 0
        return jobs + terminals, terminals

    async def count_active_global(self) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE runtime = 'execution_v1' "
            "AND state IN ('received','starting','running','unknown')"
        ) as cursor:
            row = await cursor.fetchone()
        jobs = int(row["n"]) if row is not None else 0
        async with self.db.execute(
            "SELECT COUNT(*) AS n FROM terminals WHERE state IN ('starting','running','unknown')"
        ) as cursor:
            row = await cursor.fetchone()
        return jobs + (int(row["n"]) if row is not None else 0)

    async def reserve_output_budget(
        self, *, resource_id: str, session_id: str, bytes_needed: int, session_budget: int
    ) -> int:
        """为资源预留输出额度。返回该 session 当前已预留总量。

        事务内完成"求和 -> 检查 -> 插入"，避免并发写超上限；不可超限时抛
        `ValueError`（调用方转成确定拒绝）。
        """
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.execute(
                "SELECT COALESCE(SUM(reserved_bytes), 0) AS n FROM budget_reservations "
                "WHERE session_id = ? AND released = 0",
                (session_id,),
            ) as cursor:
                row = await cursor.fetchone()
            used = int(row["n"]) if row is not None else 0
            if used + bytes_needed > session_budget:
                raise ValueError(
                    f"session 采集日志预算不足（已预留 {used}，请求 {bytes_needed}，"
                    f"预算 {session_budget}）"
                )
            await self.db.execute(
                "INSERT OR REPLACE INTO budget_reservations "
                "(resource_id, session_id, reserved_bytes, released, created_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (resource_id, session_id, bytes_needed, datetime.now(UTC).isoformat()),
            )
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return used + bytes_needed

    async def release_output_budget(self, resource_id: str) -> bool:
        """释放预留；重复释放不产生第二次效果。返回是否本次真正释放。"""
        cursor = await self.db.execute(
            "UPDATE budget_reservations SET released = 1 WHERE resource_id = ? AND released = 0",
            (resource_id,),
        )
        released = cursor.rowcount > 0
        await cursor.close()
        await self.db.commit()
        return released

    async def session_budget_used(self, session_id: str) -> int:
        async with self.db.execute(
            "SELECT COALESCE(SUM(reserved_bytes), 0) AS n FROM budget_reservations "
            "WHERE session_id = ? AND released = 0",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["n"]) if row is not None else 0

    async def settle_output_budget(self, resource_id: str, stored_bytes: int) -> None:
        """结束只释放未用预留；仍保留的日志继续占用 session 存储预算。"""
        await self.db.execute(
            "UPDATE budget_reservations SET reserved_bytes = ?, released = 0 "
            "WHERE resource_id = ?",
            (max(0, stored_bytes), resource_id),
        )
        await self.db.commit()

    # ------------------------------------------------------------------ #
    # 执行底座：控制 fence
    # ------------------------------------------------------------------ #

    async def get_fence(self, session_id: str) -> dict[str, Any] | None:
        async with self.db.execute(
            "SELECT * FROM session_fences WHERE session_id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "session_id": row["session_id"],
            "seq": int(row["seq"]),
            "run_id": row["run_id"],
            "owner": row["owner"],
            "owner_generation": int(row["owner_generation"]),
            "control_version": int(row["control_version"]),
            "revocations": json.loads(row["revocations_json"] or "[]"),
            "updated_at": row["updated_at"],
        }

    async def sync_fence(
        self,
        *,
        session_id: str,
        run_id: str,
        owner: str,
        owner_generation: int,
        control_version: int,
        revocations: list[dict[str, Any]],
        expected_seq: int | None,
    ) -> tuple[bool, dict[str, Any], str]:
        """同步 fence。

        规则：
        - `expected_seq` 与当前 seq 不一致 -> 不应用，返回当前视图（乐观并发）；
        - 同一 run 的 control_version 回退 -> 不应用（旧方向同步不得回退 fence）；
        - 应用时 seq + 1，并把 revocations 合并进视图。
        """
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            current = await self.get_fence(session_id)
            if current is None:
                next_seq = 1
                merged = list(revocations)
                await self.db.execute(
                    "INSERT INTO session_fences (session_id, seq, run_id, owner, owner_generation, "
                    "control_version, revocations_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        session_id,
                        next_seq,
                        run_id,
                        owner,
                        int(owner_generation),
                        int(control_version),
                        json.dumps(merged, ensure_ascii=False),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                await self.db.commit()
                view = {
                    "session_id": session_id,
                    "seq": next_seq,
                    "run_id": run_id,
                    "owner": owner,
                    "owner_generation": int(owner_generation),
                    "control_version": int(control_version),
                    "revocations": merged,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
                return True, view, ""
            if expected_seq is not None and int(expected_seq) != int(current["seq"]):
                await self.db.commit()
                return False, current, "expected_seq 落后于执行端当前 fence；请刷新后重试"
            if (
                run_id == current["run_id"]
                and int(control_version) < int(current["control_version"])
            ):
                await self.db.commit()
                return False, current, "control_version 回退，拒绝把 fence 退回旧方向"
            merged_revocations: list[dict[str, Any]] = list(current["revocations"])
            seen = {
                (str(item.get("delegation_id")), int(item.get("revision", 0)))
                for item in merged_revocations
                if isinstance(item, dict)
            }
            for item in revocations:
                key = (str(item.get("delegation_id")), int(item.get("revision", 0)))
                if key not in seen:
                    merged_revocations.append(item)
                    seen.add(key)
            next_seq = int(current["seq"]) + 1
            await self.db.execute(
                "UPDATE session_fences SET seq = ?, run_id = ?, owner = ?, owner_generation = ?, "
                "control_version = ?, revocations_json = ?, updated_at = ? WHERE session_id = ?",
                (
                    next_seq,
                    run_id,
                    owner,
                    int(owner_generation),
                    int(control_version),
                    json.dumps(merged_revocations, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                    session_id,
                ),
            )
            await self.db.commit()
            return (
                True,
                {
                    "session_id": session_id,
                    "seq": next_seq,
                    "run_id": run_id,
                    "owner": owner,
                    "owner_generation": int(owner_generation),
                    "control_version": int(control_version),
                    "revocations": merged_revocations,
                    "updated_at": datetime.now(UTC).isoformat(),
                },
                "",
            )
        except Exception:
            await self.db.rollback()
            raise

    # ------------------------------------------------------------------ #
    # 执行底座：终端
    # ------------------------------------------------------------------ #

    async def insert_terminal(
        self,
        *,
        terminal_id: str,
        session_id: str,
        run_id: str,
        operation_id: str,
        mode: str,
        argv_json: str,
        cwd: str,
        state: str,
        pid: int | None = None,
        pid_start_ticks: int | None = None,
        pgid: int | None = None,
        host_boot_id: str | None = None,
        service_boot_id: str | None = None,
        cols: int = 80,
        rows: int = 24,
        prompt_regex: str | None = None,
        quiet_ms: int = 8000,
        startup_grace_ms: int = 15000,
        watch_json: str = "[]",
        log_dir: str | None = None,
        purpose: str = "",
        output_limit_action: str = "preserve_process",
    ) -> None:
        now = datetime.now(UTC).isoformat()
        await self.db.execute(
            """
            INSERT INTO terminals (
                terminal_id, session_id, run_id, operation_id, mode, argv_json, cwd, state,
                interaction_state, pid, pid_start_ticks, pgid, host_boot_id, service_boot_id,
                cols, rows, prompt_regex, quiet_ms, startup_grace_ms, watch_json, input_seq,
                log_dir, purpose, output_limit_action, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'available', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                terminal_id,
                session_id,
                run_id,
                operation_id,
                mode,
                argv_json,
                cwd,
                state,
                pid,
                pid_start_ticks,
                pgid,
                host_boot_id,
                service_boot_id,
                cols,
                rows,
                prompt_regex,
                quiet_ms,
                startup_grace_ms,
                watch_json,
                log_dir,
                purpose,
                output_limit_action,
                now,
            ),
        )
        await self.db.commit()

    _TERMINAL_UPDATABLE: ClassVar[set[str]] = {
        "state",
        "interaction_state",
        "pid",
        "pid_start_ticks",
        "pgid",
        "host_boot_id",
        "service_boot_id",
        "cols",
        "rows",
        "prompt_regex",
        "quiet_ms",
        "startup_grace_ms",
        "watch_json",
        "started_at",
        "exited_at",
        "closed_at",
        "exit_code",
        "termination_reason",
        "error_code",
        "error_message",
        "input_seq",
    }

    async def update_terminal(self, terminal_id: str, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - self._TERMINAL_UPDATABLE
        if unknown:
            raise ValueError(f"不允许更新终端字段：{sorted(unknown)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        await self.db.execute(
            f"UPDATE terminals SET {assignments} WHERE terminal_id = ?",
            (*values, terminal_id),
        )
        await self.db.commit()

    async def get_terminal(self, terminal_id: str) -> aiosqlite.Row | None:
        async with self.db.execute(
            "SELECT * FROM terminals WHERE terminal_id = ?", (terminal_id,)
        ) as cursor:
            return await cursor.fetchone()

    async def list_terminals(
        self, session_id: str, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[aiosqlite.Row], int]:
        async with self.db.execute(
            "SELECT COUNT(*) AS n FROM terminals WHERE session_id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
        total = int(row["n"]) if row is not None else 0
        async with self.db.execute(
            "SELECT * FROM terminals WHERE session_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
        return list(rows), total

    async def allocate_input_seq(
        self,
        *,
        terminal_id: str,
        operation_id: str,
        request_digest: str,
        receipt: dict[str, Any],
    ) -> tuple[int, dict[str, Any], bool]:
        """分配终端输入序号并写回执。

        返回 `(input_seq, receipt, deduplicated)`：
        - 同一 `operation_id` 重试 -> 返回原回执，**不再次写入**；
        - 不同操作并发 -> 事务串行分配序号。
        """
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.execute(
                "SELECT * FROM terminal_inputs WHERE operation_id = ?", (operation_id,)
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise OperationConflict("同一终端输入操作绑定了不同参数")
                await self.db.commit()
                return int(existing["input_seq"]), json.loads(existing["receipt_json"]), True
            async with self.db.execute(
                "SELECT input_seq FROM terminals WHERE terminal_id = ?", (terminal_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise KeyError(terminal_id)
            next_seq = int(row["input_seq"]) + 1
            await self.db.execute(
                "UPDATE terminals SET input_seq = ? WHERE terminal_id = ?",
                (next_seq, terminal_id),
            )
            await self.db.execute(
                "INSERT INTO terminal_inputs (terminal_id, input_seq, operation_id, "
                "request_digest, receipt_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    terminal_id,
                    next_seq,
                    operation_id,
                    request_digest,
                    json.dumps(receipt, ensure_ascii=False),
                    datetime.now(UTC).isoformat(),
                ),
            )
            await self.db.commit()
            return next_seq, receipt, False
        except Exception:
            await self.db.rollback()
            raise

    async def get_input_receipt(self, operation_id: str) -> tuple[int, dict[str, Any]] | None:
        async with self.db.execute(
            "SELECT * FROM terminal_inputs WHERE operation_id = ?", (operation_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return int(row["input_seq"]), json.loads(row["receipt_json"])

    async def mark_input_receipt(self, operation_id: str, receipt: dict[str, Any]) -> None:
        """写入完成后更新回执。中途崩溃时回执保持 `writing`，恢复路径不重发。"""
        await self.db.execute(
            "UPDATE terminal_inputs SET receipt_json = ? WHERE operation_id = ?",
            (json.dumps(receipt, ensure_ascii=False), operation_id),
        )
        await self.db.commit()

    # ------------------------------------------------------------------ #
    # 执行底座：持久事件
    # ------------------------------------------------------------------ #

    async def append_event(
        self,
        *,
        event_id: str,
        type: str,
        session_id: str = "",
        resource_id: str | None = None,
        operation_id: str | None = None,
        payload: dict[str, Any] | None = None,
        availability: str = "available",
        occurred_at: str | None = None,
    ) -> int:
        cursor = await self.db.execute(
            """
            INSERT INTO execution_events
                (event_id, occurred_at, session_id, resource_id, operation_id, type,
                 payload_json, availability)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                occurred_at or datetime.now(UTC).isoformat(),
                session_id,
                resource_id,
                operation_id,
                type,
                json.dumps(payload or {}, ensure_ascii=False),
                availability,
            ),
        )
        seq = int(cursor.lastrowid or 0)
        await cursor.close()
        await self.db.commit()
        return seq

    async def read_events(self, *, after: int, limit: int) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM execution_events WHERE seq > ? ORDER BY seq LIMIT ?",
            (after, limit),
        ) as cursor:
            return list(await cursor.fetchall())

    async def max_event_seq(self) -> int:
        async with self.db.execute("SELECT COALESCE(MAX(seq), 0) AS n FROM execution_events") as cursor:
            row = await cursor.fetchone()
        return int(row["n"]) if row is not None else 0
