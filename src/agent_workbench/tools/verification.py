"""仅开放可确定性复核的窄场景，不接受模型直接写 confirmed。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any
from uuid import UUID

from langchain.tools import ToolRuntime
from langchain_core.tools import InjectedToolCallId, tool

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.enums import OperationStatus
from agent_workbench.contracts.errors import ApiError
from agent_workbench.domain.verifiers import (
    confirm_public_git_head,
    preflight_public_git_head,
    validate_public_git_head_url,
)
from agent_workbench.observability.logging import get_logger
from agent_workbench.observability.spans import Span, SpanRecorder
from agent_workbench.tools.http import HttpRequestArgs, run_http_request
from agent_workbench.tools.runtime import Toolbox

log = get_logger("agent_workbench.verification")


def _response(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def _verification_stage(
    spans: SpanRecorder,
    name: str,
    toolbox: Toolbox,
) -> Iterator[Span]:
    span = spans.start(
        name,
        session_id=toolbox.session_id,
        run_id=toolbox.run_id,
    )
    try:
        yield span
    except (Exception, asyncio.CancelledError) as exc:
        span.finish(error=type(exc).__name__)
        spans.record(span, status="failed")
        raise
    else:
        span.finish()
        spans.record(span, status="completed")


@tool("verify_public_git_head")
async def verify_public_git_head(
    runtime: ToolRuntime[RunContext],
    tool_call_id: Annotated[str, InjectedToolCallId],
    finding_id: str,
    url: str,
    initial_body_artifact_id: str,
    initial_meta_artifact_id: str,
) -> str:
    """独立复核候选发现：目标的 `/.git/HEAD` 是否公开返回 Git 分支引用。

    Args:
        finding_id: 信息泄露类候选发现 ID；初次 HTTP 响应工件必须已绑定该发现。
        url: 初次观察的完整 `http(s)://目标/.git/HEAD` 地址。
        initial_body_artifact_id: 初次 `http_request` 返回的 `response.body` 工件 ID。
        initial_meta_artifact_id: 同次请求返回的 `response.meta.json` 工件 ID。

    Returns:
        只确认“Git HEAD 分支引用公开可读”；不推断仓库对象或源码可读。

    Notes:
        - 本工具会使用隔离的匿名会话重新发起一次 GET，请勿把普通 200 当作结果；
        - 初次观察和复测必须是不同、成功且证据完整的 HTTP 操作；
        - 复测受阻、超时或证据不匹配时保持 candidate，不产生阴性结论。
    """
    toolbox: Toolbox = runtime.context.toolbox
    spans = SpanRecorder()
    try:
        return await _verify_public_git_head_workflow(
            runtime,
            toolbox,
            spans,
            tool_call_id=tool_call_id,
            finding_id=finding_id,
            url=url,
            initial_body_artifact_id=initial_body_artifact_id,
            initial_meta_artifact_id=initial_meta_artifact_id,
        )
    finally:
        try:
            async with toolbox.db.transaction() as db:
                await spans.flush(db)
        except Exception as exc:
            log.warning(
                "verification_span_persist_failed",
                session_id=str(toolbox.session_id),
                error_type=type(exc).__name__,
            )


async def _verify_public_git_head_workflow(
    runtime: ToolRuntime[RunContext],
    toolbox: Toolbox,
    spans: SpanRecorder,
    *,
    tool_call_id: str,
    finding_id: str,
    url: str,
    initial_body_artifact_id: str,
    initial_meta_artifact_id: str,
) -> str:
    try:
        finding_uuid = UUID(finding_id)
        initial_body_uuid = UUID(initial_body_artifact_id)
        initial_meta_uuid = UUID(initial_meta_artifact_id)
    except ValueError:
        return _response(verified=False, reason="发现或工件 ID 不是合法 UUID")

    try:
        with _verification_stage(spans, "verification.git_head.preflight", toolbox):
            validate_public_git_head_url(url)
            async with toolbox.db.session() as db:
                await preflight_public_git_head(
                    db,
                    toolbox.artifacts,
                    session_id=toolbox.session_id,
                    finding_id=finding_uuid,
                    url=url,
                    initial_body_id=initial_body_uuid,
                    initial_meta_id=initial_meta_uuid,
                )
    except ApiError as exc:
        return _response(verified=False, reason=exc.message, code=str(exc.code))

    # 调用 ID 固定后，崩溃重放复用同一远端 operation；新工具调用才是显式重测。
    digest = hashlib.sha256(tool_call_id.encode("utf-8")).hexdigest()[:32]
    recheck_identity = f"verify-git-head-{digest}"
    with _verification_stage(spans, "verification.git_head.recheck_dispatch", toolbox) as span:
        recheck = await run_http_request(
            HttpRequestArgs(
                url=url,
                method="GET",
                identity=recheck_identity,
                follow_redirects=False,
                save_body_artifact=True,
            ),
            toolbox,
            f"verify-git-head:{digest}",
        )
        span.operation_id = recheck.operation_id
    if recheck.status is not OperationStatus.SUCCEEDED:
        return _response(
            verified=False,
            reason="独立复测未成功完成，候选状态不变",
            recheck_status=str(recheck.status),
            recheck_job_id=str(recheck.job_id) if recheck.job_id else None,
            operation_id=str(recheck.operation_id),
        )

    refs = {
        "body": next(
            (
                ref.artifact_id
                for ref in recheck.artifact_refs
                if (ref.summary or "").startswith("response.body（")
            ),
            None,
        ),
        "meta": next(
            (
                ref.artifact_id
                for ref in recheck.artifact_refs
                if (ref.summary or "").startswith("response.meta.json（")
            ),
            None,
        ),
    }
    if refs["body"] is None or refs["meta"] is None:
        return _response(verified=False, reason="复测工件未完整同步，候选状态不变")

    try:
        with _verification_stage(spans, "verification.git_head.confirm", toolbox):
            async with toolbox.db.transaction() as db:
                verification_id = await confirm_public_git_head(
                    db,
                    toolbox.artifacts,
                    session_id=toolbox.session_id,
                    finding_id=finding_uuid,
                    url=url,
                    initial_body_id=initial_body_uuid,
                    initial_meta_id=initial_meta_uuid,
                    recheck_body_id=refs["body"],
                    recheck_meta_id=refs["meta"],
                    recheck_identity=recheck_identity,
                )
    except ApiError as exc:
        return _response(verified=False, reason=exc.message, code=str(exc.code))
    runtime.context.note_progress(f"verification:{verification_id}")
    return _response(
        verified=True,
        finding_id=finding_id,
        verification_id=str(verification_id),
        scope="仅确认 Git HEAD 分支引用公开可读；未确认仓库对象或源代码可读",
        recheck_operation_id=str(recheck.operation_id),
    )


VERIFICATION_TOOLS = [verify_public_git_head]
