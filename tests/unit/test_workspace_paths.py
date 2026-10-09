"""任务工作区的路径与版本安全。

这些是**安全边界**，必须逐条钉死（BUILD_PLAN §8.2、§9.1）：

- 绝对路径、`..`、过深路径一律拒绝；
- **软链接逃逸**必须被挡住 —— 这是最容易漏的一条，因为"看起来在工作区里"的路径
  经过一次 `resolve()` 就可能落到外面；
- 覆盖已有文件必须显式给出 `expected_version`，不允许静默盖掉别人的改动；
- `edit` 的匹配必须唯一（除非显式 `replace_all`），否则容易改错地方；
- 非 UTF-8 内容给明确说明，不做猜测性解码。
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from workbench_executor.filesystem import (
    WorkspaceError,
    WorkspaceFileSystem,
    workspace_for,
)


@pytest.fixture
def workspace(tmp_path: Path) -> WorkspaceFileSystem:
    return WorkspaceFileSystem(tmp_path / "ws")


def test_absolute_path_is_rejected(workspace: WorkspaceFileSystem) -> None:
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.resolve("/etc/passwd")
    assert excinfo.value.code == "invalid_path"


@pytest.mark.parametrize("path", ["../outside.txt", "a/../../outside.txt", "a/b/../../../x"])
def test_parent_traversal_is_rejected(workspace: WorkspaceFileSystem, path: str) -> None:
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.resolve(path)
    assert excinfo.value.code == "invalid_path"


def test_too_deep_path_is_rejected(workspace: WorkspaceFileSystem) -> None:
    deep = "/".join(f"d{index}" for index in range(40))
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.resolve(deep)
    assert excinfo.value.code == "invalid_path"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 创建符号链接需要额外权限")
def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    """工作区里指向外部的软链接**不能**成为逃逸通道。"""
    root = tmp_path / "ws"
    outside = tmp_path / "outside"
    root.mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("不该被读到", encoding="utf-8")
    link = root / "escape"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover - 环境不支持
        pytest.skip("当前环境无法创建符号链接")

    workspace = WorkspaceFileSystem(root)
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.resolve("escape/secret.txt")
    assert excinfo.value.code == "path_escape"

    # 反向：软链接本身也要被挡
    with pytest.raises(WorkspaceError):
        workspace.resolve("escape")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 创建符号链接需要额外权限")
def test_cannot_create_file_through_escaping_symlink(tmp_path: Path) -> None:
    """经由指向外部的软链接**创建**文件也必须被拒（目标尚不存在的那条路径）。"""
    root = tmp_path / "ws"
    outside = tmp_path / "outside"
    root.mkdir(parents=True)
    outside.mkdir()
    try:
        (root / "escape").symlink_to(outside)
    except (OSError, NotImplementedError):  # pragma: no cover
        pytest.skip("当前环境无法创建符号链接")

    workspace = WorkspaceFileSystem(root)
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.write_file("escape/new.txt", "内容")
    assert excinfo.value.code in {"path_escape", "invalid_path"}
    assert not (outside / "new.txt").exists()


def test_workspace_id_must_be_uuid(tmp_path: Path) -> None:
    """`workspace_id` 不是 UUID 时必须拒绝 —— 否则它就成了路径注入点。"""
    for bad in ("../..", "a/b", "", None, "not-a-uuid"):
        with pytest.raises(WorkspaceError) as excinfo:
            workspace_for(tmp_path, bad)  # type: ignore[arg-type]
        assert excinfo.value.code == "invalid_workspace"


def test_workspace_id_is_normalized(tmp_path: Path) -> None:
    identifier = uuid.uuid4()
    workspace = workspace_for(tmp_path, str(identifier))
    assert workspace.root == tmp_path / str(identifier)


def test_write_then_read_roundtrip(workspace: WorkspaceFileSystem) -> None:
    written = workspace.write_file("notes/a.txt", "第一行\n第二行\n")
    assert written["created"] is True
    assert written["previous_version"] == "absent"

    content = workspace.read_file("notes/a.txt")
    assert content.text == "第一行\n第二行\n"
    assert content.version == written["version"]
    assert content.encoding_note is None


def test_overwrite_requires_expected_version(workspace: WorkspaceFileSystem) -> None:
    """覆盖必须显式给出预期版本，不能被静默盖掉。"""
    first = workspace.write_file("a.txt", "v1")
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.write_file("a.txt", "v2")
    assert excinfo.value.code == "already_exists"

    second = workspace.write_file("a.txt", "v2", expected_version=str(first["version"]))
    assert second["previous_version"] == first["version"]
    assert second["version"] != first["version"]


def test_write_with_stale_version_is_rejected(workspace: WorkspaceFileSystem) -> None:
    first = workspace.write_file("a.txt", "v1")
    workspace.write_file("a.txt", "v2", expected_version=str(first["version"]))
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.write_file("a.txt", "v3", expected_version=str(first["version"]))
    assert excinfo.value.code == "version_conflict"


def test_edit_requires_unique_match(workspace: WorkspaceFileSystem) -> None:
    written = workspace.write_file("a.txt", "foo bar foo")
    version = str(written["version"])
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.edit_file("a.txt", old_text="foo", new_text="baz", expected_version=version)
    assert excinfo.value.code == "pattern_ambiguous"

    edited = workspace.edit_file(
        "a.txt", old_text="foo", new_text="baz", expected_version=version, replace_all=True
    )
    assert edited["replacements"] == 2
    assert workspace.read_file("a.txt").text == "baz bar baz"


def test_edit_with_stale_version_is_rejected(workspace: WorkspaceFileSystem) -> None:
    first = workspace.write_file("a.txt", "hello")
    workspace.write_file("a.txt", "hello world", expected_version=str(first["version"]))
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.edit_file(
            "a.txt",
            old_text="hello",
            new_text="hi",
            expected_version=str(first["version"]),
        )
    assert excinfo.value.code == "version_conflict"


def test_edit_missing_pattern_is_rejected(workspace: WorkspaceFileSystem) -> None:
    written = workspace.write_file("a.txt", "hello")
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.edit_file(
            "a.txt", old_text="absent", new_text="x", expected_version=str(written["version"])
        )
    assert excinfo.value.code == "pattern_not_found"


def test_list_dir_reports_kinds(workspace: WorkspaceFileSystem) -> None:
    workspace.write_file("a.txt", "x")
    workspace.write_file("sub/b.txt", "y")
    entries = {item.path: item for item in workspace.list_dir("")}
    assert entries["a.txt"].kind == "file"
    assert entries["sub"].kind == "dir"
    assert workspace.list_dir("sub")[0].path == "sub/b.txt"


def test_list_missing_dir_is_not_found(workspace: WorkspaceFileSystem) -> None:
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.list_dir("nope")
    assert excinfo.value.code == "not_found"


def test_invalid_utf8_is_reported_not_guessed(workspace: WorkspaceFileSystem) -> None:
    """非法 UTF-8 给明确说明；不做 GBK/Latin-1 兜底。"""
    root = workspace.ensure_root()
    (root / "bin.dat").write_bytes(b"\xff\xfe\x00\x01")
    content = workspace.read_file("bin.dat")
    assert content.text is None
    assert content.encoding_note and "不是合法 UTF-8" in content.encoding_note


def test_large_file_requires_ranged_read(workspace: WorkspaceFileSystem) -> None:
    big = "x" * (300 * 1024)
    workspace.write_file("big.txt", big)
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.read_file("big.txt")
    assert excinfo.value.code == "file_too_large"

    page = workspace.read_file("big.txt", offset=0, limit=10)
    assert page.text == "x" * 10
    assert page.truncated is True


def test_content_over_write_limit_is_rejected(workspace: WorkspaceFileSystem) -> None:
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.write_file("huge.txt", "y" * (5 * 1024 * 1024))
    assert excinfo.value.code == "content_too_large"


def test_file_version_is_content_addressed(workspace: WorkspaceFileSystem) -> None:
    """版本按内容算，不依赖 mtime（同一秒内修改也要能区分）。"""
    first = workspace.write_file("a.txt", "one")
    second = workspace.write_file("a.txt", "two", expected_version=str(first["version"]))
    assert first["version"] != second["version"]
    assert len(str(second["version"])) == 64


def test_workspace_root_is_separate_per_workspace_id(tmp_path: Path) -> None:
    """不同任务的工作区必须互相隔离。"""
    a = workspace_for(tmp_path, str(uuid.uuid4()))
    b = workspace_for(tmp_path, str(uuid.uuid4()))
    a.write_file("x.txt", "A")
    assert not (b.root / "x.txt").exists()
    assert os.path.realpath(a.root) != os.path.realpath(b.root)
