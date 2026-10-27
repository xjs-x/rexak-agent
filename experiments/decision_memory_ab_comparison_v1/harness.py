"""S14 A/B 独立对照；保留官方循环、生产上下文与自动图核对。"""

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
from sqlalchemy.engine import make_url

from agent_workbench.agent.context import RunContext
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.tables import ModelCallRow, RunRow

ROOT = Path(__file__).resolve().parent
ARMS = ("A", "B")
MAX_ESTIMATED_INPUT_TOKENS = 131072


def identity(arm):
    if arm not in ARMS:
        raise ValueError("only_new_A_and_B_are_authorized")
    history = original.load("history.json")
    sid = UUID(history["session_id"])
    case = f"S14-{arm}-AB1"
    return {
        "case": case,
        "arm": arm,
        "base_case": f"S14-{arm}",
        "iteration": "ab_comparison_v1",
        "run_id": str(uuid5(sid, case)),
        "thread_id": f"memory-live-{case}-{uuid5(sid, case + '/thread')}",
        "session_id": str(sid),
        "history_sha256": hashlib.sha256(
            (original.BUNDLE / "history.json").read_bytes()
        ).hexdigest(),
        "decision_memory_mode": "current",
        "recent_k": 5 if arm == "B" else None,
        "max_provider_requests": 8,
        "max_agent_requests": 6,
        "max_graph_review_requests": 2,
        "comparison_request_cap": 16,
        "max_estimated_input_tokens": MAX_ESTIMATED_INPUT_TOKENS,
        "canonical_results": f"experiments/decision_memory_ab_comparison_v1/live-results/{arm}",
    }


def results_for(arm):
    identity(arm)
    return ROOT / "live-results" / arm


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

    def __init__(self, inner, arm="A", *, durable=False):
        self.arm = arm
        self.mock_transport = isinstance(inner, httpx.MockTransport)
        super().__init__(
            DecodedBoundary(inner, self),
            f"S14-{arm}",
            live=durable,
            ledger=results_for(arm) / "ledger.jsonl" if durable else None,
        )

    async def handle_async_request(self, request):
        cfg = original.load("config.json")
        phase = "graph_review" if original.graph_review_scope.get() else "agent"
        prior = [row for row in self.rows if row["phase"] == phase]
        if (
            request.method != "POST"
            or str(request.url) != cfg["endpoint"].rstrip("/") + "/chat/completions"
        ):
            raise original.ExperimentLimit("frozen_endpoint_mismatch")
        if (
            self.stopped
            or len(self.rows) >= 8
            or len(prior) >= (2 if phase == "graph_review" else 6)
        ):
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
        allowed = (
            {"get_graph", "update_graph"}
            if phase == "graph_review"
            else {t.name for t in original.TOOLS}
        )
        expected_schemas = [
            s for s in original.load("tools.json") if s["function"]["name"] in allowed
        ]
        if wire.get("tools") != expected_schemas:
            raise original.ExperimentLimit("frozen_tool_schema_mismatch")
        if (
            self.live
            and not self.rows
            and original.visible(wire)
            != json.loads((ROOT / f"bundle/initial/{self.arm}.json").read_text(encoding="utf-8"))
        ):
            raise original.ExperimentLimit("frozen_initial_request_mismatch_before_send")
        from agent_workbench.models.context_budget import serialized

        row = {
            "index": len(self.rows) + 1,
            "phase": phase,
            "phase_index": len(prior) + 1,
            "reserved_at_utc": datetime.now(UTC).isoformat(),
            "request": original.visible(wire),
            "wire_sha256": hashlib.sha256(serialized(wire).encode()).hexdigest(),
            "visible_request_sha256": hashlib.sha256(
                serialized(original.visible(wire)).encode()
            ).hexdigest(),
            "status": "reserved",
            "graph_review_window": original.graph_review_scope.get(),
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
            if phase == "agent":
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
                        raise original.ExperimentLimit(
                            "invalid_arguments_before_execution"
                        ) from exc
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
                        results_for(self.arm) / "STOP.json",
                        {
                            "case": identity(self.arm)["case"],
                            "reason": row.get("failure_code"),
                            "requires_new_review": True,
                        },
                        exclusive=True,
                    )

    def append_live(self, row):
        if row["record"] == "result":
            annotate(self.rows[-1])
            row.update({k: v for k, v in self.rows[-1].items() if k != "request"})
        super().append_live(
            {
                **row,
                "case": identity(self.arm)["case"],
                "arm": self.arm,
                "run_id": identity(self.arm)["run_id"],
            }
        )

    def reserve_live(self, row):
        results = results_for(self.arm)
        claim = json.loads((results / "ATTEMPT.json").read_text(encoding="utf-8"))
        if (
            claim["identity"] != identity(self.arm)
            or (results / "STOP.json").exists()
            or (ROOT / "INFRASTRUCTURE_STOP.json").exists()
        ):
            raise original.ExperimentLimit("iteration_claim_or_stop_mismatch")
        assert self.ledger is not None
        own = read_ledger(self.ledger)
        combined = [row for arm in ARMS for row in read_ledger(results_for(arm) / "ledger.jsonl")]
        prior = [
            row
            for name in (
                "decision_memory_live_v1",
                "decision_memory_s14c_r2",
                "decision_memory_s14c_r3",
            )
            for row in read_ledger(ROOT.parent / name / "live-results/ledger.jsonl")
        ]
        reserved = [r for r in own if r.get("record") == "reserved"]
        phase_count = sum(r.get("phase") == row["phase"] for r in reserved)
        combined_count = sum(r.get("record") == "reserved" for r in combined)
        campaign_count = combined_count + sum(r.get("record") == "reserved" for r in prior)
        if (
            len(reserved) >= 8
            or phase_count >= (2 if row["phase"] == "graph_review" else 6)
            or combined_count >= 16
            or campaign_count >= 66
        ):
            raise original.ExperimentLimit("durable_physical_request_budget_exhausted")
        super().reserve_live(row)


def read_ledger(path):
    return (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )


async def make_box(database, data, arm):
    """始终从原固定历史新建 A/B Run；不读取旧 checkpoint 或旧实验中途状态。"""
    cfg, ids = original.load("config.json"), identity(arm)
    sid, rid = UUID(ids["session_id"]), UUID(ids["run_id"])
    objective = (
        f"当前对象 entity:{data['fixture_ids']['entity']}。"
        + original.QUESTIONS[14]
        + original.COMMON
    )
    settings = database.settings.model_copy(
        update={**cfg["context_settings"], "decision_memory_mode": "current"}
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
                config_snapshot={"decision_memory_mode": "current", "experiment_case": ids["case"]},
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


def build_graph(model, saver, arm):
    """原 A/B 官方图；B 仅增加原 RecentListMiddleware。"""
    middleware = [
        original.CommandGuardMiddleware(),
        original.WorkingContextMiddleware(),
        original.ResponseRecoveryMiddleware(max_recoveries=0),
        original.ContextBuilderMiddleware(original.profile_for()),
        *([original.RecentListMiddleware()] if arm == "B" else []),
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
    arm = transport.arm
    if result_path.exists():
        raise RuntimeError("result_exists_no_overwrite")
    started = time.monotonic()
    data = await original.import_history(database)
    box, prefix = await make_box(database, data, arm)
    before = await original.snapshot(box)
    status, error, chain = "completed", None, []
    thread = identity(arm)["thread_id"]
    if not transport.live:
        # 多个 Mock 共享测试库时也不得读取上一轨迹的 checkpoint。
        thread += f"-mock-{uuid4()}"
    config: Any = {"configurable": {"thread_id": thread}, "recursion_limit": 100}
    async with (
        httpx.AsyncClient(transport=transport, trust_env=False) as client,
        open_checkpointer_connection(database.settings) as connection,
    ):
        graph = build_graph(
            original.model_for(client, key=key), AsyncPostgresSaver(connection), arm
        )
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
        **identity(arm),
        "thread_id": thread,
        "status": status,
        "error_category": error,
        "exception_chain": chain,
        "stop_reason": next((c["code"] for c in chain if "code" in c), error),
        "checkpoint_id": (saved.config.get("configurable") or {}).get("checkpoint_id"),
        "database_name": make_url(database.settings.database_url).database,
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


def claim_attempt(approved_hash, arm):
    results = results_for(arm)
    results.mkdir(parents=True, exist_ok=True)
    if (ROOT / "INFRASTRUCTURE_STOP.json").exists() or any(
        (results / n).exists() for n in ("ATTEMPT.json", "STOP.json", "ledger.jsonl", "result.json")
    ):
        raise RuntimeError("iteration_already_attempted_no_replay")
    original.write_json(
        results / "ATTEMPT.json",
        {"identity": identity(arm), "approved_freeze": approved_hash},
        exclusive=True,
    )


def audit_a_before_b():
    path = results_for("A") / "result.json"
    if not path.exists():
        raise RuntimeError("A_must_finish_and_be_preserved_before_B")
    result = json.loads(path.read_text(encoding="utf-8"))
    rows = read_ledger(results_for("A") / "ledger.jsonl")
    reserved = [r for r in rows if r.get("record") == "reserved"]
    outcomes = [r for r in rows if r.get("record") == "result"]
    if (
        result["run_id"] != identity("A")["run_id"]
        or len(reserved) != len(outcomes)
        or len(reserved) != len(result["calls"])
        or result["real_provider_requests"]
        != (len(reserved) if result["transport_mode"] == "live" else 0)
        or len([r for r in rows if r.get("record") == "trajectory"]) != 1
    ):
        raise RuntimeError("shared_infrastructure_ledger_integrity_failure")
    for request, response in zip(reserved, outcomes, strict=True):
        if (
            request["index"] != response["index"]
            or request["wire_sha256"] != response["wire_sha256"]
        ):
            raise RuntimeError("shared_infrastructure_ledger_integrity_failure")
    if any(c.get("http_status") in {401, 403} for c in result["calls"]):
        raise RuntimeError("shared_provider_authorization_failure")
    if result["status"] != "completed":
        safe = {
            "physical_request_budget_exhausted",
            "durable_physical_request_budget_exhausted",
            "tool_budget_exhausted_before_batch_execution",
            "experimental_request_envelope_exceeded_no_trimming",
            "protocol_or_provider_failure",
            "invalid_arguments_before_execution",
            "disallowed_tool_before_execution",
            "invalid_or_duplicate_tool_call_identity",
            "finish_reason_tool_calls_mismatch",
        }
        model_quality_stop = result.get("error_category") in {
            "ModelResponseUnavailable",
            "ModelResponseRefused",
        }
        transport_stop = any(
            c.get("error_category")
            in {
                "ReadTimeout",
                "ConnectTimeout",
                "WriteTimeout",
                "PoolTimeout",
                "ReadError",
                "ConnectError",
                "RemoteProtocolError",
            }
            for c in result["calls"]
        )
        if result.get("stop_reason") not in safe and not model_quality_stop and not transport_stop:
            raise RuntimeError("A_unclassified_failure_requires_shared_infrastructure_review")
    return {
        "status": "A_PRESERVED_NO_SHARED_DEFECT_OBSERVED",
        "A_status": result["status"],
        "A_requests": len(reserved),
        "transport_mode": result["transport_mode"],
    }


async def execute_authorized(database, arm, approved_hash, request_cap):
    from experiments.decision_memory_ab_comparison_v1.prepare import verify

    if approved_hash != verify() or request_cap != 8:
        raise RuntimeError("explicit_AB_hash_and_eight_requests_required")
    key = os.environ.get("REXAK_MEMORY_LIVE_API_KEY")
    if not key:
        raise RuntimeError("experiment_only_key_required")
    if arm == "B":
        audit_a_before_b()
    claim_attempt(approved_hash, arm)
    results = results_for(arm)
    try:
        gate = WireGate(httpx.AsyncHTTPTransport(retries=0), arm, durable=True)
        result = await run_case(database, gate, results / "result.json", key=key)
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
                {"case": identity(arm)["case"], "reason": result["stop_reason"]},
                exclusive=True,
            )
        return result
    except BaseException as exc:
        if not (results / "STOP.json").exists():
            original.write_json(
                results / "STOP.json",
                {"case": identity(arm)["case"], "reason": type(exc).__name__},
                exclusive=True,
            )
        raise
