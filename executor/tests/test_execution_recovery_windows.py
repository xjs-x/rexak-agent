"""启动边界故障注入：只对能够证明未启动的操作恢复执行。"""

import asyncio
import json
import os
import sys
from uuid import uuid4

import pytest
from workbench_executor._generated.contracts.execution_runtime import ExecutionOperationEnvelope
from workbench_executor.command_runtime import CommandRuntime
from workbench_executor.ledger import Ledger
from workbench_executor.settings import ExecutorSettings

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="真实 Linux 包装进程与回执")


@pytest.mark.parametrize("window", ["received", "claimed", "started", "completed", "bad_receipt", "pid_reuse"])
def test_recovery_fault_windows_never_duplicate_execution(tmp_path, monkeypatch, window):
    class FixtureCrash(RuntimeError):
        pass

    async def scenario():
        settings = ExecutorSettings(data_root=tmp_path, workspace_root=tmp_path / "workspaces",
                                    token="fixture", disk_low_watermark_bytes=0)
        ledger = Ledger(settings.ledger_path)
        await ledger.open()
        runtime = CommandRuntime(settings, ledger, "fixture")
        original_spawn = runtime._spawn_worker

        async def crash_before_spawn(_job_id):
            raise FixtureCrash("fixture: received persisted, worker not launched")

        monkeypatch.setattr(runtime, "_spawn_worker", crash_before_spawn)
        session_id = str(uuid4())
        operation_id = str(uuid4())
        arguments = {"argv": ["/bin/sh", "-c", "echo once >> marker.txt; exit 7"],
                     "workspace_id": session_id}
        envelope = ExecutionOperationEnvelope(operation_id=operation_id, session_id=session_id,
            run_id=str(uuid4()), tool_call_id="recovery-window", request_digest="fixture",
            action="run_command", arguments=arguments)
        try:
            with pytest.raises(FixtureCrash):
                await runtime.accept(envelope, arguments=arguments, kind="command")
            operation = await ledger.get_operation(operation_id)
            assert operation is not None
            job_id = operation.resource_id
            assert job_id
            job_dir = runtime.job_dir(job_id)
            marker = settings.workspace_root / session_id / "marker.txt"
            monkeypatch.setattr(runtime, "_spawn_worker", original_spawn)
            if window == "pid_reuse":
                await ledger.update(job_id, worker_pid=os.getpid(), worker_pid_start_ticks=-1)
                (job_dir / "process.json").write_text(json.dumps({"child_pid": os.getpid(),
                    "child_pid_start_ticks": -1}), encoding="utf-8")
                outcome = await runtime.reconcile()
                assert job_id in outcome["unknown"] and not marker.exists()
                os.kill(os.getpid(), 0)  # PID 指向其他进程时不误杀。
                return
            if window in {"claimed", "started"}:
                (job_dir / "claim.lock").write_text(json.dumps({"worker_pid": 99999999}), encoding="utf-8")
                if window == "started":
                    (job_dir / "start.json").write_text(json.dumps({"worker_pid": 99999999}), encoding="utf-8")
                outcome = await runtime.reconcile()
                assert job_id in outcome["unknown"]
                assert not marker.exists()
                await runtime.reconcile()
                assert not marker.exists()  # unknown 不因再次恢复而重发
                return
            if window == "received":
                outcome = await runtime.reconcile()
                assert job_id in outcome["respawned"]
            else:
                assert await original_spawn(job_id)
            for _ in range(300):
                if (job_dir / "completion.json").is_file():
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("fixture 完成回执超时")
            # 模拟完成回执持久化后，控制服务尚未导入结果即崩溃。
            await ledger.update(job_id, state="running", exit_code=None)
            if window == "bad_receipt":
                receipt = json.loads((job_dir / "completion.json").read_text(encoding="utf-8"))
                receipt["exit_code"] = 0
                (job_dir / "completion.json").write_text(json.dumps(receipt), encoding="utf-8")
            await runtime.reconcile()
            record = await ledger.get(job_id)
            assert record is not None
            if window == "bad_receipt":
                # 等待包装进程收尾，避免把存活瞬间错误当作已完成。
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    await runtime.reconcile()
                    record = await ledger.get(job_id)
                    if record is not None and record.state == "unknown":
                        break
                assert record is not None and record.state == "unknown"
            else:
                assert record.exit_code == 7 and record.state == "failed"
            await runtime.reconcile()
            assert marker.read_text(encoding="utf-8").splitlines() == ["once"]
        finally:
            await ledger.close()

    asyncio.run(scenario())
