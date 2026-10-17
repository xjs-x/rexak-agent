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

import base64
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import regex as regex_engine

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
        self._require_writable(relative)
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
        return self.edit_file_batch(
            relative,
            edits=[
                {"old_text": old_text, "new_text": new_text, "replace_all": replace_all}
            ],
            expected_version=expected_version,
        )

    def edit_file_batch(
        self,
        relative: str,
        *,
        edits: list[dict[str, object]],
        expected_version: str,
    ) -> dict[str, object]:
        """批量编辑：**先在内存中校验全部替换**，任一失败不写文件；
        全部成功后写临时文件并原子替换（保留版本冲突与唯一匹配规则）。"""
        self._require_writable(relative)
        if not edits:
            raise WorkspaceError("invalid_edit", "edits 不能为空")
        if len(edits) > 100:
            raise WorkspaceError("invalid_edit", "单次批量编辑最多 100 项")

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

        total_replacements = 0
        for index, edit in enumerate(edits):
            old_text = str(edit.get("old_text") or "")
            new_text = str(edit.get("new_text") or "")
            replace_all = bool(edit.get("replace_all", False))
            if not old_text:
                raise WorkspaceError("invalid_edit", f"第 {index + 1} 项 old_text 不能为空")
            occurrences = text.count(old_text)
            if occurrences == 0:
                raise WorkspaceError(
                    "pattern_not_found", f"第 {index + 1} 项的 old_text 在文件中不存在"
                )
            if occurrences > 1 and not replace_all:
                raise WorkspaceError(
                    "pattern_ambiguous",
                    f"第 {index + 1} 项的 old_text 出现 {occurrences} 次；"
                    "请扩大上下文使其唯一，或显式 replace_all=true",
                )
            text = text.replace(old_text, new_text, -1 if replace_all else 1)
            total_replacements += occurrences if replace_all else 1

        payload = text.encode("utf-8")
        if len(payload) > self._max_write_bytes:
            raise WorkspaceError("content_too_large", "编辑后内容超过写入上限")
        if payload == raw:
            # 全部命中但没有产生变化：仍算成功，但版本不变，如实说明。
            return {
                "path": relative,
                "version": current,
                "previous_version": current,
                "replacements": total_replacements,
                "byte_size": len(raw),
                "no_change": True,
            }

        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if self.file_version(relative) != expected_version:
                raise WorkspaceError("version_conflict", "写入前版本再次变化，拒绝覆盖并发改动")
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)

        return {
            "path": relative,
            "version": self.file_version(relative),
            "previous_version": current,
            "replacements": total_replacements,
            "byte_size": len(payload),
        }

    def _require_writable(self, relative: str) -> None:
        target = self.resolve(relative, must_exist=False)
        cache = self._real_root() / ".rexak" / "skills"
        if target == cache or target.is_relative_to(cache):
            raise WorkspaceError("skill_path_denied", "已发布 Skill 缓存不能通过文件工具修改；请发布新版本")

    # ---------------- 检索与枚举 ----------------

    def _bounded_files(self, base: Path) -> tuple[list[Path], bool]:
        """有界枚举且不跟随目录软链接；目录项也计入扫描预算。"""
        pending = [base]
        files: list[Path] = []
        seen = 0
        real_root = self._real_root()
        while pending:
            current = pending.pop()
            entries = []
            with os.scandir(current) as iterator:
                for entry in iterator:
                    seen += 1
                    if seen > 20000:
                        return sorted(files), True
                    entries.append(entry)
            for entry in sorted(entries, key=lambda item: item.name):
                candidate = Path(entry.path)
                if entry.is_symlink():
                    real = candidate.resolve(strict=True)
                    if real != real_root and real_root not in real.parents:
                        raise WorkspaceError("path_escape", "枚举结果经软链接指向工作区之外")
                    if candidate.is_file():
                        files.append(candidate)
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(candidate)
                elif entry.is_file(follow_symlinks=False):
                    files.append(candidate)
        return sorted(files), False

    def glob_files(
        self,
        pattern: str,
        *,
        root: str = ".",
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict[str, object]:
        """工作区内 glob：支持 `**`，确定性排序与分页，拒绝路径/软链接逃逸。"""
        if not pattern or pattern.startswith(("/", "\\")):
            raise WorkspaceError("invalid_path", "pattern 必须是非空相对模式")
        if ".." in Path(pattern).parts:
            raise WorkspaceError("invalid_path", "pattern 不得包含 '..'")
        if len(pattern) > 512:
            raise WorkspaceError("invalid_path", "pattern 过长")
        base = self.resolve(root or ".", must_exist=True)
        if not base.is_dir():
            raise WorkspaceError("not_a_directory", f"不是目录：{root}")
        real_root = self._real_root()
        entries: list[dict[str, object]] = []
        scanned = 0
        scan_limited = False
        candidates, scan_limited = self._bounded_files(base)
        for candidate in candidates:
            relative = candidate.relative_to(base)
            matched = relative.match(pattern) or (
                pattern.startswith("**/") and relative.match(pattern[3:])
            )
            if "/" not in pattern and len(relative.parts) > 1:
                matched = False
            if not matched:
                continue
            scanned += 1
            try:
                real = candidate.resolve(strict=True)
            except OSError:
                continue
            if real != real_root and real_root not in real.parents:
                raise WorkspaceError("path_escape", "glob 结果经软链接指向工作区之外")
            if not candidate.is_file():
                continue
            stat = candidate.stat()
            entries.append(
                {
                    "path": candidate.relative_to(self._root).as_posix(),
                    "byte_size": int(stat.st_size),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                }
            )
        entries.sort(key=lambda item: str(item["path"]))
        start = 0
        if cursor:
            for index, item in enumerate(entries):
                if str(item["path"]) > cursor:
                    start = index
                    break
            else:
                start = len(entries)
        bounded = max(1, min(int(limit), 1000))
        page = entries[start : start + bounded]
        next_cursor = str(page[-1]["path"]) if len(entries) > start + len(page) else None
        return {
            "action": "glob",
            "pattern": pattern,
            "root": root or ".",
            "entries": page,
            "count": len(page),
            "total_matched": len(entries),
            "next_cursor": next_cursor,
            "scan_limited": scan_limited,
        }

    def search_files(
        self,
        pattern: str,
        *,
        paths: list[str] | None = None,
        regex: bool = False,
        case_sensitive: bool = True,
        context_lines: int = 0,
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict[str, object]:
        """有界文本检索：返回路径/行号/上下文；二进制与非法编码显式说明。

        不无界扫描、不静默改码；扫描预算用尽时如实报告 `scan_limited`。
        """
        if not pattern or len(pattern) > 512:
            raise WorkspaceError("invalid_search", "pattern 必须非空且不超过 512 字符")
        bounded_context = max(0, min(int(context_lines), 10))
        bounded_limit = max(1, min(int(limit), 1000))
        compiled = None
        if regex:
            try:
                compiled = regex_engine.compile(pattern, 0 if case_sensitive else regex_engine.IGNORECASE)
            except regex_engine.error as exc:
                raise WorkspaceError("invalid_search", f"正则非法：{exc}") from exc
        needle = pattern if case_sensitive else pattern.lower()

        targets: list[Path] = []
        enumeration_limited = False
        for item in paths or ["."]:
            target = self.resolve(item, must_exist=True)
            files, limited = self._bounded_files(target) if target.is_dir() else ([target], False)
            targets.extend(files[:max(0, 20000-len(targets))])
            enumeration_limited |= limited or len(targets) >= 20000
            if enumeration_limited:
                break
        targets = sorted(set(targets))
        signature = hashlib.sha256(json.dumps(
            [pattern, paths or ["."], regex, case_sensitive, bounded_context], ensure_ascii=False
        ).encode()).hexdigest()
        resume_path, resume_line = "", 0
        if cursor:
            try:
                saved = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if saved["query"] != signature:
                    raise ValueError("游标不属于当前检索")
                resume_path, resume_line = str(saved["path"]), int(saved["line"])
            except (ValueError, KeyError, TypeError) as exc:
                raise WorkspaceError("invalid_cursor", "检索游标无效或查询已改变") from exc
        matches: list[dict[str, object]] = []
        scanned_files = 0
        scanned_bytes = 0
        scan_limited = enumeration_limited
        skipped: list[dict[str, str]] = []
        last_path, last_line = resume_path, resume_line
        exhausted = True
        for target in targets:
            if scanned_files > 2000 or len(matches) >= bounded_limit * 4:
                scan_limited = True
                exhausted = False
                break
            try:
                relative = target.relative_to(self._root).as_posix()
            except ValueError:
                raise WorkspaceError("path_escape", "检索目标越界") from None
            real = target.resolve(strict=True)
            real_root = self._real_root()
            if real != real_root and real_root not in real.parents:
                raise WorkspaceError("path_escape", "检索目标经软链接指向工作区之外")
            if relative < resume_path:
                continue
            scanned_files += 1
            try:
                size = target.stat().st_size
            except OSError:
                continue
            if size > 2 * 1024 * 1024:
                skipped.append({"path": relative, "reason": "文件超过 2 MiB 检索上限"})
                continue
            try:
                if scanned_bytes + size > 8 * 1024 * 1024:
                    scan_limited, exhausted = True, False
                    break
                with target.open("rb") as handle:
                    raw = handle.read(2 * 1024 * 1024 + 1)
                scanned_bytes += len(raw)
                if len(raw) > 2 * 1024 * 1024:
                    skipped.append({"path": relative, "reason": "读取期间文件增长超过 2 MiB"})
                    continue
            except OSError as exc:
                skipped.append({"path": relative, "reason": f"读取失败：{type(exc).__name__}"})
                continue
            if b"\x00" in raw[:8192]:
                skipped.append({"path": relative, "reason": "二进制文件（含 NUL 字节）"})
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                skipped.append(
                    {"path": relative, "reason": f"不是合法 UTF-8（首个非法位置 {exc.start}）"}
                )
                continue
            lines = text.splitlines()
            for index, line in enumerate(lines):
                if relative == resume_path and index + 1 <= resume_line:
                    continue
                try:
                    hit = (compiled.search(line, timeout=0.01) is not None if compiled is not None
                           else needle in (line if case_sensitive else line.lower()))
                except TimeoutError as exc:
                    raise WorkspaceError("invalid_search", "正则检索超过单行时间边界") from exc
                last_path, last_line = relative, index + 1
                if not hit:
                    continue
                window = lines[
                    max(0, index - bounded_context) : index + bounded_context + 1
                ]
                matches.append(
                    {
                        "path": relative,
                        "line": index + 1,
                        "text": line[:2000],
                        "context": [item[:2000] for item in window],
                    }
                )
                if len(matches) >= bounded_limit:
                    break
            if len(matches) >= bounded_limit:
                break
        if len(matches) >= bounded_limit:
            exhausted = False
        next_cursor = base64.urlsafe_b64encode(json.dumps({
            "query": signature, "path": last_path, "line": last_line
        }).encode()).decode() if not exhausted and (last_path, last_line) != (resume_path, resume_line) else None
        return {
            "action": "search",
            "pattern": pattern,
            "regex": regex,
            "case_sensitive": case_sensitive,
            "matches": matches,
            "count": len(matches),
            "next_cursor": next_cursor,
            "scan_limited": scan_limited,
            "skipped": skipped[:50],
            "note": (
                "扫描预算或结果上限已触发时会明确报告 scan_limited；"
                "二进制/非法编码文件不会被静默改码。"
            ),
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
