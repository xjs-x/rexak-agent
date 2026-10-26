"""主机 Docker 的 localhost 不得被默认为 Kali 上的目标。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import SystemMessage

from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
from agent_workbench.contracts.enums import ExecutionCertainty, OperationStatus
from agent_workbench.contracts.identity import ScopeRule, ScopeSnapshot
from agent_workbench.domain.scope import ScopePolicy, TargetMappingRequired
from agent_workbench.tools.browser import run_browser_action
from agent_workbench.tools.http import HttpRequestArgs, run_http_request
from agent_workbench.tools.runtime import Toolbox


def policy(host: str = "localhost", **values: Any) -> ScopePolicy:
    return ScopePolicy(
        ScopeSnapshot(
            mode="instruction",
            rules=[ScopeRule(host=host)],
            **values,
        )
    )


@pytest.mark.parametrize("host", ["localhost", "localhost.", "127.0.0.1", "127.0.0.2", "[::1]"])
def test_instruction_loopback_requires_machine_mapping(host: str) -> None:
    scope = policy(host.strip("[]"))
    with pytest.raises(TargetMappingRequired, match="未连接目标"):
        scope.check_execution_url(f"http://{host}:8080/")


@pytest.mark.parametrize(
    "changes", [{}, {"from_port": "8081"}, {"scheme": "https"}, {"to_port": "bad"}, {"to_host": ""}]
)
def test_mapping_matches_exact_host_port_and_scheme(changes: dict[str, str]) -> None:
    mapping = {
        "from_host": "localhost",
        "from_port": "8080",
        "to_host": "host.test",
        "to_port": "8080",
        "scheme": "http",
        **changes,
    }
    scope = policy(target_mappings=[mapping])
    if changes:
        with pytest.raises(TargetMappingRequired):
            scope.check_execution_url("http://localhost:8080/")
    else:
        assert scope.check_execution_url("http://localhost:8080/").port == 8080


def test_remote_target_and_explicit_executor_scope_keep_working() -> None:
    assert policy("lab.test").check_execution_url("http://lab.test:8080/").host == "lab.test"
    scoped = ScopePolicy(
        ScopeSnapshot(rules=[ScopeRule(scheme="http", host="127.0.0.1", port=8080)])
    )
    assert scoped.check_execution_url("http://127.0.0.1:8080/").port == 8080


@pytest.mark.parametrize("csrf", [False, True])
async def test_http_mapping_failure_never_dispatches_or_creates_an_operation(csrf: bool) -> None:
    scope = policy("lab.test" if csrf else "localhost")
    if csrf:
        scope.snapshot.rules.append(ScopeRule(host="localhost"))
        scope = ScopePolicy(scope.snapshot)
    executor = SimpleNamespace(submit=AsyncMock())
    toolbox = cast(Toolbox, SimpleNamespace(scope=scope, executor=executor))
    args = HttpRequestArgs(
        url="http://lab.test/" if csrf else "http://localhost:8080/",
        csrf_source_url="http://localhost:8080/form" if csrf else None,
    )
    result = await run_http_request(args, toolbox, "local-target")
    assert result.status == OperationStatus.FAILED
    assert result.execution_certainty == ExecutionCertainty.NOT_APPLIED
    assert result.operation_id.int == 0 and result.job_id is None
    assert result.data["error_category"] == "target_mapping"
    assert result.data["delivery_state"] == "not_applied"
    assert result.error and result.error.retryable is False
    assert "用户电脑" in result.summary
    executor.submit.assert_not_called()


@pytest.mark.parametrize(
    ("action", "fields"),
    [
        ("navigate", {"url": "http://localhost:8080/"}),
        ("login", {"login_url": "http://localhost:8080/login"}),
        ("login", {"probe_url": "http://localhost:8080/profile"}),
    ],
)
async def test_browser_mapping_failure_never_dispatches(
    action: str, fields: dict[str, str]
) -> None:
    executor = SimpleNamespace(submit=AsyncMock())
    toolbox = cast(
        Toolbox,
        SimpleNamespace(
            scope=policy(),
            executor=executor,
            assert_control_current=AsyncMock(),
            identity_id=None,
            session_id="test-session",
        ),
    )
    result = await run_browser_action(
        toolbox, tool_call_id="local-target", tool_name="browser_test", action=action, **fields
    )
    assert result.execution_certainty == ExecutionCertainty.NOT_APPLIED
    assert result.data["error_category"] == "target_mapping"
    assert result.error and result.error.retryable is False
    executor.submit.assert_not_called()


async def test_context_exposes_actual_mapping_and_connection_origin() -> None:
    mapping = {
        "from_host": "localhost",
        "from_port": "8080",
        "to_host": "host.test",
        "to_port": "8080",
    }
    toolbox = SimpleNamespace(scope=policy(target_mappings=[mapping]), working_context=None)
    request = SimpleNamespace(
        state={},
        runtime=SimpleNamespace(context=SimpleNamespace(toolbox=toolbox)),
        system_message=SystemMessage(content="base"),
        messages=[],
        tools=[],
        override=lambda **values: SimpleNamespace(**values),
    )
    handler = AsyncMock()
    await ContextBuilderMiddleware().awrap_model_call(cast(Any, request), handler)
    actual = str(handler.call_args.args[0].system_message.content)
    assert "执行岛" in actual and "host.test" in actual and "8080" in actual
    assert "不得把此条件写成目标未运行" in actual
    assert actual == toolbox.context_manifest["context_projection"] or actual.endswith(
        toolbox.context_manifest["context_projection"]
    )
