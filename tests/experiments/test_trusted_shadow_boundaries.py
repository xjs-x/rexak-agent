"""正式评价后的集成状态/资源边界回归；不是新的留出评分。"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from experiments.history_action_trusted_shadow_v1.fixture import (
    RunRow,
    approve,
    business,
    graph_run,
    make_toolbox,
    tool_arguments,
)

from agent_workbench.tools import history_action_shadow as shadow


async def close(box):
    if box.history_action_shadow:
        await box.history_action_shadow.aclose()
    await box.executor.close()


@pytest.mark.parametrize("new_material", [False, True])
async def test_sequential_continuation_uses_material_evidence_not_new_ids(tmp_path, new_material):
    a = tool_arguments()
    ids = ["seed", "r1", "r2", "r3"]
    box = make_toolbox(tmp_path, True, [approve(c, a) for c in ids])
    if new_material:
        original = box.executor._response

        def respond(request):
            response = original(request)
            if len(box.executor.submissions) == 2:
                job = response.json()["job_id"]
                body = b"a new independent response body"
                box.executor.files[job]["response.body"] = body
                meta = json.loads(box.executor.files[job]["response.meta.json"])
                meta.update(body_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest())
                box.executor.files[job]["response.meta.json"] = json.dumps(meta).encode()
            return response

        await box.executor.client.aclose()
        box.executor.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        await graph_run(box, [[("http_request", a, c)] for c in ids])
        await box.history_action_shadow.drain()
        outcomes = box.history_action_shadow.outcomes
        assert sum(o.get("emit_shadow_event", False) for o in outcomes) == (
            2 if new_material else 1
        )
        assert len(box.executor.jobs) == 4  # 观察不阻止原 Agent 继续执行。
    finally:
        await close(box)


async def test_dense_history_guard_abstains_without_changing_execution(tmp_path, monkeypatch):
    a = tool_arguments()
    comparisons = []
    reader = shadow.read_records
    for enabled in (False, True):
        box = make_toolbox(tmp_path / str(enabled), enabled, [approve("seed", a)])
        box.settings.history_action_shadow_history_limit = 1
        if enabled:

            async def too_many_records(current):
                # 使用现有 read_records 的真实资源检查；不是返回截断后假装完整的历史。
                from uuid import UUID

                from agent_workbench.persistence.tables import ToolOperationRow

                current.db.rows[ToolOperationRow].append(
                    ToolOperationRow(
                        operation_id=UUID(int=123),
                        session_id=current.session_id,
                        tool_name="http_request",
                        tool_call_id="old",
                        run_id=current.run_id,
                    )
                )
                try:
                    return await reader(current)
                finally:
                    current.db.rows[ToolOperationRow].pop()

            monkeypatch.setattr(shadow, "read_records", too_many_records)
        try:
            _, model, result, _, _ = await graph_run(box, [[("http_request", a, "seed")]])
            if box.history_action_shadow:
                await box.history_action_shadow.drain()
                assert any(
                    o.get("diagnostic") == "ShadowHistoryLimit"
                    for o in box.history_action_shadow.outcomes
                )
            comparisons.append(business(box, result, model))
        finally:
            await close(box)
    assert comparisons[0] == comparisons[1]


@pytest.mark.parametrize("name", ["http_request_alias", "simulate_check"])
async def test_unregistered_tools_do_not_gain_http_semantics(tmp_path, name):
    a = tool_arguments()
    comparisons = []
    for enabled in (False, True):
        box = make_toolbox(
            tmp_path / str(enabled), enabled, [approve("seed", a), approve("other", a)]
        )
        try:
            _, model, result, _, _ = await graph_run(
                box, [[("http_request", a, "seed")], [(name, a, "other")]]
            )
            if box.history_action_shadow:
                await box.history_action_shadow.drain()
            assert len(box.executor.submissions) == 1
            comparisons.append(business(box, result, model))
        finally:
            await close(box)
    assert comparisons[0] == comparisons[1]


@pytest.mark.parametrize("state", ["stale", "cancelled", "wrong-owner"])
async def test_original_control_guards_still_precede_shadow(tmp_path, state):
    a = tool_arguments()
    comparisons = []
    for enabled in (False, True):
        box = make_toolbox(tmp_path / str(enabled), enabled, [approve("seed", a)])
        row = box.db.rows[RunRow][0]
        if state == "stale":
            row.control_version = 1
        elif state == "cancelled":
            row.status = "cancelled"
        else:
            row.owner = "other-owner"
        try:
            _, model, result, _, _ = await graph_run(box, [[("http_request", a, "seed")]])
            assert not box.executor.submissions and box.history_action_shadow is None
            comparisons.append(business(box, result, model))
        finally:
            await close(box)
    assert comparisons[0] == comparisons[1]


def test_filesystem_event_claim_is_atomic_under_competing_workers(tmp_path):
    path = tmp_path / "events" / "same-key.json"
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(
            pool.map(
                lambda _: shadow.write_once(path, {"dedup_key": "same-key", "status": "triggered"}),
                range(32),
            )
        )
    assert sum(claims) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "triggered"


def test_public_tool_schema_is_identical_to_exact_prechange_source():
    import importlib.util
    import sys
    from pathlib import Path
    from typing import Any, cast

    from agent_workbench.tools.http import http_request

    path = (
        Path(__file__).resolve().parents[2]
        / "experiments/history_action_trusted_shadow_v1/baseline/src__agent_workbench__tools__http.py"
    )
    spec = importlib.util.spec_from_file_location("offline_prechange_schema", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert (
        cast(Any, http_request.tool_call_schema).model_json_schema()
        == module.http_request.tool_call_schema.model_json_schema()
    )


async def test_reverse_operation_job_conflict_never_becomes_completed_repeat(tmp_path):
    from experiments.history_action_trusted_shadow_v1.dataset import generate, predict

    frame = await generate(tmp_path, "development-reverse-job")
    for row in frame["operations"]:
        if row["tool_call_id"] == "seed":
            row["job_id"] = "00000000-0000-0000-0000-000000000099"
    result = predict(frame)
    assert not result["observation"]["emit_shadow_event"]
    assert any("reverse_link_conflict" in r for r in result["reasons"])


async def test_approval_added_after_dispatch_cannot_be_used_as_predecision_fact(tmp_path):
    from experiments.history_action_trusted_shadow_v1.dataset import generate

    frame = await generate(tmp_path / "source", "development-approval-time")
    box = make_toolbox(tmp_path / "target", True, [])
    service = shadow.ShadowService(box)
    await service._gate.acquire()
    try:
        service.enqueue(frame["arguments"], frame["operation"], "current", frame["context"])
        box.settings.history_action_shadow_grants_path.write_text(
            json.dumps([frame["grant"]]), encoding="utf-8"
        )
        service._gate.release()
        await service.drain()
        assert service.outcomes[0]["classification"] == "NOT_IN_SCOPE"
        assert not service.outcomes[0]["emit_shadow_event"]
    finally:
        await service.aclose()
        await box.executor.close()


@pytest.mark.parametrize("boundary", ["run", "task"])
async def test_same_session_does_not_authorize_cross_run_or_task_history(tmp_path, boundary):
    from experiments.history_action_trusted_shadow_v1.dataset import generate, predict

    from agent_workbench.domain.history_action_reflection import digest

    frame = await generate(tmp_path, "development-authorization")
    for row in frame["operations"]:
        if row["tool_call_id"] != "seed":
            continue
        prior = frame["captures"][row["operation_id"]]
        if boundary == "run":
            other_run = "00000000-0000-4000-8000-000000000066"
            row["run_id"] = other_run
            prior["run_id"] = other_run
            prior["approval"]["run_id"] = other_run
        else:
            prior["approval"]["task_version"] = "another-authorized-task"
        prior["approval_sha256"] = digest(prior["approval"])
    result = predict(frame)
    assert not result["observation"]["emit_shadow_event"]
    assert any("authorization_boundary" in reason for reason in result["reasons"])
