"""身份模型：project / session / run / command。

生命周期与规则见 BUILD_PLAN §4.1：
- project：一个被测网站/业务项目，隔离经验、账号、历史任务；
- session：一次可持续对话的测试任务，映射**一个稳定 graph thread_id**；
- run：一次用户启动的自主工作阶段；阶段结束后继续输入创建**新 run**，沿用 session/thread；
- command：一条用户指令，幂等提交、排队、消费记录。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_workbench.contracts.enums import (
    CommandKind,
    CommandStatus,
    CompletionReason,
    RunStatus,
)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _utc(value: datetime) -> datetime:
    """时间一律存 UTC；UI 按 Asia/Shanghai 显示。"""
    if value.tzinfo is None:
        raise ValueError("时间字段必须带时区（UTC）")
    return value


class ScopeRule(_Base):
    """范围规则。

    **不得**像旧实现那样剥掉端口后放行所有服务（BUILD_PLAN §10）。
    范围按 协议 + 主机 + 端口 建模，并显式声明子域规则。
    """

    scheme: Literal["http", "https", "tcp", "any"] = "any"
    host: str
    port: int | None = None
    include_subdomains: bool = False
    #: 允许被测私网与明确本地映射；不能用"一律拒绝私网"破坏本地测试需求
    allow_private: bool = True
    notes: str | None = None

    @field_validator("host")
    @classmethod
    def _lower(cls, value: str) -> str:
        return value.strip().lower().rstrip(".")


class ScopeSnapshot(_Base):
    """任务开始时固化的范围。运行中变更产生新 revision 并记录理由。"""

    revision: int = 1
    rules: list[ScopeRule] = Field(default_factory=list)
    #: 控制端 localhost 到执行岛实际地址的显式映射（§9.3）
    target_mappings: list[dict[str, str]] = Field(default_factory=list)
    denied_metadata_hosts: list[str] = Field(
        default_factory=lambda: [
            "169.254.169.254",
            "metadata.google.internal",
            "100.100.100.200",
        ]
    )


class PolicySnapshot(_Base):
    """操作策略快照。范围内的常规探索自动执行，不逐操作弹窗。"""

    revision: int = 1
    allow_generic_commands: bool = True
    command_network_isolation: Literal["enforced", "unavailable"] = "unavailable"
    allow_browser: bool = True
    max_concurrent_tools: int = 4
    max_concurrent_per_origin: int = 2
    #: 任务总时长与费用硬上限默认为空（§16.1）
    max_total_seconds: int | None = None
    max_total_cost: str | None = None


class ProjectCreate(_Base):
    name: str
    target_description: str
    default_scope: ScopeSnapshot = Field(default_factory=ScopeSnapshot)
    settings_version: str = "1"


class Project(ProjectCreate):
    id: UUID
    owner_id: str
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _utc(value)


class IdentityRef(_Base):
    """测试身份引用。**无明文密码列**；凭据通过 `secret_ref` 解析（§5.2 identities）。"""

    display_name: str
    role_label: str = "primary"
    secret_ref: str | None = None
    auth_state_ref: str | None = None
    auth_revision: int = 0


class SessionCreate(_Base):
    project_id: UUID
    title: str
    initial_objective: str
    scope: ScopeSnapshot = Field(default_factory=ScopeSnapshot)
    policy: PolicySnapshot = Field(default_factory=PolicySnapshot)
    identities: list[IdentityRef] = Field(default_factory=list)
    provider_id: str | None = None
    model_name: str | None = None


class Session(_Base):
    id: UUID
    project_id: UUID
    #: 稳定映射：同一 session always 使用同一 thread_id
    thread_id: str
    title: str
    scope_snapshot: ScopeSnapshot
    policy_snapshot: PolicySnapshot
    status: str = "active"
    created_at: datetime
    updated_at: datetime
    revision: int = 1

    @field_validator("created_at", "updated_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _utc(value)


class RunCreate(_Base):
    session_id: UUID
    objective: str
    trigger_command_id: UUID | None = None


class Run(_Base):
    id: UUID
    session_id: UUID
    #: 阶段内固定模型配置快照；修改默认模型只影响新 run（§11.1）
    provider_id: str
    model_name: str
    status: RunStatus
    completion_reason: CompletionReason | None = None
    control_version: int = 0
    #: 运行所有权：owner 为空表示无 owner；generation 用于拒绝过期 owner 派发
    owner: str | None = None
    owner_generation: int = 0
    lease_until: datetime | None = None
    code_version: str = "dev"
    prompt_version: str = "1"
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    stop_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _utc(value)


class UserCommandCreate(_Base):
    content: str
    kind: CommandKind = CommandKind.MESSAGE
    #: 幂等提交键。同键同内容返回原结果；同键不同内容返回冲突。
    idempotency_key: str
    payload: dict[str, Any] = Field(default_factory=dict)


class UserCommand(_Base):
    id: UUID
    session_id: UUID
    run_id: UUID | None
    kind: CommandKind
    content: str
    payload: dict[str, Any]
    idempotency_key: str
    #: 内容摘要，用于"同键不同内容"冲突判定
    request_digest: str
    status: CommandStatus
    priority: int = 0
    control_version: int = 0
    consumed_checkpoint_id: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def _tz(cls, value: datetime) -> datetime:
        return _utc(value)
