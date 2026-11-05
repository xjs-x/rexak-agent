"""原始附件的固定输入路径及原子发布；不解析或执行输入。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import UUID

from workbench_executor._generated.contracts.execution_runtime import (
    ExecutionOperationEnvelope,
    InputFileArguments,
)
from workbench_executor.filesystem import WorkspaceFileSystem, workspace_for


def destination(settings, envelope: ExecutionOperationEnvelope, value: InputFileArguments) -> Path:
    root = workspace_for(settings.workspace_root, str(UUID(envelope.session_id))).root
    relative = value.relative_path
    if value.worker_workspace_id:
        if str(value.worker_workspace_id) != envelope.delegation_id or envelope.delegation_revision != 1:
            raise ValueError("resource_owner_mismatch: Worker 输入身份不一致")
        relative = f".rexak/workers/{value.worker_workspace_id}/{relative}"
    elif envelope.delegation_id:
        raise ValueError("resource_owner_mismatch: 委派输入必须指定对应 Worker")
    fs = WorkspaceFileSystem(root)
    target = fs.resolve(relative, must_exist=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    fs.resolve(relative, must_exist=False)
    # 输入目录不接受链接，包括指向同一工作区内其他目录的链接。
    current = target
    while current != root:
        if current.is_symlink():
            raise ValueError("invalid_path: 输入路径不得包含符号链接")
        current = current.parent
    return target


def verify(path: Path, value: InputFileArguments) -> None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size != value.byte_size:
        raise ValueError("input_file_mismatch: 文件大小或类型不符")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != value.sha256:
        raise ValueError("input_file_mismatch: SHA-256 不符")


def publish(staged: Path, target: Path, value: InputFileArguments) -> bool:
    """原子创建，不覆盖；同内容重复交付返回原文件。"""
    verify(staged, value)
    try:
        os.link(staged, target)
    except FileExistsError:
        verify(target, value)
        return True
    return False
