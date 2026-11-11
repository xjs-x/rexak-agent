"""Run 的只读墙钟投影；不保存可推导时间、不控制任务结束。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_workbench.contracts.events import EventType
from agent_workbench.persistence.tables import EventRow, RunRow

TIME_PROJECTION = "host_run_time"
_TSEC_HEADER = re.compile(r"目标：完成 TSecBench 评测题目 `[^`\n]+`，获取全部 flag。")
_TSEC_BUDGET = re.compile(r"4\. 本次执行预算约 ([1-9][0-9]*) 分钟；到点前请确保已把已有结论写进最终回答。")


def task_budget_seconds(objective: str) -> int | None:
    """当前无结构化 Run 时间预算；只接受 TSecBench 生成模板的唯一明确预算。

    不从任意数字、历史摘要、模型回复或工具输出推测预算。
    这里读取的是任务文字的软预算，不改变启动器原有 max_seconds 语义。
    """
    lines = objective.strip().splitlines()
    if not lines or _TSEC_HEADER.fullmatch(lines[0]) is None:
        return None
    matches = [_TSEC_BUDGET.fullmatch(line) for line in lines]
    budgets = [int(match[1]) * 60 for match in matches if match is not None]
    return budgets[0] if len(budgets) == 1 else None


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("运行时间必须包含明确时区")
    return moment.astimezone(UTC)


def _duration(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes} 分 {seconds} 秒"


@dataclass(frozen=True, slots=True)
class RunTiming:
    started_at: datetime | None
    budget_seconds: int | None = None

    def message(self, now: datetime) -> AIMessage:
        now = _utc(now)
        lines = [
            "【宿主运行时间状态：Python UTC 墙钟计算，本次请求有效】",
            "本块是当前运行时间的权威值；历史消息、工作摘要中的时间估计不代表当前时间。",
            "计时包含模型调用、工具执行、后台等待、暂停及恢复间隔；不包含首次运行前的排队。",
            "时间预算仅供自主决策参考，不要求耗尽预算，不改变自然结束、暂停或取消。",
        ]
        started = _utc(self.started_at) if self.started_at is not None else None
        lines.append("开始时间：" + (started.strftime("%Y-%m-%d %H:%M:%S UTC") if started else "未知（缺少首次 running 事件）"))
        lines.append("当前时间：" + now.strftime("%Y-%m-%d %H:%M:%S UTC"))
        if started is None or now < started:
            lines.append("已运行：无法可靠计算" + ("（当前 UTC 早于运行起点，请检查系统时钟）" if started else ""))
        else:
            elapsed = int((now - started).total_seconds())
            lines.append("已运行：" + _duration(elapsed))
            if self.budget_seconds is not None:
                lines.append("任务预算：" + _duration(self.budget_seconds) + "（来源：Run 原始 TSecBench 任务预算）")
                lines.append("剩余预算：" + _duration(max(0, self.budget_seconds - elapsed)))
                if elapsed >= self.budget_seconds:
                    lines.append("预算状态：已耗尽；超出时长：" + _duration(elapsed - self.budget_seconds))
        return AIMessage(content="\n".join(lines), additional_kwargs={"host_projection": TIME_PROJECTION})


async def load_run_timing(db: AsyncSession, run: RunRow) -> RunTiming:
    """首次 running 事件已与状态同事务持久化；恢复只读原事件，不取本轮起点。

    按事件序号而非最小时间排序，避免系统校时导致误选后续恢复事件。
    """
    started = await db.scalar(
        select(EventRow.occurred_at)
        .where(
            EventRow.session_id == run.session_id,
            EventRow.run_id == run.id,
            EventRow.type == str(EventType.RUN_STATUS),
            EventRow.payload["status"].astext == "running",
        )
        .order_by(EventRow.session_seq)
        .limit(1)
    )
    return RunTiming(started, task_budget_seconds(run.objective))
