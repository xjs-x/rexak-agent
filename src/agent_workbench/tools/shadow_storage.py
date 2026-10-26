"""独立观察文件的有界写入；从不清理旧观察或业务文件。"""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path


class ShadowStorageLimit(RuntimeError):
    pass


class ShadowStorageBusy(RuntimeError):
    pass


class ShadowStore:
    def __init__(self, root: Path, settings) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root, self.settings = root.resolve(), settings

    def write(self, path: Path, value) -> bool:
        data = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8")
        if len(data) > self.settings.history_action_shadow_record_bytes:
            raise ShadowStorageLimit("observation_record_size_limit")
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("shadow_storage_path_boundary")
        self.root.mkdir(parents=True, exist_ok=True)
        lock = self.root / ".writer.lock"
        try:
            lock.mkdir()
        except (FileExistsError, PermissionError) as exc:
            raise ShadowStorageBusy("shadow_writer_busy_skip") from exc
        try:
            if path.exists():
                return False
            total = 0

            # 在独占锁内逐文件核算，不缓存跨进程容量；scandir 复用目录项元数据。
            def files():
                pending: list[Path | str] = [self.root]
                while pending:
                    with os.scandir(pending.pop()) as entries:
                        for entry in entries:
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(entry.path)
                            elif fnmatch.fnmatch(entry.name, "*.json"):
                                yield entry

            for count, old in enumerate(files(), start=1):
                if count >= self.settings.history_action_shadow_storage_files:
                    raise ShadowStorageLimit("session_observation_file_limit")
                total += old.stat().st_size
                if total + len(data) > self.settings.history_action_shadow_storage_bytes:
                    raise ShadowStorageLimit("session_observation_storage_limit")
            if len(data) + total > self.settings.history_action_shadow_storage_bytes:
                raise ShadowStorageLimit("session_observation_storage_limit")
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("xb") as stream:
                    stream.write(data)
            except FileExistsError:
                return False
            return True
        finally:
            lock.rmdir()  # 仅本调用独占创建的空锁目录；不删除任何既有观察文件。
