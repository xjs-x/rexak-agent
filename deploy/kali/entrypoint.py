"""Supervise SSHD and the existing non-root Executor; retain disk state only."""

# Linux-only container entrypoint, checked from the Windows control host.
# pyright: reportAttributeAccessIssue=false

import os
import signal
import subprocess
import time
from pathlib import Path

ROOT = Path("/opt/rexak-executor")
children = []
stopping = False


def stop(_signum, _frame):
    global stopping
    stopping = True


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    host_root = Path("/var/lib/rexak-ssh")
    host_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = host_root / "ssh_host_ed25519_key"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    data = Path("/home/kali/rexak-agent-data")
    data.mkdir(parents=True, exist_ok=True)
    os.chown(data, 1000, 1000)
    ssh_home = Path("/home/kali/.ssh")
    if ssh_home.is_symlink():
        raise RuntimeError("Refusing symlinked SSH home")
    ssh_home.mkdir(mode=0o700, exist_ok=True)
    ssh_home.chmod(0o700)
    os.chown(ssh_home, 1000, 1000)
    authorized = ssh_home / "authorized_keys"
    if authorized.is_symlink():
        raise RuntimeError("Refusing symlinked authorized_keys")
    authorized.write_bytes(Path("/etc/rexak/authorized_keys").read_bytes())
    authorized.chmod(0o600)
    os.chown(authorized, 1000, 1000)
    config = ROOT / ".env"
    # This bind mount contains only Executor configuration, never controller secrets.
    config.write_bytes(Path("/etc/rexak/executor.env").read_bytes())
    config.chmod(0o600)
    os.chown(config, 1000, 1000)
    subprocess.run(["/usr/sbin/sshd", "-t"], check=True)
    ssh = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e"])
    children.append(ssh)
    env = dict(os.environ, HOME="/home/kali")
    descriptor = os.open(
        data / "executor-service.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
    )
    log = os.fdopen(descriptor, "ab")
    executor = subprocess.Popen(
        ["runuser", "-u", "kali", "--", str(ROOT / ".venv/bin/python"), "-m", "workbench_executor"],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    children.append(executor)
    try:
        while not stopping and all(child.poll() is None for child in children):
            time.sleep(0.5)
        return 0 if stopping else 1
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=35)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        log.close()


if __name__ == "__main__":
    raise SystemExit(main())
