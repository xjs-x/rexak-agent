"""在执行岛上跑**只能在 Linux 验证**的那部分能力。

一条命令完成：同步测试文件到执行岛 -> 在岛的 venv 里跑 pytest -> 回传结果。

覆盖：进程组控制与增量输出（`test_processes.py`）、
任务工作区文件与软链接逃逸（`test_filesystem_jobs.py`）。

为什么需要它：进程组、`/proc/<pid>/stat`、`killpg` 这些只在 Linux 上存在，
在 Windows 控制端跑等于没验证。这个脚本把"在正确的地方验证"变成默认动作。

**不会**在执行岛上开启任何通用命令能力：测试直接调用 `ProcessSupervisor`
（与生产同一份实现），只是把发起者从模型换成 pytest。

用法：

    uv run python scripts/executor_process_check.py
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import asyncssh

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_workbench.config.settings import get_settings  # noqa: E402
from agent_workbench.platform_compat import configure_stdio_utf8  # noqa: E402

configure_stdio_utf8()

REMOTE_ROOT = "~/rexak-agent-executor"
#: 整个执行岛测试目录：进程控制 + 文件能力都在这里跑
TEST_RELATIVE = "executor/tests"
#: 执行岛上的 pip 源（岛上 GitHub 不通，PyPI 走镜像）
PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"


async def _remote_exec(settings, command: str, data: bytes) -> subprocess.CompletedProcess[str]:
    """使用与正式隧道相同的 AsyncSSH 凭据，不依赖 Windows scp 的 ACL 判定。"""
    key = Path(settings.executor_ssh_key).expanduser()
    known_hosts = Path(settings.executor_known_hosts).expanduser()
    connection = await asyncssh.connect(
        settings.executor_host,
        port=settings.executor_port,
        username=settings.executor_user,
        client_keys=[str(key)],
        known_hosts=str(known_hosts) if known_hosts.exists() else None,
        connect_timeout=15,
    )
    try:
        result = await connection.run(command, input=data, encoding=None, check=False)
        stdout = result.stdout or b""
        stderr = result.stderr or b""
        return subprocess.CompletedProcess(
            args=command,
            returncode=int(result.exit_status or 0),
            stdout=(stdout if isinstance(stdout, bytes) else stdout.encode()).decode(
                "utf-8", errors="replace"
            ),
            stderr=(stderr if isinstance(stderr, bytes) else stderr.encode()).decode(
                "utf-8", errors="replace"
            ),
        )
    finally:
        connection.close()
        await connection.wait_closed()


def _run_remote_script(settings, script: str) -> subprocess.CompletedProcess[str]:
    return asyncio.run(
        _remote_exec(settings, "bash -s", script.replace("\r\n", "\n").encode("utf-8"))
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="执行岛实机验证（进程控制 + 文件能力）")
    parser.add_argument("--skip-install", action="store_true", help="跳过 pytest 安装")
    args = parser.parse_args()

    settings = get_settings()
    test_file = REPO_ROOT / TEST_RELATIVE
    if not test_file.exists():
        print(f"找不到测试文件：{test_file}")
        return 2

    print(f"== 目标执行岛：{settings.executor_host}（{settings.executor_id}）==")

    # 1) 确保目录与 pytest
    if not args.skip_install:
        print("== 1) 确认执行岛上有 pytest ==")
        install = _run_remote_script(
            settings,
            script=(
                "set -euo pipefail\n"
                f"cd {REMOTE_ROOT}\n"
                f"mkdir -p executor/tests\n"
                f"if ! .venv/bin/python -c 'import pytest' 2>/dev/null; then\n"
                f"  .venv/bin/python -m pip install -q -i {PIP_INDEX} pytest\n"
                f"fi\n"
                ".venv/bin/python -c \"import pytest; print('pytest', pytest.__version__)\"\n"
            ),
        )
        print(install.stdout.strip() or install.stderr.strip())
        if install.returncode != 0:
            print("准备环境失败：\n" + (install.stderr or install.stdout))
            return 1

    # 2) 同步测试文件（连同 executor 包源码，保证测的是最新实现）
    print("== 2) 同步测试文件与 executor 源码 ==")
    # 注意 arcname：执行岛上的仓库布局是 ~/rexak-agent-executor/executor/...，
    # 所以必须解到同名相对路径下，否则新文件根本不会生效
    # （第一次跑就因为解错位置，测的还是旧安装里的代码）。
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "executor.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(REPO_ROOT / "executor" / "tests", arcname="executor/tests")
            tar.add(
                REPO_ROOT / "executor" / "src" / "workbench_executor",
                arcname="executor/src/workbench_executor",
            )
            tar.add(REPO_ROOT / "executor" / "pyproject.toml", arcname="executor/pyproject.toml")
            # fixture 目标也一起同步：浏览器用例依赖它新增的登录/表单页面。
            # 注意目标进程加载的是仓库根的 `target_app.py`（见 start-target.sh），
            # 不是 `evals/fixtures/` 下的那份 —— 只同步后者等于没同步。
            fixtures = REPO_ROOT / "evals" / "fixtures"
            if fixtures.exists():
                tar.add(fixtures, arcname="evals/fixtures")
                tar.add(fixtures / "target_app.py", arcname="target_app.py")
        copy = asyncio.run(
            _remote_exec(settings, "cat > /tmp/executor-src.tar.gz", archive.read_bytes())
        )
        if copy.returncode != 0:
            print("同步失败：\n" + (copy.stderr or copy.stdout))
            return 1
        unpack = _run_remote_script(
            settings,
            script=(
                "set -euo pipefail\n"
                f"cd {REMOTE_ROOT}\n"
                "tar -xzf /tmp/executor-src.tar.gz\n"
                "rm -f /tmp/executor-src.tar.gz\n"
                # 用**可编辑安装**：以后同步源码就立即生效，不必每次重装
                ".venv/bin/python -m pip install -q --no-deps -e ./executor\n"
                # 重启目标与 Executor，确保跑的是刚同步的代码（不是旧进程里的）
                "if [ -x ./stop.sh ]; then ./stop.sh >/dev/null 2>&1 || true; fi\n"
                "sleep 1\n"
                "if [ -x ./start.sh ]; then ./start.sh >/dev/null 2>&1 || true; fi\n"
                # 强制重启目标：start-target.sh 发现 pid 存活会直接退出（不会加载新代码）
                'if [ -f target.pid ]; then kill "$(cat target.pid)" 2>/dev/null || true; rm -f target.pid; sleep 1; fi\n'
                "if [ -x ./start-target.sh ]; then ./start-target.sh >/dev/null 2>&1 || true; fi\n"
                "for i in $(seq 1 20); do\n"
                "  if curl -s --max-time 3 -o /dev/null http://127.0.0.1:8899/; then break; fi\n"
                "  sleep 1\n"
                "done\n"
                "for i in $(seq 1 20); do\n"
                "  if curl -s --max-time 3 -o /dev/null http://127.0.0.1:8791/health; then break; fi\n"
                "  sleep 1\n"
                "done\n"
                "echo synced\n"
            ),
        )
        print(unpack.stdout.strip() or unpack.stderr.strip())
        if unpack.returncode != 0:
            print("解包/重装失败：\n" + (unpack.stderr or unpack.stdout))
            return 1

    # 3) 跑实机验证
    print("== 3) 在岛上运行实机验证 ==")
    # `set -o pipefail` 是必须的：否则 `pytest ... | tail` 的退出码取的是 tail 的，
    # 测试失败也会被判成成功（第一次跑就出现了这个假绿）。
    result = _run_remote_script(
        settings,
        script=(
            "set -euo pipefail\n"
            f"cd {REMOTE_ROOT}\n"
            f".venv/bin/python -m pytest {TEST_RELATIVE} -q 2>&1 | tail -40\n"
        ),
    )
    print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip()[-800:])

    if result.returncode != 0:
        print("实机验证未通过。")
        return 1
    print(
        "实机验证通过：进程组控制、增量输出、取消回收、PID 身份核对、"
        "工作区文件与软链接边界、浏览器身份会话及 HTTP 范围测试。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
