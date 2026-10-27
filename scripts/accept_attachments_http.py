"""仅访问 attachment_ui_fixture 创建的临时服务，验证真实 HTTP 上传与刷新归档。"""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx


def main() -> None:
    fixture = json.loads(
        Path("reports/attachment-mvp-v1/ui-fixture.json").read_text(encoding="utf-8")
    )
    if (
        not fixture["database"].startswith("workbench_attachment_ui_")
        or fixture["url"] != "http://127.0.0.1:8777/console/"
    ):
        raise RuntimeError("只允许本轮临时验收服务")
    raw = Path(fixture["file"]).read_bytes()
    with httpx.Client(base_url="http://127.0.0.1:8777", trust_env=False, timeout=30) as client:
        client.get("/console/").raise_for_status()
        headers = {"X-Workbench-CSRF": client.post("/api/session").json()["csrf_token"]}
        project = client.post(
            "/api/projects",
            headers=headers,
            json={"name": "Attachment UI fixture", "target_description": "只读临时附件联调"},
        )
        project.raise_for_status()
        pid = project.json()["id"]
        draft = client.post(
            f"/api/projects/{pid}/sessions",
            headers=headers,
            json={
                "project_id": pid,
                "title": "附件临时验收",
                "initial_objective": "",
                "draft": True,
            },
        )
        draft.raise_for_status()
        assert draft.json()["run"] is None
        sid = draft.json()["session"]["id"]
        response = client.post(
            f"/api/sessions/{sid}/attachments?filename=ui-assets.txt",
            content=raw,
            headers={
                **headers,
                "Idempotency-Key": str(uuid4()),
                "Content-Type": "application/octet-stream",
            },
        )
        response.raise_for_status()
        attachment = response.json()
        assert attachment["sha256"] == hashlib.sha256(raw).hexdigest() and attachment["readable"]
        aid = attachment["attachment_id"]
        message_key = str(uuid4())
        payload = {"content": "分析附件，制定计划。", "attachment_ids": [aid]}
        receipt = client.post(
            f"/api/sessions/{sid}/messages",
            json=payload,
            headers={**headers, "Idempotency-Key": message_key},
        )
        receipt.raise_for_status()
        assert receipt.status_code == 202
        text = client.get(f"/api/sessions/{sid}/attachments/{aid}/text").json()
        assert "example.invalid" in text["text"]
        downloaded = client.get(f"/api/sessions/{sid}/attachments/{aid}/content")
        assert downloaded.content == raw
        client.cookies.clear()
        client.post("/api/session").raise_for_status()
        history = client.get(f"/api/sessions/{sid}/conversation").json()
        assert history["entries"][0]["attachments"][0]["attachment_id"] == aid
    evidence = {
        "http_upload": "passed",
        "draft_does_not_start_run": True,
        "sha256_verified": True,
        "message_binding": "passed",
        "download_matches": True,
        "new_cookie_history_reload": "passed",
        "bounded_read_reference": text["reference"],
        "database": fixture["database"],
        "runner_started": False,
        "real_model_calls": False,
        "browser_clicks": "NOT_VERIFIED_URL_POLICY_BLOCK",
    }
    Path("reports/attachment-mvp-v1/http-smoke.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    main()
