"""仅接收版本化包，不运行安装 hook；执行继续走原 operations 台账。"""

from __future__ import annotations

import base64
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from workbench_executor._generated.contracts.skill_package import decode_package, package_hash


def materialize(settings: Any, payload: dict[str, Any]) -> dict[str, Any]:
    if settings.execution_mode != "trusted":
        raise ValueError("skill_dependency_unavailable: trusted 执行模式未启用")
    session = str(UUID(payload["session_id"]))
    digest = str(payload["package_hash"])
    output_key = str(payload["output_key"])
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or not re.fullmatch(r"[a-f0-9]{32}", output_key):
        raise ValueError("skill_package_invalid: 哈希或输出标识无效")
    # 使用与 command runtime 相同的 workspace root/session 归属。
    workspace = settings.workspace_root / session
    workspace.mkdir(parents=True, exist_ok=True)
    if workspace.is_symlink() or not workspace.resolve().is_relative_to(settings.workspace_root.resolve()):
        raise ValueError("skill_path_denied")
    root = workspace / ".rexak" / "skills"
    if root.resolve() != root.absolute():
        raise ValueError("skill_path_denied: 缓存目录不能通过链接跳转")
    root.mkdir(parents=True, exist_ok=True)
    target = root / digest
    cached = target.exists()
    if cached:
        verify_materialized(target, digest)
    else:
        if not payload.get("archive_base64"):
            return {"package_hash": digest, "package_required": True}
        raw = base64.b64decode(payload["archive_base64"], validate=True)
        files = decode_package(raw)
        if package_hash(files) != digest:
            raise ValueError("skill_hash_mismatch")
        stage = Path(tempfile.mkdtemp(prefix=".skill-", dir=root))
        try:
            for name, body in files.items():
                path = stage / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as handle:
                    handle.write(body)
                    handle.flush()
                    os.fsync(handle.fileno())
            try:
                stage.rename(target)
            except OSError:
                if not target.exists():
                    raise
                verify_materialized(target, digest)
        finally:
            # stage 是本函数在已验证根中创建的精确临时目录。
            if stage.exists() and stage.parent == root:
                shutil.rmtree(stage)
    names = {"python": getattr(settings, "resolved_python_interpreter", "python3"),
             "shell": getattr(settings, "shell_path", "bash"), "node": "node"}
    interpreter = shutil.which(names.get(str(payload.get("interpreter")), "__unsupported_skill__"))
    missing = [item for item in payload.get("required_binaries", []) if not isinstance(item, str) or not shutil.which(item)]
    if not interpreter or missing:
        raise ValueError(f"skill_dependency_unavailable: 缺少解释器或依赖 {missing}")
    relative = f"skill-output/{output_key}"
    output = workspace / relative
    if output.resolve() != output.absolute():
        raise ValueError("skill_path_denied: 产物目录不能为链接")
    output.mkdir(parents=True, exist_ok=True)
    return {"package_hash": digest, "cached": cached, "skill_dir": target.as_posix(),
            "output_relative": relative, "output_dir": output.as_posix(),
            "workspace": workspace.as_posix(), "interpreter": interpreter}


def verify_materialized(root: Path, digest: str) -> None:
    if root.is_symlink():
        raise ValueError("skill_path_denied")
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("skill_path_denied")
        if path.is_file():
            # Python 字节码不应写入包，调用端禁用 bytecode。
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    if package_hash(files) != digest:
        raise ValueError("skill_hash_mismatch: 已物化的技能包被修改")
