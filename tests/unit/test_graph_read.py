"""Shared deterministic graph views; synthetic data only."""

import copy
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError
from scripts.generate_graph_contract import generated

from agent_workbench.contracts.graph_read import GraphQuery, WorkerGraphRequest
from agent_workbench.domain.graph_read import adaptive, overview, query_graph, records
from agent_workbench.models.context_budget import estimate_value
from tests.unit.test_context_budget import profile
from tests.unit.test_history_assistance_mvp import Request


def synthetic(size=8):
    entities = [{"id": f"e{i:05}", "label": f"Synthetic entity {i}", "source_refs": []} for i in range(size)]
    states = [{"id": f"s{i:05}", "summary": f"Synthetic state {i}", "entity_refs": [f"e{i:05}"]} for i in range(size)]
    relations = [{"id": f"r{i:05}", "from_id": f"e{i:05}", "to_id": f"e{(i+1)%size:05}", "relation": f"Synthetic link {i}", "status": ("observed", "hypothesis", "refuted", "superseded")[i%4], "source_refs": []} for i in range(size)]
    attempts = [{"id": f"a{i:05}", "from_id": f"s{i:05}", "to_id": f"s{(i+1)%size:05}", "action": f"Synthetic transition {i}", "outcome": ("success", "negative", "blocked", "unknown")[i%4], "source_refs": ["operation:00000000-0000-0000-0000-000000000001"]} for i in range(size)]
    return {"revision": 7, "information_graph": {"nodes": entities, "edges": relations}, "attack_graph": {"nodes": states, "edges": attempts}}


@pytest.mark.parametrize("size", [0, 1, 8, 400, 2000])
def test_overview_counts_stability_and_visibility(size):
    graph = synthetic(size)
    before = copy.deepcopy(graph)
    view = overview(graph)
    assert view == overview(graph)
    assert graph == before
    assert view["mode"] == "overview" and view["total"] == size * 4
    assert view["displayed"] + view["omitted"] == size * 4
    assert view["counts"]["entities"] == size
    assert view["counts"]["linked_attack_states"] == size
    assert sum(view["counts"]["relation_status"].values()) == size
    assert sum(view["counts"]["attempt_outcome"].values()) == size
    for c in view["categories"].values():
        assert c["displayed"] + c["omitted"] == c["total"]
        assert c["displayed"] <= 4
    assert estimate_value(view) <= 4096
    if size:
        assert query_graph(graph, GraphQuery(ids=[f"e{size-1:05}"]))["total"] == 1
    for subgraph in graph.values():
        if isinstance(subgraph, dict):
            for objects in subgraph.values():
                objects.reverse()
    assert overview(graph) == view


@pytest.mark.parametrize("only", ["information", "attack"])
def test_single_graph_and_missing_refs(only):
    g = synthetic()
    g[("attack" if only == "information" else "information") + "_graph"] = {"nodes": [], "edges": []}
    if only == "information":
        g["information_graph"]["edges"][1]["to_id"] = "missing"
    else:
        g["attack_graph"]["nodes"][0]["entity_refs"] = ["missing"]
    v = overview(g)
    assert v["counts"]["linked_attack_states"] == 0
    real_ids = {r["data"]["id"] for r in records(g)}
    assert all(r["data"]["id"] in real_ids for r in v["endpoints"])
    assert all("source_refs" not in r["data"] for r in v["endpoints"] if r["graph"] == "attack")


@pytest.mark.parametrize("args,total", [
    ({"ids": ["e00000", "s00002", "r00001"]}, 3),
    ({"keyword": "ENTITY 7"}, 1), ({"status": "observed"}, 2),
    ({"outcome": "negative"}, 2), ({"graph": "attack", "kind": "nodes"}, 8),
    ({"ids": ["absent"]}, 0),
])
def test_filters(args, total):
    assert query_graph(synthetic(), GraphQuery(**args))["total"] == total


def test_pages_budget_and_revision_scope():
    g = synthetic(40)
    seen, cursor = [], None
    while True:
        v = query_graph(g, GraphQuery(limit=7, cursor=cursor), scope="session-a")
        assert len(v["records"]) <= 7 and v["total"] == 160
        seen.extend((r["graph"], r["kind"], r["data"]["id"]) for r in v["records"])
        cursor = v["next_cursor"]
        if not v["has_more"]:
            break
    assert len(seen) == len(set(seen)) == 160 and seen == sorted(seen)
    cursor = query_graph(g, GraphQuery(limit=1), scope="session-a")["next_cursor"]
    assert query_graph(g, GraphQuery(cursor=cursor), scope="other")["error"] == "invalid_cursor"
    assert query_graph(g, GraphQuery(cursor=cursor, keyword="x"), scope="session-a")["error"] == "invalid_cursor"
    g["revision"] += 1
    assert query_graph(g, GraphQuery(cursor=cursor), scope="session-a")["error"] == "stale_cursor"
    assert query_graph(g, GraphQuery(expected_revision=7))["error"] == "stale_revision"
    assert query_graph(g, GraphQuery(cursor="garbage"))["error"] == "invalid_cursor"
    for budget in [256, 512, 1024, 4096, 8192]:
        v = query_graph(g, GraphQuery(limit=200, token_budget=budget))
        assert estimate_value(v) <= budget


@pytest.mark.parametrize("graph,prefix,edgeprefix", [("information", "e", "r"), ("attack", "s", "a")])
def test_traversal_cycles_parallel_self_loops_and_directions(graph, prefix, edgeprefix):
    g = synthetic(4)
    edges = g[graph+"_graph"]["edges"]
    edges.extend([{**edges[0], "id": "parallel"}, {**edges[0], "id": "self", "to_id": prefix+"00000"},
                  {**edges[0], "id": "reverse", "from_id": prefix+"00001", "to_id": prefix+"00000"}])
    out = query_graph(g, GraphQuery(graph=graph, ids=[prefix+"00000"], expand=True, direction="out"))
    ids = {r["data"]["id"] for r in out["records"]}
    assert {"parallel", "self", prefix+"00000", prefix+"00001", edgeprefix+"00000"} <= ids
    assert "reverse" not in ids
    incoming = query_graph(g, GraphQuery(graph=graph, ids=[prefix+"00000"], expand=True, direction="in"))
    assert "reverse" in {r["data"]["id"] for r in incoming["records"]}
    all_nodes = query_graph(g, GraphQuery(graph=graph, ids=[prefix+"00000"], expand=True, hops=4))
    keys = [(r["kind"], r["data"]["id"]) for r in all_nodes["records"]]
    assert len(keys) == len(set(keys)) == 11


def test_cross_graph_progressive_queries():
    g = synthetic()
    result = query_graph(g, GraphQuery(graph="information", ids=["e00000"], cross_graph=True))
    assert {r["data"]["id"] for r in result["records"]} == {"e00000", "s00000"}
    result = query_graph(g, GraphQuery(graph="attack", ids=["s00000"], cross_graph=True, expand=True, hops=2))
    assert {"e00000", "e00001", "a00000", "s00001"} <= {r["data"]["id"] for r in result["records"]}
    g["attack_graph"]["nodes"][0]["entity_refs"] = []
    assert query_graph(g, GraphQuery(ids=["s00000"], cross_graph=True))["total"] == 1


def test_adaptive_disabled_and_independent_budgets():
    g = synthetic(400)
    assert adaptive(synthetic(1), 4096)["mode"] == "full"
    assert adaptive(g, 4096)["mode"] == "overview"
    assert adaptive(g, 8)["mode"] == "omitted"
    assert adaptive(g, 200000)["mode"] == "full"
    assert overview(g, enabled=False)["total"] == 0
    assert all(r["graph"] == "information" for r in query_graph(g, GraphQuery(), attack_enabled=False)["records"])


@pytest.mark.parametrize("args", [{"hops": 0}, {"hops": 5}, {"limit": 201}, {"token_budget": 8193}, {"graph": "other"}, {"expand": True}, {"cross_graph": True}, {"status": "confirmed"}, {"session_id": "other"}])
def test_invalid_parameters(args):
    with pytest.raises(ValidationError):
        GraphQuery(**args)


def test_generated_contract_and_worker_cannot_write():
    assert Path("worker/src/graph-contract.ts").read_text(encoding="utf-8") == generated()
    assert "update_graph" not in WorkerGraphRequest.model_json_schema()["properties"]["action"]["enum"]


@pytest.mark.parametrize("size,budget,expected", [(1, 20000, "full"), (400, 10000, "overview"), (400, 2000, "omitted"), (40, 4000, "overview"), (100, 5000, "overview"), (100, 20000, "overview")])
async def test_main_actual_request_adaptive_context(monkeypatch, tmp_path, size, budget, expected):
    from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
    from agent_workbench.config.settings import Settings

    @asynccontextmanager
    async def session():
        yield None

    async def read(*_args):
        return synthetic(size)

    monkeypatch.setattr("agent_workbench.domain.session_graph.read_graph", read)
    box = SimpleNamespace(db=SimpleNamespace(session=session), session_id="synthetic", working_context={},
        settings=Settings(data_root=tmp_path), scope=None)
    request = Request(box, [HumanMessage(content="Synthetic objective")])
    captured = []

    async def handler(value):
        captured.append(value)
        return ModelResponse(result=[AIMessage(content="done")])

    await ContextBuilderMiddleware(profile(budget, 1000)).awrap_model_call(cast(Any, request), handler)
    assert box.context_manifest["working_context"]["shared_graph"]["mode"] == expected
    assert box.context_manifest["budget"]["estimated_input_tokens"] <= box.context_manifest["budget"]["allowed_input_tokens"]
    assert captured[0].system_message.content.startswith("unchanged system")
    assert '"revision"' not in captured[0].system_message.content
    assert not any("graph" in str(m.content) for m in request.messages)


async def test_failed_main_call_retries_initial_graph(monkeypatch, tmp_path):
    from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
    from agent_workbench.config.settings import Settings

    reads = []

    @asynccontextmanager
    async def session():
        yield None

    async def read(*_args):
        reads.append(1)
        return synthetic(1)

    monkeypatch.setattr("agent_workbench.domain.session_graph.read_graph", read)
    box = SimpleNamespace(db=SimpleNamespace(session=session), session_id="synthetic", working_context={}, settings=Settings(data_root=tmp_path), scope=None)
    request = Request(box, [HumanMessage(content="Synthetic objective")])
    middleware = ContextBuilderMiddleware(profile(20000, 1000))

    async def fail(_request):
        raise RuntimeError("synthetic provider failure")

    async def succeed(_request):
        return ModelResponse(result=[AIMessage(content="done")])

    with pytest.raises(RuntimeError):
        await middleware.awrap_model_call(cast(Any, request), fail)
    assert not getattr(box, "graph_awareness_initialized", False)
    await middleware.awrap_model_call(cast(Any, request), succeed)
    assert len(reads) == 2 and box.graph_awareness_initialized


async def test_overview_does_not_present_or_acknowledge_maintenance_page(monkeypatch, tmp_path):
    from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
    from agent_workbench.config.settings import Settings

    presented = []

    async def present(*args):
        presented.append(args)

    monkeypatch.setattr("agent_workbench.domain.graph_sync.present_page", present)
    box = SimpleNamespace(working_context={"session_memory": {"graph": synthetic(400), "page_id": "unpresented-page"}},
        graph_sync_snapshot={"page_id": "unpresented-page"}, settings=Settings(data_root=tmp_path), scope=None)
    request = Request(box, [HumanMessage(content="Synthetic objective")])

    async def handler(_request):
        return ModelResponse(result=[AIMessage(content="done")])

    await ContextBuilderMiddleware(profile(5000, 1000)).awrap_model_call(cast(Any, request), handler)
    view = box.context_manifest["working_context"]
    assert view["shared_graph"]["mode"] == "overview"
    assert view["session_memory"]["overflow"] and "page_id" not in view["session_memory"]
    assert not presented and box.graph_sync_snapshot is None
