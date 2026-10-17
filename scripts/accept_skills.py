"""已部署 Skill 工程冒烟：只运行自有无害文件 fixture，不调用模型或访问测试目标。"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path
from uuid import uuid4, uuid5

from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.execution_runtime import execution_request_digest
from agent_workbench.contracts.skill_package import encode_package, package_hash
from agent_workbench.execution.client import ExecutorRejected, HttpExecutorClient
from agent_workbench.execution.tunnel import SshTunnel
from agent_workbench.platform_compat import (
    configure_event_loop_policy,
    configure_local_loopback,
    configure_stdio_utf8,
)


async def verify(output: Path) -> None:
    settings = get_settings()
    tunnel = SshTunnel(settings)
    client = HttpExecutorClient(tunnel, settings=settings, expected_executor_id=settings.executor_id)
    session = uuid4()
    run = str(uuid4())
    files = {
        "SKILL.md": b"---\nname: engineering-fixture\ndescription: harmless package fixture\n---\nRead local fixtures only.\n",
        "scripts/helper.py": 'LABEL = "中文资源"\n'.encode(),
        "assets/value.txt": b"fixture-resource",
        "scripts/main.py": (
            b"import json, os, sys\nfrom pathlib import Path\nfrom helper import LABEL\n"
            b"root = Path(os.environ['SKILL_DIR'])\n"
            b"counter = Path('count.txt')\n"
            b"count = int(counter.read_text()) + 1 if counter.exists() else 1\n"
            b"counter.write_text(str(count), encoding='utf-8')\n"
            b"print(json.dumps({'label': LABEL, 'marker': (root/'assets/value.txt').read_text(), "
            b"'count': count, 'argv': sys.argv[1:]}, ensure_ascii=False))\n"
        ),
    }
    digest = package_hash(files)
    payload = {"session_id": str(session), "package_hash": digest,
               "output_key": hashlib.sha256(str(run).encode()).hexdigest()[:32], "interpreter": "python"}
    try:
        await client.start()
        caps = await client.capabilities()
        assert caps.execution_capable("skill_packages"), "执行岛未声明 Skill 包能力"
        probe = await client.execution_skill_package(payload)
        assert probe.get("package_required") is True
        staged = await client.execution_skill_package({**payload,
            "archive_base64": base64.b64encode(encode_package(files)).decode()})
        cached = await client.execution_skill_package(payload)
        assert not staged["cached"] and cached["cached"]
        ack = await client.execution_sync_control({"session_id": str(session), "run_id": run,
            "owner": "skill-engineering-smoke", "owner_generation": 1, "control_version": 1})
        assert ack["applied"]
        arguments = {"argv": [staged["interpreter"], staged["skill_dir"] + "/scripts/main.py", "参数含 空格;$(literal)"],
            "workspace_id": str(session), "cwd": staged["output_relative"],
            "env": {"SKILL_DIR": staged["skill_dir"], "SKILL_OUTPUT_DIR": staged["output_dir"],
                    "PYTHONDONTWRITEBYTECODE": "1"}, "execution_timeout_seconds": 15,
            "skill_ref": {"skill_id": "engineering-fixture", "package_hash": digest, "script": "scripts/main.py"}}
        envelope = {"operation_id": str(uuid5(session, "skill-script")), "session_id": str(session),
            "run_id": run, "tool_call_id": "engineering-script-once", "action": "run_command",
            "arguments": arguments, "request_digest": execution_request_digest("run_command", arguments),
            "owner_generation": 1, "dispatch_control_version": 1,
            "session_control_seq": ack["fence"]["seq"], "wait_ms": 15000}
        first = await client.execution_submit(envelope)
        assert first["process_state"] == "exited" and first["exit_code"] == 0, first
        replay = await client.execution_submit(envelope)
        assert replay["resource_id"] == first["resource_id"] and replay["deduplicated"]
        page = await client.execution_output(first["resource_id"], session_id=str(session))
        data = json.loads(page["text"].strip())
        assert data == {"label": "中文资源", "marker": "fixture-resource", "count": 1,
                        "argv": ["参数含 空格;$(literal)"]}
        try:
            await client.execution_skill_package({**payload, "required_binaries": ["rexak-fixture-missing-binary"]})
        except ExecutorRejected as exc:
            assert exc.status_code == 422
        else:
            raise AssertionError("缺少依赖时未拒绝")
        evidence = {"status": "PASS", "session_id": str(session), "package_hash": digest,
            "checks": ["能力声明", "完整包与相对导入", "中文资源", "独立产物目录", "argv 字面值",
                       "缓存复用", "操作重放复用同一资源", "缺依赖确定拒绝"],
            "first": first, "replay": replay, "stdout": data,
            "real_model_called": False, "target_accessed": False}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Skill 已部署工程冒烟 PASS：8 项检查，无模型调用或目标访问。")
    finally:
        await client.close()
        await tunnel.close()


def main() -> None:
    configure_stdio_utf8()
    configure_event_loop_policy()
    configure_local_loopback()
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(verify(parser.parse_args().output))


if __name__ == "__main__":
    main()
