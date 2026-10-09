"""文件能力的**实机验证**（在执行岛上运行）。

分两层：

1. 直接测 `WorkspaceFileSystem`：软链接逃逸这类用例在 Windows 上会 skip
   （创建符号链接需要额外权限），必须在 Linux 上真正跑一遍；
2. 走 **Executor HTTP 接口**跑一遍完整 job 路径：提交 -> 幂等接收 -> 结果工件 ->
   版本冲突，确认线上链路而不只是单元函数。

环境通过 env 覆盖，不碰执行岛的真实数据根与令牌。
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pytest

# 必须在导入应用之前设置：pydantic-settings 的环境变量优先于 .env
_TMP_DATA = tempfile.mkdtemp(prefix="executor-fs-test-")
os.environ["EXECUTOR_TOKEN"] = "fs-test-token"
os.environ["EXECUTOR_DATA_ROOT"] = _TMP_DATA
os.environ["EXECUTOR_WORKSPACE_ROOT"] = str(Path(_TMP_DATA) / "workspaces")
os.environ["EXECUTOR_NETWORK_ISOLATION"] = "unavailable"

from workbench_executor.filesystem import (  # noqa: E402
    WorkspaceError,
    WorkspaceFileSystem,
    workspace_for,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="符号链接语义与目标平台不同；Windows 上跑等于没验证（用控制端的单测覆盖路径逻辑）",
)

TOKEN_HEADERS = {"Authorization": "Bearer fs-test-token"}


# --------------------------------------------------------------------------- #
# 1) 直接测文件系统实现
# --------------------------------------------------------------------------- #


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    """工作区内指向外部的软链接不能成为逃逸通道。"""
    root = tmp_path / "ws"
    outside = tmp_path / "outside"
    root.mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("不该被读到", encoding="utf-8")
    (root / "escape").symlink_to(outside)

    workspace = WorkspaceFileSystem(root)
    with pytest.raises(WorkspaceError) as excinfo:
        workspace.resolve("escape/secret.txt")
    assert excinfo.value.code == "path_escape"
    with pytest.raises(WorkspaceError):
        workspace.resolve("escape")


def test_cannot_write_through_escaping_symlink(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    outside = tmp_path / "outside"
    root.mkdir(parents=True)
    outside.mkdir()
    (root / "escape").symlink_to(outside)

    workspace = WorkspaceFileSystem(root)
    with pytest.raises(WorkspaceError):
        workspace.write_file("escape/new.txt", "内容")
    assert not (outside / "new.txt").exists(), "写操作不得穿透软链接落到工作区之外"


def test_symlink_inside_workspace_is_allowed(tmp_path: Path) -> None:
    """指向工作区**内部**的软链接是正常用法，不应被误伤。"""
    root = tmp_path / "ws"
    (root / "real").mkdir(parents=True)
    (root / "real" / "a.txt").write_text("hello", encoding="utf-8")
    (root / "alias").symlink_to(root / "real")

    workspace = WorkspaceFileSystem(root)
    resolved = workspace.resolve("alias/a.txt")
    assert resolved == root / "alias" / "a.txt"
    assert workspace.read_file("alias/a.txt").text == "hello"


# --------------------------------------------------------------------------- #
# 2) 走 Executor HTTP 接口跑完整 job 路径
# --------------------------------------------------------------------------- #


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    app_module = importlib.import_module("workbench_executor.api")
    with TestClient(app_module.app) as test_client:
        yield test_client


def _submit(client: Any, payload: dict[str, Any], *, digest: str, **extra: Any) -> dict[str, Any]:
    body = {
        "operation_id": str(uuid.uuid4()),
        "kind": "filesystem",
        "request_digest": digest,
        "scope_revision": 1,
        "scope": {"revision": 1, "rules": [], "mappings": []},
        "payload": payload,
    }
    body.update(extra)
    response = client.post("/jobs", json=body, headers=TOKEN_HEADERS)
    assert response.status_code in {200, 202}, response.text
    return response.json()


def _wait(client: Any, job_id: str, *, attempts: int = 60) -> dict[str, Any]:
    import time

    for _ in range(attempts):
        snapshot = client.get(f"/jobs/{job_id}", headers=TOKEN_HEADERS).json()
        if snapshot["state"] in {"succeeded", "failed", "cancelled", "unknown"}:
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"job 在超时前没有到终态：{snapshot}")


def _result(client: Any, job_id: str) -> dict[str, Any]:
    import json

    response = client.get(f"/artifacts/{job_id}/result.json", headers=TOKEN_HEADERS)
    assert response.status_code == 200, response.text
    return json.loads(response.content.decode("utf-8"))


def test_filesystem_job_write_read_edit_roundtrip(client: Any) -> None:
    workspace_id = str(uuid.uuid4())

    write = _submit(
        client,
        {"action": "write", "workspace_id": workspace_id, "path": "notes/a.txt", "content": "v1\n"},
        digest="d1",
    )
    snapshot = _wait(client, write["job_id"])
    assert snapshot["state"] == "succeeded", snapshot
    payload = _result(client, write["job_id"])
    assert payload["ok"] is True
    version = payload["result"]["version"]

    read = _submit(
        client,
        {"action": "read", "workspace_id": workspace_id, "path": "notes/a.txt"},
        digest="d2",
    )
    payload = _result(client, _wait(client, read["job_id"])["job_id"])
    assert payload["result"]["text"] == "v1\n"
    assert payload["result"]["version"] == version

    edit = _submit(
        client,
        {
            "action": "edit",
            "workspace_id": workspace_id,
            "path": "notes/a.txt",
            "old_text": "v1",
            "new_text": "v2",
            "expected_version": version,
        },
        digest="d3",
    )
    payload = _result(client, _wait(client, edit["job_id"])["job_id"])
    assert payload["ok"] is True
    assert payload["result"]["replacements"] == 1

    listing = _submit(client, {"action": "list", "workspace_id": workspace_id}, digest="d4")
    payload = _result(client, _wait(client, listing["job_id"])["job_id"])
    assert [item["path"] for item in payload["result"]["entries"]] == ["notes"]


def test_filesystem_job_version_conflict_is_deterministic_failure(client: Any) -> None:
    """版本冲突是**确定失败**（不是 unknown）：调用方不该重试。"""
    workspace_id = str(uuid.uuid4())
    first = _submit(
        client,
        {"action": "write", "workspace_id": workspace_id, "path": "a.txt", "content": "one"},
        digest="d1",
    )
    stale_version = _result(client, _wait(client, first["job_id"])["job_id"])["result"]["version"]

    second = _submit(
        client,
        {
            "action": "write",
            "workspace_id": workspace_id,
            "path": "a.txt",
            "content": "two",
            "expected_version": stale_version,
        },
        digest="d2",
    )
    _wait(client, second["job_id"])
    good_version = _result(client, second["job_id"])["result"]["version"]

    conflict = _submit(
        client,
        {
            "action": "write",
            "workspace_id": workspace_id,
            "path": "a.txt",
            "content": "three",
            "expected_version": stale_version,
        },
        digest="d3",
    )
    snapshot = _wait(client, conflict["job_id"])
    assert snapshot["state"] == "failed"
    assert snapshot["error_code"] == "version_conflict"

    payload = _result(client, conflict["job_id"])
    assert payload["ok"] is False
    assert payload["error"]["code"] == "version_conflict"

    # 内容没有被改动
    read = _submit(
        client, {"action": "read", "workspace_id": workspace_id, "path": "a.txt"}, digest="d4"
    )
    assert (
        _result(client, _wait(client, read["job_id"])["job_id"])["result"]["version"]
        == good_version
    )


def test_filesystem_job_rejects_path_escape(client: Any) -> None:
    workspace_id = str(uuid.uuid4())
    for bad in ("../../etc/passwd", "/etc/passwd"):
        job = _submit(
            client,
            {"action": "read", "workspace_id": workspace_id, "path": bad},
            digest=f"d-{bad}",
        )
        snapshot = _wait(client, job["job_id"])
        assert snapshot["state"] == "failed", f"{bad} 应被拒绝，实际 {snapshot}"
        assert snapshot["error_code"] in {"invalid_path", "not_found"}, snapshot


def test_filesystem_job_rejects_non_uuid_workspace(client: Any) -> None:
    job = _submit(
        client,
        {"action": "list", "workspace_id": "../other", "path": ""},
        digest="d-escape-ws",
    )
    snapshot = _wait(client, job["job_id"])
    assert snapshot["state"] == "failed"
    assert snapshot["error_code"] == "invalid_workspace"


def test_filesystem_job_is_idempotent_by_operation_id(client: Any) -> None:
    """同 operation_id + 同 digest 返回原 job，不产生第二次副作用。"""
    workspace_id = str(uuid.uuid4())
    operation_id = str(uuid.uuid4())
    payload = {
        "action": "write",
        "workspace_id": workspace_id,
        "path": "once.txt",
        "content": "only-once",
    }
    body = {
        "operation_id": operation_id,
        "kind": "filesystem",
        "request_digest": "same-digest",
        "scope_revision": 1,
        "scope": {"revision": 1, "rules": [], "mappings": []},
        "payload": payload,
    }
    first = client.post("/jobs", json=body, headers=TOKEN_HEADERS)
    second = client.post("/jobs", json=body, headers=TOKEN_HEADERS)
    assert first.status_code in {200, 202}
    assert second.status_code in {200, 202}
    assert first.json()["job_id"] == second.json()["job_id"]
    assert second.json()["deduplicated"] is True

    # 同 operation_id 换参数 -> 冲突，不重放
    changed = dict(body)
    changed["request_digest"] = "different-digest"
    conflict = client.post("/jobs", json=changed, headers=TOKEN_HEADERS)
    assert conflict.status_code == 409, conflict.text


def test_workspaces_are_isolated_by_id(client: Any) -> None:
    """不同任务的工作区互相看不到对方的文件。"""
    workspace_a = str(uuid.uuid4())
    workspace_b = str(uuid.uuid4())
    write = _submit(
        client,
        {"action": "write", "workspace_id": workspace_a, "path": "secret.txt", "content": "A"},
        digest="wa",
    )
    _wait(client, write["job_id"])

    listing_b = _submit(client, {"action": "list", "workspace_id": workspace_b}, digest="wb")
    payload = _result(client, _wait(client, listing_b["job_id"])["job_id"])
    assert payload["result"]["count"] == 0

    read_b = _submit(
        client,
        {"action": "read", "workspace_id": workspace_b, "path": "secret.txt"},
        digest="wb-read",
    )
    assert _wait(client, read_b["job_id"])["state"] == "failed"


def test_workspace_for_rejects_bad_ids(tmp_path: Path) -> None:
    for bad in ("../..", "a/b", "", "not-a-uuid"):
        with pytest.raises(WorkspaceError):
            workspace_for(tmp_path, bad)
