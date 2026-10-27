"""独立实验接缝；官方 Agent 是唯一模型—工具循环。默认没有真实网络通道。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import UUID, uuid4, uuid5

import httpx
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, messages_from_dict, messages_to_dict
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import SecretStr
from sqlalchemy import DateTime, delete, select
from tests.experiments.decision_memory_benchmark import (
    RecentListMiddleware,
    bind_control,
    digest,
    encoded,
    persisted_manifests,
    snapshot,
)
from tests.experiments.test_decision_memory_offline import local_condition_check

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.command_guard import CommandGuardMiddleware
from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
from agent_workbench.agent.middleware.model_observer import ModelObserverMiddleware
from agent_workbench.agent.middleware.response_recovery import ResponseRecoveryMiddleware
from agent_workbench.agent.middleware.working_context import WorkingContextMiddleware
from agent_workbench.agent.prompts import SYSTEM_PROMPT
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.config.catalog import ProviderProfile
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.models.bailian import BailianChatOpenAI
from agent_workbench.models.context_budget import estimate_request
from agent_workbench.models.invocation import graph_review_scope
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.tables import (
    ArtifactRow,
    EventRow,
    FindingRow,
    IdentityRow,
    ModelCallRow,
    ProjectRow,
    RunRow,
    SessionGraphReceiptRow,
    SessionGraphRow,
    SessionRow,
    ToolJobRow,
    ToolOperationRow,
)
from agent_workbench.tools.evidence import read_artifact
from agent_workbench.tools.history import search_history
from agent_workbench.tools.session_graph import get_graph, update_graph

ROOT = Path(__file__).resolve().parent
BUNDLE = ROOT / "bundle"
TABLES = [
    ProjectRow,
    IdentityRow,
    SessionRow,
    RunRow,
    ToolOperationRow,
    ToolJobRow,
    ArtifactRow,
    FindingRow,
    SessionGraphRow,
]
QUESTIONS = {
    14: "仅就资产 Q 的历史检查 A，核对它目前能支持的结论及适用范围，判断是否需要新的本地动作。",
    15: "本轮目标仅是资产 Q 上的 B；核对它当前是否能够推进，选择并实施一项有信息价值的本地后续动作。",
    17: "核对原 U 执行结果目前能确定到什么程度，同时考虑独立 C 记录，说明是否需要后续处理。",
}
COMMON = "仅本地模拟，不访问网络或 Executor。关联实体引用用于定位历史资料，不取代题目明确指定的资产目标。可以按需查询历史和读取原工件。目标得到足够说明、必要的本地动作完成后自然结束；最后简短说明结论、适用条件和来源 ID，不输出隐藏推理。"


def load(name):
    return json.loads((BUNDLE / name).read_text(encoding="utf-8"))


def write_json(path, value, *, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x" if exclusive else "w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def visible(value):
    """导出正常可见内容；协议回传的隐藏推理不落实验文件。"""
    if isinstance(value, dict):
        return {
            k: visible(v)
            for k, v in value.items()
            if k not in {"reasoning_content", "analysis_content", "reasoning"}
        }
    if isinstance(value, list):
        return [visible(x) for x in value]
    return value


def isolated(database):
    from sqlalchemy.engine import make_url

    if not (make_url(database.settings.database_url).database or "").startswith("workbench_test_"):
        raise RuntimeError("isolated_database_required")


async def export_history(database, box, prefix, ids):
    isolated(database)
    tables = {}
    async with database.session() as db:
        task = await db.get(SessionRow, box.session_id)
        for model in TABLES:
            if model is ProjectRow:
                query = select(model).where(model.id == task.project_id)
            elif model is IdentityRow:
                query = select(model).where(model.project_id == task.project_id)
            elif model is SessionRow:
                query = select(model).where(model.id == box.session_id)
            elif model is ToolJobRow:
                query = (
                    select(model)
                    .join(ToolOperationRow, model.operation_id == ToolOperationRow.operation_id)
                    .where(ToolOperationRow.session_id == box.session_id)
                )
            else:
                query = select(model).where(model.session_id == box.session_id)
            rows = list((await db.scalars(query)).all())
            # 夹具库中只存在本次前缀；不从业务数据库导出。
            tables[model.__tablename__] = [
                {c.key: getattr(row, c.key) for c in model.__table__.columns} for row in rows
            ]
        artifacts = list(
            (
                await db.scalars(
                    select(ArtifactRow).where(ArtifactRow.session_id == box.session_id)
                )
            ).all()
        )
    files = {
        str(a.id): base64.b64encode(box.artifacts.read_bytes(a.relative_key)).decode()
        for a in artifacts
    }
    state = {**prefix, "messages": messages_to_dict(prefix["messages"])}
    return json.loads(
        json.dumps(
            {
                "tables": tables,
                "artifact_bytes_base64": files,
                "prefix": state,
                "fixture_ids": ids,
                "session_id": box.session_id,
            },
            default=str,
        )
    )


async def import_history(database):
    isolated(database)
    data = load("history.json")
    store = ArtifactStore(database.settings)
    for row in data["tables"]["artifacts"]:
        raw = base64.b64decode(data["artifact_bytes_base64"][row["id"]], validate=True)
        assert hashlib.sha256(raw).hexdigest() == row["sha256"]
        saved = store.write_bytes(
            raw,
            media_type=row["media_type"],
            capture_time=datetime.fromisoformat(row["captured_at"]),
        )
        assert saved.relative_key == row["relative_key"]
    async with database.transaction() as db:
        # 同一 pytest 会话的配对格复用隔离库，只删除本夹具的固定 Session/Project。
        # 每条真实 CLI 轨迹由独立 pytest 进程创建全新随机测试库。
        sid = UUID(data["session_id"])
        await db.execute(delete(EventRow).where(EventRow.session_id == sid))
        await db.execute(delete(ModelCallRow).where(ModelCallRow.session_id == sid))
        await db.execute(
            delete(ToolJobRow).where(
                ToolJobRow.job_id.in_([UUID(r["job_id"]) for r in data["tables"]["tool_jobs"]])
            )
        )
        for model in (
            SessionGraphReceiptRow,
            SessionGraphRow,
            FindingRow,
            ArtifactRow,
            ToolOperationRow,
            RunRow,
        ):
            await db.execute(delete(model).where(model.session_id == sid))
        await db.execute(
            delete(ProjectRow).where(ProjectRow.id == UUID(data["tables"]["projects"][0]["id"]))
        )
        for model in TABLES:
            for raw in data["tables"][model.__tablename__]:
                values = dict(raw)
                for column in model.__table__.columns:
                    val = values[column.key]
                    if val is None:
                        continue
                    if isinstance(column.type, DateTime):
                        values[column.key] = datetime.fromisoformat(val)
                    elif column.type.python_type is UUID:
                        values[column.key] = UUID(val)
                # 历史 Run 必须终态；不继承已过期测试租约。
                if model is RunRow:
                    values.update(status="completed", owner=None, lease_until=None)
                db.add(model(**values))
            await db.flush()
        graph = await db.get(SessionGraphRow, UUID(data["session_id"]))
        graph.completed_rounds, graph.next_sync_round = 13, 15
        graph.reviewed_seq = graph.reviewed_rounds = graph.missed_syncs = 0
    return data


async def make_box(database, data, case):
    cfg = load("config.json")
    scenario, arm = case.split("-")
    number = int(scenario[1:])
    sid = UUID(data["session_id"])
    rid = uuid5(sid, case)
    objective = f"当前对象 entity:{data['fixture_ids']['entity']}。" + QUESTIONS[number] + COMMON
    settings = database.settings.model_copy(
        update={
            **cfg["context_settings"],
            "decision_memory_mode": "blackboard_v1" if arm == "C" else "current",
        }
    )
    async with database.transaction() as db:
        db.add(
            RunRow(
                id=rid,
                session_id=sid,
                objective=objective,
                provider_id="bailian",
                model_name=cfg["model"],
                status="running",
                owner="natural",
                owner_generation=1,
                lease_until=datetime.now(UTC) + timedelta(hours=2),
                prompt_version=str(cfg["prompt_version"]),
                config_snapshot={
                    "decision_memory_mode": settings.decision_memory_mode,
                    "experiment_case": case,
                },
            )
        )
    box = SimpleNamespace(
        db=database,
        run_id=rid,
        session_id=sid,
        owner="natural",
        owner_generation=1,
        control_version=0,
        settings=settings,
        artifacts=ArtifactStore(settings),
        scope=None,
        scope_revision=0,
        working_context=None,
        context_manifest=None,
        graph_review_window_id=None,
        graph_sync_snapshot=None,
        skill_manifest=[],
        provider_id="bailian",
        model_name=cfg["model"],
        sdk_retries_disabled=True,
        experiment_actions=[],
        experiment_unsent_requests=[],
    )
    bind_control(box)
    prefix = {**data["prefix"], "messages": messages_from_dict(data["prefix"]["messages"])}
    prefix.update(
        objective=objective,
        session_id=str(sid),
        active_run_id=str(rid),
        current_focus={"intent": "旧 Q 路线", "source": "explicit"},
        plan_revision=1,
    )
    prefix["messages"].append(HumanMessage(content=objective, id=f"objective-{rid}"))
    return box, prefix


@tool
async def read_local_execution(runtime: ToolRuntime[RunContext], operation_id: str) -> str:
    """只读当前 Session 的本地 Operation/Job/Artifact 最后观察；不联系执行端，不改变未知结果。"""
    box = runtime.context.toolbox
    await box.assert_control_current()
    data = await snapshot(box)
    ref = f"operation:{UUID(operation_id)}"
    operation = next((op for op in data.operations if op["record_ref"] == ref), None)
    return encoded(
        {
            "operation": operation,
            "artifacts": [a for a in data.artifacts if a["source_operation_ref"] == ref],
            "not_live_executor_check": True,
            "body_read": False,
        }
    )


@tool
async def simulate_check(
    runtime: ToolRuntime[RunContext],
    route: Literal["A", "B", "K", "U"],
    environment: Literal["Q", "R"],
    configuration: Literal["v1", "v2"],
) -> str:
    """在隔离夹具中进行一次本地模拟检查，绝不发网络请求。结果不代表真实漏洞验证。"""
    box: Any = runtime.context.toolbox
    await box.assert_control_current()
    isolated(box.db)
    # 稳定调用身份：重放复用，同一ID参数变化拒绝；不随机生成操作身份。
    operation_id = uuid5(box.run_id, str(runtime.tool_call_id))
    args = {"route": route, "environment": environment, "configuration": configuration}
    if route == "U":
        result = {"performed": False, "reason": "unknown_replay_refused"}
    else:
        result = load("simulator.json")[f"{route}/{environment}/{configuration}"]
    payload = {
        "simulated_only": True,
        "operation_id": str(operation_id),
        **args,
        "result": result,
        "not_real_security_validation": True,
    }
    async with box.db.transaction() as db:
        existing = await db.get(ToolOperationRow, operation_id)
        if existing:
            if existing.request_digest != digest(args):
                raise ValueError("simulated_operation_identity_conflict")
            return encoded(existing.result_snapshot)
        blob = box.artifacts.write_bytes(encoded(payload).encode(), media_type="application/json")
        aid = uuid5(operation_id, "result")
        db.add(
            ToolOperationRow(
                operation_id=operation_id,
                session_id=box.session_id,
                run_id=box.run_id,
                tool_call_id=str(runtime.tool_call_id),
                tool_name="simulate_check",
                request_digest=digest(args),
                effect_class="read_only",
                status="failed" if route == "U" else "succeeded",
                execution_context={"simulation": True, **args},
                result_snapshot=payload,
                result_artifact_ids=[str(aid)],
            )
        )
        db.add(
            ArtifactRow(
                id=aid,
                session_id=box.session_id,
                source_operation_id=operation_id,
                sha256=blob.sha256,
                byte_size=blob.byte_size,
                media_type=blob.media_type,
                relative_key=blob.relative_key,
                captured_at=blob.captured_at,
            )
        )
    box.experiment_actions.append(payload)
    return encoded(payload)


TOOLS = [
    search_history,
    read_artifact,
    read_local_execution,
    get_graph,
    update_graph,
    local_condition_check,
    simulate_check,
]


def tool_schemas():
    return [convert_to_openai_tool(t) for t in TOOLS]


class ExperimentLimit(RuntimeError):
    pass


class ToolLimits(AgentMiddleware[AgentGraphState, RunContext]):
    def __init__(self):
        self.rounds = self.calls = 0

    async def aafter_model(self, state, runtime):
        _ = runtime
        response = next((m for m in reversed(state["messages"]) if isinstance(m, AIMessage)), None)
        if (
            response
            and response.tool_calls
            and not response.additional_kwargs.get("workbench_graph_review_window")
        ):
            self.rounds += 1
            self.calls += len(response.tool_calls)
            cfg = load("config.json")
            if self.rounds > cfg["max_tool_rounds"] or self.calls > cfg["max_tool_calls"]:
                raise ExperimentLimit("tool_budget_exhausted_before_batch_execution")

    async def awrap_tool_call(self, request, handler):
        ctx: Any = request.runtime.context
        await ctx.toolbox.assert_control_current()
        return await handler(request)


class RequestEnvelope(AgentMiddleware[AgentGraphState, RunContext]):
    async def awrap_model_call(self, request, handler):
        ctx: Any = request.runtime.context
        profile = profile_for()
        estimate = estimate_request(
            request.messages,
            request.system_message,
            request.tools,
            profile,
            model=request.model,
            model_settings=request.model_settings,
            tool_choice=request.tool_choice,
        )
        if estimate.estimated_input_tokens > load("config.json")["max_estimated_input_tokens"]:
            ctx.toolbox.experiment_unsent_requests.append(
                {
                    "reason": "experimental_request_envelope_exceeded",
                    "estimate": estimate.to_dict(),
                    "manifest": ctx.toolbox.context_manifest,
                    "provider_sent": False,
                }
            )
            raise ExperimentLimit("experimental_request_envelope_exceeded_no_trimming")
        ctx.toolbox.context_manifest["experiment_request_estimate"] = estimate.to_dict()
        return await handler(request)


def profile_for():
    cfg = load("config.json")
    return ProviderProfile(
        provider_id="bailian",
        model_name=cfg["model"],
        base_url=cfg["endpoint"],
        secret_ref="experiment-only-never-resolved",
        context_limit=1_000_000,
        max_input_tokens=1_000_000,
        request_output_limit=cfg["max_completion_tokens"],
        output_reserve=cfg["max_completion_tokens"],
        max_response_recoveries=0,
    )


def model_for(client, *, key="offline-placeholder"):
    cfg = load("config.json")
    return BailianChatOpenAI(
        model=cfg["model"],
        base_url=cfg["endpoint"],
        api_key=SecretStr(key),
        http_async_client=client,
        max_retries=0,
        temperature=cfg["temperature"],
        max_completion_tokens=cfg["max_completion_tokens"],
        reasoning_effort=cfg["reasoning_effort"],
        use_responses_api=False,
        streaming=False,
        timeout=cfg["timeout_seconds"],
    )


def build_graph(model, saver, arm):
    middleware = [
        CommandGuardMiddleware(),
        WorkingContextMiddleware(),
        ResponseRecoveryMiddleware(max_recoveries=0),
        ContextBuilderMiddleware(profile_for()),
    ]
    if arm == "B":
        middleware.append(RecentListMiddleware())
    middleware.extend([RequestEnvelope(), ModelObserverMiddleware(), ToolLimits()])
    return create_agent(
        model,
        tools=TOOLS,
        system_prompt=SYSTEM_PROMPT,
        state_schema=AgentGraphState,
        context_schema=RunContext,
        middleware=middleware,
        checkpointer=saver,
    )


class WireGate(httpx.AsyncBaseTransport):
    """每个真实交换之前计入不可回滚名额；SDK重试关闭，异常与截断立即停止。"""

    def __init__(self, inner, case, *, live=False, ledger=None):
        self.inner, self.case, self.live, self.ledger = inner, case, live, ledger
        self.rows: list[dict[str, Any]] = []
        self.stopped = False

    async def handle_async_request(self, request):
        cfg = load("config.json")
        phase = "graph_review" if graph_review_scope.get() else "agent"
        if phase == "graph_review" and self.case.endswith("C"):
            raise ExperimentLimit("C_automatic_graph_review_forbidden")
        if (
            request.method != "POST"
            or str(request.url) != cfg["endpoint"].rstrip("/") + "/chat/completions"
        ):
            raise ExperimentLimit("frozen_endpoint_mismatch")
        prior = [r for r in self.rows if r["phase"] == phase]
        if self.stopped or len(prior) >= (2 if phase == "graph_review" else 6):
            raise ExperimentLimit("physical_request_budget_exhausted")
        wire = json.loads(request.content)
        expected = {
            "model": cfg["model"],
            "temperature": cfg["temperature"],
            "max_completion_tokens": cfg["max_completion_tokens"],
            "reasoning_effort": cfg["reasoning_effort"],
            "stream": False,
        }
        if any(wire.get(k) != v for k, v in expected.items()) or any(
            k in wire for k in ("max_tokens", "tool_choice", "response_format", "enable_thinking")
        ):
            raise ExperimentLimit("frozen_wire_settings_mismatch")
        names = {t["function"]["name"] for t in wire["tools"]}
        allowed = (
            {"get_graph", "update_graph"} if phase == "graph_review" else {t.name for t in TOOLS}
        )
        if names != allowed:
            raise ExperimentLimit("frozen_tool_permissions_mismatch")
        expected_schemas = [t for t in load("tools.json") if t["function"]["name"] in allowed]
        if wire["tools"] != expected_schemas:
            raise ExperimentLimit("frozen_tool_schema_mismatch")
        if self.live and not self.rows and visible(wire) != load(f"initial/{self.case}.json"):
            raise ExperimentLimit("frozen_initial_request_mismatch_before_send")
        from agent_workbench.models.context_budget import serialized

        row: dict[str, Any] = {
            "index": len(self.rows) + 1,
            "phase": phase,
            "request": visible(wire),
            "wire_sha256": hashlib.sha256(serialized(wire).encode()).hexdigest(),
            "status": "reserved",
            "graph_review_window": graph_review_scope.get(),
        }
        if self.live:
            self.reserve_live(row)
        self.rows.append(row)
        start = time.monotonic()
        try:
            response = await self.inner.handle_async_request(request)
            raw_bytes = await response.aread()
            raw = json.loads(raw_bytes)
            choice = (raw.get("choices") or [{}])[0]
            row.update(
                status="response",
                http_status=response.status_code,
                elapsed_seconds=time.monotonic() - start,
                finish_reason=choice.get("finish_reason"),
                usage=raw.get("usage"),
                returned_model=raw.get("model"),
                request_id=raw.get("id"),
                system_fingerprint=raw.get("system_fingerprint"),
            )
            message = choice.get("message") or {}
            row["native_tool_names"] = [
                c.get("function", {}).get("name") for c in message.get("tool_calls") or []
            ]
            if response.status_code != 200 or row["finish_reason"] not in {"stop", "tool_calls"}:
                self.stopped = True
                raise ExperimentLimit("protocol_or_provider_failure")
            # 普通阶段协议错误不自动补测；维护阶段沿用既有两次格式修正合同。
            if phase == "agent":
                schemas = {t.name: t.tool_call_schema for t in TOOLS}
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    name = function.get("name")
                    if name not in schemas:
                        raise ExperimentLimit("disallowed_tool_before_execution")
                    try:
                        schemas[name].model_validate(json.loads(function.get("arguments", "")))
                    except (ValueError, TypeError) as exc:
                        raise ExperimentLimit("invalid_arguments_before_execution") from exc
            return httpx.Response(
                response.status_code, headers=response.headers, content=raw_bytes, request=request
            )
        except asyncio.CancelledError:
            self.stopped = True
            row["error_category"] = "cancelled"
            raise
        except Exception as exc:
            self.stopped = True
            row["error_category"] = type(exc).__name__
            raise
        finally:
            if self.live:
                assert self.ledger is not None
                self.append_live(
                    {
                        "case": self.case,
                        "record": "result",
                        **{k: v for k, v in row.items() if k != "request"},
                    }
                )
                if self.stopped:
                    (self.ledger.parent / "STOP.json").write_text(
                        encoded(
                            {
                                "case": self.case,
                                "reason": row.get("error_category"),
                                "requires_new_review": True,
                            }
                        ),
                        encoding="utf-8",
                    )

    def append_live(self, row):
        assert self.ledger is not None
        with self.ledger.open("a", encoding="utf-8") as stream:
            stream.write(encoded(row) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def reserve_live(self, row):
        assert self.ledger is not None
        old = (
            [json.loads(line) for line in self.ledger.read_text(encoding="utf-8").splitlines()]
            if self.ledger.exists()
            else []
        )
        if sum(r.get("record") == "reserved" for r in old) >= 66:
            raise ExperimentLimit("global_provider_budget_exhausted")
        self.append_live(
            {
                "case": self.case,
                "record": "reserved",
                "index": row["index"],
                "phase": row["phase"],
                "wire_sha256": row["wire_sha256"],
            }
        )

    async def aclose(self):
        await self.inner.aclose()


async def run_case(database, case, transport, result_path):
    started = time.monotonic()
    data = await import_history(database)
    box, prefix = await make_box(database, data, case)
    before = await snapshot(box)
    status, error, checkpoint_id = "completed", None, None
    key = (
        os.environ.get("REXAK_MEMORY_LIVE_API_KEY", "") if transport.live else "offline-placeholder"
    )
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        model = model_for(client, key=key)
        thread_id = f"memory-live-{case}-{uuid4()}"
        config: Any = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
        async with open_checkpointer_connection(database.settings) as connection:
            graph = build_graph(model, AsyncPostgresSaver(connection), case[-1])
            try:
                await graph.ainvoke(
                    cast(Any, prefix),
                    config,
                    context=RunContext(toolbox=cast(Any, box), thread_id=thread_id),
                )
            except Exception as exc:
                status, error = "stopped", type(exc).__name__
            saved = await graph.aget_state(config)
            checkpoint_id = (saved.config.get("configurable") or {}).get("checkpoint_id")
    manifests = await persisted_manifests(box)
    async with database.session() as db:
        calls = list(
            (await db.scalars(select(ModelCallRow).where(ModelCallRow.run_id == box.run_id))).all()
        )
    public_messages = visible(messages_to_dict(saved.values.get("messages", [])))
    exported = {
        "case": case,
        "status": status,
        "error_category": error,
        "run_id": str(box.run_id),
        "session_id": str(box.session_id),
        "thread_id": thread_id,
        "checkpoint_id": checkpoint_id,
        "calls": transport.rows,
        "actions": box.experiment_actions,
        "messages": public_messages,
        "model_calls": [
            {
                k: str(getattr(c, k)) if k.endswith("_id") else getattr(c, k)
                for k in (
                    "model_call_id",
                    "logical_call_id",
                    "phase",
                    "retry_index",
                    "input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "cache_read_tokens",
                    "usage_status",
                    "cost_status",
                )
            }
            for c in calls
        ],
        "manifests": manifests,
        "source_snapshot_sha256": digest(before.operations),
        "real_provider_requests": len(transport.rows) if transport.live else 0,
        "semantic_review": "PENDING_BLIND_REVIEW",
        "unsent_requests": box.experiment_unsent_requests,
        "trajectory_elapsed_seconds": time.monotonic() - started,
    }
    exported["evidence_exposure"] = evidence_exposure(exported)
    prefix_calls = {
        c["id"] for m in prefix["messages"] if isinstance(m, AIMessage) for c in m.tool_calls
    }
    exported["tool_loop_exercised"] = any(
        m.get("role") == "tool" and m.get("tool_call_id") not in prefix_calls
        for c in transport.rows
        for m in c["request"]["messages"]
    )
    exported["artifact_body_cycle_exercised"] = any(
        a.get("body_visible") for r in exported["evidence_exposure"] for a in r["artifact_views"]
    )
    write_json(result_path, exported)
    return exported


def evidence_exposure(result):
    """只记录最终请求确实含正文的时点；不把 Tool 执行或 UUID 出现当语义成功。"""
    rows = []
    for call in result["calls"]:
        messages = call["request"]["messages"]
        invocations = {c["id"]: c["function"] for m in messages for c in m.get("tool_calls", [])}
        bodies = []
        for message in messages:
            if message["role"] != "tool":
                continue
            invocation = invocations.get(message.get("tool_call_id"), {})
            if invocation.get("name") != "read_artifact":
                continue
            try:
                value = json.loads(message["content"])
            except ValueError:
                bodies.append(
                    {
                        "tool_call_id": message.get("tool_call_id"),
                        "body_visible": False,
                        "parsing_status": "invalid_json_not_evidence",
                    }
                )
                continue
            data = value.get("data", {})
            if not isinstance(data, dict):
                continue
            text = data.get("text")
            bodies.append(
                {
                    "artifact_id": data.get("artifact_id"),
                    "source_operation_id": data.get("source_operation_id"),
                    "body_visible": bool(text),
                    "complete_text_view": bool(text)
                    and not data.get("truncated", True)
                    and data.get("offset") == 0,
                    "text_sha256": hashlib.sha256(str(text).encode()).hexdigest(),
                    "encoding_note": data.get("encoding_note"),
                    "tool_call_id": message.get("tool_call_id"),
                }
            )
        rows.append(
            {
                "request_index": call["index"],
                "phase": call["phase"],
                "artifact_views": bodies,
                "ids_alone_are_not_body": True,
            }
        )
    return rows
