"""Prepare the optional local Kali container without changing the VM configuration."""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from pathlib import Path

import asyncssh

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "data" / "kali-docker"
COMPOSE = [
    "docker",
    "compose",
    "-f",
    str(ROOT / "compose.yaml"),
    "-f",
    str(ROOT / "compose.kali.yaml"),
]


def command(*args: str) -> None:
    subprocess.run([*COMPOSE, *args], cwd=ROOT, check=True)


def configure() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    private = STATE / "id_ed25519"
    if not private.exists():
        key = asyncssh.generate_private_key("ssh-ed25519")
        with private.open("xb") as stream:
            stream.write(key.export_private_key())
        private.chmod(0o600)
        (STATE / "authorized_keys").write_bytes(key.export_public_key() + b"\n")
    if not (STATE / "authorized_keys").is_file():
        raise RuntimeError("Docker SSH public key missing; existing private key preserved")
    config = STATE / "executor.env"
    if not config.exists():
        with config.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(
                "EXECUTOR_EXECUTOR_ID=kali-docker-01\n"
                f"EXECUTOR_TOKEN={secrets.token_urlsafe(40)}\n"
                "EXECUTOR_DATA_ROOT=/home/kali/rexak-agent-data\n"
                "EXECUTOR_WORKSPACE_ROOT=/home/kali/rexak-agent-data/workspaces\n"
                "EXECUTOR_EXECUTION_MODE=trusted\n"
                "EXECUTOR_BROWSER_EXECUTABLE=/usr/bin/chromium\n"
            )
        config.chmod(0o600)


def activate(*, start: bool = True) -> None:
    configure()
    if start:
        command("up", "-d", "--wait", "kali")
    # Obtain identity from the locally controlled Docker engine, never trust a network scan.
    public = STATE / "host_ed25519.pub"
    subprocess.run(
        ["docker", "cp", "workbench-kali:/var/lib/rexak-ssh/ssh_host_ed25519_key.pub", str(public)],
        check=True,
        capture_output=True,
    )
    known = STATE / "known_hosts"
    expected = "[127.0.0.1]:2222 " + public.read_text(encoding="utf-8").strip() + "\n"
    if known.exists() and known.read_text(encoding="utf-8") != expected:
        raise RuntimeError("Docker SSH host identity changed; existing known_hosts preserved")
    if not known.exists():
        known.write_text(expected, encoding="utf-8", newline="\n")
    values = dict(
        line.split("=", 1)
        for line in (STATE / "executor.env").read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    os.environ.update(
        WORKBENCH_EXECUTOR_HOST="127.0.0.1",
        WORKBENCH_EXECUTOR_PORT="2222",
        WORKBENCH_EXECUTOR_USER="kali",
        WORKBENCH_EXECUTOR_ID="kali-docker-01",
        WORKBENCH_EXECUTOR_SSH_KEY=str(STATE / "id_ed25519"),
        WORKBENCH_EXECUTOR_KNOWN_HOSTS=str(known),
        WORKBENCH_EXECUTOR_INSTALL_ROOT="/opt/rexak-executor",
        WORKBENCH_REMOTE_DATA_ROOT="/home/kali/rexak-agent-data",
        WORKBENCH_EXECUTOR_REMOTE_PORT="8791",
        WORKBENCH_EXECUTOR_LOCAL_PORT="0",
        WORKBENCH_EXECUTOR_TOKEN=values["EXECUTOR_TOKEN"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["configure", "build", "up", "stop", "status"])
    action = parser.parse_args().action
    if action == "configure":
        configure()
    elif action == "build":
        configure()
        command("build", "kali")
    elif action == "up":
        activate()
    elif action == "stop":
        command("stop", "kali")
    else:
        command("ps", "kali")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"Kali Docker operation failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
