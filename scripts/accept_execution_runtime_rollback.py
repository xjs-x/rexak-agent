"""在 Linux 专属 fixture 中演练旧执行端受限回退及恢复新版。

先用 git archive 导出本仓库旧版 executor/src，传入 --legacy-source；不读取外部项目。
旧执行端无执行子协议时不允许接管新资源。它可以拒绝启动；恢复新版后必须能
核对同一资源、单次启动标记、连续日志并确认取消。此脚本不降级日常服务。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from accept_execution_runtime import ExecutorApi, FixtureExecutor, IsolatedSuite, Recorder


def exercise(legacy_source: Path, output: Path) -> dict:
    if sys.platform != "linux":
        raise RuntimeError("受限回退演练需要 Linux 真进程")
    if not (legacy_source / "workbench_executor" / "api.py").is_file():
        raise ValueError("legacy-source 必须是本仓库旧版本的 executor/src 目录")
    root = Path(tempfile.mkdtemp(prefix="rexak-rollback-fixture-"))
    recorder = Recorder(output)
    suite = IsolatedSuite(root, recorder)
    legacy = FixtureExecutor(
        root=suite.fixture.root,
        token=suite.fixture.token,
        extra_env={"PYTHONPATH": str(legacy_source.resolve())},
    )
    try:
        suite.start()
        receipt = suite.run_command(
            {"argv": ["/bin/sh", "-c", "echo started-once; while :; do echo tick; sleep 0.2; done"]},
            wait_ms=300,
        )
        resource_id = str(receipt["resource_id"])
        suite.wait_state(resource_id, {"running"})
        assert suite.api is not None
        before, _ = suite.read_all(resource_id)
        assert before.count("started-once") == 1
        suite.api.close()
        suite.fixture.stop()
        ledger = suite.fixture.root / "executor-ledger.sqlite3"
        snapshot = root / "before-legacy.sqlite3"
        with sqlite3.connect(ledger) as source, sqlite3.connect(snapshot) as target:
            source.backup(target)
            original = source.execute(
                "SELECT operation_id, worker_pid, pid FROM jobs WHERE job_id=?", (resource_id,)
            ).fetchone()
        assert original is not None
        legacy_status = "refused_startup"
        try:
            legacy.start()
        except RuntimeError:
            # 不支持新台账的旧版可拒绝启动；日志和新版恢复结果共同构成证据。
            if legacy.process is not None and legacy.process.poll() is None:
                legacy.stop()
        else:
            client = ExecutorApi(legacy.base_url, legacy.token)
            try:
                capabilities = client.capabilities()
                assert not capabilities.get("details", {}).get("execution", {}).get("command")
                response = client.client.get(f"/execution/v1/resources/{resource_id}")
                assert response.status_code == 404
                legacy_status = "legacy_http_only_new_execution_unavailable"
            finally:
                client.close()
                legacy.stop()
        # 不能盲目把快照覆盖回仍有包装进程写入的数据库；保持台账并恢复新版核对。
        suite.fixture.start()
        suite.api = ExecutorApi(suite.fixture.base_url, suite.fixture.token)
        suite.api.sync(suite.session_id, suite.run_id)
        restored = suite.wait_state(resource_id, {"running"})
        time.sleep(0.6)
        after, _ = suite.read_all(resource_id)
        with sqlite3.connect(ledger) as db:
            recovered = db.execute(
                "SELECT operation_id, worker_pid, pid FROM jobs WHERE job_id=?", (resource_id,)
            ).fetchone()
        assert recovered == original, (original, recovered)
        assert after.count("started-once") == 1 and after.count("tick") > before.count("tick")
        suite.dispatch("cancel_resource", {"resource_id": resource_id}, wait_ms=20000)
        final = suite.wait_state(resource_id, {"exited", "closed"})
        assert final.get("cancel_confirmed") is True
        return {
            "id": "H06", "status": "PASS", "legacy_mode": legacy_status,
            "restored_state": restored["state"], "identity_preserved": True,
            "started_count": after.count("started-once"), "logs_continued": True,
            "cancel_confirmed": True, "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            "scope": "isolated noninteractive resource; no live PTY downgrade or daily service downgrade",
        }
    finally:
        if legacy.process is not None and legacy.process.poll() is None:
            legacy.stop()
        suite.cleanup()
        # 正常路径按服务停止方式退出，避免杀掉不属于此 fixture 的进程。
        if suite.fixture.process is not None:
            suite.fixture.process.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    result = exercise(args.legacy_source, args.output)
    (args.output / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
