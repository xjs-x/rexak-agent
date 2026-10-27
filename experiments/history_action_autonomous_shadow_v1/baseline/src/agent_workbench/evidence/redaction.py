"""结构化只读展示的统一脱敏。"""

from __future__ import annotations

import json
import re
from typing import Any

from agent_workbench.evidence.artifacts import redact_text

_SECRET_FIELD = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie|csrf|xsrf|private[_-]?key)"
)


def redact_api_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if _SECRET_FIELD.search(str(key)) else redact_api_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        if len(value) == 2 and isinstance(value[0], str) and _SECRET_FIELD.search(value[0]):
            return [value[0], "<redacted>"]
        return [
            "<redacted>"
            if index
            and isinstance(value[index - 1], str)
            and value[index - 1].startswith("-")
            and _SECRET_FIELD.search(value[index - 1])
            else redact_api_value(item)
            for index, item in enumerate(value)
        ]
    if isinstance(value, str):
        if value.strip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass
            else:
                if isinstance(parsed, dict | list):
                    return json.dumps(redact_api_value(parsed), ensure_ascii=False)
        return redact_text(value)
    return value
