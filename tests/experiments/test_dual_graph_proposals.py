"""用冻结来源和现有领域事务校验图提议；全部来源是隔离库合成记录。"""

import json
import os
from pathlib import Path
from uuid import UUID, uuid5

import pytest
from pydantic import TypeAdapter
from tests.experiments.test_dual_graph_replay import isolated_box

from agent_workbench.contracts.session_graph import GraphOperation
from agent_workbench.domain.session_graph import lock_graph, update_graph
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.persistence.repositories import ArtifactRepository
from agent_workbench.persistence.tables import SessionGraphReceiptRow, ToolOperationRow

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2] / "experiments/dual_graph_replay"


async def seed(database, unique_name, facts, *, page=None):
    box, _ = await isolated_box(database, unique_name)
    box.artifacts = ArtifactStore(database.settings)
    box.source_remap = {
        ref: "operation:" + str(uuid5(box.session_id, ref))
        for fact in facts
        for ref in fact["source_refs"]
    }
    if page is not None:
        box.source_remap.update(
            {
                "artifact:" + a["artifact_id"]: "artifact:"
                + str(uuid5(box.session_id, a["artifact_id"]))
                for a in page["artifacts"]
            }
        )
    async with database.transaction() as db:
        for fact in facts:
            ref = box.source_remap[fact["source_refs"][0]]
            db.add(
                ToolOperationRow(
                    operation_id=UUID(ref.split(":", 1)[1]),
                    run_id=box.run_id,
                    session_id=box.session_id,
                    tool_call_id=fact["fact_id"],
                    tool_name="synthetic_fixture",
                    request_digest="0" * 64,
                    effect_class="read_only",
                    status=fact.get("operation_status", "succeeded"),
                )
            )
        if page is not None:
            for evidence in page["artifacts"]:
                stored = box.artifacts.write_bytes(
                    evidence["excerpt"].encode(), media_type="text/plain"
                )
                await ArtifactRepository(db).create(
                    artifact_id=UUID(
                        box.source_remap["artifact:" + evidence["artifact_id"]].split(":", 1)[1]
                    ),
                    sha256=stored.sha256,
                    byte_size=stored.byte_size,
                    media_type=stored.media_type,
                    relative_key=stored.relative_key,
                    session_id=box.session_id,
                    source_operation_id=UUID(
                        box.source_remap["operation:" + evidence["source_operation_id"]].split(
                            ":", 1
                        )[1]
                    ),
                    sensitivity="internal",
                    availability="available",
                    captured_at=stored.captured_at,
                )
            await lock_graph(db, box.session_id)
            db.add(
                SessionGraphReceiptRow(
                    session_id=box.session_id,
                    request_id=page["page_id"],
                    digest="synthetic-frozen-page",
                    receipt={
                        "presented": True,
                        "complete": True,
                        "from_seq": 0,
                        "through_seq": page["through_seq"],
                        "revision": 0,
                        "rounds": 5,
                    },
                )
            )
    return box


def remap_sources(box, operations):
    """隔离库每个Session使用不同物理UUID；同一逻辑来源一一映射，避免互相污染。"""
    result = json.loads(json.dumps(operations))
    for op in result:
        if "source_refs" in op["data"]:
            op["data"]["source_refs"] = [
                box.source_remap.get(ref, ref) for ref in op["data"]["source_refs"]
            ]
    return result


@pytest.mark.parametrize("scenario_index", [0, 1])
async def test_nonempty_frozen_graph_passes_domain(database, unique_name, scenario_index):
    scenario = json.loads((ROOT / "fixtures.json").read_text(encoding="utf-8"))["scenarios"][
        scenario_index
    ]
    box = await seed(database, unique_name, scenario["fact_ledger"])
    operations = []
    for kind, name in [("information", "information_graph"), ("attack", "attack_graph")]:
        for category, op in [("nodes", "add_node"), ("edges", "add_edge")]:
            for value in scenario["graph_memory"][name][category]:
                operations.append(
                    {
                        "op": op,
                        "graph": kind,
                        "temp_id": value["id"],
                        "data": {k: v for k, v in value.items() if k != "id"},
                    }
                )
    parsed = TypeAdapter(list[GraphOperation]).validate_python(remap_sources(box, operations))
    async with database.transaction() as db:
        result = await update_graph(db, box, "frozen-graph", 0, parsed)
    assert result["revision"] == 1


async def test_e1_frozen_sources_and_page_confirmation(database, unique_name):
    page = json.loads((ROOT / "e1-page.json").read_text(encoding="utf-8"))
    box = await seed(database, unique_name, page["operations"], page=page)
    operations = TypeAdapter(list[GraphOperation]).validate_python(
        remap_sources(
            box,
            [
                {
                    "op": "add_node",
                    "graph": "information",
                    "temp_id": "asset",
                    "data": {"label": "合成资产", "source_refs": page["legal_source_refs"]},
                }
            ],
        )
    )
    async with database.transaction() as db:
        result = await update_graph(
            db, box, "synthetic-proposal", 0, operations, page_id=page["page_id"]
        )
    assert result["revision"] == 1 and result["reviewed_seq"] == 14


async def test_real_e1_proposals_only_when_available(database, unique_name):
    candidate_file = os.environ.get("REXAK_E1_RESULTS")
    if not candidate_file:
        pytest.skip(
            "E1 NOT_EXECUTED；批准后的提议可用REXAK_E1_RESULTS指向实验ledger，仍只在隔离库校验"
        )
    rows = [json.loads(x) for x in Path(candidate_file).read_text(encoding="utf-8").splitlines()]
    page = json.loads((ROOT / "e1-page.json").read_text(encoding="utf-8"))
    for index, row in enumerate(rows):
        proposal = row.get("result", {}).get("proposal")
        if proposal is None:
            continue
        box = await seed(database, f"{unique_name}-{index}", page["operations"], page=page)
        async with database.transaction() as db:
            # 真实提议仍保持试验性质；不连接生产数据库，不产生真实执行。
            result = await update_graph(
                db,
                box,
                row["id"],
                0,
                TypeAdapter(list[GraphOperation]).validate_python(remap_sources(box, proposal)),
                page_id=page["page_id"],
            )
        assert result["reviewed_seq"] == 14
