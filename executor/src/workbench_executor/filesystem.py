"""任务工作区文件系统。

约束（BUILD_PLAN §8.2、§9.1、§17）：

- 只允许在**本任务工作区**内操作。工作区路径由服务端拼装（`workspace_root/<workspace_id>`），
  客户端只能传 `workspace_id`（必须是 UUID），**不接受任意路径前缀**；
- 拒绝绝对路径、`..` 越界、以及**软链接逃逸**（解析后的真实路径必须仍在工作区内）；
- `edit` 带**预期内容版本**校验，避免覆盖别人的改动；版本用内容 sha256 表示，
  不依赖 mtime（mtime 在同一秒内可变、跨文件系统语义也不一致）；
- 读文件保留原始字节；解码不确定时返回明确说明，**不做** GBK/Latin-1 兜底猜测；
- 文件大小有上限，超出返回确定错误而不是静默截断。

**与命令工作区分开**：浏览器状态、Executor 自身令牌、控制端凭据都不在这个目录下，
因此文件工具无法读到它们。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_MAX_READ_BYTES = 256 * 1024
DEFAULT_MAX_WRITE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_LIST_ENTRIES = 500
DEFAULT_MAX_PATH_DEPTH = 16


class WorkspaceError(Exception):
    """工作区操作被拒绝。调用方据此返回确定错误，不重试。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(slots=True)
class FileEntry:
    path: str
    kind: str  # file | dir | symlink | other
    byte_size: int
    modified_at: str


@dataclass(slots=True)
class FileContent:
    path: str
    version: str
    byte_size: int
    text: str | None
    encoding_note: str | None
    offset: int
    truncated: bool


class WorkspaceFileSystem:
    """一个任务工作区的受限文件视图。"""

    def __init__(
        self,
        root: Path,
        *,
        max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
        max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES,
        max_list_entries: int = DEFAULT_MAX_LIST_ENTRIES,
    ) -> None:
        self._root = Path(root)
        self._max_read_bytes = max_read_bytes
        self._max_write_bytes = max_write_bytes
        self._max_list_entries = max_list_entries

    @property
    def root(self) -> Path:
        return self._root

    def ensure_root(self) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root

    # ---------------- 路径解析 ----------------

    def _real_root(self) -> Path:
        self.ensure_root()
        return self._root.resolve(strict=True)

    def resolve(self, relative: str, *, must_exist: bool = True) -> Path:
        """把相对路径解析为工作区内的绝对路径，越界一律拒绝。

        这里做了**三层**检查，缺一不可：
        1. 语法层：拒绝绝对路径与 `..` 分量；
        2. 深度层：拒绝过深路径（避免构造型攻击与误用）；
        3. 真实路径层：`resolve()` 之后（它会展开软链接）必须仍在工作区内 ——
           这一层才是挡住软链接逃逸的关键。
        """
        if not relative or relative in {".", "./"}:
            candidate = self._root
        else:
            raw = Path(relative)
            # 平台无关的"绝对路径"判定。
            # **不能只用 `Path.is_absolute()`**：Windows 上 `/etc/passwd` 会被判为
            # 相对路径（缺少盘符），而它在语义上明显是绝对路径。
            # 这里显式认三种形态：以分隔符开头、带盘符、UNC 前缀。
            if (
                relative.startswith(("/", "\\"))
                or (len(relative) > 1 and relative[1] == ":")
                or relative.startswith("\\\\")
            ):
                raise WorkspaceError("invalid_path", "只允许工作区内的相对路径")
            if raw.is_absolute():
                raise WorkspaceError("invalid_path", "只允许工作区内的相对路径")
            parts = [part for part in raw.parts if part not in {"", "."}]
            if any(part == ".." for part in parts):
                raise WorkspaceError("invalid_path", "路径不得包含 '..'")
            if len(parts) > DEFAULT_MAX_PATH_DEPTH:
                raise WorkspaceError("invalid_path", f"路径层级过深（>{DEFAULT_MAX_PATH_DEPTH}）")
            candidate = self._root.joinpath(*parts)

        if must_exist and not candidate.exists():
            raise WorkspaceError("not_found", f"路径不存在：{relative}")

        real_root = self._real_root()
        if not candidate.exists():
            # 目标尚不存在：检查**最近的已存在祖先**是否仍在工作区内，
            # 否则可以在指向外部的软链接目录里创建文件。
            probe = candidate.parent
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            real_probe = probe.resolve(strict=True)
            if real_probe != real_root and real_root not in real_probe.parents:
                raise WorkspaceError("path_escape", "路径经软链接指向工作区之外")
            return candidate

        real_candidate = candidate.resolve(strict=True)
        if real_candidate != real_root and real_root not in real_candidate.parents:
            raise WorkspaceError("path_escape", "路径经软链接指向工作区之外")
        return candidate

    # ---------------- 操作 ----------------

    def list_dir(self, relative: str = "") -> list[FileEntry]:
        # 工作区根目录可以还没被创建（新任务、还没写过任何文件）。
        # "空工作区" 的正确语义是**返回空列表**，不是报错 ——
        # 否则模型第一次 list_files 就会拿到一个让人困惑的 not_found。
        self.ensure_root()
        target = self.resolve(relative, must_exist=True)
        if not target.is_dir():
            raise WorkspaceError("not_a_directory", f"不是目录：{relative}")
        entries: list[FileEntry] = []
        for child in sorted(target.iterdir(), key=lambda item: item.name):
            if len(entries) >= self._max_list_entries:
                break
            try:
                stat = child.lstat()
            except OSError:
                continue
            if child.is_symlink():
                kind = "symlink"
            elif child.is_dir():
                kind = "dir"
            elif child.is_file():
                kind = "file"
            else:
                kind = "other"
            entries.append(
                FileEntry(
                    path=str(child.relative_to(self._root).as_posix()),
                    kind=kind,
                    byte_size=int(stat.st_size),
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                )
            )
        return entries

    def read_file(self, relative: str, *, offset: int = 0, limit: int | None = None) -> FileContent:
        target = self.resolve(relative, must_exist=True)
        if not target.is_file():
            raise WorkspaceError("not_a_file", f"不是普通文件：{relative}")

        size = target.stat().st_size
        if size > self._max_read_bytes and limit is None:
            raise WorkspaceError(
                "file_too_large",
                f"文件 {size} 字节，超过单次读取上限 {self._max_read_bytes}；"
                "请用 offset/limit 分段读取",
            )

        bounded_offset = max(0, int(offset))
        bounded_limit = min(
            int(limit) if limit is not None else self._max_read_bytes, self._max_read_bytes
        )
        with target.open("rb") as handle:
            handle.seek(bounded_offset)
            chunk = handle.read(bounded_limit)

        version = self.file_version(relative)
        text: str | None = None
        note: str | None = None
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError as exc:
            note = (
                f"该段不是合法 UTF-8（首个非法位置 {exc.start}）；未做猜测性解码，"
                "请改用支持二进制的处理方式或分段读取。"
            )
        return FileContent(
            path=relative or ".",
            version=version,
            byte_size=size,
            text=text,
            encoding_note=note,
            offset=bounded_offset,
            truncated=bounded_offset + len(chunk) < size,
        )

    def file_version(self, relative: str) -> str:
        """内容版本：整文件 sha256。缺少文件时返回空版本标记。"""
        target = self.resolve(relative, must_exist=False)
        if not target.is_file():
            return "absent"
        hasher = hashlib.sha256()
        with target.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                hasher.update(chunk)
        return hasher.hexdigest()

    def write_file(
        self,
        relative: str,
        content: str,
        *,
        expected_version: str | None = None,
    ) -> dict[str, object]:
        payload = content.encode("utf-8")
        if len(payload) > self._max_write_bytes:
            raise WorkspaceError(
                "content_too_large",
                f"内容 {len(payload)} 字节，超过写入上限 {self._max_write_bytes}",
            )

        target = self.resolve(relative, must_exist=False)
        if target.is_dir():
            raise WorkspaceError("is_directory", f"目标是目录：{relative}")

        current = self.file_version(relative)
        if expected_version is not None and expected_version != current:
            raise WorkspaceError(
                "version_conflict",
                f"文件版本已变化（期望 {expected_version}，实际 {current}）；"
                "请先重新读取再决定是否覆盖。",
            )
        if expected_version is None and current != "absent":
            raise WorkspaceError(
                "already_exists",
                f"文件已存在（version={current}）。覆盖必须显式提供 expected_version，"
                "避免静默盖掉别人的改动。",
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)

        return {
            "path": relative,
            "version": self.file_version(relative),
            "byte_size": len(payload),
            "previous_version": current,
            "created": current == "absent",
        }

    def edit_file(
        self,
        relative: str,
        *,
        old_text: str,
        new_text: str,
        expected_version: str,
        replace_all: bool = False,
    ) -> dict[str, object]:
        """按内容替换。**必须**提供 expected_version，避免覆盖并发改动。"""
        if not old_text:
            raise WorkspaceError("invalid_edit", "old_text 不能为空")

        current = self.file_version(relative)
        if current == "absent":
            raise WorkspaceError("not_found", f"文件不存在：{relative}")
        if current != expected_version:
            raise WorkspaceError(
                "version_conflict",
                f"文件版本已变化（期望 {expected_version}，实际 {current}）；请重新读取后再编辑。",
            )

        target = self.resolve(relative, must_exist=True)
        raw = target.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError(
                "not_utf8",
                f"文件不是合法 UTF-8，无法按文本编辑（首个非法位置 {exc.start}）",
            ) from exc

        occurrences = text.count(old_text)
        if occurrences == 0:
            raise WorkspaceError("pattern_not_found", "old_text 在文件中不存在")
        if occurrences > 1 and not replace_all:
            raise WorkspaceError(
                "pattern_ambiguous",
                f"old_text 出现 {occurrences} 次；请扩大上下文使其唯一，或显式 replace_all=true",
            )

        updated = text.replace(old_text, new_text, -1 if replace_all else 1)
        payload = updated.encode("utf-8")
        if len(payload) > self._max_write_bytes:
            raise WorkspaceError("content_too_large", "编辑后内容超过写入上限")

        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)

        return {
            "path": relative,
            "version": self.file_version(relative),
            "previous_version": current,
            "replacements": occurrences if replace_all else 1,
            "byte_size": len(payload),
        }


def workspace_for(workspace_root: Path, workspace_id: str) -> WorkspaceFileSystem:
    """按 `workspace_id` 构造工作区。

    `workspace_id` 必须是 UUID：这样它**不可能**包含路径分隔符或 `..`，
    客户端也就无法借它跳到别处。校验放在这里而不是调用方，避免有人忘了校验。
    """
    try:
        parsed = uuid.UUID(str(workspace_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise WorkspaceError(
            "invalid_workspace", f"workspace_id 必须是 UUID，收到 {workspace_id!r}"
        ) from exc
    root = Path(workspace_root) / str(parsed)
    return WorkspaceFileSystem(root)


def describe_workspace(root: Path) -> dict[str, object]:
    """诊断用：工作区是否可写、剩余空间。"""
    try:
        usage = shutil.disk_usage(root if root.exists() else root.parent)
    except OSError:
        return {"path": str(root), "writable": False}
    return {
        "path": str(root),
        "exists": root.exists(),
        "free_bytes": usage.free,
    }
