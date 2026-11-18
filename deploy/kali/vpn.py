"""Set up CTFy split DNS and WireGuard inside Kali's network namespace."""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import time
from pathlib import Path

SOURCE = Path("/etc/rexak/ctfy-wg.conf")
RUNTIME = Path("/run/rexak-wg/wg0.conf")
RESOLV = Path("/etc/resolv.conf")


def _prepare_config() -> list[str]:
    raw = SOURCE.read_text(encoding="utf-8")
    section = ""
    dns: list[str] = []
    lines: list[str] = []
    for line in raw.splitlines():
        match = re.fullmatch(r"\s*\[([^]]+)\]\s*", line)
        if match:
            section = match.group(1).lower()
        if section == "interface" and re.match(r"\s*DNS\s*=", line, re.IGNORECASE):
            dns.extend(item.strip() for item in line.split("=", 1)[1].split(","))
        else:
            lines.append(line)
    if not dns:
        raise RuntimeError("CTFy VPN configuration has no DNS server")
    for address in dns:
        ipaddress.ip_address(address)
    if "[Interface]" not in raw or "[Peer]" not in raw:
        raise RuntimeError("CTFy VPN configuration is incomplete")
    RUNTIME.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    RUNTIME.write_text("\n".join(lines) + "\n", encoding="utf-8")
    RUNTIME.chmod(0o600)
    return dns


def _bridge_address() -> str:
    result = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", "eth0"],
        check=True, capture_output=True, text=True,
    )
    match = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/", result.stdout)
    if match is None:
        raise RuntimeError("Kali Docker eth0 has no IPv4 address")
    return match.group(1)


def _restrict_tunnel_ingress() -> None:
    ports = [item.strip() for item in os.environ.get("CTFY_CALLBACK_PORTS", "").split(",") if item.strip()]
    for item in ports:
        if not item.isdecimal() or not 1 <= int(item) <= 65535 or int(item) in {22, 5432, 8791}:
            raise RuntimeError("CTFY_CALLBACK_PORTS contains an invalid or reserved port")
    subprocess.run(
        ["iptables", "-A", "INPUT", "-i", "wg0", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        check=True,
    )
    for item in ports:
        for protocol in ("tcp", "udp"):
            subprocess.run(
                ["iptables", "-A", "INPUT", "-i", "wg0", "-p", protocol, "--dport", item, "-j", "ACCEPT"],
                check=True,
            )
    subprocess.run(["iptables", "-A", "INPUT", "-i", "wg0", "-j", "DROP"], check=True)


def start() -> tuple[subprocess.Popen[bytes] | None, str]:
    bridge = _bridge_address()
    if not SOURCE.is_file():
        return None, bridge
    dns = _prepare_config()
    subprocess.run(["wg-quick", "up", str(RUNTIME)], check=True)
    _restrict_tunnel_ingress()
    # Docker's embedded resolver remains the upstream for ordinary names and
    # Compose service names; only CTFy .internal queries use the tunnel DNS.
    server = next((value for value in dns if ipaddress.ip_address(value).is_private), dns[0])
    process = subprocess.Popen([
        "dnsmasq", "--keep-in-foreground", "--no-resolv", "--bind-interfaces",
        "--listen-address=127.0.0.1", f"--server=/internal/{server}",
        "--server=127.0.0.11", "--cache-size=0", "--port=53",
    ])
    time.sleep(0.3)
    if process.poll() is not None:
        raise RuntimeError("CTFy split DNS failed to start")
    RESOLV.write_text("nameserver 127.0.0.1\noptions ndots:0\n", encoding="ascii")
    print("CTFy VPN interface and split DNS started", flush=True)
    return process, bridge


def status() -> int:
    if not SOURCE.is_file():
        print("CTFy VPN: disabled (configuration not mounted)")
        return 2
    interface = subprocess.run(["wg", "show", "wg0", "latest-handshakes"], capture_output=True, text=True)
    if interface.returncode:
        print("CTFy VPN: interface unavailable")
        return 1
    handshakes = [int(row.split()[-1]) for row in interface.stdout.splitlines() if row.split()]
    latest = max(handshakes, default=0)
    age = int(time.time() - latest) if latest else None
    print(f"CTFy VPN: interface up; latest handshake age={age if age is not None else 'none'}s")
    return 0 if age is not None and age <= 180 else 1


if __name__ == "__main__":
    raise SystemExit(status())
