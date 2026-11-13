"""Deployment configuration must preserve VM credentials and pin container identity."""

import pytest
from scripts import kali_docker


def test_configure_retains_existing_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(kali_docker, "STATE", tmp_path)
    kali_docker.configure()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    kali_docker.configure()
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert "WORKBENCH" not in (tmp_path / "executor.env").read_text()


def test_activate_rejects_changed_host_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(kali_docker, "STATE", tmp_path)
    kali_docker.configure()
    (tmp_path / "known_hosts").write_text("existing identity\n")
    (tmp_path / "host_ed25519.pub").write_text("ssh-ed25519 replacement\n")
    monkeypatch.setattr(kali_docker.subprocess, "run", lambda *_a, **_kw: None)
    with pytest.raises(RuntimeError, match="identity changed"):
        kali_docker.activate(start=False)
    assert (tmp_path / "known_hosts").read_text() == "existing identity\n"
