"""`workbench tsec` 子命令：配置检查、题目列表、单题执行、容器释放。

不新增前端：评测入口就是命令行。所有会改动远端状态的动作（启动靶场、提交
Flag、关闭容器）都由用户显式调用；``--dry-run`` 只展示计划。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any

import typer

from agent_workbench.domain.scope import executor_scope_payload
from agent_workbench.platform_compat import configure_stdio_utf8
from agent_workbench.tsecbench.checks import (
    FAIL,
    NOT_RUN,
    PASS,
    ProbeError,
    api_runtime_status,
    executor_status,
    kali_network_status,
    platform_status,
    probe_targets,
)
from agent_workbench.tsecbench.config import BenchmarkConfig, ConfigError, load_config
from agent_workbench.tsecbench.flow import FlowOptions, FlowOutcome, SingleChallengeFlow
from agent_workbench.tsecbench.objective import build_scope, parse_address
from agent_workbench.tsecbench.platform import PlatformError, SdkPlatformClient
from agent_workbench.tsecbench.workbench_api import HttpWorkbenchApi, WorkbenchApiError

tsec_app = typer.Typer(
    name="tsec",
    help="TSecBench 评测接入（单题串行，不改 Agent 主流程）",
    no_args_is_help=True,
    add_completion=False,
)


def _echo(message: str) -> None:
    configure_stdio_utf8()
    typer.echo(message)


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _fail(message: str, code: int = 2) -> None:
    _echo(message)
    raise typer.Exit(code=code)


def _config_or_exit() -> BenchmarkConfig:
    try:
        return load_config()
    except ConfigError as exc:
        _fail(str(exc))
        raise


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


@tsec_app.command("doctor")
def doctor(
    skip_platform: Annotated[bool, typer.Option("--skip-platform", help="跳过平台/VPN 检查")] = False,
    skip_executor: Annotated[bool, typer.Option("--skip-executor", help="跳过 Kali 检查")] = False,
    json_output: Annotated[bool, typer.Option("--json", help="输出机器可读 JSON")] = False,
) -> None:
    """检查配置、本机 API 指纹、平台 VPN 与 Kali 执行岛。不启动渗透测试。"""
    config = _config_or_exit()
    result = asyncio.run(
        _doctor(config, skip_platform=skip_platform, skip_executor=skip_executor)
    )
    if json_output:
        _echo(_dump(result))
    else:
        _print_doctor(result)
    failed = [name for name, payload in result.items() if payload.get("status") == FAIL]
    raise typer.Exit(code=1 if failed else 0)


async def _doctor(
    config: BenchmarkConfig, *, skip_platform: bool, skip_executor: bool
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}

    missing = []
    if not config.token_present:
        missing.append("BENCHMARK_TOKEN")
    if not config.base_url:
        missing.append("BENCHMARK_BASE_URL")
    ovpn = Path(str(config.ovpn_path)).expanduser() if config.ovpn_path else None
    result["config"] = {
        "status": FAIL if missing else PASS,
        "base_url": config.base_url,
        "token_present": config.token_present,
        "token_fingerprint": config.token_fingerprint,
        "api_base_url": config.api_base_url,
        "api_token_present": bool(config.api_token)
        and not config.api_token.startswith("change-me"),
        "env_file": config.env_file,
        "ovpn_path": config.ovpn_path,
        "ovpn_present": bool(ovpn and ovpn.is_file()),
        "missing": missing,
    }

    async with HttpWorkbenchApi(config) as api:
        try:
            result["api"] = await api_runtime_status(config, api)
        except WorkbenchApiError as exc:
            result["api"] = {"status": FAIL, "error": str(exc)}
        except Exception as exc:
            result["api"] = {"status": FAIL, "error": f"{type(exc).__name__}: {exc}"}

    if skip_platform:
        result["platform"] = {"status": NOT_RUN, "reason": "显式跳过"}
    elif missing:
        result["platform"] = {
            "status": FAIL,
            "stage": "config",
            "error": "缺少平台配置，无法做 VPN 预检",
        }
    else:
        client = SdkPlatformClient(config, auto_check_vpn=False)
        try:
            await client.open()
            result["platform"] = await platform_status(config, client)
        except PlatformError as exc:
            result["platform"] = {"status": FAIL, "stage": "platform", "error": str(exc)}
        except Exception as exc:
            result["platform"] = {
                "status": FAIL,
                "stage": "platform",
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            await client.close()

    if skip_executor:
        result["executor"] = {"status": NOT_RUN, "reason": "显式跳过"}
    else:
        result["executor"] = await executor_status()

    return result


def _print_doctor(result: dict[str, dict[str, Any]]) -> None:
    for name, payload in result.items():
        _echo(f"[{payload.get('status')}] {name}")
        for key, value in payload.items():
            if key == "status":
                continue
            _echo(f"    {key}: {value}")
    _echo("")
    _echo("说明：config/platform 的 FAIL 会阻止调用平台；api FAIL 会阻止创建 Run；")
    _echo("executor FAIL 表示 Kali 不可达（属基础设施故障，不是目标阴性结论）。")


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #


@tsec_app.command("list")
def list_command(
    all_challenges: Annotated[
        bool, typer.Option("--all", help="包含已完成的题目（默认只列未完成）")
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="输出机器可读 JSON")] = False,
) -> None:
    """列出题目与 flag 进度。只读：不会启动容器。"""
    config = _config_or_exit()
    try:
        config.require_platform_ready()
    except ConfigError as exc:
        _fail(str(exc))
        return

    async def _list() -> list[dict[str, Any]]:
        # 列表只访问平台 API（公网），不依赖 VPN；VPN 仅在启动容器后才必需。
        client = SdkPlatformClient(config, auto_check_vpn=False)
        try:
            await client.open()
            rows = await client.list_challenges()
        finally:
            await client.close()
        return [
            {
                "unique_code": item.unique_code,
                "difficulty": item.difficulty,
                "level": item.level,
                "total_score": item.total_score,
                "flags": f"{item.correct_flag_count}/{item.flag_count}",
                "completed": item.is_completed,
                "container_status": item.container_status,
                "addresses": list(item.container_addr),
                "summary": (item.description or "").strip().splitlines()[0][:40]
                if (item.description or "").strip()
                else "",
            }
            for item in rows
            if all_challenges or not item.is_completed
        ]

    try:
        rows = asyncio.run(_list())
    except PlatformError as exc:
        _fail(str(exc), code=1)
        return

    if json_output:
        _echo(_dump(rows))
        return
    if not rows:
        _echo("没有符合条件的题目（可用 --all 查看已完成题目）。")
        return
    _echo(f"{'unique_code':<28}{'难度':<8}{'flag':<8}{'完成':<6}{'容器':<12}描述")
    for row in rows:
        _echo(
            f"{row['unique_code']:<28}{row['difficulty']:<8}{row['flags']:<8}"
            f"{row['completed']!s:<6}{row['container_status']:<12}{row['summary']}"
        )


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #


@tsec_app.command("run")
def run_command(
    unique_code: Annotated[str, typer.Argument(help="要执行的题目 unique_code")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="只展示目标与范围，不启动容器/不提交")
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="已完成的题目也重跑")] = False,
    skip_reachability: Annotated[
        bool,
        typer.Option(
            "--skip-reachability",
            help="跳过 Kali 对靶场的 HTTP(S) 预检（仅确认是 TLS/非 HTTP 目标时使用）",
        ),
    ] = False,
    max_seconds: Annotated[
        float | None, typer.Option("--max-seconds", help="单题墙钟上限（秒）")
    ] = None,
    max_model_calls: Annotated[
        int | None, typer.Option("--max-model-calls", help="单题模型调用上限")
    ] = None,
    max_tool_jobs: Annotated[
        int | None, typer.Option("--max-tool-jobs", help="单题工具操作上限")
    ] = None,
    poll_interval: Annotated[
        float | None, typer.Option("--poll", help="状态轮询间隔（秒）")
    ] = None,
    spawn_runner: Annotated[
        bool,
        typer.Option(
            "--spawn-runner/--no-spawn-runner",
            help="为本题启动专用 Runner（只推进本 Run，避免碰库里的历史 queued 任务）",
        ),
    ] = True,
) -> None:
    """执行单题：启动靶场 → 创建 Run → 等待 → 提交 Flag → 关闭容器。"""
    try:
        config = load_config(
            overrides={
                "max_seconds": max_seconds,
                "max_model_calls": max_model_calls,
                "max_tool_jobs": max_tool_jobs,
                "poll_interval": poll_interval,
            }
        )
        config.require_ready()
    except ConfigError as exc:
        _fail(str(exc))
        return

    options = FlowOptions(
        unique_code=unique_code,
        dry_run=dry_run,
        force=force,
        skip_reachability=skip_reachability,
        max_seconds=config.max_seconds,
        max_model_calls=config.max_model_calls,
        max_tool_jobs=config.max_tool_jobs,
        poll_interval=config.poll_interval,
        cancel_grace=config.cancel_grace,
        spawn_runner=spawn_runner,
    )
    platform = SdkPlatformClient(config)
    api = HttpWorkbenchApi(config)

    async def _run() -> FlowOutcome:
        async with api:
            flow = SingleChallengeFlow(
                config=config,
                platform=platform,
                api=api,
                options=options,
                echo=_echo,
            )
            return await flow.run()

    try:
        outcome = asyncio.run(_run())
    except WorkbenchApiError as exc:
        _fail(str(exc), code=1)
        return
    except PlatformError as exc:
        _fail(str(exc), code=1)
        return

    _print_outcome(outcome)
    if outcome.outcome in {"completed", "dry_run", "skipped"}:
        return
    raise typer.Exit(code=1 if outcome.outcome == "failed" else 2)


def _print_outcome(outcome: FlowOutcome) -> None:
    _echo("")
    _echo("===== 评测结果 =====")
    _echo(f"题目：{outcome.unique_code}")
    _echo(f"结论：{outcome.outcome}" + ("" if outcome.settled else "（存在未决远程执行）"))
    if outcome.run_id:
        _echo(f"Run：{outcome.run_id}（status={outcome.run_status}）")
    if outcome.platform_progress:
        _echo(f"平台进度：{outcome.platform_progress}")
    _echo(f"Flag 候选：{len(outcome.candidates)}；提交：{len(outcome.submissions)}")
    for item in outcome.submissions:
        flag_state = "重复" if item.duplicate else ("正确" if item.correct else "错误")
        extra = f"，错误：{item.error}" if item.error else ""
        _echo(f"  - {item.candidate.preview} -> {flag_state}（{item.progress}）{extra}")
    if outcome.unresolved_jobs:
        _echo("未决远程执行（不能视为完成）：")
        for job in outcome.unresolved_jobs:
            _echo(f"  - {job}")
    if outcome.record_path:
        _echo(f"记录：{outcome.record_path}")
    for note in outcome.notes:
        _echo(f"说明：{note}")


# --------------------------------------------------------------------------- #
# release / net check
# --------------------------------------------------------------------------- #


@tsec_app.command("release")
def release_command(
    unique_code: Annotated[str, typer.Argument(help="要关闭容器的 unique_code")],
) -> None:
    """手动关闭题目容器（不依赖 VPN 预检，只调平台关闭接口）。"""
    config = _config_or_exit()
    try:
        config.require_platform_ready()
    except ConfigError as exc:
        _fail(str(exc))
        return

    async def _release() -> bool:
        client = SdkPlatformClient(config, auto_check_vpn=False)
        try:
            await client.open()
            return await client.close_challenge(unique_code)
        finally:
            await client.close()

    try:
        closed = asyncio.run(_release())
    except PlatformError as exc:
        _fail(str(exc), code=1)
        return
    if not closed:
        _fail("平台返回 closed=false：容器可能仍在运行，请到平台控制台确认。", code=1)
        return
    _echo(f"{unique_code} 的容器已关闭。")


@tsec_app.command("net")
def net_command(
    target: Annotated[
        list[str] | None,
        typer.Option("--target", help="从 Kali 探测的授权地址 IP:端口（可重复）"),
    ] = None,
    skip_platform: Annotated[bool, typer.Option("--skip-platform")] = False,
    skip_executor: Annotated[bool, typer.Option("--skip-executor")] = False,
) -> None:
    """两层连通性检查：Windows→平台 VPN、Kali→靶场地址。"""
    config = _config_or_exit()
    results: dict[str, Any] = {}
    if skip_platform:
        results["platform_vpn"] = {"status": NOT_RUN, "reason": "显式跳过"}
    elif not config.token_present or not config.base_url:
        results["platform_vpn"] = {
            "status": FAIL,
            "error": "缺少 BENCHMARK_TOKEN / BENCHMARK_BASE_URL，无法做 VPN 预检",
        }
    else:
        results["platform_vpn"] = asyncio.run(_check_platform_vpn(config))

    if skip_executor:
        results["kali"] = {"status": NOT_RUN, "reason": "显式跳过"}
        results["kali_network"] = {"status": NOT_RUN, "reason": "显式跳过"}
        results["targets"] = {"status": NOT_RUN, "reason": "显式跳过"}
    else:
        results["kali"] = asyncio.run(executor_status())
        results["kali_network"] = asyncio.run(kali_network_status())
        results["targets"] = asyncio.run(_check_targets(target or []))

    _echo(_dump(results))
    if any(payload.get("status") == FAIL for payload in results.values()):
        raise typer.Exit(code=1)


async def _check_platform_vpn(config: BenchmarkConfig) -> dict[str, Any]:
    client = SdkPlatformClient(config, auto_check_vpn=False)
    try:
        await client.open()
        vpn = await client.check_vpn()
        return {
            "status": PASS if vpn.ok else FAIL,
            "client_ip": vpn.client_ip,
            "checked_at": vpn.checked_at,
            "note": "这是运行本命令的主机（控制端）看到的 VPN 地址。",
        }
    except PlatformError as exc:
        return {"status": FAIL, "error": str(exc)}
    finally:
        await client.close()


async def _check_targets(raw_addresses: list[str]) -> dict[str, Any]:
    if not raw_addresses:
        return {
            "status": NOT_RUN,
            "reason": "未提供 --target；先启动授权题目后再检查靶场连通性。",
        }
    try:
        targets = [parse_address(raw) for raw in raw_addresses]
    except ValueError as exc:
        return {"status": FAIL, "error": str(exc)}
    scope = build_scope(targets, "net-check")
    urls = [url for target in targets for url in target.url_candidates]
    try:
        probes = await probe_targets(urls, executor_scope_payload(scope))
    except ProbeError as exc:
        return {"status": FAIL, "error": str(exc)}
    reachable = [item for item in probes if item.reachable]
    return {
        "status": PASS if reachable else FAIL,
        "probes": [
            {
                "url": item.url,
                "status": item.status,
                "stage": item.stage,
                "http_status": item.http_status,
                "detail": item.detail,
            }
            for item in probes
        ],
        "note": (
            "HTTP(S) 探针全失败不代表一定不通：自签名 TLS 或非 HTTP 服务也会失败。"
            "确认属此类后可对 run 使用 --skip-reachability。"
        ),
    }


__all__ = ["tsec_app"]
