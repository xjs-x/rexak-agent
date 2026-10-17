# 本文件由 scripts/generate_executor_contracts.py 自动生成，禁止手改。
# 源文件：contracts/skill_package.py
# 源 SHA256：cbc8e8b96e81ffe18279d2f6a561f98ee7f94ade08c0cb310739c2b091dd6c6a
# 重新生成：uv run python scripts/generate_executor_contracts.py
"""Skill 包的跨端校验真源；仅依赖标准库，生成执行端副本。"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import PurePosixPath

MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_PACKAGE_FILES = 1000
MAX_FILE_BYTES = 8 * 1024 * 1024
TEXT_SUFFIXES = {".md", ".py", ".sh", ".js", ".ts", ".json", ".yaml", ".yml", ".txt", ".toml"}


def safe_skill_path(value: str) -> str:
    """拒绝跨平台路径歧义、链接逃逸和 Windows 设备路径。"""
    path = PurePosixPath(value)
    if (
        not value or len(value) > 512 or any(char in value for char in '\\:<>"|?*')
        or path.is_absolute() or value != path.as_posix()
        or any(part in {"", ".", ".."} or part.endswith((".", " "))
               or re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?", part)
               or any(ord(c) < 32 for c in part) for part in value.split("/"))
    ):
        raise ValueError(f"skill_path_denied: {value!r}")
    return value


def validate_files(files: dict[str, bytes]) -> dict[str, bytes]:
    if not files or len(files) > MAX_PACKAGE_FILES:
        raise ValueError("skill_package_invalid: 文件数量超限或为空")
    seen: set[str] = set()
    total = 0
    for name, body in files.items():
        safe_skill_path(name)
        if name.casefold() in seen:
            raise ValueError("skill_package_invalid: 大小写冲突")
        seen.add(name.casefold())
        total += len(body)
        if len(body) > MAX_FILE_BYTES or total > MAX_PACKAGE_BYTES:
            raise ValueError("skill_package_invalid: 包或文件过大")
        if PurePosixPath(name).suffix.lower() in TEXT_SUFFIXES:
            if body.startswith(b"\xef\xbb\xbf"):
                raise ValueError(f"skill_package_invalid: {name} 必须为 UTF-8 无 BOM")
            body.decode("utf-8", errors="strict")
    if "SKILL.md" not in files:
        raise ValueError("skill_package_invalid: 缺少 SKILL.md")
    if any(parent.as_posix().casefold() in seen
           for name in files for parent in PurePosixPath(name).parents if parent.as_posix() != "."):
        raise ValueError("skill_package_invalid: 文件与目录路径冲突")
    return files


def package_hash(files: dict[str, bytes]) -> str:
    manifest = [(name, len(body), hashlib.sha256(body).hexdigest())
                for name, body in sorted(validate_files(files).items())]
    return hashlib.sha256(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def encode_package(files: dict[str, bytes]) -> bytes:
    validate_files(files)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in sorted(files.items()):
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, body)
    encoded = output.getvalue()
    if len(encoded) > MAX_PACKAGE_BYTES:
        raise ValueError("skill_package_invalid: 压缩包过大")
    return encoded


def decode_package(data: bytes, *, allow_root: bool = False) -> dict[str, bytes]:
    if len(data) > MAX_PACKAGE_BYTES:
        raise ValueError("skill_package_invalid: 压缩包过大")
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if len(archive.infolist()) > MAX_PACKAGE_FILES:
            raise ValueError("skill_package_invalid: 条目过多")
        total = 0
        for info in archive.infolist():
            safe_skill_path(info.filename.rstrip("/"))
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}):
                raise ValueError("skill_package_invalid: 不允许链接或特殊文件")
            if info.is_dir():
                continue
            total += info.file_size
            if info.file_size > MAX_FILE_BYTES or total > MAX_PACKAGE_BYTES or info.filename in files:
                raise ValueError("skill_package_invalid: 重复文件或大小超限")
            files[info.filename] = archive.read(info)
    if allow_root and "SKILL.md" not in files:
        roots = {name.split("/")[0] for name in files}
        if len(roots) == 1 and all("/" in name for name in files):
            files = {name.split("/", 1)[1]: body for name, body in files.items()}
    return validate_files(files)
