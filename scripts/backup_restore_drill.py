"""P8 隔离还原演练：用临时数据库和工件验证 backup 的可恢复性。

只创建带 p8_drill_ 前缀的临时数据库；不会修改 workbench 正式数据库。
需要本机 Docker 和已运行的 PostgreSQL 容器。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path


def _run(command: list[str], *, input_file: Path | None = None, env: dict[str, str] | None = None) -> str:
    with input_file.open("rb") if input_file else Path(os.devnull).open("rb") as source:
        proc = subprocess.run(
            command,
            stdin=source,
            capture_output=True,
            env=env,
            check=False,
        )
    if proc.returncode:
        detail = proc.stderr.decode("utf-8", errors="backslashreplace").strip()[:1000]
        raise RuntimeError(f"命令失败（退出码 {proc.returncode}）：{command[:4]}：{detail}")
    return proc.stdout.decode("utf-8", errors="backslashreplace").strip()


def drill(container: str) -> None:
    suffix = uuid.uuid4().hex[:12]
    source_db = f"p8_drill_source_{suffix}"
    restored_db = f"p8_drill_restored_{suffix}"
    created: list[str] = []
    try:
        for name in (source_db, restored_db):
            _run(["docker", "exec", container, "createdb", "-U", "workbench", name])
            created.append(name)

        _run(
            [
                "docker", "exec", container, "psql", "-X", "-v", "ON_ERROR_STOP=1",
                "-U", "workbench", "-d", source_db, "-c",
                "CREATE SCHEMA app; "
                "CREATE TABLE app.restore_probe (id integer PRIMARY KEY, artifact_key text NOT NULL, note text NOT NULL); "
                "INSERT INTO app.restore_probe VALUES (1, 'probe/visible.txt', '中文恢复演练');",
            ]
        )

        with tempfile.TemporaryDirectory(prefix="p8-backup-restore-") as temporary:
            root = Path(temporary)
            data_root = root / "data"
            artifact = data_root / "artifacts" / "probe" / "visible.txt"
            artifact.parent.mkdir(parents=True)
            expected = "可恢复工件：UTF-8 中文\n".encode()
            artifact.write_bytes(expected)
            env = {
                **os.environ,
                "PYTHONIOENCODING": "utf-8",
                "WORKBENCH_DATABASE_URL": (
                    f"postgresql+psycopg://workbench:workbench@127.0.0.1:55432/{source_db}"
                ),
                "WORKBENCH_DATA_ROOT": str(data_root),
            }
            _run(
                [
                    sys.executable, "-m", "agent_workbench.cli", "backup", "--output",
                    str(root / "backups"), "--docker-container", container,
                ],
                env=env,
            )
            backups = list((root / "backups").iterdir())
            if len(backups) != 1:
                raise AssertionError(f"预期一份备份，实际 {len(backups)} 份")
            backup = backups[0]
            dump = backup / "database.sql"
            restored_artifact = backup / "artifacts" / "probe" / "visible.txt"
            if not dump.is_file() or not (backup / "MANIFEST.txt").is_file():
                raise AssertionError("缺少数据库备份或清单")
            if restored_artifact.read_bytes() != expected:
                raise AssertionError("备份工件正文与来源不一致")
            verify_command = [sys.executable, "-m", "agent_workbench.cli", "verify-backup", str(backup)]
            _run(verify_command, env=env)

            _run(
                [
                    "docker", "exec", "-i", container, "psql", "-X", "-v", "ON_ERROR_STOP=1",
                    "-U", "workbench", "-d", restored_db,
                ],
                input_file=dump,
            )
            row = _run(
                [
                    "docker", "exec", container, "psql", "-X", "-A", "-t", "-U",
                    "workbench", "-d", restored_db, "-c",
                    "SELECT artifact_key || '|' || note FROM app.restore_probe WHERE id = 1;",
                ]
            )
            if row != "probe/visible.txt|中文恢复演练":
                raise AssertionError(f"还原后的数据库行不匹配：{row}")
            restored_artifact.write_bytes(b"corrupted")
            damaged = subprocess.run(verify_command, capture_output=True, env=env, check=False)
            if damaged.returncode == 0:
                raise AssertionError("工件损坏后备份校验仍错误地通过")
            print("P8 备份还原演练通过：隔离数据库行与对应 UTF-8 工件均已恢复")
    finally:
        for name in reversed(created):
            _run(["docker", "exec", container, "dropdb", "-U", "workbench", "--if-exists", name])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="workbench-postgres")
    args = parser.parse_args()
    drill(args.container)
