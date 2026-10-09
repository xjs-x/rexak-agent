"""HTTP 工具组：`http_request`、`session_info`。

行为与限制（§8.2）：
- 复用**按身份隔离**的连接池（连接池在 Executor 侧维护）；
- 原始响应字节与解析视图**分开**：原始字节落工件，模型只看到摘要 + `artifact_id`；
- 逐跳检查重定向范围；
- 超过交互等待阈值时返回 `accepted` + `job_id`，**不占着模型调用链**。

持久操作身份顺序（§8.5）：
准备 tool_operation -> 提交控制端记录 -> Executor 幂等接收 -> 启动 -> 记录结果 ->
控制端同步 -> 写 tool message/checkpoint。

运行依赖通过 `ToolRuntime[RunContext]` 注入；模型只能看到其余参数的 schema。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from langchain.tools import ToolRuntime
from langchain_core.tools import tool
from pydantic import BaseModel, Field

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.enums import (
    ArtifactAvailability,
    ErrorCode,
    ExecutionCertainty,
    OperationStatus,
    Sensitivity,
    ToolEffectClass,
)
from agent_workbench.contracts.tool import ArtifactRef, ToolError, ToolResult
from agent_workbench.domain.scope import ScopeViolation
from agent_workbench.evidence.artifacts import redact_text
from agent_workbench.execution.client import (
    ExecutorConnectionError,
    ExecutorProtocolError,
    ExecutorRejected,
)
from agent_workbench.persistence.repositories import (
    ArtifactRepository,
)
from agent_workbench.tools.remote import (
    json_text,
    prepare_operation,
    record_result,
    record_submitted,
)
from agent_workbench.tools.runtime import Toolbox, origin_lock_key

# --------------------------------------------------------------------------- #
# 内部入参模型（用于参数互斥校验，不直接作为工具 schema）
# --------------------------------------------------------------------------- #


class HttpRequestArgs(BaseModel):
    method: str = "GET"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str] | None = None
    body: str | None = None
    json_body: Any | None = None
    identity: str = "anonymous"
    follow_redirects: bool = True
    save_body_artifact: bool = True
    note: str | None = None


READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #


async def _store_response_artifacts(
    toolbox: Toolbox,
    *,
    operation_id: UUID,
    job_id: UUID,
    payload: dict[str, Any],
    identity: str,
) -> list[ArtifactRef]:
    """把原始响应字节落成不可变工件，并提交元数据。

    顺序：先写文件（原子替换），再提交数据库元数据。
    文件写成功但元数据失败 -> 标记 `orphaned`，**不返回可用证据**（§5.3）。
    """
    refs: list[ArtifactRef] = []
    for name in ("response.body", "response.meta.json"):
        media_type = (
            str(payload.get("content_type") or "application/octet-stream")
            if name == "response.body"
            else "application/json"
        )
        try:
            data, headers = await toolbox.executor.download_artifact(
                job_id, name, limit=8 * 1024 * 1024
            )
        except (ExecutorConnectionError, ExecutorProtocolError) as exc:
            refs.append(
                ArtifactRef(
                    artifact_id=UUID(int=0),
                    media_type=media_type,
                    byte_size=0,
                    sha256="",
                    availability=ArtifactAvailability.REMOTE_ONLY,
                    source_operation_id=operation_id,
                    summary=f"工件仍在远端、未能同步：{type(exc).__name__}",
                )
            )
            continue

        expected = headers.get("sha256") or ""
        stored = toolbox.artifacts.write_bytes(
            data,
            media_type=media_type,
            sensitivity=Sensitivity.INTERNAL,
            captured_by=identity,
            source_url=str(payload.get("final_url") or ""),
            source_operation=str(operation_id),
            capture_time=datetime.now(UTC),
        )

        if expected and stored.sha256 != expected:
            refs.append(
                ArtifactRef(
                    artifact_id=UUID(int=0),
                    media_type=media_type,
                    byte_size=stored.byte_size,
                    sha256=stored.sha256,
                    availability=ArtifactAvailability.ORPHANED,
                    source_operation_id=operation_id,
                    summary=(
                        f"哈希核对失败（期望 {expected[:12]}…，实际 {stored.sha256[:12]}…）；"
                        "不作为可用证据"
                    ),
                )
            )
            continue

        try:
            async with toolbox.db.transaction() as db:
                row = await ArtifactRepository(db).create(
                    sha256=stored.sha256,
                    byte_size=stored.byte_size,
                    media_type=media_type,
                    relative_key=stored.relative_key,
                    session_id=toolbox.session_id,
                    source_operation_id=operation_id,
                    sensitivity=str(Sensitivity.INTERNAL),
                    availability=str(ArtifactAvailability.AVAILABLE),
                    captured_at=stored.captured_at,
                    truncated=stored.truncated,
                    captured_by_identity=identity,
                    source_url=str(payload.get("final_url") or ""),
                    summary=f"{name}（{stored.byte_size} 字节）",
                )
            refs.append(
                ArtifactRef(
                    artifact_id=row.id,
                    media_type=media_type,
                    byte_size=stored.byte_size,
                    sha256=stored.sha256,
                    availability=ArtifactAvailability.AVAILABLE,
                    sensitivity=Sensitivity.INTERNAL,
                    truncated=stored.truncated,
                    source_operation_id=operation_id,
                    captured_at=stored.captured_at,
                    summary=f"原始 {name}",
                )
            )
        except Exception as exc:
            refs.append(
                ArtifactRef(
                    artifact_id=UUID(int=0),
                    media_type=media_type,
                    byte_size=stored.byte_size,
                    sha256=stored.sha256,
                    availability=ArtifactAvailability.ORPHANED,
                    source_operation_id=operation_id,
                    summary=f"元数据提交失败，待修复孤儿：{type(exc).__name__}",
                )
            )
    return refs


def _preview_text(raw: bytes, content_type: str, limit: int) -> tuple[str, bool]:
    """生成给模型看的可读预览。

    非法 UTF-8 **不做** GBK/Latin-1 兜底猜测，返回明确编码说明（§16.3）。
    """
    chunk = raw[:limit]
    truncated = len(raw) > limit
    textual = content_type.startswith("text/") or content_type in {
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-www-form-urlencoded",
    }
    if not textual:
        return (
            f"[二进制响应] content-type={content_type} 字节数={len(raw)}；原始字节已保存为工件。",
            truncated,
        )
    try:
        return redact_text(chunk.decode("utf-8")), truncated
    except UnicodeDecodeError as exc:
        return (
            f"[编码问题] 响应体前 {len(chunk)} 字节不是合法 UTF-8（首个非法位置 {exc.start}）；"
            "已保留原始字节为工件，未做猜测性解码。",
            truncated,
        )


def _job_state_to_status(state: str) -> OperationStatus:
    return {
        "received": OperationStatus.ACCEPTED,
        "starting": OperationStatus.RUNNING,
        "running": OperationStatus.RUNNING,
        "succeeded": OperationStatus.SUCCEEDED,
        "failed": OperationStatus.FAILED,
        "cancelled": OperationStatus.CANCELLED,
        "unknown": OperationStatus.UNKNOWN,
    }.get(state, OperationStatus.UNKNOWN)


async def _load_job_meta(toolbox: Toolbox, job_id: UUID) -> dict[str, Any]:
    try:
        data, _headers = await toolbox.executor.download_artifact(
            job_id, "response.meta.json", limit=256 * 1024
        )
    except (ExecutorConnectionError, ExecutorProtocolError):
        return {}
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _read_body_preview(
    toolbox: Toolbox, job_id: UUID, payload: dict[str, Any]
) -> bytes | None:
    size = int(payload.get("body_bytes") or 0)
    if size <= 0:
        return None
    limit = min(size, toolbox.settings.inline_output_limit_bytes)
    try:
        data, _headers = await toolbox.executor.download_artifact(
            job_id, "response.body", offset=0, limit=limit
        )
    except (ExecutorConnectionError, ExecutorProtocolError):
        return None
    return data


def _summarize_http(state: str, payload: dict[str, Any]) -> str:
    if state == "succeeded":
        return (
            f"HTTP {payload.get('status_code')}，最终地址 {payload.get('final_url')}，"
            f"响应体 {payload.get('body_bytes')} 字节。"
            "注意：请求成功本身不代表目标存在缺陷。"
        )
    if state == "unknown":
        return (
            "执行状态无法确认：既不能证明仍在运行，也不能证明已结束。"
            "请先用 get_job 查询结论，禁止直接重试。"
        )
    if state == "cancelled":
        return "该请求已被取消。"
    if state == "failed":
        return f"请求未成功执行：{payload.get('error_message') or '未返回原因'}"
    return f"当前状态：{state}"


def _digest_for(arguments: dict[str, Any]) -> str:
    from agent_workbench.persistence.repositories import compute_request_digest

    return compute_request_digest({"tool": "http_request", "arguments": arguments})


def _scope_payload(toolbox: Toolbox) -> dict[str, Any]:
    """构造执行岛可接受的范围载荷。

    **只发送执行岛 schema 声明过的字段**（执行岛对未知字段是 `extra="forbid"`）。
    控制端内部的 `notes` 等展示字段不进载荷。
    """
    snapshot = toolbox.scope.snapshot
    return {
        "revision": snapshot.revision,
        "rules": [
            {
                "scheme": rule.scheme,
                "host": rule.host,
                "port": rule.port,
                "include_subdomains": rule.include_subdomains,
                "allow_private": rule.allow_private,
            }
            for rule in snapshot.rules
        ],
        "mappings": [
            {
                key: value
                for key, value in {
                    "from_host": item.get("from_host"),
                    "from_port": item.get("from_port"),
                    "to_host": item.get("to_host"),
                    "to_port": item.get("to_port"),
                    "host_header": item.get("host_header"),
                    "sni": item.get("sni"),
                    "scheme": item.get("scheme"),
                }.items()
                if value is not None
            }
            for item in snapshot.target_mappings
        ],
        "denied_metadata_hosts": list(snapshot.denied_metadata_hosts),
    }


def _failed(operation_id: UUID, code: ErrorCode, message: str, **extra: Any) -> ToolResult:
    return ToolResult(
        operation_id=operation_id,
        status=OperationStatus.FAILED,
        summary=message,
        error=ToolError(code=str(code), message=message, **extra),
        execution_certainty=ExecutionCertainty.NOT_APPLIED,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


async def run_http_request(
    args: HttpRequestArgs, toolbox: Toolbox, tool_call_id: str
) -> ToolResult:
    started_at = datetime.now(UTC)

    # 1) 范围预检：给出**清楚原因**；最终放行由执行岛 NetworkPolicy 决定
    try:
        toolbox.scope.check_url(args.url)
    except ScopeViolation as exc:
        return _failed(
            UUID(int=0),
            ErrorCode.SCOPE_DENIED,
            f"范围预检未通过：{exc.reason}",
            recovery_hint="如果该目标确实属于测试范围，请让用户在项目范围中显式加入。",
            details={"target": exc.target},
        )

    if args.body is not None and args.json_body is not None:
        return _failed(UUID(int=0), ErrorCode.VALIDATION_FAILED, "body 与 json_body 不能同时提供")

    method = args.method.upper()
    effect_class = (
        ToolEffectClass.READ_ONLY if method in READ_ONLY_METHODS else ToolEffectClass.SIDE_EFFECTING
    )

    # 2) 派发前检查控制版本：过期响应不得自动执行
    await toolbox.assert_control_current()

    # 3) 资源锁：同 origin 有界并发；写操作叠加身份锁
    keys = [origin_lock_key(args.url)]
    if effect_class is ToolEffectClass.SIDE_EFFECTING:
        keys.append(f"identity:{args.identity}")

    async with toolbox.locks.acquire(keys, timeout=90.0):
        arguments = args.model_dump(exclude={"note"})
        operation_id, _replayed = await prepare_operation(
            toolbox,
            tool_call_id or f"http_request:{method}:{args.url}",
            "http_request",
            arguments,
            effect_class=effect_class,
        )

        operation = {
            "operation_id": str(operation_id),
            "kind": "http_request",
            "request_digest": _digest_for(arguments),
            "scope_revision": toolbox.scope_revision,
            "owner_generation": toolbox.owner_generation,
            "scope": _scope_payload(toolbox),
            "payload": {
                "method": method,
                "url": args.url,
                "headers": args.headers,
                "params": args.params,
                "body": args.body,
                "json_body": args.json_body,
                "identity_id": args.identity,
                "max_redirects": 10 if args.follow_redirects else 0,
                "save_artifact": args.save_body_artifact,
            },
        }

        # 4) 提交到执行岛（幂等：同 operation_id 同 digest 返回原 job）
        try:
            receipt = await toolbox.executor.submit(operation)
        except ExecutorRejected as exc:
            # 明确拒绝 => 操作**确定没有执行**
            await record_result(
                toolbox,
                operation_id=operation_id,
                job_id=None,
                status=str(OperationStatus.FAILED),
                refs=[],
                failure=f"被执行岛拒绝：{exc}",
            )
            return ToolResult(
                operation_id=operation_id,
                status=OperationStatus.FAILED,
                summary=f"执行岛明确拒绝了该操作，请求未发出：{exc}",
                error=ToolError(
                    code=str(ErrorCode.VALIDATION_FAILED),
                    message=str(exc),
                    retryable=False,
                    recovery_hint=(
                        "这属于参数/策略不匹配，重试同样会被拒绝。"
                        "请修正参数或（如果目标确实在范围内）让用户更新范围配置。"
                    ),
                ),
                execution_certainty=ExecutionCertainty.NOT_APPLIED,
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )
        except (ExecutorConnectionError, ExecutorProtocolError) as exc:
            # 传输不确定 => 可能已经执行，必须先查询
            return ToolResult(
                operation_id=operation_id,
                status=OperationStatus.UNKNOWN,
                summary="无法确认远端是否已接收该操作；必须先查询再决定后续，不要直接重试",
                error=ToolError(
                    code=str(ErrorCode.EXECUTOR_UNAVAILABLE),
                    message=str(exc),
                    retryable=True,
                    recovery_hint=(
                        "按同一 operation_id 重新提交只会得到原 job，不会产生第二个副作用。"
                    ),
                ),
                execution_certainty=ExecutionCertainty.UNCERTAIN,
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )

        await record_submitted(toolbox, operation_id, receipt.job_id)

        # 5) 交互阈值内等待；超时返回 accepted，模型调用链不被占住
        try:
            snapshot = await asyncio.wait_for(
                toolbox.wait_for_terminal(receipt.job_id),
                timeout=toolbox.interactive_threshold_seconds,
            )
        except TimeoutError:
            return ToolResult.accepted(
                operation_id,
                receipt.job_id,
                (
                    "请求已受理但尚未结束（超过交互等待阈值）。稍后用 get_job 查询状态，"
                    "或 read_job_output 增量读取输出；**不要**重复提交同一请求。"
                ),
            )

    # 6) 组装统一结果
    status = _job_state_to_status(str(snapshot.state))
    payload = await _load_job_meta(toolbox, receipt.job_id)
    refs: list[ArtifactRef] = []
    if status is OperationStatus.SUCCEEDED and args.save_body_artifact:
        refs = await _store_response_artifacts(
            toolbox,
            operation_id=operation_id,
            job_id=receipt.job_id,
            payload=payload,
            identity=args.identity,
        )

    data: dict[str, Any] = {
        "job_id": str(receipt.job_id),
        "method": method,
        "url": args.url,
        "scope_revision": toolbox.scope_revision,
    }
    truncated = False
    if payload:
        data.update(
            {
                "status_code": payload.get("status_code"),
                "final_url": payload.get("final_url"),
                "content_type": payload.get("content_type"),
                "redirect_chain": payload.get("redirect_chain"),
                "elapsed_ms": payload.get("elapsed_ms"),
                "body_bytes": payload.get("body_bytes"),
                "body_sha256": payload.get("body_sha256"),
            }
        )
        if status is OperationStatus.SUCCEEDED:
            raw = await _read_body_preview(toolbox, receipt.job_id, payload)
            if raw is not None:
                preview, truncated = _preview_text(
                    raw,
                    str(payload.get("content_type") or ""),
                    toolbox.settings.inline_output_limit_bytes,
                )
                data["body_preview"] = preview
        if payload.get("decode_error"):
            data["decode_error"] = payload["decode_error"]

    error = None
    if status is OperationStatus.FAILED and snapshot.error is not None:
        error = ToolError(
            code=snapshot.error.code,
            message=snapshot.error.message or "执行岛未返回明确原因",
            retryable=snapshot.error.retryable,
        )
    elif status is OperationStatus.UNKNOWN:
        error = ToolError(
            code=str(ErrorCode.EXECUTOR_UNAVAILABLE),
            message="执行状态不确定，需先查询 job 结论",
            retryable=False,
            recovery_hint="使用 get_job 查询；在结论明确前不要重做该操作。",
        )

    result = ToolResult(
        operation_id=operation_id,
        job_id=receipt.job_id,
        status=status,
        summary=_summarize_http(str(snapshot.state), payload),
        data=data,
        artifact_refs=refs,
        error=error,
        truncated=truncated,
        execution_certainty=(
            ExecutionCertainty.CONFIRMED
            if status in {OperationStatus.SUCCEEDED, OperationStatus.FAILED}
            else ExecutionCertainty.UNCERTAIN
        ),
        started_at=started_at,
        finished_at=datetime.now(UTC),
    )

    await record_result(
        toolbox,
        operation_id=operation_id,
        job_id=receipt.job_id,
        status=str(snapshot.state),
        refs=refs,
    )
    return result


# --------------------------------------------------------------------------- #
# 对外工具
# --------------------------------------------------------------------------- #


@tool("http_request")
async def http_request(
    runtime: ToolRuntime[RunContext],
    url: str,
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = "GET",
    identity: str = "anonymous",
    headers_json: str | None = None,
    params_json: str | None = None,
    body: str | None = None,
    json_body_json: str | None = None,
    follow_redirects: bool = True,
    save_body_artifact: bool = True,
) -> str:
    """对测试范围内的目标发起一次 HTTP 请求，返回统一 ToolResult。

    Args:
        url: 完整 URL，必须落在项目声明的测试范围内。重定向会逐跳重新做范围检查。
        method: HTTP 方法。
        identity: 使用哪个测试身份对应的隔离会话（如 anonymous 或账号别名）。
            不同身份使用独立 Cookie jar，不会互相污染。
        headers_json: 附加请求头的 JSON 对象字符串，例如 `{"X-Test":"1"}`。
        params_json: 查询参数的 JSON 对象字符串，例如 `{"id":"1"}`。
        body: 原始请求体字符串。与 json_body_json 互斥。
        json_body_json: JSON 请求体的 JSON 字符串。与 body 互斥。
        follow_redirects: 是否跟随重定向（每一跳仍会做范围检查）。
        save_body_artifact: 是否把原始响应体保存为不可变工件（建议保持开启，
            否则后续无法引用原始证据）。

    Returns:
        ToolResult 的 JSON 文本：status、summary、data（状态码/最终地址/响应预览）
        与 artifact_refs（原始响应字节的不可变引用）。

    Notes:
        - `200` 只表示请求成功，**不代表**目标存在缺陷；反之 `4xx/5xx` 也不代表目标安全。
        - 长于交互阈值的请求会返回 `accepted` 与 `job_id`，请用 `get_job` /
          `read_job_output` 跟进，不要重复提交。
    """
    toolbox = runtime.context.toolbox
    args = HttpRequestArgs(
        method=method,
        url=url,
        identity=identity,
        headers=_parse_json_object(headers_json, "headers_json"),
        params=_parse_json_object(params_json, "params_json"),
        body=body,
        json_body=_parse_json_value(json_body_json, "json_body_json"),
        follow_redirects=follow_redirects,
        save_body_artifact=save_body_artifact,
    )
    result = await run_http_request(args, toolbox, runtime.tool_call_id or "")
    return json_text(result)


@tool("session_info")
async def session_info(
    runtime: ToolRuntime[RunContext],
    identity: str = "anonymous",
) -> str:
    """查看当前会话、范围版本、身份隔离说明与工具调用统计（本地查询，无远端请求）。

    Args:
        identity: 要查询的身份标识。
    """
    toolbox = runtime.context.toolbox
    result = ToolResult(
        operation_id=UUID(int=0),
        status=OperationStatus.SUCCEEDED,
        summary="当前会话信息（本地统计，不涉及远端请求）",
        data={
            "identity": identity,
            "scope_revision": toolbox.scope_revision,
            "policy_revision": toolbox.policy_revision,
            "tool_stats": toolbox.stats,
            "identity_isolation": (
                "每个身份在 Executor 侧使用独立 BrowserContext 与 HTTP Cookie jar；"
                "跨身份不共享 Cookie。"
            ),
            "cookie_note": (
                "Cookie 只在显式同身份会话转换时同步，并检查域/路径/Secure 与身份版本；"
                "浏览器登录不保证 HTTP 客户端自动获得同样的认证语义。"
            ),
        },
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    return json_text(result)


def _parse_json_object(raw: str | None, field: str) -> dict[str, str]:
    if not raw:
        return {}
    parsed = _parse_json_value(raw, field)
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} 必须是 JSON 对象")
    return {str(k): str(v) for k, v in parsed.items()}


def _parse_json_value(raw: str | None, field: str) -> Any:
    if raw is None or raw == "":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} 不是合法 JSON：{exc}") from exc


HTTP_TOOLS = [http_request, session_info]


def build_http_tools(toolbox: Toolbox) -> list[Any]:
    """兼容入口：HTTP 工具现在通过 `ToolRuntime` 注入依赖，无需按 run 构造。

    保留该函数是为了让调用方（`agent/factory.py`）保持单一装配入口的写法。
    """
    _ = toolbox
    return list(HTTP_TOOLS)
