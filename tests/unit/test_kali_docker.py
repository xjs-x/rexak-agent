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


def test_vpn_import_keeps_existing_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(kali_docker, "STATE", tmp_path)
    source = tmp_path / "source.conf"
    source.write_text("[Interface]\nPrivateKey = example\n[Peer]\n", encoding="utf-8")
    kali_docker.import_vpn(source)
    installed = tmp_path / "ctfy" / "wg0.conf"
    assert installed.read_bytes() == source.read_bytes()
    source.write_text("[Interface]\nPrivateKey = replacement\n[Peer]\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="already exists"):
        kali_docker.import_vpn(source)
    assert b"example" in installed.read_bytes()


def test_idle_profile_switch_preserves_prior_file_and_recreates_kali(tmp_path, monkeypatch):
    monkeypatch.setattr(kali_docker, "STATE", tmp_path)
    monkeypatch.setattr(kali_docker, "VPN_CONFIG", tmp_path / "ctfy" / "wg0.conf")
    monkeypatch.setattr(kali_docker, "_active_executor_jobs", lambda: 0)
    def activate(*, start):
        assert start is False

    monkeypatch.setattr(kali_docker, "activate", activate)
    commands = []

    def run(args, **_kwargs):
        if args[0] != "icacls":
            commands.append(args)

    monkeypatch.setattr(kali_docker.subprocess, "run", run)
    old = b"[Interface]\nPrivateKey = first\n[Peer]\n"
    new = b"[Interface]\nPrivateKey = second\n[Peer]\n"
    assert kali_docker.apply_vpn_bytes(old) is True
    assert kali_docker.apply_vpn_bytes(new) is True
    assert (tmp_path / "ctfy" / "wg0.conf").read_bytes() == new
    retained = list((tmp_path / "ctfy").glob("profile-*.conf"))
    assert len(retained) == 1 and retained[0].read_bytes() == old
    assert all("--force-recreate" in args for args in commands)
