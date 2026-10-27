"""Flag 候选提取与基本校验。

**不**从原始工具日志里批量抓取疑似字符串：只接受 Agent **最终回答**中的候选，
并要求满足格式、长度与占位符形态校验。平台判定是唯一权威，本模块只负责
"减少明显噪声 + 去重 + 可审计"。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

#: ``flag{...}``，大小写不敏感；内容不允许花括号、换行与空白。
FLAG_PATTERN = re.compile(r"(?<![A-Za-z0-9_])flag\{([^{}\r\n]{1,200})\}", re.IGNORECASE)

#: 整行出现这些词时视为"格式说明/示例"，跳过该行的候选。
_PLACEHOLDER_CONTEXT = (
    "示例",
    "例如",
    "比如",
    "格式",
    "形如",
    "placeholder",
    "example",
    "e.g.",
    "format:",
    "格式为",
)

_PLACEHOLDER_VALUES = frozenset(
    {
        "...",
        "…",
        "xxx",
        "xxxx",
        "flag",
        "your_flag",
        "your_flag_here",
        "your-flag-here",
        "example",
        "placeholder",
        "todo",
        "redacted",
        "here",
        "string",
    }
)

MAX_CANDIDATES_DEFAULT = 16


@dataclass(frozen=True, slots=True)
class FlagCandidate:
    """一个通过基础校验的 Flag 候选。"""

    value: str
    origin: str
    line: int

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.value.encode("utf-8")).hexdigest()

    @property
    def preview(self) -> str:
        return mask_flag(self.value)


def mask_flag(value: str) -> str:
    """只显示首尾少量字符，避免日志/报告出现完整答案。"""
    if len(value) <= 10:
        return value[:2] + "…" + value[-2:]
    return value[:8] + "…" + value[-4:]


def _looks_like_placeholder(content: str) -> bool:
    normalized = content.strip().lower()
    if not normalized:
        return True
    if any(char in content for char in "<>{}"):
        return True
    if "$" in content or "%s" in content:
        return True
    if re.search(r"\s", content):
        return True
    if normalized in _PLACEHOLDER_VALUES:
        return True
    # 只有 x / 点 / 下划线 / 连字符的占位内容
    return set(normalized) <= {"x", ".", "_", "-"}


def _is_placeholder_line(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_CONTEXT)


def extract_flag_candidates(
    text: str,
    *,
    origin: str = "final_answer",
    max_candidates: int = MAX_CANDIDATES_DEFAULT,
) -> list[FlagCandidate]:
    """从最终回答提取候选，按出现顺序去重（大小写不敏感）。"""
    candidates: list[FlagCandidate] = []
    seen: set[str] = set()
    for index, line in enumerate(text.splitlines(), start=1):
        if _is_placeholder_line(line):
            continue
        for match in FLAG_PATTERN.finditer(line):
            content = match.group(1)
            if _looks_like_placeholder(content):
                continue
            value = match.group(0)
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(FlagCandidate(value=value, origin=origin, line=index))
            if len(candidates) >= max_candidates:
                return candidates
    return candidates


__all__ = [
    "FLAG_PATTERN",
    "MAX_CANDIDATES_DEFAULT",
    "FlagCandidate",
    "extract_flag_candidates",
    "mask_flag",
]
