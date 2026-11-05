"""Run all Worker regressions against the same isolated PostgreSQL used by this test session."""

import os
import shutil
import subprocess

import pytest
from sqlalchemy.engine import make_url

from agent_workbench.config.settings import REPO_ROOT

pytestmark = pytest.mark.integration


def test_node_worker_regressions(settings, tmp_path):
    node = shutil.which("node")
    assert node
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC", "USERPROFILE"}}
    env["REXAK_TEST_DATABASE_URL"] = make_url(settings.database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    files = [str(path.relative_to(REPO_ROOT / "worker")) for path in (REPO_ROOT / "worker/test").glob("*.test.ts")]
    path = tmp_path / "node-all.log"
    with path.open("wb") as output:
        # Files share one runtime schema; recovery tests must not fence another file's live tasks.
        result = subprocess.run([node, "--import", "tsx", "--test", "--test-concurrency=1", *files], cwd=REPO_ROOT / "worker",
            env=env, stdout=output, stderr=subprocess.STDOUT, check=False, timeout=120)
    # Explicit output destination is useful for reproducible delivery reports, and never contains credentials.
    if target := os.environ.get("REXAK_NODE_TEST_LOG"):
        shutil.copyfile(path, target)
    assert result.returncode == 0, path.read_text(encoding="utf-8", errors="backslashreplace")
