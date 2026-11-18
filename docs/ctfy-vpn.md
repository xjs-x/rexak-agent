# CTFy WireGuard for Kali Docker

CTFy VPN runs in the Kali container network namespace. The optional Compose overlay adds only `NET_ADMIN` and `/dev/net/tun`; it does not use privileged mode, host networking, or the Docker socket. SSH continues to publish on Windows `127.0.0.1:2222` and listens only on Kali's Docker bridge address. Executor HTTP remains on container loopback.

## Import and start

The supplied `.conf` may be tied to a challenge or instance. Do not assume another challenge uses the same key, tunnel address, peer, or DNS server. A different `.internal` target name alone needs no code change. Keep the original file as provided by CTFy.

From the repository root on Windows:

```powershell
uv run python scripts/kali_docker.py vpn-import --config C:\path\to\ctfy.conf
docker compose -f compose.yaml -f compose.kali.yaml -f compose.ctfy-vpn.yaml build kali
uv run python scripts/kali_docker.py up
uv run python scripts/kali_docker.py vpn-status
```

The import writes `data/kali-docker/ctfy/wg0.conf`, which is ignored by Git. It refuses to replace an existing file unless `--replace` is passed. On Windows, the imported file ACL permits the current account and SYSTEM. On Linux, its parent directory is mode 0700 and the file is mode 0600. The container receives it read only. Check that no Executor job or terminal is running before changing the config or recreating the container. The original desktop entry remains `start-workbench.cmd`; it selects the VPN overlay automatically when the imported file exists.

The official config's `AllowedIPs` controls the tunnel routes. Its `DNS` entry is used for `.internal` names by a local split DNS resolver; other names still use Docker DNS. The runtime copy under `/run/rexak-wg` removes only the `DNS` line to prevent `wg-quick` from replacing Docker DNS globally. The private key is never placed in the image or command line.

## Automatic handoff after CTFy starts an instance

When Main calls the existing `ctfy_call_tool` with `start_instance`, the trusted control host checks whether the returned surface requires VPN. The platform sometimes returns an empty `surface.vpn.config_url`; in that case the control host reads `get_instance(instance_id)` and uses `info.vpn_endpoint.config_url`. It accepts only the same-origin HTTPS `/api/v1/instances/<id>/vpn-config` endpoint, downloads the profile with the control host's CTFy token, and never sends the profile or token to Pi, Kali, or the model.

If the profile differs from the installed one, the control host checks the Executor ledger for running jobs and terminals, retains the old profile under the ignored private directory, imports the new file, and force-recreates only Kali. A deployment failure attempts to restore the old profile and container. The platform `start_instance` action is never replayed to recover a VPN error. The tool result includes `vpn_setup.status`: `ready`, `handshake_pending`, or `failed`; the model must wait for `ready` before interpreting target failures. No new LLM VPN tool or Main/Pi/A2A protocol was added. Only one Kali tunnel profile can be active at a time; a concurrent executing job defers switching.

On 2026-11-13, two user-opened instances were reported `ready` by CTFy. Their authenticated profile downloads and automatic control-host handoff were exercised in sequence without starting another instance. Kali loaded both profiles, but neither peer returned a WireGuard handshake. Target DNS and traffic therefore remain unverified; `vpn_setup.status=handshake_pending` is the expected result for this network state. This is not a successful VPN acceptance. See `reports/ctfy-vpn-auto-20261113.md` for the checks and remaining network diagnosis.

## Health and callback ports

The VPN overlay's Docker health check requires Executor HTTP health and an established WireGuard interface. `vpn-status` separately requires a handshake no older than 180 seconds and reports its age without revealing keys. The general desktop launcher does not require a recent competition handshake: an old profile or inactive challenge may have none. For an active challenge, run `vpn-status` and target checks before interpreting DNS or HTTP failures as evidence about the target.

Inbound traffic on `wg0` is denied except established replies and explicitly listed callback ports. By default no unsolicited callback port is allowed. Set `CTFY_CALLBACK_PORTS` to comma-separated numeric ports before recreating the Kali container, for example `18080`; bind the listener to the tunnel address from the config. Do not select SSH, Executor, or PostgreSQL ports. A listener and local socket test do not prove that a challenge can reach `ctfy-callback:<port>`; that requires a safe request from the challenge side.

WireGuard's `PersistentKeepalive` and normal rekeying handle transient connectivity loss. If the interface itself is lost, inspect `docker logs workbench-kali`, `docker exec workbench-kali wg show`, and the `vpn-status` result. After verifying that no real task is executing, re-create only the tunnel with `wg-quick down /run/rexak-wg/wg0.conf` followed by `wg-quick up /run/rexak-wg/wg0.conf`, or restart Kali. A Kali container restart ends active commands, although the SQLite ledger, workspace volume, and SSH host key volume remain. It never requires restarting PostgreSQL.

## Linux VPS

Use the same Compose files and `vpn-import` command on a Linux/amd64 host with Docker Compose, a WireGuard-capable kernel, `/dev/net/tun`, and UDP egress to the peer endpoint. Keep `data/kali-docker` private and back up its credentials alongside the Kali data and host key volumes. Confirm the host's container device and `NET_ADMIN` policy, UDP firewall, route, DNS, handshake, and callback ingress on the actual VPS; a Windows Docker Desktop run does not prove these VPS conditions.
