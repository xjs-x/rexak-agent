"""稳定的已加载代码指纹：不读磁盘源码、不运行模型或Executor。"""

import json
import os
import subprocess
import sys
from pathlib import Path

from agent_workbench.agent.factory import loaded_code_version, loaded_runtime_versions


def test_loaded_fingerprint_is_stable_across_references_and_process_hash_seeds():
    from agent_workbench.domain.decision_memory import select_memory

    before = loaded_code_version()
    held = [
        select_memory.__code__.co_code,
        select_memory.__code__.co_consts,
        select_memory.__code__.co_names,
        select_memory.__code__.co_varnames,
    ]
    assert held and loaded_code_version() == before
    code = "from agent_workbench.agent.factory import loaded_code_version; print(loaded_code_version())"
    for seed in ["1", "42"]:
        child = subprocess.check_output(
            [sys.executable, "-X", "utf8", "-c", code],
            env={**os.environ, "PYTHONHASHSEED": seed},
            text=True,
            encoding="utf-8",
        )
        assert child.strip() == before


def test_reference_sensitive_marshal_defect_is_reproduced_without_code_changes():
    code = """import marshal,json
from agent_workbench.domain.decision_memory import select_memory
a=marshal.dumps(select_memory.__code__)
held=[select_memory.__code__.co_consts,select_memory.__code__.co_names,select_memory.__code__.co_code]
b=marshal.dumps(select_memory.__code__)
print(json.dumps({'changed':a!=b}))"""
    result = subprocess.check_output(
        [sys.executable, "-X", "utf8", "-c", code], text=True, encoding="utf-8"
    )
    assert json.loads(result)["changed"]


def test_memory_implementation_and_active_import_bindings_change_fingerprint(monkeypatch):
    from agent_workbench.agent.middleware import history_assistance as module

    before = loaded_code_version()
    assert any("memory_view" in name for name in loaded_runtime_versions()["components"])
    assert any(
        "HistoryAssistanceMiddleware.awrap_model_call" in name
        for name in loaded_runtime_versions()["components"]
    )
    monkeypatch.setattr(module, "memory_view", lambda *_args, **_kwargs: None)
    assert loaded_code_version() != before


def test_loaded_fingerprint_does_not_read_current_disk(monkeypatch):
    before = loaded_code_version()

    def reject(*args, **kwargs):
        raise AssertionError("fingerprint must use loaded code, not disk")

    monkeypatch.setattr(Path, "read_bytes", reject)
    assert loaded_code_version() == before
