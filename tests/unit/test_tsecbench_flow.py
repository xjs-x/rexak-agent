"""单题流程的可靠性测试：正常路径、超时、取消、清理失败与未决执行。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from agent_workbench.tsecbench.checks import ProbeResult
from agent_workbench.tsecbench.config import BenchmarkConfig
from agent_workbench.tsecbench.flow import FlowOptions, SingleChallengeFlow, SpawnedRunner
from agent_workbench.tsecbench.platform import (
    ChallengeInfo,
    PlatformError,
    ResourceBusy,
    StartInfo,
    SubmitInfo,
)


def _config() -> BenchmarkConfig:
    return BenchmarkConfig(
        token="token-value",
        base_url="https://benchmark.example.com",
        api_base_url="http://127.0.0.1:8770",
        api_token="api-token",
    )


def _challenge(**overrides: Any) -> ChallengeInfo:
    base: dict[str, Any] = {
        "unique_code": "web-001",
        "description": "内部 CMS，请获取 flag。",
        "difficulty": "medium",
        "level": "2",
        "total_score": 300,
        "flag_count": 2,
        "correct_flag_count": 0,
        "is_completed": False,
        "container_status": "available",
        "container_addr": ("10.1.2.3:8080",),
    }
    return ChallengeInfo(**{**base, **overrides})


class FakePlatform:
    def __init__(
        self,
        challenge: ChallengeInfo,
        *,
        refresh: ChallengeInfo | None = None,
        submit_script: list[Any] | None = None,
        start_error: Exception | None = None,
        open_error: Exception | None = None,
        close_result: bool = True,
    ) -> None:
        self.challenge = challenge
        self.refresh = refresh or challenge
        self.submit_script = list(submit_script or [])
        self.start_error = start_error
        self.open_error = open_error
        self.close_result = close_result
        self.opened = False
        self.closed = False
        self.start_calls = 0
        self.close_calls = 0
        self.submissions: list[str] = []
        self._list_calls = 0

    async def open(self) -> None:
        if self.open_error is not None:
            raise self.open_error
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def check_vpn(self):
        raise AssertionError("流程不应直接调用 check_vpn")

    async def list_challenges(self) -> list[ChallengeInfo]:
        self._list_calls += 1
        return [self.challenge if self._list_calls == 1 else self.refresh]

    async def start_challenge(self, unique_code: str) -> StartInfo:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        return StartInfo(unique_code, self.challenge.container_addr)

    async def submit_flag(self, unique_code: str, flag: str) -> SubmitInfo:
        self.submissions.append(flag)
        index = len(self.submissions) - 1
        if index < len(self.submit_script):
            action = self.submit_script[index]
            if isinstance(action, Exception):
                raise action
            return action
        return SubmitInfo(
            correct=True,
            awarded=0,
            cumulative_score=0,
            correct_flag_count=len(self.submissions),
            total_flag_count=self.challenge.flag_count,
            matched_flag_index=None,
        )

    async def close_challenge(self, unique_code: str) -> bool:
        self.close_calls += 1
        return self.close_result


class FakeApi:
    def __init__(
        self,
        statuses: list[str],
        *,
        final_text: str = "",
        jobs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.statuses = statuses
        self.final_text = final_text
        self.job_rows = jobs or []
        self.snapshot_calls = 0
        self.cancelled: list[str] = []
        self.projects: list[str] = []
        self.sessions: list[dict[str, Any]] = []

    async def create_project(
        self, *, name: str, target_description: str, scope: dict[str, Any]
    ) -> str:
        self.projects.append(name)
        return "project-1"

    async def create_session_and_run(
        self,
        *,
        project_id: str,
        title: str,
        objective: str,
        scope: dict[str, Any],
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        self.sessions.append(
            {"project_id": project_id, "objective": objective, "scope": scope, "policy": policy}
        )
        return {"session": {"id": "session-1"}, "run": {"id": "run-1"}}

    async def snapshot(self, session_id: str) -> dict[str, Any]:
        index = min(self.snapshot_calls, len(self.statuses) - 1)
        self.snapshot_calls += 1
        status = self.statuses[index]
        return {
            "runs": [
                {
                    "id": "run-1",
                    "status": status,
                    "completion_reason": "model_finished" if status == "completed" else None,
                    "model_response_saved": bool(self.final_text),
                }
            ]
        }

    async def jobs(self, session_id: str) -> list[dict[str, Any]]:
        return self.job_rows

    async def run_model_calls(self, session_id: str, run_id: str) -> int:
        return 3

    async def count_operations(self, session_id: str) -> int:
        return 4

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        self.cancelled.append(run_id)
        return {"status": "cancelling"}

    async def read_final_answer(self, session_id: str, run_id: str) -> tuple[str, str | None]:
        return (self.final_text, "artifact-1" if self.final_text else None)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 4321
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        return 0 if self.returncode is None else self.returncode

    def kill(self) -> None:
        self.returncode = -9


class FakeRunnerFactory:
    def __init__(self, *, error: OSError | None = None) -> None:
        self.run_ids: list[str] = []
        self.process = FakeProcess()
        self.error = error

    def __call__(self, run_id: str) -> SpawnedRunner:
        self.run_ids.append(run_id)
        if self.error is not None:
            raise self.error
        return SpawnedRunner(process=cast(Any, self.process), log_path=Path("runner.log"))


async def _reachable(urls: list[str], scope: dict[str, Any]) -> list[ProbeResult]:
    return [ProbeResult(url=url, status="reachable", stage=None, detail="", http_status=200) for url in urls]


async def _unreachable(urls: list[str], scope: dict[str, Any]) -> list[ProbeResult]:
    return [
        ProbeResult(url=url, status="unreachable", stage="tls_or_http", detail="refused")
        for url in urls
    ]


def _flow(
    platform: FakePlatform,
    api: FakeApi,
    tmp_path: Path,
    *,
    options: FlowOptions | None = None,
    probe=_reachable,
    runner_factory: FakeRunnerFactory | None = None,
) -> SingleChallengeFlow:
    clock = FakeClock()
    return SingleChallengeFlow(
        config=_config(),
        platform=platform,
        api=api,
        options=options
        or FlowOptions(
            unique_code="web-001",
            max_seconds=60,
            poll_interval=1,
            cancel_grace=10,
            spawn_runner=False,
        ),
        data_root=tmp_path,
        probe=probe,
        sleep=clock.sleep,
        clock=clock,
        echo=lambda _message: None,
        runner_factory=runner_factory,
    )


def _record(outcome) -> dict[str, Any]:
    assert outcome.record_path is not None
    return json.loads(Path(outcome.record_path).read_text(encoding="utf-8"))


async def test_flow_completes_with_multiple_flags_and_masks_record(tmp_path):
    challenge = _challenge()
    refresh = _challenge(correct_flag_count=2, is_completed=True)
    platform = FakePlatform(
        challenge,
        refresh=refresh,
        submit_script=[
            SubmitInfo(True, 100, 100, 1, 2, 0),
            SubmitInfo(True, 200, 300, 2, 2, 1),
        ],
    )
    api = FakeApi(["completed"], final_text="找到：\nflag{alpha_secret}\nflag{beta_secret}\n")

    outcome = await _flow(platform, api, tmp_path).run()

    assert outcome.outcome == "completed"
    assert outcome.settled is True
    assert outcome.run_status == "completed"
    assert len(outcome.submissions) == 2
    assert platform.submissions == ["flag{alpha_secret}", "flag{beta_secret}"]
    assert platform.close_calls == 1
    assert platform.closed is True

    record = _record(outcome)
    assert record["outcome"] == "completed"
    assert record["cleanup"]["closed"] is True
    assert record["flags"]["platform_progress"] == "2/2"
    assert record["flags"]["values_stored"] is False
    assert "alpha_secret" not in json.dumps(record, ensure_ascii=False)
    assert record["run"]["run_id"] == "run-1"
    assert record["authorization"]["addresses"] == ["10.1.2.3:8080"]


async def test_flow_skips_completed_challenge(tmp_path):
    platform = FakePlatform(_challenge(is_completed=True, correct_flag_count=2))
    api = FakeApi(["completed"])

    outcome = await _flow(platform, api, tmp_path).run()

    assert outcome.outcome == "skipped"
    assert platform.start_calls == 0
    assert platform.close_calls == 0
    assert outcome.record_path is None


async def test_flow_dry_run_does_not_start_container(tmp_path):
    platform = FakePlatform(_challenge(container_addr=()))
    api = FakeApi(["queued"])

    outcome = await _flow(
        platform,
        api,
        tmp_path,
        options=FlowOptions(unique_code="web-001", dry_run=True, spawn_runner=False),
    ).run()

    assert outcome.outcome == "dry_run"
    assert platform.start_calls == 0
    assert platform.close_calls == 0
    assert api.projects == []


async def test_flow_aborts_before_creating_run_when_kali_cannot_reach_target(tmp_path):
    platform = FakePlatform(_challenge())
    api = FakeApi(["completed"])

    outcome = await _flow(platform, api, tmp_path, probe=_unreachable).run()

    assert outcome.outcome == "failed"
    assert platform.start_calls == 1
    assert platform.close_calls == 1
    assert api.projects == []
    assert any("Kali" in note for note in outcome.notes)


async def test_flow_timeout_cancels_and_keeps_unresolved_jobs(tmp_path):
    platform = FakePlatform(_challenge())
    api = FakeApi(
        ["running", "running", "running", "cancelled"],
        jobs=[{"job_id": "job-1", "state": "unknown", "sync_status": "uncertain"}],
    )

    outcome = await _flow(
        platform,
        api,
        tmp_path,
        options=FlowOptions(
            unique_code="web-001",
            max_seconds=2,
            poll_interval=1,
            cancel_grace=10,
            spawn_runner=False,
        ),
    ).run()

    assert outcome.outcome == "timed_out"
    assert outcome.settled is False
    assert api.cancelled == ["run-1"]
    assert platform.close_calls == 1
    assert outcome.unresolved_jobs == ["job-1:unknown/uncertain"]
    record = _record(outcome)
    assert record["unresolved"]["jobs"] == ["job-1:unknown/uncertain"]


async def test_flow_waiting_user_is_cancelled(tmp_path):
    platform = FakePlatform(_challenge())
    api = FakeApi(["waiting_user", "cancelled"])

    outcome = await _flow(platform, api, tmp_path).run()

    assert outcome.outcome == "cancelled"
    assert api.cancelled == ["run-1"]
    assert platform.close_calls == 1


async def test_flow_records_close_failure_and_manual_hint(tmp_path):
    platform = FakePlatform(_challenge(), close_result=False)
    api = FakeApi(["completed"], final_text="flag{alpha_secret}\n")

    outcome = await _flow(platform, api, tmp_path).run()

    record = _record(outcome)
    assert record["cleanup"]["close_attempted"] is True
    assert record["cleanup"]["closed"] is False
    assert "tsec release web-001" in str(record["cleanup"]["manual_command"])
    assert any("release web-001" in note for note in outcome.notes)


async def test_flow_start_failure_records_without_close(tmp_path):
    platform = FakePlatform(_challenge(), start_error=ResourceBusy("busy"))
    api = FakeApi(["completed"])

    outcome = await _flow(platform, api, tmp_path).run()

    assert outcome.outcome == "failed"
    assert platform.close_calls == 0
    record = _record(outcome)
    assert record["cleanup"]["close_attempted"] is False
    assert record["run"]["run_id"] is None


async def test_flow_ignores_placeholder_flags(tmp_path):
    platform = FakePlatform(_challenge())
    api = FakeApi(["completed"], final_text="格式为 flag{...}，未获取到真实 flag。")

    outcome = await _flow(platform, api, tmp_path).run()

    assert outcome.candidates == []
    assert platform.submissions == []
    assert outcome.outcome == "partial"


async def test_flow_duplicate_submit_is_not_repeated(tmp_path):
    platform = FakePlatform(
        _challenge(flag_count=1),
        refresh=_challenge(flag_count=1, correct_flag_count=1, is_completed=True),
        submit_script=[SubmitInfo(True, 0, 100, 1, 1, None, duplicate=True)],
    )
    api = FakeApi(["completed"], final_text="flag{already_done}\n")

    outcome = await _flow(platform, api, tmp_path).run()

    assert len(outcome.submissions) == 1
    assert outcome.submissions[0].duplicate is True
    assert outcome.outcome == "completed"


async def test_flow_completed_run_with_unsettled_jobs_is_not_reported_as_settled(tmp_path):
    platform = FakePlatform(
        _challenge(flag_count=1),
        refresh=_challenge(flag_count=1, correct_flag_count=1, is_completed=True),
        submit_script=[SubmitInfo(True, 100, 100, 1, 1, 0)],
    )
    api = FakeApi(
        ["completed"],
        final_text="flag{the_answer}\n",
        jobs=[{"job_id": "job-9", "state": "running", "sync_status": "ok"}],
    )

    outcome = await _flow(platform, api, tmp_path).run()

    assert outcome.outcome == "completed"
    assert outcome.settled is False
    assert outcome.unresolved_jobs == ["job-9:running/ok"]


async def test_flow_platform_open_failure_still_closes(tmp_path):
    platform = FakePlatform(_challenge(), open_error=PlatformError("vpn down"))
    api = FakeApi(["completed"])

    outcome = await _flow(platform, api, tmp_path).run()

    assert outcome.outcome == "failed"
    assert platform.closed is True
    assert outcome.record_path is None


async def test_flow_spawns_and_stops_dedicated_runner(tmp_path):
    platform = FakePlatform(
        _challenge(flag_count=1),
        refresh=_challenge(flag_count=1, correct_flag_count=1, is_completed=True),
        submit_script=[SubmitInfo(True, 100, 100, 1, 1, 0)],
    )
    api = FakeApi(["completed"], final_text="flag{the_answer}\n")
    factory = FakeRunnerFactory()

    outcome = await _flow(
        platform,
        api,
        tmp_path,
        options=FlowOptions(
            unique_code="web-001",
            max_seconds=60,
            poll_interval=1,
            cancel_grace=10,
            spawn_runner=True,
        ),
        runner_factory=factory,
    ).run()

    assert outcome.outcome == "completed"
    assert factory.run_ids == ["run-1"]
    assert factory.process.terminated is True
    record = _record(outcome)
    assert record["runner"]["pid"] == 4321
    assert record["runner"]["forced_stop"] is False


async def test_flow_can_disable_runner_spawn(tmp_path):
    platform = FakePlatform(
        _challenge(flag_count=1),
        refresh=_challenge(flag_count=1, correct_flag_count=1, is_completed=True),
        submit_script=[SubmitInfo(True, 100, 100, 1, 1, 0)],
    )
    api = FakeApi(["completed"], final_text="flag{the_answer}\n")
    factory = FakeRunnerFactory()

    outcome = await _flow(
        platform,
        api,
        tmp_path,
        options=FlowOptions(
            unique_code="web-001",
            max_seconds=60,
            poll_interval=1,
            cancel_grace=10,
            spawn_runner=False,
        ),
        runner_factory=factory,
    ).run()

    assert outcome.outcome == "completed"
    assert factory.run_ids == []
    assert _record(outcome)["runner"] is None


async def test_flow_spawn_failure_closes_container(tmp_path):
    platform = FakePlatform(_challenge())
    api = FakeApi(["completed"])
    factory = FakeRunnerFactory(error=OSError("cannot spawn"))

    outcome = await _flow(
        platform,
        api,
        tmp_path,
        options=FlowOptions(
            unique_code="web-001",
            max_seconds=60,
            poll_interval=1,
            cancel_grace=10,
            spawn_runner=True,
        ),
        runner_factory=factory,
    ).run()

    assert outcome.outcome == "failed"
    assert any("专用 Runner" in note for note in outcome.notes)
    assert platform.close_calls == 1
    assert _record(outcome)["cleanup"]["close_attempted"] is True
