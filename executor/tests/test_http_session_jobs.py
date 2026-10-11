"""跨 Executor job 的身份 Cookie 持久化与隔离。"""

from __future__ import annotations

import json
import time
import uuid
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
            "include_subdomains": False,
            "allow_private": True,
        }
    ],
    "mappings": [],
    "denied_metadata_hosts": [],
}

pytestmark = pytest.mark.skipif(not target_is_up(), reason="本地 fixture 目标未运行")


def _request(
    client: Any,
    headers: dict[str, str],
    identity: str,
    url: str,
    scope_override: dict[str, Any] | None = None,
) -> int:
    response = client.post(
        "/jobs",
        headers=headers,
        json={
            "operation_id": str(uuid.uuid4()),
            "kind": "http_request",
            "request_digest": uuid.uuid4().hex,
            "scope_revision": 1,
            "scope": scope_override or SCOPE,
            "payload": {"method": "GET", "url": url, "identity_id": identity},
        },
    )
    assert response.status_code in {200, 202}, response.text
    job_id = response.json()["job_id"]
    for _ in range(100):
        status = client.get(f"/jobs/{job_id}", headers=headers).json()
        if status["state"] in {"succeeded", "failed", "unknown"}:
            break
        time.sleep(0.05)
    assert status["state"] == "succeeded", status
    artifact = client.get(f"/artifacts/{job_id}/response.meta.json", headers=headers)
    assert artifact.status_code == 200
    return int(json.loads(artifact.content)["status_code"])


def test_http_cookie_survives_next_job_without_crossing_identity(
    client: Any, auth_headers: dict[str, str]
) -> None:
    alice = f"alice-{uuid.uuid4().hex[:8]}"
    bob = f"bob-{uuid.uuid4().hex[:8]}"
    target = "http://127.0.0.1:8899"
    assert _request(client, auth_headers, alice, f"{target}/login?user=alice") == 200
    assert _request(client, auth_headers, alice, f"{target}/private/profile") == 200
    assert _request(client, auth_headers, bob, f"{target}/private/profile") == 401


def test_csrf_token_is_refreshed_and_injected_without_model_value(
    client: Any, auth_headers: dict[str, str]
) -> None:
    identity = f"csrf-{uuid.uuid4().hex[:8]}"
    target = "http://127.0.0.1:8899"

    def submit(*, refresh: bool) -> int:
        payload: dict[str, Any] = {
            "method": "POST",
            "url": f"{target}/csrf-submit",
            "identity_id": identity,
            "body": "message=hello",
        }
        if refresh:
            payload.update(
                {
                    "csrf_source_url": f"{target}/csrf-form",
                    "csrf_input_name": "csrf_token",
                    "csrf_destination": "form",
                }
            )
        response = client.post(
            "/jobs",
            headers=auth_headers,
            json={
                "operation_id": str(uuid.uuid4()),
                "kind": "http_request",
                "request_digest": uuid.uuid4().hex,
                "scope_revision": 1,
                "scope": SCOPE,
                "payload": payload,
            },
        )
        assert response.status_code in {200, 202}
        job_id = response.json()["job_id"]
        for _ in range(100):
            status = client.get(f"/jobs/{job_id}", headers=auth_headers).json()
            if status["state"] in {"succeeded", "failed", "unknown"}:
                break
            time.sleep(0.05)
        assert status["state"] == "succeeded", status
        meta = client.get(f"/artifacts/{job_id}/response.meta.json", headers=auth_headers)
        return int(json.loads(meta.content)["status_code"])

    assert submit(refresh=False) == 403
    assert submit(refresh=True) == 200
    assert submit(refresh=True) == 200

    rejected = client.post(
        "/jobs",
        headers=auth_headers,
        json={
            "operation_id": str(uuid.uuid4()),
            "kind": "http_request",
            "request_digest": uuid.uuid4().hex,
            "scope_revision": 1,
            "scope": SCOPE,
            "payload": {
                "method": "POST",
                "url": f"{target}/csrf-submit",
                "identity_id": identity,
                "body": "message=hello",
                "csrf_source_url": f"{target}/csrf-form",
                "csrf_input_name": "missing_field",
            },
        },
    )
    job_id = rejected.json()["job_id"]
    for _ in range(100):
        status = client.get(f"/jobs/{job_id}", headers=auth_headers).json()
        if status["state"] in {"succeeded", "failed", "unknown"}:
            break
        time.sleep(0.05)
    assert status["state"] == "failed"
    assert status["error_code"] == "csrf_unavailable"
