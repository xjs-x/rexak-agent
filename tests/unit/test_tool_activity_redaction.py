import json

from langchain_core.messages import ToolMessage

from agent_workbench.observability.tool_activity import result_view, visible_text


def test_nested_json_and_header_pairs_are_redacted_before_display():
    value = {
        "headers_json": '{"Authorization":"Bearer private-header"}',
        "meta": {"headers": [["Set-Cookie", "private-cookie"]]},
        "argv": ["client", "--password", "private-password", "--url", "https://example.test/"],
    }
    view = visible_text(value)
    assert (
        "private-header" not in view
        and "private-cookie" not in view
        and "private-password" not in view
    )
    assert "https://example.test/" in view
    assert "redacted" in view


def test_result_failure_is_not_relabelled_as_success_and_output_is_bounded():
    status, output = result_view(
        ToolMessage(
            content=json.dumps({"status": "failed", "error": {"message": "certificate failure"}}),
            tool_call_id="one",
        )
    )
    assert status == "failed" and "certificate failure" in output
    assert len(visible_text("x" * 100_000)) < 8300
