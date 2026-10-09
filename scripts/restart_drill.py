"""进程级重启演练。

目的：验证 ACCEPTANCE §3 切点 2（Runner 在远端任务在途时被杀死）与
"重启恢复必须先核对、再决定是否续跑"。

做法（全部是真实动作，不是模拟）：
1. 在**独立进程**里启动 Runner，让它真的派发一个长耗时 HTTP 任务到执行岛；
2. 在任务仍在执行时**硬杀**该进程（`kill()`，不是优雅退出）；
3. 等 lease 过期；
4. 跑真实启动恢复，打印核对报告；
5. 用**目标端服务端计数器**证明：该任务只执行过一次，没有被重跑。

用法：

    uv run python scripts/restart_drill.py

退出码：0 = 演练通过；1 = 演练失败（并打印失败原因）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agent_workbench.platform_compat import (  # noqa: E402
    child_process_env,
    configure_event_loop_policy,
    configure_stdio_utf8,
)

configure_event_loop_policy()
configure_stdio_utf8()

from sqlalchemy import select  # noqa: E402

from agent_workbench.config.settings import get_settings  # noqa: E402
from agent_workbench.contracts.enums import RunStatus  # noqa: E402
from agent_workbench.contracts.events import EventType  # noqa: E402
from agent_workbench.contracts.identity import (  # noqa: E402
    PolicySnapshot,
    ScopeRule,
    ScopeSnapshot,
)
from agent_workbench.domain.run_state import create_run  # noqa: E402
from agent_workbench.observability.events import append_event  # noqa: E402
from agent_workbench.persistence.db import Database  # noqa: E402
from agent_workbench.persistence.repositories import (  # noqa: E402
    ProjectRepository,
    SessionRepository,
    ToolJobRepository,
)
from agent_workbench.persistence.tables import EventRow, RunRow  # noqa: E402
from agent_workbench.runtime.recovery import run_startup_recovery  # noqa: E402

#: 让 Runner 把长任务当成"后台任务"快速返回 accepted，便于在任务在途时杀进程
DRILL_ENV = {
    "WORKBENCH_TOOL_INTERACTIVE_THRESHOLD_SECONDS": "3",
    "WORKBENCH_RUN_LEASE_SECONDS": "8",
    "WORKBENCH_LEASE_HEARTBEAT_SECONDS": "3",
    "WORKBENCH_SINGLE_OPERATION_TIMEOUT_SECONDS": "120",
}

#: 目标端慢接口参数：4 段 × 8 秒 ≈ 24 秒，仍小于 httpx 的 30 秒读超时
SLOW_PATH = "/slow?delay=8&chunks=4"

COUNTER_ENDPOINT = "/__fixture__/counters"


class DrillFailure(RuntimeError):
    pass


async def _fetch_counters() -> dict[str, int]:
    """读目标端服务端计数器（独立事实）。"""
    from agent_workbench.execution.client import HttpExecutorClient
    from agent_workbench.execution.tunnel import SshTunnel

    settings = get_settings()
    tunnel = SshTunnel(settings)
    client = HttpExecutorClient(
        tunnel, settings=settings, expected_executor_id=settings.executor_id
    )
    try:
        await client.start()
        receipt = await client.submit(
            {
                "operation_id": str(uuid.uuid4()),
                "kind": "http_request",
                "request_digest": f"drill-counters-{uuid.uuid4().hex}",
                "scope_revision": 1,
                "scope": {
                    "revision": 1,
                    "rules": [
                        {
                            "scheme": "http",
                            "host": "127.0.0.1",
                            "port": 8899,
                            "include_subdomains": False,
                            "allow_private": True,
                        }
                    ],
                    "mappings": [],
                    "denied_metadata_hosts": [],
                },
                "payload": {
                    "method": "GET",
                    "url": f"http://127.0.0.1:8899{COUNTER_ENDPOINT}",
                    "identity_id": "drill",
                    "max_redirects": 0,
                    "save_artifact": True,
                },
            }
        )
        snapshot = await client.get_job(receipt.job_id)
        for _ in range(40):
            snapshot = await client.get_job(receipt.job_id)
            if str(snapshot.state) in {"succeeded", "failed", "cancelled", "unknown"}:
                break
            await asyncio.sleep(0.25)
        if str(snapshot.state) != "succeeded":
            raise DrillFailure(f"读取目标端计数器失败：job 状态 {snapshot.state}")
        body, _headers = await client.download_artifact(receipt.job_id, "response.body")
        parsed = json.loads(body.decode("utf-8"))
        return {str(k): int(v) for k, v in (parsed.get("counters") or {}).items()}
    finally:
        await client.close()
        await tunnel.close()


def _slow_hits(counters: dict[str, int]) -> int:
    return sum(
        value
        for key, value in counters.items()
        if key.startswith("GET /slow") and not key.endswith(COUNTER_ENDPOINT)
    )


async def _prepare_run(database: Database) -> tuple[uuid.UUID, uuid.UUID]:
    settings = get_settings()
    scope = ScopeSnapshot(revision=1, rules=[ScopeRule(scheme="http", host="127.0.0.1", port=8899)])
    async with database.transaction() as session:
        project = await ProjectRepository(session).create(
            owner_id=settings.owner_id,
            name=f"drill-{uuid.uuid4().hex[:8]}",
            target_description="重启演练目标",
            default_scope=scope.model_dump(),
        )
        session_row = await SessionRepository(session).create(
            project_id=project.id,
            title="重启演练",
            scope_snapshot=scope.model_dump(),
            policy_snapshot=PolicySnapshot().model_dump(),
            provider_id=settings.default_provider,
            model_name=settings.xiaomi_model,
        )
        run = await create_run(
            session,
            session_id=session_row.id,
            objective=(
                f"请用 http_request 获取 http://127.0.0.1:8899{SLOW_PATH}（这是一个较慢的接口）。"
                "如果工具返回 accepted，请**不要**重复提交，也不要用 get_job 密集轮询；"
                "直接调用 propose_completion（reason=goal_answered）说明当前状态即可。"
            ),
            provider_id=settings.default_provider,
            model_name=settings.xiaomi_model,
            config_snapshot={"drill": True, "slow_path": SLOW_PATH},
        )
        await append_event(
            session,
            session_id=session_row.id,
            run_id=run.id,
            event_type=EventType.RUN_STATUS,
            payload={"status": str(RunStatus.QUEUED), "reason": "restart_drill"},
        )
        return session_row.id, run.id


async def _wait_for_inflight_job(
    database: Database, run_id: uuid.UUID, *, timeout_seconds: float = 180.0
) -> str:
    """等到该 run 名下出现一个"在途"任务（received/starting/running）。"""
    deadline = time.monotonic() + timeout_seconds
    last: list[str] = []
    while time.monotonic() < deadline:
        async with database.session() as session:
            jobs = await ToolJobRepository(session).list_active(get_settings().executor_id)
            run_jobs = []
            from agent_workbench.persistence.tables import ToolOperationRow

            operations = (
                (
                    await session.execute(
                        select(ToolOperationRow).where(ToolOperationRow.run_id == run_id)
                    )
                )
                .scalars()
                .all()
            )
            op_ids = {op.operation_id for op in operations}
            run_jobs = [job for job in jobs if job.operation_id in op_ids]
            last = [f"{job.job_id}:{job.state}" for job in run_jobs]
            if run_jobs:
                return last[0]
        await asyncio.sleep(0.5)
    raise DrillFailure(f"等待在途任务超时；最后一次观察：{last}")


def _launch_runner(
    *, once: bool, log_path: Path, only_run_id: str | None = None
) -> subprocess.Popen[bytes]:
    """启动真实 Runner 子进程。

    带 `--only-run-id` 是必要的：数据库里可能还堆着其它 queued run（例如测试留下的），
    不带这个参数时 Runner 会先去处理它们，演练就永远等不到自己的在途任务
    —— 这是第一次演练失败的真实原因。
    """
    env = child_process_env(DRILL_ENV)
    command = [sys.executable, "-m", "agent_workbench.cli", "runner"]
    if once:
        command.append("--once")
    # 演练自己会在第 4 步显式调用启动恢复；让 Runner 启动时再跑一遍会先花时间
    # 核对历史 job，把阶段 1/4b 拖到看不到目标 run（演练里真实发生过）。
    command.append("--no-recover")
    if only_run_id:
        command.extend(["--only-run-id", only_run_id])
    handle = log_path.open("wb")
    return subprocess.Popen(
        command,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )


async def _run_drill(args: argparse.Namespace) -> int:
    settings = get_settings()
    out_dir = Path(args.output) if args.output else settings.data_root / "drills"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    work_dir = out_dir / f"restart-{stamp}"
    work_dir.mkdir(parents=True, exist_ok=True)

    database = Database(settings)
    await database.start()
    findings: list[str] = []
    try:
        print("== 0) 前置检查：执行岛与目标端 ==")
        before = await _fetch_counters()
        print(f"   目标端计数器（前）：{before}")
        slow_before = _slow_hits(before)

        session_id, run_id = await _prepare_run(database)
        print(f"   已创建 run {run_id}")

        print("== 1) 启动独立 Runner 进程 ==")
        runner_log = work_dir / "runner.log"
        proc = _launch_runner(once=False, log_path=runner_log, only_run_id=str(run_id))
        print(f"   pid={proc.pid}，日志：{runner_log}")

        try:
            print("== 2) 等待远端任务在途 ==")
            inflight = await _wait_for_inflight_job(database, run_id)
            print(f"   观察到在途任务：{inflight}")
        except DrillFailure as exc:
            findings.append(str(exc))
            print(f"   ！{exc}")
        finally:
            print("== 3) 硬杀 Runner 进程（模拟崩溃，非优雅退出）==")
            killed = proc.poll() is None
            if killed:
                proc.kill()
            proc.wait(timeout=30)
            print(f"   killed={killed} exitcode={proc.returncode}")

        print(
            "== 3b) 等 lease 过期（leased={}s）==".format(DRILL_ENV["WORKBENCH_RUN_LEASE_SECONDS"])
        )
        await asyncio.sleep(float(DRILL_ENV["WORKBENCH_RUN_LEASE_SECONDS"]) + 4)

        async with database.session() as session:
            row = (await session.execute(select(RunRow).where(RunRow.id == run_id))).scalar_one()
        print(f"   被杀后 run.status={row.status} owner={row.owner} lease_until={row.lease_until}")
        if RunStatus(row.status) is not RunStatus.RUNNING:
            findings.append(
                f"预期 run 停在 running（进程是硬杀的），实际 {row.status}；"
                "若为 queued 说明它从未被 Runner 取走"
            )

        print("== 4) 执行真实启动恢复（先核对、再决定归宿）==")
        report = await run_startup_recovery(database, settings)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        (work_dir / "recovery-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        async with database.session() as session:
            row_after = (
                await session.execute(select(RunRow).where(RunRow.id == run_id))
            ).scalar_one()
            events = (
                (
                    await session.execute(
                        select(EventRow)
                        .where(EventRow.session_id == session_id)
                        .order_by(EventRow.session_seq)
                    )
                )
                .scalars()
                .all()
            )
        print(f"   恢复后 run.status={row_after.status}")
        print(f"   stop_reason={row_after.stop_reason}")

        # --- 判据 ---
        if str(run_id) not in [str(item) for item in report["runs_marked_recovering"]]:
            findings.append("lease 过期后该 run 未进入 recovering 流程")
        decided = [str(item) for item in report["allowed_to_resume"]] + [
            str(item) for item in report["blocked_pending_user"]
        ]
        if str(run_id) not in decided:
            findings.append(
                "恢复流程没有对该 run 做出明确决定（既没允许续跑，也没阻塞）——说明核对待办被跳过"
            )
        # 允许续跑的前提是"核对通过"；重新排队必须能看到对应依据事件
        requeued_with_evidence = any(
            str(item.type) == str(EventType.RUN_STATUS)
            and item.payload.get("reason") == "reconciled_before_requeue"
            for item in events
        )
        if (
            RunStatus(row_after.status) is RunStatus.QUEUED
            and report["executor_reachable"]
            and not requeued_with_evidence
        ):
            findings.append("run 被重新排队，但没有'已核对'的依据事件")

        print("== 4b) 重启 Runner（--once --only-run-id），确认不会自行续跑被阻塞的 run ==")
        resume_log = work_dir / "runner-after-recovery.log"
        proc2 = _launch_runner(once=True, log_path=resume_log, only_run_id=str(run_id))
        try:
            proc2.wait(timeout=300)
        except subprocess.TimeoutExpired:
            proc2.kill()
            findings.append("重启后的 Runner 在 300 秒内没有结束（--once 应很快返回）")
        print(f"   重启后退出码={proc2.returncode}，日志：{resume_log}")

        async with database.session() as session:
            row_resumed = (
                await session.execute(select(RunRow).where(RunRow.id == run_id))
            ).scalar_one()
        print(f"   重启后 run.status={row_resumed.status}")
        if (
            RunStatus(row_after.status) is RunStatus.RECOVERING
            and RunStatus(row_resumed.status) is not RunStatus.RECOVERING
        ):
            findings.append(
                f"被阻塞的 run 在重启后被自动推进到 {row_resumed.status} —— "
                "未知远端状态必须由用户显式确认后才能恢复"
            )

        print("== 5) 等待远端任务自然结束，再用计数器证明未重复执行 ==")
        await asyncio.sleep(40)
        after = await _fetch_counters()
        print(f"   目标端计数器（后）：{after}")
        slow_after = _slow_hits(after)
        executed = slow_after - slow_before
        print(f"   /slow 实际执行次数（本次演练新增）：{executed}")
        if executed != 1:
            findings.append(
                f"目标端 /slow 执行了 {executed} 次，预期 1 次 —— 存在重复副作用或未执行"
            )

        (work_dir / "drill-summary.json").write_text(
            json.dumps(
                {
                    "run_id": str(run_id),
                    "session_id": str(session_id),
                    "run_status_after_recovery": str(row_after.status),
                    "stop_reason": row_after.stop_reason,
                    "executor_reachable": report["executor_reachable"],
                    "allowed_to_resume": report["allowed_to_resume"],
                    "blocked_pending_user": report["blocked_pending_user"],
                    "slow_hits_before": slow_before,
                    "slow_hits_after": slow_after,
                    "findings": findings,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    finally:
        await database.close()

    print()
    if findings:
        print("演练失败：")
        for item in findings:
            print(f"  - {item}")
        print(f"证据目录：{work_dir}")
        return 1
    print(
        "演练通过：Runner 在远端任务在途时被杀死后，恢复流程先核对再决定归宿，"
        "且目标端只执行过一次。"
    )
    print(f"证据目录：{work_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Runner 进程级重启演练")
    parser.add_argument("--output", default="", help="证据输出目录")
    args = parser.parse_args()
    if not os.environ.get("XIAOMI_API_KEY"):
        print("缺少 XIAOMI_API_KEY：演练需要真实模型才能派发任务。")
        return 2
    return asyncio.run(_run_drill(args))


if __name__ == "__main__":
    raise SystemExit(main())
