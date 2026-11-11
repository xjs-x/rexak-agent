"""执行岛目标探针必须区分可达、越界与基础设施错误。"""

from __future__ import annotations

from typing import Any

import pytest

from conftest import target_is_up

SCOPE = {
    "revision": 1,
    "rules": [
        {
            "scheme": "http",
            "host": "127.0.0.1",
            "port": 8899,
            "allow_private": True,
        }
    ],
}


@pytest.mark.skipif(not target_is_up(), reason="本地 fixture 目标未运行")
def test_target_probe_reports_actual_connection(client: Any, auth_headers: dict[str, str]) -> None:
    response = client.post(
        "/target-probe",
        headers=auth_headers,
        json={"url": "http://127.0.0.1:8899/", "scope": SCOPE},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "reachable", result
    assert result["http_status"] == 200
    assert "127.0.0.1" in result["dns_addresses"]
    assert result["proxy_inherited"] is False


def test_target_probe_never_connects_outside_scope(
    client: Any, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/target-probe",
        headers=auth_headers,
        json={"url": "http://example.invalid/", "scope": SCOPE},
    )
    assert response.json()["status"] == "scope_denied"


def test_https_cross_host_mapping_is_reported_unsupported(
    client: Any, auth_headers: dict[str, str]
) -> None:
    scope = {
        "rules": [{"scheme": "https", "host": "127.0.0.1", "port": 9443}],
        "mappings": [
            {
                "from_host": "site.example",
                "from_port": 443,
                "to_host": "127.0.0.1",
                "to_port": 9443,
                "sni": "site.example",
            }
        ],
    }
    response = client.post(
        "/target-probe",
        headers=auth_headers,
        json={"url": "https://site.example/", "scope": scope},
    )
    assert response.json()["status"] == "unsupported"
    assert response.json()["stage"] == "tls_sni"


@pytest.mark.parametrize("connected", [True, False])
def test_non_http_service_uses_tcp_evidence(client, auth_headers, monkeypatch, connected):
    from unittest.mock import AsyncMock, Mock

    import workbench_executor.api as api
    from workbench_executor.http_sessions import HttpSessionRegistry

    monkeypatch.setattr(HttpSessionRegistry, "request", AsyncMock(side_effect=ValueError("binary banner")))
    writer = Mock()
    writer.wait_closed = AsyncMock()
    connect = AsyncMock(return_value=(Mock(), writer)) if connected else AsyncMock(side_effect=ConnectionRefusedError())
    monkeypatch.setattr(api.asyncio, "open_connection", connect)
    result = client.post("/target-probe", headers=auth_headers,
                         json={"url": "http://127.0.0.1:8899/", "scope": SCOPE}).json()
    assert result["status"] == ("reachable" if connected else "unreachable")
    assert result["stage"] == "tcp"
    assert result.get("http_status") is None
    if connected:
        writer.close.assert_called_once()
        writer.write.assert_not_called()
