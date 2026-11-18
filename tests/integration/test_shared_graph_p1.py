"""Isolated PostgreSQL, native Main model assembly and task-bound read-only graph access."""

import asyncio
import copy
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
from agent_workbench.agent.middleware.working_context import WorkingContextMiddleware
from agent_workbench.agent.prompts.system import SYSTEM_PROMPT
from agent_workbench.config.catalog import ProviderProfile
from agent_workbench.config.settings import REPO_ROOT
from agent_workbench.contracts.delegation import DelegationInput
from agent_workbench.contracts.errors import ApiError
from agent_workbench.contracts.graph_read import GraphRead, WorkerGraphRequest
from agent_workbench.domain.graph_read import read_shared
from agent_workbench.domain.session_graph import read_graph, update_graph
from agent_workbench.domain.worker_graph import WorkerGraphService, graph_signature
from agent_workbench.persistence.tables import (
    AgentDelegationRow,
    RunRow,
    SessionGraphReceiptRow,
    SessionGraphRow,
)
from agent_workbench.tools.session_graph import GRAPH_READ_TOOLS, GRAPH_TOOLS
from tests.integration.test_session_graph import adapter
from tests.integration.test_ultra_delegations import TOKEN, make_manager
from tests.integration.test_worker_evidence import artifact, evidence_case, free_port  # noqa: F401
from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call
from tests.unit.test_graph_read import synthetic

pytestmark = pytest.mark.integration


@pytest.fixture
async def graph_case(database, evidence_case):  # noqa: F811
    manager, _, evidence_call, delegation, cfg = evidence_case
    source = await artifact(database, cfg, manager.session_id, b"Synthetic graph provenance")
    snapshot = synthetic(400)
    for edge in snapshot["attack_graph"]["edges"]:
        edge["source_refs"] = [f"artifact:{source}"]
    async with database.transaction() as db:
        db.add(SessionGraphRow(session_id=manager.session_id, revision=7,
                              document={k: v for k, v in snapshot.items() if k != "revision"}))
        row = await db.get(AgentDelegationRow, delegation)
        row.handoff_snapshot = {**row.handoff_snapshot, "graph_access": "session_graph_read_v1"}
        row.authorization_snapshot = {**row.authorization_snapshot, "tool_allowlist": ["graph_overview", "graph_query", "read_shared_graph", "list_evidence", "read_evidence"]}
        context_id = row.context_id
    from agent_workbench.execution.a2a_worker import A2AWorkerClient
    remote = await A2AWorkerClient(cfg).lookup(delegation)
    assert remote
    service = WorkerGraphService(database, cfg)

    def request(**args):
        return WorkerGraphRequest(task_id=remote["task_id"], worker_id=remote["worker_id"], context_id=context_id, **args)

    async def call(**args):
        value = request(**args)
        return await service.handle(delegation, value, graph_signature(TOKEN, delegation, value))

    return manager, delegation, cfg, call, request, service, evidence_call


async def graph_state(database, session_id):
    async with database.session() as db:
        row = await db.get(SessionGraphRow, session_id)
        receipt_count = await db.scalar(select(func.count()).select_from(SessionGraphReceiptRow).where(SessionGraphReceiptRow.session_id == session_id))
        return copy.deepcopy({k: getattr(row, k) for k in ("document", "revision", "reviewed_seq", "completed_rounds", "next_sync_round", "missed_syncs")}), receipt_count


async def test_worker_pure_read_concurrency_version_and_evidence(database, graph_case):
    manager, _, cfg, call, _, _, evidence_call = graph_case
    before = await graph_state(database, manager.session_id)
    overview = await call(action="overview")
    assert overview["revision"] == 7 and overview["mode"] == "overview"
    assert "e00399" not in json.dumps(overview)
    hidden = await call(action="query", query={"ids": ["e00399"]})
    assert hidden["total"] == 1
    cross = await call(action="query", query={"graph": "information", "ids": ["e00399"], "cross_graph": True})
    assert {r["data"]["id"] for r in cross["records"]} == {"e00399", "s00399"}
    expanded = await call(action="query", query={"graph": "attack", "ids": ["s00399"], "expand": True})
    ref = next(r["data"]["source_refs"][0] for r in expanded["records"] if r["kind"] == "edges")
    evidence = await evidence_call(artifact_id=ref.split(":")[1])
    assert evidence["data"]["text"] == "Synthetic graph provenance"
    parallel = await asyncio.gather(*(call(action="query", query={"ids": [f"e{i:05}"]}) for i in range(12)))
    assert all(v["total"] == 1 for v in parallel)
    assert await graph_state(database, manager.session_id) == before
    box = SimpleNamespace(session_id=manager.session_id, run_id=manager.run_id, owner="test-owner", owner_generation=1, control_version=0, settings=cfg)
    async with database.transaction() as db:
        result = await update_graph(db, box, "p1-main-update", 7, adapter.validate_python([{"op": "add_node", "graph": "information", "temp_id": "new", "data": {"label": "Synthetic newly shared entity"}}]))
    assert result["revision"] == 8
    fresh = await call(action="query", query={"keyword": "newly shared"})
    assert fresh["revision"] == 8 and fresh["total"] == 1
    assert (await call(action="query", query={"expected_revision": 7}))["error"] == "stale_revision"
    after_write = await graph_state(database, manager.session_id)
    await call(action="overview")
    assert await graph_state(database, manager.session_id) == after_write


@pytest.mark.parametrize("stop", ["cancel", "pause", "lease", "generation", "control", "terminal", "permission", "old_handoff"])
async def test_worker_read_revocation(database, graph_case, stop):
    manager, delegation, _, call, _, _, _ = graph_case
    await call()
    async with database.transaction() as db:
        run = await db.get(RunRow, manager.run_id)
        row = await db.get(AgentDelegationRow, delegation)
        if stop == "cancel":
            row.cancel_requested = True
        elif stop == "pause":
            run.status = "paused"
        elif stop == "lease":
            run.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        elif stop == "generation":
            run.owner_generation += 1
        elif stop == "control":
            run.control_version += 1
        elif stop == "terminal":
            row.state = "completed"
        elif stop == "permission":
            row.authorization_snapshot = {**row.authorization_snapshot, "tool_allowlist": ["update_graph"]}
        else:
            row.handoff_snapshot = {"evidence_access": "session_read_v1"}
    with pytest.raises(ApiError):
        await call()


async def test_worker_identity_and_session_boundary(database, graph_case):
    manager, delegation, cfg, call, request, service, _ = graph_case
    value = request()
    with pytest.raises(ApiError):
        await service.handle(delegation, value, "bad")
    for field in ("task_id", "worker_id", "context_id"):
        forged = value.model_copy(update={field: str(uuid4()) if field == "task_id" else uuid4()})
        with pytest.raises(ApiError):
            await service.handle(delegation, forged, graph_signature(TOKEN, delegation, forged))
    # IDs are only looked up in the delegation's Session snapshot.
    assert (await call(action="query", query={"ids": [str(uuid4())]}))["total"] == 0
    with pytest.raises(ValueError):
        request(session_id=str(uuid4()))
    before = await graph_state(database, manager.session_id)
    with pytest.raises(ValueError):
        request(action="update_graph")
    assert await graph_state(database, manager.session_id) == before
    async with database.session() as db:
        main = await read_shared(db, manager.session_id, GraphRead(action="overview"), cfg)
    assert main == await call(action="overview")


async def test_foreign_ids_and_operation_artifact_resolution(database, graph_case):
    from agent_workbench.persistence.tables import ToolOperationRow

    manager, _, cfg, call, _, _, evidence_call = graph_case
    other = await make_manager(database, cfg)
    operation_id = uuid4()
    async with database.transaction() as db:
        db.add(ToolOperationRow(operation_id=operation_id, run_id=manager.run_id,
            session_id=manager.session_id, tool_call_id="synthetic-source", tool_name="fixture",
            request_digest="0" * 64, effect_class="read", status="succeeded"))
        db.add(SessionGraphRow(session_id=other.session_id, revision=99, document={
            "information_graph": {"nodes": [{"id": "foreign-only", "label": "private foreign entity", "source_refs": []}], "edges": []},
            "attack_graph": {"nodes": [], "edges": []}}))
    local = await artifact(database, cfg, manager.session_id, b"Operation-derived synthetic content")
    foreign = await artifact(database, cfg, other.session_id, b"Foreign content")
    from agent_workbench.persistence.tables import ArtifactRow

    async with database.transaction() as db:
        for key in (local, foreign):
            item = await db.get(ArtifactRow, key)
            item.source_operation_id = operation_id
    directory = await evidence_call(action="list", source_operation_id=operation_id)
    assert [v["artifact_id"] for v in directory["artifacts"]] == [str(local)]
    assert (await evidence_call(artifact_id=local))["data"]["text"] == "Operation-derived synthetic content"
    assert (await evidence_call(artifact_id=foreign))["error"] == "evidence_not_available"
    assert (await call(action="query", query={"ids": ["foreign-only"]}))["total"] == 0


async def test_distinct_workers_share_one_session_snapshot(database, graph_case, monkeypatch):
    from agent_workbench.execution.a2a_worker import A2AWorkerClient

    manager, delegation, _, first, _, service, _ = graph_case
    other, context_id, worker_id, task_id = uuid4(), uuid4(), uuid4(), str(uuid4())
    async with database.transaction() as db:
        original = await db.get(AgentDelegationRow, delegation)
        db.add(AgentDelegationRow(id=other, parent_session_id=manager.session_id, parent_run_id=manager.run_id,
            parent_tool_call_id="parallel-worker", request_digest="1" * 64, context_id=context_id,
            handoff_snapshot=original.handoff_snapshot, authorization_snapshot=original.authorization_snapshot,
            objective="Synthetic second worker", context_refs=[], state="working", summary="", cancel_requested=False,
            a2a_task_id=task_id, worker_id=str(worker_id)))
    original_lookup = A2AWorkerClient.lookup

    async def lookup(client, identity):
        if identity == other:
            return {"phase": "running", "task_id": task_id, "worker_id": str(worker_id), "task": {"contextId": str(context_id)}}
        return await original_lookup(client, identity)

    monkeypatch.setattr(A2AWorkerClient, "lookup", lookup)
    request = WorkerGraphRequest(task_id=task_id, worker_id=worker_id, context_id=context_id, action="overview")
    before = await graph_state(database, manager.session_id)
    left, right = await asyncio.gather(first(action="overview"), service.handle(other, request, graph_signature(TOKEN, other, request)))
    assert left == right and left["revision"] == 7
    assert await graph_state(database, manager.session_id) == before


async def test_main_native_loop_budget_cache_and_progressive_reads(database, graph_case):
    manager, _, cfg, _, _, _, _ = graph_case
    box = SimpleNamespace(db=database, session_id=manager.session_id, run_id=manager.run_id,
        owner="test-owner", owner_generation=1, control_version=0, settings=cfg,
        working_context=None, graph_review_window_id=None, graph_sync_snapshot=None)
    captured = []

    class Capture(ScriptedChatModel):
        def _generate(self, messages, *args, **kwargs):
            captured.append(messages)
            return super()._generate(messages, *args, **kwargs)

    model = Capture(responses=[
        tool_call("graph_query", {"query": {"ids": ["e00399"]}}, "p1-entity"),
        tool_call("graph_query", {"query": {"ids": ["e00399"], "cross_graph": True}}, "p1-cross"),
        tool_call("graph_query", {"query": {"ids": ["s00399"], "expand": True}}, "p1-attempt"),
        final_text("Synthetic graph reads complete"),
    ])
    profile = ProviderProfile(provider_id="mock", model_name="fixture", base_url="http://fixture.invalid", secret_ref="", context_limit=30000, output_reserve=1000)
    graph = create_agent(model, tools=[*GRAPH_TOOLS, *GRAPH_READ_TOOLS], context_schema=RunContext, system_prompt=SYSTEM_PROMPT,
        middleware=[WorkingContextMiddleware(), ContextBuilderMiddleware(profile)])
    result = await graph.ainvoke({"messages": [HumanMessage(content="Read synthetic shared graph")]}, context=RunContext(toolbox=cast(Any, box)))
    assert result["messages"][-1].content == "Synthetic graph reads complete"
    outputs = [json.loads(str(m.content)) for m in result["messages"] if isinstance(m, ToolMessage)]
    assert outputs[0]["total"] == 1 and outputs[1]["total"] == 2
    assert any(r["kind"] == "edges" for r in outputs[2]["records"])
    systems = [next(m.content for m in ms if isinstance(m, SystemMessage)) for ms in captured]
    assert len(set(systems)) == 1
    projections = [[m for m in ms if m.additional_kwargs.get("host_projection") == "host_context_update"] for ms in captured]
    # Immutable cache-prefix projections are retained; each round appends only its current view.
    assert all(len(p) >= 1 for p in projections)
    assert all(sum('"shared_graph"' in str(m.content) for m in p) == 1 for p in projections)
    first = str(projections[0][-1].content)
    assert '"mode": "overview"' in first and '"mode": "full"' not in first
    assert '"shared_graph"' not in str(projections[1][-1].content)
    async with database.session() as db:
        assert (await read_graph(db, manager.session_id))["revision"] == 7
        row = await db.get(SessionGraphRow, manager.session_id)
        assert row.completed_rounds == 0 and row.reviewed_seq == 0


@pytest.fixture
async def graph_runtime(settings, tmp_path):
    api_port, worker_port = free_port(), free_port()
    cfg = settings.model_copy(update={"ultra_worker_url": f"http://127.0.0.1:{worker_port}", "ultra_worker_token": TOKEN})
    env = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"}}
    env.update(WORKBENCH_DATABASE_URL=settings.database_url, WORKBENCH_DATA_ROOT=str(settings.data_root),
        WORKBENCH_ULTRA_WORKER_URL=cfg.ultra_worker_url, WORKBENCH_ULTRA_WORKER_TOKEN=TOKEN,
        WORKBENCH_OWNER_ID=str(settings.owner_id), WORKBENCH_API_PORT=str(api_port),
        REXAK_WORKER_DATABASE_URL=make_url(settings.database_url).set(drivername="postgresql").render_as_string(hide_password=False),
        REXAK_WORKER_TOKEN=TOKEN, FIXTURE_PORT=str(worker_port), REXAK_WORKER_CONTROL_URL=f"http://127.0.0.1:{api_port}", PYTHONIOENCODING="utf-8")
    node = shutil.which("node")
    assert node
    with (tmp_path / "api.log").open("wb") as api_log, (tmp_path / "worker.log").open("wb") as worker_log:
        api = subprocess.Popen([sys.executable, "-m", "agent_workbench.cli", "api", "--host", "127.0.0.1", "--port", str(api_port)], env=env, cwd=REPO_ROOT, stdout=api_log, stderr=api_log)
        worker = subprocess.Popen([node, "--import", "tsx", "test/graph-a2a-server.ts"], env=env, cwd=REPO_ROOT / "worker", stdout=worker_log, stderr=worker_log)
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
                for url in [f"http://127.0.0.1:{api_port}/health", cfg.ultra_worker_url + "/health"]:
                    for _ in range(200):
                        assert api.poll() is None and worker.poll() is None
                        try:
                            if (await client.get(url, headers={"Authorization": "Bearer " + TOKEN})).status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.05)
                    else:
                        raise AssertionError("Graph runtime startup timeout")
            yield cfg
        finally:
            for process in (worker, api):
                process.terminate()
                process.wait(timeout=10)


async def test_real_a2a_pi_graph_roundtrip_and_main_revision(database, graph_runtime):
    cfg = graph_runtime
    manager = await make_manager(database, cfg)
    source = await artifact(database, cfg, manager.session_id, b"Synthetic runtime graph evidence")
    snapshot = synthetic(400)
    snapshot["information_graph"]["nodes"][-1]["label"] = "unlisted entity"
    for edge in snapshot["attack_graph"]["edges"]:
        edge["source_refs"] = [f"artifact:{source}"]
    async with database.transaction() as db:
        db.add(SessionGraphRow(session_id=manager.session_id, revision=7, document={k: v for k, v in snapshot.items() if k != "revision"}))
    before = await graph_state(database, manager.session_id)
    record = await manager.delegate("p1-runtime", DelegationInput(objective="Read shared synthetic graph and evidence"), owner="test-owner", generation=1, control_version=0)
    async with httpx.AsyncClient(trust_env=False, headers={"Authorization": "Bearer " + TOKEN}, timeout=2) as client:
        for _ in range(200):
            if (await client.get(cfg.ultra_worker_url + "/graph-checkpoint")).json()["checkpoint"]:
                break
            await asyncio.sleep(0.05)
        else:
            result = await manager.wait(record.id, 1)
            raise AssertionError(f"Pi graph checkpoint missing: {result.state} {result.summary}")
        assert await graph_state(database, manager.session_id) == before
        box = SimpleNamespace(session_id=manager.session_id, run_id=manager.run_id, owner="test-owner", owner_generation=1, control_version=0, settings=cfg)
        async with database.transaction() as db:
            await update_graph(db, box, "p1-runtime-main-update", 7, adapter.validate_python([{"op": "add_node", "graph": "information", "temp_id": "fresh", "data": {"label": "New shared state"}}]))
        after_write = await graph_state(database, manager.session_id)
        await client.post(cfg.ultra_worker_url + "/graph-release")
    result = await manager.wait(record.id, 15)
    assert result.state == "completed", result.summary
    recap = json.loads(result.summary)
    assert recap["oldRevision"] == 7 and recap["newRevision"] == 8
    assert recap["hiddenEntity"] == "unlisted entity" and recap["evidence"] == "Synthetic runtime graph evidence"
    assert recap["requests"][0]["initialMode"] == "overview"
    assert all("initialMode" not in item for item in recap["requests"][1:])
    assert await graph_state(database, manager.session_id) == after_write
    (REPO_ROOT / "reports/shared-graph-p1/runtime-recap.json").write_text(json.dumps(recap, indent=2), encoding="utf-8")


async def test_isolated_graph_performance_and_input_estimates(database, settings):
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from agent_workbench.agent.middleware.context_updates import CONTEXT_UPDATE_RULE, append_update
    from agent_workbench.domain.graph_read import adaptive, overview
    from agent_workbench.models.context_budget import estimate_request, estimate_value, serialized

    manager = await make_manager(database, settings)
    profile = ProviderProfile(provider_id="mock", model_name="fixture", base_url="http://fixture.invalid", secret_ref="", context_limit=1000000, output_reserve=1000)
    tools = [convert_to_openai_tool(t) for t in [*GRAPH_TOOLS, *GRAPH_READ_TOOLS]]
    report = []
    for size in (8, 400, 2000):
        snapshot = synthetic(size)
        async with database.transaction() as db:
            row = await db.get(SessionGraphRow, manager.session_id)
            if row:
                row.document = {k: v for k, v in snapshot.items() if k != "revision"}
            else:
                db.add(SessionGraphRow(session_id=manager.session_id, revision=7, document={k: v for k, v in snapshot.items() if k != "revision"}))
        full = adaptive(snapshot, 1000000)
        index = overview(snapshot)
        measurements = {}
        for action in ("read", "overview", "query"):
            elapsed = []
            for _ in range(5):
                start = time.perf_counter()
                async with database.session() as db:
                    response = await read_shared(db, manager.session_id, GraphRead(action=action, token_budget=8192), settings)
                serialized(response)
                elapsed.append((time.perf_counter() - start) * 1000)
            measurements[action + "_median_ms"] = round(statistics.median(elapsed), 3)
        entry = {"entities": size, "total_objects": size * 4, **measurements}
        for name, view in (("full", full), ("overview", index)):
            messages = append_update([HumanMessage(content="Synthetic graph investigation")], "host_context_update", serialized(view))[0]
            estimate = estimate_request(messages, SystemMessage(content=SYSTEM_PROMPT + CONTEXT_UPDATE_RULE), tools, profile)
            entry[name] = {"utf8_bytes": len(serialized(view).encode()), "graph_estimated_tokens": estimate_value(view),
                "complete_request_estimated_input_tokens": estimate.estimated_input_tokens,
                "estimation_method": estimate.estimation_method, "displayed": view.get("displayed", size * 4)}
        report.append(entry)
    (REPO_ROOT / "reports/shared-graph-p1/performance.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
