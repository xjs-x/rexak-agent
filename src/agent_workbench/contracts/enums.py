"""协议枚举。所有状态机取值集中在此，避免在模块里散落字符串字面量。"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    """Run 状态机（BUILD_PLAN §4.2）。

    queued -> running -> waiting_tool / waiting_user / pausing / completed / failed / cancelling

    另有 recovering（重启后核对任务与外部副作用）与 cancelled。
    终态：completed / failed / cancelled。
    """

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_TOOL = "waiting_tool"
    WAITING_USER = "waiting_user"
    PAUSING = "pausing"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})

#: 只有这些状态的 run 可以 resume
RESUMABLE_RUN_STATUSES = frozenset(
    {RunStatus.PAUSED, RunStatus.WAITING_USER, RunStatus.WAITING_TOOL, RunStatus.FAILED}
)

#: 这些状态算"活动"，用于 session 单一活动 run 约束
ACTIVE_RUN_STATUSES = frozenset(
    {
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.WAITING_TOOL,
        RunStatus.WAITING_USER,
        RunStatus.PAUSING,
        RunStatus.PAUSED,
        RunStatus.CANCELLING,
        RunStatus.RECOVERING,
    }
)


class CompletionReason(StrEnum):
    """阶段结束理由，单独记录，不混入成功率。"""

    GOAL_ANSWERED = "goal_answered"
    EXPLORATION_CONVERGED = "exploration_converged"


class CommandKind(StrEnum):
    MESSAGE = "message"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    SCOPE_CHANGE = "scope_change"
    MODEL_SWITCH = "model_switch"


class CommandStatus(StrEnum):
    QUEUED = "queued"
    CONSUMED = "consumed"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class OperationStatus(StrEnum):
    """工具操作状态。`succeeded` 只表示工具成功执行，不表示漏洞确认。"""

    ACCEPTED = "accepted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ExecutionCertainty(StrEnum):
    """外部副作用的不确定性等级（§8.5）。"""

    CONFIRMED = "confirmed"
    UNCERTAIN = "uncertain"
    NOT_APPLIED = "not_applied"


class JobState(StrEnum):
    RECEIVED = "received"
    STARTING = "starting"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ToolEffectClass(StrEnum):
    """操作的副作用类别，与任务策略共同决定是否可自动重试。"""

    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    SIDE_EFFECTING = "side_effecting"


class ArtifactAvailability(StrEnum):
    REMOTE_ONLY = "remote_only"
    SYNCING = "syncing"
    AVAILABLE = "available"
    MISSING = "missing"

    #: 元数据已提交但文件缺失，或反之 —— 待修复的孤儿，**不得**当作可用证据
    ORPHANED = "orphaned"


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    SECRET = "secret"


class LeadStatus(StrEnum):
    NEW = "new"
    INVESTIGATING = "investigating"
    NEEDS_VERIFICATION = "needs_verification"
    CONFIRMED = "confirmed"
    DISPROVED = "disproved"
    BLOCKED = "blocked"
    DEFERRED = "deferred"


#: blocked / deferred 必须有理由与恢复条件
LEAD_STATUSES_REQUIRE_REASON = frozenset({LeadStatus.BLOCKED, LeadStatus.DEFERRED})


class FindingStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    DISPROVED = "disproved"
    INCONCLUSIVE = "inconclusive"


class FindingCategory(StrEnum):
    """首版只交付可扩展的验证接口，不承诺覆盖所有漏洞类别。"""

    INFO_DISCLOSURE = "info_disclosure"
    AUTH_BOUNDARY = "auth_boundary"
    ACCESS_CONTROL = "access_control"
    INPUT_VALIDATION = "input_validation"
    CONFIGURATION = "configuration"
    TRANSPORT = "transport"
    SESSION_MANAGEMENT = "session_management"
    OTHER = "other"


class CoverageStatus(StrEnum):
    NOT_CHECKED = "not_checked"
    CHECKED_NEGATIVE = "checked_negative"
    CHECKED_POSITIVE = "checked_positive"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"


class UsageStatus(StrEnum):
    PENDING = "pending"
    ESTIMATED = "estimated"
    REPORTED = "reported"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class CostStatus(StrEnum):
    ESTIMATED = "estimated"
    CALCULATED = "calculated"
    UNPRICED = "unpriced"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class ErrorCode(StrEnum):
    """稳定错误码。**不能从中文错误文本猜测类型。**"""

    VALIDATION_FAILED = "validation_failed"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    SCOPE_DENIED = "scope_denied"
    IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
    RUN_NOT_RESUMABLE = "run_not_resumable"
    RUN_ALREADY_ACTIVE = "run_already_active"
    LEASE_LOST = "lease_lost"
    EXECUTOR_UNAVAILABLE = "executor_unavailable"
    EXECUTOR_INCOMPATIBLE = "executor_incompatible"
    OPERATION_CONFLICT = "operation_conflict"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PROVIDER_ERROR = "provider_error"
    CONTEXT_OVERFLOW = "context_overflow"
    GRAPH_RECURSION_LIMIT = "graph_recursion_limit"
    INTERNAL_ERROR = "internal_error"


class Phase(StrEnum):
    """可分摊的耗时阶段（§14.2）。"""

    COMMAND_TO_RUNNER = "command_to_runner"
    CONTEXT_BUILD = "context_build"
    MODEL_QUEUE = "model_queue"
    MODEL_FIRST_TOKEN = "model_first_token"
    MODEL_COMPLETE = "model_complete"
    MODEL_RETRY_WAIT = "model_retry_wait"
    TOOL_ADMISSION = "tool_admission"
    TOOL_TUNNEL = "tool_tunnel"
    TOOL_REMOTE_QUEUE = "tool_remote_queue"
    TOOL_EXECUTE = "tool_execute"
    ARTIFACT_SYNC = "artifact_sync"
    RESULT_POSTPROCESS = "result_postprocess"
    TOOL_DONE_TO_NEXT_MODEL = "tool_done_to_next_model"
    COMPLETION_TO_REPORT = "completion_to_report"
