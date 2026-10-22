"""版本化方法资料的**只读**装载器。

规则（§6.3）：
- 方法资料不是可写记忆，**不进** LangGraph Store；
- 按需检索，不把全部资料塞进系统提示；
- 每份资料带版本与来源摘要，便于报告引用"当时用的是哪一版方法"。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

METHODS_DIR = Path(__file__).resolve().parent / "methods"

#: 资料集版本。新增/修改 md 文件时必须同步递增，并写入 run 的 prompt_version 快照。
#: v3：新增 `weakness-chains.md`（弱点关系驱动的持续探索）。
#: v4：明确外部条件与可由探索获取的条件，以及 no_new_action 的已有尝试依据。
#: v5：区分用户主机回环与执行岛回环，避免连接失败的跨机器误判。
METHODS_BUNDLE_VERSION = "8"


@dataclass(frozen=True, slots=True)
class MethodDoc:
    name: str
    title: str
    content: str
    sha256: str
    byte_size: int


class MethodNotAvailable(LookupError):
    """请求了不存在的方法资料。返回**确定错误**，不诱导模型去找文件系统。"""


def _read_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


@lru_cache(maxsize=1)
def _load_all() -> dict[str, MethodDoc]:
    docs: dict[str, MethodDoc] = {}
    if not METHODS_DIR.exists():
        return docs
    for path in sorted(METHODS_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        docs[path.stem] = MethodDoc(
            name=path.stem,
            title=_read_title(text, path.stem),
            content=text,
            sha256=digest,
            byte_size=len(text.encode("utf-8")),
        )
    return docs


def list_methods() -> list[dict[str, object]]:
    """列出可用方法（不返回正文，避免无意中把全部资料注入上下文）。"""
    return [
        {
            "name": doc.name,
            "title": doc.title,
            "byte_size": doc.byte_size,
            "sha256": doc.sha256,
            "bundle_version": METHODS_BUNDLE_VERSION,
        }
        for doc in _load_all().values()
    ]


def load_method(name: str) -> MethodDoc:
    docs = _load_all()
    doc = docs.get(name)
    if doc is None:
        raise MethodNotAvailable(f"没有名为 {name!r} 的方法资料。可用：{sorted(docs)}")
    return doc


def methods_bundle_version() -> str:
    return METHODS_BUNDLE_VERSION
