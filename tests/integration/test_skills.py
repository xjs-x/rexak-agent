"""真实隔离数据库、官方 Agent 主循环及管理 API 的 Skill 闭环。"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from pydantic import Field
from sqlalchemy import select

from agent_workbench.agent.context import RunContext
from agent_workbench.agent.middleware.context_builder import ContextBuilderMiddleware
from agent_workbench.agent.middleware.skills import SkillsMiddleware, prepare_skill_context
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.api.app import create_app
from agent_workbench.contracts.skill_package import encode_package
from agent_workbench.persistence.bootstrap import open_checkpointer_connection
from agent_workbench.persistence.tables import SessionRow, SkillVersionRow, ToolOperationRow
from agent_workbench.skills.service import SkillService
from agent_workbench.tools.skills import SKILL_TOOLS, load_skill, run_skill_script
from tests.integration.test_execution_tools import fake_toolbox
from tests.integration.test_instruction_scope import create_task
from tests.support.executor_fixtures import FakeExecutor
from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call
from tests.unit.test_skill_packages import sample

pytestmark = pytest.mark.integration


async def fixture(database, name):
    with TestClient(create_app()) as client:
        session_id, run_id, _headers = create_task(client, name)
    box = fake_toolbox(database, session_id, FakeExecutor(), run_id=run_id)
    box.skill_catalog = None
    box.skill_context = ""
    box.skill_manifest = []
    box.settings = database.settings
    return box


async def test_publish_version_snapshot_and_disabled_read(database, unique_name):
    service = SkillService(database)
    first = await service.publish(sample(), f"zip:{unique_name}")
    assert first.enabled is False
    same = await service.publish(sample(), f"zip:{unique_name}")
    assert first.version_id == same.version_id
    await service.update(first.skill_id, {"enabled": True})
    box = await fixture(database, unique_name)
    catalog = await service.catalog(box.run_id)
    newer = await service.publish(sample("新版正文"), f"zip:{unique_name}")
    assert newer.version_id != first.version_id
    assert (await service.catalog(box.run_id)) == catalog
    _card, old_files, _spec = await service.files(first.skill_id, first.version_id)
    assert "固定的技能指导" in old_files["SKILL.md"].decode()
    await service.update(first.skill_id, {"enabled": False})
    with pytest.raises(ValueError, match="skill_disabled"):
        await service.files(first.skill_id, first.version_id)
    _card, old_files, _spec = await service.files(first.skill_id, first.version_id, enforce_enabled=False)
    assert old_files


async def test_official_agent_loads_next_request_and_restores_after_restart(database, unique_name):
    class Captured(ScriptedChatModel):
        seen: list[str] = Field(default_factory=list)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.seen.append("\n".join(str(message.content) for message in messages))
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    box = await fixture(database, unique_name)
    card = await SkillService(database).publish(sample(), f"zip:{unique_name}")
    await SkillService(database).update(card.skill_id, {"enabled": True})
    model = Captured(responses=[tool_call("load_skill", {"skill_id": card.skill_id}, "load-1"), final_text("继续工作")])
    memory = InMemorySaver()
    def graph() -> Any:
        return create_agent(model=model, tools=SKILL_TOOLS, state_schema=AgentGraphState,
            context_schema=RunContext, middleware=[SkillsMiddleware(), ContextBuilderMiddleware()], checkpointer=memory)
    config = {"configurable": {"thread_id": str(box.session_id)}}
    context = RunContext(toolbox=box, thread_id=str(box.session_id))
    result = await graph().ainvoke({"messages": [HumanMessage(content="使用可用技能")], "active_skills": {}, "skill_directives": {}}, config, context=context)
    assert "固定的技能指导" not in model.seen[0]
    assert model.seen[1].count("固定的技能指导") == 1
    assert box.context_manifest["skills"][0]["package_hash"] == card.package_hash
    # 模拟原工具历史已被压缩；正文从 checkpoint 引用恢复，源目录更新不替换它。
    await SkillService(database).publish(sample("新版正文"), f"zip:{unique_name}")
    box.skill_catalog = None
    await graph().ainvoke({"messages": [HumanMessage(content="继续")], "active_skills": result["active_skills"]}, config, context=context)
    assert "固定的技能指导" in model.seen[-1] and "新版正文" not in model.seen[-1]


async def test_project_metadata_does_not_auto_activate_global_skill(database, unique_name):
    box = await fixture(database, unique_name)
    service = SkillService(database)
    card = await service.publish(sample(), f"zip:{unique_name}")
    await service.update(card.skill_id, {"enabled": True})
    async with database.session() as db:
        project = (await db.get(SessionRow, box.session_id)).project_id
    await service.update(card.skill_id, {"default_for_projects": [str(project)]})
    update = await prepare_skill_context({}, box)
    assert update is not None
    assert card.skill_id not in update["active_skills"]
    assert box.skill_manifest == []
    explicit = await prepare_skill_context({"messages": [HumanMessage(content="使用技能", id="explicit-1",
        additional_kwargs={"skill_refs": [card.skill_id]})]}, box)
    assert explicit is not None
    assert explicit["active_skills"][card.skill_id]["source"] == "explicit"


async def test_api_import_csrf_versions_files_export_and_traversal(database):
    with TestClient(create_app()) as client:
        assert client.get("/api/skills").status_code == 401
        ticket = client.post("/api/session").json()
        headers = {"X-Workbench-CSRF": ticket["csrf_token"]}
        body = {"zip_base64": base64.b64encode(encode_package(sample())).decode()}
        assert client.post("/api/skills/import", json=body).status_code == 403
        saved = client.post("/api/skills/import", json=body, headers=headers)
        assert saved.status_code == 200, saved.text
        value = saved.json()
        assert value["enabled"] is False
        base = f"/api/skills/{value['skill_id']}/versions/{value['version_id']}"
        assert "固定的技能指导" in client.get(base + "/files").json()["text"]
        assert client.get(base + "/files?path=../outside").status_code == 422
        assert client.get(base + "/export").content[:2] == b"PK"
        assert client.patch(f"/api/skills/{value['skill_id']}", json={"enabled": False}, headers=headers).status_code == 200
        assert client.get(base + "/files").status_code == 200  # 管理预览仍可用
        invalid = client.post("/api/skills/import", json={"zip_base64": "AA=="}, headers=headers)
        assert invalid.status_code == 422


async def test_api_create_edit_restore_and_archive_preserves_versions(database):
    with TestClient(create_app()) as client:
        ticket = client.post("/api/session").json()
        headers = {"X-Workbench-CSRF": ticket["csrf_token"]}
        created = client.post("/api/skills/create", json={"name": "test-created-skill",
            "description": "Use for local fixture work", "body": "Initial instructions"}, headers=headers)
        assert created.status_code == 200, created.text
        first = created.json()
        assert first["enabled"] is False
        skill_id = first["skill_id"]
        changed = client.put(f"/api/skills/{skill_id}/files", json={"base_version_id": first["version_id"], "text_files": {
            "references/guide.md": "Detailed reference", "scripts/helper.py": "print('fixture')\n"}}, headers=headers)
        assert changed.status_code == 200, changed.text
        second = changed.json()
        assert second["version_id"] != first["version_id"]
        duplicate = client.post("/api/skills/create", json={"name": "test-created-skill",
            "description": "Duplicate fixture", "body": "Must not replace the package"}, headers=headers)
        assert duplicate.status_code == 409
        stale = client.put(f"/api/skills/{skill_id}/files", json={"base_version_id": first["version_id"],
            "text_files": {"references/guide.md": "This edit is stale"}}, headers=headers)
        assert stale.status_code == 409
        assert client.get(f"/api/skills/{skill_id}").json()["version_id"] == second["version_id"]
        assert client.get(f"/api/skills/{skill_id}/versions/{first['version_id']}/files").status_code == 200
        assert client.get(f"/api/skills/{skill_id}/versions/{second['version_id']}/files?path=references/guide.md").json()["text"] == "Detailed reference"
        same = client.put(f"/api/skills/{skill_id}/files", json={"text_files": {
            "references/guide.md": "Detailed reference"}}, headers=headers)
        assert same.json()["version_id"] == second["version_id"]
        restored = client.patch(f"/api/skills/{skill_id}", json={"version_id": first["version_id"]}, headers=headers)
        assert restored.status_code == 200
        assert client.get(f"/api/skills/{skill_id}").json()["version_id"] == first["version_id"]
        archived = client.delete(f"/api/skills/{skill_id}", headers=headers)
        assert archived.status_code == 200
        assert all(item["skill_id"] != skill_id for item in client.get("/api/skills").json()["skills"])
        assert client.get(f"/api/skills/{skill_id}/versions/{first['version_id']}/files").status_code == 200


async def test_corrupt_version_fails_closed(database, unique_name):
    service = SkillService(database)
    card = await service.publish(sample(), f"zip:{unique_name}")
    await service.update(card.skill_id, {"enabled": True})
    async with database.transaction() as db:
        row = (await db.execute(select(SkillVersionRow).where(SkillVersionRow.id == __import__("uuid").UUID(card.version_id)))).scalar_one()
        row.package_hash = "0" * 64
    with pytest.raises(ValueError, match="skill_hash_mismatch"):
        await service.files(card.skill_id)


async def test_main_skill_status_reports_pinned_version_and_unavailable_resources(database, unique_name):
    box = await fixture(database, unique_name)
    service = SkillService(database)
    card = await service.publish(sample(), f"zip:{unique_name}")
    await service.update(card.skill_id, {"enabled": True})
    async with database.session() as db:
        row = await db.get(SessionRow, box.session_id)
        assert row is not None
        thread = row.thread_id
    graph = StateGraph(AgentGraphState)
    def fixture_node(state: AgentGraphState) -> dict:
        return {}

    graph.add_node("fixture", fixture_node)
    graph.add_edge(START, "fixture")
    graph.add_edge("fixture", END)
    async with open_checkpointer_connection(database.settings) as connection:
        compiled = graph.compile(checkpointer=AsyncPostgresSaver(connection))
        await compiled.aupdate_state({"configurable": {"thread_id": thread}}, {"active_skills": {
            card.skill_id: {"skill_id": card.skill_id, "version_id": card.version_id, "package_hash": card.package_hash},
        }})
    status_path = f"/api/sessions/{box.session_id}/skills"
    with TestClient(create_app()) as client:
        client.post("/api/session")
        active = client.get(status_path).json()["active"][0]
        assert active["name"] == card.name and active["version_id"] == card.version_id
        assert active["status"] == "loaded"
        await service.update(card.skill_id, {"enabled": False})
        unavailable = client.get(status_path).json()["active"][0]
        assert unavailable["status"] == "unavailable"


async def test_explicit_unload_is_not_reactivated_and_visibility_is_rechecked(database, unique_name):
    box = await fixture(database, unique_name)
    service = SkillService(database)
    card = await service.publish(sample(), f"zip:{unique_name}")
    await service.update(card.skill_id, {"enabled": True})
    state = {"messages": [HumanMessage(content="使用技能", id="explicit-1",
              additional_kwargs={"skill_refs": [card.skill_id]})]}
    first = await prepare_skill_context(state, box)
    assert first is not None
    assert card.skill_id in first["active_skills"]
    # 同一用户指令已消费；下一轮卸载后不能由旧消息重新激活。
    second = await prepare_skill_context({**state, **first, "active_skills": {card.skill_id: None}}, box)
    assert second is not None
    assert card.skill_id not in second["active_skills"]
    assert len(second["skill_directives"]) <= 2
    await service.update(card.skill_id, {"enabled": False})
    third = await prepare_skill_context({**state, **first}, box)
    assert third is not None
    assert "固定的技能指导" not in box.skill_context
    assert box.skill_manifest[0]["status"] == "unavailable"
    with pytest.raises(ValueError, match="skill_disabled"):
        await service.session_files(box.session_id, card.skill_id, card.version_id)
    assert third["active_skills"][card.skill_id]["version_id"] == card.version_id


async def test_skill_script_uses_pinned_package_and_existing_operation_identity(database, unique_name, tmp_path):
    from workbench_executor.skill_packages import materialize

    box = await fixture(database, unique_name)
    service = SkillService(database)
    original = sample()
    original["assets/marker.txt"] = b"pinned-resource"
    original["rexak.yaml"] = b"schema_version: 1\nrequires:\n  capabilities: [command]\nentrypoints:\n  main:\n    path: scripts/main.py\n    interpreter: python\n"
    card = await service.publish(original, f"zip:{unique_name}")
    await service.update(card.skill_id, {"enabled": True})
    active = {card.skill_id: {"skill_id": card.skill_id, "version_id": card.version_id,
                             "package_hash": card.package_hash}}
    box.skill_manifest = [active[card.skill_id]]
    await service.publish({**sample("新版本"), "assets/marker.txt": b"new-resource"}, f"zip:{unique_name}")
    packages = []
    async def stage(payload):
        packages.append(payload)
        return materialize(SimpleNamespace(execution_mode="trusted", workspace_root=tmp_path,
                          resolved_python_interpreter=__import__("sys").executable), payload)
    box.executor.execution_skill_package = stage
    runtime = cast(Any, SimpleNamespace(context=RunContext(toolbox=box, thread_id=str(box.session_id)),
                        state={"active_skills": active}, tool_call_id="script-once"))
    script = cast(StructuredTool, run_skill_script)
    assert script.coroutine is not None
    first = json.loads(await script.coroutine(runtime=runtime, skill_id=card.skill_id,
                       path="main", argv=["参数含 空格;$(literal)"], stdin_json='{"fixture":true}', background=True))
    second = json.loads(await script.coroutine(runtime=runtime, skill_id=card.skill_id,
                        path="main", argv=["参数含 空格;$(literal)"], stdin_json='{"fixture":true}', background=True))
    assert first["operation_id"] == second["operation_id"]
    assert len(box.executor.resources) == 1
    args = box.executor.envelopes[0]["arguments"]
    assert args["argv"][-1] == "参数含 空格;$(literal)"
    assert args["skill_ref"]["package_hash"] == card.package_hash
    assert args["stdin_text"] == '{"fixture":true}'
    async with database.session() as db:
        operation = await db.get(ToolOperationRow, UUID(first["operation_id"]))
        assert operation.execution_context["explicit_skill"]["version_id"] == card.version_id
        assert operation.execution_context["contextual_skills"][0]["package_hash"] == card.package_hash
    assert (tmp_path / str(box.session_id) / ".rexak" / "skills" / card.package_hash / "assets/marker.txt").read_bytes() == b"pinned-resource"
    assert len(packages) == 3 and "archive_base64" not in packages[-1]


async def test_loading_oversized_body_is_rejected_before_activation(database, unique_name):
    box = await fixture(database, unique_name)
    card = await SkillService(database).publish(sample("很长的指导" * 10000), f"zip:{unique_name}")
    await SkillService(database).update(card.skill_id, {"enabled": True})
    runtime = cast(Any, SimpleNamespace(context=RunContext(toolbox=box, thread_id=str(box.session_id)),
                        state={"active_skills": {}}, tool_call_id="too-large"))
    loader = cast(StructuredTool, load_skill)
    assert loader.coroutine is not None
    with pytest.raises(ValueError, match="skill_context_budget_exceeded"):
        await loader.coroutine(runtime=runtime, skill_id=card.skill_id)
    assert runtime.state["active_skills"] == {}


async def test_run_bundle_contains_pinned_skill_dependencies(database, unique_name, tmp_path):
    from agent_workbench.contracts.skill_package import decode_package
    from agent_workbench.maintenance.run_bundle import (
        collect_run_bundle,
        verify_run_bundle,
        write_run_bundle,
    )

    box = await fixture(database, unique_name)
    service = SkillService(database)
    original = sample()
    first = await service.publish(original, f"zip:{unique_name}")
    await service.update(first.skill_id, {"enabled": True})
    await service.catalog(box.run_id)
    newer = await service.publish(sample("后续发布版本"), f"zip:{unique_name}")
    payload = await collect_run_bundle(database, box.run_id)
    assert not payload["skills"]["errors"]
    versions = {item["version_id"] for item in payload["skills"]["versions"]}
    assert first.version_id in versions and newer.version_id not in versions
    path = write_run_bundle(tmp_path / "private-bundle", payload, service.store)
    assert verify_run_bundle(path) == []
    index = json.loads((path / "skills/index.json").read_text(encoding="utf-8"))
    first_version = next(item for item in index["versions"] if item["version_id"] == first.version_id)
    assert decode_package((path / "skills/packages" / f"{first_version['artifact_id']}.zip").read_bytes()) == original


async def test_main_rejects_loading_a_version_outside_run_snapshot(database, unique_name):
    box = await fixture(database, unique_name)
    service = SkillService(database)
    first = await service.publish(sample(), f"zip:{unique_name}")
    await service.update(first.skill_id, {"enabled": True})
    await service.catalog(box.run_id)
    newer = await service.publish(sample("后来发布的版本"), f"zip:{unique_name}")
    runtime = cast(Any, SimpleNamespace(context=RunContext(toolbox=box, thread_id=str(box.session_id)),
                        state={"active_skills": {}}, tool_call_id="wrong-version"))
    loader = cast(StructuredTool, load_skill)
    assert loader.coroutine is not None
    with pytest.raises(ValueError, match="skill_version_not_in_run"):
        await loader.coroutine(runtime=runtime, skill_id=first.skill_id, version_id=newer.version_id)
