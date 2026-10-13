"""HTTP 请求体形态的**协议真源**（§6.1）。

四种请求体互斥：raw(`body`) / json(`json_body`) / form / multipart。
`none` 表示没有请求体。

契约约束：
- 表单使用**数组**，支持重复字段名与原顺序（字典无法表达重复键）；
- 第一批只实现有界 UTF-8 文本字段与文本文件 part：足以消除手写 boundary/换行的
  机械负担，**不让模型生成大段 base64**；二进制与大文件随后通过既有工件/工作区
  文件引用接入；
- raw body 原样保留，不自动"修正"用户有意构造的非标准协议内容；
- 正常模式**拒绝**与结构化 multipart 相冲突的手工 boundary。

执行端消费本协议的同步副本（`workbench_executor.http_encoding`），
由 `tests/unit/test_http_contract.py` 做字段与取值一致性检查 —— 不允许两端
各自维护不同字段与约束。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

BodyMode = Literal["none", "raw", "json", "form", "multipart"]

#: 请求体形态的完整取值域（执行端能力声明必须覆盖它）。
BODY_MODES: tuple[str, ...] = ("raw", "json", "form", "multipart")

#: 需要执行端结构化编码的形态。
STRUCTURED_BODY_MODES: tuple[str, ...] = ("form", "multipart")

#: 本批实现的边界（资源初值，不是任务限制）。
MAX_FORM_FIELDS = 64
MAX_MULTIPART_PARTS = 64
MAX_FIELD_CHARS = 65536
MAX_PART_BYTES = 262144

#: 结构化 multipart 使用执行端编码器产生 boundary；手写这两个头会被明确拒绝。
MANAGED_MULTIPART_HEADERS = ("content-type",)


class FormField(BaseModel):
    """一个 `application/x-www-form-urlencoded` 字段。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    name: str = Field(min_length=1, max_length=256)
    value: str = Field(default="", max_length=MAX_FIELD_CHARS)


class MultipartPart(BaseModel):
    """一个 `multipart/form-data` part（有界 UTF-8 文本）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    name: str = Field(min_length=1, max_length=256)
    value: str = Field(default="", max_length=MAX_PART_BYTES)
    filename: str | None = Field(default=None, max_length=256)
    content_type: str | None = Field(default=None, max_length=128)

    @field_validator("filename", "content_type", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value


def classify_body_mode(
    *,
    body: str | None,
    json_body: Any,
    form_fields: list[FormField] | None,
    multipart_parts: list[MultipartPart] | None,
) -> BodyMode:
    """判定本次请求的体形态；多个非空形态直接报错（互斥）。

    错误信息说明**是哪两种形态冲突**，便于模型自我修正，而不是笼统"参数非法"。
    """
    present: list[str] = []
    if body is not None:
        present.append("body")
    if json_body is not None:
        present.append("json_body")
    if form_fields:
        present.append("form_fields_json")
    if multipart_parts:
        present.append("multipart_parts_json")
    if len(present) > 1:
        raise ValueError("请求体形态互斥，不能同时提供：" + "、".join(present))
    if not present:
        return "none"
    if present[0] == "body":
        return "raw"
    if present[0] == "json_body":
        return "json"
    if present[0] == "form_fields_json":
        return "form"
    return "multipart"


def assert_structured_headers_ok(mode: BodyMode, headers: dict[str, str]) -> None:
    """正常模式下拒绝与结构化 multipart 相冲突的手工 boundary。"""
    if mode != "multipart":
        return
    lowered = {key.lower() for key in headers}
    conflicts = [name for name in MANAGED_MULTIPART_HEADERS if name in lowered]
    if conflicts:
        raise ValueError(
            "multipart_parts_json 由执行端统一编码 boundary 与 Content-Type，"
            f"不能再手工提供 {conflicts}；请移除这些请求头。"
        )


__all__ = [
    "BODY_MODES",
    "MANAGED_MULTIPART_HEADERS",
    "MAX_FIELD_CHARS",
    "MAX_FORM_FIELDS",
    "MAX_MULTIPART_PARTS",
    "MAX_PART_BYTES",
    "STRUCTURED_BODY_MODES",
    "BodyMode",
    "FormField",
    "MultipartPart",
    "assert_structured_headers_ok",
    "classify_body_mode",
]
