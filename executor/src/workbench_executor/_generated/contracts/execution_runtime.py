# 本文件由 scripts/generate_executor_contracts.py 自动生成，禁止手改。
# 源文件：contracts/execution_runtime.py
# 源 SHA256：d45de45ccfc0d6f8a6c74e374e8d1c1e390ca4c7d23696132d4756d4457ba9aa
# 重新生成：uv run python scripts/generate_executor_contracts.py
"""命令执行底座协议真源（`/execution/v1` 子协议）。

本模块是**纯协议模块**：
- 只依赖标准库与 pydantic，不导入设置、模型 SDK、数据库或任何包内其他模块；
- 由 `scripts/generate_executor_contracts.py` 原样复制到执行端
  `workbench_executor/_generated/contracts/`，执行端独立安装、不 import 控制端源码；
- **禁止**在派生副本中手改；漂移由 `--check` 检测。

编码约定（规格 §5.1）：
- 命令正文、stdin、终端 text/paste 等字段**禁止** `str_strip_whitespace`，
  首尾空格、换行与控制字符必须原样保留；路径/名称单独校验。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: 该子协议的整体版本号；`/capabilities.execution_runtime_versions` 暴露支持列表。
EXECUTION_SUBPROTOCOL_VERSION = 1
EXECUTION_PREFIX = "/execution/v1"

#: 协议数据模型 schema_version，出现在操作包与回执里，便于将来演进。
EXECUTION_SCHEMA_VERSION = 1


def execution_request_digest(action: str, arguments: dict[str, Any]) -> str:
    """线协议摘要与控制端业务操作摘要分开；两端对同一原始载荷独立核验。"""
    return hashlib.sha256(json.dumps(
        {"action": action, "arguments": arguments}, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")).hexdigest()


class _Base(BaseModel):
    """协议基类。

    **刻意不设置 `str_strip_whitespace`**：命令、stdin、终端输入必须逐字节保真。
    """

    model_config = ConfigDict(extra="forbid")


class _InputBase(BaseModel):
    """自由文本载荷基类（命令正文、stdin、终端输入等）。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #


class ExecutionMode(StrEnum):
    """执行模式。`disabled` 为发行缺省；`trusted` 必须由个人部署显式配置。"""

    DISABLED = "disabled"
    TRUSTED = "trusted"


class ResourceKind(StrEnum):
    COMMAND = "command"
    SCRIPT = "script"
    MONITOR = "monitor"
    TERMINAL = "terminal"


class ResourceStatus(StrEnum):
    """资源状态。**不要**把交互失效等同于进程已退出。"""

    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class InteractionState(StrEnum):
    AVAILABLE = "available"
    LOST = "lost"
    NOT_APPLICABLE = "not_applicable"


class TerminationReason(StrEnum):
    """退出原因。`unknown` 表示无法核实，不得伪造。"""

    EXITED_NORMALLY = "exited_normally"
    EXECUTION_TIMEOUT = "execution_timeout"
    USER_CANCELLED = "user_cancelled"
    OUTPUT_LIMIT_TERMINATE = "output_limit_terminate"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class OutputLimitAction(StrEnum):
    """采集日志超限动作。缺省保活；只有显式设置才终止。"""

    PRESERVE_PROCESS = "preserve_process"
    TERMINATE = "terminate"


class WaitReason(StrEnum):
    """一次等待结束的原因，至少覆盖以下取值。"""

    OUTPUT = "output"
    PROMPT = "prompt"
    QUIET = "quiet"
    EXITED = "exited"
    WAIT_TIMEOUT = "wait_timeout"
    INTERRUPTED = "interrupted"
    WATCH_MATCH = "watch_match"


class TerminalMode(StrEnum):
    INTERACTIVE = "interactive"
    HANDS_FREE = "hands_free"
    DISPATCH = "dispatch"
    MONITOR = "monitor"


class WatchMode(StrEnum):
    LITERAL = "literal"
    REGEX = "regex"


class WatchNotify(StrEnum):
    ONCE = "once"
    REPEAT = "repeat"


class ExecutionEventType(StrEnum):
    """持久执行事件类型。实际输出是数据，不是指令。"""

    RESOURCE_RECEIVED = "resource.received"
    RESOURCE_STARTED = "resource.started"
    RESOURCE_EXITED = "resource.exited"
    RESOURCE_CLOSED = "resource.closed"
    RESOURCE_FAILED = "resource.failed"
    INTERACTION_LOST = "resource.interaction_lost"
    WATCH_MATCH = "resource.watch_match"
    CANCEL_REQUESTED = "resource.cancel_requested"
    CANCEL_CONFIRMED = "resource.cancel_confirmed"
    OUTPUT_LIMIT_REACHED = "output.limit_reached"
    OUTPUT_DROPPED = "output.dropped"
    RESOURCE_WARNING = "resource.warning"
    RECOVERY_CHECKED = "resource.recovery_checked"
    DISPATCH_NOTIFIED = "terminal.dispatch_notified"


class OutputAvailability(StrEnum):
    AVAILABLE = "available"
    TRUNCATED = "truncated"
    UNAVAILABLE = "unavailable"


class StreamName(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"
    #: PTY 为合流；读取时用 `stdout` 语义指向合流日志，此值用于列表展示。
    COMBINED = "combined"


class ScriptLanguage(StrEnum):
    PYTHON = "python"
    SHELL = "shell"
    NODE = "node"


#: 输入构造顺序固定：text -> submit 回车 -> keys -> hex -> paste（规格 §6.3）。
INPUT_ORDER = ("text", "submit", "keys", "hex", "paste")

#: 支持的最小按键集合（未知按键在写入前拒绝）。
SUPPORTED_KEYS = (
    "Enter",
    "Tab",
    "Esc",
    "Backspace",
    "Delete",
    "Up",
    "Down",
    "Left",
    "Right",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "Ctrl-C",
    "Ctrl-D",
)

#: 按键名 -> 字节序列。
KEY_BYTES: dict[str, bytes] = {
    "Enter": b"\r",
    "Tab": b"\t",
    "Esc": b"\x1b",
    "Backspace": b"\x7f",
    "Delete": b"\x1b[3~",
    "Up": b"\x1b[A",
    "Down": b"\x1b[B",
    "Right": b"\x1b[C",
    "Left": b"\x1b[D",
    "Home": b"\x1b[H",
    "End": b"\x1b[F",
    "PageUp": b"\x1b[5~",
    "PageDown": b"\x1b[6~",
    "Ctrl-C": b"\x03",
    "Ctrl-D": b"\x04",
}

#: bracketed paste 字节协议。目标程序可能不支持；不得宣称任何多行粘贴都绝不执行。
BRACKETED_PASTE_START = b"\x1b[200~"
BRACKETED_PASTE_END = b"\x1b[201~"

#: 终端尺寸边界（每边 1–500，并限制总网格大小）。
TERMINAL_MIN_SIDE = 1
TERMINAL_MAX_SIDE = 500
#: 总网格上限（cols * rows），防止一格 500x500 以上耗尽内存。
TERMINAL_MAX_GRID = 500 * 200

#: 单资源采集日志初值（控制端与执行端两侧能力报告一致）。
DEFAULT_RESOURCE_LOG_LIMIT_BYTES = 64 * 1024 * 1024
#: session 采集日志预算初值。
DEFAULT_SESSION_LOG_BUDGET_BYTES = 512 * 1024 * 1024
#: 输出页默认与上限。
DEFAULT_OUTPUT_PAGE_BYTES = 16 * 1024
MAX_OUTPUT_PAGE_BYTES = 256 * 1024
#: 监视规则限制。
MAX_WATCH_RULES = 16
MAX_WATCH_PATTERN_CHARS = 512
MAX_HEX_ITEMS = 64
MAX_HEX_BYTES = 4096


class CapabilityState(StrEnum):
    """能力可用性三态。缺能力时给出确定原因，不默认远端支持。"""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# 操作包：身份、版本与控制
# --------------------------------------------------------------------------- #


class DelegationRevocation(_Base):
    """一次撤销的委托版本。旧 revision 的操作不得再获得执行权。"""

    delegation_id: str
    revision: int


class ExecutionControlSync(_Base):
    """控制端向执行端同步的受信会话 fence。

    执行端持久保存并只允许匹配当前 fence 的请求；旧 run、旧 owner、旧方向
    及已撤销委托的操作不得靠携带更大的数字自动获得执行权。
    """

    schema_version: int = EXECUTION_SCHEMA_VERSION
    session_id: str
    run_id: str
    owner: str = ""
    owner_generation: int = 0
    control_version: int = 0
    #: 控制端最近一次收到的 fence 序号，用于乐观并发；首次同步为 None。
    expected_seq: int | None = None
    revocations: list[DelegationRevocation] = Field(default_factory=list)
    note: str = ""


class FenceView(_Base):
    """执行端当前 fence 的只读视图。"""

    session_id: str
    seq: int
    run_id: str = ""
    owner: str = ""
    owner_generation: int = 0
    control_version: int = 0
    revocations: list[DelegationRevocation] = Field(default_factory=list)
    updated_at: str = ""


class ExecutionControlAck(_Base):
    schema_version: int = EXECUTION_SCHEMA_VERSION
    session_id: str
    applied: bool
    fence: FenceView
    #: 未应用时的原因（例如 expected_seq 落后于当前 seq）；为空表示已应用。
    reason: str = ""


class ExecutionOperationEnvelope(_Base):
    """公共操作包。身份、版本、工作区与摘要由控制端可信代码补齐。

    执行端独立校验 `request_digest`（基于规范化后且影响执行的参数），
    重试不创建新资源。
    """

    schema_version: int = EXECUTION_SCHEMA_VERSION
    operation_id: str
    session_id: str
    run_id: str
    tool_call_id: str
    request_digest: str
    #: run_command / run_script / start_monitor / terminal_open / terminal_send /
    #: terminal_resize / terminal_close / cancel_resource
    action: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    owner_generation: int = 0
    dispatch_control_version: int = 0
    #: 控制端最近一次收到的 fence 序号；执行端要求它不超过当前 fence。
    session_control_seq: int = 0
    delegation_id: str | None = None
    delegation_revision: int | None = None
    #: 控制面等待提示：受理后在执行端内等待至多该毫秒数，到期只返回原 job。
    wait_ms: int = Field(default=0, ge=0, le=60_000)
    created_at: str = ""

    @field_validator("operation_id", "session_id", "run_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("身份字段不得为空")
        return value


# --------------------------------------------------------------------------- #
# 操作参数（模型可见入口对应的执行端参数）
# --------------------------------------------------------------------------- #


class RawOutputSnapshot(_Base):
    """一次有界读取的原始字节快照；hash 仅对应本次区间，不能代表变化中的整文件。"""

    resource_id: str
    stream: str
    stored_offset: int
    next_offset: int
    observed_size: int
    sha256: str
    data_base64: str
    captured_at: str
    at_current_end: bool
    note: str = ""


class WatchRule(_Base):
    """监视规则。`literal` 或 `regex`；限定长度、数量与检测窗口。"""

    kind: WatchMode = WatchMode.LITERAL
    pattern: str
    case_sensitive: bool = True
    notify: WatchNotify = WatchNotify.ONCE
    #: 重复通知的最短冷却（毫秒）；`once` 模式忽略。
    cooldown_ms: int = Field(default=5000, ge=0, le=600_000)

    @field_validator("pattern")
    @classmethod
    def _pattern_size(cls, value: str) -> str:
        if not value:
            raise ValueError("watch 规则模式不得为空")
        if len(value) > MAX_WATCH_PATTERN_CHARS:
            raise ValueError(f"watch 规则模式超过 {MAX_WATCH_PATTERN_CHARS} 字符上限")
        return value


class _CommonExecutionArguments(_InputBase):
    skill_ref: dict[str, str] | None = None
    completion_role: Literal["work", "resource"] = "work"
    template_hash: str | None = None
    template_revision: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    cwd: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    #: 采集日志上限动作；缺省保活。不可由任务类型自动选择。
    output_limit_action: OutputLimitAction = OutputLimitAction.PRESERVE_PROCESS
    #: 执行截止（秒）。None 表示无执行截止（仍有启动/取消等待边界）。
    execution_timeout_seconds: int | None = Field(default=None, ge=1, le=7 * 24 * 3600)
    description: str = ""
    workspace_id: str = ""


class CommandArguments(_CommonExecutionArguments):
    command: str | None = None
    argv: list[str] | None = None
    stdin_text: str | None = None

    def validate_one_form(self) -> None:
        if bool(self.command) == bool(self.argv):
            raise ValueError("command 与 argv 必须二选一")


class ScriptArguments(_CommonExecutionArguments):
    language: ScriptLanguage
    code: str | None = None
    path: str | None = None
    params_json: str | None = None
    stdin_text: str | None = None

    @field_validator("params_json")
    @classmethod
    def _json_parameters(cls, value: str | None) -> str | None:
        if value is not None:
            json.loads(value)
        return value

    def validate_one_form(self) -> None:
        if bool(self.code is not None) == bool(self.path):
            raise ValueError("code 与 path 必须二选一")


class MonitorArguments(_CommonExecutionArguments):
    command: str | None = None
    argv: list[str] | None = None
    watch: list[WatchRule] = Field(default_factory=list)

    def validate_one_form(self) -> None:
        if bool(self.command) == bool(self.argv):
            raise ValueError("command 与 argv 必须二选一")
        if len(self.watch) > MAX_WATCH_RULES:
            raise ValueError(f"watch 规则数量超过 {MAX_WATCH_RULES} 上限")


class TerminalOpenArguments(_CommonExecutionArguments):
    command: str | None = None
    argv: list[str] | None = None
    mode: TerminalMode = TerminalMode.INTERACTIVE
    rows: int = Field(default=24, ge=TERMINAL_MIN_SIDE, le=TERMINAL_MAX_SIDE)
    cols: int = Field(default=80, ge=TERMINAL_MIN_SIDE, le=TERMINAL_MAX_SIDE)
    prompt_regex: str | None = None
    quiet_ms: int = Field(default=8000, ge=0, le=600_000)
    startup_grace_ms: int = Field(default=15000, ge=0, le=600_000)
    watch: list[WatchRule] = Field(default_factory=list)

    def validate_one_form(self) -> None:
        if bool(self.command) == bool(self.argv):
            raise ValueError("command 与 argv 必须二选一")


class TerminalSendArguments(_InputBase):
    terminal_id: str
    text: str | None = None
    submit: bool = False
    keys: list[str] = Field(default_factory=list)
    hex: list[str] = Field(default_factory=list)
    paste: str | None = None
    wait: bool = True
    wait_ms: int = Field(default=10000, ge=0, le=60_000)
    prompt_regex: str | None = None
    quiet_ms: int | None = Field(default=None, ge=0, le=600_000)


class TerminalResizeArguments(_Base):
    terminal_id: str
    rows: int = Field(ge=TERMINAL_MIN_SIDE, le=TERMINAL_MAX_SIDE)
    cols: int = Field(ge=TERMINAL_MIN_SIDE, le=TERMINAL_MAX_SIDE)


class TerminalCloseArguments(_Base):
    terminal_id: str
    #: 是否在正常终止未确认时升级为强制结束；仍无法确认则如实返回 unknown。
    escalate: bool = True


class CancelResourceArguments(_Base):
    resource_id: str


ACTION_ARGUMENT_MODELS: dict[str, type[BaseModel]] = {
    "run_command": CommandArguments,
    "run_script": ScriptArguments,
    "start_monitor": MonitorArguments,
    "terminal_open": TerminalOpenArguments,
    "terminal_send": TerminalSendArguments,
    "terminal_resize": TerminalResizeArguments,
    "terminal_close": TerminalCloseArguments,
    "cancel_resource": CancelResourceArguments,
}


# --------------------------------------------------------------------------- #
# 输出与资源视图
# --------------------------------------------------------------------------- #


class ByteRange(_Base):
    """原始字节区间（左闭右开）。"""

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    reason: str = ""


class ArtifactPointer(_Base):
    """工件引用：不可变快照/区间的标识与哈希。"""

    artifact_id: str
    name: str = ""
    media_type: str = "application/octet-stream"
    byte_size: int = 0
    sha256: str = ""
    byte_range: ByteRange | None = None
    availability: OutputAvailability = OutputAvailability.AVAILABLE


class TerminalScreenView(_Base):
    """终端屏幕快照。`rebuilt` 表示由保留日志重建而非实时维护状态。"""

    rows: int
    cols: int
    lines: list[str] = Field(default_factory=list)
    cursor_x: int = 0
    cursor_y: int = 0
    captured_at: str = ""
    rebuilt: bool = False
    truncated: bool = False
    note: str = ""


class OutputPageV2(_Base):
    """有界输出页。游标绑定资源/stream/offset，不能用于读取其他资源。"""

    resource_id: str
    stream: str = "stdout"
    cursor: str | None = None
    next_cursor: str | None = None
    byte_start: int = 0
    byte_end: int = 0
    text: str = ""
    byte_count: int = 0
    #: 是否已读到**当前**末尾；不代表程序已结束。
    at_current_end: bool = False
    process_exited: bool = False
    truncated: bool = False
    missing_ranges: list[ByteRange] = Field(default_factory=list)
    encoding_error: str | None = None
    artifact_refs: list[ArtifactPointer] = Field(default_factory=list)
    note: str = ""


class ResourceStatusView(_Base):
    """资源状态视图（控制端只读投影的来源）。"""

    resource_id: str
    session_id: str = ""
    run_id: str = ""
    kind: ResourceKind
    state: ResourceStatus
    interaction_state: InteractionState = InteractionState.NOT_APPLICABLE
    mode: str = ""
    purpose: str = ""
    created_at: str = ""
    started_at: str | None = None
    exited_at: str | None = None
    closed_at: str | None = None
    exit_code: int | None = None
    termination_reason: TerminationReason | None = None
    deadline_at: str | None = None
    output_limit_action: OutputLimitAction = OutputLimitAction.PRESERVE_PROCESS
    output_bytes: int = 0
    dropped_bytes: int = 0
    output_limit_bytes: int = 0
    cancel_requested: bool = False
    cancel_confirmed: bool = False
    last_checked_at: str = ""
    watch_hits: int = 0
    notes: list[str] = Field(default_factory=list)


class ResourceListPage(_Base):
    schema_version: int = EXECUTION_SCHEMA_VERSION
    executor_id: str = ""
    session_id: str = ""
    resources: list[ResourceStatusView] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int = 0
    session_quota: dict[str, int] = Field(default_factory=dict)


class OperationReceipt(_Base):
    """操作回执。`status` 只表示**本次操作**的状态，不代表被控制程序已结束。"""

    schema_version: int = EXECUTION_SCHEMA_VERSION
    operation_id: str
    action: str = ""
    status: str
    resource_id: str | None = None
    job_id: str | None = None
    terminal_id: str | None = None
    process_state: ResourceStatus | None = None
    process_confirmed_exited: bool | None = None
    interaction_state: InteractionState | None = None
    wait_reason: WaitReason | None = None
    exit_code: int | None = None
    termination_reason: TerminationReason | None = None
    resource: ResourceStatusView | None = None
    output: OutputPageV2 | None = None
    deduplicated: bool = False
    accepted_at: str = ""
    executed_at: str | None = None
    note: str = ""
    capabilities_note: str | None = None


class ExecutionEventRecord(_Base):
    seq: int
    event_id: str
    executor_id: str = ""
    session_id: str = ""
    resource_id: str | None = None
    operation_id: str | None = None
    type: str
    occurred_at: str
    payload: dict[str, Any] = Field(default_factory=dict)
    availability: OutputAvailability = OutputAvailability.AVAILABLE


class ExecutionEventPage(_Base):
    schema_version: int = EXECUTION_SCHEMA_VERSION
    executor_id: str = ""
    events: list[ExecutionEventRecord] = Field(default_factory=list)
    next_after: int = 0
    has_more: bool = False


class ExecutionEnvironmentReport(_Base):
    """环境发现结果。不返回秘密（代理/CA 只报是否配置）。"""

    schema_version: int = EXECUTION_SCHEMA_VERSION
    executor_id: str = ""
    platform: str = ""
    os_release: str = ""
    python_version: str = ""
    shell: dict[str, str] = Field(default_factory=dict)
    interpreters: dict[str, str] = Field(default_factory=dict)
    tools: dict[str, str] = Field(default_factory=dict)
    missing: dict[str, str] = Field(default_factory=dict)
    proxy_configured: bool = False
    ca_configured: bool = False
    workspace_root: str = ""
    workspace_storage_enforcement: str = "unsupported"
    workspace_bytes_used: int = 0
    dependency_environment_hint: str = ""
    notes: list[str] = Field(default_factory=list)


class ExecutionCapabilityDetails(_Base):
    """执行子协议能力明细，作为 `/capabilities.details` 的结构化字段。"""

    execution_mode: str = "disabled"
    execution_runtime_versions: list[int] = Field(default_factory=list)
    command: CapabilityState = CapabilityState.UNAVAILABLE
    script: CapabilityState = CapabilityState.UNAVAILABLE
    pty: CapabilityState = CapabilityState.UNAVAILABLE
    screen: CapabilityState = CapabilityState.UNAVAILABLE
    monitor: CapabilityState = CapabilityState.UNAVAILABLE
    durable_events: CapabilityState = CapabilityState.UNAVAILABLE
    detached_job_recovery: CapabilityState = CapabilityState.UNAVAILABLE
    skill_packages: CapabilityState = CapabilityState.UNAVAILABLE
    output_limit_actions: list[str] = Field(default_factory=list)
    workspace_storage_enforcement: str = "unsupported"
    dependencies: dict[str, str] = Field(default_factory=dict)
    unavailable_reasons: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


def validate_arguments(action: str, arguments: dict[str, Any]) -> BaseModel:
    """按动作校验参数。未知动作或非法参数直接抛错（在产生副作用之前）。"""
    model = ACTION_ARGUMENT_MODELS.get(action)
    if model is None:
        raise ValueError(f"未知执行动作：{action}")
    parsed = model.model_validate(arguments)
    validator = getattr(parsed, "validate_one_form", None)
    if validator is not None:
        validator()
    return parsed


__all__ = [
    "ACTION_ARGUMENT_MODELS",
    "BRACKETED_PASTE_END",
    "BRACKETED_PASTE_START",
    "DEFAULT_OUTPUT_PAGE_BYTES",
    "DEFAULT_RESOURCE_LOG_LIMIT_BYTES",
    "DEFAULT_SESSION_LOG_BUDGET_BYTES",
    "EXECUTION_PREFIX",
    "EXECUTION_SCHEMA_VERSION",
    "EXECUTION_SUBPROTOCOL_VERSION",
    "INPUT_ORDER",
    "KEY_BYTES",
    "MAX_HEX_BYTES",
    "MAX_HEX_ITEMS",
    "MAX_OUTPUT_PAGE_BYTES",
    "MAX_WATCH_PATTERN_CHARS",
    "MAX_WATCH_RULES",
    "SUPPORTED_KEYS",
    "CancelResourceArguments",
    "CapabilityState",
    "CommandArguments",
    "ExecutionCapabilityDetails",
    "ExecutionControlAck",
    "ExecutionControlSync",
    "ExecutionEnvironmentReport",
    "ExecutionEventPage",
    "ExecutionEventRecord",
    "ExecutionEventType",
    "ExecutionMode",
    "ExecutionOperationEnvelope",
    "FenceView",
    "InteractionState",
    "MonitorArguments",
    "OperationReceipt",
    "OutputAvailability",
    "OutputLimitAction",
    "OutputPageV2",
    "ResourceKind",
    "ResourceListPage",
    "ResourceStatus",
    "ResourceStatusView",
    "ScriptArguments",
    "ScriptLanguage",
    "StreamName",
    "TerminalCloseArguments",
    "TerminalMode",
    "TerminalOpenArguments",
    "TerminalResizeArguments",
    "TerminalScreenView",
    "TerminalSendArguments",
    "TerminationReason",
    "WaitReason",
    "WatchMode",
    "WatchNotify",
    "WatchRule",
    "validate_arguments",
]
