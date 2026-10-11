"""P8 故障注入：磁盘空间不足时工件写入不能假装成功。"""

from __future__ import annotations

import errno
import hashlib
import io
import os
from pathlib import Path

import pytest

from agent_workbench.config.settings import Settings
from agent_workbench.evidence.artifacts import ArtifactStore


@pytest.mark.parametrize("stream", [False, True])
def test_disk_full_during_content_publish_cleans_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    store = ArtifactStore(Settings(data_root=tmp_path))

    def no_space(_source: Path, _target: Path) -> None:
        raise OSError(errno.ENOSPC, "模拟磁盘空间不足")

    monkeypatch.setattr(os, "link", no_space)
    with pytest.raises(OSError) as error:
        if stream:
            store.write_stream(io.BytesIO(b"evidence"), media_type="text/plain")
        else:
            store.write_bytes(b"evidence", media_type="text/plain")
    assert error.value.errno == errno.ENOSPC
    assert not list(tmp_path.rglob("*.tmp"))
    assert not store.exists(f"{hashlib.sha256(b'evidence').hexdigest()[:2]}/"
                            f"{hashlib.sha256(b'evidence').hexdigest()}")


def test_metadata_publish_failure_can_retry_without_claiming_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(Settings(data_root=tmp_path))
    original_link = os.link
    calls = 0

    def fail_second_link(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.ENOSPC, "模拟元数据写入空间不足")
        original_link(source, target)

    monkeypatch.setattr(os, "link", fail_second_link)
    with pytest.raises(OSError) as error:
        store.write_bytes(b"evidence", media_type="text/plain")
    assert error.value.errno == errno.ENOSPC
    assert not list(tmp_path.rglob("*.tmp"))

    monkeypatch.setattr(os, "link", original_link)
    stored = store.write_bytes(b"evidence", media_type="text/plain")
    assert store.verify(stored.relative_key, stored.sha256)
    assert store.resolve(stored.relative_key).with_suffix(".meta.json").is_file()
