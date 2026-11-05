"""Full synthetic Main/Pi/A2A/Executor collaboration using a dedicated Linux process."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from agent_workbench.config.settings import REPO_ROOT, get_settings
from agent_workbench.platform_compat import configure_event_loop_policy
from verify_ultra_review_repairs import fixture_executor


async def main():
    cfg = get_settings()
    wheel = REPO_ROOT / "reports/ultra-goal-stage3-review-fixes/wheels/workbench_executor-0.1.0-py3-none-any.whl"
    async with fixture_executor(cfg, wheel) as (isolated, identity):
        env = dict(os.environ)
        for key in ("host", "port", "user", "ssh_key", "known_hosts", "install_root", "remote_port", "local_port", "token", "id"):
            env["WORKBENCH_EXECUTOR_" + key.upper()] = str(getattr(isolated, "executor_" + key))
        env["PI_REAL_EXECUTOR"] = "true"
        env["REXAK_CTF_RECAP"] = str(REPO_ROOT / "reports/ultra-goal-stage5/linux-ctf-recap.json")
        print("Dedicated Linux fixture started; production Executor untouched", flush=True)
        result = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "pytest",
            "tests/integration/test_ultra_stage5.py::test_synthetic_ctf_full_collaboration_and_recap",
            "-q", "--tb=short", "--junitxml=reports/ultra-goal-stage5/linux-ctf.xml"],
            cwd=REPO_ROOT, env=env, check=False, timeout=180)
        Path("reports/ultra-goal-stage5/linux-fixture.txt").write_text(
            f"pid={identity['pid']}\nport={identity['port']}\nroot={identity['root']}\nexit={result.returncode}\n", encoding="utf-8")
        if result.returncode:
            raise RuntimeError("Synthetic Linux closure failed; see pytest evidence")


if __name__ == "__main__":
    configure_event_loop_policy()
    asyncio.run(main())
