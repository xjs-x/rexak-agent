"""执行工作区的原子编辑、稳定行游标与编码边界。"""

from __future__ import annotations

from pathlib import Path

import pytest
from workbench_executor.filesystem import WorkspaceError, WorkspaceFileSystem


def test_batch_edit_is_all_or_nothing_and_keeps_version(tmp_path: Path):
    fs = WorkspaceFileSystem(tmp_path)
    path = tmp_path / "script.py"
    path.write_text("第一行\n第二行\n", encoding="utf-8")
    version = fs.file_version("script.py")
    with pytest.raises(WorkspaceError, match="不存在"):
        fs.edit_file_batch("script.py", expected_version=version, edits=[
            {"old_text": "第一行", "new_text": "已替换"},
            {"old_text": "未命中", "new_text": "错误"},
        ])
    assert fs.file_version("script.py") == version
    result = fs.edit_file_batch("script.py", expected_version=version, edits=[
        {"old_text": "第一行", "new_text": "更新"},
        {"old_text": "第二行", "new_text": "正常中文"},
    ])
    assert result["replacements"] == 2
    assert path.read_text(encoding="utf-8") == "更新\n正常中文\n"
    with pytest.raises(WorkspaceError, match="版本"):
        fs.edit_file_batch("script.py", expected_version=version, edits=[
            {"old_text": "更新", "new_text": "陈旧修改"}])


def test_search_cursor_keeps_remaining_lines_in_same_file(tmp_path: Path):
    (tmp_path / "text.txt").write_text("命中一\n命中二\n命中三\n", encoding="utf-8")
    fs = WorkspaceFileSystem(tmp_path)
    cursor = None
    matches = []
    for _ in range(5):
        page = fs.search_files("命中", cursor=cursor, limit=1)
        matches.extend(page["matches"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert [item["line"] for item in matches] == [1, 2, 3]
    first = fs.search_files("命中", limit=1)
    with pytest.raises(WorkspaceError, match="游标"):
        fs.search_files("另一查询", cursor=first["next_cursor"], limit=1)


def test_glob_pages_and_search_report_unknown_bytes(tmp_path: Path):
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("中文", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"\0bad")
    (tmp_path / "invalid.txt").write_bytes(b"\xff")
    fs = WorkspaceFileSystem(tmp_path)
    first = fs.glob_files("*.py", limit=2)
    second = fs.glob_files("*.py", cursor=first["next_cursor"], limit=2)
    assert [item["path"] for item in first["entries"] + second["entries"]] == ["a.py", "b.py", "c.py"]
    result = fs.search_files("中文")
    assert len(result["matches"]) == 3
    assert {item["path"] for item in result["skipped"]} == {"binary.bin", "invalid.txt"}


def test_search_regex_has_runtime_boundary(tmp_path: Path):
    (tmp_path / "slow.txt").write_text("a" * 100000 + "!", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="时间边界"):
        WorkspaceFileSystem(tmp_path).search_files("(a+)+$", regex=True)


def test_batch_edit_rechecks_version_before_replace(tmp_path: Path, monkeypatch):
    path = tmp_path / "file.txt"
    path.write_text("原文", encoding="utf-8")
    fs = WorkspaceFileSystem(tmp_path)
    original = fs.file_version
    version = original("file.txt")
    calls = 0

    def change_before_second_check(relative):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_text("并发修改", encoding="utf-8")
        return original(relative)

    monkeypatch.setattr(fs, "file_version", change_before_second_check)
    with pytest.raises(WorkspaceError, match="再次变化"):
        fs.edit_file_batch("file.txt", expected_version=version,
                           edits=[{"old_text": "原文", "new_text": "不应覆盖"}])
    assert path.read_text(encoding="utf-8") == "并发修改"
