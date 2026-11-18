"""Prepare the optional local Kali container without changing the VM configuration."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import os
import re
import secrets
import subprocess
import sys
from collections.abc import Iterator
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
VPN_CONFIG = STATE / "ctfy" / "wg0.conf"


def compose_command() -> list[str]:
    files = [*COMPOSE]
    if VPN_CONFIG.is_file():
        files.extend(["-f", str(ROOT / "compose.ctfy-vpn.yaml")])
    return files


def command(*args: str) -> None:
    subprocess.run([*compose_command(), *args], cwd=ROOT, check=True)


def import_vpn_bytes(payload: bytes, *, replace: bool = False) -> bool:
    raw = payload.decode("utf-8")
    if "[Interface]" not in raw or "[Peer]" not in raw or "PrivateKey" not in raw:
        raise RuntimeError("CTFy WireGuard configuration is incomplete")
    if re.search(r"(?im)^\s*(PreUp|PostUp|PreDown|PostDown)\s*=", raw):
        raise RuntimeError("CTFy WireGuard shell hooks require manual review")
    target = STATE / "ctfy" / "wg0.conf"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    if os.name == "nt":
        user = getpass.getuser()
        subprocess.run(
            ["icacls", str(target.parent), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
            check=True, capture_output=True,
        )
    if target.exists() and target.read_bytes() == payload:
        return False
    if target.exists() and not replace:
        raise RuntimeError("CTFy VPN configuration already exists; use --replace to switch files")
    temporary = target.with_suffix(".pending")
    with temporary.open("xb") as stream:
        stream.write(payload)
    try:
        temporary.chmod(0o600)
        if os.name == "nt":
            user = getpass.getuser()
            subprocess.run(
                ["icacls", str(temporary), "/inheritance:r", "/grant:r", f"{user}:F", "*S-1-5-18:R"],
                check=True, capture_output=True,
            )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def import_vpn(source: Path, *, replace: bool = False) -> None:
    if not source.is_file():
        raise RuntimeError("CTFy WireGuard configuration file is missing")
    import_vpn_bytes(source.read_bytes(), replace=replace)


def _active_executor_jobs() -> int:
    code = (
        "import sqlite3;from workbench_executor.processes import pid_is_alive;"
        "c=sqlite3.connect('file:/home/kali/rexak-agent-data/executor-ledger.sqlite3?mode=ro',uri=True);"
        "a=c.execute(\"select count(*) from jobs where state in ('received','starting','running')\").fetchone()[0];"
        "b=c.execute(\"select count(*) from terminals where state in ('starting','running')\").fetchone()[0];"
        "unknown=c.execute(\"select pid,pid_start_ticks,worker_pid,worker_pid_start_ticks "
        "from jobs where state='unknown'\").fetchall();"
        "live=sum(bool((p and pid_is_alive(p,expected_start_ticks=t)) or "
        "(w and pid_is_alive(w,expected_start_ticks=u))) for p,t,w,u in unknown if p or w);"
        "print(a+b+live)"
    )
    result = subprocess.run(
        ["docker", "exec", "workbench-kali", "/opt/rexak-executor/.venv/bin/python", "-c", code],
        check=True, capture_output=True, text=True, timeout=15,
    )
    return int(result.stdout.strip())


def _retain_profile(payload: bytes) -> None:
    retained = STATE / "ctfy" / ("profile-" + hashlib.sha256(payload).hexdigest()[:16] + ".conf")
    if retained.exists():
        if retained.read_bytes() != payload:
            raise RuntimeError("CTFy VPN retained profile fingerprint conflict")
        return
    with retained.open("xb") as stream:
        stream.write(payload)
    retained.chmod(0o600)
    if os.name == "nt":
        user = getpass.getuser()
        subprocess.run(
            ["icacls", str(retained), "/inheritance:r", "/grant:r", f"{user}:F", "*S-1-5-18:R"],
            check=True, capture_output=True,
        )


def _runtime_profile_matches(payload: bytes) -> bool:
    section = ""
    lines = []
    for line in payload.decode("utf-8").splitlines():
        match = re.fullmatch(r"\s*\[([^]]+)\]\s*", line)
        if match:
            section = match.group(1).lower()
        if section != "interface" or not re.match(r"\s*DNS\s*=", line, re.IGNORECASE):
            lines.append(line)
    expected = hashlib.sha256(("\n".join(lines) + "\n").encode("utf-8")).hexdigest()
    result = subprocess.run(
        ["docker", "exec", "workbench-kali", "sha256sum", "/run/rexak-wg/wg0.conf"],
        capture_output=True, text=True, check=False, timeout=10,
    )
    parts = result.stdout.split()
    return result.returncode == 0 and bool(parts) and parts[0] == expected


@contextlib.contextmanager
def _vpn_deployment_lock() -> Iterator[None]:
    directory = STATE / "ctfy"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = directory / "deployment.lock"
    with lock_path.open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("Another CTFy VPN deployment is in progress") from exc
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Another CTFy VPN deployment is in progress") from exc
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def apply_vpn_bytes(payload: bytes) -> bool:
    """Switch an idle Kali container to a platform profile; preserve the prior one."""
    with _vpn_deployment_lock():
        return _apply_vpn_bytes_unlocked(payload)


def _apply_vpn_bytes_unlocked(payload: bytes) -> bool:
    target = STATE / "ctfy" / "wg0.conf"
    previous = target.read_bytes() if target.is_file() else None
    if previous == payload and _runtime_profile_matches(payload):
        return False
    if _active_executor_jobs():
        raise RuntimeError("Kali has executing jobs; CTFy VPN switch deferred")
    # Validate before touching the current profile.
    raw = payload.decode("utf-8")
    if "[Interface]" not in raw or "[Peer]" not in raw or "PrivateKey" not in raw:
        raise RuntimeError("CTFy WireGuard configuration is incomplete")
    if re.search(r"(?im)^\s*(PreUp|PostUp|PreDown|PostDown)\s*=", raw):
        raise RuntimeError("CTFy WireGuard shell hooks require manual review")
    if previous is not None:
        _retain_profile(previous)
    import_vpn_bytes(payload, replace=True)
    try:
        subprocess.run(
            [*compose_command(), "up", "-d", "--force-recreate", "--no-deps", "--wait", "kali"],
            cwd=ROOT, check=True, capture_output=True, timeout=150,
        )
        activate(start=False)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        _retain_profile(payload)
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            import_vpn_bytes(previous, replace=True)
        subprocess.run(
            [*compose_command(), "up", "-d", "--force-recreate", "--no-deps", "--wait", "kali"],
            cwd=ROOT, check=True, capture_output=True, timeout=150,
        )
        raise RuntimeError("CTFy VPN deployment failed; prior Kali configuration restored") from None
    return True


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
    parser.add_argument("action", choices=["configure", "build", "up", "stop", "status", "vpn-import", "vpn-status"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    action = args.action
    if action == "configure":
        configure()
    elif action == "build":
        configure()
        command("build", "kali")
    elif action == "up":
        activate()
    elif action == "stop":
        command("stop", "kali")
    elif action == "vpn-import":
        if args.config is None:
            raise RuntimeError("vpn-import requires --config PATH")
        import_vpn(args.config, replace=args.replace)
    elif action == "vpn-status":
        subprocess.run(["docker", "exec", "workbench-kali", "python3", "/usr/local/lib/rexak_vpn.py"], check=True)
    else:
        command("ps", "kali")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"Kali Docker operation failed: {type(exc).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
