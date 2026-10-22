"""生产可见合同与退出路径；历史适配字段不构成新任务义务。"""

from typing import Any, cast
from uuid import uuid4

from agent_workbench.agent.factory import build_agent_middleware
from agent_workbench.agent.state import new_graph_state
from agent_workbench.config.catalog import default_price_catalog
from agent_workbench.config.settings import Settings
from agent_workbench.models.provider import ModelBinding
from agent_workbench.tools.completion import propose_completion
from agent_workbench.tools.registry import build_tools, default_selection
from agent_workbench.tools.task import record_lead, record_observation, update_plan
from tests.support.scripted_model import ScriptedChatModel, final_text
from tests.unit.test_context_budget import profile


def test_model_schemas_have_no_assessment_or_completion_proof():
    plan = cast(Any, update_plan.tool_call_schema).model_json_schema()
    assert plan["required"] == ["expected_plan_revision"]
    assert set(plan["properties"]) == {"expected_plan_revision", "steps", "focus", "rationale"}
    assert set(plan["$defs"]["PlanStepPatch"]["properties"]) == {
        "step_id",
        "description",
        "status",
        "rationale",
        "lead_id",
    }
    assert cast(Any, propose_completion.tool_call_schema).model_json_schema()["required"] == [
        "goal_answer"
    ]
    for item in (record_lead, record_observation):
        properties = cast(Any, item.tool_call_schema).model_json_schema()["properties"]
        assert not (
            {"assessment_json", "actionability_json", "coverage_status"} & properties.keys()
        )


def test_new_state_and_default_graph_do_not_activate_legacy_protocols():
    state = new_graph_state(session_id=uuid4(), run_id=uuid4(), objective="本地测试")
    assert state.get("state_schema_version") == 7
    assert not (
        {"work_assessment", "coverage_revision", "progress_feedback", "report_retry_key"}
        & state.keys()
    )
    selected = default_selection(Settings())
    tools = build_tools(selected)
    assert not selected.skills and not selected.verification
    assert not (
        {"record_weakness", "verify_finding", "load_skill", "verify_public_git_head"}
        & {t.name for t in tools}
    )
    middleware = build_agent_middleware(
        ScriptedChatModel(responses=[final_text("完成")]),
        ModelBinding(profile=profile(), request_timeout_seconds=30),
        Settings(),
        tools,
        default_price_catalog(),
    )
    names = {type(item).__name__ for item in middleware}
    assert not ({"ProgressMiddleware", "StageReportMiddleware", "SkillsMiddleware"} & names)
