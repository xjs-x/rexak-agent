"""上传执行端源码和离线 wheel，复用现有凭据与运行数据。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path

import asyncssh
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_workbench.config.settings import get_settings  # noqa: E402
from agent_workbench.platform_compat import (  # noqa: E402
    configure_event_loop_policy,
    configure_local_loopback,
    configure_stdio_utf8,
)


def upload_files(wheel: Path) -> list[tuple[Path, str]]:
    files = [(ROOT / "executor/pyproject.toml", "executor/pyproject.toml")]
    files.extend(
        (path, path.relative_to(ROOT).as_posix())
        for path in sorted((ROOT / "executor/src").rglob("*.py"))
    )
    files.extend(
        [
            (ROOT / "deploy/executor_control.py", "executor-control.py"),
            (ROOT / "deploy/rexak-executor.service", "rexak-executor.service"),
            (wheel, wheel.name),
        ]
    )
    return files


async def deploy(wheel: Path, evidence_output: Path | None = None) -> None:
    settings = get_settings()
    if not settings.executor_known_hosts.exists():
        raise RuntimeError("known_hosts 不存在；拒绝未验证的 SSH 连接")
    if not settings.executor_token or settings.executor_token.startswith("change-me"):
        raise RuntimeError("请先配置执行端令牌")
    from sqlalchemy import text

    from agent_workbench.persistence.db import Database

    database = Database(settings)
    await database.start()
    try:
        async with database.session() as db:
            leases = (
                await db.execute(text("SELECT count(*) FROM app.runs WHERE lease_until > now()"))
            ).scalar_one()
            if leases:
                raise RuntimeError("控制端仍有活动运行租约，保留当前执行端并停止部署")
    finally:
        await database.close()
    async with asyncssh.connect(
        settings.executor_host,
        port=settings.executor_port,
        username=settings.executor_user,
        client_keys=[str(settings.executor_ssh_key)],
        known_hosts=str(settings.executor_known_hosts),
        connect_timeout=15,
    ) as connection:
        async with connection.start_sftp_client() as sftp:
            home = str(await sftp.realpath("."))
            remote_root = home + "/rexak-agent-executor"
            release = remote_root + "/releases/" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            await sftp.makedirs(release, exist_ok=False)
            manifest = {}
            for source, relative in upload_files(wheel):
                target = release + "/" + relative
                await sftp.makedirs(str(Path(target).parent).replace("\\", "/"), exist_ok=True)
                await sftp.put(str(source), target)
                manifest[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
            async with sftp.open(release + "/manifest.json", "w") as output:
                await output.write(json.dumps(manifest, sort_keys=True) + "\n")
            # 仅写执行端自身配置；模型密钥、数据库连接信息不会上传。
            env_path = remote_root + "/.env"
            if not await sftp.exists(env_path):
                async with sftp.open(env_path, "w") as output:
                    await output.write(
                        f"EXECUTOR_EXECUTOR_ID={settings.executor_id}\n"
                        "EXECUTOR_HOST=127.0.0.1\n"
                        f"EXECUTOR_PORT={settings.executor_remote_port}\n"
                        f"EXECUTOR_TOKEN={settings.executor_token}\n"
                        f"EXECUTOR_DATA_ROOT={settings.remote_data_root}\n"
                        f"EXECUTOR_WORKSPACE_ROOT={settings.remote_data_root}/workspaces\n"
                    )
            else:
                async with sftp.open(env_path, "r") as source:
                    existing = str(await source.read())
                from io import StringIO

                from dotenv import dotenv_values

                values = dotenv_values(stream=StringIO(existing))
                if values.get("EXECUTOR_TOKEN") != settings.executor_token:
                    raise RuntimeError("现有执行端令牌与控制端不一致，保留配置并停止部署")
            await sftp.chmod(env_path, 0o600)

        listener = await connection.forward_local_port(
            "127.0.0.1", 0, "127.0.0.1", settings.executor_remote_port
        )
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{listener.get_port()}", trust_env=False, timeout=5
        ) as client:
            previous_boot_id = None
            try:
                response = await client.get("/health")
                response.raise_for_status()
                if response.json()["active_jobs"]:
                    raise RuntimeError("执行端仍有活动任务，保留当前进程并停止部署")
                previous_boot_id = response.json().get("boot_id")
            except httpx.ConnectError:
                pass
        listener.close()
        # 所有安装路径由 SFTP 返回的 home 派生，并逐项按 shell 规则引用。
        root_arg, release_arg = shlex.quote(remote_root), shlex.quote(release)
        remote_script = f"""set -euo pipefail
cd {root_arg}
python3 - {release_arg} <<'PY'
import hashlib, json, pathlib, sys
release=pathlib.Path(sys.argv[1])
for name, digest in json.loads((release/'manifest.json').read_text()).items():
    assert hashlib.sha256((release/name).read_bytes()).hexdigest()==digest, name
print('上传文件哈希验证通过')
PY
cp {release_arg}/executor-control.py executor-control.py
.venv/bin/python executor-control.py stop
if .venv/bin/python -m pip install --no-deps --force-reinstall {release_arg}/{shlex.quote(wheel.name)}; then
    cp -r {release_arg}/executor/. executor/
    cp {release_arg}/rexak-executor.service rexak-executor.service
    .venv/bin/python executor-control.py start
else
    .venv/bin/python executor-control.py start
    exit 1
fi
.venv/bin/python -m pip check
"""
        print(f"已上传 {len(manifest)} 个文件；更新执行端并检查启动状态。")
        result = await connection.run("bash -s", input=remote_script, check=False, timeout=120)
        # 供应商密钥不在远端环境中；仍过滤执行端令牌，防止异常输出泄露。
        print(str(result.stdout).replace(settings.executor_token, "<redacted>"))
        if result.exit_status:
            print(
                str(result.stderr).replace(settings.executor_token, "<redacted>"), file=sys.stderr
            )
            raise RuntimeError("执行端安装失败，请检查上方安装信息")
        installed_check = await connection.run(
            f"cd {root_arg} && .venv/bin/python - <<'PY'\n"
            "import hashlib,json,pathlib,workbench_executor\n"
            "root=pathlib.Path(workbench_executor.__file__).parent\n"
            "print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*.py') if '__pycache__' not in p.parts},sort_keys=True))\nPY",
            check=True,
            timeout=20,
        )
        installed = json.loads(str(installed_check.stdout))
        expected = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (ROOT / "executor/src/workbench_executor").rglob("*.py")
        }
        if installed != expected:
            raise RuntimeError("已安装执行端文件哈希与本机源码不一致，部署验收未通过")
        listener = await connection.forward_local_port(
            "127.0.0.1", 0, "127.0.0.1", settings.executor_remote_port
        )
        try:
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{listener.get_port()}", trust_env=False, timeout=10
            ) as client:
                response = await client.get("/health")
                response.raise_for_status()
                health = response.json()
                response = await client.get(
                    "/capabilities", headers={"Authorization": "Bearer " + settings.executor_token}
                )
                response.raise_for_status()
                capabilities = response.json()
            if (
                health.get("executor_id") != settings.executor_id
                or capabilities.get("executor_id") != settings.executor_id
            ):
                raise RuntimeError("执行端身份不一致，部署验收未通过")
            if previous_boot_id and previous_boot_id == health.get("boot_id"):
                raise RuntimeError("执行端未加载新的启动实例，部署验收未通过")
            if capabilities.get("protocol_version") != 1:
                raise RuntimeError("执行端协议版本不兼容")
            evidence = {
                "captured_at_utc": datetime.now(UTC).isoformat(),
                "remote_release": release,
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "uploaded_files_verified": len(manifest),
                "installed_source_sha256": installed,
                "previous_boot_id": previous_boot_id,
                "boot_id": health.get("boot_id"),
                "executor_identity_matches": True,
                "active_jobs": health.get("active_jobs"),
                "protocol_version": capabilities.get("protocol_version"),
                "body_modes": capabilities.get("body_modes"),
                "pip_check": "passed",
                "real_target_testing_performed": False,
                "real_model_call_performed": False,
            }
            if evidence_output is not None:
                evidence_output.parent.mkdir(parents=True, exist_ok=True)
                evidence_output.write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
            print(f"已安装 {len(installed)} 个文件哈希、启动实例、身份和协议核对通过。")
        finally:
            listener.close()
        print(f"Kali 部署完成：{settings.executor_host}，发布目录 {release}")


def main() -> None:
    configure_stdio_utf8()
    configure_event_loop_policy()
    configure_local_loopback()
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path)
    args = parser.parse_args()
    if not args.wheel.is_file():
        raise SystemExit("执行端 wheel 不存在")
    asyncio.run(deploy(args.wheel.resolve(), args.evidence_output))


if __name__ == "__main__":
    main()
