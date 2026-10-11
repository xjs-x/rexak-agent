"""同一项目身份在不同测试会话中不能共享浏览器/HTTP Cookie。"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from agent_workbench.tools.runtime import execution_identity


def test_execution_identity_is_session_scoped() -> None:
    identity_id = uuid4()
    first = SimpleNamespace(session_id=uuid4(), identity_id=identity_id)
    second = SimpleNamespace(session_id=uuid4(), identity_id=identity_id)

    first_key = execution_identity(first)  # type: ignore[arg-type]
    assert first_key == execution_identity(first, str(identity_id))  # type: ignore[arg-type]
    assert first_key != execution_identity(second)  # type: ignore[arg-type]
    assert first_key != execution_identity(second, str(identity_id))  # type: ignore[arg-type]
