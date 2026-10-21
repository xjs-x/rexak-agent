"""回档重写的只读验收：保存/比较日常数据，核对空外部库存和执行岛。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from agent_workbench.config.settings import get_settings
from agent_workbench.execution.client import HttpExecutorClient
from agent_workbench.execution.tunnel import SshTunnel
from agent_workbench.persistence.db import Database
from agent_workbench.platform_compat import (
    configure_event_loop_policy,
    configure_local_loopback,
    configure_stdio_utf8,
)
from agent_workbench.skills.service import SkillService
from agent_workbench.tools.registry import ToolSelection, build_tools


async def verify(phase: str, output: Path) -> None:
    settings = get_settings()
    database = Database(settings)
    await database.start()
    try:
        async with database.session() as db:
            preserved: dict[str, Any] = {}
            for name in ("runs", "sessions", "model_calls"):
                preserved[name] = (await db.execute(text(f"SELECT count(*) FROM app.{name}"))).scalar_one()
            preserved["run_statuses"] = [list(row) for row in (await db.execute(
                text("SELECT status,count(*) FROM app.runs GROUP BY status ORDER BY status"))).all()]
            leases = (await db.execute(text("SELECT count(*) FROM app.runs WHERE lease_until > now()"))).scalar_one()
            builtin = [dict(row) for row in (await db.execute(text(
                "SELECT source,enabled FROM app.skill_installations WHERE source LIKE 'builtin:%' ORDER BY source"))).mappings()]
            versions = (await db.execute(text("SELECT count(*) FROM app.skill_versions"))).scalar_one()
        assert leases == 0, "仍有活动运行租约，拒绝进入验收/更新边界"
        output.mkdir(parents=True, exist_ok=True)
        state = {"preserved": preserved, "builtin": builtin, "version_count": versions}
        if phase == "before":
            path = output / "before.json"
            if path.exists():
                raise FileExistsError("更新前证据已存在，不覆盖")
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("已保存更新前的任务、模型调用和历史 Skill 状态；活动租约为 0。")
            return
        before = json.loads((output / "before.json").read_bytes())
        assert preserved == before["preserved"], "任务或模型调用状态发生变化，需核对"
        assert all(not row["enabled"] for row in builtin), "旧 builtin 仍启用"
        assert versions == before["version_count"], "历史包版本发生变化"
        inventory = await SkillService(database).list()
        assert not inventory, "日常库存已有用户外部包，应另行明确验证预期"
        assert not any(item.name == "load_skill" for item in build_tools(ToolSelection()))
        tunnel = SshTunnel(settings)
        client = HttpExecutorClient(tunnel, settings=settings, expected_executor_id=settings.executor_id)
        try:
            await client.start()
            capabilities = await client.capabilities()
            remote = {"identity": capabilities.executor_id, "mode": capabilities.execution_mode,
                "skill_packages": capabilities.execution.get("skill_packages"),
                "network_isolation": capabilities.network_isolation}
        finally:
            await client.close()
            await tunnel.close()
        state.update(status="PASS", external_inventory=0, executor=remote,
            model_called=False, target_accessed=False)
        (output / "after.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("回档重写的日常数据保留、空库存及执行岛只读核对 PASS。")
    finally:
        await database.close()


def main() -> None:
    configure_stdio_utf8()
    configure_event_loop_policy()
    configure_local_loopback()
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(verify(args.phase, args.output))


if __name__ == "__main__":
    main()
