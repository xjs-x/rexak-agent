"""工件键不能利用同前缀兄弟目录逃出数据根。"""

from __future__ import annotations

import pytest

from agent_workbench.config.settings import Settings
from agent_workbench.evidence.artifacts import ArtifactError, ArtifactStore


def test_artifact_key_rejects_sibling_with_same_prefix(tmp_path) -> None:
    store = ArtifactStore(Settings(data_root=tmp_path))
    sibling = store.root.parent / f"{store.root.name}-extra"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("不可读", encoding="utf-8")
    with pytest.raises(ArtifactError, match="越界"):
        store.read_bytes(f"../{sibling.name}/secret.txt")
