"""协议真源（single source of truth）。

前端 TS 类型由 OpenAPI 生成（`web/src/api/generated/`），SSE 事件模型也导出为 schema。
**禁止**在别处手工维护第二份枚举或字段清单。
"""

from __future__ import annotations

from agent_workbench.contracts.enums import (
    ArtifactAvailability,
    CommandKind,
    CommandStatus,
    CompletionReason,
    CostStatus,
    CoverageStatus,
    ErrorCode,
    ExecutionCertainty,
    FindingCategory,
    FindingStatus,
    JobState,
    LeadStatus,
    OperationStatus,
    RunStatus,
    Sensitivity,
    ToolEffectClass,
    UsageStatus,
)
from agent_workbench.contracts.events import (
    EVENT_SCHEMA_VERSION,
    EventEnvelope,
    EventType,
)
from agent_workbench.contracts.identity import (
    Project,
    ProjectCreate,
    Run,
    RunCreate,
    Session,
    SessionCreate,
    UserCommand,
    UserCommandCreate,
)
from agent_workbench.contracts.tool import (
    TOOL_RESULT_SCHEMA_VERSION,
    ArtifactRef,
    JobReceipt,
    JobSnapshot,
    OutputPage,
    ToolError,
    ToolOperation,
    ToolResult,
)
from agent_workbench.contracts.usage import ModelCallUsage, PriceSnapshot, UsageUpdate

__all__ = [
    "EVENT_SCHEMA_VERSION",
    "TOOL_RESULT_SCHEMA_VERSION",
    "ArtifactAvailability",
    "ArtifactRef",
    "CommandKind",
    "CommandStatus",
    "CompletionReason",
    "CostStatus",
    "CoverageStatus",
    "ErrorCode",
    "EventEnvelope",
    "EventType",
    "ExecutionCertainty",
    "FindingCategory",
    "FindingStatus",
    "JobReceipt",
    "JobSnapshot",
    "JobState",
    "LeadStatus",
    "ModelCallUsage",
    "OperationStatus",
    "OutputPage",
    "PriceSnapshot",
    "Project",
    "ProjectCreate",
    "Run",
    "RunCreate",
    "RunStatus",
    "Sensitivity",
    "Session",
    "SessionCreate",
    "ToolEffectClass",
    "ToolError",
    "ToolOperation",
    "ToolResult",
    "UsageStatus",
    "UsageUpdate",
    "UserCommand",
    "UserCommandCreate",
]
