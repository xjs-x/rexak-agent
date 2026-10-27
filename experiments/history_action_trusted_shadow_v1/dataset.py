"""事前案例标签；通过真实工具路径和捕获旁路生成工程格式，不运行检测器。"""

from __future__ import annotations

import base64
import copy
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from experiments.history_action_trusted_shadow_v1.fixture import (
    ArtifactRow,
    ToolJobRow,
    ToolOperationRow,
    approve,
    graph_run,
    make_toolbox,
    row_dump,
    tool_arguments,
)
from sqlalchemy import inspect as inspect_sql

from agent_workbench.contracts.tool import ExecutorCapabilities
from agent_workbench.domain.trusted_http_shadow import ControlGrant, Records, adapt_history, capture

# 标签先定义；正式输入生成与一次性评分均不依据检测输出修订。
CASES = [
    ("same-get", "ADAPTED", True),
    ("same-post", "ADAPTED", True),
    ("same-params", "ADAPTED", True),
    ("configuration", "ADAPTED", False),
    ("environment", "ADAPTED", False),
    ("prerequisite", "ADAPTED", False),
    ("changed-params", "ADAPTED", False),
    ("retest", "ADAPTED", False),
    ("new-target", "ADAPTED", False),
    ("navigation", "OUT_OF_SCOPE", False),
    ("identity", "UNSUPPORTED", False),
    ("privilege-context", "UNSUPPORTED", False),
    ("cookie-unobserved", "UNSUPPORTED", False),
    ("dynamic-csrf", "UNSUPPORTED", False),
    ("unknown-encoding", "UNSUPPORTED", False),
    ("executor-unverified", "UNSUPPORTED", False),
    ("operation-unknown", "HISTORY_GAP", False),
    ("job-unknown", "HISTORY_GAP", False),
    ("no-artifact", "HISTORY_GAP", False),
    ("model-claim-only", "HISTORY_GAP", False),
    ("artifact-owner", "HISTORY_GAP", False),
    ("time-unknown", "HISTORY_GAP", False),
    ("job-operation-mix", "HISTORY_GAP", False),
    ("same-batch", "HISTORY_GAP", False),
    ("cross-session", "HISTORY_GAP", False),
    ("bad-hash", "HISTORY_GAP", False),
]


class CaptureOnly:
    """替换观察消费者，保留生产 HTTP 分发捕获位置；不导入/执行检测器。"""

    def __init__(self, box, grants, case):
        self.box, self.grants, self.case = box, grants, case
        self.captures, self.frame = {}, None

    def enqueue(self, arguments, packet, tool_call_id, context):
        grant = next((g for g in self.grants if g["tool_call_id"] == tool_call_id), None)
        now = datetime.now(UTC)
        current, _ = capture(
            session_id=str(self.box.session_id),
            run_id=str(self.box.run_id),
            tool_call_id=tool_call_id,
            arguments=arguments,
            operation=packet,
            actual_identity_id=self.box.identity_id,
            executor=self.box.executor,
            grant=ControlGrant.model_validate(grant) if grant else None,
            mapping_signature=grant["mapping_signature"] if grant else "",
            now=now,
        )
        if tool_call_id == "seed":
            if current:
                self.captures[packet["operation_id"]] = current
            return
        records = Records(
            copy.deepcopy(self.box.db.rows.get(ToolOperationRow, [])),
            copy.deepcopy(self.box.db.rows.get(ToolJobRow, [])),
            copy.deepcopy(self.box.db.rows.get(ArtifactRow, [])),
        )
        prior = next(op for op in records.operations if op.tool_call_id == "seed")
        if self.case == "operation-unknown":
            prior.status = "unknown"
        if self.case == "job-unknown":
            for job in records.jobs:
                job.state = "unknown"
        if self.case == "time-unknown":
            for job in records.jobs:
                job.executor_finished_at = None
        if self.case == "job-operation-mix":
            for job in records.jobs:
                job.operation_id = job.job_id
        if self.case == "no-artifact":
            records.artifacts.clear()
        if self.case == "artifact-owner":
            for artifact in records.artifacts:
                artifact.source_operation_id = UUID(packet["operation_id"])
        if self.case == "cross-session":
            prior.session_id = UUID(int=44)
        if self.case == "bad-hash":
            for artifact in records.artifacts:
                artifact.sha256 = "0" * 64
        if self.case == "model-claim-only":
            self.captures.clear()
            prior.result_snapshot = {"model_claim": "trusted verified security test"}
        if self.case == "same-batch":
            context = copy.deepcopy(context)
            context["batch_ids"].append("seed")
        self.frame = {
            "source_class": "PRODUCTION_CONTRACT_FIXTURE",
            "bounded_fault_injection": self.case
            in {
                "operation-unknown",
                "job-unknown",
                "time-unknown",
                "job-operation-mix",
                "no-artifact",
                "artifact-owner",
                "cross-session",
                "bad-hash",
                "model-claim-only",
                "same-batch",
            },
            "case": self.case,
            "arguments": arguments,
            "operation": packet,
            "grant": grant,
            "actual_identity_id": str(self.box.identity_id) if self.box.identity_id else None,
            "caps": self.box.executor._preflight.model_dump(mode="json")
            if self.box.executor._preflight
            else None,
            "mapping_signature": grant["mapping_signature"] if grant else "",
            "cutoff": now.isoformat(),
            "context": context,
            "captures": self.captures,
            "operations": [row_dump(r) for r in records.operations],
            "jobs": [row_dump(r) for r in records.jobs],
            "artifacts": [row_dump(r) for r in records.artifacts],
            "bytes": {
                str(art.id): base64.b64encode(
                    self.box.artifacts.read_bytes(art.relative_key)
                ).decode()
                for art in records.artifacts
            },
        }


async def generate(root: Path, case: str):
    seed, current = tool_arguments(), tool_arguments()
    changes = {}
    if case == "same-post":
        seed = current = tool_arguments(method="POST", body="fixed fixture body")
    if case == "same-params":
        seed = current = tool_arguments(params_json='{"record":"7"}')
    if case == "changed-params":
        current = tool_arguments(params_json='{"record":"8"}')
    if case == "new-target":
        current = tool_arguments(url="https://contract.invalid/other")
    if case == "identity":
        current = tool_arguments(identity="another-identity")
    if case == "cookie-unobserved":
        current = tool_arguments(headers_json="{}")
    if case == "dynamic-csrf":
        current = tool_arguments(
            csrf_source_url="https://contract.invalid/form", csrf_input_name="csrf"
        )
    if case == "unknown-encoding":
        current = tool_arguments(headers_json='{"Cookie":"","Content-Encoding":"unregistered"}')
    if case in {"configuration", "environment"}:
        changes[case] = "operator-changed:v2"
    if case == "prerequisite":
        changes["prerequisites"] = {"operator_fixture_ready": False}
    if case == "retest":
        changes["retest_required"] = True
    if case == "navigation":
        changes["purpose"] = "navigation"
    grants = [approve("seed", seed), approve("current", current, **changes)]
    box = make_toolbox(root, True, grants)
    if case == "privilege-context":
        box.identity_id = UUID(int=77)
    if case == "executor-unverified":
        box.executor._preflight = None
    sink = CaptureOnly(box, grants, case)
    box.history_action_shadow = sink
    try:
        await graph_run(
            box, [[("http_request", seed, "seed")], [("http_request", current, "current")]]
        )
        if sink.frame is None:
            raise ValueError("production_fixture_did_not_reach_dispatch")
        return sink.frame
    finally:
        await box.executor.close()


def hydrate(cls, rows):
    columns = {c.key: c for c in inspect_sql(cls).columns}
    result = []
    for row in rows:
        values = dict(row)
        for key, value in values.items():
            if value is None:
                continue
            try:
                kind = columns[key].type.python_type
            except NotImplementedError:
                continue
            if kind is UUID:
                values[key] = UUID(value)
            elif kind is datetime:
                values[key] = datetime.fromisoformat(value)
        result.append(cls(**values))
    return result


def predict(frame):
    from agent_workbench.domain.history_action_reflection import observe_input

    started = time.perf_counter_ns()
    clock = datetime.fromisoformat(frame["cutoff"])
    current, reasons = capture(
        session_id=frame["grant"]["session_id"],
        run_id=frame["grant"]["run_id"],
        tool_call_id=frame["grant"]["tool_call_id"],
        arguments=frame["arguments"],
        operation=frame["operation"],
        actual_identity_id=frame["actual_identity_id"],
        executor=SimpleNamespace(
            _preflight=ExecutorCapabilities.model_validate(frame["caps"]) if frame["caps"] else None
        ),
        grant=ControlGrant.model_validate(frame["grant"]),
        mapping_signature=frame["mapping_signature"],
        now=clock,
    )
    if current is None:
        return {
            "case": frame["case"],
            "adapter_complete": False,
            "reasons": reasons,
            "observation": None,
            "capture_ms": (time.perf_counter_ns() - started) / 1e6,
        }
    records = Records(
        hydrate(ToolOperationRow, frame["operations"]),
        hydrate(ToolJobRow, frame["jobs"]),
        hydrate(ArtifactRow, frame["artifacts"]),
    )
    captured = time.perf_counter_ns()
    snapshot, diagnostics = adapt_history(
        current,
        records,
        frame["captures"],
        {key: base64.b64decode(value) for key, value in frame["bytes"].items()},
        frame["context"],
    )
    adapted = time.perf_counter_ns()
    observation = observe_input(snapshot)
    return {
        "case": frame["case"],
        "adapter_complete": True,
        "reasons": diagnostics,
        "observation": observation.model_dump(mode="json"),
        "capture_and_hydration_ms": (captured - started) / 1e6,
        "history_association_ms": (adapted - captured) / 1e6,
        "trigger_ms": (time.perf_counter_ns() - adapted) / 1e6,
        "historical_records": len(records.operations) - 1,
        "related_candidates": len(observation.related_history),
    }
