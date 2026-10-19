"""外部方法论扩展的边界：空库存原循环、宿主方法隔离和旧图继续。"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select

from agent_workbench.agent.factory import checkpoint_needs_skill_bridge
from agent_workbench.agent.middleware.skills import SkillsMiddleware, prepare_skill_context
from agent_workbench.agent.state import AgentGraphState
from agent_workbench.api.app import create_app
from agent_workbench.persistence.tables import ArtifactRow, RunRow, SkillInstallationRow
from agent_workbench.skills.service import SkillService
from tests.integration.test_skills import fixture
from tests.support.scripted_model import ScriptedChatModel, final_text, tool_call
from tests.unit.test_skill_packages import sample

pytestmark = pytest.mark.integration


async def test_library_get_never_imports_and_explicit_refresh_never_publishes_methods(
    database, unique_name, tmp_path, monkeypatch,
):
    monkeypatch.setattr(database.settings, "owner_id", unique_name)
    monkeypatch.setattr(database.settings, "skills_root", tmp_path / "external")
    service = SkillService(database)
    source = service.root / "fixture-skill"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_bytes(sample()["SKILL.md"])
    async with database.session() as db:
        artifacts_before = (await db.execute(select(func.count()).select_from(ArtifactRow))).scalar_one()
        versions_before = (await db.execute(select(func.count()).select_from(SkillInstallationRow))).scalar_one()
    with TestClient(create_app()) as client:
        ticket = client.post("/api/session").json()
        headers = {"X-Workbench-CSRF": ticket["csrf_token"]}
        for _ in range(2):
            response = client.get("/api/skills")
            assert response.status_code == 200 and response.json()["skills"] == []
        async with database.session() as db:
            assert (await db.execute(select(func.count()).select_from(ArtifactRow))).scalar_one() == artifacts_before
            assert (await db.execute(select(func.count()).select_from(SkillInstallationRow))).scalar_one() == versions_before
        for _ in range(2):
            response = client.post("/api/skills/rescan", headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["total"] == 1
            assert response.json()["skills"][0]["source"] == "user:fixture-skill"
    async with database.session() as db:
        rows = (await db.execute(select(SkillInstallationRow).where(
            SkillInstallationRow.owner_id == unique_name,
        ))).scalars().all()
        assert len(rows) == 1 and rows[0].source == "user:fixture-skill"


async def test_empty_external_inventory_does_not_scan_publish_or_write_run_state(database, unique_name, tmp_path):
    box = await fixture(database, unique_name)
    settings = database.settings.model_copy(update={"owner_id": unique_name, "skills_root": tmp_path / "user"})
    box.settings = settings
    service = SkillService(database, settings)
    source = service.root / "fixture-skill"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_bytes(sample()["SKILL.md"])
    async with database.session() as db:
        before = dict((await db.get(RunRow, box.run_id)).config_snapshot)
        artifacts = (await db.execute(select(func.count()).select_from(ArtifactRow))).scalar_one()
    assert await service.catalog(box.run_id) == []  # 源目录有包也不能暗中导入。
    assert await prepare_skill_context({}, box) is None
    assert box.skill_context == "" and box.skill_manifest == []
    async with database.session() as db:
        assert (await db.get(RunRow, box.run_id)).config_snapshot == before
        assert (await db.execute(select(func.count()).select_from(ArtifactRow))).scalar_one() == artifacts
    rows, errors = await service.rescan()  # 用户显式刷新后才能成为外部扩展。
    assert not errors and len(rows) == 1 and rows[0].source == "user:fixture-skill"
    box.skill_catalog = None
    assert len(await service.catalog(box.run_id)) == 1


async def test_legacy_builtin_is_not_discoverable_executable_or_reenabled(database, unique_name, tmp_path):
    box = await fixture(database, unique_name)
    settings = database.settings.model_copy(update={"owner_id": unique_name, "skills_root": tmp_path / "user"})
    box.settings = settings
    service = SkillService(database, settings)
    card = await service.publish(sample(), "zip:legacy-fixture")
    async with database.transaction() as db:
        row = await db.get(SkillInstallationRow, UUID(card.skill_id))
        row.source = "builtin:work-records"
        row.enabled = True  # 即使旧配置尚未迁移，也不能重新进入运行。
    assert await service.list() == [] and await service.catalog(box.run_id) == []
    assert await service.rescan() == ([], [])
    with pytest.raises(ValueError, match="skill_retired"):
        await service.files(card.skill_id)
    with pytest.raises(ValueError, match="skill_retired"):
        await service.update(card.skill_id, {"enabled": True})
    with pytest.raises(ValueError, match="skill_source_denied"):
        await service.publish(sample(), "builtin:new-copy")
    old, files, _spec = await service.files(card.skill_id, card.version_id, enforce_enabled=False)
    assert old.version_id == card.version_id and files == sample()  # 历史原件保留。
    refs = {card.skill_id: {"version_id": card.version_id, "package_hash": card.package_hash, "source": "model"}}
    update = await prepare_skill_context({"active_skills": refs}, box)
    assert update == {"active_skills": {card.skill_id: None}}
    assert box.skill_context == "" and not box.skill_manifest
    # 旧指令/初始配置引用 builtin 时，不应阻塞后来用户接入的真正外部方法。
    external = await service.publish(sample("外部第三方方法"), "zip:external-reference")
    async with database.transaction() as db:
        run = await db.get(RunRow, box.run_id)
        run.config_snapshot = {**run.config_snapshot, "initial_skill_refs": [card.skill_id, external.skill_id]}
    box.skill_catalog = None
    result = await prepare_skill_context({"messages": [HumanMessage(content="原指令", id="legacy-choice",
        additional_kwargs={"skill_refs": [card.skill_id]})]}, box)
    assert result is not None and result["active_skills"][card.skill_id] is None
    assert result["active_skills"][external.skill_id]["source"] == "explicit"
    assert "外部第三方方法" in box.skill_context and "固定的技能指导" not in box.skill_context


async def test_old_pending_skill_hook_resumes_through_inert_bridge_without_tool_replay():
    calls = []
    @tool
    def record_once(key: str) -> str:
        """无害的本地计数器。"""
        calls.append(key)
        return key
    memory = InMemorySaver()
    model = ScriptedChatModel(responses=[tool_call("record_once", {"key": "once"}, "op-original"), final_text("继续原任务")])
    config = cast(Any, {"configurable": {"thread_id": str(uuid4())}})
    old = create_agent(model=model, tools=[record_once], middleware=[SkillsMiddleware()],
        state_schema=AgentGraphState, checkpointer=memory, interrupt_before=["SkillsMiddleware.before_model"])
    await old.ainvoke(cast(Any, {"messages": [HumanMessage(content="原目标", id="original-user")]}), config)
    before = await old.aget_state(config)
    assert before.next == ("SkillsMiddleware.before_model",) and calls == []
    saved = await memory.aget_tuple(config)
    assert saved is not None and checkpoint_needs_skill_bridge(dict(saved.checkpoint))
    new = create_agent(model=model, tools=[record_once], middleware=[SkillsMiddleware(enabled=False)],
        state_schema=AgentGraphState, checkpointer=memory)
    result = await new.ainvoke(None, config)
    assert calls == ["once"] and result["messages"][0].id == "original-user"


async def test_inert_bridge_has_no_skill_database_dependency_or_state_update():
    class ForbiddenDatabase:
        def session(self):
            raise AssertionError("空扩展不能在每轮访问 Skill 数据库")
    box = SimpleNamespace(db=ForbiddenDatabase(), skill_context="", skill_manifest=[])
    state = {"messages": [], "plan": [{"step_id": "original", "status": "in_progress"}]}
    before = copy.deepcopy(state)
    assert await SkillsMiddleware(enabled=False).abefore_model(cast(Any, state), cast(Any, SimpleNamespace(
        context=SimpleNamespace(toolbox=box)))) is None
    assert state == before and box.skill_context == ""
