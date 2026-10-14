"""本地回显服务上的 Executor API、编码和真实台账幂等验收。"""

import hashlib
import json
import time
from typing import Any
from uuid import uuid4

import pytest
from tests.integration.test_structured_http import _EchoServer, parse_multipart


@pytest.mark.parametrize("mode", ["form", "multipart"])
def test_structured_body_job_replay_sends_once(client: Any, auth_headers: dict[str, str], mode):
    echo = _EchoServer()
    try:
        caps = client.get("/capabilities", headers=auth_headers).json()
        assert mode in caps["body_modes"]
        payload = {
            "method": "POST",
            "url": echo.url("/submit"),
            "identity_id": str(uuid4()),
            "form_fields" if mode == "form" else "multipart_parts": [
                {"name": "field", "value": "中文值"}
            ],
        }
        request = {
            "operation_id": str(uuid4()),
            "kind": "http_request",
            "request_digest": hashlib.sha256(json.dumps(payload).encode()).hexdigest(),
            "scope_revision": 1,
            "scope": {
                "rules": [
                    {
                        "scheme": "http",
                        "host": "127.0.0.1",
                        "port": echo.port,
                        "allow_private": True,
                    }
                ]
            },
            "payload": payload,
        }
        receipt = client.post("/jobs", headers=auth_headers, json=request)
        assert receipt.status_code in (200, 202), receipt.text
        job_id = receipt.json()["job_id"]
        for _ in range(100):
            snapshot = client.get(f"/jobs/{job_id}", headers=auth_headers).json()
            if snapshot["state"] in {"succeeded", "failed", "unknown"}:
                break
            time.sleep(0.05)
        assert snapshot["state"] == "succeeded", snapshot
        repeated = client.post("/jobs", headers=auth_headers, json=request)
        assert repeated.json()["job_id"] == job_id
        meta = client.get(f"/artifacts/{job_id}/response.meta.json", headers=auth_headers).json()
        hits = [row for row in echo.records if row["path"] == "/submit"]
        assert len(hits) == 1
        assert meta["request_encoding"] == mode
        assert meta["request_body_bytes"] == len(hits[0]["body"])
        assert meta["request_body_sha256"] == hashlib.sha256(hits[0]["body"]).hexdigest()
        if mode == "multipart":
            parts = parse_multipart(hits[0]["body"], hits[0]["content_type"])
            assert parts[0][1].decode("utf-8") == "中文值"
    finally:
        echo.close()
