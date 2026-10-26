"""控制端任务授权与有限 HTTP 测试配方；二者不互相证明，不授予执行权限。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent_workbench.domain.history_action_reflection import digest
from agent_workbench.domain.trusted_http_shadow import ControlGrant


class HttpTestProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    # 参数为控制端固定配方；模型只选择已发布配方，不提交任意请求再自称可信。
    request: dict[str, Any]
    environment: str
    configuration: str
    prerequisites: dict[str, Any]
    retest_required: bool = False


class TaskAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["shadow-task-authorization/1"] = "shadow-task-authorization/1"
    session_id: str
    run_id: str
    task_version: str
    revision: int = Field(ge=1)
    status: Literal["active", "revoked"]
    valid_until: str
    scope_revision: int
    scope_sha256: str
    policy_revision: int
    control_version: int
    executor_id: str
    executor_boot_id: str
    http_enabled: bool
    profiles: dict[str, HttpTestProfile]


def load_authorization(box: Any) -> TaskAuthorization:
    path = box.settings.history_action_shadow_context_path
    if path is None:
        raise ValueError("task_authorization_not_configured")
    if path.stat().st_size > 1048576:
        raise ValueError("task_authorization_size_limit")
    policy = TaskAuthorization.model_validate_json(path.read_bytes())
    expiry = datetime.fromisoformat(policy.valid_until)
    caps = getattr(box.executor, "_preflight", None)
    if (
        policy.status != "active"
        or expiry.tzinfo is None
        or expiry <= datetime.now(UTC)
        or policy.session_id != str(box.session_id)
        or policy.run_id != str(box.run_id)
        or policy.scope_revision != box.scope_revision
        or policy.scope_sha256 != digest(box.scope.snapshot.model_dump(mode="json"))
        or policy.policy_revision != box.policy_revision
        or policy.control_version != box.control_version
        or not policy.http_enabled
        or caps is None
        or not caps.http_available
        or caps.executor_id != policy.executor_id
        or caps.boot_id != policy.executor_boot_id
    ):
        raise ValueError("task_authorization_not_current")
    return policy


_SEAL = object()


@dataclass(frozen=True)
class ProfileBinding:
    policy_json: str
    profile_id: str
    argument_sha256: str
    _seal: object = field(repr=False)

    def __deepcopy__(self, memo):
        return self  # 所有数据为不可变标量；不复制控制端身份对象。


def bind_profile(policy: TaskAuthorization, profile_id: str, arguments: dict) -> ProfileBinding:
    if profile_id not in policy.profiles:
        raise ValueError("unpublished_http_test_profile")
    return ProfileBinding(policy.model_dump_json(), profile_id, digest(arguments), _SEAL)


def grant_for_binding(box, binding, arguments, operation, tool_call_id):
    if not isinstance(binding, ProfileBinding) or binding._seal is not _SEAL:
        raise ValueError("untrusted_profile_binding")
    current = load_authorization(box)
    pinned = TaskAuthorization.model_validate_json(binding.policy_json)
    if current != pinned or binding.argument_sha256 != digest(arguments):
        raise ValueError("profile_authorization_or_parameters_changed")
    profile = current.profiles[binding.profile_id]
    from agent_workbench.contracts.planning import planning_digest

    grant = ControlGrant(
        approval_id="task-context:" + digest(current.model_dump()),
        session_id=current.session_id,
        run_id=current.run_id,
        tool_call_id=tool_call_id,
        purpose="security_test",
        production_request_digest=operation["request_digest"],
        task_version=current.task_version,
        environment=profile.environment,
        configuration=profile.configuration,
        prerequisites=profile.prerequisites,
        executor_id=current.executor_id,
        executor_boot_id=current.executor_boot_id,
        mapping_signature=planning_digest(box.scope.snapshot.target_mappings),
        retest_required=profile.retest_required,
    )
    return grant, {
        "authorization_sha256": digest(current.model_dump()),
        "authorization_revision": current.revision,
        "profile_id": binding.profile_id,
        "entry": "run_http_test_profile",
        "entry_arguments": {"profile_id": binding.profile_id},
        "execution_authorization_source": "controller-task-policy-and-existing-dispatch-guards",
        "test_semantics_source": "fixed-controller-http-recipe/1",
    }


def canonical_history_context(context: dict, captures: dict) -> dict:
    """仅转换有控制端历史捕获及准确参数绑定的配方入口；不是工具名称别名表。"""
    result = dict(context)
    entries = {
        v["tool_call_id"]: v["trusted_task"] for v in captures.values() if v.get("trusted_task")
    }
    messages = []
    for message in context.get("messages", []):
        value = dict(message)
        if value.get("type") == "ai":
            calls = []
            for call in value.get("tool_calls", []):
                item = dict(call)
                source = entries.get(str(item.get("id")))
                if (
                    source
                    and item.get("name") == source["entry"]
                    and item.get("args") == source["entry_arguments"]
                ):
                    item["name"] = "http_request"
                calls.append(item)
            value["tool_calls"] = calls
        messages.append(value)
    result["messages"] = messages
    return result
