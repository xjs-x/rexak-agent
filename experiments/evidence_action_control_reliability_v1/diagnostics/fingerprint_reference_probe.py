"""Offline diagnostic: measure marshal fingerprints while retaining code constants.

This script does not replace loaded_code_version, call a model, open a database,
or execute any agent runtime. Its outputs stay beside this script.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import marshal
import sys
import types
from pathlib import Path

from agent_workbench.agent import factory
from agent_workbench.agent.decision_memory import build_blackboard
from agent_workbench.contracts.plan import PlanStepPatch
from agent_workbench.domain.decision_memory import read_snapshot, select_memory
from agent_workbench.domain.graph_sync import graph_context
from agent_workbench.domain.session_graph import apply_operations, update_graph
from agent_workbench.tools.planning import apply_plan_update, normalize_steps

FUNCTIONS = (
    factory.build_agent_middleware,
    factory.open_agent_runtime,
    apply_plan_update,
    normalize_steps,
    apply_operations,
    update_graph,
    graph_context,
    factory.ToolDispatchMiddleware._dispatch_tool_call,
    factory.ContextBuilderMiddleware.awrap_model_call,
    factory.WorkingContextMiddleware.before_model,
    build_blackboard,
    read_snapshot,
    select_memory,
)
OLD_EXPECTED = "loaded-minimal-loop:1f6d8c41a0e287625a8be3f1f2b1bb0ca9651641"


def resolve(function, path):
    value = inspect.unwrap(function).__code__
    for index in path:
        value = value.co_consts[index] if isinstance(value, types.CodeType) else value[index]
    return value


def paths(value, prefix=()):
    """Return index paths only, keeping no reference to original constants."""
    result = []
    sequence = value.co_consts if isinstance(value, types.CodeType) else value
    if not isinstance(sequence, tuple):
        return result
    for index in range(len(sequence)):
        current_path = (*prefix, index)
        result.append(current_path)
        if isinstance(sequence[index], (tuple, types.CodeType)):
            result.extend(paths(sequence[index], current_path))
    return result


def snapshot():
    result = {}
    for function in FUNCTIONS:
        code = inspect.unwrap(function).__code__
        result[function.__qualname__] = {
            "code_identity": id(code),
            "co_code_sha256": hashlib.sha256(code.co_code).hexdigest(),
            "marshal_sha256": hashlib.sha256(marshal.dumps(code)).hexdigest(),
        }
    result["schema_sha256"] = hashlib.sha256(
        json.dumps(PlanStepPatch.model_json_schema(), sort_keys=True).encode("utf-8")
    ).hexdigest()
    result["system_prompt_sha256"] = hashlib.sha256(
        factory.SYSTEM_PROMPT.encode("utf-8")
    ).hexdigest()
    return result


def main():
    baseline = factory.loaded_code_version()
    baseline_snapshot = snapshot()
    changes = []
    held = []
    for function in FUNCTIONS:
        for path in paths(inspect.unwrap(function).__code__):
            before = factory.loaded_code_version()
            held.append(resolve(function, path))
            after = factory.loaded_code_version()
            if before != after:
                active = snapshot()
                changes.append({
                    "function": function.__qualname__,
                    "constant_path": list(path),
                    "constant_type": type(held[-1]).__name__,
                    "before": before,
                    "after": after,
                    "matches_old_expected": after == OLD_EXPECTED,
                    "changed_components": {
                        key: {"before": baseline_snapshot[key], "held": active[key]}
                        for key in active if baseline_snapshot[key] != active[key]
                    },
                })
            held.clear()
            restored = factory.loaded_code_version()
            if restored != before:
                raise RuntimeError(f"Constant reference did not restore: {function.__qualname__}")
    result = {
        "python": sys.version,
        "baseline": baseline,
        "final": factory.loaded_code_version(),
        "baseline_components": baseline_snapshot,
        "retained_reference_changes": changes,
        "old_expected_matches": [item for item in changes if item["matches_old_expected"]],
        "scope": "offline_only_no_database_no_model_no_executor",
    }
    output = Path(__file__).with_name("fingerprint-reference-probe.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "baseline": baseline,
        "final": result["final"],
        "changed_reference_cases": len(changes),
        "old_expected_matches": len(result["old_expected_matches"]),
        "output": str(output),
    }, ensure_ascii=False))


def targeted_probe():
    """Reproduce the exact recorded pair with one extra reference, then release it."""
    code = graph_context.__code__
    held = []
    before = factory.loaded_code_version()
    before_bytes = marshal.dumps(code)
    before_refs = sys.getrefcount(code.co_consts[24])
    held.append(code.co_consts[24])
    during = factory.loaded_code_version()
    during_bytes = marshal.dumps(code)
    during_refs = sys.getrefcount(code.co_consts[24])
    held.clear()
    restored = factory.loaded_code_version()
    changed_offsets = [
        {"offset": index, "before_hex": f"{left:02x}", "held_hex": f"{right:02x}"}
        for index, (left, right) in enumerate(zip(before_bytes, during_bytes, strict=True))
        if left != right
    ]
    result = {
        "python": sys.version,
        "before": before,
        "while_one_extra_reference_is_held": during,
        "after_release": restored,
        "matches_both_old_hashes": before.endswith("ef173acba02c50ca0bdeb7ca8c6e6070c917253d")
        and during == OLD_EXPECTED,
        "function": "graph_context",
        "function_source": "src/agent_workbench/domain/graph_sync.py:23",
        "constant_index": 24,
        "constant_value": code.co_consts[24],
        "reference_count_before_including_probe_argument": before_refs,
        "reference_count_held_including_probe_argument": during_refs,
        "code_object_same_identity": code is graph_context.__code__,
        "marshal_loaded_code_objects_equal": marshal.loads(before_bytes) == marshal.loads(during_bytes),
        "co_code_sha256": hashlib.sha256(code.co_code).hexdigest(),
        "marshal_lengths": [len(before_bytes), len(during_bytes)],
        "marshal_sha256": [hashlib.sha256(before_bytes).hexdigest(), hashlib.sha256(during_bytes).hexdigest()],
        "changed_byte_offsets": changed_offsets,
        "scope": "offline_only_no_database_no_model_no_executor",
    }
    output = Path(__file__).with_name("fingerprint-targeted-reproduction.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def sqlalchemy_probe():
    """Construct and compile the source LIKE expression without opening a database."""
    import gc

    from sqlalchemy import select

    from agent_workbench.persistence.tables import SessionGraphReceiptRow

    code = graph_context.__code__
    before = factory.loaded_code_version()
    before_components = snapshot()
    statement = (
        select(SessionGraphReceiptRow)
        .where(SessionGraphReceiptRow.request_id.like(code.co_consts[24]))
        .order_by(SessionGraphReceiptRow.created_at.desc())
    )
    compiled = statement.compile()
    same_value = any(value is code.co_consts[24] for value in compiled.params.values())
    retained = factory.loaded_code_version()
    retained_components = snapshot()
    del compiled, statement
    gc.collect()
    released = factory.loaded_code_version()
    result = {
        "python": sys.version,
        "before": before,
        "while_sqlalchemy_statement_is_retained": retained,
        "after_release_and_gc": released,
        "compiled_parameter_is_original_constant": same_value,
        "matches_both_old_hashes": before.endswith("ef173acba02c50ca0bdeb7ca8c6e6070c917253d")
        and retained == OLD_EXPECTED,
        "changed_components": {
            key: {"before": before_components[key], "retained": retained_components[key]}
            for key in retained_components if before_components[key] != retained_components[key]
        },
        "scope": "sqlalchemy_construct_and_compile_only_no_connection_no_database_no_model_no_executor",
    }
    output = Path(__file__).with_name("fingerprint-sqlalchemy-reproduction.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if "--targeted" in sys.argv:
        targeted_probe()
    elif "--sqlalchemy" in sys.argv:
        sqlalchemy_probe()
    else:
        main()
