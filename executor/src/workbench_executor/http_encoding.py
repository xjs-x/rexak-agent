"""请求体编码（执行端）。

这是 `agent_workbench.contracts.http` 的**同步副本**：控制端定义协议真源，
执行端消费同步后的字段与约束。两端的字段名与取值域由
`tests/unit/test_http_contract.py` 做一致性检查，**不允许各自维护不同版本**。

编码职责：
- form：统一按规定编码；
- multipart：由本模块产生 CRLF、boundary、`Content-Disposition` 与最终字节长度，
  **不让调用方手拼字符串**；
- raw 原样保留，不"修正"用户有意构造的非标准协议内容；
- json 统一 UTF-8。

正常模式拒绝相冲突的手工 multipart `Content-Type`，返回明确构造错误。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field

BodyMode = Literal["none", "raw", "json", "form", "multipart"]

#: 与 contracts/http.py 保持一致。
BODY_MODES: tuple[str, ...] = ("raw", "json", "form", "multipart")
MAX_FORM_FIELDS = 64
MAX_MULTIPART_PARTS = 64
MAX_FIELD_CHARS = 65536
MAX_PART_BYTES = 262144
MANAGED_MULTIPART_HEADERS = ("content-type",)

FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
JSON_CONTENT_TYPE = "application/json"
MULTIPART_CONTENT_TYPE = "multipart/form-data"


class BodyConstructionError(ValueError):
    """本地构造成立与否的明确信号：构造失败 = 请求**没有**发出。"""


class FormField(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    name: str = Field(min_length=1, max_length=256)
    value: str = Field(default="", max_length=MAX_FIELD_CHARS)


class MultipartPart(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    name: str = Field(min_length=1, max_length=256)
    value: str = Field(default="", max_length=MAX_PART_BYTES)
    filename: str | None = Field(default=None, max_length=256)
    content_type: str | None = Field(default=None, max_length=128)


@dataclass(frozen=True, slots=True)
class EncodedBody:
    """编码结果。真实生成的字节 hash **另记**，不混进逻辑请求去重键。"""

    mode: BodyMode
    body: bytes
    content_type: str | None

    @property
    def byte_count(self) -> int:
        return len(self.body)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


def classify_body_mode(
    *,
    body: str | None,
    json_body: Any,
    form_fields: list[Any] | None,
    multipart_parts: list[Any] | None,
) -> BodyMode:
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
        raise BodyConstructionError("请求体形态互斥，不能同时提供：" + "、".join(present))
    if not present:
        return "none"
    return {
        "body": "raw",
        "json_body": "json",
        "form_fields_json": "form",
        "multipart_parts_json": "multipart",
    }[present[0]]  # type: ignore[return-value]


def _sanitize_token(value: str) -> str:
    """part 名称/文件名不许包含 CR/LF 或引号，避免头部注入与畸形编码。"""
    cleaned = value.replace("\r", "").replace("\n", "").replace('"', "'")
    if not cleaned:
        raise BodyConstructionError("multipart part 的名称/文件名不能为空")
    return cleaned


def encode_request_body(
    *,
    body: str | None,
    json_body: Any,
    form_fields: list[FormField] | None,
    multipart_parts: list[MultipartPart] | None,
    headers: dict[str, str] | None = None,
) -> EncodedBody:
    """把逻辑参数编码为最终字节；构造失败抛 `BodyConstructionError`。"""
    mode = classify_body_mode(
        body=body, json_body=json_body, form_fields=form_fields, multipart_parts=multipart_parts
    )
    lowered = {key.lower() for key in (headers or {})}

    if mode == "none":
        return EncodedBody("none", b"", None)
    if mode == "raw":
        return EncodedBody("raw", (body or "").encode("utf-8"), None)
    if mode == "json":
        return EncodedBody(
            "json",
            json.dumps(json_body, ensure_ascii=False).encode("utf-8"),
            JSON_CONTENT_TYPE,
        )
    if mode == "form":
        fields = form_fields or []
        if len(fields) > MAX_FORM_FIELDS:
            raise BodyConstructionError(f"表单字段最多 {MAX_FORM_FIELDS} 个")
        pairs = [(item.name, item.value) for item in fields]
        return EncodedBody("form", urlencode(pairs).encode("utf-8"), FORM_CONTENT_TYPE)

    parts = multipart_parts or []
    if len(parts) > MAX_MULTIPART_PARTS:
        raise BodyConstructionError(f"multipart part 最多 {MAX_MULTIPART_PARTS} 个")
    if any(name in lowered for name in MANAGED_MULTIPART_HEADERS):
        raise BodyConstructionError(
            "multipart 由执行端统一编码 boundary 与 Content-Type，不能再手工提供这些请求头"
        )
    boundary = f"----WorkbenchBoundary{secrets.token_hex(16)}"
    chunks: list[bytes] = []
    for part in parts:
        name = _sanitize_token(part.name)
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if part.filename:
            disposition += f'; filename="{_sanitize_token(part.filename)}"'
        block = bytearray()
        block.extend(b"--" + boundary.encode() + b"\r\n")
        block.extend(disposition.encode("utf-8") + b"\r\n")
        content_type = part.content_type or ("text/plain" if part.filename else None)
        if content_type:
            block.extend(f"Content-Type: {content_type}".encode() + b"\r\n")
        block.extend(b"\r\n")
        block.extend(part.value.encode("utf-8"))
        block.extend(b"\r\n")
        chunks.append(bytes(block))
    chunks.append(f"--{boundary}--\r\n".encode())
    return EncodedBody(
        "multipart",
        b"".join(chunks),
        f'{MULTIPART_CONTENT_TYPE}; boundary="{boundary}"',
    )


def encode_form_fields(pairs: list[tuple[str, str]]) -> EncodedBody:
    """CSRF 注入后重新编码 urlencoded 表单（保持字段原顺序与重复名）。"""
    return EncodedBody("form", urlencode(pairs).encode("utf-8"), FORM_CONTENT_TYPE)


__all__ = [
    "BODY_MODES",
    "FORM_CONTENT_TYPE",
    "JSON_CONTENT_TYPE",
    "MANAGED_MULTIPART_HEADERS",
    "MAX_FIELD_CHARS",
    "MAX_FORM_FIELDS",
    "MAX_MULTIPART_PARTS",
    "MAX_PART_BYTES",
    "MULTIPART_CONTENT_TYPE",
    "BodyConstructionError",
    "BodyMode",
    "EncodedBody",
    "FormField",
    "MultipartPart",
    "classify_body_mode",
    "encode_form_fields",
    "encode_request_body",
]
