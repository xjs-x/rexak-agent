"""工具箱（Toolbox）：一次 run 内工具共享的运行依赖与资源并发控制。

并发初值（§8.3，可调，**不是**总时长/费用限制）：
- 总工具任务 4；
- 同一 origin 主动请求 2；
- 同一身份的浏览器写操作 1。

**共享副作用必须串行**：同一页面、Cookie jar、文件、账号状态。
不能因为同一条模型响应包含多个 tool_calls 就认定它们互相独立。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from agent_workbench.config.settings import Settings
from agent_workbench.domain.scope import ScopePolicy
from agent_workbench.evidence.artifacts import ArtifactStore
from agent_workbench.execution.client import ExecutorClient
from agent_workbench.observability.spans import SpanRecorder
from agent_workbench.persistence.db import Database


class ResourceBusyTimeout(TimeoutError):
    """获取资源锁超时。属于"受阻"，不是阴性结论。"""


class ResourceLocks:
    """按资源键串行。键形如 `origin:https://a.example:443`、`file:...`、`identity:...`。"""

    def __init__(self, settings: Settings) -> None:
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._global = asyncio.Semaphore(settings.tool_max_concurrent)
        self._origin_limit = settings.origin_max_concurrent
        self._browser_write_limit = settings.browser_write_max_concurrent
        self._registry_lock = asyncio.Lock()

    async def _semaphore_for(self, key: str) -> asyncio.Semaphore:
        async with self._registry_lock:
            semaphore = self._semaphores.get(key)
            if semaphore is None:
                limit = self._limit_for(key)
                semaphore = asyncio.Semaphore(limit)
                self._semaphores[key] = semaphore
            return semaphore

    def _limit_for(self, key: str) -> int:
        if key.startswith("origin:"):
            return self._origin_limit
        if key.startswith("browser-write:"):
            return self._browser_write_limit
        return 1  # 文件、身份、账号状态等共享副作用：默认串行

    @contextlib.asynccontextmanager
    async def acquire(self, keys: list[str], *, timeout: float = 60.0):
        """按稳定顺序获取多个资源锁，避免死锁。"""
        ordered = sorted(set(keys))
        async with self._global:
            acquired: list[asyncio.Semaphore] = []
            try:
                for key in ordered:
                    semaphore = await self._semaphore_for(key)
                    try:
                        await asyncio.wait_for(semaphore.acquire(), timeout=timeout)
                    except TimeoutError as exc:
                        raise ResourceBusyTimeout(
                            f"等待资源锁超时：{key}（已持有 {len(acquired)} 个锁）"
                        ) from exc
                    acquired.append(semaphore)
                yield
            finally:
                for semaphore in reversed(acquired):
                    semaphore.release()


@dataclass(slots=True)
class Toolbox:
    """一次 run 内工具共享的依赖。

    注意：这里放的是**运行依赖**，不属于图状态（§6.1）。
    不把数据库连接、HTTP client、浏览器对象写入 state。
    """

    run_id: UUID
    session_id: UUID
    owner: str
    owner_generation: int
    settings: Settings
    db: Database
    scope: ScopePolicy
    executor: ExecutorClient
    artifacts: ArtifactStore
    spans: SpanRecorder
    locks: ResourceLocks
    provider_id: str = "xiaomi"
    model_name: str = "mimo-v2.6-pro"
    #: 是否已关闭 SDK 隐藏重试。关闭后"请求尝试数 == 我们记录的物理调用数"；
    #: 未关闭时用量与费用必须标注"可能不完整"（§11.2）。
    sdk_retries_disabled: bool = True
    policy_revision: int = 1
    scope_revision: int = 1
    identity_id: UUID | None = None
    #: **本模型轮次固定下来的** control_version（由 CommandGuardMiddleware 在模型请求
    #: 开始时写入）。派发前必须与数据库当前值比较；不一致说明该响应已过期，
    #: 其工具调用不得执行（§4.3）。
    control_version: int = 0
    #: 交互等待阈值：超过则工具返回 accepted + job_id，而不是占着模型调用链（§8.3）
    interactive_threshold_seconds: float = 20.0
    #: 从执行岛同步单个工件的字节上限（资源初值，不是任务总时长限制）
    artifact_sync_limit_bytes: int = 8 * 1024 * 1024
    #: 由 Runner 注入：在当前一致边界检查控制版本与暂停/取消
    control_check: Any = None
    #: 最近一次上下文装配的 manifest（只读投影，供诊断导出与复盘）
    context_manifest: dict[str, Any] | None = None
    #: 只读版本元数据；由Runner绑定，不作为执行权限或图状态。
    loaded_runtime_versions: dict[str, Any] | None = None
    working_context: dict[str, Any] | None = None
    graph_sync_snapshot: dict[str, Any] | None = None
    graph_review_window_id: str | None = None
    #: Runner 注入，只读已提交 checkpoint 的规划头，不持有第二份可写计划。
    planning_head_reader: Any = None
    #: 本次请求的低权限历史参考来源清单，不是可写记忆。
    history_assistance_manifest: dict[str, Any] | None = None
    skill_catalog: list[dict[str, Any]] | None = None
    skill_context: str = ""
    skill_manifest: list[dict[str, Any]] = field(default_factory=list)
    #: 执行端当前会话 fence 序号（每次同步后更新；操作包带上它以拒绝自填提权）
    execution_fence_seq: int | None = None
    execution_delegation_id: str | None = None
    execution_delegation_revision: int | None = None
    _stats: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def bump(self, name: str) -> int:
        self._stats[name] += 1
        return self._stats[name]

    def reset_segment_work_units(self) -> None:
        self._stats["segment_work_units"] = 0

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    @property
    def work_units(self) -> int:
        """片段内已消耗的工作单位（工具调用次数）。用于受控片段边界判断。"""
        return int(self._stats.get("segment_work_units", 0))

    async def assert_control_current(self) -> None:
        """派发前检查控制版本。过期响应**不得**自动执行工具（§4.3）。

        这里比较的是**本模型轮次固定下来的** `control_version`，
        而不是"读取数据库当前值再和自己比"（那样永远相等，等于没检查）。
        """
        if self.control_check is None:
            return
        await self.control_check(
            self.run_id, self.owner, self.owner_generation, self.control_version
        )

    async def wait_for_terminal(self, job_id: UUID, *, poll_interval: float = 0.5) -> Any:
        """有界轮询到 Terminal 状态。

        控制面在**交互阈值内**使用它把短任务变成同步结果；阈值由调用方的
        `asyncio.wait_for` 施加。长任务应走事件唤醒而不是持续轮询（§8.3）。
        """
        from agent_workbench.execution.client import wait_for_terminal

        return await wait_for_terminal(self.executor, job_id, poll_interval=poll_interval)


def origin_lock_key(url: str) -> str:
    from agent_workbench.domain.scope import parse_target

    target = parse_target(url)
    return f"origin:{target.origin}"


def execution_identity(toolbox: Toolbox, label: str = "anonymous") -> str:
    """将模型可见身份标签绑定到当前任务，防止跨任务共用 Cookie。"""
    normalized = label.strip() or "anonymous"
    if normalized == "anonymous":
        if toolbox.identity_id is not None:
            return f"session-{toolbox.session_id}-identity-{toolbox.identity_id}"
        return f"session-{toolbox.session_id}"
    if toolbox.identity_id is not None and normalized == str(toolbox.identity_id):
        return f"session-{toolbox.session_id}-identity-{toolbox.identity_id}"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"session-{toolbox.session_id}-{digest}"
