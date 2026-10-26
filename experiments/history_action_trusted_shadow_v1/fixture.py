"""真实 Tool Schema/OperationRegistry/仓储代码 + 内存 SQL 替身 + MockTransport。

没有数据库或真实执行端。领域行由生产仓储创建，不手工填写检测器所需的完整视图。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field
from sqlalchemy import inspect as inspect_sql
from sqlalchemy.sql import operators
from sqlalchemy.sql.dml import Update
from tests.support.scripted_model import ScriptedChatModel

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.config.settings import Settings
from agent_workbench.contracts.enums import JobState
from agent_workbench.contracts.identity import ScopeRule, ScopeSnapshot
from agent_workbench.contracts.planning import planning_digest
from agent_workbench.contracts.tool import ExecutorCapabilities, JobReceipt, JobSnapshot
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.tables import ArtifactRow, RunRow, ToolJobRow, ToolOperationRow
from agent_workbench.tools.http import (
    HttpRequestArgs,
    _digest_for,
    _parse_json_object,
    http_request,
)
from agent_workbench.tools.runtime import ResourceLocks, Toolbox

SESSION = UUID("20000000-0000-4000-8000-000000000001")
RUN = UUID("20000000-0000-4000-8000-000000000002")
URL = "https://contract.invalid/check"


class Result:
    def __init__(self, values):
        self.values = values

    def scalar_one_or_none(self):
        return self.values[0] if self.values else None

    def scalar_one(self):
        assert len(self.values) == 1
        return self.values[0]

    def first(self):
        return self.values[0] if self.values else None

    def all(self):
        return self.values

    def scalars(self):
        return self


def matches(row, clause):
    if hasattr(clause, "clauses"):
        return all(matches(row, c) for c in clause.clauses)
    left = getattr(row, clause.left.key)
    right = clause.right.value
    if clause.operator is operators.in_op:
        return left in right
    if clause.operator is operators.eq:
        return left == right
    raise AssertionError(f"unsupported offline SQL predicate:{clause.operator}")


class MemorySession:
    def __init__(self, db):
        self.db = db

    def add(self, row):
        if hasattr(row, "created_at") and row.created_at is None:
            row.created_at = datetime.now(UTC)
        if hasattr(row, "updated_at") and row.updated_at is None:
            row.updated_at = datetime.now(UTC)
        if isinstance(row, ToolOperationRow) and row.result_artifact_ids is None:
            row.result_artifact_ids = []
        self.db.rows.setdefault(type(row), []).append(row)

    async def flush(self):
        pass

    async def rollback(self):
        raise AssertionError("unexpected offline transaction rollback")

    async def get(self, cls, key):
        pk = inspect_sql(cls).primary_key[0].key
        return next((r for r in self.db.rows.get(cls, []) if getattr(r, pk) == key), None)

    async def scalars(self, query):
        return await self.execute(query)

    async def execute(self, query: Any):
        if isinstance(query, Update):
            cls = next(
                c
                for c in self.db.rows
                if getattr(c, "__tablename__", None) == cast(Any, query.table).name
            )
            for row in self.db.rows.get(cls, []):
                if all(matches(row, c) for c in query._where_criteria):
                    for key, value in (query._values or {}).items():
                        setattr(row, str(getattr(key, "key", key)), value.value)
            return Result([])
        desc = query.column_descriptions
        if not desc or desc[0].get("entity") is None:
            raise AssertionError("non-domain SQL in offline fixture")
        cls = desc[0]["entity"]
        rows = [
            r
            for r in self.db.rows.get(cls, [])
            if all(matches(r, c) for c in query._where_criteria)
        ]
        if len(desc) == 1 and desc[0]["expr"] is cls:
            return Result(rows)
        values = [tuple(getattr(r, d["name"]) for d in desc) for r in rows]
        if len(desc) == 1:
            values = [v[0] for v in values]
        return Result(values)


class MemoryDB:
    def __init__(self):
        self.rows = {
            RunRow: [
                RunRow(
                    id=RUN,
                    session_id=SESSION,
                    owner="offline",
                    owner_generation=1,
                    control_version=0,
                    status="running",
                    lease_until=None,
                )
            ]
        }
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def session(self):
        yield MemorySession(self)

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            yield MemorySession(self)


class Executor:
    """所有 HTTP 都绑定 MockTransport；没有 socket、SSH 或真实 Executor。"""

    def __init__(self):
        self.submissions, self.jobs, self.files = [], {}, {}
        self._preflight = ExecutorCapabilities(
            executor_id="offline-executor",
            service_version="fixture",
            protocol_version=1,
            boot_id="fixture-boot",
            started_at=datetime.now(UTC),
        )
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._response))
        self.error: Exception | None = None
        self.submit_barrier: asyncio.Event | None = None

    def _response(self, request):
        packet = json.loads(request.content)
        oid = packet["operation_id"]
        if oid not in self.jobs:
            job = uuid5(NAMESPACE_URL, oid)
            now = datetime.now(UTC)
            self.jobs[oid] = JobSnapshot(
                job_id=job,
                operation_id=UUID(oid),
                kind="http_request",
                state=JobState.SUCCEEDED,
                started_at=now,
                finished_at=now,
            )
            body = b"controlled response; status is not a vulnerability conclusion"
            meta = {
                "status_code": 403,
                "final_url": packet["payload"]["url"],
                "content_type": "text/plain",
                "elapsed_ms": 1,
                "body_bytes": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "request_encoding": "none",
                "request_body_bytes": 0,
                "request_body_sha256": hashlib.sha256(b"").hexdigest(),
            }
            self.files[str(job)] = {
                "response.body": body,
                "response.meta.json": json.dumps(meta).encode(),
            }
        return httpx.Response(200, json={"job_id": str(self.jobs[oid].job_id)})

    async def submit(self, packet):
        self.submissions.append(copy.deepcopy(packet))
        if self.submit_barrier is not None:
            await self.submit_barrier.wait()
        if self.error is not None:
            raise self.error
        response = await self.client.post("https://mock.invalid/submit", json=packet)
        return JobReceipt(
            job_id=UUID(response.json()["job_id"]),
            operation_id=UUID(packet["operation_id"]),
            state=JobState.RECEIVED,
            accepted_at=datetime.now(UTC),
        )

    async def get_job(self, job_id):
        return next(j for j in self.jobs.values() if j.job_id == job_id)

    async def download_artifact(self, job_id, name, *, offset=0, limit=1048576):
        raw = self.files[str(job_id)][name]
        return raw[offset : offset + limit], {
            "size": str(len(raw)),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "offset": str(offset),
        }

    async def capabilities(self):
        return self._preflight

    async def close(self):
        await self.client.aclose()


def tool_arguments(**change):
    return {
        "url": URL,
        "method": "GET",
        "headers_json": '{"Cookie":""}',
        "follow_redirects": False,
        **change,
    }


def parsed(arguments):
    values = dict(arguments)
    values["headers"] = _parse_json_object(values.pop("headers_json", None), "headers_json")
    values["params"] = _parse_json_object(values.pop("params_json", None), "params_json")
    if "json_body_json" in values:
        values["json_body"] = json.loads(values.pop("json_body_json"))
    return HttpRequestArgs.model_validate(values)


def approve(tool_call_id, arguments, **changes):
    args = parsed(arguments)
    saved = args.model_dump(exclude={"note"})
    if args.verify_tls:
        saved.pop("verify_tls")
    return {
        "approval_id": "control:" + tool_call_id,
        "session_id": str(SESSION),
        "run_id": str(RUN),
        "tool_call_id": tool_call_id,
        "purpose": "security_test",
        "production_request_digest": _digest_for(saved),
        "task_version": "operator-task:v1",
        "environment": "operator-env:Q",
        "configuration": "operator-deploy:v1",
        "prerequisites": {"operator_fixture_ready": True},
        "executor_id": "offline-executor",
        "executor_boot_id": "fixture-boot",
        "mapping_signature": planning_digest([]),
        "retest_required": False,
        **changes,
    }


def make_toolbox(root: Path, enabled: bool, grants) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "operator-grants.json"
    path.write_text(json.dumps(grants), encoding="utf-8")
    settings = cast(Any, Settings)(
        _env_file=None,
        data_root=root,
        executor_id="offline-executor",
        history_action_shadow_enabled=enabled,
        history_action_shadow_grants_path=path,
        history_action_shadow_timeout_seconds=2,
    )
    db, executor = MemoryDB(), Executor()
    return Toolbox(
        run_id=RUN,
        session_id=SESSION,
        owner="offline",
        owner_generation=1,
        settings=settings,
        db=cast(Any, db),
        scope=ScopePolicy(
            ScopeSnapshot(rules=[ScopeRule(scheme="https", host="contract.invalid", port=443)])
        ),
        executor=cast(Any, executor),
        artifacts=ArtifactStore(settings),
        spans=cast(Any, None),
        locks=ResourceLocks(settings),
    )


class Model(ScriptedChatModel):
    inputs: list = Field(default_factory=list)

    def _generate(self, messages, *args, **kwargs):
        self.inputs.append([m.model_dump(mode="json") for m in messages])
        return super()._generate(messages, *args, **kwargs)


async def graph_run(box, rounds, *, interrupt_after=None, checkpointer=None, tool_impl=None):
    responses = [
        AIMessage(
            content="",
            id=f"ai-{index}",
            tool_calls=[
                {"name": name, "args": arguments, "id": call_id, "type": "tool_call"}
                for name, arguments, call_id in batch
            ],
        )
        for index, batch in enumerate(rounds)
    ]
    responses.append(AIMessage(content="offline final: execution observations only", id="ai-final"))
    model = Model(responses=responses)
    graph = create_agent(
        model=model,
        tools=[tool_impl or http_request],
        middleware=[ToolDispatchMiddleware()],
        state_schema=AgentGraphState,
        context_schema=RunContext,
        checkpointer=checkpointer or InMemorySaver(),
        interrupt_after=interrupt_after,
    )
    config = {"configurable": {"thread_id": "offline-thread"}}
    context = RunContext(toolbox=box, thread_id="offline-thread")
    result = await cast(Any, graph).ainvoke(
        {
            "messages": [HumanMessage(content="controlled offline request", id="user")],
            "dispatch_control_version": 0,
        },
        config,
        context=context,
    )
    return cast(Any, graph), model, result, cast(Any, config), context


def row_dump(row):
    values = {}
    for col in inspect_sql(type(row)).columns:
        value = getattr(row, col.key)
        values[col.key] = (
            value.isoformat()
            if isinstance(value, datetime)
            else str(value)
            if isinstance(value, UUID)
            else value
        )
    return values


def business(box, result, model):
    ids = {
        str(op.operation_id): "operation:" + op.tool_call_id
        for op in box.db.rows.get(ToolOperationRow, [])
    }
    for j in box.db.rows.get(ToolJobRow, []):
        ids[str(j.job_id)] = "job:" + ids[str(j.operation_id)]
    for a in box.db.rows.get(ArtifactRow, []):
        ids[str(a.id)] = "artifact:" + ids[str(a.source_operation_id)] + ":" + a.sha256
    times = {
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "captured_at",
        "accepted_at",
        "last_synced_at",
        "executor_started_at",
        "executor_finished_at",
        "executor_accepted_at",
    }

    def normalize(value, key=""):
        if key in times and value is not None:
            return "<time>"
        if isinstance(value, dict):
            return {
                k: normalize(v, k)
                for k, v in value.items()
                if not (k == "id" and value.get("type") in {"ai", "tool", "human"})
            }
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, str):
            try:
                parsed_value = json.loads(value)
                if isinstance(parsed_value, (dict, list)):
                    return normalize(parsed_value)
            except ValueError:
                pass
            for raw, stable in ids.items():
                value = value.replace(raw, stable)
        return value

    return normalize(
        {
            "inputs": model.inputs,
            "result": {
                k: [m.model_dump(mode="json") for m in v] if k == "messages" else v
                for k, v in result.items()
            },
            "dispatches": box.executor.submissions,
            "physical_jobs": len(box.executor.jobs),
            "tool_stats": box.stats,
            "records": {
                cls.__name__: [row_dump(r) for r in rows] for cls, rows in box.db.rows.items()
            },
        }
    )
