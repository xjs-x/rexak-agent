"""事先固定的等价投影：保留 ToolCall/参数/正文/业务状态，归一化有来源的身份和时间。"""

from __future__ import annotations

import json
import re

from experiments.history_action_trusted_shadow_v1.fixture import row_dump

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)?")


def business_view(result):
    operations = result["operations"]
    ids = {str(o.operation_id): "operation:" + o.tool_call_id for o in operations}
    for job in result["jobs"]:
        ids[str(job.job_id)] = "job:" + ids[str(job.operation_id)]
    for artifact in result["artifacts"]:
        if str(artifact.source_operation_id) in ids:
            ids[str(artifact.id)] = (
                "artifact:" + ids[str(artifact.source_operation_id)] + ":" + artifact.sha256
            )
        else:
            label = "technical-artifact:" + str(artifact.summary)
            ids[str(artifact.id)] = label
            ids[artifact.sha256] = "technical-content-hash:" + str(artifact.summary)
    technical = {}

    def normalize(value):
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        if isinstance(value, dict):
            return {
                k: normalize(v)
                for k, v in value.items()
                if k != "id" or value.get("type") not in {"ai", "tool", "human"}
            }
        if isinstance(value, (list, tuple)):
            return [normalize(item) for item in value]
        if not isinstance(value, str):
            return value
        try:
            obj = json.loads(value)
            if isinstance(obj, (dict, list)):
                return normalize(obj)
        except ValueError:
            pass
        for source, target in ids.items():
            value = value.replace(source, target)
        value = TIMESTAMP.sub("<timestamp>", value)

        def identity(match):
            raw = match.group()
            if raw not in technical:
                technical[raw] = f"technical-identity:{len(technical)}"
            return technical[raw]

        return UUID.sub(identity, value)

    return normalize(
        {
            "model_inputs": result["model"].received,
            "dispatches": result["submissions"],
            "operations": [row_dump(o) for o in operations],
            "jobs": [row_dump(j) for j in result["jobs"]],
            "http_artifacts": [
                row_dump(a) for a in result["artifacts"] if a.source_operation_id is not None
            ],
            "checkpoint": result["checkpoint"],
        }
    )


def differences(left, right, prefix=""):
    if type(left) is not type(right):
        return [prefix + ":type"]
    if isinstance(left, dict):
        result = [prefix + ":keys"] if left.keys() != right.keys() else []
        for key in left.keys() & right.keys():
            result.extend(differences(left[key], right[key], prefix + "/" + key))
        return result
    if isinstance(left, list):
        if len(left) != len(right):
            return [prefix + ":length"]
        return [
            p
            for i, (a, b) in enumerate(zip(left, right, strict=True))
            for p in differences(a, b, prefix + "/" + str(i))
        ]
    return [prefix] if left != right else []
