"""图协议诊断有界且不放行原输入；无模型、网络或数据库。"""

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from agent_workbench.contracts.graph_errors import GraphValidationError
from agent_workbench.contracts.session_graph import AttackState, GraphOperation
from agent_workbench.contracts.tool import ToolError
from agent_workbench.domain.session_graph import apply_operations, empty_graph
from agent_workbench.tools.graph_errors import format_graph_schema_error, render_graph_failure

ADAPTER = TypeAdapter(list[GraphOperation])
HISTORY = (
    Path(__file__).resolve().parents[2]
    / "experiments/graph_update_engineering_fix_20261028/history/snapshot-20261028T070507678035Z"
)


def test_old_entity_alias_is_rejected_and_points_to_exact_field():
    ops = ADAPTER.validate_python(
        [
            {"op": "add_node", "graph": "information", "temp_id": "e", "data": {"label": "入口"}},
        ]
    )
    before, ids = apply_operations(empty_graph(), ops, "first")
    invalid = ADAPTER.validate_python(
        [
            {
                "op": "add_node",
                "graph": "attack",
                "temp_id": "s",
                "data": {"summary": "状态", "entity_refs": ["e"]},
            },
        ]
    )
    with pytest.raises(GraphValidationError) as caught:
        apply_operations(before, invalid, "second")
    error = json.loads(render_graph_failure(caught.value))
    assert error["status"] == "failed" and error["error"]["code"] == "validation_failed"
    assert error["error"]["details"]["path"] == "operations[0].data.entity_refs[0]"
    assert error["error"]["details"]["graph_code"] == "INVALID_ENTITY_REF"
    assert "id_mapping" in error["error"]["recovery_hint"]
    assert not before["attack_graph"]["nodes"]
    state = invalid[0].data
    assert isinstance(state, AttackState)
    state.entity_refs = [ids["e"]]
    after, _ = apply_operations(before, invalid, "second")
    assert len(after["attack_graph"]["nodes"]) == 1


@pytest.mark.parametrize("case_id", ["R1-F01", "R1-F02", "R3-F02", "R4-F02"])
def test_historical_schema_errors_still_rejected_without_kwargs_echo(case_id):
    case = json.loads((HISTORY / f"{case_id}.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError) as caught:
        ADAPTER.validate_python(case["failed_call"]["arguments"]["operations"])
    result = json.loads(format_graph_schema_error(caught.value))
    assert result["error"]["details"]["graph_code"] == "GRAPH_SCHEMA_INVALID"
    assert result["execution_certainty"] == "not_applied"
    assert 1 <= len(result["error"]["details"]["issues"]) <= 4
    assert len(json.dumps(result, ensure_ascii=False)) < 4000
    ToolError.model_validate(result["error"])


def test_schema_diagnostics_do_not_echo_private_data():
    invalid = [{"graph": "attack", "data": {"summary": "PRIVATE_INPUT_SENTINEL"}}]
    with pytest.raises(ValidationError) as caught:
        ADAPTER.validate_python(invalid)
    rendered = format_graph_schema_error(caught.value)
    assert "PRIVATE_INPUT_SENTINEL" not in rendered
    assert json.loads(rendered)["error"]["details"]["path"] == "[0].op"


def test_graph_and_data_mismatch_still_rejected():
    ops = ADAPTER.validate_python(
        [
            {
                "op": "add_node",
                "graph": "attack",
                "temp_id": "s",
                "data": {"label": "wrong schema"},
            },
        ]
    )
    with pytest.raises(GraphValidationError) as caught:
        apply_operations(empty_graph(), ops, "mismatch")
    assert caught.value.reason_code == "GRAPH_DATA_TYPE_MISMATCH"
    assert caught.value.expected == "AttackState" and caught.value.actual == "Entity"


def test_invalid_op_value_is_not_echoed_by_pydantic_message():
    invalid = [{"op": "PRIVATE_OP_SENTINEL", "graph": "attack", "data": {"summary": "状态"}}]
    with pytest.raises(ValidationError) as caught:
        ADAPTER.validate_python(invalid)
    rendered = format_graph_schema_error(caught.value)
    assert "PRIVATE_OP_SENTINEL" not in rendered
    assert json.loads(rendered)["error"]["details"]["path"] == "[0].op"
