"""控制端与执行端的请求体协议必须保持一致（字段名、取值域与边界）。

协议真源在 `agent_workbench.contracts.http`；执行端消费同步副本
`workbench_executor.http_encoding`。两端各自维护不同字段与约束会让"结构化请求"
在不同一侧静默失效，因此这里做强制一致性检查。
"""

from __future__ import annotations

import pytest
from workbench_executor import http_encoding as executor

from agent_workbench.contracts import http as control


def _fields(model: type) -> set[str]:
    return set(model.model_fields)


def test_body_mode_vocabularies_match() -> None:
    assert tuple(control.BODY_MODES) == tuple(executor.BODY_MODES)
    for name in (
        "MAX_FORM_FIELDS",
        "MAX_MULTIPART_PARTS",
        "MAX_FIELD_CHARS",
        "MAX_PART_BYTES",
    ):
        assert getattr(control, name) == getattr(executor, name), name
    assert tuple(control.MANAGED_MULTIPART_HEADERS) == tuple(executor.MANAGED_MULTIPART_HEADERS)


def test_structured_models_declare_the_same_fields() -> None:
    assert _fields(control.FormField) == _fields(executor.FormField)
    assert _fields(control.MultipartPart) == _fields(executor.MultipartPart)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"body": "raw", "json_body": None, "form_fields": None, "multipart_parts": None},
        {"body": None, "json_body": {"a": 1}, "form_fields": None, "multipart_parts": None},
        {"body": None, "json_body": None, "form_fields": [1], "multipart_parts": None},
        {"body": None, "json_body": None, "form_fields": None, "multipart_parts": [1]},
        {"body": None, "json_body": None, "form_fields": None, "multipart_parts": None},
    ],
)
def test_classification_agrees_on_the_same_inputs(kwargs: dict) -> None:
    assert control.classify_body_mode(**kwargs) == executor.classify_body_mode(**kwargs)


def test_conflicting_body_modes_are_rejected_on_both_sides() -> None:
    kwargs = {"body": "raw", "json_body": {"a": 1}, "form_fields": None, "multipart_parts": None}
    with pytest.raises(ValueError):
        control.classify_body_mode(**kwargs)
    with pytest.raises(ValueError):
        executor.classify_body_mode(**kwargs)


def test_manual_multipart_content_type_is_refused_on_both_sides() -> None:
    headers = {"Content-Type": "multipart/form-data; boundary=manual"}
    with pytest.raises(ValueError):
        control.assert_structured_headers_ok("multipart", headers)
    with pytest.raises(executor.BodyConstructionError):
        executor.encode_request_body(
            body=None,
            json_body=None,
            form_fields=None,
            multipart_parts=[executor.MultipartPart(name="a", value="b")],
            headers=headers,
        )
