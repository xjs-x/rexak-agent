"""工具参数容错解析。

背景：不同模型对"列表参数"的表达不一致 —— 有的直接给 JSON 数组，
有的给 JSON 字符串，有的给单个字符串。**不接受**在业务层猜测含义，
但可以**明确地**接受这几种等价形态，并把无法识别的情况变成**确定错误**。

规则：
- `None` / 空串 -> `[]`；
- 真正的列表 -> 逐项 `str()`；
- 字符串 -> 尝试 `json.loads`；成功且是列表则使用；失败则视为单个元素；
- 其他（dict、数字等）-> 抛 `ValueError`，不做猜测。
"""

from __future__ import annotations

import json
from typing import Any


def coerce_str_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, bool):
        raise ValueError(f"{field} 不能是布尔值")
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # 单值形态：例如直接给了一个 UUID 或 URL
            return [text]
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item).strip()]
        if isinstance(parsed, str):
            return [parsed] if parsed.strip() else []
        raise ValueError(f"{field} 解析后不是数组，而是 {type(parsed).__name__}")
    raise ValueError(f"{field} 必须是数组或其 JSON 字符串，收到 {type(value).__name__}")


def coerce_json_object(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} 不是合法 JSON：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{field} 必须是 JSON 对象，解析结果是 {type(parsed).__name__}")
        return parsed
    raise ValueError(f"{field} 必须是对象或其 JSON 字符串，收到 {type(value).__name__}")


def coerce_str_dict(value: Any, field: str) -> dict[str, str]:
    raw = coerce_json_object(value, field)
    return {str(key): str(item) for key, item in raw.items()}
