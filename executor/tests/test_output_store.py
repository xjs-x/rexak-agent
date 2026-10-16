"""有界输出采集与游标（验收 E01/E02/E06/E07 的可移植部分）。

这些检查不依赖 Linux：采集、游标、缺失区间、增量解码都是纯文件/字节逻辑。
真进程额度行为（E03/E04/E05）在 `test_execution_jobs_linux.py`（Linux 上运行）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from workbench_executor.output_store import (
    CursorError,
    OutputIndex,
    ResourceOutputStore,
    decode_chunk,
    encode_cursor,
    missing_ranges_for,
    parse_cursor,
    read_slice,
    write_index,
)


def test_cursor_binds_resource_and_stream():
    cursor = encode_cursor("res-1", "stdout", 42)
    stream, offset = parse_cursor(cursor, resource_id="res-1", default_stream="stderr")
    assert (stream, offset) == ("stdout", 42)
    with pytest.raises(CursorError, match="另一个资源"):
        parse_cursor(cursor, resource_id="res-2", default_stream="stdout")
    with pytest.raises(CursorError):
        parse_cursor("not-base64!!", resource_id="res-1", default_stream="stdout")


def test_decode_chunk_keeps_partial_tail_and_reports_invalid_bytes():
    # 中文「好」= e5 a5 bd：故意切在中间
    first = decode_chunk(b"abc\xe5\xa5")
    assert first.text == "abc"
    assert first.consumed == 3  # 尾部未完成字符不消费
    combined = b"abc\xe5\xa5" + b"\xbd\xe4\xb8\xad"
    full = decode_chunk(combined)
    assert full.text == "abc好中"
    assert full.consumed == len(combined)
    # 非法字节：报告区间但不静默丢弃、不猜测 GBK
    invalid = decode_chunk(b"ok\xffbad")
    assert "\ufffd" in invalid.text
    assert invalid.error_range is not None
    assert invalid.error_detail


def test_output_store_enforces_limit_and_records_missing_ranges(tmp_path: Path):
    store = ResourceOutputStore(
        tmp_path,
        resource_id="res-1",
        limit_bytes=10,
        limit_action="preserve_process",
        streams=("stdout", "stderr"),
    )
    store.open()
    accepted = store.write("stdout", b"0123456789abcde")  # 15 字节，只能存 10
    assert accepted == 10
    assert store.limit_reached
    accepted_stderr = store.write("stderr", b"zz")  # 共享额度已耗尽
    assert accepted_stderr == 0
    store.close()
    index = OutputIndex.load(tmp_path / "output-index.json", resource_id="res-1")
    assert index is not None
    assert index.streams["stdout"].stored_bytes == 10
    assert index.streams["stdout"].source_bytes == 15
    assert index.streams["stdout"].missing_total_bytes == 5
    assert index.streams["stderr"].missing_total_bytes == 2
    ranges = (
        index.streams["stdout"].to_json()["drops"],
        index.streams["stderr"].to_json()["drops"],
    )
    assert ranges[0][0]["dropped"] == 5
    assert ranges[1][0]["dropped"] == 2
    # 保留日志本身不删除
    assert (tmp_path / "stdout.log").read_bytes() == b"0123456789"


def test_missing_ranges_map_to_source_coordinates(tmp_path: Path):
    store = ResourceOutputStore(
        tmp_path,
        resource_id="res-1",
        limit_bytes=6,
        limit_action="preserve_process",
        streams=("stdout",),
    )
    store.open()
    store.write("stdout", b"abcdef")  # 全部保存
    store.write("stdout", b"GHIJKL")  # 全部丢弃（6 字节）
    store.write("stdout", b"MNOPQR")  # 再丢 6 字节（合并进同一段）
    store.close()
    index = OutputIndex.load(tmp_path / "output-index.json", resource_id="res-1")
    assert index is not None
    ranges = missing_ranges_for(index, "stdout", stored_start=0, stored_end=6)
    assert ranges and ranges[0][0] == 6 and ranges[0][1] == 18
    # 窗口在丢弃之前：不返回该区间
    assert missing_ranges_for(index, "stdout", stored_start=0, stored_end=1) == []


def test_read_slice_and_terminal_terminate_action(tmp_path: Path):
    store = ResourceOutputStore(
        tmp_path,
        resource_id="res-1",
        limit_bytes=4,
        limit_action="terminate",
        streams=("stdout",),
    )
    store.open()
    with pytest.raises(Exception) as excinfo:
        store.write("stdout", b"abcdefgh")
    assert "terminate" in str(excinfo.value)
    store.close()
    data, size = read_slice(tmp_path / "stdout.log", offset=0, limit=100)
    assert data == b"abcd"
    assert size == 4
    # 读到"当前末尾"不等于程序已结束 —— 这是调用方的语义，不由存储层伪造
    tail, _ = read_slice(tmp_path / "stdout.log", offset=4, limit=100)
    assert tail == b""


def test_index_survives_corruption(tmp_path: Path):
    """索引侧车损坏时按"无索引"处理；**不**因此丢弃保留日志。"""
    (tmp_path / "output-index.json").write_text("{not json", encoding="utf-8")
    assert OutputIndex.load(tmp_path / "output-index.json", resource_id="res-1") is None
    # 资源 id 不匹配时同样拒绝（防止读到别的资源的索引）
    write_index(OutputIndex(resource_id="res-2"), tmp_path / "output-index.json")
    assert OutputIndex.load(tmp_path / "output-index.json", resource_id="res-1") is None
