"""通用路径、工具版本与失败传播回归；同一文件可在 Kali 独立运行。"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from workbench_executor.command_runtime import CommandRuntime
from workbench_executor.execution_diagnostics import check_outputs
from workbench_executor.execution_environment import _probe_version, discover_environment
from workbench_executor.settings import ExecutorSettings


class AnalysisTests(unittest.TestCase):
    def test_environment_rejects_paths_and_shell_text(self):
        for names in (["../java"], ["java;id"], ["tool"] * 17):
            with self.assertRaises(ValueError):
                discover_environment(ExecutorSettings(token="test"), tool_names=names)

    def test_missing_and_empty_outputs_are_distinct_from_process_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "empty").touch()
            (root / "good").write_bytes(b"ok")
            result = check_outputs({"cwd": folder, "expected_files": ["empty", "missing", "good"]})
            self.assertEqual(result["output_validation"], "failed")
            self.assertEqual([r["state"] for r in result["files"]], ["empty", "missing_or_not_file", "nonempty"])

    def test_no_declared_outputs_does_not_claim_validation(self):
        self.assertEqual(check_outputs({"cwd": "."})["output_validation"], "not_requested")

    @unittest.skipIf(os.name == "nt", "Kali 校验真实符号链接")
    def test_output_symlink_cannot_read_outside_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "work").mkdir()
            (root / "secret").write_bytes(b"private")
            (root / "work/link").symlink_to(root / "secret")
            result = check_outputs({"cwd": str(root / "work"), "expected_files": ["link"]})
            self.assertEqual(result["files"][0]["state"], "outside_workspace")
            self.assertIsNone(result["files"][0]["byte_size"])

    def test_java_style_version_on_stderr(self):
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=b"", stderr=b"java version test\n")):
            self.assertEqual(_probe_version(["java", "-version"]), "java version test")

    def test_failed_version_probe_is_not_a_version(self):
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=1, stdout=b"usage error", stderr=b"")):
            self.assertEqual(_probe_version(["tool", "--version"]), "")

    @unittest.skipIf(os.name == "nt", "在 Kali 验证真实 Bash 进程")
    def test_pipeline_and_missing_command_do_not_reach_trailing_success(self):
        runtime = CommandRuntime(ExecutorSettings(token="test", shell_path="/bin/bash"), None, "test")
        for text in ("false | cat; echo masked", "rexak_missing_analysis_tool; echo masked"):
            argv, _ = runtime._resolve_argv("command", {"command": text, "strict_shell": True}, Path(), None)
            result = subprocess.run(argv, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(b"masked", result.stdout)

    def test_legacy_and_argv_semantics_are_preserved(self):
        runtime = CommandRuntime(ExecutorSettings(token="test", shell_path="/bin/bash"), None, "test")
        argv, _ = runtime._resolve_argv("command", {"command": "false; echo ok"}, Path(), None)
        self.assertEqual(argv, ["/bin/bash", "-c", "false; echo ok"])
        argv, _ = runtime._resolve_argv("command", {"argv": ["program", "a b"], "strict_shell": True}, Path(), None)
        self.assertEqual(argv, ["program", "a b"])


if __name__ == "__main__":
    unittest.main()
