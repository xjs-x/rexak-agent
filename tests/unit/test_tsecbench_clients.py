"""SDK 封装与本机 API 客户端测试（不发真实网络请求）。"""

from __future__ import annotations

import json
import sys
from types import ModuleType
from typing import Any, ClassVar

import httpx
import pytest

from agent_workbench.tsecbench import platform as platform_module
from agent_workbench.tsecbench.config import BenchmarkConfig
from agent_workbench.tsecbench.platform import (
    ChallengeConflict,
    ChallengeNotFound,
    PlatformUnavailable,
    ResourceBusy,
    SdkPlatformClient,
    VpnUnavailable,
)
from agent_workbench.tsecbench.workbench_api import (
    HttpWorkbenchApi,
    WorkbenchApiError,
    WorkbenchUnavailable,
)


def _config() -> BenchmarkConfig:
    return BenchmarkConfig(
        token="token-value",
        base_url="https://benchmark.example.com",
        api_base_url="http://127.0.0.1:8770",
        api_token="api-token",
    )


# --------------------------------------------------------------------------- #
# Fake SDK：行为用字符串开关，异常类必须由**同一个模块**抛出（except 按类对象匹配）
# --------------------------------------------------------------------------- #


def _fake_sdk(**behaviour: Any) -> Any:
    module = ModuleType("tsec_benchmark")

    class TSecError(Exception):
        def __init__(self, message: str, *, code: str | None = None) -> None:
            super().__init__(message)
            self.message = message
            self.code = code

    class TSecConnectionError(TSecError):
        pass

    class VpnCheckError(TSecError):
        def __init__(self, message: str = "vpn failed", detail: Any = None) -> None:
            super().__init__(message, code="vpn_check_failed")
            self.detail = detail or {}

    class TaskNotFound(TSecError):
        pass

    class ChallengeNotFound(TSecError):
        pass

    class InvalidState(TSecError):
        pass

    class DuplicateSubmit(TSecError):
        pass

    class ResourceUnavailable(TSecError):
        pass

    class Challenge:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class StartResult:
        def __init__(self, unique_code: str, container_addr: list[str]) -> None:
            self.unique_code = unique_code
            self.container_addr = container_addr

    class SubmitResult:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class CloseResult:
        def __init__(self, unique_code: str, closed: bool) -> None:
            self.unique_code = unique_code
            self.closed = closed

    class VpnCheckResult:
        def __init__(self, status: str = "ok", client_ip: str = "10.8.0.2") -> None:
            self.status = status
            self.client_ip = client_ip
            self.time = "2026-10-26 12:00:00"
            self.ok = status == "ok"

    class TSecBenchmarkAsync:
        instances: ClassVar[list[Any]] = []

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.calls: list[tuple[str, Any]] = []
            self.closed = False
            TSecBenchmarkAsync.instances.append(self)

        async def __aenter__(self) -> Any:
            if behaviour.get("vpn_error"):
                raise VpnCheckError("boom", {"reason": "network_error"})
            if behaviour.get("enter_error") == "connection":
                raise TSecConnectionError("connect failed")
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            await self.aclose()

        async def aclose(self) -> None:
            self.closed = True

        async def check_vpn(self) -> VpnCheckResult:
            return VpnCheckResult()

        async def list_challenges(self) -> list[Any]:
            rows = behaviour.get("list_rows")
            if rows is not None:
                return rows
            return [
                Challenge(
                    unique_code="web-001",
                    description="desc",
                    difficulty="easy",
                    level=1,
                    total_score=100,
                    flag_count=2,
                    correct_flag_count=1,
                    is_completed=False,
                    container_status="available",
                    container_addr=["10.1.2.3:8080"],
                )
            ]

        async def start_challenge(self, unique_code: str) -> Any:
            self.calls.append(("start", unique_code))
            error = behaviour.get("start_error")
            if error == "resource":
                raise ResourceUnavailable("busy")
            if error == "max_active":
                raise InvalidState("max active reached")
            if error == "not_found":
                raise ChallengeNotFound("nope")
            return StartResult(unique_code, ["10.1.2.3:8080"])

        async def submit_flag(self, unique_code: str, flag: str) -> Any:
            self.calls.append(("submit", (unique_code, flag)))
            if behaviour.get("submit_error") == "duplicate":
                raise DuplicateSubmit("dup")
            return SubmitResult(
                correct=True,
                awarded=50,
                cumulative_score=50,
                correct_flag_count=2,
                total_flag_count=2,
                matched_flag_index=1,
            )

        async def close_challenge(self, unique_code: str) -> Any:
            self.calls.append(("close", unique_code))
            return CloseResult(unique_code, True)

    module.TSecBenchmarkAsync = TSecBenchmarkAsync  # type: ignore[attr-defined]
    for name, value in {
        "TSecError": TSecError,
        "TSecConnectionError": TSecConnectionError,
        "VpnCheckError": VpnCheckError,
        "TaskNotFound": TaskNotFound,
        "ChallengeNotFound": ChallengeNotFound,
        "InvalidState": InvalidState,
        "DuplicateSubmit": DuplicateSubmit,
        "ResourceUnavailable": ResourceUnavailable,
    }.items():
        setattr(module, name, value)
    return module


@pytest.fixture(autouse=True)
def _reset_sdk_cache(monkeypatch):
    monkeypatch.setattr(platform_module, "_sdk_module", None, raising=False)
    yield


async def test_sdk_client_maps_challenge_and_start(monkeypatch):
    sdk = _fake_sdk()
    monkeypatch.setattr(platform_module, "_sdk_module", sdk)

    client = SdkPlatformClient(_config())
    await client.open()
    rows = await client.list_challenges()
    started = await client.start_challenge("web-001")
    await client.close()

    assert rows[0].unique_code == "web-001"
    assert rows[0].container_addr == ("10.1.2.3:8080",)
    assert rows[0].level == "1"
    assert started.addresses == ("10.1.2.3:8080",)
    instance = sdk.TSecBenchmarkAsync.instances[0]
    assert instance.kwargs["base_url"] == "https://benchmark.example.com"
    assert instance.kwargs["token"] == "token-value"
    assert instance.kwargs["auto_check_vpn"] is True
    assert ("start", "web-001") in instance.calls


async def test_sdk_open_translates_vpn_failure(monkeypatch):
    monkeypatch.setattr(platform_module, "_sdk_module", _fake_sdk(vpn_error=True))

    client = SdkPlatformClient(_config())
    with pytest.raises(VpnUnavailable, match="VPN 预检未通过"):
        await client.open()


async def test_sdk_open_reports_missing_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "tsec_benchmark", None)

    client = SdkPlatformClient(_config())
    with pytest.raises(PlatformUnavailable, match="tsec-benchmark"):
        await client.open()


async def test_sdk_maps_duplicate_and_limits(monkeypatch):
    client = SdkPlatformClient(_config())
    monkeypatch.setattr(platform_module, "_sdk_module", _fake_sdk())
    await client.open()
    ok = await client.submit_flag("web-001", "flag{a}")
    assert ok.correct and not ok.duplicate

    duplicate = SdkPlatformClient(_config())
    monkeypatch.setattr(platform_module, "_sdk_module", _fake_sdk(submit_error="duplicate"))
    await duplicate.open()
    result = await duplicate.submit_flag("web-001", "flag{a}")
    assert result.duplicate is True
    assert result.correct is True

    busy = SdkPlatformClient(_config())
    monkeypatch.setattr(platform_module, "_sdk_module", _fake_sdk(start_error="resource"))
    await busy.open()
    with pytest.raises(ResourceBusy, match="resource_unavailable"):
        await busy.start_challenge("web-001")

    conflict = SdkPlatformClient(_config())
    monkeypatch.setattr(platform_module, "_sdk_module", _fake_sdk(start_error="max_active"))
    await conflict.open()
    with pytest.raises(ChallengeConflict, match="名额已满"):
        await conflict.start_challenge("web-001")

    missing = SdkPlatformClient(_config())
    monkeypatch.setattr(platform_module, "_sdk_module", _fake_sdk(start_error="not_found"))
    await missing.open()
    with pytest.raises(ChallengeNotFound):
        await missing.start_challenge("missing")

    for item in (client, duplicate, busy, conflict, missing):
        await item.close()


# --------------------------------------------------------------------------- #
# 本机 API 客户端
# --------------------------------------------------------------------------- #


def _api(handler) -> HttpWorkbenchApi:
    return HttpWorkbenchApi(_config(), transport=httpx.MockTransport(handler))


async def test_api_sends_access_token_and_retries_project_name_conflict():
    seen: list[httpx.Request] = []
    names: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/projects":
            payload = json.loads(request.content)
            names.append(payload["name"])
            if len(names) == 1:
                return httpx.Response(409, json={"code": "conflict", "message": "duplicate"})
            return httpx.Response(201, json={"id": "project-1"})
        raise AssertionError(request.url.path)

    api = _api(handler)
    try:
        project_id = await api.create_project(
            name="TSecBench web-001", target_description="d", scope={"mode": "scoped"}
        )
    finally:
        await api.close()

    assert project_id == "project-1"
    assert names[0] == "TSecBench web-001"
    assert names[1].startswith("TSecBench web-001-")
    assert all(item.headers["X-Workbench-Token"] == "api-token" for item in seen)


async def test_api_maps_auth_failure_and_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": "unauthorized", "message": "no"})

    api = _api(handler)
    try:
        with pytest.raises(WorkbenchApiError, match="本机访问令牌"):
            await api.snapshot("session-1")
    finally:
        await api.close()

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    api = _api(broken)
    try:
        with pytest.raises(WorkbenchUnavailable, match="不可达"):
            await api.health()
    finally:
        await api.close()


async def test_api_reads_paginated_final_answer():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/sessions/s1/conversation":
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {
                            "id": "artifact-1",
                            "role": "assistant",
                            "source": "result",
                            "run_id": "r1",
                            "available": True,
                        }
                    ],
                    "next_cursor": None,
                },
            )
        if path == "/api/sessions/s1/conversation/result/artifact-1/text":
            offset = int(request.url.params.get("offset", "0"))
            if offset == 0:
                return httpx.Response(
                    200, json={"text": "flag{one}", "offset": 0, "next_offset": 9}
                )
            return httpx.Response(200, json={"text": " flag{two}", "offset": 9, "next_offset": None})
        raise AssertionError(path)

    api = _api(handler)
    try:
        text, artifact = await api.read_final_answer("s1", "r1")
    finally:
        await api.close()

    assert text == "flag{one} flag{two}"
    assert artifact == "artifact-1"


async def test_api_create_session_payload_carries_scope_and_policy():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/projects/p1/sessions":
            captured.update(json.loads(request.content))
            return httpx.Response(
                201,
                json={"session": {"id": "s1"}, "run": {"id": "r1", "status": "queued"}},
            )
        raise AssertionError(request.url.path)

    api = _api(handler)
    try:
        data = await api.create_session_and_run(
            project_id="p1",
            title="t",
            objective="o",
            scope={"mode": "scoped", "revision": 1, "rules": []},
            policy={"revision": 1},
        )
    finally:
        await api.close()

    assert data["run"]["id"] == "r1"
    assert captured["scope"]["mode"] == "scoped"
    assert captured["policy"] == {"revision": 1}
    assert captured["initial_objective"] == "o"


async def test_api_cancel_uses_idempotency_key():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/runs/r1/cancel":
            captured["key"] = request.headers["Idempotency-Key"]
            return httpx.Response(202, json={"status": "cancelling"})
        raise AssertionError(request.url.path)

    api = _api(handler)
    try:
        await api.cancel_run("r1")
    finally:
        await api.close()
    assert captured["key"].startswith("tsecbench-cancel:")
