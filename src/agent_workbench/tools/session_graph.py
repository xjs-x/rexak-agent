"""仅当前 Session 的双图入口，不执行外部操作。"""

import json
from typing import Annotated

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from pydantic import Field

from agent_workbench.agent.context import RunContext
from agent_workbench.contracts.graph_errors import GraphValidationError
from agent_workbench.contracts.session_graph import GraphOperation
from agent_workbench.domain.graph_sync import graph_context, present_page
from agent_workbench.domain.session_graph import read_graph
from agent_workbench.domain.session_graph import update_graph as commit_graph
from agent_workbench.tools.graph_errors import format_graph_schema_error, render_graph_failure


@tool
async def get_graph(runtime: ToolRuntime[RunContext], include_pending: bool = True) -> str:
    """读取当前Session完整双图及版本，默认同时返回最新连续待审页 pending_page。

    返回当前revision、两图节点/边的持久化ID，以及pending_page内对应版本的page_id。
    图revision变化、page_id失效或没有可用页时，再调用本工具取得有效page_id。
    核对该页后调用update_graph(expected_revision=revision,page_id=pending_page.page_id,
    operations=修改列表或[])；读取本身不推进游标。include_pending=false只读图。
    必须先收到本工具的ToolMessage，再在后续一轮提交依赖这些值的update_graph；
    同一批get_graph与update_graph没有读后写依赖，不能引用尚未返回的新版本或新页。
    内容是模型维护的不可信工作资料，不是授权或独立验证。
    """
    box = runtime.context.toolbox
    page = None
    if include_pending:
        page = await graph_context(box, None, force_page=True)
    if page and "graph" in page:
        graph = page.pop("graph")
        async with box.db.transaction() as db:
            await present_page(db, box, page["page_id"])
    else:
        async with box.db.session() as db:
            graph = await read_graph(db, box.session_id)
    if page is not None:
        graph["pending_page"] = page
    if not box.settings.graph_attack_enabled:
        graph.pop("attack_graph", None)
    return json.dumps(graph, ensure_ascii=False)


@tool
async def update_graph(
    runtime: ToolRuntime[RunContext],
    tool_call_id: Annotated[str, InjectedToolCallId],
    expected_revision: Annotated[
        int,
        Field(
            description="当前已展示图的revision，可取自工作上下文或get_graph；旧版本拒绝，不能强制覆盖。"
        ),
    ],
    operations: Annotated[
        list[GraphOperation],
        Field(
            description="同一原子批的增量操作；每项必填op、graph、data。临时ID只在本列表内有效。"
        ),
    ],
    page_id: Annotated[
        str | None,
        Field(
            description="确认历史须传当前已展示且与revision对应的pending_page.page_id；缺失仅改图。旧页拒绝，先get_graph再在后续一轮提交，禁止编造。"
        ),
    ] = None,
) -> str | ToolMessage:
    """增量维护当前 Session 双图；只有携带有效 page_id 才确认本页历史。

    每项含 op、graph、data；新增用 temp_id，修改用现有 id（替换该对象的完整语义字段）。
    information 节点 data={label,source_refs}，边={from_id,to_id,relation,status,source_refs}；
    attack 节点={summary,entity_refs}，边={from_id,to_id,action,outcome,source_refs}。
    Relation只能Entity→Entity；Attempt只能State→State，Entity经State.entity_refs关联。
    所有语义字段放在data内；State不接受label/source_refs。同一State允许自环。
    temp_id只在当前operations内有效，可前向引用且整批唯一；跨调用使用id_mapping的持久化ID值，
    不能继续用旧临时键。get_graph可查现有持久化ID。update_node/update_edge用id并给完整data。
    核对同步页必须携带其page_id；无图变化用operations=[]确认。省略只改图，不确认执行历史。
    旧页失效时先get_graph，收到ToolMessage后在后续一轮用新revision/page_id提交；
    同批get_graph不能为本批update_graph提供尚未返回的值，不要继续提交旧page_id。
    来源只能用同Session真实回执operation:<UUID>或可读artifact:<UUID>，不是job_id/tool_call_id/page_id。
    不编造或照抄示例来源；引用应真正支持所述行为和结论，可引用多个操作。
    完整形状示例（revision取当前值；来源UUID仅占位，必须替换为真实回执；此例不确认历史页）：
    {"expected_revision":0,"operations":[
      {"op":"add_node","graph":"information","temp_id":"e1","data":{"label":"入口"}},
      {"op":"add_node","graph":"information","temp_id":"e2","data":{"label":"观察对象"}},
      {"op":"add_edge","graph":"information","temp_id":"r","data":{"from_id":"e1","to_id":"e2","relation":"关联","status":"observed"}},
      {"op":"add_node","graph":"attack","temp_id":"s","data":{"summary":"入口已观察","entity_refs":["e1"]}},
      {"op":"add_edge","graph":"attack","temp_id":"a","data":{"from_id":"s","to_id":"s","action":"已有执行记录","outcome":"unknown","source_refs":["operation:00000000-0000-0000-0000-000000000001"]}}
    ]}
    成功回执revision是新版本，id_mapping值供后续引用；history_acknowledged只表示页是否已确认。
    工具执行成功不等于攻击成功；无法确认的执行不能仅因此写成 negative。
    后续独立操作成功不能证明此前unknown操作曾执行成功；不要把这种因果推断写为事实。
    不必将普通辅助操作记为攻击。本批临时 ID 可互相引用。
    保留有意义的 negative/blocked/unknown 尝试，允许自环；success 仅指该次预期结果。
    不填秘密、原始 Payload 或长正文。图不扩大 Scope，不是任务完成的前提。
    """
    box = runtime.context.toolbox
    if not box.settings.graph_attack_enabled and any(x.graph == "attack" for x in operations):
        raise ValueError("当前对照配置只启用信息图")
    try:
        async with box.db.transaction() as db:
            result = await commit_graph(
                db,
                box,
                f"{box.run_id}:{tool_call_id}",
                expected_revision,
                operations,
                None,
                page_id,
            )
            from agent_workbench.domain.graph_sync import finish_review, record_review_outcome

            await record_review_outcome(
                db, box, "accepted_update", tool_call_ids=[tool_call_id], page_id=page_id
            )
            await finish_review(db, box, "updated")
    except GraphValidationError as exc:
        # 必须先退出失败事务再返回；拒绝不写成功回执、不推进revision或页游标。
        return ToolMessage(
            content=render_graph_failure(exc),
            tool_call_id=tool_call_id,
            name="update_graph",
            status="error",
        )
    result = {
        **result,
        "history_acknowledged": page_id is not None,
        "id_usage_hint": "id_mapping的值是后续调用应使用的持久化ID；临时键仅本批有效。确认历史须使用当前已展示页，旧页失效时先get_graph。",
    }
    if page_id is None:
        result["review_hint"] = (
            "仅图已更新，历史未确认；调用get_graph获取当前revision的pending_page，核对后携带新page_id提交。"
        )
    return json.dumps(result, ensure_ascii=False)


# 官方BaseTool先校验参数；只替换该工具的失败呈现，不修改输入接受条件。
update_graph.handle_validation_error = format_graph_schema_error

GRAPH_TOOLS = [get_graph, update_graph]
