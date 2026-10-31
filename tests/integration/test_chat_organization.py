"""隔离 PostgreSQL 验证聊天归类、独立性与定向物理删除；无模型或靶场请求。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.store.memory import InMemoryStore
from sqlalchemy import select, text

from agent_workbench.api.app import create_app
from agent_workbench.config.settings import get_settings
from agent_workbench.domain.run_state import create_run
from agent_workbench.memory.store import MemoryStore
from agent_workbench.persistence.tables import (
    ArtifactRow,
    EventRow,
    IdentityRow,
    ModelCallRow,
    RunRow,
    SessionGraphRow,
    SessionRow,
    ToolJobRow,
    ToolOperationRow,
)

pytestmark = pytest.mark.integration


def draft(client, headers, project=None, title="独立对话"):
    path = f"/api/projects/{project}/sessions" if project else "/api/sessions"
    response = client.post(path, headers=headers, json={
        "project_id": project, "title": title, "initial_objective": "", "draft": True,
        "identities": [{"display_name": "同名账号"}],
    })
    assert response.status_code == 201, response.text
    return response.json()["session"]


async def test_standalone_grouping_rename_and_ownership(database, unique_name):
    with TestClient(create_app()) as client:
        headers = {"X-Workbench-CSRF": client.post("/api/session").json()["csrf_token"]}
        project = client.post("/api/projects", headers=headers,
                              json={"name": unique_name, "target_description": "仅归类"}).json()
        ungrouped = draft(client, headers)
        first = draft(client, headers, project["id"], "第一个")
        second = draft(client, headers, project["id"], "第二个")
        assert ungrouped["project_id"] is None
        assert ungrouped["id"] in {s["id"] for s in client.get("/api/sessions").json()["sessions"]}
        grouped = client.get(f"/api/projects/{project['id']}/sessions").json()["sessions"]
        assert {first["id"], second["id"]} <= {s["id"] for s in grouped}
        assert ungrouped["id"] not in {s["id"] for s in grouped}
        assert client.patch(f"/api/sessions/{first['id']}", headers=headers,
                            json={"name": "新名称"}).status_code == 200
        assert client.patch(f"/api/projects/{project['id']}", headers=headers,
                            json={"name": unique_name + "改名"}).status_code == 200
        assert client.patch(f"/api/sessions/{first['id']}", headers=headers,
                            json={"name": "  "}).status_code == 422
        assert client.patch(f"/api/sessions/{first['id']}",
                            json={"name": "无 CSRF"}).status_code == 403
        async with database.transaction() as db:
            row = await db.get(SessionRow, UUID(second["id"]))
            row.owner_id = "another-owner"
        assert client.patch(f"/api/sessions/{second['id']}", headers=headers,
                            json={"name": "越权"}).status_code == 404
        assert client.delete(f"/api/sessions/{second['id']}", headers=headers).status_code == 404


async def test_delete_removes_only_selected_chat_and_checkpoint(database):
    settings = get_settings()
    with TestClient(create_app()) as client:
        headers = {"X-Workbench-CSRF": client.post("/api/session").json()["csrf_token"]}
        target = draft(client, headers)
        survivor = draft(client, headers)
        sid = UUID(target["id"])
        async with database.transaction() as db:
            row = await db.get(SessionRow, sid)
            thread = row.thread_id
            run = await create_run(db, session_id=sid, objective="fixture",
                                   provider_id="xiaomi", model_name="mimo-v2.6-pro", config_snapshot={})
            run.status = "paused"
            run_id = run.id
            artifact = ArtifactRow(session_id=sid, sha256="a" * 64, byte_size=1,
                                   media_type="text/plain", relative_key="aa/fixture",
                                   captured_at=datetime.now(UTC))
            shared_bytes = ArtifactRow(session_id=UUID(survivor["id"]), sha256="a" * 64,
                                       byte_size=1, media_type="text/plain", relative_key="aa/fixture",
                                       captured_at=datetime.now(UTC))
            db.add_all([artifact, shared_bytes])
            await db.flush()
            survivor_artifact = shared_bytes.id
            call = ModelCallRow(model_call_id=uuid4(), logical_call_id=uuid4(), run_id=run_id,
                                session_id=sid, phase="agent", provider_id="xiaomi", model_name="fixture",
                                response_artifact_id=artifact.id)
            op = ToolOperationRow(operation_id=uuid4(), run_id=run_id, session_id=sid,
                                  tool_call_id="fixture", tool_name="http_request", request_digest="a"*64,
                                  effect_class="read_only", status="succeeded")
            db.add_all([call, op, SessionGraphRow(session_id=sid), EventRow(
                event_id=uuid4(), session_id=sid, run_id=run_id, session_seq=1,
                type="run.status", occurred_at=datetime.now(UTC), payload={})])
            await db.flush()
            db.add(ToolJobRow(job_id=uuid4(), operation_id=op.operation_id,
                             executor_id="fixture", state="succeeded"))
            await db.execute(text(f'INSERT INTO "{settings.checkpoint_schema}".checkpoints '
                                  '(thread_id,checkpoint_ns,checkpoint_id,type,checkpoint,metadata) '
                                  "VALUES (:t,'','fixture','json','{}','{}')"), {"t": thread})
            await db.execute(text(f'INSERT INTO "{settings.memory_schema}".store(prefix,key,value) '
                                  "VALUES (:p,'fixture','{}')"),
                             {"p": f"{settings.owner_id}:session:{sid}.methods"})
            await db.execute(text(f'INSERT INTO "{settings.app_schema}".checkpoint_heads '
                                  '(thread_id,checkpoint_ns,accepted_checkpoint_id,writer_epoch,'
                                  'owner_generation,control_version,accepted_at) '
                                  "VALUES (:t,'','fixture',1,1,0,now())"), {"t": thread})
            await db.execute(text(f'INSERT INTO "{settings.app_schema}".checkpoint_write_receipts '
                                  '(receipt_id,thread_id,checkpoint_ns,checkpoint_id,task_id,write_key,'
                                  'content_hash,writer_epoch,owner_generation,control_version) '
                                  "VALUES (:id,:t,'','fixture','task','v2:slot:0',:hash,1,1,0)"),
                             {"id": uuid4(), "t": thread, "hash": "a"*64})
        deleted = client.delete(f"/api/sessions/{sid}", headers=headers)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted"] is True
        async with database.session() as db:
            assert await db.get(SessionRow, sid) is None
            assert await db.get(RunRow, run_id) is None
            assert await db.get(SessionRow, UUID(survivor["id"])) is not None
            assert await db.get(ArtifactRow, survivor_artifact) is not None
            for model in (ArtifactRow, EventRow, IdentityRow, ModelCallRow, SessionGraphRow, ToolOperationRow):
                assert not (await db.scalars(select(model).where(model.session_id == sid))).all()
            assert await db.scalar(select(ToolJobRow.job_id).where(
                ToolJobRow.operation_id == op.operation_id)) is None
            assert not (await db.execute(text(f'SELECT 1 FROM "{settings.checkpoint_schema}".checkpoints '
                                              'WHERE thread_id=:t'), {"t": thread})).first()
            for table in ("checkpoint_heads", "checkpoint_write_receipts"):
                assert not (await db.execute(text(f'SELECT 1 FROM "{settings.app_schema}".{table} '
                                                  'WHERE thread_id=:t'), {"t": thread})).first()
        assert client.get(f"/api/sessions/{sid}/snapshot").status_code == 404
        assert client.get(f"/api/sessions/{survivor['id']}/snapshot").status_code == 200


async def test_delete_blocks_queued_run_without_losing_records(database):
    with TestClient(create_app()) as client:
        headers = {"X-Workbench-CSRF": client.post("/api/session").json()["csrf_token"]}
        target = draft(client, headers)
        async with database.transaction() as db:
            await create_run(db, session_id=UUID(target["id"]), objective="fixture",
                             provider_id="xiaomi", model_name="mimo-v2.6-pro", config_snapshot={})
        response = client.delete(f"/api/sessions/{target['id']}", headers=headers)
        assert response.status_code == 409
        async with database.session() as db:
            assert await db.get(SessionRow, UUID(target["id"])) is not None
            assert await db.scalar(select(RunRow.id).where(RunRow.session_id == UUID(target["id"])))


async def test_memory_does_not_cross_chats_even_in_one_project():
    store = InMemoryStore()
    settings = get_settings()
    project = uuid4()
    a = MemoryStore(store, settings, session_id=uuid4())
    b = MemoryStore(store, settings, session_id=uuid4())
    await a.propose(project_id=project, memory_type="observed_endpoint", content="仅第一对话可见",
                    subject_key="fixture")
    assert len(await a.search(project_id=project, query="", include_candidates=True)) == 1
    assert await b.search(project_id=project, query="", include_candidates=True) == []
