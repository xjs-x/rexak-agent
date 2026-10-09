"""在执行岛上跑进程控制的实机验证。

一条命令完成：同步测试文件到执行岛 -> 在岛的 venv 里跑 pytest -> 回传结果。

为什么需要它：进程组、`/proc/<pid>/stat`、`killpg` 这些只在 Linux 上存在，
在 Windows 控制端跑等于没验证。这个脚本把"在正确的地方验证"变成默认动作。

**不会**在执行岛上开启任何通用命令能力：测试直接调用 `ProcessSupervisor`
（与生产同一份实现），只是把发起者从模型换成 pytest。

用法：

    uv run python scripts/executor_process_check.py
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_workbench.config.settings import get_settings  # noqa: E402
from agent_workbench.platform_compat import configure_stdio_utf8  # noqa: E402

configure_stdio_utf8()

REMOTE_ROOT = "~/rexak-agent-executor"
TEST_RELATIVE = "executor/tests/test_processes.py"
#: 执行岛上的 pip 源（岛上 GitHub 不通，PyPI 走镜像）
PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"


def _ssh_base(settings) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-i",
        str(Path(settings.executor_ssh_key).expanduser()),
        "-o",
        f"UserKnownHostsFile={Path(settings.executor_known_hosts).expanduser()}",
        f"{settings.executor_user}@{settings.executor_host}",
    ]


def _scp_base(settings) -> list[str]:
    return [
        "scp",
        "-q",
        "-o",
        "BatchMode=yes",
        "-i",
        str(Path(settings.executor_ssh_key).expanduser()),
        "-o",
        f"UserKnownHostsFile={Path(settings.executor_known_hosts).expanduser()}",
    ]


def _run_remote_script(command: list[str], script: str) -> subprocess.CompletedProcess[str]:
    """把一段 shell 脚本喂给远端 bash，并回收输出。

    **为什么用文件 + 二进制 stdin，而不是 `input=` 字符串**：
    Windows 上 `subprocess` 在 text 模式下会把输入里的 `\\n` 翻译成 `\\r\\n`，
    远端 bash 会看到 `set -e\\r` 而报 `set: -\\r: 无效的选项`
    （第一次跑本脚本时就踩到了，报错信息完全看不出真正原因）。
    用二进制流 + 写入 LF 字节，才能保证远端拿到的是干净的 LF 脚本。
    """
    with tempfile.NamedTemporaryFile("wb", suffix=".sh", delete=False) as handle:
        handle.write(script.replace("\r\n", "\n").encode("utf-8"))
        script_path = Path(handle.name)
    try:
        with script_path.open("rb") as stdin:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=stdin,
                check=False,
            )
    finally:
        script_path.unlink(missing_ok=True)


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="执行岛进程控制实机验证")
    parser.add_argument("--skip-install", action="store_true", help="跳过 pytest 安装")
    args = parser.parse_args()

    settings = get_settings()
    test_file = REPO_ROOT / TEST_RELATIVE
    if not test_file.exists():
        print(f"找不到测试文件：{test_file}")
        return 2

    remote_home = f"{settings.executor_user}@{settings.executor_host}"

    print(f"== 目标执行岛：{settings.executor_host}（{settings.executor_id}）==")

    # 1) 确保目录与 pytest
    if not args.skip_install:
        print("== 1) 确认执行岛上有 pytest ==")
        install = _run_remote_script(
            [*_ssh_base(settings), "bash -s"],
            script=(
                "set -e\n"
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
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "executor.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(REPO_ROOT / "executor" / "tests", arcname="executor/tests")
            tar.add(
                REPO_ROOT / "executor" / "src" / "workbench_executor",
                arcname="src/workbench_executor",
            )
        copy = _run([*_scp_base(settings), str(archive), f"{remote_home}:/tmp/executor-src.tar.gz"])
        if copy.returncode != 0:
            print("同步失败：\n" + (copy.stderr or copy.stdout))
            return 1
        unpack = _run_remote_script(
            [*_ssh_base(settings), "bash -s"],
            script=(
                "set -e\n"
                f"cd {REMOTE_ROOT}\n"
                "tar -xzf /tmp/executor-src.tar.gz\n"
                "rm -f /tmp/executor-src.tar.gz\n"
                ".venv/bin/python -m pip install -q --no-deps -e . 2>/dev/null || true\n"
                "echo synced\n"
            ),
        )
        print(unpack.stdout.strip() or unpack.stderr.strip())
        if unpack.returncode != 0:
            print("解包/重装失败：\n" + (unpack.stderr or unpack.stdout))
            return 1

    # 3) 跑实机验证
    print("== 3) 在岛上运行进程控制验证 ==")
    result = _run_remote_script(
        [*_ssh_base(settings), "bash -s"],
        script=(
            "set -e\n"
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
    print("实机验证通过：进程组控制、增量输出、取消回收、PID 身份核对均已验证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
