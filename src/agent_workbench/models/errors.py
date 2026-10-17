"""供应商错误的结构化识别；不解析或重放被拒绝的请求正文。"""

import json
from dataclasses import dataclass
from typing import Any

from openai import APIError


@dataclass(frozen=True)
class ProviderRejection:
    code: str
    request_id: str | None


def content_rejection(exc: BaseException) -> ProviderRejection | None:
    if not isinstance(exc, APIError):
        return None
    body: Any = exc.body
    if not isinstance(body, dict):
        raw = body if isinstance(body, str) else exc.message
        if raw.startswith("data:"):
            raw = raw[5:].strip()
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(body, dict):
        return None
    error = body.get("error", body)
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if not isinstance(code, str) or code.lower() not in {
        "data_inspection_failed",
        "datainspectionfailed",
    }:
        return None
    request_id = body.get("request_id") or body.get("id") or getattr(exc, "request_id", None)
    return ProviderRejection(code, request_id if isinstance(request_id, str) else None)
