"""评测运行记录落盘。

记录写在 ``WORKBENCH_DATA_ROOT/tsecbench/``（仓库之外），不写源码树。
内容只包含摘要、命中判定与脱敏后的 flag 预览；**不保存完整 flag 与任何令牌**。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

RECORD_SCHEMA_VERSION = 1

_SAFE_CODE = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_code(unique_code: str) -> str:
    cleaned = _SAFE_CODE.sub("_", unique_code.strip())[:64].strip("._-")
    return cleaned or "challenge"


def records_dir(data_root: Path) -> Path:
    return data_root / "tsecbench"


def record_path(data_root: Path, unique_code: str, when: datetime) -> Path:
    stamp = when.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return records_dir(data_root) / f"{sanitize_code(unique_code)}-{stamp}.json"


def write_record(
    payload: dict[str, object],
    *,
    data_root: Path,
    unique_code: str,
    when: datetime | None = None,
) -> Path:
    """原子写入一份记录，返回最终路径。"""
    moment = when or datetime.now(UTC)
    target = record_path(data_root, unique_code, moment)
    target.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": RECORD_SCHEMA_VERSION, **payload}
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    tmp.replace(target)
    return target


__all__ = [
    "RECORD_SCHEMA_VERSION",
    "record_path",
    "records_dir",
    "sanitize_code",
    "write_record",
]
