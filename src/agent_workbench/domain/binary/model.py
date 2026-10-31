"""仅解析进程使用的索引约定。版本变化使持久缓存失效。"""

from importlib.metadata import version
from typing import Any

ANALYZER = "rexak-static"
INDEX_VERSION = "1.0.0"
ANALYZER_VERSION = (
    INDEX_VERSION
    + ";"
    + ";".join(f"{name}={version(name)}" for name in ("pefile", "pyelftools", "capstone"))
)
MAX_SECTIONS = 256
MAX_SYMBOLS = 20000


def address_location(offset: int, sections: list[dict[str, Any]]) -> dict[str, Any]:
    matches = [s for s in sections if s["offset"] <= offset < s["offset"] + s["file_size"]]
    if len(matches) != 1:
        return {"section": None, "rva": None, "va": None}
    section = matches[0]
    delta = offset - section["offset"]
    return {
        "section": section["name"],
        "rva": section["rva"] + delta if section["rva"] is not None else None,
        "va": section["va"] + delta if section["va"] is not None else None,
    }


def offset_for_va(va: int, sections: list[dict[str, Any]]) -> int | None:
    matches = [
        s for s in sections if s["va"] is not None and s["va"] <= va < s["va"] + s["file_size"]
    ]
    return matches[0]["offset"] + va - matches[0]["va"] if len(matches) == 1 else None
