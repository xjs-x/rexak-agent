"""工件存储：内容寻址 + 原子建档 + 元数据后置提交。

目录布局（相对 `WORKBENCH_DATA_ROOT`）：

    artifacts/<sha256[:2]>/<sha256>              原始字节（不可变）
    artifacts/<sha256[:2]>/<sha256>.meta.json    采集元数据旁证（便于离线核对）
    artifacts/views/<sha256>/<view-id>.txt       脱敏可读视图（与原件分开）

**哈希只证明内容一致性，不证明漏洞结论。**
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from agent_workbench.config.settings import Settings, get_settings
from agent_workbench.contracts.enums import ArtifactAvailability, Sensitivity

_CHUNK = 1024 * 1024

#: 需要在可读视图中打码的字段名（大小写不敏感）
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie|set-cookie|"
    r"session[_-]?id|csrf|xsrf|bearer|basic|private[_-]?key)\b"
)
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/=]+")
_KV_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie|"
    r"set-cookie|session[_-]?id|csrf|xsrf|private[_-]?key)"
    r"\s*[=:]\s*([^\s;&\"']+)"
)


class ArtifactError(RuntimeError):
    pass


@dataclass(slots=True)
class StoredArtifact:
    """落盘后的工件描述符（尚未提交数据库）。"""

    relative_key: str
    sha256: str
    byte_size: int
    media_type: str
    captured_at: datetime
    truncated: bool = False
    sensitive: bool = False

    @property
    def availability(self) -> ArtifactAvailability:
        return ArtifactAvailability.AVAILABLE


@dataclass(slots=True)
class ArtifactWriteResult:
    artifact: StoredArtifact
    #: 元数据提交失败时残留的文件（需修复）。**不得**当作可用证据引用。
    orphan: bool = False
    error: str | None = None


class ArtifactStore:
    """基于数据根目录的内容寻址存储。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._root = self._settings.artifact_root
        self._views = self._root / "views"

    @property
    def root(self) -> Path:
        return self._root

    # ---------------- 写入 ----------------

    def write_bytes(
        self,
        data: bytes,
        *,
        media_type: str,
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
        captured_by: str | None = None,
        source_url: str | None = None,
        source_operation: str | None = None,
        truncated: bool = False,
        capture_time: datetime | None = None,
    ) -> StoredArtifact:
        """写入字节。同哈希内容只建档一次，原件不可变。"""
        content = data
        digest = hashlib.sha256(content).hexdigest()
        relative_key = self._key_for(digest)
        target = self._root / relative_key
        target.parent.mkdir(parents=True, exist_ok=True)

        if not target.exists():
            tmp = target.with_name(f".{digest}.{uuid.uuid4().hex}.tmp")
            try:
                with tmp.open("wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    # 同目录硬链接是原子 create-if-absent；不替换并发读写中的目标。
                    os.link(tmp, target)
                except FileExistsError:
                    if not self.verify(relative_key, digest):
                        raise ArtifactError(f"同哈希工件内容不一致：{relative_key}") from None
            finally:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)

        stored = StoredArtifact(
            relative_key=relative_key,
            sha256=digest,
            byte_size=len(content),
            media_type=media_type,
            captured_at=capture_time or datetime.now(UTC),
            truncated=truncated,
            sensitive=sensitivity in {Sensitivity.CONFIDENTIAL, Sensitivity.SECRET},
        )
        self._write_meta(
            stored,
            captured_by=captured_by,
            source_url=source_url,
            source_operation=source_operation,
            sensitivity=sensitivity,
        )
        return stored

    def write_stream(
        self,
        stream: BinaryIO,
        *,
        media_type: str,
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
        captured_by: str | None = None,
        source_url: str | None = None,
        source_operation: str | None = None,
        max_bytes: int | None = None,
    ) -> StoredArtifact:
        """流式写入，边读边算哈希。超过 `max_bytes` 时截断并标记 `truncated`。"""
        tmp_dir = self._settings.tmp_root
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / f"artifact-{uuid.uuid4().hex}.tmp"

        hasher = hashlib.sha256()
        size = 0
        truncated = False
        try:
            with tmp.open("wb") as handle:
                while True:
                    chunk = stream.read(_CHUNK)
                    if not chunk:
                        break
                    if max_bytes is not None and size + len(chunk) > max_bytes:
                        keep = max_bytes - size
                        if keep > 0:
                            handle.write(chunk[:keep])
                            hasher.update(chunk[:keep])
                            size += keep
                        truncated = True
                        break
                    handle.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())

            digest = hasher.hexdigest()
            relative_key = self._key_for(digest)
            target = self._root / relative_key
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(tmp, target)
            except FileExistsError:
                if not self.verify(relative_key, digest):
                    raise ArtifactError(f"同哈希工件内容不一致：{relative_key}") from None
        finally:
            tmp.unlink(missing_ok=True)

        stored = StoredArtifact(
            relative_key=relative_key,
            sha256=digest,
            byte_size=size,
            media_type=media_type,
            captured_at=datetime.now(UTC),
            truncated=truncated,
        )
        self._write_meta(
            stored,
            captured_by=captured_by,
            source_url=source_url,
            source_operation=source_operation,
            sensitivity=sensitivity,
        )
        return stored

    def _key_for(self, digest: str) -> str:
        return f"{digest[:2]}/{digest}"

    def _write_meta(
        self,
        stored: StoredArtifact,
        *,
        captured_by: str | None,
        source_url: str | None,
        source_operation: str | None,
        sensitivity: Sensitivity,
    ) -> None:
        meta_path = (self._root / stored.relative_key).with_suffix(".meta.json")
        payload = {
            "sha256": stored.sha256,
            "byte_size": stored.byte_size,
            "media_type": stored.media_type,
            "captured_at": stored.captured_at.isoformat(),
            "captured_by": captured_by,
            "source_url": source_url,
            "source_operation": source_operation,
            "sensitivity": str(sensitivity),
            "truncated": stored.truncated,
        }
        tmp = meta_path.with_name(f".{meta_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            # 同哈希的并发写入共享首份旁证；逐次元数据由数据库行保存。
            with contextlib.suppress(FileExistsError):
                os.link(tmp, meta_path)
        finally:
            tmp.unlink(missing_ok=True)

    # ---------------- 读取 ----------------

    def resolve(self, relative_key: str) -> Path:
        """把相对键解析为绝对路径，并拒绝越界。"""
        candidate = (self._root / relative_key).resolve()
        root = self._root.resolve()
        if not candidate.is_relative_to(root):
            raise ArtifactError(f"工件键越界：{relative_key}")
        return candidate

    def exists(self, relative_key: str) -> bool:
        try:
            return self.resolve(relative_key).is_file()
        except ArtifactError:
            return False

    def read_bytes(self, relative_key: str, *, offset: int = 0, limit: int | None = None) -> bytes:
        path = self.resolve(relative_key)
        if not path.is_file():
            raise ArtifactError(f"工件文件缺失：{relative_key}")
        with path.open("rb") as handle:
            if offset:
                handle.seek(offset)
            return handle.read(limit) if limit is not None else handle.read()

    def verify(self, relative_key: str, expected_sha256: str) -> bool:
        """核对文件内容哈希。失败即视为不可用证据。"""
        path = self.resolve(relative_key)
        if not path.is_file():
            return False
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                hasher.update(chunk)
        return hasher.hexdigest() == expected_sha256

    # ---------------- 脱敏视图 ----------------

    def render_redacted_view(self, relative_key: str, *, media_type: str) -> str:
        """生成脱敏可读文本。原件保持不变。

        - 非法 UTF-8：不做 GBK/Latin-1 兜底猜测，返回明确编码问题说明与二进制提示；
        - HTML/脚本：**原样返回文本**，由前端负责转义与隔离展示，绝不在应用源域执行。
        """
        raw = self.read_bytes(relative_key)
        if media_type.startswith("text/") or media_type in {
            "application/json",
            "application/xml",
            "application/javascript",
        }:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                return (
                    "[编码问题] 该工件不是合法 UTF-8，未做猜测性解码。\n"
                    f"字节数：{len(raw)}；首个非法位置：{exc.start}\n"
                    "请下载原件按声明编码处理。"
                )
        else:
            return f"[二进制工件] media_type={media_type} 字节数={len(raw)}，请受控下载查看。"

        return redact_text(text)

    def write_view(self, source_key: str, text: str, *, label: str = "redacted") -> str:
        """把脱敏视图单独落盘，返回视图相对键。"""
        view_dir = self._views / Path(source_key).name
        view_dir.mkdir(parents=True, exist_ok=True)
        target = view_dir / f"{label}.txt"
        tmp = target.with_suffix(".txt.tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        tmp.replace(target)
        return str(target.relative_to(self._root))


def redact_text(text: str) -> str:
    """按字段/路径脱敏（§10）。返回的文本仅用于展示与模型摘要。

    **顺序很重要**：必须先处理 `Bearer/Basic <token>` 这种"方案 + 值"的形态，
    否则 `Authorization: Bearer <token>` 会先被键值规则吃掉「Bearer」三个字，
    令牌本身反而留在结果里（这正是本项目单元测试抓到过的泄漏）。
    """
    # 去除会干扰终端/日志的控制字符（保留 \n \t）
    cleaned = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc")
    # 1) 先打掉认证方案后的令牌本体
    masked = _BEARER_PATTERN.sub(lambda m: f"{m.group(1)} <redacted>", cleaned)
    # 2) 再按字段名打码值；保留字段名便于理解结构
    return _KV_PATTERN.sub(lambda m: f"{m.group(1)}=<redacted>", masked)


def contains_secret_marker(text: str) -> bool:
    """判断文本中是否已有敏感字段标记，供流式输出在字段完整前暂缓发布。"""
    return _SENSITIVE_KEY_PATTERN.search(text) is not None


def contains_control_chars(value: str) -> bool:
    return any(ch not in "\n\t" and unicodedata.category(ch) == "Cc" for ch in value)


def build_artifact_store() -> ArtifactStore:
    settings = get_settings()
    settings.ensure_directories()
    return ArtifactStore(settings)
