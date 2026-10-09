"""`workbench` 命令行入口（§16.2）。

目标入口：`api`、`runner`、`db-init`、`doctor`、`export-run`、`backup`。
Executor 有独立启动与 doctor 命令（`workbench-executor` / `python -m workbench_executor`）。

一键开发启动应能明确启动/停止三个本地服务与数据库。
**API reload 不自动生成第二个 Runner。**
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer

from agent_workbench import __version__
from agent_workbench.config.settings import get_settings
from agent_workbench.platform_compat import (
    child_process_env,
    configure_event_loop_policy,
    configure_stdio_utf8,
)

app = typer.Typer(
    name="workbench",
    help="Web/API 自主黑盒测试工作台（控制端）",
    no_args_is_help=True,
    add_completion=False,
)


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
) -> None:
    """启动 Agent Runner。需要有效 run lease 才会推进任务。

    `--only-run-id` 用于**定向驱动**：当数据库里还堆着其它 queued run 时，
    调试/演练需要确定性地只处理目标 run，而不是被前面的 run 拖住。
    """
    asyncio.run(_run_runner(once=once, owner=owner, recover=recover, only_run_id=only_run_id))


async def _run_runner(
    *, once: bool, owner: str | None, recover: bool, only_run_id: str | None = None
) -> None:
    from agent_workbench.observability.logging import configure_logging, get_logger
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
        if recover:
            report = await run_startup_recovery(db, settings)
            log.info("startup_recovery", **report)
            _echo(f"启动核对：{json.dumps(report, ensure_ascii=False)}")

        runner = AgentRunner(settings=settings, owner=owner, database=db)
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
        await runner.serve()
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
    command.upgrade(cfg, "head")
    _echo("业务表迁移：已升级到 head")


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
) -> None:
    """自检：数据库、编码、模型能力、执行岛能力。

    **无凭据时相关项标 NOT_RUN，不能算通过。**
    """
    asyncio.run(
        _doctor(provider=provider, model=model, skip_model=skip_model, skip_executor=skip_executor)
    )


async def _doctor(
    *, provider: str | None, model: str | None, skip_model: bool, skip_executor: bool
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
            from agent_workbench.persistence.bootstrap import business_schema_status

            async with db.session() as session:
                status = await business_schema_status(session, settings)
            results["business_schema"] = {
                "status": "PASS" if status["table_count"] else "NOT_RUN",
                "table_count": status["table_count"],
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
        results["executor"] = await _probe_executor()

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


async def _probe_executor() -> dict[str, Any]:
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
        "status": "PASS",
        "executor_id": health.executor_id,
        "protocol_version": health.protocol_version,
        "free_bytes": health.free_bytes,
        "browser_available": caps.browser_available,
        "network_isolation": caps.network_isolation,
        "note": ("network_isolation=unavailable 时通用命令的无人值守模式必须保持关闭。"),
    }


# --------------------------------------------------------------------------- #
# export-run / backup
# --------------------------------------------------------------------------- #


@app.command("export-run")
def export_run(
    run_id: Annotated[str, typer.Argument(help="要导出的 run id")],
    output: Annotated[Path | None, typer.Option(help="输出目录")] = None,
) -> None:
    """导出脱敏诊断包：版本、状态与事件、模型调用元数据、（不含密钥）。"""
    asyncio.run(_export_run(run_id=run_id, output=output))


async def _export_run(*, run_id: str, output: Path | None) -> None:
    from uuid import UUID

    from sqlalchemy import select

    from agent_workbench.contracts.enums import RunStatus  # noqa: F401
    from agent_workbench.observability.events import read_events_after
    from agent_workbench.persistence.db import get_database
    from agent_workbench.persistence.repositories import ModelCallRepository
    from agent_workbench.persistence.tables import RunRow

    try:
        parsed = UUID(run_id)
    except ValueError:
        _echo(f"run id 不是合法 UUID：{run_id}")
        raise typer.Exit(code=2) from None

    settings = get_settings()
    db = get_database()
    await db.start()
    try:
        async with db.session() as session:
            run = (
                await session.execute(select(RunRow).where(RunRow.id == parsed))
            ).scalar_one_or_none()
            if run is None:
                _echo(f"run 不存在：{run_id}")
                raise typer.Exit(code=1)
            calls = await ModelCallRepository(session).list_for_run(parsed)
            events = await read_events_after(session, session_id=run.session_id, limit=10000)

        target_dir = (output or (settings.data_root / "diagnostics")) / str(parsed)
        target_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "exported_at": datetime.now(UTC).isoformat(),
            "service_version": __version__,
            "run": {
                "id": str(run.id),
                "session_id": str(run.session_id),
                "status": run.status,
                "completion_reason": run.completion_reason,
                "provider_id": run.provider_id,
                "model_name": run.model_name,
                "code_version": run.code_version,
                "prompt_version": run.prompt_version,
                "config_snapshot": run.config_snapshot,
                "stop_reason": run.stop_reason,
            },
            "model_calls": [
                {
                    "model_call_id": str(row.model_call_id),
                    "logical_call_id": str(row.logical_call_id),
                    "retry_index": row.retry_index,
                    "phase": row.phase,
                    "usage_status": row.usage_status,
                    "cost_status": row.cost_status,
                    "cost_amount": str(row.cost_amount) if row.cost_amount is not None else None,
                    "cost_currency": row.cost_currency,
                    "input_tokens": row.input_tokens,
                    "output_tokens": row.output_tokens,
                    "attempts_observable": row.attempts_observable,
                    "error_code": row.error_code,
                }
                for row in calls
            ],
            "events": [envelope.model_dump(mode="json") for envelope in events],
            "redaction": (
                "本包默认不含密钥、Cookie、Authorization 与请求体正文；"
                "工件正文请通过受控接口单独导出。"
            ),
        }
        out_file = target_dir / "diagnostic.json"
        out_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        _echo(f"已导出诊断包：{out_file}")
    finally:
        await db.close()


@app.command("backup")
def backup(
    output: Annotated[Path | None, typer.Option(help="备份目录")] = None,
    include_artifacts: Annotated[bool, typer.Option(help="同时备份工件目录")] = True,
) -> None:
    """备份数据库（pg_dump）+ 工件。

    备份**同时包含数据库与对应工件版本**；还原演练见 docs/decisions。
    """
    import subprocess

    settings = get_settings()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = (output or settings.backup_path) / stamp
    target.mkdir(parents=True, exist_ok=True)

    _echo(f"备份目标：{target}")
    dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    dump_file = target / "database.sql"
    try:
        proc = subprocess.run(
            ["pg_dump", "--no-owner", "--no-privileges", "-f", str(dump_file), dsn],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_process_env(),
            check=False,
        )
    except FileNotFoundError:
        _echo("找不到 pg_dump：请在 PATH 中提供 PostgreSQL 客户端，或用容器内 pg_dump 执行。")
        raise typer.Exit(code=1) from None
    if proc.returncode != 0:
        _echo(f"pg_dump 失败：{(proc.stderr or '').strip()[:500]}")
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
    _echo("备份完成")


@app.command("version")
def version() -> None:
    """打印版本。"""
    _echo(f"agent-workbench {__version__}")


def main(argv: list[str] | None = None) -> int:
    # 入口统一设置事件循环策略（含 Windows Selector）与 UTF-8 标准输出，
    # 避免子命令遗漏导致 cp936 控制台下中文输出崩溃。
    configure_event_loop_policy()
    configure_stdio_utf8()
    try:
        app(args=argv, standalone_mode=False)
    except typer.Exit as exc:  # type: ignore[attr-defined]
        return int(exc.exit_code)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
