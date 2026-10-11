"""远端工件必须完整分块下载并验证内容，不能把首块当作证据。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from agent_workbench.tools.remote import _download_complete_artifact


class FakeExecutor:
    def __init__(
        self, data: bytes, *, corrupt_second: bool = False, fail_second_once: bool = False
    ) -> None:
        self.data = data
        self.offsets: list[int] = []
        self.corrupt_second = corrupt_second
        self.fail_second_once = fail_second_once

    async def download_artifact(self, _job_id, _name, *, offset: int, limit: int):
        self.offsets.append(offset)
        if offset and self.fail_second_once:
            self.fail_second_once = False
            raise ConnectionError("模拟同步中断")
        chunk = self.data[offset : offset + limit]
        if self.corrupt_second and offset:
            chunk = b"X" + chunk[1:]
        return chunk, {
            "size": str(len(self.data)),
            "offset": str(offset),
            "sha256": hashlib.sha256(self.data).hexdigest(),
        }


@pytest.mark.asyncio
async def test_download_complete_artifact_reads_all_chunks() -> None:
    data = b"a" * 1_048_576 + b"tail"
    executor = FakeExecutor(data)
    received, headers = await _download_complete_artifact(
        executor, job_id=uuid4(), name="body", limit=len(data) + 100
    )

    assert received == data
    assert executor.offsets == [0, 1_048_576]
    assert headers["sha256"] == hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
async def test_download_complete_artifact_rejects_changed_chunk() -> None:
    data = b"a" * 1_048_576 + b"tail"
    with pytest.raises(ValueError, match="哈希"):
        await _download_complete_artifact(
            FakeExecutor(data, corrupt_second=True),
            job_id=uuid4(),
            name="body",
            limit=len(data) + 100,
        )


@pytest.mark.asyncio
async def test_download_resumes_from_persisted_offset_after_interruption(tmp_path: Path) -> None:
    data = b"a" * 1_048_576 + b"tail"
    executor = FakeExecutor(data, fail_second_once=True)
    job_id = uuid4()

    with pytest.raises(ConnectionError, match="模拟同步中断"):
        await _download_complete_artifact(
            executor,
            job_id=job_id,
            name="response.body",
            limit=len(data) + 100,
            resume_root=tmp_path,
        )
    assert list((tmp_path / ".sync").glob("*.part"))

    received, _headers = await _download_complete_artifact(
        executor,
        job_id=job_id,
        name="response.body",
        limit=len(data) + 100,
        resume_root=tmp_path,
    )
    assert received == data
    assert executor.offsets == [0, 1_048_576, 1_048_576]
    assert not list((tmp_path / ".sync").iterdir())
