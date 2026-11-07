"""Kali 隔离验收：只运行本地合成命令，不读取日常凭据或访问靶场。"""

import json
import tempfile
import uuid
from pathlib import Path

from accept_execution_runtime import ExecutorApi, FixtureExecutor, envelope


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="rexak-analysis-accept-"))
    fixture = FixtureExecutor(root=root, token=uuid.uuid4().hex)
    fixture.start()
    api = ExecutorApi(fixture.base_url, fixture.token)
    try:
        session, run = str(uuid.uuid4()), str(uuid.uuid4())
        api.sync(session, run)

        def submit(command: str, files: list[str] | None = None):
            value = envelope(session_id=session, run_id=run, action="run_command",
                             arguments={"command": command, "strict_shell": True,
                                        "expected_files": files or [], "workspace_id": session},
                             wait_ms=10000)
            response = api.submit(value)
            response.raise_for_status()
            return value, response.json()

        _, result = submit("rexak_missing_analysis_tool | cat; echo masked")
        assert result["exit_code"] != 0
        assert "rexak_missing_analysis_tool" in result["stderr_output"]["text"]
        assert "masked" not in (result.get("output") or {}).get("text", "")
        _, result = submit("touch empty; printf ok > valid", ["empty", "missing", "valid"])
        assert result["exit_code"] == 0
        assert result["resource"]["diagnostics"]["output_validation"] == "failed"
        assert result["execution_context"]["cwd"].endswith(session)
        assert api.resource(result["resource_id"])["diagnostics"] == result["resource"]["diagnostics"]
        value, result = submit("printf ok > artifact", ["artifact"])
        assert result["resource"]["diagnostics"]["output_validation"] == "passed"
        replay = api.submit(value)
        replay.raise_for_status()
        assert replay.json()["resource_id"] == result["resource_id"]
        invalid = envelope(session_id=session, run_id=run, action="run_command",
                           arguments={"command": "echo must_not_run", "expected_files": ["../outside"]})
        assert api.submit(invalid).status_code == 422
        report = api.environment()
        assert report["observed_at"]
        assert all(name in report["tools"] or name in report["missing"] for name in ("java", "javac", "javap"))
        response = api.client.get("/execution/v1/environment", params={"tool_names": "rexak_missing_analysis_tool"})
        response.raise_for_status()
        assert "rexak_missing_analysis_tool" in response.json()["missing"]
        assert api.client.get("/execution/v1/environment", params={"tool_names": "../java"}).status_code == 422
        print(json.dumps({"status": "passed", "cases": 6, "evidence_root": str(root)}))
    finally:
        api.close()
        fixture.stop()


if __name__ == "__main__":
    main()
