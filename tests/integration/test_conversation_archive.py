"""对话正文归档与只读投影验收。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, SystemMessage
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.model_observer import ModelObserverMiddleware
from agent_workbench.api.app import create_app
from agent_workbench.config.settings import get_settings
from agent_workbench.contracts.enums import CommandKind
from agent_workbench.domain.run_state import create_run
from agent_workbench.persistence.repositories import (
    CommandRepository,
    ProjectRepository,
    SessionRepository,
)
from agent_workbench.persistence.tables import ArtifactRow, ModelCallRow

pytestmark = pytest.mark.integration


async def test_model_text_archived_and_conversation_is_scoped(database, unique_name) -> None:
    async with database.transaction() as db:
        project = await ProjectRepository(db).create(
            owner_id=get_settings().owner_id,
            name=unique_name,
            target_description="本地目标",
            default_scope={},
        )
        sessions = [
            await SessionRepository(db).create(
                project_id=project.id,
                title=f"测试会话 {index}",
                scope_snapshot={},
                policy_snapshot={},
            )
            for index in range(2)
        ]
        run = await create_run(
            db,
            session_id=sessions[0].id,
            objective="检查登录页 token=objective-secret",
            provider_id="test",
            model_name="scripted",
            config_snapshot={"created_from_session_create": True},
        )
        await CommandRepository(db).submit(
            session_id=sessions[0].id,
            run_id=run.id,
            kind=CommandKind.MESSAGE,
            content="继续检查 password=user-secret",
            idempotency_key=uuid4().hex,
        )

    toolbox = SimpleNamespace(
        db=database,
        run_id=run.id,
        session_id=sessions[0].id,
        provider_id="test",
        model_name="scripted",
        sdk_retries_disabled=True,
    )
    request = SimpleNamespace(
        runtime=SimpleNamespace(context=RunContext(toolbox=cast(Any, toolbox))),
        state={},
        system_message=SystemMessage(content="测试"),
        messages=[],
    )
    body = "工具检查完成。token=assistant-secret\n" + "细节。" * 500

    async def handler(_request: Any) -> Any:
        return SimpleNamespace(result=[AIMessage(content=body)])

    response = await ModelObserverMiddleware().awrap_model_call(cast(Any, request), handler)
    assert response.result[0].content == body
    async with database.session() as db:
        call = (await db.execute(select(ModelCallRow).where(ModelCallRow.run_id == run.id))).scalar_one()
        assert call.response_artifact_id is not None
        assert call.first_token_at is None, "无流式分片时不能伪造首个输出时间"
        artifact = (await db.execute(select(ArtifactRow).where(
            ArtifactRow.id == call.response_artifact_id
        ))).scalar_one()
        assert artifact.session_id == sessions[0].id
        assert artifact.byte_size == len(body.encode("utf-8"))

    first, second = (session.id for session in sessions)
    with TestClient(create_app()) as client:
        ticket = client.post("/api/session")
        assert ticket.status_code == 200
        page = client.get(f"/api/sessions/{first}/conversation?limit=2")
        assert page.status_code == 200, page.text
        data = page.json()
        assert len(data["entries"]) == 2
        assert data["next_cursor"] is not None
        assistant = next(item for item in data["entries"] if item["role"] == "assistant")
        assert assistant["has_more"] is True
        assert "assistant-secret" not in str(data)
        assert client.get(f"/api/sessions/{second}/conversation").json()["entries"] == []
        assert client.get(
            f"/api/sessions/{second}/conversation/assistant/{assistant['id']}/text"
        ).status_code == 404

        parts: list[str] = []
        offset: int | None = 0
        while offset is not None:
            detail = client.get(
                f"/api/sessions/{first}/conversation/assistant/{assistant['id']}/text",
                params={"offset": offset, "limit": 11},
            )
            assert detail.status_code == 200, detail.text
            payload = detail.json()
            parts.append(payload["text"])
            offset = payload["next_offset"]
        assert "assistant-secret" not in "".join(parts)
        assert "<redacted>" in "".join(parts)
        fresh = client.post(
            f"/api/sessions/{first}/messages",
            json={"content": "新补充指令"},
            headers={
                "X-Workbench-CSRF": ticket.json()["csrf_token"],
                "Idempotency-Key": uuid4().hex,
            },
        )
        assert fresh.status_code == 202, fresh.text
        older = client.get(
            f"/api/sessions/{first}/conversation",
            params={"limit": 2, "cursor": data["next_cursor"]},
        ).json()
        assert len(older["entries"]) == 1
        assert older["entries"][0]["source"] == "objective"
        assert "objective-secret" not in str(older)
        assert client.get(
            f"/api/sessions/{first}/conversation", params={"cursor": "not-a-cursor"}
        ).status_code == 422
