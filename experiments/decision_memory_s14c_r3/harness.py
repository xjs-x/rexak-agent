"""S14-C-R3 独立装载与响应记录边界；Agent 循环仍由原 create_agent 构造。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

import httpx
from experiments.decision_memory_live_v1 import harness as original
from langchain_core.messages import AIMessage, HumanMessage, messages_from_dict, messages_to_dict
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.tables import ModelCallRow, RunRow

ROOT = Path(__file__).resolve().parent
CASE = "S14-C-R3"
BASE_CASE = "S14-C"
MAX_ESTIMATED_INPUT_TOKENS = 131072


def identity():
    history = original.load("history.json")
    sid = UUID(history["session_id"])
    return {
        "case": CASE,
        "base_case": BASE_CASE,
        "iteration": 3,
        "run_id": str(uuid5(sid, CASE)),
        "thread_id": f"memory-live-{CASE}-{uuid5(sid, CASE + '/thread')}",
        "session_id": str(sid),
        "history_sha256": hashlib.sha256(
            (original.BUNDLE / "history.json").read_bytes()
        ).hexdigest(),
        "max_provider_requests": 6,
        "max_estimated_input_tokens": MAX_ESTIMATED_INPUT_TOKENS,
        "canonical_results": "experiments/decision_memory_s14c_r3/live-results",
    }


def exception_chain(exc):
    """只存异常类型及代码位置，绝不导出包含原始正文/密钥的异常字符串。"""
    result, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        frames = []
        tb = exc.__traceback__
        while tb:
            frames.append(
                {
                    "file": tb.tb_frame.f_code.co_filename,
                    "line": tb.tb_lineno,
                    "function": tb.tb_frame.f_code.co_name,
                }
            )
            tb = tb.tb_next
        item = {"type": type(exc).__name__, "frames": frames}
        if isinstance(exc, original.ExperimentLimit):
            item["code"] = str(exc)
        result.append(item)
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return result


class DecodedBoundary(httpx.AsyncBaseTransport):
    """aread 返回已解码实体，重建响应时删除实体编码和旧长度头。"""

    def __init__(self, inner, owner):
        self.inner, self.owner = inner, owner

    async def handle_async_request(self, request):
        row = self.owner.rows[-1]
        row["failure_stage"] = "http_exchange"
        response = await self.inner.handle_async_request(request)
        row.update(
            http_status=response.status_code,
            response_content_encoding=response.headers.get("content-encoding"),
            failure_stage="response_body_decode",
        )
        try:
            body = await response.aread()
            row["decoded_body_sha256"] = hashlib.sha256(body).hexdigest()
            # 只保留哈希和可见消息，隐藏推理正文仍不写入报告/ledger。
            row["failure_stage"] = "json_and_tool_validation"
            headers = response.headers.copy()
            for key in ("content-encoding", "content-length", "transfer-encoding"):
                headers.pop(key, None)
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=body,
                request=request,
                extensions=response.extensions,
            )
        finally:
            await response.aclose()

    async def aclose(self):
        await self.inner.aclose()


def annotate(row):
    error = row.get("error_category")
    if error:
        row["status"] = (
            "timeout"
            if error in {"ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout"}
            else "cancelled"
            if error == "cancelled"
            else "http_error"
            if row.get("http_status", 200) != 200
            else "protocol_error"
        )
    else:
        row["status"] = "http_response_validated"
        row.pop("failure_stage", None)
    row["model_conversion"] = "NOT_OBSERVED_AT_TRANSPORT_BOUNDARY"


class WireGate(original.WireGate):
    """继承冻结合同及工具拦截；独立账本，物理额度与原活动累计检查。"""

    def __init__(self, inner, *, durable=False):
        self.mock_transport = isinstance(inner, httpx.MockTransport)
        super().__init__(
            DecodedBoundary(inner, self),
            BASE_CASE,
            live=durable,
            ledger=ROOT / "live-results/ledger.jsonl" if durable else None,
        )

    async def handle_async_request(self, request):
        cfg = original.load("config.json")
        if original.graph_review_scope.get():
            raise original.ExperimentLimit("C_automatic_graph_review_forbidden")
        if (
            request.method != "POST"
            or str(request.url) != cfg["endpoint"].rstrip("/") + "/chat/completions"
        ):
            raise original.ExperimentLimit("frozen_endpoint_mismatch")
        if self.stopped or len(self.rows) >= 6:
            raise original.ExperimentLimit("physical_request_budget_exhausted")
        wire = json.loads(request.content)
        expected = {
            k: cfg[k]
            for k in ("model", "temperature", "max_completion_tokens", "reasoning_effort", "stream")
        }
        if any(wire.get(k) != v for k, v in expected.items()) or any(
            k in wire for k in ("max_tokens", "tool_choice", "response_format", "enable_thinking")
        ):
            raise original.ExperimentLimit("frozen_wire_settings_mismatch")
        if wire.get("tools") != original.load("tools.json"):
            raise original.ExperimentLimit("frozen_tool_schema_mismatch")
        if (
            self.live
            and not self.rows
            and original.visible(wire)
            != json.loads((ROOT / "bundle/initial.json").read_text(encoding="utf-8"))
        ):
            raise original.ExperimentLimit("frozen_initial_request_mismatch_before_send")
        from agent_workbench.models.context_budget import serialized

        row = {
            "index": len(self.rows) + 1,
            "phase": "agent",
            "request": original.visible(wire),
            "wire_sha256": hashlib.sha256(serialized(wire).encode()).hexdigest(),
            "visible_request_sha256": hashlib.sha256(
                serialized(original.visible(wire)).encode()
            ).hexdigest(),
            "status": "reserved",
            "graph_review_window": None,
        }
        if self.live:
            self.reserve_live(row)
        self.rows.append(row)
        started = time.monotonic()
        try:
            response = await self.inner.handle_async_request(request)
            raw = response.json()
            choice = (raw.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            row.update(
                http_status=response.status_code,
                finish_reason=choice.get("finish_reason"),
                usage=raw.get("usage"),
                returned_model=raw.get("model"),
                request_id=raw.get("id"),
                system_fingerprint=raw.get("system_fingerprint"),
            )
            row["native_tool_names"] = [
                c.get("function", {}).get("name") for c in message.get("tool_calls") or []
            ]
            row["visible_response_message"] = original.visible(message)
            if response.status_code != 200 or row["finish_reason"] not in {"stop", "tool_calls"}:
                raise original.ExperimentLimit("protocol_or_provider_failure")
            calls = message.get("tool_calls") or []
            if bool(calls) != (row["finish_reason"] == "tool_calls"):
                raise original.ExperimentLimit("finish_reason_tool_calls_mismatch")
            schemas: dict[str, Any] = {t.name: t.tool_call_schema for t in original.TOOLS}
            seen = set()
            for call in calls:
                cid = call.get("id")
                if (
                    not isinstance(cid, str)
                    or not cid
                    or cid in seen
                    or call.get("type") != "function"
                ):
                    raise original.ExperimentLimit("invalid_or_duplicate_tool_call_identity")
                seen.add(cid)
                function = call.get("function") or {}
                name = function.get("name")
                if name not in schemas:
                    raise original.ExperimentLimit("disallowed_tool_before_execution")
                try:
                    schemas[name].model_validate(json.loads(function.get("arguments", "")))
                except (ValueError, TypeError) as exc:
                    raise original.ExperimentLimit("invalid_arguments_before_execution") from exc
            return response
        except BaseException as exc:
            self.stopped = True
            row["error_category"] = (
                "cancelled" if isinstance(exc, asyncio.CancelledError) else type(exc).__name__
            )
            row["failure_code"] = (
                str(exc) if isinstance(exc, original.ExperimentLimit) else row["error_category"]
            )
            row["exception_chain"] = exception_chain(exc)
            raise
        finally:
            row["elapsed_seconds"] = time.monotonic() - started
            annotate(row)
            if self.live:
                self.append_live(
                    {"record": "result", **{k: v for k, v in row.items() if k != "request"}}
                )
                if self.stopped:
                    original.write_json(
                        ROOT / "live-results/STOP.json",
                        {
                            "case": CASE,
                            "reason": row.get("failure_code"),
                            "requires_new_review": True,
                        },
                        exclusive=True,
                    )

    def append_live(self, row):
        if row["record"] == "result":
            annotate(self.rows[-1])
            row.update({k: v for k, v in self.rows[-1].items() if k != "request"})
        super().append_live({**row, "case": CASE, "run_id": identity()["run_id"]})

    def reserve_live(self, row):
        results = ROOT / "live-results"
        claim = json.loads((results / "ATTEMPT.json").read_text(encoding="utf-8"))
        if claim["identity"] != identity() or (results / "STOP.json").exists():
            raise original.ExperimentLimit("iteration_claim_or_stop_mismatch")
        assert self.ledger is not None
        old = read_ledger(self.ledger)
        inherited = read_ledger(original.ROOT / "live-results/ledger.jsonl") + read_ledger(
            ROOT.parent / "decision_memory_s14c_r2/live-results/ledger.jsonl"
        )
        own_count = sum(r.get("record") == "reserved" for r in old)
        total = own_count + sum(r.get("record") == "reserved" for r in inherited)
        if own_count >= 6 or total >= 66:
            raise original.ExperimentLimit("durable_physical_request_budget_exhausted")
        super().reserve_live(row)


def read_ledger(path):
    return (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )


async def make_box(database, data):
    """始终从原固定历史新建 R3 Run；不读取旧 checkpoint 或旧实验中途状态。"""
    cfg, ids = original.load("config.json"), identity()
    sid, rid = UUID(ids["session_id"]), UUID(ids["run_id"])
    objective = (
        f"当前对象 entity:{data['fixture_ids']['entity']}。"
        + original.QUESTIONS[14]
        + original.COMMON
    )
    settings = database.settings.model_copy(
        update={**cfg["context_settings"], "decision_memory_mode": "blackboard_v1"}
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
                config_snapshot={"decision_memory_mode": "blackboard_v1", "experiment_case": CASE},
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
    original.bind_control(box)
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


class RequestEnvelope(original.AgentMiddleware[original.AgentGraphState, RunContext]):
    async def awrap_model_call(self, request, handler):
        ctx: Any = request.runtime.context
        profile = original.profile_for()
        estimate = original.estimate_request(
            request.messages,
            request.system_message,
            request.tools,
            profile,
            model=request.model,
            model_settings=request.model_settings,
            tool_choice=request.tool_choice,
        )
        if estimate.estimated_input_tokens > MAX_ESTIMATED_INPUT_TOKENS:
            ctx.toolbox.experiment_unsent_requests.append(
                {
                    "reason": "experimental_request_envelope_exceeded",
                    "estimate": estimate.to_dict(),
                    "manifest": ctx.toolbox.context_manifest,
                    "provider_sent": False,
                }
            )
            raise original.ExperimentLimit("experimental_request_envelope_exceeded_no_trimming")
        ctx.toolbox.context_manifest["experiment_request_estimate"] = estimate.to_dict()
        return await handler(request)


def build_graph(model, saver):
    """原 C 组官方图，仅替换独立实验的输入包络中间件。"""
    middleware = [
        original.CommandGuardMiddleware(),
        original.WorkingContextMiddleware(),
        original.ResponseRecoveryMiddleware(max_recoveries=0),
        original.ContextBuilderMiddleware(original.profile_for()),
        RequestEnvelope(),
        original.ModelObserverMiddleware(),
        original.ToolLimits(),
    ]
    return original.create_agent(
        model,
        tools=original.TOOLS,
        system_prompt=original.SYSTEM_PROMPT,
        state_schema=original.AgentGraphState,
        context_schema=RunContext,
        middleware=middleware,
        checkpointer=saver,
    )


async def run_case(database, transport, result_path, *, key="offline-placeholder"):
    original.isolated(database)
    if result_path.exists():
        raise RuntimeError("result_exists_no_overwrite")
    started = time.monotonic()
    data = await original.import_history(database)
    box, prefix = await make_box(database, data)
    before = await original.snapshot(box)
    status, error, chain = "completed", None, []
    thread = identity()["thread_id"]
    if not transport.live:
        # 多个 Mock 共享测试库时也不得读取上一轨迹的 checkpoint。
        thread += f"-mock-{uuid4()}"
    config: Any = {"configurable": {"thread_id": thread}, "recursion_limit": 100}
    async with (
        httpx.AsyncClient(transport=transport, trust_env=False) as client,
        open_checkpointer_connection(database.settings) as connection,
    ):
        graph = build_graph(original.model_for(client, key=key), AsyncPostgresSaver(connection))
        if (await graph.aget_state(config)).values:
            raise RuntimeError("thread_already_exists_no_resume")
        try:
            await graph.ainvoke(
                cast(Any, prefix),
                config,
                context=RunContext(toolbox=cast(Any, box), thread_id=thread),
            )
        except (Exception, asyncio.CancelledError) as exc:
            status, error, chain = "stopped", type(exc).__name__, exception_chain(exc)
        saved = await graph.aget_state(config)
    manifests = await original.persisted_manifests(box)
    async with database.session() as db:
        calls = list(
            (await db.scalars(select(ModelCallRow).where(ModelCallRow.run_id == box.run_id))).all()
        )
    exported = {
        **identity(),
        "thread_id": thread,
        "status": status,
        "error_category": error,
        "exception_chain": chain,
        "stop_reason": next((c["code"] for c in chain if "code" in c), error),
        "checkpoint_id": (saved.config.get("configurable") or {}).get("checkpoint_id"),
        "pending_nodes": list(saved.next),
        "calls": transport.rows,
        "actions": box.experiment_actions,
        "messages": original.visible(messages_to_dict(saved.values.get("messages", []))),
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
                    "error_code",
                )
            }
            for c in calls
        ],
        "manifests": manifests,
        "source_snapshot_sha256": original.digest(before.operations),
        "transport_mode": "mock" if transport.mock_transport else "live",
        "real_provider_requests": len(transport.rows)
        if transport.live and not transport.mock_transport
        else 0,
        "semantic_review": "PENDING_BLIND_REVIEW",
        "unsent_requests": box.experiment_unsent_requests,
        "trajectory_elapsed_seconds": time.monotonic() - started,
    }
    exported["evidence_exposure"] = original.evidence_exposure(exported)
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
    original.write_json(result_path, exported, exclusive=True)
    return exported


def claim_attempt(approved_hash):
    """固定位置的一次性身份登记；改变结果路径不会获得新的逻辑名额。"""
    results = ROOT / "live-results"
    results.mkdir(exist_ok=True)
    if any(
        (results / name).exists()
        for name in ("ATTEMPT.json", "STOP.json", "ledger.jsonl", f"{CASE}.json")
    ):
        raise RuntimeError("iteration_already_attempted_no_replay")
    original.write_json(
        results / "ATTEMPT.json",
        {"identity": identity(), "approved_freeze": approved_hash},
        exclusive=True,
    )


async def execute_authorized(database, approved_hash, request_cap):
    from experiments.decision_memory_s14c_r3.prepare import verify

    if approved_hash != verify() or request_cap != 6:
        raise RuntimeError("explicit_r3_hash_and_six_requests_required")
    key = os.environ.get("REXAK_MEMORY_LIVE_API_KEY")
    if not key:
        raise RuntimeError("experiment_only_key_required")
    claim_attempt(approved_hash)
    results = ROOT / "live-results"
    try:
        gate = WireGate(httpx.AsyncHTTPTransport(retries=0), durable=True)
        result = await run_case(database, gate, results / f"{CASE}.json", key=key)
        gate.append_live(
            {
                "record": "trajectory",
                "status": result["status"],
                "error_category": result["error_category"],
                "stop_reason": result["stop_reason"],
                "exception_chain": result["exception_chain"],
                "physical_requests": len(gate.rows),
                "checkpoint_id": result["checkpoint_id"],
                "transport_mode": result["transport_mode"],
            }
        )
        if result["status"] != "completed" and not (results / "STOP.json").exists():
            original.write_json(
                results / "STOP.json",
                {"case": CASE, "reason": result["stop_reason"]},
                exclusive=True,
            )
        return result
    except BaseException as exc:
        if not (results / "STOP.json").exists():
            original.write_json(
                results / "STOP.json", {"case": CASE, "reason": type(exc).__name__}, exclusive=True
            )
        raise
