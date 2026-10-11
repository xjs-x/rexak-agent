"""自然目标矩阵执行与离线复核。管理数据不会进入 Agent context。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from evals.fixture_control import PUBLIC_PORT, FixtureControl
from evals.grader import aggregate, export_evidence, replay_trial, seal_trial, write_json
from evals.scenarios import ACCEPTANCE_SCENARIOS, AcceptanceScenario

from agent_workbench.config.secrets import build_secret_store
from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.events import EventType
from agent_workbench.contracts.identity import PolicySnapshot, ScopeRule, ScopeSnapshot
from agent_workbench.domain.run_state import create_run
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.maintenance.diagnostics import collect_run_diagnostic, write_run_diagnostic
from agent_workbench.models.provider import ModelBinding, resolve_provider_profile
from agent_workbench.observability.events import append_event
from agent_workbench.persistence.db import Database
from agent_workbench.persistence.repositories import ProjectRepository, SessionRepository
from agent_workbench.persistence.tables import IdentityRow
from agent_workbench.runtime.runner import AgentRunner

ROOT = Path(__file__).resolve().parents[1]


class ModelCredentialsUnavailable(RuntimeError):
    pass


def fingerprint() -> dict:
    digest = hashlib.sha256()
    for folder in ("src", "executor/src", "evals"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            digest.update(path.relative_to(ROOT).as_posix().encode())
            digest.update(path.read_bytes())
    return {
        "code_sha256": digest.hexdigest(),
        "lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "python": platform.python_version(),
        "dependencies": {
            name: version(name)
            for name in (
                "langchain",
                "langgraph",
                "langgraph-checkpoint-postgres",
                "sqlalchemy",
                "psycopg",
            )
        },
    }


async def run_natural_trial(
    scenario: AcceptanceScenario,
    trial: int,
    database: Database,
    control: FixtureControl,
    output: Path,
) -> dict:
    from evals.runner import _drive_until_terminal

    settings = database.settings
    raw = output / scenario.scenario_id / f"trial-{trial}"
    raw.mkdir(parents=True, exist_ok=False)
    reset = await control.request("POST", "/reset")
    accounts = reset.pop("accounts")
    expected_fingerprint = hashlib.sha256(
        (ROOT / "evals" / "fixtures" / "acceptance_target.py").read_bytes()
    ).hexdigest()
    if reset["fingerprint"] != expected_fingerprint:
        raise ValueError("执行岛评测靶场源码版本不一致，请部署当前版本")
    scope = ScopeSnapshot(
        revision=1, rules=[ScopeRule(scheme="http", host="127.0.0.1", port=PUBLIC_PORT)]
    )
    objective = scenario.objective(f"http://127.0.0.1:{PUBLIC_PORT}")
    identity_names: dict[str, str] = {}
    env_names = []
    try:
        async with database.transaction() as session:
            project = await ProjectRepository(session).create(
                owner_id=settings.owner_id,
                name=f"natural-{scenario.scenario_id}-{uuid.uuid4().hex[:8]}",
                target_description="自然目标评测",
                default_scope=scope.model_dump(),
            )
            task = await SessionRepository(session).create(
                project_id=project.id,
                title=f"{scenario.scenario_id} trial {trial}",
                scope_snapshot=scope.model_dump(),
                policy_snapshot=PolicySnapshot().model_dump(),
                provider_id=settings.default_provider,
                model_name=settings.xiaomi_model,
            )
            for name in scenario.accounts:
                env_name = f"EVAL_IDENTITY_{uuid.uuid4().hex.upper()}"
                env_names.append(env_name)
                os.environ[env_name] = json.dumps(accounts[name])
                identity = IdentityRow(
                    project_id=project.id,
                    display_name=name,
                    role_label=name,
                    secret_ref=f"env:{env_name}",
                )
                session.add(identity)
                await session.flush()
                identity_names[str(identity.id)] = name
                identity_names[f"session-{task.id}-identity-{identity.id}"] = name
                identity_names[
                    f"session-{task.id}-{hashlib.sha256(str(identity.id).encode()).hexdigest()[:16]}"
                ] = name
                if name == "alice":
                    task.active_identity_id = identity.id
                    identity_names["anonymous"] = name
            run = await create_run(
                session,
                session_id=task.id,
                objective=objective,
                provider_id=settings.default_provider,
                model_name=settings.xiaomi_model,
                config_snapshot={
                    "evaluation": "natural-goals-v1",
                    "scenario_id": scenario.scenario_id,
                    "trial": trial,
                },
            )
            await append_event(
                session,
                session_id=task.id,
                run_id=run.id,
                event_type=EventType.RUN_STATUS,
                payload={"status": "queued", "reason": "eval_start"},
            )
        private = {
            "scenario_id": scenario.scenario_id,
            "trial": trial,
            "run_id": str(run.id),
            "session_id": str(task.id),
            "before": reset,
            "after": None,
            "identity_names": identity_names,
            "objective": objective,
            "scope": scope.model_dump(),
            "fingerprint": fingerprint(),
            "watchdog_triggered": False,
            "model": model_snapshot(settings),
            "started_at": datetime.now(UTC).isoformat(),
        }
        write_json(raw / "control.json", private)
        runner = AgentRunner(
            settings=settings, database=database, owner=f"natural-eval-{uuid.uuid4().hex[:8]}"
        )
        boundary = asyncio.timeout(scenario.timeout_seconds)
        try:
            async with boundary:
                await _drive_until_terminal(runner, run.id)
        except TimeoutError:
            if not boundary.expired():
                raise
            private["watchdog_triggered"] = True
        finally:
            diagnostic = await collect_run_diagnostic(database, run.id)
            await asyncio.to_thread(write_run_diagnostic, raw, diagnostic)
        private["after"] = await control.request("GET", "/snapshot")
        private["finished_at"] = datetime.now(UTC).isoformat()
        write_json(raw / "control.json", private)
        export_evidence(diagnostic, ArtifactStore(settings), raw)
        seal_trial(raw)
        grade = replay_trial(raw)
        write_json(raw / "grade.json", grade)
        # 未知执行/恢复状态或活跃请求时停止矩阵，防止重置污染与自动重放副作用。
        grade["stop_suite"] = (
            private["watchdog_triggered"]
            or diagnostic["run"]["status"] == "recovering"
            or private["after"]["active_requests"] != 0
            or any(
                job["state"] not in {"succeeded", "failed", "cancelled"}
                for job in diagnostic["jobs"]
            )
        )
        return grade
    finally:
        for env_name in env_names:
            os.environ.pop(env_name, None)
        accounts.clear()


def replay_suite(output: Path) -> dict:
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    grades = []
    seen = set()
    for item in plan:
        if (
            item["scenario_id"] not in {s.scenario_id for s in ACCEPTANCE_SCENARIOS}
            or type(item["trial"]) is not int
            or item["trial"] < 1
        ):
            raise ValueError("计划中的 trial 无效")
        identity = (item["scenario_id"], item["trial"])
        if identity in seen:
            raise ValueError("计划含重复 trial")
        seen.add(identity)
        directory = output / item["scenario_id"] / f"trial-{item['trial']}"
        if (directory / "manifest.json").exists():
            grade = replay_trial(directory)
            if (grade["scenario_id"], grade["trial"]) != (item["scenario_id"], item["trial"]):
                raise ValueError("评测包和计划身份不一致")
            grades.append(grade)
    return aggregate(grades, plan)


async def main_acceptance(args: Any) -> int:
    settings = get_settings()
    if args.trials < 1 or args.provider != settings.default_provider:
        print("trial 数量或供应商配置不匹配")
        return 2
    selected = {s.scenario_id for s in ACCEPTANCE_SCENARIOS}
    if args.scenario:
        selected = {item.strip().upper() for item in args.scenario.split(",")}
        if not selected <= {s.scenario_id for s in ACCEPTANCE_SCENARIOS}:
            print("存在未知场景 ID")
            return 2
    scenarios = [s for s in ACCEPTANCE_SCENARIOS if s.scenario_id in selected]
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%S%fZ")
    output = Path(args.output) if args.output else settings.data_root / "evals" / "results" / stamp
    output.mkdir(parents=True, exist_ok=False)
    plan = [
        {"scenario_id": scenario.scenario_id, "trial": trial}
        for scenario in scenarios
        for trial in range(1, args.trials + 1)
    ]
    write_json(output / "plan.json", plan)
    db, control = Database(settings), FixtureControl(settings)
    grades, errors = [], []
    try:
        profile = resolve_provider_profile(
            settings.default_provider,
            settings.xiaomi_model if settings.default_provider == "xiaomi" else None,
            settings,
        )
        if not build_secret_store().exists(profile.secret_ref):
            raise ModelCredentialsUnavailable("模型凭据未提供")
        await db.start()
        await control.open()
        stop = False
        for scenario in scenarios:
            for trial in range(1, args.trials + 1):
                if stop:
                    break
                print(f"自然目标 {scenario.scenario_id} trial {trial}/{args.trials}", flush=True)
                try:
                    grade = await run_natural_trial(scenario, trial, db, control, output)
                    stop = grade.pop("stop_suite")
                    grades.append(grade)
                    print(f"独立判定：{grade['outcome']}", flush=True)
                except Exception as exc:
                    # 异常只记录类别，防止私有管理凭据随 URL/响应进入普通结果。
                    errors.append(
                        {
                            "scenario_id": scenario.scenario_id,
                            "trial": trial,
                            "error_code": type(exc).__name__,
                        }
                    )
                    stop = True
                summary = aggregate(grades, plan)
                summary["errors"] = errors
                write_json(output / "summary.json", summary)
    except Exception as exc:
        errors.append({"error_code": type(exc).__name__, "stage": "preflight"})
    finally:
        await control.close()
        await db.close()
    summary = aggregate(grades, plan)
    summary["errors"] = errors
    write_json(output / "summary.json", summary)
    print(f"评测包已保存：{output}")
    return int(bool(errors) or summary["passed"] != len(plan))


def model_snapshot(settings: Any) -> dict:
    profile = resolve_provider_profile(
        settings.default_provider,
        settings.xiaomi_model if settings.default_provider == "xiaomi" else None,
        settings,
    )
    binding = ModelBinding(
        profile=profile, request_timeout_seconds=settings.model_request_timeout_seconds
    )
    snapshot = binding.to_snapshot()
    snapshot.pop("secret_ref", None)
    snapshot["request_timeout_seconds"] = binding.request_timeout_seconds
    snapshot["streaming"] = True
    return snapshot


def compare_paired(candidate: Path, baseline: Path) -> dict:
    """仅接受同一评测包协议的基线。旧框架需导出真实台账，不能伪造兼容结果。"""
    current, previous = replay_suite(candidate), replay_suite(baseline)
    current_grades = {(g["scenario_id"], g["trial"]): g for g in current["grades"]}
    previous_grades = {(g["scenario_id"], g["trial"]): g for g in previous["grades"]}
    pairs, excluded = [], []
    for identity in sorted(current_grades.keys() | previous_grades.keys()):
        if identity not in current_grades or identity not in previous_grades:
            excluded.append(
                {"scenario_id": identity[0], "trial": identity[1], "reason": "missing_pair"}
            )
            continue
        left = candidate / identity[0] / f"trial-{identity[1]}"
        right = baseline / identity[0] / f"trial-{identity[1]}"
        controls = [
            json.loads((directory / "control.json").read_text(encoding="utf-8"))
            for directory in (left, right)
        ]
        a, b = controls
        compatible = (
            bool(a.get("model"))
            and a["model"] == b.get("model")
            and a["objective"] == b["objective"]
            and a["scope"] == b["scope"]
            and a["before"]["fingerprint"] == b["before"]["fingerprint"]
            and bool(a.get("fingerprint"))
            and bool(b.get("fingerprint"))
        )
        results = [current_grades[identity], previous_grades[identity]]
        if not compatible or any(g["outcome"] == "invalid" for g in results):
            excluded.append(
                {
                    "scenario_id": identity[0],
                    "trial": identity[1],
                    "reason": "incompatible_configuration_or_invalid_trial",
                }
            )
            continue
        pairs.append(
            {
                "scenario_id": identity[0],
                "trial": identity[1],
                "candidate": results[0]["outcome"],
                "baseline": results[1]["outcome"],
            }
        )
    delta = sum(int(p["candidate"] == "passed") - int(p["baseline"] == "passed") for p in pairs)
    return {
        "paired_trials": len(pairs),
        "pairs": pairs,
        "excluded": excluded,
        "observed_success_rate_delta": delta / len(pairs) if pairs else None,
        "note": "仅配对样本的观察差量，不是显著性结论；基线需另行提供真实同协议原始包。",
    }
