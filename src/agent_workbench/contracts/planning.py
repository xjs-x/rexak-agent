"""增量规划的有界合同。检查结果由程序生成，不是模型可写的验证等级。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PLAN_REVISION_SUMMARY = "计划修订候选：是否生效必须检查 checkpoint 引用"


class PlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class PlanningSource(PlanningModel):
    kind: Literal["artifact", "operation", "observation", "verification"]
    id: UUID
    relation: Literal["direct", "supporting"] = "direct"


class PlanningValidationError(ValueError):
    """可操作的规划反馈；不携带凭据、正文或隐式重执行指令。"""

    def __init__(self, code: str, message: str, *, recovery: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.recovery = recovery
        self.details = details


def http_endpoint(value: str) -> str:
    """范围只包含协议、主机、端口和路径；不把载荷、凭据放进计划。"""
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("HTTP 范围必须是无凭据的 http(s) 端点")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    host = parts.hostname.lower().rstrip(".")
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}:{port}{parts.path or '/'}"


class HttpWorkItem(PlanningModel):
    """证明检查了哪些端点/方法；不是漏洞语义核验器。"""

    method: str = Field(min_length=1, max_length=20)
    url: str = Field(min_length=1, max_length=2048)
    identity: str | None = Field(default=None, max_length=128)
    authentication: Literal["anonymous", "identity_session", "explicit_headers"] | None = None

    @field_validator("method")
    @classmethod
    def http_method(cls, value: str) -> str:
        if not value.isascii() or not value.isalpha():
            raise ValueError("HTTP 方法必须为 ASCII 字母")
        return value.upper()

    @field_validator("url")
    @classmethod
    def endpoint_without_payload(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.query or parts.fragment:
            raise ValueError("HTTP 工作范围只填端点，不填查询载荷或 fragment；参数条件仍属模型判断")
        return http_endpoint(value)


class CompletionPredicate(PlanningModel):
    """只检查持久化操作回执的结构化标量；不执行模型提供的代码。"""

    operation_id: UUID
    path: list[str] = Field(min_length=1, max_length=8)
    equals: str | int | float | bool | None

    @field_validator("path")
    @classmethod
    def bounded_components(cls, value: list[str]) -> list[str]:
        if any(not part or len(part) > 128 for part in value):
            raise ValueError("谓词路径每项必须为 1–128 字符")
        return value

    @field_validator("equals")
    @classmethod
    def bounded_scalar(
        cls, value: str | int | float | bool | None
    ) -> str | int | float | bool | None:
        if isinstance(value, str) and len(value) > 500:
            raise ValueError("谓词预期值最多 500 字；长正文应保存工件")
        return value


class StepCompletionSpec(PlanningModel):
    mode: Literal["investigate", "achieve"]
    done_when: str = Field(min_length=1, max_length=500)
    check_type: Literal["model", "predicate", "verification"] = "model"
    predicate: CompletionPredicate | None = None
    verification_id: UUID | None = None
    http_checks: list[HttpWorkItem] = Field(default_factory=list, max_length=12)
    not_before: datetime | None = None

    @field_validator("not_before")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("not_before 必须带时区；不设置隐式证据有效期")
        return value

    @field_validator("http_checks")
    @classmethod
    def unique_checks(cls, value: list[HttpWorkItem]) -> list[HttpWorkItem]:
        if len({(v.method, v.url, v.identity, v.authentication) for v in value}) != len(value):
            raise ValueError("http_checks 不能重复")
        return value

    @model_validator(mode="after")
    def check_parameters(self) -> StepCompletionSpec:
        if (self.check_type == "predicate") != (self.predicate is not None):
            raise ValueError("predicate 检查必须且只能提供 predicate")
        if (self.check_type == "verification") != (self.verification_id is not None):
            raise ValueError("verification 检查必须且只能提供 verification_id")
        return self


class StepCompletionClaim(PlanningModel):
    outcome: Literal["satisfied", "not_satisfied", "inconclusive"]
    summary: str = Field(min_length=1, max_length=500)
    source_refs: list[PlanningSource] = Field(min_length=1, max_length=8)


class SourceLink(PlanningModel):
    kind: Literal["artifact", "operation", "observation", "verification"]
    id: UUID
    relation: Literal["direct", "supporting"]
    operation_ids: list[UUID] = Field(max_length=64)
    source_step_ids: list[str] = Field(max_length=20)


class HttpCoverage(PlanningModel):
    operation_id: UUID
    method: str
    url: str
    identity: str
    authentication: Literal["anonymous", "identity_session", "explicit_headers", "unknown"]
    status_code: int = Field(ge=100, le=599)
    source_step_id: str | None
    operation_created_at: datetime


class StepCompletionCheck(PlanningModel):
    status: Literal["accepted"] = "accepted"
    method: Literal["model", "predicate", "verification"]
    scope: str = Field(max_length=300)
    spec_digest: str
    source_signature: str
    context_signature: str
    run_id: str
    version: Literal[1, 2] = 1
    source_authenticity: Literal["checked"] | None = None
    source_applicability: Literal["checked", "limited"] | None = None
    sufficiency: Literal["model_judgment", "receipt_fields", "verification_record"] | None = None
    http_coverage: list[HttpCoverage] = Field(default_factory=list, max_length=12)
    source_links: list[SourceLink] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class StepCompletionRecord(StepCompletionClaim):
    check: StepCompletionCheck


class AssessmentInput(PlanningModel):
    changed: str = Field(min_length=1, max_length=500)
    next_action: str = Field(min_length=1, max_length=500)
    uncertainty: str | None = Field(default=None, max_length=500)
    evidence_refs: list[str] = Field(default_factory=list, max_length=8)
    evaluated_step_ids: list[str] = Field(default_factory=list, max_length=20)
    evaluated_plan_revision: int = Field(ge=0)
    decision: Literal["continue", "repair", "expand", "switch", "defer", "close"]
    trigger: Literal[
        "new_evidence",
        "condition_changed",
        "tool_failure",
        "no_progress",
        "user_instruction",
        "initial_plan",
        "completion",
        "plan_adjustment",
    ]
    basis_refs: list[PlanningSource] = Field(default_factory=list, max_length=8)


def planning_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def completion_review_reason(step: dict[str, Any]) -> str | None:
    """只做静态合同检查；来源的新鲜度由领域检查器再次核对。"""
    status = step.get("status")
    if status == "dropped":
        if step.get("completion_protocol") == 1 and not str(step.get("rationale") or "").strip():
            return "放弃步骤缺少处置理由"
        return None
    if status != "done":
        return None
    if step.get("legacy_completion"):
        return "历史完成步骤尚未按当前条件复查，不能补造验证"
    if step.get("completion_protocol") != 1:
        return None
    try:
        spec = StepCompletionSpec.model_validate(step.get("completion_spec"))
        record = StepCompletionRecord.model_validate(step.get("completion"))
    except ValueError:
        return "完成步骤缺少有效完成条件或程序检查记录"
    if record.check.version == 1:
        return "旧完成检查需补充工作范围并复核；使用已有来源重新提交，不要自动重测"
    if record.check.spec_digest != planning_digest(spec.model_dump(mode="json")):
        return "完成条件已变化，原完成记录失效"
    if record.check.method != spec.check_type:
        return "完成检查方式与条件不一致"
    if spec.mode == "achieve" and record.outcome != "satisfied":
        return "达成型任务的预期条件尚未成立"
    if spec.mode == "investigate" and record.outcome == "inconclusive":
        return "调查尚无明确结果，请保留未决工作"
    return None
