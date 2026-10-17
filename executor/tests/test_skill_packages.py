"""执行岛包落地与完整性；不访问任何目标。"""

import base64
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from workbench_executor._generated.contracts.skill_package import encode_package, package_hash
from workbench_executor.filesystem import WorkspaceError, WorkspaceFileSystem
from workbench_executor.skill_packages import materialize, verify_materialized


def test_full_package_relative_resource_and_cache(tmp_path):
    files = {"SKILL.md": b"fixture", "scripts/main.py": b"from pathlib import Path\nprint((Path(__file__).parent.parent/'assets/value.txt').read_text())\n",
             "assets/value.txt": b"fixture-resource"}
    settings = SimpleNamespace(execution_mode="trusted", workspace_root=tmp_path)
    payload = {"session_id": str(uuid4()), "package_hash": package_hash(files),
               "archive_base64": base64.b64encode(encode_package(files)).decode(),
               "output_key": "a" * 32, "interpreter": "python"}
    first = materialize(settings, payload)
    assert materialize(settings, payload)["cached"]
    run = subprocess.run([sys.executable, first["skill_dir"] + "/scripts/main.py"], cwd=first["output_dir"], capture_output=True, check=True)
    assert b"fixture-resource" in run.stdout
    (tmp_path / payload["session_id"] / ".rexak" / "skills" / payload["package_hash"] / "assets/value.txt").write_bytes(b"changed")
    with pytest.raises(ValueError, match="skill_hash_mismatch"):
        materialize(settings, payload)


def test_disabled_and_invalid_hash_do_not_publish(tmp_path):
    settings = SimpleNamespace(execution_mode="disabled", workspace_root=tmp_path)
    with pytest.raises(ValueError, match="trusted"):
        materialize(settings, {})
    assert not list(tmp_path.iterdir())


def test_missing_package_does_not_verify(tmp_path):
    with pytest.raises(ValueError):
        verify_materialized(tmp_path / "missing", "0" * 64)


def test_file_tools_cannot_edit_materialized_skill_cache(tmp_path):
    fs = WorkspaceFileSystem(tmp_path)
    path = tmp_path / ".rexak" / "skills" / "test" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("original", encoding="utf-8")
    relative = path.relative_to(tmp_path).as_posix()
    version = fs.file_version(relative)
    with pytest.raises(WorkspaceError, match="Skill"):
        fs.write_file(relative, "changed", expected_version=version)
    with pytest.raises(WorkspaceError, match="Skill"):
        fs.edit_file_batch(relative, edits=[{"old_text": "original", "new_text": "changed"}], expected_version=version)
    assert path.read_text() == "original"
