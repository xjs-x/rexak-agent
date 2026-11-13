"""Real SSH/HTTP smoke checks against the dedicated Docker Executor; no model calls."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.accept_execution_runtime import envelope
from scripts.kali_docker import STATE, activate, command


async def check(restart: bool) -> None:
    from agent_workbench.config.settings import Settings
    from agent_workbench.domain.worker_tools import open_executor
    from agent_workbench.execution.client import HttpExecutorClient
    from agent_workbench.execution.tunnel import SshTunnel

    activate(start=False)
    cfg = Settings()
    sid, rid = str(uuid4()), str(uuid4())
    results = {}
    tunnel = SshTunnel(cfg)
    client = HttpExecutorClient(tunnel, settings=cfg, expected_executor_id="kali-docker-01")
    await client.start()
    try:
        caps = await client.capabilities()
        assert caps.command_available and caps.browser_available
        assert (await client.health()).active_jobs == 0, "Use an idle acceptance container"
        results["ssh_http_capabilities"] = "PASS"
        code, output, _ = await tunnel.run_remote_command(
            '/opt/rexak-executor/.venv/bin/python -c "import pathlib,hashlib,json,workbench_executor; '
            "r=pathlib.Path(workbench_executor.__file__).parent; "
            "print(json.dumps({p.relative_to(r).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() "
            "for p in r.rglob('*.py')}))\""
        )
        local_root = Path(__file__).resolve().parents[1] / "executor/src/workbench_executor"
        local = {
            p.relative_to(local_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in local_root.rglob("*.py")
        }
        assert code == 0 and json.loads(output) == local
        results["installed_executor_source_hashes"] = "PASS"
        await client.execution_sync_control(
            {
                "session_id": sid,
                "run_id": rid,
                "owner": "acceptance",
                "owner_generation": 1,
                "control_version": 1,
                "expected_seq": None,
                "revocations": [],
            }
        )

        async def dispatch(action, arguments, wait=0, session=None):
            return await client.execution_submit(
                envelope(
                    session_id=session or sid,
                    run_id=rid,
                    action=action,
                    arguments=arguments,
                    wait_ms=wait,
                )
            )

        async def run(arguments, session=None) -> str:
            receipt = await dispatch("run_command", arguments, 8000, session)
            resource = str(receipt["resource_id"])
            for _ in range(150):
                view = await client.execution_resource(resource)
                if view["state"] == "exited":
                    assert view["exit_code"] == 0, view
                    raw = await client.execution_raw_output(resource, session_id=session or sid)
                    import base64

                    return base64.b64decode(raw["data_base64"]).decode("utf-8")
                await asyncio.sleep(0.2)
            raise AssertionError("Command did not finish")

        assert "shell-ok" in await run({"command": "printf shell-ok; python3 -c 'print(42)'"})
        results["shell_python"] = "PASS"
        assert "gdb" in await run(
            {
                "command": "gdb --version; sqlmap --version; nmap --version; checksec --version; pwn-python -c 'import pwn; print(42)'; ROPgadget --version"
            }
        )
        results["web_pwn_tools"] = "PASS"
        debug = await run(
            {
                "command": "printf 'int main(){return 0;}' > debug.c; gcc -g debug.c -o debug; gdb --batch -ex run ./debug"
            }
        )
        assert "exited normally" in debug, debug
        results["gdb_child_debugging"] = "PASS"
        other = str(uuid4())
        await client.execution_sync_control(
            {
                "session_id": other,
                "run_id": rid,
                "owner": "acceptance",
                "owner_generation": 1,
                "control_version": 1,
                "expected_seq": None,
                "revocations": [],
            }
        )
        outputs = await asyncio.gather(
            run({"command": "echo first > own.txt; sleep 1; cat own.txt"}),
            run({"command": "echo second > own.txt; sleep 1; cat own.txt"}, other),
        )
        assert outputs == ["first\n", "second\n"], outputs
        results["concurrent_workspace_isolation"] = "PASS"
        artifact = STATE / "acceptance-input.txt"
        artifact.write_bytes(b"docker-upload-proof\n")
        args = {
            "attachment_id": str(uuid4()),
            "filename": artifact.name,
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "byte_size": artifact.stat().st_size,
        }
        packet = envelope(
            session_id=sid, run_id=rid, action="materialize_attachment", arguments=args
        )
        receipt = await client.execution_input_file(packet, artifact)
        results["input_file_upload"] = receipt["status"]
        path = receipt["input_file"]["relative_path"]
        assert "docker-upload-proof" in await run({"argv": ["cat", path]})
        results["uploaded_file_readback"] = "PASS"
        server = await dispatch(
            "run_command",
            {"argv": ["python3", "-m", "http.server", "18888", "--bind", "127.0.0.1"]},
        )
        await asyncio.sleep(1)
        try:
            assert "docker-upload-proof" in await run(
                {"argv": ["curl", "--fail", "--silent", f"http://127.0.0.1:18888/{path}"]}
            )
            job = await client.submit(
                {
                    "operation_id": str(uuid4()),
                    "kind": "http_request",
                    "request_digest": hashlib.sha256(sid.encode()).hexdigest(),
                    "scope": {"rules": [{"scheme": "http", "host": "127.0.0.1", "port": 18888}]},
                    "payload": {"url": f"http://127.0.0.1:18888/{path}"},
                }
            )
            for _ in range(100):
                snapshot = await client.get_job(job.job_id)
                if str(snapshot.state) in {"succeeded", "failed", "unknown"}:
                    assert str(snapshot.state) == "succeeded", snapshot
                    break
                await asyncio.sleep(0.1)
            body, _ = await client.download_artifact(job.job_id, "response.body")
            assert body == artifact.read_bytes()
            results["curl_http_artifact_download"] = "PASS"
        finally:
            await dispatch("cancel_resource", {"resource_id": str(server["resource_id"])}, 8000)
        long = await dispatch("run_command", {"command": "sleep 120"})
        await asyncio.sleep(1)
        cancelled = await dispatch(
            "cancel_resource", {"resource_id": str(long["resource_id"])}, 8000
        )
        assert cancelled.get("process_confirmed_exited"), cancelled
        results["cancel_process_group"] = "PASS"
        terminal = await dispatch("terminal_open", {"argv": ["/bin/sh"], "mode": "interactive"})
        tid = str(terminal["terminal_id"])
        await dispatch(
            "terminal_send",
            {
                "terminal_id": tid,
                "text": "echo PTY_DOCKER_OK",
                "submit": True,
                "wait": True,
                "wait_ms": 2000,
            },
        )
        page = await client.execution_output(tid, limit=8192)
        assert "PTY_DOCKER_OK" in page.get("text", ""), page
        await dispatch("terminal_close", {"terminal_id": tid}, 8000)
        results["pty"] = "PASS"
        browser_code = """import asyncio
from workbench_executor.browser import BrowserManager
from workbench_executor.settings import get_executor_settings
async def main():
    manager=BrowserManager(get_executor_settings())
    browser=await manager.ensure_started()
    page=await browser.new_page()
    await page.set_content('<title>DOCKER_BROWSER_OK</title>')
    print(await page.title())
    await manager.close()
asyncio.run(main())
"""
        assert "DOCKER_BROWSER_OK" in await run(
            {"argv": ["/opt/rexak-executor/.venv/bin/python", "-c", browser_code]}
        )
        results["playwright_chromium"] = "PASS"
        assert "proof" in await run(
            {"command": "printf proof > persistence.txt; cat persistence.txt"}
        )
        async with open_executor(cfg) as worker_client:
            assert (await worker_client.capabilities()).executor_id == "kali-docker-01"
        results["worker_executor_channel"] = "PASS"
        if restart:
            long = await dispatch(
                "run_command",
                {
                    "command": "echo started > restart-marker.txt; sleep 120; echo finished > restart-complete.txt"
                },
            )
            await asyncio.sleep(2)
            await client.close()
            await tunnel.close()
            await asyncio.to_thread(command, "restart", "kali")
            await asyncio.to_thread(command, "up", "-d", "--wait", "kali")
            activate(start=False)
            tunnel = SshTunnel(cfg)
            client = HttpExecutorClient(tunnel, settings=cfg, expected_executor_id="kali-docker-01")
            await client.start()
            view = await client.execution_resource(str(long["resource_id"]))
            assert view["state"] == "unknown", view
            body, _ = await client.download_artifact(job.job_id, "response.body")
            assert body == artifact.read_bytes()
            results["artifact_download_after_restart"] = "PASS"
            assert "proof" in await run(
                {
                    "command": "cat persistence.txt; test -f restart-marker.txt; test ! -f restart-complete.txt"
                }
            )
            results["restart_files_hostkey_ledger_no_replay"] = "PASS"
    finally:
        await client.close()
        await tunnel.close()
        (STATE / "acceptance-results.json").write_text(
            json.dumps(results, indent=2), encoding="utf-8"
        )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--restart-container", action="store_true", help="Only use on an idle acceptance container"
    )
    asyncio.run(check(parser.parse_args().restart_container))
