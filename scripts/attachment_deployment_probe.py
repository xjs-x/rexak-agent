"""附件交付只读探针：不启动 Run，不修改业务状态或执行岛。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncssh
import httpx
from sqlalchemy import func, select

from agent_workbench.config.deployment import remote_path
from agent_workbench.config.settings import REPO_ROOT, get_settings
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.tables import Base, RunRow
from agent_workbench.platform_compat import configure_event_loop_policy, configure_stdio_utf8


async def main(target: Path) -> None:
    cfg = get_settings()
    evidence: dict[str, Any] = {
        "captured_at": datetime.now(UTC).isoformat(),
        "real_model_calls": False,
        "target_tests": False,
        "env_sha256": hashlib.sha256((REPO_ROOT / ".env").read_bytes()).hexdigest(),
    }
    db = Database(cfg)
    await db.start()
    try:
        async with db.session() as session:
            from sqlalchemy import text

            existing = set(
                (
                    await session.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables WHERE table_schema=:schema"
                        ),
                        {"schema": cfg.app_schema},
                    )
                ).scalars()
            )
            evidence["table_counts"] = {
                table.name: await session.scalar(select(func.count()).select_from(table))
                for table in Base.metadata.sorted_tables
                if table.name in existing
            }
            evidence["nonterminal_runs"] = [
                {
                    "run_id": str(row.id),
                    "status": row.status,
                    "owner_present": bool(row.owner),
                    "lease_until": row.lease_until.isoformat() if row.lease_until else None,
                }
                for row in (
                    await session.execute(
                        select(RunRow).where(
                            RunRow.status.not_in(["completed", "cancelled", "failed"])
                        )
                    )
                ).scalars()
            ]
            rows = evidence.pop("nonterminal_runs")
            evidence["nonterminal_summary"] = {
                "count": len(rows),
                "by_status": dict(Counter(row["status"] for row in rows)),
                "snapshot_sha256": hashlib.sha256(
                    json.dumps(sorted(rows, key=lambda row: row["run_id"]), sort_keys=True).encode()
                ).hexdigest(),
            }
    finally:
        await db.close()
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            evidence["local_http"] = {}
            for route in ("/health", "/console/", "/openapi.json"):
                result = await client.get(f"http://127.0.0.1:{cfg.api_port}{route}")
                evidence["local_http"][route] = result.status_code
                if route == "/openapi.json" and result.status_code == 200:
                    evidence["attachment_api_loaded"] = any(
                        "attachments" in path for path in result.json()["paths"]
                    )
    except httpx.HTTPError as exc:
        evidence["local_error"] = type(exc).__name__
    try:
        async with asyncssh.connect(
            cfg.executor_host,
            port=cfg.executor_port,
            username=cfg.executor_user,
            client_keys=[str(cfg.executor_ssh_key)],
            known_hosts=str(cfg.executor_known_hosts),
            connect_timeout=8,
        ) as connection:
            home = str((await connection.run('printf %s "$HOME"', check=True)).stdout).strip()
            root = remote_path(cfg.executor_install_root, home)
            script = """import hashlib,json,pathlib,workbench_executor
root=pathlib.Path(workbench_executor.__file__).parent
print(json.dumps({p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*.py')},sort_keys=True))
"""
            result = await connection.run(
                f"cd {shlex.quote(root)} && .venv/bin/python - <<'PY'\n{script}PY",
                check=True,
                timeout=15,
            )
            installed = json.loads(str(result.stdout))
            expected = {
                p.relative_to(
                    REPO_ROOT / "executor/src/workbench_executor"
                ).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (REPO_ROOT / "executor/src/workbench_executor").rglob("*.py")
            }
            evidence["executor_installed_hashes_match"] = installed == expected
            evidence["executor_file_count"] = len(installed)
            listener = await connection.forward_local_port(
                "127.0.0.1", 0, "127.0.0.1", cfg.executor_remote_port
            )
            try:
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{listener.get_port()}", trust_env=False, timeout=10
                ) as client:
                    response = await client.get("/health")
                    response.raise_for_status()
                    health = response.json()
                    response = await client.get(
                        "/capabilities", headers={"Authorization": "Bearer " + cfg.executor_token}
                    )
                    response.raise_for_status()
                    caps = response.json()
                evidence["executor"] = {
                    key: health.get(key)
                    for key in ("executor_id", "boot_id", "active_jobs", "protocol_version")
                }
                evidence["executor_identity_matches"] = (
                    health.get("executor_id") == cfg.executor_id == caps.get("executor_id")
                )
                evidence["executor_execution"] = caps.get("details", {}).get("execution", {})
            finally:
                listener.close()
    except (OSError, asyncssh.Error, httpx.HTTPError, ValueError) as exc:
        evidence["executor_error"] = type(exc).__name__
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                key: value
                for key, value in evidence.items()
                if key not in {"nonterminal_runs", "table_counts", "executor_execution"}
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    configure_stdio_utf8()
    configure_event_loop_policy()
    asyncio.run(main(Path(sys.argv[1])))
