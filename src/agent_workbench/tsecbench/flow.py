"""单题端到端流程：启动靶场 → 创建现有 Run → 等待 → 取 Flag → 提交 → 关闭容器。

设计原则：
- **复用**现有本机 API 创建/观察 Run，不自己驱动图，也不另造 Runner；
- 任何路径（成功、失败、取消、异常）都走同一个 ``finally`` 尝试关闭容器；
- 不把"已生成最终文本"当成"远程执行已收尾"：未决 Job 单独记录并显式呈现；
- 平台判定是 Flag 唯一权威；适配器只做候选校验、去重与节奏控制。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_workbench.config.settings import REPO_ROOT, get_settings
from agent_workbench.domain.scope import executor_scope_payload
from agent_workbench.platform_compat import child_process_env
from agent_workbench.tsecbench.checks import ProbeError, ProbeResult, probe_targets
from agent_workbench.tsecbench.config import BenchmarkConfig
from agent_workbench.tsecbench.flags import FlagCandidate, extract_flag_candidates
from agent_workbench.tsecbench.objective import (
    ContainerTarget,
    build_objective,
    build_scope,
    default_policy,
    description_digest,
    parse_addresses,
)
from agent_workbench.tsecbench.platform import (
    ChallengeConflict,
    ChallengeInfo,
    PlatformClient,
    PlatformError,
    PlatformUnavailable,
    StartInfo,
)
from agent_workbench.tsecbench.records import write_record
from agent_workbench.tsecbench.workbench_api import WorkbenchApi, WorkbenchApiError

TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})

ProbeFn = Callable[[list[str], dict[str, Any]], Awaitable[list[ProbeResult]]]
SleepFn = Callable[[float], Awaitable[Any]]
ClockFn = Callable[[], float]


class FlowError(RuntimeError):
    """流程前置条件不满足；消息面向操作者。"""


@dataclass(frozen=True, slots=True)
class FlowOptions:
    unique_code: str
    dry_run: bool = False
    force: bool = False
    skip_reachability: bool = False
    max_seconds: float = 3600.0
    max_model_calls: int | None = None
    max_tool_jobs: int | None = None
    poll_interval: float = 10.0
    cancel_grace: float = 120.0
    max_flag_candidates: int = 16
    #: 只为本题拉起一个专用 Runner（只推进本 Run，不做全局执行事件巡检）。
    #: 数据库里可能积压大量历史 queued run，通用 Runner 会去推进它们。
    spawn_runner: bool = True


@dataclass
class SpawnedRunner:
    process: subprocess.Popen[bytes]
    log_path: Path


@dataclass(frozen=True, slots=True)
class MonitorResult:
    status: str
    cancelled: bool
    reason: str | None
    elapsed_seconds: float
    model_calls: int
    tool_operations: int
    completion_reason: str | None = None
    final_answer_saved: bool = False


@dataclass(frozen=True, slots=True)
class SubmissionRecord:
    candidate: FlagCandidate
    correct: bool
    awarded: int
    duplicate: bool
    progress: str
    error: str | None = None


@dataclass
class FlowOutcome:
    outcome: str
    unique_code: str
    project_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None
    run_status: str | None = None
    record_path: str | None = None
    settled: bool = True
    unresolved_jobs: list[str] = field(default_factory=list)
    candidates: list[FlagCandidate] = field(default_factory=list)
    submissions: list[SubmissionRecord] = field(default_factory=list)
    platform_progress: str = ""
    flags_correct_after: int | None = None
    notes: list[str] = field(default_factory=list)


class SingleChallengeFlow:
    """一次只处理一道题；调用方保证串行。"""

    def __init__(
        self,
        *,
        config: BenchmarkConfig,
        platform: PlatformClient,
        api: WorkbenchApi,
        options: FlowOptions,
        data_root: Path | None = None,
        probe: ProbeFn | None = None,
        sleep: SleepFn = asyncio.sleep,
        clock: ClockFn = time.monotonic,
        echo: Callable[[str], None] = print,
        runner_factory: Callable[[str], SpawnedRunner] | None = None,
    ) -> None:
        self._config = config
        self._platform = platform
        self._api = api
        self._options = options
        self._data_root = data_root or get_settings().data_root
        self._probe = probe or probe_targets
        self._sleep = sleep
        self._clock = clock
        self._echo = echo
        self._runner_factory = runner_factory or self._spawn_runner
        self._runner: SpawnedRunner | None = None

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #

    async def run(self) -> FlowOutcome:
        options = self._options
        outcome = FlowOutcome(outcome="failed", unique_code=options.unique_code)
        challenge: ChallengeInfo | None = None
        targets: list[ContainerTarget] = []
        probes: list[ProbeResult] = []
        scope_payload: dict[str, Any] | None = None
        objective = ""
        started = False
        monitor: MonitorResult | None = None
        final_text = ""
        final_artifact: str | None = None
        final_challenge: ChallengeInfo | None = None
        record_needed = False
        runner_info: dict[str, Any] | None = None

        try:
            await self._platform.open()
            challenges = await self._platform.list_challenges()
            challenge = self._select(challenges)
            self._echo(
                f"题目 {challenge.unique_code}：难度 {challenge.difficulty}，"
                f"flag {challenge.correct_flag_count}/{challenge.flag_count}，"
                f"容器状态 {challenge.container_status}"
            )
            if challenge.is_completed and not options.force:
                outcome.outcome = "skipped"
                outcome.notes.append("题目已完成；默认跳过（--force 可强制重跑）。")
                self._echo("该题已全部完成，跳过。")
                return outcome

            if options.dry_run:
                return self._dry_run(challenge, outcome)

            record_needed = True
            try:
                start = await self._platform.start_challenge(challenge.unique_code)
            except ChallengeConflict:
                if challenge.container_addr and challenge.container_status == "available":
                    start = StartInfo(
                        unique_code=challenge.unique_code,
                        addresses=challenge.container_addr,
                    )
                    outcome.notes.append("复用平台已运行的容器（start 被拒绝，但地址可用）。")
                else:
                    raise
            started = True
            addresses = start.addresses or challenge.container_addr
            targets = parse_addresses(addresses)
            self._echo("靶场地址：" + "、".join(target.origin for target in targets))
            scope = build_scope(targets, challenge.unique_code)
            scope_payload = scope.model_dump()
            objective = build_objective(challenge, targets, max_seconds=options.max_seconds)

            if options.skip_reachability:
                outcome.notes.append("已跳过 Kali 连通性预检（--skip-reachability）。")
            else:
                urls = [url for target in targets for url in target.url_candidates]
                probes = await self._probe(urls, executor_scope_payload(scope))
                if not any(item.reachable for item in probes):
                    raise FlowError(
                        "从 Kali 执行岛访问授权地址的 HTTP/HTTPS 探针全部失败。"
                        "常见原因：Kali 未连接评测 VPN、VMware NAT 未放通，或目标只提供"
                        "非 HTTP 服务/自签名 TLS。请先运行 `workbench tsec net check --target "
                        f"{targets[0].origin}` 核对；确认属 TLS 或非 HTTP 后可用 "
                        "--skip-reachability 跳过本预检。"
                    )
                self._echo(
                    "Kali 可达性："
                    + "；".join(f"{item.url} -> {item.status}" for item in probes)
                )

            project_id = await self._api.create_project(
                name=f"TSecBench {challenge.unique_code} "
                f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
                target_description=(
                    f"TSecBench 授权靶场 {challenge.unique_code}；"
                    f"地址 {', '.join(target.origin for target in targets)}"
                ),
                scope=scope_payload,
            )
            outcome.project_id = project_id
            created = await self._api.create_session_and_run(
                project_id=project_id,
                title=f"TSecBench {challenge.unique_code}",
                objective=objective,
                scope=scope_payload,
                policy=default_policy().model_dump(),
            )
            outcome.session_id = str(created["session"]["id"])
            outcome.run_id = str(created["run"]["id"])
            self._echo(
                f"已创建 Run {outcome.run_id}（session {outcome.session_id}），开始等待执行。"
            )
            if options.spawn_runner:
                try:
                    self._runner = self._runner_factory(outcome.run_id)
                except OSError as exc:
                    raise FlowError(
                        f"无法为本题启动专用 Runner：{type(exc).__name__}: {exc}。"
                        "如已有专用 Runner 在跑，可用 --no-spawn-runner 关闭。"
                    ) from exc
                self._echo(
                    f"已按需启动专用 Runner（pid={self._runner.process.pid}），"
                    f"只推进本 Run；日志：{self._runner.log_path}"
                )

            monitor = await self._monitor(outcome.session_id, outcome.run_id)
            outcome.run_status = monitor.status

            final_text, final_artifact = await self._api.read_final_answer(
                outcome.session_id, outcome.run_id
            )
            if final_text:
                outcome.candidates = extract_flag_candidates(
                    final_text,
                    origin=f"final_answer:{outcome.run_id}",
                    max_candidates=options.max_flag_candidates,
                )
                self._echo(
                    f"最终回答 {len(final_text)} 字符，提取到 {len(outcome.candidates)} 个 Flag 候选。"
                )
            else:
                outcome.notes.append("未读取到该 Run 的最终回答；没有可提交的 Flag 候选。")

            outcome.submissions = await self._submit(
                challenge, outcome.candidates, outcome
            )
            final_challenge = await self._refresh_challenge(challenge, outcome)
            outcome.platform_progress = (
                f"{final_challenge.correct_flag_count}/{final_challenge.flag_count}"
                if final_challenge
                else "未知（刷新题目进度失败）"
            )

            unresolved = await self._unresolved_jobs(outcome.session_id)
            outcome.unresolved_jobs = unresolved
            outcome.settled = not unresolved
            if unresolved:
                outcome.notes.append(
                    "Run 仍有未确认的远程执行；不能据此认定作业已完成。"
                    "关闭容器只释放靶场资源，不代表这些作业成功。"
                )

            outcome.outcome = self._classify(
                monitor, final_challenge or challenge, outcome
            )
            return outcome
        except FlowError as exc:
            outcome.outcome = "failed"
            outcome.notes.append(str(exc))
            self._echo(f"[失败] {exc}")
            return outcome
        except (PlatformError, WorkbenchApiError, ProbeError, ValueError) as exc:
            outcome.outcome = "failed"
            outcome.notes.append(f"{type(exc).__name__}: {exc}")
            self._echo(f"[失败] {type(exc).__name__}: {exc}")
            return outcome
        finally:
            try:
                if started:
                    closed, close_error = await self._close_container(options.unique_code)
                    if not closed:
                        outcome.notes.append(
                            f"容器关闭未确认：{close_error}。请执行 "
                            f"`uv run workbench tsec release {options.unique_code}` 重试，"
                            "或到平台控制台手动关闭，避免占用活跃名额。"
                        )
                else:
                    closed, close_error = (None, None)

                runner_info = self._stop_runner()

                if record_needed:
                    record = self._build_record(
                        outcome=outcome,
                        challenge=challenge,
                        final_challenge=final_challenge,
                        targets=targets,
                        probes=probes,
                        monitor=monitor,
                        final_text=final_text,
                        final_artifact=final_artifact,
                        close_attempted=started,
                        closed=closed,
                        close_error=close_error,
                        runner_info=runner_info,
                    )
                    path = write_record(
                        record,
                        data_root=self._data_root,
                        unique_code=options.unique_code,
                    )
                    outcome.record_path = str(path)
                    self._echo(f"运行记录：{path}")
            finally:
                await self._platform.close()

    # ------------------------------------------------------------------ #
    # 选择 / 预演
    # ------------------------------------------------------------------ #

    def _select(self, challenges: list[ChallengeInfo]) -> ChallengeInfo:
        for item in challenges:
            if item.unique_code == self._options.unique_code:
                return item
        available = "、".join(item.unique_code for item in challenges[:20]) or "（空）"
        raise FlowError(
            f"平台题目集中没有 {self._options.unique_code}。可用的 unique_code：{available}"
        )

    def _dry_run(self, challenge: ChallengeInfo, outcome: FlowOutcome) -> FlowOutcome:
        """只展示计划：不启动容器、不创建 Run、不提交。"""
        outcome.outcome = "dry_run"
        addresses = challenge.container_addr
        if not addresses:
            self._echo("容器未启动，平台尚未下发地址；以下仅展示题目与流程计划。")
            self._echo(f"题目：{challenge.unique_code}（{challenge.difficulty}）")
            self._echo(f"flag 进度：{challenge.correct_flag_count}/{challenge.flag_count}")
            self._echo(
                "后续会依次执行：start_challenge → 校验地址 → Kali 预检 → "
                "创建独立 Run → 等待结束 → 提取并提交 Flag → close_challenge。"
            )
            return outcome
        targets = parse_addresses(addresses)
        scope = build_scope(targets, challenge.unique_code)
        objective = build_objective(challenge, targets, max_seconds=self._options.max_seconds)
        self._echo("=== dry-run：不会启动容器/创建 Run/提交 Flag ===")
        self._echo("授权范围（严格模式）：")
        for rule in scope.rules:
            self._echo(f"- {rule.host}:{rule.port}（协议 any，允许私网）")
        self._echo("将提交给 Agent 的目标：")
        self._echo(objective)
        return outcome

    # ------------------------------------------------------------------ #
    # 监控
    # ------------------------------------------------------------------ #

    async def _monitor(self, session_id: str, run_id: str) -> MonitorResult:
        options = self._options
        started_at = self._clock()
        warned_queued = False
        cancel_reason: str | None = None
        model_calls = 0
        operations = 0
        status = "queued"
        completion_reason: str | None = None
        final_answer_saved = False

        while True:
            snapshot = await self._api.snapshot(session_id)
            run = self._find_run(snapshot, run_id)
            status = str(run.get("status"))
            completion_reason = run.get("completion_reason")
            final_answer_saved = bool(run.get("model_response_saved"))
            elapsed = self._clock() - started_at

            if status in TERMINAL_RUN_STATUSES:
                break
            if status == "waiting_user":
                cancel_reason = "agent_waiting_user"
                break
            if elapsed >= options.max_seconds:
                cancel_reason = "max_seconds"
                break

            if options.max_model_calls is not None:
                model_calls = await self._api.run_model_calls(session_id, run_id)
                if model_calls >= options.max_model_calls:
                    cancel_reason = "max_model_calls"
                    break
            if options.max_tool_jobs is not None:
                operations = await self._api.count_operations(session_id)
                if operations >= options.max_tool_jobs:
                    cancel_reason = "max_tool_jobs"
                    break

            if status == "queued" and elapsed > 60 and not warned_queued:
                warned_queued = True
                self._echo(
                    "Run 已在 queued 状态超过 60 秒：请确认专用 Runner 是否存活，"
                    "或另一个终端里的 `uv run workbench runner` 正在运行。"
                )
                if self._runner is not None and self._runner.process.poll() is not None:
                    self._echo(
                        f"[警告] 专用 Runner 已退出（exit={self._runner.process.returncode}），"
                        f"日志：{self._runner.log_path}"
                    )
            await self._sleep(options.poll_interval)

        elapsed = self._clock() - started_at
        cancelled = False
        if cancel_reason is not None:
            cancelled = True
            self._echo(f"触发停止条件（{cancel_reason}），通过现有取消接口收尾。")
            try:
                await self._api.cancel_run(run_id)
            except WorkbenchApiError as exc:
                self._echo(f"[警告] 取消请求未被受理：{exc}")
            deadline = self._clock() + options.cancel_grace
            while self._clock() < deadline:
                snapshot = await self._api.snapshot(session_id)
                run = self._find_run(snapshot, run_id)
                status = str(run.get("status"))
                final_answer_saved = bool(run.get("model_response_saved"))
                if status in TERMINAL_RUN_STATUSES:
                    break
                await self._sleep(options.poll_interval)
            if status not in TERMINAL_RUN_STATUSES:
                self._echo(
                    "取消已受理，但 Run 尚未进入终态；未决远程执行保持'未知'，"
                    "不会用关闭容器来假装它们已完成。"
                )

        if options.max_model_calls is not None:
            model_calls = await self._api.run_model_calls(session_id, run_id)
        if options.max_tool_jobs is not None:
            operations = await self._api.count_operations(session_id)
        # 记录口径：即使没有设置预算，也取一次实际用量，避免记录里恒为 0。
        if options.max_model_calls is None:
            try:
                model_calls = await self._api.run_model_calls(session_id, run_id)
            except WorkbenchApiError:
                model_calls = -1
        if options.max_tool_jobs is None:
            try:
                operations = await self._api.count_operations(session_id)
            except WorkbenchApiError:
                operations = -1

        return MonitorResult(
            status=status,
            cancelled=cancelled,
            reason=cancel_reason,
            elapsed_seconds=round(elapsed, 1),
            model_calls=model_calls,
            tool_operations=operations,
            completion_reason=completion_reason,
            final_answer_saved=final_answer_saved,
        )

    @staticmethod
    def _find_run(snapshot: dict[str, Any], run_id: str) -> dict[str, Any]:
        for row in snapshot.get("runs") or []:
            if str(row.get("id")) == run_id:
                return row
        raise FlowError(f"本机 API 快照里找不到 Run {run_id}")

    # ------------------------------------------------------------------ #
    # 提交与收尾
    # ------------------------------------------------------------------ #

    async def _submit(
        self,
        challenge: ChallengeInfo,
        candidates: list[FlagCandidate],
        outcome: FlowOutcome,
    ) -> list[SubmissionRecord]:
        submissions: list[SubmissionRecord] = []
        progress = challenge.correct_flag_count
        total = challenge.flag_count
        if total <= 0:
            outcome.notes.append("平台报告的 flag 总数为 0，未提交任何候选。")
            return submissions
        if progress >= total:
            outcome.notes.append("题目在本次执行前已全部完成，未重复提交。")
            return submissions

        seen: set[str] = set()
        for candidate in candidates:
            if progress >= total:
                break
            key = candidate.value.casefold()
            if key in seen:
                continue
            seen.add(key)
            try:
                result = await self._platform.submit_flag(
                    challenge.unique_code, candidate.value
                )
            except ChallengeConflict as exc:
                outcome.notes.append(f"平台拒绝继续提交：{exc}")
                break
            except PlatformError as exc:
                submissions.append(
                    SubmissionRecord(
                        candidate=candidate,
                        correct=False,
                        awarded=0,
                        duplicate=False,
                        progress=f"{progress}/{total}",
                        error=str(exc),
                    )
                )
                outcome.notes.append(f"提交 {candidate.preview} 失败：{exc}")
                if isinstance(exc, PlatformUnavailable):
                    # 传输层不可用时继续提交只会放大不确定性。
                    break
                continue
            if result.duplicate:
                submissions.append(
                    SubmissionRecord(
                        candidate=candidate,
                        correct=True,
                        awarded=0,
                        duplicate=True,
                        progress=f"{progress}/{total}（重复提交）",
                    )
                )
                continue
            if result.correct:
                progress = max(progress, result.correct_flag_count)
            submissions.append(
                SubmissionRecord(
                    candidate=candidate,
                    correct=result.correct,
                    awarded=result.awarded,
                    duplicate=False,
                    progress=f"{result.correct_flag_count}/{result.total_flag_count}",
                )
            )
            self._echo(
                f"提交 {candidate.preview}：correct={result.correct} "
                f"awarded={result.awarded} 进度 {result.correct_flag_count}/"
                f"{result.total_flag_count}"
            )
        outcome.flags_correct_after = progress
        return submissions

    async def _refresh_challenge(
        self, challenge: ChallengeInfo, outcome: FlowOutcome
    ) -> ChallengeInfo | None:
        try:
            rows = await self._platform.list_challenges()
        except PlatformError as exc:
            outcome.notes.append(f"刷新题目进度失败：{exc}")
            return None
        for row in rows:
            if row.unique_code == challenge.unique_code:
                return row
        outcome.notes.append("刷新后题目列表里找不到该题，进度以最后一次提交结果为准。")
        return None

    async def _unresolved_jobs(self, session_id: str) -> list[str]:
        try:
            rows = await self._api.jobs(session_id)
        except WorkbenchApiError as exc:
            return [f"无法读取作业台账：{exc}"]
        unresolved: list[str] = []
        for row in rows:
            state = str(row.get("state") or "")
            sync = str(row.get("sync_status") or "")
            cancel_confirmed = bool(row.get("cancel_confirmed"))
            if state == "unknown" or sync == "uncertain" or (state in {"accepted", "running"} and not cancel_confirmed):
                unresolved.append(f"{row.get('job_id')}:{state}/{sync}")
        return unresolved

    def _spawn_runner(self, run_id: str) -> SpawnedRunner:
        """只为本题拉起一个专用 Runner。

        命令仍是现有入口 `workbench runner --only-run-id ...`，**不是**第二套循环；
        额外带 `--skip-execution-cycle`，避免全局执行事件巡检碰到库里的历史任务。
        """
        log_dir = self._data_root / "tsecbench" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"runner-{run_id}.log"
        command = [
            sys.executable,
            "-m",
            "agent_workbench.cli",
            "runner",
            "--no-recover",
            "--only-run-id",
            run_id,
            "--skip-execution-cycle",
        ]
        env = child_process_env()
        # 评测令牌不属于 Agent 运行环境；子进程不继承。
        for key in (
            "BENCHMARK_TOKEN",
            "BENCHMARK_BASE_URL",
            "WORKBENCH_TSECBENCH_HOME",
            "WORKBENCH_TSECBENCH_OVPN",
        ):
            env.pop(key, None)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        with log_path.open("ab") as stream:
            process = subprocess.Popen(
                command,
                cwd=str(REPO_ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        return SpawnedRunner(process=process, log_path=log_path)

    def _stop_runner(self) -> dict[str, Any] | None:
        handle, self._runner = self._runner, None
        if handle is None:
            return None
        process = handle.process
        forced = False
        error: str | None = None
        if process.poll() is None:
            try:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    forced = True
                    process.kill()
                    process.wait(timeout=10)
            except OSError as exc:
                error = f"{type(exc).__name__}: {exc}"
        self._echo(
            f"专用 Runner 已停止（pid={process.pid}，exit={process.returncode}）。"
        )
        return {
            "pid": process.pid,
            "exit_code": process.returncode,
            "log": str(handle.log_path),
            "forced_stop": forced,
            "error": error,
        }

    async def _close_container(self, unique_code: str) -> tuple[bool, str | None]:
        try:
            closed = await self._platform.close_challenge(unique_code)
        except PlatformError as exc:
            self._echo(f"[警告] 关闭容器失败：{exc}")
            return False, str(exc)
        except Exception as exc:
            self._echo(f"[警告] 关闭容器出现未预期异常：{type(exc).__name__}: {exc}")
            return False, f"{type(exc).__name__}: {exc}"
        if closed:
            self._echo("靶场容器已关闭。")
        else:
            self._echo("[警告] 平台返回 closed=false，容器可能仍在运行。")
        return closed, None if closed else "平台返回 closed=false"

    @staticmethod
    def _classify(
        monitor: MonitorResult, challenge: ChallengeInfo, outcome: FlowOutcome
    ) -> str:
        if challenge.is_completed:
            return "completed"
        # 刷新进度失败时退回到最后一次提交结果，避免把"已全对"误报成 partial。
        if (
            outcome.flags_correct_after is not None
            and challenge.flag_count > 0
            and outcome.flags_correct_after >= challenge.flag_count
        ):
            return "completed"
        if monitor.status == "failed":
            return "failed"
        if monitor.status == "cancelled":
            return "timed_out" if (monitor.reason or "").startswith("max_") else "cancelled"
        if monitor.status == "completed":
            return "partial"
        return "blocked"

    # ------------------------------------------------------------------ #
    # 记录
    # ------------------------------------------------------------------ #

    def _build_record(
        self,
        *,
        outcome: FlowOutcome,
        challenge: ChallengeInfo | None,
        final_challenge: ChallengeInfo | None,
        targets: list[ContainerTarget],
        probes: list[ProbeResult],
        monitor: MonitorResult | None,
        final_text: str,
        final_artifact: str | None,
        close_attempted: bool,
        closed: bool | None,
        close_error: str | None,
        runner_info: dict[str, Any] | None,
    ) -> dict[str, object]:
        before = challenge
        after = final_challenge or challenge
        return {
            "kind": "tsecbench_challenge_run",
            "created_at": datetime.now(UTC).isoformat(),
            "outcome": outcome.outcome,
            "settled": outcome.settled,
            "challenge": {
                "unique_code": self._options.unique_code,
                "difficulty": before.difficulty if before else None,
                "level": before.level if before else None,
                "total_score": before.total_score if before else None,
                "flag_count": before.flag_count if before else None,
                "correct_flag_count_before": before.correct_flag_count if before else None,
                "is_completed_before": before.is_completed if before else None,
                "correct_flag_count_after": after.correct_flag_count if after else None,
                "is_completed_after": after.is_completed if after else None,
                "description_sha256": description_digest(before.description) if before else None,
                "description_chars": len(before.description) if before else None,
            },
            "authorization": {
                "mode": "scoped",
                "addresses": [target.origin for target in targets],
                "scope_rules": [
                    {"scheme": "any", "host": target.host, "port": target.port}
                    for target in targets
                ],
            },
            "kali_probes": [
                {
                    "url": item.url,
                    "status": item.status,
                    "stage": item.stage,
                    "http_status": item.http_status,
                    "detail": item.detail,
                }
                for item in probes
            ],
            "run": {
                "project_id": outcome.project_id,
                "session_id": outcome.session_id,
                "run_id": outcome.run_id,
                "status": monitor.status if monitor else None,
                "completion_reason": monitor.completion_reason if monitor else None,
                "final_answer_saved": monitor.final_answer_saved if monitor else None,
                "elapsed_seconds": monitor.elapsed_seconds if monitor else None,
                "model_calls": monitor.model_calls if monitor else None,
                "tool_operations": monitor.tool_operations if monitor else None,
                "final_answer_chars": len(final_text),
                "final_answer_artifact_id": final_artifact,
            },
            "runner": runner_info,
            "flags": {
                "candidate_count": len(outcome.candidates),
                "candidates": [
                    {
                        "preview": item.preview,
                        "sha256": item.digest,
                        "origin": item.origin,
                        "line": item.line,
                    }
                    for item in outcome.candidates
                ],
                "submissions": [
                    {
                        "preview": item.candidate.preview,
                        "sha256": item.candidate.digest,
                        "correct": item.correct,
                        "awarded": item.awarded,
                        "duplicate": item.duplicate,
                        "progress": item.progress,
                        "error": item.error,
                    }
                    for item in outcome.submissions
                ],
                "platform_progress": outcome.platform_progress,
                "values_stored": False,
            },
            "cleanup": {
                "close_attempted": close_attempted,
                "closed": closed,
                "error": close_error,
                "manual_command": (
                    f"uv run workbench tsec release {self._options.unique_code}"
                    if close_attempted and not closed
                    else None
                ),
            },
            "unresolved": {
                "jobs": outcome.unresolved_jobs,
                "note": (
                    "unknown/uncertain 表示远端是否执行未知；关闭容器不构成完成证明。"
                ),
            },
            "notes": outcome.notes,
        }


__all__ = [
    "TERMINAL_RUN_STATUSES",
    "FlowError",
    "FlowOptions",
    "FlowOutcome",
    "MonitorResult",
    "SingleChallengeFlow",
    "SpawnedRunner",
    "SubmissionRecord",
]
