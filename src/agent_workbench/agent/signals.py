"""图执行中的控制信号。

Runner 捕获这些异常并映射为 run 状态，**不把它们当成任务失败**。
"""

from __future__ import annotations


class GraphRunAborted(Exception):
    """暂停 / 取消请求在图的一致边界生效。

    抛出点必须在**没有非幂等副作用悬空**的位置（§17"interrupt 前代码会重跑"）。
    这些位置是：模型调用之前、工具派发之前、工具结果落库之后。
    """

    def __init__(self, reason: str, *, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class LeaseLostError(Exception):
    """运行所有权已转移（另一个 Runner 接管）。

    旧 owner 的迟到模型响应**可以记录用量，但不得派发**（§4.3）。
    """

    def __init__(self, run_id: str, owner: str) -> None:
        super().__init__(f"run {run_id} 的运行所有权已不在 {owner!r} 手中")
        self.run_id = run_id
        self.owner = owner


class SegmentBoundaryReached(Exception):
    """达到受控执行片段的**工作阈值**。

    在安全边界保存 checkpoint 并调度下一片段；**不等于**任务完成，
    也不把上限耗尽当成完成（§6.4）。
    """

    def __init__(self, run_id: str, work_units: int) -> None:
        super().__init__(f"run {run_id} 达到片段工作阈值（{work_units} 个单位）")
        self.run_id = run_id
        self.work_units = work_units


class StaleResponseError(Exception):
    """该模型响应已过期：用户在本轮模型调用期间改变了方向。

    §4.3 要求："旧模型响应可以记录用量，但派发前必须重新检查版本；
    不得把过期响应中的工具自动执行。"

    抛出它的位置必须在**工具真正产生副作用之前**（HTTP 工具在提交到执行岛之前，
    派发中间件在调用 handler 之前）。
    """

    def __init__(self, *, pinned_version: int, current_version: int) -> None:
        super().__init__(
            f"模型响应已过期：该轮固定 control_version={pinned_version}，当前为 {current_version}"
        )
        self.pinned_version = pinned_version
        self.current_version = current_version


class FinishGateRejected(Exception):
    """完成提议未通过确定性检查。把反馈交回模型，而不是直接结束。"""

    def __init__(self, feedback: list[str]) -> None:
        super().__init__("完成提议未通过 finish_gate 检查")
        self.feedback = feedback


class NoProgressBlocked(Exception):
    """连续重复观察后在模型调用前暂停，保留同一任务供用户继续。"""

    def __init__(self, streak: int) -> None:
        super().__init__(f"连续 {streak} 轮没有新增证据、入口、线索状态或独立验证")
        self.streak = streak
