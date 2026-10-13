"""生成私有试验启动配置，不建库、不启动模型或访问目标。"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

from agent_workbench.config.settings import Settings
from agent_workbench.contracts.experiments import TrialRegistration
from agent_workbench.maintenance.provenance import fingerprint_source

BASELINE_COMMIT = "a37ae3786ab495035bbe14bd50276a7553438995"


def prepare_trial(
    registration: TrialRegistration, source_root: Path, output: Path, settings: Settings
) -> Path:
    if registration.group not in {"B0", "V1"}:
        raise ValueError("启动配置只支持 B0/V1；有观测补丁的基线须另行登记 B0-observed")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", registration.trial_id):
        raise ValueError("启动用 trial_id 必须为 1–80 个英文字母、数字、下划线或连字符")
    if output.exists():
        raise FileExistsError("试验输出已存在，拒绝覆盖")
    source = fingerprint_source(source_root)
    for field in ("source_commit", "source_tree_hash", "lock_hash"):
        expected = getattr(registration, field)
        if expected is not None and expected != source[field]:
            raise ValueError(f"预登记的 {field} 与启动源码不一致，不能改写原登记")
    if source["dirty"]:
        raise ValueError("正式试验必须使用冻结且没有源码修改的 checkout")
    if registration.group == "B0" and source["source_commit"] != BASELINE_COMMIT:
        raise ValueError("B0 启动目录不属于登记的原始基线 commit")
    if registration.group == "V1" and source["source_commit"] == BASELINE_COMMIT:
        raise ValueError("V1 尚未冻结到新 commit，不能冒充正式 V1")
    key = hashlib.sha256(registration.trial_id.encode()).hexdigest()[:16]
    database_name = f"workbench_trial_{key}"
    db_url = (
        make_url(settings.database_url)
        .set(database=database_name)
        .render_as_string(hide_password=False)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".trial-", dir=output.parent) as temporary:
        staging = Path(temporary) / "trial"
        staging.mkdir()
        (staging / ".gitignore").write_text("*\n", encoding="utf-8")
        trial = registration.model_copy(
            update={
                "source_commit": source["source_commit"],
                "source_tree_hash": source["source_tree_hash"],
                "lock_hash": source["lock_hash"],
            }
        )
        (staging / "trial.json").write_text(
            trial.model_dump_json(by_alias=True, indent=2) + "\n", encoding="utf-8"
        )
        environment = {
            "WORKBENCH_DATABASE_URL": db_url,
            "WORKBENCH_DATA_ROOT": str(output.resolve() / "data"),
            "WORKBENCH_OWNER_ID": f"trial-{registration.trial_id}",
            "WORKBENCH_EXPERIMENT_TRIAL_ID": registration.trial_id,
            "WORKBENCH_EXPERIMENT_GROUP": registration.group,
            "WORKBENCH_MEMORY_READ_BRANCH": registration.trial_id,
            "WORKBENCH_MEMORY_WRITE_BRANCH": registration.trial_id,
        }
        (staging / "environment.json").write_text(
            json.dumps(environment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (staging / "source.json").write_text(
            json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        source_ps = str(source_root.resolve()).replace("'", "''")
        output_ps = str(output.resolve()).replace("'", "''")
        launch = f"""# UTF-8；只加载当前试验配置，启动入口仍是官方 workbench。
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$trialEnvironment = Get-Content -LiteralPath '{output_ps}/environment.json' -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($property in $trialEnvironment.PSObject.Properties) {{
    Set-Item -LiteralPath ('Env:' + $property.Name) -Value ([string]$property.Value)
}}
Set-Location -LiteralPath '{source_ps}'
Write-Output '请先创建当前试验专用空库，再执行 uv run workbench db-init。'
Write-Output 'API 和 Runner 分别用 uv run workbench api / uv run workbench runner 启动。'
Write-Output '当前脚本只加载配置，未建库、未启动模型或访问目标。'
"""
        (staging / "load-environment.ps1").write_text(launch, encoding="utf-8", newline="\n")
        (staging / "setup.json").write_text(
            json.dumps(
                {
                    "database_name": database_name,
                    "database_create_sql": f'CREATE DATABASE "{database_name}";',
                    "automatic_target_access": False,
                    "task_time_or_cost_cap": None,
                    "memory_isolation": "trial_owner_and_project_and_method_branches",
                    "source_root": str(source_root.resolve()),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        staging.rename(output)
    return output
