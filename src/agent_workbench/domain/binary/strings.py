"""受限字符串索引；保留不同位置，不将疑似凭据认定为有效秘密。"""

import ipaddress
import re
from collections import Counter
from typing import Any

from agent_workbench.domain.binary.model import address_location

KINDS = {"any", "url", "ip", "api_path", "file_path", "error", "config", "plain"}


def classify(text: str) -> str:
    lower = text.lower()
    if re.search(r"https?://", lower):
        return "url"
    if re.search(r"/(?:api\b|v[1-9]/)|/[^ ]+\.(?:php|aspx?)(?:\?|$)", lower):
        return "api_path"
    if re.search(r"[a-z]:\\|/(?:etc|usr|var|home|tmp)/", lower):
        return "file_path"
    for candidate in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
        try:
            ipaddress.ip_address(candidate)
            return "ip"
        except ValueError:
            continue
    if any(word in lower for word in ("error", "failed", "exception", "denied", "invalid")):
        return "error"
    if any(word in lower for word in ("config", "http/", "content-type", "token", "password", "=")):
        return "config"
    return "plain"


def extract(
    raw: bytes,
    sections: list[dict[str, Any]],
    max_strings: int,
    filters: dict[str, Any] | None = None,
) -> tuple[list[Any], dict[str, Any]]:
    # 最短索引长度 4；工具可再提高 min_length。ASCII 和 ASCII 子集 UTF-16LE/BE。
    patterns = [
        ("ascii", rb"[\x20-\x7e]{4,}", 1),
        ("utf-16le", rb"(?:[\x20-\x7e]\x00){4,}", 2),
        ("utf-16be", rb"(?:\x00[\x20-\x7e]){4,}", 2),
    ]
    entries = []
    counts: Counter[str] = Counter()
    total = 0
    matched = 0
    for encoding, pattern, width in patterns:
        for match in re.finditer(pattern, raw):
            if width == 2 and match.start() % 2:
                continue
            # 长字符串分块保留位置；不无声丢掉后部。每条最多 512 字符。
            for start in range(match.start(), match.end(), 512 * width):
                end = min(start + 512 * width, match.end())
                text = raw[start:end].decode(encoding)
                kind = classify(text)
                total += 1
                counts[kind] += 1
                loc = address_location(start, sections)
                if filters and (
                    len(text) < filters["min_length"]
                    or filters["query"] not in text.casefold()
                    or (filters["kind"] != "any" and kind != filters["kind"])
                    or (filters["section"] is not None and loc["section"] != filters["section"])
                ):
                    continue
                matched += 1
                if len(entries) < max_strings:
                    entries.append(
                        [start, text, encoding, loc["section"], loc["rva"], loc["va"], kind]
                    )
    entries.sort(key=lambda item: (item[0], item[2]))
    return entries, {
        "observed": total,
        "indexed": len(entries),
        "by_kind": dict(counts),
        "truncated": matched > len(entries),
        "matched": matched,
        "index_scope": "filtered" if filters else "all",
        "encodings": [p[0] for p in patterns],
        "note": "UTF-16 当前仅索引双字节对齐的可打印 ASCII 子集；无完整 Unicode 字符串恢复。",
    }
