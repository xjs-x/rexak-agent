"""补充来源审计，不重评分：真实隔离数据库、实际模型入参、原始工件字节闭环。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import pytest
from experiments.history_action_autonomous_shadow_v1.evaluate import ROOT, write

from agent_workbench.evidence.artifacts import ArtifactStore
from tests.integration.test_autonomous_shadow import run_case

pytestmark = pytest.mark.integration


async def test_saved_raw_bytes_really_enter_later_model_input(
    database, unique_name, tmp_path, monkeypatch
):
    result = await run_case(database, unique_name, tmp_path / "evidence", monkeypatch)
    source = next(o for o in result["operations"] if o.tool_call_id == "first")
    candidate = next(o for o in result["operations"] if o.tool_call_id == "repeat")
    captures = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in (result["shadow_root"] / "captures").glob("*.json")
    ]
    current = next(c for c in captures if c["tool_call_id"] == "repeat")
    job = next(j for j in result["jobs"] if j.operation_id == source.operation_id)
    assert job.job_id == source.job_id and job.executor_finished_at < datetime.fromisoformat(
        current["captured_at"]
    )
    event = result["events"][0]
    store = ArtifactStore(result["settings"])
    refs = []
    for ref in event["evidence_refs"]:
        aid = ref.removeprefix("artifact:")
        artifact = next(a for a in result["artifacts"] if str(a.id) == aid)
        assert artifact.source_operation_id == source.operation_id
        raw = store.read_bytes(artifact.relative_key)
        assert len(raw) == artifact.byte_size and hashlib.sha256(raw).hexdigest() == artifact.sha256
        found = []
        for index, messages in enumerate(result["model"].received):
            for message in messages:
                if message.type == "tool" and message.tool_call_id == "first":
                    data = json.loads(message.content)
                    if data["data"].get("body_preview") == raw.decode("utf-8"):
                        found.append(index)
        assert 1 in found, "原文必须已进入生成第二次候选的实际模型输入"
        refs.append(
            {
                "artifact_id": aid,
                "sha256": artifact.sha256,
                "raw_body_utf8": raw.decode("utf-8"),
                "visible_model_calls": found,
            }
        )
    evidence = {
        "source": "REAL_POSTGRES_PRODUCTION_RUNNER_OFFLINE_EXECUTOR_NOT_REAL_TARGET",
        "source_operation": str(source.operation_id),
        "candidate_operation": str(candidate.operation_id),
        "event": event,
        "verified_evidence": refs,
        "model_inputs": [
            [m.model_dump(mode="json") for m in messages] for messages in result["model"].received
        ],
        "database_name": database.settings.database_url.rsplit("/", 1)[-1],
        "evaluation_rerun": False,
    }
    # 单次追加审计证据；回归重跑写在 pytest 自己目录，不覆盖既有交付审计。
    output = ROOT / "verification/raw-evidence-audit.json"
    if not output.exists():
        write(output, evidence)
    else:
        write(tmp_path / "raw-evidence-audit.json", evidence)
