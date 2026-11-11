"""固定 UTC 时钟：只计算事实，不控制运行。"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from agent_workbench.domain.run_timing import RunTiming, task_budget_seconds
from agent_workbench.tsecbench.objective import build_objective, parse_addresses
from agent_workbench.tsecbench.platform import ChallengeInfo

START = datetime(2026, 11, 10, 5, 39, 18, tzinfo=UTC)
OBJECTIVE = (
    "目标：完成 TSecBench 评测题目 `b-02`，获取全部 flag。\n\n执行约束：\n"
    "4. 本次执行预算约 70 分钟；到点前请确保已把已有结论写进最终回答。\n"
)


@pytest.mark.parametrize(("elapsed", "remaining"), [(2265, "32 分 15 秒"), (1510, "44 分 50 秒")])
def test_confirmed_early_completion_cases(elapsed, remaining):
    message = RunTiming(START, 4200).message(START + timedelta(seconds=elapsed))
    assert "剩余预算：" + remaining in str(message.content)
    assert "预算状态：已耗尽" not in str(message.content)


def test_no_budget_has_no_invented_remaining_or_deadline():
    text = str(RunTiming(START).message(START + timedelta(seconds=2265)).content)
    assert "已运行：37 分 45 秒" in text
    assert "开始时间：2026-11-10 05:39:18 UTC" in text
    assert "当前时间：2026-11-10 06:17:03 UTC" in text
    assert "任务预算：" not in text and "剩余预算：" not in text


@pytest.mark.parametrize(("elapsed", "overrun"), [(4200, "0 分 0 秒"), (4261, "1 分 1 秒")])
def test_exhausted_budget_clamps_remaining_and_reports_overrun(elapsed, overrun):
    text = str(RunTiming(START, 4200).message(START + timedelta(seconds=elapsed)).content)
    assert "剩余预算：0 分 0 秒" in text
    assert "预算状态：已耗尽；超出时长：" + overrun in text


def test_explicit_timezone_and_cross_day():
    started = datetime(2026, 11, 10, 23, 50, tzinfo=timezone(timedelta(hours=8)))
    now = datetime(2026, 11, 11, 0, 27, 45, tzinfo=timezone(timedelta(hours=8)))
    text = str(RunTiming(started, 4200).message(now).content)
    assert "已运行：37 分 45 秒" in text and "剩余预算：32 分 15 秒" in text
    text = str(RunTiming(datetime(2026, 11, 10, 23, 50, tzinfo=UTC), 4200).message(
        datetime(2026, 11, 11, 0, 27, 45, tzinfo=UTC)).content)
    assert "当前时间：2026-11-11 00:27:45 UTC" in text
    assert "剩余预算：32 分 15 秒" in text


def test_unknown_origin_clock_rollback_and_naive_times_are_explicit():
    assert "无法可靠计算" in str(RunTiming(None, 4200).message(START).content)
    text = str(RunTiming(START, 4200).message(START - timedelta(seconds=1)).content)
    assert "当前 UTC 早于运行起点" in text and "剩余预算：" not in text
    with pytest.raises(ValueError, match="明确时区"):
        RunTiming(START.replace(tzinfo=None)).message(START)
    with pytest.raises(ValueError, match="明确时区"):
        RunTiming(START).message(START.replace(tzinfo=None))


def test_budget_only_from_unique_explicit_tsecbench_template():
    assert task_budget_seconds(OBJECTIVE) == 4200
    challenge = ChallengeInfo(
        unique_code="b-02", description="本地模板验证", difficulty="fixture", level="1",
        total_score=1, flag_count=1, correct_flag_count=0, is_completed=False,
        container_status="available", container_addr=("192.0.2.1:80",),
    )
    generated = build_objective(challenge, parse_addresses(challenge.container_addr), max_seconds=4200)
    assert task_budget_seconds(generated) == 4200
    assert task_budget_seconds("请执行 70 分钟，端口 4200") is None
    assert task_budget_seconds(OBJECTIVE.replace("70", "0")) is None
    assert task_budget_seconds(OBJECTIVE.replace("70", "很多")) is None
    assert task_budget_seconds(OBJECTIVE + OBJECTIVE.splitlines()[-1]) is None

