"""E0：真实装配/适配器接 MockTransport，只写会话独占测试库。"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import SecretStr
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.factory import build_agent_middleware
from agent_workbench.agent.prompts import SYSTEM_PROMPT
from agent_workbench.agent.signals import GraphRunAborted, StaleResponseError
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.config.catalog import ProviderProfile, default_price_catalog
from agent_workbench.contracts.enums import RunStatus
from agent_workbench.domain.run_state import claim_run, create_run, set_run_status
from agent_workbench.models.bailian import BailianChatOpenAI
from agent_workbench.models.invocation import graph_review_scope
from agent_workbench.models.provider import ModelBinding
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import (
    EventRow,
    ModelCallRow,
    RunRow,
    SessionGraphReceiptRow,
    SessionGraphRow,
    ToolOperationRow,
)
from agent_workbench.tools.registry import build_tools, default_selection

pytestmark = pytest.mark.integration
RESULTS = Path(__file__).resolve().parents[2] / "experiments" / "dual_graph_replay" / "results"


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def isolated_box(database, unique_name):
    assert "/workbench_test_" in database.settings.database_url
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id="e0-only",
            name=unique_name,
            target_description="完全模拟，无目标连接",
            default_scope={},
        )
        session = await SessionRepository(db).create(
            project_id=project.id, title=unique_name, scope_snapshot={}, policy_snapshot={}
        )
        run = await create_run(
            db,
            session_id=session.id,
            objective="仅模拟协议验证",
            provider_id="bailian",
            model_name="fixture",
            config_snapshot={},
        )
        await set_run_status(db, run_id=run.id, target=RunStatus.RUNNING)
        _, generation = await claim_run(db, run_id=run.id, owner="e0-only", lease_seconds=600)
    box = SimpleNamespace(
        db=database,
        settings=database.settings.model_copy(
            update={
                "session_graph_enabled": True,
                "graph_bounded_review": True,
                "graph_sync_interval": 5,
                "graph_inject_every_round": False,
                "graph_attack_enabled": True,
                "blind_test": True,
            }
        ),
        run_id=run.id,
        session_id=session.id,
        owner="e0-only",
        owner_generation=generation,
        control_version=0,
        scope_revision=1,
        scope=None,
        provider_id="bailian",
        model_name="fixture",
        sdk_retries_disabled=True,
        context_manifest=None,
        working_context=None,
        graph_sync_snapshot=None,
        graph_review_window_id=None,
        bump=lambda _: None,
    )

    async def control():
        async with database.session() as db:
            current = await db.get(RunRow, run.id)
            if current.status != "running":
                raise GraphRunAborted(current.status)
            if current.control_version != box.control_version:
                raise StaleResponseError(
                    pinned_version=box.control_version, current_version=current.control_version
                )

    box.assert_control_current = control
    return box, session


def native_response(name=None, args=None, *, invalid=False, response_id="synthetic"):
    message: dict[str, Any] = {"role": "assistant", "content": "" if name else "模拟阶段结束"}
    if name:
        message["tool_calls"] = [
            {
                "id": response_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": "{broken" if invalid else json.dumps(args or {}),
                },
            }
        ]
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": 1,
        "model": "fixture",
        "choices": [
            {"index": 0, "message": message, "finish_reason": "tool_calls" if name else "stop"}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


def response_http(body, stream):
    if not stream:
        return httpx.Response(200, json=body)
    message = body["choices"][0]["message"]
    base = {"id": body["id"], "object": "chat.completion.chunk", "created": 1, "model": "fixture"}
    deltas = [{"role": "assistant", "content": message["content"]}]
    for tc in message.get("tool_calls", []):
        name, arguments = tc["function"]["name"], tc["function"]["arguments"]
        mid = len(name) // 2
        deltas += [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": name[:mid], "arguments": ""},
                    }
                ]
            },
            {
                "tool_calls": [
                    {"index": 0, "function": {"name": name[mid:], "arguments": arguments}}
                ]
            },
        ]
    chunks = [
        {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        for delta in deltas
    ]
    chunks += [
        {
            **base,
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": body["choices"][0]["finish_reason"]}
            ],
        },
        {**base, "choices": [], "usage": body["usage"]},
    ]
    wire = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire.encode())


@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "disallowed",
        "no_tool",
        "invalid_json",
        "missing_page",
        "schema_invalid",
        "validation_failed",
        "get_graph_only",
        "disallowed_twice",
        "control_pause",
        "control_cancel",
        "control_guidance",
    ],
)
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
async def test_e0_full_assembly_wire(database, unique_name, monkeypatch, case, stream):
    # 禁止一切默认HTTP传输；MockTransport独占所有HTTP交换。DB只用隔离库。
    async def no_async_http(*args, **kwargs):
        pytest.fail("非Mock HTTP被阻断")

    def no_sync_http(*args, **kwargs):
        pytest.fail("非Mock HTTP被阻断")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_async_http)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_sync_http)
    import agent_workbench.config.secrets as secrets

    monkeypatch.setattr(secrets, "build_secret_store", no_sync_http)
    box, session = await isolated_box(database, unique_name)
    requests, bindings, work, trap = [], [], [], []
    review_index, normal_index = 0, 0

    @tool
    async def fixture_read() -> str:
        """仅返回固定合成记录，不使用Executor、网络或模型。"""
        work.append(1)
        return "模拟记录：条件没有变化；这里只验证协议，不宣称任何漏洞。"

    async def forbidden_external(*args, **kwargs):
        trap.append(1)
        pytest.fail("被拒绝的维护调用错误进入了外部工具")

    tools = [
        t.model_copy(update={"coroutine": forbidden_external}) if t.name == "http_request" else t
        for t in build_tools(default_selection(box.settings))
    ] + [fixture_read]
    all_names = [t.name for t in tools]

    class CapturedBailian(BailianChatOpenAI):
        def bind_tools(self, tools, **kwargs):
            bindings.append(
                {
                    "phase": "graph_review" if graph_review_scope.get() else "agent",
                    "names": [
                        t.name if not isinstance(t, dict) else t["function"]["name"] for t in tools
                    ],
                    "tool_choice": kwargs.get("tool_choice", "ABSENT"),
                }
            )
            return super().bind_tools(tools, **kwargs)

    async def capture(request):
        nonlocal review_index, normal_index
        assert request.url.host == "offline.invalid"
        wire = json.loads(request.content)
        reviewing = bool(graph_review_scope.get())
        names = [t["function"]["name"] for t in wire["tools"]]
        assert names == (["get_graph", "update_graph"] if reviewing else all_names)
        assert "tool_choice" not in wire
        assert wire["stream"] == stream
        assert wire["messages"][0]["role"] == "system"
        pending = set()
        for msg in wire["messages"]:
            for tc in msg.get("tool_calls", []):
                pending.add(tc["id"])
            if msg["role"] == "tool":
                assert msg["tool_call_id"] in pending
                pending.remove(msg["tool_call_id"])
        assert not pending
        if reviewing:
            review_index += 1
            if case.startswith("control_"):
                async with database.transaction() as db:
                    run = await db.get(RunRow, box.run_id)
                    if case == "control_guidance":
                        run.control_version += 1
                    else:
                        run.status = "pausing" if case == "control_pause" else "cancelling"
            page = box.working_context["session_memory"]["page_id"]
            assert page in wire["messages"][0]["content"]
            assert "有限图核对请求" in wire["messages"][0]["content"]
            assert box.context_manifest["request_tool_names"] == names
            assert box.context_manifest["request_tools_sha256"] == sha(wire["tools"])
            assert (
                sha(wire["tools"])
                == "f15e08ef04492e4f4d9523e88ed46f50e441341b296cc4d2e89bdf43cd5a8334"
            )
            args: dict[str, Any] = {"expected_revision": 0, "page_id": page, "operations": []}
            name, invalid = "update_graph", False
            if review_index == 1:
                if case in {"disallowed", "disallowed_twice"}:
                    name, args = "http_request", {}
                elif case == "no_tool":
                    name = None
                elif case == "invalid_json":
                    invalid = True
                elif case == "missing_page":
                    args.pop("page_id")
                elif case == "schema_invalid":
                    args["expected_revision"] = "not-an-integer"
                elif case == "validation_failed":
                    args["operations"] = [
                        {
                            "op": "add_edge",
                            "graph": "attack",
                            "temp_id": "bad",
                            "data": {
                                "from_id": "missing",
                                "to_id": "missing",
                                "action": "仅模拟",
                                "outcome": "unknown",
                                "source_refs": ["operation:00000000-0000-0000-0000-000000000000"],
                            },
                        }
                    ]
                elif case == "get_graph_only":
                    name, args = "get_graph", {"include_pending": True}
            # 合法分支确实持久化非空图，而非只验证空确认。
            if case == "disallowed_twice":
                name, args = "http_request", {}
            elif case == "valid" or review_index == 2:
                args["operations"] = [
                    {
                        "op": "add_node",
                        "graph": "information",
                        "temp_id": "e",
                        "data": {"label": "合成资产；没有安全效果断言"},
                    }
                ]
            body = native_response(
                name, args, invalid=invalid, response_id=f"review-{review_index}"
            )
        else:
            normal_index += 1
            body = (
                native_response("fixture_read", {}, response_id=f"work-{normal_index}")
                if normal_index <= 5
                else native_response()
            )
        native_names = [
            c["function"]["name"] for c in body["choices"][0]["message"].get("tool_calls", [])
        ]
        requests.append(
            {
                "phase": "graph_review" if reviewing else "agent",
                "tools": names,
                "tools_sha256": sha(wire["tools"]),
                "tool_choice": "ABSENT",
                "stream": stream,
                "response_format": wire.get("response_format", "ABSENT"),
                "output_limit": {
                    k: wire[k] for k in ["max_tokens", "max_completion_tokens"] if k in wire
                },
                "message_roles": [m["role"] for m in wire["messages"]],
                "message_chars": [len(str(m.get("content", ""))) for m in wire["messages"]],
                "history_tool_names": [
                    c["function"]["name"] for m in wire["messages"] for c in m.get("tool_calls", [])
                ],
                "native_tool_names": native_names,
                "pairing_complete": True,
            }
        )
        return response_http(body, stream)

    profile = ProviderProfile(
        provider_id="bailian",
        model_name="fixture",
        base_url="https://offline.invalid/v1",
        secret_ref="synthetic-only",
        context_limit=1000000,
        output_reserve=4096,
        request_output_limit=4096,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(capture), trust_env=False) as client:
        model = CapturedBailian(
            model="fixture",
            api_key=SecretStr("synthetic-unused"),
            base_url=profile.base_url,
            http_async_client=client,
            max_retries=0,
            use_responses_api=False,
            streaming=stream,
            stream_usage=True,
            max_completion_tokens=4096,
        )
        middleware = build_agent_middleware(
            model, ModelBinding(profile), box.settings, tools, default_price_catalog()
        )
        topology = [type(m).__name__ for m in middleware]
        assert topology[:2] == ["CommandGuardMiddleware", "SegmentMiddleware"]
        config = {"configurable": {"thread_id": session.thread_id}, "recursion_limit": 100}
        payload = {
            "messages": [HumanMessage(content="仅离线模拟，达到边界后自然结束", id="e0-user")],
            "active_run_id": str(box.run_id),
            "objective": "合成协议验证",
        }
        reconnects = 0
        for _ in range(16):
            box.graph_sync_snapshot = None
            box.graph_review_window_id = None
            async with open_checkpointer_connection(database.settings) as connection:
                graph = create_agent(
                    model,
                    tools=tools,
                    system_prompt=SYSTEM_PROMPT,
                    middleware=middleware,
                    state_schema=AgentGraphState,
                    context_schema=RunContext,
                    checkpointer=AsyncPostgresSaver(connection),
                    interrupt_after=["tools"],
                )
                try:
                    result = await graph.ainvoke(
                        cast(Any, payload),
                        cast(Any, config),
                        context=RunContext(toolbox=cast(Any, box), thread_id=session.thread_id),
                    )
                except (GraphRunAborted, StaleResponseError):
                    if not case.startswith("control_"):
                        raise
                    async with database.session() as db:
                        row = await db.get(SessionGraphRow, box.session_id)
                        window = await db.get(
                            SessionGraphReceiptRow,
                            {"session_id": box.session_id, "request_id": "review:5"},
                        )
                    assert review_index == 1 and len(work) == 5 and not trap
                    assert row.revision == 0 and row.reviewed_seq == 0
                    assert window.receipt["attempts"] == 1
                    RESULTS.mkdir(parents=True, exist_ok=True)
                    (RESULTS / f"e0-{case}-{'sse' if stream else 'json'}.json").write_text(
                        json.dumps(
                            {
                                "case": case,
                                "stream": stream,
                                "status": "PASS",
                                "http_exchanges": len(requests),
                                "review_requests": 1,
                                "requests": requests,
                                "middleware": topology,
                                "boundary_result": "late response blocked; quota spent; no graph commit",
                                "real_provider_requests": 0,
                                "external_dispatches": 0,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    return
                saved = await graph.aget_state(cast(Any, config))
            reconnects += 1
            if result.get("segment_yield_reason"):
                payload = {
                    "segment_index": int(result.get("segment_index") or 0) + 1,
                    "segment_completed_rounds": 0,
                    "segment_yield_reason": None,
                }
            elif not saved.next:
                break
            else:
                payload = None
        else:
            pytest.fail("有限离线请求未结束")
    assert result["messages"][-1].content == "模拟阶段结束"
    assert len(work) == 5 and not trap
    assert normal_index == 6
    expected_reviews = 1 if case in {"valid", "get_graph_only"} else 2
    assert review_index == expected_reviews
    assert [r["phase"] for r in requests] == ["agent"] * 5 + ["graph_review"] * expected_reviews + [
        "agent"
    ]
    assert len(bindings) == len(requests)
    for bound, wire in zip(bindings, requests, strict=True):
        assert bound["names"] == wire["tools"]
    async with database.session() as db:
        row = await db.get(SessionGraphRow, box.session_id)
        window = await db.get(
            SessionGraphReceiptRow, {"session_id": box.session_id, "request_id": "review:5"}
        )
        calls = (
            (await db.execute(select(ModelCallRow).where(ModelCallRow.run_id == box.run_id)))
            .scalars()
            .all()
        )
        events = (
            (
                await db.execute(
                    select(EventRow)
                    .where(EventRow.run_id == box.run_id, EventRow.type == "model.response_quality")
                    .order_by(EventRow.session_seq)
                )
            )
            .scalars()
            .all()
        )
        operations = (
            (
                await db.execute(
                    select(ToolOperationRow).where(ToolOperationRow.run_id == box.run_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(calls) == len(requests)
    assert window.receipt["attempts"] == expected_reviews and window.receipt["status"] == "closed"
    outcomes = [e.payload["outcome"] for e in events if e.payload.get("scope") == "graph_review"]
    wanted = {
        "valid": "submitted_update",
        "disallowed": "disallowed_tool",
        "no_tool": "no_tool_call",
        "invalid_json": "invalid_arguments",
        "missing_page": "invalid_arguments",
        "schema_invalid": "invalid_arguments",
        "validation_failed": "validation_failed",
        "get_graph_only": "read_graph",
        "disallowed_twice": "disallowed_tool",
    }[case]
    assert wanted in outcomes
    accepted = case not in {"get_graph_only", "disallowed_twice"}
    assert row.revision == int(accepted)
    assert bool(row.reviewed_seq) == accepted and row.reviewed_rounds == (5 if accepted else 0)
    assert len(row.document["information_graph"]["nodes"]) == int(accepted)
    assert not operations
    messages = result["messages"]
    pending = {c["id"] for m in messages for c in getattr(m, "tool_calls", [])}
    pending -= {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    assert not pending
    # 被拒绝响应保持在观测中，不能落成待派发Checkpoint历史。
    rejected = (
        {"review-1"} if case not in {"valid", "validation_failed", "get_graph_only"} else set()
    )
    if case == "disallowed_twice":
        rejected.add("review-2")
    assert not rejected & {c["id"] for m in messages for c in getattr(m, "tool_calls", [])}
    qualities = [
        e.payload
        for e in events
        if e.payload.get("scope") == "graph_review" and e.payload.get("response_id")
    ]
    review_wires = [r for r in requests if r["phase"] == "graph_review"]
    assert len(qualities) == len(review_wires)
    for quality, wire in zip(qualities, review_wires, strict=True):
        assert quality["tool_names"] == wire["native_tool_names"]
    checkpoint_names = {c["id"]: c["name"] for m in messages for c in getattr(m, "tool_calls", [])}
    assert all(checkpoint_names[f"work-{i}"] == "fixture_read" for i in range(1, 6))
    summary = {
        "case": case,
        "stream": stream,
        "status": "PASS",
        "middleware": topology,
        "http_exchanges": len(requests),
        "review_requests": review_index,
        "requests": requests,
        "bindings": bindings,
        "outcomes": outcomes,
        "revision": row.revision,
        "reviewed_seq_positive": bool(row.reviewed_seq),
        "reviewed_rounds": row.reviewed_rounds,
        "checkpoint_reconnections": reconnects,
        "external_dispatches": len(trap),
        "production_database_used": False,
        "real_provider_requests": 0,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"e0-{case}-{'sse' if stream else 'json'}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
