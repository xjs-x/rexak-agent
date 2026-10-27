"""Official create_agent, production tools and repositories, isolated offline SQL.

Historical clones are reconstituted by executing the frozen recipe and reading
its evidence through real tools, not by editing IDs in exported JSON. This is
an offline replay provenance, not a claim of fresh target observations.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
from experiments.history_action_autonomous_shadow_v1.fixture import (
    make_box,
    policy_for,
    save_policy,
)
from experiments.history_action_reflection_pilot_readiness_v1.budget import (
    GuardTransport,
    Ledger,
    request_scope,
)
from experiments.history_action_reflection_pilot_readiness_v1.protocol import (
    NEUTRAL,
    ROOT,
    sha,
    write,
)
from experiments.history_action_reflection_preparation_v1.reflection import (
    FeedbackLedger,
    all_tools_paired,
    notice,
)
from experiments.history_action_trusted_shadow_v1.fixture import parsed, row_dump, tool_arguments
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import Field
from tests.support.scripted_model import ScriptedChatModel, tool_call

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
from agent_workbench.agent.middleware.tool_dispatch import ToolDispatchMiddleware
from agent_workbench.agent.prompts import SYSTEM_PROMPT
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.persistence.tables import ArtifactRow, RunRow, ToolJobRow, ToolOperationRow
from agent_workbench.tools.evidence import read_artifact
from agent_workbench.tools.jobs import get_job
from agent_workbench.tools.shadow_test_profile import run_http_test_profile

TOOLS = [run_http_test_profile, read_artifact, get_job]


def mock_response(_: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "offline",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "mock"},
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "completion_tokens_details": {"reasoning_tokens": 5},
                "prompt_tokens_details": {"cached_tokens": 0},
            },
        },
    )


class RecordingModel(ScriptedChatModel):
    inputs: list = Field(default_factory=list)
    box: Any = None
    phase: str = "seed"
    task_id: str = "A"
    react: bool = False
    ledger: Any = None

    def _generate(self, messages, *args, **kwargs):
        serialized = [m.model_dump(mode="json") for m in messages]
        self.inputs.append(serialized)
        tools = [m for m in messages if isinstance(m, ToolMessage)]
        if self.phase == "seed":
            if not tools:
                answer = tool_call(
                    "run_http_test_profile", {"profile_id": "baseline"}, "seed-probe"
                )
            elif not any(m.name == "read_artifact" for m in tools):
                artifact = next(
                    a for a in self.box.db.rows[ArtifactRow] if a.media_type.startswith("text/")
                )
                answer = tool_call("read_artifact", {"artifact_id": str(artifact.id)}, "seed-read")
            else:
                answer = AIMessage(
                    content="Seed boundary: offline replay evidence prepared, not a live result."
                )
        else:
            count = sum(
                m.name == "run_http_test_profile" and not m.tool_call_id.startswith("seed-")
                for m in tools
            )
            hinted = any("_host_history_reference" in str(m.content) for m in tools)
            final = count >= 3 or (hinted and self.react and count >= 1)
            answer = (
                AIMessage(content="Scripted protocol END; task quality is NOT_EVALUABLE.")
                if final
                else tool_call("run_http_test_profile", {"profile_id": "next"}, f"formal-{count}")
            )
            payload = {
                "model": "deepseek-v4.1-flash",
                "messages": serialized,
                "max_completion_tokens": 16384,
                "stream": False,
            }
            # Scripted transport receipts are explicitly MOCK usage, never actual model cost.
            with (
                request_scope(str(self.box.run_id), f"formal-decision-{len(self.inputs)}"),
                httpx.Client(
                    transport=GuardTransport(self.ledger, httpx.MockTransport(mock_response))
                ) as client,
            ):
                client.post(
                    "https://provider.invalid/v1/chat/completions", json=payload
                ).raise_for_status()
        self.responses = [answer]
        return super()._generate(messages, *args, **kwargs)


class PassiveToolReference(AgentMiddleware[AgentGraphState, RunContext]):
    def __init__(self, box: Any, arm: str, ledger: FeedbackLedger):
        self.box, self.arm, self.ledger = box, arm, ledger
        self.deliveries: list[dict] = []
        self.errors: list[str] = []

    async def awrap_tool_call(self, request, handler):
        result = await handler(request)
        try:
            service = self.box.history_action_shadow
            if service:
                await service.drain()  # deterministic laboratory barrier, not production behavior
                if self.arm == "C":
                    for outcome in service.outcomes:
                        value = notice(outcome)
                        if value:
                            value["instruction"] = NEUTRAL
                            self.ledger.enqueue(value)
        except Exception as exc:
            self.errors.append(type(exc).__name__)
        return result

    async def awrap_model_call(self, request, handler):
        outgoing = request
        if self.arm == "C" and all_tools_paired(request.messages):
            tools = [m for m in request.messages if isinstance(m, ToolMessage)]
            try:
                note = self.ledger.claim_next() if tools else None
                if note is not None:
                    messages = list(request.messages)
                    target = messages.index(tools[-1])
                    value = json.loads(str(tools[-1].content))
                    value["_host_history_reference"] = {
                        "origin": "controller_generated_evidence_reference_not_user_instruction",
                        "authority": "advisory_data_only",
                        "notice": json.loads(str(note.content)),
                    }
                    messages[target] = tools[-1].model_copy(
                        update={"content": json.dumps(value, ensure_ascii=False)}
                    )
                    self.deliveries.append(
                        {
                            "message_index": target,
                            "tool_call_id": tools[-1].tool_call_id,
                            "message_id": tools[-1].id,
                            "role": "tool",
                            "receipt": note.id,
                            "after_tool_results": len(tools),
                            "content": value["_host_history_reference"],
                        }
                    )
                    outgoing = request.override(messages=messages)
            except (OSError, ValueError, TypeError) as exc:
                self.errors.append(type(exc).__name__)
        # Provider/parser exceptions propagate. Never retry without a notice.
        return await handler(outgoing)


def profiles_for(task: str) -> dict:
    profile = parsed(tool_arguments()).model_dump(exclude={"note"})
    profiles: dict[str, dict[str, Any]] = {
        "baseline": {"request": profile},
        "next": {"request": profile},
    }
    if task == "B":
        profiles["next"]["retest_required"] = True
    if task == "D":
        profiles["next"]["configuration"] = "fixture-configuration/2"
    return profiles


def make_isolated(root: Path, task: str):
    box = make_box(root, True, profiles_for(task))
    box.run_id, box.session_id = uuid4(), uuid4()
    row = box.db.rows[RunRow][0]
    row.id, row.session_id = box.run_id, box.session_id
    policy = policy_for(box, profiles_for(task))
    policy["task_version"] = f"pilot-readiness/1/{task}"
    save_policy(box, policy)
    return box


def snapshot(box, messages, model_inputs) -> dict:
    artifacts = []
    for a in box.db.rows.get(ArtifactRow, []):
        raw = box.artifacts.read_bytes(a.relative_key, limit=a.byte_size + 1)
        assert hashlib.sha256(raw).hexdigest() == a.sha256
        artifacts.append({"record": row_dump(a), "raw_utf8": raw.decode("utf-8")})
    return {
        "identity": {"run": str(box.run_id), "session": str(box.session_id)},
        "operations": [row_dump(r) for r in box.db.rows.get(ToolOperationRow, [])],
        "jobs": [row_dump(r) for r in box.db.rows.get(ToolJobRow, [])],
        "artifacts": artifacts,
        "messages": [m.model_dump(mode="json") for m in messages],
        "model_inputs": model_inputs,
        "dispatches": box.executor.submissions,
        "policy": json.loads(
            box.settings.history_action_shadow_context_path.read_text(encoding="utf-8")
        ),
    }


async def seed(box, thread: str):
    model = RecordingModel(box=box)
    graph = create_agent(
        model=model,
        tools=TOOLS,
        middleware=[ToolDispatchMiddleware()],
        state_schema=AgentGraphState,
        context_schema=RunContext,
        checkpointer=InMemorySaver(),
    )
    result = await graph.ainvoke(
        cast(
            Any,
            {
                "messages": [
                    HumanMessage(
                        content="Reconstitute frozen offline historical fixture; read its original evidence.",
                        id="seed-task",
                    )
                ],
                "dispatch_control_version": 0,
            },
        ),
        {"configurable": {"thread_id": thread}},
        context=RunContext(toolbox=box, thread_id=thread),
    )
    await box.history_action_shadow.drain()
    assert all_tools_paired(result["messages"])
    assert all(m.status != "error" for m in result["messages"] if isinstance(m, ToolMessage))
    read = next(
        m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "read_artifact"
    )
    raw = json.loads(str(read.content))["data"]["text"]
    assert raw and raw in json.dumps(model.inputs[-1])
    return result["messages"], snapshot(box, result["messages"], model.inputs)


def equivalence(source: dict, clone: dict) -> dict:
    assert len(source["operations"]) == len(clone["operations"]) == 1
    assert len(source["jobs"]) == len(clone["jobs"]) == 1
    mapping = {source["identity"][k]: clone["identity"][k] for k in ("run", "session")}
    for field, key in (("operations", "operation_id"), ("jobs", "job_id")):
        mapping[source[field][0][key]] = clone[field][0][key]
    source_art = {a["record"]["sha256"]: a for a in source["artifacts"]}
    clone_art = {a["record"]["sha256"]: a for a in clone["artifacts"]}
    assert source_art.keys() == clone_art.keys()
    for digest, original in source_art.items():
        fresh = clone_art[digest]
        assert original["raw_utf8"] == fresh["raw_utf8"]
        mapping[original["record"]["id"]] = fresh["record"]["id"]
        assert original["record"]["id"] != fresh["record"]["id"]
        assert fresh["record"]["source_operation_id"] == clone["operations"][0]["operation_id"]
        assert fresh["record"]["session_id"] == clone["identity"]["session"]
    assert all(a != b for a, b in mapping.items())
    for key in ("tool_name", "status", "request_digest"):
        assert source["operations"][0][key] == clone["operations"][0][key]
    assert source["jobs"][0]["state"] == clone["jobs"][0]["state"]
    assert source["policy"]["profiles"] == clone["policy"]["profiles"]
    original_payload = copy.deepcopy(source["dispatches"][0]["payload"])
    clone_payload = clone["dispatches"][0]["payload"]
    # Controller-owned anonymous session identity, no credential identity change.
    assert original_payload["identity_id"] == "session-" + source["identity"]["session"]
    assert clone_payload["identity_id"] == "session-" + clone["identity"]["session"]
    mapping[original_payload["identity_id"]] = clone_payload["identity_id"]
    original_payload["identity_id"] = clone_payload["identity_id"]
    assert original_payload == clone_payload
    # Compare complete records and initial messages after only declared identity/time remaps.
    for original, fresh in zip(source["messages"], clone["messages"], strict=True):
        if original.get("id") and fresh.get("id") and original["id"] != fresh["id"]:
            mapping[original["id"]] = fresh["id"]
    timestamps = {
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "captured_at",
        "last_synced_at",
        "executor_accepted_at",
        "executor_started_at",
        "executor_finished_at",
    }

    def normalized(value, original_side: bool, key=""):
        if key in timestamps and value is not None:
            return "<timestamp>"
        if isinstance(value, dict):
            return {k: normalized(v, original_side, k) for k, v in value.items()}
        if isinstance(value, list):
            return [normalized(v, original_side) for v in value]
        if isinstance(value, str):
            try:
                parsed_value = json.loads(value)
                if isinstance(parsed_value, (dict, list)):
                    return normalized(parsed_value, original_side)
            except ValueError:
                pass
            if original_side:
                for old, fresh in mapping.items():
                    value = value.replace(old, fresh)
        return value

    for field in ("operations", "jobs", "artifacts", "messages", "model_inputs"):
        assert normalized(source[field], True) == normalized(clone[field], False), field
    return {
        "status": "PASS",
        "mapping": mapping,
        "artifact_hashes": sorted(source_art),
        "provenance": "production tools + repository writes + frozen MockTransport response replay; not JSON ID mutation",
        "nonsemantic_variation": ["UUIDs", "capture/finish times", "policy expiry/binding digest"],
        "raw_evidence_seen": "actual read_artifact ToolMessage in seed scripted model request; no assertion that live model read it",
        "complete_initial_comparison": [
            "operations",
            "jobs",
            "artifacts",
            "messages",
            "model_inputs",
        ],
    }


async def close(box):
    if box.history_action_shadow:
        await box.history_action_shadow.aclose()
    await box.executor.close()


async def run_one(
    root: Path, task: dict, arm: str, source: dict, ledger: Ledger, *, react=False, recover=False
) -> dict:
    started = time.perf_counter()
    box = make_isolated(root, task["task_id"])
    thread = str(uuid4())
    try:
        messages, initial = await seed(box, thread)
        match = equivalence(source, initial)
        write(
            root / "clone.json",
            {
                "source_sha256": hashlib.sha256(
                    json.dumps(source, sort_keys=True).encode()
                ).hexdigest(),
                "equivalence": match,
                "snapshot": initial,
            },
        )
        if arm == "A":
            await box.history_action_shadow.aclose()
            box.history_action_shadow = None
            box.settings.history_action_shadow_enabled = False
        else:
            box.history_action_shadow.outcomes.clear()
        ledger.register(str(box.run_id))
        model = RecordingModel(
            box=box, phase="formal", task_id=task["task_id"], react=react, ledger=ledger
        )
        feedback = FeedbackLedger(root / "feedback", str(box.run_id), thread)
        middleware = PassiveToolReference(box, arm, feedback)
        saver = InMemorySaver()
        config: Any = {"configurable": {"thread_id": thread}, "recursion_limit": 20}
        context = RunContext(toolbox=box, thread_id=thread)
        deliveries = []

        def graph():
            return create_agent(
                model=model,
                tools=TOOLS,
                system_prompt=SYSTEM_PROMPT,
                middleware=[ToolDispatchMiddleware(), ContextBuilderMiddleware(), middleware],
                state_schema=AgentGraphState,
                context_schema=RunContext,
                checkpointer=saver,
                interrupt_after=["tools"] if recover else None,
            )

        runner = graph()
        initial_input = {
            "messages": [*messages, HumanMessage(content=task["user_task"], id="formal-task")],
            "dispatch_control_version": 0,
        }
        result = await runner.ainvoke(cast(Any, initial_input), config, context=context)
        if recover:
            for _ in range(5):
                state = await runner.aget_state(config)
                if not state.next:
                    break
                deliveries.extend(middleware.deliveries)
                middleware = PassiveToolReference(
                    box, arm, FeedbackLedger(root / "feedback", str(box.run_id), thread)
                )
                runner = graph()
                result = await runner.ainvoke(None, config, context=context)
        deliveries.extend(middleware.deliveries)
        if box.history_action_shadow:
            await box.history_action_shadow.drain()
        assert all_tools_paired(result["messages"])
        assert all(m.status != "error" for m in result["messages"] if isinstance(m, ToolMessage))
        formal_messages = result["messages"][len(messages) :]
        calls = [c for m in formal_messages if isinstance(m, AIMessage) for c in m.tool_calls]
        observed = (
            copy.deepcopy(box.history_action_shadow.outcomes) if box.history_action_shadow else []
        )
        assert (arm != "A" or not observed) and (arm == "C" or not deliveries)
        assert len(model.inputs) == len(calls) + 1
        assert len(box.executor.jobs) == len(calls) + 1
        assert not middleware.errors, middleware.errors
        # Injection exists only in normal request; checkpoint retains original tool result.
        assert not any("_host_history_reference" in str(m.content) for m in result["messages"])
        for wire in model.inputs:
            assert len([m for m in wire if m["type"] == "human"]) == 2
        post = snapshot(box, result["messages"], model.inputs)
        ids = {k: post["identity"][k] for k in post["identity"]}
        ids["thread"] = thread
        output = {
            "task": task["task_id"],
            "arm": arm,
            "identity": ids,
            "recovery": recover,
            "react_script": react,
            "clone": match,
            "initial_history_boundary": len(messages),
            "model_inputs": model.inputs,
            "tool_calls": calls,
            "final_messages": [m.model_dump(mode="json") for m in formal_messages],
            "deliveries": deliveries,
            "observations": observed,
            "model_requests": len(model.inputs),
            "formal_executor_jobs": len(calls),
            "end": "natural_END",
            "snapshot": post,
            "elapsed_ms": (time.perf_counter() - started) * 1000,
            "usage_origin": "MOCK_TRANSPORT_ONLY_NOT_PROVIDER_USAGE",
            "actual_usage": "NOT_AVAILABLE",
            "currency": "NOT_AVAILABLE",
            "quality": "NOT_EVALUABLE_SCRIPTED_NOT_INDEPENDENTLY_REVIEWED",
            "production_stack_parity": "NOT_VERIFIED_FULL_WORKING_CONTEXT_GRAPHREVIEW_RUNNER",
            "first_triggering_call_saved": False,
        }
        write(root / "trace.json", output)
        return output
    finally:
        await close(box)


async def main():
    tasks = {
        t["task_id"]: t
        for t in json.loads((ROOT / "frozen/tasks.json").read_text(encoding="utf-8"))
    }
    for name, value in (
        ("tool-schemas.json", [convert_to_openai_tool(t) for t in TOOLS]),
        (
            "production-prompt.json",
            {"text": SYSTEM_PROMPT, "sha256": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()},
        ),
    ):
        path = ROOT / "frozen" / name
        if path.exists():
            assert json.loads(path.read_text(encoding="utf-8")) == value
        else:
            write(path, value)
    sources = {}
    for task in tasks:
        existing = ROOT / "seeds" / task / "source.json"
        if existing.exists():
            sources[task] = json.loads(existing.read_text(encoding="utf-8"))
            continue
        box = make_isolated(ROOT / "seeds" / task, task)
        try:
            _, sources[task] = await seed(box, str(uuid4()))
            write(ROOT / "seeds" / task / "source.json", sources[task])
        finally:
            await close(box)
    ledger = Ledger(ROOT / "results/requests.sqlite")
    schedule = json.loads((ROOT / "frozen/schedule.json").read_text(encoding="utf-8"))["rows"]
    rows = []
    for entry in schedule:
        row = await run_one(
            ROOT / "results/runs" / entry["key"],
            tasks[entry["task"]],
            entry["arm"],
            sources[entry["task"]],
            ledger,
            react=entry["task"] == "A" and entry["repeat"] == 2,
            recover=entry["arm"] == "C" and entry["repeat"] == 1,
        )
        rows.append(row)
        print(
            entry["key"],
            "tools",
            len(row["tool_calls"]),
            "notices",
            len(row["deliveries"]),
            flush=True,
        )
    identities = [v for r in rows for v in r["identity"].values()]
    assert len(identities) == len(set(identities))
    all_ops = [o["operation_id"] for r in rows for o in r["snapshot"]["operations"]]
    all_jobs = [o["job_id"] for r in rows for o in r["snapshot"]["jobs"]]
    all_artifacts = [o["record"]["id"] for r in rows for o in r["snapshot"]["artifacts"]]
    for values in (all_ops, all_jobs, all_artifacts):
        assert len(values) == len(set(values))
    for task in tasks:
        starts = [r["model_inputs"][0] for r in rows if r["task"] == task]
        assert (
            len({next(m["content"] for m in x if m.get("id") == "formal-task") for x in starts})
            == 1
        )
    write(
        ROOT / "results/rehearsal.json",
        {
            "status": "PASS_COMPONENT_PROTOCOL_ONLY",
            "rows": [
                {
                    k: r[k]
                    for k in (
                        "task",
                        "arm",
                        "identity",
                        "model_requests",
                        "formal_executor_jobs",
                        "deliveries",
                        "react_script",
                        "recovery",
                        "quality",
                        "production_stack_parity",
                    )
                }
                for r in rows
            ],
            "physical_mock_requests": len(ledger.rows()),
            "provider_requests": 0,
            "isolation": "24 distinct run/session/thread/saver/domain dictionaries/data roots; operations/jobs/artifact IDs disjoint",
            "source_hashes": {t: sha(ROOT / "seeds" / t / "source.json") for t in tasks},
        },
    )
    write(ROOT / "results/request-ledger.json", {"origin": "MOCK_ONLY", "rows": ledger.rows()})


if __name__ == "__main__":
    asyncio.run(main())
