"""浏览器工具：navigate / snapshot / text / click / fill / press / screenshot / download。

设计要点（BUILD_PLAN §10）：

- **范围在派发前就已判定**：`navigate` 的控制端先做一次范围解析与检查，
  越界的 URL 连请求都不会发到执行岛；
- 执行岛再对**每一个请求**逐跳检查（含 XHR / 图片 / iframe），
  并覆盖范围解析后的地址 —— 只检查导航挡不住页面脚本自己 fetch；
- **最终 URL 判定**：JS 重定向后按 `page.url` 再判一次；越界时控制端保留失败操作，
  且**不**把该页内容当作可用证据；
- 页面正文与截图落成**不可变工件**，模型上下文里只放摘要与 `artifact_id`；
- 身份隔离：每个 `identity_id` 一个浏览器上下文，互不影响。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.enums import (
    ErrorCode,
    ExecutionCertainty,
    OperationStatus,
    ToolEffectClass,
)
from agent_workbench.contracts.tool import ArtifactRef, ToolError, ToolResult
from agent_workbench.domain.scope import ScopeViolation, TargetMappingRequired
from agent_workbench.execution.client import ExecutorRejected
from agent_workbench.tools.remote import (
    download_result_json,
    json_text,
    prepare_operation,
    record_result,
    record_submitted,
    rejection_result,
    scope_payload,
    store_remote_artifact,
    transport_unknown_result,
    wait_or_accept,
)
from agent_workbench.tools.runtime import Toolbox, execution_identity

_ACCEPT_NOTE = (
    "浏览器动作已受理但尚未结束（超过交互等待阈值）。稍后用 get_job 查询状态；"
    "**不要**重复提交同一个操作。"
)

#: 各动作要同步回来的工件（名字相同则按此媒体类型落库）
_ARTIFACT_MEDIA = {
    "page.txt": "text/plain; charset=utf-8",
    "screenshot.png": "image/png",
}


async def run_browser_action(
    toolbox: Toolbox,
    *,
    tool_call_id: str,
    tool_name: str,
    action: str,
    **fields: Any,
) -> ToolResult:
    await toolbox.assert_control_current()

    identity_label = str(fields.pop("identity_id", "") or "anonymous")
    identity_id = execution_identity(toolbox, identity_label)

    # ---- 派发前的控制端范围判定（navigate 才需要，其它动作沿用当前页面） ----
    url = fields.get("url")
    if action in {"navigate", "login"}:
        try:
            # 控制端先判一次；执行岛逐请求复核所有子资源。
            for target in (url, fields.get("login_url"), fields.get("probe_url")):
                if target:
                    toolbox.scope.check_execution_url(str(target))
        except TargetMappingRequired as exc:
            return ToolResult(
                operation_id=UUID(int=0),
                status=OperationStatus.FAILED,
                summary=str(exc),
                data={"error_category": "target_mapping", "delivery_state": "not_applied"},
                error=ToolError(
                    code=str(ErrorCode.VALIDATION_FAILED),
                    message=str(exc),
                    retryable=False,
                    recovery_hint="先明确目标所在机器及执行岛可访问的地址；不要据此要求用户启动服务。",
                    details={"target": exc.target, "connection_origin": "executor"},
                ),
                execution_certainty=ExecutionCertainty.NOT_APPLIED,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
        except ScopeViolation as exc:
            # 越界：**不派发**，错误和执行历史保留实际拒绝原因
            return ToolResult(
                operation_id=UUID(int=0),
                status=OperationStatus.FAILED,
                summary=f"目标不在声明的测试范围内，未发起导航：{exc}",
                error=ToolError(
                    code=str(ErrorCode.SCOPE_DENIED),
                    message=str(exc),
                    retryable=False,
                    recovery_hint="这是刻意的边界，不是故障；请改用范围内目标，或先由用户扩展范围。",
                ),
                execution_certainty=ExecutionCertainty.NOT_APPLIED,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )

    # 浏览器写操作（点击/填写/提交/下载）属于副作用，按 session 串行，避免互相踩
    effect_class = (
        ToolEffectClass.READ_ONLY
        if action in {"snapshot", "text", "screenshot"}
        else ToolEffectClass.SIDE_EFFECTING
    )
    lock_keys = [f"browser:{identity_id}"] if effect_class is ToolEffectClass.SIDE_EFFECTING else []

    async with toolbox.locks.acquire(lock_keys, timeout=120.0):
        arguments = {"action": action, "identity_id": identity_id, **fields}
        operation_id, _replayed = await prepare_operation(
            toolbox, tool_call_id, tool_name, arguments, effect_class=effect_class
        )
        operation = {
            "operation_id": str(operation_id),
            "kind": "browser",
            "request_digest": _digest(arguments),
            "scope_revision": toolbox.scope_revision,
            "owner_generation": toolbox.owner_generation,
            "scope": scope_payload(toolbox),
            "payload": arguments,
        }
        try:
            receipt = await toolbox.executor.submit(operation)
        except ExecutorRejected as exc:
            await record_result(
                toolbox,
                operation_id=operation_id,
                job_id=None,
                status=str(OperationStatus.FAILED),
                failure=f"被执行岛拒绝：{exc}",
            )
            return rejection_result(operation_id, exc)
        except Exception as exc:
            return transport_unknown_result(operation_id, exc)

        await record_submitted(
            toolbox, operation_id, receipt.job_id, accepted_at=receipt.accepted_at
        )
        accepted = await wait_or_accept(
            toolbox, operation_id, receipt.job_id, accept_note=_ACCEPT_NOTE
        )
        if accepted is not None:
            return accepted

    snapshot = await toolbox.executor.get_job(receipt.job_id)
    payload = await download_result_json(toolbox, receipt.job_id)
    state = str(snapshot.state)

    if state != "succeeded" or payload is None or not payload.get("ok"):
        error = (payload or {}).get("error") or {}
        code = error.get("code") or (
            snapshot.error.code if snapshot.error else str(ErrorCode.INTERNAL_ERROR)
        )
        message = error.get("message") or (
            snapshot.error.message if snapshot.error else "浏览器动作未成功"
        )
        await record_result(
            toolbox,
            operation_id=operation_id,
            job_id=receipt.job_id,
            status=str(OperationStatus.FAILED),
            snapshot=snapshot,
        )
        return ToolResult(
            operation_id=operation_id,
            job_id=receipt.job_id,
            status=OperationStatus.FAILED,
            summary=f"浏览器动作未成功（{code}）：{message}",
            data={"error_code": code, "details": error.get("details") or {}},
            error=ToolError(
                code=str(code),
                message=str(message),
                retryable=False,
                recovery_hint=(
                    "范围越界是刻意边界；元素找不到请先重新 snapshot 拿新句柄。"
                    if code in {"scope_violation", "target_not_found"}
                    else "这是执行岛侧的确定失败，重试同样会失败。"
                ),
            ),
            execution_certainty=ExecutionCertainty.NOT_APPLIED,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )

    result: dict[str, Any] = payload["result"]
    if result.get("final_url_out_of_scope"):
        # 发生范围外跳转：不下载正文/截图，也不挂任何可用证据引用。
        await record_result(
            toolbox,
            operation_id=operation_id,
            job_id=receipt.job_id,
            status=str(OperationStatus.FAILED),
            snapshot=snapshot,
        )
        return ToolResult(
            operation_id=operation_id,
            job_id=receipt.job_id,
            status=OperationStatus.FAILED,
            summary=(
                f"动作已执行，但页面最终跳转到范围之外的地址（{result.get('final_url')}）；"
                "已记录覆盖，未同步该页内容或截图。"
            ),
            data=_model_view(result),
            error=ToolError(code=str(ErrorCode.SCOPE_DENIED), message="页面最终 URL 超出声明范围"),
            execution_certainty=ExecutionCertainty.CONFIRMED,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )

    refs = await _sync_artifacts(toolbox, operation_id, receipt.job_id, result)
    await record_result(
        toolbox,
        operation_id=operation_id,
        job_id=receipt.job_id,
        status=str(OperationStatus.SUCCEEDED),
        refs=refs,
        snapshot=snapshot,
    )
    return ToolResult(
        operation_id=operation_id,
        job_id=receipt.job_id,
        status=OperationStatus.SUCCEEDED,
        summary=_summarize(action, result),
        data=_model_view(result),
        artifact_refs=refs,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )


def _model_view(result: dict[str, Any]) -> dict[str, Any]:
    """给模型的视图：**不含**长正文（正文在工件里），只留导航与交互所需信息。"""
    view = {
        key: value
        for key, value in result.items()
        if key
        in {
            "action",
            "final_url",
            "title",
            "status",
            "identity_id",
            "navigated",
            "submitted",
            "selector",
            "key",
            "interactive",
            "interactive_count",
            "text_chars",
            "screenshot_bytes",
            "screenshot_sha256",
            "filename",
            "byte_size",
            "sha256",
            "source_url",
            "blocked_request_count",
            "final_url_out_of_scope",
            "out_of_scope_reason",
            "artifact_manifest",
        }
    }
    blocked = result.get("blocked_requests") or []
    if blocked:
        view["blocked_requests"] = blocked[:10]
    return view


def _summarize(action: str, result: dict[str, Any]) -> str:
    title = result.get("title") or ""
    url = result.get("final_url") or ""
    head = f"{action}: {title or '（无标题）'} @ {url}"
    if action == "snapshot":
        return f"{head}；可交互元素 {result.get('interactive_count', 0)} 个，正文 {result.get('text_chars', 0)} 字"
    if action == "screenshot":
        return f"{head}；截图 {result.get('screenshot_bytes', 0)} 字节"
    if action == "download":
        return f"{head}；已下载 {result.get('filename')}（{result.get('byte_size', 0)} 字节）"
    blocked = result.get("blocked_request_count") or 0
    return f"{head}" + (f"；已阻断 {blocked} 个范围外请求" if blocked else "")


def _identity_failure(message: str) -> str:
    return json_text(
        ToolResult(
            operation_id=UUID(int=0),
            status=OperationStatus.FAILED,
            summary=message,
            error=ToolError(code=str(ErrorCode.VALIDATION_FAILED), message=message),
            execution_certainty=ExecutionCertainty.NOT_APPLIED,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    )


def _digest(arguments: dict[str, Any]) -> str:
    from agent_workbench.persistence.repositories import compute_request_digest

    return compute_request_digest({"tool": "browser", "arguments": arguments})


async def _sync_artifacts(
    toolbox: Toolbox, operation_id: UUID, job_id: UUID, result: dict[str, Any]
) -> list[ArtifactRef]:
    """把执行岛上的截图/正文/下载同步为不可变工件。

    哈希在**两侧**各自计算并比对（执行岛给 manifest，控制端同步后再算一次）：
    不一致就标 `orphaned`，不把可能被截断或替换的内容当作可用证据。
    """
    manifest = result.get("artifact_manifest") or {}
    if not manifest:
        return []
    refs: list[ArtifactRef] = []
    identity = str(result.get("identity_id") or "")
    source_url = str(result.get("final_url") or "")
    for name, meta in manifest.items():
        media_type = _ARTIFACT_MEDIA.get(
            name,
            "application/octet-stream"
            if "." in name and not name.endswith(".txt")
            else "text/plain; charset=utf-8",
        )
        refs.append(
            await store_remote_artifact(
                toolbox,
                name=name,
                job_id=job_id,
                operation_id=operation_id,
                media_type=media_type,
                identity=identity,
                source_url=source_url,
                expected_sha256=str((meta or {}).get("sha256") or ""),
            )
        )
    return refs


# --------------------------------------------------------------------------- #
# 对外工具
# --------------------------------------------------------------------------- #


@tool("list_test_identities")
async def list_test_identities(runtime: ToolRuntime[RunContext]) -> str:
    """列出当前项目的测试身份 ID、名称与角色；不返回 secret_ref 或凭据。"""
    from sqlalchemy import select

    from agent_workbench.persistence.tables import IdentityRow, SessionRow

    toolbox = runtime.context.toolbox
    async with toolbox.db.session() as db:
        session_row = (
            await db.execute(select(SessionRow).where(SessionRow.id == toolbox.session_id))
        ).scalar_one()
        identities = (
            (
                await db.execute(
                    select(IdentityRow).where(IdentityRow.project_id == session_row.project_id)
                )
            )
            .scalars()
            .all()
        )
    return json_text(
        ToolResult(
            operation_id=UUID(int=0),
            status=OperationStatus.SUCCEEDED,
            summary=f"当前项目有 {len(identities)} 个测试身份",
            data={
                "identities": [
                    {
                        "identity_id": str(row.id),
                        "display_name": row.display_name,
                        "role_label": row.role_label,
                        "active": row.id == toolbox.identity_id,
                        "credential_available": bool(row.secret_ref),
                    }
                    for row in identities
                ]
            },
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
    )


@tool("browser_login")
async def browser_login(
    runtime: ToolRuntime[RunContext],
    identity_id: str,
    login_url: str,
    username_selector: str,
    password_selector: str,
    submit_selector: str,
    logged_in_selector: str,
    probe_url: str = "",
) -> str:
    """用项目身份的 secret_ref 登录，并用选择器验证实际认证状态。

    凭据在控制端解析并直接交给执行岛；模型输入、工具返回与工件不包含密码。
    secret_ref 应指向含 username/password 字段的 UTF-8 JSON 对象。
    """
    from sqlalchemy import select

    from agent_workbench.config.secrets import SecretNotFoundError, build_secret_store
    from agent_workbench.persistence.tables import IdentityRow, SessionRow

    toolbox = runtime.context.toolbox
    try:
        target_id = UUID(identity_id)
    except ValueError:
        return _identity_failure("identity_id 必须是 list_test_identities 返回的 UUID")
    async with toolbox.db.session() as db:
        session_row = (
            await db.execute(select(SessionRow).where(SessionRow.id == toolbox.session_id))
        ).scalar_one()
        identity = (
            await db.execute(
                select(IdentityRow).where(
                    IdentityRow.id == target_id,
                    IdentityRow.project_id == session_row.project_id,
                )
            )
        ).scalar_one_or_none()
    if identity is None or not identity.secret_ref:
        return _identity_failure("该身份不属于当前项目，或没有配置 secret_ref")
    try:
        secret = json.loads(build_secret_store().resolve(identity.secret_ref))
    except (SecretNotFoundError, json.JSONDecodeError):
        return _identity_failure("测试账号 secret_ref 无法解析为凭据 JSON")
    if (
        not isinstance(secret, dict)
        or not isinstance(secret.get("username"), str)
        or not isinstance(secret.get("password"), str)
    ):
        return _identity_failure("测试账号凭据需要 username/password 字符串字段")
    result = await run_browser_action(
        toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="browser_login",
        action="login",
        identity_id=identity_id,
        login_url=login_url,
        username=secret["username"],
        password=secret["password"],
        username_selector=username_selector,
        password_selector=password_selector,
        submit_selector=submit_selector,
        logged_in_selector=logged_in_selector,
        probe_url=probe_url or None,
    )
    return json_text(result)


@tool("browser_navigate")
async def browser_navigate(
    runtime: ToolRuntime[RunContext], url: str, identity_id: str = ""
) -> str:
    """在无头浏览器中打开一个 URL。

    Args:
        url: 目标地址。必须在声明的测试范围内，否则**不会发起请求**。
        identity_id: 可选项目身份 ID；留空使用本 session 当前身份。

    Returns:
        ToolResult 的 JSON。`data.final_url` 是**重定向后的最终地址**；
        若它超出范围，会记一条覆盖且该页内容不作为证据。
    """
    result = await run_browser_action(
        runtime.context.toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="browser_navigate",
        action="navigate",
        url=url,
        identity_id=identity_id,
    )
    return json_text(result)


@tool("browser_snapshot")
async def browser_snapshot(
    runtime: ToolRuntime[RunContext], max_elements: int = 200, identity_id: str = ""
) -> str:
    """获取当前页面的结构化快照：可交互元素 + 正文（正文本体落工件）。

    Args:
        max_elements: 最多返回多少个可交互元素。
        identity_id: 可选项目身份 ID；留空使用本 session 当前身份。

    Returns:
        ToolResult 的 JSON。`data.interactive[]` 每项带 `selector`，
        **直接把它传给 browser_click / browser_fill 即可**，不需要自己猜 CSS。
    """
    result = await run_browser_action(
        runtime.context.toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="browser_snapshot",
        action="snapshot",
        max_elements=max_elements,
        identity_id=identity_id,
    )
    return json_text(result)


@tool("browser_text")
async def browser_text(
    runtime: ToolRuntime[RunContext], selector: str = "body", identity_id: str = ""
) -> str:
    """读取页面（或某个元素）的可见文本；正文落工件，上下文里只给摘要与引用。

    Args:
        selector: 要读取的元素选择器，默认整页正文。
        identity_id: 可选项目身份 ID。
    """
    result = await run_browser_action(
        runtime.context.toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="browser_text",
        action="text",
        selector=selector,
        identity_id=identity_id,
    )
    return json_text(result)


@tool("browser_click")
async def browser_click(
    runtime: ToolRuntime[RunContext], selector: str = "", text: str = "", identity_id: str = ""
) -> str:
    """点击页面元素。

    Args:
        selector: 元素选择器，直接使用 browser_snapshot 返回的 `selector`。
        text: 也可以按可见文本精确定位（selector 为空时生效）。
        identity_id: 可选项目身份 ID。
    """
    result = await run_browser_action(
        runtime.context.toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="browser_click",
        action="click",
        selector=selector or None,
        text=text or None,
        identity_id=identity_id,
    )
    return json_text(result)


@tool("browser_fill")
async def browser_fill(
    runtime: ToolRuntime[RunContext],
    selector: str,
    value: str,
    submit: bool = False,
    identity_id: str = "",
) -> str:
    """在输入框中填写内容（可选回车提交）。

    Args:
        selector: 输入框选择器（用 snapshot 返回的 `selector`）。
        value: 要填的值。
        submit: 填完后是否按回车提交。
        identity_id: 可选项目身份 ID。
    """
    result = await run_browser_action(
        runtime.context.toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="browser_fill",
        action="fill",
        selector=selector,
        value=value,
        submit=submit,
        identity_id=identity_id,
    )
    return json_text(result)


@tool("browser_press")
async def browser_press(
    runtime: ToolRuntime[RunContext], key: str, selector: str = "", identity_id: str = ""
) -> str:
    """按键。

    Args:
        key: 键名，例如 Enter / Escape / Tab / ArrowDown。
        selector: 可选；给定时对该元素按键，否则对页面按键。
        identity_id: 可选项目身份 ID。
    """
    result = await run_browser_action(
        runtime.context.toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="browser_press",
        action="press",
        key=key,
        selector=selector or None,
        identity_id=identity_id,
    )
    return json_text(result)


@tool("browser_screenshot")
async def browser_screenshot(
    runtime: ToolRuntime[RunContext], full_page: bool = False, identity_id: str = ""
) -> str:
    """截屏并落成工件。

    Args:
        full_page: 是否整页截图（默认只截可视区域）。
        identity_id: 可选项目身份 ID。
    """
    result = await run_browser_action(
        runtime.context.toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="browser_screenshot",
        action="screenshot",
        full_page=full_page,
        identity_id=identity_id,
    )
    return json_text(result)


@tool("browser_download")
async def browser_download(
    runtime: ToolRuntime[RunContext], selector: str, identity_id: str = ""
) -> str:
    """点击一个会触发下载的元素，并把文件落成工件。

    Args:
        selector: 触发下载的元素选择器（用 snapshot 返回的 `selector`）。
        identity_id: 可选项目身份 ID。

    Returns:
        ToolResult 的 JSON，含文件名、大小与 sha256。
    """
    result = await run_browser_action(
        runtime.context.toolbox,
        tool_call_id=runtime.tool_call_id or "",
        tool_name="browser_download",
        action="download",
        selector=selector,
        identity_id=identity_id,
    )
    return json_text(result)


BROWSER_TOOLS = [
    list_test_identities,
    browser_login,
    browser_navigate,
    browser_snapshot,
    browser_text,
    browser_click,
    browser_fill,
    browser_press,
    browser_screenshot,
    browser_download,
]
