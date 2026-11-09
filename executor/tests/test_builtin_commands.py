"""Unified commands must not shadow Python/Web tools or leak a venv."""

import os
from pathlib import Path

import pytest
from workbench_executor.builtin_commands import provision
from workbench_executor.processes import minimal_environment
from workbench_executor.settings import ExecutorSettings


def test_append_only_environment_preserves_python_and_worker_paths(tmp_path, monkeypatch):
    directory = tmp_path / "tools/bin"
    directory.mkdir(parents=True)
    original = os.pathsep.join([str(tmp_path / "web"), str(tmp_path / "system")])
    monkeypatch.setenv("PATH", original)
    monkeypatch.setenv("PYTHONPATH", "must-not-inherit")
    monkeypatch.setenv("VIRTUAL_ENV", "must-not-inherit")
    monkeypatch.setenv("PYTHONHOME", "must-not-inherit")
    monkeypatch.setenv("EXECUTOR_TOKEN", "must-not-inherit")
    private = {"HOME": "/fixture/worker/.home", "TMPDIR": "/fixture/worker/.tmp"}
    env = minimal_environment(private, builtin_commands_dir=directory)
    assert env["PATH"].split(os.pathsep) == [*original.split(os.pathsep), str(directory.resolve())]
    assert all(env[k] == v for k, v in private.items())
    assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "EXECUTOR_TOKEN"} & env.keys()
    assert minimal_environment(env, builtin_commands_dir=directory)["PATH"] == env["PATH"]
    override = minimal_environment({"PATH": original}, builtin_commands_dir=directory)
    assert override["PATH"] == env["PATH"]


def test_missing_directory_keeps_legacy_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "original-path")
    before = minimal_environment()
    assert minimal_environment(builtin_commands_dir=tmp_path / "missing") == before
    settings = ExecutorSettings(data_root=tmp_path, token="fixture")
    assert settings.builtin_commands_dir == tmp_path / "tools/bin"


def make_toolchain(root: Path) -> None:
    for name in ("bin/gdb", "bin/checksec", "bin/patchelf", "venv/bin/python3", "venv/bin/ROPgadget"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    gef = root / "sysroot/usr/share/gdb/gef.py"
    gef.parent.mkdir(parents=True)
    gef.write_text("# fixture\n", encoding="utf-8")


def test_provision_has_no_default_python_or_web_alias_and_is_repeatable(tmp_path):
    tools, data = tmp_path / "toolchain with space", tmp_path / "data"
    make_toolchain(tools)
    paths = provision(data, tools)
    assert {p.name for p in paths} == {"gdb", "gef", "checksec", "ROPgadget", "patchelf", "pwn-python"}
    assert provision(data, tools) == paths
    assert str(tools / "venv/bin/python3") in (data / "tools/bin/pwn-python").read_text()
    assert all(p.read_text().endswith(' "$@"\n') for p in paths)


def test_provision_conflict_preserves_existing_commands_before_writing(tmp_path):
    tools, data = tmp_path / "toolchain", tmp_path / "data"
    make_toolchain(tools)
    directory = data / "tools/bin"
    directory.mkdir(parents=True)
    existing = directory / "checksec"
    existing.write_text("existing user command", encoding="utf-8")
    with pytest.raises(ValueError, match="preserved"):
        provision(data, tools)
    assert list(directory.iterdir()) == [existing]
    assert existing.read_text() == "existing user command"
