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

from agent_workbench.config.deployment import (  # noqa: E402
    remote_path,
    validate_executor_connection,
)
from agent_workbench.config.settings import Settings, get_settings  # noqa: E402
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
            (ROOT / "deploy/executor-requirements.txt", "executor-requirements.txt"),
            (wheel, wheel.name),
        ]
    )
    return files


def bootstrap_command(remote_root: str, *, bootstrap: bool, python: str = "python3") -> str:
    """远端只创建缺失的专用环境；不安装系统包，不要求免密 sudo。"""
    return f"""{shlex.quote(python)} - {shlex.quote(remote_root)} <<'PY'
import json, pathlib, shutil, subprocess, sys
root = pathlib.Path(sys.argv[1])
venv = root / '.venv'
python = venv / 'bin/python'
created = False
browser = shutil.which('chromium') or shutil.which('chromium-browser')
if not python.is_file():
    if not {bootstrap!r}:
        raise SystemExit('首次部署请使用 --bootstrap，或运行 setup_workbench.py install')
    if venv.exists():
        raise SystemExit('远端 .venv 不完整；保留目录，请人工检查，不自动覆盖')
    if not (3, 12) <= sys.version_info[:2] < (3, 14):
        raise SystemExit('Kali 需要 Python 3.12 或 3.13；当前 python3 版本不兼容')
    if not browser:
        raise SystemExit('Kali 缺少 Chromium；请先按部署指南安装系统依赖')
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
    created = True
subprocess.run([str(python), '-c', 'import sys; assert (3,12) <= sys.version_info[:2] < (3,14); import pip'], check=True)
print(json.dumps({{'created_venv': created, 'browser_executable': browser}}))
PY"""


async def read_previous_boot_id(
    connection: asyncssh.SSHClientConnection, settings: Settings
) -> str | None:
    """SSH 隧道总能接收本地连接，先在远端区分未监听与 HTTP 协议错误。"""
    probe = (
        "import socket,sys; s=socket.socket(); s.settimeout(2); "
        "print(int(s.connect_ex(('127.0.0.1',int(sys.argv[1])))==0)); s.close()"
    )
    result = await connection.run(
        f"{shlex.quote(settings.executor_python)} -c {shlex.quote(probe)} {settings.executor_remote_port}",
        check=True,
        timeout=10,
    )
    status = str(result.stdout).strip()
    if status == "0":
        return None
    if status != "1":
        raise RuntimeError("远端端口预检返回非法结果，保留现有服务并停止部署")
    listener = await connection.forward_local_port(
        "127.0.0.1", 0, "127.0.0.1", settings.executor_remote_port
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{listener.get_port()}", trust_env=False, timeout=5
        ) as client:
            response = await client.get("/health")
            response.raise_for_status()
            health = response.json()
            if health.get("executor_id") != settings.executor_id:
                raise RuntimeError("远端端口属于其他 Executor，保留该服务并停止部署")
            if health["active_jobs"]:
                raise RuntimeError("执行端仍有活动任务，保留当前进程并停止部署")
            boot_id = health.get("boot_id")
            if not isinstance(boot_id, str) or not boot_id:
                raise RuntimeError("执行端缺少启动身份，保留该服务并停止部署")
            return boot_id
    finally:
        listener.close()


async def deploy(
    wheel: Path, evidence_output: Path | None = None, *, bootstrap: bool = False
) -> None:
    settings = get_settings()
    validate_executor_connection(settings)
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
            remote_root = remote_path(settings.executor_install_root, home)
            data_root = remote_path(settings.remote_data_root, home)
            if data_root == remote_root:
                raise RuntimeError("远端安装目录和数据目录不能相同")
            prepared = await connection.run(
                bootstrap_command(
                    remote_root, bootstrap=bootstrap, python=settings.executor_python
                ),
                check=False,
                timeout=120,
            )
            if prepared.exit_status:
                detail = str(prepared.stderr).replace(settings.executor_token, "<redacted>")
                print(detail, file=sys.stderr)
                raise RuntimeError("Kali 环境准备失败；请检查 Python、venv 和 Chromium 系统依赖")
            preparation = json.loads(str(prepared.stdout))
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
                    executor_env = {
                        "EXECUTOR_EXECUTOR_ID": settings.executor_id,
                        "EXECUTOR_HOST": "127.0.0.1",
                        "EXECUTOR_PORT": str(settings.executor_remote_port),
                        "EXECUTOR_TOKEN": settings.executor_token,
                        "EXECUTOR_DATA_ROOT": data_root,
                        "EXECUTOR_WORKSPACE_ROOT": data_root + "/workspaces",
                    }
                    if preparation["browser_executable"]:
                        executor_env["EXECUTOR_BROWSER_EXECUTABLE"] = preparation[
                            "browser_executable"
                        ]
                    await output.write(
                        "".join(
                            f"{key}={json.dumps(value, ensure_ascii=False)}\n"
                            for key, value in executor_env.items()
                        )
                    )
            else:
                async with sftp.open(env_path, "r") as source:
                    existing = str(await source.read())
                from io import StringIO

                from dotenv import dotenv_values

                values = dotenv_values(stream=StringIO(existing))
                if values.get("EXECUTOR_TOKEN") != settings.executor_token:
                    raise RuntimeError("现有执行端令牌与控制端不一致，保留配置并停止部署")
                if values.get("EXECUTOR_EXECUTOR_ID") != settings.executor_id:
                    raise RuntimeError("现有执行端身份与控制端不一致，保留配置并停止部署")
                if int(values.get("EXECUTOR_PORT") or 8791) != settings.executor_remote_port:
                    raise RuntimeError("现有执行端端口与控制端不一致，保留配置并停止部署")
            await sftp.chmod(env_path, 0o600)

        previous_boot_id = await read_previous_boot_id(connection, settings)
        # 所有安装路径由 SFTP 返回的 home 派生，并逐项按 shell 规则引用。
        root_arg, release_arg = shlex.quote(remote_root), shlex.quote(release)
        index_args = (
            f" --index-url {shlex.quote(settings.executor_pip_index_url)}"
            if settings.executor_pip_index_url
            else ""
        )
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
.venv/bin/python -m pip install{index_args} --require-hashes -r {release_arg}/executor-requirements.txt
if [ -f executor.pid ]; then
    .venv/bin/python executor-control.py stop
fi
if .venv/bin/python -m pip install --no-deps --force-reinstall {release_arg}/{shlex.quote(wheel.name)}; then
    cp -r {release_arg}/executor/. executor/
    cp {release_arg}/rexak-executor.service rexak-executor.service
    cp .env {release_arg}/executor.env.backup
    .venv/bin/python - <<'PY'
from pathlib import Path
p=Path('.env')
lines=[line for line in p.read_text(encoding='utf-8').splitlines() if not line.startswith('EXECUTOR_EXECUTION_MODE=')]
p.write_text(chr(10).join(lines+['EXECUTOR_EXECUTION_MODE=trusted'])+chr(10),encoding='utf-8')
p.chmod(0o600)
PY
    .venv/bin/python executor-control.py start
else
    .venv/bin/python executor-control.py start
    exit 1
fi
.venv/bin/python -m pip check
"""
        print(f"已上传 {len(manifest)} 个文件；更新执行端并检查启动状态。")
        result = await connection.run(
            "bash -s", input=remote_script, check=False, timeout=600 if bootstrap else 120
        )
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
            "print(json.dumps({p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*.py') if '__pycache__' not in p.parts},sort_keys=True))\nPY",
            check=True,
            timeout=20,
        )
        installed = json.loads(str(installed_check.stdout))
        expected = {
            path.relative_to(ROOT / "executor/src/workbench_executor").as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
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
            execution = capabilities.get("details", {}).get("execution", {})
            if execution.get("execution_mode") != "trusted":
                raise RuntimeError("执行底座未加载可信执行配置")
            evidence = {
                "captured_at_utc": datetime.now(UTC).isoformat(),
                "remote_release": release,
                "remote_install_root": remote_root,
                "created_venv": preparation["created_venv"],
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "uploaded_files_verified": len(manifest),
                "installed_source_sha256": installed,
                "previous_boot_id": previous_boot_id,
                "boot_id": health.get("boot_id"),
                "executor_identity_matches": True,
                "active_jobs": health.get("active_jobs"),
                "protocol_version": capabilities.get("protocol_version"),
                "body_modes": capabilities.get("body_modes"),
                "execution": execution,
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
    parser.add_argument("--bootstrap", action="store_true", help="首次部署时创建缺失的 Kali venv")
    args = parser.parse_args()
    if not args.wheel.is_file():
        raise SystemExit("执行端 wheel 不存在")
    asyncio.run(deploy(args.wheel.resolve(), args.evidence_output, bootstrap=args.bootstrap))


if __name__ == "__main__":
    main()
