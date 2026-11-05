"""冻结历史形状的工程 A/B；不调用模型、Executor 或真实目标。

原非法请求与后续历史合法请求是两类 oracle。后者不是模型修复输出，
更不保证与首次请求语义相同。所有 A/B 写入均在测试库的回滚事务中。
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import event, select
from sqlalchemy.engine import make_url

from agent_workbench.contracts.graph_errors import GraphValidationError
from agent_workbench.contracts.session_graph import GraphOperation
from agent_workbench.domain import session_graph as current
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.tables import (
    ArtifactRow,
    SessionGraphReceiptRow,
    SessionGraphRow,
    ToolOperationRow,
)
from agent_workbench.tools.graph_errors import format_graph_schema_error, render_graph_failure
from agent_workbench.tools.session_graph import update_graph as graph_tool
from tests.integration.test_session_graph import fixture

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = ROOT / "experiments/graph_update_engineering_fix_20261028"
HISTORY = EXPERIMENT / "history/snapshot-20261028T070507678035Z"
ADAPTER = TypeAdapter(list[GraphOperation])
CASE_FILES = sorted(HISTORY.glob("R*-F*.json"))
assert len(CASE_FILES) == 14


def baseline_module():
    """只加载已冻结的旧领域实现；契约结构另有 Schema 等价回归。"""
    rel = "src/agent_workbench/domain/session_graph.py"
    path = EXPERIMENT / "baseline-source" / rel
    manifest = json.loads((EXPERIMENT / "baseline-source/manifest.json").read_text("utf-8"))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest[rel]
    spec = importlib.util.spec_from_file_location("frozen_graph_engineering_baseline", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BASELINE = baseline_module()


def load_case(path):
    manifest = json.loads((HISTORY / "manifest.json").read_text("utf-8"))
    metadata = next(x for x in manifest["cases"] if x["filename"] == path.name)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata["sha256"]
    return json.loads(path.read_text("utf-8"))


async def seed_sources(db, box, case, *, repair_time):
    """只重建 fixture 声明存在的同会话来源；不补造原错误的来源。"""
    sources = [
        x for x in case["source_reference_state"]
        if x["exists"] and x["same_session"]
        and (repair_time or x["exists_by_failed_call_time"])
    ]
    operation_ids = {
        UUID(x["ref"].split(":", 1)[1]) for x in sources if x["ref"].startswith("operation:")
    }
    operation_ids.update(
        UUID(x["source_operation_ref"].split(":", 1)[1])
        for x in sources if x.get("source_operation_ref")
    )
    operation_metadata = {
        UUID(x["ref"].split(":", 1)[1]): x
        for x in sources if x["ref"].startswith("operation:")
    }
    for key in operation_ids:
        if await db.get(ToolOperationRow, key) is None:
            metadata = operation_metadata.get(key, {})
            db.add(ToolOperationRow(
                operation_id=key, run_id=box.run_id, session_id=box.session_id,
                tool_call_id=f"fixture-{key}", tool_name="fixture_evidence",
                request_digest="0" * 64, effect_class="read_only", policy_revision=1,
                scope_revision=1, status=metadata.get("status") or "unknown",
                result_artifact_ids=[],
            ))
    for source in sources:
        if not source["ref"].startswith("artifact:"):
            continue
        key = UUID(source["ref"].split(":", 1)[1])
        if await db.get(ArtifactRow, key) is not None:
            continue
        stored = box.artifacts.write_bytes(b"synthetic graph provenance", media_type="text/plain")
        db.add(ArtifactRow(
            id=key, session_id=box.session_id, sha256=stored.sha256,
            byte_size=stored.byte_size, media_type=stored.media_type,
            relative_key=stored.relative_key,
            source_operation_id=(
                UUID(source["source_operation_ref"].split(":", 1)[1])
                if source.get("source_operation_ref") else None
            ),
            availability=source["availability"], sensitivity=source["sensitivity"],
            captured_at=datetime.now(UTC),
        ))
    await db.flush()


async def seed_case(db, box, case):
    state = case["authoritative_before"]
    page = case["failed_call"].get("page_record")
    # Historical snapshots preserve page.from_seq, but not a complete runtime
    # cursor. Both arms use the same explicit isolated frontier, not a claim of
    # exact whole-Run replay. A stale revision remains stale regardless of it.
    frontier = page["from_seq"] if page else 0
    db.add(SessionGraphRow(
        session_id=box.session_id, document=copy.deepcopy(state["document"]),
        revision=state["revision"], reviewed_seq=frontier, completed_rounds=0,
        reviewed_rounds=0, next_sync_round=5, missed_syncs=0,
    ))
    if page:
        db.add(SessionGraphReceiptRow(
            session_id=box.session_id, request_id=case["failed_call"]["arguments"]["page_id"],
            digest="frozen-page", receipt=copy.deepcopy(page),
        ))
    await seed_sources(db, box, case, repair_time=False)
    await db.flush()


async def graph_state(db, box):
    row = await db.get(SessionGraphRow, box.session_id, populate_existing=True)
    assert row is not None
    receipts = (await db.execute(select(SessionGraphReceiptRow).where(
        SessionGraphReceiptRow.session_id == box.session_id
    ))).scalars().all()
    return {
        "document": copy.deepcopy(row.document), "revision": row.revision,
        "reviewed_seq": row.reviewed_seq, "reviewed_rounds": row.reviewed_rounds,
        "next_sync_round": row.next_sync_round, "missed_syncs": row.missed_syncs,
        "receipt_count": len(receipts),
    }


async def invoke(db, box, module, request_id, args):
    # Use the actual model-visible tool parameter model. Validating only the
    # operation list would erase the "operations" location prefix and would
    # not reproduce the production callback's ValidationError.
    cast(Any, graph_tool.tool_call_schema).model_validate(args)
    parsed = ADAPTER.validate_python(args["operations"])
    result = await module.update_graph(
        db, box, request_id, args["expected_revision"], parsed,
        page_id=args.get("page_id"),
    )
    await db.flush()
    return result


async def fresh_page(db, box, case, args, name):
    """Synthetic current page for correction; never accept an old page implicitly."""
    row = await db.get(SessionGraphRow, box.session_id)
    assert row is not None
    page_id = f"page:engineering-{case['case_id']}-{name}"
    record = {
        "revision": row.revision, "from_seq": row.reviewed_seq,
        "through_seq": row.reviewed_seq + 1, "rounds": row.completed_rounds,
        "presented": True, "complete": True,
    }
    db.add(SessionGraphReceiptRow(
        session_id=box.session_id, request_id=page_id, digest="synthetic-current-page",
        receipt=record,
    ))
    await db.flush()
    args["page_id"] = page_id


def minimal_correction(case):
    """A human-defined test request, not an automatic production repair.

    Missing intended State, invalid evidence support, and misplaced State source
    semantics cannot be reconstructed from redacted text: do not invent them.
    """
    if case["case_id"] in {"R1-F01", "R1-F02", "R2-F01"}:
        return None
    args = copy.deepcopy(case["failed_call"]["arguments"])
    local_ids = {x.get("temp_id") for x in args["operations"] if "temp_id" in x}
    mapping = {}
    for previous in case["visible_prior_id_mappings"]:
        mapping.update(previous["id_mapping"])
    for operation in args["operations"]:
        data = operation["data"]
        if "summary" in operation:
            assert "summary" not in data
            data["summary"] = operation.pop("summary")
        if operation["graph"] == "attack" and operation["op"].endswith("node") and "label" in data:
            assert "summary" not in data
            data["summary"] = data.pop("label")
        for key in ("from_id", "to_id"):
            value = data.get(key)
            if value is not None and value not in local_ids and value in mapping:
                data[key] = mapping[value]
        if "entity_refs" in data:
            data["entity_refs"] = [
                mapping.get(x, x) if x not in local_ids else x for x in data["entity_refs"]
            ]
    assert len(args["operations"]) == len(case["failed_call"]["arguments"]["operations"])
    return args


async def run_arm(database, box, case, module, arm):
    async with database.session() as db:
        await db.begin()
        try:
            await seed_case(db, box, case)
            before = await graph_state(db, box)
            counts = Counter()

            def count_sql(_connection, _cursor, statement, _parameters, _context, _many):
                counts[statement.split(None, 1)[0].upper()] += 1

            connection = (await db.connection()).sync_connection
            event.listen(connection, "before_cursor_execute", count_sql)
            try:
                with pytest.raises(ValueError) as rejected:
                    async with db.begin_nested():
                        await invoke(db, box, module, "invalid-frozen-input", case["failed_call"]["arguments"])
            finally:
                event.remove(connection, "before_cursor_execute", count_sql)
            assert await graph_state(db, box) == before
            error = rejected.value
            feedback = None
            if arm == "B":
                if isinstance(error, ValidationError):
                    feedback = json.loads(format_graph_schema_error(error))
                else:
                    assert isinstance(error, GraphValidationError)
                    feedback = json.loads(render_graph_failure(error))
                assert feedback["status"] == "failed"
                assert feedback["execution_certainty"] == "not_applied"
                assert feedback["error"]["code"] == "validation_failed"
                details = feedback["error"]["details"]
                assert details["graph_code"] and details["path"]
                assert details["expected"] and details["actual"]
                assert feedback["error"]["retryable"] is False
                assert feedback["error"]["recovery_hint"]
                assert details["path"].startswith(("operations[", "page_id"))
                reported_paths = {details["path"]} | {
                    issue["path"] for issue in details.get("issues", [])
                }
                expected_paths = set()
                for diagnostic in case["failure_diagnostics"]:
                    parts = [x for x in diagnostic["field_path"] if x not in {
                        "add_node", "update_node", "add_edge", "update_edge",
                        "Entity", "AttackState", "Relation", "Attempt",
                    }]
                    expected_paths.add("".join(
                        f"[{part}]" if isinstance(part, int) else "." + part
                        for part in parts
                    ).lstrip("."))
                assert any(
                    actual == expected or actual.startswith(expected + ".")
                    for actual in reported_paths for expected in expected_paths
                ), (reported_paths, expected_paths)
                rendered = json.dumps(feedback, ensure_ascii=False)
                assert "Redacted semantic text" not in rendered and len(rendered) < 4096

            # A minimally corrected original input is separate from the first
            # later accepted historical request, which may contain new facts.
            corrected = minimal_correction(case)
            correction_result = None
            if corrected is not None:
                async with db.begin_nested() as point:
                    if case["failure_category"] == "stale_page_revision":
                        await fresh_page(db, box, case, corrected, "minimal")
                    correction_result = await invoke(db, box, module, "minimal-correction", corrected)
                    expected, _ = BASELINE.apply_operations(
                        before["document"], ADAPTER.validate_python(corrected["operations"]),
                        str(box.session_id) + "minimal-correction",
                    )
                    actual = await graph_state(db, box)
                    assert actual["document"] == expected
                    assert actual["revision"] == before["revision"] + 1
                    assert correction_result["revision"] == before["revision"] + 1
                    await point.rollback()
                assert await graph_state(db, box) == before

            # Replay the recorded *later* accepted shape with its repair-time
            # source membership, identical in A and B. No model is involved.
            await seed_sources(db, box, case, repair_time=True)
            later = copy.deepcopy(case["accepted_repair"]["arguments"])
            assert case["accepted_repair"]["before_graph"] == before["document"]
            assert later["expected_revision"] == before["revision"]
            await fresh_page(db, box, case, later, "historical-legal")
            accepted = await invoke(db, box, module, "historical-legal", later)
            after = await graph_state(db, box)
            assert after["revision"] == before["revision"] + 1
            expected, expected_ids = BASELINE.apply_operations(
                before["document"], ADAPTER.validate_python(later["operations"]),
                str(box.session_id) + "historical-legal",
            )
            assert after["document"] == expected and accepted["id_mapping"] == expected_ids
            assert await invoke(db, box, module, "historical-legal", later) == accepted
            assert await graph_state(db, box) == after
            return {
                "arm": arm, "original_rejected": True,
                "rejection_sql_statements": dict(counts),
                "feedback": feedback,
                "minimal_original_correction": "accepted" if corrected else "NOT_DETERMINED",
                "minimal_original_result": correction_result,
                "historical_later_request": "accepted_not_semantic_identity_claim",
                "historical_result": accepted, "after": after,
                "model_calls": 0, "extra_graph_reads": 0,
                "correction_requires_fresh_page": case["failure_category"] == "stale_page_revision",
                "frontier_scope": "same_synthetic_frontier_not_complete_historical_runtime",
            }
        finally:
            await db.rollback()


@pytest.mark.parametrize("case_path", CASE_FILES, ids=lambda path: path.stem)
async def test_frozen_fourteen_original_rejections_and_legal_paths(
    database, unique_name, case_path, record_property,
):
    assert (make_url(database.settings.database_url).database or "").startswith("workbench_test_")
    case = load_case(case_path)
    box = await fixture(database, unique_name)
    box.artifacts = ArtifactStore(database.settings)
    baseline = await run_arm(database, box, case, BASELINE, "A")
    revised = await run_arm(database, box, case, current, "B")
    for key in ("original_rejected", "minimal_original_correction", "minimal_original_result",
                "historical_result", "after", "rejection_sql_statements"):
        assert baseline[key] == revised[key], key
    record_property("engineering_case", json.dumps({
        "case_id": case["case_id"], "category": case["failure_category"],
        "original_arguments_sha256": case["provenance"]["raw_arguments_sha256"],
        "A": baseline, "B": revised,
        "model_failure_rate_reduction": "MODEL_FAILURE_RATE_REDUCTION_NOT_VERIFIED",
    }, ensure_ascii=False))


@pytest.mark.parametrize("fault", ["old_alias", "wrong_graph_type", "cross_graph_endpoint", "foreign_source"])
async def test_rejected_boundaries_are_identical(database, unique_name, fault):
    box = await fixture(database, unique_name)
    box.artifacts = ArtifactStore(database.settings)
    operation_id = uuid4()
    inputs = {
        "old_alias": [{"op": "add_node", "graph": "attack", "temp_id": "s",
                       "data": {"summary": "state", "entity_refs": ["old-e"]}}],
        "wrong_graph_type": [{"op": "add_node", "graph": "attack", "temp_id": "s",
                              "data": {"label": "entity is not state"}}],
        "cross_graph_endpoint": [
            {"op": "add_node", "graph": "information", "temp_id": "e", "data": {"label": "entity"}},
            {"op": "add_edge", "graph": "attack", "temp_id": "attempt",
             "data": {"from_id": "e", "to_id": "e", "action": "attempt", "outcome": "unknown",
                      "source_refs": [f"operation:{operation_id}"]}},
        ],
        "foreign_source": [{"op": "add_node", "graph": "information", "temp_id": "e",
                            "data": {"label": "entity", "source_refs": [f"operation:{operation_id}"]}}],
    }
    for module in (BASELINE, current):
        async with database.session() as db:
            await db.begin()
            try:
                db.add(ToolOperationRow(
                    operation_id=operation_id, run_id=box.run_id,
                    session_id=uuid4() if fault == "foreign_source" else box.session_id,
                    tool_call_id="isolated-boundary", tool_name="fixture", request_digest="0" * 64,
                    effect_class="read_only", policy_revision=1, scope_revision=1,
                    status="succeeded", result_artifact_ids=[],
                ))
                await db.flush()
                with pytest.raises(ValueError):
                    async with db.begin_nested():
                        await invoke(db, box, module, "boundary", {"expected_revision": 0, "operations": inputs[fault]})
                assert await db.get(SessionGraphRow, box.session_id) is None
            finally:
                await db.rollback()


async def test_empty_ack_replay_and_revision_race(database, unique_name):
    """Same old revision has one winner; an empty valid ack still advances once."""
    import asyncio

    for module in (BASELINE, current):
        box = await fixture(database, unique_name + module.__name__)

        async def write(request_id, writer=module, runtime=box):
            try:
                async with database.transaction() as db:
                    return await writer.update_graph(db, runtime, request_id, 0, [])
            except ValueError as error:
                return error

        results = await asyncio.gather(write("concurrent-a"), write("concurrent-b"))
        winners = [x for x in results if isinstance(x, dict)]
        failures = [x for x in results if isinstance(x, ValueError)]
        assert len(winners) == len(failures) == 1
        assert winners[0]["revision"] == 1
        assert "版本冲突" in str(failures[0])
        winner_id = "concurrent-a" if isinstance(results[0], dict) else "concurrent-b"
        async with database.transaction() as db:
            assert await module.update_graph(db, box, winner_id, 0, []) == winners[0]
        async with database.session() as db:
            row = await db.get(SessionGraphRow, box.session_id)
            assert row.revision == 1 and row.document == current.empty_graph()
