"""`workbench` 命令行入口（§16.2）。

目标入口：`api`、`runner`、`db-init`、`doctor`、`export-run`、`backup`、`prune-events`。
Executor 有独立启动与 doctor 命令（`workbench-executor` / `python -m workbench_executor`）。

一键开发启动应能明确启动/停止三个本地服务与数据库。
**API reload 不自动生成第二个 Runner。**
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer

from agent_workbench import __version__
from agent_workbench.config.settings import REPO_ROOT, get_settings
from agent_workbench.platform_compat import (
    child_process_env,
    configure_event_loop_policy,
    configure_stdio_utf8,
)
from agent_workbench.skills.cli import app as skills_app
from agent_workbench.tsecbench.cli import tsec_app

app = typer.Typer(
    name="workbench",
    help="Web/API 自主黑盒测试工作台（控制端）",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(skills_app, name="skills")
app.add_typer(tsec_app, name="tsec")


def _echo(message: str) -> None:
    """打印一行。确保输出流是 UTF-8，避免在 cp936 控制台下中文报 UnicodeEncodeError。"""
    configure_stdio_utf8()
    typer.echo(message)


# --------------------------------------------------------------------------- #
# api / runner
# --------------------------------------------------------------------------- #


@app.command("api")
def serve_api(
    host: Annotated[str | None, typer.Option(help="监听地址；首版只允许回环")] = None,
    port: Annotated[int | None, typer.Option(help="监听端口")] = None,
    reload: Annotated[bool, typer.Option(help="开发热重载（不用于常驻部署）")] = False,
) -> None:
    """启动控制端 API + SSE。**不会自动启动 Runner。**

    注意：这里**程序化**启动 uvicorn，而不是 `uvicorn.run(...)`。
    原因是 Windows 上 uvicorn 的 `Server.run()` 会调用 `config.setup_event_loop()`
    把事件循环策略改回 `ProactorEventLoop`，而 psycopg 的异步连接在该循环上会直接
    抛 `InterfaceError`。用 `asyncio.run(server.serve())` 跳过那一步，
    保持我们在 `platform_compat` 里设置的 Selector 策略。
    """
    import uvicorn

    settings = get_settings()
    bind_host = host or settings.api_host
    if bind_host not in {"127.0.0.1", "localhost", "::1"}:
        _echo(
            f"拒绝启动：控制端 API 只允许监听回环地址，收到 {bind_host!r}。"
            "对外暴露需要先补齐鉴权与 TLS 方案。"
        )
        raise typer.Exit(code=2)

    policy = configure_event_loop_policy()
    _echo(f"事件循环策略：{policy}")

    config = uvicorn.Config(
        "agent_workbench.api.app:app",
        host=bind_host,
        port=port or settings.api_port,
        reload=reload,
        log_level="info",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


@app.command("runner")
def serve_runner(
    once: Annotated[bool, typer.Option("--once", help="只跑一轮就退出（调试用）")] = False,
    owner: Annotated[str | None, typer.Option(help="Runner 实例标识")] = None,
    recover: Annotated[bool, typer.Option(help="启动时执行恢复核对")] = True,
    only_run_id: Annotated[
        str | None,
        typer.Option(
            "--only-run-id",
            help="只推进指定的 run（演练/调试用）。不传则按常规候选扫描。",
        ),
    ] = None,
    skip_execution_cycle: Annotated[
        bool,
        typer.Option(
            "--skip-execution-cycle",
            help="只推进 --only-run-id 指定的 run，不做全局执行事件巡检（评测隔离用）",
        ),
    ] = False,
) -> None:
    """启动 Agent Runner。需要有效 run lease 才会推进任务。

    `--only-run-id` 用于**定向驱动**：当数据库里还堆着其它 queued run 时，
    调试/演练需要确定性地只处理目标 run，而不是被前面的 run 拖住。
    `--skip-execution-cycle` 进一步关闭全局执行事件巡检，只保留本题的作业唤醒，
    供评测适配器按需拉起专用 Runner 使用；默认关闭，不影响常规运行。
    """
    if skip_execution_cycle and not only_run_id:
        raise typer.BadParameter("--skip-execution-cycle 只能与 --only-run-id 一起使用")
    asyncio.run(
        _run_runner(
            once=once,
            owner=owner,
            recover=recover,
            only_run_id=only_run_id,
            execution_cycle_enabled=not skip_execution_cycle,
        )
    )


async def _run_runner(
    *,
    once: bool,
    owner: str | None,
    recover: bool,
    only_run_id: str | None = None,
    execution_cycle_enabled: bool = True,
) -> None:
    from agent_workbench.observability.logging import configure_logging, get_logger
    from agent_workbench.persistence.bootstrap import assert_business_schema_current
    from agent_workbench.persistence.db import get_database
    from agent_workbench.runtime.recovery import run_startup_recovery
    from agent_workbench.runtime.runner import AgentRunner

    configure_logging()
    log = get_logger("agent_workbench.cli")
    settings = get_settings()
    settings.ensure_directories()
    db = get_database()
    await db.start()

    target: UUID | None = None
    if only_run_id:
        try:
            target = UUID(only_run_id)
        except ValueError:
            _echo(f"--only-run-id 不是合法 UUID：{only_run_id}")
            await db.close()
            raise typer.Exit(code=2) from None

    try:
        async with db.session() as session:
            await assert_business_schema_current(session, settings)
        if recover:
            report = await run_startup_recovery(
                db,
                settings,
                only_run_id=target,
                owner_id=settings.owner_id if target is None else None,
            )
            log.info("startup_recovery", **report)
            _echo(f"启动核对：{json.dumps(report, ensure_ascii=False)}")

        runner = AgentRunner(
            settings=settings, owner=owner, database=db, production_scan=target is None
        )
        from agent_workbench.agent.prompts import PROMPT_VERSION

        log.info("runner_runtime_versions", prompt_version=PROMPT_VERSION,
                 code_version=runner.startup_runtime_versions["code_version"],
                 runtime_versions=runner.startup_runtime_versions,
                 history_assistance_mode=settings.history_assistance_mode,
                 history_assistance_force_off=settings.history_assistance_force_off,
                 scope=runner.startup_runtime_versions["scope"])
        if once:
            outcome = await runner.step(only_run_id=target)
            if outcome is None:
                _echo("当前没有可推进的 run")
            else:
                _echo(
                    f"run {outcome.run_id} -> {outcome.status}"
                    + (f"（{outcome.detail}）" if outcome.detail else "")
                )
            return
        _echo(f"Runner 已启动（owner={runner.owner}）。关闭网页不影响任务。")
        await runner.serve(only_run_id=target, execution_cycle_enabled=execution_cycle_enabled)
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# db-init
# --------------------------------------------------------------------------- #


@app.command("db-init")
def db_init(
    skip_framework: Annotated[bool, typer.Option(help="跳过框架 checkpoint/store 初始化")] = False,
    skip_alembic: Annotated[bool, typer.Option(help="跳过业务表迁移")] = False,
) -> None:
    """初始化数据库：创建 schema -> 框架表 -> 业务表迁移。

    顺序很重要：框架 `setup()` 与 Alembic 迁移**分开**执行，
    且都**不能**在 API 请求里调用（§5.1）。
    """
    asyncio.run(_db_init(skip_framework=skip_framework, skip_alembic=skip_alembic))


async def _db_init(*, skip_framework: bool, skip_alembic: bool) -> None:
    from agent_workbench.persistence.bootstrap import create_schemas, setup_framework_tables

    settings = get_settings()
    _echo(f"目标数据库：{_redact_dsn(settings.database_url)}")

    created = await create_schemas(settings)
    _echo(f"schema 创建：{created or '已存在（无变化）'}")

    if not skip_framework:
        report = await setup_framework_tables(settings)
        _echo(f"框架表初始化：{report}")
    else:
        _echo("跳过框架表初始化")

    if not skip_alembic:
        _run_alembic_upgrade(settings.database_url)
    else:
        _echo("跳过业务表迁移")

    _echo("db-init 完成")


def _run_alembic_upgrade(database_url: str) -> None:  # noqa: ARG001
    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    # 保留已应用的合法历史分支；不 stamp、不降级、不重跑另一分支的数据迁移。
    command.upgrade(cfg, "heads")
    _echo("业务表迁移：已升级到全部既有 heads")


def _redact_dsn(dsn: str) -> str:
    if "@" not in dsn:
        return dsn
    head, tail = dsn.split("@", 1)
    if ":" in head:
        scheme, _, _creds = head.partition("://")
        return f"{scheme}://***@{tail}"
    return dsn


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


@app.command("doctor")
def doctor(
    provider: Annotated[str | None, typer.Option(help="要探测的供应商")] = None,
    model: Annotated[str | None, typer.Option(help="要探测的模型")] = None,
    skip_model: Annotated[bool, typer.Option(help="跳过真实模型探针")] = False,
    skip_executor: Annotated[bool, typer.Option(help="跳过执行岛检查")] = False,
    target_url: Annotated[str | None, typer.Option(help="从执行岛探测的目标 URL")] = None,
    scope_file: Annotated[
        Path | None, typer.Option(help="目标范围 JSON 文件；与 --target-url 同用")
    ] = None,
) -> None:
    """自检：数据库、编码、模型能力、执行岛能力。

    **无凭据时相关项标 NOT_RUN，不能算通过。**
    """
    if (target_url is None) != (scope_file is None):
        raise typer.BadParameter("--target-url 与 --scope-file 必须同时提供")
    if target_url and skip_executor:
        raise typer.BadParameter("指定 --target-url 时不能跳过执行岛检查")
    asyncio.run(
        _doctor(
            provider=provider,
            model=model,
            skip_model=skip_model,
            skip_executor=skip_executor,
            target_url=target_url,
            scope_file=scope_file,
        )
    )


async def _doctor(
    *,
    provider: str | None,
    model: str | None,
    skip_model: bool,
    skip_executor: bool,
    target_url: str | None = None,
    scope_file: Path | None = None,
) -> None:
    from agent_workbench.persistence.db import get_database

    settings = get_settings()
    results: dict[str, Any] = {"version": __version__}

    # 1) 数据库
    db = get_database()
    await db.start()
    try:
        try:
            info = await db.ping()
            results["database"] = {"status": "PASS", **info}
        except Exception as exc:
            results["database"] = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}

        # 2) 业务表
        try:
            from agent_workbench.persistence.bootstrap import (
                assert_business_schema_current,
                business_schema_status,
                expected_business_revisions,
            )

            async with db.session() as session:
                status = await business_schema_status(session, settings)
                installed = (
                    await assert_business_schema_current(session, settings)
                    if status["table_count"]
                    else set()
                )
            results["business_schema"] = {
                "status": "PASS" if status["table_count"] else "NOT_RUN",
                "table_count": status["table_count"],
                "installed_revisions": sorted(installed),
                "expected_revisions": sorted(expected_business_revisions()),
                "hint": "如果没有表，请先运行 `uv run workbench db-init`",
            }
        except Exception as exc:
            results["business_schema"] = {"status": "FAIL", "error": str(exc)}
    finally:
        await db.close()

    # 3) 编码检查
    results["encoding"] = _check_encoding()

    # 4) 真实模型探针
    if skip_model:
        results["model"] = {"status": "NOT_RUN", "reason": "显式跳过"}
    else:
        results["model"] = await _probe_model(provider, model)

    # 5) 执行岛
    if skip_executor:
        results["executor"] = {"status": "NOT_RUN", "reason": "显式跳过"}
    else:
        results["executor"] = await _probe_executor(target_url=target_url, scope_file=scope_file)

    _echo(json.dumps(results, ensure_ascii=False, indent=2))

    failed = [
        name
        for name, payload in results.items()
        if isinstance(payload, dict) and payload.get("status") == "FAIL"
    ]
    if failed:
        raise typer.Exit(code=1)


def _check_encoding() -> dict[str, Any]:
    """在子进程里跑编码检查。

    两个必须显式处理的点（都曾在真实验收中被踩到）：
    1. **子进程的 I/O 编码**必须固定为 UTF-8。否则在 cp936 控制台下子进程写 GBK
       字节，父进程按 UTF-8 解码会在读线程里抛 `UnicodeDecodeError`，
       而 `subprocess` 会把这个异常吞掉，最终表现为 `proc.stdout is None`；
    2. 读取时再加 `errors="replace"` 兜底，并且**允许 stdout 为 None**，
       不能因此让整个 doctor 崩掉。
    """
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "check_encoding.py"
    if not script.exists():
        return {"status": "NOT_RUN", "reason": "找不到 scripts/check_encoding.py"}
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_process_env(),
        cwd=str(repo_root),
        check=False,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    result: dict[str, Any] = {
        "status": "PASS" if proc.returncode == 0 else "FAIL",
        "stdout": stdout[-1500:],
    }
    if stderr:
        result["stderr"] = stderr[-500:]
    return result


async def _probe_model(provider: str | None, model: str | None) -> dict[str, Any]:
    from agent_workbench.config.secrets import build_secret_store
    from agent_workbench.models.probe import run_probe

    store = build_secret_store()
    try:
        profile = __import__(
            "agent_workbench.models.provider", fromlist=["resolve_provider_profile"]
        ).resolve_provider_profile(provider, model)
    except Exception as exc:
        return {"status": "FAIL", "stage": "resolve_profile", "error": str(exc)}

    if not store.exists(profile.secret_ref):
        return {
            "status": "NOT_RUN",
            "reason": f"缺少凭据 {profile.secret_ref}（未配置），按方案标 NOT_RUN",
        }
    try:
        report = await run_probe(provider_id=provider, model_name=model, secret_store=store)
    except Exception as exc:
        return {"status": "FAIL", "stage": "probe", "error": f"{type(exc).__name__}: {exc}"}

    failed = [probe.name for probe in report.probes if not probe.ok]
    return {
        "status": "PASS" if not failed else "FAIL",
        "failed_probes": failed,
        "supports_tools": report.supports_tools,
        "supports_streaming": report.supports_streaming,
        "supports_usage": report.supports_usage,
        "summary": report.summary_lines(),
    }


async def _probe_executor(
    *, target_url: str | None = None, scope_file: Path | None = None
) -> dict[str, Any]:
    from agent_workbench.execution.client import (
        ExecutorConnectionError,
        ExecutorProtocolError,
        HttpExecutorClient,
    )
    from agent_workbench.execution.tunnel import SshTunnel, TunnelUnavailable

    settings = get_settings()
    tunnel = SshTunnel(settings)
    client = HttpExecutorClient(
        tunnel, settings=settings, expected_executor_id=settings.executor_id
    )
    try:
        await client.start()
        health = await client.health()
        caps = await client.capabilities()
        target_probe = None
        if target_url and scope_file:
            scope = json.loads(scope_file.read_text(encoding="utf-8"))
            if not isinstance(scope, dict):
                raise ValueError("范围文件必须是 JSON 对象")
            target_probe = await client.probe_target(target_url, scope)
    except (TunnelUnavailable, ExecutorConnectionError, ExecutorProtocolError) as exc:
        return {
            "status": "FAIL",
            "classification": "infrastructure_failure",
            "error": f"{type(exc).__name__}: {exc}",
            "note": "执行岛不可达属基础设施故障，不是目标网站的阴性结论。",
        }
    finally:
        await client.close()
        await tunnel.close()
    return {
        "status": "PASS"
        if target_probe is None or target_probe.get("status") == "reachable"
        else "FAIL",
        "executor_id": health.executor_id,
        "protocol_version": health.protocol_version,
        "free_bytes": health.free_bytes,
        "browser_available": caps.browser_available,
        "network_isolation": caps.network_isolation,
        "execution_mode": caps.execution_mode,
        "command_available": caps.execution_capable("command"),
        "target_probe": target_probe,
        "note": (
            "命令能力按执行端声明的 trusted 模式和依赖开放；可信执行不承诺系统级网络隔离。"
            if caps.execution_mode == "trusted" else
            "命令能力当前未启用可信执行；HTTP、浏览器与文件能力分别核对。"
        ),
    }


# --------------------------------------------------------------------------- #
# export-run / backup
# --------------------------------------------------------------------------- #


@app.command("export-run")
def export_run(
    run_id: Annotated[str, typer.Argument(help="要导出的 run id")],
    output: Annotated[Path | None, typer.Option(help="输出目录")] = None,
    bundle: Annotated[
        bool, typer.Option(help="导出同会话的本机私有实验包，包含真实工具工件")
    ] = False,
    trial: Annotated[
        Path | None, typer.Option(help="实验登记 JSON 文件，仅配合 --bundle 使用")
    ] = None,
) -> None:
    """导出运行台账、保留事件、用量与工件完整性；按已知规则脱敏。"""
    if trial is not None and not bundle:
        _echo("--trial 必须配合 --bundle 使用")
        raise typer.Exit(code=2)
    asyncio.run(_export_run(run_id=run_id, output=output, bundle=bundle, trial=trial))


async def _export_run(
    *, run_id: str, output: Path | None, bundle: bool = False, trial: Path | None = None
) -> None:
    from agent_workbench.maintenance.diagnostics import (
        collect_run_diagnostic,
        write_run_diagnostic,
    )
    from agent_workbench.persistence.db import get_database

    try:
        parsed = UUID(run_id)
    except ValueError:
        _echo(f"run id 不是合法 UUID：{run_id}")
        raise typer.Exit(code=2) from None

    settings = get_settings()
    registration = None
    if trial is not None:
        from pydantic import ValidationError

        from agent_workbench.contracts.experiments import TrialRegistration

        try:
            registration = TrialRegistration.model_validate_json(trial.read_bytes())
        except (OSError, ValidationError) as exc:
            # 校验异常可能包含操作者填入的私有原文，只展示异常类型。
            _echo(f"实验登记无法读取或不符合协议：{type(exc).__name__}")
            raise typer.Exit(code=2) from None
    db = get_database()
    await db.start()
    try:
        try:
            if bundle:
                from agent_workbench.maintenance.provenance import exporter_fingerprint
                from agent_workbench.maintenance.run_bundle import export_run_bundle

                target_dir = (
                    (output or (settings.data_root / "diagnostics")) / str(parsed) / "bundle"
                )
                source = await asyncio.to_thread(exporter_fingerprint, REPO_ROOT)
                out_dir = await export_run_bundle(
                    db, parsed, target_dir, registration=registration, exporter_source=source
                )
                _echo(f"已导出本机私有实验包（包含工具原件）：{out_dir}")
                return
            payload = await collect_run_diagnostic(db, parsed)
        except LookupError:
            _echo(f"run 不存在：{run_id}")
            raise typer.Exit(code=1) from None
        except (ValueError, FileExistsError) as exc:
            _echo(str(exc))
            raise typer.Exit(code=2) from None
        target_dir = (output or (settings.data_root / "diagnostics")) / str(parsed)
        out_file = await asyncio.to_thread(write_run_diagnostic, target_dir, payload)
        _echo(f"已导出诊断包及 SHA-256 清单：{out_file}")
    finally:
        await db.close()


@app.command("verify-bundle")
def verify_bundle(directory: Annotated[Path, typer.Argument(help="实验包目录")]) -> None:
    """离线核对实验包；哈希一致不代表目标测试成功。"""
    from pydantic import ValidationError

    from agent_workbench.maintenance.run_bundle import verify_run_bundle

    try:
        errors = verify_run_bundle(directory)
    except (OSError, ValueError, ValidationError) as exc:
        _echo(f"实验包无法读取：{type(exc).__name__}")
        raise typer.Exit(code=2) from None
    if errors:
        for error in errors:
            _echo(error)
        raise typer.Exit(code=1)
    _echo("实验包大小、SHA-256 与文件清单一致；未判定测试结论。")


@app.command("sync-model-observations")
def sync_model_observations_command() -> None:
    """将本地待同步模型观察幂等回写台账，不重新调用模型。"""

    async def sync() -> dict[str, Any]:
        from agent_workbench.models.pending_observations import sync_model_observations
        from agent_workbench.persistence.db import Database

        database = Database(get_settings())
        await database.start()
        try:
            return await sync_model_observations(database)
        finally:
            await database.close()

    result = asyncio.run(sync())
    _echo(json.dumps(result, ensure_ascii=False))
    if result["failed"]:
        raise typer.Exit(code=1)


@app.command("backup")
def backup(
    output: Annotated[Path | None, typer.Option(help="备份目录")] = None,
    include_artifacts: Annotated[bool, typer.Option(help="同时备份工件目录")] = True,
    docker_container: Annotated[
        str | None,
        typer.Option(help="在指定 PostgreSQL 容器内运行 pg_dump（本机没有客户端时）"),
    ] = None,
) -> None:
    """备份数据库（pg_dump）+ 工件。

    备份**同时包含数据库与对应工件版本**；还原演练见 docs/decisions。
    """
    import subprocess

    settings = get_settings()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = (output or settings.backup_path) / stamp
    target.mkdir(parents=True, exist_ok=False)

    _echo(f"备份目标：{target}")
    dump_file = target / "database.sql"
    if docker_container:
        from sqlalchemy.engine import make_url

        url = make_url(settings.database_url)
        if url.host not in {"localhost", "127.0.0.1", "::1"} or not url.database:
            _echo("容器备份只支持本机回环 PostgreSQL，且数据库名必须明确。")
            raise typer.Exit(code=2)
        command = [
            "docker",
            "exec",
            "-i",
            docker_container,
            "pg_dump",
            "--no-owner",
            "--no-privileges",
            "-U",
            url.username or "workbench",
            "-d",
            url.database,
        ]
    else:
        dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        command = ["pg_dump", "--no-owner", "--no-privileges", dsn]
    try:
        with dump_file.open("wb") as output_stream:
            proc = subprocess.run(
                command,
                stdout=output_stream,
                stderr=subprocess.PIPE,
                env=child_process_env(),
                check=False,
            )
    except FileNotFoundError:
        _echo("找不到 pg_dump 或 docker：请安装 PostgreSQL 客户端，或指定 --docker-container。")
        raise typer.Exit(code=1) from None
    if proc.returncode != 0:
        _echo(
            f"pg_dump 失败：{proc.stderr.decode('utf-8', errors='backslashreplace').strip()[:500]}"
        )
        raise typer.Exit(code=1)
    _echo(f"数据库已导出：{dump_file}")

    if include_artifacts and settings.artifact_root.exists():
        artifacts_target = target / "artifacts"
        shutil.copytree(settings.artifact_root, artifacts_target, dirs_exist_ok=True)
        _echo(f"工件已复制：{artifacts_target}")

    (target / "MANIFEST.txt").write_text(
        "\n".join(
            [
                f"created_at={datetime.now(UTC).isoformat()}",
                f"service_version={__version__}",
                f"database_url={_redact_dsn(settings.database_url)}",
                f"artifact_root={settings.artifact_root}",
                "note=恢复时同时还原数据库与工件目录，否则报告中的证据引用会失效。",
            ]
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    files = _backup_file_entries(target)
    (target / "MANIFEST.json").write_text(
        json.dumps(
            {"schema_version": 1, "created_at": datetime.now(UTC).isoformat(), "files": files},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _echo("备份完成")


def _backup_file_entries(root: Path) -> list[dict[str, Any]]:
    """生成或复核备份内每个普通文件的 SHA-256；拒绝符号链接。"""
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"备份中不允许符号链接：{path.relative_to(root)}")
        if not path.is_file() or path == root / "MANIFEST.json":
            continue
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        entries.append(
            {"path": path.relative_to(root).as_posix(), "size": size, "sha256": digest.hexdigest()}
        )
    return entries


@app.command("verify-backup")
def verify_backup(path: Annotated[Path, typer.Argument(help="备份目录")]) -> None:
    """离线核对数据库 dump、工件和清单是否仍与备份时一致。"""
    manifest_path = path / "MANIFEST.json"
    if not manifest_path.is_file():
        _echo("备份缺少 MANIFEST.json，不能验证完整性")
        raise typer.Exit(code=1)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = payload["files"]
        if payload["schema_version"] != 1 or not isinstance(expected, list):
            raise ValueError("清单版本或文件表不正确")
        actual = _backup_file_entries(path)
        if expected != actual or not (path / "database.sql").is_file():
            raise ValueError("备份文件缺失、内容变化或额外文件出现")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _echo(f"备份校验失败：{exc}")
        raise typer.Exit(code=1) from None
    _echo(f"备份校验通过：{len(actual)} 个文件")


@app.command("prune-events")
def prune_events(
    apply: Annotated[bool, typer.Option(help="实际删除；缺省只预览")] = False,
    days: Annotated[int | None, typer.Option(help="保留天数；缺省使用配置值")] = None,
    limit: Annotated[int, typer.Option(help="单次最多处理的事件数")] = 1000,
    session_id: Annotated[
        UUID | None, typer.Option(help="只处理指定任务；缺省处理全部任务")
    ] = None,
) -> None:
    """显式清理旧 SSE 事件；保留每个任务的最新事件及全部业务记录。"""
    settings = get_settings()
    retention_days = days if days is not None else settings.event_retention_days
    if retention_days < 1 or limit < 1:
        raise typer.BadParameter("保留天数和单次上限都必须大于零")
    asyncio.run(_prune_events(days=retention_days, limit=limit, apply=apply, session_id=session_id))


async def _prune_events(*, days: int, limit: int, apply: bool, session_id: UUID | None) -> None:
    from agent_workbench.maintenance.retention import prune_old_events
    from agent_workbench.persistence.db import Database

    db = Database()
    await db.start()
    try:
        async with db.transaction() as session:
            count = await prune_old_events(
                session, days=days, limit=limit, apply=apply, session_id=session_id
            )
    finally:
        await db.close()
    action = "已删除" if apply else "预览可删除"
    _echo(f"{action} {count} 条旧事件（保留 {days} 天；单次上限 {limit}）。")
    if count == limit:
        _echo("本次达到上限；仍可能有更多旧事件，可再次运行。")


@app.command("version")
def version() -> None:
    """打印版本。"""
    _echo(f"agent-workbench {__version__}")


@app.command("prepare-trial")
def prepare_trial_command(
    registration: Annotated[Path, typer.Argument(help="试验登记 JSON")],
    source_root: Annotated[Path, typer.Option(help="冻结的 B0/V1 checkout")],
    output: Annotated[Path, typer.Option(help="新的私有试验目录")],
) -> None:
    """生成独立数据库、数据根和记忆隔离配置；不自动访问目标。"""
    from agent_workbench.contracts.experiments import TrialRegistration
    from agent_workbench.maintenance.experiment_config import prepare_trial

    try:
        target = prepare_trial(
            TrialRegistration.model_validate_json(registration.read_bytes()),
            source_root,
            output,
            get_settings(),
        )
    except (ValueError, OSError) as exc:
        _echo(f"试验准备失败：{exc}")
        raise typer.Exit(code=1) from None
    _echo(f"试验配置已生成：{target}；请阅读 setup.json 并在启动前加载 load-environment.ps1。")


@app.command("analyze-bundle")
def analyze_bundle_command(
    paths: Annotated[list[Path], typer.Argument(help="一个或多个完整实验包")],
    output: Annotated[Path, typer.Option(help="新的离线分析 JSON 文件")],
) -> None:
    """核对包并重建工程时间线；实际效果与结论准确性保持未知。"""
    from agent_workbench.maintenance.experiment_analysis import compare_bundles

    if output.exists():
        raise typer.BadParameter("分析输出已存在，拒绝覆盖")
    try:
        result = compare_bundles(paths)
    except (OSError, ValueError) as exc:
        _echo(f"离线分析失败：{exc}")
        raise typer.Exit(code=1) from None
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _echo(f"离线分析已保存：{output}；工程指标不代表真实测试效果。")


def main(argv: list[str] | None = None) -> int:
    # 入口统一设置事件循环策略（含 Windows Selector）与 UTF-8 标准输出，
    # 避免子命令遗漏导致 cp936 控制台下中文输出崩溃。
    configure_event_loop_policy()
    configure_stdio_utf8()
    try:
        result = app(args=argv, standalone_mode=False)
    except typer.Exit as exc:  # type: ignore[attr-defined]
        return int(exc.exit_code)
    except SystemExit as exc:
        return int(exc.code or 0)
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
