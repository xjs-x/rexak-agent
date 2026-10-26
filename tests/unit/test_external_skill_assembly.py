"""默认图与原工作方式独立；外部 Skill 是显式启用的扩展。"""

from agent_workbench.agent.factory import build_agent_middleware, checkpoint_needs_skill_bridge
from agent_workbench.agent.middleware.skills import SkillsMiddleware
from agent_workbench.agent.prompts.system import SYSTEM_PROMPT
from agent_workbench.config.catalog import default_price_catalog
from agent_workbench.config.settings import get_settings
from agent_workbench.models.provider import ModelBinding
from agent_workbench.tools.registry import ToolSelection, build_tools
from tests.support.scripted_model import ScriptedChatModel, final_text
from tests.unit.test_context_budget import profile


def test_default_tools_and_middleware_do_not_enable_external_extension():
    assert not checkpoint_needs_skill_bridge({"channel_values": {"messages": [], "active_skills": {}},
        "versions_seen": {"model": {}}, "channel_versions": {"messages": "1"}})
    selection = ToolSelection()
    tools = build_tools(selection)
    names = {item.name for item in tools}
    assert "search_methods" not in names  # 盲测默认关闭未分类的跨任务方法库
    assert "search_methods" in {item.name for item in build_tools(ToolSelection(knowledge=True))}
    assert not {"load_skill", "search_skills", "read_skill_resource", "run_skill_script", "unload_skill"} & names
    assert "skills" not in selection.to_manifest()
    assert not any("skill" in name for name in selection.disabled_tools())
    middleware = build_agent_middleware(ScriptedChatModel(responses=[final_text("你好")]),
        ModelBinding(profile()), get_settings(), tools, default_price_catalog())
    assert not any(isinstance(item, SkillsMiddleware) for item in middleware)
    assert not any(name in SYSTEM_PROMPT for name in ("search_skills", "load_skill", "run_skill_script"))


def test_external_skills_are_independent_of_host_methods_and_keep_command_gate():
    tools = build_tools(ToolSelection(knowledge=False, skills=True, command=False))
    names = {item.name for item in tools}
    assert "search_methods" not in names and "load_skill" in names
    assert "run_skill_script" not in names
    assert "run_skill_script" in ToolSelection(skills=True, command=False).disabled_tools()
