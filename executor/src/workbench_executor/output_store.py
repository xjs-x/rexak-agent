"""有界输出采集：追加日志、游标、缺失区间与增量 UTF-8 解码。

设计（规格 §8）：
- **采集日志真实限额**：单资源 stdout+stderr 合计上限；超限默认 `preserve_process` ——
  继续排空管道并丢弃超额日志，同时记录缺失区间与可核实计数；只有显式
  `terminate` 才触发取消升级。
- **游标绑定资源/stream/offset**，不能用于读取其他任务；按**已存字节**（stored 偏移）
  推进，与丢弃策略解耦。
- **缺失区间以"源流坐标"记录**：每条丢弃段记录 `(at_stored, dropped)`；
  stored 偏移 -> source 偏移的映射由丢弃表推导，输出页同时给出两种坐标的换算结果。
- **UTF-8 分页跨字符**：不完整尾部保留为 carry，由下一页继续拼接；非法字节
  不静默 replace，而是报告编码错误与原始区间，同时继续推进游标。

本模块既被 job_worker（写侧）使用，也被 Executor 服务（读侧）使用，
两处共用同一份文件格式，避免两份实现漂移。
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

#: 缺失区间元数据的上限（条数）。超过后合并为汇总区间并置位 truncated 标记，
#: 保证元数据本身有界，不会随输出无限增长。
MAX_MISSING_RANGE_ENTRIES = 64

#: 索引侧车文件的原子替换顺序：写 tmp -> fsync -> rename。
INDEX_FILENAME = "output-index.json"


class CursorError(ValueError):
    """游标非法或跨资源/stream 复用。**拒绝而不是悄悄从头读**。"""


def encode_cursor(resource_id: str, stream: str, offset: int) -> str:
    raw = f"v2|{resource_id}|{stream}|{int(offset)}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_cursor(cursor: str | None, *, resource_id: str, default_stream: str) -> tuple[str, int]:
    """解析游标。非法或跨资源使用 -> `CursorError`（调用方转成确定错误）。"""
    if not cursor:
        return default_stream, 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
        version, bound_resource, stream, offset = decoded.split("|")
    except (ValueError, UnicodeDecodeError) as exc:
        raise CursorError(f"游标格式非法：{cursor!r}") from exc
    if version != "v2":
        raise CursorError("游标版本不受支持；请从该资源的输出页重新开始读取")
    if bound_resource != resource_id:
        raise CursorError("该游标绑定的是另一个资源，不能跨资源复用")
    if stream not in {"stdout", "stderr"}:
        raise CursorError("游标绑定的 stream 非法")
    return stream, int(offset)


# --------------------------------------------------------------------------- #
# 解码：增量、不吞错、保留原始区间
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DecodeResult:
    text: str
    consumed: int
    #: (start, end) 原始字节区间，出现非法字节时非空
    error_range: tuple[int, int] | None = None
    error_detail: str = ""


def decode_chunk(data: bytes) -> DecodeResult:
    """把一块原始字节解码为文本。

    - 末尾不完整的多字节字符**不消费**（`consumed` 小于 `len(data)`），
      由调用方保留 carry；这保证分页边界不漏字、不重复。
    - 非法起始字节或不合法序列：输出 U+FFFD 占位并记录原始区间，
      游标仍按原始字节推进；**不**静默丢弃、**不**猜测 GBK/Latin-1。
    """
    out: list[str] = []
    i = 0
    n = len(data)
    error_start: int | None = None
    error_end = 0
    detail = ""
    while i < n:
        byte = data[i]
        if byte < 0x80:
            out.append(chr(byte))
            i += 1
            continue
        if 0xC2 <= byte <= 0xDF:
            need = 2
        elif 0xE0 <= byte <= 0xEF:
            need = 3
        elif 0xF0 <= byte <= 0xF4:
            need = 4
        else:
            if error_start is None:
                error_start = i
            error_end = i + 1
            detail = detail or f"非法起始字节 0x{byte:02x}（偏移 {i}）"
            out.append("\ufffd")
            i += 1
            continue
        if i + need > n:
            # 尾部不完整：不消费，保留给下一页
            break
        sequence = data[i : i + need]
        try:
            out.append(sequence.decode("utf-8"))
            i += need
            continue
        except UnicodeDecodeError:
            if error_start is None:
                error_start = i
            error_end = i + 1
            detail = detail or f"非法 UTF-8 序列 @{i}: {sequence.hex()}"
            out.append("\ufffd")
            i += 1
            continue
    consumed = i
    return DecodeResult(
        text="".join(out),
        consumed=consumed,
        error_range=(error_start, error_end) if error_start is not None else None,
        error_detail=detail,
    )


# --------------------------------------------------------------------------- #
# 索引侧车
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DropSegment:
    """一段被丢弃的源字节。`at_stored` 是丢弃发生时该 stream 的已存字节数。"""

    at_stored: int
    dropped: int


@dataclass(slots=True)
class StreamIndex:
    stored_bytes: int = 0
    source_bytes: int = 0
    drops: list[DropSegment] = field(default_factory=list)
    missing_truncated: bool = False
    missing_total_bytes: int = 0

    def dropped_before(self, stored_offset: int) -> int:
        """在 stored 偏移**之前**发生的丢弃总量。

        严格小于 `stored_offset`：位于同一边界的丢弃段（`at_stored == stored_offset`）
        表示"从该偏移之后开始丢失"，不应把它计入这里的映射，否则
        `stored_end=6` 会被映射成源偏移 18，缺失区间起点也就错了。
        """
        total = 0
        for item in self.drops:
            if item.at_stored < stored_offset:
                total += item.dropped
            else:
                break
        return total

    def missing_ranges(self) -> list[tuple[int, int]]:
        """以 stored 坐标为参照，把丢弃段展开为源坐标区间列表。"""
        ranges: list[tuple[int, int]] = []
        for item in self.drops:
            source = item.at_stored + self.dropped_before(item.at_stored)
            ranges.append((source, source + item.dropped))
        return ranges

    def to_json(self) -> dict[str, Any]:
        return {
            "stored_bytes": self.stored_bytes,
            "source_bytes": self.source_bytes,
            "drops": [{"at_stored": item.at_stored, "dropped": item.dropped} for item in self.drops],
            "missing_truncated": self.missing_truncated,
            "missing_total_bytes": self.missing_total_bytes,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> StreamIndex:
        return cls(
            stored_bytes=int(raw.get("stored_bytes", 0)),
            source_bytes=int(raw.get("source_bytes", 0)),
            drops=[
                DropSegment(at_stored=int(item["at_stored"]), dropped=int(item["dropped"]))
                for item in raw.get("drops", [])
            ],
            missing_truncated=bool(raw.get("missing_truncated", False)),
            missing_total_bytes=int(raw.get("missing_total_bytes", 0)),
        )


@dataclass(slots=True)
class OutputIndex:
    resource_id: str
    streams: dict[str, StreamIndex] = field(default_factory=dict)
    limit_bytes: int = 0
    limit_action: str = "preserve_process"
    limit_reached: bool = False
    updated_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "resource_id": self.resource_id,
            "limit_bytes": self.limit_bytes,
            "limit_action": self.limit_action,
            "limit_reached": self.limit_reached,
            "updated_at": self.updated_at,
            "streams": {name: item.to_json() for name, item in self.streams.items()},
        }

    @classmethod
    def load(cls, path: Path, *, resource_id: str) -> OutputIndex | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("resource_id") != resource_id:
            return None
        index = cls(
            resource_id=resource_id,
            limit_bytes=int(raw.get("limit_bytes", 0)),
            limit_action=str(raw.get("limit_action", "preserve_process")),
            limit_reached=bool(raw.get("limit_reached", False)),
            updated_at=str(raw.get("updated_at", "")),
        )
        streams = raw.get("streams") or {}
        if not isinstance(streams, dict):
            return None
        for name, item in streams.items():
            if isinstance(item, dict):
                index.streams[str(name)] = StreamIndex.from_json(item)
        return index


def write_index(index: OutputIndex, path: Path) -> None:
    """原子写入索引侧车（tmp + fsync + rename），防止读到半个 JSON。"""
    index.updated_at = datetime.now(UTC).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    data = json.dumps(index.to_json(), ensure_ascii=False, sort_keys=True)
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


# --------------------------------------------------------------------------- #
# 写侧：job_worker 使用的有界采集
# --------------------------------------------------------------------------- #


class OutputLimitExceeded(RuntimeError):
    """显式 `terminate` 模式下达到额度。调用方负责触发取消升级。"""


class ResourceOutputStore:
    """一个资源的输出采集器（写侧）。

    - stdout/stderr 分开保存（普通命令）；PTY 只用一个合流 stream；
    - **合计**额度；超出后默认丢弃并记录缺失区间；
    - `terminate` 模式下超限抛出 `OutputLimitExceeded`，由 worker 转换为止损动作。
    """

    def __init__(
        self,
        directory: Path,
        *,
        resource_id: str,
        limit_bytes: int,
        limit_action: str,
        streams: tuple[str, ...] = ("stdout", "stderr"),
        index_flush_interval_seconds: float = 1.0,
    ) -> None:
        self._dir = directory
        self._resource_id = resource_id
        self._limit = max(0, int(limit_bytes))
        self._action = limit_action
        self._streams = streams
        self._handles: dict[str, Any] = {}
        self._index = OutputIndex(
            resource_id=resource_id, limit_bytes=self._limit, limit_action=limit_action
        )
        for name in streams:
            self._index.streams[name] = StreamIndex()
        self._flush_interval = index_flush_interval_seconds
        self._last_flush = 0.0
        self._closed = False

    @property
    def index(self) -> OutputIndex:
        return self._index

    @property
    def limit_reached(self) -> bool:
        return self._index.limit_reached

    @property
    def stored_total(self) -> int:
        return sum(item.stored_bytes for item in self._index.streams.values())

    def open(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        for name in self._streams:
            path = self._path_for(name)
            self._handles[name] = path.open("ab")
        self.flush_index(force=True)

    def _path_for(self, stream: str) -> Path:
        return self._dir / f"{stream}.log"

    def path_for(self, stream: str) -> Path:
        return self._path_for(stream)

    def write(self, stream: str, data: bytes) -> int:
        """写入一块输出；返回被**保存**的字节数。

        超出额度的部分在 `preserve_process` 下丢弃并记录；在 `terminate` 下
        抛 `OutputLimitExceeded`（本块已保存的部分先落盘）。
        """
        if self._closed or not data:
            return 0
        if stream not in self._handles:
            # 未声明的 stream（例如 PTY 只有合流）：并入 stdout 语义拒绝，防止静默建文件
            raise KeyError(f"未声明的输出 stream：{stream}")
        remaining = self._limit - self.stored_total
        accepted = data if remaining >= len(data) else data[: max(0, remaining)]
        if accepted:
            self._handles[stream].write(accepted)
            self._handles[stream].flush()
            entry = self._index.streams[stream]
            entry.stored_bytes += len(accepted)
            entry.source_bytes += len(accepted)
        dropped = len(data) - len(accepted)
        if dropped:
            entry = self._index.streams[stream]
            entry.source_bytes += dropped
            entry.missing_total_bytes += dropped
            self._record_drop(entry, dropped)
            if not self._index.limit_reached:
                self._index.limit_reached = True
            if self._action == "terminate":
                self.flush_index(force=True)
                raise OutputLimitExceeded(
                    f"采集日志达到上限 {self._limit} 字节（output_limit_action=terminate）"
                )
        self._maybe_flush()
        return len(accepted)

    def _record_drop(self, entry: StreamIndex, dropped: int) -> None:
        if entry.drops and entry.drops[-1].at_stored == entry.stored_bytes:
            entry.drops[-1].dropped += dropped
        else:
            entry.drops.append(DropSegment(at_stored=entry.stored_bytes, dropped=dropped))
        if len(entry.drops) > MAX_MISSING_RANGE_ENTRIES:
            # 元数据有界：保留头部区间，把其余合并进最后一个汇总段。
            head = entry.drops[: MAX_MISSING_RANGE_ENTRIES - 1]
            tail = entry.drops[MAX_MISSING_RANGE_ENTRIES - 1 :]
            merged = DropSegment(
                at_stored=tail[0].at_stored,
                dropped=sum(item.dropped for item in tail),
            )
            entry.drops = [*head, merged]
            entry.missing_truncated = True

    def note_source_bytes(self, stream: str, extra_source: int) -> None:
        """记录"读到了但未保存"的额外源字节（terminate 模式丢弃后继续排空时使用）。"""
        entry = self._index.streams.get(stream)
        if entry is None or extra_source <= 0:
            return
        entry.source_bytes += extra_source
        entry.missing_total_bytes += extra_source
        self._record_drop(entry, extra_source)
        self._maybe_flush()

    def _maybe_flush(self) -> None:
        now = time.monotonic()
        if now - self._last_flush >= self._flush_interval:
            self.flush_index()

    def flush_index(self, *, force: bool = False) -> None:
        if self._closed and not force:
            return
        write_index(self._index, self._dir / INDEX_FILENAME)
        self._last_flush = time.monotonic()

    def close(self) -> None:
        if self._closed:
            return
        for handle in self._handles.values():
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except OSError:
                pass
            handle.close()
        self._handles.clear()
        write_index(self._index, self._dir / INDEX_FILENAME)
        self._closed = True


# --------------------------------------------------------------------------- #
# 读侧：Executor 服务读取保留日志
# --------------------------------------------------------------------------- #


def read_slice(path: Path, *, offset: int, limit: int) -> tuple[bytes, int]:
    """读取保留日志切片；返回 `(数据, 当前文件大小)`。

    文件不存在按空处理（例如尚未产生该 stream 的输出）。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return b"", 0
    start = min(max(offset, 0), size)
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read(max(0, limit))
    return data, size


def missing_ranges_for(
    index: OutputIndex | None, stream: str, *, stored_start: int, stored_end: int
) -> list[tuple[int, int, str]]:
    """页窗口（stored 坐标）相关的缺失区间（源坐标）。

    除真正与窗口相交的区间外，**紧邻窗口末尾**（`start == source_end`）的区间
    也会被报告：调用方读到"当前末尾"时必须知道其后还有内容被丢弃，
    否则会把"读到末尾"误当成完整输出。
    """
    if index is None:
        return []
    entry = index.streams.get(stream)
    if entry is None or not entry.drops:
        return []
    source_start = stored_start + entry.dropped_before(stored_start)
    source_end = stored_end + entry.dropped_before(stored_end)
    out: list[tuple[int, int, str]] = []
    for start, end in entry.missing_ranges():
        if end <= source_start or start > source_end:
            continue
        out.append((start, end, "采集额度超限：该区间输出在源流中存在但未被保存"))
    if entry.missing_truncated and not out:
        total = entry.missing_total_bytes
        out.append((source_start, source_start, f"缺失区间元数据已合并（共 {total} 字节未保存）"))
    return out


Mode = Literal["stdout", "stderr"]


def stream_path(directory: Path, stream: str) -> Path:
    return directory / f"{stream}.log"
